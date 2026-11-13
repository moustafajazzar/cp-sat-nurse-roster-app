import { useState } from "react"

import { Link } from "@tanstack/react-router"

import { getExportRosterVersionUrl, useCompareRosterVersions, useListRosterVersions } from "@/api/generated/endpoints"
import type { ExportRosterVersionFormat, VersionRow } from "@/api/generated/model"
import { plural } from "@/components/roster-notes"
import { buttonVariants } from "@/components/ui/button"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { hospitalTime, longDay } from "@/lib/utils"

/** The versions of an approved roster and what changed between any two, for the unit's manager and the approvers. A version's
 *  reason can say a cell was leave, so no nurse reads this: they see the roster in force on My roster. */

const ORIGIN: Record<VersionRow["origin"], string> = {
  solve: "Approved from a solve",
  edit: "Changed by hand",
  replan: "Re-planned",
  swap: "Swap",
  request: "Request",
  cover: "Cover taken",
}

const shown = (code: string) => code || "Off"
const FILE_LINK = buttonVariants({ variant: "outline", size: "sm" })

/** A version's print page and its two files. Each file taken is in the audit log, as the staff list is; the print page is
 *  not, since it makes no file. A file holds a day of leave as a day off, and never a leave's type. */
function VersionFiles({ unitId, month, number }: { unitId: string; month: string; number: number }) {
  const file = (format: ExportRosterVersionFormat) => getExportRosterVersionUrl(unitId, { month, version: number, format })
  return (
    <div className="flex flex-wrap gap-1">
      <Link to="/roster/print" search={{ unit: unitId, month, version: number }} className={FILE_LINK} aria-label={`Print version ${number}`}>Print</Link>
      <a href={file("csv")} download className={FILE_LINK} aria-label={`Version ${number} as CSV`}>CSV</a>
      <a href={file("xlsx")} download className={FILE_LINK} aria-label={`Version ${number} as Excel`}>Excel</a>
    </div>
  )
}

function Comparison({ unitId, month, from, to }: { unitId: string; month: string; from: number; to: number }) {
  const compared = useCompareRosterVersions(unitId, { month, from_version: from, to_version: to })
  const changes = compared.data?.changes
  return (
    <div className="flex flex-col gap-2">
      <p role="status" className="text-sm font-medium">
        {compared.isError ? "These versions could not be compared. Reload the page to try again."
          : !changes ? "Comparing…"
          : from === to ? "That is the same version twice, so nothing differs."
          : changes.length === 0 ? `Version ${to} holds the same cells as version ${from}.`
          : `${plural(changes.length, "cell")} ${changes.length === 1 ? "differs" : "differ"} from version ${from} to version ${to}.`}
      </p>
      {changes && changes.length > 0 && (
        <div className="overflow-x-auto">
          <Table>
            <caption className="sr-only">Cells that differ from version {from} to version {to}</caption>
            <TableHeader>
              <TableRow>
                <TableHead scope="col">Nurse</TableHead>
                <TableHead scope="col">Date</TableHead>
                <TableHead scope="col">Version {from}</TableHead>
                <TableHead scope="col">Version {to}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {changes.map((change) => (
                <TableRow key={change.hospital_id + change.date}>
                  <TableCell>{change.full_name || change.hospital_id}</TableCell>
                  <TableCell className="tabular-nums">{longDay(change.date)}</TableCell>
                  <TableCell>{shown(change.before)}</TableCell>
                  <TableCell>{shown(change.after)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  )
}

function VersionPicker({ id, label, value, numbers, onChange }: { id: string; label: string; value: number; numbers: number[]; onChange: (value: number) => void }) {
  return (
    <div className="flex w-36 flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium">{label}</label>
      <NativeSelect id={id} value={value} onChange={(event) => onChange(Number(event.target.value))}>
        {numbers.map((number) => <NativeSelectOption key={number} value={number}>Version {number}</NativeSelectOption>)}
      </NativeSelect>
    </div>
  )
}

export function RosterVersions({ unitId, month }: { unitId: string; month: string }) {
  const list = useListRosterVersions(unitId, { month })
  const [chosen, setChosen] = useState<{ from: number; to: number } | null>(null)
  const rows = list.data
  if (list.isPending) return <p className="text-sm">Loading the versions…</p>
  if (!rows) return <p role="alert" className="text-sm">The versions of this roster could not be read. Reload the page to try again.</p>
  if (rows.length === 0) return null
  const newest = rows[0].number
  // The latest change, until another pair is chosen.
  const pair = chosen ?? { from: Math.max(1, newest - 1), to: newest }
  const numbers = rows.map((row) => row.number)
  return (
    <section aria-labelledby="versions" className="flex flex-col gap-3">
      <h3 id="versions" className="font-medium">Versions</h3>
      <p className="text-sm text-muted-foreground">Every change to the approved roster is a new version, in force at once. The reasons are for the unit's manager, the CNO and the Deputy CNO: nurses read only the roster in force.</p>
      <p className="text-sm text-muted-foreground">Any version prints, or downloads as CSV or Excel, with each nurse's hours and shift counts. A file leaves the system, so it shows a day of leave as a day off, and the audit log records who took which.</p>
      <div className="overflow-x-auto">
        <Table>
          <caption className="sr-only">Versions of this roster, newest first</caption>
          <TableHeader>
            <TableRow>
              <TableHead scope="col">Version</TableHead>
              <TableHead scope="col">What</TableHead>
              <TableHead scope="col">By</TableHead>
              <TableHead scope="col">When</TableHead>
              <TableHead scope="col">Cells changed</TableHead>
              <TableHead scope="col">Why</TableHead>
              <TableHead scope="col">Print or download</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row) => (
              <TableRow key={row.number}>
                <TableCell className="tabular-nums">{row.number}{row.number === newest ? " (in force)" : ""}</TableCell>
                <TableCell>{ORIGIN[row.origin]}</TableCell>
                <TableCell>{row.created_by}</TableCell>
                <TableCell className="tabular-nums">{hospitalTime(row.created_at)}</TableCell>
                <TableCell className="tabular-nums">{row.cells_changed ?? "—"}</TableCell>
                <TableCell className="min-w-48 whitespace-pre-wrap wrap-anywhere">{row.reason || "—"}</TableCell>
                <TableCell><VersionFiles unitId={unitId} month={month} number={row.number} /></TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      {rows.length > 1 && (
        <div className="flex flex-col gap-3">
          <h4 className="text-sm font-medium">Compare two versions</h4>
          <div className="flex flex-wrap gap-3">
            <VersionPicker id="compare-from" label="From" value={pair.from} numbers={numbers} onChange={(from) => setChosen({ ...pair, from })} />
            <VersionPicker id="compare-to" label="To" value={pair.to} numbers={numbers} onChange={(to) => setChosen({ ...pair, to })} />
          </div>
          <Comparison unitId={unitId} month={month} from={pair.from} to={pair.to} />
        </div>
      )}
    </section>
  )
}
