import { zodResolver } from "@hookform/resolvers/zod"
import { useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useNavigate, useRouteContext } from "@tanstack/react-router"
import { Fragment, useEffect, useRef, useState } from "react"
import { useController, useFieldArray, useForm, type FieldError, type UseFormReturn } from "react-hook-form"
import { z } from "zod"

import { getGetUnitRulesQueryKey, getListUnitsQueryKey, useGetUnitRules, useUpdateUnitRules } from "@/api/generated/endpoints"
import { Role, type HeadcountRule, type ShiftChoice, type SkillNeedRule, type UnitRulesAnswer, type UnitRulesBody, type UnitRulesState } from "@/api/generated/model"
import {
  updateUnitRulesBodyPreferenceCapMax as PREFERENCE_CAP_MAX,
  updateUnitRulesBodyRequestCapMax as REQUEST_CAP_MAX,
  updateUnitRulesBodyRulesCarryCapMax as PARAM_MAX,
  updateUnitRulesBodyRulesHeadcountsItemWeekdayMinMax as HEADCOUNT_MAX,
  updateUnitRulesBodyRulesMaxFullRunsMax as FULL_RUNS_MAX,
  updateUnitRulesBodyRulesNightRestDaysMax as REST_DAYS_MAX,
  updateUnitRulesBodyRulesOvertimeToleranceMax as OVERTIME_HOURS_MAX,
  updateUnitRulesBodyRulesShiftsMax as SHIFTS_MAX,
  updateUnitRulesBodyRulesSkillNeedsMax as SKILL_NEEDS_MAX,
} from "@/api/generated/zod"
import { CodeCheckboxes } from "@/components/code-checkboxes"
import { CheckboxField, FormField, SelectField } from "@/components/form-field"
import { onOff, ReadOnlyList, Section } from "@/components/rules-section"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { FieldGroup } from "@/components/ui/field"
import { NativeSelectOption } from "@/components/ui/native-select"
import { UnitPicker, useChosenUnit } from "@/components/unit-picker"
import { serverErrors, showServerErrors } from "@/lib/server-errors"
import {
  type NumberRule,
  type SwitchRule,
  SHIFTS_DESCRIPTION,
  NIGHT_AND_DAY,
  HEADCOUNT_SWITCH,
  HEADCOUNTS_DESCRIPTION,
  WHO_SWITCHES,
  SKILL_NEEDS_DESCRIPTION,
  SKILL_NEEDS_SWITCH,
  needKey,
  NEED_COUNTS,
  SCORE_TARGETS,
  SCORE_SWITCH,
  SCORE_MODES,
  SCORE_MODE_DESCRIPTION,
  SCORE_CEILINGS,
  NIGHT_REST_DAYS,
  NIGHT_REST_SWITCH,
  LONGEST_RUN,
  FULL_RUNS,
  AFTER_TWO_RUNS,
  fullRunsApply,
  MIXED_SWITCH,
  FAIR_SHIFTS_DESCRIPTION,
  FAIR_TOLERANCE,
  WEEKEND_SWITCH,
  OVERTIME_SWITCH,
  OVERTIME_TOLERANCE,
  CARRY_SWITCH,
  CARRY_CAP,
  OVERTIME_CARRY_CAP,
  LIMITS,
  SWITCHES_HARD,
  REQUEST_CAP_DESCRIPTION,
  PREFERENCE_CAP_DESCRIPTION,
  TIME_LIMIT_DESCRIPTION,
  COUNT_FIELDS,
  UNIT_RULE_LABELS,
} from "@/lib/unit-rules-words"
import type { Explained } from "@/lib/weights"
import { onlyFor } from "@/session"

const searchSchema = z.object({ unit: z.string().optional().catch(undefined) })

export const Route = createFileRoute("/_signed-in/unit-rules")({
  validateSearch: (search) => searchSchema.parse(search),
  beforeLoad: onlyFor(Role.unit_manager, Role.cno, Role.deputy_cno),
  staticData: { title: "Unit rules" },
  component: UnitRulesPage,
})


