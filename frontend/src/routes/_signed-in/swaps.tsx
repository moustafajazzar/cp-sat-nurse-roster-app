import { keepPreviousData, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, Link } from "@tanstack/react-router"
import { useRef, useState, type RefObject } from "react"
import { useFieldArray, useForm } from "react-hook-form"
import { z } from "zod"

import { ApiError } from "@/api/fetcher"
import {
  getListMySwapsQueryKey, useAcceptSwap, useGetSwapComposer, useListMySwaps, useListShifts, useProposeSwap, useRejectSwap,
  useWithdrawSwap,
} from "@/api/generated/endpoints"
import { Role, type MySwapRow, type SwapComposer } from "@/api/generated/model"
import { ConfirmDialog, type Question } from "@/components/confirm-dialog"
import { MONTH_LINK, monthInAddress, MonthNav } from "@/components/month-nav"
import { SwapCells } from "@/components/swap-cells"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import { Textarea } from "@/components/ui/textarea"
import { NO_CONNECTION, shiftNames } from "@/lib/request-words"
import { plainRefusal, serverErrors } from "@/lib/server-errors"
import { answersWords, cellWords, swapDays, SWAP_STATE, WAITING } from "@/lib/swap-words"
import { hospitalTime, shortDay } from "@/lib/utils"
import { onlyFor } from "@/session"

const searchSchema = z.object({ month: monthInAddress })

/** A staff nurse's swaps: proposing one on the roster in force, and the swaps they proposed or are in, with their answers. */
export const Route = createFileRoute("/_signed-in/swaps")({
  validateSearch: (search) => searchSchema.parse(search),
  beforeLoad: onlyFor(Role.staff_nurse),
  staticData: { title: "Swaps" },
  component: SwapsPage,
})

const STATUS = "swaps-status"

type Cell = { hospital_id: string; date: string; shift: string }
type FormValues = { cells: Cell[]; note: string }

/** What each cell of the composer and the whole of it were refused for, from a 422's locations. */
function refusalsOf(error: unknown): { byCell: Record<number, string[]>; general: string[] } {
  const byCell: Record<number, string[]> = {}
  const general: string[] = []
  for (const { path, message } of serverErrors(error)) {
    const [field, index] = path.split(".")
    if (field === "cells" && index !== undefined) (byCell[Number(index)] ??= []).push(message)
    else general.push(message)
  }
  return { byCell, general }
}

