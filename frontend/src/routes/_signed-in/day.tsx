import { keepPreviousData } from "@tanstack/react-query"
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router"
import { Fragment, useState } from "react"
import { z } from "zod"

import { useGetHospitalDay } from "@/api/generated/endpoints"
import { Role, type DayColumn, type DayFigures, type DayShiftCell, type DayUnit } from "@/api/generated/model"
import { dateInAddress, MONTH_LINK } from "@/components/month-nav"
import { plural } from "@/components/roster-notes"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Table, TableBody, TableCell, TableFooter, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { cn, dayAfter, fullDay, scrollableRegion } from "@/lib/utils"
import { onlyFor } from "@/session"

const searchSchema = z.object({ date: dateInAddress })

export const Route = createFileRoute("/_signed-in/day")({
  validateSearch: (search) => searchSchema.parse(search),
  beforeLoad: onlyFor(Role.cno, Role.deputy_cno),
  staticData: { title: "Day view" },
  component: DayPage,
})

const LINK = "underline underline-offset-3"
const STATE_WORDS = { none: "No roster", draft: "Draft", submitted: "Submitted" } as const
/** The unit column stays in view while the rest scrolls sideways. */
const STICKY = "sticky start-0 z-10 bg-background print:static"

/** The table's scrollable region, which prints whole. */
function region(label: string) {
  const scrollable = scrollableRegion(label)
  return { ...scrollable, className: cn(scrollable.className, "print:overflow-visible") }
}

/** "3 of 4–6 short", "2", and the team leader's and genders' words under it: never colour alone. */
function ShiftCell({ shift }: { shift: DayShiftCell | undefined }) {
  if (!shift) return <TableCell />
  const range = shift.minimum === null ? "" : shift.minimum === shift.maximum ? ` of ${shift.minimum}` : ` of ${shift.minimum}–${shift.maximum}`
  const marks = [shift.team_leader === false && "no team leader", shift.both_genders === false && "one gender"].filter(Boolean)
  return (
    <TableCell className="tabular-nums">
      <span>{shift.present}{range}</span>
      {shift.short && <strong className="ms-1 font-medium text-destructive">short</strong>}
      {shift.over && <strong className="ms-1 font-medium">over</strong>}
      {marks.length > 0 && <span className="block text-sm">{marks.join(", ")}</span>}
    </TableCell>
  )
}

function Overtime({ figures }: { figures: DayFigures }) {
  if (!figures.hours_counted) return <>Hours not counted</>
  return <>{figures.overtime} h of {figures.overtime_cap}, {plural(figures.with_overtime, "nurse")}</>
}

/** The names behind a row's counts: each shift's own staff, team leaders marked, then who is on leave. The answer holds them. */
function Names({ figures, columns }: { figures: DayFigures; columns: DayColumn[] }) {
  const named = new Map(columns.map((column) => [column.code, column.display_name]))
  return (
    <dl className="grid gap-x-4 gap-y-1 whitespace-normal sm:grid-cols-[max-content_1fr]">
      {figures.shifts.map((shift) => (
        <Fragment key={shift.code}>
          <dt className="font-medium">{named.get(shift.code) ?? shift.code}</dt>
          <dd>{shift.nurses.length === 0 ? "Nobody" : shift.nurses.map((nurse) => nurse.full_name + (nurse.team_leader ? " (team leader)" : "")).join(", ")}</dd>
        </Fragment>
      ))}
      <dt className="font-medium">On leave</dt>
      <dd>{figures.on_leave.length === 0 ? "Nobody" : figures.on_leave.map((nurse) => nurse.full_name).join(", ")}</dd>
    </dl>
  )
}

