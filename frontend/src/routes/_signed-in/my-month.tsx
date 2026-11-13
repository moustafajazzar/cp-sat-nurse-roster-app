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

const searchSchema = z.object({ month: monthInAddress })

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
