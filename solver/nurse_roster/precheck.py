"""Cheap sanity checks on the input, run before the solver, that explain the most common
causes of infeasibility in plain words. CP-SAT only says INFEASIBLE."""
from __future__ import annotations

from . import fairness as F
from .problem import (Problem, barred_days, counted, expired_required, expiry_words, holds_skill,  # noqa: F401
                      needed_history)          # (needed_history: re-exported, defined with the contract)

# ---- cycle-rule automaton -------------------------------------------------------------
# state = (run, offrun, last_full, danger, need_rest)
#   run       worked days in the current block (0 while off)
#   offrun    off days in the current gap, capped at R (0 while working)
#   last_full the last finished block had exactly K shifts
#   danger    the current block follows a full block after exactly one day off, so if it
#             also reaches K the gap after it must hold R off days
#   need_rest the current gap must reach R off days before any work
State = tuple[int, int, bool, bool, bool]


def _start(K: int, R: int) -> State:
    return (0, max(1, R), False, False, False)


def _step(st: State, work: bool, K: int, R: int) -> State | None:
    """Next state after a worked / off day, or None when the day breaks a rule."""
    run, offrun, last_full, danger, need_rest = st
    if work:
        if K > 0 and run >= K:
            return None
        if run == 0:
            if need_rest and offrun < R:
                return None
            danger = R > 1 and last_full and offrun == 1
        return (run + 1, 0, last_full, danger, False)
    if run > 0:
        full = K > 0 and run == K
        return (0, 1, full, False, R > 1 and full and danger)
    return (0, min(offrun + 1, max(1, R)), last_full, danger, need_rest)


def _forced(st: State, K: int) -> State:
    """State after a worked day the rules forbade (history is fact). Exact for the run rule:
    the block is at least full, so no more work before an off day. The double-cycle memory is
    dropped: the model anchors a rest window at the K-th worked day, which this automaton
    cannot express once a block ran past K, and the conservative choice is the one that can
    only miss a warning, never raise a false one (the solver enforces the rule either way)."""
    run = st[0]
    return (min(run + 1, K) if K > 0 else run + 1, 0, False, False, False)


def history_state(previous: list[str], K: int, R: int) -> State:
    st = _start(K, R)
    for s in previous:
        work = s != ""
        st = _step(st, work, K, R) or _forced(st, K)        # a rule-breaking history keeps its
    return st                                                # trailing obligations (off days never break a rule)


def max_shifts_fit(days: int, leave: set[int], K: int, R: int, previous: list[str] = ()) -> int:
    """Most shifts a nurse can work in the month under 'max K in a row' and 'cycle, 1 off,
    cycle -> R off', with leave days forced off (they still count as rest) and the
    previous month's tail setting the starting state. Exact small-state DP."""
    if K <= 0:
        return days - len({d for d in leave if 0 <= d < days})
    best: dict[State, int] = {history_state(list(previous), K, R): 0}
    for d in range(days):
        nxt: dict[State, int] = {}
        for st, k in best.items():
            for work in ((False,) if d in leave else (False, True)):
                ns = _step(st, work, K, R)
                if ns is not None and nxt.get(ns, -1) < k + work:
                    nxt[ns] = k + work
        best = nxt
    return max(best.values(), default=0)


def _hours_window(p: Problem, n) -> tuple[int, int]:
    """[lowest, highest] hours a nurse may work this month under the hours rule; the hours of
    her requirement no roster could work are exempt from the floor (see Problem.max_hours)."""
    req = p.required_hours(n)
    return max(0, req - p.unreachable_hours(n) - p.max_shortfall(n)), req + p.overtime_cap(n)


def fewest_shifts(lengths: list[int], lo: int, hi: int, at_most: int) -> int | None:
    """Fewest shifts, drawn from `lengths` (hours each, any mix), whose total lies in
    [lo, hi]; None when no count up to `at_most` works. Small DP over reachable totals."""
    if lo <= 0 <= hi:
        return 0
    reach = {0}
    for k in range(1, at_most + 1):
        reach = {t + h for t in reach for h in lengths if t + h <= hi}
        if any(lo <= t <= hi for t in reach):
            return k
        if not reach:
            break
    return None