function UnitRows({ unit, columns, date }: { unit: DayUnit; columns: DayColumn[]; date: string }) {
  const [open, setOpen] = useState(false)
  const month = date.slice(0, 7)
  const name = unit.archived ? `${unit.name} (archived)` : unit.name
  const figures = unit.figures
  const namesId = `names-${unit.id}`
  const toRoster = <Link to="/unit-roster" search={{ unit: unit.id, month, date }} className={LINK}>{name}</Link>
  return (
    <>
      <TableRow>
        <TableHead scope="row" className={cn(STICKY, "font-normal")}>
          <span className="flex items-center gap-2">
            {figures && (
              <button type="button" aria-expanded={open} aria-controls={namesId} aria-label={`Names in ${unit.name}`}
                onClick={() => setOpen(!open)}
                className="inline-flex size-6 items-center justify-center rounded-sm border text-xs outline-none focus-visible:ring-3 focus-visible:ring-ring print:hidden">
                <span aria-hidden="true">{open ? "−" : "+"}</span>
              </button>
            )}
            {unit.state === "none" || unit.state === "draft" ? name : toRoster}
          </span>
        </TableHead>
        {figures ? (
          <>
            {columns.map((column) => <ShiftCell key={column.code} shift={figures.shifts.find((shift) => shift.code === column.code)} />)}
            <TableCell className="tabular-nums">{figures.on_leave.length}</TableCell>
            <TableCell className="tabular-nums"><Overtime figures={figures} /></TableCell>
          </>
        ) : (
          <TableCell colSpan={columns.length + 2} className="whitespace-normal">
            {unit.unchecked ? `Could not be checked. The solver's code says: ${unit.unchecked}`
              : unit.state === "submitted" ? <Link to="/unit-roster" search={{ unit: unit.id, month }} className={cn(LINK, "font-medium")}>Submitted</Link>
                : <span className="text-muted-foreground">{STATE_WORDS[unit.state as keyof typeof STATE_WORDS]}</span>}
          </TableCell>
        )}
      </TableRow>
      {figures && open && (
        <TableRow id={namesId}>
          <TableCell colSpan={columns.length + 3}><Names figures={figures} columns={columns} /></TableCell>
        </TableRow>
      )}
    </>
  )
}

function DayPage() {
  const { date } = Route.useSearch()
  const navigate = useNavigate({ from: Route.fullPath })
  // No polling: it reads on load and on a date change, and a reload is the refresh (not a return to the window).
  const day = useGetHospitalDay(date ? { date } : undefined,
    { query: { placeholderData: keepPreviousData, refetchOnWindowFocus: false, refetchOnReconnect: false } })
  const answer = day.data
  /** The day asked for: while it loads, the one before stays shown, but the controls move on from the new one. */
  const asked = date ?? answer?.date ?? ""
  const heading = answer ? fullDay(answer.date) + (answer.holiday ? `, ${answer.holiday}` : answer.weekend ? ", weekend day" : "") : ""
  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-4 [page:day-view]">
      <h1 className="text-xl font-medium">Day view</h1>
      <p className="text-muted-foreground print:hidden">Every unit on one date, from its roster in force checked on the month as it stands, or from its close once the month has closed. Open a unit to read its roster on that date.</p>
      {day.isPending && <p>Loading…</p>}
      {day.isError && <p role="alert">The day could not be read. Reload the page to try again.</p>}
      {answer && (
        <>
          <nav aria-label="Days" className="flex flex-wrap items-end gap-x-3 gap-y-2 print:hidden">
            <Link to="/day" search={{ date: dayAfter(asked, -1) }} className={MONTH_LINK}>Earlier day</Link>
            <Link to="/day" search={{ date: dayAfter(asked, 1) }} className={MONTH_LINK}>Later day</Link>
            <div className="flex flex-col gap-1">
              <Label htmlFor="day-date">Date</Label>
              <Input id="day-date" type="date" className="w-auto" value={asked}
                onChange={(event) => { if (event.target.value) void navigate({ search: { date: event.target.value } }) }} />
            </div>
          </nav>
          <h2 className="text-base font-medium">{heading}</h2>
          {answer.units.length === 0 ? <p>There are no units yet.</p> : (
            <Table container={region(`Day view, ${heading}`)}>
              <TableHeader>
                <TableRow>
                  <TableHead scope="col" className={STICKY}>Unit</TableHead>
                  {answer.shifts.map((column) => <TableHead key={column.code} scope="col">{column.display_name} ({column.code})</TableHead>)}
                  <TableHead scope="col">On leave</TableHead>
                  <TableHead scope="col">Overtime this month</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {answer.units.map((unit) => <UnitRows key={unit.id} unit={unit} columns={answer.shifts} date={answer.date} />)}
              </TableBody>
              <TableFooter>
                <TableRow>
                  <TableHead scope="row" className={cn(STICKY, "bg-muted")}>
                    All units
                    <span className="block font-normal">{answer.totals.units_short === 0 ? "None short" : `${plural(answer.totals.units_short, "unit")} short somewhere`}</span>
                  </TableHead>
                  {answer.totals.on_duty.map((total) => <TableCell key={total.code} className="tabular-nums">{total.on_duty}</TableCell>)}
                  <TableCell className="tabular-nums">{answer.totals.on_leave}</TableCell>
                  <TableCell className="tabular-nums">{answer.totals.overtime} h</TableCell>
                </TableRow>
              </TableFooter>
            </Table>
          )}
        </>
      )}
    </div>
  )
}
