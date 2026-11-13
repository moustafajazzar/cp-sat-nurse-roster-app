"""How a waiting cover ends by nobody's hand: it expires when its date has passed on the hospital's clock, and it is filled when a
new version puts one more nurse on its shift-day, whatever made the version.

A date is read by every page (`state_now`), so nothing waits on the worker; the stored state follows in the hourly job
(`covers.tasks`) or the next write about the cover, whichever comes first (`expire_passed`), under each cover's row lock, so it
happens once. A version is made in one place, `rosters.versions.make_version`, which calls `on_new_version` under the roster's row
lock, after the swaps' hook. Neither is audited: both are nobody's act. The nurses offered one are told it ended, never why."""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from uuid import UUID

from django.db.models import QuerySet
from django.utils import timezone

from notifications import sending
from nurses import membership
from rosters.drafts import in_current_codes
from rosters.models import Roster, VersionOrigin
from rosters.versions import Grid

from covers.models import WAITING, Cover, CoverState



def state_now(cover: Cover) -> str:
    """What a page says of it: a waiting cover whose date has passed is expired, whether or not the job has seen it."""
    if cover.waiting and cover.date < membership.today():
        return CoverState.EXPIRED
    return cover.state


def _locked(ids: Iterable[UUID]) -> list[Cover]:
    """In the order every write that locks several takes them, so two never wait on each other."""
    return list(Cover.objects.select_for_update(of=("self",)).select_related("unit")
                .filter(pk__in=list(ids), state__in=WAITING).order_by("opened_at", "pk"))


def _end(rows: Iterable[Cover], state: CoverState) -> list[Cover]:
    step = sending.CoverStep.EXPIRED if state == CoverState.EXPIRED else sending.CoverStep.FILLED
    ended = []
    for cover in rows:
        cover.state, cover.ended_at = state, timezone.now()
        cover.save(update_fields=["state", "ended_at"])
        sending.cover_step(cover, step, None)
        ended.append(cover)
    return ended


def expire_passed(covers: QuerySet[Cover] | None = None) -> list[Cover]:
    """Those of these whose date has passed, every cover when none are named."""
    covers = Cover.objects.all() if covers is None else covers
    ids = covers.filter(state__in=WAITING, date__lt=membership.today()).values_list("pk", flat=True)
    return _end(_locked(ids), CoverState.EXPIRED)


def _on_each_shift_day(grid: Grid) -> Counter:
    return Counter((day, code) for row in in_current_codes(grid).values() for day, code in row.items() if code)


def on_new_version(roster: Roster, before: Grid, after: Grid, origin: str) -> list[Cover]:
    """For each shift-day on which `after` holds more nurses than `before`, one waiting cover of that shift-day per nurse added,
    oldest first, is filled. A cover whose date has passed is not waiting, and is left for the job to expire."""
    if origin == VersionOrigin.COVER:
        # A cover's own acceptance changes one cell, its taker's, which its cover accounts for: a second cover of the same
        # shift-day stays offered.
        return []
    added = _on_each_shift_day(after) - _on_each_shift_day(before)
    if not added:
        return []
    # Only the covers' rows: the shifts joined are catalogue rows, whose locks come before the roster's.
    waiting = (Cover.objects.select_for_update(of=("self",))
               .filter(roster=roster, state__in=WAITING, date__gte=membership.today())
               .select_related("shift", "unit").order_by("opened_at", "pk"))
    filled, left = [], dict(added)
    for cover in waiting:
        key = (cover.date.isoformat(), cover.shift.code)
        if left.get(key, 0) > 0:
            left[key] -= 1
            filled.append(cover)
    return _end(filled, CoverState.FILLED)
