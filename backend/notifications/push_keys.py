"""The installation's own VAPID key pair. Made at first start by `bootstrap`, which `prepare` runs, with no step of the
hospital's; replaced only after a compromise, by `rotate_push_keys`. The private key leaves this module only for the sender."""
from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.log import record
from notifications.models import PushKeyPair


def _new_pair() -> dict[str, str]:
    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    point = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"private_key": pem, "public_key": base64.urlsafe_b64encode(point).rstrip(b"=").decode()}


def ensure() -> bool:
    """Makes the pair when there is none. True when it did."""
    if PushKeyPair.objects.exists():
        return False
    try:
        with transaction.atomic():
            PushKeyPair.objects.create(pk=1, **_new_pair())
    except IntegrityError:  # another process made it first
        return False
    return True


def public_key() -> str:
    """What a browser subscribes with, as `applicationServerKey` takes it. An installation that never ran `prepare` (a test)
    gets its pair here."""
    found = PushKeyPair.objects.values_list("public_key", flat=True).first()
    if found is None:
        ensure()
        found = PushKeyPair.objects.values_list("public_key", flat=True).get()
    return found


def private_key() -> str:
    """The PEM the sender signs with, for the worker alone."""
    public_key()  # makes the pair when there is none
    return PushKeyPair.objects.values_list("private_key", flat=True).get()


def rotate() -> None:
    """A new pair in place of the old, from the server, so no actor. No subscription is deleted: a device bound to the old key
    re-subscribes at its next start of the app."""
    with transaction.atomic():
        PushKeyPair.objects.update_or_create(pk=1, defaults=_new_pair() | {"created_at": timezone.now()})
        record("push.keys_rotated", after={"from": "the server"})
