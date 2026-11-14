# Nurse roster solver: design

This document covers how the solver turns one request into one roster: the data flow, the time model, the decision
variables, every rule and how it is encoded, the fairness and hours arithmetic, and the checks that keep the model
honest. [how-to.md](how-to.md) covers usage.

## 1. Principles

* **One pure function.** The solver maps one request (one unit, one month) to one result. It has no database, no
  defaults and no settings for any particular unit. Every rule, switch and weight is in the request, and a missing
  field is an error that names its path.
* **Dates at the edge, indices inside.** Requests and results use ISO dates. Internally, days are 0-based indices
  within the month, and the request loader translates any error that names a day back into a date.
* **One rule registry.** `rules.py` lists every rule once, with its level, weight field and unit. The model, the
  validator and the explanation all iterate over the same registry. A rule's `active(p, id)` predicate controls whether
  the model adds anything for it and whether the validator evaluates it.
* **An independent validator.** `validate.py` re-derives every rule from a plain nurse × day matrix without OR-Tools.
  Every roster the model returns is re-scored by the validator, and a disagreement is reported as an error (exit 3)
  rather than hidden.
* **Facts are not errors.** Recorded days (history, and days before a re-plan cutoff) are fixed. A hard rule broken
  entirely within them is reported, never enforced, so the past can never make the month infeasible.

## 2. Data flow

```
request JSON ──request.load_request──▶ Problem ──precheck──▶ warnings
                                          │
                                          ├─model.build_model──▶ CP-SAT model ──model.solve──▶ Solution
                                          │                                                     │
                                          └──────────validate / explain (re-score)◀─────────────┘
                                                                                                │
                                                                       result.build_result ─────▶ result JSON
```

| Module | Role |
|---|---|
| `request.py` | Loads and checks a request. Works out the calendar from `month`, maps dates to indices, applies `replan_from`, and returns a `Request` (problem, settings, cutoff). |
| `calendar.py` | Month facts: day count, first weekday, and Umm al-Qura Ramadan days (via `hijridate`, 1924–2077). |
| `problem.py` | The internal contract: `Problem`, `Nurse`, `GeneralRules`, `DepartmentRules`, `Weights`. Strict checks with size limits, plus derived quantities such as hours, caps, allowed shifts and replan facts. |
| `rules.py` | The rule registry, giving each rule's level, weight and enabling predicate. |
| `model.py` | Builds the CP-SAT model and runs the search, with progress callbacks, cancellation and reproducible mode. |
| `validate.py` | The independent checker: one function per rule, returning matches (who, when, how much). |
| `explain.py` | Organises the validator's matches per rule and per nurse, and compares two rosters. |
| `fairness.py` | The fairness and overtime-share arithmetic shared by the model, the validator and the carry-over. |
| `precheck.py` | Cheap checks that explain likely infeasibility in plain words. |
| `result.py` | Serialises the result, including hours accounting and the carry-over. Also holds `verdict` (the exit code). |
| `export.py` | CSV export. |
| `solver_service/` | The HTTP service: a single-job runner in a background thread, plus `/validate` and `/precheck` without a search. |

The request is the external contract. A `Problem` can also be serialised as its own JSON (schema version 8), mostly for
tests and debugging.

## 3. Time model

The **horizon** is the nurse's history `previous` (P days, the longest history among the nurses) followed by the D days
of the month. Pattern rules (successions, run limits, double cycles, night rest, switches) run over the whole horizon,
so they see across the month boundary with no special cases. History days are fixed by constraints, and presolve
removes them.

Only constraints that reach a **free** day are enforced as hard rules. A free day is a month day on or after the
planning cutoff `planning_from`, which is 0 unless the month is being re-planned. Hours, headcounts, fairness and
weekend counts use month days only.

Two memories reach further back than any history, and they are carried explicitly so that `previous` stays bounded
(at most 62 days):

* `last_shift_kind`: whether the last shift worked before the history began was a night or a day shift. The switch rule
  uses this.
* `rest_owed`: off days still owed to the last night before the history began. The night-rest rule uses this.

`needed_history` works out how far back the active pattern rules look (for example `2K + R` for the double-cycle rule).
A request whose rules would look back more than 62 days is refused.

### Re-plans and month close

`replan_from = d` makes days `0..d-1` facts. Each elapsed cell is pinned: an explicit pin wins, then a leave day is
off, and otherwise the cell takes its `published` value. `planning_from` becomes `d`. From `d` onwards, the `change`
weight charges each unpinned cell that differs from `published`. A cutoff equal to the day after the month makes every
day a fact. That month-end run produces the reconciled carry-over.

## 4. Decision variables

For every nurse `n`, horizon day `t` and unit shift `s`:

