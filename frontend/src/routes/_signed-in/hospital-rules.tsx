import { zodResolver } from "@hookform/resolvers/zod"
import { useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useRouteContext } from "@tanstack/react-router"
import { Trash2Icon } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { useController, useForm, type Control, type FieldError } from "react-hook-form"
import { z } from "zod"

import {
  getGetHospitalRulesQueryKey,
  useGetHospitalRules,
  useUpdateHospitalRules,
} from "@/api/generated/endpoints"
import { Role, type HospitalRules, type HospitalRulesState, type Weights } from "@/api/generated/model"
import {
  updateHospitalRulesBodyHoursPerDayMax as DAILY_HOURS_MAX,
  updateHospitalRulesBodyOvertimeMonthCapMax as HOURS_MAX,
  updateHospitalRulesBodyWeightsChangeMax as WEIGHT_MAX,
} from "@/api/generated/zod"
import { CHECKBOX, CheckboxField, FormField } from "@/components/form-field"
import { onOff, ReadOnlyList, Section } from "@/components/rules-section"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { FieldError as FieldErrorText, FieldGroup, FieldLegend, FieldSet } from "@/components/ui/field"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import { serverErrors, showServerErrors } from "@/lib/server-errors"
import {
  DAILY_HOURS,
  SWITCHES,
  CAPS,
  WEEKEND_DESCRIPTION,
  SUCCESSIONS_TITLE,
  SUCCESSIONS_DESCRIPTION,
  SUCCESSIONS_SWITCH_LABEL,
  SUCCESSIONS_SWITCH,
  WEIGHTS_DESCRIPTION,
  HOSPITAL_RULE_LABELS,
  WEEKDAYS,
} from "@/lib/hospital-rules-words"
import { OVERTIME_SHARE_ABOVE_OVERTIME, WEIGHTS } from "@/lib/weights"
import { onlyFor } from "@/session"

export const Route = createFileRoute("/_signed-in/hospital-rules")({
  beforeLoad: onlyFor(Role.cno, Role.deputy_cno),
  staticData: { title: "Hospital rules" },
  component: HospitalRulesPage,
})


const wholeNumber = (max: number) =>
  z.int("Enter a whole number.").min(0, `Between 0 and ${max}.`).max(max, `Between 0 and ${max}.`)

const dailyHours = wholeNumber(DAILY_HOURS_MAX)
const hours = wholeNumber(HOURS_MAX)
const weight = wholeNumber(WEIGHT_MAX)

const schema = z
  .object({
    hours_per_day: dailyHours,
    ramadan_hours_per_day: dailyHours,
    breastfeeding_reduction: dailyHours,
    weekend_weekdays: z.array(z.int()),
    forbidden_successions: z.array(z.array(z.string())),
    forbidden_successions_enabled: z.boolean(),
    hours_rule_enabled: z.boolean(),
    overtime_caps_enabled: z.boolean(),
    overtime_month_cap: hours,
    overtime_year_cap: hours,
    dept_overtime_cap: hours,
    weights: z.object(Object.fromEntries(WEIGHTS.map(({ name }) => [name, weight])) as Record<keyof Weights, typeof weight>),
  })
  .superRefine((rules, context) => {
    if (rules.weights.overtime_above_share > rules.weights.nurse_overtime)
      context.addIssue({ code: "custom", path: ["weights", "overtime_above_share"], message: OVERTIME_SHARE_ABOVE_OVERTIME })
    if (rules.ramadan_hours_per_day > rules.hours_per_day)
      context.addIssue({ code: "custom", path: ["ramadan_hours_per_day"], message: "Cannot be more than the hours per day." })
    if (rules.breastfeeding_reduction > rules.ramadan_hours_per_day)
      context.addIssue({ code: "custom", path: ["breastfeeding_reduction"], message: "Cannot be more than the Ramadan hours per day." })
    const seen = new Set<string>()
    for (const [today, tomorrow] of rules.forbidden_successions) {
      const pair = `${today} then ${tomorrow}`
      if (seen.has(pair)) {
        context.addIssue({ code: "custom", path: ["forbidden_successions"], message: `${pair} is listed twice. Remove one.` })
        break
      }
      seen.add(pair)
    }
  })

function WeekendField({ control, error }: { control: Control<HospitalRules>; error?: FieldError }) {
  const { field } = useController({ control, name: "weekend_weekdays" })
  const toggle = (day: number, on: boolean) =>
    field.onChange(on ? [...field.value, day].sort((a, b) => a - b) : field.value.filter((other) => other !== day))
  return (
    <FieldSet ref={field.ref} tabIndex={-1} aria-describedby={error ? "weekend-days-error" : undefined} className="outline-none">
      <FieldLegend className="sr-only">Weekend days</FieldLegend>
      <div className="flex flex-wrap gap-x-4 gap-y-2">
        {WEEKDAYS.map(([day, name]) => (
          <label key={day} className="flex items-center gap-2 text-sm">
            <input type="checkbox" className={CHECKBOX} checked={field.value.includes(day)}
              onChange={(event) => toggle(day, event.target.checked)} />
            {name}
          </label>
        ))}
      </div>
      <FieldErrorText id="weekend-days-error" errors={error ? [error] : undefined} role={undefined} />
    </FieldSet>
  )
}

