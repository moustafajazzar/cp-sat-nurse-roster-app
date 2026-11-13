import { keepPreviousData } from "@tanstack/react-query"
import { createFileRoute, Link, useRouteContext } from "@tanstack/react-router"
import { z } from "zod"

import { useGetMyRoster } from "@/api/generated/endpoints"
import { Role, type GridView, type NurseRoster } from "@/api/generated/model"
import { MONTH_LINK, monthInAddress, MonthNav } from "@/components/month-nav"
import { GridLegend, RosterGrid } from "@/components/roster-grid"
import { hospitalTime, monthName } from "@/lib/utils"
import { onlyFor } from "@/session"

const searchSchema = z.object({ month: monthInAddress })

export const Route = createFileRoute("/_signed-in/my-roster")({
  validateSearch: (search) => searchSchema.parse(search),
  beforeLoad: onlyFor(Role.staff_nurse, Role.unit_manager),
  staticData: { title: "My roster" },
  component: MyRosterPage,
})

/** The grid the page draws. A colleague's cell is a shift or nothing, as the answer has it: there is no leave to show for one.
 *  The reader's own row comes first among the counted nurses, or among those on probation when they are, with what their own
 *  days were; the rows on probation stay last, under their heading. */
function asGrid(roster: NurseRoster, me: string): GridView {
  const ownFirst = (rows: NurseRoster["rows"]) => [...rows.filter((row) => row.hospital_id === me), ...rows.filter((row) => row.hospital_id !== me)]
  const ordered = [...ownFirst(roster.rows.filter((row) => !row.on_probation)), ...ownFirst(roster.rows.filter((row) => row.on_probation))]
  return {
    days: roster.days,
    shifts: roster.shifts,
    rows: ordered.map((row) => ({
      hospital_id: row.hospital_id,
      on_probation: row.on_probation,
      preceptor_name: row.preceptor_name,
      full_name: row.hospital_id === me ? `${row.full_name || row.hospital_id} (you)` : row.full_name,
      cells: row.shifts.map((code, index) => ({
        shift: code,
        kind: row.hospital_id === me && roster.mine ? roster.mine[index].kind : code ? "shift" : "off",
      })),
    })),
  }
}

function MyRosterPage() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  const { month } = Route.useSearch()
  const mine = useGetMyRoster(month ? { month } : undefined, { query: { placeholderData: keepPreviousData } })
  const answer = mine.data
  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-4">
      <h1 className="text-xl font-medium">{answer?.unit ? `Roster of ${answer.unit}` : "My roster"}</h1>
      <p className="text-muted-foreground">Your unit's approved roster for this month and the months ahead. Your colleagues see the same names and shifts, and see a day off where you are on leave. Your hours, shifts and leave with its type are under <Link to="/my-month" search={{ month: answer?.month }} className="underline underline-offset-3">My month</Link>.</p>
      {mine.isPending && <p>Loading…</p>}
      {mine.isError && <p role="alert">The roster could not be read. Reload the page to try again.</p>}
      {answer && (
        <>
          <MonthNav month={answer.month} link={(shown, label) => shown < answer.first_month ? null : (
            <Link to="/my-roster" search={{ month: shown }} className={MONTH_LINK}>{label}</Link>
          )} />
          {answer.month < answer.first_month && <p>A month already over shows here once it has closed.</p>}
          {answer.month >= answer.first_month && !answer.unit && <p>You are on no unit's roster in {monthName(answer.month)}.</p>}
          {answer.month >= answer.first_month && answer.unit && !answer.roster && <p>No roster yet for {monthName(answer.month)}.</p>}
          {answer.roster && (
            <>
              <div className="flex flex-wrap items-center gap-3">
                <p className="text-sm text-muted-foreground">Approved {hospitalTime(answer.roster.approved_at)}.{answer.roster.mine ? "" : " You are not on it."}</p>
                <Link to="/my-roster/print" search={{ month: answer.month }} className={MONTH_LINK}>Print this roster</Link>
              </div>
              <RosterGrid grid={asGrid(answer.roster, user.username)} label={`Roster of ${answer.unit}, ${monthName(answer.month)}`} />
              <GridLegend grid={asGrid(answer.roster, user.username)} />
            </>
          )}
        </>
      )}
    </div>
  )
}
