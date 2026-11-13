import { zodResolver } from "@hookform/resolvers/zod"
import { useQueryClient } from "@tanstack/react-query"
import { createFileRoute, Link, useRouteContext } from "@tanstack/react-router"
import { useRef, useState, type RefObject } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { ApiError } from "@/api/fetcher"
import {
  getGetRosterMonthQueryKey, getListRequestWindowsQueryKey, getListRosterVersionsQueryKey, getListWindowRequestsQueryKey,
  useAcceptRequest, useCancelRequest, useCloseRequestWindow, useEditRequest, useListRequestWindows, useListShifts,
  useListWindowPreferences, useListWindowRequests, useOpenRequestWindow, useRejectRequest,
} from "@/api/generated/endpoints"
import { Role, type UnitRequestRow, type UnitWindows, type WindowRow } from "@/api/generated/model"
import { ConfirmDialog } from "@/components/confirm-dialog"
import { FormField, SelectField } from "@/components/form-field"
import { monthInAddress } from "@/components/month-nav"
import {
  AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field"
import { NativeSelectOption } from "@/components/ui/native-select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Textarea } from "@/components/ui/textarea"
import { useChosenUnit } from "@/components/unit-picker"
import {
  askedFor, inSentence, herReason, lastDecision, NO_CONNECTION, PREFERENCE_STATE, PREFERS, shiftNames, UNIT_STATE,
} from "@/lib/request-words"
import { plainRefusal, showServerErrors } from "@/lib/server-errors"
import { monthName, scrollableRegion, shortDay } from "@/lib/utils"
import { onlyFor } from "@/session"

const searchSchema = z.object({ month: monthInAddress, window: z.string().optional().catch(undefined) })

/** The unit manager's request windows, and the unit's requests in each with their decisions. */
export const Route = createFileRoute("/_signed-in/requests")({
  validateSearch: (search) => searchSchema.parse(search),
  beforeLoad: onlyFor(Role.unit_manager),
  staticData: { title: "Requests" },
  component: RequestsPage,
})

const LINK = "underline underline-offset-3"
const STATUS = "requests-status"
const WINDOW_STATE = { scheduled: "Opens later", open: "Open", closed: "Closed" } as const

const windowSchema = z.object({
  month: z.string().regex(/^\d{4}-\d{2}$/, "Choose a month."),
  opens_on: z.iso.date("Choose the first day."),
  closes_on: z.iso.date("Choose the last day."),
})

function OpenWindowForm({ unitId, onOpened }: { unitId: string; onOpened: (message: string) => void }) {
  const queryClient = useQueryClient()
  const open = useOpenRequestWindow()
  const form = useForm<z.infer<typeof windowSchema>>({ resolver: zodResolver(windowSchema), defaultValues: { month: "", opens_on: "", closes_on: "" } })
  const refusal = plainRefusal(open.error)
  const submit = form.handleSubmit((data) => open.mutate({ unitId, data }, {
    onSuccess: async (window) => {
      await queryClient.invalidateQueries({ queryKey: getListRequestWindowsQueryKey(unitId) })
      form.reset()
      onOpened(window.state === "open"
        ? `Requests for ${monthName(window.month)} are open until the end of ${shortDay(window.closes_on)}. The unit's nurses of that month are told.`
        : `Requests for ${monthName(window.month)} open on ${shortDay(window.opens_on)}. The unit's nurses of that month are told then.`)
    },
    onError: (error) => showServerErrors(form, error),
  }))
  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-3">
      <div className="grid gap-3 sm:grid-cols-3">
        <FormField label="Month" type="month" description="A month not yet begun." registration={form.register("month")} error={form.formState.errors.month} />
        <FormField label="First day" type="date" description="Nurses are told on this day." registration={form.register("opens_on")} error={form.formState.errors.opens_on} />
        <FormField label="Last day" type="date" description="Requests are taken until its end, before the month begins." registration={form.register("closes_on")} error={form.formState.errors.closes_on} />
      </div>
      {refusal && <p role="alert" className="text-sm text-destructive">{refusal}</p>}
      {open.isError && !(open.error instanceof ApiError) && <p role="alert" className="text-sm text-destructive">{NO_CONNECTION}</p>}
      <div><Button type="submit" disabled={open.isPending}>Open the window</Button></div>
    </form>
  )
}

