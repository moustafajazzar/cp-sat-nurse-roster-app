import { useQueryClient } from "@tanstack/react-query"
import { useRef, useState, type RefObject } from "react"

import {
  getListNurseAttributesQueryKey,
  useEndProbation,
  useExtendProbation,
  useListPreceptorChoices,
  useSetPreceptor,
  useSetProbationLength,
} from "@/api/generated/endpoints"
import type { NurseAttributesRow, ProbationOnTheRow } from "@/api/generated/model"
import { ConfirmDialog } from "@/components/confirm-dialog"
import { CHECKBOX } from "@/components/form-field"
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import { serverErrors } from "@/lib/server-errors"
import { monthName, shortDay } from "@/lib/utils"

/** A nurse on probation's row: its length and last day, her preceptor, and her unit manager's four
 *  actions, each behind its question. The last day each action would give, and the months a preceptor may be chosen from, come
 *  from the server, on the hospital's clock. */

export type ProbationAction = "length" | "extend" | "end" | "preceptor"

const months = (count: number) => (count === 1 ? "1 month" : `${count} months`)
const actionId = (action: ProbationAction, nurse: NurseAttributesRow) => `probation-${action}-${nurse.id}`
const COULD_NOT_SAVE = "That did not work. Check your connection and try again."

export function ProbationCell({ nurse, mayChange, onAsk }: {
  nurse: NurseAttributesRow
  mayChange: boolean
  onAsk: (action: ProbationAction, nurse: NurseAttributesRow) => void
}) {
  const probation = nurse.probation
  if (!probation) return <span className="text-muted-foreground">No</span>
  // Each accessible name starts with the button's own word, so a voice command that says it finds it.
  const actions: [ProbationAction, string, string, boolean][] = [
    ["length", "Length", `Length of ${nurse.full_name}'s probation`, probation.lengths.length > 0],
    ["extend", "Extend", `Extend the probation of ${nurse.full_name}`, probation.if_extended !== null],
    ["end", "End", `End the probation of ${nurse.full_name}`, probation.if_ended !== null],
  ]
  const shown = mayChange && !nurse.joins_from ? actions.filter(([, , , available]) => available) : []
  // The new unit's manager chooses her preceptor there before she joins, so this one is not held back by it.
  if (mayChange && probation.preceptor_months.length > 0) {
    const word = probation.preceptors.length > 0 ? "Change preceptor" : "Choose preceptor"
    shown.push(["preceptor", word, `${word} for ${nurse.full_name}`, true])
  }
  return (
    <div className="flex flex-col items-start gap-1">
      <span className="whitespace-nowrap">Until {shortDay(probation.last_day)}</span>
      <span className="text-muted-foreground">{months(probation.months)}</span>
      {probation.ended_early && <Badge variant="outline">Ended early</Badge>}
      {!probation.ticked && <span className="text-muted-foreground">Taken off by the CNO, after the months already approved</span>}
      <Preceptors probation={probation} />
      {shown.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {shown.map(([action, label, name]) => (
            <Button key={action} id={actionId(action, nurse)} variant="outline" size="sm"
              aria-label={name} onClick={() => onAsk(action, nurse)}>
              {label}
            </Button>
          ))}
        </div>
      )}
    </div>
  )
}

const span = (first: string, last: string) => (first === last ? monthName(first) : `${monthName(first)} to ${monthName(last)}`)

/** Who precepts her from this month on, each over the months they do. */
function Preceptors({ probation }: { probation: ProbationOnTheRow }) {
  if (probation.preceptors.length === 0) return <span>No preceptor</span>
  return (
    <ul className="flex flex-col">
      {probation.preceptors.map((preceptor) => (
        <li key={preceptor.hospital_id + preceptor.from_month}>
          Preceptor: {preceptor.full_name}, <span className="whitespace-nowrap">{span(preceptor.from_month, preceptor.to_month)}</span>
        </li>
      ))}
    </ul>
  )
}

type Asking = { action: ProbationAction; nurse: NurseAttributesRow } | null

function failureOf(error: unknown): string | undefined {
  return serverErrors(error)[0]?.message
}

/** The questions behind the three actions. After an answer, focus goes to the nurse's row button `afterId` names, since the
 *  action's own button may be gone. */
