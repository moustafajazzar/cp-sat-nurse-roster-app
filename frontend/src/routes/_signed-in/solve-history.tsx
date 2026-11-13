import { keepPreviousData } from "@tanstack/react-query"
import { createFileRoute, Link, useRouteContext } from "@tanstack/react-router"
import { useEffect, useRef } from "react"
import { z } from "zod"

import { useListHospitalSolveHistory, useListUnitSolveHistory } from "@/api/generated/endpoints"
import { Role, type Me, type UnitSolveRow } from "@/api/generated/model"
import { monthInAddress } from "@/components/month-nav"
import { CompareTwoResults } from "@/components/result-comparison"
import { SolveHistoryTable } from "@/components/solve-history-table"
import { buttonVariants } from "@/components/ui/button"
import { useChosenUnit } from "@/components/unit-picker"
import { failedBecause, replanStatus, solveStatus } from "@/lib/solve-words"
import { cn, hospitalTime, monthName } from "@/lib/utils"
import { onlyFor } from "@/session"

const searchSchema = z.object({
  month: monthInAddress,
  older_than: z.uuid().optional().catch(undefined),
  a: z.uuid().optional().catch(undefined),
  b: z.uuid().optional().catch(undefined),
})

export const Route = createFileRoute("/_signed-in/solve-history")({
  validateSearch: (search) => searchSchema.parse(search),
  beforeLoad: onlyFor(Role.unit_manager, Role.system_admin),
  staticData: { title: "Solve history" },
  component: SolveHistoryPage,
})

/** The link that was clicked may be gone from the new page, which would drop focus on the floor. */
function useFocusOnNewPage(older_than: string | undefined) {
  const heading = useRef<HTMLHeadingElement>(null)
  const previous = useRef(older_than)
  useEffect(() => {
    if (previous.current !== older_than) heading.current?.focus()
    previous.current = older_than
  }, [older_than])
  return heading
}

function Older({ older_than, month }: { older_than: string | null; month?: string }) {
  if (!older_than) return null
  return <Link to="/solve-history" search={{ month, older_than }} className={cn(buttonVariants({ variant: "outline" }), "self-start")}>Older solves</Link>
}

type Pair = { a?: string; b?: string }

function UnitHistory({ unit, month, older_than, pair }: { unit: { id: string; name: string }; month: string | undefined; older_than: string | undefined; pair: Pair }) {
  const params = { ...(month && { month }), ...(older_than && { older_than }) }
  const history = useListUnitSolveHistory(unit.id, Object.keys(params).length ? params : undefined, { query: { placeholderData: keepPreviousData } })
  const heading = useFocusOnNewPage(older_than)
  const page = history.data
  return (
    <>
      <h1 ref={heading} tabIndex={-1} className="text-xl font-medium outline-none">Solve history of {unit.name}</h1>
      <p className="text-muted-foreground">Every solve is kept, newest first. Open one to see what it answered, and to restore it as the draft. Times are hospital time.</p>
      {month && <p className="text-sm">Solves of {monthName(month)} only. <Link to="/solve-history" search={{}} className="underline underline-offset-3">Show every month</Link></p>}
      {history.isPending && <p>Loading…</p>}
      {history.isError && <p role="alert">The solve history could not be read. Reload the page to try again.</p>}
      {page && page.solves.length === 0 && <p>{month ? `No solve of ${monthName(month)} yet.` : "This unit has not solved a month yet."}</p>}
      {page && page.solves.length > 0 && (
        <SolveHistoryTable<UnitSolveRow> label={`Solves of ${unit.name}`} solves={page.solves}
          status={(row) => (row.replan_from ? replanStatus(row) : row.in_draft ? `${solveStatus(row)}, the draft` : solveStatus(row))}
          trailing={[
            { header: "Cost", end: true, cell: (row) => row.cost ?? "None" },
            {
              header: <span className="sr-only">Result</span>,
              cell: (row) => (
                <Link to="/solve-history/$solveId" params={{ solveId: row.id }} className="underline underline-offset-3">
                  Open<span className="sr-only"> the result of the solve asked for at {hospitalTime(row.requested_at)}</span>
                </Link>
              ),
            },
          ]} />
      )}
      {page && <Older older_than={page.older_than} month={month} />}
      {page && month && <CompareTwoResults unitId={unit.id} month={month} rows={page.solves} a={pair.a} b={pair.b} />}
    </>
  )
}

function ManagerHistory({ user, month, older_than, pair }: { user: Me; month: string | undefined; older_than: string | undefined; pair: Pair }) {
  const { units, chosen } = useChosenUnit(user, undefined)
  return (
    <>
      {!chosen && <h1 className="text-xl font-medium">Solve history</h1>}
      {units.isPending && <p>Loading…</p>}
      {units.isError && <p role="alert">Your unit could not be read. Reload the page to try again.</p>}
      {units.data && !chosen && <p>No unit is assigned to you yet. The CNO assigns each unit its manager.</p>}
      {chosen && <UnitHistory key={chosen.id} unit={chosen} month={month} older_than={older_than} pair={pair} />}
    </>
  )
}

function HospitalHistory({ older_than }: { older_than: string | undefined }) {
  const history = useListHospitalSolveHistory(older_than ? { older_than } : undefined, { query: { placeholderData: keepPreviousData } })
  const heading = useFocusOnNewPage(older_than)
  const page = history.data
  return (
    <>
      <h1 ref={heading} tabIndex={-1} className="text-xl font-medium outline-none">Solve history</h1>
      <p className="text-muted-foreground">
        Every unit's solves, newest first, with times in hospital time. No roster or nurse appears here. A solve's own words could name a nurse, so a failed
        solve says only which kind of failure it was; its unit's manager reads the rest and can run it again. For the solver or the worker, their logs on the
        server say more.
      </p>
      {history.isPending && <p>Loading…</p>}
      {history.isError && <p role="alert">The solve history could not be read. Reload the page to try again.</p>}
      {page && page.solves.length === 0 && <p>No unit has solved a month yet.</p>}
      {page && page.solves.length > 0 && (
        <SolveHistoryTable label="Solves of every unit" solves={page.solves}
          leading={{ header: "Unit", cell: (row) => row.unit }}
          trailing={[
            { header: "Why it failed", cell: (row) => failedBecause(row.failure) },
            { header: "Time limit", cell: (row) => `${row.time_limit_seconds} s` },
            { header: "Attempts", end: true, cell: (row) => row.attempts },
            { header: "Cost", end: true, cell: (row) => row.cost ?? "None" },
          ]} />
      )}
      {page && <Older older_than={page.older_than} />}
    </>
  )
}

function SolveHistoryPage() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  const { month, older_than, a, b } = Route.useSearch()
  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4">
      {user.role === Role.system_admin
        ? <HospitalHistory older_than={older_than} />
        : <ManagerHistory user={user} month={month} older_than={older_than} pair={{ a, b }} />}
    </div>
  )
}
