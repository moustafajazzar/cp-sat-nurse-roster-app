from django.core.management.base import BaseCommand

from notifications import push_keys


class Command(BaseCommand):
    help = ("Replaces the installation's push key pair, after a compromise. Deletes no push subscription: each device "
            "re-subscribes with the new key at its next start of the app, without asking its user again. Audited, from the "
            "server.")

    def handle(self, *args, **options):
        push_keys.rotate()
        self.stdout.write("Replaced the push key pair; each device re-subscribes at its next start of the app.")