function SuccessionsField({ control, codes, error }: { control: Control<HospitalRules>; codes: string[]; error?: FieldError }) {
  const { field } = useController({ control, name: "forbidden_successions" })
  const pairs = field.value
  const add = useRef<HTMLButtonElement>(null)
  const list = useRef<HTMLUListElement>(null)
  const [focus, setFocus] = useState<"none" | "last" | "add">("none")

  // After the list has drawn what the button just changed, so focus never falls back to the page.
  useEffect(() => {
    if (focus === "add") add.current?.focus()
    if (focus === "last") list.current?.querySelector<HTMLElement>("li:last-child select")?.focus()
    if (focus !== "none") setFocus("none")
  }, [focus])

  const listed = new Set(pairs.map(([today, tomorrow]) => `${today} ${tomorrow}`))
  // Two different shifts first: a shift forbidden after itself is a far stronger rule to add by accident.
  const unlisted = codes
    .flatMap((today) => codes.map((tomorrow) => [today, tomorrow]))
    .filter(([today, tomorrow]) => !listed.has(`${today} ${tomorrow}`))
  const nextPair = unlisted.find(([today, tomorrow]) => today !== tomorrow) ?? unlisted[0]
  const change = (index: number, side: 0 | 1, code: string) =>
    field.onChange(pairs.map((pair, i) => (i === index ? (side === 0 ? [code, pair[1]] : [pair[0], code]) : pair)))

  return (
    <div ref={field.ref} tabIndex={-1} role="group" aria-label="Pairs" aria-describedby={error ? "forbidden-successions-error" : undefined}
      className="flex flex-col gap-3 outline-none">
      {pairs.length === 0 && <p className="text-sm text-muted-foreground">No successions are forbidden.</p>}
      <ul ref={list} className="flex flex-col gap-3 sm:gap-2">
        {pairs.map(([today, tomorrow], index) => (
          // By position: a row holds no state of its own, only what `pairs` says.
          <li key={index}>
            <div role="group" aria-label={`Pair ${index + 1}`} className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
            <label className="flex items-center gap-2">
              After
              <NativeSelect className="w-20" value={today} onChange={(event) => change(index, 0, event.target.value)}>
                {codes.map((code) => <NativeSelectOption key={code} value={code}>{code}</NativeSelectOption>)}
              </NativeSelect>
            </label>
            <label className="flex items-center gap-2">
              the next day cannot be
              <NativeSelect className="w-20" value={tomorrow} onChange={(event) => change(index, 1, event.target.value)}>
                {codes.map((code) => <NativeSelectOption key={code} value={code}>{code}</NativeSelectOption>)}
              </NativeSelect>
            </label>
            <Button type="button" variant="ghost" size="icon" aria-label={`Remove pair ${index + 1}`}
              onClick={() => {
                field.onChange(pairs.filter((_, i) => i !== index))
                setFocus(pairs.length === 1 || index === pairs.length - 1 ? "add" : "none")
              }}>
              <Trash2Icon aria-hidden="true" />
            </Button>
            </div>
          </li>
        ))}
      </ul>
      <FieldErrorText id="forbidden-successions-error" errors={error ? [error] : undefined} role={undefined} />
      <Button ref={add} type="button" variant="outline" className="self-start" disabled={!nextPair}
        onClick={() => {
          if (!nextPair) return
          field.onChange([...pairs, nextPair])
          setFocus("last")
        }}>
        Add a pair
      </Button>
    </div>
  )
}

/** "weights.change" as it is, and a pair's or a weekend day's position stops at the list, which shows the error. */
const rulesPath = ([first, second]: (string | number)[]) => (first === "weights" ? `weights.${second}` : String(first))

