"""The request: what a caller sends the solver for one solve of one unit's month.

It is the only input the solver takes. Everything is explicit (every rule, switch and weight;
no unit's numbers live in the code), days are ISO dates, and the month itself is given:
the server derives the day count, the first weekday and the Ramadan days from it. The
loader validates the request, maps dates to the 0-based indices of the internal
`Problem`, and returns the problem together with the solver settings. Only ValueError
leaves, prefixed `request: <field path>: ...`.

    {"version": 2, "month": "2026-03",
     "ramadan_days": {"from": "2026-02-18", "to": "2026-03-19"},      # optional, see below
     "public_holidays": ["2026-03-10"],
     "general": {... every GeneralRules field, "weights": {... every Weights field}},
     "department": {... every DepartmentRules field},
     "nurses": [{"id": "N01", "gender": "M", "team_leader": true, "score": 0.99,
                 "ramadan_reduced": false, "breastfeeding": false, "ytd_overtime": 100,
                 "hours_balance": 0, "night_balance": 0.0, "friday_balance": 0.0,
                 "leave": ["2026-03-04"], "undesired_days": [], "desired_days": [],
                 "pinned": {"2026-03-01": "D"}, "allowed_shifts": null,
                 "previous": ["D", "D", "", "N", "N", "N", "", "", "D", "D"],
                 "last_shift_kind": null, "rest_owed": 0, "preferences_not_kept": 0,
                 "skills": {"BLS": "2026-03-14", "ICU": null}}],
     "solver": {"time_limit_seconds": 20, "reproducible": false},
     "published": {"N01": {"2026-03-01": "D", "2026-03-02": "", ...}},   # optional
     "replan_from": "2026-03-12",                                      # optional
     "renamed_shifts": {"N": "Night"}}                                 # optional

`ramadan_days`: absent or null = derived from the Umm al-Qura calendar; [] = none; a list of
dates = exactly those; {"from", "to"} = the inclusive range (which may start or end outside
the month, as Ramadan straddles months) intersected with the month, for the dates fixed
after the moon sighting. A nurse's six lists may be absent (absent = none); her scalars are
required, among them the two memories the rules keep across any number of days, which a
result carries over so the history itself stays one month long: `last_shift_kind`, the kind
("night" / "day" for any other work / null when none is known) of the last shift she worked
before `previous` begins (the switch rule's memory; a kind, so it survives a renamed or
retired shift), and `rest_owed`, the off days still owed to her last night before `previous`
begins (the night-rest rule's memory, 0 when none). One more carried scalar is optional, absent
meaning 0, so a request made before it existed still loads: `preferences_not_kept`, how many of
her `undesired_days` and `desired_days` the month before did not keep; above 0, each of hers this
month costs twice its weight. A name in `previous` the unit no longer
defines is accepted as work of no kind (neither night nor day; precheck says so);
`renamed_shifts` maps the old name of a renamed shift to its current one in every history.
`published` is the roster in force, in the shape the result's `roster` has, and
`replan_from` the first date to re-plan (earlier days are recorded facts); the first day
after the month marks the whole month as elapsed, for the month-end reconciliation that
turns the recorded facts into next month's carry-over.

Version 2 adds skills. `general` gains `required_skills` (the
skill names required to work) and `weights.skill_shortfall`; `department` gains `skill_needs`
(a list of {"shift", "skill", "weekday_min", "weekend_min"}, a shift of the unit's, one need a
shift and skill) and `skill_needs_hard`; all four are required. A nurse's `skills` (optional,
absent = none recorded) maps each skill name to its expiry, the last day it counts, or null for
one that counts all month: the one dated field accepted before the month (a skill expired before
it is sent with that date, since absence means no record), while a date after the month is
refused (it counts all month: null). Version 1 is still read, as version 2 with no skills, no
needs, the needs hard, no required skills and the weight 0, since every stored input is version
1; it may not carry any of those five fields. The result's version is unchanged.

Full runs add two fields, optional in either version so every
stored input still loads and gives what it gave: `department.max_full_runs`, the most full runs (runs
of exactly `max_consecutive_shifts` shifts) a nurse may work in the month, a whole number 0..31,
absent or null = no cap; and `general.weights.excess_full_run`, the price of each one above it,
absent = 0. A full run counts in the month that holds its last day, one reaching the month's last
day included (`validate.count_full_runs`).

The overtime share adds five fields, optional in either version
for the same reason: `department.share_overtime` (absent = false, the rule off),
`department.overtime_tolerance` and `department.overtime_carry_cap` (whole hours 0..744, absent =
0), `general.weights.overtime_above_share` (absent = 0) and a nurse's `overtime_balance`, the hours
her overtime has stood above (+) or below (-) her overtime share so far this year (absent = 0; a
carried value, as `preferences_not_kept` is). The rule: `fairness.overtime_priced`.

Probation adds two nurse fields, optional in either version for the
same reason: `on_probation` (absent = false) and `preceptor`, the id of another nurse of the input she follows
(absent or null = none). Refused: a preceptor that is not another nurse of the input, one herself on probation, one
named by two nurses, and a preceptor on a nurse not on probation. What they mean: `problem.counted`,
`Problem.allowed`, `Problem.overtime_cap` and the `following` rule (`validate._following`).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from .calendar import MonthInfo, parse_date, parse_month
from .problem import MAX_HOURS, SCHEMA_VERSION, Problem, read_json

REQUEST_VERSION = 2           # what a caller writes; READABLE_VERSIONS: what the loader still reads
READABLE_VERSIONS = (1, 2)
REPRODUCIBLE_WALL_FACTOR = 10    # reproducible: wall-clock guard = this x the deterministic budget.
                                 # CP-SAT's deterministic clock runs slower than the wall clock with
                                 # one worker (4-5 s per unit measured on a 10-nurse month), so the
                                 # guard must leave room for the budget to be what stops the search

_ROOT_REQUIRED = ("version", "month", "public_holidays", "general", "department", "nurses", "solver")
_ROOT_OPTIONAL = ("ramadan_days", "published", "replan_from", "renamed_shifts")
_NURSE_REQUIRED = ("id", "gender", "team_leader", "score", "ramadan_reduced", "breastfeeding",
                   "ytd_overtime", "hours_balance", "night_balance", "friday_balance",
                   "last_shift_kind", "rest_owed")
_KINDS = (None, "night", "day")
_NURSE_DATES = ("leave", "undesired_days", "desired_days")
_NURSE_OPTIONAL = _NURSE_DATES + ("pinned", "allowed_shifts", "previous", "preferences_not_kept", "overtime_balance",
                                  "skills", "on_probation", "preceptor")
# the fields version 2 adds, with the values a version-1 request is read with (no skills)
_V2_GENERAL = {"required_skills": []}
_V2_WEIGHTS = {"skill_shortfall": 0}
_V2_DEPARTMENT = {"skill_needs": [], "skill_needs_hard": True}
# the full-run fields, optional in either version, with the values an input without them is read with
_FULL_RUN_DEPARTMENT = {"max_full_runs": None}
_FULL_RUN_WEIGHTS = {"excess_full_run": 0}
# the overtime-share fields, likewise: the rule off, nothing free, nothing carried, no price
_OVERTIME_SHARE_DEPARTMENT = {"share_overtime": False, "overtime_tolerance": 0, "overtime_carry_cap": 0}
_OVERTIME_SHARE_WEIGHTS = {"overtime_above_share": 0}
_SOLVER_FIELDS = ("time_limit_seconds", "reproducible")
_DAY_REF = re.compile(r"\bday (\d+)\b")


@dataclass(frozen=True, slots=True)
class SolverSettings:
    time_limit_seconds: float
    reproducible: bool

    def solve_kwargs(self, workers: int) -> dict:
        """Arguments for `model.solve`. Reproducible: one worker, a fixed seed and a
        deterministic-time budget of `time_limit_seconds` units (CP-SAT's own clock, which
        runs slower than the wall clock), so the same request gives the same roster on any
        machine; the wall clock, at REPRODUCIBLE_WALL_FACTOR times the budget, then only
        stops a runaway search (the result's `solver.deterministic` says if it did)."""
        t = self.time_limit_seconds
        if self.reproducible:
            return dict(time_limit=REPRODUCIBLE_WALL_FACTOR * t, workers=1, seed=0, deterministic_time=t)
        return dict(time_limit=t, workers=workers, seed=0)


@dataclass(frozen=True, slots=True)
class Request:
    """Frozen, like its month and settings: what the result reports about the calendar, the
    cutoff and the solver settings is bound to what was loaded, and `build_result` checks it
    against the solution. The problem is a mutable object the solver snapshots on entry."""
    month: MonthInfo
    problem: Problem                 # calendar applied, published set, replan_from applied
    solver: SolverSettings
    replan_from: int | None          # 0-based cutoff (== days: all elapsed), None: all planned
    differ: int                      # recorded past cells that differ from the published plan


def read_request(path: str | Path) -> Request:
    return load_request(read_json(path, where="request"))


def load_request(raw: object) -> Request:
    if not isinstance(raw, dict):
        raise ValueError("request: must be a JSON object")
    _keys(raw, "request", _ROOT_REQUIRED, _ROOT_OPTIONAL)
    version = raw["version"]
    if isinstance(version, bool) or not isinstance(version, (int, float)) or version not in READABLE_VERSIONS:
        raise ValueError(f"request: version must be {' or '.join(map(str, READABLE_VERSIONS))}, got {version!r}")
    general, department = raw["general"], raw["department"]
    if version == 1:
        general, department = _version_1(raw)
    general, department = _optional_rules(general, department)
    try:
        month = parse_month(raw["month"])
    except ValueError as e:
        raise ValueError(f"request: {e}") from None
    ramadan = _ramadan(month, raw.get("ramadan_days"))
    holidays = _dates(month, raw["public_holidays"], "request: public_holidays")
    shifts = _shift_names(raw["department"])
    renames = _renames(raw.get("renamed_shifts"), shifts)
    nurses, ids = [], []
    for i, nd in enumerate(_list(raw["nurses"], "request: nurses")):
        if version == 1 and isinstance(nd, dict) and "skills" in nd:
            raise ValueError(f"request: nurses[{i}]: unknown field(s) ['skills'] (version 1 has no skills)")
        n = _nurse(month, nd, f"request: nurses[{i}]", shifts, renames)
        nurses.append(n)
        ids.append(n["id"])
    _preceptors(nurses)
    settings = _settings(raw["solver"])
    published = _published(month, raw.get("published"), ids, shifts) if "published" in raw else None
    replan = None
    if "replan_from" in raw and raw["replan_from"] is not None:
        replan = month.cutoff_index(raw["replan_from"], "request: replan_from")
        if published is None:
            raise ValueError("request: replan_from needs a published roster")
    d = {"version": SCHEMA_VERSION, "days": month.days, "first_weekday": month.first_weekday, "month": month.label,
         "nurses": nurses, "general": general, "department": department,
         "public_holidays": holidays, "ramadan_days": ramadan, "published": published, "planning_from": 0}
    try:
        p = Problem.from_dict(d, check_planning=replan is None)
        differ = p.replan_from(replan) if replan is not None else 0
    except ValueError as e:
        raise ValueError(_translate(str(e), month)) from None
    return Request(month, p, settings, replan, differ)


# ---- pieces ---------------------------------------------------------------------------------------
def _version_1(raw: dict) -> tuple[object, object]:
    """A version-1 request's `general` and `department` as version 2 reads them: copies with no
    required skills, a skill shortfall weight of 0, no skill needs and the needs hard. A block
    that is not an object is left for the problem's own check to name; one that already carries a
    field of version 2 is refused, so no request is read half one version and half the other."""
    def upgraded(block: object, added: dict, where: str) -> object:
        if not isinstance(block, dict):
            return block
        clash = sorted(set(block) & set(added))
        if clash:
            raise ValueError(f"request: {where}: unknown field(s) {clash} (version 1 has no skills)")
        return {**block, **added}
    general = upgraded(raw["general"], _V2_GENERAL, "general")
    if isinstance(general, dict) and "weights" in general:   # missing: the problem names it
        general["weights"] = upgraded(general["weights"], _V2_WEIGHTS, "general.weights")
    return general, upgraded(raw["department"], _V2_DEPARTMENT, "department")


def _optional_rules(general: object, department: object) -> tuple[object, object]:
    """`general` and `department` with the rule fields a request may leave out filled, on copies:
    the full runs' (no cap, `department.max_full_runs` null, and `weights.excess_full_run` 0) and
    the overtime share's (off, 0 hours free, 0 carried, `weights.overtime_above_share` 0), so an
    input made before them, of either version, loads and gives what it gave. A block that is not
    an object, or weights missing or not an object, are left for the problem's own check to name."""
    if isinstance(department, dict):
        department = {**_FULL_RUN_DEPARTMENT, **_OVERTIME_SHARE_DEPARTMENT, **department}
    if isinstance(general, dict) and isinstance(general.get("weights"), dict):
        general = {**general, "weights": {**_FULL_RUN_WEIGHTS, **_OVERTIME_SHARE_WEIGHTS, **general["weights"]}}
    return general, department


def _keys(raw: object, where: str, required: tuple, optional: tuple = ()) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: expected an object, got {type(raw).__name__}")
    unknown = set(raw) - set(required) - set(optional)
    if unknown:
        raise ValueError(f"{where}: unknown field(s) {sorted(unknown)}")
    missing = [k for k in required if k not in raw]
    if missing:
        raise ValueError(f"{where}: missing field(s) {missing}")
    return raw


def _list(v: object, where: str) -> list:
    if not isinstance(v, list):
        raise ValueError(f"{where}: must be a list, got {type(v).__name__}")
    return v


def _dates(month: MonthInfo, v: object, where: str) -> list[int]:
    out = [month.index(x, f"{where}[{i}]") for i, x in enumerate(_list(v, where))]
    seen: set[int] = set()
    for d in out:
        if d in seen:
            raise ValueError(f"{where}: {month.iso(d)} is listed twice")
        seen.add(d)
    return out


def _ramadan(month: MonthInfo, v: object) -> list[int]:
    where = "request: ramadan_days"
    if v is None:
        return list(month.ramadan_days)
    if isinstance(v, dict):
        _keys(v, where, ("from", "to"))
        lo, hi = parse_date(v["from"], f"{where}.from"), parse_date(v["to"], f"{where}.to")
        if lo > hi:
            raise ValueError(f"{where}: from {v['from']} is after to {v['to']}")
        days = [d for d in range(month.days) if lo <= month.date(d) <= hi]
        if not days:
            raise ValueError(f"{where}: no day of {month.label} lies within {v['from']}..{v['to']}")
        return days
    return _dates(month, v, where)


def _shift_names(department: object) -> list[str] | None:
    """The unit's shift names when the department block names them properly, so a shift a
    nurse refers to can be checked here with its date; otherwise `Problem.check` reports."""
    if isinstance(department, dict) and isinstance(department.get("shifts"), list) \
            and all(isinstance(s, str) for s in department["shifts"]):
        return department["shifts"]
    return None


def _shift(v: object, where: str, shifts: list[str] | None, *, off_ok: bool) -> None:
    if not isinstance(v, str) or (shifts is not None and v != "" and v not in shifts) or (v == "" and not off_ok):
        raise ValueError(f"{where}: {v!r} is not a department shift")


def _renames(raw: object, shifts: list[str] | None) -> dict[str, str]:
    """`renamed_shifts`: {old name: current name}, applied to every history (`previous`)."""
    where = "request: renamed_shifts"
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: must be an object {{old name: current name}}")
    for old, new in raw.items():
        if not isinstance(old, str) or not old.strip():       # "" is an off day, never a shift
            raise ValueError(f"{where}: {old!r} is not a shift name")
        if not isinstance(new, str) or (shifts is not None and new not in shifts):
            raise ValueError(f"{where}[{old}]: {new!r} is not a department shift")
        if shifts is not None and old in shifts:
            raise ValueError(f"{where}: {old!r} is still a department shift")
    return dict(raw)


def _nurse(month: MonthInfo, nd: object, where: str, shifts: list[str] | None,
           renames: dict[str, str] = {}) -> dict:
    _keys(nd, where, _NURSE_REQUIRED, _NURSE_OPTIONAL)
    if not isinstance(nd["id"], str) or not nd["id"].strip():
        raise ValueError(f"{where}.id: must be a non-empty string, got {nd['id']!r}")
    n = {k: nd[k] for k in _NURSE_REQUIRED}
    for key, target in (("leave", "unavailable"), ("undesired_days", "undesired_days"),
                        ("desired_days", "desired_days")):
        if key in nd:
            n[target] = _dates(month, nd[key], f"{where}.{key}")
    both = set(n.get("undesired_days", ())) & set(n.get("desired_days", ()))
    if both:
        raise ValueError(f"{where}: {month.iso(min(both))} is both desired and undesired")
    if "pinned" in nd:
        pins = nd["pinned"]
        if not isinstance(pins, dict):
            raise ValueError(f"{where}.pinned: must be an object {{date: shift or \"\"}}")
        leave = set(n.get("unavailable", ()))
        n["pinned"] = {}
        for text, s in pins.items():
            d = month.index(text, f"{where}.pinned")
            _shift(s, f"{where}.pinned[{text}]", shifts, off_ok=True)
            if s and d in leave:
                raise ValueError(f"{where}.pinned[{text}]: on leave but pinned to {s!r}")
            n["pinned"][d] = s
    if "allowed_shifts" in nd:
        allowed = nd["allowed_shifts"]
        if allowed is not None:
            for i, s in enumerate(_list(allowed, f"{where}.allowed_shifts")):
                _shift(s, f"{where}.allowed_shifts[{i}]", shifts, off_ok=False)
        n["allowed_shifts"] = allowed
    if "previous" in nd:                                      # any shift name: one the unit no longer
        prev = []                                             # defines is work of no kind (precheck)
        for i, s in enumerate(_list(nd["previous"], f"{where}.previous")):
            if not isinstance(s, str) or (s != "" and not s.strip()):
                raise ValueError(f"{where}.previous[{i}]: {s!r} is not a shift name or \"\"")
            prev.append(renames.get(s, s) if s else s)        # an off day is never renamed
        n["previous"] = prev
    if nd["last_shift_kind"] not in _KINDS:
        raise ValueError(f"{where}.last_shift_kind: must be \"night\", \"day\" or null, got {nd['last_shift_kind']!r}")
    owed = nd["rest_owed"]
    if not isinstance(owed, int) or isinstance(owed, bool) or owed < 0:
        raise ValueError(f"{where}.rest_owed: must be a whole number of days, 0 or more, got {owed!r}")
    if "preferences_not_kept" in nd:
        unkept = nd["preferences_not_kept"]
        if not isinstance(unkept, int) or isinstance(unkept, bool) or unkept < 0:
            raise ValueError(f"{where}.preferences_not_kept: must be a whole number, 0 or more, got {unkept!r}")
        n["preferences_not_kept"] = unkept
    if "overtime_balance" in nd:
        balance = nd["overtime_balance"]
        if isinstance(balance, bool) or not isinstance(balance, (int, float)) or not math.isfinite(balance) \
                or abs(balance) > MAX_HOURS:
            raise ValueError(f"{where}.overtime_balance: must be a number of hours within ±{MAX_HOURS}, got {balance!r}")
        n["overtime_balance"] = balance
    if "skills" in nd:
        n["skills"] = _skills(month, nd["skills"], f"{where}.skills")
    if "on_probation" in nd:
        if not isinstance(nd["on_probation"], bool):
            raise ValueError(f"{where}.on_probation: must be true or false, got {nd['on_probation']!r}")
        n["on_probation"] = nd["on_probation"]
    if "preceptor" in nd:
        q = nd["preceptor"]
        if q is not None and not isinstance(q, str):
            raise ValueError(f"{where}.preceptor: must be a nurse id or null, got {q!r}")
        n["preceptor"] = q
    return n


def _preceptors(nurses: list[dict]) -> None:
    """Each preceptor named is another nurse of the input, not herself on probation, named by one nurse only, and only
    for a nurse on probation (the problem checks the same by id, for a problem file)."""
    by_id = {n["id"]: n for n in nurses}
    precepting: dict[str, int] = {}
    for i, n in enumerate(nurses):
        q, where = n.get("preceptor"), f"request: nurses[{i}].preceptor"
        if q is None:
            continue
        if not n.get("on_probation", False):
            raise ValueError(f"{where}: {q!r} is named for a nurse not on probation")
        if q == n["id"] or q not in by_id:
            raise ValueError(f"{where}: {q!r} is not another nurse of the input")
        if by_id[q].get("on_probation", False):
            raise ValueError(f"{where}: {q!r} is herself on probation")
        if q in precepting:
            raise ValueError(f"{where}: {q!r} already precepts nurses[{precepting[q]}] (one nurse on probation each)")
        precepting[q] = i


def _skills(month: MonthInfo, raw: object, where: str) -> dict[str, int | None]:
    """{skill: expiry date or null} -> {skill: last day index, None, or -1 before the month}. A
    name the problem checks; the date is the one accepted before the month, never after it."""
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: must be an object {{skill: expiry date or null}}")
    out: dict[str, int | None] = {}
    first, last = month.date(0), month.date(month.days - 1)
    for skill, text in raw.items():
        if text is None:
            out[skill] = None
            continue
        x = parse_date(text, f"{where}[{skill}]")
        if x > last:
            raise ValueError(f"{where}[{skill}]: {text} is after {month.label} (a skill that counts all "
                             f"month has no expiry: null)")
        out[skill] = -1 if x < first else x.day - 1
    return out


def _settings(raw: object) -> SolverSettings:
    where = "request: solver"
    _keys(raw, where, _SOLVER_FIELDS)
    t = raw["time_limit_seconds"]
    if not isinstance(t, (int, float)) or isinstance(t, bool) or not math.isfinite(t) or t <= 0:
        raise ValueError(f"{where}.time_limit_seconds: must be a positive number of seconds, got {t!r}")
    if not isinstance(raw["reproducible"], bool):
        raise ValueError(f"{where}.reproducible: must be true or false, got {raw['reproducible']!r}")
    return SolverSettings(float(t), raw["reproducible"])


def _published(month: MonthInfo, raw: object, ids: list[str], shifts: list[str] | None) -> list[list[str]] | None:
    where = "request: published"
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: must be an object {{nurse id: {{date: shift or \"\"}}}}")
    unknown = set(raw) - set(ids)
    if unknown:
        raise ValueError(f"{where}: unknown nurse {sorted(unknown)[0]!r}")
    dates = month.dates()
    out = []
    for nid in ids:
        if nid not in raw:
            raise ValueError(f"{where}: no roster for nurse {nid!r}")
        row = raw[nid]
        if not isinstance(row, dict):
            raise ValueError(f"{where}[{nid}]: must be an object {{date: shift or \"\"}}")
        for text in row:
            month.index(text, f"{where}[{nid}]")
        missing = [t for t in dates if t not in row]
        if missing:
            raise ValueError(f"{where}[{nid}]: missing date {missing[0]}")
        cells = []
        for text in dates:
            _shift(row[text], f"{where}[{nid}][{text}]", shifts, off_ok=True)
            cells.append(row[text])
        out.append(cells)
    return out


def _translate(msg: str, month: MonthInfo) -> str:
    """A `Problem` error as a request error: its prefix, and any 0-based day it names."""
    if msg.startswith("problem: "):
        msg = msg[len("problem: "):]
    elif msg.startswith("problem "):
        msg = msg[len("problem "):]

    def to_date(m: re.Match) -> str:
        d = int(m.group(1))
        return month.iso(d) if 0 <= d < month.days else m.group(0)
    return "request: " + _DAY_REF.sub(to_date, msg)