const wholeNumber = (min: number, max: number) => z.int("Enter a whole number.").min(min, `Between ${min} and ${max}.`).max(max, `Between ${min} and ${max}.`)
const number = (max: number) => z.number("Enter a number.").min(0, `Between 0 and ${max}.`).max(max, `Between 0 and ${max}.`)
const count = wholeNumber(0, PARAM_MAX)
const scoreLevel = number(1)
const shiftCount = number(PARAM_MAX)
const people = wholeNumber(0, HEADCOUNT_MAX)
const fullRuns = wholeNumber(0, FULL_RUNS_MAX)
// The tolerance's and the carry's limit alike: the solver bounds both by the hours of a 31-day month (MAX_OVERTIME_HOURS).
const overtimeHours = wholeNumber(0, OVERTIME_HOURS_MAX)

function schemaFor(timeLimitCap: number) {
  return z
    .object({
      rules: z.object({
        shifts: z.array(z.string()).min(1, "Choose at least one shift.").max(SHIFTS_MAX, `At most ${SHIFTS_MAX} shifts.`),
        night_shift: z.string().min(1, "Choose the night shift."),
        day_shift: z.string().min(1, "Choose the day shift."),
        headcounts: z.array(z.object({
          shift: z.string(),
          weekday_min: people,
          weekday_max: people,
          weekend_min: people,
          weekend_max: people,
          also_counts: z.array(z.string()),
        }).superRefine((row, context) => {
          if (row.weekday_max < row.weekday_min) context.addIssue({ code: "custom", path: ["weekday_max"], message: "Cannot be below the weekday minimum." })
          if (row.weekend_max < row.weekend_min) context.addIssue({ code: "custom", path: ["weekend_max"], message: "Cannot be below the weekend minimum." })
        })),
        headcount_enabled: z.boolean(),
        require_team_leader: z.boolean(),
        require_both_genders: z.boolean(),
        score_target: scoreLevel,
        score_target_weekend: scoreLevel,
        score_balance: z.boolean(),
        score_balance_mode: z.enum(["band", "spread"]),
        score_band_hi: scoreLevel,
        score_band_hi_weekend: scoreLevel,
        night_rest_days: wholeNumber(0, REST_DAYS_MAX),
        night_rest_hard: z.boolean(),
        fair_shifts: z.array(z.string()),
        fair_tolerance: shiftCount,
        fair_weekends: z.boolean(),
        carry_fairness: z.boolean(),
        carry_cap: shiftCount,
        share_overtime: z.boolean(),
        overtime_tolerance: overtimeHours,
        overtime_carry_cap: overtimeHours,
        max_consecutive_shifts: count,
        // Checked below, and only while the field shows: a hidden field cannot show its error. `withFullRunsKept` sends the
        // saved value instead of one left wrong behind it.
        max_full_runs: z.union([z.number(), z.nan()]),
        double_cycle_rest: count,
        max_weekends: count,
        no_mixed_blocks: z.boolean(),
        max_switches: count,
        switches_hard: z.boolean(),
        skill_needs: z.array(z.object({ shift: z.string(), skill: z.string(), weekday_min: people, weekend_min: people }))
          .max(SKILL_NEEDS_MAX, `At most ${SKILL_NEEDS_MAX} skill needs.`),
        skill_needs_hard: z.boolean(),
      }).superRefine((rules, context) => {
        // A shift is taken out only once no need names it, as the API asks: the manager takes the need out first.
        const named = rules.skill_needs.filter((need) => !rules.shifts.includes(need.shift)).map((need) => `${need.skill} on ${need.shift}`)
        if (named.length > 0)
          context.addIssue({ code: "custom", path: ["shifts"], message: `A skill need names a shift you are taking out: ${named.join("; ")}. Take the need out first.` })
        const pairs = new Set<string>()
        rules.skill_needs.forEach((need, index) => {
          const pair = needKey(need)
          if (pairs.has(pair)) context.addIssue({ code: "custom", path: ["skill_needs", index, "skill"], message: `${need.shift} already has a need for ${need.skill}.` })
          pairs.add(pair)
        })
        if (fullRunsApply(rules.max_consecutive_shifts)) {
          const checked = fullRuns.safeParse(rules.max_full_runs)
          if (!checked.success) context.addIssue({ code: "custom", path: ["max_full_runs"], message: checked.error.issues[0].message })
        }
        if (rules.night_shift && rules.night_shift === rules.day_shift)
          context.addIssue({ code: "custom", path: ["day_shift"], message: "Cannot be the night shift too." })
        if (rules.score_balance && rules.score_balance_mode === "band") {
          if (rules.score_target > rules.score_band_hi)
            context.addIssue({ code: "custom", path: ["score_band_hi"], message: "Cannot be below the weekday score target." })
          if (rules.score_target_weekend > rules.score_band_hi_weekend)
            context.addIssue({ code: "custom", path: ["score_band_hi_weekend"], message: "Cannot be below the weekend score target." })
        }
      }),
      request_cap: wholeNumber(0, REQUEST_CAP_MAX),
      preference_cap: wholeNumber(0, PREFERENCE_CAP_MAX),
      default_time_limit: wholeNumber(1, timeLimitCap),
    })
}

