# Using the nurse roster solver

The solver turns one **request** (one unit, one month, every rule spelled out) into one **result**. The result holds the
roster as nurse × date, the rules it breaks, its cost, and a per-nurse carry-over block for next month. You can run it
as a command-line program on JSON files, or call it through its HTTP service (see [README.md](README.md)). The request
and result formats are the same either way.

Contents

1. Install and run the sample
2. Write a request
3. Run the solver
4. Read the result
5. Month to month: the carry-over
6. Re-plan a month that has started
7. Close a month
8. Reproducible runs
9. Skills
10. Nurses on probation
11. Renaming or retiring a shift
12. The hours rule
13. Troubleshooting

---

## 1. Install and run the sample

You need Python 3.14 and `uv`.

```bash
uv sync
uv run python -m nurse_roster solve tests/fixtures/er_request_2026-03.json --out result.json --csv roster.csv
uv run python -m pytest -q
```

The sample is a 25-nurse emergency unit for March 2026, with two 12-hour shifts and a Ramadan period that runs into the
month. It is also the best template for your own requests. A smaller one is
`service_tests/fixtures/four_nurses.json`, which section 2.5 reproduces.

---

## 2. Write a request

A request is one JSON object. Every rule, switch and weight must be present, and an unknown field is an error, so the
solver can never fill in a number you did not choose. Dates are written `YYYY-MM-DD` and must fall inside the month.

### 2.1 Top level

```json
{
  "version": 2,
  "month": "2026-03",
  "ramadan_days": {"from": "2026-02-18", "to": "2026-03-19"},
  "public_holidays": [],
  "general":    { "...": "hospital-wide rules, 2.2" },
  "department": { "...": "the unit's rules, 2.3" },
  "nurses":     [ "... one object per nurse, 2.4" ],
  "solver":     {"time_limit_seconds": 20, "reproducible": false},
  "published":  { "...": "optional, section 6" },
  "replan_from": "2026-03-12",
  "renamed_shifts": {"N": "Night"}
}
```

| Field | Required | Meaning |
|---|---|---|
| `version` | yes | `2`. Version `1` (requests without skills) is still accepted; see 2.6. |
| `month` | yes | `YYYY-MM`. The solver works out the day count, the first weekday and the Ramadan days from it. |
| `ramadan_days` | no | Absent or `null`: taken from the Umm al-Qura calendar. `[]`: no Ramadan days. A list of dates: exactly those days. `{"from", "to"}`: an inclusive range, which may start or end outside the month, cut to the month. Use the range form when the dates are fixed by moon sighting. |
| `public_holidays` | yes | Dates, possibly none. No regular hours are owed on them. |
| `general` | yes | Hospital-wide rules (2.2). |
| `department` | yes | The unit's rules (2.3). |
| `nurses` | yes | One object per nurse (2.4). |
| `solver` | yes | `time_limit_seconds` (a positive number) and `reproducible` (section 8). |
| `published` | no | The roster in force, in the same shape as a result's `roster`. Used for re-plans (section 6). |
| `replan_from` | no | The first date to plan again. Needs `published`. |
| `renamed_shifts` | no | `{old name: current name}`, applied to every nurse's history (section 11). |

### 2.2 `general`: hospital-wide rules

```json
"general": {
  "shift_hours": {"D": 12, "N": 12, "A": 8, "B": 8, "C": 8},
  "hours_per_day": 8,
  "ramadan_hours_per_day": 6,
  "breastfeeding_reduction": 1,
  "weekend_weekdays": [4],
  "forbidden_successions": [["N", "D"], ["N", "A"], ["D", "C"]],
  "forbidden_successions_enabled": true,
  "hours_rule_enabled": true,
  "overtime_caps_enabled": true,
  "overtime_month_cap": 40,
  "overtime_year_cap": 720,
  "dept_overtime_cap": 400,
  "required_skills": ["BLS"],
  "weights": { "...": "below" }
}
```

