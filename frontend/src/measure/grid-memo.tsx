/* MEASUREMENT ONLY (task: measure the memoised roster grid on real phones). The roster grid's editable shape — one ARIA grid,
   a roving tab stop, arrow keys, a button per cell coloured by its shift — with the React foundation fixes:
   - each row is a memo component compared by its row data, its active column (or -1) and a signature of its marks, so an arrow
     key or an edit renders one or two rows rather than all of them;
   - handlers reach the rows through a ref, so they are stable;
   - the column date labels are formatted once per grid, not once per cell per render.
   It draws every row (no windowing), as the workspace decision keeps for up to 60 nurses. */
import { type KeyboardEvent, memo, useEffect, useMemo, useRef, useState } from "react"

import type { GridRow, GridShift, GridView } from "@/api/generated/model"
import type { CellMarks } from "@/components/roster-grid"
import { asDate, cn, readableOn } from "@/lib/utils"

const weekday = new Intl.DateTimeFormat("en-GB", { weekday: "short", timeZone: "UTC" })
const longDay = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "long", timeZone: "UTC" })

type Handlers = { open: (hid: string, date: string, el: HTMLButtonElement) => void; key: (event: KeyboardEvent<HTMLButtonElement>, row: number, col: number) => void }

const Row = memo(function Row({ row, index, activeCol, marks, signature: _signature, labels, days, shifts, handlers }: {
  row: GridRow; index: number; activeCol: number; marks: CellMarks[]; signature: string; labels: string[]; days: GridView["days"]
  shifts: Map<string, GridShift>; handlers: { current: Handlers }
}) {
  const name = row.full_name || row.hospital_id
  return (
    <tr aria-rowindex={index + 2} style={{ height: 33 }}>
      <th scope="row" className="sticky start-0 z-10 max-w-28 border-b bg-background px-2 py-1 text-start font-normal wrap-anywhere sm:max-w-48">{name}</th>
      {row.cells.map((cell, col) => {
        const mark = marks[col]
        const code = mark.pending && mark.pending.shows !== null ? mark.pending.shows : cell.shift
        const shift = code ? shifts.get(code) : undefined
        const label = `${name}, ${labels[col]}: ${shift?.display_name ?? (code || (cell.kind === "leave" ? "Leave" : "Off"))}${mark.pinned ? `, ${mark.pinned}` : ""}`
        return (
          <td key={days[col].date} role="gridcell" className="border-b border-s p-0 text-center text-xs font-medium"
            style={shift ? { backgroundColor: shift.colour, color: readableOn(shift.colour) } : undefined}>
            <button type="button" id={`m-cell-${index}-${col}`} tabIndex={activeCol === col ? 0 : -1} aria-label={label} title={label}
              onClick={(event) => handlers.current.open(row.hospital_id, days[col].date, event.currentTarget)}
              onKeyDown={(event) => handlers.current.key(event, index, col)}
              className={cn("relative flex size-full min-h-8 items-center justify-center outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset",
                mark.pending && "outline-2 -outline-offset-4 outline-dashed outline-current", mark.refused && "ring-2 ring-inset ring-destructive")}>
              <span aria-hidden="true">{code || (cell.kind === "leave" ? "L" : "")}</span>
              {mark.pinned && <span aria-hidden="true" className="absolute end-0.5 top-0.5 size-1.5 rounded-full bg-current" />}
            </button>
          </td>
        )
      })}
    </tr>
  )
}, (a, b) => a.row === b.row && a.activeCol === b.activeCol && a.signature === b.signature && a.shifts === b.shifts && a.labels === b.labels)

export function RosterGridMemo({ grid, label, marks, onOpen }: { grid: GridView; label: string; marks: (hid: string, date: string) => CellMarks; onOpen: (hid: string, date: string, el: HTMLButtonElement) => void }) {
  const shifts = useMemo(() => new Map(grid.shifts.map((shift) => [shift.code, shift])), [grid.shifts])
  const labels = useMemo(() => grid.days.map((day) => longDay.format(asDate(day.date))), [grid.days])
  const heads = useMemo(() => grid.days.map((day) => ({ d: asDate(day.date).getUTCDate(), w: weekday.format(asDate(day.date)) })), [grid.days])
  const [active, setActive] = useState({ row: 0, col: 0 })
  const movedTo = useRef<{ row: number; col: number } | null>(null)
  const handlers = useRef<Handlers>({ open: onOpen, key: () => {} })
  handlers.current = {
    open: onOpen,
    key: (event, row, col) => {
      const last = { row: grid.rows.length - 1, col: grid.days.length - 1 }
      const to = { ArrowUp: { row: row - 1, col }, ArrowDown: { row: row + 1, col }, ArrowLeft: { row, col: col - 1 }, ArrowRight: { row, col: col + 1 },
        Home: { row, col: 0 }, End: { row, col: last.col }, PageUp: { row: 0, col }, PageDown: { row: last.row, col } }[event.key]
      if (!to) return
      event.preventDefault()
      const next = { row: Math.min(Math.max(to.row, 0), last.row), col: Math.min(Math.max(to.col, 0), last.col) }
      movedTo.current = next
      setActive(next)
    },
  }
  useEffect(() => {
    const to = movedTo.current
    if (!to) return
    movedTo.current = null
    document.getElementById(`m-cell-${to.row}-${to.col}`)?.focus()
  })
  return (
    <div className="relative overflow-auto border">
      <table role="grid" aria-label={label} aria-rowcount={grid.rows.length + 1} className="w-max border-separate border-spacing-0 text-sm">
        <thead className="sticky top-0 z-20 bg-background">
          <tr aria-rowindex={1}>
            <th scope="col" className="sticky start-0 z-30 min-w-24 border-b bg-background px-2 py-1 text-start font-medium sm:min-w-40">Nurse</th>
            {heads.map((head, i) => (
              <th key={grid.days[i].date} scope="col" className={cn("w-9 min-w-9 border-b border-s px-0 py-1 text-center text-xs font-medium tabular-nums", grid.days[i].weekend && "bg-muted")}>
                <span className="block">{head.d}</span><span className="block font-normal">{head.w}</span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {grid.rows.map((row, index) => {
            const rowMarks = grid.days.map((day) => marks(row.hospital_id, day.date))
            const signature = rowMarks.map((m) => `${m.pinned ?? ""}|${m.pending?.shows ?? "-"}|${m.refused ? 1 : 0}`).join(",")
            return <Row key={row.hospital_id} row={row} index={index} activeCol={active.row === index ? active.col : -1} marks={rowMarks} signature={signature}
              labels={labels} days={grid.days} shifts={shifts} handlers={handlers} />
          })}
        </tbody>
      </table>
    </div>
  )
}
