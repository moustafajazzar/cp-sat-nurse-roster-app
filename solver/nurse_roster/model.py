"""CP-SAT model: hours-based contracts, general + department rules.

Boolean-only decision variables, clause / small-linear-sum constraints, pre-indexed lists in
the hot loops so model construction stays negligible next to solve time.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
import time
from typing import Callable
from dataclasses import asdict, dataclass, field

from ortools.sat.python import cp_model

from .problem import (Problem, barred_days, counted, followable_candidates, preceptor_of, preference_factor,
                      skill_holders, superseding)
from . import fairness as F
from .explain import Analysis, explain
from .rules import active, soft_ids, weight
from .validate import count_switches, matrix_errors, rest_owed_after, switch_memory, switch_seed

MAX_WORKERS = 128
STOP_POLL = 0.05        # seconds between checks of the caller's stop event (see solve)

Sum = cp_model.LinearExpr.sum
WSum = cp_model.LinearExpr.weighted_sum


@dataclass(slots=True)
class RosterModel:
    model: cp_model.CpModel
    x: list[list[list[cp_model.IntVar]]]        # x[n][d][s]
    off: list[list[cp_model.IntVar]]
    penalties: dict[str, tuple[list, int]]      # name -> (terms, weight)
    build_seconds: float
    stats: dict[str, int] = field(default_factory=dict)
    fingerprint: str = ""                       # of the Problem snapshot it was built from (see solve)


def fingerprint(p: Problem) -> str:
    return hashlib.sha256(json.dumps(asdict(p), sort_keys=True).encode()).hexdigest()


@dataclass(slots=True)
class Solution:
    """`objective` and `penalty_breakdown` are the validator's cost of the returned roster.
    The solver's own objective is only a diagnostic: its penalty variables are bounded from
    below, so a FEASIBLE (time-limit) incumbent can carry slack above the true cost. At
    OPTIMAL the two agree, and at any status the solver's is never the lower one; `run()`
    treats either violation as a model / validator mismatch (exit 3)."""
    status: str
    objective: float
    best_bound: float
    solve_seconds: float
    assignment: list[list[int]]                 # assignment[n][d] = shift index or -1
    penalty_breakdown: dict[str, int]
    solver_objective: float = float("nan")
    hard_errors: list[str] = field(default_factory=list)   # validator findings (expected empty)
    message: str = ""                           # why there is no roster (MODEL_INVALID detail)
    analysis: Analysis | None = None            # per-rule matches and per-nurse indictments
    improvements: int = 0                       # incumbents the solver reported on the way
    deterministic_time: float = float("nan")    # CP-SAT's own clock for this search (see solve)
    stopped_by: str = ""                        # what ended the search: "proof" (OPTIMAL /
                                                # INFEASIBLE), "deterministic_budget",
                                                # "time_limit", "cancelled", "model_invalid"
    problem: Problem | None = None              # the snapshot this roster was scored against:
                                                # serialize from it, never from the caller's copy
    mismatch: str = ""                          # evidence that the model and the validator disagree
                                                # (an incumbent validated below the proven optimum):
                                                # verdict reports it as exit 3 (see _select)
    time_limit: float = float("nan")            # the limits the search ran with (see solve), what
    workers: int = 0                            # a result reports; workers 0 = not recorded (a
    seed: int = 0                               # Solution built by hand)
    deterministic_budget: float | None = None


def build_model(p: Problem) -> RosterModel:
    t0 = time.perf_counter()
    p = copy.deepcopy(p)                # the snapshot every constraint and the fingerprint come from:
    p.check()                           # an edit by another thread during the build can neither
    m = cp_model.CpModel()              # enter the model nor be certified by its fingerprint
    g, dep = p.general, p.department
    shifts = dep.shifts
    idx = {s: i for i, s in enumerate(shifts)}
    S, D, N = len(shifts), p.days, len(p.nurses)
    P = p.history_len()                 # fixed days from the previous month, placed before day 0
    T = P + D                           # full horizon; pattern rules run over all of it but only
                                        # constrain windows that reach into the month, so a
                                        # history that breaks a rule can never make it infeasible
    cut = p.planning_from               # elapsed month days (every cell pinned) are facts like the
    Q = P + cut                         # history: a hard rule is enforced only where it reaches a
                                        # free day (horizon index >= Q); soft rules keep counting
                                        # them, as the validator does, so the costs agree
    hours = [g.shift_hours[s] for s in shifts]
    weekend = [p.is_weekend(d) for d in range(D)]
    nb = m.new_bool_var
    W = g.weights

    x = [[[nb(f"x{n}_{t}_{s}") for s in range(S)] for t in range(T)] for n in range(N)]
    off = [[nb(f"o{n}_{t}") for t in range(T)] for n in range(N)]

    # penalty terms per soft rule; the weight is 0 when the registry says the rule is off
    pen: dict[str, tuple[list, int]] = {rid: ([], weight(p, rid)) for rid in soft_ids()}
    rule = lambda rid: active(p, rid)                        # the registry's truth table
    weekend_days = [P + d for d in range(D) if weekend[d]]
    succ = [(idx[a], idx[b]) for a, b in p.successions() if a in idx and b in idx]
    NI, DI = idx.get(dep.night_shift), idx.get(dep.day_shift)
    adjacent_rest_exempt = (NI, DI) in succ
    rest_gaps = [k for k in range(1, min(dep.night_rest_days, T - 1) + 1)
                 if not (k == 1 and adjacent_rest_exempt)]  # k=1 already hard-forbidden; gaps
                                                             # longer than the horizon cannot occur
    # Rest rule: the first D after an N needs night_rest_days off days in between (a D in
    # between means the nurse already switched; later Ds are not new violations). With a
    # third worked shift (A/B/C) the gap is not all off days, so for gaps longer than the
    # rest period an enforced linear sum counts them. In a pure D/N unit every worked day
    # in the gap is an N closer to the D or a D that ends the rule, so the short clauses
    # are exact. Gaps that start in the fixed history are handled statically below, since a
    # history may break the max-in-a-row rule that bounds the free part of a gap.
    other_work = any(i not in (NI, DI) for i in range(S))
    R_rest = dep.night_rest_days
    rest_long = range(0)
    if other_work and rule("night_rest"):
        # Only free days obey the run limit. A gap can include up to `cut` elapsed days
        # before its free part guarantees R off days after R*(K+1) days. Clamp to the
        # horizon (parameters up to 1000 would otherwise cost a million iterations).
        hi = cut + R_rest * (dep.max_consecutive_shifts + 1) if dep.max_consecutive_shifts > 0 else T - 1
        rest_long = range(R_rest + 1, min(hi, T - 1) + 1)
    ot_vars = []                        # one per nurse while the hours rule is on, in the nurses' order
    ot_caps = []
    counts = F.fair_shifts(p)
    share = F.share_m(p)                                     # thousandths of demand per fair shift
    fri_per = F.friday_slots_m(p)                            # the Friday share's per-problem part
    tol = round(dep.fair_tolerance * 1000)                   # in thousandths of a shift
    allowed = [set(p.allowed(nu)) for nu in p.nurses]        # a nurse on probation's: no night shift
    staff = counted(p)                                       # who counts toward a shift (not on probation)

    bar = rule("skill_expired")                              # once: its test scans every nurse
    for n, nurse in enumerate(p.nurses):
        xn, on = x[n], off[n]
        flat = [v for day in xn[P:] for v in day]              # month days only

        start = P - len(nurse.previous)                        # fix known history
        other = {start + i for i, name in enumerate(nurse.previous) if name and name not in idx}
        for t in range(T):                                     # one shift per day, except a history
            if t not in other:                                 # day worked in a shift the unit no
                m.add_exactly_one(xn[t] + [on[t]])             # longer defines: worked, no shift
        for i, name in enumerate(nurse.previous):
            t = start + i
            if name == "":
                m.add(on[t] == 1)
            elif name in idx:
                m.add(xn[t][idx[name]] == 1)
            else:                                              # other work: neither off nor any
                m.add(on[t] == 0)                              # current shift (not the night one,
                for s in range(S):                             # not the day one)
                    m.add(xn[t][s] == 0)
        for d in nurse.unavailable:
            m.add(on[P + d] == 1)
        if bar:                                                # after a required skill's expiry: off
            for d in barred_days(p, nurse):                    # (an elapsed day is a fact, pinned)
                if d >= cut:
                    m.add(on[P + d] == 1)
        if rule("pin"):                                        # admin-fixed or already worked
            for d, s_name in nurse.pinned.items():
                m.add((on[P + d] if s_name == "" else xn[P + d][idx[s_name]]) == 1)
        if rule("change"):                                     # stay close to the published roster
            for d, s_name in enumerate(p.published[n]):
                if d not in nurse.pinned:
                    pen["change"][0].append((on[P + d] if s_name == "" else xn[P + d][idx[s_name]]).Not())
        for s_name in shifts:                                  # shift restriction (hard)
            if s_name not in allowed[n]:
                si = idx[s_name]
                for d in range(cut, D):
                    m.add(xn[P + d][si] == 0)
        for a, b in succ:                                      # forbidden successions
            for t in range(max(0, Q - 1), T - 1):              # later day free
                m.add_bool_or([xn[t][a].Not(), xn[t + 1][b].Not()])

        # hours: worked − required = overtime − shortfall; overtime ≤ cap (hard); both penalised
        # so the nearest shift count wins. The shortfall has no hard floor: what she can still
        # work is bounded by every other hard rule at once (runs, rest, headcount maxima...), so
        # a floor from any per-nurse bound could make the month infeasible; instead the part
        # beyond a remainder below one shift and the hours her recorded and free days cannot
        # give carries the dominant `avoidable_shortfall` weight
        if rule("hours_bounds"):
            req, cap, short_max = p.required_hours(nurse), p.overtime_cap(nurse), p.max_shortfall(nurse)
            cap = max(cap, p.elapsed_hours(nurse) - req)       # a past already over the cap cannot be
                                                               # undone: no further shift, not infeasible
            total_h = WSum(flat, [hours[s] for _ in range(D) for s in range(S)])
            ot = m.new_int_var(0, cap, f"ot{n}")
            short = m.new_int_var(0, req, f"sh{n}")
            m.add(total_h - req == ot - short)
            if rule("overtime_above_share"):                   # her overtime exactly, as the validator reads it:
                m.add_max_equality(ot, [total_h - req, 0])     # never raised with the shortfall to move the shares
            ot_vars.append(ot)
            ot_caps.append(cap)
            pen["nurse_overtime"][0].append(ot)
            pen["nurse_shortfall"][0].append(short)
            if rule("avoidable_shortfall"):
                exempt = short_max + p.unreachable_hours(nurse)
                avoid = m.new_int_var(0, req, f"av{n}")
                m.add(avoid >= short - exempt)
                pen["avoidable_shortfall"][0].append(avoid)

        # N -> D rest: a D with too few off days since the last N is forbidden (hard), or
        # carries one penalty literal per offending D day (soft)
        if rule("night_rest"):
            viol: dict[int, cp_model.IntVar] = {}             # D day -> penalty literal
            def rest_lit(td: int) -> cp_model.IntVar:
                if td not in viol:
                    viol[td] = nb(f"nr{n}_{td}")
                    pen["night_rest"][0].append(viol[td])
                return viol[td]
            lo_t = Q if dep.night_rest_hard else P             # hard: the D day must be free; soft:
            for k in rest_gaps:                                # elapsed penalties still count
                for t in range(max(0, lo_t - k), T - k):       # D day inside the month / free
                    lits = [xn[t][NI].Not(), xn[t + k][DI].Not()]
                    lits += [xn[t + i][DI] for i in range(1, k)]   # an earlier D ends the rule
                    if not dep.night_rest_hard:
                        lits.append(rest_lit(t + k))
                        if adjacent_rest_exempt:
                            # An elapsed N->D is reported as a succession, never also as
                            # rest owed to an earlier N. Apply this to every encoding.
                            lits.append(xn[t + k - 1][NI])
                    m.add_bool_or(lits)
            for k in rest_long:
                for t in range(max(P, lo_t - k), T - k):       # N inside the month, D day free
                    cond = [xn[t][NI], xn[t + k][DI]] + [xn[t + i][DI].Not() for i in range(1, k)]
                    if not dep.night_rest_hard:
                        cond.append(rest_lit(t + k).Not())
                        if adjacent_rest_exempt:
                            cond.append(xn[t + k - 1][NI].Not())
                    m.add(Sum(on[t + 1:t + k]) >= R_rest).only_enforce_if(cond)
            if R_rest > 0:
                # off days still owed when the month begins: the nurse's `rest_owed` (the memory
                # before her history) walked through the history, where a night with no D since
                # owes what the off days after it have not paid back, whatever the gap's length
                owed = rest_owed_after(nurse.rest_owed, nurse.previous, dep.night_shift, dep.day_shift, R_rest)
                if owed > 0:
                    K_ = dep.max_consecutive_shifts           # elapsed days need not obey K;
                    reach = min(D, cut + owed * (K_ + 1)) if K_ > 0 else D  # bound the free suffix
                    first = cut if dep.night_rest_hard else 0  # an elapsed first D is a fact
                    for j in range(first, reach):             # first D of the month at day j
                        cond = [xn[P + j][DI]] + [xn[P + i][DI].Not() for i in range(j)]
                        if not dep.night_rest_hard:
                            cond.append(rest_lit(P + j).Not())
                            # the day before must be a real one (hers, or the month's), never the
                            # free padding before a short history, which the solver would fill
                            # with an N to waive the penalty
                            if adjacent_rest_exempt and P + j - 1 >= P - len(nurse.previous):
                                cond.append(xn[P + j - 1][NI].Not())
                        m.add(Sum(on[P:P + j]) >= owed).only_enforce_if(cond)

        # max K shifts in a row (hard): an off day in every window of K+1
        K = dep.max_consecutive_shifts
        if K > 0:
            for t in range(max(0, Q - K), T - K):              # window ends on a free day
                m.add_bool_or(on[t:t + K + 1])

        # cycle, 1 off, cycle  ->  at least R days off afterwards (hard)
        R = dep.double_cycle_rest
        if K > 0 and R > 1:
            work = [o.Not() for o in on]
            span = 2 * K + 1                                   # W*K O W*K
            for t in range(T - span):
                head = [on[t + i] for i in range(K)] + [work[t + K]] + [on[t + K + 1 + i] for i in range(K)]
                for r in range(R):                             # ¬pattern ∨ off[t+span+r]
                    if Q <= t + span + r < T:                  # consequence day free
                        m.add_bool_or(head + [on[t + span + r]])

        # preferences (soft): a worked day the nurse asked off, an off day she asked to work; each
        # term listed once per time it counts (twice after an unkept month: preference_factor)
        leave = superseding(p, nurse)                          # leave and a barred day supersede one
        times = preference_factor(nurse)
        if rule("undesired_day"):
            pen["undesired_day"][0].extend(on[P + d].Not() for d in nurse.undesired_days if d not in leave
                                           for _ in range(times))
        if rule("desired_day"):
            pen["desired_day"][0].extend(on[P + d] for d in nurse.desired_days if d not in leave
                                         for _ in range(times))

        # weekends worked above the department maximum (soft)
        if rule("excess_weekend") and dep.max_weekends < len(weekend_days):
            ex = m.new_int_var(0, len(weekend_days), f"xw{n}")
            m.add(ex >= Sum([on[d].Not() for d in weekend_days]) - dep.max_weekends)
            pen["excess_weekend"][0].append(ex)

        # full runs above the department maximum (soft): one literal per month day t that can end
        # a run of exactly K, at least 1 when it does (the objective holds it at 0 otherwise), as
        # `count_full_runs` counts them: K worked days to t, an off day after or the month's end,
        # and an off day before, or the start of her own history. A run reaching the month's last
        # day is this month's: it cannot grow, since the next month's longest run reads it as
        # history. The free padding before a short history is never read, or the solver would fill
        # it with work to make the run look longer
        if rule("excess_full_run"):
            K = dep.max_consecutive_shifts
            ends = []
            for t in range(max(P, start + K - 1), T):
                worked = [on[i].Not() for i in range(t - K + 1, t + 1)]
                bounds = ([on[t + 1]] if t + 1 < T else []) + ([on[t - K]] if t - K >= start else [])
                fr = nb(f"fr{n}_{t}")
                m.add_bool_or([lit.Not() for lit in worked + bounds] + [fr])
                ends.append(fr)
            if dep.max_full_runs < len(ends):
                ex = m.new_int_var(0, len(ends), f"xfr{n}")
                m.add(ex >= Sum(ends) - dep.max_full_runs)
                pen["excess_full_run"][0].append(ex)

        # Friday fairness: |fridays − fair share + carried balance| beyond tolerance
        if rule("fair_weekend") and weekend_days and not nurse.on_probation:   # she has no Friday share
            fc = Sum([on[d].Not() for d in weekend_days])
            fri_exp = F.friday_expected_m(p, nurse, fri_per)   # prorated for leave, thousandths
            target = fri_exp - 100 * F.adj_tenths(nurse.friday_balance, p)
            ub = -(-max(abs(target), abs(1000 * len(weekend_days) - target)) // 100) + 1
            devf = m.new_int_var(0, ub, f"devf{n}")            # tenths of a shift
            m.add(100 * devf >= 1000 * fc - target - tol)
            m.add(100 * devf >= target - 1000 * fc - tol)
            pen["fair_weekend"][0].append(devf)

        # contract-relative D/N fairness: |count_s − (share_s·shifts − carried adj)| beyond
        # tolerance, per nurse free to work every fair shift
        if F.flexible(p, nurse) and counts:
            k = Sum(flat)                                      # shifts worked this month
            for s in counts:
                c = Sum([xn[P + d][idx[s]] for d in range(D)])
                key = "fair_night" if s == dep.night_shift else "fair_day"
                if pen[key][1] > 0:
                    a10 = F.shift_adj_tenths(p, nurse, s)      # + lowers the target
                    dev = m.new_int_var(0, 10 * D + abs(a10) + 1, f"dev{s}{n}")   # tenths
                    # 100·dev ≥ 1000·c − (share·k − 100·a10) − tol   (and the mirror)
                    m.add(100 * dev >= 1000 * c - share[s] * k + 100 * a10 - tol)
                    m.add(100 * dev >= share[s] * k - 100 * a10 - 1000 * c - tol)
                    pen[key][0].append(dev)

    # ---- per day / per anchor shift ------------------------------------------
    tl = [n for n in staff if p.nurses[n].team_leader]       # a nurse on probation counts toward nothing
    males = [n for n in staff if p.nurses[n].gender == "M"]
    females = [n for n in staff if p.nurses[n].gender != "M"]
    thr_wd, thr_we = round(dep.score_target * 100), round(dep.score_target_weekend * 100)
    score100 = [round(nu.score * 100) for nu in p.nurses]
    balance = rule("score_spread") and dep.score_balance_mode == "spread"
    band = rule("score_spread") and dep.score_balance_mode == "band"
    band_wd, band_we = round(dep.score_band_hi * 100), round(dep.score_band_hi_weekend * 100)
    if balance:
        avg_lo, avg_hi = m.new_int_var(0, 100, "avg_lo"), m.new_int_var(0, 100, "avg_hi")
        m.add(avg_hi >= avg_lo)                                # tightens the objective bound
    for hc in dep.headcounts:
        a = idx[hc.shift]
        extra = [idx[s] for s in hc.also_counts if s in idx]
        for d in range(D):
            lo, hi = (hc.weekend_min, hc.weekend_max) if weekend[d] else (hc.weekday_min, hc.weekday_max)
            free = d >= cut                                    # an elapsed day is a fact: no hard
            if not (rule("headcount") and free):               # rule, and any headcount may occur
                lo, hi = 0, N
            t = P + d
            pure = [x[n][t][a] for n in staff]
            present = pure + [x[n][t][e] for e in extra for n in staff]
            if rule("headcount") and free:
                m.add_linear_constraint(Sum(present), lo, hi)
            if rule("team_leader") and free:
                m.add_bool_or([x[n][t][a] for n in tl])
            if rule("gender") and free:
                m.add_bool_or([x[n][t][a] for n in males])
                m.add_bool_or([x[n][t][a] for n in females])
            thr = thr_we if weekend[d] else thr_wd             # Friday shifts have their own target
            band_hi = band_we if weekend[d] else band_wd
            scores = [score100[n] for n in staff]              # in the order of `pure`
            if thr > 0 and rule("score_deficit"):                # soft: avg score >= target
                deficit = m.new_int_var(0, 100 * hi, f"sd{hc.shift}{d}")
                m.add(deficit >= WSum(pure, [thr - sc for sc in scores]))  # max(0, thr*h - sum)
                pen["score_deficit"][0].append(deficit)
            if band:                                           # soft: avg <= band_hi (linear)
                excess = m.new_int_var(0, 100 * hi, f"sx{hc.shift}{d}")
                m.add(excess >= WSum(pure, [sc - band_hi for sc in scores]))
                pen["score_spread"][0].append(excess)
            if balance:                                        # avg_lo <= sum/h <= avg_hi (0.01 units)
                total = WSum(pure, scores)                     # linearised by enumerating the
                hvars = []                                     # pure headcount h (0 = no shift)
                for h in range(0, (min(hi, N) if dep.headcount_enabled else N) + 1):
                    b = nb(f"h{hc.shift}{d}_{h}")
                    m.add(total >= h * avg_lo).only_enforce_if(b)
                    m.add(total <= h * avg_hi).only_enforce_if(b)
                    hvars.append((h, b))
                m.add_exactly_one([b for _, b in hvars])
                m.add(Sum(pure) == WSum([b for _, b in hvars], [h for h, _ in hvars]))

    # ---- skill needs: nurses holding the skill on exactly its shift, against the minimum -------
    if rule("skill_need"):
        for k in dep.skill_needs:
            a = idx[k.shift]
            for d in range(D):
                want = k.minimum(weekend[d])
                if want == 0:
                    continue
                have = Sum([x[n][P + d][a] for n in skill_holders(p, k.skill, d)])
                if dep.skill_needs_hard:
                    if d >= cut:                               # an elapsed day is a fact
                        m.add(have >= want)
                else:                                          # soft: elapsed days still count (as in the validator)
                    short = m.new_int_var(0, want, f"sk{a}_{d}_{len(pen['skill_need'][0])}")
                    m.add(short >= want - have)
                    pen["skill_need"][0].append(short)

    # ---- department overtime (soft) --------------------------------------------
    unit_ot_cap = F.unit_overtime(p, ot_caps) if ot_vars else 0   # her rounding overtime is her own record
    if rule("dept_overtime") and ot_vars:
        ex = m.new_int_var(0, unit_ot_cap, "dept_ot_excess")    # includes already-recorded excess
        m.add(ex >= F.unit_overtime(p, ot_vars) - g.dept_overtime_cap)
        pen["dept_overtime"][0].append(ex)

    # ---- overtime share (soft): each nurse's overtime above her target, beyond the tolerance ----
    # (fairness.overtime_priced, which says it in full). The share moves with the unit's overtime O,
    # a variable: with S = 100 R, S p >= S (ot - tol) - 100 r O + R adj - (S - 1) makes the least p
    # floor((S (ot - tol) - 100 r O + R adj) / S). Its cap at her overtime is an either-or, p >= ot or
    # the floor, needed only where the carried balance can push the floor above her overtime: a
    # balance held above the tolerance (a plain upper bound p <= ot would make such a month infeasible).
    owed = F.overtime_owed(p) if rule("overtime_above_share") and ot_vars else 0
    if owed > 0:
        unit_ot = m.new_int_var(0, unit_ot_cap, "unit_ot")
        m.add(unit_ot == F.unit_overtime(p, ot_vars))
        scale, tol = 100 * owed, dep.overtime_tolerance
        for n, nurse in enumerate(p.nurses):
            r = p.required_hours(nurse)
            if r == 0 or nurse.on_probation:                   # she owes none, or is on probation: no share
                continue
            adj = F.overtime_adj_100(p, nurse)
            priced = m.new_int_var(0, ot_caps[n], f"ots{n}")
            floor = m.add(scale * priced >= scale * (ot_vars[n] - tol) - 100 * r * unit_ot + owed * adj - (scale - 1))
            if adj > 100 * tol:
                whole = nb(f"otw{n}")
                m.add(priced >= ot_vars[n]).only_enforce_if(whole)
                floor.only_enforce_if(whole.Not())
            pen["overtime_above_share"][0].append(priced)

    # ---- following (hard): `validate._following` says it in full. On each day the rule judges,
    # fol is whether her preceptor works a shift she is allowed (a followable day): then a shift of hers is his, and a
    # followable day she is off sets `missed`, which forbids her any judged day that is not followable. Nothing here
    # binds the preceptor: she can always be off.
    if rule("following"):
        for n, nurse in enumerate(p.nurses):
            q = preceptor_of(p, nurse)
            if q is None:
                continue
            mine = [idx[s] for s in shifts if s in allowed[n]]
            missed = nb(f"fm{n}")
            for d in followable_candidates(p, nurse):
                t = P + d
                fol = nb(f"fo{n}_{d}")
                m.add(fol == Sum([x[q][t][s] for s in mine]))
                for s in mine:
                    m.add_bool_or([x[n][t][s].Not(), fol.Not(), x[q][t][s]])
                m.add_bool_or([fol.Not(), off[n][t].Not(), missed])
                m.add_bool_or([missed.Not(), fol, off[n][t]])

    # ---- night/day switches: changes between night work and any other work -----------
    # last_n[t] remembers whether the most recent worked shift up to day t was the night
    # shift; a worked day whose kind (night / not night) differs from last_n[t-1] is a
    # switch. History days feed the memory, so the month boundary counts, also for a nurse
    # restricted to one shift this month; only switches on month days are counted. The kind
    # before the month is the history's last worked day, or `last_shift_kind` when the history
    # shows no work (a nurse back from leave), so a run of off days of any length keeps it.
    if rule("shift_switch"):
        for n, nurse in enumerate(p.nurses):
            xn, on = x[n], off[n]
            last_n = [nb(f"ln{n}_{t}") for t in range(T)]
            for t in range(T):
                m.add(last_n[t] == xn[t][NI]).only_enforce_if(on[t].Not())
                if t > 0:
                    m.add(last_n[t] == last_n[t - 1]).only_enforce_if(on[t])
            memory = switch_memory(nurse, dep.night_shift)     # None: unknown, the solver's choice
            if memory is not None:
                if P > 0:
                    m.add(last_n[P - 1] == int(memory))
                else:                                          # no history day holds it: day 0 keeps
                    m.add(last_n[0] == int(memory)).only_enforce_if(on[0])   # it while off
            switches = []
            for t in range(P if P > 0 or memory is not None else 1, T):
                sw = nb(f"sw{n}_{t}")
                if t == 0:                                     # no history day holds the memory:
                    if memory:                                 # it is the constant `last_shift_kind`
                        m.add_bool_or([sw, on[0], xn[0][NI]])                 # day work after N-memory
                    else:
                        m.add_bool_or([sw, xn[0][NI].Not()])                  # N after day-memory
                else:
                    m.add_bool_or([sw, xn[t][NI].Not(), last_n[t - 1]])        # N after day-memory
                    m.add_bool_or([sw, on[t], xn[t][NI], last_n[t - 1].Not()])  # day work after N-memory
                switches.append(sw)
            if dep.switches_hard:                              # a past alone over the maximum is a
                seed = switch_seed(nurse)
                seq = [idx.get(s, S) if s else -1 for s in nurse.previous] \
                    + [idx[nurse.pinned[d]] if nurse.pinned[d] else -1 for d in range(cut)]   # S: no kind
                past = count_switches(seq, len(nurse.previous), NI, len(seq), seed)   # fact: no more
                m.add(Sum(switches) <= max(dep.max_switches, past))
            else:
                ex = m.new_int_var(0, len(switches), f"xsw{n}")
                m.add(ex >= Sum(switches) - dep.max_switches)
                pen["shift_switch"][0].append(ex)

    if balance:
        pen["score_spread"][0].append(avg_hi - avg_lo)

    terms, weights = [], []
    for name, (vs, w) in pen.items():
        if w > 0:
            terms.extend(vs)
            weights.extend([w] * len(vs))
    m.minimize(WSum(terms, weights))

    proto = m.proto
    return RosterModel(m, x, off, pen, time.perf_counter() - t0,
                       {"variables": len(proto.variables), "constraints": len(proto.constraints),
                        "objective_terms": len(terms)}, fingerprint(p))


def _assignment(value: Callable, rm: RosterModel, p: Problem) -> list[list[int]]:
    """Month part of a solution as nurse x day shift index (-1 = off); `value` reads a literal."""
    D, S, P = p.days, len(p.department.shifts), p.history_len()
    out = []
    for xn in rm.x:
        row = [-1] * D
        for d in range(D):
            for s in range(S):
                if value(xn[P + d][s]):
                    row[d] = s
                    break
        out.append(row)
    return out


def _solution(status: str, a: list[list[int]], p: Problem, bound: float, seconds: float,
              solver_objective: float, improvements: int) -> Solution:
    an = explain(p, a, checked=True)        # the snapshot build_model checked (solve: same fingerprint)
    return Solution(status, float(an.total), bound, seconds, a,
                    {rid: an.rules[rid].weighted for rid in soft_ids()},
                    solver_objective=solver_objective, hard_errors=an.errors, analysis=an,
                    improvements=improvements, problem=p)


def stop_cause(status: str, det: float, budget: float | None, wall: float, time_limit: float,
               cancelled: bool) -> str:
    """What ended a search, from what CP-SAT reports. A proof (OPTIMAL / INFEASIBLE) or an
    invalid model needed no limit; a cancellation is the caller's. Otherwise CP-SAT stops at
    or just past the limit it hit, so a deterministic clock at or past its budget says the
    budget tripped, and a wall clock still under its limit says the wall clock did not. When
    both have tripped the cause is ambiguous and the machine-dependent one is assumed: a stop
    counts as the budget's only when the wall clock demonstrably played no part."""
    if status in ("OPTIMAL", "INFEASIBLE"):
        return "proof"
    if status == "MODEL_INVALID":
        return "model_invalid"
    if cancelled:
        return "cancelled"
    if budget is not None and det >= budget and wall < time_limit:
        return "deterministic_budget"
    return "time_limit"


