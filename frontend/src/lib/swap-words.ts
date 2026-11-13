import type { MySwapRowState, SwapColleagueAnswer } from "@/api/generated/model"
import { shortDay } from "@/lib/utils"

/** A cell as a swap shows it: a shift's name with its code, or Off for a day not worked, which is all a colleague's leave
 *  ever reads as. */
export function cellWords(code: string, names: Record<string, string> = {}): string {
  if (!code) return "Off"
  return names[code] ? `${names[code]} (${code})` : code
}

export const SWAP_STATE: Record<MySwapRowState, string> = {
  pending: "Waiting for its colleagues",
  awaiting_manager: "Waiting for the manager",
  approved: "Approved",
  rejected: "Rejected",
  withdrawn: "Withdrawn",
  expired: "Expired",
}

const ANSWER: Record<SwapColleagueAnswer, string> = {
  pending: "not answered yet",
  accepted: "accepted",
  rejected: "rejected",
}

/** Each colleague's answer, in one sentence. One who never answered a swap that has ended did not, rather than not yet. */
export function answersWords(colleagues: { full_name: string; hospital_id: string; answer: SwapColleagueAnswer }[],
  state: MySwapRowState): string {
  const waiting = WAITING.includes(state)
  return `${colleagues.map((colleague) => `${colleague.full_name || colleague.hospital_id}: ${
    colleague.answer === "pending" && !waiting ? "did not answer" : ANSWER[colleague.answer]}`).join(". ")}.`
}

/** "7 Dec 2026", or several: what tells one swap from another on a page. */
export const swapDays = (cells: { date: string }[]) => [...new Set(cells.map((cell) => shortDay(cell.date)))].join(", ")

/** A swap still waiting on its colleagues or the manager. */
export const WAITING: MySwapRowState[] = ["pending", "awaiting_manager"]
