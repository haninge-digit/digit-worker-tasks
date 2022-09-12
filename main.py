import os
import logging
import asyncio

from Tasks import Tasks


"""
Environment
"""
RUN_ZEEBE_LOOP = os.getenv('RUN_ZEEBE_LOOP',"true") == "true"
RUN_HTTP_SERVER = os.getenv('RUN_HTTP_SERVER',"false") == "true"

DEBUG_MODE = os.getenv('DEBUG',"false") == "true"                       # Global DEBUG logging
LOGFORMAT = "%(asctime)s %(funcName)-10s [%(levelname)s] %(message)s"   # Log format


"""
MAIN function (starting point)
"""
def main():
    # Enable logging. INFO is default. DEBUG if requested
    logging.basicConfig(level=logging.DEBUG if DEBUG_MODE else logging.INFO, format=LOGFORMAT)

    loop = asyncio.new_event_loop()         # Create an async loop

    worker = Tasks(loop)           # Create an instance of the worker

    if RUN_ZEEBE_LOOP:
        from zeebe_worker import worker_loop
        zeebe_runner = loop.create_task(worker_loop(worker))       # Create Zeebe worker loop

    if RUN_HTTP_SERVER:
        from http_server import http_server
        http_runner = loop.create_task(http_server(worker))        # Create http server

    try:
        asyncio.set_event_loop(loop)        # Make the create loop the event loop
        loop.run_forever()                  # And run everything
    except (KeyboardInterrupt):             # Until somebody hits wants to terminate
        pass
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()



if __name__ == "__main__":
    main()