type Deciding = { row: UnitRequestRow; what: "accept" | "reject" | "edit" | "cancel" }

const QUESTION = {
  accept: { title: "Accept", confirm: "Accept the request" },
  reject: { title: "Reject", confirm: "Reject the request" },
  edit: { title: "Change", confirm: "Change what it promises" },
  cancel: { title: "Cancel", confirm: "Cancel the request" },
} as const

const decisionSchema = (required: boolean) => z.object({
  reason: required ? z.string().trim().min(1, "Say why: the nurse reads it with her request.").max(2000, "At most 2000 characters.")
    : z.string().max(2000, "At most 2000 characters."),
  shift: z.string(),
})

function DecisionDialog({ unitId, windowId, deciding, shifts, focusAfter, onDone, onClose }: {
  unitId: string
  windowId: string
  deciding: Deciding
  shifts: { code: string; display_name: string }[]
  focusAfter: RefObject<string | null>
  onDone: (message: string) => void
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const accept = useAcceptRequest()
  const reject = useRejectRequest()
  const edit = useEditRequest()
  const cancel = useCancelRequest()
  const { row, what } = deciding
  const mutation = { accept, reject, edit, cancel }[what]
  const required = what !== "accept"
  const form = useForm<z.infer<ReturnType<typeof decisionSchema>>>({
    resolver: zodResolver(decisionSchema(required)), defaultValues: { reason: "", shift: row.promised ?? "" },
  })
  const names = shiftNames(shifts)
  const asked = inSentence(row.shift, names)
  const whose = `${row.full_name}'s request for ${asked} on ${shortDay(row.date)}`
  const reasonError = form.formState.errors.reason
  const refusal = plainRefusal(mutation.error)

  const submit = form.handleSubmit(({ reason, shift }) => {
    const variables = { unitId, requestId: row.id }
    const callbacks = {
      onSuccess: async ({ version }: { version: number | null }) => {
        await Promise.all([
          queryClient.invalidateQueries({ queryKey: getListWindowRequestsQueryKey(unitId, windowId) }),
          queryClient.invalidateQueries({ queryKey: getListRequestWindowsQueryKey(unitId) }),
          queryClient.invalidateQueries({ queryKey: getGetRosterMonthQueryKey(unitId) }),
          queryClient.invalidateQueries({ queryKey: getListRosterVersionsQueryKey(unitId) }),
        ])
        const done = { accept: "Accepted", reject: "Rejected", edit: "Changed", cancel: "Cancelled" }[what]
        const versioned = version ? ` Version ${version} of the roster is now in force, and the unit's nurses are told it changed, not why.` : ""
        onDone(`${done} ${whose}. ${row.full_name} is told.${versioned}`)
      },
      onError: (error: unknown) => showServerErrors(form, error),
    }
    if (what === "accept") accept.mutate({ ...variables, data: { reason } }, callbacks)
    else if (what === "reject") reject.mutate({ ...variables, data: { reason } }, callbacks)
    else if (what === "edit") edit.mutate({ ...variables, data: { reason, shift: shift || null } }, callbacks)
    else cancel.mutate({ ...variables, data: { reason } }, callbacks)
  })

  const body = {
    accept: row.replaces_pin !== null
      ? `It becomes a pinned cell every solve keeps, and replaces your pin of ${inSentence(row.replaces_pin, names)} on that day.`
      : "It becomes a pinned cell every solve keeps. On an approved roster, a changed cell is a new version.",
    reject: "It stays on record, and the nurse reads your reason on My month.",
    edit: "The request keeps what she asked for; its pin holds what you choose here.",
    cancel: "Its pin ends and no cell changes: the next solve, or your own edit, may change it.",
  }[what]

  return (
    <AlertDialog open onOpenChange={(open) => { if (!open && !mutation.isPending) onClose() }}>
      <AlertDialogContent finalFocus={() => (focusAfter.current ? document.getElementById(focusAfter.current) : true)}>
        <form onSubmit={submit} noValidate className="flex flex-col gap-4">
          <AlertDialogHeader>
            <AlertDialogTitle>{QUESTION[what].title} {whose}?</AlertDialogTitle>
            <AlertDialogDescription>{body}</AlertDialogDescription>
          </AlertDialogHeader>
          {what === "edit" && (
            <SelectField label="It now promises" registration={form.register("shift")} error={form.formState.errors.shift}>
              <NativeSelectOption value="">A day off</NativeSelectOption>
              {shifts.map((shift) => <NativeSelectOption key={shift.code} value={shift.code}>{askedFor(shift.code, names)}</NativeSelectOption>)}
            </SelectField>
          )}
          <Field data-invalid={!!reasonError || undefined}>
            <FieldLabel htmlFor="decision-reason">{required ? "Why" : "Why (optional)"}</FieldLabel>
            <Textarea id="decision-reason" rows={2} aria-invalid={!!reasonError || undefined}
              aria-describedby={["decision-reason-description", reasonError && "decision-reason-error"].filter(Boolean).join(" ")}
              {...form.register("reason")} />
            <FieldDescription id="decision-reason-description">The nurse reads it with her request. It is never in a notification or the audit log.</FieldDescription>
            <FieldError id="decision-reason-error" role={undefined} errors={reasonError ? [reasonError] : undefined} />
          </Field>
          {refusal && <p role="alert" className="text-sm text-destructive">{refusal}</p>}
          {mutation.isError && !(mutation.error instanceof ApiError) && <p role="alert" className="text-sm text-destructive">{NO_CONNECTION}</p>}
          <AlertDialogFooter>
            <AlertDialogCancel type="button">Back</AlertDialogCancel>
            <Button type="submit" variant={what === "accept" || what === "edit" ? "default" : "destructive"} disabled={mutation.isPending}>
              {QUESTION[what].confirm}
            </Button>
          </AlertDialogFooter>
        </form>
      </AlertDialogContent>
    </AlertDialog>
  )
}

function Decision({ row, names }: { row: UnitRequestRow; names: Record<string, string> }) {
  const decision = lastDecision(row.steps)
  const changed = row.promised !== null && row.promised !== row.shift
  return (
    <div className="flex flex-col gap-0.5">
      <span>{UNIT_STATE[row.state]}</span>
      {changed && <span className="text-sm">Now promises {inSentence(row.promised ?? "", names)}</span>}
      {row.set_aside && <span className="text-sm">Its pin is set aside: it no longer fits the nurse's month, so no solve takes it.</span>}
      {decision?.reason && <span className="text-sm text-muted-foreground">{decision.by ? `${decision.by}: ` : ""}{decision.reason}</span>}
    </div>
  )
}

function WindowRequests({ unitId, window, onDone }: { unitId: string; window: WindowRow; onDone: (message: string) => void }) {
  const rows = useListWindowRequests(unitId, window.id)
  const catalogue = useListShifts()
  const [deciding, setDeciding] = useState<Deciding | null>(null)
  const focusAfter = useRef<string | null>(null)
  const shifts = (catalogue.data ?? []).filter((shift) => shift.active)
  const names = shiftNames(shifts)
  const month = monthName(window.month)
  const begin = (row: UnitRequestRow, what: Deciding["what"]) => {
    focusAfter.current = `decide-${what}-${row.id}`
    setDeciding({ row, what })
  }
  const button = (row: UnitRequestRow, what: Deciding["what"], label: string) => (
    <Button id={`decide-${what}-${row.id}`} variant="outline" size="sm" onClick={() => begin(row, what)}
      aria-label={`${label} ${row.full_name}'s request for ${shortDay(row.date)}`}>{label}</Button>
  )

  return (
    <section aria-labelledby="window-requests" className="flex flex-col gap-3">
      <h2 id="window-requests" className="text-base font-medium">Requests for {month}</h2>
      {rows.isPending && <p>Loading…</p>}
      {rows.isError && <p role="alert">The requests could not be read. Reload the page to try again.</p>}
      {rows.data && rows.data.length === 0 && <p>No requests were made in this window.</p>}
      {rows.data && rows.data.length > 0 && (
        <Table container={scrollableRegion(`Requests for ${month}, by date`)}>
          <TableHeader>
            <TableRow>
              <TableHead>Nurse</TableHead>
              <TableHead>Date</TableHead>
              <TableHead>Asked for</TableHead>
              <TableHead>Decision</TableHead>
              <TableHead><span className="sr-only">Actions</span></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.data.map((row) => (
              <TableRow key={row.id}>
                <TableCell>{row.full_name}</TableCell>
                <TableCell className="tabular-nums">{shortDay(row.date)}</TableCell>
                <TableCell>
                  {askedFor(row.shift, names)}
                  {herReason(row.steps) && <div className="text-sm text-muted-foreground">{herReason(row.steps)}</div>}
                </TableCell>
                <TableCell><Decision row={row} names={names} /></TableCell>
                <TableCell>
                  <div className="flex flex-wrap gap-2">
                    {row.state === "pending" && (row.accept_refusal
                      ? <span className="text-sm">Cannot be accepted: {row.accept_refusal}</span>
                      : button(row, "accept", "Accept"))}
                    {row.state === "pending" && button(row, "reject", "Reject")}
                    {row.state === "accepted" && button(row, "edit", "Change")}
                    {row.state === "accepted" && button(row, "cancel", "Cancel")}
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {deciding && (
        <DecisionDialog key={`${deciding.what}-${deciding.row.id}`} unitId={unitId} windowId={window.id} deciding={deciding} shifts={shifts} focusAfter={focusAfter}
          onClose={() => setDeciding(null)}
          onDone={(message) => {
            focusAfter.current = STATUS
            setDeciding(null)
            onDone(message)
          }} />
      )}
    </section>
  )
}

/** Read-only: nobody decides a preference. The solver keeps each live one when it can, at the CNO's price. */
function WindowPreferences({ unitId, window }: { unitId: string; window: WindowRow }) {
  const rows = useListWindowPreferences(unitId, window.id)
  const month = monthName(window.month)
  return (
    <section aria-labelledby="window-preferences" className="flex flex-col gap-3">
      <h2 id="window-preferences" className="text-base font-medium">Preferences for {month}</h2>
      <p className="text-sm text-muted-foreground">Nobody decides a preference. Every solve keeps those made when it can, at the price the CNO sets, and the draft's explanation shows what it paid.</p>
      {rows.isPending && <p>Loading…</p>}
      {rows.isError && <p role="alert">The preferences could not be read. Reload the page to try again.</p>}
      {rows.data && rows.data.length === 0 && <p>No preferences were made in this window.</p>}
      {rows.data && rows.data.length > 0 && (
        <Table container={scrollableRegion(`Preferences for ${month}, by date`)}>
          <TableHeader>
            <TableRow>
              <TableHead>Nurse</TableHead>
              <TableHead>Date</TableHead>
              <TableHead>Prefers</TableHead>
              <TableHead>State</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.data.map((row) => (
              <TableRow key={row.id}>
                <TableCell>{row.full_name}</TableCell>
                <TableCell className="tabular-nums">{shortDay(row.date)}</TableCell>
                <TableCell>{PREFERS[row.kind]}</TableCell>
                <TableCell>{PREFERENCE_STATE[row.state]}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </section>
  )
}

function chosenWindow(answer: UnitWindows, id: string | undefined, month: string | undefined): WindowRow | undefined {
  return answer.windows.find((window) => window.id === id)
    ?? answer.windows.find((window) => window.month === month)
    ?? answer.windows[0]
}

function UnitRequests({ unitId, windowId, month }: { unitId: string; windowId?: string; month?: string }) {
  const queryClient = useQueryClient()
  const windows = useListRequestWindows(unitId)
  const close = useCloseRequestWindow()
  const [said, setSaid] = useState("")
  const [closing, setClosing] = useState<WindowRow | null>(null)
  const focusAfter = useRef<string | null>(STATUS)
  const status = useRef<HTMLParagraphElement>(null)
  const answer = windows.data
  const chosen = answer && chosenWindow(answer, windowId, month)
  const tell = (message: string) => {
    setSaid(message)
    status.current?.focus()
  }

  return (
    <>
      <p id={STATUS} role="status" ref={status} tabIndex={-1} className="text-sm outline-none empty:sr-only">{said}</p>
      {windows.isPending && <p>Loading…</p>}
      {windows.isError && <p role="alert">The request windows could not be read. Reload the page to try again.</p>}
      {answer && (
        <>
          <Card>
            <CardHeader>
              <CardTitle><h2>Open a request window</h2></CardTitle>
              <CardDescription>
                Each nurse may have up to {answer.request_cap} {answer.request_cap === 1 ? "request" : "requests"} pending or accepted and {answer.preference_cap} {answer.preference_cap === 1 ? "preference" : "preferences"} a month, which you set on the <Link to="/unit-rules" className={LINK}>Unit rules</Link> page.
              </CardDescription>
            </CardHeader>
            <CardContent>
              {answer.open_refusal ? <p>{answer.open_refusal}</p> : <OpenWindowForm unitId={unitId} onOpened={tell} />}
            </CardContent>
          </Card>
          {answer.windows.length === 0 ? <p>No request window has been opened yet.</p> : (
            <section aria-labelledby="windows" className="flex flex-col gap-3">
              <h2 id="windows" className="text-base font-medium">Request windows</h2>
              <Table container={scrollableRegion("Request windows, newest first")}>
                <TableHeader>
                  <TableRow>
                    <TableHead>Month</TableHead>
                    <TableHead>Requests taken</TableHead>
                    <TableHead>State</TableHead>
                    <TableHead>Requests</TableHead>
                    <TableHead>Preferences</TableHead>
                    <TableHead><span className="sr-only">Actions</span></TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {answer.windows.map((window) => (
                    <TableRow key={window.id} aria-current={window.id === chosen?.id || undefined}>
                      <TableCell>{monthName(window.month)}</TableCell>
                      <TableCell className="tabular-nums">{shortDay(window.opens_on)} to {shortDay(window.closes_on)}</TableCell>
                      <TableCell>{WINDOW_STATE[window.state]}{window.closed_early_by ? `, early by ${window.closed_early_by}` : ""}</TableCell>
                      <TableCell className="tabular-nums">{window.requests}{window.pending > 0 ? `, ${window.pending} waiting` : ""}</TableCell>
                      <TableCell className="tabular-nums">{window.preferences}</TableCell>
                      <TableCell>
                        <div className="flex flex-wrap gap-2">
                          {window.id !== chosen?.id && (
                            <Link to="/requests" search={{ window: window.id }} className={LINK}
                              aria-label={`Show the requests for ${monthName(window.month)}, ${shortDay(window.opens_on)} to ${shortDay(window.closes_on)}`}>Show requests</Link>
                          )}
                          {window.state !== "closed" && (
                            <Button variant="outline" size="sm" onClick={() => { close.reset(); setClosing(window) }}>Close early</Button>
                          )}
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </section>
          )}
          {chosen && <WindowRequests key={chosen.id} unitId={unitId} window={chosen} onDone={tell} />}
          {chosen && <WindowPreferences key={`preferences-${chosen.id}`} unitId={unitId} window={chosen} />}
        </>
      )}
      <ConfirmDialog
        question={closing && {
          title: `Close the request window for ${monthName(closing.month)} now?`,
          body: closing.state === "open"
            ? "No more requests can be made in it. The nurses told it opened are told it closed early, and the requests made stay for your decision."
            : "It has not opened yet, so nobody has been told of it and nobody is told now.",
          confirm: "Close the window",
        }}
        pending={close.isPending} failed={close.isError} failure={plainRefusal(close.error)} focusAfter={focusAfter}
        onCancel={() => setClosing(null)}
        onConfirm={() => closing && close.mutate({ unitId, windowId: closing.id }, {
          onSuccess: async () => {
            await queryClient.invalidateQueries({ queryKey: getListRequestWindowsQueryKey(unitId) })
            const month = monthName(closing.month)
            setClosing(null)
            tell(`The request window for ${month} is closed.`)
          },
        })} />
    </>
  )
}

function RequestsPage() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  const { window, month } = Route.useSearch()
  const { units, chosen } = useChosenUnit(user, undefined)
  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4">
      <h1 className="text-xl font-medium">{chosen ? `Requests in ${chosen.name}` : "Requests"}</h1>
      <p className="text-muted-foreground">
        Open a window for a coming month, and the unit's nurses of that month may ask for a day off or a shift on its dates, and say which days they would rather have off or work. An accepted request is a pinned cell every solve keeps; a preference is kept when the solver can. Only you and the nurse read a request's reasons and her preferences.
      </p>
      {units.isPending && <p>Loading…</p>}
      {units.isError && <p role="alert">Your unit could not be read. Reload the page to try again.</p>}
      {units.data && !chosen && <p>No unit is assigned to you yet. The CNO assigns each unit its manager.</p>}
      {chosen && <UnitRequests key={chosen.id} unitId={chosen.id} windowId={window} month={month} />}
    </div>
  )
}
