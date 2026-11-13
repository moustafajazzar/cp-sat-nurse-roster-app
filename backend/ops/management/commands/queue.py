import logging

from procrastinate import cli
from procrastinate.contrib.django.management.commands.procrastinate import Command as ProcrastinateCommand

logger = logging.getLogger("procrastinate.cli")


def _log(*args) -> None:
    logger.info(" ".join(str(arg) for arg in args))


class Command(ProcrastinateCommand):
    """`manage.py procrastinate`, as the worker container runs it (`queue worker --concurrency 4`), but what the library prints
    to standard error at a start, a stop and a `defer` is logged instead, so the container's output stays JSON."""

    help = "Procrastinate's commands, with what they print logged as JSON."

    def handle(self, *args, **kwargs):
        cli.print_stderr = _log
        return super().handle(*args, **kwargs)
