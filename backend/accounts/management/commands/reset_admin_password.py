from django.core.management.base import BaseCommand, CommandError

from accounts.management.commands.bootstrap import USERNAME
from accounts.models import Role, User
from accounts.provisioning import reset_from_the_server


class Command(BaseCommand):
    help = ("Gives a System Admin account a new generated password, printed once and to be changed at the next login, ends its "
            "sessions and lifts a lock from wrong passwords. Run by deploy/reset-admin-password.sh.")

    def add_arguments(self, parser):
        parser.add_argument("--username", default=USERNAME, help=f"The System Admin's username, '{USERNAME}' unless changed.")

    def handle(self, *args, username: str, **options):
        user = User.objects.filter(username=username).first()
        if user is None:
            raise CommandError(f"No account is called '{username}'.")
        if user.role != Role.SYSTEM_ADMIN:
            raise CommandError(f"'{username}' is not a System Admin. The System Admin resets the other accounts' passwords in "
                               "the application.")
        if not user.is_active:
            raise CommandError(f"'{username}' is deactivated.")
        user, password = reset_from_the_server(username)
        self.stdout.write(f"New password for {user.username}: {password}")
        self.stdout.write("It is shown only this once, and must be changed at the next login.")