| Field | Meaning |
|---|---|
| `shift_hours` | Every shift any unit may use, with its length in hours (1–24). Each of a unit's `shifts` must appear here. `L` is reserved, because the CSV export uses it to mark leave. |
| `hours_per_day` | Regular hours owed per working day, meaning a day that is neither a weekend day nor a public holiday. |
| `ramadan_hours_per_day` | Hours owed on Ramadan days by a nurse with `ramadan_reduced: true`. |
| `breastfeeding_reduction` | Hours taken off each working day for a nurse with `breastfeeding: true`. |
| `weekend_weekdays` | Weekdays that count as the weekend, 0 = Monday … 6 = Sunday. `[4]` means Friday only. |
| `forbidden_successions` | `[today, tomorrow]` pairs of shifts that may not follow each other. |
| `forbidden_successions_enabled` | Turns the list above on or off. |
| `hours_rule_enabled` | When off, there is no limit on the number of shifts and no overtime or shortfall accounting. |
| `overtime_caps_enabled` | When off, there is no monthly or yearly overtime cap per nurse. |
| `overtime_month_cap` | Hard cap on a nurse's overtime hours this month. |
| `overtime_year_cap` | Hard cap on a nurse's overtime hours in the year, counted from their `ytd_overtime`. |
| `dept_overtime_cap` | Soft cap on the unit's total overtime hours this month. |
| `required_skills` | Skills a nurse must hold to work (section 9). |

`weights` sets the price of each soft rule per unit of violation. A weight of `0` turns the rule off. Only the ratios
between weights matter, because the solver minimises their weighted sum. Each weight is a whole number from 0 to 1 000 000.

| Weight | Charged per … |
|---|---|
| `night_rest` | day shift that comes too soon after a night shift (only when `night_rest_hard` is false) |
| `dept_overtime` | hour of unit overtime above `dept_overtime_cap` |
| `nurse_overtime` | hour of a nurse's overtime |
| `nurse_shortfall` | hour a nurse ends below their requirement (carried over) |
| `avoidable_shortfall` | hour of shortfall beyond what is unavoidable (section 12). Make this the largest weight by a wide margin; the samples use 1000. |
| `fair_day`, `fair_night` | 0.1 shift by which a nurse's day (or night) count strays from their proportional share, beyond `fair_tolerance` |
| `fair_weekend` | 0.1 shift by which a nurse's weekend days stray from their fair share |
| `score_deficit` | 0.01 score point × person by which a shift's average score falls below the target |
| `score_spread` | 0.01 score point × person above the band ceiling (band mode), or 0.01 between the strongest and weakest shift (spread mode) |
| `excess_weekend` | weekend day worked above `max_weekends` |
| `excess_full_run` | full run worked above `max_full_runs` (optional, defaults to 0) |
| `shift_switch` | day/night switch above `max_switches` (only when `switches_hard` is false) |
| `undesired_day` | day a nurse asked to have off but works |
| `desired_day` | day a nurse asked to work but has off |
| `change` | cell that differs from `published` in a re-plan |
| `skill_shortfall` | nurse missing from a skill need on one shift-day (only when `skill_needs_hard` is false) |
| `overtime_above_share` | whole hour of a nurse's overtime above their overtime target (optional, defaults to 0) |

### 2.3 `department`: the unit's rules

```json
"department": {
  "name": "ER",
  "shifts": ["D", "N"],
  "headcounts": [
    {"shift": "D", "weekday_min": 7, "weekday_max": 9, "weekend_min": 8, "weekend_max": 9, "also_counts": []},
    {"shift": "N", "weekday_min": 7, "weekday_max": 9, "weekend_min": 8, "weekend_max": 9, "also_counts": []}
  ],
  "headcount_enabled": true,
  "require_team_leader": true,
  "require_both_genders": true,
  "score_target": 0.70, "score_target_weekend": 0.80,
  "score_balance": true, "score_balance_mode": "band",
  "score_band_hi": 0.78, "score_band_hi_weekend": 0.88,
  "night_shift": "N", "day_shift": "D",
  "night_rest_days": 2, "night_rest_hard": true,
  "fair_shifts": ["D", "N"], "fair_tolerance": 0.5, "fair_weekends": true,
  "carry_fairness": true, "carry_cap": 2.0,
  "max_consecutive_shifts": 3, "double_cycle_rest": 2,
  "max_weekends": 3, "max_full_runs": 3,
  "no_mixed_blocks": true, "max_switches": 2, "switches_hard": false,
  "share_overtime": true, "overtime_tolerance": 12, "overtime_carry_cap": 12,
  "skill_needs": [{"shift": "N", "skill": "ICU", "weekday_min": 2, "weekend_min": 2}],
  "skill_needs_hard": true
}
```

