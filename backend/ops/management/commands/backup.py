from django.core.management.base import BaseCommand
from django.utils import timezone

from ops import backups


class Command(BaseCommand):
    help = ("Takes a backup now, whatever the schedule, and prints its name. deploy/restore.sh runs it before it restores. "
            "Removes none: the nightly job does.")

    def handle(self, *args, **options):
        self.stdout.write(backups.take(timezone.now()).name)
