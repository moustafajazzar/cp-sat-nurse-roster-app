import type { ReactNode } from "react"

import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { solveDuration, solveStatus, type SolveRow } from "@/lib/solve-words"
import { hospitalTime, monthName, scrollableRegion } from "@/lib/utils"

/** The columns the two histories share, and each one's own. */
export function SolveHistoryTable<Row extends SolveRow>({ label, solves, leading, trailing, status = solveStatus }: {
  label: string
  solves: Row[]
  leading?: { header: string; cell: (row: Row) => ReactNode }
  trailing: { header: ReactNode; cell: (row: Row) => ReactNode; end?: boolean }[]
  status?: (row: Row) => string
}) {
  return (
    <Table container={scrollableRegion(label)}>
      <TableHeader>
        <TableRow>
          {leading && <TableHead>{leading.header}</TableHead>}
          <TableHead>Month</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Asked for by</TableHead>
          <TableHead>Asked for at</TableHead>
          <TableHead>Duration</TableHead>
          {trailing.map((column, index) => <TableHead key={index} className={column.end ? "text-end" : undefined}>{column.header}</TableHead>)}
        </TableRow>
      </TableHeader>
      <TableBody>
        {solves.map((row) => (
          <TableRow key={row.id}>
            {leading && <TableHead scope="row" className="font-normal">{leading.cell(row)}</TableHead>}
            <TableCell>{monthName(row.month)}</TableCell>
            <TableCell>{status(row)}</TableCell>
            <TableCell>{row.requested_by}</TableCell>
            <TableCell className="tabular-nums">{hospitalTime(row.requested_at)}</TableCell>
            <TableCell className="tabular-nums">{solveDuration(row)}</TableCell>
            {trailing.map((column, index) => <TableCell key={index} className={column.end ? "text-end tabular-nums" : undefined}>{column.cell(row)}</TableCell>)}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