| Field | Meaning |
|---|---|
| `name` | The unit's name. |
| `shifts` | The shifts this unit works (at most 12), each listed in `general.shift_hours`. |
| `headcounts` | One entry per staffed ("anchor") shift, giving the minimum and maximum number of nurses on a weekday and on a weekend day. `also_counts` lists overlapping shifts that also count toward it. |
| `headcount_enabled` | When off, there is no headcount rule. |
| `require_team_leader` | Hard: every anchor shift needs at least one `team_leader`. |
| `require_both_genders` | Hard: every anchor shift needs at least one `M` and one `F`. |
| `score_target`, `score_target_weekend` | Soft: the average `score` of each anchor shift should reach this, on weekdays and weekend days. |
| `score_balance`, `score_balance_mode`, `score_band_hi`, `score_band_hi_weekend` | Soft: keep shift averages close together. `band` mode charges for any average above the ceiling and solves fast. `spread` mode minimises the gap between the strongest and weakest shift and solves more slowly. |
| `night_shift`, `day_shift` | Tells the night-rest, switch and fairness rules which shift is the night and which is the day. |
| `night_rest_days`, `night_rest_hard` | Off days owed after a night shift before the next day shift (0–7; 0 turns the rule off). Hard, or charged at `night_rest`. |
| `fair_shifts`, `fair_tolerance` | Shifts shared out fairly (each needs a headcount entry), and the deviation, in shifts, that costs nothing. |
| `fair_weekends` | Spread weekend days evenly. |
| `carry_fairness`, `carry_cap` | Let the carried `night_balance` and `friday_balance` move this month's fair targets, by at most `carry_cap` shifts. |
| `max_consecutive_shifts` | Hard: the longest run of worked days, called a cycle (0 = no limit). |
| `double_cycle_rest` | Hard: after a cycle, one day off and another cycle, at least this many days off must follow. |
| `max_weekends` | Soft: weekend days a nurse may work per month before `excess_weekend` applies. |
| `max_full_runs` | Soft: full runs (exactly `max_consecutive_shifts` worked days in a row) a nurse may work per month before `excess_full_run` applies (0–31; absent or `null` = no cap). A run counts in the month that holds its last day. |
| `no_mixed_blocks` | Hard: a run of consecutive worked days is all day shifts or all night shifts. |
| `max_switches`, `switches_hard` | Day/night switches allowed per month, counting the switch across the month boundary. Hard, or charged at `shift_switch`. |
| `share_overtime`, `overtime_tolerance`, `overtime_carry_cap` | Soft: share the unit's overtime in proportion to the hours each nurse owes (2.7). The last two are whole hours, 0–744. Optional; absent means off. |
| `skill_needs`, `skill_needs_hard` | Minimum skilled nurses per shift (section 9). |

The pattern rules may not look back further than a history can hold. `max_consecutive_shifts`, `double_cycle_rest` and
`night_rest_days` together must look back no more than 62 days; otherwise the request is refused with a message saying
which rules to lower.

### 2.4 `nurses`

```json
{
  "id": "N01", "gender": "M", "team_leader": true, "score": 0.99,
  "ramadan_reduced": false, "breastfeeding": false,
  "ytd_overtime": 100, "hours_balance": 0, "night_balance": 0.0, "friday_balance": 0.0,
  "last_shift_kind": null, "rest_owed": 0,
  "preferences_not_kept": 0, "overtime_balance": 0.0,
  "leave": ["2026-03-04"], "undesired_days": [], "desired_days": ["2026-03-20"],
  "pinned": {"2026-03-01": "D"}, "allowed_shifts": null,
  "previous": ["D", "D", "", "N", "N", "N", "", "", "D", "D"],
  "skills": {"BLS": "2026-03-14", "ICU": null},
  "on_probation": false, "preceptor": null
}
```

The twelve fields `id` to `rest_owed` are required. Every other field may be left out: a list then defaults to empty, a
balance or count to 0, `on_probation` to false and `preceptor` to none.