function Composer({ composer, onProposed }: { composer: SwapComposer; onProposed: (message: string) => void }) {
  const queryClient = useQueryClient()
  const propose = useProposeSwap()
  const me = composer.nurses.find((nurse) => nurse.me)
  const first = composer.days[0]?.date ?? ""
  const blank = (hospital_id = me?.hospital_id ?? "", date = first): Cell => ({ hospital_id, date, shift: "" })
  const form = useForm<FormValues>({ defaultValues: { cells: [blank()], note: "" } })
  const cells = useFieldArray({ control: form.control, name: "cells" })
  const values = form.watch("cells")
  const names = shiftNames(composer.shifts)
  const refused = refusalsOf(propose.error)
  const refusal = plainRefusal(propose.error)
  const generalAlert = useRef<HTMLDivElement>(null)

  const now = (cell: Cell) => {
    const nurse = composer.nurses.find((row) => row.hospital_id === cell.hospital_id)
    const index = composer.days.findIndex((day) => day.date === cell.date)
    return nurse && index >= 0 ? nurse.shifts[index] : undefined
  }
  const submit = form.handleSubmit(({ cells: chosen, note }) => propose.mutate({
    data: { month: composer.month, note, cells: chosen.map((cell) => ({ ...cell, shift: cell.shift || null })) },
  }, {
    onSuccess: async (swap) => {
      await queryClient.invalidateQueries({ queryKey: getListMySwapsQueryKey() })
      form.reset({ cells: [blank()], note: "" })
      const asked = swap.colleagues.map((colleague) => colleague.full_name || colleague.hospital_id)
      onProposed(asked.length
        ? `Your swap is proposed. ${asked.join(" and ")} ${asked.length === 1 ? "is" : "are"} asked to accept it, then your manager decides.`
        : "Your swap is proposed. It changes only your own cells, so it goes straight to your manager.")
    },
    onError: (error) => {
      const { byCell, general } = refusalsOf(error)
      const firstCell = Object.keys(byCell).map(Number).sort((a, b) => a - b)[0]
      if (firstCell !== undefined && general.length === 0) document.getElementById(`swap-cell-${firstCell}-nurse`)?.focus()
      else generalAlert.current?.focus()
    },
  }))

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-4">
      <ol className="flex flex-col gap-3">
        {cells.fields.map((field, index) => {
          const value = values[index]
          const errors = refused.byCell[index] ?? []
          const errorId = errors.length ? `swap-cell-${index}-error` : undefined
          const held = value ? now(value) : undefined
          const nowId = `swap-cell-${index}-now`
          return (
            <li key={field.id} className="flex flex-col gap-2 rounded-md border p-3">
              <div className="grid gap-3 sm:grid-cols-3">
                <Field data-invalid={errors.length > 0 || undefined}>
                  <FieldLabel htmlFor={`swap-cell-${index}-nurse`}>Nurse</FieldLabel>
                  <NativeSelect id={`swap-cell-${index}-nurse`} aria-invalid={errors.length > 0 || undefined}
                    aria-describedby={[errorId, nowId].filter(Boolean).join(" ")} {...form.register(`cells.${index}.hospital_id`)}>
                    {composer.nurses.map((nurse) => (
                      <NativeSelectOption key={nurse.hospital_id} value={nurse.hospital_id}>
                        {nurse.me ? `${nurse.full_name || nurse.hospital_id} (you)` : nurse.full_name || nurse.hospital_id}
                      </NativeSelectOption>
                    ))}
                  </NativeSelect>
                </Field>
                <Field>
                  <FieldLabel htmlFor={`swap-cell-${index}-date`}>Date</FieldLabel>
                  <NativeSelect id={`swap-cell-${index}-date`} aria-describedby={nowId} {...form.register(`cells.${index}.date`)}>
                    {composer.days.map((day) => <NativeSelectOption key={day.date} value={day.date}>{shortDay(day.date)}</NativeSelectOption>)}
                  </NativeSelect>
                </Field>
                <Field>
                  <FieldLabel htmlFor={`swap-cell-${index}-shift`}>Becomes</FieldLabel>
                  <NativeSelect id={`swap-cell-${index}-shift`} aria-describedby={nowId} {...form.register(`cells.${index}.shift`)}>
                    <NativeSelectOption value="">Off</NativeSelectOption>
                    {composer.shifts.map((shift) => (
                      <NativeSelectOption key={shift.code} value={shift.code}>{cellWords(shift.code, names)}</NativeSelectOption>
                    ))}
                  </NativeSelect>
                </Field>
              </div>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p id={nowId} className="text-sm text-muted-foreground">
                  {held === undefined ? "" : `In the roster in force: ${cellWords(held, names)}.`}
                </p>
                {cells.fields.length > 1 && (
                  <Button type="button" variant="outline" size="sm" onClick={() => cells.remove(index)}
                    aria-label={`Remove cell ${index + 1}`}>Remove</Button>
                )}
              </div>
              <FieldError id={errorId} role={undefined} errors={errors.map((message) => ({ message }))} />
            </li>
          )
        })}
      </ol>
      <div>
        <Button type="button" variant="outline" size="sm" onClick={() => cells.append(blank(composer.nurses.find((nurse) => !nurse.me)?.hospital_id, values.at(-1)?.date))}>
          Add a cell
        </Button>
      </div>
      <Field>
        <FieldLabel htmlFor="swap-note">Note (optional)</FieldLabel>
        <Textarea id="swap-note" rows={2} maxLength={500} aria-describedby="swap-note-description" {...form.register("note")} />
        <FieldDescription id="swap-note-description">Everyone in the swap and your manager read it.</FieldDescription>
      </Field>
      <div ref={generalAlert} tabIndex={-1} className="outline-none">
        {(refused.general.length > 0 || refusal) && (
          <div role="alert" className="flex flex-col gap-1 text-sm text-destructive">
            {[...refused.general, ...(refusal ? [refusal] : [])].map((message) => <p key={message}>{message}</p>)}
          </div>
        )}
        {propose.isError && !(propose.error instanceof ApiError) && <p role="alert" className="text-sm text-destructive">{NO_CONNECTION}</p>}
      </div>
      <div><Button type="submit" disabled={propose.isPending}>Propose the swap</Button></div>
    </form>
  )
}

