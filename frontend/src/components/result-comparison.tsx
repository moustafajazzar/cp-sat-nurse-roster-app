import { useNavigate } from "@tanstack/react-router"
import type { ReactNode } from "react"

import { useCompareSolveResults } from "@/api/generated/endpoints"
import type {
  ComparedCell, ComparedHours, ComparedRule, ComparedSolve, HeadcountRule, InputChange, NurseAccounting, NurseInputChangeKindsItem, ResultComparison, RuleSide,
  SkillNeedRule, UnitSolveRow, Violation, ViolationGroups,
} from "@/api/generated/model"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { HOSPITAL_RULE_LABELS, WEEKDAYS } from "@/lib/hospital-rules-words"
import { plainRefusal } from "@/lib/server-errors"
import { replanStatus, ruleName, signed, solveDuration, solverLine, solveStatus, stoppedBy } from "@/lib/solve-words"
import { SCORE_MODES, UNIT_RULE_LABELS } from "@/lib/unit-rules-words"
import { hospitalTime, longDay, monthName, scrollableRegion, shortDay, WRAP_ON_PHONE } from "@/lib/utils"

/** Two results of one month side by side, for the unit's manager: the older on the left, the newer on the right,
 *  every difference newer minus older. Read-only: a result is restored as the draft from its own page. */

const NUM = "text-end tabular-nums"
const plural = (count: number, one: string, many = `${one}s`) => `${count} ${count === 1 ? one : many}`
const shiftShown = (code: string) => code || "Off"
const NOT_IN_INPUT = "Not in its input"

const KIND_WORDS: Record<NurseInputChangeKindsItem, string> = {
  joined: "joined the month",
  left: "left the month",
  attributes: "rostering attributes",
  leave: "leave",
  pins: "pins",
  carry_over: "carry-over",
  preferences: "preferences",
  skills: "skills",
  probation: "probation or preceptor",
}

const GROUPS: { group: InputChange["group"]; title: string }[] = [
  { group: "hospital_rules", title: "Hospital rules" },
  { group: "unit_rules", title: "Unit rules" },
  { group: "time_limit", title: "The search" },
  { group: "calendar", title: "Calendar" },
]

function fieldLabel({ group, field }: InputChange): string {
  if (group === "hospital_rules") return HOSPITAL_RULE_LABELS[field] ?? field
  if (group === "unit_rules") return UNIT_RULE_LABELS[field] ?? field
  if (group === "time_limit") return "Time limit"
  return field === "ramadan_days" ? "Ramadan days" : "Public holidays"
}

const dates = (value: unknown) => ((value as string[]).length ? (value as string[]).map(shortDay).join(", ") : "None")
const codes = (value: unknown) => ((value as string[]).length ? (value as string[]).join(", ") : "None")

/** A rule's value as a person reads it, in the rules pages' words where they have some. */
function valueShown({ group, field }: InputChange, value: unknown): string {
  if (value === null || value === undefined) return NOT_IN_INPUT
  if (typeof value === "boolean") return value ? "On" : "Off"
  if (group === "time_limit") return `${value} s`
  if (group === "calendar") return dates(value)
  if (field === "weekend_weekdays") {
    const days = new Set(value as number[])
    return WEEKDAYS.filter(([day]) => days.has(day)).map(([, name]) => name).join(", ") || "None"
  }
  if (field === "forbidden_successions") {
    return (value as string[][]).map(([today, tomorrow]) => `${today} then ${tomorrow}`).join(", ") || "None"
  }
  if (field === "shift_hours") return Object.entries(value as Record<string, number>).map(([code, hours]) => `${code} ${hours} h`).join(", ")
  if (field === "headcounts") {
    return (value as HeadcountRule[])
      .map((row) => `${row.shift}: weekday ${row.weekday_min}–${row.weekday_max}, weekend day ${row.weekend_min}–${row.weekend_max}`
        + (row.also_counts.length ? `, ${row.also_counts.join(" and ")} also counting` : "")).join("; ") || "None"
  }
  if (field === "skill_needs") {
    return (value as SkillNeedRule[])
      .map((need) => `${need.shift} needs ${need.skill}: weekday ${need.weekday_min}, weekend day ${need.weekend_min}`).join("; ") || "None"
  }
  if (field === "score_balance_mode") return SCORE_MODES[value as keyof typeof SCORE_MODES] ?? String(value)
  if (Array.isArray(value)) return codes(value)
  if (typeof value === "object") return JSON.stringify(value)
  return String(value)
}

