import { keepPreviousData, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, Link } from "@tanstack/react-router"
import { useRef, useState } from "react"
import { z } from "zod"

import { getListMyCoversQueryKey, useAcceptCover, useDeclineCover, useListMyCovers, useListShifts } from "@/api/generated/endpoints"
import { Role, type MyCoverRow, type ShiftSchema } from "@/api/generated/model"
import { ConfirmDialog } from "@/components/confirm-dialog"
import { MONTH_LINK, monthInAddress, MonthNav } from "@/components/month-nav"
import { Button } from "@/components/ui/button"
import { coverName, myCoverWords, waitsForMe } from "@/lib/cover-words"
import { NO_CONNECTION, shiftNames } from "@/lib/request-words"
import { plainRefusal } from "@/lib/server-errors"
import { monthName } from "@/lib/utils"
import { onlyFor } from "@/session"

const searchSchema = z.object({ month: monthInAddress })

/** A staff nurse's covers: shifts her manager offered her, to accept or decline, and those she answered or that ended. */
export const Route = createFileRoute("/_signed-in/covers")({
  validateSearch: (search) => searchSchema.parse(search),
  beforeLoad: onlyFor(Role.staff_nurse),
  staticData: { title: "Covers" },
  component: CoversPage,
})

const STATUS = "covers-status"
const REFUSAL = "covers-refusal"

/** "07:00 to 19:00, 12 hours": when the shift is, from the catalogue, and how long. */
function hoursOf(cover: MyCoverRow, shifts: ShiftSchema[]): string {
  const shift = shifts.find((row) => row.code === cover.shift)
  const hours = `${cover.hours} ${cover.hours === 1 ? "hour" : "hours"}`
  return shift ? `${shift.start_time.slice(0, 5)} to ${shift.end_time.slice(0, 5)}, ${hours}` : hours
}

function CoverItem({ cover, names, shifts, pending, onAccept, onDecline }: {
  cover: MyCoverRow
  names: Record<string, string>
  shifts: ShiftSchema[]
  pending: boolean
  onAccept: () => void
  onDecline: () => void
}) {
  const name = coverName(cover, names)
  return (
    <li className="flex flex-col gap-2 border-b py-3 last:border-b-0">
      <h3 className="font-medium">{name}: {myCoverWords(cover)}</h3>
      <p className="text-sm text-muted-foreground">{hoursOf(cover, shifts)}, in {cover.unit}.</p>
      {cover.note && <p className="text-sm">Your manager's note: {cover.note}</p>}
      {waitsForMe(cover) && (
        <div className="flex flex-wrap gap-2">
          <Button disabled={pending} onClick={onAccept} aria-label={`Accept the cover for ${name}`}>Accept</Button>
          <Button id={`decline-${cover.id}`} variant="outline" disabled={pending} onClick={onDecline}
            aria-label={`Decline the cover for ${name}`}>Decline</Button>
        </div>
      )}
    </li>
  )
}

function CoversPage() {
  const { month } = Route.useSearch()
  const queryClient = useQueryClient()
  const covers = useListMyCovers(month ? { month } : undefined, { query: { placeholderData: keepPreviousData } })
  const catalogue = useListShifts()
  const accept = useAcceptCover()
  const decline = useDeclineCover()
  const [said, setSaid] = useState("")
  const [refused, setRefused] = useState("")
  const [asking, setAsking] = useState<MyCoverRow | null>(null)
  const status = useRef<HTMLParagraphElement>(null)
  const alert = useRef<HTMLDivElement>(null)
  const focusAfter = useRef<string | null>(STATUS)
  const shifts = catalogue.data ?? []
  // Every shift of the catalogue, so a cover of a shift since retired still reads by name.
  const names = shiftNames(shifts)
  const shown = covers.data?.month
  const refresh = () => queryClient.invalidateQueries({ queryKey: getListMyCoversQueryKey() })
  const tell = (message: string) => {
    setRefused("")
    setSaid(message)
    status.current?.focus()
  }

  const acceptOne = (cover: MyCoverRow) => accept.mutate({ coverId: cover.id }, {
    onSuccess: async () => {
      await refresh()
      tell(`You took the cover for ${coverName(cover, names)}. It is in your cell on My roster now.`)
    },
    onError: async (error) => {
      await refresh()
      setSaid("")
      setRefused(plainRefusal(error) ?? NO_CONNECTION)
      alert.current?.focus()
    },
  })

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4">
      <h1 className="text-xl font-medium">Covers</h1>
      <p className="text-muted-foreground">
        When your unit needs one more nurse on a shift you could work, your manager may offer it to you. The first nurse to accept takes it, and it is on <Link to="/my-roster" search={{ month: shown }} className="underline underline-offset-3">My roster</Link> at once.
      </p>
      {/* Always rendered, because a live region mounted with its text may never be read out. */}
      <p id={STATUS} role="status" ref={status} tabIndex={-1} className="text-sm outline-none empty:sr-only">{said}</p>
      <div id={REFUSAL} role="alert" ref={alert} tabIndex={-1} className="text-sm text-destructive outline-none empty:sr-only">{refused}</div>
      {covers.isPending && <p>Loading…</p>}
      {covers.isError && <p role="alert">Your covers could not be read. Reload the page to try again.</p>}
      {covers.data && shown && (
        <>
          <MonthNav month={shown} link={(other, label) => <Link to="/covers" search={{ month: other }} className={MONTH_LINK}>{label}</Link>} />
          {covers.data.covers.length === 0 ? (
            <p>No cover waits for your answer, and none was offered to you for {monthName(shown)}.</p>
          ) : (
            <ul aria-label="Covers offered to you: those waiting for your answer first">
              {covers.data.covers.map((cover) => (
                <CoverItem key={cover.id} cover={cover} names={names} shifts={shifts} pending={accept.isPending}
                  onAccept={() => acceptOne(cover)}
                  onDecline={() => { decline.reset(); focusAfter.current = `decline-${cover.id}`; setAsking(cover) }} />
              ))}
            </ul>
          )}
        </>
      )}
      <ConfirmDialog
        question={asking && { title: `Decline the cover for ${coverName(asking, names)}?`, body: "Your manager is told, and you are not asked again for this cover.", confirm: "Decline the cover" }}
        pending={decline.isPending} failed={decline.isError} failure={plainRefusal(decline.error)} focusAfter={focusAfter}
        onCancel={() => setAsking(null)}
        onConfirm={() => asking && decline.mutate({ coverId: asking.id }, {
          onSuccess: async () => {
            await refresh()
            focusAfter.current = STATUS
            const name = coverName(asking, names)
            setAsking(null)
            tell(`You declined the cover for ${name}. Your manager is told.`)
          },
        })} />
    </div>
  )
}
