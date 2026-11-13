import { Link } from "@tanstack/react-router"

import { useListExpiries } from "@/api/generated/endpoints"
import { ExpiryState, type ExpiryRow, type ExpiryRowKind } from "@/api/generated/model"
import { Badge } from "@/components/ui/badge"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { KINDS } from "@/lib/skill-kinds"
import { scrollableRegion, shortDay } from "@/lib/utils"

const STATE_WORDS: Record<ExpiryState, string> = {
  expired: "Expired",
  expiring: "Expiring",
  no_record: "No record",
}

/** What the list holds and what it holding nothing means, said once for the page, the manager's Nurses page and the home. The
 *  horizon is the API's, fixed, so "expiring" means one thing across the hospital. */
const listed = (days: number) =>
  `skills that have expired or expire within ${days} days, and skills required to work that a nurse has no record of`
const nothingListed = (days: number) =>
  `no skill has expired or expires within ${days} days, and every nurse has a record of each skill required to work`

const KIND_WORDS = Object.fromEntries(KINDS.map(({ kind, one }) => [kind, one])) as Record<ExpiryRowKind, string>

function State({ state }: { state: ExpiryState }) {
  return <Badge variant={state === ExpiryState.expired ? "destructive" : "outline"}>{STATE_WORDS[state]}</Badge>
}

function ExpiriesTable({ rows, label, showUnit }: { rows: ExpiryRow[]; label: string; showUnit: boolean }) {
  return (
    <Table container={scrollableRegion(label)}>
      <TableHeader>
        <TableRow>
          <TableHead>Name</TableHead>
          <TableHead>Hospital ID</TableHead>
          {showUnit && <TableHead>Unit</TableHead>}
          <TableHead>Skill</TableHead>
          <TableHead>Kind</TableHead>
          <TableHead>State</TableHead>
          <TableHead>Expiry</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => (
          <TableRow key={`${row.nurse_id} ${row.skill}`}>
            <TableCell>{row.full_name}</TableCell>
            <TableCell translate="no">{row.hospital_id}</TableCell>
            {showUnit && <TableCell>{row.unit.name}</TableCell>}
            <TableCell>{row.skill}</TableCell>
            <TableCell>{KIND_WORDS[row.kind]}</TableCell>
            <TableCell><State state={row.state} /></TableCell>
            <TableCell className="tabular-nums">
              {row.expiry ? shortDay(row.expiry) : <span className="text-muted-foreground">Required to work</span>}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

/** The list as the reader may see it: every unit's for the CNO and the Deputy CNO, a manager's own unit's. Oldest date first,
 *  then the skills nobody has recorded, as the API orders them. `who` says who records skills and where, in the reader's words: a sentence. */
export function ExpiriesList({ label, showUnit, who }: { label: string; showUnit: boolean; who: string }) {
  const expiries = useListExpiries()
  if (expiries.isPending) return <p>Loading the expiries…</p>
  if (expiries.isError) return <p role="alert">The expiries could not be read. Reload the page to try again.</p>
  const { horizon_days: days, rows } = expiries.data
  const words = listed(days)
  return (
    <>
      <p className="text-muted-foreground">
        {words[0].toUpperCase() + words.slice(1)}. Deactivated nurses and those past their end date are left out. {who}
      </p>
      {rows.length === 0
        ? <p>Nothing to list: {nothingListed(days)}.</p>
        : <ExpiriesTable rows={rows} label={label} showUnit={showUnit} />}
    </>
  )
}

/** The CNO's home: how many rows the page has, read afresh, so it always matches the page. */
export function ExpiriesCount() {
  const expiries = useListExpiries()
  if (expiries.isPending) return null
  if (expiries.isError) return <p role="alert">The expiries could not be read, so their count is not shown. Reload the page to try again.</p>
  const { count, horizon_days: days } = expiries.data
  const link = <Link to="/expiries" className="underline underline-offset-3">the Expiries page</Link>
  if (count === 0) return <p>Nothing is on {link}: {nothingListed(days)}.</p>
  return <p>{count} {count === 1 ? "entry is" : "entries are"} on {link}: {listed(days)}.</p>
}
