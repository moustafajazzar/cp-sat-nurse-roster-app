import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import Role, User
from audit.log import record
from hospital.catalogue import seed_catalogue
from hospital.models import HospitalSettings
from notifications import push_keys

USERNAME = "admin"


class Command(BaseCommand):
    help = ("First start: seeds the hospital rules and the catalogue, makes the push key pair, and creates the System Admin "
            "from INITIAL_ADMIN_PASSWORD.")

    def handle(self, *args, **options):
        # Before the return below: an installation may predate these.
        HospitalSettings.load()
        seed_catalogue()
        if push_keys.ensure():
            self.stdout.write("Made the installation's push key pair.")
        if User.objects.filter(role=Role.SYSTEM_ADMIN).exists():
            return
        password = os.environ.get("INITIAL_ADMIN_PASSWORD", "")
        if not password:
            raise CommandError("INITIAL_ADMIN_PASSWORD is not set and there is no System Admin yet.")
        with transaction.atomic():
            user = User.objects.create_user(USERNAME, password, Role.SYSTEM_ADMIN, full_name="System Admin",
                                            must_change_password=True)
            record("user.created", target=user.username,
                   after={"username": user.username, "role": user.role, "must_change_password": True})
        self.stdout.write(f"Created the System Admin '{USERNAME}'; the password must change at first login.")
