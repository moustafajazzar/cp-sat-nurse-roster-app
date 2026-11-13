# The data the browser checks need, on a scratch stack's empty database: run by e2e/stack.sh, never on an installation's own.
#
#   docker compose exec -T -e SEED_PASSWORD=… web python manage.py shell < e2e/seed.py
#
# Every month here is solved for real in-process, submitted and approved, on the real clock; the seed bypasses the pages'
# guards (a month over cannot be submitted there).
#
# - ER: the solver's four-nurse sample (its JSON copied to /tmp/four_nurses.json first), Mona its manager.
#   Last month approved and not closed, so the hourly catch-up closes it; next month approved; a request window
#   for the month after open from today. The nurses start two months back, so last month is theirs.
# - ICU: sixty-four nurses, sixteen of each of the sample's, its headcounts sixteen times the sample's: past the
#   sixty a roster grid is windowed above, and the size an installation's timings are taken on. Omar its manager has a nurse
#   profile there, one of the sixty-four. Next month approved; the month
#   after a draft.
# - Carla the CNO, Hala the Deputy CNO. The System Admin `admin` that `prepare` made takes the seed's password too, with no
#   change due, so a check can sign in.
#
# It prints one line, `E2E_SEED {...}`, for e2e/stack.sh; a sound swap of each unit's approved month comes from
# e2e/sound_swap.py after it.
import copy
import datetime as dt
import json
import os

from django.db import transaction
from django.utils import timezone
from nurse_roster.model import build_model, solve
from nurse_roster.precheck import precheck
from nurse_roster.request import load_request
from nurse_roster.result import build_result

from accounts.models import Role, User
from hospital.catalogue import seed_catalogue
from hospital.models import HospitalSettings
from nurses import membership
from nurses.models import NurseProfile, Placement
from requesting import making
from rosters import drafts, review
from rosters.models import Roster
from solves.input import assemble
from solves.models import Solve, SolveState
from units.models import Unit

PASSWORD = os.environ["SEED_PASSWORD"]
SAMPLE = json.load(open("/tmp/four_nurses.json"))
NAMES = {"Amal": "Amal Saleh", "Badr": "Badr Harbi", "Dana": "Dana Odeh", "Emad": "Emad Nasser", "mona": "Mona Ali",
         "carla": "Carla Haddad", "hala": "Hala Mansour", "omar": "Omar Khalil"}
ICU_COPIES = 16

assert not Unit.objects.exists(), "e2e/seed.py runs once, on an empty scratch database"


def first_of_next(month: dt.date) -> dt.date:
    return (month + dt.timedelta(days=32)).replace(day=1)


today = membership.today()
this_month = today.replace(day=1)
last_month = (this_month - dt.timedelta(days=1)).replace(day=1)
started = (last_month - dt.timedelta(days=1)).replace(day=1)
approved_month = first_of_next(this_month)
window_month = first_of_next(approved_month)


def solved(unit: Unit, month: dt.date, manager: User, time_limit: int) -> Solve:
    """A real solve in this process, recorded as the worker records one."""
    raw = assemble(unit, month, time_limit=time_limit).input
    req = load_request(raw)
    model = build_model(req.problem)
    solution = solve(model, req.problem, **req.solver.solve_kwargs(4))
    result = build_result(req, model, solution, precheck=precheck(req.problem), workers=4)
    assert result["ok"], f"{unit.name} {month}: {result['message']}"
    now = timezone.now()
    return Solve.objects.create(unit=unit, month=month, requested_by=manager, time_limit_seconds=time_limit,
                                state=SolveState.SUCCEEDED, input=raw, result=result, started_at=now, finished_at=now,
                                best_cost=result["cost"])


def approved(unit: Unit, month: dt.date, manager: User, approver: User, time_limit: int) -> None:
    with transaction.atomic():
        done = solved(unit, month, manager, time_limit)
        roster = drafts.replace_grid(done, done.result["roster"])
        review.submit(roster, manager)
        review.approve(Roster.objects.get(pk=roster.pk), approver)


def nurse(user: User, unit: Unit, row: dict) -> None:
    profile = NurseProfile.objects.create(user=user, gender=row["gender"], start_date=started, team_leader=row["team_leader"],
                                          score=row["score"], attributes_saved=True)
    Placement.objects.create(nurse=profile, unit=unit, from_month=started, allowed_shifts=row.get("allowed_shifts"))


def rules_times(department: dict, copies: int) -> dict:
    rules = {k: v for k, v in copy.deepcopy(department).items() if k != "name"}
    for headcount in rules["headcounts"]:
        for key in ("weekday_min", "weekday_max", "weekend_min", "weekend_max"):
            headcount[key] *= copies
    return rules


with transaction.atomic():
    seed_catalogue()
    admin = User.objects.get(role=Role.SYSTEM_ADMIN)
    admin.set_password(PASSWORD)
    admin.must_change_password = False
    admin.save()
    mona = User.objects.create_user("mona", PASSWORD, Role.UNIT_MANAGER, full_name=NAMES["mona"])
    omar = User.objects.create_user("omar", PASSWORD, Role.UNIT_MANAGER, full_name=NAMES["omar"])
    carla = User.objects.create_user("carla", PASSWORD, Role.CNO, full_name=NAMES["carla"])
    hala = User.objects.create_user("hala", PASSWORD, Role.DEPUTY_CNO, full_name=NAMES["hala"])
    settings = HospitalSettings.load()
    # The catalogues give a solve input its shift hours and required skills.
    settings.rules = {k: v for k, v in copy.deepcopy(SAMPLE["general"]).items()
                      if k not in ("shift_hours", "required_skills")}
    settings.save()

    er = Unit.objects.create(name="ER", manager=mona, rules=rules_times(SAMPLE["department"], 1))
    for row in SAMPLE["nurses"]:
        nurse(User.objects.create_user(row["id"], PASSWORD, Role.STAFF_NURSE, full_name=NAMES[row["id"]]), er, row)

    icu = Unit.objects.create(name="ICU", manager=omar, rules=rules_times(SAMPLE["department"], ICU_COPIES))
    nurse(omar, icu, SAMPLE["nurses"][0])
    for number in range(1, ICU_COPIES * len(SAMPLE["nurses"])):
        username = f"icu{number:02d}"
        NAMES[username] = f"ICU Nurse {number:02d}"
        user = User.objects.create_user(username, PASSWORD, Role.STAFF_NURSE, full_name=NAMES[username])
        nurse(user, icu, SAMPLE["nurses"][number % len(SAMPLE["nurses"])])

approved(er, last_month, mona, carla, time_limit=10)
approved(er, approved_month, mona, carla, time_limit=10)
approved(icu, approved_month, omar, hala, time_limit=30)
with transaction.atomic():
    draft = solved(icu, window_month, omar, time_limit=30)
    drafts.replace_grid(draft, draft.result["roster"])

with transaction.atomic():
    closes = min(today + dt.timedelta(days=14), window_month - dt.timedelta(days=1))
    making.open_window(Unit.objects.select_for_update().get(pk=er.pk), window_month, today, closes, mona, None)

print("E2E_SEED " + json.dumps({
    "unit": "ER",
    "large_unit": "ICU",
    "names": {**NAMES, "admin": admin.full_name},
    "past_month": last_month.strftime("%Y-%m"),
    "approved_month": approved_month.strftime("%Y-%m"),
    "window_month": window_month.strftime("%Y-%m"),
}))
