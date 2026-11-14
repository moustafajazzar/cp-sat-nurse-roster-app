import { zodResolver } from "@hookform/resolvers/zod"
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, Link, useBlocker, useMatch, useNavigate, useRouteContext } from "@tanstack/react-router"
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { ApiError } from "@/api/fetcher"
import {
  checkDraftEdits,
  getGetRosterMonthQueryKey,
  getGetSolveProgressQueryKey,
  getListRosterVersionsQueryKey,
  getPreviewReplanQueryKey,
  getPreviewSolveQueryKey,
  useGetRosterMonth,
  useGetSolveProgress,
  usePreviewReplan,
  usePreviewSolve,
  useSaveDraftPins,
  useSaveRosterVersion,
  useStartReplan,
  useStartSolve,
  useStopSolve,
  useSubmitRoster,
} from "@/api/generated/endpoints"
import {
  Role,
  type Draft,
  type DraftCheck,
  type EditBody,
  type ReplanPreview,
  type RosterMonth,
  type SolvePreview,
  type SolveProgress,
} from "@/api/generated/model"
import { type EditableCellState, CellEditor } from "@/components/cell-editor"
import { FormField } from "@/components/form-field"
import { MonthCloseCard } from "@/components/month-close"
import { MONTH_LINK, monthInAddress, MonthNav } from "@/components/month-nav"
import { LeavePageDialog } from "@/components/password-dialog"
import { type CellMarks, GridLegend, RosterGrid } from "@/components/roster-grid"
import { changeSays, PinNotes, plural, ResultDetails, ROSTER_HEADING, RosterStatus, Violations } from "@/components/roster-notes"
import { RosterVersions } from "@/components/roster-versions"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field"
import { Textarea } from "@/components/ui/textarea"
import { useChosenUnit } from "@/components/unit-picker"
import { cellKey, keyOf, usePendingEdits } from "@/lib/pending-edits"
import { plainRefusal, serverErrors, showServerErrors } from "@/lib/server-errors"
import { useDebounced } from "@/lib/use-debounced"
import { hospitalTime, longDay, monthName } from "@/lib/utils"
import { onlyFor } from "@/session"
import { WorkspacePrototype } from "@/components/workspace-prototype"

const searchSchema = z.object({ month: monthInAddress, variant: z.enum(["a", "b", "c"]).optional() })

export const Route = createFileRoute("/_signed-in/roster")({
  validateSearch: (search) => searchSchema.parse(search),
  beforeLoad: onlyFor(Role.unit_manager),
  staticData: { title: "Roster" },
  component: RosterPage,
})

/** How often an open page asks how a solve is getting on. Short in a test, so a test sees the page change without waiting. */
const POLL_MS = import.meta.env.MODE === "test" ? 10 : 1500
const LINK = "underline underline-offset-3"

const isRefusal = (error: unknown) => error instanceof ApiError && error.status < 500
const isActive = (solve: SolveProgress) => solve.state === "queued" || solve.state === "running"
/** A re-plan of an approved month, which ends as a new version rather than a draft. */
const isReplan = (solve: SolveProgress) => !!solve.replan_from

function ProgressCard({ unitId, solve: first, onEnded }: { unitId: string; solve: SolveProgress; onEnded: (solve: SolveProgress) => void }) {
  const queryClient = useQueryClient()
  const progress = useGetSolveProgress(unitId, first.id, {
    query: {
      initialData: first,
      // A refusal will not change by asking again, and a page that goes on asking keeps a tab busy for good.
      refetchInterval: (query) => (query.state.data && isActive(query.state.data) && !isRefusal(query.state.error) ? POLL_MS : false),
    },
  })
  const stop = useStopSolve()
  const solve = progress.data ?? first

  // Once: the card stays until the month's next answer takes it away, and the page around it renders meanwhile.
  const told = useRef(false)
  useEffect(() => {
    if (isActive(solve) || told.current) return
    told.current = true
    onEnded(solve)
  }, [solve, onEnded])

  const stopNow = () => stop.mutate({ unitId, solveId: first.id }, {
    onSuccess: (answer) => queryClient.setQueryData(getGetSolveProgressQueryKey(unitId, first.id), answer),
  })

  return (
    <Card role="region" aria-labelledby="solve-progress" data-solve-progress>
      <CardHeader>
        <CardTitle><h3 id="solve-progress" tabIndex={-1} className="outline-none">
          {isReplan(solve) ? `Re-planning ${monthName(solve.month)} from ${longDay(solve.replan_from!)}` : `Solving ${monthName(solve.month)}`}
        </h3></CardTitle>
        <CardDescription>{isReplan(solve)
          ? "You can leave this page: the re-plan carries on. When it ends its roster goes in force as a new version, unless the roster in force or the month changes meanwhile."
          : "You can leave this page: the solve carries on, and its draft is here when you come back."}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col items-start gap-3">
        {solve.state === "queued" && (
          <p>{solve.queue_ahead ? `Waiting in the hospital's queue: ${plural(solve.queue_ahead, "solve")} ahead of this one.` : "Next in the hospital's queue."}</p>
        )}
        {solve.state === "running" && (
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm tabular-nums">
            <dt className="text-muted-foreground">Searched for</dt>
            <dd>{Math.round(solve.elapsed_seconds)} of {plural(solve.time_limit_seconds, "second")}</dd>
            <dt className="text-muted-foreground">Best cost so far</dt>
            <dd>{solve.best_cost ?? "No roster yet"}</dd>
            <dt className="text-muted-foreground">Improving rosters</dt>
            <dd>{solve.improvements}</dd>
          </dl>
        )}
        {solve.attempts > 1 && <p className="text-sm text-muted-foreground">The first attempt failed, so this is the second: {solve.error}</p>}
        {progress.isError && (
          <p role="alert" className="text-sm text-destructive">
            {isRefusal(progress.error) ? "This solve can no longer be read from here. Reload the page." : "The latest progress could not be read. This page keeps trying."}
          </p>
        )}
        {stop.isError && <p role="alert" className="text-sm text-destructive">{plainRefusal(stop.error) ?? "The solve could not be stopped. Check your connection and try again."}</p>}
        <Button variant="outline" onClick={stopNow} disabled={stop.isPending || solve.stop_requested}>
          {solve.stop_requested ? "Stopping…" : solve.state === "queued" ? "Take out of the queue" : isReplan(solve) ? "Stop and put the best roster in force" : "Stop and keep the best roster"}
        </Button>
      </CardContent>
    </Card>
  )
}

