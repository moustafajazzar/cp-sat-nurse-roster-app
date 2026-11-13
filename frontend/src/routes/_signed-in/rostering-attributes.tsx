import { zodResolver } from "@hookform/resolvers/zod"
import { useQueryClient } from "@tanstack/react-query"
import { createFileRoute, Link, useNavigate, useRouteContext } from "@tanstack/react-router"
import { useRef, useState } from "react"
import { useController, useForm, type FieldError, type UseFormReturn } from "react-hook-form"
import { z } from "zod"

import {
  getListNurseAttributesQueryKey,
  useGetUnitRules,
  useListNurseAttributes,
  useUpdateNurseAllowedShifts,
  useUpdateNurseAttributes,
} from "@/api/generated/endpoints"
import { Role, type AllowedShiftsBody, type AttributesBody, type NurseAttributesRow } from "@/api/generated/model"
import {
  updateNurseAttributesBodyHoursBalanceMax as HOURS_MAX,
  updateNurseAttributesBodyNightBalanceMax as BALANCE_MAX,
  updateNurseAttributesBodyOvertimeBalanceMax as OVERTIME_BALANCE_MAX,
} from "@/api/generated/zod"
import { CodeCheckboxes } from "@/components/code-checkboxes"
import { CheckboxField, FormField } from "@/components/form-field"
import { RecordsTable, SkillList } from "@/components/nurse-skills"
import { ProbationCell, useProbationQuestions, type ProbationAction } from "@/components/probation"
import { SkillsPanel } from "@/components/skill-records"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { FieldGroup, FieldLegend, FieldSet } from "@/components/ui/field"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { UnitPicker, useChosenUnit } from "@/components/unit-picker"
import { serverErrors, showServerErrors } from "@/lib/server-errors"
import { useEditing } from "@/lib/use-editing"
import { monthName, scrollableRegion } from "@/lib/utils"
import { useFocusOnOpen } from "@/lib/use-focus-on-open"
import { onlyFor } from "@/session"

const searchSchema = z.object({ unit: z.string().optional().catch(undefined) })

export const Route = createFileRoute("/_signed-in/rostering-attributes")({
  validateSearch: (search) => searchSchema.parse(search),
  beforeLoad: onlyFor(Role.unit_manager, Role.cno, Role.deputy_cno),
  staticData: { title: "Rostering attributes" },
  component: RosteringAttributesPage,
})

const GENDERS = { F: "Female", M: "Male" } as const
const ANY_SHIFT = "Any"
const ALLOWED_DESCRIPTION = "Tick none for any shift this unit works, now or later. Ticking them all would leave out a shift the unit adds later."
const COULD_NOT_SAVE = "That did not work. Check your connection and try again."

const between = (min: number, max: number) => `Between ${min} and ${max}.`
const hours = (min: number) => z.int("Enter a whole number of hours.").min(min, between(min, HOURS_MAX)).max(HOURS_MAX, between(min, HOURS_MAX))
const within = (max: number, message: string) => z.number(message).min(-max, between(-max, max)).max(max, between(-max, max))
const balance = within(BALANCE_MAX, "Enter a number.")
const allowedShifts = z.array(z.string()).nullable()

const attributesSchema = z.object({
  team_leader: z.boolean(),
  score: z.number("Enter a score from 0 to 1.").min(0, between(0, 1)).max(1, between(0, 1)),
  ramadan_reduced: z.boolean(),
  breastfeeding: z.boolean(),
  allowed_shifts: allowedShifts,
  ytd_overtime: hours(0),
  hours_balance: hours(-HOURS_MAX),
  night_balance: balance,
  friday_balance: balance,
  overtime_balance: within(OVERTIME_BALANCE_MAX, "Enter a number of hours."),
})
const dueToJoinSchema = z.object({ allowed_shifts: allowedShifts })

/** A position in the list stops at the list, which shows the error. */
const formPath = ([first]: (string | number)[]) => String(first)

function AllowedShiftsField<Form extends AllowedShiftsBody>({ form, shifts }: { form: UseFormReturn<Form>; shifts: string[] }) {
  const typed = form as unknown as UseFormReturn<AllowedShiftsBody>
  const { field } = useController({ control: typed.control, name: "allowed_shifts" })
  return (
    <CodeCheckboxes id="field-allowed_shifts" legend="Allowed shifts" description={ALLOWED_DESCRIPTION}
      options={shifts.map((code) => ({ code, label: code }))} value={field.value ?? []}
      onChange={(next) => field.onChange(next.length ? next : null)} fieldRef={field.ref}
      error={typed.formState.errors.allowed_shifts as FieldError | undefined} />
  )
}