| Field | Meaning |
|---|---|
| `id` | A unique name (at most 100 characters). |
| `gender` | `"M"` or `"F"`. |
| `team_leader` | Counts toward `require_team_leader`. |
| `score` | Performance score, 0 to 1. |
| `ramadan_reduced` | Owes `ramadan_hours_per_day` on Ramadan days. |
| `breastfeeding` | Owes `breastfeeding_reduction` fewer hours per working day. |
| `ytd_overtime` | Overtime hours already worked this year, checked against `overtime_year_cap`. |
| `hours_balance` | Hours still owed from earlier months (+) or worked ahead (−). Carried over. |
| `night_balance`, `friday_balance` | Nights and weekend days worked above (+) or below (−) the nurse's fair share so far this year, in shifts. Carried over. |
| `overtime_balance` | Hours of overtime worked above (+) or below (−) the nurse's overtime share so far this year. Carried over. |
| `last_shift_kind` | `"night"`, `"day"` (any other work) or `null`: the kind of the last shift worked before `previous` begins. Carried over. Use `null` for a new nurse. |
| `rest_owed` | Off days still owed to the last night shift before `previous` begins. Carried over. |
| `preferences_not_kept` | How many of the nurse's preferences last month's roster did not keep. When above 0, each preference this month costs twice its weight. Carried over. |
| `leave` | Dates off for leave of any kind. These are credited at the regular daily rate, so leave never creates a debt. |
| `undesired_days`, `desired_days` | Dates the nurse would rather not work, or would like to work (soft). A date that falls on leave is ignored. A date may not appear in both lists. |
| `pinned` | `{date: shift}` cells that are fixed, with `""` meaning pinned off (hard). |
| `allowed_shifts` | `null` = any of the unit's shifts; otherwise the only shifts this nurse works. |
| `previous` | The nurse's last days before the month, oldest first, as a shift name or `""` for off (at most 62). Carried over. |
| `skills` | `{skill: expiry date or null}` (section 9). |
| `on_probation`, `preceptor` | Section 10. |

For a **new unit or new nurse**, use `previous: []`, `last_shift_kind: null`, `rest_owed: 0`, set every balance to 0,
and set `ytd_overtime` to the hours worked so far this year.

### 2.5 A complete small request

Four nurses, two 12-hour shifts and June 2026. It is the same as `service_tests/fixtures/four_nurses.json`.

```json
{
  "version": 2, "month": "2026-06", "ramadan_days": [], "public_holidays": [],
  "general": {
    "shift_hours": {"D": 12, "N": 12}, "hours_per_day": 8, "ramadan_hours_per_day": 6,
    "breastfeeding_reduction": 1, "weekend_weekdays": [4],
    "forbidden_successions": [["N", "D"]], "forbidden_successions_enabled": true,
    "hours_rule_enabled": true, "overtime_caps_enabled": true,
    "overtime_month_cap": 40, "overtime_year_cap": 720, "dept_overtime_cap": 400,
    "weights": {"night_rest": 0, "dept_overtime": 1, "nurse_overtime": 1, "nurse_shortfall": 1,
                "avoidable_shortfall": 1000, "fair_day": 1, "fair_night": 1, "fair_weekend": 1,
                "overtime_above_share": 1, "score_deficit": 0, "score_spread": 0, "excess_weekend": 0,
                "excess_full_run": 4, "shift_switch": 8, "undesired_day": 2, "desired_day": 1,
                "change": 3, "skill_shortfall": 0},
    "required_skills": []
  },
  "department": {
    "name": "Ward A", "shifts": ["D", "N"],
    "headcounts": [{"shift": "D", "weekday_min": 1, "weekday_max": 1, "weekend_min": 1, "weekend_max": 1, "also_counts": []},
                   {"shift": "N", "weekday_min": 1, "weekday_max": 2, "weekend_min": 1, "weekend_max": 2, "also_counts": []}],
    "headcount_enabled": true, "require_team_leader": false, "require_both_genders": false,
    "score_target": 0.0, "score_target_weekend": 0.0, "score_balance": false, "score_balance_mode": "band",
    "score_band_hi": 1.0, "score_band_hi_weekend": 1.0,
    "night_shift": "N", "day_shift": "D", "night_rest_days": 2, "night_rest_hard": true,
    "fair_shifts": ["D", "N"], "fair_tolerance": 0.5, "fair_weekends": true,
    "carry_fairness": true, "carry_cap": 2.0,
    "share_overtime": true, "overtime_tolerance": 12, "overtime_carry_cap": 12,
    "max_consecutive_shifts": 4, "double_cycle_rest": 2, "max_weekends": 3, "max_full_runs": 3,
    "no_mixed_blocks": true, "max_switches": 2, "switches_hard": false,
    "skill_needs": [], "skill_needs_hard": true
  },
  "nurses": [
    {"id": "Amal", "gender": "F", "team_leader": true, "score": 0.9, "ramadan_reduced": false, "breastfeeding": false,
     "ytd_overtime": 0, "hours_balance": 0, "night_balance": 0.0, "friday_balance": 0.0, "last_shift_kind": null, "rest_owed": 0},
    {"id": "Badr", "gender": "M", "team_leader": false, "score": 0.8, "ramadan_reduced": false, "breastfeeding": false,
     "ytd_overtime": 0, "hours_balance": 0, "night_balance": 0.0, "friday_balance": 0.0, "last_shift_kind": null, "rest_owed": 0,
     "leave": ["2026-06-10", "2026-06-11"]},
    {"id": "Dana", "gender": "F", "team_leader": false, "score": 0.7, "ramadan_reduced": false, "breastfeeding": true,
     "ytd_overtime": 0, "hours_balance": 0, "night_balance": 0.0, "friday_balance": 0.0, "last_shift_kind": null, "rest_owed": 0,
     "undesired_days": ["2026-06-05"], "desired_days": ["2026-06-12"], "preferences_not_kept": 1},
    {"id": "Emad", "gender": "M", "team_leader": true, "score": 0.6, "ramadan_reduced": false, "breastfeeding": false,
     "ytd_overtime": 0, "hours_balance": 0, "night_balance": 0.0, "friday_balance": 0.0, "last_shift_kind": null, "rest_owed": 0,
     "allowed_shifts": ["D"]}
  ],
  "solver": {"time_limit_seconds": 10, "reproducible": false}
}
```