class _Progress(cp_model.CpSolverSolutionCallback):
    """Validates every incumbent, keeps the best one by the roster's true cost (the solver's
    objective can rank two incumbents the other way round because of slack), reports each
    improvement in that cost, and stops the search at once when the caller's event is set
    (the watcher in `solve` also stops it, so a search without incumbents is cancellable)."""

    def __init__(self, rm: RosterModel, p: Problem, on_solution, stop, t0: float, limits: dict):
        super().__init__()
        self.rm, self.p, self.on_solution, self.stop, self.t0 = rm, p, on_solution, stop, t0
        self.limits = limits                                  # every incumbent carries the search's
        self.count = 0
        self.best: Solution | None = None
        self.stopped = False                                  # this callback ended the search

    def on_solution_callback(self) -> None:
        if self.stopped:                                      # a worker's solution found before the
            return                                            # stop took effect: the caller stopped
        a = _assignment(self.boolean_value, self.rm, self.p)  # at the incumbent it saw
        sol = _solution("FEASIBLE", a, self.p, self.best_objective_bound,
                        time.perf_counter() - self.t0, self.objective_value, self.count + 1)
        _stamp(sol, self.limits)                              # settings, so a callback can build a result
        if self.best is None or sol.objective < self.best.objective:
            self.count += 1
            sol.improvements = self.count
            self.best = sol
            if self.on_solution is not None:
                self.on_solution(copy.deepcopy(sol))           # the retained one stays ours
        if self.stop is not None and self.stop.is_set():
            self.stopped = True
            self.stop_search()