function UnitRulesLink({ children }: { children: string }) {
  return <Link to="/unit-rules" className={LINK}>{children}</Link>
}

function RulesRefused({ detail }: { detail: string }) {
  return (
    <div className="flex flex-col gap-1">
      <p>The solver cannot take the rules as they stand. <UnitRulesLink>Review the unit rules</UnitRulesLink>, or ask the CNO if this names a hospital rule.</p>
      <p className="text-sm text-muted-foreground">It says: {detail}</p>
    </div>
  )
}

const IN_FORCE_AS_IT_WAS = "so the roster in force is as it was."

/** Why a re-plan left the roster in force as it was. */
function WhyNotInForce({ solve }: { solve: SolveProgress }) {
  if (solve.state === "failed") return <p role="alert">The re-plan failed, {IN_FORCE_AS_IT_WAS} You can run it again. It says: {solve.error}</p>
  if (solve.replan_outcome === "unchanged") return <p>The re-plan changed no cell, {IN_FORCE_AS_IT_WAS}</p>
  if (solve.replan_outcome === "outdated") {
    return <p role="alert">The roster in force or the month changed while the re-plan ran, so its roster was not put in force: it would have undone that change. Re-plan again to take it in.</p>
  }
  if (solve.replan_outcome === "month_over") return <p>The month ended while the re-plan ran, so its roster was not put in force.</p>
  if (solve.state === "cancelled") {
    return <p>The re-plan you stopped had found {solve.best_cost === null ? "no roster yet" : "no roster it could keep"}, {IN_FORCE_AS_IT_WAS}</p>
  }
  return (
    <>
      <p role="alert">The re-plan ended with no roster to keep, {IN_FORCE_AS_IT_WAS} {solve.message}</p>
      {solve.precheck.length > 0 && <ul className="list-disc ps-5 text-sm">{solve.precheck.map((issue) => <li key={issue}>{issue}</li>)}</ul>}
    </>
  )
}

function WhyNoDraft({ solve }: { solve: SolveProgress }) {
  if (solve.failure === "refused") return <RulesRefused detail={solve.error} />
  if (isReplan(solve)) return <WhyNotInForce solve={solve} />
  if (solve.state === "failed") {
    return <p role="alert">The solve failed, and the draft is as it was. You can run it again. It says: {solve.error}</p>
  }
  if (solve.state === "cancelled") {
    return <p>The solve you stopped had found no roster yet, so nothing has changed.</p>
  }
  return (
    <>
      <p role="alert">The solver found no roster for this month, so nothing has changed. {solve.message}</p>
      {solve.precheck.length > 0 && <ul className="list-disc ps-5 text-sm">{solve.precheck.map((issue) => <li key={issue}>{issue}</li>)}</ul>}
    </>
  )
}

/** Why the latest solve left no draft of its own. The whole of it is one link away. */
function LastSolve({ solve }: { solve: SolveProgress }) {
  return (
    <div className="flex flex-col gap-1">
      <WhyNoDraft solve={solve} />
      <p><Link to="/solve-history/$solveId" params={{ solveId: solve.id }} className={`text-sm ${LINK}`}>Open this result</Link></p>
    </div>
  )
}

type Pending = ReturnType<typeof usePendingEdits>

const signed = (delta: number) => (delta > 0 ? `+${delta}` : delta < 0 ? `−${-delta}` : "no change")

/** What the edits not yet saved would do, asked for once the editing pauses. The answer shown is always the one for the edits as
 *  they are: an older one arriving late belongs to another query and is never shown. */
function CheckOfEdits({ unitId, month, edits, version, what }: { unitId: string; month: string; edits: EditBody[]; version: string; what: "draft" | "roster" }) {
  const current = JSON.stringify(edits)
  const settled = useDebounced(current)
  const check = useQuery({
    queryKey: ["checkDraftEdits", unitId, month, version, settled],
    queryFn: ({ signal }) => checkDraftEdits(unitId, { month, edits: JSON.parse(settled) as EditBody[] }, { signal }),
    enabled: edits.length > 0,
    staleTime: Infinity,
    gcTime: 60_000,
    retry: false,
  })
  const answer: DraftCheck | undefined = settled === current ? check.data : undefined
  const failed = settled === current && check.isError
  const said = failed ? "These changes could not be checked: the server did not answer. They are still here, and saving still works."
    : !answer ? "Checking these changes…"
    : answer.reason ? "The solver's code could not check these changes."
    : `With these changes the ${what} ${answer.ok ? "breaks no hard rule" : `breaks ${plural(answer.violations.length, "hard rule")}`}, and costs ${answer.cost}${answer.delta === null ? ", against a saved grid whose cost could not be worked out" : ` (${signed(answer.delta)})`}.`
  return (
    <div className="flex flex-col gap-2">
      <p role="status" className="text-sm font-medium tabular-nums">{said}</p>
      {answer && !answer.ok && <Violations check={answer} what={`the ${what} with these changes`} />}
      {answer?.reason && <p className="text-sm">It says: {answer.reason}</p>}
      {answer && answer.refusals.length > 0 && (
        <div className="flex flex-col gap-1 text-sm">
          <p className="font-medium">These would be refused, so the check leaves them out:</p>
          <ul className="list-disc ps-5">{answer.refusals.map((refusal) => <li key={refusal.hospital_id + refusal.date}>{refusal.reason}</li>)}</ul>
        </div>
      )}
    </div>
  )
}

