import { useQueryClient } from "@tanstack/react-query"
import { createFileRoute, Link, useRouteContext } from "@tanstack/react-router"
import { useRef, useState } from "react"

import {
  getGetRosterMonthQueryKey,
  getGetSolveResultQueryKey,
  getListUnitSolveHistoryQueryKey,
  getPreviewSolveQueryKey,
  useGetSolveResult,
  useRestoreSolveResult,
} from "@/api/generated/endpoints"
import { Role, type SolveResult } from "@/api/generated/model"
import { ConfirmDialog } from "@/components/confirm-dialog"
import { GridLegend, RosterGrid } from "@/components/roster-grid"
import { SolveResultPanels } from "@/components/solve-result"
import { Button } from "@/components/ui/button"
import { useChosenUnit } from "@/components/unit-picker"
import { plainRefusal } from "@/lib/server-errors"
import { replanStatus, solveDuration, solveStatus } from "@/lib/solve-words"
import { hospitalTime, longDay, monthName } from "@/lib/utils"
import { onlyFor } from "@/session"

export const Route = createFileRoute("/_signed-in/solve-history_/$solveId")({
  beforeLoad: onlyFor(Role.unit_manager),
  staticData: { title: "Solve result" },
  component: SolveResultPage,
})

const LINK = "underline underline-offset-3"

function Restoring({ unitId, result, restored, onRestored }: { unitId: string; result: SolveResult; restored: boolean; onRestored: () => void }) {
  const queryClient = useQueryClient()
  const restore = useRestoreSolveResult()
  const [asking, setAsking] = useState(false)
  // Nowhere until it works, so that Cancel returns to the button. Once it works the button is gone.
  const focusAfter = useRef<string | null>(null)
  const { solve } = result
  const month = monthName(solve.month)
  const confirm = () => restore.mutate({ unitId, solveId: solve.id }, {
    onSuccess: (answer) => {
      queryClient.setQueryData(getGetSolveResultQueryKey(unitId, solve.id), answer)
      void queryClient.invalidateQueries({ queryKey: getGetRosterMonthQueryKey(unitId) })
      void queryClient.invalidateQueries({ queryKey: getPreviewSolveQueryKey(unitId) })
      void queryClient.invalidateQueries({ queryKey: getListUnitSolveHistoryQueryKey(unitId) })
      focusAfter.current = "restore-state"
      setAsking(false)
      onRestored()
    },
    // What stopped it may be for good now, or may be new, so the page should say the same as the dialog.
    onError: () => void queryClient.invalidateQueries({ queryKey: getGetSolveResultQueryKey(unitId, solve.id) }),
  })
  return (
    <section aria-label="Restoring this result" className="flex flex-col items-start gap-2">
      {/* Always rendered, because a live region mounted with its text may never be read out. */}
      <p id="restore-state" role="status" tabIndex={-1} className="text-sm outline-none empty:sr-only">
        {restored && solve.in_draft ? `This result is now the draft of ${month}. What the draft held before is still in the solve history.` : ""}
      </p>
      {!restored && solve.in_draft && <p className="text-sm">This result is the draft of {month}.</p>}
      {solve.in_draft && <Link to="/roster" search={{ month: solve.month }} className={`text-sm ${LINK}`}>See the draft</Link>}
      {!solve.in_draft && result.restore_refusal && <p className="text-sm">{result.restore_refusal}</p>}
      {!solve.in_draft && !result.restore_refusal && <Button variant="outline" onClick={() => { restore.reset(); setAsking(true) }}>Restore as the draft</Button>}
      <ConfirmDialog
        question={asking ? {
          title: "Restore this result as the draft?",
          body: `Its grid replaces the draft of ${month}. The solve history keeps what it replaces.`,
          confirm: "Restore",
        } : null}
        pending={restore.isPending} failed={restore.isError} failure={plainRefusal(restore.error)} focusAfter={focusAfter}
        onConfirm={confirm} onCancel={() => setAsking(false)} />
    </section>
  )
}

