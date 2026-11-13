"""The ER instance the solver was shaped on (25 staff, real scores), as test data.

The package carries no unit's rules: every rule, switch and weight is explicit in the input.
These factories spell out the ER configuration for the tests (factories, not constants, because
tests mutate rule objects in place), `generate` builds a random but realistic ER month, and the
dict helpers build complete problem dicts for the JSON contract tests.
"""
from __future__ import annotations

import copy
import json
import random
from datetime import timedelta
from dataclasses import asdict

from nurse_roster.problem import (MAX_DAYS, MAX_HISTORY, MAX_NURSES, SCHEMA_VERSION, DepartmentRules,
                                  GeneralRules, Headcount, Nurse, Problem, Weights)

# Real ER scores (25 staff), assigned in order so the 6 team leaders are N01..N06.
ER_SCORES = [0.99, 0.99, 0.95, 0.95, 0.92, 0.92, 0.89, 0.79, 0.79, 0.79, 0.78, 0.77, 0.71,
             0.71, 0.70, 0.70, 0.69, 0.68, 0.68, 0.64, 0.52, 0.50, 0.50, 0.48, 0.45]


def er_weights() -> Weights:
    return Weights(
        night_rest=5,          # per N -> D with too little rest
        dept_overtime=1,       # per hour over the department cap
        nurse_overtime=1,      # per hour of individual overtime
        nurse_shortfall=1,     # per hour under contract carried to next month
        avoidable_shortfall=1000,   # per hour short beyond that remainder and what her days cannot give:
                               # a shift idled for a preference costs 12,000, more than a whole month
        fair_day=1,            # per 0.1 shift of D deviation beyond the tolerance
        fair_night=1,          # same for N
        fair_weekend=1,        # per 0.1 shift of Friday deviation
        score_deficit=2,       # per 0.01 score-point x person below the target
        score_spread=1,        # per 0.01 above the band ceiling
        excess_weekend=6,      # per weekend worked above the maximum
        shift_switch=8,        # per D/N switch above the maximum
        undesired_day=2,       # per requested-off day worked
        desired_day=1,         # per requested-work day not worked
        change=3,              # per cell that differs from the published roster
    )


def er_general() -> GeneralRules:
    """Hospital-wide rules as confirmed with the ward."""
    return GeneralRules(
        shift_hours={"D": 12, "N": 12, "A": 8, "B": 8, "C": 8},
        hours_per_day=8,
        ramadan_hours_per_day=6,
        breastfeeding_reduction=1,
        weekend_weekdays=[4],                                 # Friday
        forbidden_successions=[("D", "C"), ("N", "D"), ("N", "A"), ("N", "B"), ("A", "B"),
                               ("A", "C"), ("B", "D"), ("C", "D"), ("C", "A"), ("C", "B")],
        forbidden_successions_enabled=True,
        hours_rule_enabled=True,
        overtime_caps_enabled=True,
        overtime_month_cap=40,
        overtime_year_cap=720,
        dept_overtime_cap=400,
        weights=er_weights(),
    )


def er_department() -> DepartmentRules:
    """The ER: D/N 12 h shifts, 7-9 per shift (8-9 on Fridays), pure D or N blocks."""
    return DepartmentRules(
        name="ER",
        shifts=["D", "N"],
        headcounts=[Headcount("D", 7, 9, 8, 9), Headcount("N", 7, 9, 8, 9)],
        headcount_enabled=True,
        require_team_leader=True,
        require_both_genders=True,
        score_target=0.70,
        score_target_weekend=0.80,
        score_balance=True,
        score_balance_mode="band",
        score_band_hi=0.78,
        score_band_hi_weekend=0.88,
        night_shift="N",
        day_shift="D",
        night_rest_days=2,
        night_rest_hard=True,
        fair_shifts=["D", "N"],
        fair_tolerance=0.5,
        fair_weekends=True,
        carry_fairness=True,
        carry_cap=2.0,
        max_consecutive_shifts=3,
        double_cycle_rest=2,
        max_weekends=3,
        no_mixed_blocks=True,
        max_switches=2,
        switches_hard=False,
    )


