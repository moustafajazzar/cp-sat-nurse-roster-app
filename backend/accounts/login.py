"""Logging in, with the lockout per username and the throttle per client address."""
from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta

from django.contrib.auth import authenticate, login
from django.db import transaction
from django.db.models import Q
from django.http import HttpRequest
from django.utils import timezone

from accounts.models import AddressFailures, LoginAttempts, User
from audit.log import client_ip, record

MAX_FAILURES = 5
LOCK_DURATION = timedelta(minutes=15)
# The throttle: this many failed logins from one address within one window refuse its logins for the rest of the window.
MAX_ADDRESS_FAILURES = 50
THROTTLE_WINDOW = timedelta(minutes=15)
# A name's count of failures is forgotten this long after its last failure, unless the name is locked then.
FAILURES_KEPT = timedelta(hours=24)


class LoginRefused(Exception):
    """Wrong credentials, or locked when `locked_until` is set."""

    def __init__(self, locked_until: datetime | None = None):
        super().__init__("login refused")
        self.locked_until = locked_until


class Throttled(Exception):
    """Too many failed logins from the request's address: refused before any password is looked at."""

    def __init__(self, until: datetime):
        super().__init__("throttled")
        self.until = until


def attempt_login(request: HttpRequest, username: str, password: str) -> User:
    """Logs the request in or raises Throttled or LoginRefused. A throttled address is refused first, so its login
    touches no name's row and hashes nothing. Otherwise the name's row lock is held across the password check, so a burst
    of parallel guesses at one username gets no more than five checks. While locked the password is not looked at, so
    even the right one is refused. Each refusal counts one failure for the address."""
    check_throttle(request)
    refused = None
    with transaction.atomic():
        attempts, _ = LoginAttempts.objects.select_for_update().get_or_create(username=username)
        now = timezone.now()
        if attempts.locked_until and attempts.locked_until > now:
            refused = LoginRefused(attempts.locked_until)
            _record_failure(request, username, reason="locked")
        else:
            if attempts.locked_until:  # the lock has run out: a fresh count
                attempts.failures, attempts.locked_until = 0, None
            user = authenticate(request, username=username, password=password)
            if user is not None:
                attempts.delete()
                login(request, user)
                record("auth.login", actor=user, target=username, request=request)
                return user
            attempts.failures += 1
            attempts.last_failure_at = now
            if attempts.failures >= MAX_FAILURES:
                attempts.locked_until = now + LOCK_DURATION
            attempts.save()
            refused = LoginRefused(attempts.locked_until)
            _record_failure(request, username, reason="wrong_credentials", failures=attempts.failures)
    # After the name's transaction, so the address's row is never held across the hash: a burst of parallel tries may
    # pass the limit by as many as the web pool serves at once.
    record_address_failure(request)
    raise refused


def _record_failure(request: HttpRequest, username: str, **details) -> None:
    """Names the typed username only when an account has it, so a password typed into the username box, or a made-up
    name of any length, is never kept where the System Admin reads it."""
    user = User.objects.filter(username=username).first()
    record("auth.login_failed", actor=user, target=username if user else "", after=details, request=request)


def check_throttle(request: HttpRequest) -> None:
    """Raises Throttled while the request's address has had too many failed logins in its window. Reads only."""
    key = _address_key(request)
    if key is None:
        return
    row = AddressFailures.objects.filter(address=key).first()
    if row and row.failures >= MAX_ADDRESS_FAILURES:
        until = row.window_start + THROTTLE_WINDOW
        if until > timezone.now():
            raise Throttled(until)


def record_address_failure(request: HttpRequest) -> None:
    """Counts one failed login for the request's address. A failure after its window has ended starts a new window,
    counting one. The failure that brings the count to the limit writes the one `auth.login_throttled` entry."""
    key = _address_key(request)
    if key is None:
        return
    now = timezone.now()
    with transaction.atomic():
        row, _ = AddressFailures.objects.select_for_update().get_or_create(
            address=key, defaults={"window_start": now})
        if row.window_start + THROTTLE_WINDOW <= now:
            row.window_start, row.failures = now, 0
        row.failures += 1
        row.save()
        if row.failures == MAX_ADDRESS_FAILURES:  # once: a parallel failure that lands past the limit writes nothing
            until = timezone.localtime(row.window_start + THROTTLE_WINDOW)
            record("auth.login_throttled", after={"until": until.isoformat(timespec="seconds")}, request=request)


def remove_old_failures() -> None:
    """Housekeeping: removes each name's count not locked now whose last failure is over a day old or was never
    kept, and each address's window that has ended. Neither is anyone's record; a name or an address removed counts afresh
    from its next failure, as it would have anyway. The audit entries of the failures stay."""
    now = timezone.now()
    (LoginAttempts.objects.exclude(locked_until__gt=now)
     .filter(Q(last_failure_at__lt=now - FAILURES_KEPT) | Q(last_failure_at__isnull=True)).delete())
    AddressFailures.objects.filter(window_start__lte=now - THROTTLE_WINDOW).delete()


def _address_key(request: HttpRequest) -> str | None:
    """What the throttle counts by: the address the audit log reads (`audit.log.client_ip`, the one place it is worked
    out), an IPv6 address by its /64 and an IPv4 address written as IPv6 as that IPv4 address. None without an address,
    which is never throttled."""
    found = client_ip(request)
    if found is None:
        return None
    address = ipaddress.ip_address(found)
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)
