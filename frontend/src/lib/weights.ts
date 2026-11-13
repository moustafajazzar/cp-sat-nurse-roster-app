import type { Weights } from "@/api/generated/model"

export type Explained<Name extends string> = { name: Name; label: string; description: string }

/** What the API also says, beside the field: above it, the solver could lower the rule's cost by adding overtime. */
export const OVERTIME_SHARE_ABOVE_OVERTIME = "Cannot be above Nurse overtime, or the solver could lower this cost by adding overtime."

/** Every soft rule, by the name its weight and its penalty share. */
export const WEIGHTS: Explained<keyof Weights>[] = [
  { name: "avoidable_shortfall", label: "Avoidable hours short", description: "Each hour a nurse ends short beyond what is less than one shift and what their days could not give. Keep this far above every other weight, so that nothing leaves a nurse idle while a shift of theirs is free." },
  { name: "nurse_shortfall", label: "Hours short", description: "Each hour a nurse ends the month under what they owe. Those hours carry over to the next month." },
  { name: "nurse_overtime", label: "Nurse overtime", description: "Each hour of a nurse's overtime." },
  { name: "dept_overtime", label: "Unit overtime", description: "Each hour of a unit's overtime above the unit overtime cap." },
  { name: "night_rest", label: "Night rest", description: "Each day shift that follows a night shift with too little rest. Applies only in a unit that lets this rule be broken." },
  { name: "shift_switch", label: "Day and night switches", description: "Each switch between day and night shifts above the unit's maximum. Applies only in a unit that lets this rule be broken." },
  { name: "skill_shortfall", label: "Short of a skill need", description: "Each nurse short of a unit's skill need on a shift and day. Applies only in a unit that lets its skill needs be broken." },
  { name: "excess_weekend", label: "Too many weekend days", description: "Each weekend day a nurse works above the unit's maximum." },
  { name: "excess_full_run", label: "Full run above the most", description: "Each full run a nurse works in a month above the unit's most. A full run is as many shifts in a row as the unit's longest run allows." },
  { name: "fair_day", label: "Fair share of day shifts", description: "Each tenth of a shift that a nurse's day shifts stray from their fair share, beyond the unit's tolerance." },
  { name: "fair_night", label: "Fair share of night shifts", description: "Each tenth of a shift that a nurse's night shifts stray from their fair share, beyond the unit's tolerance." },
  { name: "fair_weekend", label: "Fair share of weekend days", description: "Each tenth of a shift that a nurse's weekend days stray from their fair share, beyond the unit's tolerance." },
  { name: "overtime_above_share", label: "Overtime away from the fair share", description: "Each hour of overtime a nurse works above their overtime share, beyond the unit's overtime tolerance. At most the weight of Nurse overtime, so that adding overtime never lowers this cost." },
  { name: "score_deficit", label: "Shift score below target", description: "Each hundredth of a score point, per person, that a shift's average score falls below the unit's target." },
  { name: "score_spread", label: "Shift scores far apart", description: "Each hundredth of a score point, per person, that a shift's average rises above the unit's ceiling; or, in a unit that compares shifts, each hundredth between the strongest and the weakest." },
  { name: "change", label: "Changes in a re-plan", description: "Each cell that differs from the roster in force when a month is re-planned." },
  { name: "undesired_day", label: "Working a day asked off", description: "Each day a nurse preferred to have off but works; twice that for a nurse whose preference last month was not kept." },
  { name: "desired_day", label: "Off on a day asked to work", description: "Each day a nurse preferred to work but is off; twice that for a nurse whose preference last month was not kept." },
]
