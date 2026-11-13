import { keepPreviousData, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, Link, useRouteContext } from "@tanstack/react-router"
import { useRef, useState, type RefObject } from "react"
import { useForm } from "react-hook-form"
import { zodResolver } from "@hookform/resolvers/zod"
import { z } from "zod"

import { ApiError } from "@/api/fetcher"
import {
  getGetRosterMonthQueryKey, getGetSwapDecisionQueryKey, getListRosterVersionsQueryKey, getListUnitSwapsQueryKey,
  useApproveUnitSwap, useGetSwapDecision, useListShifts, useListUnitSwaps, useRejectUnitSwap,
} from "@/api/generated/endpoints"
import { Role, type DecisionCell, type SwapDecision, type UnitSwapRow } from "@/api/generated/model"
import { MONTH_LINK, monthInAddress, MonthNav } from "@/components/month-nav"
import { SwapCells } from "@/components/swap-cells"
import {
  AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Textarea } from "@/components/ui/textarea"
import { useChosenUnit } from "@/components/unit-picker"
import { NO_CONNECTION, shiftNames } from "@/lib/request-words"
import { plainRefusal, showServerErrors } from "@/lib/server-errors"
import { answersWords, cellWords, swapDays, SWAP_STATE, WAITING } from "@/lib/swap-words"
import { hospitalTime, monthName, scrollableRegion } from "@/lib/utils"
import { onlyFor } from "@/session"

const searchSchema = z.object({ month: monthInAddress, swap: z.string().optional().catch(undefined) })

/** The unit manager's swaps: each month's, and the decision on one its colleagues have all accepted. */
export const Route = createFileRoute("/_signed-in/unit-swaps")({
  validateSearch: (search) => searchSchema.parse(search),
  beforeLoad: onlyFor(Role.unit_manager),
  staticData: { title: "Swaps" },
  component: UnitSwapsPage,
})

const LINK = "underline underline-offset-3"
const STATUS = "unit-swaps-status"
const EXPIRED_WHY = { date_passed: "one of its dates passed", cell_changed: "a new version changed one of its cells" } as const

const days = (swap: UnitSwapRow) => swapDays(swap.cells)

const DECIDING = {
  approve: {
    title: "Approve", body: "Its cells become pinned cells in a new version of the roster, in force at once.", why: "Why (optional)",
    confirm: "Approve the swap", tone: "default",
  },
  reject: { title: "Reject", body: "It ends, and nothing on the roster changes.", why: "Why", confirm: "Reject the swap", tone: "destructive" },
} as const

const reasonSchema = (required: boolean) => z.object({
  reason: required ? z.string().trim().min(1, "Say why: everyone in the swap reads it.").max(2000, "At most 2000 characters.")
    : z.string().max(2000, "At most 2000 characters."),
})

function DecideDialog({ unitId, decision, what, focusAfter, onDone, onClose }: {
  unitId: string
  decision: SwapDecision
  what: "approve" | "reject"
  focusAfter: RefObject<string | null>
  onDone: (message: string) => void
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const approve = useApproveUnitSwap()
  const reject = useRejectUnitSwap()
  const mutation = what === "approve" ? approve : reject
  const words = DECIDING[what]
  const required = what === "reject"
  const form = useForm<{ reason: string }>({ resolver: zodResolver(reasonSchema(required)), defaultValues: { reason: "" } })
  const reasonError = form.formState.errors.reason
  const refusal = plainRefusal(mutation.error)
  const swap = decision.swap
  const whose = `${swap.proposer}'s swap on ${days(swap)}`

  const submit = form.handleSubmit(({ reason }) => mutation.mutate({ unitId, swapId: swap.id, data: { reason } }, {
    onSuccess: async (answer) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: getListUnitSwapsQueryKey(unitId) }),
        queryClient.invalidateQueries({ queryKey: getGetSwapDecisionQueryKey(unitId, swap.id) }),
        queryClient.invalidateQueries({ queryKey: getGetRosterMonthQueryKey(unitId) }),
        queryClient.invalidateQueries({ queryKey: getListRosterVersionsQueryKey(unitId) }),
      ])
      onDone(what === "approve"
        ? `You approved ${whose}. Version ${answer.swap.version} of the roster is now in force, its cells pinned. Everyone in it is told, and the unit's nurses are told the roster changed, not why.`
        : `You rejected ${whose}. Everyone in it is told, and reads your reason.`)
    },
    onError: (error) => showServerErrors(form, error),
  }))

  return (
    <AlertDialog open onOpenChange={(open) => { if (!open && !mutation.isPending) onClose() }}>
      <AlertDialogContent finalFocus={() => (focusAfter.current ? document.getElementById(focusAfter.current) : true)}>
        <form onSubmit={submit} noValidate className="flex flex-col gap-4">
          <AlertDialogHeader>
            <AlertDialogTitle>{words.title} {whose}?</AlertDialogTitle>
            <AlertDialogDescription>{words.body}</AlertDialogDescription>
          </AlertDialogHeader>
          <Field data-invalid={!!reasonError || undefined}>
            <FieldLabel htmlFor="swap-reason">{words.why}</FieldLabel>
            <Textarea id="swap-reason" rows={2} aria-invalid={!!reasonError || undefined}
              aria-describedby={["swap-reason-description", reasonError && "swap-reason-error"].filter(Boolean).join(" ")}
              {...form.register("reason")} />
            <FieldDescription id="swap-reason-description">
              The proposer and every colleague in the swap read it. It is never in a notification or the audit log.
            </FieldDescription>
            <FieldError id="swap-reason-error" role={undefined} errors={reasonError ? [reasonError] : undefined} />
          </Field>
          {refusal && <p role="alert" className="text-sm text-destructive">{refusal}</p>}
          {mutation.isError && !(mutation.error instanceof ApiError) && <p role="alert" className="text-sm text-destructive">{NO_CONNECTION}</p>}
          <AlertDialogFooter>
            <AlertDialogCancel type="button">Back</AlertDialogCancel>
            <Button type="submit" variant={words.tone} disabled={mutation.isPending}>{words.confirm}</Button>
          </AlertDialogFooter>
        </form>
      </AlertDialogContent>
    </AlertDialog>
  )
}