/** The panel that held Save and Discard is gone once either is done, so focus goes back into the grid, where the editing was. */
const backToTheGrid = () => requestAnimationFrame(() => document.querySelector<HTMLElement>("[data-cell][tabindex='0']")?.focus())

const monthIn = (location: unknown) => (location as { search?: { month?: string } }).search?.month

/** Asks before anything would throw away edits not yet saved: another page, or another month of this one. The browser asks
 *  before a reload or closing the tab. */
function UnsavedEditsGuard({ holding, month }: { holding: boolean; month: string | undefined }) {
  const page = useMatch({ strict: false, select: (match) => match.routeId })
  const blocker = useBlocker({
    shouldBlockFn: ({ current, next }) => holding && current.routeId === page
      && (next.routeId !== page || monthIn(current) !== monthIn(next)),
    enableBeforeUnload: () => holding,
    withResolver: true,
    disabled: !holding,
  })
  return (
    <LeavePageDialog open={blocker.status === "blocked"} title="Leave without saving your changes?"
      // The month, not "the draft": the changes may be to the roster in force.
      description={`Your changes to the roster of ${month ? monthName(month) : "this month"} are not saved yet. If you leave, they are lost.`} action="Leave without saving"
      onStay={() => blocker.reset?.()} onLeave={() => blocker.proceed?.()} />
  )
}

const reasonSchema = z.object({ reason: z.string().trim().min(1, "Say why, for the version list.").max(2000, "At most 2000 characters.") })

function PendingPanel({ unitId, draft, month, approved, pending, onSaved, refused, setRefused }: {
  unitId: string
  draft: Draft
  month: string
  /** An approved roster's changes are saved as a new version, with a reason. */
  approved: boolean
  pending: Pending
  onSaved: (answer: RosterMonth, count: number) => void
  refused: Map<string, string>
  setRefused: (refused: Map<string, string>) => void
}) {
  const savePins = useSaveDraftPins()
  const saveVersion = useSaveRosterVersion()
  const save = approved ? saveVersion : savePins
  const form = useForm<z.infer<typeof reasonSchema>>({ resolver: zodResolver(reasonSchema), defaultValues: { reason: "" } })
  const edits = Object.values(pending.edits)
  const names = new Map(draft.rows.map((row) => [row.hospital_id, row.full_name || row.hospital_id]))
  const callbacks = {
    onSuccess: (answer: RosterMonth) => { setRefused(new Map()); onSaved(answer, edits.length) },
    onError: (error: unknown) => {
      const found = new Map<string, string>()
      serverErrors(error).forEach(({ path, message }) => {
        if (path === "reason") form.setError("reason", { message })
        const index = Number(path.split(".")[1])
        if (path.startsWith("edits.") && edits[index]) found.set(keyOf(edits[index]), message)
      })
      setRefused(found)
    },
  }
  const sendPins = () => savePins.mutate({ unitId, data: { month, edits } }, callbacks)
  const sendVersion = form.handleSubmit(({ reason }) => saveVersion.mutate({ unitId, data: { month, edits, reason } }, callbacks))
  const send = approved ? () => void sendVersion() : sendPins
  const reasonError = form.formState.errors.reason
  // Once the list is on the page: it is not there yet when the answer comes.
  useEffect(() => { if (refused.size > 0) document.getElementById("save-refused")?.focus() }, [refused])
  const plainly = save.isError && serverErrors(save.error).length === 0 && (plainRefusal(save.error) ?? "The changes could not be saved. Check your connection and try again. They are still here.")
  return (
    // Held at the foot of the screen while the grid scrolls, and never taller than half of it, so the cells stay in view on a phone.
    <section aria-labelledby="unsaved" className="sticky bottom-0 z-40 flex max-h-[50svh] flex-col gap-3 overflow-y-auto rounded-md border bg-background p-3 shadow-md">
      <h4 id="unsaved" className="text-sm font-medium">{plural(edits.length, "change")} not saved yet</h4>
      {/* Whatever the answer depends on besides the edits: the draft, its pins, and the month's leave, dates and nurses. */}
      <CheckOfEdits unitId={unitId} month={month} edits={edits} what={approved ? "roster" : "draft"}
        version={JSON.stringify([draft.solve_id, draft.cost, draft.pins, draft.editor, draft.changes, draft.rows.map((row) => row.cells)])} />
      {refused.size > 0 && (
        <div id="save-refused" tabIndex={-1} role="alert" className="flex flex-col gap-1 text-sm text-destructive outline-none">
          <p className="font-medium">Nothing was saved, because of these. Change or undo them, then save again:</p>
          <ul className="list-disc ps-5">
            {[...refused].map(([key, message]) => {
              const [hospitalId, date] = key.split("|")
              return <li key={key}>{names.get(hospitalId) ?? hospitalId}, {longDay(date)}: {message}</li>
            })}
          </ul>
        </div>
      )}
      {approved && (
        <Field data-invalid={!!reasonError || undefined}>
          <FieldLabel htmlFor="change-reason">Why</FieldLabel>
          <Textarea id="change-reason" rows={2} aria-invalid={!!reasonError || undefined}
            aria-describedby={["change-reason-description", reasonError && "change-reason-error"].filter(Boolean).join(" ")} {...form.register("reason")} />
          <FieldDescription id="change-reason-description">Kept with the new version, for you, the CNO and the Deputy CNO. The unit's nurses are told only that the roster changed, never why.</FieldDescription>
          <FieldError id="change-reason-error" role={undefined} errors={reasonError ? [reasonError] : undefined} />
        </Field>
      )}
      {plainly && <p role="alert" className="text-sm text-destructive">{plainly}</p>}
      <div className="flex flex-wrap gap-2">
        <Button onClick={send} disabled={save.isPending}>{save.isPending ? "Saving…" : approved ? "Save as a new version" : "Save changes"}</Button>
        <Button variant="outline" disabled={save.isPending} onClick={() => { setRefused(new Map()); pending.clear(); backToTheGrid() }}>Discard changes</Button>
      </div>
    </section>
  )
}


