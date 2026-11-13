import { createFileRoute, Link, useRouteContext } from "@tanstack/react-router"
import { z } from "zod"

import { useGetMyRosterSheet } from "@/api/generated/endpoints"
import { Role } from "@/api/generated/model"
import { MONTH_LINK, monthInAddress } from "@/components/month-nav"
import { SheetPage } from "@/components/roster-sheet"
import { monthName } from "@/lib/utils"
import { onlyFor } from "@/session"

const searchSchema = z.object({ month: monthInAddress })

/** My roster for paper: the roster in force of the reader's own unit, as its files hold it. A colleague's leave is a day off
 *  here as everywhere a colleague reads; only the reader's own row may say leave. No file is made, so nothing is audited. */
export const Route = createFileRoute("/_signed-in/my-roster_/print")({
  validateSearch: (search) => searchSchema.parse(search),
  beforeLoad: onlyFor(Role.staff_nurse, Role.unit_manager),
  staticData: { title: "Print my roster" },
  component: PrintMyRosterPage,
})

function PrintMyRosterPage() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  const { month } = Route.useSearch()
  const mine = useGetMyRosterSheet(month ? { month } : undefined)
  const answer = mine.data
  const back = <Link to="/my-roster" search={{ month: answer?.month ?? month }} className={MONTH_LINK}>Back to My roster</Link>
  if (mine.isPending) return <p>Loading…</p>
  if (!answer) return <p role="alert">The roster could not be read. Reload the page to try again.</p>
  if (!answer.sheet) return <p>There is no approved roster of {monthName(answer.month)} to print. {back}</p>
  return <div className="[page:roster]"><SheetPage sheet={answer.sheet} me={user.username} back={back} /></div>
}
