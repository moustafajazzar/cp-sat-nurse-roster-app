import { useGetSolveResult } from "@/api/generated/endpoints"
import type { Draft, DraftChange, DraftPin, GridCheck, RosterMonth } from "@/api/generated/model"
import { SolveResultPanels } from "@/components/solve-result"
import { ownWords } from "@/lib/solve-words"
import { hospitalTime, longDay, monthName } from "@/lib/utils"

/** What the roster pages say beside a grid: what changed since its solve, what it breaks, and the pins leave ended or that
 *  count for nothing. The manager's page and the approvers' read the same words. */

export const plural = (count: number, one: string, many = `${one}s`) => `${count} ${count === 1 ? one : many}`

type MonthDates = "ramadan_days" | "public_holidays"

/** The month's own changes, which come with no hospital ID. */
const MONTH_DATES_SAY: Record<MonthDates, string> = { ramadan_days: "Ramadan days", public_holidays: "public holidays" }

export const CHANGE_SAYS: Record<Exclude<DraftChange["what"], MonthDates>, string> = {
  gender: "gender was corrected",
  leave: "leave or dates changed",
  joined: "is now in this month but not in this draft",
  left: "is no longer in this month but still in this draft",
  pins: "pinned cells changed",
  preferences: "preferences changed",
  skills: "skills changed",
  closed: "the month before has closed since, so this month starts from its closed carry-over, not the projected one",
  on_probation: "is on probation this month, but it was planned with her counted",
  off_probation: "is no longer on probation this month, but it was planned with her on probation",
  preceptor: "has a different preceptor this month from the one it was planned with",
}

/** One line of a draft's changes since its solve, under "This draft was solved before these changes". A change with no
 *  hospital ID is the unit's own (`skills`) or the month's (its Ramadan days or public holidays), which the
 *  manager's page words in full with `since` ("since this draft was solved", the spec's sentence); the approvers' page adds
 *  its own ", since it was submitted" instead. */
export function changeSays(change: DraftChange, month: string, since = ""): string {
  const what = change.what
  if (what === "ramadan_days" || what === "public_holidays") {
    return `The ${MONTH_DATES_SAY[what]} of ${monthName(month)} changed${since ? ` ${since}` : ""}`
  }
  if (!change.hospital_id) return "The skill needs, the skills required to work or the skill shortfall weight changed"
  return `${change.full_name || change.hospital_id}: ${CHANGE_SAYS[what]}`
}

const shiftWord = (code: string) => (code ? code : "a day off")

const SET_ASIDE: Record<NonNullable<DraftPin["set_aside"]>, string> = {
  not_in_month: "no longer in this unit's month",
  outside_dates: "outside their start and end dates",
  leave: "on leave that day",
  shift_not_worked: "a shift this unit no longer works",
  shift_not_allowed: "not one of their allowed shifts",
  barred: "a day after a skill required to work expired, so they can work no shift",
  night_on_probation: "the night shift, which a nurse on probation does not work",
}

export function Violations({ check, what }: { check: GridCheck; what: string }) {
  if (check.reason) return <p className="text-sm">The solver's code could not check {what}, so whether it breaks a hard rule is not known. It says: {check.reason}</p>
  if (check.ok) return <p className="text-sm">{what[0].toUpperCase() + what.slice(1)} breaks no hard rule.</p>
  return (
    <div className="flex flex-col gap-1 text-sm">
      <p className="font-medium">{what[0].toUpperCase() + what.slice(1)} breaks {plural(check.violations.length, "hard rule")}:</p>
      <ul className="list-disc ps-5">
        {check.violations.map((violation, index) => (
          <li key={index}>{[violation.full_name, violation.date && longDay(violation.date)].filter(Boolean).join(", ")}{violation.full_name || violation.date ? ": " : ""}{violation.full_name ? ownWords(violation) : violation.text}</li>
        ))}
      </ul>
    </div>
  )
}

export function PinNotes({ draft }: { draft: Draft }) {
  const aside = draft.pins.filter((pin) => pin.set_aside)
  return (
    <>
      {draft.pins_lost_to_leave.length > 0 && (
        <div className="flex flex-col gap-1 rounded-md border p-3 text-sm">
          <p className="font-medium">Leave recorded since this solve replaced these pins, as leave wins over a pin:</p>
          <ul className="list-disc ps-5">
            {draft.pins_lost_to_leave.map((pin) => (
              <li key={pin.hospital_id + pin.date}>{pin.full_name || pin.hospital_id}, {longDay(pin.date)}: {shiftWord(pin.shift)}, by the leave {pin.by} recorded</li>
            ))}
          </ul>
          <p>A cell still holding a shift on a day of leave can be taken off by hand, or solve again.</p>
        </div>
      )}
      {aside.length > 0 && (
        <div className="flex flex-col gap-1 rounded-md border p-3 text-sm">
          <p className="font-medium">These pins count for nothing at present, so no solve is given them. They come back if that changes, or you can clear them:</p>
          <ul className="list-disc ps-5">
            {aside.map((pin) => <li key={pin.hospital_id + pin.date}>{pin.full_name || pin.hospital_id}, {longDay(pin.date)}: {shiftWord(pin.shift)}, {SET_ASIDE[pin.set_aside!]}</li>)}
          </ul>
        </div>
      )}
    </>
  )
}


/** Where a roster stands in its approval, and the last rejection's comment, which the manager keeps seeing until the roster is
 *  approved. Focus comes here after a submit or a decision, as the button pressed is gone. */
export function RosterStatus({ answer, forApprover = false }: { answer: RosterMonth; forApprover?: boolean }) {
  const { submitted, approved, last_rejection: rejected, state } = answer
  const says = submitted ? `Submitted ${hospitalTime(submitted.submitted_at)} by ${submitted.submitted_by}, waiting for a decision by the CNO or the Deputy CNO.`
    : approved ? `Approved ${hospitalTime(approved.approved_at)} by ${approved.approved_by}. Version ${approved.version} is in force, and the unit's nurses see it.`
    : null
  return (
    <div className="flex flex-col gap-2">
      {says && <p id="roster-status" tabIndex={-1} className="font-medium outline-none">{says}{submitted && !forApprover ? " Nothing can change it until they decide." : ""}</p>}
      {rejected && state !== "approved" && (
        <figure className="flex flex-col gap-1 rounded-md border border-destructive/40 p-3 text-sm">
          <figcaption className="font-medium">
            {state === "draft" ? "Rejected" : "Rejected before this submit"} {hospitalTime(rejected.rejected_at)} by {rejected.rejected_by}, who said:
          </figcaption>
          <blockquote className="whitespace-pre-wrap wrap-anywhere">{rejected.comment}</blockquote>
        </figure>
      )}
    </div>
  )
}

/** What a roster is called on its pages, by its state. */
export const ROSTER_HEADING: Record<RosterMonth["state"], string> = { none: "Draft", draft: "Draft", submitted: "Submitted roster", approved: "Approved roster" }

/** Its solve's panels beside a grid, which stays the roster if they cannot be read. */
export function ResultDetails({ unitId, solveId }: { unitId: string; solveId: string }) {
  const result = useGetSolveResult(unitId, solveId)
  if (result.isPending) return <p className="text-sm">Loading the details of this result…</p>
  if (!result.data) return <p role="alert" className="text-sm">The details of this result could not be read. The grid above is still the roster. Reload the page to try again.</p>
  return <SolveResultPanels result={result.data} />
}