# ---- complete problem dicts for the JSON contract tests ------------------------------------------
def nurse(id: str = "A", **over) -> dict:
    """A nurse dict with every required scalar; overrides may be deliberately malformed."""
    d = {"id": id, "gender": "F", "team_leader": False, "score": 0.8, "ramadan_reduced": False,
         "breastfeeding": False, "ytd_overtime": 0, "hours_balance": 0, "night_balance": 0.0,
         "friday_balance": 0.0, "last_shift_kind": None, "rest_owed": 0}
    d.update(over)
    return d


def gen(**over) -> dict:
    d = asdict(er_general())
    d["forbidden_successions"] = [list(t) for t in d["forbidden_successions"]]
    d.update(over)
    return d


def dep(**over) -> dict:
    d = asdict(er_department())
    d.update(over)
    return d


def raw(**over) -> dict:
    """A complete, valid problem dict (7 days from a Monday, nurses A and B)."""
    d = {"version": SCHEMA_VERSION, "days": 7, "first_weekday": 0, "nurses": [nurse("A"), nurse("B")],
         "general": gen(), "department": dep()}
    d.update(over)
    return d


# ---- random but realistic ER month -------------------------------------------------------------
def _history(rng: random.Random, days: int, shifts=("D", "N")) -> list[str]:
    """Plausible tail of last month: work blocks of 1-3, off blocks of 1-2, no N->D."""
    out: list[str] = []
    while len(out) < days:
        for _ in range(rng.randint(1, 3)):
            if out and out[-1] == "N":
                out.append("N")
            else:
                out.append(rng.choice(shifts))
        out.extend([""] * rng.randint(1, 2))
    return out[-days:]


def generate(n_nurses: int = 25, days: int = 31, first_weekday: int = 0, seed: int = 0,
             ramadan: bool = False, holidays: list[int] | None = None,
             team_leaders: int = 6, history_days: int = 10,
             only_shift: dict[int, str] | None = None) -> Problem:
    """only_shift: 1-based staff number -> the only shift that nurse works. ER default:
    staff 4 and 11 work D only, staff 13 works N only."""
    # bounds first, before anything is allocated (the contract would reject it later anyway)
    if not 1 <= n_nurses <= MAX_NURSES:
        raise ValueError(f"n_nurses must be within 1..{MAX_NURSES}, got {n_nurses}")
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be within 1..{MAX_DAYS}, got {days}")
    if not 0 <= history_days <= MAX_HISTORY:
        raise ValueError(f"history_days must be within 0..{MAX_HISTORY}, got {history_days}")
    if only_shift is None:
        only_shift = {4: "D", 11: "D", 13: "N"}
    rng = random.Random(seed)
    nurses = []
    for i in range(n_nurses):
        only = only_shift.get(i + 1)
        leave = sorted(rng.sample(range(days), min(days, rng.choice([0, 0, 0, 1, 2, 5]))))
        free = [d for d in range(days) if d not in leave]
        asks = rng.sample(free, min(3, len(free))) if rng.random() < 0.1 else []   # 2 off + 1 on
        nurses.append(Nurse(
            id=f"N{i + 1:02d}",
            allowed_shifts=[only] if only else None,
            gender="M" if rng.random() < 0.45 else "F",
            team_leader=i < team_leaders,
            score=ER_SCORES[i] if i < len(ER_SCORES) else round(rng.uniform(0.45, 0.99), 2),
            ramadan_reduced=rng.random() < 0.3,
            breastfeeding=rng.random() < 0.1,
            ytd_overtime=rng.choice([0, 100, 300, 500, 700]),
            unavailable=leave,
            undesired_days=sorted(asks[:2]),
            desired_days=sorted(asks[2:]),
            previous=_history(rng, history_days, (only,) if only else ("D", "N")) if history_days else [],
        ))
    return Problem(days=days, first_weekday=first_weekday, nurses=nurses,
                   general=er_general(), department=er_department(),
                   public_holidays=holidays or [],
                   ramadan_days=list(range(days)) if ramadan else [])


