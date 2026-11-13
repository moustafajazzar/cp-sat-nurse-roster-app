"""A staff nurse's covers: those offered to her, and her answers. A cover is about the shift-day, never about whose absence made
it short, so her rows carry its date, shift, hours, the manager's note, her answer and its state, and nothing of why it opened,
which version, who else was offered it, how many or who took it. A cover not offered to her is the same 404 as none."""
from __future__ import annotations

import datetime as dt
from typing import Literal
from uuid import UUID

from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from ninja import Field, Query, Router, Schema

from accounts.authz import SignedIn
from accounts.models import Role
from nurses import membership
from nurses.api import MonthText

from covers import steps
from covers.expiring import expire_passed, state_now
from covers.models import WAITING, Answer, Cover, CoverOffer

router = Router(tags=["covers"])
nurse_only = SignedIn(Role.STAFF_NURSE)


class MyCoverRow(Schema):
    id: UUID
    month: str
    unit: str
    date: dt.date
    shift: str = Field(description="The shift's code now.")
    hours: int = Field(description="The shift's hours.")
    note: str = Field(description="The manager's words to the nurses offered it.")
    state: Literal["open", "offered", "taken", "withdrawn", "filled", "expired"] = Field(
        description="As it stands now: a waiting cover whose date has passed is expired. `taken` says nothing of by whom: "
                    "`my_answer` says whether it was you.")
    my_answer: Literal["pending", "accepted", "declined"]
    answered_at: dt.datetime | None


class MyCovers(Schema):
    month: str = Field(description="YYYY-MM: the one asked for, or the hospital's current month.")
    covers: list[MyCoverRow] = Field(description="Those waiting for your answer, of every month, soonest first; then the "
                                                 "month's others, answered or ended, latest date first.")


def _row(offer: CoverOffer) -> MyCoverRow:
    cover = offer.cover
    return MyCoverRow(id=cover.pk, month=cover.date.strftime("%Y-%m"), unit=cover.unit.name, date=cover.date,
                      shift=cover.shift.code, hours=cover.shift.hours, note=cover.note, state=state_now(cover),
                      my_answer=offer.answer, answered_at=timezone.localtime(offer.answered_at) if offer.answered_at else None)


def _waiting_for_me(offer: CoverOffer) -> bool:
    return offer.answer == Answer.PENDING and state_now(offer.cover) in WAITING


class MyCoversFilter(Schema):
    month: MonthText | None = Field(None, description="YYYY-MM. The hospital's current month when left out.")


def _offers(request: HttpRequest):
    return CoverOffer.objects.filter(nurse__user=request.user).select_related("cover__unit", "cover__shift")


@router.get("/mine", response=MyCovers, auth=nurse_only, operation_id="listMyCovers")
def my_covers(request: HttpRequest, filters: Query[MyCoversFilter]) -> MyCovers:
    month = dt.date.fromisoformat(f"{filters.month}-01") if filters.month else membership.this_month()
    offers = list(_offers(request))
    waiting = sorted((offer for offer in offers if _waiting_for_me(offer)), key=lambda offer: (offer.cover.date, offer.cover.pk))
    others = sorted((offer for offer in offers if not _waiting_for_me(offer) and offer.cover.date.replace(day=1) == month),
                    key=lambda offer: (offer.cover.date, offer.cover.pk), reverse=True)
    return MyCovers(month=month.strftime("%Y-%m"), covers=[_row(offer) for offer in waiting + others])


def _answer(request: HttpRequest, cover_id: UUID) -> MyCoverRow:
    return _row(_offers(request).get(cover_id=cover_id))


def _expire(cover_id: UUID, request: HttpRequest) -> None:
    """Its own transaction, so a refusal after it keeps the expiry stored."""
    with transaction.atomic():
        expire_passed(Cover.objects.filter(pk=cover_id, offers__nurse__user=request.user))


@router.post("/mine/{cover_id}/accept", response=MyCoverRow, auth=nurse_only, operation_id="acceptCover")
def accept_cover(request: HttpRequest, cover_id: UUID) -> MyCoverRow:
    """The first acceptance takes it: your cell holds its shift in the roster in force at once. Refused (409) once it has been
    taken, withdrawn, filled or its date has passed, or when you could no longer take it, in your own words."""
    _expire(cover_id, request)
    with transaction.atomic():
        steps.accept(request.user, cover_id, request)
    return _answer(request, cover_id)


@router.post("/mine/{cover_id}/decline", response=MyCoverRow, auth=nurse_only, operation_id="declineCover")
def decline_cover(request: HttpRequest, cover_id: UUID) -> MyCoverRow:
    """Your manager is told. You are not asked again."""
    _expire(cover_id, request)
    with transaction.atomic():
        steps.decline(request.user, cover_id, request)
    return _answer(request, cover_id)
