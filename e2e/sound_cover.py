# A cover the app's own judging would let a colleague take, on a unit's approved month as the roster in force stands now: run by
# e2e/stack.sh cover (for e2e/tests/cover.spec.ts), never on an installation's own.
#
#   docker compose exec -T -e UNIT=ER -e MONTH=2026-12 [-e FROM=2026-12-20] [-e AVOID=a,b] web python manage.py shell < e2e/sound_cover.py
#
# The first day, from tomorrow on the hospital's clock (or FROM, if later), on which a staff nurse works a shift that sick leave
# would take off, opening a cover that a colleague could take: each is tried in a transaction rolled back, with the leave
# recorded and the shift taken off as the leave form does (`nurses.leave.take_off_for_leave`), then the cover judged
# (`covers.judging.judge`). Nothing is kept. Neither nurse is one of AVOID's hospital IDs. One line, `E2E_COVER {...}`.
import datetime as dt
import json
import os

from django.db import transaction

from accounts.models import Role
from covers.judging import judge
from covers.models import Cover
from hospital.models import LeaveType
from nurses import membership
from nurses.leave import take_off_for_leave
from nurses.models import Leave, NurseProfile
from rosters.models import Roster

month = dt.date.fromisoformat(f"{os.environ['MONTH']}-01")
roster = Roster.objects.select_related("unit__manager").get(unit__name=os.environ["UNIT"], month=month)
unit, grid = roster.unit, roster.grid
avoid = set(filter(None, os.environ.get("AVOID", "").split(",")))
staff = set(NurseProfile.objects.filter(user__username__in=list(grid), user__role=Role.STAFF_NURSE)
            .values_list("user__username", flat=True))
ids = sorted(nurse for nurse in grid if nurse in staff and nurse not in avoid)
sick = LeaveType.objects.get(name="Sick")


def who_could(absent: str, day: dt.date) -> list[str]:
    with transaction.atomic():
        nurse = NurseProfile.objects.get(user__username=absent)
        Leave.objects.create(nurse=nurse, date=day, leave_type=sick, recorded_by=unit.manager)
        take_off_for_leave(nurse, unit, [day], unit.manager)
        cover = Cover.objects.select_related("shift").filter(roster=roster, date=day).order_by("-opened_at").first()
        listed = [] if cover is None else [could.hospital_id for could in judge(Roster.objects.get(pk=roster.pk), day,
                                                                                 cover.shift).nurses]
        transaction.set_rollback(True)
    return [nurse for nurse in listed if nurse in ids]


def a_sound_cover() -> dict:
    first = max(membership.today() + dt.timedelta(days=1), month, dt.date.fromisoformat(os.environ.get("FROM") or "2000-01-01"))
    for offset in range(31):
        day = first + dt.timedelta(days=offset)
        if day.month != month.month:
            break
        for absent in ids:
            shift = grid[absent].get(day.isoformat(), "")
            if not shift:
                continue
            listed = who_could(absent, day)
            if listed:
                return {"absent": absent, "colleague": listed[0], "date": day.isoformat(), "shift": shift}
    raise SystemExit(f"no sound cover on {unit.name}'s {month:%Y-%m}")


print("E2E_COVER " + json.dumps(a_sound_cover()))
