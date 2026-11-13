import { zodResolver } from "@hookform/resolvers/zod"
import { useQueryClient } from "@tanstack/react-query"
import { createFileRoute, Link, useRouteContext } from "@tanstack/react-router"
import { useEffect, useRef, useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import {
  getListHolidayProposalsQueryKey,
  getListPublicHolidaysQueryKey,
  getListRamadanMonthsQueryKey,
  getListRamadansQueryKey,
  useAddProposedHolidays,
  useConfirmRamadan,
  useCreatePublicHoliday,
  useDeletePublicHoliday,
  useFixRamadanMonth,
  useListHolidayProposals,
  useListPublicHolidays,
  useListRamadanMonths,
  useListRamadans,
  useUnfixRamadanMonth,
  useUpdatePublicHoliday,
} from "@/api/generated/endpoints"
import { Role, RamadanCardSource, RamadanMonthSource, type HolidayProposal, type PublicHolidayBody, type PublicHolidaySchema, type RamadanCard, type RamadanConfirm, type RamadanMonth } from "@/api/generated/model"
import {
  addProposedHolidaysBodyLengthMax as LENGTH_MAX,
  createPublicHolidayBodyNameMax as NAME_MAX,
  listRamadanMonthsQueryYearMax as LAST_YEAR,
  listRamadanMonthsQueryYearMin as FIRST_YEAR,
} from "@/api/generated/zod"
import { CHECKBOX, FormField } from "@/components/form-field"
import { ConfirmDialog } from "@/components/confirm-dialog"
import { Badge } from "@/components/ui/badge"
import { Button, buttonVariants } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { FieldDescription, FieldError, FieldGroup, FieldLegend, FieldSet } from "@/components/ui/field"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { serverErrors, showServerErrors } from "@/lib/server-errors"
import { useEditing } from "@/lib/use-editing"
import { asDate, cn, monthName, scrollableRegion } from "@/lib/utils"
import { useFocusOnOpen } from "@/lib/use-focus-on-open"

/** A hand-edited year is dropped rather than sent: the API would refuse it and the page could only say "reload". */
const searchSchema = z.object({ year: z.number().int().min(FIRST_YEAR).max(LAST_YEAR).optional().catch(undefined) })

/** Open to every role, which may all read the catalogue. Only the CNO gets the forms. */
export const Route = createFileRoute("/_signed-in/holidays")({
  validateSearch: (search) => searchSchema.parse(search),
  staticData: { title: "Holidays and Ramadan" },
  component: HolidaysPage,
})

const longDate = new Intl.DateTimeFormat("en-GB", { weekday: "long", day: "numeric", month: "long", year: "numeric", timeZone: "UTC" })
const dayOf = (iso: string) => Number(iso.slice(8))

/** "1 to 19, 25" for days of one month, in order. */
function dayRanges(dates: string[]): string {
  const runs: [number, number][] = []
  for (const day of dates.map(dayOf)) {
    const last = runs.at(-1)
    if (last && day === last[1] + 1) last[1] = day
    else runs.push([day, day])
  }
  return runs.map(([first, last]) => (first === last ? `${first}` : `${first} to ${last}`)).join(", ") || "None"
}

const isOneRun = (dates: string[]) => dates.length > 0 && dates.every((day, index) => index === 0 || dayOf(day) === dayOf(dates[index - 1]) + 1)

// Unlike the shifts' table, a row's actions here do not stay in view on a narrow screen: two buttons
// would cover the dates, which are what the row is for.

/** The proposals follow the holidays entered and the Ramadan in force, so every write to either reads them again, in
 *  every year: a Ramadan can end in the next year, and Eid al-Fitr with it. */
const readProposalsAgain = (queryClient: ReturnType<typeof useQueryClient>) =>
  queryClient.invalidateQueries({ queryKey: [getListHolidayProposalsQueryKey()[0]] })

// ---- public holidays ------------------------------------------------------------------------------
function HolidayForm({ year, row, onDone, onCancel }: { year: number; row: PublicHolidaySchema | null; onDone: (message: string) => void; onCancel: () => void }) {
  const queryClient = useQueryClient()
  const create = useCreatePublicHoliday()
  const update = useUpdatePublicHoliday()
  const mutation = row ? update : create
  const schema = z.object({
    date: z.iso.date("Enter a date.").refine((date) => date.startsWith(`${year}-`), `This is not in ${year}. Go to that year to enter it.`),
    name: z.string().trim().min(1, "Enter a name.").max(NAME_MAX, `At most ${NAME_MAX} characters.`),
  })
  const form = useForm<PublicHolidayBody>({ resolver: zodResolver(schema), defaultValues: row ?? { date: "", name: "" } })
  const { errors } = form.formState

  useFocusOnOpen(() => {
    if (row) form.setFocus("date")
  }, [row, form])

  const saved = (message: string) => {
    void queryClient.invalidateQueries({ queryKey: getListPublicHolidaysQueryKey({ year }) })
    void readProposalsAgain(queryClient)
    onDone(message)
  }
  const refused = (error: unknown) => showServerErrors(form, error)

  const submit = form.handleSubmit((data) => {
    if (row) {
      update.mutate({ holidayId: row.id, data }, { onSuccess: (next) => saved(`${next.name} is saved.`), onError: refused })
    } else {
      create.mutate({ data }, {
        onSuccess: (next) => {
          form.reset({ date: "", name: "" })
          saved(`${next.name} is added.`)
        },
        onError: refused,
      })
    }
  })
  const unexpected = mutation.isError && serverErrors(mutation.error).length === 0

  return (
    <form onSubmit={submit} noValidate>
      <FieldGroup>
        <FormField label="Date" type="date" min={`${year}-01-01`} max={`${year}-12-31`} registration={form.register("date")} error={errors.date} />
        <FormField label="Name" autoComplete="off" registration={form.register("name")} error={errors.name} />
        {unexpected && <p role="alert" className="text-sm text-destructive">The holiday could not be saved. Check your connection and try again.</p>}
        <div className="flex gap-2">
          <Button type="submit" disabled={mutation.isPending}>{row ? "Save holiday" : "Add holiday"}</Button>
          {row && <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>}
        </div>
      </FieldGroup>
    </form>
  )
}

function Holidays({ year, mayChange }: { year: number; mayChange: boolean }) {
  const queryClient = useQueryClient()
  const holidays = useListPublicHolidays({ year })
  const remove = useDeletePublicHoliday()
  const { editing, start, finish, drop } = useEditing<PublicHolidaySchema>()
  const [done, setDone] = useState("")
  const [asked, setAsked] = useState<PublicHolidaySchema | null>(null)
  const focusAfter = useRef<string | null>(null)

  function removeHoliday(row: PublicHolidaySchema) {
    remove.mutate({ holidayId: row.id }, {
      onSuccess: () => {
        void queryClient.invalidateQueries({ queryKey: getListPublicHolidaysQueryKey({ year }) })
        void readProposalsAgain(queryClient)
        setDone(`${row.name} is removed.`)
        if (editing?.id === row.id) drop()
        focusAfter.current = "field-date" // Where the next holiday would be entered.
        setAsked(null)
      },
    })
  }

  return (
    <section className="flex flex-col gap-4">
      <h2 className="text-lg font-medium">Public holidays in {year}</h2>
      {holidays.isPending && <p>Loading…</p>}
      {holidays.isError && <p role="alert">The public holidays could not be read. Reload the page to try again.</p>}
      <p role="status" className="text-sm text-muted-foreground empty:sr-only">{done}</p>
      {holidays.data && (
        <Card>
          <CardContent>
            {holidays.data.length === 0
              ? <p className="text-sm text-muted-foreground">No public holidays are entered for {year}.</p>
              : (
                <Table container={mayChange ? undefined : scrollableRegion(`Public holidays in ${year}`)}>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Date</TableHead>
                      <TableHead>Name</TableHead>
                      {mayChange && <TableHead><span className="sr-only">Actions</span></TableHead>}
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {holidays.data.map((row) => (
                      <TableRow key={row.id}>
                        <TableCell className="tabular-nums">{longDate.format(asDate(row.date))}</TableCell>
                        <TableCell>{row.name}</TableCell>
                        {mayChange && (
                          <TableCell>
                            <div className="flex justify-end gap-2">
                              <Button variant="outline" size="sm" aria-label={`Edit ${row.name}`}
                                onClick={(event) => { setDone(""); start(row, event.currentTarget) }}>
                                Edit
                              </Button>
                              <Button variant="outline" size="sm" aria-label={`Remove ${row.name}`}
                                onClick={() => { setDone(""); remove.reset(); focusAfter.current = null; setAsked(row) }}>
                                Remove
                              </Button>
                            </div>
                          </TableCell>
                        )}
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
          </CardContent>
        </Card>
      )}
      {mayChange && <HolidayProposals year={year} onStart={() => setDone("")} onDone={(message) => { drop(); setDone(message) }} />}
      {mayChange && holidays.data && (
        <Card className="max-w-xl">
          <CardHeader><CardTitle><h3>{editing ? `Edit ${editing.name}` : "Add a public holiday"}</h3></CardTitle></CardHeader>
          <CardContent>
            <HolidayForm key={`${year} ${editing?.id ?? "new"}`} year={year} row={editing} onCancel={finish}
              onDone={(message) => { setDone(message); finish() }} />
          </CardContent>
        </Card>
      )}
      <ConfirmDialog pending={remove.isPending} failed={remove.isError} focusAfter={focusAfter}
        question={asked && {
          title: `Remove ${asked.name}?`,
          body: `${longDate.format(asDate(asked.date))} stops counting as a public holiday in solves from now on.`,
          confirm: "Remove holiday",
        }}
        onCancel={() => setAsked(null)} onConfirm={() => { if (asked) removeHoliday(asked) }} />
    </section>
  )
}

// ---- the holidays card: what the calendar proposes -----------------------------------------------
const fullDate = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" })
const listed = new Intl.ListFormat("en-GB", { type: "conjunction" })
const dates = (days: string[]) => listed.format(days.map((day) => fullDate.format(asDate(day))))
const lineId = (proposal: HolidayProposal) => `proposal-${proposal.name.toLowerCase().replace(/[^a-z0-9]+/g, "-")}-${proposal.first}`

/** What Add did, in the section's status line: the days added, by name when a skipped one breaks them, and the days left
 *  as they were. */
function addedWords(name: string, added: string[], skipped: string[]): string {
  const one = skipped.length === 1
  if (added.length === 0) return `${name} is not added: ${dates(skipped)} ${one ? "is already a holiday" : "are already holidays"}.`
  if (skipped.length === 0) {
    const days = added.length === 1 ? fullDate.format(asDate(added[0])) : `${dateRange(added[0], added.at(-1) as string)}, ${dayCount(added.length)}`
    return `${name} is added: ${days}.`
  }
  return `${name} is added on ${dates(added)}. ${dates(skipped)} ${one ? "was already a holiday and is unchanged" : "were already holidays and are unchanged"}.`
}

type ProposalFields = { first: string; length: number }

function ProposalLine({ proposal, title, year, onStart, onAdded }: {
  proposal: HolidayProposal; title: string; year: number; onStart: () => void; onAdded: (message: string, gone: boolean) => void
}) {
  const add = useAddProposedHolidays()
  const notALength = `Enter a number of days from 1 to ${LENGTH_MAX}.`
  const schema = z.object({
    first: z.iso.date("Enter a date.").refine((date) => date.startsWith(`${year}-`), `This is not in ${year}. Go to that year to add it.`),
    length: z.number({ error: notALength }).int(notALength).min(1, notALength).max(LENGTH_MAX, notALength),
  })
  const form = useForm<ProposalFields>({ resolver: zodResolver(schema), defaultValues: { first: proposal.first, length: proposal.length } })
  const { errors } = form.formState
  const id = lineId(proposal)

  const submit = form.handleSubmit((data) => {
    if (add.isPending) return
    onStart()
    add.mutate({ params: { year }, data: { name: proposal.name, ...data } }, {
      onSuccess: ({ added, skipped }) => {
        // A line whose first day was changed stays, as the calendar gives it, for the next year's circular to differ.
        form.reset({ first: proposal.first, length: proposal.length })
        onAdded(addedWords(proposal.name, added, skipped), [...added, ...skipped].includes(proposal.first))
      },
      onError: (error) => showServerErrors(form, error),
    })
  })
  const unexpected = add.isError && serverErrors(add.error).length === 0

  return (
    <form onSubmit={submit} noValidate>
      <FieldSet>
        <FieldLegend>{title}</FieldLegend>
        <FieldDescription>From {longDate.format(asDate(proposal.first))}, {dayCount(proposal.length)}.</FieldDescription>
        <FieldGroup>
          <div className="grid gap-4 sm:grid-cols-2">
            <FormField id={`${id}-first`} label="First day" aria-label={`First day of ${title}`} type="date"
              min={`${year}-01-01`} max={`${year}-12-31`} registration={form.register("first")} error={errors.first} />
            <FormField id={`${id}-length`} label="Days" aria-label={`Days of ${title}`} type="number" inputMode="numeric"
              min={1} max={LENGTH_MAX} registration={form.register("length", { valueAsNumber: true })} error={errors.length} />
          </div>
          {unexpected && <p role="alert" className="text-sm text-destructive">{title} could not be added. Check your connection and try again.</p>}
          <div>
            {/* Not disabled while it is sent: the button keeps focus, and a second press is ignored instead. */}
            <Button type="submit" variant="outline" aria-label={`Add ${title}`}>Add</Button>
          </div>
        </FieldGroup>
      </FieldSet>
    </form>
  )
}

/** The CNO's alone. Nothing is stored for a proposal: a line shows while its first day is not a public holiday. */
function HolidayProposals({ year, onStart, onDone }: { year: number; onStart: () => void; onDone: (message: string) => void }) {
  const queryClient = useQueryClient()
  const proposals = useListHolidayProposals({ year })
  const heading = useRef<HTMLHeadingElement>(null)

  async function added(proposal: HolidayProposal, message: string, gone: boolean) {
    onDone(message)
    // An Eid's last days can fall in the next year.
    void queryClient.invalidateQueries({ queryKey: getListPublicHolidaysQueryKey({ year }) })
    void queryClient.invalidateQueries({ queryKey: getListPublicHolidaysQueryKey({ year: year + 1 }) })
    await readProposalsAgain(queryClient)
    if (!gone) return // Focus stays on Add.
    // The line has gone: on to the next one, as a CNO adding them in turn would go, or to the card when none is left.
    const left = queryClient.getQueryData<HolidayProposal[]>(getListHolidayProposalsQueryKey({ year })) ?? []
    const next = left.find((other) => other.first > proposal.first) ?? left.at(-1)
    ;(next ? document.getElementById(`${lineId(next)}-first`) : heading.current)?.focus()
  }

  if (proposals.isError) return <p role="alert">The proposed holidays could not be read. Reload the page to try again.</p>
  if (!proposals.data) return null
  const titleOf = (proposal: HolidayProposal) =>
    // Two of one Eid in a year, as in 2033, are told apart by their month.
    proposals.data.filter((other) => other.name === proposal.name).length > 1 ? `${proposal.name} in ${monthName(proposal.first.slice(0, 7))}` : proposal.name

  return (
    <Card>
      <CardHeader>
        <CardTitle><h3 ref={heading} tabIndex={-1} className="outline-none">Proposed by the calendar</h3></CardTitle>
        <CardDescription>
          The Eids by the Umm al-Qura calendar and the Ramadan in force, and the national days, each until its first day is a
          holiday. Change the first day or the number of days to match this year's circular.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {proposals.data.length === 0
          ? <p className="text-sm text-muted-foreground">Every holiday the calendar proposes for {year} is entered.</p>
          : (
            <ul className="flex flex-col gap-6">
              {proposals.data.map((proposal) => (
                <li key={lineId(proposal)}>
                  <ProposalLine proposal={proposal} title={titleOf(proposal)} year={year} onStart={onStart}
                    onAdded={(message, gone) => void added(proposal, message, gone)} />
                </li>
              ))}
            </ul>
          )}
      </CardContent>
    </Card>
  )
}

// ---- Ramadan dates --------------------------------------------------------------------------------
type RamadanFields = { mode: "range" | "list"; first: string; last: string; days: string[] }

function RamadanForm({ state, onDone, onCancel }: { state: RamadanMonth; onDone: (message: string) => void; onCancel: () => void }) {
  const queryClient = useQueryClient()
  const fix = useFixRamadanMonth()
  const { month } = state
  const name = monthName(month)
  const [year, number] = month.split("-").map(Number)
  const lastDay = new Date(Date.UTC(year, number, 0)).getUTCDate()
  const allDays = Array.from({ length: lastDay }, (_, index) => `${month}-${String(index + 1).padStart(2, "0")}`)
  const heading = useRef<HTMLHeadingElement>(null)

  const schema = z.object({ mode: z.enum(["range", "list"]), first: z.string(), last: z.string(), days: z.array(z.string()) })
    .superRefine((fields, context) => {
      if (fields.mode !== "range") return
      if (!allDays.includes(fields.first)) context.addIssue({ code: "custom", path: ["first"], message: `Enter a day in ${name}.` })
      if (!allDays.includes(fields.last)) context.addIssue({ code: "custom", path: ["last"], message: `Enter a day in ${name}.` })
      else if (fields.first && fields.last < fields.first) context.addIssue({ code: "custom", path: ["last"], message: "This is before the first day." })
    })
  const form = useForm<RamadanFields>({
    resolver: zodResolver(schema),
    defaultValues: {
      // A month fixed to no days has nothing a range could show.
      mode: isOneRun(state.dates) || state.source === RamadanMonthSource.derived ? "range" : "list",
      first: state.dates[0] ?? "",
      last: state.dates.at(-1) ?? "",
      days: state.dates,
    },
  })
  const { errors } = form.formState
  const mode = form.watch("mode")

  useEffect(() => heading.current?.focus(), [])

  const submit = form.handleSubmit((fields) =>
    fix.mutate(
      { month, data: fields.mode === "range" ? { first: fields.first, last: fields.last } : { dates: fields.days } },
      {
        onSuccess: () => {
          void queryClient.invalidateQueries({ queryKey: getListRamadanMonthsQueryKey({ year }) })
          void readProposalsAgain(queryClient)
          onDone(`The Ramadan dates of ${name} are fixed.`)
        },
        onError: (error) => showServerErrors(form, error, ([field]) => (field === "dates" ? "days" : String(field))),
      },
    ),
  )
  const unexpected = fix.isError && serverErrors(fix.error).length === 0

  return (
    <Card className="max-w-xl">
      <CardHeader>
        <CardTitle><h3 ref={heading} tabIndex={-1} className="outline-none">Fix the Ramadan dates of {name}</h3></CardTitle>
        <CardDescription>The dates from the moon sighting. The calendar says: {dayRanges(state.derived_dates)}.</CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} noValidate>
          <FieldGroup>
            <FieldSet>
              <FieldLegend variant="label">Enter them as</FieldLegend>
              <div className="flex flex-wrap gap-x-4 gap-y-2">
                <label className="flex items-center gap-2 text-sm">
                  <input type="radio" value="range" className={CHECKBOX} {...form.register("mode")} /> A range
                </label>
                <label className="flex items-center gap-2 text-sm">
                  <input type="radio" value="list" className={CHECKBOX} {...form.register("mode")} /> A list of days
                </label>
              </div>
            </FieldSet>
            {mode === "range" ? (
              <div className="grid gap-4 sm:grid-cols-2">
                <FormField label="First day" type="date" min={allDays[0]} max={allDays.at(-1)} registration={form.register("first")} error={errors.first} />
                <FormField label="Last day" type="date" min={allDays[0]} max={allDays.at(-1)} registration={form.register("last")} error={errors.last} />
              </div>
            ) : (
              <FieldSet aria-describedby={errors.days ? "ramadan-days-error" : "ramadan-days-hint"}>
                <FieldLegend variant="label">Days of {name}</FieldLegend>
                <div className="grid grid-cols-7 gap-2">
                  {allDays.map((day) => (
                    <label key={day} className="flex items-center gap-1 text-sm tabular-nums">
                      <input type="checkbox" value={day} className={CHECKBOX} {...form.register("days")} /> {dayOf(day)}
                    </label>
                  ))}
                </div>
                <p id="ramadan-days-hint" className="text-sm text-muted-foreground">With none ticked, this month has no Ramadan days.</p>
                <FieldError id="ramadan-days-error" role={undefined} errors={errors.days ? [errors.days as { message?: string }] : undefined} />
              </FieldSet>
            )}
            {unexpected && <p role="alert" className="text-sm text-destructive">The dates could not be saved. Check your connection and try again.</p>}
            <div className="flex gap-2">
              <Button type="submit" disabled={fix.isPending}>Fix dates</Button>
              <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
            </div>
          </FieldGroup>
        </form>
      </CardContent>
    </Card>
  )
}

// ---- a whole Ramadan: the cards -------------------------------------------------------------------
const dayAndMonth = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "long", timeZone: "UTC" })
const yearOf = (iso: string) => iso.slice(0, 4)

/** "8 February – 8 March 2027", or "20 December 2030 – 18 January 2031" across New Year. `withYear: false` leaves a
 *  one-year range's year out, for the calendar's days beside the CNO's, whose year is already said. */
function dateRange(first: string, last: string, withYear = true): string {
  const [from, to] = [dayAndMonth.format(asDate(first)), dayAndMonth.format(asDate(last))]
  if (yearOf(first) !== yearOf(last)) return `${from} ${yearOf(first)} – ${to} ${yearOf(last)}`
  return withYear ? `${from} – ${to} ${yearOf(last)}` : `${from} – ${to}`
}

const calendarDays = (card: RamadanCard) => (Date.parse(card.calendar_last) - Date.parse(card.calendar_first)) / 86_400_000 + 1

const dayCount = (days: number) => `${days} ${days === 1 ? "day" : "days"}`

/** What is in force, and the calendar's days beside them only when they differ: a difference is the expected case
 *  after a sighting, so it is said in plain words, not as a warning. */
function inForce(card: RamadanCard): string {
  const calendar = (sameYear: boolean) => dateRange(card.calendar_first, card.calendar_last, !sameYear)
  if (!card.first || !card.last) return `The CNO: no days. The calendar says ${calendar(false)}.`
  const range = `${dateRange(card.first, card.last)}, ${dayCount(card.days)}`
  if (card.source === RamadanCardSource.derived) return `${range}. The calendar's.`
  const differs = card.first !== card.calendar_first || card.last !== card.calendar_last || card.days !== calendarDays(card)
  // The calendar's year goes unsaid only when all four days share the one already said.
  const sameYear = new Set([card.first, card.last, card.calendar_first, card.calendar_last].map(yearOf)).size === 1
  return `The CNO: ${range}.${differs ? ` The calendar says ${calendar(sameYear)}.` : ""}`
}

function RamadanCardForm({ card, onStart, onDone }: { card: RamadanCard; onStart: () => void; onDone: (message: string) => void }) {
  const queryClient = useQueryClient()
  const confirm = useConfirmRamadan()
  const name = `Ramadan ${card.hijri_year}`
  const prefilled = { first: card.first ?? card.calendar_first, last: card.last ?? card.calendar_last }
  const schema = z.object({ first: z.iso.date("Enter a date."), last: z.iso.date("Enter a date.") })
    .refine((fields) => fields.last >= fields.first, { path: ["last"], message: "This is before the first day." })
  const form = useForm<RamadanConfirm>({ resolver: zodResolver(schema), defaultValues: prefilled })
  const { errors } = form.formState

  // The month table can change what is in force; the fields follow it, without taking focus from anywhere.
  const { first, last } = prefilled
  useEffect(() => form.reset({ first, last }), [form, first, last])

  const submit = form.handleSubmit((data) => {
    if (confirm.isPending) return
    onStart() // A line saying an earlier confirmation went through would sit above this one's refusal.
    confirm.mutate({ hijriYear: card.hijri_year, data }, {
      onSuccess: (next) => {
        // A confirmation can write into the next or the previous year, so every year's lists are read again.
        void queryClient.invalidateQueries({ queryKey: [getListRamadanMonthsQueryKey()[0]] })
        void queryClient.invalidateQueries({ queryKey: [getListRamadansQueryKey()[0]] })
        void readProposalsAgain(queryClient)
        onDone(next.first && next.last ? `${name} is confirmed: ${dateRange(next.first, next.last)}.` : `${name} is confirmed.`)
      },
      onError: (error) => showServerErrors(form, error),
    })
  })
  const unexpected = confirm.isError && serverErrors(confirm.error).length === 0
  const id = `ramadan-${card.hijri_year}`

  return (
    <Card>
      <CardHeader>
        <CardTitle><h3>{name}</h3></CardTitle>
        <CardDescription>{inForce(card)}</CardDescription>
      </CardHeader>
      <CardContent>
        {/* Not disabled while it is sent: the button keeps focus, and a second press is ignored instead. */}
        <form onSubmit={submit} noValidate>
          <FieldGroup>
            <div className="grid gap-4 sm:grid-cols-2">
              <FormField id={`${id}-first`} label="First day" aria-label={`First day of ${name}`} type="date"
                min={`${FIRST_YEAR}-01-01`} max={`${LAST_YEAR}-12-31`} registration={form.register("first")} error={errors.first} />
              <FormField id={`${id}-last`} label="Last day" aria-label={`Last day of ${name}`} type="date"
                min={`${FIRST_YEAR}-01-01`} max={`${LAST_YEAR}-12-31`} registration={form.register("last")} error={errors.last} />
            </div>
            {unexpected && <p role="alert" className="text-sm text-destructive">{name} could not be confirmed. Check your connection and try again.</p>}
            <div>
              <Button type="submit" aria-label={`Confirm ${name}`}>Confirm</Button>
            </div>
          </FieldGroup>
        </form>
      </CardContent>
    </Card>
  )
}

/** The CNO's alone. Nothing of their own is stored: each card reads the months as they stand. */
function RamadanCards({ year, onStart, onDone }: { year: number; onStart: () => void; onDone: (message: string) => void }) {
  const cards = useListRamadans({ year })
  if (cards.isError) return <p role="alert">The Ramadans of {year} could not be read. Reload the page to try again.</p>
  if (!cards.data) return null
  return (
    <div className="flex flex-col gap-4">
      {cards.data.map((card) => <RamadanCardForm key={card.hijri_year} card={card} onStart={onStart} onDone={onDone} />)}
    </div>
  )
}

function Ramadan({ year, mayChange }: { year: number; mayChange: boolean }) {
  const queryClient = useQueryClient()
  const months = useListRamadanMonths({ year })
  const unfix = useUnfixRamadanMonth()
  const { editing, start, finish, drop } = useEditing<RamadanMonth>()
  const [done, setDone] = useState("")
  const [asked, setAsked] = useState<RamadanMonth | null>(null)
  const focusAfter = useRef<string | null>(null)

  function backToCalendar(state: RamadanMonth) {
    unfix.mutate({ month: state.month }, {
      onSuccess: () => {
        void queryClient.invalidateQueries({ queryKey: getListRamadanMonthsQueryKey({ year }) })
        void readProposalsAgain(queryClient)
        setDone(`${monthName(state.month)} follows the calendar again.`)
        if (editing?.month === state.month) drop()
        focusAfter.current = `fix-${state.month}` // The button that opened the question goes with the fixed dates.
        setAsked(null)
      },
    })
  }

  return (
    <section className="flex flex-col gap-4">
      <h2 className="text-lg font-medium">Ramadan dates in {year}</h2>
      <p className="text-sm text-muted-foreground">
        The days a Ramadan-reduced nurse owes fewer hours. Each month follows the Umm al-Qura calendar unless the CNO fixes its dates after the moon sighting.
      </p>
      {months.isPending && <p>Loading…</p>}
      {months.isError && <p role="alert">The Ramadan dates could not be read. Reload the page to try again.</p>}
      <p role="status" className="text-sm text-muted-foreground empty:sr-only">{done}</p>
      {/* A month form left open is closed without taking focus back to its row: focus stays on Confirm. */}
      {mayChange && <RamadanCards year={year} onStart={() => setDone("")} onDone={(message) => { drop(); setDone(message) }} />}
      {months.data && (
        <Card>
          <CardContent>
            <Table container={mayChange ? undefined : scrollableRegion(`Ramadan dates in ${year}`)}>
              <TableHeader>
                <TableRow>
                  <TableHead>Month</TableHead>
                  <TableHead>Ramadan days</TableHead>
                  <TableHead>Set by</TableHead>
                  {mayChange && <TableHead><span className="sr-only">Actions</span></TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
                {months.data.map((state) => {
                  const fixed = state.source === RamadanMonthSource.fixed
                  const name = monthName(state.month)
                  return (
                    <TableRow key={state.month}>
                      <TableCell>{name}</TableCell>
                      <TableCell className="tabular-nums">
                        {dayRanges(state.dates)}
                        {fixed && <p className="text-xs text-muted-foreground">The calendar says: {dayRanges(state.derived_dates)}</p>}
                      </TableCell>
                      <TableCell><Badge variant={fixed ? "secondary" : "outline"}>{fixed ? "The CNO" : "The calendar"}</Badge></TableCell>
                      {mayChange && (
                        <TableCell>
                          <div className="flex justify-end gap-2">
                            <Button id={`fix-${state.month}`} variant="outline" size="sm" aria-label={`Fix the dates of ${name}`}
                              onClick={(event) => { setDone(""); start(state, event.currentTarget) }}>
                              Fix dates
                            </Button>
                            {fixed && (
                              <Button variant="outline" size="sm" aria-label={`Use the calendar for ${name}`}
                                onClick={() => { setDone(""); unfix.reset(); focusAfter.current = null; setAsked(state) }}>
                                Use the calendar
                              </Button>
                            )}
                          </div>
                        </TableCell>
                      )}
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
      {mayChange && editing && (
        <RamadanForm key={editing.month} state={editing} onCancel={finish}
          onDone={(message) => { setDone(message); finish() }} />
      )}
      <ConfirmDialog pending={unfix.isPending} failed={unfix.isError} focusAfter={focusAfter}
        question={asked && {
          title: `Use the calendar for ${monthName(asked.month)}?`,
          body: `The fixed dates (${dayRanges(asked.dates)}) are dropped, and the month's Ramadan days become ${dayRanges(asked.derived_dates)}.`,
          confirm: "Use the calendar",
        }}
        onCancel={() => setAsked(null)} onConfirm={() => { if (asked) backToCalendar(asked) }} />
    </section>
  )
}

/** This year on the hospital's clock, which the browser's can be a day away from at New Year. */
function yearIn(timeZone: string): number {
  return Number(new Intl.DateTimeFormat("en-GB", { year: "numeric", timeZone }).format(new Date()))
}

function HolidaysPage() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  const { year: chosen } = Route.useSearch()
  const year = chosen ?? yearIn(user.time_zone)
  const mayChange = user.role === Role.cno
  const yearLink = cn(buttonVariants({ variant: "outline", size: "sm" }))

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6">
      <div className="flex flex-col gap-2">
        <h1 className="text-xl font-medium">Holidays and Ramadan</h1>
        <p className="text-muted-foreground">
          The days every unit's hours are worked out from.
          {!mayChange && " Only the CNO can change these."}
        </p>
        <nav aria-label="Year" className="flex items-center gap-2">
          {year > FIRST_YEAR && <Link to="/holidays" search={{ year: year - 1 }} className={yearLink}>{year - 1}</Link>}
          <span aria-current="page" className="px-2 font-medium tabular-nums">{year}</span>
          {year < LAST_YEAR && <Link to="/holidays" search={{ year: year + 1 }} className={yearLink}>{year + 1}</Link>}
        </nav>
      </div>
      <Holidays key={`holidays ${year}`} year={year} mayChange={mayChange} />
      <Ramadan key={`ramadan ${year}`} year={year} mayChange={mayChange} />
    </div>
  )
}
