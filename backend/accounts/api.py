import math
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from django.conf import settings
from django.contrib.auth import logout, update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.http import HttpRequest, HttpResponse
from django.middleware.csrf import get_token
from ninja import Router, Schema, Status
from ninja.errors import HttpError
from ninja.utils import check_csrf
from pydantic import AfterValidator, ConfigDict, Field, StringConstraints, field_validator

from accounts.authz import SignedIn
from accounts.login import LoginRefused, Throttled, attempt_login, check_throttle, record_address_failure
from accounts.models import Role, User
from accounts.provisioning import (RoleTaken, UnitArchived, UsernameTaken, correct_name, create_user, deactivate,
                                   reactivate, reset_password)
from audit.log import record

router = Router(tags=["auth"])
users_router = Router(tags=["users"], auth=SignedIn(Role.SYSTEM_ADMIN))

# The System Admin's users are these three: never a nurse, and never their own account.
MANAGED_ROLES = (Role.CNO, Role.DEPUTY_CNO, Role.UNIT_MANAGER)


def _no_nul(value: str) -> str:
    # PostgreSQL cannot hold or compare one; it would otherwise be a 500.
    if "\x00" in value:
        raise ValueError("not a possible name")
    return value


Username = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150,
                                            pattern=r"^[A-Za-z0-9._-]+$")]
FullName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200), AfterValidator(_no_nul)]


class Me(Schema):
    username: str
    full_name: str
    role: Role
    must_change_password: bool
    has_nurse_profile: bool = Field(description="Whether they are on rosters themselves. A Staff Nurse always is; a Unit "
                                               "Manager may be, and then also reads My roster and My month.")
    time_zone: str = Field(description="The hospital's time zone, for a page that must pick a default by the hospital's "
                                       "clock before it can ask the API anything, such as the year the holidays page opens on.")

    @staticmethod
    def resolve_has_nurse_profile(user) -> bool:
        return hasattr(user, "nurse_profile")

    @staticmethod
    def resolve_time_zone(user) -> str:
        return settings.TIME_ZONE


class SessionState(Schema):
    user: Me | None


class LoginBody(Schema):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1)

    @field_validator("username")
    @classmethod
    def no_nul(cls, value: str) -> str:
        # PostgreSQL cannot hold or compare one; it would otherwise be a 500.
        if "\x00" in value:
            raise ValueError("not a possible username")
        return value


class LoginRefusal(Schema):
    detail: str
    locked_until: datetime | None = None


class ThrottleRefusal(Schema):
    detail: str
    throttled_until: datetime = Field(description="When this address may try again, in the hospital's time zone.")


THROTTLED = "Too many failed logins from this network."
# The lockout is the name's, whether or not an account has it: the words name no account; the page adds `locked_until`.
LOCKED_OUT = "Too many failed logins."
# The 429 declared in full: Ninja's `openapi_extra` replaces the answer it names rather than adding the header to it.
RETRY_AFTER = {"responses": {"429": {
    "description": "Too Many Requests",
    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ThrottleRefusal"}}},
    "headers": {"Retry-After": {"description": "Seconds until this address may try again.", "schema": {"type": "integer"}}},
}}}


def _throttled(response: HttpResponse, refused: Throttled) -> Status:
    """A 429 with the time and `Retry-After`, in whole seconds rounded up."""
    response["Retry-After"] = str(max(1, math.ceil((refused.until - timezone.now()).total_seconds())))
    return Status(429, ThrottleRefusal(detail=THROTTLED, throttled_until=timezone.localtime(refused.until)))


class ChangePasswordBody(Schema):
    current_password: str
    new_password: str


class ChangePasswordRefusal(Schema):
    current_password: list[str] = []
    new_password: list[str] = []


@router.get("/session", response=SessionState, auth=None, operation_id="getSession")
def session(request: HttpRequest) -> SessionState:
    """Who is signed in, if anyone. The SPA calls this first: it also sets the CSRF cookie."""
    get_token(request)
    return SessionState(user=request.user if request.user.is_authenticated else None)


@router.post("/login", response={200: Me, 401: LoginRefusal, 423: LoginRefusal, 429: ThrottleRefusal}, auth=None,
             operation_id="login", openapi_extra=RETRY_AFTER)
def login(request: HttpRequest, response: HttpResponse, body: LoginBody):
    # Ninja exempts its views from Django's middleware and checks CSRF in the session auth, which
    # this one endpoint cannot have.
    if check_csrf(request):
        raise HttpError(403, "CSRF check Failed")
    try:
        return Status(200, attempt_login(request, body.username, body.password))
    except Throttled as refused:
        return _throttled(response, refused)
    except LoginRefused as refused:
        if refused.locked_until:
            return Status(423, LoginRefusal(detail=LOCKED_OUT,
                                            locked_until=timezone.localtime(refused.locked_until)))
        return Status(401, LoginRefusal(detail="Wrong username or password."))


@router.post("/logout", response={204: None}, auth=SignedIn(during_forced_change=True), operation_id="logout")
def sign_out(request: HttpRequest):
    record("auth.logout", actor=request.user, target=request.user.username, request=request)
    logout(request)
    return Status(204, None)