/** "rules.shifts.2" stops at the list, which shows the error, and a headcount's shift has no input of its own. */
function formPath(loc: (string | number)[]): string {
  const [first, second, index, field] = loc
  if (second === "shifts" || second === "fair_shifts") return `${first}.${second}`
  if (second === "headcounts" && field === "shift") return `${first}.shifts`
  if (second === "headcounts" && field === "also_counts") return `${first}.${second}.${index}.${field}`
  return loc.join(".")
}

const shiftLabel = ({ code, display_name }: ShiftChoice) => `${code}, ${display_name}`

/** The cap keeps its value while hidden, but one typed wrong and then hidden by lowering the longest run is no value: the
 *  saved one goes instead. */
function withFullRunsKept(data: UnitRulesBody, saved: UnitRulesState): UnitRulesBody {
  if (fullRuns.safeParse(data.rules.max_full_runs).success) return data
  return { ...data, rules: { ...data.rules, max_full_runs: saved.rules.max_full_runs } }
}

const NEW_HEADCOUNT = { weekday_min: 1, weekday_max: 1, weekend_min: 1, weekend_max: 1, also_counts: [] }

/** The first shift and skill with no need yet, so that a new row is not refused as a second need. */
function nextNeed(needs: SkillNeedRule[], shifts: string[], skills: string[]): SkillNeedRule | undefined {
  const taken = new Set(needs.map(needKey))
  for (const skill of skills)
    for (const shift of shifts)
      if (!taken.has(needKey({ shift, skill }))) return { shift, skill, weekday_min: 1, weekend_min: 1 }
  return undefined
}

