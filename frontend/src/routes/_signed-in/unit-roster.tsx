import { zodResolver } from "@hookform/resolvers/zod"
import { keepPreviousData, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, Link, useNavigate, useRouteContext } from "@tanstack/react-router"
import { useEffect, useRef, useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import {
  getGetRosterMonthQueryKey,
  getGetRosterOverviewQueryKey,
  getListSubmittedRostersQueryKey,
  useApproveRoster,
  useGetRosterMonth,
  useRejectRoster,
} from "@/api/generated/endpoints"
import { Role, type Draft, type RosterMonth } from "@/api/generated/model"
import { ConfirmDialog } from "@/components/confirm-dialog"
import { dateInAddress, MONTH_LINK, monthInAddress, MonthNav } from "@/components/month-nav"
import { GridLegend, RosterGrid } from "@/components/roster-grid"
import { changeSays, PinNotes, ResultDetails, ROSTER_HEADING, RosterStatus, Violations } from "@/components/roster-notes"
import { RosterVersions } from "@/components/roster-versions"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field"
import { Textarea } from "@/components/ui/textarea"
import { UnitPicker, useChosenUnit } from "@/components/unit-picker"
import { plainRefusal, showServerErrors } from "@/lib/server-errors"
import { monthName } from "@/lib/utils"
import { onlyFor } from "@/session"

const searchSchema = z.object({ unit: z.string().optional().catch(undefined), month: monthInAddress, date: dateInAddress })

export const Route = createFileRoute("/_signed-in/unit-roster")({
  validateSearch: (search) => searchSchema.parse(search),
  beforeLoad: onlyFor(Role.cno, Role.deputy_cno),
  staticData: { title: "Unit roster" },
  component: UnitRosterPage,
})

const LINK = "underline underline-offset-3"

/** What changed since its solve, with what came after the submit marked: the manager was warned of the rest. */
function Changes({ answer, draft }: { answer: RosterMonth; draft: Draft }) {
  if (draft.changes.length === 0) return null
  const later = new Set((answer.submitted?.new_since ?? []).map((change) => change.hospital_id + change.what))
  return (
    <div className="flex flex-col gap-1 rounded-md border border-destructive/40 p-3 text-sm">
      <p className="font-medium">This roster was solved before these changes. It is checked again on the month as it stands, below.</p>
      <ul className="list-disc ps-5">
        {draft.changes.map((change) => (
          <li key={change.hospital_id + change.what}>
            {changeSays(change, answer.month)}
            {later.has(change.hospital_id + change.what) && <strong className="font-medium">, since it was submitted</strong>}
          </li>
        ))}
      </ul>
      {later.size === 0 && answer.state === "submitted" && <p>Its manager saw these when submitting.</p>}
    </div>
  )
}

function Decision({ unitId, answer, onDecided }: { unitId: string; answer: RosterMonth; onDecided: (answer: RosterMonth, said: string) => void }) {
  const approve = useApproveRoster()
  const reject = useRejectRoster()
  const [asking, setAsking] = useState(false)
  const focusAfter = useRef<string | null>(null)
  const schema = z.object({ comment: z.string().trim().min(1, "Say why, for the unit's manager.").max(2000, "At most 2000 characters.") })
  const form = useForm<z.infer<typeof schema>>({ resolver: zodResolver(schema), defaultValues: { comment: "" } })
  const month = monthName(answer.month)
  const confirm = () => approve.mutate({ unitId, data: { month: answer.month } }, {
    onSuccess: (decided) => {
      focusAfter.current = "roster-status"
      setAsking(false)
      onDecided(decided, `Approved. The roster of ${month} is in force, and the unit's nurses see it.`)
    },
  })
  const send = form.handleSubmit(({ comment }) => reject.mutate({ unitId, data: { month: answer.month, comment } }, {
    onSuccess: (decided) => onDecided(decided, `Rejected. The roster of ${month} is a draft again, and its manager sees your comment.`),
    onError: (error) => showServerErrors(form, error, (loc) => String(loc.at(-1))),
  }))
  const rejectFailed = reject.isError && !form.formState.errors.comment && (plainRefusal(reject.error) ?? "The roster could not be rejected. Check your connection and try again.")
  const error = form.formState.errors.comment
  return (
    <section aria-labelledby="decision" className="flex flex-col gap-4 rounded-md border p-3">
      <h3 id="decision" className="font-medium">Your decision</h3>
      <div className="flex flex-col items-start gap-2">
        <p className="text-sm text-muted-foreground">Approving makes it the roster in force: the unit's nurses see it, with names and shifts only.</p>
        {answer.approve_refusal && <p id="approve-refusal" className="text-sm">{answer.approve_refusal}</p>}
        <Button onClick={() => setAsking(true)} disabled={!!answer.approve_refusal} aria-describedby={answer.approve_refusal ? "approve-refusal" : undefined}>Approve</Button>
      </div>
      <form onSubmit={send} noValidate className="flex flex-col items-start gap-2">
        <Field data-invalid={!!error || undefined}>
          <FieldLabel htmlFor="reject-comment">Why reject it</FieldLabel>
          <Textarea id="reject-comment" rows={3} aria-invalid={!!error || undefined}
            aria-describedby={["reject-comment-description", error && "reject-comment-error"].filter(Boolean).join(" ")} {...form.register("comment")} />
          <FieldDescription id="reject-comment-description">The manager sees this with the draft. It is not in the audit log.</FieldDescription>
          <FieldError id="reject-comment-error" role={undefined} errors={error ? [error] : undefined} />
        </Field>
        {rejectFailed && <p role="alert" className="text-sm text-destructive">{rejectFailed}</p>}
        <Button type="submit" variant="outline" disabled={reject.isPending || !!answer.reject_refusal}>{reject.isPending ? "Rejecting…" : "Reject with this comment"}</Button>
      </form>
      <ConfirmDialog tone="default"
        question={asking ? { title: `Approve the roster of ${month}?`, body: "It becomes the roster in force and the unit's nurses see it. It cannot be sent back afterwards.", confirm: "Approve" } : null}
        pending={approve.isPending} failed={approve.isError} failure={plainRefusal(approve.error)} focusAfter={focusAfter}
        onConfirm={confirm} onCancel={() => setAsking(false)} />
    </section>
  )
}

function RosterView({ unit, unitInAddress, month: inAddress, date }: { unit: { id: string; name: string }; unitInAddress: string | undefined; month: string | undefined; date: string | undefined }) {
  const queryClient = useQueryClient()
  const roster = useGetRosterMonth(unit.id, inAddress ? { month: inAddress } : undefined, { query: { placeholderData: keepPreviousData } })
  const [said, setSaid] = useState("")
  const focusNext = useRef<string | null>(null)
  useEffect(() => {
    if (focusNext.current && document.getElementById(focusNext.current)) {
      document.getElementById(focusNext.current)?.focus()
      focusNext.current = null
    }
  })
  const decided = (answer: RosterMonth, words: string) => {
    queryClient.setQueryData(getGetRosterMonthQueryKey(unit.id, inAddress ? { month: inAddress } : undefined), answer)
    void queryClient.invalidateQueries({ queryKey: getGetRosterOverviewQueryKey() })
    void queryClient.invalidateQueries({ queryKey: getListSubmittedRostersQueryKey() })
    setSaid(words)
    // The buttons are gone with the decision. Approved, the status line says so; rejected, there is nothing left to show.
    focusNext.current = answer.state === "approved" ? "roster-status" : "decided"
  }
  const answer = roster.data
  const draft = answer?.draft
  const label = answer ? `${ROSTER_HEADING[answer.state]} of ${unit.name}, ${monthName(answer.month)}` : ""
  return (
    <>
      {roster.isPending && <p>Loading…</p>}
      {roster.isError && <p role="alert">This month's roster could not be read. Reload the page to try again.</p>}
      <p id="decided" role="status" tabIndex={-1} className="text-sm text-muted-foreground outline-none empty:sr-only">{said}</p>
      {answer && (
        <>
          <MonthNav month={answer.month} link={(month, text) => (
            <Link to="/unit-roster" search={{ unit: unitInAddress, month }} className={MONTH_LINK} onClick={() => setSaid("")}>{text}</Link>
          )} />
          {answer.state === "none" && <p>{unit.name} has no roster for {monthName(answer.month)} yet.</p>}
          {answer.state === "draft" && <p>{unit.name} has a draft for {monthName(answer.month)}, not submitted yet. It is its manager's until they submit it.</p>}
          <RosterStatus answer={answer} forApprover />
          {draft && (
            <section aria-labelledby="roster" className="flex flex-col gap-4">
              <h3 id="roster" className="font-medium">{ROSTER_HEADING[answer.state]}</h3>
              <p className="text-sm text-muted-foreground tabular-nums">
                {draft.edited ? (answer.state === "approved" ? "Changed by hand after its solve, as the versions below say. " : "Edited by hand after its solve. ") : ""}
                {draft.check ? `Cost ${draft.cost ?? "not known"} on the month as it stands, against ${draft.solved_cost} as solved.` : `Cost ${draft.cost}.`}
              </p>
              <Changes answer={answer} draft={draft} />
              <PinNotes draft={draft} />
              {draft.check && <Violations check={draft.check} what="the roster as it stands" />}
              <RosterGrid grid={draft} label={label} markedDate={date} />
              <GridLegend grid={draft} />
              {answer.state === "submitted" && <Decision unitId={unit.id} answer={answer} onDecided={decided} />}
              {answer.state === "approved" && <RosterVersions unitId={unit.id} month={answer.month} />}
              {draft.edited && <p className="text-sm text-muted-foreground">The details below are what its solve answered, before the changes made by hand.</p>}
              <ResultDetails unitId={unit.id} solveId={draft.solve_id} />
            </section>
          )}
        </>
      )}
    </>
  )
}

function UnitRosterPage() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  const { unit: unitInAddress, month, date } = Route.useSearch()
  const navigate = useNavigate({ from: Route.fullPath })
  const { units, chosen } = useChosenUnit(user, unitInAddress)
  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-4">
      <h1 className="text-xl font-medium">{chosen ? `Roster of ${chosen.name}` : "Unit roster"}</h1>
      <p className="text-muted-foreground">A unit's submitted or approved roster, to read, and to approve or reject when it is submitted. Only its manager changes it. <Link to="/rosters" className={LINK}>Every unit's rosters</Link></p>
      {units.isPending && <p>Loading…</p>}
      {units.isError && <p role="alert">The units could not be read. Reload the page to try again.</p>}
      {units.data && units.data.length === 0 && <p>There are no units yet.</p>}
      {units.data && chosen && <UnitPicker units={units.data} value={chosen.id} onChange={(unit) => void navigate({ search: { unit, month } })} />}
      {chosen && <RosterView key={chosen.id} unit={chosen} unitInAddress={unitInAddress ?? chosen.id} month={month} date={date} />}
    </div>
  )
}