def _stamp(sol: Solution, limits: dict) -> None:
    sol.time_limit, sol.workers, sol.seed = limits["time_limit"], limits["workers"], limits["seed"]
    sol.deterministic_budget = limits["deterministic_budget"]


def solve(rm: RosterModel, p: Problem, time_limit: float = 60.0, workers: int = 8,
          log: bool = False, hint: list[list[int]] | None = None, seed: int = 0,
          on_solution: Callable[[Solution], None] | None = None,
          stop: threading.Event | None = None, deterministic_time: float | None = None) -> Solution:
    """`hint`: a previous assignment (nurse x month-day shift index, -1 = off) used as a
    warm start, e.g. when re-solving after the admin edits a rule or a request.
    `on_solution`: called with every incumbent that lowers the roster's validated cost
    (status FEASIBLE) so an app can show progress; `stop`: an Event that ends the search
    within about STOP_POLL seconds of being set, incumbent or not (an event set before the
    call returns at once, without solving); the best incumbent so far is returned, or status
    UNKNOWN with a "cancelled" message when there is none. With either set, every incumbent
    is validated and the best one by validated cost is returned, which can differ from the
    solver's last incumbent at a time limit; a proven OPTIMAL roster is returned as is, since
    a cheaper earlier incumbent would mean the model and the validator disagree, a mismatch
    `run()` reports. Both are optional and cost nothing unset.
    `p` is snapshotted on entry: the callback (or another thread) editing the caller's
    problem during the search cannot change what is solved or how the result is scored.
    A fixed `seed` does not make a wall-clock-limited multi-worker search reproducible:
    `deterministic_time` is a budget on CP-SAT's own deterministic clock (roughly seconds of
    single-threaded work), and with one worker it makes the stopping point, hence the roster,
    a function of the input alone; `time_limit` then only guards against a runaway search.
    `p` must be the problem `rm` was built from: an input edited after `build_model` would
    get a roster for the old rules with an optimality claim for the new ones, so a
    mismatch is a ValueError (rebuild instead)."""
    p = copy.deepcopy(p)                                      # the snapshot the result refers to
    if rm.fingerprint != fingerprint(p):                      # checked on the snapshot itself
        raise ValueError("solve: the problem differs from the one the model was built from; "
                         "call build_model again")
    if not isinstance(time_limit, (int, float)) or isinstance(time_limit, bool) \
            or not math.isfinite(time_limit) or time_limit <= 0:
        raise ValueError(f"time_limit must be a positive number of seconds, got {time_limit!r}")
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"workers must be an integer within 1..{MAX_WORKERS}, got {workers!r}")
    if not isinstance(seed, int) or isinstance(seed, bool) or not -2 ** 31 <= seed < 2 ** 31:
        raise ValueError(f"seed must be a signed 32-bit integer, got {seed!r}")
    if deterministic_time is not None and (not isinstance(deterministic_time, (int, float))
                                           or isinstance(deterministic_time, bool)
                                           or not math.isfinite(deterministic_time) or deterministic_time <= 0):
        raise ValueError(f"deterministic_time must be a positive number, got {deterministic_time!r}")
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit)
    if deterministic_time is not None:
        solver.parameters.max_deterministic_time = float(deterministic_time)
    solver.parameters.num_workers = workers
    solver.parameters.random_seed = seed
    solver.parameters.log_search_progress = log
    if hint is not None:
        bad = matrix_errors(p, hint)
        if bad:
            raise ValueError("hint: " + bad[0])
        P0, S0 = p.history_len(), len(p.department.shifts)
        rm.model.clear_hints()
        for xn, on, row in zip(rm.x, rm.off, hint):
            for d, v in enumerate(row):
                for s_ in range(S0):
                    rm.model.add_hint(xn[P0 + d][s_], int(v == s_))
                rm.model.add_hint(on[P0 + d], int(v < 0))
    if stop is not None and not isinstance(stop, threading.Event):
        raise ValueError("stop must be a threading.Event")
    limits = dict(time_limit=float(time_limit), workers=workers, seed=seed,
                  deterministic_budget=None if deterministic_time is None else float(deterministic_time))
    cancelled = "the search was cancelled before a roster was found"
    if stop is not None and stop.is_set():
        return Solution("UNKNOWN", float("nan"), float("nan"), 0.0, [], {}, message=cancelled,
                        stopped_by="cancelled", problem=p, **limits)
    t0 = time.perf_counter()
    progress = _Progress(rm, p, on_solution, stop, t0, limits) if (on_solution or stop is not None) else None
    done = threading.Event()                                  # ends the watcher when the solve does
    interrupted = threading.Event()                           # the watcher asked CP-SAT to stop

    def watch() -> None:
        # CP-SAT only runs the solution callback at a new incumbent, so a search that has
        # none (or has stopped improving) would otherwise ignore the event until its time
        # limit. stop_search() is a no-op until the solver has started and harmless when
        # repeated, so the request is re-issued every poll until the solve returns.
        while not done.wait(STOP_POLL):
            if stop.is_set():
                interrupted.set()
                solver.stop_search()
    watcher = threading.Thread(target=watch, name="nurse-roster-stop", daemon=True) if stop is not None else None
    if watcher is not None:
        watcher.start()
    try:
        status = solver.solve(rm.model, progress)
    finally:
        done.set()
        if watcher is not None:
            watcher.join()
    dt = time.perf_counter() - t0
    improvements = progress.count if progress else 0
    name = solver.status_name(status)
    det = float(solver.deterministic_time)
    stopped_by = stop_cause(name, det, limits["deterministic_budget"], float(solver.wall_time),
                            limits["time_limit"], interrupted.is_set() or (progress is not None and progress.stopped))
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        msg = ""
        if status == cp_model.MODEL_INVALID:
            msg = "CP-SAT rejected the model: " + (rm.model.validate() or "a coefficient or bound "
                                                      "overflows int64 (a weight or hour value too large)")
        elif status == cp_model.INFEASIBLE:
            msg = "no roster satisfies the hard rules (see precheck)"
        elif status == cp_model.UNKNOWN:
            msg = cancelled if stop is not None and stop.is_set() else "no roster found within the time limit"
        return Solution(name, float("nan"), float("nan"), dt, [], {}, message=msg,
                        deterministic_time=det, stopped_by=stopped_by, problem=p, **limits)
    final = _solution(name, _assignment(solver.boolean_value, rm, p), p, solver.best_objective_bound,
                      dt, solver.objective_value, improvements)
    final.deterministic_time, final.stopped_by = det, stopped_by
    _stamp(final, limits)
    return _select(name, final, progress.best if progress else None,
                   progress is not None and progress.stopped, dt, improvements)


