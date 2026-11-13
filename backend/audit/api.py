"""The System Admin's reading of the audit log."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone
from ninja import Query, Router, Schema
from pydantic import Field, field_validator

from accounts.authz import SignedIn
from accounts.models import Role, User
from audit.models import AuditEntry

router = Router(tags=["audit"], auth=SignedIn(Role.SYSTEM_ADMIN))


class AuditEntrySchema(Schema):
    id: int
    at: datetime = Field(description="In the hospital's time zone, which the date filters also use.")
    actor: str | None = Field(description="Empty for the system, and for a failed login with an unknown username.")
    action: str
    target: str
    before: dict | None
    after: dict | None
    ip: str | None

    @staticmethod
    def resolve_at(entry: AuditEntry) -> datetime:
        return timezone.localtime(entry.at)

    @staticmethod
    def resolve_actor(entry: AuditEntry) -> str | None:
        return entry.actor.username if entry.actor else None


class AuditPage(Schema):
    entries: list[AuditEntrySchema]
    older_than: int | None = Field(description="Pass as `older_than` for the next page. Empty on the last page.")


class AuditFilters(Schema):
    user: str | None = Field(None, description="A username, any case: what they did and what was done to them.")
    action: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    older_than: int | None = None
    limit: int = Field(50, ge=1, le=200)

    @field_validator("user", "action")
    @classmethod
    def no_nul(cls, value: str | None) -> str | None:
        # PostgreSQL cannot hold or compare one; it would otherwise be a 500.
        if value and "\x00" in value:
            raise ValueError("not a possible value")
        return value


def _start_of(day: date) -> datetime:
    return timezone.make_aware(datetime.combine(day, time.min))


@router.get("/entries", response=AuditPage, operation_id="listAuditEntries")
def entries(request, filters: Query[AuditFilters]) -> AuditPage:
    found = AuditEntry.objects.select_related("actor")
    if filters.user:
        found = found.filter(Q(actor__username__iexact=filters.user) | Q(target__iexact=filters.user))
    if filters.action:
        found = found.filter(action=filters.action)
    if filters.date_from:
        found = found.filter(at__gte=_start_of(filters.date_from))
    if filters.date_to:
        found = found.filter(at__lt=_start_of(filters.date_to + timedelta(days=1)))
    if filters.older_than is not None:
        found = found.filter(id__lt=filters.older_than)
    page = list(found[:filters.limit + 1])
    has_more = len(page) > filters.limit
    page = page[:filters.limit]
    return AuditPage(entries=page, older_than=page[-1].id if has_more else None)


class AuditFilterOptions(Schema):
    actions: list[str]
    users: list[str]


@router.get("/filter-options", response=AuditFilterOptions, operation_id="getAuditFilterOptions")
def filter_options(request) -> AuditFilterOptions:
    return AuditFilterOptions(
        actions=list(AuditEntry.objects.order_by("action").values_list("action", flat=True).distinct()),
        users=list(User.objects.order_by("username").values_list("username", flat=True)),
    )