# ---- requests ----------------------------------------------------------------------------------
_SCALARS = ("id", "gender", "team_leader", "score", "ramadan_reduced", "breastfeeding",
            "ytd_overtime", "hours_balance", "night_balance", "friday_balance", "last_shift_kind", "rest_owed",
            "preferences_not_kept")


def request_of(p: Problem, month: str, *, time_limit: float = 5, reproducible: bool = False,
               replan_from: int | None = None) -> dict:
    """The request that loads to `p` (the inverse of `request.load_request`): `p` must be
    shaped like `month` (day count and first weekday); its indices become dates."""
    from nurse_roster.calendar import parse_month
    info = parse_month(month)
    if (info.days, info.first_weekday) != (p.days, p.first_weekday):
        raise ValueError(f"{month} has {info.days} days from weekday {info.first_weekday}; "
                         f"the problem has {p.days} from {p.first_weekday}")
    if p.month is not None and p.month != month:
        raise ValueError(f"the problem was built for {p.month}, not {month}")
    d = json.loads(json.dumps(asdict(p)))                     # tuples -> lists, int keys -> str
    nurses = []
    for nu in d["nurses"]:
        n = {k: nu[k] for k in _SCALARS}
        n["leave"] = [info.iso(x) for x in nu["unavailable"]]
        n["undesired_days"] = [info.iso(x) for x in nu["undesired_days"]]
        n["desired_days"] = [info.iso(x) for x in nu["desired_days"]]
        n["pinned"] = {info.iso(int(k)): v for k, v in nu["pinned"].items()}
        n["allowed_shifts"] = nu["allowed_shifts"]
        n["previous"] = nu["previous"]
        if nu["skills"]:                                      # -1: any day before the month
            before = (info.date(0) - timedelta(days=1)).isoformat()
            n["skills"] = {k: None if v is None else before if v < 0 else info.iso(v)
                           for k, v in nu["skills"].items()}
        nurses.append(n)
    req = {"version": 2, "month": month,
           "ramadan_days": [info.iso(x) for x in p.ramadan_days],
           "public_holidays": [info.iso(x) for x in p.public_holidays],
           "general": d["general"], "department": d["department"], "nurses": nurses,
           "solver": {"time_limit_seconds": time_limit, "reproducible": reproducible}}
    if p.published is not None:
        req["published"] = {nu.id: {info.iso(x): c for x, c in enumerate(row)}
                            for nu, row in zip(p.nurses, p.published)}
    if replan_from is not None:
        req["replan_from"] = info.cutoff_iso(replan_from)
    return req


def er_request(month: str = "2026-03", seed: int = 0, **kw) -> dict:
    """A random ER month for `month` (Ramadan from the calendar), as a request."""
    from nurse_roster.calendar import parse_month
    info = parse_month(month)
    p = generate(25, info.days, info.first_weekday, seed, False, [])
    p.ramadan_days = list(info.ramadan_days)
    return request_of(p, month, **kw)


def write_request(req: dict, path) -> None:
    with open(path, "w") as f:
        json.dump(req, f, indent=1)


def version_1(raw: dict) -> dict:
    """A version 2 request with no skills written as version 1, as every input stored before version 2 was. The
    four-nurse sample is version 2, with no skills, needs or required skills."""
    out = copy.deepcopy(raw)
    out["version"] = 1
    del out["general"]["required_skills"], out["general"]["weights"]["skill_shortfall"]
    del out["department"]["skill_needs"], out["department"]["skill_needs_hard"]
    for nurse in out["nurses"]:
        nurse.pop("skills", None)
    return out