/** Sends the draft to the CNO and the Deputy CNO. The server checks it again on the month as it stands, and says why if it
 *  cannot go: a refusal it would give is on the month's answer already, so the page says it before the press. */
function SubmitForm({ unitId, answer, unsaved, onSubmitted }: { unitId: string; answer: RosterMonth; unsaved: boolean; onSubmitted: (answer: RosterMonth) => void }) {
  const submit = useSubmitRoster()
  const refusal = answer.submit_refusal
  const failed = submit.isError && (plainRefusal(submit.error) ?? "The roster could not be submitted. Check your connection and try again.")
  return (
    <section aria-labelledby="submit" className="flex flex-col items-start gap-2 rounded-md border p-3">
      <h3 id="submit" className="font-medium">Submit for approval</h3>
      <p className="text-sm text-muted-foreground">The CNO or the Deputy CNO then approves it, or rejects it with a comment. Until they decide, nothing can change it: no solve, no edit.</p>
      {answer.draft && answer.draft.changes.length > 0 && !refusal && <p className="text-sm">It was solved before the changes listed above. It breaks no hard rule on the month as it stands, so it can go as it is, or you can solve again first.</p>}
      {refusal && <p className="text-sm">{refusal}</p>}
      {unsaved && <p className="text-sm">Only what is saved can be submitted, so save or discard your changes first.</p>}
      {failed && <p role="alert" className="text-sm text-destructive">{failed}</p>}
      <Button onClick={() => submit.mutate({ unitId, data: { month: answer.month } }, { onSuccess: onSubmitted })}
        disabled={submit.isPending || !!refusal || unsaved}>{submit.isPending ? "Submitting…" : "Submit for approval"}</Button>
    </section>
  )
}