* `x[n][t][s]`: the nurse works shift `s` on day `t`;
* `off[n][t]`: the nurse is off (or on leave).

`add_exactly_one(x[n][t][*] + [off[n][t]])` holds on every day. The one exception is a history day that names a shift
the unit no longer defines: that day is worked, but in no current shift.

Auxiliary variables are created per rule, as described below. All of them are Boolean or small bounded integers, and
the objective is a weighted sum of penalty terms.

## 5. Hard rules

| Rule | What it means | Encoding |
|---|---|---|
| one shift per day | One shift or off, every day | `add_exactly_one` (not in the registry: a matrix cannot break it) |
| `unavailable` | No shift on a leave day | `off = 1` |
| `allowed_shifts` | Only the nurse's allowed shifts; nurses on probation never work the night shift | `x = 0` on free days |
| `pin` | A pinned cell holds its value | literal `= 1` |
| `succession` | Forbidden `(today, tomorrow)` pairs; `no_mixed_blocks` adds D→N and N→D | clause `¬x[t,a] ∨ ¬x[t+1,b]` |
| `hours_bounds` | Overtime within the cap: `min(month cap, year cap − ytd)`, or less than the nurse's shortest shift for a nurse on probation | `worked − required = ot − short`, `0 ≤ ot ≤ cap` |
| `consecutive` | At most `K = max_consecutive_shifts` worked days in a row | an off day in every window of `K+1` (`bool_or`) |
| `double_cycle` | A cycle, one day off and another cycle must be followed by `R = double_cycle_rest` off days | for each window `W^K O W^K`, clause `¬pattern ∨ off[t+2K+1+r]` for `r < R` |
| `headcount` | Anchor shift staffing within `[min, max]` (weekday / weekend), counting `also_counts` shifts | linear constraint over the counted nurses |
| `team_leader` | At least one team leader on each anchor shift | `bool_or` |
| `gender` | At least one `M` and one `F` on each anchor shift | two `bool_or`s |
| `skill_expired` | No shift after the expiry of a recorded required skill | `off = 1` on the barred days |
| `following` | A nurse on probation follows their preceptor (section 8) | reified per-day literals plus one `missed` literal |

Rules with a department switch between hard and soft:

