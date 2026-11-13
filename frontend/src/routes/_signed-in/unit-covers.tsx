import { keepPreviousData, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, Link, useNavigate, useRouteContext } from "@tanstack/react-router"
import { useEffect, useRef, useState } from "react"
import { useForm } from "react-hook-form"
import { zodResolver } from "@hookform/resolvers/zod"
import { z } from "zod"

import { ApiError } from "@/api/fetcher"
import {
  getGetUnitCoverQueryKey, getListUnitCoversQueryKey, useGetUnitCover, useGetUnitRules, useListShifts, useListUnitCovers,
  useOfferUnitCover, useOpenUnitCover, useWithdrawUnitCover,
} from "@/api/generated/endpoints"
import { Role, type CoverCheck, type CoverOfferRow, type CoverPage, type UnitCoverRow } from "@/api/generated/model"
import { ConfirmDialog } from "@/components/confirm-dialog"
import { CHECKBOX, FormField, SelectField } from "@/components/form-field"
import { MONTH_LINK, monthInAddress, MonthNav } from "@/components/month-nav"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field"
import { NativeSelectOption } from "@/components/ui/native-select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Textarea } from "@/components/ui/textarea"
import { useChosenUnit } from "@/components/unit-picker"
import { COVER_STATE, coverName, hoursWords, OFFER_ANSWER, overtimeWords, REPLACES_PIN, WAITING } from "@/lib/cover-words"
import { NO_CONNECTION, shiftNames } from "@/lib/request-words"
import { plainRefusal, serverErrors, showServerErrors } from "@/lib/server-errors"
import { hospitalTime, monthName, scrollableRegion } from "@/lib/utils"
import { useFocusOnOpen } from "@/lib/use-focus-on-open"
import { onlyFor } from "@/session"

const day = z.string().regex(/^\d{4}-\d{2}-\d{2}$/).optional().catch(undefined)
const searchSchema = z.object({
  month: monthInAddress,
  cover: z.string().optional().catch(undefined),
  /** A shift-day to open a cover for, from the roster grid: the form opens with it filled in. */
  date: day,
  shift: z.string().optional().catch(undefined),
})

/** The unit manager's covers: each month's, one cover's shift-day and who could take it, and opening one by hand. */
export const Route = createFileRoute("/_signed-in/unit-covers")({
  validateSearch: (search) => searchSchema.parse(search),
  beforeLoad: onlyFor(Role.unit_manager),
  staticData: { title: "Covers" },
  component: UnitCoversPage,
})

const LINK = "underline underline-offset-3"
const STATUS = "unit-covers-status"
const OPEN_BUTTON = "open-cover"

function openedWords(cover: UnitCoverRow): string {
  const when = hospitalTime(cover.opened_at)
  return cover.opened === "leave"
    ? `Opened ${when} by leave, which took the shift off the roster in force as version ${cover.opened_on}.`
    : `Opened ${when} by you, on version ${cover.opened_on}.`
}

function endedWords(cover: UnitCoverRow): string {
  const when = cover.ended_at ? `, ${hospitalTime(cover.ended_at)}` : ""
  switch (cover.state) {
    case "withdrawn": return ` Withdrawn by ${cover.ended_by ?? "you"}${when}.`
    case "filled": return ` A new version of the roster put one more nurse on this shift-day, which filled it${when}.`
    case "expired": return " Its date passed before anyone took it."
    case "taken": return ` Taken by ${cover.taker}${cover.version ? `, in version ${cover.version}` : ""}${when}.`
    default: return ""
  }
}

/** Who could take it, with a tick for each not yet offered it (all ticked until the manager unticks one) and, for one already
 *  in it, her answer so far. `ticks` is null once the cover has ended, or when it cannot be offered now. */
