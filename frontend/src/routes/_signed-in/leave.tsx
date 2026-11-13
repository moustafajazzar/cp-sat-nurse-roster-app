import { zodResolver } from "@hookform/resolvers/zod"
import { keepPreviousData, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, Link, useNavigate, useRouteContext } from "@tanstack/react-router"
import { useEffect, useRef, useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import {
  getGetRosterMonthQueryKey,
  getListRosterVersionsQueryKey,
  getListUnitLeaveQueryKey,
  useChangeLeave,
  useListLeaveTypes,
  useListUnitLeave,
  useRecordLeave,
  useRemoveLeave,
} from "@/api/generated/endpoints"
import { Role, type LeaveNurse, type LeaveRun, type RemovedPin, type SavedLeave, type ShiftTakenOff, type UnitLeaveMonth } from "@/api/generated/model"
import { ConfirmDialog } from "@/components/confirm-dialog"
import { FormField, SelectField } from "@/components/form-field"
import { MONTH_LINK, monthInAddress, MonthNav } from "@/components/month-nav"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { UnitPicker, useChosenUnit } from "@/components/unit-picker"
import { plainRefusal, serverErrors, showServerErrors } from "@/lib/server-errors"
import { useEditing } from "@/lib/use-editing"
import { dayRange, monthAfter, monthName, scrollableRegion } from "@/lib/utils"
import { useFocusOnOpen } from "@/lib/use-focus-on-open"
import { onlyFor } from "@/session"

const searchSchema = z.object({
  unit: z.string().optional().catch(undefined),
  month: monthInAddress,
  nurse: z.string().optional().catch(undefined),
})

export const Route = createFileRoute("/_signed-in/leave")({
  validateSearch: (search) => searchSchema.parse(search),
  beforeLoad: onlyFor(Role.unit_manager, Role.cno, Role.deputy_cno),
  staticData: { title: "Leave" },
  component: LeavePage,
})

const RECORD_BUTTON = "record-leave"

type LeaveSearch = z.infer<typeof searchSchema>

const formSchema = z.object({
  nurse_id: z.string().min(1, "Choose a nurse."),
  leave_type_id: z.string().min(1, "Choose a leave type."),
  first: z.iso.date("Enter the first day."),
  last: z.iso.date("Enter a date, or leave this empty for one day.").or(z.literal("")),
})
type FormValues = z.infer<typeof formSchema>

function LeaveForm({ unitId, nurses, run, defaultNurse, onCancel, onDone }: {
  unitId: string
  nurses: LeaveNurse[]
  /** The leave being changed, or nothing for new leave. */
  run: LeaveRun | null
  defaultNurse: string | undefined
  onCancel: () => void
  onDone: (message: string, first: string, taken: ShiftTakenOff[]) => void
}) {
  const queryClient = useQueryClient()
  const types = useListLeaveTypes()
  const record = useRecordLeave()
  const change = useChangeLeave()
  const mutation = run ? change : record
  const form = useForm<FormValues>({
    resolver: zodResolver(formSchema),
    defaultValues: {
      nurse_id: run?.nurse.id ?? defaultNurse ?? "",
      leave_type_id: run?.leave_type.id ?? "",
      first: run?.first ?? "",
      last: run && run.last !== run.first ? run.last : "",
    },
  })
  const { errors } = form.formState
  // Only an active type may be chosen, but leave of a type since retired may keep it.
  const choices = (types.data ?? []).filter((type) => type.active || type.id === run?.leave_type.id)

  useFocusOnOpen(() => { form.setFocus(run ? "leave_type_id" : "nurse_id") }, [form, run])
  // The select has no options until the types arrive, so a value set before then would not show.
  useEffect(() => { if (run && types.data) form.setValue("leave_type_id", run.leave_type.id) }, [form, run, types.data])

  const submit = form.handleSubmit(({ nurse_id, leave_type_id, first, last }) => {
    const dates = { leave_type_id, first, last: last || first }
    const name = run?.nurse.full_name ?? nurses.find((nurse) => nurse.id === nurse_id)?.full_name
    const callbacks = {
      onSuccess: (saved: SavedLeave) => {
        void queryClient.invalidateQueries({ queryKey: getListUnitLeaveQueryKey(unitId) })
        if (saved.shifts_taken_off.length > 0) {
          void queryClient.invalidateQueries({ queryKey: getGetRosterMonthQueryKey(unitId) })
          void queryClient.invalidateQueries({ queryKey: getListRosterVersionsQueryKey(unitId) })
        }
        onDone(`The leave of ${name} is saved: ${dayRange(first, last || first)}.${pinsReplaced(saved.pins_removed)}${shiftsTakenOff(saved.shifts_taken_off)}`, first, saved.shifts_taken_off)
      },
      onError: (error: unknown) => showServerErrors(form, error),
    }
    if (run) change.mutate({ unitId, data: { ...dates, leave_ids: run.leave_ids } }, callbacks)
    else record.mutate({ unitId, data: { ...dates, nurse_id } }, callbacks)
  })

  const refused = mutation.isError && serverErrors(mutation.error).length === 0
  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-4">
      <FieldGroup>
        {!run && (
          <SelectField label="Nurse" registration={form.register("nurse_id")} error={errors.nurse_id}>
            <NativeSelectOption value="">Choose a nurse</NativeSelectOption>
            {nurses.map((nurse) => <NativeSelectOption key={nurse.id} value={nurse.id}>{nurse.full_name}</NativeSelectOption>)}
          </SelectField>
        )}
        <SelectField label="Leave type" registration={form.register("leave_type_id")} error={errors.leave_type_id}
          description={types.isError ? "The leave types could not be read. Reload the page to try again." : undefined}>
          <NativeSelectOption value="">Choose a type</NativeSelectOption>
          {choices.map((type) => <NativeSelectOption key={type.id} value={type.id}>{type.active ? type.name : `${type.name} (retired)`}</NativeSelectOption>)}
        </SelectField>
        <FormField label="First day" type="date" registration={form.register("first")} error={errors.first} />
        <FormField label="Last day" type="date" description="Leave this empty for one day." registration={form.register("last")} error={errors.last} />
      </FieldGroup>
      {refused && <p role="alert" className="text-sm text-destructive">{plainRefusal(mutation.error) ?? "That did not work. Check your connection and try again."}</p>}
      <div className="flex gap-2">
        <Button type="submit" disabled={mutation.isPending}>Save</Button>
        <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
      </div>
    </form>
  )
}

