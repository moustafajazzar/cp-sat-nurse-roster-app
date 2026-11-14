import { zodResolver } from "@hookform/resolvers/zod"
import { useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useRouteContext } from "@tanstack/react-router"
import { useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import {
  getGetHospitalRulesQueryKey,
  getListShiftsQueryKey,
  useCreateShift,
  useListShifts,
  useUpdateShift,
} from "@/api/generated/endpoints"
import { Role, type ShiftBody, type ShiftSchema } from "@/api/generated/model"
import {
  createShiftBodyCodeRegExp as CODE,
  createShiftBodyColourRegExp as COLOUR,
  createShiftBodyDisplayNameMax as NAME_MAX,
  createShiftBodyHoursMax as HOURS_MAX,
  createShiftBodyStartTimeRegExp as CLOCK_TIME,
} from "@/api/generated/zod"
import { CheckboxField, FormField } from "@/components/form-field"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { FieldGroup } from "@/components/ui/field"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { serverErrors, showServerErrors } from "@/lib/server-errors"
import { useEditing } from "@/lib/use-editing"
import { cn, readableOn, scrollableRegion } from "@/lib/utils"
import { useFocusOnOpen } from "@/lib/use-focus-on-open"
import { FormLayoutFrame } from "@/components/form-layout-prototype"

/** Open to every role, which may all read the catalogue. Only the CNO gets the form. */
export const Route = createFileRoute("/_signed-in/shifts")({
  validateSearch: (search) => z.object({ variant: z.enum(["a", "b", "c"]).optional().catch(undefined) }).parse(search),
  staticData: { title: "Shifts" },
  component: ShiftsPage,
})

const HOURS_MIN = 1
const hoursMessage = `Enter whole hours from ${HOURS_MIN} to ${HOURS_MAX}.`

const schema = z.object({
  code: z.string().trim().regex(CODE, "Use up to 8 letters, digits, dashes or underscores, starting with a letter or digit."),
  display_name: z.string().trim().min(1, "Enter a name.").max(NAME_MAX, `At most ${NAME_MAX} characters.`),
  hours: z.int(hoursMessage).min(HOURS_MIN, hoursMessage).max(HOURS_MAX, hoursMessage),
  start_time: z.string().regex(CLOCK_TIME, "Enter a time."),
  end_time: z.string().regex(CLOCK_TIME, "Enter a time."),
  colour: z.string().regex(COLOUR, "Choose a colour."),
  active: z.boolean(),
})

const NEW_SHIFT = { code: "", display_name: "", start_time: "", end_time: "", colour: "#64748b", active: true } as ShiftBody

function ShiftForm({ shift, onDone, onCancel }: { shift: ShiftSchema | null; onDone: (message: string) => void; onCancel: () => void }) {
  const queryClient = useQueryClient()
  const create = useCreateShift()
  const update = useUpdateShift()
  const mutation = shift ? update : create
  const form = useForm<ShiftBody>({ resolver: zodResolver(schema), defaultValues: shift ?? NEW_SHIFT })
  const { errors } = form.formState

  useFocusOnOpen(() => {
    if (shift) form.setFocus("code")
  }, [shift, form])

  const saved = (message: string) => {
    void queryClient.invalidateQueries({ queryKey: getListShiftsQueryKey() })
    // A new code changes the pairs saved in the hospital rules, and what those may name.
    void queryClient.invalidateQueries({ queryKey: getGetHospitalRulesQueryKey() })
    onDone(message)
  }
  const refused = (error: unknown) => showServerErrors(form, error)

  const submit = form.handleSubmit((data) => {
    if (shift) {
      update.mutate({ shiftId: shift.id, data }, { onSuccess: (row) => saved(`${row.code} is saved.`), onError: refused })
    } else {
      create.mutate({ data }, {
        onSuccess: (row) => {
          form.reset(NEW_SHIFT)
          saved(`${row.code} is added.`)
        },
        onError: refused,
      })
    }
  })
  const unexpected = mutation.isError && serverErrors(mutation.error).length === 0

  return (
    <form onSubmit={submit} noValidate>
      <FieldGroup>
        <FormField label="Code" autoComplete="off" spellCheck={false} autoCapitalize="none" translate="no"
          description={shift
            ? "What rosters and the solver call this shift. A new code is recorded so nurses' histories can follow it, and the forbidden successions change with it."
            : "What rosters and the solver call this shift."}
          registration={form.register("code")} error={errors.code} />
        <FormField label="Display name" autoComplete="off" registration={form.register("display_name")} error={errors.display_name} />
        <FormField label="Hours" type="number" inputMode="numeric" description="The hours this shift counts for a nurse."
          registration={form.register("hours", { valueAsNumber: true })} error={errors.hours} />
        <div className="grid gap-4 sm:grid-cols-2">
          <FormField label="Start time" type="time" registration={form.register("start_time")} error={errors.start_time} />
          <FormField label="End time" type="time" description="Earlier than the start when the shift runs past midnight."
            registration={form.register("end_time")} error={errors.end_time} />
        </div>
        {/* Given its value here because a colour input keeps its own black over the form's default. */}
        <FormField label="Colour" type="color" className="w-16 p-1" value={form.watch("colour")}
          registration={form.register("colour")} error={errors.colour} />
        <CheckboxField label="Active" description="Turn this off to retire the shift. It stays on this list, and it can be made active again."
          registration={form.register("active")} error={errors.active} />
        {unexpected && <p role="alert" className="text-sm text-destructive">The shift could not be saved. Check your connection and try again.</p>}
        <div className="flex gap-2">
          <Button type="submit" disabled={mutation.isPending}>{shift ? "Save shift" : "Add shift"}</Button>
          {shift && <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>}
        </div>
      </FieldGroup>
    </form>
  )
}

function FormerCodes({ shift }: { shift: ShiftSchema }) {
  if (shift.renames.length === 0) return null
  return (
    <ul className="text-xs text-muted-foreground">
      {shift.renames.map((rename) => (
        <li key={rename.at}>Was <span translate="no">{rename.old_code}</span> until {rename.at.slice(0, 10)}</li>
      ))}
    </ul>
  )
}

function ShiftsTable({ shifts, onEdit }: { shifts: ShiftSchema[]; onEdit?: (shift: ShiftSchema, button: HTMLElement) => void }) {
  return (
    // Nothing else in a reader's table can take focus, and a keyboard still has to scroll it.
    <Table container={onEdit ? undefined : scrollableRegion("Shifts")}>
      <TableHeader>
        <TableRow>
          <TableHead>Code</TableHead>
          <TableHead>Name</TableHead>
          <TableHead>Hours</TableHead>
          <TableHead>Time</TableHead>
          <TableHead>Colour</TableHead>
          <TableHead>Status</TableHead>
          {onEdit && <TableHead className="sticky end-0 bg-card"><span className="sr-only">Actions</span></TableHead>}
        </TableRow>
      </TableHeader>
      <TableBody>
        {shifts.map((shift) => (
          <TableRow key={shift.id}>
            <TableCell>
              <span translate="no" className="font-medium">{shift.code}</span>
              <FormerCodes shift={shift} />
            </TableCell>
            <TableCell>{shift.display_name}</TableCell>
            <TableCell className="tabular-nums">{shift.hours}</TableCell>
            <TableCell className="tabular-nums">{shift.start_time} to {shift.end_time}</TableCell>
            <TableCell>
              <span className="flex items-center gap-2">
                <span aria-hidden="true" className="size-4 shrink-0 rounded-sm border" style={{ backgroundColor: shift.colour }} />
                <span translate="no">{shift.colour}</span>
              </span>
            </TableCell>
            <TableCell>
              <Badge variant={shift.active ? "secondary" : "outline"}>{shift.active ? "Active" : "Retired"}</Badge>
            </TableCell>
            {onEdit && (
              <TableCell className="sticky end-0 bg-card text-end">
                <Button variant="outline" size="sm" aria-label={`Edit ${shift.code}`} onClick={(event) => onEdit(shift, event.currentTarget)}>Edit</Button>
              </TableCell>
            )}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function ShiftsPage() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  const shifts = useListShifts()
  const { editing, start, finish } = useEditing<ShiftSchema>()
  const [done, setDone] = useState("")
  const mayChange = user.role === Role.cno
  // PROTOTYPE (forms pattern): ?variant=a|b|c lays the catalogue out three ways.
  const variant = (Route.useSearch() as { variant?: "a" | "b" | "c" }).variant
  if (variant && mayChange && shifts.data) return (
    <FormLayoutFrame variant={variant} sections={[]}>
      <ShiftsPrototype variant={variant} shifts={shifts.data} editing={editing} start={start} finish={finish} done={done} setDone={setDone} />
    </FormLayoutFrame>
  )

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4">
      <h1 className="text-xl font-medium">Shifts</h1>
      <p className="text-muted-foreground">
        Every shift a unit may work. Times are hospital time.
        {!mayChange && " Only the CNO can change these."}
      </p>
      {shifts.isPending && <p>Loading…</p>}
      {shifts.isError && <p role="alert">The shifts could not be read. Reload the page to try again.</p>}
      {/* Always rendered, because a live region mounted with its text may never be read out. */}
      <p role="status" className="text-sm text-muted-foreground empty:sr-only">{done}</p>
      {shifts.data && (
        <Card>
          <CardContent>
            <ShiftsTable shifts={shifts.data} onEdit={mayChange ? (shift, button) => { setDone(""); start(shift, button) } : undefined} />
          </CardContent>
        </Card>
      )}
      {mayChange && shifts.data && (
        <Card className="max-w-xl">
          <CardHeader><CardTitle><h2>{editing ? <>Edit <span translate="no">{editing.code}</span></> : "Add a shift"}</h2></CardTitle></CardHeader>
          <CardContent>
            <ShiftForm key={editing?.id ?? "new"} shift={editing} onCancel={finish}
              onDone={(message) => { setDone(message); finish() }} />
          </CardContent>
        </Card>
      )}
    </div>
  )
}

/* PROTOTYPE (forms pattern): the Shifts catalogue three ways. A: the table, the form below it. B: Edit opens the form in a side
   drawer. C: the list beside the chosen shift's detail. */
function ShiftsPrototype({ variant, shifts, editing, start, finish, done, setDone }: {
  variant: "a" | "b" | "c"; shifts: ShiftSchema[]; editing: ShiftSchema | null; start: (shift: ShiftSchema, button: HTMLElement) => void
  finish: () => void; done: string; setDone: (message: string) => void
}) {
  const [adding, setAdding] = useState(false)
  const open = editing !== null || adding
  const close = () => { setAdding(false); finish() }
  const form = (
    <ShiftForm key={editing?.id ?? "new"} shift={editing} onCancel={close} onDone={(message) => { setDone(message); close() }} />
  )
  const title = editing ? <>Edit <span translate="no">{editing.code}</span></> : "Add a shift"
  const head = (
    <div className="flex flex-wrap items-center gap-3">
      <h1 className="text-xl font-semibold">Shifts</h1><span className="text-sm text-muted-foreground">Every shift a unit may work. Times are hospital time.</span>
      <span className="flex-1" /><Button onClick={() => { finish(); setAdding(true) }}>Add a shift</Button>
    </div>
  )
  const status = <p role="status" className="text-sm text-primary empty:sr-only">{done}</p>
  if (variant === "c") {
    const chosen = editing
    return (
      <div className="flex flex-col gap-4">
        {head}{status}
        <div className="grid gap-4 md:grid-cols-[18rem_minmax(0,1fr)]">
          <ul className={cn("flex flex-col divide-y rounded-lg border bg-[var(--ward-surface)]", open && "max-md:hidden")}>
            {shifts.map((shift) => (
              <li key={shift.id}><button type="button" onClick={(event) => { setAdding(false); start(shift, event.currentTarget) }}
                className={cn("flex w-full items-center gap-3 px-3 py-2.5 text-start hover:bg-muted", chosen?.id === shift.id && "bg-accent")}>
                <span className="grid size-8 place-items-center rounded-[5px] text-sm font-bold" ref={(el) => { if (el) { el.style.backgroundColor = shift.colour; el.style.color = readableOn(shift.colour) } }}>{shift.code}</span>
                <span className="min-w-0 flex-1"><span className="block font-medium">{shift.display_name}</span><span className="block text-xs text-muted-foreground tabular-nums">{shift.start_time}–{shift.end_time} · {shift.hours} h</span></span>
                {!shift.active && <span className="text-xs text-muted-foreground">Retired</span>}
              </button></li>
            ))}
          </ul>
          <section className={cn("rounded-lg border bg-[var(--ward-surface)] p-4", !open && "max-md:hidden")}>
            {open ? <><button type="button" onClick={close} className="mb-2 text-sm font-medium text-primary md:hidden">‹ All shifts</button><h2 className="mb-3 text-base font-semibold">{title}</h2>{form}</>
              : <p className="text-sm text-muted-foreground">Choose a shift to see and change it, or add one.</p>}
          </section>
        </div>
      </div>
    )
  }
  return (
    <div className="flex flex-col gap-4">
      {head}{status}
      <div className="rounded-lg border bg-[var(--ward-surface)] p-2"><ShiftsTable shifts={shifts} onEdit={(shift, button) => { setDone(""); setAdding(false); start(shift, button) }} /></div>
      {variant === "a" && open && <section className="max-w-xl rounded-lg border bg-[var(--ward-surface)] p-4"><h2 className="mb-3 text-base font-semibold">{title}</h2>{form}</section>}
      {variant === "b" && open && (
        <div className="fixed inset-0 z-40 flex justify-end bg-black/30" onClick={close}>
          <aside aria-label="Shift" onClick={(event) => event.stopPropagation()} className="h-full w-[28rem] max-w-full overflow-y-auto border-s bg-[var(--ward-surface)] p-5 shadow-xl">
            <div className="mb-4 flex items-center"><h2 className="text-base font-semibold">{title}</h2><span className="flex-1" /><Button variant="outline" size="sm" onClick={close}>Close</Button></div>
            {form}
          </aside>
        </div>
      )}
    </div>
  )
}
