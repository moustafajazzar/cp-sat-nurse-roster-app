"""The carry-over chain. A nurse's month starts from the carry-over of her latest closed
month, projected forward through each approved roster in between by the same computation a close makes: the solver package's
carry-over of a month with every day recorded, worked out with no search (`solver_service.grid`). A month with no
approved roster carries what came into it on, with no shift history. A close takes no projection: its chain is closed months only.
The carry-over handed into January has overtime-to-date and the three fairness balances (nights, weekend days, overtime against
her overtime share) reset to zero.

The nurse's unit is the one each month falls under for her (`membership.placement_in`), so a transferred nurse's carry-over follows
her into the new unit from the month the transfer takes effect."""
from __future__ import annotations

import bisect
import datetime as dt
import logging
from collections.abc import Iterable
from dataclasses import dataclass

from django.contrib.postgres.fields import ArrayField
from django.db.models import Func, TextField, prefetch_related_objects
from solver_service.grid import month_facts, nurses_of

from closes.models import CarryOver, CarrySource, MonthClose
from nurses import membership
from nurses.models import NurseProfile
from rosters.models import Roster, RosterState

logger = logging.getLogger(__name__)

# A solve input's nurse fields that one month hands to the next: a result's `carry_over` block, field for field.
# `preferences_not_kept` is passed through as is: 0 until the app feeds preferences.
# `overtime_balance` likewise: the solver hands it on unchanged until a unit shares its overtime. It starts from her opening
# value and resets in January with the other two balances.
CARRIED = ("ytd_overtime", "hours_balance", "night_balance", "friday_balance", "previous", "last_shift_kind", "rest_owed",
           "preferences_not_kept", "overtime_balance")
NO_HISTORY = {"previous": [], "last_shift_kind": None, "rest_owed": 0, "preferences_not_kept": 0}
# The carried values added since the first closes: a close stored before the solver carried the count, or the overtime
# balance, has none, and hands on 0, as nothing was priced then.
CARRIED_SINCE = {"preferences_not_kept": 0, "overtime_balance": 0.0}
NEW_YEAR = {"ytd_overtime": 0, "night_balance": 0.0, "friday_balance": 0.0, "overtime_balance": 0.0}


def next_month(month: dt.date) -> dt.date:
    return (month + dt.timedelta(days=32)).replace(day=1)


def month_before(month: dt.date) -> dt.date:
    return (month - dt.timedelta(days=1)).replace(day=1)


@dataclass(frozen=True)
class Carried:
    """What a month hands to the next: `values` in the solve input's field names, where they come from, the month they are
    carried out of (None for the opening values), and the year they are of."""
    values: dict
    source: CarrySource
    out_of: dt.date | None
    year: int

    def into(self, month: dt.date) -> dict:
        """The values as `month` starts from them: overtime-to-date and the three fairness balances start again in a later year, the
        carry-over into January, from December or across months with no roster, and opening values entered the year before."""
        if month.year > self.year:
            return self.values | NEW_YEAR
        return dict(self.values)


def _added(nurse: NurseProfile) -> dt.date:
    """The month a nurse's opening values stand at: her first placement's, which is the month she was added, or her start's if
    that was earlier (`membership.place_new_nurse`). Her placements are read with the chain's (`Chain.carry_ins`)."""
    first = min((placement.from_month for placement in nurse.placements.all()), default=None)
    return first or nurse.start_date.replace(day=1)


def opening(nurse: NurseProfile) -> dict:
    return {"ytd_overtime": nurse.ytd_overtime, "hours_balance": nurse.hours_balance, "night_balance": nurse.night_balance,
            "friday_balance": nurse.friday_balance, "overtime_balance": nurse.overtime_balance, **NO_HISTORY}


