"""Creating users, deactivating them, resetting their passwords and correcting the names of the management
roles: every change is audited here, in one transaction with the change itself. Who may do it is the
caller's business (accounts.api for the management roles; nurses.api for nurses, whose names it corrects)."""
from __future__ import annotations

import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from django.contrib.auth import SESSION_KEY
from django.contrib.auth.hashers import make_password
from django.contrib.sessions.models import Session
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone

from accounts.models import ONE_OF_A_KIND_CONSTRAINTS, LoginAttempts, User
from audit.log import record
from notifications import subscriptions
from nurses.membership import archived_unit_in_the_way
from units.assignment import release_managed_unit
from units.models import Unit

# Read aloud or copied from a note at times, so nothing that looks like something else.
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def generate_password() -> str:
    """Four groups of four: about 79 bits, and it passes the password policy."""
    return "-".join("".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(4))


@dataclass(frozen=True)
class NewPassword:
    plain: str = field(repr=False)
    hashed: str = field(repr=False)


def new_password() -> NewPassword:
    plain = generate_password()
    return NewPassword(plain, make_password(plain))


def new_passwords(count: int) -> list[NewPassword]:
    """Hashing is slow on purpose, so many of them are made side by side, as PBKDF2 lets go of the GIL, and before a
    transaction takes any lock. At most four at once, because the solver shares the server."""
    with ThreadPoolExecutor(max_workers=min(os.process_cpu_count() or 1, 4)) as pool:
        return list(pool.map(lambda _: new_password(), range(count)))


class UsernameTaken(Exception):
    pass


class UnitArchived(Exception):
    """Coming back into service would put this nurse in an archived unit."""

    def __init__(self, unit: Unit):
        super().__init__(unit.name)
        self.unit = unit


class RoleTaken(Exception):
    """An active user already holds this one-of-a-kind role."""

    def __init__(self, role: str):
        super().__init__(role)
        self.holder = User.objects.get(role=role, is_active=True)


def _refusal(error: IntegrityError, role: str) -> Exception:
    constraint = getattr(getattr(error.__cause__, "diag", None), "constraint_name", None)
    return RoleTaken(role) if constraint in ONE_OF_A_KIND_CONSTRAINTS else UsernameTaken()


def create_user(request: HttpRequest, *, username: str, full_name: str, role: str,
                password: NewPassword | None = None) -> tuple[User, str]:
    """The user and their generated password, which is shown once and kept nowhere. The database's
    constraints decide refusals, so two people creating a CNO at the same moment get one CNO. `password` is
    for a caller that hashed it already, so as not to hash inside its own transaction."""
    password = password or new_password()
    try:
        with transaction.atomic():
            user = User(username=username, password=password.hashed, role=role, full_name=full_name,
                        must_change_password=True)
            user.save()
            record("user.created", actor=request.user, target=username, request=request,
                   after={"username": username, "full_name": full_name, "role": role, "must_change_password": True})
    except IntegrityError as error:
        raise _refusal(error, role) from error
    return user, password.plain


def correct_name(request: HttpRequest, user_id, full_name: str) -> User:
    """Changes nothing else about the user, whether or not they are active. No change writes no entry."""
    with transaction.atomic():
        user = User.objects.select_for_update(no_key=True).get(pk=user_id)
        before = user.full_name
        if full_name != before:
            user.full_name = full_name
            user.save(update_fields=["full_name"])
            record("user.name_corrected", actor=request.user, target=user.username, request=request,
                   before={"full_name": before}, after={"full_name": full_name})
    return user


def deactivate(request: HttpRequest, user_id) -> User:
    """Ends the user's sessions too, so reactivating cannot bring an old browser back in, and takes a manager
    off their unit, which reactivating does not undo."""
    with transaction.atomic():
        user = User.objects.select_for_update(no_key=True).get(pk=user_id)
        if user.is_active:
            user.is_active = False
            user.save(update_fields=["is_active"])
            _end_sessions({str(user.pk)})
            subscriptions.end_for([user.pk])
            record("user.deactivated", actor=request.user, target=user.username, request=request,
                   before={"is_active": True}, after={"is_active": False})
            release_managed_unit(request, user)
    return user


def deactivate_many(users: QuerySet[User]) -> None:
    """For the end of the day rather than a person's request, so no actor. Reads the sessions once however many
    users there are. Only for users who hold no unit, since nothing here takes one off them.

    `users` is asked twice, because the first answer can be out of date by the time the locks are held: after waiting
    for a row PostgreSQL looks again only if that row changed, and clearing a nurse's end date changes their profile's.
    Whatever `users` filters on must be written under the user's lock, as `_locked_nurse` in `nurses.api` does."""
    with transaction.atomic():
        locked = [user.pk for user in users.select_for_update(of=("self",), no_key=True).order_by("pk")]
        leaving = list(users.filter(pk__in=locked).order_by("pk"))
        if not leaving:
            return
        User.objects.filter(pk__in=[user.pk for user in leaving]).update(is_active=False)
        _end_sessions({str(user.pk) for user in leaving})
        subscriptions.end_for(user.pk for user in leaving)
        for user in leaving:
            record("user.deactivated", target=user.username, before={"is_active": True}, after={"is_active": False})


def reactivate(request: HttpRequest, user_id) -> User:
    """Refused rather than leave a nurse in a unit archived while they were away."""
    try:
        with transaction.atomic():
            user = User.objects.select_for_update(no_key=True).get(pk=user_id)
            if not user.is_active:
                unit = archived_unit_in_the_way(user)
                if unit is not None:
                    raise UnitArchived(unit)
                user.is_active = True
                user.save(update_fields=["is_active"])
                record("user.reactivated", actor=request.user, target=user.username, request=request,
                       before={"is_active": False}, after={"is_active": True})
    except IntegrityError as error:
        raise _refusal(error, User.objects.get(pk=user_id).role) from error
    return user


def reset_password(request: HttpRequest, user_id) -> tuple[User, str]:
    """A new generated password, shown once, with a forced change at the next login. The new hash
    ends the user's sessions, and a lock from failed logins goes, or they would stay locked out."""
    return _reset(User.objects.filter(pk=user_id), request=request)


def reset_from_the_server(username: str) -> tuple[User, str]:
    """`reset_password` for `deploy/reset-admin-password.sh`, run by whoever holds the server: no one signed in, so no actor,
    and the entry says where it came from. Who may be reset that way is the command's business."""
    return _reset(User.objects.filter(username=username), after={"from": "the server"})


def _reset(users: QuerySet[User], *, request: HttpRequest | None = None, after: dict | None = None) -> tuple[User, str]:
    password = generate_password()
    with transaction.atomic():
        user = users.select_for_update(no_key=True).get()
        user.set_password(password)
        user.must_change_password = True
        user.save(update_fields=["password", "must_change_password"])
        LoginAttempts.objects.filter(username=user.username).delete()
        record("user.password_reset", actor=request.user if request else None, target=user.username, request=request,
               after={"must_change_password": True} | (after or {}))
    return user, password


def _end_sessions(user_ids: set[str]) -> None:
    """Sessions are not indexed by user, so this reads them all. It is a rare action on a table of
    one hospital's sessions."""
    for session in Session.objects.filter(expire_date__gt=timezone.now()).iterator():
        if session.get_decoded().get(SESSION_KEY) in user_ids:
            session.delete()