function Part({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className="flex flex-col gap-2">
      <h3 id={id} className="font-medium">{title}</h3>
      {children}
    </section>
  )
}

/** Each column's heading: which of the two, and when it was asked for, as the pickers name it. */
const sideName = (which: "Older" | "Newer", solve: ComparedSolve) => `${which}, asked for at ${hospitalTime(solve.requested_at)}`

function Sides({ older, newer, first = "" }: { older: ComparedSolve; newer: ComparedSolve; first?: string }) {
  return (
    <TableRow>
      <TableHead>{first}</TableHead>
      <TableHead>{sideName("Older", older)}</TableHead>
      <TableHead>{sideName("Newer", newer)}</TableHead>
    </TableRow>
  )
}

function Facts({ older, newer }: { older: ComparedSolve; newer: ComparedSolve }) {
  const rows: [string, (solve: ComparedSolve) => ReactNode][] = [
    ["Status", (solve) => (solve.replan_from ? replanStatus(solve) : solveStatus(solve))],
    ["Re-planned from", (solve) => (solve.replan_from ? `${longDay(solve.replan_from)}, of version ${solve.replan_of}` : "Not a re-plan")],
    ["Asked for by", (solve) => solve.requested_by],
    ["Duration", solveDuration],
    ["Time limit", (solve) => `${solve.time_limit_seconds} s`],
    ["Cost", (solve) => solve.cost ?? "None"],
    ["Of which the unit as a whole", (solve) => solve.unit_cost ?? "Not known"],
    ["The draft", (solve) => (solve.in_draft ? "Yes" : "No")],
  ]
  return (
    <Part id="compare-facts" title="The two solves">
      <Table container={scrollableRegion("What each solve was")} className={WRAP_ON_PHONE}>
        <TableHeader><Sides older={older} newer={newer} /></TableHeader>
        <TableBody>
          {rows.map(([name, value]) => (
            <TableRow key={name}>
              <TableHead scope="row" className="font-normal">{name}</TableHead>
              <TableCell className="tabular-nums">{value(older)}</TableCell>
              <TableCell className="tabular-nums">{value(newer)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Part>
  )
}

function Inputs({ answer }: { answer: ResultComparison }) {
  const { changes, nurses } = answer.inputs
  const { older, newer } = answer
  return (
    <Part id="compare-inputs" title="What changed between their inputs">
      {changes.length === 0 && nurses.length === 0 && <p className="text-sm">Both solves were given the same month.</p>}
      {changes.length > 0 && (
        <Table container={scrollableRegion("Rules that differ between the two inputs")} className="[&_td]:min-w-40 [&_td]:whitespace-normal">
          <TableHeader><Sides older={older} newer={newer} first="Rule" /></TableHeader>
          {GROUPS.filter(({ group }) => changes.some((change) => change.group === group)).map(({ group, title }) => (
            <TableBody key={group}>
              <TableRow><TableHead colSpan={3} scope="colgroup" className="font-medium">{title}</TableHead></TableRow>
              {changes.filter((change) => change.group === group).map((change) => (
                <TableRow key={change.field}>
                  <TableHead scope="row" className="font-normal whitespace-normal">{fieldLabel(change)}</TableHead>
                  <TableCell className="tabular-nums wrap-anywhere">{valueShown(change, change.older)}</TableCell>
                  <TableCell className="tabular-nums wrap-anywhere">{valueShown(change, change.newer)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          ))}
        </Table>
      )}
      {changes.some((change) => change.older === null || change.newer === null) && (
        <p className="text-sm text-muted-foreground">"{NOT_IN_INPUT}" is a rule the solve was not given: it was stored before the rule existed.</p>
      )}
      {nurses.length > 0 && (
        <>
          <p className="text-sm font-medium">Nurses whose month differs</p>
          <ul className="list-disc ps-5 text-sm">
            {nurses.map((nurse) => (
              <li key={nurse.hospital_id}>{nurse.full_name || nurse.hospital_id}: {nurse.kinds.map((kind) => KIND_WORDS[kind]).join(", ")}</li>
            ))}
          </ul>
        </>
      )}
      {(older.replan_from || newer.replan_from) && (
        <p className="text-sm text-muted-foreground">A re-plan also started from the roster in force on its date, as its facts above say.</p>
      )}
    </Part>
  )
}

/** One side of a rule. Empty for the `change` rule of a solve that was no re-plan, which has none to price. */
function SideCells({ side, explained }: { side: RuleSide; explained: boolean }) {
  if (side.penalty === null) {
    return <><TableCell className={NUM}><span className="sr-only">None</span></TableCell><TableCell /><TableCell /></>
  }
  const known = (value: number | null) => (value === null ? (explained ? "—" : "Not known") : value)
  return (
    <>
      <TableCell className={NUM}>{known(side.amount)}</TableCell>
      <TableCell className={NUM}>{known(side.weight)}</TableCell>
      <TableCell className={NUM}>{side.penalty}</TableCell>
    </>
  )
}

function Rules({ answer }: { answer: ResultComparison }) {
  const { rules, older, newer, explained_older, explained_newer } = answer
  const differs = rules.some((rule) => rule.weight_differs)
  const change = rules.some((rule) => rule.rule === "change")
  const unexplained = [!explained_older && "older", !explained_newer && "newer"].filter(Boolean)
  return (
    <Part id="compare-rules" title="Each rule, side by side">
      {unexplained.length > 0 && (
        <p className="text-sm">
          The {unexplained.join(" and the ")} result's amounts and weights are not known: the solver's code no longer reads its input. Its penalties are
          the totals it reported.
        </p>
      )}
      {rules.length === 0
        ? <p className="text-sm">No soft rule costs anything in either result.</p>
        : (
          <>
            <Table container={scrollableRegion("Each rule's amount, weight and penalty in both")}>
              <TableHeader>
                <TableRow>
                  <TableHead rowSpan={2}>Rule</TableHead>
                  <TableHead colSpan={3} scope="colgroup" className="text-center">{sideName("Older", older)}</TableHead>
                  <TableHead colSpan={3} scope="colgroup" className="text-center">{sideName("Newer", newer)}</TableHead>
                  <TableHead rowSpan={2} className="text-end">Difference of penalty</TableHead>
                </TableRow>
                <TableRow>
                  {["older", "newer"].flatMap((which) => ["Amount", "Weight", "Penalty"].map((name) => (
                    <TableHead key={which + name} className="text-end">{name}<span className="sr-only">, {which}</span></TableHead>
                  )))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {rules.map((rule: ComparedRule) => (
                  <TableRow key={rule.rule}>
                    <TableHead scope="row" className="font-normal">
                      {ruleName(rule.rule)}
                      {rule.weight_differs && <span className="ms-2 rounded-sm border px-1 text-xs">Weight differs</span>}
                    </TableHead>
                    <SideCells side={rule.older} explained={explained_older} />
                    <SideCells side={rule.newer} explained={explained_newer} />
                    <TableCell className={NUM}>{signed((rule.newer.penalty ?? 0) - (rule.older.penalty ?? 0))}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {differs && (
              <p className="text-sm">Where the weight differs, the two penalties were priced differently: compare the amounts, which are counted alike.</p>
            )}
            {change && (
              <p className="text-sm text-muted-foreground">{ruleName("change")} is the price a re-plan pays for each cell it moves from the roster in force. A solve that is no re-plan has none, so its side is empty.</p>
            )}
            <p className="text-sm text-muted-foreground">Every other soft rule costs nothing in both.</p>
          </>
        )}
    </Part>
  )
}

/** The cells, in the answer's order of name then date, a group for each nurse. */
function byNurse(cells: ComparedCell[]): ComparedCell[][] {
  const groups: ComparedCell[][] = []
  for (const cell of cells) {
    const last = groups.at(-1)
    if (last && last[0].hospital_id === cell.hospital_id) last.push(cell)
    else groups.push([cell])
  }
  return groups
}

function Cells({ answer }: { answer: ResultComparison }) {
  const { cells, older, newer } = answer
  const onlyOne = answer.inputs.nurses.some((nurse) => nurse.kinds.includes("joined") || nurse.kinds.includes("left"))
  return (
    <Part id="compare-cells" title="Cells that differ">
      <p className="text-sm">{cells.length === 0 ? "No cell differs between the two rosters." : `${plural(cells.length, "cell")} ${cells.length === 1 ? "differs" : "differ"}.`}</p>
      {onlyOne && <p className="text-sm text-muted-foreground">A nurse in only one of the two is named under what changed, not here.</p>}
      {cells.length > 0 && (
        <Table container={scrollableRegion("Each cell that differs")}>
          <TableHeader>
            <TableRow>
              <TableHead>Nurse</TableHead>
              <TableHead>Date</TableHead>
              <TableHead>{sideName("Older", older)}</TableHead>
              <TableHead>{sideName("Newer", newer)}</TableHead>
            </TableRow>
          </TableHeader>
          {byNurse(cells).map((rows) => (
            <TableBody key={rows[0].hospital_id} className="border-b last:border-b-0">
              {rows.map((cell, index) => (
                <TableRow key={cell.date} className="border-b-0">
                  {index === 0 && (
                    <TableHead scope="rowgroup" rowSpan={rows.length} className="align-top font-normal">
                      {cell.full_name || cell.hospital_id}<span className="block text-xs text-muted-foreground">{plural(rows.length, "cell")}</span>
                    </TableHead>
                  )}
                  <TableCell className="tabular-nums">{longDay(cell.date)}</TableCell>
                  <TableCell>{shiftShown(cell.older)}</TableCell>
                  <TableCell>{shiftShown(cell.newer)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          ))}
        </Table>
      )}
    </Part>
  )
}

const HOURS: { name: string; of: (row: NurseAccounting) => number }[] = [
  { name: "Contract", of: (row) => row.contract },
  { name: "Required", of: (row) => row.required },
  { name: "Worked", of: (row) => row.hours_worked },
  { name: "Overtime", of: (row) => row.overtime },
  { name: "Shortfall", of: (row) => row.shortfall },
  { name: "Cost", of: (row) => row.cost },
]

/** "180 → 168, −12", or the one figure when both agree. */
function change(older: number | undefined, newer: number | undefined): string {
  if (older === undefined) return `Not in the older → ${newer}`
  if (newer === undefined) return `${older} → not in the newer`
  return older === newer ? String(older) : `${older} → ${newer}, ${signed(newer - older)}`
}

function Hours({ nurses }: { nurses: ComparedHours[] }) {
  return (
    <Part id="compare-hours" title="Hours and cost per nurse">
      <Table container={scrollableRegion("Each nurse's hours and cost in both")}>
        <TableHeader>
          <TableRow>
            <TableHead>Nurse</TableHead>
            {HOURS.map(({ name }) => <TableHead key={name} className="text-end">{name}</TableHead>)}
          </TableRow>
        </TableHeader>
        <TableBody>
          {nurses.map((nurse) => (
            <TableRow key={nurse.hospital_id}>
              <TableHead scope="row" className="font-normal">{nurse.full_name || nurse.hospital_id}</TableHead>
              {HOURS.map(({ name, of }) => (
                <TableCell key={name} className={NUM}>{change(nurse.older ? of(nurse.older) : undefined, nurse.newer ? of(nurse.newer) : undefined)}</TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <p className="text-sm text-muted-foreground">In hours, apart from the cost: the older's figure, then the newer's and the difference where they differ.</p>
    </Part>
  )
}

function Grouped({ id, title, groups, none, note }: { id: string; title: string; groups: ViolationGroups; none: string; note?: string }) {
  const parts: [string, Violation[]][] = [["In both", groups.both], ["Only in the older", groups.only_older], ["Only in the newer", groups.only_newer]]
  const empty = parts.every(([, rows]) => rows.length === 0)
  return (
    <Part id={id} title={title}>
      {empty ? <p className="text-sm">{none}</p> : (
        <>
          {note && <p className="text-sm text-muted-foreground">{note}</p>}
          {parts.map(([name, rows]) => (
            <div key={name} className="flex flex-col gap-1">
              <p className="text-sm font-medium">{name}</p>
              {rows.length === 0
                ? <p className="text-sm text-muted-foreground">None.</p>
                : <ul className="list-disc ps-5 text-sm">{rows.map((row, index) => <li key={index} className="wrap-anywhere">{solverLine(row)}</li>)}</ul>}
            </div>
          ))}
        </>
      )}
    </Part>
  )
}

const figure = (value: number | null | undefined, unit = "") => (value === null || value === undefined ? "Not known" : `${Math.round(value * 100) / 100}${unit}`)

function Statistics({ older, newer }: { older: ComparedSolve; newer: ComparedSolve }) {
  const rows: [string, (solve: ComparedSolve) => string][] = [
    ["What stopped it", (solve) => stoppedBy(solve.statistics?.stopped_by ?? null)],
    ["Searched for", (solve) => figure(solve.statistics?.solve_seconds, " s")],
    ["Improving rosters", (solve) => figure(solve.statistics?.improvements)],
    ["Cost as the search counted it", (solve) => figure(solve.statistics?.objective)],
    ["Lowest cost possible, as far as it proved", (solve) => figure(solve.statistics?.best_bound)],
  ]
  return (
    <Part id="compare-statistics" title="Solver statistics">
      <Table container={scrollableRegion("Solver statistics of both")} className={WRAP_ON_PHONE}>
        <TableHeader><Sides older={older} newer={newer} /></TableHeader>
        <TableBody>
          {rows.map(([name, value]) => (
            <TableRow key={name}>
              <TableHead scope="row" className="font-normal">{name}</TableHead>
              <TableCell className="tabular-nums">{value(older)}</TableCell>
              <TableCell className="tabular-nums">{value(newer)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Part>
  )
}

function Comparison({ unitId, a, b }: { unitId: string; a: string; b: string }) {
  const compared = useCompareSolveResults(unitId, { a, b })
  const answer = compared.data
  if (compared.isPending) return <p role="status" className="text-sm">Comparing…</p>
  if (!answer) {
    return (
      <p role="alert" className="text-sm">
        {plainRefusal(compared.error) ?? "These two results could not be compared."} Reload the page to try again, or choose another pair.
      </p>
    )
  }
  return (
    <div className="flex flex-col gap-6">
      <p className="text-sm text-muted-foreground">The older is on the left and the newer on the right, whichever way they were chosen. Each difference is the newer's figure less the older's. Each result is costed on its own input.</p>
      <Facts older={answer.older} newer={answer.newer} />
      <Inputs answer={answer} />
      <Rules answer={answer} />
      <Cells answer={answer} />
      <Hours nurses={answer.nurses} />
      <Grouped id="compare-violations" title="Hard rules each roster breaks" groups={answer.violations} none="Neither roster breaks a hard rule." />
      <Grouped id="compare-facts-broken" title="Recorded facts that broke rules" groups={answer.facts} none="No recorded fact broke a rule in either."
        note="These days had happened, so no solve could change them. They are facts, not errors." />
      <Statistics older={answer.older} newer={answer.newer} />
    </div>
  )
}

/** When it was asked for, which heads its column below, and its cost: short enough for a phone's picker. */
const optionName = (row: Pick<UnitSolveRow, "requested_at" | "cost">) => `${hospitalTime(row.requested_at)}, cost ${row.cost ?? "none"}`

function Picker({ id, label, value, rows, onChange }: { id: string; label: string; value: string; rows: UnitSolveRow[]; onChange: (id: string) => void }) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium">{label}</label>
      <NativeSelect id={id} value={value} onChange={(event) => onChange(event.target.value)}>
        {!rows.some((row) => row.id === value) && <NativeSelectOption value={value}>A result from an older page of the history</NativeSelectOption>}
        {rows.map((row) => <NativeSelectOption key={row.id} value={row.id}>{optionName(row)}</NativeSelectOption>)}
      </NativeSelect>
    </div>
  )
}

/** The month's results that hold a roster, from the history page's own rows, newest first. With neither in the address, the
 *  two newest; with one, the newest other. */
export function CompareTwoResults({ unitId, month, rows, a, b }: { unitId: string; month: string; rows: UnitSolveRow[]; a?: string; b?: string }) {
  const navigate = useNavigate({ from: "/solve-history" })
  const candidates = rows.filter((row) => row.found_roster)
  const first = a ?? (b ? candidates.find((row) => row.id !== b)?.id : candidates[0]?.id)
  const second = b ?? candidates.find((row) => row.id !== first)?.id
  const choose = (pair: { a: string; b: string }) => void navigate({ search: (previous) => ({ ...previous, ...pair }), replace: true })
  return (
    <section aria-labelledby="compare" className="flex flex-col gap-3 border-t pt-4">
      <h2 id="compare" className="text-lg font-medium">Compare two results</h2>
      {!first || !second
        ? <p className="text-sm">Fewer than two solves of {monthName(month)} hold a roster, so there is nothing to compare yet.</p>
        : (
          <>
            <div className="flex flex-wrap gap-3">
              <Picker id="compare-a" label="One result" value={first} rows={candidates.filter((row) => row.id !== second)} onChange={(id) => choose({ a: id, b: second })} />
              <Picker id="compare-b" label="The other" value={second} rows={candidates.filter((row) => row.id !== first)} onChange={(id) => choose({ a: first, b: id })} />
            </div>
            <Comparison unitId={unitId} a={first} b={second} />
          </>
        )}
    </section>
  )
}
