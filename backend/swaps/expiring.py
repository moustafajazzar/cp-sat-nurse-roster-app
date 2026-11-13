"""When a waiting swap expires: when any of its dates has passed, on the hospital's clock, and when a new version changes any of
its cells, since what its colleagues accepted is then no longer what it would do.

A date is read by every page (`state_now`), so nothing waits on the worker; the stored state and who is told follow in the hourly
job (`swaps.tasks`) or the next write about the swap, whichever comes first (`expire_passed`), under each swap's row lock, so it
happens once. A version is made in one place, `rosters.versions.make_version`, which calls `on_new_version` under the roster's
row lock. Nobody is told why a swap expired: a cell a version changed can be a colleague's leave."""
from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from django.db.models import QuerySet
from django.utils import timezone

from notifications import sending
from nurses import membership
from rosters.drafts import in_current_codes
from rosters.models import Roster
from rosters.versions import Grid
from swaps.models import WAITING, Expiry, Swap, SwapState


def state_now(swap: Swap) -> str:
    """What a page says of it: a waiting swap whose earliest date has passed is expired, whether or not the job has seen it.
    `swap.cells` prefetched."""
    if swap.waiting and any(cell.date < membership.today() for cell in swap.cells.all()):
        return SwapState.EXPIRED
    return swap.state


def _expire(rows: Iterable[Swap], why: Expiry) -> list[Swap]:
    expired = []
    for swap in rows:
        awaiting = swap.state == SwapState.AWAITING_MANAGER
        swap.state, swap.expired_why, swap.ended_at = SwapState.EXPIRED, why, timezone.now()
        swap.save(update_fields=["state", "expired_why", "ended_at"])
        sending.swap_step(swap, sending.SwapStep.EXPIRED, None, awaiting=awaiting)
        expired.append(swap)
    return expired


def _locked(ids: Iterable[UUID]) -> list[Swap]:
    return list(Swap.objects.select_for_update(of=("self",)).select_related("unit__manager", "roster", "proposer__user")
                .filter(pk__in=list(ids), state__in=WAITING).order_by("pk"))


def expire_passed(swaps: QuerySet[Swap] | None = None) -> list[Swap]:
    """Those of these whose earliest date has passed, every swap when none are named. No audit entry: an expiry is nobody's act,
    and one beside the change that caused it could date a colleague's leave for the System Admin."""
    swaps = Swap.objects.all() if swaps is None else swaps
    ids = swaps.filter(state__in=WAITING, cells__date__lt=membership.today()).values_list("pk", flat=True).distinct()
    return _expire(_locked(ids), Expiry.DATE_PASSED)


def on_new_version(roster: Roster, before: Grid, after: Grid) -> list[Swap]:
    """Each waiting swap of this roster one of whose cells this version changed. Called by `make_version`, holding the roster's
    row lock, which every swap's proposal and approval take before a swap's, so none is proposed on the grid this replaces."""
    before, after = in_current_codes(before), in_current_codes(after)
    changed = {(nurse, day) for nurse in set(before) | set(after)
               for day in set(before.get(nurse, {})) | set(after.get(nurse, {}))
               if before.get(nurse, {}).get(day, "") != after.get(nurse, {}).get(day, "")}
    if not changed:
        return []
    waiting = Swap.objects.filter(roster=roster, state__in=WAITING).prefetch_related("cells__nurse__user")
    hit = [swap.pk for swap in waiting
           if any((cell.nurse.user.username, cell.date.isoformat()) in changed for cell in swap.cells.all())]
    return _expire(_locked(hit), Expiry.CELL_CHANGED) if hit else []

