import { createFileRoute, Link, useRouteContext } from "@tanstack/react-router"
import { z } from "zod"

import { useGetRosterVersionSheet } from "@/api/generated/endpoints"
import { Role } from "@/api/generated/model"
import { MONTH_LINK, monthInAddress } from "@/components/month-nav"
import { SheetPage } from "@/components/roster-sheet"
import { onlyFor } from "@/session"

const searchSchema = z.object({
  unit: z.string().catch(""),
  month: monthInAddress,
  version: z.coerce.number().int().min(1).catch(0),
})

/** A version of a unit's roster for paper, for the unit's manager and the approvers. Nothing is audited here: it makes no file
 *  and shows what they read on the roster pages. Its CSV and Excel files are taken from the versions table. */
export const Route = createFileRoute("/_signed-in/roster_/print")({
  validateSearch: (search) => searchSchema.parse(search),
  beforeLoad: onlyFor(Role.unit_manager, Role.cno, Role.deputy_cno),
  staticData: { title: "Print the roster" },
  component: PrintRosterPage,
})

function PrintRosterPage() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  const { unit, month, version } = Route.useSearch()
  const chosen = !!unit && !!month && version > 0
  const sheet = useGetRosterVersionSheet(unit, { month: month ?? "", version }, { query: { enabled: chosen } })
  const back = user.role === Role.unit_manager
    ? <Link to="/roster" search={{ month }} className={MONTH_LINK}>Back to the roster</Link>
    : <Link to="/unit-roster" search={{ unit, month }} className={MONTH_LINK}>Back to the roster</Link>
  if (!chosen) return <p role="alert">This address names no roster version. Open one from the versions of a roster.</p>
  if (sheet.isPending) return <p>Loading…</p>
  if (!sheet.data) {
    return <p role="alert">This version could not be read. It may not exist, or the roster may not be yours to read. {back}</p>
  }
  return <div className="[page:roster]"><SheetPage sheet={sheet.data} back={back} /></div>
}