function WhoCould({ check, offered, ticks }: {
  check: CoverCheck
  offered: Record<string, CoverOfferRow>
  ticks: { unticked: Set<string>; toggle: (hospitalId: string, on: boolean) => void } | null
}) {
  if (check.reason) return <p>Who could take it cannot be worked out now. {check.reason}</p>
  const lacking = check.lack.length > 0
  return (
    <>
      {lacking ? (
        <div className="flex flex-col gap-1">
          <p>On the roster in force this shift-day lacks:</p>
          <ul className="list-disc ps-5 text-sm">{check.lack.map((lack) => <li key={lack}>{lack}</li>)}</ul>
        </div>
      ) : <p>On the roster in force this shift-day lacks nothing the solver checks.</p>}
      {check.full ? <p>{check.full}</p> : check.nurses.length === 0 ? (
        <p>No staff nurse could take it without newly breaking a hard rule. To cover it, edit the roster.</p>
      ) : (
        <Table container={scrollableRegion("Who could take it, fairest first")}>
          <TableHeader>
            <TableRow>
              {ticks && <TableHead>Offer</TableHead>}
              <TableHead>Nurse</TableHead>
              <TableHead>Hours this month</TableHead>
              <TableHead>Overtime this year</TableHead>
              {/* Each would, when it lacks nothing. */}
              {lacking && <TableHead>Makes the shift-day whole</TableHead>}
              <TableHead>Note</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {check.nurses.map((nurse) => {
              const name = nurse.full_name || nurse.hospital_id
              const theirs = offered[nurse.hospital_id]
              return (
                <TableRow key={nurse.hospital_id}>
                  {ticks && (
                    <TableCell>
                      {theirs ? `Offered: ${OFFER_ANSWER[theirs.answer].toLowerCase()}` : (
                        <label className="flex items-center pointer-coarse:min-h-11">
                          <input type="checkbox" className={CHECKBOX} aria-label={`Offer it to ${name}`}
                            checked={!ticks.unticked.has(nurse.hospital_id)}
                            onChange={(event) => ticks.toggle(nurse.hospital_id, event.target.checked)} />
                        </label>
                      )}
                    </TableCell>
                  )}
                  <TableCell>{name}</TableCell>
                  <TableCell className="tabular-nums">{hoursWords(nurse)}</TableCell>
                  <TableCell className="tabular-nums">{overtimeWords(nurse)}</TableCell>
                  {lacking && <TableCell>{nurse.makes_whole ? "Yes" : "No"}</TableCell>}
                  <TableCell>{nurse.replaces_pin ? REPLACES_PIN[nurse.replaces_pin] : ""}</TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      )}
    </>
  )
}

function Answers({ offers }: { offers: CoverOfferRow[] }) {
  return (
    <section aria-labelledby="cover-answers" className="flex flex-col gap-2">
      <h3 id="cover-answers" className="text-sm font-medium">Offered to</h3>
      <Table container={scrollableRegion("Nurses offered it, with their answers")}>
        <TableHeader>
          <TableRow>
            <TableHead>Nurse</TableHead>
            <TableHead>Answer</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {offers.map((offer) => (
            <TableRow key={offer.hospital_id}>
              <TableCell>{offer.full_name || offer.hospital_id}</TableCell>
              <TableCell>{OFFER_ANSWER[offer.answer]}{offer.answered_at ? `, ${hospitalTime(offer.answered_at)}` : ""}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </section>
  )
}

const OFFER_REFUSAL = "cover-offer-refusal"

function CoverDetail({ unitId, coverId, names, onDone }: {
  unitId: string
  coverId: string
  names: Record<string, string>
  onDone: (message: string) => void
}) {
  const queryClient = useQueryClient()
  const page = useGetUnitCover(unitId, coverId)
  const withdraw = useWithdrawUnitCover()
  const offer = useOfferUnitCover()
  const [asking, setAsking] = useState(false)
  const [unticked, setUnticked] = useState<Set<string>>(new Set())
  const [note, setNote] = useState("")
  const offerAlert = useRef<HTMLDivElement>(null)
  const focusAfter = useRef<string | null>(null)
  const answer: CoverPage | undefined = page.data
  const cover = answer?.cover
  const name = cover ? coverName(cover, names) : ""
  const offered = Object.fromEntries((answer?.offers ?? []).map((row) => [row.hospital_id, row]))
  const check = answer?.check
  const offerable = check && !check.reason && !check.full ? check.nurses.filter((nurse) => !offered[nurse.hospital_id]) : []
  const ticked = offerable.filter((nurse) => !unticked.has(nurse.hospital_id))
  const adding = cover?.state === "offered"
  const offerRefusals = [...serverErrors(offer.error).map((error) => error.message), ...(plainRefusal(offer.error) ? [plainRefusal(offer.error)!] : [])]
  const refresh = () => Promise.all([
    queryClient.invalidateQueries({ queryKey: getListUnitCoversQueryKey(unitId) }),
    queryClient.invalidateQueries({ queryKey: getGetUnitCoverQueryKey(unitId, coverId) }),
  ])

  const offerIt = () => offer.mutate({ unitId, coverId, data: { nurses: ticked.map((nurse) => nurse.hospital_id), note: adding ? "" : note } }, {
    onSuccess: async () => {
      await refresh()
      const count = ticked.length === 1 ? "1 nurse" : `${ticked.length} nurses`
      setUnticked(new Set())
      onDone(adding ? `You added ${count} to the cover for ${name}. Each is told.` : `You offered the cover for ${name} to ${count}. Each is told.`)
    },
    onError: () => offerAlert.current?.focus(),
  })

  const confirm = () => withdraw.mutate({ unitId, coverId }, {
    onSuccess: async () => {
      await refresh()
      focusAfter.current = STATUS
      setAsking(false)
      onDone(`You withdrew the cover for ${name}. Nothing on the roster changes.`)
    },
  })

  return (
    <section aria-labelledby="cover-heading" className="flex flex-col gap-3">
      <h2 id="cover-heading" className="text-base font-medium">{cover ? `${name}: ${COVER_STATE[cover.state]}` : "The cover"}</h2>
      {page.isPending && <p>Loading…</p>}
      {page.isError && <p role="alert">This cover could not be read. Reload the page to try again.</p>}
      {cover && answer && (
        <>
          <p className="text-sm text-muted-foreground">{openedWords(cover)}{endedWords(cover)}</p>
          {cover.note && <p className="text-sm">Your note to the nurses offered it: {cover.note}</p>}
          {answer.check && (
            <WhoCould check={answer.check} offered={offered} ticks={WAITING.includes(cover.state) ? {
              unticked,
              toggle: (hospitalId, on) => setUnticked((was) => {
                const next = new Set(was)
                if (on) next.delete(hospitalId)
                else next.add(hospitalId)
                return next
              }),
            } : null} />
          )}
          {cover.state === "open" && <p>It is not offered to anyone yet.</p>}
          {WAITING.includes(cover.state) && offerable.length > 0 && (
            <form noValidate className="flex max-w-xl flex-col gap-3" onSubmit={(event) => { event.preventDefault(); offerIt() }}>
              {!adding && (
                <Field>
                  <FieldLabel htmlFor="cover-note">Note (optional)</FieldLabel>
                  <Textarea id="cover-note" rows={2} maxLength={500} aria-describedby="cover-note-description" value={note}
                    onChange={(event) => setNote(event.target.value)} />
                  <FieldDescription id="cover-note-description">
                    Each nurse offered it reads it beside the date and the shift. They are never told why the shift is short or who else was asked.
                  </FieldDescription>
                </Field>
              )}
              <div ref={offerAlert} id={OFFER_REFUSAL} tabIndex={-1} className="outline-none">
                {offerRefusals.length > 0 && (
                  <div role="alert" className="flex flex-col gap-1 text-sm text-destructive">
                    {offerRefusals.map((message) => <p key={message}>{message}</p>)}
                  </div>
                )}
                {offer.isError && !(offer.error instanceof ApiError) && <p role="alert" className="text-sm text-destructive">{NO_CONNECTION}</p>}
              </div>
              <div>
                <Button type="submit" disabled={offer.isPending}>{adding ? "Add to the offer" : "Offer the cover"}</Button>
              </div>
            </form>
          )}
          {answer.offers.length > 0 && <Answers offers={answer.offers} />}
          {WAITING.includes(cover.state) && (
            <div>
              <Button id="cover-withdraw" variant="outline" aria-label={`Withdraw the cover for ${name}`}
                onClick={() => { withdraw.reset(); focusAfter.current = "cover-withdraw"; setAsking(true) }}>Withdraw</Button>
            </div>
          )}
          <ConfirmDialog
            question={asking ? { title: `Withdraw the cover for ${name}?`, body: "It ends, and nothing on the roster changes. To ask for one more nurse on this shift-day again, open a new cover.", confirm: "Withdraw the cover" } : null}
            pending={withdraw.isPending} failed={withdraw.isError}
            failure={withdraw.error instanceof ApiError ? plainRefusal(withdraw.error) : NO_CONNECTION}
            focusAfter={focusAfter} onConfirm={confirm} onCancel={() => setAsking(false)} />
        </>
      )}
    </section>
  )
}

const openSchema = z.object({
  date: z.string().min(1, "Choose the date."),
  shift: z.string().min(1, "Choose the shift."),
})
type OpenValues = z.infer<typeof openSchema>

function OpenCoverForm({ unitId, given, names, onOpened, onCancel }: {
  unitId: string
  given: { date?: string; shift?: string }
  names: Record<string, string>
  onOpened: (page: CoverPage) => void
  onCancel: () => void
}) {
  const queryClient = useQueryClient()
  const rules = useGetUnitRules(unitId)
  const open = useOpenUnitCover()
  const form = useForm<OpenValues>({ resolver: zodResolver(openSchema), defaultValues: { date: given.date ?? "", shift: given.shift ?? "" } })
  const { errors } = form.formState
  const shifts = rules.data?.rules.shifts ?? []
  const refusal = plainRefusal(open.error)

  useFocusOnOpen(() => { form.setFocus(given.date ? "shift" : "date") }, [form, given.date])
  // The select has no options until the unit's shifts arrive, so a value set before then would not show.
  useEffect(() => { if (given.shift && rules.data) form.setValue("shift", given.shift) }, [form, given.shift, rules.data])

  const submit = form.handleSubmit((data) => open.mutate({ unitId, data }, {
    onSuccess: async (page) => {
      await queryClient.invalidateQueries({ queryKey: getListUnitCoversQueryKey(unitId) })
      onOpened(page)
    },
    onError: (error) => showServerErrors(form, error),
  }))

  return (
    <Card className="max-w-xl">
      <CardHeader>
        <CardTitle><h2>Open a cover</h2></CardTitle>
        <CardDescription>For one more nurse on a shift of the roster in force, today or later. Nobody is told until you offer it.</CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} noValidate className="flex flex-col gap-4">
          <FieldGroup>
            <FormField label="Date" type="date" registration={form.register("date")} error={errors.date} />
            <SelectField label="Shift" registration={form.register("shift")} error={errors.shift}
              description={rules.isError ? "The unit's shifts could not be read. Reload the page to try again." : undefined}>
              <NativeSelectOption value="">Choose a shift</NativeSelectOption>
              {shifts.map((code) => <NativeSelectOption key={code} value={code}>{names[code] ? `${names[code]} (${code})` : code}</NativeSelectOption>)}
            </SelectField>
          </FieldGroup>
          {refusal && <p role="alert" className="text-sm text-destructive">{refusal}</p>}
          {open.isError && !(open.error instanceof ApiError) && <p role="alert" className="text-sm text-destructive">{NO_CONNECTION}</p>}
          <div className="flex gap-2">
            <Button type="submit" disabled={open.isPending}>Open the cover</Button>
            <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}

function UnitCovers({ unitId, month, coverId, given }: {
  unitId: string
  month: string | undefined
  coverId: string | undefined
  given: { date?: string; shift?: string }
}) {
  const navigate = useNavigate()
  const covers = useListUnitCovers(unitId, month ? { month } : undefined, { query: { placeholderData: keepPreviousData } })
  const catalogue = useListShifts()
  const names = shiftNames(catalogue.data ?? [])
  const [said, setSaid] = useState("")
  const [opening, setOpening] = useState(!!(given.date || given.shift))
  const status = useRef<HTMLParagraphElement>(null)
  const rows = covers.data?.covers
  const shown = covers.data?.month
  const chosen = rows?.find((cover) => cover.id === coverId) ?? rows?.find((cover) => WAITING.includes(cover.state))
  const tell = (message: string) => {
    setSaid(message)
    status.current?.focus()
  }
  /** Cancelling gives focus back to the button that opened the form, once it is drawn again. */
  const refocus = useRef(false)
  useEffect(() => {
    if (opening || !refocus.current) return
    refocus.current = false
    document.getElementById(OPEN_BUTTON)?.focus()
  }, [opening])
  const close = () => {
    refocus.current = true
    setOpening(false)
    if (given.date || given.shift) void navigate({ to: "/unit-covers", search: { month, cover: coverId } })
  }

  return (
    <>
      <p id={STATUS} role="status" ref={status} tabIndex={-1} className="text-sm outline-none empty:sr-only">{said}</p>
      {opening ? (
        <OpenCoverForm unitId={unitId} given={given} names={names} onCancel={close}
          onOpened={(page) => {
            setOpening(false)
            void navigate({ to: "/unit-covers", search: { month: page.cover.month, cover: page.cover.id } })
            tell(`You opened a cover for ${coverName(page.cover, names)}. It is not offered to anyone yet.`)
          }} />
      ) : (
        <div><Button id={OPEN_BUTTON} onClick={() => { setSaid(""); setOpening(true) }}>Open a cover</Button></div>
      )}
      {covers.isPending && <p>Loading…</p>}
      {covers.isError && <p role="alert">The covers could not be read. Reload the page to try again.</p>}
      {rows && (
        <>
          {shown && <MonthNav month={shown} link={(other, label) => <Link to="/unit-covers" search={{ month: other }} className={MONTH_LINK}>{label}</Link>} />}
          {rows.length === 0 ? <p>No cover has been opened for {shown ? monthName(shown) : "this month"}.</p> : (
            <Table container={scrollableRegion("The unit's covers, newest first")}>
              <TableHeader>
                <TableRow>
                  <TableHead>Shift</TableHead>
                  <TableHead>State</TableHead>
                  <TableHead>Opened by</TableHead>
                  <TableHead>Taken by</TableHead>
                  <TableHead><span className="sr-only">Actions</span></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((cover) => (
                  <TableRow key={cover.id} aria-current={cover.id === chosen?.id || undefined}>
                    <TableCell className="tabular-nums">{coverName(cover, names)}</TableCell>
                    <TableCell>{COVER_STATE[cover.state]}</TableCell>
                    <TableCell>{cover.opened === "leave" ? "Leave" : "You"}</TableCell>
                    <TableCell>{cover.taker ?? ""}</TableCell>
                    <TableCell>
                      {cover.id !== chosen?.id && (
                        <Link to="/unit-covers" search={{ month: cover.month, cover: cover.id }} className={LINK}
                          aria-label={`Show the cover for ${coverName(cover, names)}`}>Show</Link>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          {chosen && <CoverDetail key={chosen.id} unitId={unitId} coverId={chosen.id} names={names} onDone={tell} />}
        </>
      )}
    </>
  )
}

function UnitCoversPage() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  const { month, cover, date, shift } = Route.useSearch()
  const { units, chosen } = useChosenUnit(user, undefined)
  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4">
      <h1 className="text-xl font-medium">{chosen ? `Covers in ${chosen.name}` : "Covers"}</h1>
      <p className="text-muted-foreground">
        A cover asks for one more nurse on one shift of the roster in force. Leave that takes a shift off the roster opens one by itself, and you can open one for any shift from today on. Each cover lists the staff nurses who could take it without breaking a hard rule, furthest below their owed hours first, and says what its shift still lacks. Offer it to any of them: the first to accept takes it, in a new version of the roster.
      </p>
      {units.isPending && <p>Loading…</p>}
      {units.isError && <p role="alert">Your unit could not be read. Reload the page to try again.</p>}
      {units.data && !chosen && <p>No unit is assigned to you yet. The CNO assigns each unit its manager.</p>}
      {chosen && <UnitCovers key={chosen.id} unitId={chosen.id} month={month ?? date?.slice(0, 7)} coverId={cover} given={{ date, shift }} />}
    </div>
  )
}
