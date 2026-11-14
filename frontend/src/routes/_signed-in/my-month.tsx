import { keepPreviousData } from "@tanstack/react-query"
import { createFileRoute, Link, useRouteContext } from "@tanstack/react-router"
import { z } from "zod"

import { useGetMyLeave, useGetMyMonth, useGetMySkills } from "@/api/generated/endpoints"
import { Role, type MyHours, type MyMonth } from "@/api/generated/model"
import { MONTH_LINK, monthInAddress, MonthNav } from "@/components/month-nav"
import { MyPreferencesCard } from "@/components/my-preferences"
import { MyRequestsCard } from "@/components/my-requests"
import { RecordsTable } from "@/components/nurse-skills"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { dayRange, fullDay, hospitalTime, monthName, scrollableRegion } from "@/lib/utils"
import { onlyFor } from "@/session"
import { PhoneShell, type PhoneVariant } from "@/components/phone-shell-prototype"
import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react"
import { useState, type ReactNode } from "react"
import { cn } from "@/lib/utils"

const searchSchema = z.object({ month: monthInAddress, variant: z.enum(["a", "b", "c"]).optional().catch(undefined) })

/** A nurse's own month: their hours and shifts in the roster in force, and their leave with its type. A manager with a nurse
 *  profile reads theirs here too. */
export const Route = createFileRoute("/_signed-in/my-month")({
  validateSearch: (search) => searchSchema.parse(search),
  beforeLoad: onlyFor(Role.staff_nurse, Role.unit_manager),
  staticData: { title: "My month" },
  component: MyMonthPage,
})

const LINK = "underline underline-offset-3"
const hours = (count: number) => `${count} ${count === 1 ? "hour" : "hours"}`

function carried(count: number): string {
  if (count > 0) return `${hours(count)} owed`
  if (count < 0) return `${hours(-count)} worked ahead`
  return "None"
}

function HoursList({ mine }: { mine: MyHours }) {
  const rows: [string, string][] = [
    ["Hours worked", hours(mine.worked)],
    ["Leave counted as worked", hours(mine.leave_credit)],
  ]
  // With the hours rule off nothing is owed, so nothing is over or carried either.
  if (mine.owed !== null) rows.push(["Hours this month asks of you", hours(mine.owed)], ["Overtime", hours(mine.overtime)])
  if (mine.carried !== null) rows.push(["Carried into next month", carried(mine.carried)])
  return (
    <>
      <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-1">
        {rows.map(([term, value]) => (
          <div key={term} className="contents">
            <dt className="text-muted-foreground">{term}</dt>
            <dd className="tabular-nums">{value}</dd>
          </div>
        ))}
      </dl>
      <h4 className="mt-2 font-medium">Shifts</h4>
      {mine.shifts.length === 0 ? <p>No shifts this month.</p> : (
        <ul className="flex flex-wrap gap-x-4 gap-y-1">
          {mine.shifts.map((shift) => (
            <li key={shift.code} className="tabular-nums">{shift.count} {shift.display_name}</li>
          ))}
        </ul>
      )}
    </>
  )
}

function Hours({ answer }: { answer: MyMonth }) {
  const month = monthName(answer.month)
  if (answer.month < answer.first_month) return <p>Hours are shown from this month on, and for a month already over once it has closed.</p>
  if (!answer.unit) return <p>You are on no unit's roster in {month}.</p>
  if (!answer.roster) return <p>No roster yet for {month}. Your hours show here once it is approved.</p>
  return (
    <>
      <p className="text-sm text-muted-foreground">
        From version {answer.roster.version} of the {answer.unit} roster, approved {hospitalTime(answer.roster.approved_at)}.{" "}
        {answer.roster.closed && "The month has closed: these are what really happened, with your leave. "}
        <Link to="/my-roster" search={{ month: answer.month }} className={LINK}>See it under My roster</Link>
      </p>
      {answer.roster.hours ? <HoursList mine={answer.roster.hours} /> : <p>You are not on this roster.</p>}
      {!answer.roster.closed && answer.roster.hours?.carried != null && (
        <p className="text-sm text-muted-foreground">What is carried is worked out again when the month closes, from what really happened.</p>
      )}
    </>
  )
}

