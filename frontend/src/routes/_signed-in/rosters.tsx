import { keepPreviousData } from "@tanstack/react-query"
import { createFileRoute, Link } from "@tanstack/react-router"
import { z } from "zod"

import { useGetRosterOverview } from "@/api/generated/endpoints"
import { Role, type OverviewUnitStatesItem } from "@/api/generated/model"
import { MONTH_LINK, monthInAddress } from "@/components/month-nav"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { monthAfter, monthName, scrollableRegion } from "@/lib/utils"
import { onlyFor } from "@/session"

const searchSchema = z.object({ month: monthInAddress })

export const Route = createFileRoute("/_signed-in/rosters")({
  validateSearch: (search) => searchSchema.parse(search),
  beforeLoad: onlyFor(Role.cno, Role.deputy_cno),
  staticData: { title: "Rosters" },
  component: OverviewPage,
})

const STATE_WORDS: Record<OverviewUnitStatesItem, string> = {
  none: "No roster",
  draft: "Draft",
  submitted: "Submitted",
  approved: "Approved",
  closed: "Closed",
}

function OverviewPage() {
  const { month } = Route.useSearch()
  const overview = useGetRosterOverview(month ? { month } : undefined, { query: { placeholderData: keepPreviousData } })
  const answer = overview.data
  const span = answer ? `${monthName(answer.months[0])} to ${monthName(answer.months.at(-1)!)}` : ""
  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4">
      <h1 className="text-xl font-medium">Rosters</h1>
      <p className="text-muted-foreground">Where each unit's roster stands, month by month. Open a submitted, approved or closed one to read it, and a submitted one to approve or reject it. A draft is its manager's until it is submitted. A month closes once it is over: its roster in force, leave and pins become fact.</p>
      {overview.isPending && <p>Loading…</p>}
      {overview.isError && <p role="alert">The rosters could not be read. Reload the page to try again.</p>}
      {answer && (
        <>
          <nav aria-label="Months" className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <Link to="/rosters" search={{ month: monthAfter(answer.months[0], -1) }} className={MONTH_LINK}>Earlier month</Link>
            <h2 className="text-base font-medium tabular-nums">{span}</h2>
            <Link to="/rosters" search={{ month: monthAfter(answer.months[0], 1) }} className={MONTH_LINK}>Later month</Link>
          </nav>
          {answer.units.length === 0 ? <p>There are no units yet.</p> : (
            <Table container={scrollableRegion(`Rosters, ${span}`)}>
              <TableHeader>
                <TableRow>
                  <TableHead>Unit</TableHead>
                  {answer.months.map((shown) => <TableHead key={shown}>{monthName(shown)}</TableHead>)}
                </TableRow>
              </TableHeader>
              <TableBody>
                {answer.units.map((unit) => (
                  <TableRow key={unit.id}>
                    <TableHead scope="row" className="font-normal">{unit.archived ? `${unit.name} (archived)` : unit.name}</TableHead>
                    {unit.states.map((state, index) => (
                      <TableCell key={answer.months[index]}>
                        {state === "submitted" || state === "approved" || state === "closed"
                          ? <Link to="/unit-roster" search={{ unit: unit.id, month: answer.months[index] }} className={`underline underline-offset-3 ${state === "submitted" ? "font-medium" : ""}`}>{STATE_WORDS[state]}</Link>
                          : <span className="text-muted-foreground">{STATE_WORDS[state]}</span>}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </>
      )}
    </div>
  )
}