// A row's actions do not stay in view on a narrow screen, as on the holidays page: two buttons would cover the dates. Like
// the other lists, this one has not moved to TanStack Table yet. Those who only read have no buttons, and in one nurse's
// month no links, so a keyboard scrolls the table through its region.
function LeaveTable({ runs, showNurse, label, nurseSearch, actions }: {
  runs: LeaveRun[]
  showNurse: boolean
  label: string
  nurseSearch: (nurse: string) => LeaveSearch
  /** What a manager can do to each leave. Those who only read have none. */
  actions: {
    onChange: (run: LeaveRun, button: HTMLElement) => void
    onRemove: (run: LeaveRun) => void
  } | null
}) {
  return (
    <Table container={actions ? undefined : scrollableRegion(label)}>
      <TableHeader>
        <TableRow>
          {showNurse && <TableHead>Nurse</TableHead>}
          <TableHead>Dates</TableHead>
          <TableHead>Days</TableHead>
          <TableHead>Type</TableHead>
          {actions && <TableHead><span className="sr-only">Actions</span></TableHead>}
        </TableRow>
      </TableHeader>
      <TableBody>
        {runs.map((run) => {
          const what = `leave of ${run.nurse.full_name}, ${dayRange(run.first, run.last)}`
          return (
            <TableRow key={run.leave_ids[0]}>
              {showNurse && (
                <TableCell>
                  <Link to="/leave" search={nurseSearch(run.nurse.id)} className="underline underline-offset-3">{run.nurse.full_name}</Link>
                </TableCell>
              )}
              <TableCell>
                <div className="flex flex-col items-start gap-1">
                  <span className="tabular-nums">{dayRange(run.first, run.last)}</span>
                  {run.outside_dates && <Badge variant="outline">Outside their dates</Badge>}
                </div>
              </TableCell>
              <TableCell className="tabular-nums">{run.days}</TableCell>
              <TableCell>{run.leave_type.name}</TableCell>
              {actions && (
                <TableCell>
                  <div className="flex gap-2">
                    <Button variant="outline" size="sm" aria-label={`Change ${what}`} onClick={(event) => actions.onChange(run, event.currentTarget)}>Change</Button>
                    <Button variant="outline" size="sm" aria-label={`Remove ${what}`} onClick={() => actions.onRemove(run)}>Remove</Button>
                  </div>
                </TableCell>
              )}
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
  )
}

type Panel = { run: LeaveRun | null }

/** Leave wins over a shift on an approved roster too, so saving it took any such shift off as a new version. */
function shiftsTakenOff(taken: ShiftTakenOff[]): string {
  if (taken.length === 0) return ""
  const each = taken.map((day) => `${day.shift} on ${dayRange(day.date, day.date)}`).join(", ")
  const versions = [...new Set(taken.map((day) => day.version))].join(" and ")
  const opened = taken.filter((day) => day.cover).length
  const covers = opened === 0 ? ""
    : opened === 1 ? " It opened a cover for one more nurse on that shift, not offered to anyone yet: it is linked below."
      : ` It opened ${opened} covers for one more nurse on those shifts, not offered to anyone yet: they are linked below.`
  return ` It took ${taken.length === 1 ? "a shift" : `${taken.length} shifts`} off the approved roster: ${each}. Version ${versions} is now in force, and the unit's nurses are told the roster has changed, not why.${covers}`
}

/** Each cover the leave opened, to offer from the Covers page. Apart from the status, which a link inside would not help. */
function CoversOpened({ taken }: { taken: ShiftTakenOff[] }) {
  const opened = taken.filter((day) => day.cover)
  if (opened.length === 0) return null
  return (
    <ul aria-label="Covers the leave opened" className="flex flex-col gap-1 text-sm">
      {opened.map((day) => (
        <li key={day.cover}>
          <Link to="/unit-covers" search={{ month: day.date.slice(0, 7), cover: day.cover! }} className="underline underline-offset-3">
            Cover for {day.shift} on {dayRange(day.date, day.date)}
          </Link>
        </li>
      ))}
    </ul>
  )
}

/** Where a replaced pin was decides what follows: a draft still holds its shift, a submitted roster keeps it for the decision,
 * on the roster in force the leave took it off as a version, which `shiftsTakenOff` then says, and a month with no roster yet
 * has nothing to change. In this order. */
const PIN_PLACES: Record<RemovedPin["roster"], { where: string; then: string }> = {
  draft: { where: "on the draft", then: " A shift the draft still holds on a day of leave can be taken off on the Roster page, or solve again." },
  submitted: { where: "on the submitted roster", then: " The submitted roster keeps its shift until the decision, which checks it again." },
  approved: { where: "on the roster in force", then: "" },
  none: { where: "in a month with no roster yet", then: "" },
}

/** Leave wins over a pin, so saving it ended any on its days, and the Roster page goes on saying so. */
function pinsReplaced(pins: RemovedPin[]): string {
  if (pins.length === 0) return ""
  const places = Object.entries(PIN_PLACES).map(([roster, place]) => ({ ...place, pins: pins.filter((pin) => pin.roster === roster) }))
    .filter((place) => place.pins.length > 0)
  const said = places.map(({ where, pins: here }) => {
    const each = here.map((pin) => `${pin.shift || "a day off"} on ${dayRange(pin.date, pin.date)}`).join(", ")
    return ` It replaced ${here.length === 1 ? "a pin" : `${here.length} pins`} ${where}: ${each}.`
  }).join("")
  const requests = pins.filter((pin) => pin.origin === "request")
  const told = requests.length === 0 ? ""
    : requests.length === 1 ? " One was an accepted request, which stays on record as replaced by leave; its nurse is told."
    : ` ${requests.length} were accepted requests, which stay on record as replaced by leave; their nurses are told.`
  return `${said}${told}${places.map((place) => place.then).join("")}`
}

function UnitLeave({ unit, unitInAddress, month: inAddress, nurseId, mayChange }: {
  unit: { id: string; name: string }
  unitInAddress: string | undefined
  month: string | undefined
  nurseId: string | undefined
  mayChange: boolean
}) {
  const queryClient = useQueryClient()
  const navigate = useNavigate({ from: Route.fullPath })
  const leave = useListUnitLeave(unit.id, inAddress ? { month: inAddress } : undefined, { query: { placeholderData: keepPreviousData } })
  const remove = useRemoveLeave()
  const { editing, start, finish, drop } = useEditing<Panel>()
  const [removing, setRemoving] = useState<LeaveRun | null>(null)
  const [said, setSaid] = useState<{ message: string; taken: ShiftTakenOff[] }>({ message: "", taken: [] })
  const done = said.message
  const setDone = (message: string, taken: ShiftTakenOff[] = []) => setSaid({ message, taken })
  const focusAfter = useRef<string | null>(null)

  const answer: UnitLeaveMonth | undefined = leave.data
  const chosenNurse = answer?.nurses.find((nurse) => nurse.id === nurseId)
  const runs = (answer?.runs ?? []).filter((run) => !chosenNurse || run.nurse.id === chosenNurse.id)
  const days = runs.reduce((total, run) => total + run.days, 0)
  const searchFor = (month: string | undefined, nurse: string | undefined): LeaveSearch => ({ unit: unitInAddress, month, nurse })

  const confirmRemoval = () => {
    if (!removing) return
    const { full_name } = removing.nurse
    const when = dayRange(removing.first, removing.last)
    remove.mutate({ unitId: unit.id, data: { leave_ids: removing.leave_ids } }, {
      onSuccess: () => {
        void queryClient.invalidateQueries({ queryKey: getListUnitLeaveQueryKey(unit.id) })
        focusAfter.current = RECORD_BUTTON
        setRemoving(null)
        setDone(`The leave of ${full_name} is removed: ${when}.`)
      },
    })
  }

  return (
    <>
      {leave.isPending && <p>Loading…</p>}
      {leave.isError && <p role="alert">The leave could not be read. Reload the page to try again.</p>}
      {/* Always rendered, because a live region mounted with its text may never be read out. */}
      <p role="status" className="text-sm text-muted-foreground empty:sr-only">{done}</p>
      <CoversOpened taken={said.taken} />
      {answer && (
        <>
          <MonthNav month={answer.month} link={(month, label) => <Link to="/leave" search={searchFor(month, nurseId)} className={MONTH_LINK}>{label}</Link>} />
          <div className="flex flex-wrap items-end gap-3">
            <Field className="max-w-xs">
              <FieldLabel htmlFor="chosen-nurse">Show</FieldLabel>
              <NativeSelect id="chosen-nurse" value={chosenNurse?.id ?? ""}
                onChange={(event) => void navigate({ search: searchFor(answer.month, event.target.value || undefined) })}>
                <NativeSelectOption value="">Everyone</NativeSelectOption>
                {answer.nurses.map((nurse) => <NativeSelectOption key={nurse.id} value={nurse.id}>{nurse.full_name}</NativeSelectOption>)}
              </NativeSelect>
            </Field>
            {mayChange && <Button id={RECORD_BUTTON} onClick={(event) => { setDone(""); remove.reset(); start({ run: null }, event.currentTarget) }}>Record leave</Button>}
          </div>
          {editing && (
            <Card className="max-w-xl">
              <CardHeader>
                <CardTitle><h3>{editing.run ? `Change leave of ${editing.run.nurse.full_name}` : "Record leave"}</h3></CardTitle>
                <CardDescription>Every day in between counts. Only you, the nurse, the CNO and the Deputy CNO can see the type.</CardDescription>
              </CardHeader>
              <CardContent>
                <LeaveForm key={editing.run?.leave_ids[0] ?? "new"} unitId={unit.id} nurses={answer.nurses} run={editing.run}
                  defaultNurse={chosenNurse?.id} onCancel={finish}
                  onDone={(message, first, taken) => {
                    setDone(message, taken)
                    if (editing.run) {
                      // The row and its button may be gone, so focus waits on the button that stays.
                      drop()
                      document.getElementById(RECORD_BUTTON)?.focus()
                    } else finish()
                    const saved = first.slice(0, 7)
                    if (saved !== answer.month) void navigate({ search: searchFor(saved, nurseId) })
                  }} />
              </CardContent>
            </Card>
          )}
          {answer.closed === "closed" && (
            <p className="text-sm">{monthName(answer.month)} has closed. Leave recorded or changed here is taken in when {mayChange ? "you close it again on the Roster page" : "its manager closes it again"}, which can be done until {monthName(monthAfter(answer.month, 1))} closes.</p>
          )}
          {answer.closed === "fixed" && (
            <p className="text-sm">{monthName(answer.month)} has closed, and so has the month after it, so its leave can no longer change.</p>
          )}
          {nurseId && !chosenNurse && (
            <p>The nurse you chose is not in {unit.name} in {monthName(answer.month)}, so this shows everyone.</p>
          )}
          {chosenNurse && (
            <p>{chosenNurse.full_name} has {days === 1 ? "1 day" : `${days} days`} of leave in {monthName(answer.month)}.</p>
          )}
          {!chosenNurse && runs.length === 0 && (
            <p>{answer.nurses.length === 0 ? `${unit.name} has no nurses in ${monthName(answer.month)}.` : `No leave is recorded in ${monthName(answer.month)}.`}</p>
          )}
          {runs.length > 0 && (
            <Card>
              <CardContent>
                <LeaveTable runs={runs} showNurse={!chosenNurse} label={`Leave in ${unit.name}, ${monthName(answer.month)}`}
                  nurseSearch={(nurse) => searchFor(answer.month, nurse)}
                  actions={mayChange ? {
                    onChange: (run, button) => { setDone(""); start({ run }, button) },
                    onRemove: (run) => { setDone(""); remove.reset(); focusAfter.current = null; setRemoving(run) },
                  } : null} />
              </CardContent>
            </Card>
          )}
          {runs.some((run) => run.outside_dates) && (
            <p className="text-sm text-muted-foreground">A correction to the nurse's start or end date left the days marked Outside their dates beyond them, where they count for nothing. {mayChange ? "Remove them" : "The unit's manager removes them"} unless the date is corrected back.</p>
          )}
        </>
      )}
      <ConfirmDialog
        question={removing && {
          title: `Remove this leave of ${removing.nurse.full_name}?`,
          body: `${dayRange(removing.first, removing.last)}, ${removing.leave_type.name}. These days count as ordinary days again.`,
          confirm: "Remove leave",
        }}
        pending={remove.isPending} failed={remove.isError} failure={plainRefusal(remove.error)} focusAfter={focusAfter}
        onConfirm={confirmRemoval} onCancel={() => setRemoving(null)} />
    </>
  )
}

function LeavePage() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  const { unit: unitInAddress, month, nurse } = Route.useSearch()
  const navigate = useNavigate({ from: Route.fullPath })
  const { units, chosen } = useChosenUnit(user, unitInAddress)
  const mayChange = user.role === Role.unit_manager

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4">
      <h1 className="text-xl font-medium">{mayChange && chosen ? `Leave in ${chosen.name}` : "Leave"}</h1>
      <p className="text-muted-foreground">
        {mayChange
          ? "The leave of your unit's nurses, month by month. A month in another unit is that unit's manager's. Only you, the nurse, the CNO and the Deputy CNO can see a leave's type: colleagues see a day off."
          : "The leave of each unit's nurses, month by month. Only the unit's manager can change it. A leave's type says something about a person, so colleagues see only a day off."}
      </p>
      {units.isPending && <p>Loading…</p>}
      {units.isError && <p role="alert">{mayChange ? "Your unit" : "The units"} could not be read. Reload the page to try again.</p>}
      {units.data && !chosen && (mayChange ? (
        <p>
          No unit is assigned to you yet. The CNO assigns each unit its manager. If you are on a roster yourself, your own leave and hours are under{" "}
          <Link to="/my-month" className="underline underline-offset-3">My month</Link>.
        </p>
      ) : <p>There are no units yet.</p>)}
      {!mayChange && units.data && chosen && (
        // A nurse is in one unit a month, so the one chosen does not follow to the next unit. The month does.
        <UnitPicker units={units.data} value={chosen.id} onChange={(unit) => void navigate({ search: { unit, month } })} />
      )}
      {chosen && <UnitLeave key={chosen.id} unit={chosen} unitInAddress={unitInAddress} month={month} nurseId={nurse} mayChange={mayChange} />}
    </div>
  )
}