/** Her probation, or the nurse on probation the reader precepts, in the month shown and as it stands now. Nothing for
 *  anyone else. */
function ProbationCard({ answer }: { answer: MyMonth }) {
  const month = monthName(answer.month)
  if (answer.probation) {
    const { last_day, preceptor_name: preceptor } = answer.probation
    return (
      <Card>
        <CardHeader><CardTitle><h3>Probation</h3></CardTitle></CardHeader>
        <CardContent className="flex flex-col gap-2">
          <p>You are on probation in {month}. Its last day is {fullDay(last_day)}.</p>
          <p>{preceptor
            ? `Your preceptor is ${preceptor}. You work beside them on their day shifts, and work no night shift.`
            : "No preceptor is assigned to you yet. Your unit manager chooses one. Until then, your day shifts are planned without a preceptor to follow."}</p>
          <p className="text-sm text-muted-foreground">On the roster your row is under On probation: a shift is fully staffed without you.</p>
        </CardContent>
      </Card>
    )
  }
  if (answer.precepts) {
    return (
      <Card>
        <CardHeader><CardTitle><h3>Precepting</h3></CardTitle></CardHeader>
        <CardContent>
          <p>In {month} you precept {answer.precepts}, a nurse on probation, who works beside you on your day shifts.</p>
        </CardContent>
      </Card>
    )
  }
  return null
}

/** Not of a month: what she holds today, with the last day each counts, so she knows what to renew and when. */
function MySkillsCard() {
  const skills = useGetMySkills()
  return (
    <Card>
      <CardHeader><CardTitle><h3>Skills, certificates and licences</h3></CardTitle></CardHeader>
      <CardContent className="flex flex-col gap-2">
        {skills.isPending && <p>Loading…</p>}
        {skills.isError && <p role="alert">Your skills could not be read. Reload the page to try again.</p>}
        {skills.data && skills.data.length === 0 && <p>No skills are recorded for you.</p>}
        {skills.data && skills.data.length > 0 && (
          <RecordsTable skills={skills.data} label="Your skills" details />
        )}
        <p className="text-sm text-muted-foreground">The CNO and the Deputy CNO record them and change one when you renew it. Colleagues cannot see them, and your manager sees no identifier or issue date.</p>
      </CardContent>
    </Card>
  )
}