function FormButtons({ pending, onCancel }: { pending: boolean; onCancel: () => void }) {
  return (
    <div className="flex gap-2">
      <Button type="submit" disabled={pending}>Save</Button>
      <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
    </div>
  )
}

function Unexpected({ error }: { error: unknown }) {
  return error && serverErrors(error).length === 0 ? <p role="alert" className="text-sm text-destructive">{COULD_NOT_SAVE}</p> : null
}

type FormProps = { unitId: string; nurse: NurseAttributesRow; shifts: string[]; onCancel: () => void; onDone: (message: string) => void }

function useRefreshList(unitId: string) {
  const queryClient = useQueryClient()
  return () => void queryClient.invalidateQueries({ queryKey: getListNurseAttributesQueryKey(unitId) })
}

function AttributesForm({ unitId, nurse, shifts, isOwn, onCancel, onDone }: FormProps & { isOwn: boolean }) {
  const refresh = useRefreshList(unitId)
  const update = useUpdateNurseAttributes()
  const form = useForm<AttributesBody>({
    resolver: zodResolver(attributesSchema),
    defaultValues: {
      team_leader: nurse.team_leader, score: nurse.score, ramadan_reduced: nurse.ramadan_reduced, breastfeeding: nurse.breastfeeding,
      allowed_shifts: nurse.allowed_shifts ?? null, ytd_overtime: nurse.ytd_overtime, hours_balance: nurse.hours_balance,
      night_balance: nurse.night_balance, friday_balance: nurse.friday_balance, overtime_balance: nurse.overtime_balance,
    },
  })
  const { errors } = form.formState
  const locked = nurse.opening_values_locked
  const asNumber = { valueAsNumber: true }

  useFocusOnOpen(() => { form.setFocus("team_leader") }, [form])

  const submit = form.handleSubmit((data) =>
    update.mutate({ unitId, nurseId: nurse.id, data }, {
      onSuccess: () => {
        refresh()
        onDone(`The rostering attributes of ${nurse.full_name} are saved.`)
      },
      onError: (error) => showServerErrors(form, error, formPath),
    }))

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-4">
      {isOwn && <p className="text-sm">These are your own. The CNO and the Deputy CNO can read them, and the audit log shows that you changed them.</p>}
      <FieldGroup>
        <CheckboxField label="Team leader" description="May lead a shift, which counts where the unit requires a team leader on every shift."
          registration={form.register("team_leader")} />
        <FormField label="Score" type="number" inputMode="decimal" step="any" description="Their performance score, from 0 to 1, which the solver uses to balance the strength of shifts."
          registration={form.register("score", asNumber)} error={errors.score} />
        <AllowedShiftsField form={form} shifts={shifts} />
        <CheckboxField label="Ramadan-reduced" description="Owes the hospital's Ramadan hours per day on Ramadan days."
          registration={form.register("ramadan_reduced")} />
        <CheckboxField label="Breastfeeding" description="Owes the hospital's breastfeeding reduction fewer hours on each working day."
          registration={form.register("breastfeeding")} />
      </FieldGroup>
      <FieldSet className="gap-4">
        <FieldLegend variant="legend" className="mb-0">Opening values</FieldLegend>
        <p className="text-sm text-muted-foreground">
          {locked
            ? "Where this nurse stood on the day they were added. Their first month has closed, so these can no longer change: each month now starts from the carry-over of the one before, on the Roster page."
            : "Where this nurse stands on the day they were added. Later months carry on from these, until their first month closes."}
        </p>
        <FieldGroup>
          <FormField label="Overtime this year (hours)" type="number" inputMode="numeric" description="Overtime already worked this year, which counts towards the yearly cap."
            readOnly={locked} registration={form.register("ytd_overtime", asNumber)} error={errors.ytd_overtime} />
          <FormField label="Hours balance (hours)" type="number" inputMode="numeric" description="Hours still owed from earlier months, or below zero, hours worked ahead."
            readOnly={locked} registration={form.register("hours_balance", asNumber)} error={errors.hours_balance} />
          <FormField label="Night fairness balance (shifts)" type="number" inputMode="decimal" step="any" description="Night shifts above their fair share so far this year, or below zero, under it."
            readOnly={locked} registration={form.register("night_balance", asNumber)} error={errors.night_balance} />
          <FormField label="Weekend fairness balance (shifts)" type="number" inputMode="decimal" step="any" description="Weekend days above their fair share so far this year, or below zero, under it."
            readOnly={locked} registration={form.register("friday_balance", asNumber)} error={errors.friday_balance} />
          <FormField label="Overtime above or below their overtime share (hours)" type="number" inputMode="decimal" step="any" description="Hours of overtime above their share of the unit's overtime so far this year, or below zero, under it."
            readOnly={locked} registration={form.register("overtime_balance", asNumber)} error={errors.overtime_balance} />
        </FieldGroup>
      </FieldSet>
      <Unexpected error={update.error} />
      <FormButtons pending={update.isPending} onCancel={onCancel} />
    </form>
  )
}

