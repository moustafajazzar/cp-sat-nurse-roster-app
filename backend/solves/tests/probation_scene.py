"""A probation scene, on the solves app's: the solver's four-nurse sample as ER, today 15 May 2026, and Faten, who started on
1 April 2026 on probation. Her 3 months are April, May and June (she joined on the 1st, so April counts): her probation's last
day is 30 June 2026, counted by hand, and in July she is rostered as every other nurse. Amal holds Preceptorship, a skill marked
"Lets a nurse precept", and precepts her from May. A roster here can come from the real solver, run in this process with the
input the app assembled, so a test judges a swap or a cover on a roster the solver itself made beside her preceptor."""
from __future__ import annotations

import datetime as dt

from nurse_roster.model import build_model, solve
from nurse_roster.precheck import precheck
from nurse_roster.request import load_request
from nurse_roster.result import build_result

from accounts.models import User
from hospital.models import Skill
from nurses.models import NurseProfile, NurseSkill
from solves.tests.conftest import job, nurse_in


def precepting_skill() -> Skill:
    return Skill.objects.get_or_create(name="Preceptorship", defaults={"expires": False, "required_to_work": False,
                                                                       "lets_a_nurse_precept": True})[0]


def holds_the_skill(hospital_id: str) -> None:
    NurseSkill.objects.create(nurse=NurseProfile.objects.get(user__username=hospital_id), skill=precepting_skill(),
                              recorded_by=User.objects.get(username="mona"))


def add_faten(unit, make_user, *, start=dt.date(2026, 4, 1)) -> NurseProfile:
    return nurse_in(unit, make_user, "Faten", full_name="Faten Ali", start=start, on_probation=True)


def assign(client, unit, nurse_id, preceptor_hospital_id: str, month: str):
    preceptor = NurseProfile.objects.get(user__username=preceptor_hospital_id)
    response = client.put(f"/api/units/{unit.pk}/nurses/{nurse_id}/preceptor",
                          {"preceptor_id": str(preceptor.pk), "from_month": month}, content_type="application/json")
    assert response.status_code == 200, response.content
    return response.json()


def real_result(solve_input: dict, seconds: float = 10) -> dict:
    """The real solver's result for this input, in this process: the service's own steps (`nurse_roster.__main__.run`)."""
    request = load_request(solve_input | {"solver": {"time_limit_seconds": seconds, "reproducible": True}})
    model = build_model(request.problem)
    found = solve(model, request.problem, **request.solver.solve_kwargs(1))
    return build_result(request, model, found, precheck=precheck(request.problem), workers=1)


def real_job(solve_input: dict, seconds: float = 10) -> dict:
    result = real_result(solve_input, seconds)
    return job("finished", result=result, best_cost=result["cost"], improvements=1)


def by_row(roster: dict[str, dict[str, str]], hospital_id: str) -> str:
    """A row as one line, a dot for a day off: "DD.N…", day 1 first."""
    return "".join(roster[hospital_id][day] or "." for day in sorted(roster[hospital_id]))