function MyMonthPage() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  const { month } = Route.useSearch()
  const params = month ? { month } : undefined
  const mine = useGetMyMonth(params, { query: { placeholderData: keepPreviousData } })
  const leave = useGetMyLeave(params, { query: { placeholderData: keepPreviousData } })
  const shown = mine.data?.month ?? leave.data?.month
  const { variant } = Route.useSearch() as { variant?: PhoneVariant }
  if (variant && user.role === Role.staff_nurse) return <MyMonthPrototype variant={variant} month={month} mine={mine.data} leave={leave.data} />

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4">
      <h1 className="text-xl font-medium">My month</h1>
      <p className="text-muted-foreground">Your hours and shifts in the approved roster, {user.role === Role.staff_nurse ? "your requests and preferences, " : ""}your skills and your leave. Only you, your manager, the CNO and the Deputy CNO can see your leave's type: colleagues see a day off.</p>
      {shown && <MonthNav month={shown} link={(next, label) => <Link to="/my-month" search={{ month: next }} className={MONTH_LINK}>{label}</Link>} />}
      {mine.data && <ProbationCard answer={mine.data} />}
      <Card>
        <CardHeader><CardTitle><h3>Hours and shifts</h3></CardTitle></CardHeader>
        <CardContent className="flex flex-col gap-2">
          {mine.isPending && <p>Loading…</p>}
          {mine.isError && <p role="alert">Your hours could not be read. Reload the page to try again.</p>}
          {mine.data && <Hours answer={mine.data} />}
        </CardContent>
      </Card>
      {user.role === Role.staff_nurse && <MyRequestsCard month={month} />}
      {user.role === Role.staff_nurse && <MyPreferencesCard month={month} />}
      <MySkillsCard />
      <Card>
        <CardHeader><CardTitle><h3>Leave</h3></CardTitle></CardHeader>
        <CardContent>
          {leave.isPending && <p>Loading…</p>}
          {leave.isError && <p role="alert">Your leave could not be read. Reload the page to try again.</p>}
          {leave.data && leave.data.runs.length === 0 && <p>You have no leave recorded in {monthName(leave.data.month)}.</p>}
          {leave.data && leave.data.runs.length > 0 && (
            <Table container={scrollableRegion(`Your leave in ${monthName(leave.data.month)}`)}>
              <TableHeader>
                <TableRow>
                  <TableHead>Dates</TableHead>
                  <TableHead>Days</TableHead>
                  <TableHead>Type</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {leave.data.runs.map((run) => (
                  <TableRow key={run.first}>
                    <TableCell className="tabular-nums">{dayRange(run.first, run.last)}</TableCell>
                    <TableCell className="tabular-nums">{run.days}</TableCell>
                    <TableCell>{run.leave_type}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

/* PROTOTYPE (Staff Nurse phone, My month): the same sections three ways at phone width.
   A "Long scroll"  hours as four tiles on top, then requests, preferences, leave and skills as cards
   B "Tabs"         a segmented control: Hours, Requests, Leave, Skills; one shown at a time
   C "Summary"      an hours bar, then one row per section with its state in a line; a row opens the section, with Back */
const MONTH_NAMES: Record<PhoneVariant, string> = { a: "Long scroll", b: "Tabs", c: "Summary" }

function HoursTiles({ mine }: { mine?: MyMonth }) {
  const h = mine?.roster?.hours
  if (!h) return <p className="text-sm text-muted-foreground">No approved roster this month yet.</p>
  const tiles: [string, string][] = [["Worked", `${h.worked} h`], ["Asked of you", h.owed === null ? "–" : `${h.owed} h`], ["Overtime", `${h.overtime} h`], ["Carried", h.carried === null ? "–" : carried(h.carried)]]
  return (
    <div className="grid grid-cols-2 gap-2">
      {tiles.map(([label, value]) => <div key={label} className="rounded-lg border bg-[var(--ward-surface)] p-3"><b className="block text-lg tabular-nums">{value}</b><span className="text-xs text-muted-foreground">{label}</span></div>)}
    </div>
  )
}

function HoursBar({ mine }: { mine?: MyMonth }) {
  const h = mine?.roster?.hours
  if (!h) return <p className="text-sm text-muted-foreground">No approved roster this month yet.</p>
  const owed = h.owed ?? h.worked
  const pct = Math.min(100, ((h.worked + h.leave_credit) / Math.max(owed + h.leave_credit, 1)) * 100)
  return (
    <section className="rounded-xl border bg-[var(--ward-surface)] p-4">
      <div className="flex items-baseline gap-2"><b className="text-2xl tabular-nums">{h.worked} h</b><span className="text-sm text-muted-foreground">of {owed} h asked{h.leave_credit ? `, ${h.leave_credit} h leave credit` : ""}</span></div>
      <div className="mt-2 h-2 overflow-hidden rounded-full bg-muted"><div className="h-full bg-primary" style={{ width: `${pct}%` }} /></div>
      <p className="mt-2 text-sm text-muted-foreground tabular-nums">{h.overtime ? `${h.overtime} h overtime · ` : ""}{h.carried === null ? "" : `Carried: ${carried(h.carried)}`}</p>
      <p className="mt-1 text-sm">{h.shifts.map((s) => `${s.count} ${s.display_name}`).join(" · ")}</p>
    </section>
  )
}

function MyMonthPrototype({ variant, month, mine, leave }: { variant: PhoneVariant; month?: string; mine?: MyMonth; leave?: { month: string; runs: { first: string; last: string; days: number; leave_type: string }[] } }) {
  const [tab, setTab] = useState("hours")
  const [open, setOpen] = useState<string | null>(null)
  const leaveDays = leave?.runs.reduce((n, run) => n + run.days, 0) ?? 0
  const leaveList = leave && (leave.runs.length === 0 ? <p className="text-sm text-muted-foreground">No leave in {monthName(leave.month)}.</p> : (
    <ul className="flex flex-col divide-y rounded-lg border bg-[var(--ward-surface)]">{leave.runs.map((run) => <li key={run.first} className="flex items-center gap-2 px-3 py-2.5 text-sm"><span className="flex-1 tabular-nums">{dayRange(run.first, run.last)}</span><span className="text-muted-foreground">{run.leave_type} · {run.days} d</span></li>)}</ul>))
  const sections: [string, string, string, ReactNode][] = [
    ["hours", "Hours and shifts", mine?.roster?.hours ? `${mine.roster.hours.worked} h worked` : "No roster yet", mine ? <Hours answer={mine} /> : null],
    ["requests", "Requests", "Ask for shifts or days off", <MyRequestsCard month={month} />],
    ["preferences", "Preferences", "What you would rather work", <MyPreferencesCard month={month} />],
    ["leave", "Leave", leaveDays ? `${leaveDays} days this month` : "None this month", leaveList],
    ["skills", "Skills and licences", "What you hold, and when each lapses", <MySkillsCard />],
  ]
  const monthLine = <p className="text-sm text-muted-foreground">{mine ? monthName(mine.month) : ""}{mine?.unit ? ` · ${mine.unit}` : ""}</p>
  let body: ReactNode
  if (variant === "a") body = (
    <div className="flex flex-col gap-4 p-4">{monthLine}{mine && <ProbationCard answer={mine} />}<HoursTiles mine={mine} />
      {sections.slice(1).map(([key, title, , content]) => <section key={key} className="flex flex-col gap-2"><h2 className="text-sm font-semibold">{title}</h2>{content}</section>)}</div>
  )
  else if (variant === "b") {
    const tabs: [string, string][] = [["hours", "Hours"], ["requests", "Requests"], ["leave", "Leave"], ["skills", "Skills"]]
    body = (
      <div className="flex flex-col gap-3 p-4">{monthLine}
        <div role="tablist" className="grid grid-cols-4 gap-1 rounded-lg bg-muted p-1">{tabs.map(([key, label]) => (
          <button key={key} role="tab" type="button" aria-selected={tab === key} onClick={() => setTab(key)} className={cn("min-h-9 rounded-md text-sm text-muted-foreground", tab === key && "bg-[var(--ward-surface)] font-medium text-foreground shadow-sm")}>{label}</button>))}</div>
        {tab === "hours" && <>{mine && <ProbationCard answer={mine} />}<HoursTiles mine={mine} />{sections[0][3]}</>}
        {tab === "requests" && <>{sections[1][3]}{sections[2][3]}</>}
        {tab === "leave" && leaveList}
        {tab === "skills" && sections[4][3]}
      </div>
    )
  }
  else {
    const chosen = sections.find(([key]) => key === open)
    body = chosen ? (
      <div className="flex flex-col gap-3 p-4"><button type="button" onClick={() => setOpen(null)} className="flex items-center gap-1 self-start text-sm font-medium text-primary"><ChevronLeftIcon className="size-4" />My month</button><h2 className="text-lg font-semibold">{chosen[1]}</h2>{chosen[3]}</div>
    ) : (
      <div className="flex flex-col gap-4 p-4">{monthLine}{mine && <ProbationCard answer={mine} />}<HoursBar mine={mine} />
        <ul className="flex flex-col divide-y rounded-lg border bg-[var(--ward-surface)]">{sections.slice(1).map(([key, title, line]) => (
          <li key={key}><button type="button" onClick={() => setOpen(key)} className="flex min-h-14 w-full items-center gap-2 px-3 py-2.5 text-start"><span className="min-w-0 flex-1"><b className="block font-medium">{title}</b><span className="block text-sm text-muted-foreground">{line}</span></span><ChevronRightIcon className="size-4 text-muted-foreground" /></button></li>))}</ul>
      </div>
    )
  }
  return <PhoneShell title="My month" current="/my-month" variant={variant} names={MONTH_NAMES}>{body}</PhoneShell>
}