```bash
uv run python -m nurse_roster solve service_tests/fixtures/four_nurses.json --out small_result.json --csv small.csv
```

This request is solved to proven optimality in well under a second.

### 2.6 Optional fields and version 1

Some rule fields may be left out. When they are, the rule behaves as if switched off:

* `department.max_full_runs` (absent = no cap) and `weights.excess_full_run` (absent = 0);
* `department.share_overtime` (absent = false), `overtime_tolerance` and `overtime_carry_cap` (absent = 0), and
  `weights.overtime_above_share` (absent = 0);
* the nurse fields listed as optional in 2.4.

A **version 1** request is read as a version 2 request with no skills: no `required_skills`, a `skill_shortfall` weight
of 0, no `skill_needs`, `skill_needs_hard` set to true and no nurse `skills`. A version 1 request may not contain any of
those five fields.

### 2.7 The overtime share

When `share_overtime` is on, the unit's overtime for the month is shared in proportion to the hours each nurse owes:

* a nurse's **share** = the unit's overtime × the hours that nurse owes ÷ the hours every nurse owes;
* their **target** = the share minus their `overtime_balance`, limited to ± `overtime_carry_cap` hours. The balance only
  moves the target when `carry_fairness` is on;
* every whole hour of their overtime above the target, beyond `overtime_tolerance`, costs `overtime_above_share`, up to
  the amount of overtime they actually worked. Part hours are never charged.

A nurse who owes no hours has no share. Nothing is charged while the hours rule is off.

---

## 3. Run the solver

```bash
uv run python -m nurse_roster solve REQUEST.json [--out RESULT.json] [--csv ROSTER.csv] [--workers N]
                                                 [--explain] [--explain-json PATH] [--progress] [--log]
```

| Option | Effect |
|---|---|
| `--out RESULT.json` | Writes the result (section 4). It is written whenever a solve ran, even without a roster. |
| `--csv ROSTER.csv` | Also exports the roster as a spreadsheet. Each nurse gets a row with their history, the month's cells (`L` marks leave), hours, balances and shift counts. Headcount, team-leader and score rows per shift follow. Only written when there is a roster. |
| `--workers N` | Search threads, 1–128. Default: `NURSE_ROSTER_WORKERS`, otherwise 8. |
| `--explain` | Prints each soft rule's largest violations and the cost per nurse. In a re-plan, it also compares the result with the published roster. |
| `--explain-json PATH` | Writes that full analysis as JSON. |
| `--progress` | Prints the cost of each improved roster while the search runs. |
| `--log` | Prints CP-SAT's own search log. |

