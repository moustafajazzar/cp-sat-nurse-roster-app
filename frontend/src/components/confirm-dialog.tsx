import type { RefObject } from "react"

import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"

export type Question = { title: string; body: string; confirm: string }

/** Asks before something that would have to be entered again by hand. `focusAfter` names what takes focus once it is
 *  done, because the button that opened this is gone by then. Cancelling returns to that button. */
export function ConfirmDialog({ question, pending, failed, failure, focusAfter, onConfirm, onCancel, tone = "destructive" }: {
  question: Question | null
  /** `default` for a step forward that cannot be taken back, rather than something lost. */
  tone?: "destructive" | "default"
  pending: boolean
  failed: boolean
  /** What to say when it failed, if the answer said more than a lost connection would. */
  failure?: string
  focusAfter: RefObject<string | null>
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <AlertDialog open={question !== null} onOpenChange={(open) => { if (!open && !pending) onCancel() }}>
      <AlertDialogContent finalFocus={() => (focusAfter.current ? document.getElementById(focusAfter.current) : true)}>
        <AlertDialogHeader>
          <AlertDialogTitle>{question?.title}</AlertDialogTitle>
          <AlertDialogDescription>{question?.body}</AlertDialogDescription>
        </AlertDialogHeader>
        {failed && <p role="alert" className="text-sm text-destructive">{failure ?? "That did not work. Check your connection and try again."}</p>}
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <Button variant={tone} disabled={pending} onClick={onConfirm}>{question?.confirm}</Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