function DraftCard({ unitId, month, draft, state, label, pending, solving, onSaved, submit }: {
  unitId: string
  month: string
  draft: Draft
  state: RosterMonth["state"]
  /** Between what the grid breaks and its solve's details, which are long, so it is found where the draft is read. */
  submit?: ReactNode
  label: string
  pending: Pending
  /** What of this month is queued or running, if anything. */
  solving: "solve" | "replan" | null
  onSaved: (answer: RosterMonth, count: number) => void
}) {
  const navigate = useNavigate()
  const [open, setOpen] = useState<{ cell: EditableCellState; anchor: HTMLElement } | null>(null)
  const [refused, setRefused] = useState<Map<string, string>>(new Map())
  const pins = useMemo(() => new Map(draft.pins.map((pin) => [cellKey(pin.hospital_id, pin.date), pin])), [draft.pins])
  const nurses = useMemo(() => new Map(draft.editor.nurses.map((nurse) => [nurse.hospital_id, nurse])), [draft.editor.nurses])
  const shiftsByCode = useMemo(() => new Map(draft.editor.shifts.map((shift) => [shift.code, shift])), [draft.editor.shifts])
  const editable = draft.editor.refusal === null
  const approved = state === "approved"
  const gone = useMemo(() => new Set(draft.editor.gone), [draft.editor.gone])

  const marks = (hospitalId: string, date: string): CellMarks => {
    const key = cellKey(hospitalId, date)
    const pin = pins.get(key)
    const edit = pending.edits[key]
    const pinned = pin && (pin.set_aside ? "pinned, but counts for nothing now" : pin.origin === "manager" ? "pinned" : `pinned by an accepted ${pin.origin}`)
    const pendingMark = edit && (edit.action === "clear" ? { shows: null, says: "pin to be cleared, not saved" }
      : { shows: edit.action === "off" ? "" : edit.shift!, says: `to be pinned ${edit.action === "off" ? "off" : `to ${shiftsByCode.get(edit.shift!)?.display_name ?? edit.shift}`}, not saved` })
    return { pinned, pending: pendingMark, refused: refused.has(key) }
  }

  const openCell = (hospitalId: string, date: string, button: HTMLButtonElement) => {
    const nurse = nurses.get(hospitalId)
    const row = draft.rows.find((candidate) => candidate.hospital_id === hospitalId)
    const cell = row?.cells[draft.days.findIndex((day) => day.date === date)]
    const credited = gone.has(hospitalId) ? "gone" : nurse?.credited.includes(date) ? (cell?.kind === "outside" ? "outside" : "leave") : null
    const allowed = nurse?.allowed_shifts
    const key = cellKey(hospitalId, date)
    const pin = pins.get(key)
    setOpen({
      anchor: button,
      cell: {
        hospitalId, date, title: `${row?.full_name || hospitalId}, ${longDay(date)}`, credited, holdsShift: !!cell?.shift,
        shifts: draft.editor.shifts.filter((shift) => !allowed || allowed.includes(shift.code)),
        pin: pin ? { shift: pin.shift, origin: pin.origin } : null, pending: pending.edits[key] ?? null,
      },
    })
  }

  const choose = (edit: EditBody) => {
    const pin = pins.get(keyOf(edit))
    const same = pin && ((edit.action === "shift" && pin.shift === edit.shift) || (edit.action === "off" && pin.shift === ""))
    if (same) pending.drop(keyOf(edit))
    else pending.put(edit)
    setOpen(null)
  }

  return (
    <section aria-labelledby="draft" className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <h3 id="draft" tabIndex={-1} className="font-medium outline-none">{ROSTER_HEADING[state]}</h3>
        <p className="text-sm text-muted-foreground tabular-nums">
          {draft.restored_at ? `Restored ${hospitalTime(draft.restored_at)} from the solve of ${hospitalTime(draft.solved_at)}.` : `Solved ${hospitalTime(draft.solved_at)}.`}{" "}
          {draft.edited && (approved ? "Changed by hand since, as the versions below say. " : "Edited by hand since. ")}
          {draft.check
            ? `Cost ${draft.cost ?? "not known"} on the month as it stands, against ${draft.solved_cost} as solved.`
            : `Cost ${draft.cost}.`}
          {draft.stopped_early && (approved
            ? " The solve or re-plan it came from was stopped early, so this is the best roster that had been found."
            : " You stopped this solve early, so this is the best roster it had found.")}
        </p>
      </div>
      {draft.changes.length > 0 && (
        <div role="alert" className="flex flex-col gap-1 rounded-md border border-destructive/40 p-3 text-sm">
          <p className="font-medium">{approved ? "This roster was solved before these changes:" : "This draft was solved before these changes:"}</p>
          <ul className="list-disc ps-5">
            {draft.changes.map((change) => <li key={change.hospital_id + change.what}>{changeSays(change, month, approved ? "since this roster was solved" : "since this draft was solved")}</li>)}
          </ul>
          <p>{approved
            ? "What it now breaks is listed below the grid. Change the cells by hand to put it right: each save is a new version."
            : "Solve again to take them in: a solve keeps your pins. Otherwise, the roster is checked again when you submit it."}</p>
        </div>
      )}
      <PinNotes draft={draft} />
      {draft.editor.refusal
        ? <p className="text-sm">{draft.editor.refusal}</p>
        : approved
          ? <p className="text-sm text-muted-foreground">Choose a cell to change it: a cover, late leave, a swap agreed in person. The arrow keys move between cells. Each save is a new version in force at once, with your reason, and the unit's nurses are told the roster has changed. Changed cells are pinned, so a later re-plan keeps them.</p>
          : <p className="text-sm text-muted-foreground">Choose a cell to pin a shift or a day off, or to clear its pin. The arrow keys move between cells. Pins are kept by every solve of this month.</p>}
      {solving === "replan" && editable && <p className="text-sm">A re-plan of this month is under way. A change saved now makes a new version at once, and the re-plan then ends without changing the roster in force, as its roster would undo yours: re-plan again after.</p>}
      {solving === "solve" && editable && <p className="text-sm">A solve of this month is under way. It was given the month as it was when it started, so changes you save now are put on top of its roster when it ends, and the draft then asks you to solve again.</p>}
      <RosterGrid grid={draft} label={label} extraShifts={draft.editor.shifts} editing={editable ? { marks, onOpen: openCell } : undefined} />
      <GridLegend grid={draft} editable={editable} />
      {editable && <CellEditor cell={open?.cell ?? null} anchor={open?.anchor ?? null} onChoose={choose}
        onUndo={() => { if (open) pending.drop(cellKey(open.cell.hospitalId, open.cell.date)); setOpen(null) }} onClose={() => setOpen(null)}
        covers={approved ? {
          shifts: draft.editor.shifts, day: longDay,
          onOpen: (date, shift) => { setOpen(null); void navigate({ to: "/unit-covers", search: { month: date.slice(0, 7), date, shift } }) },
        } : undefined} />}
      {editable && Object.keys(pending.edits).length > 0 && (
        <PendingPanel unitId={unitId} draft={draft} month={month} approved={approved} pending={pending} onSaved={onSaved} refused={refused} setRefused={setRefused} />
      )}
      {draft.check && <Violations check={draft.check} what={approved ? "the roster in force, on the month as it stands," : "the draft as it stands"} />}
      {submit}
      {approved && <RosterVersions unitId={unitId} month={month} />}
      {draft.edited && <p className="text-sm text-muted-foreground">The details below are what its solve answered, before the changes made by hand.</p>}
      <ResultDetails unitId={unitId} solveId={draft.solve_id} />
    </section>
  )
}

function Warnings({ preview }: { preview: SolvePreview }) {
  const { hospital_rules_need_review, unit_rules_need_review, unreviewed_scores, left_out } = preview.warnings
  const items = [
    hospital_rules_need_review && <>The hospital rules are still the seeded defaults, which the CNO has not reviewed.</>,
    unit_rules_need_review && <>The unit rules are still the seeded defaults. <UnitRulesLink>Review them</UnitRulesLink></>,
    unreviewed_scores > 0 && (
      <>{unreviewed_scores === 1 ? "1 nurse's attributes have never been saved, so their score is" : `${unreviewed_scores} nurses' attributes have never been saved, so their scores are`} still the default.{" "}
        <Link to="/rostering-attributes" className={LINK}>Review them</Link></>
    ),
    left_out.length > 0 && <>{left_out.join(", ")} {left_out.length === 1 ? "is" : "are"} left out of this solve: a manager's account deactivated, with no end date on the nurse profile.</>,
  ].filter(Boolean)
  if (items.length === 0) return null
  return (
    <div className="flex flex-col gap-1">
      <h4 className="text-sm font-medium">Worth a look. None of these stops a solve.</h4>
      <ul className="list-disc ps-5 text-sm">{items.map((item, index) => <li key={index}>{item}</li>)}</ul>
    </div>
  )
}

function Precheck({ issues }: { issues: string[] | null }) {
  if (issues === null) return <p className="text-sm">The solver did not answer, so the month could not be checked in advance. A solve started now may fail.</p>
  if (issues.length === 0) return <p className="text-sm">The solver's check found no reason this month could not be solved.</p>
  return (
    <div className="flex flex-col gap-1">
      <h4 className="text-sm font-medium">The solver's check says this month may not be solvable:</h4>
      <ul className="list-disc ps-5 text-sm">{issues.map((issue) => <li key={issue}>{issue}</li>)}</ul>
    </div>
  )
}

