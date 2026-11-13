import { useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import { getGetMonthCloseQueryKey, useCloseMonth, useGetMonthClose } from "@/api/generated/endpoints"
import type { CarryValues, ClosedMonth, MonthCloseView } from "@/api/generated/model"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { plainRefusal } from "@/lib/server-errors"
import { ownWords } from "@/lib/solve-words"
import { hospitalTime, longDay, monthAfter, monthName, scrollableRegion } from "@/lib/utils"

/** The manager's view of a month's close and of each nurse's carry-over: what the month starts from, what it hands
 *  on, and, once the month is over, what its recorded facts broke and a way to close it again after recording something late. */

const hours = (value: number) => (value > 0 ? `${value} owed` : value < 0 ? `${-value} ahead` : "0")
const balance = (value: number) => (value > 0 ? `+${value.toFixed(2)}` : value < 0 ? `−${(-value).toFixed(2)}` : "0")

function source(values: CarryValues): string {
  switch (values.source) {
    case "opening": return "Opening values"
    case "projected": return `Projected from ${monthName(values.out_of!)}`
    case "closed": return `Closed ${monthName(values.out_of!)}`
  }
}

function Values({ values }: { values: CarryValues | null }) {
  if (!values) return <TableCell colSpan={5} className="text-muted-foreground">No approved roster</TableCell>
  return (
    <>
      <TableCell className="tabular-nums">{hours(values.hours_balance)}</TableCell>
      <TableCell className="tabular-nums">{values.ytd_overtime}</TableCell>
      <TableCell className="tabular-nums">{balance(values.night_balance)}</TableCell>
      <TableCell className="tabular-nums">{balance(values.friday_balance)}</TableCell>
      <TableCell>{source(values)}</TableCell>
    </>
  )
}

function CarryOverTable({ view }: { view: MonthCloseView }) {
  const label = `Carry-over, ${monthName(view.month)}`
  if (!view.carry_over.length) return <p className="text-sm">No nurse is on this unit's roster for {monthName(view.month)}.</p>
  const columns = ["Hours", "Overtime this year", "Nights", "Weekends", "Where from"]
  return (
    <Table container={scrollableRegion(label)}>
      <TableHeader>
        <TableRow>
          <TableHead rowSpan={2}>Nurse</TableHead>
          <TableHead colSpan={5} scope="colgroup">{monthName(view.month)} starts from</TableHead>
          <TableHead colSpan={5} scope="colgroup">It hands to {monthName(monthAfter(view.month, 1))}</TableHead>
        </TableRow>
        <TableRow>
          {[...columns, ...columns].map((column, index) => <TableHead key={index} scope="col">{column}</TableHead>)}
        </TableRow>
      </TableHeader>
      <TableBody>
        {view.carry_over.map((row) => (
          <TableRow key={row.hospital_id}>
            <TableHead scope="row" className="font-normal">{row.full_name || row.hospital_id}</TableHead>
            <Values values={row.into} />
            <Values values={row.out} />
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function Closed({ closed }: { closed: ClosedMonth }) {
  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm">
        Closed {hospitalTime(closed.closed_at)} {closed.closed_by ? `by ${closed.closed_by}` : "by the schedule"}, with version {closed.version} as the roster in force.
      </p>
      {closed.facts_broke.length === 0
        ? <p className="text-sm">Its recorded facts broke no hard rule.</p>
        : (
          <>
            <p className="text-sm">Its recorded facts broke {closed.facts_broke.length === 1 ? "a hard rule" : `${closed.facts_broke.length} hard rules`}. A close reports these and never refuses them: they happened.</p>
            <ul className="list-disc pl-5 text-sm">
              {closed.facts_broke.map((fact, index) => (
                <li key={index}>{[fact.full_name || fact.hospital_id, fact.date && longDay(fact.date)].filter(Boolean).join(", ") || "The unit"}: {ownWords(fact)}</li>
              ))}
            </ul>
          </>
        )}
      {closed.changed_since && <p className="text-sm font-medium">Leave or a nurse's dates have changed since it closed. Close it again to take them in.</p>}
    </div>
  )
}

export function MonthCloseCard({ unitId, month }: { unitId: string; month: string }) {
  const queryClient = useQueryClient()
  const view = useGetMonthClose(unitId, { month })
  const close = useCloseMonth()
  const [said, setSaid] = useState("")
  const answer = view.data
  const refused = close.isError ? plainRefusal(close.error) : undefined
  const closeIt = () => {
    setSaid("")
    close.mutate({ unitId, data: { month } }, {
      onSuccess: (closed) => {
        queryClient.setQueryData(getGetMonthCloseQueryKey(unitId, { month }), closed)
        setSaid(`${monthName(month)} is closed. Each nurse's carry-over below is this close's.`)
      },
    })
  }
  const again = !!answer?.closed
  return (
    <Card role="region" aria-labelledby="month-close">
      <CardHeader>
        <CardTitle><h3 id="month-close" tabIndex={-1} className="outline-none">Carry-over and month close</h3></CardTitle>
        <CardDescription>
          What each nurse's month starts from and hands on: hours owed or worked ahead, overtime this year, and nights and weekends above or below a fair share. A month that has not closed hands on a projection, worked out from its roster in force. Once the month is over it closes on its own, and its roster in force, leave and pins become fact.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {/* Polite, but not a status of its own: the page's status line says what the solves did. */}
        <p aria-live="polite" className="text-sm text-muted-foreground empty:sr-only">{said}</p>
        {view.isPending && <p>Loading…</p>}
        {view.isError && <p role="alert">The carry-over could not be read. Reload the page to try again.</p>}
        {answer && (
          <>
            {answer.closed && <Closed closed={answer.closed} />}
            {again && answer.close_refusal && <p className="text-sm">{answer.close_refusal}</p>}
            {!answer.close_refusal && (
              <div className="flex flex-col items-start gap-2">
                {!again && <p className="text-sm">{monthName(month)} is over and has not closed yet. It closes on its own at the close time; close it now to see what its facts broke.</p>}
                {refused && <p role="alert" className="text-sm text-destructive">{refused}</p>}
                {close.isError && !refused && <p role="alert" className="text-sm text-destructive">The month could not be closed. Check your connection and try again.</p>}
                <Button type="button" variant={again && !answer.closed?.changed_since ? "outline" : "default"} disabled={close.isPending} onClick={closeIt}>
                  {again ? `Close ${monthName(month)} again` : `Close ${monthName(month)} now`}
                </Button>
              </div>
            )}
            <CarryOverTable view={answer} />
          </>
        )}
      </CardContent>
    </Card>
  )
}