def _select(name: str, final: Solution, best: Solution | None, stopped: bool, dt: float,
            improvements: int) -> Solution:
    """The roster `solve` returns: the solver's final one, or the best incumbent the callback
    kept. An earlier incumbent with a lower validated cost replaces a time-limit roster (the
    solver ranks incumbents by an objective that can carry slack), and a search the callback
    stopped returns the incumbent it stopped at, not one another worker delivered or proved
    meanwhile: what the caller saw is what it gets, as OPTIMAL when it attains the proven
    optimum (the solver's objective at OPTIMAL, where the slack is squeezed out), else as
    FEASIBLE under the proven bound, the cause being the cancellation. Disagreement between
    the model and the validator on either candidate (a solver objective below a validated
    cost, a gap at OPTIMAL, an incumbent validated below the proven optimum) is gathered
    first: the final roster is then returned carrying that evidence (`mismatch`), which
    `verdict` reports as exit 3 whatever the returned roster's own numbers say, and no
    objective is rewritten."""
    found = []                                                # on the default path too (no callback)
    for who, s in (("the final roster", final), ("an incumbent", best)):
        if s is not None and s.solver_objective - s.objective <= -0.5:
            found.append(f"{who} validated at {s.objective:.0f} above its solver objective {s.solver_objective:.0f}")
    if name == "OPTIMAL":
        if abs(final.solver_objective - final.objective) >= 0.5:
            found.append(f"the final roster validated at {final.objective:.0f} against the proven optimum "
                         f"{final.solver_objective:.0f}")
        if best is not None and best.objective - final.solver_objective < -0.5:
            found.append(f"an incumbent validated at {best.objective:.0f} below the proven optimum "
                         f"{final.solver_objective:.0f}")
    if found:
        final.mismatch = "model and validator disagree: " + "; ".join(dict.fromkeys(found))
        return final
    if best is None:
        return final
    if name != "OPTIMAL":
        if stopped or best.objective < final.objective:
            best.status, best.best_bound, best.solve_seconds, best.improvements = "FEASIBLE", final.best_bound, dt, improvements
            best.deterministic_time = final.deterministic_time
            best.stopped_by = "cancelled" if stopped else final.stopped_by
            return best
        return final
    gap = best.objective - final.solver_objective             # against the proven optimum
    if stopped:
        optimal = gap < 0.5
        best.status, best.best_bound = ("OPTIMAL" if optimal else "FEASIBLE"), final.best_bound
        best.solve_seconds, best.improvements = dt, improvements
        if optimal:
            best.solver_objective = final.solver_objective    # the proven optimum, which it attains
        best.deterministic_time, best.stopped_by = final.deterministic_time, "cancelled"
        return best
    return final