export function useProbationQuestions(unitId: string, afterId: (nurse: NurseAttributesRow) => string, onDone: (message: string) => void) {
  const [asking, setAsking] = useState<Asking>(null)
  const focusAfter = useRef<string | null>(null)
  return {
    ask: (action: ProbationAction, nurse: NurseAttributesRow) => { focusAfter.current = null; setAsking({ action, nurse }) },
    dialogs: (
      <ProbationQuestions unitId={unitId} asking={asking} focusAfter={focusAfter}
        onClose={() => setAsking(null)}
        onDone={(nurse, message) => { focusAfter.current = afterId(nurse); setAsking(null); onDone(message) }} />
    ),
  }
}

function ProbationQuestions({ unitId, asking, focusAfter, onClose, onDone }: {
  unitId: string
  asking: Asking
  focusAfter: RefObject<string | null>
  onClose: () => void
  onDone: (nurse: NurseAttributesRow, message: string) => void
}) {
  const queryClient = useQueryClient()
  const extend = useExtendProbation()
  const end = useEndProbation()
  const nurse = asking?.nurse
  const probation = nurse?.probation as ProbationOnTheRow | undefined
  const succeeded = (row: NurseAttributesRow, said: string) => {
    void queryClient.invalidateQueries({ queryKey: getListNurseAttributesQueryKey(unitId) })
    onDone(row, said)
  }
  const ends = (row: NurseAttributesRow) => (row.probation ? shortDay(row.probation.last_day) : "")

  return (
    <>
      <ConfirmDialog tone="default"
        question={asking?.action === "extend" && nurse && probation?.if_extended ? {
          title: `Extend the probation of ${nurse.full_name} by a month?`,
          body: `It then ends on ${shortDay(probation.if_extended)} instead of ${shortDay(probation.last_day)}, ${months(probation.months + 1)} in all. It can be extended a month at a time, to 6 months.`,
          confirm: "Extend by a month",
        } : null}
        pending={extend.isPending} failed={extend.isError} failure={failureOf(extend.error)} focusAfter={focusAfter}
        onCancel={() => { extend.reset(); onClose() }}
        onConfirm={() => nurse && extend.mutate({ unitId, nurseId: nurse.id }, {
          onSuccess: (row) => { extend.reset(); succeeded(row, `The probation of ${row.full_name} now ends on ${ends(row)}.`) },
        })} />
      <ConfirmDialog
        question={asking?.action === "end" && nurse && probation?.if_ended ? {
          title: `End the probation of ${nurse.full_name}?`,
          body: `Her last day of probation becomes ${shortDay(probation.if_ended)}, the last day of this month, so this month keeps its roster. An ended probation cannot be extended.`,
          confirm: "End probation",
        } : null}
        pending={end.isPending} failed={end.isError} failure={failureOf(end.error)} focusAfter={focusAfter}
        onCancel={() => { end.reset(); onClose() }}
        onConfirm={() => nurse && end.mutate({ unitId, nurseId: nurse.id }, {
          onSuccess: (row) => { end.reset(); succeeded(row, `The probation of ${row.full_name} ends on ${ends(row)}.`) },
        })} />
      {asking?.action === "preceptor" && nurse && probation && (
        <PreceptorQuestion key={nurse.id} unitId={unitId} nurse={nurse} probation={probation} focusAfter={focusAfter}
          onCancel={onClose} onDone={(row, said) => succeeded(row, said)} />
      )}
      {asking?.action === "length" && nurse && probation && (
        <LengthQuestion key={nurse.id} unitId={unitId} nurse={nurse} probation={probation} focusAfter={focusAfter}
          onCancel={onClose} onDone={(row) => succeeded(row, `The probation of ${row.full_name} is ${months(row.probation?.months ?? 0)}, until ${ends(row)}.`)} />
      )}
    </>
  )
}

