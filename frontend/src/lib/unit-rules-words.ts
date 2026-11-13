/** The unit rules page's words for each rule, which the comparison of two results reads its fields by too. */
import type { Explained } from "@/lib/weights"

export type NumberRule = "score_target" | "score_target_weekend" | "score_band_hi" | "score_band_hi_weekend" | "night_rest_days" | "fair_tolerance" | "carry_cap" | "overtime_tolerance" | "overtime_carry_cap" | "max_consecutive_shifts" | "max_full_runs" | "double_cycle_rest" | "max_weekends" | "max_switches"
export type SwitchRule = "headcount_enabled" | "require_team_leader" | "require_both_genders" | "score_balance" | "night_rest_hard" | "fair_weekends" | "share_overtime" | "carry_fairness" | "no_mixed_blocks" | "switches_hard" | "skill_needs_hard"

export const SHIFTS_DESCRIPTION = "Chosen from the hospital's shift catalogue. A nurse in this unit can only be given these."
export const NIGHT_AND_DAY = [
  { name: "night_shift", label: "Night shift", description: "The shift the rules on night rest, unmixed runs and switches treat as night." },
  { name: "day_shift", label: "Day shift", description: "The shift the same rules treat as day. Any other shift this unit works counts as neither." },
] as const

export const HEADCOUNT_SWITCH: Explained<SwitchRule> = {
  name: "headcount_enabled",
  label: "Hold every shift to its headcount",
  description: "When off, the numbers below stay here but the solver ignores them.",
}
export const HEADCOUNTS_DESCRIPTION = "How many nurses each shift needs on a weekday and on a weekend day. The solver returns no roster that breaks these."
export const WHO_SWITCHES: Explained<SwitchRule>[] = [
  { name: "require_team_leader", label: "A team leader on every shift", description: "Every shift with a headcount needs at least one nurse marked team leader. The solver returns no roster that breaks this." },
  { name: "require_both_genders", label: "Both genders on every shift", description: "Every shift with a headcount needs at least one female and one male nurse. The solver returns no roster that breaks this." },
]
export const SKILL_NEEDS_DESCRIPTION = "How many nurses holding a skill a shift needs on a weekday and on a weekend day. Only nurses on exactly that shift count, and a skill counts until its expiry."
export const SKILL_NEEDS_SWITCH: Explained<SwitchRule> = {
  name: "skill_needs_hard",
  label: "Never break the skill needs",
  description: "When on, the solver returns no roster that breaks them. When off, a roster may, at the price of the hospital's weight for each nurse short.",
}
/** One need a shift and skill: what identifies a need. A skill's name may hold spaces, so the two are not joined by one. */
export const needKey = ({ shift, skill }: { shift: string; skill: string }) => JSON.stringify([shift, skill])
export const NEED_COUNTS = [
  { name: "weekday_min", label: "Weekday, at least" },
  { name: "weekend_min", label: "Weekend day, at least" },
] as const

