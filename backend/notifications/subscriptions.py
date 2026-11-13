"""Push subscriptions: a device turned on for one user, and every way one ends. The audit log holds who
turned pushes on or off and when, and nothing of the device: not its address, its keys or the IP it called from."""
from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from accounts.models import User
from audit.log import record
from notifications.models import PushSubscription

# The browsers' push services, the only hosts the worker sends to, so a signed-in user
# cannot make the worker post to an address of their choosing. From each service's own documentation:
# Google's FCM, Mozilla's autopush, Apple's and Microsoft's WNS: these hosts themselves, and any name under these, on a dot.
PUSH_SERVICE_HOSTS = frozenset({"fcm.googleapis.com", "push.services.mozilla.com"})
PUSH_SERVICE_DOMAINS = frozenset({"push.services.mozilla.com", "push.apple.com", "notify.windows.com"})


def allowed(endpoint: str) -> bool:
    """An `https://` address on a push service's host, on the default port, with no user name or password in it."""
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        return False
    host = parts.hostname or ""
    if parts.scheme != "https" or port not in (None, 443) or "@" in parts.netloc or not host or host.endswith("."):
        return False
    return host in PUSH_SERVICE_HOSTS or any(host.endswith("." + domain) for domain in PUSH_SERVICE_DOMAINS)


def devices(user: User) -> int:
    return PushSubscription.objects.filter(user=user).count()


def holds(user: User, endpoint: str | None) -> bool:
    return bool(endpoint) and PushSubscription.objects.filter(user=user, endpoint=endpoint).exists()


def _audit(action: str, user: User) -> None:
    record(action, actor=user, target=user.username)


def turn_on(user: User, *, endpoint: str, p256dh: str, auth: str, replaces: str | None = None) -> None:
    """Idempotent for the same user and address. An address another user holds moves to this one, since it belongs to whoever
    last subscribed it. With `replaces`, a renewal: one of this user's addresses the browser renewed or a rotation retired
    takes the new one, and writes no entry. A renewal of an address no longer held does nothing, so a device turned off from
    another one (every device) is not turned back on by its own renewal."""
    keys = {"p256dh": p256dh, "auth": auth}
    with transaction.atomic():
        # One user's calls one at a time: a double tap must not make two rows or two entries.
        User.objects.select_for_update(no_key=True).filter(pk=user.pk).first()
        old = None
        if replaces and replaces != endpoint:
            old = PushSubscription.objects.select_for_update().filter(user=user, endpoint=replaces).first()
            if old is None:
                return
        held = PushSubscription.objects.select_for_update().filter(endpoint=endpoint).first()
        if held is not None and held.user_id == user.pk:
            PushSubscription.objects.filter(pk=held.pk).update(**keys)
            if old is not None:
                old.delete()
            return
        if held is not None:  # another user's: theirs no more
            PushSubscription.objects.filter(pk=held.pk).update(
                user=user, turned_on_at=timezone.now(), failures_in_a_row=0, last_success_at=None, **keys)
            if old is not None:
                old.delete()
            else:
                _audit("push.turned_on", user)
                _keep_the_latest(user)
            return
        if old is not None:
            PushSubscription.objects.filter(pk=old.pk).update(
                endpoint=endpoint, failures_in_a_row=0, last_success_at=None, **keys)
            return
        PushSubscription.objects.create(user=user, endpoint=endpoint, **keys)
        _audit("push.turned_on", user)
        _keep_the_latest(user)


SUBSCRIPTIONS_A_USER = 10
"""So that one account cannot make the one push job at a time send to hundreds of made-up addresses."""


def _keep_the_latest(user: User) -> None:
    """An eleventh device ends the user's earliest turned on, with no entry of its own: the new one's turning on says enough.
    Under the user's row lock, which `turn_on` holds."""
    earliest = PushSubscription.objects.filter(user=user).order_by("-turned_on_at", "-pk")[SUBSCRIPTIONS_A_USER:]
    PushSubscription.objects.filter(pk__in=list(earliest.values_list("pk", flat=True))).delete()


def turn_off(user: User, endpoint: str) -> None:
    """This device. Answers the same whether or not the address was this user's, so nobody learns whose an address is; an
    entry is written only when something was turned off."""
    with transaction.atomic():
        deleted, _ = PushSubscription.objects.filter(user=user, endpoint=endpoint).delete()
        if deleted:
            _audit("push.turned_off", user)


def turn_off_everywhere(user: User) -> None:
    """For a lost phone: every device of the user, from any of them. One entry, when there was something to turn off."""
    with transaction.atomic():
        deleted, _ = PushSubscription.objects.filter(user=user).delete()
        if deleted:
            _audit("push.turned_off", user)


FAILURES_THAT_END = 10


def reached(subscription_id) -> None:
    """A push got there: its failures start again from none."""
    PushSubscription.objects.filter(pk=subscription_id).update(failures_in_a_row=0, last_success_at=timezone.now())


def not_reached(subscription_id) -> None:
    """A push that did not get there counts one failure; the tenth in a row ends the subscription, with no entry."""
    PushSubscription.objects.filter(pk=subscription_id).update(failures_in_a_row=F("failures_in_a_row") + 1)
    PushSubscription.objects.filter(pk=subscription_id, failures_in_a_row__gte=FAILURES_THAT_END).delete()


def end(subscription_id) -> None:
    """The push service says it is gone, or its host is not one allowed: nobody's act, so no entry."""
    PushSubscription.objects.filter(pk=subscription_id).delete()


def end_for(user_ids: Iterable) -> None:
    """A deactivation's: every device of these users, beside their sessions. Not the user's own act, so no entry of its own;
    the deactivation's says enough."""
    PushSubscription.objects.filter(user_id__in=list(user_ids)).delete()