/** All a manager changes of a nurse due to join: this list is about this unit, and the rest stays with the unit they are in. */
function DueToJoinForm({ unitId, nurse, shifts, onCancel, onDone }: FormProps) {
  const refresh = useRefreshList(unitId)
  const update = useUpdateNurseAllowedShifts()
  const form = useForm<AllowedShiftsBody>({ resolver: zodResolver(dueToJoinSchema), defaultValues: { allowed_shifts: nurse.allowed_shifts ?? null } })

  useFocusOnOpen(() => { form.setFocus("allowed_shifts") }, [form])

  const submit = form.handleSubmit((data) =>
    update.mutate({ unitId, nurseId: nurse.id, data }, {
      onSuccess: () => {
        refresh()
        onDone(`The allowed shifts of ${nurse.full_name} are saved.`)
      },
      onError: (error) => showServerErrors(form, error, formPath),
    }))

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-4">
      <p className="text-sm">{nurse.full_name} joins from {monthName(nurse.joins_from!)}. Until then, the manager of the unit they are in sets everything else.</p>
      <AllowedShiftsField form={form} shifts={shifts} />
      <Unexpected error={update.error} />
      <FormButtons pending={update.isPending} onCancel={onCancel} />
    </form>
  )
}

const yesNo = (on: boolean) => (on ? "Yes" : "No")

function Status({ nurse }: { nurse: NurseAttributesRow }) {
  return (
    <div className="flex flex-col items-start gap-1">
      {!nurse.is_active && <Badge variant="outline">Deactivated</Badge>}
      {nurse.joins_from && <span className="text-muted-foreground">Joins from {monthName(nurse.joins_from)}</span>}
      {nurse.leaves_from && <span className="text-muted-foreground">Leaves from {monthName(nurse.leaves_from)}</span>}
      {nurse.is_active && !nurse.joins_from && !nurse.leaves_from && <span className="text-muted-foreground">In the unit</span>}
    </div>
  )
}

const editButtonId = (nurse: NurseAttributesRow) => `attributes-${nurse.id}`