def _closes_in_force(unit_ids: set[int], months: set[dt.date]) -> set[int]:
    """The ids of the latest close of each of those units' months: closing again replaces what the one before it said, without
    rewriting it."""
    found = {}
    for pk, unit_id, month in (MonthClose.objects.filter(unit_id__in=unit_ids, month__in=months)
                               .order_by("unit_id", "month", "-closed_at", "-id").values_list("pk", "unit_id", "month")):
        found.setdefault((unit_id, month), pk)
    return set(found.values())


def latest_closed(nurses: list[NurseProfile], before: dt.date) -> dict[int, CarryOver]:
    """{nurse id: her carry-over out of the latest month before `before` closed with her in it, from that month's close in
    force}, for those who have one. Three queries, whatever the count of nurses: the rows without their values, the closes in
    force of their months, the values of the rows chosen."""
    rows = list(CarryOver.objects.filter(nurse__in=nurses, source=CarrySource.CLOSED, month__lt=before)
                .order_by("nurse_id", "-month", "-close__closed_at").values_list("pk", "nurse_id", "unit_id", "month", "close_id"))
    in_force = _closes_in_force({unit_id for _, _, unit_id, _, _ in rows}, {month for _, _, _, month, _ in rows})
    chosen: dict[int, int] = {}
    for pk, nurse_id, _, _, close_id in rows:
        if close_id in in_force:
            chosen.setdefault(nurse_id, pk)
    by_pk = CarryOver.objects.in_bulk(chosen.values()) if chosen else {}
    return {nurse_id: by_pk[pk] for nurse_id, pk in chosen.items()}


class _Keys(Func):
    """A JSON object's keys, as an array: a roster grid's hospital IDs."""
    template = "ARRAY(SELECT jsonb_object_keys(%(expressions)s))"
    output_field = ArrayField(TextField())


def has_closed(nurse: NurseProfile) -> bool:
    """Whether any month of hers has closed, which locks her opening values."""
    return CarryOver.objects.filter(nurse=nurse, source=CarrySource.CLOSED).exists()