function SkillNeedsField({ form, skills }: { form: UseFormReturn<UnitRulesBody>; skills: string[] }) {
  const { fields, append, remove } = useFieldArray({ control: form.control, name: "rules.skill_needs" })
  const needs = form.watch("rules.skill_needs")
  const working = form.watch("rules.shifts")
  const add = useRef<HTMLButtonElement>(null)
  const list = useRef<HTMLUListElement>(null)
  const [focus, setFocus] = useState<"none" | "last" | "add">("none")
  const errors = form.formState.errors.rules?.skill_needs

  // After the list has drawn what the button just changed, so focus never falls back to the page.
  useEffect(() => {
    if (focus === "add") add.current?.focus()
    if (focus === "last") list.current?.querySelector<HTMLElement>(":scope > li:last-child select")?.focus()
    if (focus !== "none") setFocus("none")
  }, [focus])

  const next = nextNeed(needs, working, skills)
  // A value outside the choices still shows as itself, so that a need is never shown as something it is not.
  const options = (values: string[], current: string) => (values.includes(current) ? values : [current, ...values])

  return (
    <div className="flex flex-col gap-4">
      {fields.length === 0 && <p className="text-sm text-muted-foreground">No skill needs. The solver staffs every shift without looking at skills.</p>}
      <ul ref={list} className="flex flex-col gap-4">
        {fields.map((row, index) => {
          const need = needs[index] ?? row
          return (
            // By the field array's id: a row's inputs hold their values, and removing another moves its position.
            <li key={row.id}>
              <fieldset className="flex flex-col gap-3 border-t pt-4">
                <legend className="float-left mb-3 w-full text-sm font-medium">Skill need {index + 1}</legend>
                <div className="grid gap-3 sm:grid-cols-2">
                  <SelectField label="Shift" registration={form.register(`rules.skill_needs.${index}.shift`)} error={errors?.[index]?.shift}>
                    {options(working, need.shift).map((code) => <NativeSelectOption key={code} value={code}>{code}</NativeSelectOption>)}
                  </SelectField>
                  <SelectField label="Skill" registration={form.register(`rules.skill_needs.${index}.skill`)} error={errors?.[index]?.skill}>
                    {options(skills, need.skill).map((name) => <NativeSelectOption key={name} value={name}>{name}</NativeSelectOption>)}
                  </SelectField>
                  {NEED_COUNTS.map(({ name, label }) => (
                    <FormField key={name} label={label} type="number" inputMode="numeric"
                      registration={form.register(`rules.skill_needs.${index}.${name}`, { valueAsNumber: true })} error={errors?.[index]?.[name]} />
                  ))}
                </div>
                <Button type="button" variant="ghost" className="self-start" aria-label={`Remove skill need ${index + 1}`}
                  onClick={() => {
                    remove(index)
                    setFocus(fields.length === 1 || index === fields.length - 1 ? "add" : "none")
                  }}>
                  Remove this need
                </Button>
              </fieldset>
            </li>
          )
        })}
      </ul>
      {(errors?.root?.message ?? errors?.message) && <p className="text-sm text-destructive">{errors?.root?.message ?? errors?.message}</p>}
      {skills.length === 0
        ? <p className="text-sm text-muted-foreground">The skill catalogue has no skills yet. The CNO adds them.</p>
        : working.length === 0 && <p className="text-sm text-muted-foreground">Choose the unit's shifts before adding a need.</p>}
      <Button ref={add} type="button" variant="outline" className="self-start" disabled={!next || fields.length >= SKILL_NEEDS_MAX}
        onClick={() => {
          if (!next) return
          append(next)
          setFocus("last")
        }}>
        Add a skill need
      </Button>
    </div>
  )
}

function ShiftsField({ form, shifts: active }: { form: UseFormReturn<UnitRulesBody>; shifts: ShiftChoice[] }) {
  const { field } = useController({ control: form.control, name: "rules.shifts" })
  /** A shift the unit stops working goes from everywhere that may name only the unit's shifts. The night and day shifts
   *  stay, because the choice of another is the manager's and their fields then say so. */
  const change = (next: string[]) => {
    const gone = (code: string) => !next.includes(code)
    field.onChange(next)
    form.setValue("rules.headcounts", form.getValues("rules.headcounts")
      .filter((row) => !gone(row.shift))
      .map((row) => ({ ...row, also_counts: row.also_counts.filter((code) => !gone(code)) })), { shouldDirty: true })
    form.setValue("rules.fair_shifts", form.getValues("rules.fair_shifts").filter((code) => !gone(code)), { shouldDirty: true })
  }
  return (
    <CodeCheckboxes id="field-rules.shifts" legend={UNIT_RULE_LABELS.shifts} description={SHIFTS_DESCRIPTION}
      options={active.map((shift) => ({ code: shift.code, label: `${shiftLabel(shift)}, ${shift.hours} hours` }))}
      value={field.value} onChange={change} error={form.formState.errors.rules?.shifts as FieldError | undefined} fieldRef={field.ref} />
  )
}

function CodesField({ form, name, ...rest }: { form: UseFormReturn<UnitRulesBody>; name: `rules.headcounts.${number}.also_counts` | "rules.fair_shifts"; legend: string; description: string; options: { code: string; label: string }[]; error?: FieldError }) {
  const { field } = useController({ control: form.control, name })
  return <CodeCheckboxes id={`field-${name}`} value={field.value} onChange={field.onChange} fieldRef={field.ref} {...rest} />
}

function HeadcountsField({ form, shifts: active }: { form: UseFormReturn<UnitRulesBody>; shifts: ShiftChoice[] }) {
  const { fields, append, remove } = useFieldArray({ control: form.control, name: "rules.headcounts" })
  const rows = form.watch("rules.headcounts")
  const working = form.watch("rules.shifts")
  const blocks = useRef(new Map<string, HTMLElement>())
  const [focusIn, setFocusIn] = useState<string | null>(null)
  const label = (code: string) => {
    const shift = active.find((candidate) => candidate.code === code)
    return shift ? shiftLabel(shift) : code
  }

  // After the block has drawn what the button just changed, so focus never falls back to the page.
  useEffect(() => {
    if (focusIn) blocks.current.get(focusIn)?.querySelector<HTMLElement>("input, button")?.focus()
    if (focusIn) setFocusIn(null)
  }, [focusIn])

  if (working.length === 0) return <p className="text-sm text-muted-foreground">Choose the unit's shifts first.</p>
  return (
    <div className="flex flex-col gap-6">
      {working.map((code) => {
        const index = rows.findIndex((row) => row.shift === code)
        const rowErrors = index >= 0 ? form.formState.errors.rules?.headcounts?.[index] : undefined
        return (
          <section key={code} ref={(element) => { if (element) blocks.current.set(code, element); else blocks.current.delete(code) }}
            aria-label={label(code)} className="flex flex-col gap-3 border-t pt-4 first:border-t-0 first:pt-0">
            <h3 className="text-sm font-medium">{label(code)}</h3>
            {index < 0 ? (
              <>
                <p className="text-sm text-muted-foreground">No headcount of its own, so the team leader, gender, score and fairness rules leave it alone.</p>
                <Button type="button" variant="outline" className="self-start" aria-label={`Add a headcount for ${code}`}
                  onClick={() => { append({ shift: code, ...NEW_HEADCOUNT }); setFocusIn(code) }}>
                  Add a headcount
                </Button>
              </>
            ) : (
              // By the field array's id: a row's inputs hold their values, and removing another moves its position.
              <Fragment key={fields[index]?.id}>
                <div className="grid gap-3 sm:grid-cols-2">
                  {COUNT_FIELDS.map(({ name, label: countLabel }) => (
                    <FormField key={name} label={countLabel} type="number" inputMode="numeric"
                      registration={form.register(`rules.headcounts.${index}.${name}`, { valueAsNumber: true })} error={rowErrors?.[name]} />
                  ))}
                </div>
                {working.length > 1 && (
                  <CodesField form={form} name={`rules.headcounts.${index}.also_counts`} legend="Shifts that also count towards it"
                    description="For overlapping shifts: a nurse on one of these counts towards this shift's numbers."
                    options={working.filter((other) => other !== code).map((other) => ({ code: other, label: other }))}
                    error={rowErrors?.also_counts as FieldError | undefined} />
                )}
                <Button type="button" variant="ghost" className="self-start" aria-label={`Remove the headcount of ${code}`}
                  onClick={() => {
                    remove(index)
                    form.setValue("rules.fair_shifts", form.getValues("rules.fair_shifts").filter((other) => other !== code), { shouldDirty: true })
                    setFocusIn(code)
                  }}>
                  Remove this headcount
                </Button>
              </Fragment>
            )}
          </section>
        )
      })}
    </div>
  )
}

function RulesForm({ unitId, state }: { unitId: string; state: UnitRulesState }) {
  const queryClient = useQueryClient()
  const update = useUpdateUnitRules()
  const valuesOf = (from: UnitRulesState): UnitRulesBody => ({
    rules: from.rules as UnitRulesBody["rules"], request_cap: from.request_cap, preference_cap: from.preference_cap, default_time_limit: from.default_time_limit,
  })
  const form = useForm<UnitRulesBody>({ resolver: zodResolver(schemaFor(state.time_limit_cap)), defaultValues: valuesOf(state) })
  const { errors, isDirty } = form.formState
  const ruleErrors = errors.rules
  const asNumber = { valueAsNumber: true }
  const working = form.watch("rules.shifts")
  const rows = form.watch("rules.headcounts") as HeadcountRule[]
  const withHeadcount = working.filter((code) => rows.some((row) => row.shift === code))
  const longest = form.watch("rules.max_consecutive_shifts")

  const numberField = ({ name, label, description }: Explained<NumberRule>, step?: string) => (
    <FormField key={name} label={label} type="number" inputMode={step ? "decimal" : "numeric"} step={step} description={description}
      registration={form.register(`rules.${name}`, asNumber)} error={ruleErrors?.[name]} />
  )
  const switchField = ({ name, label, description }: Explained<SwitchRule>) => (
    <CheckboxField key={name} label={label} description={description} registration={form.register(`rules.${name}`)} />
  )

  const submit = form.handleSubmit((data) =>
    update.mutate(
      { unitId, data: withFullRunsKept(data, state) },
      {
        onSuccess: (saved) => {
          queryClient.setQueryData(getGetUnitRulesQueryKey(unitId), saved)
          void queryClient.invalidateQueries({ queryKey: getListUnitsQueryKey() })
          form.reset(valuesOf(saved))
        },
        onError: (error) => showServerErrors(form, error, formPath),
      },
    ),
  )
  const refused = update.isError && serverErrors(update.error, formPath).length > 0

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-4">
      <Section title="Shifts">
        <FieldGroup>
          <ShiftsField form={form} shifts={state.active_shifts} />
          {NIGHT_AND_DAY.map(({ name, label, description }) => (
            <SelectField key={name} label={label} description={description} registration={form.register(`rules.${name}`)} error={ruleErrors?.[name]}>
              <NativeSelectOption value="">Choose</NativeSelectOption>
              {state.active_shifts.map((shift) => <NativeSelectOption key={shift.code} value={shift.code}>{shiftLabel(shift)}</NativeSelectOption>)}
            </SelectField>
          ))}
        </FieldGroup>
      </Section>
      <Section title="Headcounts" description={HEADCOUNTS_DESCRIPTION}>
        <FieldGroup>
          {switchField(HEADCOUNT_SWITCH)}
          <HeadcountsField form={form} shifts={state.active_shifts} />
        </FieldGroup>
      </Section>
      <Section title="Who a shift needs">
        <FieldGroup>{WHO_SWITCHES.map(switchField)}</FieldGroup>
      </Section>
      <Section title="Skill needs" description={SKILL_NEEDS_DESCRIPTION}>
        <FieldGroup>
          {switchField(SKILL_NEEDS_SWITCH)}
          <SkillNeedsField form={form} skills={state.active_skills} />
        </FieldGroup>
      </Section>
      <Section title="Score targets">
        <FieldGroup>
          {SCORE_TARGETS.map((rule) => numberField(rule, "any"))}
          {switchField(SCORE_SWITCH)}
          <SelectField label={UNIT_RULE_LABELS.score_balance_mode} description={SCORE_MODE_DESCRIPTION} registration={form.register("rules.score_balance_mode")} error={ruleErrors?.score_balance_mode}>
            {Object.entries(SCORE_MODES).map(([mode, label]) => <NativeSelectOption key={mode} value={mode}>{label}</NativeSelectOption>)}
          </SelectField>
          {SCORE_CEILINGS.map((rule) => numberField(rule, "any"))}
        </FieldGroup>
      </Section>
      <Section title="Night rest">
        <FieldGroup>
          {numberField(NIGHT_REST_DAYS)}
          {switchField(NIGHT_REST_SWITCH)}
        </FieldGroup>
      </Section>
      <Section title="Runs of shifts">
        <FieldGroup>
          {numberField(LONGEST_RUN)}
          {fullRunsApply(longest) && numberField(FULL_RUNS)}
          {numberField(AFTER_TWO_RUNS)}
          {switchField(MIXED_SWITCH)}
        </FieldGroup>
      </Section>
      <Section title="Fairness">
        <FieldGroup>
          <CodesField form={form} name="rules.fair_shifts" legend={UNIT_RULE_LABELS.fair_shifts} description={FAIR_SHIFTS_DESCRIPTION}
            options={withHeadcount.map((code) => ({ code, label: code }))} error={ruleErrors?.fair_shifts as FieldError | undefined} />
          {numberField(FAIR_TOLERANCE, "any")}
          {switchField(WEEKEND_SWITCH)}
          {switchField(OVERTIME_SWITCH)}
          {numberField(OVERTIME_TOLERANCE)}
          {switchField(CARRY_SWITCH)}
          {numberField(CARRY_CAP, "any")}
          {numberField(OVERTIME_CARRY_CAP)}
        </FieldGroup>
      </Section>
      <Section title="Weekends and switches">
        <FieldGroup>
          {LIMITS.map((rule) => numberField(rule))}
          {switchField(SWITCHES_HARD)}
        </FieldGroup>
      </Section>
      <Section title="Requests and solves">
        <FieldGroup>
          <FormField label="Requests per nurse per month" type="number" inputMode="numeric" description={REQUEST_CAP_DESCRIPTION}
            registration={form.register("request_cap", asNumber)} error={errors.request_cap} />
          <FormField label="Preferences per nurse per month" type="number" inputMode="numeric" description={PREFERENCE_CAP_DESCRIPTION}
            registration={form.register("preference_cap", asNumber)} error={errors.preference_cap} />
          <FormField label="Default time limit (seconds)" type="number" inputMode="numeric"
            description={`${TIME_LIMIT_DESCRIPTION} The System Admin's cap is ${state.time_limit_cap} seconds.`}
            registration={form.register("default_time_limit", asNumber)} error={errors.default_time_limit} />
        </FieldGroup>
      </Section>
      {update.isError && (
        <p role="alert" className="text-sm text-destructive">
          {refused ? "Some values were refused. Correct the fields marked above and save again." : "The rules could not be saved. Check your connection and save again."}
        </p>
      )}
      {/* Always rendered, because a live region mounted with its text may never be read out. */}
      <p role="status" className="text-sm text-muted-foreground empty:sr-only">
        {update.isSuccess && !isDirty ? "Saved." : ""}
      </p>
      <Button type="submit" className="self-start" disabled={update.isPending || !(isDirty || state.needs_review)}>Save</Button>
    </form>
  )
}

