"""Pushes on this device, for the signed-in user only: none of these takes a user. Every call is a POST, the read too, since
a push service's address is a capability and goes in no query string. Nothing is sent from here."""
from __future__ import annotations

from typing import Annotated

from django.http import HttpRequest
from ninja import Field, Router, Schema
from pydantic import AfterValidator, StringConstraints

from accounts.authz import SignedIn
from notifications import push_keys, subscriptions
from notifications.models import PushSubscription

router = Router(tags=["push"])

Address = Annotated[str, StringConstraints(pattern=r"^https://\S+$", max_length=2000)]


def _a_push_service(endpoint: str) -> str:
    if not subscriptions.allowed(endpoint):
        raise ValueError("The address is not a browser push service's.")
    return endpoint


# Only a turn-on is refused: the state, a turn-off and the address a renewal replaces read an address, and send nothing to it.
PushServiceAddress = Annotated[Address, AfterValidator(_a_push_service)]


def _key(column: str):
    """A key in base64url, no longer than the column that keeps it, so one that cannot be stored is a field error."""
    longest = PushSubscription._meta.get_field(column).max_length
    return Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]+={0,2}$", min_length=1, max_length=longest)]


P256dhKey = _key("p256dh")
AuthKey = _key("auth")

# Log out is allowed during a forced password change and turns this device off first, so turning off is too.
ALSO_DURING_A_FORCED_CHANGE = SignedIn(during_forced_change=True)


class ThisDevice(Schema):
    endpoint: Address | None = Field(None, description="The browser's current subscription's address, if it has one.")


class PushState(Schema):
    public_key: str = Field(description="The installation's public key, base64url, as `applicationServerKey` takes it.")
    this_device: bool = Field(description="Whether the address given is held for this user.")
    devices: int = Field(description="How many devices of this user pushes are on for.")


class SubscriptionKeys(Schema):
    p256dh: P256dhKey
    auth: AuthKey


class TurnOn(Schema):
    endpoint: PushServiceAddress
    keys: SubscriptionKeys
    replaces: Address | None = Field(None, description="An address of this user's that this one replaces: a subscription "
                                     "the browser renewed, or one bound to a public key since replaced. When it is no longer "
                                     "held, nothing is turned on.")


class TurnOff(Schema):
    endpoint: Address


def _state(request: HttpRequest, endpoint: str | None) -> PushState:
    return PushState(public_key=push_keys.public_key(), this_device=subscriptions.holds(request.user, endpoint),
                     devices=subscriptions.devices(request.user))


@router.post("/state", response=PushState, operation_id="readPushState")
def read_state(request: HttpRequest, body: ThisDevice) -> PushState:
    return _state(request, body.endpoint)


@router.post("/on", response=PushState, operation_id="turnPushOn")
def turn_on(request: HttpRequest, body: TurnOn) -> PushState:
    subscriptions.turn_on(request.user, endpoint=body.endpoint, p256dh=body.keys.p256dh, auth=body.keys.auth,
                          replaces=body.replaces)
    return _state(request, body.endpoint)


@router.post("/off", response=PushState, auth=ALSO_DURING_A_FORCED_CHANGE, operation_id="turnPushOff")
def turn_off(request: HttpRequest, body: TurnOff) -> PushState:
    """The same answer whether or not the address was this user's."""
    subscriptions.turn_off(request.user, body.endpoint)
    return _state(request, None)


@router.post("/off-everywhere", response=PushState, auth=ALSO_DURING_A_FORCED_CHANGE, operation_id="turnPushOffEverywhere")
def turn_off_everywhere(request: HttpRequest) -> PushState:
    subscriptions.turn_off_everywhere(request.user)
    return _state(request, None)
