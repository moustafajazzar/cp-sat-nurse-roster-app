import { zodResolver } from "@hookform/resolvers/zod"
import { keepPreviousData, useQueryClient } from "@tanstack/react-query"
import { useRef, useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { ApiError } from "@/api/fetcher"
import {
  getGetMyPreferencesQueryKey, getGetMyRequestsQueryKey, useAskPreferenceAsRequest, useGetMyPreferences, useMakePreference,
  useWithdrawPreference,
} from "@/api/generated/endpoints"
import type { MyPreferenceRow, MyPreferences } from "@/api/generated/model"
import { CHECKBOX, FormField } from "@/components/form-field"
import {
  AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Field, FieldError, FieldLabel, FieldLegend, FieldSet } from "@/components/ui/field"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { askedFor, inSentence, NO_CONNECTION, PREFERENCE_OUTCOME, PREFERENCE_STATE, PREFERS, shiftNames } from "@/lib/request-words"
import { plainRefusal, serverErrors, showServerErrors } from "@/lib/server-errors"
import { monthAlone, monthName, scrollableRegion, shortDay, WRAP_ON_PHONE } from "@/lib/utils"

const STATUS = "my-preferences-status"

const prefersInSentence = (kind: MyPreferenceRow["kind"]) => PREFERS[kind].toLowerCase()

function windowWords(answer: MyPreferences): string {
  const month = monthName(answer.month)
  const window = answer.window
  if (!window) return `No request window has been opened for ${month}, so there are no preferences to make yet.`
  const closes = shortDay(window.closes_on)
  switch (window.state) {
    case "scheduled":
      return `Preferences for ${month} open on ${shortDay(window.opens_on)} and are taken until the end of ${closes}.`
    case "open":
      return `Preferences for ${month} are open until the end of ${closes}. You have ${answer.counted} of ${answer.preference_cap} preferences.`
    case "closed":
      return `The request window for ${month} has closed${window.closed_early ? " early" : ""}, so your preferences are fixed. The solver keeps them when it can.`
  }
}

/** "4 of 5 kept" once the roster is approved, "No roster yet" before, and nothing with none of hers to keep. */
function keptWords(answer: MyPreferences): string | null {
  if (answer.keepable === null) return answer.preferences.some((row) => row.state === "made") ? "No roster yet" : null
  return answer.keepable > 0 ? `${answer.kept} of ${answer.keepable} kept` : null
}

const formSchema = z.object({ date: z.iso.date("Choose a date."), kind: z.enum(["day_off", "working_day"]) })
type FormValues = z.infer<typeof formSchema>

function PreferenceForm({ answer, onMade }: { answer: MyPreferences; onMade: (message: string) => void }) {
  const queryClient = useQueryClient()
  const make = useMakePreference()
  const form = useForm<FormValues>({ resolver: zodResolver(formSchema), defaultValues: { date: "", kind: "day_off" } })
  const refusal = plainRefusal(make.error)

  const submit = form.handleSubmit(({ date, kind }) => make.mutate({ data: { date, kind } }, {
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: getGetMyPreferencesQueryKey() })
      form.reset()
      onMade(`Your preference for ${prefersInSentence(kind)} on ${shortDay(date)} is made. The solver keeps it when it can.`)
    },
    onError: (error) => showServerErrors(form, error),
  }))

  return (
    <form onSubmit={submit} noValidate className="flex max-w-md flex-col gap-3">
      <h4 className="font-medium">Make a preference</h4>
      <FormField id="preference-date" label="Day" type="date" min={answer.first_day ?? undefined} max={answer.last_day ?? undefined}
        registration={form.register("date")} error={form.formState.errors.date} />
      <FieldSet>
        <FieldLegend variant="label">What you prefer</FieldLegend>
        <div className="flex flex-wrap gap-x-4 gap-y-2">
          {(["day_off", "working_day"] as const).map((kind) => (
            <label key={kind} className="flex items-center gap-2 text-sm pointer-coarse:min-h-11">
              <input type="radio" value={kind} className={CHECKBOX} {...form.register("kind")} /> {PREFERS[kind]}
            </label>
          ))}
        </div>
      </FieldSet>
      {refusal && <p role="alert" className="text-sm text-destructive">{refusal}</p>}
      {make.isError && !(make.error instanceof ApiError) && (
        <p role="alert" className="text-sm text-destructive">{NO_CONNECTION}</p>
      )}
      <div><Button type="submit" disabled={make.isPending}>Make the preference</Button></div>
    </form>
  )
}