const named = (codes: string[], none: string) => (codes.length ? codes.join(", ") : none)
const notChosen = "Not chosen"

function HeadcountsReadOnly({ rules }: { rules: UnitRulesAnswer }) {
  if (rules.headcounts.length === 0) return <p className="text-sm">No shift has a headcount.</p>
  return (
    <ul className="flex flex-col gap-3 text-sm">
      {rules.headcounts.map((row) => (
        <li key={row.shift} className="tabular-nums">
          <span className="font-medium">{row.shift}</span>: {row.weekday_min} to {row.weekday_max} on a weekday, {row.weekend_min} to {row.weekend_max} on a weekend day
          {row.also_counts.length > 0 && `, with ${row.also_counts.join(", ")} counting towards it`}
        </li>
      ))}
    </ul>
  )
}

function SkillNeedsReadOnly({ rules }: { rules: UnitRulesAnswer }) {
  if (rules.skill_needs.length === 0) return <p className="text-sm">No skill needs.</p>
  return (
    <ul className="flex flex-col gap-3 text-sm">
      {rules.skill_needs.map((need) => (
        <li key={needKey(need)} className="tabular-nums">
          <span className="font-medium">{need.skill} on {need.shift}</span>: at least {need.weekday_min} on a weekday, {need.weekend_min} on a weekend day
        </li>
      ))}
    </ul>
  )
}