The console shows, in order:

1. `precheck:` lines: plain-language warnings about what may make the month infeasible;
2. the re-plan line, if there is a cutoff;
3. `build` and `solve` timings and the status;
4. `cost=` with the solver's objective and bound;
5. `penalties:` for each soft rule;
6. recorded facts (section 6) and any avoidable shortfall (section 12);
7. the validator's verdict.

| Exit | Meaning |
|---|---|
| 0 | A roster that meets every hard rule. The result has `ok: true`. |
| 1 | No roster found: the hard rules conflict, or the time ran out. Check the `precheck:` lines and the result's `message`. |
| 2 | The roster breaks a hard rule. This should not happen; the result lists the violations. |
| 3 | The model and the independent validator disagree on the cost. This is a bug; keep the request. |
| 4 | Malformed request or arguments. The message names the field, e.g. `error: request: general.weights: missing field(s) ['change']`. |

**Status.** `OPTIMAL` means the roster is proven best for the weighted objective. `FEASIBLE` means the time limit ended
the search first. For the 25-nurse sample, a good roster arrives within seconds and better ones keep coming for a minute
or two.

---

## 4. Read the result

From the small request above (abridged):

```json
{
  "version": 1, "month": "2026-06", "ok": true, "status": "OPTIMAL", "message": "",
  "fingerprint": "…", "precheck": [],
  "roster": {"Amal": {"2026-06-01": "D", "2026-06-02": "", "…": "…"}, "…": {}},
  "violations": [], "elapsed_violations": [],
  "cost": 104, "penalties": {"nurse_shortfall": 10, "fair_day": 47, "fair_night": 47, "…": 0},
  "department_cost": 0,
  "solver": {"objective": 104.0, "best_bound": 104.0, "solve_seconds": 0.28, "build_seconds": 0.017,
             "deterministic_time": 0.74, "improvements": 0, "workers": 8, "reproducible": false,
             "time_limit_seconds": 10.0, "stopped_by": "proof", "deterministic": false,
             "model": {"variables": 740, "constraints": 1446, "objective_terms": 39}},
  "replan": null,
  "nurses": {
    "Amal": {"leave": [], "contract": 208, "leave_credit": 0, "required": 208,
             "hours_worked": 204, "overtime": 0, "shortfall": 4, "shifts": {"D": 4, "N": 13}, "cost": 50,
             "carry_over": {"hours_balance": 4, "night_balance": 2.8, "friday_balance": 0.5,
                            "ytd_overtime": 0, "previous": ["D", "", "…"],
                            "last_shift_kind": null, "rest_owed": 0, "preferences_not_kept": 0,
                            "overtime_balance": 0.0}}
  }
}
```

| Field | Meaning |
|---|---|
| `ok` | True exactly when the command exits 0. |
| `status`, `message` | CP-SAT's status (`OPTIMAL`, `FEASIBLE`, `INFEASIBLE`, `UNKNOWN`, `MODEL_INVALID`) and, when there is no usable roster, the reason. |
| `fingerprint` | Identifies the exact problem the roster was scored against. |
| `precheck` | The same warnings the console printed. |
| `roster` | Nurse → date → shift name, with `""` for off or on leave. It has the same shape as `published`, so it can be pasted into a re-plan. |
| `violations` | Hard rules the roster breaks: `rule`, `nurse` (null for a unit-level rule), `date` (null for a monthly total) and `text`. A rule of a shift-day (headcount, team leader, both genders, skill need) also states its `shift`, and a headcount its `present`, `minimum` and `maximum`; no other rule has these keys. |
| `elapsed_violations` | Hard rules broken by recorded facts before the cutoff. They are reported, not treated as errors. |
| `cost`, `penalties`, `department_cost` | The validated cost, its split per soft rule, and the part charged to the unit as a whole. |
| `solver.stopped_by` | What ended the search: `proof`, `deterministic_budget`, `time_limit`, `cancelled` or `model_invalid`. |
| `solver.deterministic` | True when the run is reproducible (section 8). |
| `replan` | `{"from", "recorded_cells_differ"}` for a re-plan, otherwise null. |
| `nurses[id]` | The nurse's hours (`contract`, `leave_credit`, `required`, `hours_worked`, `overtime`, `shortfall`), shift counts, share of the cost, and `carry_over`. |