| Rule | Switch | Hard encoding | Soft encoding |
|---|---|---|---|
| `night_rest` | `night_rest_hard` | The first D after an N needs `night_rest_days` off days in between: short-gap clauses for each gap length `k`. In a unit with other shifts, gaps longer than the rest period use an enforced linear sum of off days. `rest_owed` is enforced on the month's first D. | the same clauses, each with a penalty literal per offending D day |
| `shift_switch` | `switches_hard` | A memory literal `last_n[t]` (was the most recent worked shift a night?) feeds the history and `last_shift_kind`. A switch is a worked day whose kind differs from that memory. `Σ switches ≤ max_switches` (or the past's own count, if that is already higher). | `excess ≥ Σ switches − max_switches` |
| `skill_need` | `skill_needs_hard` | `Σ holders on shift ≥ min` per free day | `short ≥ min − Σ holders` per day |

A nurse on probation, and anyone else not in `counted(p)`, counts toward no headcount, team leader, gender, score or
skill need.

## 6. Soft rules and the objective

The objective is `minimise Σ weight × term` over every active soft rule. A weight of 0 turns a rule off, and so does
the rule's enabling predicate. Every term is bounded below by its true value, and only the objective presses it down,
so a FEASIBLE incumbent may carry slack. That is why the cost in a result is always the validator's.

| Rule (weight) | Unit | Encoding |
|---|---|---|
| `nurse_overtime` | hour | `ot` from the hours equation |
| `nurse_shortfall` | hour | `short` from the hours equation, carried over |
| `avoidable_shortfall` | hour | `avoid ≥ short − (min shift − 1) − unreachable_hours` (section 7) |
| `dept_overtime` | hour | `excess ≥ Σ ot (counted nurses) − dept_overtime_cap` |
| `overtime_above_share` | whole hour | the floor of the hours above the nurse's target, beyond the tolerance (section 9) |
| `fair_day`, `fair_night` | 0.1 shift | deviation from `share × shifts worked` (± carried adjustment), beyond `fair_tolerance` |
| `fair_weekend` | 0.1 shift | deviation of weekend days from the fair weekend share (± carried adjustment) |
| `score_deficit` | 0.01 × person | per anchor shift-day: `deficit ≥ Σ (target − score_i) · x_i` |
| `score_spread` | 0.01 × person, or 0.01 | `band`: `excess ≥ Σ (score_i − band_hi) · x_i`, which is linear and fast. `spread`: `avg_hi − avg_lo` over every shift, linearised by enumerating the headcount. |
| `excess_weekend` | day | `ex ≥ Σ weekend days worked − max_weekends` |
| `excess_full_run` | run | one literal per possible run end of exactly K days. A run reaching the month's last day counts in this month. `ex ≥ Σ − max_full_runs`. |
| `undesired_day`, `desired_day` | day | a literal per preference day not on leave, listed twice when `preferences_not_kept > 0` |
| `change` | cell | a literal per unpinned cell that differs from `published` |

`explain.py` breaks the validated cost down per rule and per nurse. Unit-level terms are listed under the key `""` and
reported as `department_cost`.

## 7. Hours

For nurse `n`:

* `daily(d)`: `hours_per_day` (or `ramadan_hours_per_day` for a `ramadan_reduced` nurse on a Ramadan day), minus
  `breastfeeding_reduction` if applicable, and 0 on weekend days and public holidays;
* `contract` = `Σ daily(d)` over the month, and `leave_credit` = `Σ daily(d)` over the nurse's leave days;
* `required` = `max(0, contract − leave_credit + hours_balance)`. A negative value is unused advance credit and is
  carried on;
* `worked − required = ot − short`.

There is deliberately **no hard floor** on hours. What a nurse can still work is bounded by every other hard rule at
once, so any per-nurse floor could make a month infeasible. Instead:

* `max_hours` is the hours of the nurse's pinned shifts plus their longest allowed shift on every free, non-leave,
  non-barred day. `unreachable_hours = max(0, required − max_hours)`;
* `avoidable_shortfall` charges every hour of shortfall beyond a remainder below one shift and beyond the unreachable
  hours. Its weight is meant to dominate every other weight.

Carry-over: `hours_balance_out = short − unused_credit`. Overtime is paid, not carried, and adds to `ytd_overtime`.

## 8. Skills and probation

**Skills.** A nurse's `skills` maps each skill to its last day (`null` = all month, `-1` = expired before the month).
`holds_skill(n, s, d)` is true when the skill is recorded and `d` is on or before its last day. A recorded required
skill that expires during the month bars every later day (`barred_days`). Barred days hold no shift, earn no leave
credit, and count as unavailable in the hours and fairness arithmetic. Preferences on leave days and barred days are
ignored (`superseding`).

**Probation.** `counted(p)` excludes nurses on probation from every staffing count, from fairness, and from the unit's
overtime totals. `Problem.allowed` removes the night shift for them, and `overtime_cap` keeps their overtime below one
shift. The `following` rule covers each judged day: free, not pinned, not leave and not barred. On such a day, `fol`
is whether the preceptor works a shift the nurse on probation may work. If it is, the nurse on probation either works
that same shift or is off. Being off on any such day sets `missed`, and `missed` then forbids working on any day that is
not a following day. Nothing constrains the preceptor.

## 9. Fairness arithmetic

All fairness arithmetic is integer: shares are in thousandths of a shift, deviations in tenths of a shift, and the
overtime share in hundredths of an hour. This lets the model and the validator count exactly the same amounts.

* **Day/night share.** Each fair shift's share of demand comes from the headcount midpoints over the month, normalised
  to sum to exactly 1000. Only **flexible** nurses, who may work every fair shift, are measured. A nurse's target for
  shift `s` is `share_s × shifts worked`. With `carry_fairness`, the target is shifted by `night_balance`, limited to
  `carry_cap`: the night target goes down and the day target goes up by the same amount.
* **Weekend share.** Weekend slots (headcount midpoints, divided by the largest `also_counts` multiplicity) are divided
  among the counted nurses, then prorated by the weekend days each nurse is available. `friday_balance` shifts the
  target, again limited to `carry_cap`.
* **Balances out.** `night_balance += nights − share × shifts` (flexible nurses only) and
  `friday_balance += weekend days − expected`. Both are rounded to 0.01. A nurse on probation carries 0 out.
* **Overtime share.** With `R` = the hours owed by all counted nurses, `r` = the nurse's hours owed and `O` = the unit's
  overtime, the nurse's share is `O·r/R`. The target is the share minus `overtime_balance` (limited to
  `overtime_carry_cap`, and only with `carry_fairness`). The charged hours are `floor((S(ot − tol) − 100·r·O + R·adj) / S)`
  with `S = 100·R`, clamped to `[0, ot]`. The model encodes exactly this floor. The balance carried out is
  `in + ot − share`.

## 10. Search, validation and verdict

`model.solve` runs CP-SAT with the request's time limit, `workers` threads and seed 0. The problem is copied when the
model is built and again when the solve starts, and both copies are fingerprinted (SHA-256 of the problem). A caller
editing its own copy during the search therefore cannot change what is solved or how it is scored.

* **Progress.** With an `on_solution` callback or a `stop` event, every incumbent is validated, and the best by
  validated cost is kept. A stop event ends the search within about 50 ms. The best roster so far is returned, or
  `UNKNOWN` if none was found.