function RulesForm({ state }: { state: HospitalRulesState }) {
  const queryClient = useQueryClient()
  const update = useUpdateHospitalRules()
  const form = useForm<HospitalRules>({ resolver: zodResolver(schema), defaultValues: state.rules })
  const { errors, isDirty } = form.formState
  const asNumber = { valueAsNumber: true }

  const submit = form.handleSubmit((data) =>
    update.mutate(
      { data },
      {
        onSuccess: (saved) => {
          queryClient.setQueryData(getGetHospitalRulesQueryKey(), saved)
          form.reset(saved.rules)
        },
        onError: (error) => showServerErrors(form, error, rulesPath),
      },
    ),
  )
  const refused = update.isError && serverErrors(update.error, rulesPath).length > 0

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-4">
      <Section title="Daily hours">
        <FieldGroup>
          {DAILY_HOURS.map(({ name, label, description }) => (
            <FormField key={name} label={label} type="number" inputMode="numeric" description={description}
              registration={form.register(name, asNumber)} error={errors[name]} />
          ))}
        </FieldGroup>
      </Section>
      <Section title={HOSPITAL_RULE_LABELS.weekend_weekdays} description={WEEKEND_DESCRIPTION}>
        <WeekendField control={form.control} error={errors.weekend_weekdays as FieldError | undefined} />
      </Section>
      <Section title={SUCCESSIONS_TITLE} description={SUCCESSIONS_DESCRIPTION}>
        <FieldGroup>
          <CheckboxField label={SUCCESSIONS_SWITCH_LABEL} description={SUCCESSIONS_SWITCH}
            registration={form.register("forbidden_successions_enabled")} />
          <SuccessionsField control={form.control} codes={state.shift_codes}
            error={errors.forbidden_successions as FieldError | undefined} />
        </FieldGroup>
      </Section>
      <Section title="Hours and overtime">
        <FieldGroup>
          {SWITCHES.map(({ name, label, description }) => (
            <CheckboxField key={name} label={label} description={description} registration={form.register(name)} />
          ))}
          {CAPS.map(({ name, label, description }) => (
            <FormField key={name} label={label} type="number" inputMode="numeric" description={description}
              registration={form.register(name, asNumber)} error={errors[name]} />
          ))}
        </FieldGroup>
      </Section>
      <Section title="Weights" description={WEIGHTS_DESCRIPTION}>
        <FieldGroup>
          {WEIGHTS.map(({ name, label, description }) => (
            <FormField key={name} label={label} type="number" inputMode="numeric" description={description}
              registration={form.register(`weights.${name}`, asNumber)} error={errors.weights?.[name]} />
          ))}
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

function RulesReadOnly({ rules }: { rules: HospitalRules }) {
  const weekend = WEEKDAYS.filter(([day]) => rules.weekend_weekdays.includes(day)).map(([, name]) => name)
  return (
    <div className="flex flex-col gap-4">
      <Section title="Daily hours">
        <ReadOnlyList items={DAILY_HOURS.map(({ name, label, description }) => ({ label, description, value: rules[name] }))} />
      </Section>
      <Section title={HOSPITAL_RULE_LABELS.weekend_weekdays} description={WEEKEND_DESCRIPTION}>
        <ul className="flex flex-col gap-1 text-sm">
          {weekend.length ? weekend.map((name) => <li key={name}>{name}</li>) : <li>None</li>}
        </ul>
      </Section>
      <Section title={SUCCESSIONS_TITLE} description={SUCCESSIONS_DESCRIPTION}>
        <div className="flex flex-col gap-4">
          <ReadOnlyList items={[{ label: SUCCESSIONS_SWITCH_LABEL, description: SUCCESSIONS_SWITCH, value: onOff(rules.forbidden_successions_enabled) }]} />
          <ul className="flex flex-col gap-1 text-sm">
            {rules.forbidden_successions.length === 0 && <li>No successions are forbidden.</li>}
            {rules.forbidden_successions.map(([today, tomorrow]) => (
              <li key={`${today} ${tomorrow}`}>After {today}, not {tomorrow}</li>
            ))}
          </ul>
        </div>
      </Section>
      <Section title="Hours and overtime">
        <ReadOnlyList items={[
          ...SWITCHES.map(({ name, label, description }) => ({ label, description, value: onOff(rules[name]) })),
          ...CAPS.map(({ name, label, description }) => ({ label, description, value: rules[name] })),
        ]} />
      </Section>
      <Section title="Weights" description={WEIGHTS_DESCRIPTION}>
        <ReadOnlyList items={WEIGHTS.map(({ name, label, description }) => ({ label, description, value: rules.weights[name] }))} />
      </Section>
    </div>
  )
}

function HospitalRulesPage() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  const rules = useGetHospitalRules()
  const mayChange = user.role === Role.cno

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4">
      <h1 className="text-xl font-medium">Hospital rules</h1>
      <p className="text-muted-foreground">
        The rules every unit rosters by. Every solve starts from them.
        {!mayChange && " Only the CNO can change these."}
      </p>
      {rules.isPending && <p>Loading…</p>}
      {rules.isError && <p role="alert">The rules could not be read. Reload the page to try again.</p>}
      {rules.data?.needs_review && (
        <div className="flex flex-col items-start gap-1">
          <Badge variant="secondary">Defaults, review before the first solve</Badge>
          {mayChange && <p className="text-sm">Saving this page marks them as reviewed, even with nothing changed.</p>}
        </div>
      )}
      {rules.data && (mayChange ? <RulesForm state={rules.data} /> : <RulesReadOnly rules={rules.data.rules} />)}
    </div>
  )
}