function RulesReadOnly({ state }: { state: UnitRulesState }) {
  const { rules } = state
  const numbers = (items: Explained<NumberRule>[]) => items.map(({ name, label, description }) => ({ label, description, value: rules[name] }))
  const switches = (items: Explained<SwitchRule>[]) => items.map(({ name, label, description }) => ({ label, description, value: onOff(rules[name]) }))
  return (
    <div className="flex flex-col gap-4">
      <Section title="Shifts">
        <ReadOnlyList items={[
          { label: "Shifts this unit works", description: SHIFTS_DESCRIPTION, value: named(rules.shifts, "None chosen") },
          ...NIGHT_AND_DAY.map(({ name, label, description }) => ({ label, description, value: rules[name] || notChosen })),
        ]} />
      </Section>
      <Section title="Headcounts" description={HEADCOUNTS_DESCRIPTION}>
        <div className="flex flex-col gap-4">
          <ReadOnlyList items={switches([HEADCOUNT_SWITCH])} />
          <HeadcountsReadOnly rules={rules} />
        </div>
      </Section>
      <Section title="Who a shift needs"><ReadOnlyList items={switches(WHO_SWITCHES)} /></Section>
      <Section title="Skill needs" description={SKILL_NEEDS_DESCRIPTION}>
        <div className="flex flex-col gap-4">
          <ReadOnlyList items={switches([SKILL_NEEDS_SWITCH])} />
          <SkillNeedsReadOnly rules={rules} />
        </div>
      </Section>
      <Section title="Score targets">
        <ReadOnlyList items={[
          ...numbers(SCORE_TARGETS),
          ...switches([SCORE_SWITCH]),
          { label: "Way of balancing", description: SCORE_MODE_DESCRIPTION, value: SCORE_MODES[rules.score_balance_mode] },
          ...numbers(SCORE_CEILINGS),
        ]} />
      </Section>
      <Section title="Night rest"><ReadOnlyList items={[...numbers([NIGHT_REST_DAYS]), ...switches([NIGHT_REST_SWITCH])]} /></Section>
      <Section title="Runs of shifts">
        <ReadOnlyList items={[
          ...numbers([LONGEST_RUN]),
          ...(fullRunsApply(rules.max_consecutive_shifts) ? numbers([FULL_RUNS]) : []),
          ...numbers([AFTER_TWO_RUNS]),
          ...switches([MIXED_SWITCH]),
        ]} />
      </Section>
      <Section title="Fairness">
        <ReadOnlyList items={[
          { label: "Shifts to spread fairly", description: FAIR_SHIFTS_DESCRIPTION, value: named(rules.fair_shifts, "None") },
          ...numbers([FAIR_TOLERANCE]),
          ...switches([WEEKEND_SWITCH, OVERTIME_SWITCH]),
          ...numbers([OVERTIME_TOLERANCE]),
          ...switches([CARRY_SWITCH]),
          ...numbers([CARRY_CAP, OVERTIME_CARRY_CAP]),
        ]} />
      </Section>
      <Section title="Weekends and switches"><ReadOnlyList items={[...numbers(LIMITS), ...switches([SWITCHES_HARD])]} /></Section>
      <Section title="Requests and solves">
        <ReadOnlyList items={[
          { label: "Requests per nurse per month", description: REQUEST_CAP_DESCRIPTION, value: state.request_cap },
          { label: "Preferences per nurse per month", description: PREFERENCE_CAP_DESCRIPTION, value: state.preference_cap },
          { label: "Default time limit (seconds)", description: TIME_LIMIT_DESCRIPTION, value: state.default_time_limit },
        ]} />
      </Section>
    </div>
  )
}