type Asking = { swap: MySwapRow; what: "reject" | "withdraw" }

function question(asking: Asking): Question {
  return asking.what === "reject"
    ? { title: `Reject ${asking.swap.proposer}'s swap on ${swapDays(asking.swap.cells)}?`, body: "It ends for everyone in it, who are told. Nothing on the roster changes.", confirm: "Reject the swap" }
    : { title: `Withdraw your swap on ${swapDays(asking.swap.cells)}?`, body: "It ends for everyone in it, who are told. Nothing on the roster changes.", confirm: "Withdraw the swap" }
}

function SwapItem({ swap, names, onAccept, onAsk, pending }: {
  swap: MySwapRow
  names: Record<string, string>
  onAccept: () => void
  onAsk: (what: Asking["what"]) => void
  pending: boolean
}) {
  const waiting = WAITING.includes(swap.state)
  const heading = swap.proposed_by_me ? "Your swap" : `${swap.proposer}'s swap`
  const answering = swap.state === "pending" && swap.my_answer === "pending"
  const which = `${swap.proposed_by_me ? "your" : `${swap.proposer}'s`} swap on ${swapDays(swap.cells)}`
  return (
    <li className="flex flex-col gap-2 border-b py-3 last:border-b-0">
      <h3 className="font-medium">{heading} on {swapDays(swap.cells)}: {SWAP_STATE[swap.state]}</h3>
      <p className="text-sm text-muted-foreground">
        Proposed {hospitalTime(swap.proposed_at)}.
        {swap.ended_by && swap.ended_at ? ` ${SWAP_STATE[swap.state]} by ${swap.ended_by}, ${hospitalTime(swap.ended_at)}.` : ""}
      </p>
      {swap.note && <p className="text-sm">{swap.proposer}: {swap.note}</p>}
      <SwapCells cells={swap.cells} names={names} label={`${heading} on ${swapDays(swap.cells)}, its cells`} />
      {swap.colleagues.length > 0 && (
        <p className="text-sm">
          {answersWords(swap.colleagues, swap.state)}
        </p>
      )}
      {swap.reason && <p className="text-sm">The manager's reason: {swap.reason}</p>}
      {(answering || (swap.proposed_by_me && waiting)) && (
        <div className="flex flex-wrap gap-2">
          {answering && <Button size="sm" disabled={pending} onClick={onAccept} aria-label={`Accept ${which}`}>Accept</Button>}
          {answering && (
            <Button id={`reject-${swap.id}`} variant="outline" size="sm" disabled={pending} onClick={() => onAsk("reject")}
              aria-label={`Reject ${which}`}>Reject</Button>
          )}
          {swap.proposed_by_me && waiting && (
            <Button id={`withdraw-${swap.id}`} variant="outline" size="sm" disabled={pending} onClick={() => onAsk("withdraw")}
              aria-label={`Withdraw ${which}`}>Withdraw</Button>
          )}
        </div>
      )}
    </li>
  )
}

function MySwaps({ names, status, tell }: { names: Record<string, string>; status: RefObject<string | null>; tell: (message: string) => void }) {
  const queryClient = useQueryClient()
  const swaps = useListMySwaps()
  const accept = useAcceptSwap()
  const reject = useRejectSwap()
  const withdraw = useWithdrawSwap()
  const [asking, setAsking] = useState<Asking | null>(null)
  const ending = asking?.what === "reject" ? reject : withdraw
  const refresh = () => queryClient.invalidateQueries({ queryKey: getListMySwapsQueryKey() })

  const acceptOne = (swap: MySwapRow) => accept.mutate({ swapId: swap.id }, {
    onSuccess: async (answer) => {
      await refresh()
      tell(answer.state === "awaiting_manager"
        ? `You accepted ${swap.proposer}'s swap. Everyone in it has now accepted, so it waits for the manager.`
        : `You accepted ${swap.proposer}'s swap. It waits for the others in it.`)
    },
    onError: (error) => tell(plainRefusal(error) ?? NO_CONNECTION),
  })

  return (
    <section aria-labelledby="my-swaps" className="flex flex-col gap-2">
      <h2 id="my-swaps" className="text-base font-medium">Your swaps</h2>
      {swaps.isPending && <p>Loading…</p>}
      {swaps.isError && <p role="alert">Your swaps could not be read. Reload the page to try again.</p>}
      {swaps.data && swaps.data.length === 0 && <p>You have proposed no swap, and nobody has proposed one with you.</p>}
      {swaps.data && swaps.data.length > 0 && (
        <ul>
          {swaps.data.map((swap) => (
            <SwapItem key={swap.id} swap={swap} names={names} pending={accept.isPending}
              onAccept={() => acceptOne(swap)} onAsk={(what) => { ending.reset(); setAsking({ swap, what }) }} />
          ))}
        </ul>
      )}
      <ConfirmDialog question={asking && question(asking)} pending={ending.isPending} failed={ending.isError}
        failure={plainRefusal(ending.error)} focusAfter={status} onCancel={() => setAsking(null)}
        onConfirm={() => asking && ending.mutate({ swapId: asking.swap.id }, {
          onSuccess: async () => {
            await refresh()
            const done = asking.what === "reject" ? `You rejected ${asking.swap.proposer}'s swap.` : "You withdrew your swap."
            setAsking(null)
            tell(`${done} Everyone in it is told.`)
          },
        })} />
    </section>
  )
}

function SwapsPage() {
  const { month } = Route.useSearch()
  const composer = useGetSwapComposer(month ? { month } : undefined, { query: { placeholderData: keepPreviousData } })
  const [said, setSaid] = useState("")
  const status = useRef<HTMLParagraphElement>(null)
  const focusAfter = useRef<string | null>(STATUS)
  const catalogue = useListShifts()
  const answer = composer.data
  // Every shift of the catalogue, so a swap of another month or one since retired still reads by name.
  const names = shiftNames(catalogue.data ?? [])
  const tell = (message: string) => {
    setSaid(message)
    status.current?.focus()
  }

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4">
      <h1 className="text-xl font-medium">Swaps</h1>
      <p className="text-muted-foreground">
        Propose to change cells of your unit's approved roster: yours, and any colleagues' that make it work. Each colleague whose cells change accepts or rejects it, then your manager decides. Approved, its cells are in the roster in force.
      </p>
      {/* Always rendered, because a live region mounted with its text may never be read out. */}
      <p id={STATUS} role="status" ref={status} tabIndex={-1} className="text-sm outline-none empty:sr-only">{said}</p>
      <Card>
        <CardHeader>
          <CardTitle><h2>Propose a swap</h2></CardTitle>
          <CardDescription>
            Choose each cell and what it becomes. The roster in force is on <Link to="/my-roster" search={{ month: answer?.month }} className="underline underline-offset-3">My roster</Link>.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {composer.isPending && <p>Loading…</p>}
          {composer.isError && <p role="alert">The roster could not be read. Reload the page to try again.</p>}
          {answer && (
            <>
              <MonthNav month={answer.month} link={(shown, label) => (
                <Link to="/swaps" search={{ month: shown }} className={MONTH_LINK}>{label}</Link>
              )} />
              {answer.refusal ? <p>{answer.refusal}</p> : (
                <Composer key={answer.month} composer={answer} onProposed={tell} />
              )}
            </>
          )}
        </CardContent>
      </Card>
      <MySwaps names={names} status={focusAfter} tell={tell} />
    </div>
  )
}