type Asking = { row: MyPreferenceRow; what: "withdraw" | "ask" }

/** Withdraw, or ask for it as a request, each behind a question; the second names the request cap and, for a working day,
 *  asks for the shift. A refusal stays in the question, beside what it is about. */
function PreferenceQuestion({ asking, answer, focusAfter, onDone, onClose }: {
  asking: Asking
  answer: MyPreferences
  focusAfter: React.RefObject<string | null>
  onDone: (message: string) => void
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const withdraw = useWithdrawPreference()
  const ask = useAskPreferenceAsRequest()
  const [shift, setShift] = useState("")
  const [missing, setMissing] = useState(false)
  const { row, what } = asking
  const day = shortDay(row.date)
  const names = shiftNames(answer.shifts)
  const working = row.kind === "working_day"
  const pending = withdraw.isPending || ask.isPending
  const failed = what === "withdraw" ? withdraw.error : ask.error
  // A 422 is about the date or the shift, neither of which the question lets her change but the shift: said here all the same.
  const refusal = plainRefusal(failed) ?? serverErrors(failed)[0]?.message
  const reread = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: getGetMyPreferencesQueryKey() }),
      queryClient.invalidateQueries({ queryKey: getGetMyRequestsQueryKey() }),
    ])
  }
  const confirm = () => {
    if (what === "withdraw") {
      withdraw.mutate({ preferenceId: row.id }, {
        onSuccess: async () => {
          await reread()
          onDone(`Your preference for ${prefersInSentence(row.kind)} on ${day} is withdrawn.`)
        },
      })
      return
    }
    if (working && !shift) {
      setMissing(true)
      document.getElementById("ask-shift")?.focus()
      return
    }
    ask.mutate({ preferenceId: row.id, data: { shift: working ? shift : null } }, {
      onSuccess: async () => {
        await reread()
        onDone(`Your preference for ${day} is now a request for ${inSentence(working ? shift : "", names)}. Your manager decides it.`)
      },
    })
  }
  const cap = answer.request_cap ?? 0
  const title = what === "withdraw" ? `Withdraw your preference for ${day}?` : `Ask for ${day} as a request?`
  const body = what === "withdraw"
    ? "The solver no longer leans your way that day. While the window is open you can make it again."
    : `The preference ends, and a request ${working ? "for the shift you choose" : "for a day off"} on ${day} takes its place. Your manager decides it, and an accepted request is kept in every solve. It counts against your unit's ${cap} ${cap === 1 ? "request" : "requests"} a month, pending or accepted.`

  return (
    <AlertDialog open onOpenChange={(open) => { if (!open && !pending) onClose() }}>
      <AlertDialogContent finalFocus={() => (focusAfter.current ? document.getElementById(focusAfter.current) : true)}>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{body}</AlertDialogDescription>
        </AlertDialogHeader>
        {what === "ask" && working && (
          <Field data-invalid={missing || undefined}>
            <FieldLabel htmlFor="ask-shift">Shift</FieldLabel>
            <NativeSelect id="ask-shift" value={shift} aria-invalid={missing || undefined}
              aria-describedby={missing ? "ask-shift-error" : undefined}
              onChange={(event) => { setShift(event.target.value); setMissing(false) }}>
              <NativeSelectOption value="">Choose a shift</NativeSelectOption>
              {answer.shifts.map((option) => (
                <NativeSelectOption key={option.code} value={option.code}>{askedFor(option.code, names)}</NativeSelectOption>
              ))}
            </NativeSelect>
            <FieldError id="ask-shift-error" role={undefined} errors={missing ? [{ message: "Choose the shift you ask for." }] : undefined} />
          </Field>
        )}
        {failed !== null && <p role="alert" className="text-sm text-destructive">{refusal ?? NO_CONNECTION}</p>}
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <Button variant={what === "withdraw" ? "destructive" : "default"} disabled={pending} onClick={confirm}>
            {what === "withdraw" ? "Withdraw it" : "Ask for it"}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

/** A staff nurse's preferences for the month: days she would rather have off or work, which nobody decides and the solver keeps when it can, and the
 *  form to make one while the window is open. */
export function MyPreferencesCard({ month }: { month: string | undefined }) {
  const mine = useGetMyPreferences(month ? { month } : undefined, { query: { placeholderData: keepPreviousData } })
  const [said, setSaid] = useState("")
  const [asking, setAsking] = useState<Asking | null>(null)
  const status = useRef<HTMLParagraphElement>(null)
  const focusAfter = useRef<string | null>(null)
  const answer = mine.data
  const open = answer?.window?.state === "open"
  const keptLine = answer ? keptWords(answer) : null
  const approved = answer?.keepable != null

  const tell = (message: string) => {
    setSaid(message)
    status.current?.focus()
  }
  const begin = (row: MyPreferenceRow, what: Asking["what"]) => {
    setSaid("")
    focusAfter.current = `preference-${what}-${row.id}`
    setAsking({ row, what })
  }
  const button = (row: MyPreferenceRow, what: Asking["what"], label: string, name: string) => (
    <Button id={`preference-${what}-${row.id}`} variant="outline" size="sm" onClick={() => begin(row, what)} aria-label={name}>
      {label}
    </Button>
  )

  return (
    <Card>
      <CardHeader>
        <CardTitle><h3>Preferences</h3></CardTitle>
        {keptLine && <CardAction><p className="text-sm font-medium tabular-nums">{keptLine}</p></CardAction>}
        <CardDescription>
          A day you would rather have off, or rather work. Nobody decides a preference: the solver keeps it when it can. Only you and your manager read it.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {mine.isPending && <p>Loading…</p>}
        {mine.isError && <p role="alert">Your preferences could not be read. Reload the page to try again.</p>}
        {/* Always rendered, because a live region mounted with its text may never be read out. */}
        <p id={STATUS} role="status" ref={status} tabIndex={-1} className="text-sm outline-none empty:sr-only">{said}</p>
        {answer && (
          <>
            <p>{windowWords(answer)}</p>
            {answer.doubled_by && (
              <p>One of your preferences was not kept in {monthAlone(answer.doubled_by)}, so this month yours count double.</p>
            )}
            {open && answer.refusal && <p>{answer.refusal}</p>}
            {answer.preferences.length === 0 ? <p>You have made no preferences for {monthName(answer.month)}.</p> : (
              <Table container={scrollableRegion(`Your preferences for ${monthName(answer.month)}`)} className={WRAP_ON_PHONE}>
                <TableHeader>
                  <TableRow>
                    <TableHead>Date</TableHead>
                    <TableHead>You prefer</TableHead>
                    <TableHead>State</TableHead>
                    {approved && <TableHead>Outcome</TableHead>}
                    {open && <TableHead><span className="sr-only">Actions</span></TableHead>}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {answer.preferences.map((row) => (
                    <TableRow key={row.id}>
                      <TableCell className="tabular-nums">{shortDay(row.date)}</TableCell>
                      <TableCell>{PREFERS[row.kind]}</TableCell>
                      <TableCell>{PREFERENCE_STATE[row.state]}</TableCell>
                      {approved && <TableCell>{row.outcome ? PREFERENCE_OUTCOME[row.outcome] : ""}</TableCell>}
                      {open && (
                        <TableCell>
                          {row.state === "made" && (
                            <div className="flex flex-wrap gap-2">
                              {button(row, "withdraw", "Withdraw", `Withdraw your preference for ${shortDay(row.date)}`)}
                              {button(row, "ask", "Ask for it as a request", `Ask for your preference for ${shortDay(row.date)} as a request`)}
                            </div>
                          )}
                        </TableCell>
                      )}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
            {open && !answer.refusal && <PreferenceForm key={answer.month} answer={answer} onMade={tell} />}
            {asking && (
              <PreferenceQuestion key={`${asking.what}-${asking.row.id}`} asking={asking} answer={answer} focusAfter={focusAfter}
                onClose={() => setAsking(null)}
                onDone={(message) => {
                  focusAfter.current = STATUS
                  setAsking(null)
                  tell(message)
                }} />
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}