* **Reproducible mode.** One worker, seed 0, and `max_deterministic_time` set to the budget. The wall-clock limit
  becomes ten times the budget and serves only as a safeguard. `stopped_by` reports `proof`, `deterministic_budget`,
  `time_limit`, `cancelled` or `model_invalid`. A run counts as deterministic only when a proof or the deterministic
  budget ended it.
* **Verdict** (`result.verdict`, which is also the CLI exit code):
  * 0: a roster with no hard violations.
  * 1: no roster.
  * 2: the validator found hard violations.
  * 3: the solver's objective is below the validated cost, differs from it at `OPTIMAL`, or an incumbent validated
    below the proven optimum. Any of these means the model and the validator disagree.

`precheck.py` runs before the solve. It reports capacity against demand, eligible staff per shift and day, team leader
and gender availability, skill-need holders, expiring required skills, contradictory pins, histories too short for the
pattern rules, unknown history shifts, hours that cannot fit, and shifts that cannot fit under the cycle rules (worked
out with a small automaton over the run and double-cycle rules).

## 11. Carry-over

For each nurse, the result's `carry_over` uses the request's own field names, so next month's request is the fixed
nurse fields plus this block:

| Field | Value |
|---|---|
| `hours_balance` | shortfall still owed (+) or unused advance credit (−) |
| `night_balance`, `friday_balance`, `overtime_balance` | the balances after this month (section 9) |
| `ytd_overtime` | incoming value + this month's overtime |
| `previous` | the history plus this month, trimmed to `max(days in month, needed_history)` and at most 62 days |
| `last_shift_kind`, `rest_owed` | the two memories, worked out through whatever part of the history was dropped |
| `preferences_not_kept` | preferences not kept this month, ignoring leave and barred days |

The same function (`nurse_accounting`) produces the block for a solve and for `solver_service.grid.nurses_of`, which
scores a given roster with no search, so the two always agree.

## 12. Input limits

| Limit | Value |
|---|---|
| Days in a problem | 62 |
| History per nurse | 62 days |
| Nurses | 1000 |
| Shifts per unit | 12 |
| Shift length | 1–24 h |
| Weight | 0–1 000 000 |
| Hours fields (caps, balances, YTD) | ±1 000 000 |
| Rule counts and fairness parameters | 0–1000 |
| `night_rest_days`, `rest_owed` | 0–7 |
| `max_full_runs` | 0–31 |
| `overtime_tolerance`, `overtime_carry_cap` | 0–744 h |
| Skills per list | 100 |
| Names (nurse IDs, shifts, skills) | 100 characters, without leading `= + - @ '` or control characters, so the CSV export never needs to escape them |
| JSON nesting | 200 levels; duplicate keys are refused |

Every malformed input raises `ValueError` with the field path, prefixed `request:` (or `problem:` for a problem file).
The CLI maps this to exit 4 and the service to HTTP 422.

## 13. Versions

* **Request**: version 2 is written; versions 1 and 2 are read. Version 1 is read as version 2 with no skills, and may
  not contain any skill field. The full-run, overtime-share and probation fields are optional in both versions, and
  their defaults switch the rules off (`request._optional_rules`).
* **Result**: version 1. New soft rules add keys to `penalties`, and new hard rules add `rule` values to `violations`.
  A violation may gain keys beside `rule`, `nurse`, `date` and `text`; a reader ignores the ones it does not know.
* **Problem JSON**: version 8. Only the current version is read.

## 14. Performance

On the 25-nurse emergency-unit sample (two 12-hour shifts, every rule on, 8 workers, OR-Tools 9.15), the model has
about 5 200 variables and 10 300 constraints and builds in under 100 ms. A good roster arrives within a few seconds,
and the search keeps improving it for a minute or two. Score balancing in `spread` mode and very large weight ratios
slow convergence the most. The four-nurse sample is solved to proven optimality in well under a second.

## 15. Decisions

* **2026-11-14: a violation states its facts as fields.** A reader of a violation needed the shift of its shift-day and
  a headcount's figures, and had only the sentence to take them from, so rewording a sentence could break a reader
  without failing a test. A match now carries them as flat optional fields (`rules.Match`): `shift` on the four rules
  of a shift-day (headcount, team leader, both genders, skill need), and `present`, `minimum` and `maximum` on a
  headcount. They are the facts a reader uses today and no more; every other rule keeps rule, nurse, date and text.
  `result.stated` is the one rendering of a match as it leaves the solver, shared by a result's violations and by a
  no-search check of a grid (its violations and the matches of its explanation), which leaves a key out where the rule
  has no such fact. The keys are additive, so the result stays version 1.
