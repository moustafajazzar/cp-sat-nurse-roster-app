import type { CouldTakeRow, CoverOfferRow, MyCoverRow, UnitCoverRowState } from "@/api/generated/model"
import { shortDay } from "@/lib/utils"

/** How a cover's state reads on the manager's page. */
export const COVER_STATE: Record<UnitCoverRowState, string> = {
  open: "Not offered yet",
  offered: "Offered",
  taken: "Taken",
  withdrawn: "Withdrawn",
  filled: "Filled by a new version",
  expired: "Expired",
}

/** A cover still waiting for a nurse. */
export const WAITING: UnitCoverRowState[] = ["open", "offered"]

/** "Day (D) on 9 Dec 2026": what tells one cover from another on a page. */
export function coverName(cover: { date: string; shift: string }, names: Record<string, string>): string {
  const shift = names[cover.shift] ? `${names[cover.shift]} (${cover.shift})` : cover.shift
  return `${shift} on ${shortDay(cover.date)}`
}

const hours = (count: number) => `${count} ${count === 1 ? "hour" : "hours"}`

/** A nurse's hours this month against those owed, which the list is ranked by first. */
export function hoursWords(nurse: CouldTakeRow): string {
  if (nurse.owed === null) return `${hours(nurse.worked)} worked; the hospital counts no hours owed`
  const by = nurse.worked - nurse.owed
  const where = by < 0 ? `${hours(-by)} below` : by > 0 ? `${hours(by)} over` : "exactly"
  return `${nurse.worked} of ${hours(nurse.owed)} owed, ${where}`
}

export const overtimeWords = (nurse: CouldTakeRow) => hours(nurse.overtime_this_year)

export const REPLACES_PIN: Record<NonNullable<CouldTakeRow["replaces_pin"]>, string> = {
  manager: "Your pin to a day off that day would be replaced.",
  swap: "An approved swap's pin to a day off that day would be replaced.",
}

/** Where a cover offered to a nurse stands, as she reads it: never who took it, why it was short or why it ended. */
export function myCoverWords(cover: MyCoverRow): string {
  if (cover.my_answer === "accepted") return "You took it"
  if (cover.my_answer === "declined") return "You declined it"
  switch (cover.state) {
    case "taken": return "Taken by another nurse"
    case "withdrawn": return "Withdrawn"
    case "filled": return "No longer needed"
    case "expired": return "Expired"
    default: return "Waiting for your answer"
  }
}

/** Offered to her and not answered yet, while it still waits for a nurse. */
export const waitsForMe = (cover: MyCoverRow) => cover.my_answer === "pending" && WAITING.includes(cover.state)

export const OFFER_ANSWER: Record<CoverOfferRow["answer"], string> = {
  pending: "Not answered yet",
  accepted: "Accepted",
  declined: "Declined",
}