@router.post("/change-password", response={200: Me, 400: ChangePasswordRefusal, 429: ThrottleRefusal},
             auth=SignedIn(during_forced_change=True), operation_id="changePassword", openapi_extra=RETRY_AFTER)
def change_password(request: HttpRequest, response: HttpResponse, body: ChangePasswordBody):
    """Refused while the address is throttled, before the current password is checked; a wrong one counts a failure."""
    user: User = request.user
    try:
        check_throttle(request)
    except Throttled as refused:
        return _throttled(response, refused)
    if not user.check_password(body.current_password):
        record_address_failure(request)
        return Status(400, ChangePasswordRefusal(current_password=["This is not your current password."]))
    if body.new_password == body.current_password:
        return Status(400, ChangePasswordRefusal(new_password=["The new password must differ from the current one."]))
    try:
        validate_password(body.new_password, user)
    except DjangoValidationError as invalid:
        return Status(400, ChangePasswordRefusal(new_password=invalid.messages))
    forced = user.must_change_password
    with transaction.atomic():
        user.set_password(body.new_password)
        user.must_change_password = False
        user.save(update_fields=["password", "must_change_password"])
        record("auth.password_changed", actor=user, target=user.username, after={"forced": forced}, request=request)
    update_session_auth_hash(request, user)  # this session stays; any other one of theirs ends
    return Status(200, user)


class ManagedUser(Schema):
    id: UUID
    username: str
    full_name: str
    role: Role
    is_active: bool
    must_change_password: bool
    last_login: datetime | None = Field(description="In the hospital's time zone.")
    unit: str | None = Field(description="The unit a unit manager holds.")

    @staticmethod
    def resolve_unit(user: User) -> str | None:
        return user.managed_unit_name

    @staticmethod
    def resolve_last_login(user: User) -> datetime | None:
        return timezone.localtime(user.last_login) if user.last_login else None


class NewUserBody(Schema):
    username: Username
    full_name: FullName
    role: Literal[Role.CNO, Role.DEPUTY_CNO, Role.UNIT_MANAGER]


class NameCorrectionBody(Schema):
    """The username is not here, and asking to change it is refused rather than ignored."""
    model_config = ConfigDict(extra="forbid")

    full_name: FullName


class NewUserRefusal(Schema):
    username: list[str] = []
    role: list[str] = []


class UserWithPassword(Schema):
    """The only answers that ever hold a password. They ask not to be stored."""
    user: ManagedUser
    generated_password: str


class ReactivateRefusal(Schema):
    detail: str


def _role_taken(refused: RoleTaken) -> str:
    return (f"{refused.holder.username} is already the {Role(refused.holder.role).label}. "
            "Deactivate that user first.")


def _managed_or_404(user_id: UUID) -> User:
    return get_object_or_404(User, pk=user_id, role__in=MANAGED_ROLES)


@users_router.get("", response=list[ManagedUser], operation_id="listUsers")
def list_users(request: HttpRequest):
    return User.objects.filter(role__in=MANAGED_ROLES).select_related("managed_unit").order_by("username")


@users_router.post("", response={201: UserWithPassword, 409: NewUserRefusal}, operation_id="createUser")
def new_user(request: HttpRequest, response: HttpResponse, body: NewUserBody):
    try:
        user, password = create_user(request, username=body.username, full_name=body.full_name, role=body.role)
    except UsernameTaken:
        return Status(409, NewUserRefusal(username=["This username is taken."]))
    except RoleTaken as refused:
        return Status(409, NewUserRefusal(role=[_role_taken(refused)]))
    response["Cache-Control"] = "no-store"
    return Status(201, UserWithPassword(user=user, generated_password=password))


@users_router.put("/{user_id}/full-name", response=ManagedUser, operation_id="correctUserName")
def correct_user_name(request: HttpRequest, user_id: UUID, body: NameCorrectionBody):
    return correct_name(request, _managed_or_404(user_id).pk, body.full_name)


@users_router.post("/{user_id}/deactivate", response=ManagedUser, operation_id="deactivateUser")
def deactivate_user(request: HttpRequest, user_id: UUID):
    return deactivate(request, _managed_or_404(user_id).pk)


@users_router.post("/{user_id}/reactivate", response={200: ManagedUser, 409: ReactivateRefusal},
                   operation_id="reactivateUser")
def reactivate_user(request: HttpRequest, user_id: UUID):
    try:
        return Status(200, reactivate(request, _managed_or_404(user_id).pk))
    except RoleTaken as refused:
        return Status(409, ReactivateRefusal(detail=_role_taken(refused)))
    except UnitArchived as refused:
        return Status(409, ReactivateRefusal(detail=f"Their nurse profile is in {refused.unit.name}, which is archived. "
                                                    "Ask the CNO to transfer it first."))


@users_router.post("/{user_id}/reset-password", response=UserWithPassword, operation_id="resetUserPassword")
def reset_user_password(request: HttpRequest, response: HttpResponse, user_id: UUID):
    user, password = reset_password(request, _managed_or_404(user_id).pk)
    response["Cache-Control"] = "no-store"
    return UserWithPassword(user=user, generated_password=password)