function ChosenUnitRules({ unitId, mayChange }: { unitId: string; mayChange: boolean }) {
  const query = useGetUnitRules(unitId)
  return (
    <>
      {query.isPending && <p>Loading…</p>}
      {query.isError && <p role="alert">The rules could not be read. Reload the page to try again.</p>}
      {query.data?.needs_review && (
        <div className="flex flex-col items-start gap-1">
          <Badge variant="secondary">Defaults, review before the first solve</Badge>
          {mayChange && <p className="text-sm">Saving this page marks them as reviewed, even with nothing changed.</p>}
        </div>
      )}
      {query.data && (mayChange
        ? <RulesForm key={unitId} unitId={unitId} state={query.data} />
        : <RulesReadOnly state={query.data} />)}
    </>
  )
}

function UnitRulesPage() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  const { unit: inAddress } = Route.useSearch()
  const navigate = useNavigate({ from: Route.fullPath })
  const { units, chosen } = useChosenUnit(user, inAddress)
  const mayChange = user.role === Role.unit_manager

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4">
      <h1 className="text-xl font-medium">{mayChange && chosen ? `Unit rules of ${chosen.name}` : "Unit rules"}</h1>
      <p className="text-muted-foreground">
        The rules this unit rosters by, on top of the hospital rules.
        {!mayChange && " Only the unit's manager can change these."}
      </p>
      {units.isPending && <p>Loading…</p>}
      {units.isError && <p role="alert">The units could not be read. Reload the page to try again.</p>}
      {units.data && !chosen && <p>{mayChange ? "No unit is assigned to you yet. The CNO assigns each unit its manager." : "There are no units yet."}</p>}
      {!mayChange && units.data && chosen && (
        <UnitPicker units={units.data} value={chosen.id} onChange={(unit) => void navigate({ search: { unit } })} />
      )}
      {chosen && <ChosenUnitRules unitId={chosen.id} mayChange={mayChange} />}
    </div>
  )
}