With no roster at all, `roster`, `nurses` and `cost` are null and `ok` is false.

---

## 5. Month to month: the carry-over

Next month's request is this month's fixed nurse fields plus the `carry_over` block. For each nurse:

1. keep `id`, `gender`, `team_leader`, `score`, `ramadan_reduced`, `breastfeeding`, `allowed_shifts`, `skills`,
   `on_probation` and `preceptor`, updating them if anything changed;
2. copy every key of `carry_over` as it is: `hours_balance`, `night_balance`, `friday_balance`, `ytd_overtime`,
   `previous`, `last_shift_kind`, `rest_owed`, `preferences_not_kept` and `overtime_balance`;
3. enter next month's `leave`, `undesired_days`, `desired_days` and `pinned`.

Then set `month`, `ramadan_days` and `public_holidays` for the new month.

What the carried values mean:

* `hours_balance`: what the nurse still owes (+) or worked ahead (−). Normally this is less than one shift. After a
  month whose recorded days could not reach the requirement, it is the whole debt.
* `night_balance`, `friday_balance`, `overtime_balance`: the nurse's running fairness position. With `carry_fairness`
  on, these adjust next month's targets.
* `ytd_overtime`: the incoming value plus this month's overtime.
* `previous`: this month's row, so next month's rules can see across the month boundary.
* `last_shift_kind`, `rest_owed`: the kind of the last shift worked and the night rest still owed, worked out up to
  where `previous` begins. They keep the switch and rest rules correct for a nurse returning from a long absence.
* `preferences_not_kept`: this month's preferences that were not kept. Leave days are not counted.

At the start of a year, reset `ytd_overtime` and the three balances to 0. The solver never resets them itself.

---

## 6. Re-plan a month that has started

When something changes mid-month, re-plan from a date:

