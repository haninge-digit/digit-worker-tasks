from multiprocessing import current_process
import os
import traceback
import json
import logging
import asyncio
import uuid
import re

from datetime import datetime
import time

import httpx
import grpc
from zeebe_grpc import gateway_pb2_grpc
from zeebe_grpc.gateway_pb2 import (
    ActivateJobsRequest,
    CompleteJobRequest)

""" 
Environment
"""
ZEEBE_ADDRESS = os.getenv('ZEEBE_ADDRESS',"camunda-zeebe-gateway.camunda-zeebe:26500")
USERINFOCASH = os.getenv('USERINFOCASH',"userinfocash:8080") # This is the default

USER_TASK_RENEWAL_TIME = 5*60       # If a task hasn't been "renewed" with two minutes, it's assumed it has been deleted "out of bands"


"""
This is the Tasks worker class.
It's a special worker that handles and manages all user tasks

The API i described in (the non existent) tasks_api.yaml

Input header variables are the basic set (documented elseware...)
"""


class Tasks(object):

    queue_name = "tasks"        # Name of the Zeebe task queue. Will also influence the worker process ID and name


    """
    Init function. Start an async poller that periodically looks for new versions of the tasks file.
    """
    def __init__(self):
        self._active_tasks = {}                 # Holds all active user tasks. Task id (job.key) is the key
        # async_loop = asyncio.get_running_loop()
        self._collect_coroutine = asyncio.create_task(self._collect_tasks())       # Create the task collector

        # self._zeebe_channel = grpc.aio.insecure_channel(ZEEBE_ADDRESS)
        # self._zeebe_stub = gateway_pb2_grpc.GatewayStub(self._zeebe_channel)

    async def worker(self, vars):
        stand_alone = '_STANDALONE' in vars     # This worker should always be "stand alone"

        userid = vars.get('userid')
        if not userid:
            return {'_DIGIT_ERROR':"Missing mandatory variable 'userid'"}

        if re.match("^\S{6}\d{2}$", userid):    # If an internal user. Get all groups to validate admin access
            async with httpx.AsyncClient(timeout=10, verify=False) as client:       # Get all groups the user belongs to
                try:
                    r = await client.get(f"http://{USERINFOCASH}/userinfo/{userid}")
                except httpx.ConnectError:
                    return {'_DIGIT_ERROR':f"Couldn't connect to {USERINFOCASH} server"}
                if r.status_code != 200:
                    return {'_DIGIT_ERROR':f"GET userinfocash returned status code {r.status_code}"}
            userinfo = r.json()
            usergroups = userinfo.get('userGroups',[])
        else:
            usergroups = []

        task_key = vars.get('taskKey')
        if vars['_HTTP_METHOD'] == "GET":
            if not task_key:             # A list of all tasks that the user has access to, is requested
                task_id = vars.get('task_id')          # Filter on specifik tasks
                workflow_id = vars.get('workflow_id')  # and/or specific workflows
                role_type = vars.get('role_type')      # and/or specific role

                found_tasks = []
                for key, taskitem in self._active_tasks.items():
                    task = taskitem['task']
                    role = self._get_role(task, userid, usergroups)
                    if (role and 
                        (not role_type or role_type == role) and 
                        (not task_id or task_id == task['task_id']) and 
                        (not workflow_id or workflow_id == task['workflow_id'])):
                        task_info = {
                            'taskKey': key,
                            'workflowId': task['workflow_id'],
                            'usertaskId': task['usertask_id'],
                            'processInstance': str(task['process_instance']),
                            'created': task['created'],
                            'role':role
                        }
                        found_tasks.append(task_info)     # Add tasks that matches

                return {'tasks': found_tasks}    # Return the list. Can be empty.

            else:    # A taskKey is given. Return all information about that specific task
                if task_key not in self._active_tasks:
                    return {'_DIGIT_ERROR': f"Task with key {task_key} not found!"}
                task = self._active_tasks[task_key]['task']
                role = self._get_role(task, userid, usergroups)
                if not role:        # No access allowed!
                    return {'_DIGIT_ERROR': f"User {userid} can't retrieve tasks assigned to {self._active_tasks[task_key]['assignee']}"}
                task_info = {
                    'taskKey': task_key,
                    'workflowId': task['workflow_id'],
                    'usertaskId': task['usertask_id'],
                    'processInstance': str(task['process_instance']),
                    'created': task['created'],
                    'assignee': task['assignee'],
                    'originator': task['originator'],
                    'adminGroups': task['admin_groups'],
                    'role': role,
                    # 'taskVariables': task['task_variables'],
                    'workflowVariables': task['workflow_variables']
                }
                return {'taskInfo': task_info}                    # Return information about a specific task.

        # POST method completes the requested task with potential updated variables
        if not task_key:
            return {'_DIGIT_ERROR': f"Post task must have a task_key parameter!"}
        if task_key not in self._active_tasks:
            return {'_DIGIT_ERROR': f"Task with key {task_key} not found!"}
        if task_key not in self._active_tasks[task_key]['task']['assignee'] != userid:
            return {'_DIGIT_ERROR': f"User {userid} can't complete tasks assigned to {self._active_tasks[task_key]['task']['assignee']}"}

        add_vars = vars['_JSON_BODY'] if '_JSON_BODY' in vars else '{}' # New variables to add to flow?
        async with grpc.aio.insecure_channel(ZEEBE_ADDRESS) as channel:
            stub = gateway_pb2_grpc.GatewayStub(channel)
            try:
                cjr = CompleteJobRequest(jobKey=int(task_key), variables=add_vars)   # Complete task with possibly added variables
                await stub.CompleteJob(cjr)     # Do it!!!
                logging.debug(f"Task {task_key} completed by {userid}")
                if task_key in self._active_tasks:
                    del self._active_tasks[task_key]      # Delete it from active task list.
            except grpc.aio.AioRpcError as grpc_error:
                logging.fatal(f"Zeebe returned unexpected error: {grpc_error.code()}")

        return {"status": f"User task {task_key} assigned to {userid} completed!"}

    def _get_role(self,task, userid, usergroups):
        if task['assignee'] == userid:
            return "assignee"
        elif task['originator'] == userid:
            return "originator"
        # elif not set(task['admin_groups']).isdisjoint(usergroups):
        elif any(group in usergroups for group in task['admin_groups']):    # Does the user belong to any of the admin groups?
            return "admin"
        else:
            return ""        

    """
    Asynchronous task that periodically collects active tasks from Camunda
    """
    async def _collect_tasks(self):
        worker_id = str(uuid.uuid4().time_low)  # Random worker ID
        logging.info(f"Started to collect user tasks with worker {worker_id}")

        topic = "io.camunda.zeebe:userTask"     # Worker topic for all BPMN user tasks in Zeebe
        locktime = 1*60*1000                    # A too long time will create a delay on restarts (when task status is lost)
        max_poll_time = 2*60*1000               # Probaly "lagom". If less than locktime, poll will return after lock expires
        max_jobcnt = 10000                      # Can't be too many?
        ajr = ActivateJobsRequest(type=topic, worker=worker_id,
                                  timeout=locktime,
                                  maxJobsToActivate=max_jobcnt,
                                  requestTimeout=max_poll_time)    # Get user tasks request

        async with grpc.aio.insecure_channel(ZEEBE_ADDRESS) as channel:
            stub = gateway_pb2_grpc.GatewayStub(channel)
            try:
                while (True):
                    logging.debug(f"Looking for new tasks")

                    async for response in stub.ActivateJobs(ajr):   # Get all active user tasks
                        logging.debug(f"Got {len(response.jobs)} user tasks to evaluate")
                        current_time = int(time.time())      # Used for timestamping
                        for job in response.jobs:   # Loop through all returned user tasks
                            taskitem = self._active_tasks.get(str(job.key))     # Check if task i known
                            if taskitem:      # Yes
                                assignee = taskitem['task']['assignee']
                                self._active_tasks[str(job.key)]['timestamp'] = current_time           # Update timestamp
                                logging.debug(f"Found existing task {job.key} that is assigned to {assignee}")
                            else:              # No. Create it
                                task = {
                                    'process_instance': job.processInstanceKey,
                                    'workflow_id': job.bpmnProcessId,
                                    'usertask_id': job.elementId,
                                    'created':  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    'task_variables': json.loads(job.customHeaders),
                                }
                                task['assignee'] = task['task_variables'].get('io.camunda.zeebe:assignee')      # ToDo: Check if empty = fatal error!
                                candidate_groups = task['task_variables'].get('io.camunda.zeebe:candidateGroups')
                                task['admin_groups'] = json.loads(candidate_groups) if candidate_groups else []     # Can be empty = No admin groups...
                                task['workflow_variables'] = {}
                                workflow_variables = json.loads(job.variables)
                                task['originator'] = workflow_variables.get('userid',"")        # Set userid as originator
                                for key, value in workflow_variables.items():
                                    if key == '_JSON_BODY':
                                        jb = json.loads(value)
                                        task['workflow_variables']['_JSON_BODY'] = {k:v for k,v in jb.items()}     # Unpack JSON body
                                    elif key[0] == '_':
                                        pass                # Skip other internal variables
                                    else:
                                        task['workflow_variables'][key] = value
                                self._active_tasks[str(job.key)] = {    # Add task to the active_tasks list. Task key (a string) is the key
                                    'timestamp': current_time,      # Add timestamp
                                    'task': task }                  
                                logging.debug(f"New task {job.key} is assigned to {task['assignee']}")

                        logging.debug(f"Have {len(self._active_tasks)} active tasks in list.")
                    
                    current_time = int(time.time())
                    for task_key in list(self._active_tasks.keys()):     # Check task list for tasks that have "disappeared" (probably deleted in Operator?)
                        if self._active_tasks[task_key]['timestamp'] < current_time - USER_TASK_RENEWAL_TIME:       # Too old?
                            del self._active_tasks[task_key]                # Yes. Delete it
                            logging.error(f"Task with key {task_key} has disappeared?")

            except grpc.aio.AioRpcError as grpc_error:
                logging.fatal(f"Zeebe returned unexpected error: {grpc_error.code()}")
            except Exception as e:      # Catch the rest # noqa: F841
                logging.fatal(traceback.format_exc(limit=2))

        # logging.info("collect_tasks stopped!")
