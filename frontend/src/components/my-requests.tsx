import { zodResolver } from "@hookform/resolvers/zod"
import { keepPreviousData, useQueryClient } from "@tanstack/react-query"
import { useRef, useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { ApiError } from "@/api/fetcher"
import { getGetMyRequestsQueryKey, useGetMyRequests, useMakeRequest, useWithdrawRequest } from "@/api/generated/endpoints"
import type { MyRequestRow, MyRequests } from "@/api/generated/model"
import { FormField, SelectField } from "@/components/form-field"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field"
import { NativeSelectOption } from "@/components/ui/native-select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Textarea } from "@/components/ui/textarea"
import { plainRefusal, showServerErrors } from "@/lib/server-errors"
import { askedFor, inSentence, herReason, lastDecision, MY_STATE, NO_CONNECTION, shiftNames } from "@/lib/request-words"
import { monthName, scrollableRegion, shortDay, WRAP_ON_PHONE } from "@/lib/utils"

const STATUS = "my-requests-status"

function windowWords(answer: MyRequests): string {
  const month = monthName(answer.month)
  const window = answer.window
  if (!window) return `No request window has been opened for ${month}.`
  const closes = shortDay(window.closes_on)
  switch (window.state) {
    case "scheduled":
      return `Requests for ${month} open on ${shortDay(window.opens_on)} and are taken until the end of ${closes}.`
    case "open":
      return `Requests for ${month} are open until the end of ${closes}. You have ${answer.counted} of ${answer.request_cap} pending or accepted.`
    case "closed":
      return `The request window for ${month} has closed${window.closed_early ? " early" : ""}. Your manager decides any request still waiting.`
  }
}

const formSchema = z.object({
  date: z.iso.date("Choose a date."),
  shift: z.string(),
  reason: z.string().max(500, "At most 500 characters."),
})
type FormValues = z.infer<typeof formSchema>

function RequestForm({ answer, onMade }: { answer: MyRequests; onMade: (message: string) => void }) {
  const queryClient = useQueryClient()
  const make = useMakeRequest()
  const form = useForm<FormValues>({ resolver: zodResolver(formSchema), defaultValues: { date: "", shift: "", reason: "" } })
  const names = shiftNames(answer.shifts)
  const reasonError = form.formState.errors.reason
  const refusal = plainRefusal(make.error)

  const submit = form.handleSubmit(({ date, shift, reason }) => make.mutate({ data: { date, shift: shift || null, reason } }, {
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: getGetMyRequestsQueryKey() })
      form.reset()
      onMade(`Your request for ${inSentence(shift, names)} on ${shortDay(date)} is made. Your manager decides it.`)
    },
    onError: (error) => showServerErrors(form, error),
  }))

  return (
    <form onSubmit={submit} noValidate className="flex max-w-md flex-col gap-3">
      <h4 className="font-medium">Make a request</h4>
      <FormField label="Date" type="date" min={answer.first_day ?? undefined} max={answer.last_day ?? undefined}
        registration={form.register("date")} error={form.formState.errors.date} />
      <SelectField label="What you ask for" registration={form.register("shift")} error={form.formState.errors.shift}>
        <NativeSelectOption value="">A day off</NativeSelectOption>
        {answer.shifts.map((shift) => (
          <NativeSelectOption key={shift.code} value={shift.code}>{askedFor(shift.code, names)}</NativeSelectOption>
        ))}
      </SelectField>
      <Field data-invalid={!!reasonError || undefined}>
        <FieldLabel htmlFor="request-reason">Reason (optional)</FieldLabel>
        <Textarea id="request-reason" rows={2} aria-invalid={!!reasonError || undefined}
          aria-describedby={["request-reason-description", reasonError && "request-reason-error"].filter(Boolean).join(" ")}
          {...form.register("reason")} />
        <FieldDescription id="request-reason-description">Only you and your manager read it.</FieldDescription>
        <FieldError id="request-reason-error" role={undefined} errors={reasonError ? [reasonError] : undefined} />
      </Field>
      {refusal && <p role="alert" className="text-sm text-destructive">{refusal}</p>}
      {make.isError && !(make.error instanceof ApiError) && (
        <p role="alert" className="text-sm text-destructive">{NO_CONNECTION}</p>
      )}
      <div><Button type="submit" disabled={make.isPending}>Make the request</Button></div>
    </form>
  )
}

