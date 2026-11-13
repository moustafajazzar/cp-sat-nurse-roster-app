import { type ReactNode, useId } from "react"

import type { RosterSheet } from "@/api/generated/model"
import { Button } from "@/components/ui/button"
import { ON_PROBATION, ON_PROBATION_MEANS, preceptorLine } from "@/lib/probation-words"
import { asDate, cn, hospitalTime, monthName, scrollableRegion } from "@/lib/utils"

/** A roster version laid out for paper: one row a nurse, a column a date, then the hours and the count of each shift, the table
 *  its CSV and Excel files hold. It is drawn whole, never windowed, so that every row prints.
 *
 *  On A4 landscape (`@page roster` in index.css) a 31-day month fits across one page: the columns are fixed and share the width,
 *  so nothing is cut off at the right. Rows that do not fit go on to the next page, never split, and the header row repeats at
 *  the top of each, as a table's head does in print. Each row carries its own name, so no page needs another to be read. Nurses on
 *  probation come last, as the server orders them, in a row group of their own under the "On probation" heading, each with her
 *  preceptor's name under her own. */

// Two letters, so Saturday and Sunday, and Tuesday and Thursday, are told apart in a column a few millimetres wide.
const weekday = new Intl.DateTimeFormat("en-GB", { weekday: "short", timeZone: "UTC" })
const twoLetters = (iso: string) => weekday.format(asDate(iso)).slice(0, 2)
const dayNumber = (iso: string) => String(asDate(iso).getUTCDate())

/** A weekend or a holiday is shaded, on paper too. */
const shaded = (day: { weekend: boolean; holiday: boolean }) => (day.weekend || day.holiday) && "bg-muted print:[print-color-adjust:exact]"

/** What a cell of the reader's own row says when it holds no shift, on their own sheet only. */
const OWN_DAY = { leave: "L", outside: "·", off: "", shift: "" } as const

export function SheetTable({ sheet, me }: { sheet: RosterSheet; me?: string }) {
  const label = `${sheet.unit} roster, ${monthName(sheet.month)}, version ${sheet.version}`
  const region = scrollableRegion(label)
  const headingId = useId()
  const counted = sheet.rows.filter((row) => !row.on_probation)
  const onProbation = sheet.rows.filter((row) => row.on_probation)
  const width = 2 + sheet.days.length + sheet.shifts.length
  const drawRow = (row: RosterSheet["rows"][number]) => {
    const own = row.hospital_id === me && sheet.mine ? sheet.mine : null
    return (
      <tr key={row.hospital_id} className="break-inside-avoid">
        <th scope="row" aria-label={row.on_probation ? `${row.full_name || row.hospital_id}${own ? " (you)" : ""}, ${preceptorLine(row.preceptor_name)}` : undefined}
          className="border p-1 text-start font-normal wrap-anywhere">
          {row.full_name || row.hospital_id}{own ? " (you)" : ""}
          {row.on_probation && <span className="block text-muted-foreground print:text-current">{preceptorLine(row.preceptor_name)}</span>}
        </th>
        {row.shifts.map((code, index) => (
          <td key={sheet.days[index].date} className={cn("border p-0 text-center", shaded(sheet.days[index]))}>
            {code || (own ? OWN_DAY[own[index].kind] : "")}
          </td>
        ))}
        <td className="border p-0.5 text-center">{row.hours}</td>
        {row.counts.map((count, index) => <td key={sheet.shifts[index].code} className="border p-0.5 text-center">{count}</td>)}
      </tr>
    )
  }
  return (
    <div {...region} className={cn(region.className, "overflow-x-auto print:overflow-visible")}>
      <table className="roster-sheet w-full min-w-[60rem] table-fixed border-collapse text-xs tabular-nums print:min-w-0 print:text-[7pt]">
        <caption className="sr-only">{label}</caption>
        <colgroup>
          <col className="w-44 print:w-[34mm]" />
          {sheet.days.map((day) => <col key={day.date} />)}
          <col className="w-12 print:w-[9mm]" />
          {sheet.shifts.map((shift) => <col key={shift.code} className="w-10 print:w-[7mm]" />)}
        </colgroup>
        <thead>
          <tr>
            <th scope="col" className="border p-1 text-start font-medium">Nurse</th>
            {sheet.days.map((day) => (
              <th key={day.date} scope="col" className={cn("border p-0.5 text-center font-medium", shaded(day))}>
                <abbr title={day.date} className="no-underline">{dayNumber(day.date)}<br />{twoLetters(day.date)}</abbr>
              </th>
            ))}
            <th scope="col" className="border p-0.5 text-center font-medium">Hours</th>
            {sheet.shifts.map((shift) => (
              <th key={shift.code} scope="col" className="border p-0.5 text-center font-medium"><abbr title={`${shift.display_name} shifts`} className="no-underline">{shift.code}</abbr></th>
            ))}
          </tr>
        </thead>
        <tbody>{counted.map(drawRow)}</tbody>
        {onProbation.length > 0 && (
          <tbody aria-labelledby={headingId}>
            <tr className="break-inside-avoid break-after-avoid">
              <th id={headingId} scope="rowgroup" colSpan={width} className="border bg-muted p-1 text-start font-medium print:[print-color-adjust:exact]">{ON_PROBATION}</th>
            </tr>
            {onProbation.map(drawRow)}
          </tbody>
        )}
      </table>
    </div>
  )
}

/** The page around the table: what it is, the print button, the legend. */
export function SheetPage({ sheet, me, back }: { sheet: RosterSheet; me?: string; back: ReactNode }) {
  const replaced = sheet.version !== sheet.in_force
  const ownLeave = !!sheet.mine?.some((day) => day.kind === "leave")
  const ownOutside = !!sheet.mine?.some((day) => day.kind === "outside")
  return (
    <div className="flex flex-col gap-3 print:gap-1">
      <h1 className="text-xl font-medium print:text-[12pt]">{sheet.unit} roster, {monthName(sheet.month)}</h1>
      <p className="text-sm print:text-[8pt]">
        Version {sheet.version}, made {hospitalTime(sheet.made_at)}.{" "}
        {replaced ? <strong className="font-medium">Version {sheet.in_force} has replaced it, so this is not the roster in force.</strong> : "It is the roster in force."}
      </p>
      <div className="flex flex-wrap items-center gap-3 print:hidden">
        <Button onClick={() => window.print()}>Print</Button>
        {back}
      </div>
      <p className="text-sm text-muted-foreground print:hidden">Print it on A4 in landscape. To keep it as a PDF, choose Save as PDF in the print dialog.</p>
      <SheetTable sheet={sheet} me={me} />
      <p className="text-sm print:text-[7pt]">
        {sheet.shifts.map((shift) => `${shift.code}: ${shift.display_name}, ${shift.hours} hours`).join(". ")}{sheet.shifts.length ? ". " : ""}
        An empty cell is a day not worked. Hours are those of the shifts on the row.
        {sheet.rows.some((row) => row.on_probation) ? ` ${ON_PROBATION_MEANS}.` : ""}
        {ownLeave ? " L: your leave. My month counts your leave as worked in your hours; these hours count shifts only." : ""}{ownOutside ? " ·: before your start date or after your end date." : ""}
      </p>
    </div>
  )
}
