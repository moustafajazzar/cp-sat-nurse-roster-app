import { tableFeatures, useTable, type ColumnDef } from "@tanstack/react-table"
import { useVirtualizer } from "@tanstack/react-virtual"
import { type KeyboardEvent, type ReactNode, useEffect, useId, useMemo, useRef, useState } from "react"

import type { GridCell, GridRow, GridShift, GridView } from "@/api/generated/model"
import { ON_PROBATION, ON_PROBATION_MEANS, preceptorLine } from "@/lib/probation-words"
import { asDate, cn, readableOn, scrollableRegion } from "@/lib/utils"

const features = tableFeatures({})
const weekday = new Intl.DateTimeFormat("en-GB", { weekday: "short", timeZone: "UTC" })
const longDay = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "long", timeZone: "UTC" })

const NURSE_HEADER = "Nurse"
const ROW_HEIGHT = 33
/** A unit's usual month fits in the page, where the browser's find and a screen reader see all of it. Past this, only the rows
 *  in view are drawn, and `aria-rowcount` still says how many there are. */
const WINDOWED_ABOVE = 60

/** How a cell that is no shift looks and reads. */
const NOT_WORKING: Record<Exclude<GridCell["kind"], "shift">, { says: string; mark: string; className?: string }> = {
  leave: { says: "Leave", mark: "L", className: "bg-muted" },
  outside: { says: "Outside their dates", mark: "·", className: "text-muted-foreground" },
  off: { says: "Off", mark: "" },
}

/** What an editable grid says of a cell beyond what it holds. */
export type CellMarks = {
  /** What the cell will be once saved, when an edit is waiting: a shift's code, "" for off, or null for a pin being cleared. */
  pending?: { shows: string | null; says: string }
  pinned?: string
  refused?: boolean
}

export type GridEditing = {
  marks: (hospitalId: string, date: string) => CellMarks
  /** A cell's button was pressed: open its editor against it. */
  onOpen: (hospitalId: string, date: string, button: HTMLButtonElement) => void
}

function EditableCell({ cell, shifts, title, marks, focusable, id, onOpen, onKeyDown, hid, date }: {
  hid?: string
  date?: string
  cell: GridCell
  shifts: Map<string, GridShift>
  title: string
  marks: CellMarks
  focusable: boolean
  id: string
  onOpen: (button: HTMLButtonElement) => void
  onKeyDown: (event: KeyboardEvent<HTMLButtonElement>) => void
}) {
  const { pending, pinned, refused } = marks
  const code = pending && pending.shows !== null ? pending.shows : cell.shift
  const shift = code ? shifts.get(code) : undefined
  const says = code ? (shift?.display_name ?? code) : cell.kind === "shift" ? "Off" : NOT_WORKING[cell.kind].says
  const mark = code || (cell.kind === "shift" ? "" : NOT_WORKING[cell.kind].mark)
  const label = [`${title}: ${says}`, pinned && !pending ? pinned : null, pending?.says, refused ? "refused, see below the grid" : null]
    .filter(Boolean).join(", ")
  return (
    <td role="gridcell" className={cn("border-b border-s p-0 text-center text-xs font-medium", !code && cell.kind !== "shift" && NOT_WORKING[cell.kind].className)}
      style={shift ? { backgroundColor: shift.colour, color: readableOn(shift.colour) } : undefined}>
      <button type="button" id={id} data-cell={id} data-hid={hid} data-date={date} tabIndex={focusable ? 0 : -1} aria-haspopup="menu" aria-label={label} title={label}
        onClick={(event) => onOpen(event.currentTarget)} onKeyDown={onKeyDown}
        className={cn("relative flex size-full min-h-8 items-center justify-center outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset",
          pending && "outline-2 -outline-offset-4 outline-dashed outline-current",
          refused && "ring-2 ring-inset ring-destructive")}>
        <span aria-hidden="true">{mark}</span>
        {/* A corner, in the cell's own colour, so it reads on any shift's fill: the label says it for a screen reader. */}
        {(pinned || (pending && pending.shows !== null)) && <span aria-hidden="true" className="absolute end-0.5 top-0.5 size-1.5 rounded-full bg-current" />}
      </button>
    </td>
  )
}