function Check({ decision }: { decision: SwapDecision }) {
  const check = decision.check
  if (!check) return null
  if (check.reason) return <p>It cannot be checked: {check.reason}</p>
  const cost = decision.cost_after !== null && decision.cost_before !== null && decision.delta !== null
    ? ` It costs ${decision.cost_after}, against ${decision.cost_before} for the roster in force (${decision.delta > 0 ? "+" : ""}${decision.delta}).`
    : ""
  return (
    <div className="flex flex-col gap-1">
      <p>{check.ok ? "With its cells the roster breaks no hard rule." : "With its cells the roster breaks these hard rules:"}{cost}</p>
      {!check.ok && (
        <ul className="list-disc ps-5 text-sm">
          {check.violations.map((violation, index) => <li key={index}>{violation.full_name ? `${violation.full_name}: ` : ""}{violation.text}</li>)}
        </ul>
      )}
    </div>
  )
}

function Deciding({ unitId, swapId, names, onDone }: { unitId: string; swapId: string; names: Record<string, string>; onDone: (message: string) => void }) {
  const decision = useGetSwapDecision(unitId, swapId)
  const [what, setWhat] = useState<"approve" | "reject" | null>(null)
  const focusAfter = useRef<string | null>(null)
  const answer = decision.data
  const begin = (chosen: "approve" | "reject") => {
    focusAfter.current = `swap-${chosen}`
    setWhat(chosen)
  }

  return (
    <section aria-labelledby="swap-decision" className="flex flex-col gap-3">
      <h2 id="swap-decision" className="text-base font-medium">
        {answer ? `${answer.swap.proposer}'s swap: ${SWAP_STATE[answer.swap.state]}` : "The swap"}
      </h2>
      {decision.isPending && <p>Loading…</p>}
      {decision.isError && <p role="alert">This swap could not be read. Reload the page to try again.</p>}
      {answer && (
        <>
          <p className="text-sm text-muted-foreground">
            Proposed {hospitalTime(answer.swap.proposed_at)}.
            {answer.swap.ended_by && answer.swap.ended_at ? ` ${SWAP_STATE[answer.swap.state]} by ${answer.swap.ended_by}, ${hospitalTime(answer.swap.ended_at)}.` : ""}
            {answer.swap.expired_why ? ` It expired because ${EXPIRED_WHY[answer.swap.expired_why]}.` : ""}
            {answer.swap.version ? ` It made version ${answer.swap.version}.` : ""}
          </p>
          {answer.swap.note && <p className="text-sm">{answer.swap.proposer}: {answer.swap.note}</p>}
          <SwapCells cells={answer.cells} names={names} label={`${answer.swap.proposer}'s swap, before and after`} extra={[
            { heading: "In force now", cell: (row) => cellWords(row.now, names) },
            ...(WAITING.includes(answer.swap.state)
              ? [{ heading: "Your pin", cell: (row: DecisionCell) => (row.replaces_pin === null ? "" : `${cellWords(row.replaces_pin, names)}, which approving replaces`) }]
              : []),
          ]} />
          {answer.swap.colleagues.length > 0 && (
            <p className="text-sm">{answersWords(answer.swap.colleagues, answer.swap.state)}</p>
          )}
          {answer.swap.reason && <p className="text-sm">Your reason: {answer.swap.reason}</p>}
          <Check decision={answer} />
          {answer.problems.length > 0 && (
            <div className="flex flex-col gap-1">
              <p>It cannot be approved as the month stands now:</p>
              <ul className="list-disc ps-5 text-sm">{answer.problems.map((problem) => <li key={problem}>{problem}</li>)}</ul>
            </div>
          )}
          {answer.swap.state === "awaiting_manager" && (
            <div className="flex flex-wrap gap-2">
              {!answer.approve_refusal && <Button id="swap-approve" onClick={() => begin("approve")}>Approve</Button>}
              <Button id="swap-reject" variant="outline" onClick={() => begin("reject")}>Reject</Button>
            </div>
          )}
          {answer.swap.state === "pending" && <p>It waits for its colleagues' answers before you decide it.</p>}
          {what && (
            <DecideDialog key={what} unitId={unitId} decision={answer} what={what} focusAfter={focusAfter}
              onClose={() => setWhat(null)}
              onDone={(message) => {
                focusAfter.current = STATUS
                setWhat(null)
                onDone(message)
              }} />
          )}
        </>
      )}
    </section>
  )
}

function UnitSwaps({ unitId, month, swapId }: { unitId: string; month: string | undefined; swapId: string | undefined }) {
  const swaps = useListUnitSwaps(unitId, month ? { month } : undefined, { query: { placeholderData: keepPreviousData } })
  const catalogue = useListShifts()
  const names = shiftNames(catalogue.data ?? [])
  const [said, setSaid] = useState("")
  const status = useRef<HTMLParagraphElement>(null)
  const rows = swaps.data?.swaps
  const shown = swaps.data?.month
  const chosen = rows?.find((swap) => swap.id === swapId) ?? rows?.find((swap) => swap.state === "awaiting_manager")
  const tell = (message: string) => {
    setSaid(message)
    status.current?.focus()
  }

  return (
    <>
      <p id={STATUS} role="status" ref={status} tabIndex={-1} className="text-sm outline-none empty:sr-only">{said}</p>
      {swaps.isPending && <p>Loading…</p>}
      {swaps.isError && <p role="alert">The swaps could not be read. Reload the page to try again.</p>}
      {rows && (
        <>
          {shown && <MonthNav month={shown} link={(other, label) => <Link to="/unit-swaps" search={{ month: other }} className={MONTH_LINK}>{label}</Link>} />}
          {rows.length === 0 ? <p>No swap has been proposed for {shown ? monthName(shown) : "this month"}.</p> : (
            <Table container={scrollableRegion("The unit's swaps, newest first")}>
              <TableHeader>
                <TableRow>
                  <TableHead>Proposed by</TableHead>
                  <TableHead>Dates</TableHead>
                  <TableHead>State</TableHead>
                  <TableHead><span className="sr-only">Actions</span></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((swap) => (
                  <TableRow key={swap.id} aria-current={swap.id === chosen?.id || undefined}>
                    <TableCell>{swap.proposer}</TableCell>
                    <TableCell className="tabular-nums">{days(swap)}</TableCell>
                    <TableCell>{SWAP_STATE[swap.state]}</TableCell>
                    <TableCell>
                      {swap.id !== chosen?.id && (
                        <Link to="/unit-swaps" search={{ month: swap.month, swap: swap.id }} className={LINK}
                          aria-label={`Show ${swap.proposer}'s swap on ${days(swap)}`}>Show</Link>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          {chosen && <Deciding key={chosen.id} unitId={unitId} swapId={chosen.id} names={names} onDone={tell} />}
        </>
      )}
    </>
  )
}

function UnitSwapsPage() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  const { month, swap } = Route.useSearch()
  const { units, chosen } = useChosenUnit(user, undefined)
  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4">
      <h1 className="text-xl font-medium">{chosen ? `Swaps in ${chosen.name}` : "Swaps"}</h1>
      <p className="text-muted-foreground">
        A nurse proposes a swap of cells on the roster in force, and every colleague whose cells change accepts it. Then you see it before and after, checked on the month as it stands, and approve or reject it. To change a swap, reject it and edit the roster yourself.
      </p>
      {units.isPending && <p>Loading…</p>}
      {units.isError && <p role="alert">Your unit could not be read. Reload the page to try again.</p>}
      {units.data && !chosen && <p>No unit is assigned to you yet. The CNO assigns each unit its manager.</p>}
      {chosen && <UnitSwaps key={chosen.id} unitId={chosen.id} month={month} swapId={swap} />}
    </div>
  )
}