def precheck(p: Problem) -> list[str]:
    """Plain-language reasons the month may be infeasible (or a rule may be meaningless):
    capacity vs headcount demand (department, team leaders, each gender), per-day
    availability against leave, per-shift eligibility, per-nurse hours vs the shift lengths
    and the cycle rules, history depth, and shifts no headcount governs."""
    p.check()
    g, dep = p.general, p.department
    issues: list[str] = []
    N, D = len(p.nurses), p.days
    shift_h = {s: g.shift_hours[s] for s in dep.shifts}
    K, R = dep.max_consecutive_shifts, dep.double_cycle_rest
    allowed = {n.id: set(p.allowed(n)) for n in p.nurses}   # a nurse on probation's: no night shift
    staff = [p.nurses[n] for n in counted(p)]               # who counts toward a shift: every supply below
    anchors = dep.headcounts if dep.headcount_enabled else []
    cut = p.planning_from                                     # elapsed days are facts: their demand
    free = range(cut, D)                                      # is met by whatever happened

    barred = {n.id: set(barred_days(p, n)) for n in p.nurses}   # after a required skill's expiry:
                                                              # counted like leave in every sum
    def off_days(n) -> set[int]:                              # leave, barred and pinned-off days
        return set(n.unavailable) | {d for d in barred[n.id] if d not in n.pinned} \
            | {d for d, s in n.pinned.items() if s == ""}

    def most_shifts(n) -> int:                                # fit under cycle rules and hours,
        elapsed = [n.pinned.get(d, "") for d in range(cut)]   # on the free days: the elapsed
        leave = {d - cut for d in off_days(n) if d >= cut}    # days are facts, so history for
        fit = max_shifts_fit(D - cut, leave, K, R, list(n.previous) + elapsed)   # the cycle
        if g.hours_rule_enabled:                              # rules, hours already spent
            lengths = [shift_h[s] for s in allowed[n.id]]
            left = max(0, _hours_window(p, n)[1] - p.elapsed_hours(n))
            fit = min(fit, left // min(lengths))
        return fit

    def fewest(n) -> int:
        if not g.hours_rule_enabled:
            return 0
        lo, hi = _hours_window(p, n)
        lengths = sorted(set(shift_h[s] for s in allowed[n.id]))
        k = fewest_shifts(lengths, lo, hi, D)
        return k if k is not None else D + 1                  # impossible: reported per nurse

    # ---- department capacity vs headcount demand ----------------------------------
    demand_min = sum(hc.weekend_min if p.is_weekend(d) else hc.weekday_min
                     for hc in anchors for d in free)
    demand_max = sum(hc.weekend_max if p.is_weekend(d) else hc.weekday_max
                     for hc in anchors for d in range(D)) if anchors else N * D
    # a shift that counts toward several headcounts satisfies that many demands at once
    # (only shifts some nurse may work count, as in the fairness share)
    cover = F.coverage(p) if anchors else {}
    demand_min = -(-demand_min // max(1, max(cover.values(), default=1)))
    cap_max = sum(most_shifts(n) for n in staff)
    cap_min = sum(min(fewest(n), D) for n in staff)
    if cap_max < demand_min:
        issues.append(f"capacity: headcount needs at least {demand_min} shifts but staff can give "
                      f"at most {cap_max} even with full overtime")
    usable = set().union(*allowed.values())
    if cap_min > demand_max and usable <= set(cover):          # a shift outside every headcount
        issues.append(f"capacity: staff must work at least {cap_min} shifts (no avoidable shortfall) but "   # is unbounded
                      f"headcount allows at most {demand_max}")

    # ---- attribute capacity: every anchor shift needs a team leader / both genders ------
    slots = len(free) * len(dep.headcounts)                   # anchor shifts still to staff
    groups = []
    if dep.require_team_leader:
        groups.append(("team leader", [n for n in staff if n.team_leader]))
    if dep.require_both_genders:
        groups += [("male", [n for n in staff if n.gender == "M"]),
                   ("female", [n for n in staff if n.gender == "F"])]
    for label, members in groups:
        can = sum(most_shifts(n) for n in members)
        if can < slots:
            issues.append(f"{label}s: {slots} anchor shifts need one each, but the {len(members)} "
                          f"{label}(s) can work at most {can} shifts in total")

    # ---- per-shift eligibility -------------------------------------------------------
    for hc in dep.headcounts:
        able = [n for n in staff if hc.shift in allowed[n.id]]
        if dep.require_team_leader and not any(n.team_leader for n in able):
            issues.append(f"{hc.shift}: no team leader can work it")
        if dep.require_both_genders:
            for sex in ("M", "F"):
                if not any(n.gender == sex for n in able):
                    issues.append(f"{hc.shift}: no {sex} nurse can work it")
        covering = [hc.shift] + list(hc.also_counts)
        able_any = [n for n in staff if allowed[n.id] & set(covering)]
        need = max(((hc.weekend_min if p.is_weekend(d) else hc.weekday_min) for d in free), default=0)
        if dep.headcount_enabled and len(able_any) < need:        # over the days the month has
            issues.append(f"{hc.shift}: only {len(able_any)} nurses may work it"
                          f"{' or a shift counted with it' if hc.also_counts else ''}, headcount needs more")

    # ---- per day: leave clustering against the day's demand (free days only) -----------
    for d in free:
        here = [n for n in staff if d not in off_days(n)]
        today = {n.id: ({n.pinned[d]} if n.pinned.get(d) else allowed[n.id]) for n in here}   # pins fix the shift
        need = -(-sum(hc.weekend_min if p.is_weekend(d) else hc.weekday_min for hc in anchors)
                 // max(1, max(cover.values(), default=1)))
        if len(here) < need:
            issues.append(f"day {d}: headcounts need {need} nurses but only {len(here)} are not on leave")
        for hc in dep.headcounts:
            lo = (hc.weekend_min if p.is_weekend(d) else hc.weekday_min) if dep.headcount_enabled else 0
            covering = set([hc.shift] + list(hc.also_counts))
            avail = [n for n in here if today[n.id] & covering]
            if len(avail) < lo:
                issues.append(f"day {d}: {hc.shift} needs {lo} but only {len(avail)} eligible nurses "
                              f"are not on leave")
            hi = (hc.weekend_max if p.is_weekend(d) else hc.weekday_max) if dep.headcount_enabled else N
            pinned_in = sum(1 for n in here if n.pinned.get(d) in covering)
            if pinned_in > hi:
                issues.append(f"day {d}: {pinned_in} nurses are pinned to {hc.shift} (or a shift counted "
                              f"with it) but its headcount allows at most {hi}")
        pure = {hc.shift: [n for n in here if hc.shift in today[n.id]] for hc in dep.headcounts}
        if dep.require_team_leader:
            tls = {s: sum(1 for n in ns if n.team_leader) for s, ns in pure.items()}
            for s, c in tls.items():
                if c == 0:
                    issues.append(f"day {d}: no team leader available for {s}")
            if all(c > 0 for c in tls.values()) and len(dep.headcounts) > \
                    sum(1 for n in here if n.team_leader and allowed[n.id] & set(pure)):
                issues.append(f"day {d}: {len(dep.headcounts)} anchor shifts need a team leader each, "
                              f"only {sum(1 for n in here if n.team_leader)} available")
        if dep.require_both_genders:
            for sex, word in (("M", "male"), ("F", "female")):
                cnt = {s: sum(1 for n in ns if n.gender == sex) for s, ns in pure.items()}
                for s, c in cnt.items():
                    if c == 0:
                        issues.append(f"day {d}: no {word} nurse available for {s}")
                if all(c > 0 for c in cnt.values()) and len(dep.headcounts) > \
                        sum(1 for n in here if n.gender == sex and allowed[n.id] & set(pure)):
                    issues.append(f"day {d}: {len(dep.headcounts)} anchor shifts need a {word} nurse "
                                  f"each, only {sum(1 for n in here if n.gender == sex)} available")

    # ---- skill needs: anyone holding the skill, enough who may work the shift, each day --------
    for k in dep.skill_needs:
        name = f"skill need {k.shift}/{k.skill}"
        priced = "" if dep.skill_needs_hard else " (soft: each nurse short is priced by skill_shortfall)"
        mins = {d: k.minimum(p.is_weekend(d)) for d in free}
        if not any(mins.values()):
            continue
        holders = [n for n in staff if any(holds_skill(n, k.skill, d) for d in free)]
        if not holders:
            issues.append(f"{name}: no nurse holds {k.skill} on any free day" + priced)
            continue
        able = [n for n in holders if k.shift in allowed[n.id]]
        most = max(mins.values())
        if len(able) < most:
            issues.append(f"{name}: needs {most} on some day but only {len(able)} nurse(s) hold {k.skill} "
                          f"and may work {k.shift}" + priced)
        for d, want in mins.items():
            today = [n for n in able if holds_skill(n, k.skill, d) and d not in off_days(n)
                     and n.pinned.get(d, k.shift) == k.shift]
            if len(today) < want:
                issues.append(f"day {d}: {name} needs {want} but only {len(today)} nurse(s) holding {k.skill} "
                              f"may work {k.shift} (not on leave, pinned elsewhere or barred)" + priced)

    # ---- required skills that expire inside the month: no shift after the last day ---------
    for n in p.nurses:
        for skill, last in expired_required(p, n):
            if last >= 0:
                issues.append(f"{n.id}: {skill}, required to work, expires inside the month: its last day is "
                              f"{expiry_words(last)}, so no shift after it")

    # ---- pins that break a pattern rule between themselves ------------------------------
    succ = set(p.successions())
    for n in p.nurses:
        for d, s in n.pinned.items():
            nxt = n.pinned.get(d + 1)
            if s and nxt and (s, nxt) in succ and d + 1 >= cut:   # both elapsed: a fact
                issues.append(f"{n.id}: pinned {s} on day {d} and {nxt} on day {d + 1} is a forbidden succession")

    # ---- shifts no headcount governs are unbounded --------------------------------------
    if dep.headcount_enabled:
        governed = {s for hc in dep.headcounts for s in [hc.shift] + list(hc.also_counts)}
        for s in dep.shifts:
            if s not in governed:                             # `cover` drops unusable shifts
                issues.append(f"{s}: no headcount names it (as anchor or also_counts), so any number "
                              f"of nurses may work it on any day")

    # ---- a nurse on probation with no preceptor: nothing to follow -------
    for n in p.nurses:
        if n.on_probation and n.preceptor is None:
            issues.append(f"{n.id}: on probation with no preceptor, so she follows no one: she is rostered on freely "
                          f"placed day shifts, counted toward nothing")

    # ---- per-nurse: history depth, and can the owed shifts fit in the available days? --
    need_h = needed_history(dep, g)
    any_history = p.history_len() > 0
    switch_rule = (dep.switches_hard or g.weights.shift_switch > 0) and dep.night_shift in dep.shifts
    for n in p.nurses:
        if len(n.previous) < need_h and (n.previous or any_history):
            issues.append(f"{n.id}: history covers {len(n.previous)} day(s) but the pattern rules look "
                          f"back {need_h}; month-boundary checks may miss a violation")
        if any_history and len(n.previous) >= need_h:
            # a tail of the recommended depth can still hide the shift kind before a run of off
            # days: `last_shift_kind` carries it (the night-rest memory, `rest_owed`, is explicit)
            if switch_rule and all(s == "" for s in n.previous) and n.last_shift_kind is None:
                issues.append(f"{n.id}: history has no worked day and no last_shift_kind, so the shift kind "
                              f"before the month is unknown and a boundary switch cannot be checked")
        unknown = sorted({s for s in n.previous if s and s not in dep.shifts})
        if unknown:
            issues.append(f"{n.id}: history names {unknown}, which the unit no longer defines; they count "
                          f"as work of no kind (neither night nor day shift); renamed_shifts maps a "
                          f"renamed shift")
        if not g.hours_rule_enabled:
            continue
        lo, hi = _hours_window(p, n)
        beyond = p.unreachable_hours(n)
        if beyond > p.max_shortfall(n):                       # her recorded and free days cannot reach
            issues.append(f"{n.id}: {p.required_hours(n)} h owed but at most {p.max_hours(n)} h fit her free "
                          f"days; the {beyond} h beyond them are a recorded fact (carried over), the "
                          f"rest is planned as usual")
        lengths = sorted(set(shift_h[s] for s in allowed[n.id]))
        k = fewest_shifts(lengths, lo, hi, D)
        if k is None:
            issues.append(f"{n.id}: {p.required_hours(n)} h owed cannot be met with shifts of "
                          f"{'/'.join(map(str, lengths))} h inside the {p.overtime_cap(n)} h overtime cap "
                          f"(must land within {lo}..{hi} h)")
            continue
        off = off_days(n)
        fit = max_shifts_fit(D, off, K, R, n.previous)
        if k > fit:
            issues.append(f"{n.id}: needs {k} shifts but at most {fit} fit the month under the cycle "
                          f"rules ({len(off)} day(s) on leave or pinned off)")
    return issues