class Chain:
    """One reading of the chain, memoised by unit and month and by nurse and month: an assembly asks it for a month's nurses at
    once, those of a unit share each month before, and a second ask of the same reading (a close's projection, then its `into`
    column) reads nothing again. `project=False` is a close's: closed months only, never a projection."""

    def __init__(self, *, project: bool = True):
        self.project = project
        self._out: dict[tuple, dict[str, dict]] = {}
        self._rosters: dict[tuple, Roster | None] = {}
        self._months: list[dt.date] | None = None
        self._in: dict[tuple, Carried] = {}

    def carry_in(self, nurse: NurseProfile, month: dt.date) -> Carried:
        """What `month` starts from for her: `carry_ins` for one."""
        return self.carry_ins([nurse], month)[nurse.user.username]

    def carry_ins(self, nurses: Iterable[NurseProfile], month: dt.date) -> dict[str, Carried]:
        """{hospital ID: what `month` starts from for her}, for each of `nurses`, in a fixed number of queries whatever their
        count: where each one's chain starts is read for all of them at once, and the months each steps through are the
        reading's, shared. Without a closed month, from her opening values at the first approved roster that holds her, or her
        start: the months before had no roster in this application to carry anything."""
        nurses = list(nurses)
        unread = list({nurse.pk: nurse for nurse in nurses if (nurse.pk, month) not in self._in}.values())
        if unread:
            prefetch_related_objects(unread, "placements")
            closed = latest_closed(unread, month)
            first = self._first_rosters([nurse for nurse in unread if nurse.pk not in closed], month)
            for nurse in unread:
                self._in[(nurse.pk, month)] = self._walk(nurse, month, closed.get(nurse.pk), first.get(nurse.pk))
        return {nurse.user.username: self._in[(nurse.pk, month)] for nurse in nurses}

    def _walk(self, nurse: NurseProfile, month: dt.date, closed: CarryOver | None, first: dt.date | None) -> Carried:
        """From her latest closed month, or her opening values at her first approved roster, through the months up to `month`."""
        if closed is not None:
            carried = Carried(CARRIED_SINCE | closed.values, CarrySource.CLOSED, closed.month, closed.month.year)
            step = next_month(closed.month)
        else:
            carried, step = Carried(opening(nurse), CarrySource.OPENING, None, _added(nurse).year), first
        while step is not None and step < month:
            ahead = self._next_roster_month(step, month)
            # A month with no approved roster of any unit hands on what came into it, with no shift history (`_through`), so a
            # run of them is one step, however long: the work does not grow with how far ahead `month` is.
            last = month_before(ahead or month)
            if step <= last:
                carried = Carried(carried.into(last) | NO_HISTORY, carried.source, last, last.year)
            if ahead is None:
                return carried
            carried = self._through(nurse, ahead, carried)
            step = next_month(ahead)
        return carried

    def _next_roster_month(self, step: dt.date, month: dt.date) -> dt.date | None:
        """The first month from `step`, and before `month`, with an approved roster of any unit. The months are read once for
        the chain; a close's chain (`project=False`) projects through none, so reads none."""
        if self._months is None:
            self._months = (sorted(set(Roster.objects.filter(state=RosterState.APPROVED).values_list("month", flat=True)))
                            if self.project else [])
        found = self._months[bisect.bisect_left(self._months, step):]
        return found[0] if found and found[0] < month else None

    def _first_rosters(self, nurses: list[NurseProfile], month: dt.date) -> dict[int, dt.date]:
        """{nurse id: the first month before `month`, from her start's, with an approved roster of any unit that holds her}, for
        those who have one, in one query. A close's chain projects through none, so reads none."""
        if not (self.project and nurses):
            return {}
        held = (Roster.objects.filter(state=RosterState.APPROVED, month__lt=month,
                                      grid__has_any_keys=[nurse.user.username for nurse in nurses])
                .annotate(ids=_Keys("grid")).order_by("month").values_list("month", "ids"))
        first: dict[int, dt.date] = {}
        for roster_month, ids in held:
            ids = set(ids)
            for nurse in nurses:
                if nurse.pk not in first and nurse.user.username in ids and roster_month >= nurse.start_date.replace(day=1):
                    first[nurse.pk] = roster_month
        return first

    def _approved(self, unit_id, month: dt.date) -> Roster | None:
        key = (unit_id, month)
        if key not in self._rosters:
            self._rosters[key] = (Roster.objects.filter(unit_id=unit_id, month=month, state=RosterState.APPROVED)
                                  .select_related("unit").first())
        return self._rosters[key]

    def _through(self, nurse: NurseProfile, month: dt.date, carried: Carried) -> Carried:
        """What `month` hands on: its projection when it has an approved roster holding her, else what came into it, with no shift
        history, since no roster says what she worked."""
        into = carried.into(month)
        placement = membership.placement_in(nurse, month)
        roster = self._approved(placement.unit_id, month) if placement and self.project else None
        out = self.carry_out(roster).get(nurse.user.username) if roster else None
        if out is None:
            return Carried(into | NO_HISTORY, carried.source, month, month.year)
        return Carried({key: out[key] for key in CARRIED}, CarrySource.PROJECTED, month, month.year)

    def carry_out(self, roster: Roster) -> dict[str, dict]:
        """Each nurse's carry-over out of an approved roster's month, projected from its roster in force as a close would work it
        out. Empty when the solver's code cannot read the month, as a close would be refused: her chain then carries on as through
        a month with no roster."""
        from closes.making import close_input  # It assembles a month, which reads this chain.

        key = (roster.unit_id, roster.month)
        if key not in self._out:
            try:
                solve_input = close_input(roster.unit, roster.month, roster.grid, chain=self).input
                self._out[key] = {hospital_id: row["carry_over"]
                                  for hospital_id, row in nurses_of(solve_input, month_facts(solve_input)).items()}
            except ValueError as e:
                logger.warning("carry-over of %s, %s cannot be projected: %s", roster.unit.name, f"{roster.month:%Y-%m}", e)
                self._out[key] = {}
        return self._out[key]

