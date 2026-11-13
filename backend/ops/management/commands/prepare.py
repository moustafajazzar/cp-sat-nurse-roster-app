import io
import logging

from django.core.management import call_command
from django.core.management.base import BaseCommand

logger = logging.getLogger("ops.prepare")


class _EachLineLogged(io.TextIOBase):
    """What a command prints, as one log line each, so a container's output stays JSON."""

    def write(self, text: str) -> int:
        for line in text.splitlines():
            if line.strip():
                logger.info(line.strip())
        return len(text)


class Command(BaseCommand):
    help = "What `web` does before it serves: the migrations, then the first start's seeding (`bootstrap`), both logged."

    def handle(self, *args, **options):
        call_command("migrate", interactive=False, stdout=_EachLineLogged(), no_color=True)
        call_command("bootstrap", stdout=_EachLineLogged())