function Why({ result }: { result: SolveResult }) {
  const { solve, error, message } = result
  if (solve.failure === "refused") {
    return (
      <div className="flex flex-col gap-1">
        <p>The solver could not take the rules as they stood. <Link to="/unit-rules" className={LINK}>Review the unit rules</Link>, or ask the CNO if this names a hospital rule.</p>
        <p className="text-sm text-muted-foreground">It says: {error}</p>
      </div>
    )
  }
  if (solve.state === "failed") return <p>The solve failed and gave no answer. It says: {error}</p>
  return (
    <>
      {message && <p>The solver says: {message}</p>}
      {error && <p className="text-sm text-muted-foreground">An earlier attempt failed: {error}</p>}
    </>
  )
}

/** A re-plan is never restored: what it did to the roster in force is said beside its comparison. */
function ReplanLink({ month }: { month: string }) {
  return <Link to="/roster" search={{ month }} className={`text-sm ${LINK}`}>See the roster in force</Link>
}

function Result({ unit, solveId }: { unit: { id: string; name: string }; solveId: string }) {
  const answer = useGetSolveResult(unit.id, solveId)
  const [restored, setRestored] = useState(false)
  const result = answer.data
  if (answer.isPending) return <p>Loading…</p>
  if (!result) return <p role="alert">This result could not be read. Reload the page to try again, or go back to the <Link to="/solve-history" className={LINK}>solve history</Link>.</p>
  const { solve, grid } = result
  const month = monthName(solve.month)
  return (
    <>
      <h1 className="text-xl font-medium">Result for {month}</h1>
      <p className="flex flex-wrap gap-x-4 gap-y-1">
        <Link to="/solve-history" search={{ month: solve.month }} className={LINK}>Solve history of {month}</Link>
        {solve.found_roster && <Link to="/solve-history" search={{ month: solve.month, a: solve.id }} className={LINK}>Compare with another result</Link>}
      </p>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm tabular-nums">
        <dt className="text-muted-foreground">Status</dt>
        <dd>{solve.replan_from ? replanStatus(solve) : solveStatus(solve)}</dd>
        {solve.replan_from && (
          <>
            <dt className="text-muted-foreground">Re-planned from</dt>
            <dd>{longDay(solve.replan_from)}</dd>
          </>
        )}
        <dt className="text-muted-foreground">Asked for by</dt>
        <dd>{solve.requested_by}</dd>
        <dt className="text-muted-foreground">Asked for at</dt>
        <dd>{hospitalTime(solve.requested_at)}</dd>
        <dt className="text-muted-foreground">Duration</dt>
        <dd>{solveDuration(solve)}</dd>
        <dt className="text-muted-foreground">Cost</dt>
        <dd>{solve.cost ?? "None"}</dd>
      </dl>
      <Why result={result} />
      {solve.replan_from
        ? <ReplanLink month={solve.month} />
        : <Restoring unitId={unit.id} result={result} restored={restored} onRestored={() => setRestored(true)} />}
      {grid && (
        <section aria-label="The roster it answered" className="flex flex-col gap-4">
          <RosterGrid grid={grid} label={`Roster of this result, ${month}`} />
          <GridLegend grid={grid} />
        </section>
      )}
      <SolveResultPanels result={result} headingLevel="h2" />
    </>
  )
}

function SolveResultPage() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  const { solveId } = Route.useParams()
  const { units, chosen } = useChosenUnit(user, undefined)
  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-4">
      {units.isPending && <p>Loading…</p>}
      {units.isError && <p role="alert">Your unit could not be read. Reload the page to try again.</p>}
      {units.data && !chosen && <p>No unit is assigned to you yet. The CNO assigns each unit its manager.</p>}
      {chosen && <Result key={solveId} unit={chosen} solveId={solveId} />}
    </div>
  )
}
