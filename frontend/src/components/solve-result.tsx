import { createContext, useContext, type ReactNode } from "react"

import type { Following as FollowingRow, GridShift, MissedDayWhy, NurseAccounting, ReplanComparison, RuleMatch, RuleResult, SolveResult, SolverStatistics, Violation } from "@/api/generated/model"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { ruleName, signed, solverLine, stoppedBy } from "@/lib/solve-words"
import { longDay, scrollableRegion } from "@/lib/utils"

/** The level their heading takes depends on what is above: the roster page has the draft's heading and the result's page only its title. */
const HeadingLevel = createContext<"h2" | "h4">("h4")

function Panel({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  const Heading = useContext(HeadingLevel)
  return (
    <section aria-labelledby={id} className="flex flex-col gap-2">
      <Heading id={id} className="text-sm font-medium">{title}</Heading>
      {children}
    </section>
  )
}

function Accounting({ nurses, unitCost }: { nurses: NurseAccounting[]; unitCost: number | null }) {
  return (
    <Panel id="result-hours" title="Hours and cost per nurse">
      <Table container={scrollableRegion("Each nurse's hours and cost")}>
        <TableHeader>
          <TableRow>
            <TableHead>Nurse</TableHead>
            <TableHead className="text-end">Contract</TableHead>
            <TableHead className="text-end">Leave credit</TableHead>
            <TableHead className="text-end">Required</TableHead>
            <TableHead className="text-end">Worked</TableHead>
            <TableHead className="text-end">Overtime</TableHead>
            <TableHead className="text-end">Shortfall</TableHead>
            <TableHead>Shifts</TableHead>
            <TableHead className="text-end">Cost</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {nurses.map((nurse) => (
            <TableRow key={nurse.hospital_id}>
              <TableHead scope="row" className="font-normal">{nurse.full_name || nurse.hospital_id}</TableHead>
              {[nurse.contract, nurse.leave_credit, nurse.required, nurse.hours_worked, nurse.overtime, nurse.shortfall].map((hours, index) => (
                <TableCell key={index} className="text-end tabular-nums">{hours}</TableCell>
              ))}
              <TableCell className="tabular-nums">{Object.entries(nurse.shifts).filter(([, count]) => count > 0).map(([code, count]) => `${code} ${count}`).join(", ")}</TableCell>
              <TableCell className="text-end tabular-nums">{nurse.cost}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <p className="text-sm text-muted-foreground">In hours, apart from the shifts and the cost.{unitCost ? ` The unit as a whole costs ${unitCost} more, for what no one nurse is behind.` : ""}</p>
    </Panel>
  )
}

const MISSED_WHY: Record<MissedDayWhy, string> = {
  own_rules: "her hours, or her own rest and run rules, held no more",
  leave: "she was on leave or outside her dates",
  pinned: "her cell was pinned, or a request of hers accepted",
  elapsed: "the day was already over",
  barred: "a skill required to work had expired",
}

const shifts = (count: number) => (count === 1 ? "1 day shift" : `${count} day shifts`)

/** Each nurse on probation, and which of her preceptor's day shifts she did not follow, and why. */
function Following({ rows }: { rows: FollowingRow[] }) {
  return (
    <Panel id="result-following" title="Nurses on probation">
      <ul className="flex flex-col gap-2 text-sm">
        {rows.map((row) => {
          const name = row.full_name || row.hospital_id
          if (!row.preceptor) {
            return <li key={row.hospital_id}>{name} had no preceptor this month, so her day shifts were placed freely.</li>
          }
          const preceptor = row.preceptor_name || row.preceptor
          const all = row.followed + row.missed.length
          const said = all === 0 ? `${preceptor}, her preceptor, works no day shift she may this month, so her day shifts were placed freely.`
            : row.missed.length === 0 ? `${name} works beside ${preceptor}, her preceptor, on all ${shifts(all)} of theirs.`
            : `${name} works beside ${preceptor}, her preceptor, on ${row.followed} of their ${shifts(all)}. Not on:`
          return (
            <li key={row.hospital_id} className="flex flex-col gap-1">
              <span>{said}</span>
              {row.missed.length > 0 && (
                <ul className="list-disc ps-5">
                  {row.missed.map((day) => <li key={day.date}>{longDay(day.date)}, {day.shift}: {MISSED_WHY[day.why]}</li>)}
                </ul>
              )}
            </li>
          )
        })}
      </ul>
    </Panel>
  )
}

function Occurrence({ match }: { match: RuleMatch }) {
  return <li className="wrap-anywhere">{solverLine(match)}</li>
}

function Rule({ rule }: { rule: RuleResult }) {
  const name = ruleName(rule.rule)
  return (
    <li className="flex flex-col gap-1 border-b py-2 last:border-b-0">
      <p className="flex justify-between gap-4 font-medium"><span>{name}</span><span className="tabular-nums">{rule.penalty}</span></p>
      {rule.amount !== null && (
        <p className="text-muted-foreground tabular-nums">{rule.amount} × {rule.weight}, its weight. Counted in: {rule.counted_in}.</p>
      )}
      {rule.largest.length > 0 && <ul className="list-disc ps-5">{rule.largest.map((match, index) => <Occurrence key={index} match={match} />)}</ul>}
      {rule.others > 0 && <p className="text-muted-foreground">and {rule.others} more like these</p>}
    </li>
  )
}

function Penalties({ rules, explained }: { rules: RuleResult[]; explained: boolean }) {
  return (
    <Panel id="result-rules" title="Penalties per rule">
      {!explained && <p className="text-sm">These totals are what the solver reported. The occurrences behind them could not be explained: the solver's code no longer reads this solve's input.</p>}
      {rules.length === 0
        ? <p className="text-sm">No soft rule is broken.</p>
        : (
          <>
            <ul className="flex flex-col text-sm">{rules.map((rule) => <Rule key={rule.rule} rule={rule} />)}</ul>
            <p className="text-sm text-muted-foreground">Every other soft rule costs nothing.</p>
          </>
        )}
    </Panel>
  )
}

function Violations({ id, title, rows, note }: { id: string; title: string; rows: Violation[]; note: string }) {
  return (
    <Panel id={id} title={title}>
      <p className="text-sm text-muted-foreground">{note}</p>
      <ul className="list-disc ps-5 text-sm">{rows.map((row, index) => <li key={index} className="wrap-anywhere">{solverLine(row)}</li>)}</ul>
    </Panel>
  )
}

function Precheck({ issues }: { issues: string[] }) {
  return (
    <Panel id="result-precheck" title="The solver's check">
      {issues.length === 0
        ? <p className="text-sm">Before searching, it warned of nothing.</p>
        : <ul className="list-disc ps-5 text-sm">{issues.map((issue) => <li key={issue} className="wrap-anywhere">{issue}</li>)}</ul>}
    </Panel>
  )
}

const shown = (value: number | null | undefined, unit = "") => (value === null || value === undefined ? "Not known" : `${Math.round(value * 100) / 100}${unit}`)

function Statistics({ statistics }: { statistics: SolverStatistics }) {
  const rows: [string, string][] = [
    ["Outcome", statistics.status],
    ["What stopped it", stoppedBy(statistics.stopped_by)],
    ["Searched for", shown(statistics.solve_seconds, " s")],
    ["Time limit", shown(statistics.time_limit_seconds, " s")],
    ["Improving rosters", shown(statistics.improvements)],
    ["Cost as the search counted it", shown(statistics.objective)],
    ["Lowest cost possible, as far as it proved", shown(statistics.best_bound)],
    ["Workers", shown(statistics.workers)],
    ["Building the model took", shown(statistics.build_seconds, " s")],
    ["Deterministic time", shown(statistics.deterministic_time)],
    ["Model variables", shown(statistics.variables)],
    ["Model constraints", shown(statistics.constraints)],
    ["Model objective terms", shown(statistics.objective_terms)],
  ]
  return (
    <Panel id="result-statistics" title="Solver statistics">
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm tabular-nums">
        {rows.map(([name, value]) => (
          <div key={name} className="contents">
            <dt className="text-muted-foreground">{name}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </Panel>
  )
}

const cellCount = (count: number) => `${count} ${count === 1 ? "cell" : "cells"}`

const REPLAN_SAYS: Record<NonNullable<ReplanComparison["outcome"]>, (version: number | null) => string> = {
  version: (version) => `Its roster is version ${version}, in force. The unit's nurses were told the roster changed.`,
  unchanged: () => "It changed no cell, so no version was made and nobody was told.",
  outdated: () => "Its roster was not put in force: the roster in force or the month changed while it ran, and it would have undone that. Re-plan again to take it in.",
  month_over: () => "Its roster was not put in force: the month ended while it ran.",
}

/** A re-plan against the roster in force it started from: what it kept, what it changed, and what that cost, rule by rule. Both
 *  are costed on the re-plan's own input, so they compare. */
function Replan({ replan, shifts }: { replan: ReplanComparison; shifts: GridShift[] }) {
  const names = new Map(shifts.map((shift) => [shift.code, shift.display_name]))
  const shift = (code: string) => (code ? names.get(code) ?? code : "Off")
  return (
    <Panel id="result-replan" title="Against the roster in force">
      <p className="text-sm">
        Re-planned from {longDay(replan.replan_from)}, keeping every earlier day of version {replan.based_on} as it happened, with the leave and pins recorded since.{" "}
        {replan.outcome ? REPLAN_SAYS[replan.outcome](replan.version) : "It kept no roster, so the roster in force stayed as it was."}
      </p>
      {!!replan.recorded_cells_differ && (
        <p className="text-sm">{cellCount(replan.recorded_cells_differ)} before the date differed from version {replan.based_on}: leave or a pin recorded since, which the re-plan kept as fact.</p>
      )}
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm tabular-nums">
        <dt className="text-muted-foreground">Cost before</dt>
        <dd>{replan.cost_before ?? "Not known"}</dd>
        <dt className="text-muted-foreground">Cost after</dt>
        <dd>{replan.cost_after ?? "No roster"}</dd>
      </dl>
      {replan.rules.length > 0 && (
        <>
          <p id="replan-rules" className="text-sm font-medium">Each rule, before and after</p>
          <Table aria-labelledby="replan-rules" container={scrollableRegion("Each rule, before and after")}>
            <TableHeader>
              <TableRow>
                <TableHead>Rule</TableHead>
                <TableHead className="text-end">Before</TableHead>
                <TableHead className="text-end">After</TableHead>
                <TableHead className="text-end">Difference</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {replan.rules.map((rule) => (
                <TableRow key={rule.rule}>
                  <TableHead scope="row" className="font-normal">{ruleName(rule.rule)}</TableHead>
                  <TableCell className="text-end tabular-nums">{rule.before}</TableCell>
                  <TableCell className="text-end tabular-nums">{rule.after}</TableCell>
                  <TableCell className="text-end tabular-nums">{signed(rule.after - rule.before)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <p className="text-sm text-muted-foreground">The roster in force is where the re-plan starts from, so it pays nothing for changes: the re-plan's cost includes a price for each cell it changed.</p>
        </>
      )}
      {replan.changes.length === 0
        ? <p className="text-sm">No cell differs from version {replan.based_on}.</p>
        : (
          <>
          <p id="replan-cells" className="text-sm font-medium">Cells it changed</p>
          <Table aria-labelledby="replan-cells" container={scrollableRegion("Cells it changed")}>
            <TableHeader>
              <TableRow>
                <TableHead>Nurse</TableHead>
                <TableHead>Date</TableHead>
                <TableHead>Before</TableHead>
                <TableHead>After</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {replan.changes.map((change) => (
                <TableRow key={change.hospital_id + change.date}>
                  <TableHead scope="row" className="font-normal">{change.full_name || change.hospital_id}</TableHead>
                  <TableCell className="tabular-nums">{longDay(change.date)}</TableCell>
                  <TableCell>{shift(change.before)}</TableCell>
                  <TableCell>{shift(change.after)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          </>
        )}
    </Panel>
  )
}

/** Everything a result holds but its grid, for the draft beside it and for any result opened from the history. */
export function SolveResultPanels({ result, headingLevel = "h4" }: { result: SolveResult; headingLevel?: "h2" | "h4" }) {
  return (
    <HeadingLevel.Provider value={headingLevel}>
    <div className="flex flex-col gap-6">
      {result.replan && <Replan replan={result.replan} shifts={result.grid?.shifts ?? []} />}
      {result.nurses.length > 0 && <Accounting nurses={result.nurses} unitCost={result.unit_cost} />}
      {result.following.length > 0 && <Following rows={result.following} />}
      {result.grid && <Penalties rules={result.rules} explained={result.explained} />}
      {result.violations.length > 0 && (
        <Violations id="result-violations" title="Hard rules this roster breaks" rows={result.violations} note="A roster that breaks one does not become a draft." />
      )}
      {result.elapsed_violations.length > 0 && (
        <Violations id="result-facts" title="Recorded facts that broke rules" rows={result.elapsed_violations} note="These days had happened, so no solve could change them. They are facts, not errors." />
      )}
      {result.statistics && <Precheck issues={result.precheck} />}
      {result.statistics && <Statistics statistics={result.statistics} />}
    </div>
    </HeadingLevel.Provider>
  )
}
