/** The hospital rules page's words for each rule, which the comparison of two results reads its fields by too. */
import { WEIGHTS, type Explained } from "@/lib/weights"

export type NumberRule = "hours_per_day" | "ramadan_hours_per_day" | "breastfeeding_reduction" | "overtime_month_cap" | "overtime_year_cap" | "dept_overtime_cap"
export type SwitchRule = "hours_rule_enabled" | "overtime_caps_enabled"

export const DAILY_HOURS: Explained<NumberRule>[] = [
  {
    name: "hours_per_day",
    label: "Hours per day",
    description: "The regular hours a nurse owes for each working day: a day that is neither a weekend day nor a public holiday.",
  },
  {
    name: "ramadan_hours_per_day",
    label: "Ramadan hours per day",
    description: "The same on Ramadan days, for a nurse marked Ramadan-reduced. Cannot be more than the hours per day.",
  },
  {
    name: "breastfeeding_reduction",
    label: "Breastfeeding reduction (hours)",
    description: "Taken off each working day for a nurse marked breastfeeding. Cannot be more than the Ramadan hours per day.",
  },
]

export const SWITCHES: Explained<SwitchRule>[] = [
  {
    name: "hours_rule_enabled",
    label: "Count hours",
    description: "When off, a nurse may work any number of shifts and nobody's overtime or hours short are counted, so the caps below do nothing.",
  },
  {
    name: "overtime_caps_enabled",
    label: "Cap each nurse's overtime",
    description: "When off, the monthly and yearly caps below do not apply.",
  },
]

export const CAPS: Explained<NumberRule>[] = [
  {
    name: "overtime_month_cap",
    label: "Monthly overtime cap (hours)",
    description: "The most overtime a nurse may work in a month. The solver returns no roster that breaks this.",
  },
  {
    name: "overtime_year_cap",
    label: "Yearly overtime cap (hours)",
    description: "The most overtime a nurse may work in a year, counting what is already worked. The solver returns no roster that breaks this.",
  },
  {
    name: "dept_overtime_cap",
    label: "Unit overtime cap (hours)",
    description: "The overtime a unit may use in a month before each further hour costs its weight. A roster may go over this one.",
  },
]

export const WEEKEND_DESCRIPTION = "The days that count as the weekend. A nurse owes no regular hours on them, and each unit's weekend rules apply to them."
export const SUCCESSIONS_TITLE = "Forbidden successions"
export const SUCCESSIONS_DESCRIPTION = "Shifts a nurse may not work on two days in a row, in that order. The solver returns no roster that breaks these."
export const SUCCESSIONS_SWITCH_LABEL = "Forbid these successions"
export const SUCCESSIONS_SWITCH = "When off, the pairs below stay here but the solver ignores them."
export const WEIGHTS_DESCRIPTION = "A weight is the price of breaking a soft rule once. The solver looks for the roster with the lowest total, so only how the weights compare to each other matters, and 0 switches the rule off. Each one below says what it is the price of."


// The solver's numbering, 0 is Monday, shown in the order the hospital's week runs.
export const WEEKDAYS = [
  [6, "Sunday"], [0, "Monday"], [1, "Tuesday"], [2, "Wednesday"], [3, "Thursday"], [4, "Friday"], [5, "Saturday"],
] as const

/** What a field of the hospital rules is called on its page, each weight by the rule it prices. The two the catalogues give
 *  a solve input have no field there, so they are named for where they come from. */
export const HOSPITAL_RULE_LABELS: Record<string, string> = Object.fromEntries([
  ...[...DAILY_HOURS, ...SWITCHES, ...CAPS].map(({ name, label }) => [name, label]),
  ["weekend_weekdays", "Weekend days"],
  ["forbidden_successions", SUCCESSIONS_TITLE],
  ["forbidden_successions_enabled", SUCCESSIONS_SWITCH_LABEL],
  ...WEIGHTS.map(({ name, label }) => [`weights.${name}`, `Weight: ${label}`]),
  ["shift_hours", "Hours of each shift, from the shift catalogue"],
  ["required_skills", "Skills required to work, from the skill catalogue"],
])
