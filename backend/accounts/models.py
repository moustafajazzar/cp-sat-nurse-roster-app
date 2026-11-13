from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.db.models.functions import UUID7, Lower

# A role only one active user may hold. A deactivated CNO does not block their successor, and
# nothing is ever deleted, so the old account stays for the audit log.
ONE_OF_A_KIND_CONSTRAINTS = ("accounts_one_active_cno", "accounts_one_active_deputy_cno")


class Role(models.TextChoices):
    SYSTEM_ADMIN = "system_admin", "System Admin"
    CNO = "cno", "CNO"
    DEPUTY_CNO = "deputy_cno", "Deputy CNO"
    UNIT_MANAGER = "unit_manager", "Unit Manager"
    STAFF_NURSE = "staff_nurse", "Staff Nurse"


class UserManager(BaseUserManager):
    def create_user(self, username: str, password: str, role: str, **fields) -> "User":
        user = self.model(username=username, role=role, **fields)
        user.set_password(password)
        user.save(using=self._db)
        return user


class User(AbstractBaseUser):
    id = models.UUIDField(primary_key=True, db_default=UUID7(), editable=False)
    username = models.CharField(max_length=150, unique=True)
    full_name = models.CharField(max_length=200, blank=True)
    role = models.CharField(max_length=20, choices=Role)
    is_active = models.BooleanField(default=True)
    must_change_password = models.BooleanField(default=False)

    USERNAME_FIELD = "username"

    objects = UserManager()

    @property
    def held_unit(self):
        return getattr(self, "managed_unit", None)  # The reverse side raises an AttributeError when there is none.

    @property
    def managed_unit_name(self) -> str | None:
        return self.held_unit.name if self.held_unit else None

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["role"], condition=models.Q(role=Role.SYSTEM_ADMIN),
                                    name="accounts_one_system_admin"),
            models.UniqueConstraint(fields=["role"], condition=models.Q(role=Role.CNO, is_active=True),
                                    name="accounts_one_active_cno"),
            models.UniqueConstraint(fields=["role"], condition=models.Q(role=Role.DEPUTY_CNO, is_active=True),
                                    name="accounts_one_active_deputy_cno"),
            # "Fatimah" and "fatimah" as two people would make the audit log lie.
            models.UniqueConstraint(Lower("username"), name="accounts_username_any_case"),
        ]


class LoginAttempts(models.Model):
    """Consecutive failed logins per username tried, known or not. Keyed by the name rather than
    held on the user, so an unknown username locks exactly like a real one and the answer to a
    login never says which usernames exist. The row goes away at the next successful login, or a day after its last
    failure unless the name is locked then (`accounts.login.remove_old_failures`): the name typed may be anything, a
    password among them, and is no one's record."""

    username = models.CharField(max_length=150, primary_key=True)
    failures = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True)
    # Null on the rows written before it was kept; a refusal while locked does not move it.
    last_failure_at = models.DateTimeField(null=True)


class AddressFailures(models.Model):
    """The failed logins from one client address in its current window: the throttle (accounts.login). Keyed by the
    address's key, an IPv4 address as itself and an IPv6 address as its /64 network, since one household or one phone
    holds a whole /64. Whatever username each failure tried."""

    address = models.CharField(max_length=43, primary_key=True)
    window_start = models.DateTimeField()
    failures = models.PositiveIntegerField(default=0)