function Outcome({ row, names }: { row: MyRequestRow; names: Record<string, string> }) {
  const decision = lastDecision(row.steps)
  const changed = row.promised !== null && row.promised !== row.shift
  return (
    <div className="flex flex-col gap-0.5">
      <span>{MY_STATE[row.state]}</span>
      {changed && <span className="text-sm">Now promised: {askedFor(row.promised ?? "", names)}</span>}
      {decision?.reason && <span className="text-sm text-muted-foreground">{decision.by ? `${decision.by}: ` : ""}{decision.reason}</span>}
    </div>
  )
}

/** A staff nurse's requests for the month and their outcomes, and the form to make one while the window is open. */
export function MyRequestsCard({ month }: { month: string | undefined }) {
  const queryClient = useQueryClient()
  const mine = useGetMyRequests(month ? { month } : undefined, { query: { placeholderData: keepPreviousData } })
  const withdraw = useWithdrawRequest()
  const [said, setSaid] = useState("")
  const status = useRef<HTMLParagraphElement>(null)
  const answer = mine.data
  const names = shiftNames(answer?.shifts ?? [])
  const open = answer?.window?.state === "open"

  const tell = (message: string) => {
    setSaid(message)
    status.current?.focus()
  }
  const withdrawOne = (row: MyRequestRow) => {
    setSaid("")
    withdraw.mutate({ requestId: row.id }, {
      onSuccess: async () => {
        await queryClient.invalidateQueries({ queryKey: getGetMyRequestsQueryKey() })
        tell(`Your request for ${inSentence(row.shift, names)} on ${shortDay(row.date)} is withdrawn.`)
      },
      onError: (error) => tell(plainRefusal(error) ?? NO_CONNECTION),
    })
  }

  return (
    <Card>
      <CardHeader><CardTitle><h3>Requests</h3></CardTitle></CardHeader>
      <CardContent className="flex flex-col gap-3">
        {mine.isPending && <p>Loading…</p>}
        {mine.isError && <p role="alert">Your requests could not be read. Reload the page to try again.</p>}
        {/* Always rendered, because a live region mounted with its text may never be read out. */}
        <p id={STATUS} role="status" ref={status} tabIndex={-1} className="text-sm outline-none empty:sr-only">{said}</p>
        {answer && (
          <>
            <p>{windowWords(answer)}</p>
            {open && answer.refusal && <p>{answer.refusal}</p>}
            {answer.requests.length === 0 ? <p>You have made no requests for {monthName(answer.month)}.</p> : (
              <Table container={scrollableRegion(`Your requests for ${monthName(answer.month)}`)} className={WRAP_ON_PHONE}>
                <TableHeader>
                  <TableRow>
                    <TableHead>Date</TableHead>
                    <TableHead>You asked for</TableHead>
                    <TableHead>Outcome</TableHead>
                    {open && <TableHead><span className="sr-only">Actions</span></TableHead>}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {answer.requests.map((row) => (
                    <TableRow key={row.id}>
                      <TableCell className="tabular-nums">{shortDay(row.date)}</TableCell>
                      <TableCell>
                        {askedFor(row.shift, names)}
                        {herReason(row.steps) && <div className="text-sm text-muted-foreground">{herReason(row.steps)}</div>}
                      </TableCell>
                      <TableCell><Outcome row={row} names={names} /></TableCell>
                      {open && (
                        <TableCell>
                          {row.state === "pending" && (
                            <Button variant="outline" size="sm" disabled={withdraw.isPending} onClick={() => withdrawOne(row)}
                              aria-label={`Withdraw your request for ${shortDay(row.date)}`}>Withdraw</Button>
                          )}
                        </TableCell>
                      )}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
            {open && !answer.refusal && <RequestForm key={answer.month} answer={answer} onMade={tell} />}
          </>
        )}
      </CardContent>
    </Card>
  )
}
