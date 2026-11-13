"""gunicorn's settings beside its command line in deploy/compose.yaml: its own lines and one line a request, as JSON on
standard output like everything else. The application's loggers are Django's `LOGGING`."""
_TO_STDOUT = {"class": "logging.StreamHandler", "stream": "ext://sys.stdout"}

logconfig_dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "solver_service.logs.JsonFormatter"},
                   "access": {"()": "solver_service.logs.AccessFormatter"}},
    "handlers": {"stdout": _TO_STDOUT | {"formatter": "json"}, "access": _TO_STDOUT | {"formatter": "access"}},
    # gunicorn lays this over its own defaults, whose root names a handler this replaces. Django's `LOGGING` takes the root
    # over in each worker process.
    "root": {"handlers": ["stdout"], "level": "INFO"},
    "loggers": {"gunicorn.error": {"handlers": ["stdout"], "level": "INFO", "propagate": False},
                "gunicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False}},
}


def post_worker_init(worker):
    """gunicorn listens before a worker has loaded the application, which takes seconds (the solver's model and its imports):
    a request sent meanwhile waits in the socket's queue. This line says the worker now answers."""
    worker.log.info("Worker ready (pid: %s)", worker.pid)