1. put the roster in force under `published` (a result's `roster` can be pasted in directly);
2. set `replan_from` to the first date to plan again;
3. record what really happened before that date, where it differs from the plan: sick days go in `leave`, and a
   completed swap or cover becomes a `pinned` cell;
4. keep `change` above zero so the new roster stays close to the published one.

Days before the cutoff become facts. Every cell in them is pinned, from your pins and leave, with the rest taken from
`published`. A hard rule those facts broke is reported under `elapsed_violations` rather than enforced. Rules that reach
into the free days still apply, such as rest after a night, the run limit and the hours.

A pinned cell before the cutoff may name a shift the nurse is no longer allowed. A pinned cell from the cutoff on may not.

---

## 7. Close a month

When the month is over, solve it once more with `replan_from` set to **the first day after the month** (`"2026-07-01"`
for June). Put the final roster under `published` and record every late change as pins and leave. Every day is then a
fact: the solver checks the record, reports what it broke, and works out the carry-over from what actually happened.
Build next month's request from that result.

---

## 8. Reproducible runs

`"reproducible": true` makes the roster depend on the request alone. The solver uses one worker and a fixed seed, and
`time_limit_seconds` becomes a budget on CP-SAT's own deterministic clock instead of the wall clock. The same request
then gives the same roster on any machine.

The cost is speed: with one worker, the deterministic clock runs several times slower than the wall clock. As a
safeguard, the wall clock stops a runaway search at ten times the budget. When the run is reproducible, `stopped_by` is
`proof` or `deterministic_budget` and `deterministic` is true.

With `reproducible: false`, `time_limit_seconds` is wall-clock time and several workers search in parallel, so two runs
may differ.

---

## 9. Skills

* `general.required_skills` lists the skills a nurse needs in order to work. If a nurse's recorded required skill
  expires during the month, they work no shift from the day after (hard rule `skill_expired`). Their hours stay owed,
  and what their remaining days cannot hold becomes debt. A nurse with no record of the skill at all is not blocked.
* `department.skill_needs` lists entries of the form `{"shift", "skill", "weekday_min", "weekend_min"}`. Each one
  requires at least that many nurses holding the skill on exactly that shift. `also_counts` does not count here, and
  there is at most one need per shift and skill. `skill_needs_hard` makes every need hard; otherwise each missing nurse
  costs `skill_shortfall` (rule `skill_need`).
* A nurse's `skills` maps each skill to its **expiry**, the last day it counts, or to `null` when it lasts the whole
  month. A skill that expired before the month is sent with its old date, because leaving a skill out means there is
  no record of it. A date after the month is refused; send `null` instead.

`service_tests/fixtures/four_nurses_skills.json` is an example. BLS is required, and Dana's BLS expires on 24 June, so
Dana works nothing from the 25th. One ICU nurse is needed on every night shift, as a soft rule costing 50.

---

## 10. Nurses on probation

A nurse with `on_probation: true`:

* counts toward no headcount, team leader, gender, score or skill need;
* works no night shift;
* works less than what they owe plus one of their shortest shifts;
* takes no part in day, night or weekend fairness or in the overtime share, and their three balances carry out as 0.

`preceptor` names the nurse they follow. That nurse must be another nurse in the request, may not be on probation, and
may precept only one nurse. On every free day the preceptor works a shift the nurse on probation may work, the nurse on
probation works the same shift or is off. Once they are off on such a day, they work no day the preceptor does not
(rule `following`). A nurse on probation with no preceptor is rostered freely, and precheck warns about it.

---

## 11. Renaming or retiring a shift

Histories (`previous`) name shifts, and a history can be older than the unit's current shift list.

* **Renamed shift** (`N` becomes `Night`): add `"renamed_shifts": {"N": "Night"}` for as long as any history still names
  `N`. `last_shift_kind` needs nothing, because it stores a kind, not a name.
* **Retired shift**: a history that still names it loads. Those days count as worked, for the run limits, but as
  neither night nor day work. Precheck says so. Pins, leave and `published` must use current shift names.

---

## 12. The hours rule

For each nurse:

* `contract` = `hours_per_day` for every working day (fewer on Ramadan days for a `ramadan_reduced` nurse, and minus
  `breastfeeding_reduction` per working day for a breastfeeding nurse). Nothing is owed on weekend days or public
  holidays;
* `leave_credit` = the same rate for every leave day;
* `required` = `contract` − `leave_credit` + incoming `hours_balance`, never below zero. Advance credit the month
  cannot absorb is carried on.

Hours worked above `required` are **overtime**. Overtime is charged per hour and capped per month and per year (hard),
and the unit's total is charged above `dept_overtime_cap`. Hours below `required` are **shortfall**, charged per hour
and carried over in `hours_balance`.

There is no hard floor on hours, because what a nurse can still work depends on every other hard rule at once. Instead,
shortfall beyond a remainder of less than one shift is **avoidable shortfall**, charged at `avoidable_shortfall`. Hours
the nurse's recorded and free days could never hold are exempt from that charge, for example when the month has ended,
the nurse is on leave for the rest of it, or a required skill has expired. Those hours simply become debt.

* A search stopped by its time limit may still leave avoidable shortfall. The console names the nurses, and a longer
  time limit usually removes it.
* If `avoidable_shortfall` is not well above the other weights, a preference can win over a shift. For example, an
  `undesired_day` worth more than a whole shift's shortfall leaves the nurse short.

---

## 13. Troubleshooting

| You see | Meaning | What to do |
|---|---|---|
| `error: request: … missing field(s) [...]` | A rule, weight or nurse field is missing. | Add it. Every field is required on purpose. |
| `error: request: … is outside 2026-06` | A date falls outside the month. | Fix it. Only `replan_from` may be the day after the month. |
| `error: request: … is not a department shift` | A pin, preference or `published` cell names an unknown shift. | Use a current shift name. Old names are allowed only in `previous`. |
| `the pattern rules look back N days … (62)` | The run, double-cycle and night-rest rules together look back too far. | Lower them. |
| exit 1, no roster satisfies the hard rules | The hard rules conflict. | Read the `precheck:` lines. Common causes are too few eligible nurses for a headcount, a run limit that cannot fit the shifts owed, or contradictory pins. |
| exit 1, nothing found within the time limit | The time ran out before any roster was found. | Raise `time_limit_seconds` or the worker count. |
| `avoidable shortfall: …` | A nurse ends a shift or more short although free days remained. | Give the search more time, and check that `avoidable_shortfall` is the largest weight. |
| `recorded before … (facts, not errors)` | The recorded days broke a hard rule. | Nothing to fix. It is part of the record. |
| exit 2 or 3 | A hard violation, or a disagreement between the model and the validator. | This is a solver bug. Keep the request file and report it. |