export const SCORE_TARGETS: Explained<NumberRule>[] = [
  { name: "score_target", label: "Score target on a weekday", description: "What the average score of each shift with a headcount should reach, from 0 to 1. A roster may fall short, at the price of the hospital's weight." },
  { name: "score_target_weekend", label: "Score target on a weekend day", description: "The same on a weekend day." },
]
export const SCORE_SWITCH: Explained<SwitchRule> = {
  name: "score_balance",
  label: "Keep the strength of shifts balanced",
  description: "When off, a shift can be as far above its target as the roster makes it, at no price.",
}
export const SCORE_MODES = { band: "Keep each shift under a ceiling (faster to solve)", spread: "Keep the strongest and weakest shifts close (slower to solve)" } as const
export const SCORE_MODE_DESCRIPTION = "How strength is balanced. Under a ceiling, the two ceilings below apply and the targets cannot be above them."
export const SCORE_CEILINGS: Explained<NumberRule>[] = [
  { name: "score_band_hi", label: "Score ceiling on a weekday", description: "The average score above which a shift starts to cost, when balancing under a ceiling." },
  { name: "score_band_hi_weekend", label: "Score ceiling on a weekend day", description: "The same on a weekend day." },
]
export const NIGHT_REST_DAYS: Explained<NumberRule> = {
  name: "night_rest_days",
  label: "Days off after a night shift",
  description: "How many days off a nurse is owed between a night shift and the next day shift. 0 switches this off.",
}
export const NIGHT_REST_SWITCH: Explained<SwitchRule> = {
  name: "night_rest_hard",
  label: "Never break the night rest",
  description: "When on, the solver returns no roster that breaks it. When off, a roster may, at the price of the hospital's weight.",
}
export const LONGEST_RUN: Explained<NumberRule> = {
  name: "max_consecutive_shifts",
  label: "Longest run of shifts",
  description: "The most days in a row a nurse may work. 0 means no limit. The solver returns no roster that breaks this.",
}
/** Shown only while the longest run is above 1: at 0 a run has no limit and at 1 every run would be full. */
export const FULL_RUNS: Explained<NumberRule> = {
  name: "max_full_runs",
  label: "Most full runs in a month",
  description: "A full run is as many shifts in a row as the longest run allows. Each one beyond this costs the solver, which then breaks the work into shorter runs.",
}
export const AFTER_TWO_RUNS: Explained<NumberRule> = {
  name: "double_cycle_rest",
  label: "Days off after two runs",
  description: "After a run, one day off and another run, how many days off a nurse is then owed. The solver returns no roster that breaks this.",
}
/** Not while the longest run is blank, so that the field does not vanish while a new number is typed. */
export const fullRunsApply = (longest: number) => Number.isNaN(longest) || longest > 1
export const MIXED_SWITCH: Explained<SwitchRule> = {
  name: "no_mixed_blocks",
  label: "Keep runs unmixed",
  description: "When on, the days of a run are all day shifts or all night shifts. The solver returns no roster that breaks this.",
}
export const FAIR_SHIFTS_DESCRIPTION = "Whose counts are spread fairly among the nurses who may work them. Only a shift with a headcount can be chosen."
export const FAIR_TOLERANCE: Explained<NumberRule> = {
  name: "fair_tolerance",
  label: "Free difference from a fair share (shifts)",
  description: "How many shifts a nurse may stray from their fair share before it starts to cost.",
}
export const WEEKEND_SWITCH: Explained<SwitchRule> = {
  name: "fair_weekends",
  label: "Spread weekend days fairly",
  description: "Weekend days worked are spread evenly among the unit's nurses.",
}
export const OVERTIME_SWITCH: Explained<SwitchRule> = {
  name: "share_overtime",
  label: "Share overtime evenly",
  description: "Overtime is spread among the unit's nurses in proportion to the hours each owes.",
}
export const OVERTIME_TOLERANCE: Explained<NumberRule> = {
  name: "overtime_tolerance",
  label: "Overtime tolerance (hours)",
  description: "How many hours of overtime a nurse may work above their fair share before it starts to cost.",
}
export const CARRY_SWITCH: Explained<SwitchRule> = {
  name: "carry_fairness",
  label: "Carry fairness between months",
  description: "Nights, weekend days and overtime above or below a nurse's fair share so far move this month's fair share.",
}
export const CARRY_CAP: Explained<NumberRule> = {
  name: "carry_cap",
  label: "Most a month's share can move (shifts)",
  description: "The limit on how far what is carried can move a fair share in one month.",
}
export const OVERTIME_CARRY_CAP: Explained<NumberRule> = {
  name: "overtime_carry_cap",
  label: "Most a month's overtime share can move (hours)",
  description: "The limit on how far what is carried can move a nurse's overtime share in one month.",
}
export const LIMITS: Explained<NumberRule>[] = [
  { name: "max_weekends", label: "Weekend days a month", description: "How many weekend days a nurse may work in a month before each further one costs the hospital's weight." },
  { name: "max_switches", label: "Switches between day and night a month", description: "How many times a nurse may switch between day and night shifts in a month. The change from last month counts." },
]
export const SWITCHES_HARD: Explained<SwitchRule> = {
  name: "switches_hard",
  label: "Never go over the switch limit",
  description: "When on, the solver returns no roster that breaks it. When off, a roster may, at the price of the hospital's weight.",
}
export const REQUEST_CAP_DESCRIPTION = "How many requests each nurse may make for a month. 0 means none."
export const PREFERENCE_CAP_DESCRIPTION = "How many preferences each nurse may make for a month, days she would rather have off or work, which the solver keeps when it can. 0 switches them off. A request window opens while either cap is above 0."
export const TIME_LIMIT_DESCRIPTION = "How long a solve searches unless you choose otherwise when you start it."

export const COUNT_FIELDS = [
  { name: "weekday_min", label: "Weekday, at least" },
  { name: "weekday_max", label: "Weekday, at most" },
  { name: "weekend_min", label: "Weekend day, at least" },
  { name: "weekend_max", label: "Weekend day, at most" },
] as const

/** What a field of the unit's rules is called on its page: a section or legend where it has no label of its own. */
export const UNIT_RULE_LABELS: Record<string, string> = Object.fromEntries([
  ["shifts", "Shifts this unit works"],
  ...NIGHT_AND_DAY.map(({ name, label }) => [name, label]),
  ...[HEADCOUNT_SWITCH, ...WHO_SWITCHES, SKILL_NEEDS_SWITCH, ...SCORE_TARGETS, SCORE_SWITCH, ...SCORE_CEILINGS, NIGHT_REST_DAYS,
    NIGHT_REST_SWITCH, LONGEST_RUN, FULL_RUNS, AFTER_TWO_RUNS, MIXED_SWITCH, FAIR_TOLERANCE, WEEKEND_SWITCH, OVERTIME_SWITCH,
    OVERTIME_TOLERANCE, CARRY_SWITCH, CARRY_CAP, OVERTIME_CARRY_CAP, ...LIMITS,
    SWITCHES_HARD].map(({ name, label }) => [name, label]),
  ["headcounts", "Headcounts"],
  ["skill_needs", "Skill needs"],
  ["score_balance_mode", "Way of balancing"],
  ["fair_shifts", "Shifts to spread fairly"],
])
