"""Independent checker: re-derives every rule of the registry from a plain assignment
matrix without OR-Tools, one function per rule returning its matches (who, when, how
much). `validate` folds them into hard errors and the weighted soft cost; `explain.py`
keeps them for the per-match / per-nurse view."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

from .problem import (Nurse, Problem, barred_days, counted, expired_required, expiry_words, followable_candidates,
                      preceptor_of, preference_factor, skill_holders, superseding)
from . import fairness as F
from .rules import HARD, RULES, Match, active, level, soft_ids, weight


def matrix_errors(p: Problem, a) -> list[str]:
    """Shape and domain of an assignment matrix: nurses x days, integer shift index or -1."""
    S = len(p.department.shifts)
    if not isinstance(a, (list, tuple)) or len(a) != len(p.nurses):
        return [f"matrix: expected {len(p.nurses)} rows, got {len(a) if isinstance(a, (list, tuple)) else type(a).__name__}"]
    out = []
    for n, row in enumerate(a):
        if not isinstance(row, (list, tuple)) or len(row) != p.days:
            out.append(f"matrix: row {n} must have {p.days} cells")
            continue
        for d, v in enumerate(row):
            if isinstance(v, bool) or not isinstance(v, int) or not -1 <= v < S:
                out.append(f"matrix: row {n} day {d} holds {v!r}, want -1..{S - 1}")
    return out


@dataclass(slots=True)
class Ctx:
    """Everything the rule functions share, derived once per roster."""
    p: Problem
    a: list[list[int]]
    shifts: list[str]
    idx: dict[str, int]
    D: int
    N: int
    full: list[list[int]]          # history + month per nurse (shift index, -1 = off)
    H: list[int]                   # history length per nurse
    weekend_days: list[int]
    succ: set[tuple[int, int]]
    NI: int | None
    DI: int | None
    hrs: list[int]                 # hours worked in shifts per nurse
    allowed: list[set[str]]
    F: int                         # planning cutoff: month days before it are facts
    past_hrs: list[int]            # hours worked before the cutoff per nurse
    staff: list[int] = field(default_factory=list)                         # the counted nurses (problem.counted)
    pure: dict[tuple[int, int], list[int]] = field(default_factory=dict)   # (anchor, day) -> counted nurses


def _ctx(p: Problem, a: list[list[int]]) -> Ctx:
    dep = p.department
    shifts = dep.shifts
    idx = {s: i for i, s in enumerate(shifts)}
    D, N, S = p.days, len(p.nurses), len(shifts)
    full, H = [], []
    for n, nurse in enumerate(p.nurses):                      # a history name the unit no longer
        hist = [idx.get(s, S) if s else -1 for s in nurse.previous]   # defines: worked (index S),
        full.append(hist + list(a[n]))
        H.append(len(hist))
    hours = [p.general.shift_hours[s] for s in shifts]
    return Ctx(p, a, shifts, idx, D, N, full, H,
               [d for d in range(D) if p.is_weekend(d)],
               {(idx[x], idx[y]) for x, y in p.successions() if x in idx and y in idx},
               idx.get(dep.night_shift), idx.get(dep.day_shift),
               [sum(hours[s] for s in a[n] if s >= 0) for n in range(N)],
               [set(p.allowed(nu)) for nu in p.nurses],
               p.planning_from,
               [sum(hours[s] for s in a[n][:p.planning_from] if s >= 0) for n in range(N)],
               counted(p))


def count_switches(seq: list[int], H: int, NI: int | None, stop: int, last: bool | None = None) -> int:
    """Night/day switches on positions H <= t < stop of a history + month sequence (shift
    index, -1 = off); the history before H only sets the memory, which starts as `last` (the
    nurse's `last_shift_kind`, before her history; see `switch_seed`). Shared with the model,
    which needs the count on the elapsed days as a constant."""
    n = 0
    for t, v in enumerate(seq[:stop]):
        if v < 0:
            continue
        kind = v == NI
        if last is not None and kind != last and t >= H:
            n += 1
        last = kind
    return n


def last_worked(seq: list[str], fallback: str | None = None) -> str | None:
    """The last worked shift of a history (oldest first), or `fallback` when it has none."""
    for s in reversed(seq):
        if s:
            return s
    return fallback


def switch_seed(nurse: Nurse) -> bool | None:
    """The switch memory before the history (`last_shift_kind`): True night, False day work,
    None unknown. A kind, not a name, so it survives a renamed or retired shift."""
    return None if nurse.last_shift_kind is None else nurse.last_shift_kind == "night"


def switch_memory(nurse: Nurse, night: str) -> bool | None:
    """The switch rule's memory at the start of the month: whether the nurse's last worked
    shift, in her history or (`last_shift_kind`) before it, was the night shift; None when
    none is known, however many off days her history shows. A history name the unit no
    longer defines is not the night shift (`renamed_shifts` maps a renamed one)."""
    s = last_worked(nurse.previous)
    return switch_seed(nurse) if s is None else s == night


def rest_owed_after(owed: int, seq: list[str], night: str, day: str, R: int) -> int:
    """Off days still owed to the last night after walking `seq` (oldest first) from a state
    owing `owed` (clamped to R): a night owes R again, the day shift ends the rule, an off
    day pays one back, other work pays nothing. The night-rest rule's memory across a history
    of any length, and what the result carries as next month's `rest_owed`."""
    owed = min(max(0, owed), R)
    for s in seq:
        if s == night:
            owed = R
        elif s == day:
            owed = 0
        elif s == "":
            owed = max(0, owed - 1)
    return owed


def _pure(c: Ctx, ai: int, d: int) -> list[int]:
    key = (ai, d)
    if key not in c.pure:
        c.pure[key] = [n for n in c.staff if c.a[n][d] == ai]   # a nurse on probation counts toward nothing
    return c.pure[key]


def _ceil100(v: int) -> int:
    return -(-v // 100)


# ---- hard ------------------------------------------------------------------------------------
def _unavailable(c: Ctx) -> list[Match]:
    return [Match(nu.id, d, 1, f"unavailable: {nu.id} works {c.p.day_words(d)}")
            for n, nu in enumerate(c.p.nurses) for d in nu.unavailable if c.a[n][d] >= 0]


def _allowed_shifts(c: Ctx) -> list[Match]:
    out = []
    for n, nu in enumerate(c.p.nurses):
        for d, v in enumerate(c.a[n]):
            if v >= 0 and c.shifts[v] not in c.allowed[n]:
                out.append(Match(nu.id, d, 1, f"shift restriction: {nu.id} works {c.shifts[v]} on {c.p.day_words(d)}",
                                 elapsed=d < c.F))
    return out


def _succession(c: Ctx) -> list[Match]:
    out = []
    for n, nu in enumerate(c.p.nurses):
        full, H = c.full[n], c.H[n]
        for t in range(max(0, H - 1), len(full) - 1):
            if full[t] >= 0 and full[t + 1] >= 0 and (full[t], full[t + 1]) in c.succ:
                out.append(Match(nu.id, t + 1 - H, 1,
                                 f"succession: {nu.id} {c.shifts[full[t]]} on {c.p.day_words(t - H)} -> "
                                 f"{c.shifts[full[t+1]]} on {c.p.day_words(t + 1 - H)}",
                                 elapsed=t + 1 - H < c.F))
    return out


def _hours_bounds(c: Ctx) -> list[Match]:
    out, p = [], c.p
    for n, nu in enumerate(p.nurses):
        hrs, req, cap = c.hrs[n], p.required_hours(nu), p.overtime_cap(nu)
        if hrs > req + cap:                                   # a past alone over the cap is a fact,
            past = c.past_hrs[n]                              # as long as the free days add nothing
            under = " (on probation: under one shift)" if nu.on_probation else ""
            out.append(Match(nu.id, None, 1, f"overtime: {nu.id} {hrs - req} > cap {cap}{under}",
                             elapsed=past > req + cap and hrs == past))
    return out


def _consecutive(c: Ctx) -> list[Match]:
    out, K = [], c.p.department.max_consecutive_shifts
    for n, nu in enumerate(c.p.nurses):
        full, H, run = c.full[n], c.H[n], 0
        for t, v in enumerate(full):
            run = run + 1 if v >= 0 else 0
            if run > K and t >= H:
                out.append(Match(nu.id, t - H, 1, f"consecutive: {nu.id} {run} shifts in a row at {c.p.day_words(t - H)}",
                                 elapsed=t - H < c.F))
    return out


def _double_cycle(c: Ctx) -> list[Match]:
    out, dep = [], c.p.department
    K, R = dep.max_consecutive_shifts, dep.double_cycle_rest
    span = 2 * K + 1
    for n, nu in enumerate(c.p.nurses):
        full, H = c.full[n], c.H[n]
        work, T = [v >= 0 for v in full], len(full)
        for t in range(T - span):
            pat = all(work[t + i] for i in range(K)) and not work[t + K] \
                and all(work[t + K + 1 + i] for i in range(K))
            bad = [r for r in range(R) if H <= t + span + r < T and work[t + span + r]] if pat else []
            if bad:
                out.append(Match(nu.id, t + span - H, 1,
                                 f"double cycle: {nu.id} needs {R} days off after {c.p.day_words(t + span - 1 - H)}",
                                 elapsed=t + span + max(bad) - H < c.F))   # every worked rest day elapsed
    return out


def _headcount(c: Ctx) -> list[Match]:
    out, p = [], c.p
    for hc in p.department.headcounts:
        ai = c.idx[hc.shift]
        extra = {c.idx[s] for s in hc.also_counts if s in c.idx}
        for d in range(c.D):
            lo, hi = (hc.weekend_min, hc.weekend_max) if p.is_weekend(d) else (hc.weekday_min, hc.weekday_max)
            present = len(_pure(c, ai, d)) + sum(1 for n in c.staff if c.a[n][d] in extra)
            if not lo <= present <= hi:
                out.append(Match(None, d, 1, f"headcount: {c.p.day_words(d)} {hc.shift} = {present}, want {lo}..{hi}",
                                 elapsed=d < c.F, shift=hc.shift, present=present, minimum=lo, maximum=hi))
    return out


def _team_leader(c: Ctx) -> list[Match]:
    out = []
    for hc in c.p.department.headcounts:
        for d in range(c.D):
            if not any(c.p.nurses[n].team_leader for n in _pure(c, c.idx[hc.shift], d)):
                out.append(Match(None, d, 1, f"team leader: {c.p.day_words(d)} {hc.shift} has none", elapsed=d < c.F,
                                 shift=hc.shift))
    return out


def _gender(c: Ctx) -> list[Match]:
    out = []
    for hc in c.p.department.headcounts:
        for d in range(c.D):
            gs = {c.p.nurses[n].gender for n in _pure(c, c.idx[hc.shift], d)}
            if not {"M", "F"} <= gs:
                out.append(Match(None, d, 1, f"gender: {c.p.day_words(d)} {hc.shift} has only {gs}", elapsed=d < c.F,
                                 shift=hc.shift))
    return out


def _skill_expired(c: Ctx) -> list[Match]:
    """A shift on a day after the expiry of one of her required skills (the first to expire)."""
    out = []
    for n, nu in enumerate(c.p.nurses):
        barred = barred_days(c.p, nu)
        if not barred:
            continue
        skill, last = expired_required(c.p, nu)[0]
        for d in barred:
            if c.a[n][d] >= 0:
                out.append(Match(nu.id, d, 1, f"skill expired: {nu.id} works {c.shifts[c.a[n][d]]} on {c.p.day_words(d)}, "
                                 f"{skill} is required to work and its last day was {expiry_words(c.p, last)}", elapsed=d < c.F))
    return out


# ---- hard or soft ----------------------------------------------------------------------------
def _skill_need(c: Ctx) -> list[Match]:
    """Each shift-day a need is short: the nurses on exactly its shift holding its skill that day,
    against the weekday or weekend minimum; the amount is the nurses short."""
    out, p = [], c.p
    for k in p.department.skill_needs:
        ai = c.idx[k.shift]
        for d in range(c.D):
            want = k.minimum(p.is_weekend(d))
            have = sum(1 for n in skill_holders(p, k.skill, d) if c.a[n][d] == ai)
            if have < want:
                out.append(Match(None, d, want - have,
                                 f"skill need: {c.p.day_words(d)} {k.shift} has {have} holding {k.skill}, needs {want} "
                                 f"({want - have} short)", elapsed=d < c.F, shift=k.shift))
    return out


def _night_rest(c: Ctx) -> list[Match]:
    """Walk back from every D in the month: an N met before night_rest_days off days have
    accumulated is a violation (one per D day); an earlier D means the nurse already
    switched to day work, so the walk stops without a violation."""
    out, R, NI, DI = [], c.p.department.night_rest_days, c.NI, c.DI
    for n, nu in enumerate(c.p.nurses):
        full, H = c.full[n], c.H[n]
        owed = min(max(0, nu.rest_owed), R)                   # still owed when her history begins
        for t in range(H, len(full)):
            if full[t] != DI:
                continue
            rest, back = 0, t - 1
            while back >= 0 and rest < R:
                if full[back] == DI:
                    break
                if full[back] == NI:
                    if not (back == t - 1 and (NI, DI) in c.succ):   # direct N->D: succession
                        out.append(Match(nu.id, t - H, 1,
                                         f"night rest: {nu.id} D {c.p.day_words(t - H)} has only {rest} "
                                         f"day(s) off since N {c.p.day_words(back - H)}, needs {R}",
                                         elapsed=t - H < c.F))
                    break
                rest += full[back] < 0
                back -= 1
            else:                                             # neither N nor D met: the memory
                if back < 0 and rest < owed:                  # before the history still owes
                    out.append(Match(nu.id, t - H, 1,
                                     f"night rest: {nu.id} D {c.p.day_words(t - H)} has only {rest} day(s) off, "
                                     f"{owed} still owed to a night before her history",
                                     elapsed=t - H < c.F))
    return out


def _shift_switch(c: Ctx) -> list[Match]:
    """Changes between night work and any other work, month days only; history feeds the
    memory so the month boundary counts. Amount: switches above the maximum."""
    out, mx, NI = [], c.p.department.max_switches, c.NI
    for n, nu in enumerate(c.p.nurses):
        full, H = c.full[n], c.H[n]
        seed = switch_seed(nu)                                # the memory before the history
        switches = count_switches(full, H, NI, len(full), seed)
        if switches > mx:                                     # a past alone over the maximum is a
            past = count_switches(full, H, NI, H + c.F, seed) # fact while the free days add none
            out.append(Match(nu.id, None, switches - mx,
                             f"switches: {nu.id} changes night/day {switches} times, max {mx}",
                             elapsed=past > mx and switches == past))
    return out


# ---- soft ------------------------------------------------------------------------------------
def _nurse_overtime(c: Ctx) -> list[Match]:
    return [Match(nu.id, None, c.hrs[n] - c.p.required_hours(nu),
                  f"{nu.id}: {c.hrs[n] - c.p.required_hours(nu)} h overtime")
            for n, nu in enumerate(c.p.nurses) if c.hrs[n] > c.p.required_hours(nu)]


def _nurse_shortfall(c: Ctx) -> list[Match]:
    return [Match(nu.id, None, c.p.required_hours(nu) - c.hrs[n],
                  f"{nu.id}: {c.p.required_hours(nu) - c.hrs[n]} h short (carried over)")
            for n, nu in enumerate(c.p.nurses) if c.hrs[n] < c.p.required_hours(nu)]


def _avoidable_shortfall(c: Ctx) -> list[Match]:
    """Hours a nurse ends short beyond a remainder below one shift and the hours her recorded
    and free days cannot give (the two exempt parts); under-hours is this soft rule, with a
    weight meant to dominate, never a hard floor: what she can still work is bounded by every
    other hard rule at once, so a floor from any per-nurse bound could make the month
    infeasible."""
    out, p = [], c.p
    for n, nu in enumerate(p.nurses):
        req, beyond = p.required_hours(nu), p.unreachable_hours(nu)
        amt = req - c.hrs[n] - p.max_shortfall(nu) - beyond
        if amt > 0:
            out.append(Match(nu.id, None, amt, f"{nu.id}: {req - c.hrs[n]} h short, {amt} h beyond a remainder "
                             f"below one shift" + (f" and the {beyond} h her days cannot give" if beyond else "")))
    return out


def _dept_overtime(c: Ctx) -> list[Match]:
    total = F.unit_overtime(c.p, [max(0, c.hrs[n] - c.p.required_hours(nu)) for n, nu in enumerate(c.p.nurses)])
    cap = c.p.general.dept_overtime_cap
    return [Match(None, None, total - cap, f"department overtime {total} h, cap {cap}")] if total > cap else []


def _excess_weekend(c: Ctx) -> list[Match]:
    out, mx = [], c.p.department.max_weekends
    for n, nu in enumerate(c.p.nurses):
        k = sum(1 for d in c.weekend_days if c.a[n][d] >= 0)
        if k > mx:
            out.append(Match(nu.id, None, k - mx, f"{nu.id}: {k} weekend days, max {mx}"))
    return out


def count_full_runs(seq: list[int], H: int, K: int) -> int:
    """Full runs on a history + month sequence (shift index, -1 = off): runs of exactly K worked
    days whose last day is a month day (index H or later), followed by an off day or the month's
    end. A run counts in the month that holds its last day, one reaching the month's last day
    included: it cannot grow, since the next month's longest run reads it as history. The start
    of the sequence bounds a run like an off day, as for `_consecutive`."""
    k = run = 0
    for t, v in enumerate(seq):
        run = run + 1 if v >= 0 else 0
        if run == K and t >= H and (t == len(seq) - 1 or seq[t + 1] < 0):
            k += 1
    return k


def _excess_full_run(c: Ctx) -> list[Match]:
    out, dep = [], c.p.department
    K, cap = dep.max_consecutive_shifts, dep.max_full_runs
    for n, nu in enumerate(c.p.nurses):
        k = count_full_runs(c.full[n], c.H[n], K)
        if k > cap:
            out.append(Match(nu.id, None, k - cap, f"{nu.id}: {k} full runs of {K} shifts, max {cap}"))
    return out


def _fair_weekend(c: Ctx) -> list[Match]:
    out, p = [], c.p
    if not c.weekend_days:
        return out
    tol, per = round(p.department.fair_tolerance * 1000), F.friday_slots_m(p)
    for n in c.staff:                                         # a nurse on probation has no Friday share
        nu = p.nurses[n]
        fc = sum(1 for d in c.weekend_days if c.a[n][d] >= 0)
        target = F.friday_expected_m(p, nu, per) - 100 * F.adj_tenths(nu.friday_balance, p)
        amt = max(0, _ceil100(1000 * fc - target - tol), _ceil100(target - 1000 * fc - tol))
        if amt > 0:
            out.append(Match(nu.id, None, amt, f"{nu.id}: {fc} Fridays vs share {target / 1000:.1f}"))
    return out


def _fair(c: Ctx, night: bool) -> list[Match]:
    out, p, dep = [], c.p, c.p.department
    counts = [s for s in F.fair_shifts(p) if (s == dep.night_shift) == night]
    if not counts:
        return out
    share, tol = F.share_m(p), round(dep.fair_tolerance * 1000)
    for n, nu in enumerate(p.nurses):
        if not F.flexible(p, nu):
            continue
        row = c.a[n]
        k = sum(1 for v in row if v >= 0)
        for s in counts:
            cnt = sum(1 for v in row if v == c.idx[s])
            a10 = F.shift_adj_tenths(p, nu, s)
            target = share[s] * k - 100 * a10
            amt = max(0, _ceil100(1000 * cnt - target - tol), _ceil100(target - 1000 * cnt - tol))
            if amt > 0:                                       # ceil, as the model's int var
                out.append(Match(nu.id, None, amt, f"{nu.id}: {cnt} {s} vs share {target / 1000:.1f}"))
    return out


def _overtime_above_share(c: Ctx) -> list[Match]:
    """Each nurse's overtime above her overtime share's target, beyond the tolerance (`fairness.overtime_priced`)."""
    out, p = [], c.p
    overtime = [max(0, c.hrs[n] - p.required_hours(nu)) for n, nu in enumerate(p.nurses)]
    total, owed = F.unit_overtime(p, overtime), F.overtime_owed(p)
    for n, nu in enumerate(p.nurses):
        amt = F.overtime_priced(p, nu, overtime[n], total, owed)
        if amt > 0:
            share, adj = F.overtime_share(p, nu, total, owed), F.overtime_adj_100(p, nu)
            target = f", target {share - adj / 100:.1f} h after the carried balance" if adj else ""
            out.append(Match(nu.id, None, amt, f"{nu.id}: {overtime[n]} h overtime, share {share:.1f} h{target}, "
                                               f"{amt} h priced"))
    return out


def _following(c: Ctx) -> list[Match]:
    """A nurse on probation and her preceptor. Her followable days are the days the rule judges
    (`followable_candidates`: not pinned, not on leave, not barred) on which her preceptor works a shift she is allowed.
    On one she works her preceptor's shift or is off, never another; and once one is left off, she works no judged
    day that is not followable (her hours held what they could beside her preceptor). Pins and elapsed days are facts,
    never judged, so no match is elapsed."""
    out, p = [], c.p
    for n, nu in enumerate(p.nurses):
        q = preceptor_of(p, nu)
        if q is None:
            continue
        mine, theirs, qid = c.a[n], c.a[q], nu.preceptor
        name = lambda v: c.shifts[v] if v >= 0 else "off"
        days = followable_candidates(p, nu)
        followable = [d for d in days if theirs[d] >= 0 and c.shifts[theirs[d]] in c.allowed[n]]
        for d in followable:
            if mine[d] >= 0 and mine[d] != theirs[d]:
                out.append(Match(nu.id, d, 1, f"following: {nu.id} works {name(mine[d])} on {c.p.day_words(d)}, her preceptor "
                                              f"{qid} works {name(theirs[d])}"))
        missed = [d for d in followable if mine[d] < 0]
        if missed:
            first, fset = missed[0], set(followable)
            for d in days:
                if d not in fset and mine[d] >= 0:
                    doing = f"works {name(theirs[d])}" if theirs[d] >= 0 else "is off"
                    out.append(Match(nu.id, d, 1, f"following: {nu.id} works {name(mine[d])} on {c.p.day_words(d)}, when her "
                                                  f"preceptor {qid} {doing}, while she is off on {c.p.day_words(first)}, when "
                                                  f"{qid} works {name(theirs[first])}"))
    return out


def _score_deficit(c: Ctx) -> list[Match]:
    out, p, dep = [], c.p, c.p.department
    for hc in dep.headcounts:
        ai = c.idx[hc.shift]
        for d in range(c.D):
            pure = _pure(c, ai, d)
            thr = round((dep.score_target_weekend if p.is_weekend(d) else dep.score_target) * 100)
            if pure and thr > 0:
                tot = sum(round(p.nurses[n].score * 100) for n in pure)
                if thr * len(pure) > tot:
                    out.append(Match(None, d, thr * len(pure) - tot,
                                     f"{c.p.day_words(d)} {hc.shift}: avg {tot / len(pure) / 100:.2f} under {thr / 100:.2f}"))
    return out


def _score_spread(c: Ctx) -> list[Match]:
    out, p, dep = [], c.p, c.p.department
    avgs = []
    for hc in dep.headcounts:
        ai = c.idx[hc.shift]
        for d in range(c.D):
            pure = _pure(c, ai, d)
            if not pure:
                continue
            tot = sum(round(p.nurses[n].score * 100) for n in pure)
            avgs.append(tot / len(pure))
            band_hi = round((dep.score_band_hi_weekend if p.is_weekend(d) else dep.score_band_hi) * 100)
            if dep.score_balance_mode == "band" and tot > band_hi * len(pure):
                out.append(Match(None, d, tot - band_hi * len(pure),
                                 f"{c.p.day_words(d)} {hc.shift}: avg {tot / len(pure) / 100:.2f} above band {band_hi / 100:.2f}"))
    if dep.score_balance_mode == "spread" and avgs:
        spread = math.ceil(max(avgs) - 1e-9) - math.floor(min(avgs) + 1e-9)
        if spread > 0:
            out.append(Match(None, None, spread, f"shift averages span {spread / 100:.2f}"))
    return out


def _pin(c: Ctx) -> list[Match]:
    out = []
    for n, nu in enumerate(c.p.nurses):
        for d, want in nu.pinned.items():
            have = c.shifts[c.a[n][d]] if c.a[n][d] >= 0 else ""
            if have != want:
                out.append(Match(nu.id, d, 1, f"pin: {nu.id} {c.p.day_words(d)} is {have or 'off'}, pinned {want or 'off'}"))
    return out


def _change(c: Ctx) -> list[Match]:
    out = []
    for n, nu in enumerate(c.p.nurses):
        for d, was in enumerate(c.p.published[n]):
            if d in nu.pinned:
                continue
            now = c.shifts[c.a[n][d]] if c.a[n][d] >= 0 else ""
            if now != was:
                out.append(Match(nu.id, d, 1, f"{nu.id}: {c.p.day_words(d)} {was or 'off'} -> {now or 'off'}"))
    return out


_DOUBLED = "; counts double, a preference was not kept last month"


def unkept_days(p: Problem, nurse: Nurse, row: list[int], rid: str) -> list[int]:
    """The nurse's days of one preference rule (`undesired_day` or `desired_day`) the row does
    not keep: asked off and worked, or asked to work and off. A leave day or a barred one
    supersedes a preference, counted neither way (`superseding`). The one reading of "not
    kept" the price and the carry-over share."""
    leave = superseding(p, nurse)
    if rid == "undesired_day":
        return [d for d in nurse.undesired_days if d not in leave and row[d] >= 0]
    return [d for d in nurse.desired_days if d not in leave and row[d] < 0]


def _preferences(c: Ctx, rid: str, what: str, asked: str) -> list[Match]:
    """Each day not kept: one unit, or two after an unkept month (preference_factor), and its
    words say so."""
    out = []
    for n, nu in enumerate(c.p.nurses):
        times = preference_factor(nu)
        out += [Match(nu.id, d, times, f"{nu.id}: {what} {c.p.day_words(d)} ({asked}{_DOUBLED if times > 1 else ''})")
                for d in unkept_days(c.p, nu, c.a[n], rid)]
    return out


def _undesired_day(c: Ctx) -> list[Match]:
    return _preferences(c, "undesired_day", "works", "asked off")


def _desired_day(c: Ctx) -> list[Match]:
    return _preferences(c, "desired_day", "off on", "asked to work")


def preferences_not_kept(p: Problem, nurse: Nurse, row: list[int]) -> int:
    """Next month's `preferences_not_kept`, from the month's facts: her days of both rules not
    kept. Days, not price: a nurse already doubled carries the same count. Whatever the two
    weights, so a weight of 0 does not erase what happened."""
    return len(unkept_days(p, nurse, row, "undesired_day")) + len(unkept_days(p, nurse, row, "desired_day"))


CHECKS: dict[str, Callable[[Ctx], list[Match]]] = {
    "unavailable": _unavailable, "allowed_shifts": _allowed_shifts, "succession": _succession,
    "hours_bounds": _hours_bounds, "consecutive": _consecutive, "double_cycle": _double_cycle,
    "headcount": _headcount, "team_leader": _team_leader, "gender": _gender,
    "night_rest": _night_rest, "shift_switch": _shift_switch,
    "dept_overtime": _dept_overtime, "nurse_overtime": _nurse_overtime,
    "nurse_shortfall": _nurse_shortfall, "avoidable_shortfall": _avoidable_shortfall,
    "fair_day": lambda c: _fair(c, False),
    "fair_night": lambda c: _fair(c, True), "fair_weekend": _fair_weekend,
    "overtime_above_share": _overtime_above_share,
    "score_deficit": _score_deficit, "score_spread": _score_spread,
    "excess_weekend": _excess_weekend, "excess_full_run": _excess_full_run,
    "undesired_day": _undesired_day, "desired_day": _desired_day,
    "pin": _pin, "change": _change, "skill_expired": _skill_expired, "skill_need": _skill_need,
    "following": _following,
}
assert set(CHECKS) == set(RULES), "every registered rule needs a validator check"


def analyse(p: Problem, a: list[list[int]], *, checked: bool = False) -> tuple[list[str], dict[str, list[Match]]]:
    """Matrix errors (if any, nothing else is meaningful) and the matches of every active
    rule; inactive rules map to an empty list. `checked`: skip the contract check (see explain)."""
    if not checked:
        p.check()
    bad = matrix_errors(p, a)
    if bad:
        return bad, {rid: [] for rid in RULES}
    c = _ctx(p, a)
    return [], {rid: (CHECKS[rid](c) if active(p, rid) else []) for rid in RULES}


def validate(p: Problem, a: list[list[int]]) -> tuple[list[str], dict[str, int], int]:
    """Hard errors (one line per match), the weighted soft cost per rule, and the total."""
    bad, matches = analyse(p, a)
    weighted = {rid: 0 for rid in soft_ids()}
    if bad:
        return bad, weighted, 0
    errors: list[str] = []
    for rid, ms in matches.items():
        if level(p, rid) == HARD:
            errors += [m.text for m in ms if not m.elapsed]   # elapsed facts: Analysis.elapsed
        else:
            weighted[rid] = weight(p, rid) * sum(m.amount for m in ms)
    return errors, weighted, sum(weighted.values())