function Cell({ cell, shifts, title }: { cell: GridCell; shifts: Map<string, GridShift>; title: string }) {
  if (cell.kind === "shift") {
    const shift = shifts.get(cell.shift)
    return (
      <td title={`${title}: ${shift?.display_name ?? cell.shift}`} className="border-b border-s p-0 text-center text-xs font-medium"
        style={shift ? { backgroundColor: shift.colour, color: readableOn(shift.colour) } : undefined}>
        {cell.shift}
      </td>
    )
  }
  const { says, mark, className } = NOT_WORKING[cell.kind]
  return (
    <td title={`${title}: ${says}`} className={cn("border-b border-s p-0 text-center text-xs font-medium", className)}>
      <span aria-hidden="true">{mark}</span><span className="sr-only">{says}</span>
    </td>
  )
}

/** A grid's nurses across its dates. Nurse names stay in view while it scrolls, and on a phone it scrolls sideways. With `editing`
 *  it is one stop for Tab, whose arrows move between cells and whose Enter or Space opens the cell's editor. Nurses on probation
 *  come last (the server orders them so) in a row group of their own under the "On probation" heading, each with her preceptor's
 *  name; the arrows pass over the heading. */
export function RosterGrid({ grid, label, editing, extraShifts = [], markedDate, compact = false, headRows, footRows, fill = false }: { grid: GridView; label: string; editing?: GridEditing; extraShifts?: GridShift[]; markedDate?: string
  /** PROTOTYPE (roster workspace): narrower day columns, extra rows pinned above or below, and a grid that fills its parent. */
  compact?: boolean; headRows?: ReactNode; footRows?: ReactNode; fill?: boolean }) {
  const scroller = useRef<HTMLDivElement>(null)
  /** The date the Day view opened it on: its column scrolled into view once, and its header marked. */
  const marked = useRef<HTMLTableCellElement>(null)
  useEffect(() => {
    marked.current?.scrollIntoView?.({ block: "nearest", inline: "center" })
  }, [markedDate])
  const shifts = useMemo(() => new Map([...extraShifts, ...grid.shifts].map((shift) => [shift.code, shift])), [grid.shifts, extraShifts])
  const [active, setActive] = useState({ row: 0, col: 0 })
  /** The cell an arrow key moved to, until it has focus. */
  const movedTo = useRef<{ row: number; col: number } | null>(null)
  const columns = useMemo<ColumnDef<typeof features, GridRow>[]>(() => [
    { id: "nurse", header: NURSE_HEADER, accessorFn: (row) => row.full_name || row.hospital_id },
    ...grid.days.map((day, index) => ({
      id: day.date,
      header: day.date,
      accessorFn: (row: GridRow) => row.cells[index],
    })),
  ], [grid.days])
  const table = useTable({ features, columns, data: grid.rows })
  const rows = table.getRowModel().rows
  const windowed = rows.length > WINDOWED_ABOVE
  const virtualizer = useVirtualizer({
    enabled: windowed,
    count: rows.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 8,
  })
  const items = windowed ? virtualizer.getVirtualItems() : rows.map((_, index) => ({ index, start: index * ROW_HEIGHT, end: (index + 1) * ROW_HEIGHT }))
  /** The first row on probation, where the heading goes; -1 with nobody on probation. */
  const firstOnProbation = grid.rows.findIndex((row) => row.on_probation)
  const anyOnProbation = firstOnProbation >= 0
  const counted = items.filter(({ index }) => !anyOnProbation || index < firstOnProbation)
  const onProbation = items.filter(({ index }) => anyOnProbation && index >= firstOnProbation)
  const headingDrawn = onProbation[0]?.index === firstOnProbation
  // The heading is no row of the virtualizer's, so in a windowed grid its height joins the spacer on whichever side it is off.
  const headingAbove = anyOnProbation && (items[0]?.index ?? 0) > firstOnProbation
  const headingBelow = anyOnProbation && (items.at(-1)?.index ?? 0) < firstOnProbation
  const before = windowed ? (items[0]?.start ?? 0) + (headingAbove ? ROW_HEIGHT : 0) : 0
  const after = windowed ? virtualizer.getTotalSize() - (items.at(-1)?.end ?? 0) + (headingBelow ? ROW_HEIGHT : 0) : 0
  const spacer = (height: number) => height > 0 && <tr aria-hidden="true"><td style={{ height }} colSpan={grid.days.length + 1} /></tr>
  const headingId = useId()

  const region = scrollableRegion(label)
  const cellId = (row: number, col: number) => `cell-${row}-${col}`

  // Only after an arrow key: a page that re-renders must not take focus from wherever the manager is. In a windowed grid the
  // cell moved to may not be drawn yet, until the scroll to it renders its row, so the move waits for that cell.
  // It is the cell itself that is waited for, not whichever is active: the virtualizer's scroll can render the grid once more
  // before the new active cell is set, and that render must not spend the move on the old one.
  useEffect(() => {
    const to = movedTo.current
    if (!to) return
    const cell = document.getElementById(cellId(to.row, to.col))
    if (!cell) return
    movedTo.current = null
    cell.focus()
  })

  const move = (event: KeyboardEvent<HTMLButtonElement>, row: number, col: number) => {
    const last = { row: rows.length - 1, col: grid.days.length - 1 }
    const to = { ArrowUp: { row: row - 1, col }, ArrowDown: { row: row + 1, col }, ArrowLeft: { row, col: col - 1 }, ArrowRight: { row, col: col + 1 },
      Home: { row, col: 0 }, End: { row, col: last.col }, PageUp: { row: 0, col }, PageDown: { row: last.row, col } }[event.key]
    if (!to) return
    event.preventDefault()
    // The grid's own direction: a page in a right-to-left language mirrors it, and the arrows with it.
    const mirrored = getComputedStyle(event.currentTarget).direction === "rtl" && event.key.startsWith("Arrow") && to.row === row
    const next = mirrored ? { row, col: col - (to.col - col) } : to
    const clamped = { row: Math.min(Math.max(next.row, 0), last.row), col: Math.min(Math.max(next.col, 0), last.col) }
    movedTo.current = clamped
    if (windowed) virtualizer.scrollToIndex(clamped.row)
    setActive(clamped)
  }

  const drawRow = (index: number): ReactNode => {
    const row = rows[index]
    const [nurse, ...cells] = row.getAllCells()
    const name = nurse.getValue() as string
    return (
      <tr key={row.id} ref={windowed ? virtualizer.measureElement : undefined} data-index={index} aria-rowindex={index + 2 + (anyOnProbation && index >= firstOnProbation ? 1 : 0)} style={{ height: ROW_HEIGHT }}>
        {/* Wraps rather than truncates: a name has to be reachable whole, and a title is not by keyboard. Narrower on a phone,
            so a week of days shows beside it at 360 px. */}
        {/* Her name and her preceptor's read as one, with a comma: a browser parts the two lines with a space only. */}
        <th scope="row" aria-label={row.original.on_probation ? `${name}, ${preceptorLine(row.original.preceptor_name)}` : undefined}
          className="sticky start-0 z-10 max-w-28 border-b sm:max-w-48 bg-background px-2 py-1 text-start font-normal wrap-anywhere">
          {name}
          {row.original.on_probation && <span className="block text-xs text-muted-foreground">{preceptorLine(row.original.preceptor_name)}</span>}
        </th>
        {cells.map((cell, col) => {
          const title = `${name}, ${longDay.format(asDate(cell.column.id))}`
          if (!editing) return <Cell key={cell.id} cell={cell.getValue() as GridCell} shifts={shifts} title={title} />
          const hospitalId = row.original.hospital_id
          const date = cell.column.id
          return (
            <EditableCell key={cell.id} id={cellId(index, col)} hid={hospitalId} date={date} cell={cell.getValue() as GridCell} shifts={shifts} title={title}
              marks={editing.marks(hospitalId, date)} focusable={active.row === index && active.col === col}
              onOpen={(button) => { setActive({ row: index, col }); editing.onOpen(hospitalId, date, button) }}
              onKeyDown={(event) => move(event, index, col)} />
          )
        })}
      </tr>
    )
  }

  // `relative` keeps each cell's screen-reader text inside the scroller, or the whole page scrolls sideways.
  return (
    <div ref={scroller} {...region}
      className={cn(region.className, "relative overflow-auto border", windowed && !fill && "max-h-[70svh]", fill && "h-full")}>
      <table role={editing ? "grid" : undefined} aria-label={editing ? label : undefined} aria-rowcount={rows.length + 1 + (anyOnProbation ? 1 : 0)}
        className="w-max border-separate border-spacing-0 text-sm">
        <thead className="sticky top-0 z-20 bg-background">
          <tr aria-rowindex={1}>
            {table.getHeaderGroups()[0].headers.map((header, index) => {
              if (index === 0) return <th key={header.id} scope="col" className="sticky start-0 z-30 min-w-24 border-b sm:min-w-40 bg-background px-2 py-1 text-start font-medium">{NURSE_HEADER}</th>
              const day = grid.days[index - 1]
              const date = asDate(day.date)
              const isMarked = day.date === markedDate
              return (
                <th key={header.id} scope="col" title={longDay.format(date) + (day.holiday ? ", public holiday" : "")}
                  ref={isMarked ? marked : undefined} aria-current={isMarked ? "date" : undefined}
                  className={cn(compact ? "w-7 min-w-7" : "w-9 min-w-9", "border-b border-s px-0 py-1 text-center text-xs font-medium tabular-nums", (day.weekend || day.holiday) && "bg-muted",
                    isMarked && "bg-primary text-primary-foreground")}>
                  <span className="block">{date.getUTCDate()}</span>
                  {/* Plain foreground: the muted one falls short of 4.5:1 on the shaded days. */}
                  <span className="block font-normal">{day.holiday ? "Hol" : weekday.format(date)}</span>
                  {isMarked && <span className="sr-only">, the Day view's date</span>}
                </th>
              )
            })}
          </tr>
          {headRows}
        </thead>
        <tbody>
          {spacer(before)}
          {counted.map(({ index }) => drawRow(index))}
          {onProbation.length === 0 && spacer(after)}
        </tbody>
        {onProbation.length > 0 && (
          // A long grid scrolled past the heading still names the group, with the heading's own words.
          <tbody aria-labelledby={headingDrawn ? headingId : undefined} aria-label={headingDrawn ? undefined : ON_PROBATION}>
            {headingDrawn && (
              <tr aria-rowindex={firstOnProbation + 2} style={{ height: ROW_HEIGHT }}>
                <th id={headingId} scope="rowgroup" colSpan={grid.days.length + 1} className="border-b bg-muted px-0 py-1 text-start font-medium">
                  {/* Sticky, so the heading stays in view while the grid scrolls sideways on a phone. */}
                  <span className="sticky start-0 px-2">{ON_PROBATION}</span>
                </th>
              </tr>
            )}
            {onProbation.map(({ index }) => drawRow(index))}
            {spacer(after)}
          </tbody>
        )}
        {footRows && <tfoot className="sticky bottom-0 z-20 bg-background">{footRows}</tfoot>}
      </table>
    </div>
  )
}

export function GridLegend({ grid, editable = false }: { grid: GridView; editable?: boolean }) {
  return (
    <ul aria-label="What the cells mean" className="flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted-foreground">
      {grid.shifts.map((shift) => (
        <li key={shift.code} className="flex items-center gap-1.5">
          <span aria-hidden="true" className="inline-flex size-6 items-center justify-center rounded-sm text-xs font-medium"
            style={{ backgroundColor: shift.colour, color: readableOn(shift.colour) }}>{shift.code}</span>
          {shift.display_name}, {shift.hours} hours
        </li>
      ))}
      <li className="flex items-center gap-1.5"><span aria-hidden="true" className="inline-flex size-6 items-center justify-center rounded-sm bg-muted text-xs font-medium text-foreground">L</span>Leave</li>
      <li className="flex items-center gap-1.5"><span aria-hidden="true" className="inline-flex size-6 items-center justify-center rounded-sm border text-xs">·</span>Before their start date or after their end date</li>
      <li className="flex items-center gap-1.5"><span aria-hidden="true" className="inline-flex size-6 rounded-sm border" />Off</li>
      <li>Hol: a public holiday</li>
      {grid.rows.some((row) => row.on_probation) && <li>{ON_PROBATION_MEANS}</li>}
      {editable && <li className="flex items-center gap-1.5"><span aria-hidden="true" className="relative inline-flex size-6 rounded-sm border"><span className="absolute end-0.5 top-0.5 size-1.5 rounded-full bg-current" /></span>Pinned</li>}
      {editable && <li className="flex items-center gap-1.5"><span aria-hidden="true" className="inline-flex size-6 rounded-sm border outline-2 -outline-offset-4 outline-dashed" />Changed, not saved yet</li>}
    </ul>
  )
}
