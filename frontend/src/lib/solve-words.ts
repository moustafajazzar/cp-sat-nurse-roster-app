import type { UnitSolveRow } from "@/api/generated/model"

import { longDay, shortDay } from "@/lib/utils"
import { WEIGHTS } from "@/lib/weights"

/** What the unit's history and the System Admin's both say of a solve. The System Admin's row has nothing of a re-plan. */
export type SolveRow = Omit<UnitSolveRow, "in_draft" | "replan_from" | "replan_outcome">

/** What a re-plan's end did to the roster in force, in a few words, once it kept a roster. */
const REPLAN_ENDED: Record<NonNullable<UnitSolveRow["replan_outcome"]>, string> = {
  version: "made a new version",
  unchanged: "nothing changed",
  outdated: "not applied, the roster changed meanwhile",
  month_over: "not applied, the month had ended",
}

/** A re-plan's end, named as one, for the manager's history. */
export function replanStatus(row: Pick<UnitSolveRow, "state" | "failure" | "found_roster" | "replan_from" | "replan_outcome">): string {
  const ended = row.replan_outcome ? REPLAN_ENDED[row.replan_outcome] : solveStatus(row).toLowerCase()
  const stopped = row.state === "cancelled" && row.replan_outcome === "version" ? "stopped, " : ""
  return `Re-plan from ${longDay(row.replan_from!)}: ${stopped}${ended}`
}

/** A solve's end, in the manager's words. "Refused" is a matter of the rules and never reads as a failed solve. */
export function solveStatus({ state, failure, found_roster }: Pick<SolveRow, "state" | "failure" | "found_roster">): string {
  if (state === "queued") return "In the queue"
  if (state === "running") return "Solving"
  if (state === "failed") return failure === "refused" ? "Rules to review" : "Failed"
  if (state === "cancelled") return found_roster ? "Stopped, roster kept" : "Stopped, no roster"
  return found_roster ? "Finished" : "No roster to keep"
}

/** Why a solve failed, in fixed words naming no one: what the System Admin reads in place of the solver's or the worker's
 * own, which can name a nurse and a date. The labels of `SolveFailure` in backend/solves/models.py: change both together. */
const FAILED_BECAUSE: Record<NonNullable<SolveRow["failure"]>, string> = {
  refused: "The solver refused the rules it was given: the unit's or the hospital's rules need review.",
  unreachable: "The solver could not be reached.",
  lost: "The solver restarted and lost the solve.",
  overrun: "The solver ran well past the time limit and was given up on.",
  solver_error: "The solver stopped with an error of its own.",
  stuck: "The solver would not stop an earlier job, so this one could not start.",
  worker_lost: "The worker stopped during the solve and could not take it up again.",
  unexpected: "The worker met an error of its own.",
  solver: "The solver failed or could not be reached (from before the kinds were told apart).",
}
export const failedBecause = (failure: SolveRow["failure"]) => (failure ? FAILED_BECAUSE[failure] : "")

/** How long it searched. */
export function solveDuration({ attempts, elapsed_seconds }: Pick<SolveRow, "attempts" | "elapsed_seconds">): string {
  if (attempts === 0) return "Did not start"
  const seconds = Math.round(elapsed_seconds)
  return seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)} min ${seconds % 60} s`
}

const STOPPED_BY: Record<string, string> = {
  proof: "It proved no better roster exists",
  time_limit: "The time limit",
  cancelled: "It was stopped by hand",
}
/** What ended a search. An unknown word from a later solver is still readable. */
export const stoppedBy = (word: string | null) => (word ? STOPPED_BY[word] ?? word : "Unknown")

/** A soft rule's name is its weight's, but for the skill need, whose weight is the price of a nurse short. */
const RULE_LABELS = new Map<string, string>([...WEIGHTS.map((weight) => [weight.name as string, weight.label] as const),
  ["skill_need", WEIGHTS.find((weight) => weight.name === "skill_shortfall")!.label]])
/** A soft rule as the hospital rules page names its weight. A rule from a later solver is still readable. */
export const ruleName = (rule: string) => RULE_LABELS.get(rule) ?? rule

/** A difference with its sign, a true minus for a fall. */
export const signed = (delta: number) => (delta > 0 ? `+${delta}` : delta < 0 ? `−${-delta}` : "0")

/** The solver's own words on a line about a nurse. Those about her begin with her id, which the name before them already says,
 * so that is left out: "Amal Saleh: 20 h overtime", not "Amal Saleh: Amal: 20 h
 * overtime". Only her own id is, so words that begin with another's stay whole. */
export function ownWords(row: { hospital_id?: string | null; text: string }): string {
  const own = row.hospital_id ? `${row.hospital_id}: ` : null
  return own && row.text.startsWith(own) ? row.text.slice(own.length) : row.text
}

/** A line of the solver's about a nurse, or the unit when it names none: who, and when if its words do not say (most hold the
 * date already), then its own words (`ownWords`). */
export function solverLine(row: { full_name: string; hospital_id?: string | null; date?: string | null; text: string }): string {
  return `${row.full_name || row.hospital_id || "The unit"}${row.date && !row.text.includes(row.date) ? `, ${shortDay(row.date)}` : ""}: ${ownWords(row)}`
}