function LengthQuestion({ unitId, nurse, probation, focusAfter, onCancel, onDone }: {
  unitId: string
  nurse: NurseAttributesRow
  probation: ProbationOnTheRow
  focusAfter: RefObject<string | null>
  onCancel: () => void
  onDone: (row: NurseAttributesRow) => void
}) {
  const set = useSetProbationLength()
  const [chosen, setChosen] = useState(probation.months)
  const name = `length-${nurse.id}`
  const failure = set.isError ? (failureOf(set.error) ?? COULD_NOT_SAVE) : null

  return (
    <AlertDialog open onOpenChange={(open) => { if (!open && !set.isPending) onCancel() }}>
      <AlertDialogContent finalFocus={() => (focusAfter.current ? document.getElementById(focusAfter.current) : true)}>
        <AlertDialogHeader>
          <AlertDialogTitle>How long is the probation of {nurse.full_name}?</AlertDialogTitle>
          <AlertDialogDescription>
            Whole calendar months from her start date. A month she joins after the 1st does not count. Beyond 3 months, extend it a
            month at a time.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <fieldset className="flex flex-col gap-2">
          <legend className="sr-only">Length</legend>
          {probation.lengths.map((choice) => (
            <label key={choice.months} className="flex items-center gap-2 text-sm">
              <input type="radio" name={name} value={choice.months} className={CHECKBOX} checked={chosen === choice.months}
                onChange={() => setChosen(choice.months)} />
              {months(choice.months)}, until {shortDay(choice.last_day)}
            </label>
          ))}
        </fieldset>
        {failure && <p role="alert" className="text-sm text-destructive">{failure}</p>}
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <Button disabled={set.isPending} onClick={() => set.mutate({ unitId, nurseId: nurse.id, data: { months: chosen } }, { onSuccess: onDone })}>
            Set length
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

/** Choose or change her preceptor from a month on: the month first, then the nurses the server offers from it, as only it knows
 *  who holds a marked skill unexpired today and who precepts whom. */
function PreceptorQuestion({ unitId, nurse, probation, focusAfter, onCancel, onDone }: {
  unitId: string
  nurse: NurseAttributesRow
  probation: ProbationOnTheRow
  focusAfter: RefObject<string | null>
  onCancel: () => void
  onDone: (row: NurseAttributesRow, said: string) => void
}) {
  const set = useSetPreceptor()
  const [month, setMonth] = useState(probation.preceptor_months[0])
  const [chosen, setChosen] = useState("")
  const choices = useListPreceptorChoices(unitId, nurse.id, { month })
  const offered = choices.data?.nurses ?? []
  const picked = offered.some((candidate) => candidate.id === chosen) ? chosen : ""
  const failure = set.isError ? (failureOf(set.error) ?? COULD_NOT_SAVE) : null
  const changing = probation.preceptors.length > 0
  const title = changing ? `Change the preceptor of ${nurse.full_name}?` : `Choose a preceptor for ${nurse.full_name}?`
  const save = () => set.mutate({ unitId, nurseId: nurse.id, data: { preceptor_id: picked, from_month: month } }, {
    onSuccess: (row) => {
      const who = offered.find((candidate) => candidate.id === picked)?.full_name ?? "The nurse you chose"
      onDone(row, `From ${monthName(month)}, ${who} precepts ${row.full_name}.`)
    },
  })

  return (
    <AlertDialog open onOpenChange={(open) => { if (!open && !set.isPending) onCancel() }}>
      <AlertDialogContent finalFocus={() => (focusAfter.current ? document.getElementById(focusAfter.current) : true)}>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>
            From the month you choose until her probation ends, she works beside her preceptor on their day shifts. The list offers
            this unit&apos;s nurses who hold a skill marked &ldquo;Lets a nurse precept&rdquo;, unexpired today, are not on probation
            and precept no other nurse in those months. Both nurses are told{changing ? ", and so is the nurse who precepted her" : ""}.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <div className="flex flex-col gap-1">
          <label htmlFor={`preceptor-month-${nurse.id}`} className="text-sm font-medium">From</label>
          <NativeSelect id={`preceptor-month-${nurse.id}`} value={month} onChange={(event) => setMonth(event.target.value)}>
            {probation.preceptor_months.map((each) => <NativeSelectOption key={each} value={each}>{monthName(each)}</NativeSelectOption>)}
          </NativeSelect>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor={`preceptor-${nurse.id}`} className="text-sm font-medium">Preceptor</label>
          <NativeSelect id={`preceptor-${nurse.id}`} value={picked} disabled={offered.length === 0}
            aria-describedby={`preceptor-said-${nurse.id}`} onChange={(event) => setChosen(event.target.value)}>
            <NativeSelectOption value="">{choices.isPending ? "Loading…" : "Choose a nurse"}</NativeSelectOption>
            {offered.map((candidate) => <NativeSelectOption key={candidate.id} value={candidate.id}>{candidate.full_name}</NativeSelectOption>)}
          </NativeSelect>
          <p id={`preceptor-said-${nurse.id}`} className="text-sm text-muted-foreground empty:hidden">
            {choices.isError ? "The nurses who could precept her could not be read. Check your connection and try again." : choices.data?.refusal ?? ""}
          </p>
        </div>
        {failure && <p role="alert" className="text-sm text-destructive">{failure}</p>}
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <Button disabled={set.isPending || !picked} onClick={save}>{changing ? "Change preceptor" : "Choose preceptor"}</Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