function NurseTable({ nurses, unitName, mayChange, mayRecord, onEdit, onRecord, onAskProbation }: {
  nurses: NurseAttributesRow[]
  unitName: string
  mayChange: boolean
  mayRecord: boolean
  onEdit: (nurse: NurseAttributesRow, button: HTMLElement) => void
  onRecord: (nurse: NurseAttributesRow, button: HTMLElement) => void
  onAskProbation: (action: ProbationAction, nurse: NurseAttributesRow) => void
}) {
  // A region a keyboard scrolls only when nothing in it takes focus: a row's buttons scroll it otherwise.
  const hasActions = mayChange || mayRecord
  return (
    <Table container={hasActions ? undefined : scrollableRegion(`Rostering attributes of ${unitName}`)}>
      <TableHeader>
        <TableRow>
          <TableHead>Name</TableHead>
          {/* Beside the name, since a row this long would otherwise put its one action out of reach on a phone. */}
          {hasActions && <TableHead><span className="sr-only">Actions</span></TableHead>}
          <TableHead>Hospital ID</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>On probation</TableHead>
          <TableHead>Gender</TableHead>
          <TableHead>Team leader</TableHead>
          <TableHead>Score</TableHead>
          <TableHead>Allowed shifts</TableHead>
          <TableHead>Ramadan-reduced</TableHead>
          <TableHead>Breastfeeding</TableHead>
          <TableHead>Overtime this year</TableHead>
          <TableHead>Hours balance</TableHead>
          <TableHead>Night balance</TableHead>
          <TableHead>Weekend balance</TableHead>
          <TableHead>Overtime balance</TableHead>
          <TableHead>Skills</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {nurses.map((nurse) => (
          <TableRow key={nurse.id}>
            <TableCell>{nurse.full_name}</TableCell>
            {mayRecord && (
              <TableCell>
                <Button variant="outline" size="sm" aria-label={`Skills of ${nurse.full_name}`}
                  onClick={(event) => onRecord(nurse, event.currentTarget)}>
                  Skills
                </Button>
              </TableCell>
            )}
            {mayChange && (
              <TableCell>
                <Button id={editButtonId(nurse)} variant="outline" size="sm" aria-label={`${nurse.joins_from ? "Set allowed shifts of" : "Set attributes of"} ${nurse.full_name}`}
                  onClick={(event) => onEdit(nurse, event.currentTarget)}>
                  {nurse.joins_from ? "Set allowed shifts" : "Set attributes"}
                </Button>
              </TableCell>
            )}
            <TableCell translate="no">{nurse.hospital_id}</TableCell>
            <TableCell><Status nurse={nurse} /></TableCell>
            <TableCell><ProbationCell nurse={nurse} mayChange={mayChange} onAsk={onAskProbation} /></TableCell>
            <TableCell>{GENDERS[nurse.gender]}</TableCell>
            <TableCell>{yesNo(nurse.team_leader)}</TableCell>
            <TableCell className="tabular-nums">{nurse.score}</TableCell>
            <TableCell translate="no">
              <div className="flex flex-col items-start gap-1">
                {nurse.allowed_shifts?.join(", ") ?? ANY_SHIFT}
                {nurse.allowed_shifts_to_check && <Badge variant="outline">To check</Badge>}
              </div>
            </TableCell>
            <TableCell>{yesNo(nurse.ramadan_reduced)}</TableCell>
            <TableCell>{yesNo(nurse.breastfeeding)}</TableCell>
            <TableCell className="tabular-nums">{nurse.ytd_overtime}</TableCell>
            <TableCell className="tabular-nums">{nurse.hours_balance}</TableCell>
            <TableCell className="tabular-nums">{nurse.night_balance}</TableCell>
            <TableCell className="tabular-nums">{nurse.friday_balance}</TableCell>
            <TableCell className="tabular-nums">{nurse.overtime_balance}</TableCell>
            <TableCell>
              {/* The API gives the details to the CNO and the Deputy CNO only; the unit's manager reads names and expiries. */}
              {!nurse.skills.some((skill) => skill.details)
                ? <SkillList skills={nurse.skills} />
                : <RecordsTable skills={nurse.skills} label={`Skills of ${nurse.full_name}`} details scrolls={false} />}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

/** The Deputy CNO's record, renew and take off, in a panel above the list as the CNO's are on the Nurses page:
 *  a form in a row as wide as this table would scroll away on a phone. Reads her records from the list as it refreshes. */
function RecordsPanel({ nurse, refresh, onSaid, onClose }: {
  nurse: NurseAttributesRow; refresh: () => void; onSaid: (message: string) => void; onClose: () => void
}) {
  const heading = useRef<HTMLHeadingElement>(null)
  useFocusOnOpen(() => heading.current?.focus(), [])
  return (
    <Card className="max-w-xl">
      <CardHeader>
        <CardTitle><h2 ref={heading} tabIndex={-1} className="outline-none">Skills of {nurse.full_name}</h2></CardTitle>
        <CardDescription translate="no">{nurse.hospital_id}</CardDescription>
      </CardHeader>
      <CardContent>
        <SkillsPanel nurse={nurse} refresh={refresh} keepsCatalogue={false} onSaid={onSaid} onClose={onClose} />
      </CardContent>
    </Card>
  )
}

function ChosenUnitAttributes({ unit, mayChange, mayRecord, username }: {
  unit: { id: string; name: string }; mayChange: boolean; mayRecord: boolean; username: string
}) {
  const nurses = useListNurseAttributes(unit.id)
  const rules = useGetUnitRules(unit.id)
  const refresh = useRefreshList(unit.id)
  const { editing, start, finish } = useEditing<NurseAttributesRow>()
  const skillsPanel = useEditing<NurseAttributesRow>()
  // A second panel, the Deputy CNO's, beside the manager's form above: each role has one of the two.
  const recording = skillsPanel.editing && (nurses.data?.find((nurse) => nurse.id === skillsPanel.editing?.id) ?? skillsPanel.editing)
  const [done, setDone] = useState("")
  const state = rules.data
  const shifts = state?.rules.shifts ?? []
  const toCheck = (nurses.data ?? []).filter((nurse) => nurse.allowed_shifts_to_check)
  const finished = (message: string) => { setDone(message); finish() }
  // After an answer, focus goes to her row's own button, as the action's may be gone. A manager's only, as the actions are.
  const questions = useProbationQuestions(unit.id, editButtonId, setDone)

  return (
    <>
      {(nurses.isPending || rules.isPending) && <p>Loading…</p>}
      {(nurses.isError || rules.isError) && <p role="alert">The attributes could not be read. Reload the page to try again.</p>}
      {/* Always rendered, because a live region mounted with its text may never be read out. */}
      <p role="status" className="text-sm text-muted-foreground empty:sr-only">{done}</p>
      {mayChange && state && shifts.length === 0 && (
        <p>This unit has no shifts yet, so no shift can be allowed. <Link to="/unit-rules" className="underline underline-offset-3">Choose them under Unit rules</Link></p>
      )}
      {mayChange && toCheck.length > 0 && (
        <p>
          {toCheck.map((nurse) => nurse.full_name).join(", ")} came with allowed shifts this unit does not work, so {toCheck.length === 1 ? "they" : "each"} may
          now work any of its shifts. Check that this is right.
        </p>
      )}
      {editing && state && (
        <Card className="max-w-xl">
          <CardHeader>
            <CardTitle><h2>{editing.full_name}</h2></CardTitle>
            <CardDescription translate="no">{editing.hospital_id}</CardDescription>
          </CardHeader>
          <CardContent>
            {editing.joins_from
              ? <DueToJoinForm key={editing.id} unitId={unit.id} nurse={editing} shifts={shifts} onCancel={finish} onDone={finished} />
              : <AttributesForm key={editing.id} unitId={unit.id} nurse={editing} shifts={shifts} isOwn={editing.hospital_id === username} onCancel={finish} onDone={finished} />}
          </CardContent>
        </Card>
      )}
      {recording && (
        <RecordsPanel key={recording.id} nurse={recording} refresh={refresh} onSaid={setDone} onClose={skillsPanel.finish} />
      )}
      {mayChange && questions.dialogs}
      {nurses.data && state && (
        <Card>
          <CardContent>
            {nurses.data.length === 0
              ? <p>{unit.name} has no nurses yet. The CNO adds them.</p>
              : <NurseTable nurses={nurses.data} unitName={unit.name} mayChange={mayChange} mayRecord={mayRecord}
                  onEdit={(nurse, button) => { setDone(""); start(nurse, button) }}
                  onRecord={(nurse, button) => { setDone(""); skillsPanel.start(nurse, button) }}
                  onAskProbation={(action, nurse) => { setDone(""); questions.ask(action, nurse) }} />}
          </CardContent>
        </Card>
      )}
    </>
  )
}

function RosteringAttributesPage() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  const { unit: inAddress } = Route.useSearch()
  const navigate = useNavigate({ from: Route.fullPath })
  const { units, chosen } = useChosenUnit(user, inAddress)
  const mayChange = user.role === Role.unit_manager
  // The Deputy CNO's own flag, never `mayChange`: the probation's actions are the unit manager's alone.
  const mayRecord = user.role === Role.deputy_cno

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-4">
      <h1 className="text-xl font-medium">{mayChange && chosen ? `Rostering attributes of ${chosen.name}` : "Rostering attributes"}</h1>
      <p className="text-muted-foreground">
        What the solver needs to know about each nurse, and where each one stands when they are added.
        {user.role === Role.cno && " Only the unit's manager can change these. You record skills, certificates and licences on the Nurses page, and the Deputy CNO records them here."}
        {mayRecord && " Only the unit's manager can change these, but for skills, certificates and licences, which you record here and the CNO on the Nurses page."}
        {mayChange && " Gender and whether a nurse is on probation are the CNO's to set, and skills the CNO's and the Deputy CNO's to record; you set how long a probation lasts. Of all this, nurses see only their own skills."}
      </p>
      {units.isPending && <p>Loading…</p>}
      {units.isError && <p role="alert">The units could not be read. Reload the page to try again.</p>}
      {units.data && !chosen && <p>{mayChange ? "No unit is assigned to you yet. The CNO assigns each unit its manager." : "There are no units yet."}</p>}
      {!mayChange && units.data && chosen && (
        <UnitPicker units={units.data} value={chosen.id} onChange={(unit) => void navigate({ search: { unit } })} />
      )}
      {chosen && <ChosenUnitAttributes key={chosen.id} unit={chosen} mayChange={mayChange} mayRecord={mayRecord} username={user.username} />}
    </div>
  )
}