function StartForm({ unitId, preview, onStarted, solvingAgain, unsaved }: { unitId: string; preview: SolvePreview; onStarted: () => void; solvingAgain: boolean; unsaved: boolean }) {
  const start = useStartSolve()
  const schema = z.object({
    time_limit_seconds: z.number("Enter a whole number of seconds.").int("Enter a whole number of seconds.")
      .min(1, "At least 1 second.").max(preview.time_limit_cap, `The System Admin's cap is ${preview.time_limit_cap} seconds.`),
  })
  const form = useForm<z.infer<typeof schema>>({ resolver: zodResolver(schema), defaultValues: { time_limit_seconds: preview.time_limit_default } })
  const submit = form.handleSubmit((data) => start.mutate({ unitId, data: { month: preview.month, ...data } }, {
    onSuccess: onStarted,
    onError: (error) => showServerErrors(form, error, (loc) => String(loc.at(-1))),
  }))
  const refused = start.isError && plainRefusal(start.error)
  return (
    <form onSubmit={submit} noValidate className="flex flex-col items-start gap-3">
      <div className="w-48">
        <FormField label="Time limit (seconds)" type="number" inputMode="numeric" min={1} max={preview.time_limit_cap}
          description={`Up to ${preview.time_limit_cap}.`}
          registration={form.register("time_limit_seconds", { valueAsNumber: true })} error={form.formState.errors.time_limit_seconds} />
      </div>
      {solvingAgain && <p className="text-sm text-muted-foreground">Solving again replaces the draft's grid, keeping its pins. The solve history keeps what it replaced.</p>}
      {unsaved && <p className="text-sm">Save or discard your changes to the draft first: a solve is given only saved pins.</p>}
      {refused && <p role="alert" className="text-sm text-destructive">{refused}</p>}
      {start.isError && !refused && !form.formState.errors.time_limit_seconds && <p role="alert" className="text-sm text-destructive">The solve could not be started. Check your connection and try again.</p>}
      <Button type="submit" disabled={start.isPending || unsaved}>{solvingAgain ? "Solve again" : "Start solve"}</Button>
    </form>
  )
}

function PreviewCard({ unitId, month, onStarted, solvingAgain, unsaved }: { unitId: string; month: string; onStarted: () => void; solvingAgain: boolean; unsaved: boolean }) {
  const preview = usePreviewSolve(unitId, { month })
  const answer = preview.data
  return (
    <Card role="region" aria-labelledby="before-solving">
      <CardHeader>
        <CardTitle><h3 id="before-solving" tabIndex={-1} className="outline-none">What the solver will receive</h3></CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {preview.isPending && <p>Loading…</p>}
        {preview.isError && <p role="alert">This month could not be read. Reload the page to try again.</p>}
        {answer && (
          <>
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm tabular-nums">
              <dt className="text-muted-foreground">Nurses</dt>
              <dd>{answer.summary.nurses}</dd>
              <dt className="text-muted-foreground">Days of leave</dt>
              <dd>{answer.summary.leave_days}</dd>
              <dt className="text-muted-foreground">Days before a start date or after an end date</dt>
              <dd>{answer.summary.outside_days}</dd>
              <dt className="text-muted-foreground">Pinned cells</dt>
              <dd>{answer.summary.pins}</dd>
            </dl>
            {answer.rules_refusal ? <RulesRefused detail={answer.rules_refusal} /> : !answer.refusal && <Precheck issues={answer.precheck} />}
            <Warnings preview={answer} />
            {answer.refusal && <p role="alert">{answer.refusal}</p>}
            {!answer.refusal && !answer.rules_refusal && <StartForm unitId={unitId} preview={answer} onStarted={onStarted} solvingAgain={solvingAgain} unsaved={unsaved} />}
          </>
        )}
      </CardContent>
    </Card>
  )
}

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/

/** An approved month's re-plan from a date: earlier days are facts, and the solver plans the rest again close to the roster in
 *  force. Each date asks the server again what the re-plan would be given, and whether it can be that date. */
function ReplanCard({ unitId, month, onStarted, unsaved }: { unitId: string; month: string; onStarted: () => void; unsaved: boolean }) {
  const [day, setDay] = useState<string | undefined>(undefined)
  const preview = usePreviewReplan(unitId, { month, ...(day && { replan_from: day }) }, { query: { placeholderData: keepPreviousData } })
  const answer = preview.data
  return (
    <Card role="region" aria-labelledby="replan">
      <CardHeader>
        <CardTitle><h3 id="replan" tabIndex={-1} className="outline-none">Re-plan from a date</h3></CardTitle>
        <CardDescription>When something changes mid-month, such as a nurse off sick for a week, plan the rest of the month again. The result is a new version, in force at once.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {preview.isPending && <p>Loading…</p>}
        {preview.isError && <p role="alert">What a re-plan would be given could not be read. Reload the page to try again.</p>}
        {answer && (answer.refusal
          ? <p role="alert">{answer.refusal}</p>
          : <ReplanForm unitId={unitId} preview={answer} onDay={setDay} onStarted={onStarted} unsaved={unsaved} />)}
      </CardContent>
    </Card>
  )
}

function ReplanForm({ unitId, preview, onDay, onStarted, unsaved }: {
  unitId: string
  preview: ReplanPreview
  onDay: (day: string) => void
  onStarted: () => void
  unsaved: boolean
}) {
  const start = useStartReplan()
  const schema = z.object({
    replan_from: z.string().regex(ISO_DAY, "Choose a date."),
    time_limit_seconds: z.number("Enter a whole number of seconds.").int("Enter a whole number of seconds.")
      .min(1, "At least 1 second.").max(preview.time_limit_cap, `The System Admin's cap is ${preview.time_limit_cap} seconds.`),
    reason: z.string().max(2000, "At most 2000 characters."),
  })
  const form = useForm<z.infer<typeof schema>>({
    resolver: zodResolver(schema),
    defaultValues: { replan_from: preview.replan_from, time_limit_seconds: preview.time_limit_default, reason: "" },
  })
  const chosen = form.watch("replan_from")
  // Only the answer for the date as it stands says whether that date can be one.
  const current = chosen === preview.replan_from
  const submit = form.handleSubmit((data) => start.mutate({ unitId, data: { month: preview.month, ...data, reason: data.reason.trim() } }, {
    onSuccess: onStarted,
    onError: (error) => showServerErrors(form, error, (loc) => String(loc.at(-1))),
  }))
  const refused = start.isError && plainRefusal(start.error)
  const dateError = form.formState.errors.replan_from ?? (current && preview.date_refusal ? { type: "server", message: preview.date_refusal } : undefined)
  const version = `version ${preview.in_force}`
  const reasonError = form.formState.errors.reason
  return (
    <form onSubmit={submit} noValidate className="flex flex-col items-start gap-4">
      <p className="text-sm">Days before the date are kept as they happened: {version}, with the leave and pins recorded since. From the date on, the solver plans again, keeping as close to {version} as the change weight makes worth it.</p>
      {current && !!preview.recorded_cells_differ && (
        <p className="text-sm">{plural(preview.recorded_cells_differ, "cell")} before the date already {preview.recorded_cells_differ === 1 ? "differs" : "differ"} from {version}: leave or a pin recorded since, kept as fact.</p>
      )}
      {preview.change_weight === 0 && <p className="text-sm">The change weight is 0, so nothing keeps the re-plan close to {version}: it may change any cell from the date on. The CNO sets the weights.</p>}
      <div className="w-48">
        <FormField label="Re-plan from" type="date" min={preview.earliest} max={preview.latest}
          description={`From ${longDay(preview.earliest)} to ${longDay(preview.latest)}.`}
          registration={form.register("replan_from", { onChange: (event) => { if (ISO_DAY.test(event.target.value)) onDay(event.target.value) } })}
          error={dateError} />
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm tabular-nums">
        <dt className="text-muted-foreground">Nurses</dt>
        <dd>{preview.summary.nurses}</dd>
        <dt className="text-muted-foreground">Days of leave</dt>
        <dd>{preview.summary.leave_days}</dd>
        <dt className="text-muted-foreground">Pinned cells</dt>
        <dd>{preview.summary.pins}</dd>
      </dl>
      {current && (preview.rules_refusal ? <RulesRefused detail={preview.rules_refusal} /> : !preview.date_refusal && <Precheck issues={preview.precheck} />)}
      <div className="w-48">
        <FormField label="Time limit (seconds)" type="number" inputMode="numeric" min={1} max={preview.time_limit_cap}
          description={`Up to ${preview.time_limit_cap}.`}
          registration={form.register("time_limit_seconds", { valueAsNumber: true })} error={form.formState.errors.time_limit_seconds} />
      </div>
      <Field data-invalid={!!reasonError || undefined} className="max-w-xl">
        <FieldLabel htmlFor="replan-reason">Why (optional)</FieldLabel>
        <Textarea id="replan-reason" rows={2} aria-invalid={!!reasonError || undefined}
          aria-describedby={["replan-reason-description", reasonError && "replan-reason-error"].filter(Boolean).join(" ")} {...form.register("reason")} />
        <FieldDescription id="replan-reason-description">Kept with the new version, for you, the CNO and the Deputy CNO. Left empty, it says the date. The unit's nurses are told only that the roster changed, never why.</FieldDescription>
        <FieldError id="replan-reason-error" role={undefined} errors={reasonError ? [reasonError] : undefined} />
      </Field>
      {unsaved && <p className="text-sm">Save or discard your changes first: a re-plan is given only what is saved.</p>}
      {refused && <p role="alert" className="text-sm text-destructive">{refused}</p>}
      {start.isError && !refused && !Object.keys(form.formState.errors).length && <p role="alert" className="text-sm text-destructive">The re-plan could not be started. Check your connection and try again.</p>}
      <Button type="submit" disabled={start.isPending || unsaved || !current || !!preview.date_refusal || !!preview.rules_refusal}>
        {ISO_DAY.test(chosen) ? `Re-plan from ${longDay(chosen)}` : "Re-plan"}
      </Button>
    </form>
  )
}

function UnitRoster({ unit, month: inAddress }: { unit: { id: string; name: string }; month: string | undefined }) {
  const queryClient = useQueryClient()
  const roster = useGetRosterMonth(unit.id, inAddress ? { month: inAddress } : undefined, { query: { placeholderData: keepPreviousData } })
  const [said, setSaid] = useState("")
  const answer = roster.data
  const refresh = () => Promise.all([
    queryClient.invalidateQueries({ queryKey: getGetRosterMonthQueryKey(unit.id) }),
    queryClient.invalidateQueries({ queryKey: getPreviewSolveQueryKey(unit.id) }),
    queryClient.invalidateQueries({ queryKey: getPreviewReplanQueryKey(unit.id) }),
    queryClient.invalidateQueries({ queryKey: getListRosterVersionsQueryKey(unit.id) }),
  ])
  // Focus was on Start or on Stop, and each goes when the page changes, so it follows to what took their place.
  const focusNext = useRef<string | null>(null)
  useEffect(() => {
    if (focusNext.current && document.getElementById(focusNext.current)) {
      document.getElementById(focusNext.current)?.focus()
      focusNext.current = null
    }
  })
  const started = () => {
    setSaid("")
    focusNext.current = "solve-progress"
    void refresh()
  }
  const ended = (solve: SolveProgress) => {
    const how = solve.state === "cancelled" ? "stopped" : "finished"
    const inForce = solve.replan_outcome === "version"
    setSaid(!isReplan(solve)
      ? (solve.found_roster ? `The solve ${how} with a cost of ${solve.best_cost}, and the draft is replaced.` : "The solve ended with no new draft.")
      : inForce
        ? `The re-plan ${how} with a cost of ${solve.best_cost}, and its roster is now in force as a new version. The unit's nurses are told the roster has changed.`
        : "The re-plan ended, and the roster in force is as it was. The page says why.")
    // Only if focus was on Stop, which is going, or nowhere: a solve can end minutes later, with the manager elsewhere on the page.
    const onTheCard = document.activeElement?.closest("[data-solve-progress]")
    if (onTheCard || document.activeElement === document.body) {
      focusNext.current = isReplan(solve) ? (inForce ? "draft" : "replan") : solve.found_roster ? "draft" : "before-solving"
    }
    void refresh()
  }
  const pending = usePendingEdits()
  const unsaved = Object.keys(pending.edits).length > 0
  // A month left without saving takes its edits with it: they are about its cells.
  const clearPending = pending.clear
  useEffect(() => { clearPending() }, [inAddress, clearPending])
  const saved = (month: RosterMonth, count: number) => {
    queryClient.setQueryData(getGetRosterMonthQueryKey(unit.id, inAddress ? { month: inAddress } : undefined), month)
    void queryClient.invalidateQueries({ queryKey: getPreviewSolveQueryKey(unit.id) })
    pending.clear()
    void queryClient.invalidateQueries({ queryKey: getListRosterVersionsQueryKey(unit.id) })
    // An approved roster's save that changes no cell is refused, so one that succeeds always made a version.
    setSaid(month.state === "approved"
      ? `${plural(count, "change")} saved as version ${month.approved?.version}, now in force. The unit's nurses are told the roster has changed.`
      : `${plural(count, "change")} saved.${month.active_solve ? " The solve under way was given the month without them: they go on top of its roster when it ends." : ""}`)
    backToTheGrid()
  }
  const submitted = (month: RosterMonth) => {
    queryClient.setQueryData(getGetRosterMonthQueryKey(unit.id, inAddress ? { month: inAddress } : undefined), month)
    void queryClient.invalidateQueries({ queryKey: getPreviewSolveQueryKey(unit.id) })
    setSaid("Submitted. The CNO and the Deputy CNO can now see it and decide. Nothing can change it until they do.")
    // Submit is gone with the form, so focus goes to what now says where the roster stands.
    focusNext.current = "roster-status"
  }
  const active = answer?.active_solve
  const frozen = answer?.state === "submitted" || answer?.state === "approved"
  return (
    <>
      <UnsavedEditsGuard holding={unsaved} month={answer?.month} />
      {roster.isPending && <p>Loading…</p>}
      {roster.isError && <p role="alert">This month's roster could not be read. Reload the page to try again.</p>}
      {/* Always rendered, because a live region mounted with its text may never be read out. */}
      <p role="status" className="text-sm text-muted-foreground empty:sr-only">{said}</p>
      {answer && (
        <>
          <MonthNav month={answer.month} link={(month, label) => <Link to="/roster" search={{ month }} className={MONTH_LINK} onClick={() => setSaid("")}>{label}</Link>} />
          <p><Link to="/solve-history" search={{ month: answer.month }} className={`text-sm ${LINK}`}>Solve history of {monthName(answer.month)}</Link></p>
          {active && <ProgressCard key={active.id} unitId={unit.id} solve={active} onEnded={ended} />}
          {!active && answer.last_solve && <LastSolve solve={answer.last_solve} />}
          <RosterStatus answer={answer} />
          {answer.draft && <DraftCard unitId={unit.id} month={answer.month} draft={answer.draft} state={answer.state}
            label={`${ROSTER_HEADING[answer.state]} of ${unit.name}, ${monthName(answer.month)}`} pending={pending} solving={active ? (isReplan(active) ? "replan" : "solve") : null} onSaved={saved}
            submit={answer.state === "draft" && <SubmitForm unitId={unit.id} answer={answer} unsaved={unsaved} onSubmitted={submitted} />} />}
          {!answer.draft && !active && !answer.last_solve && <p>No draft yet for {monthName(answer.month)}.</p>}
          {!active && answer.state === "approved" && <ReplanCard key={answer.month} unitId={unit.id} month={answer.month} onStarted={started} unsaved={unsaved} />}
          {!active && !frozen && <PreviewCard key={answer.month} unitId={unit.id} month={answer.month} onStarted={started} solvingAgain={!!answer.draft} unsaved={unsaved} />}
          <MonthCloseCard key={`close-${answer.month}`} unitId={unit.id} month={answer.month} />
        </>
      )}
    </>
  )
}

function RosterPage() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  const { month, variant } = Route.useSearch()
  const { units, chosen } = useChosenUnit(user, undefined)
  // PROTOTYPE (roster workspace): ?variant=a|b|c swaps the page for a throwaway workspace layout.
  if (variant && chosen) return <WorkspacePrototype unit={chosen} month={month} variant={variant} user={user} />
  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-4">
      <h1 className="text-xl font-medium">{chosen ? `Roster of ${chosen.name}` : "Roster"}</h1>
      <p className="text-muted-foreground">Solve a month to get its draft, then change any cell by hand. Each solve replaces the draft's grid and keeps your pins. Only you see a draft, until you submit it for approval. Once approved, your changes are new versions, in force at once.</p>
      {units.isPending && <p>Loading…</p>}
      {units.isError && <p role="alert">Your unit could not be read. Reload the page to try again.</p>}
      {units.data && !chosen && <p>No unit is assigned to you yet. The CNO assigns each unit its manager.</p>}
      {chosen && <UnitRoster key={chosen.id} unit={chosen} month={month} />}
    </div>
  )
}
