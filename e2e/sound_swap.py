# A two-cell swap the app's own judging finds sound on a unit's approved month, on the roster in force now: run by
# e2e/stack.sh (after the seed, and for a check whose unit a re-plan has changed since), never on an installation's own.
#
#   docker compose exec -T -e UNIT=ER -e MONTH=2026-12 [-e FROM=2026-12-20] web python manage.py shell < e2e/sound_swap.py
#
# The first day, from tomorrow on the hospital's clock (or FROM, if later), on which one nurse works a shift and a colleague is off, and giving the
# shift to the colleague breaks nothing: the proposer's cell first. Staff nurses only, since only they propose. One line,
# `E2E_SWAP {...}`.
import datetime as dt
import json
import os

from accounts.models import Role
from nurses import membership
from nurses.models import NurseProfile
from rosters.models import Roster
from swaps.judging import Change, judge

month = dt.date.fromisoformat(f"{os.environ['MONTH']}-01")
roster = Roster.objects.get(unit__name=os.environ["UNIT"], month=month)
grid = roster.grid  # {hospital ID: {ISO date: shift code, "" for off}}
nurses = set(NurseProfile.objects.filter(user__username__in=list(grid), user__role=Role.STAFF_NURSE)
             .values_list("user__username", flat=True))
ids = [nurse for nurse in grid if nurse in nurses]


def a_sound_swap() -> dict:
    first = max(membership.today() + dt.timedelta(days=1), month, dt.date.fromisoformat(os.environ.get("FROM") or "2000-01-01"))
    for offset in range(31):
        day = first + dt.timedelta(days=offset)
        if day.month != month.month:
            break
        for proposer in ids:
            shift = grid[proposer].get(day.isoformat(), "")
            if not shift:
                continue
            for colleague in ids:
                if colleague == proposer or grid[colleague].get(day.isoformat(), ""):
                    continue
                changes = [Change(proposer, day, None), Change(colleague, day, shift)]
                if not judge(roster, changes, NurseProfile.objects.get(user__username=proposer)).problems:
                    return {"proposer": proposer, "colleague": colleague, "date": day.isoformat(), "shift": shift}
    raise SystemExit(f"no sound two-cell swap on {roster.unit.name}'s {month:%Y-%m}")


print("E2E_SWAP " + json.dumps(a_sound_swap()))
