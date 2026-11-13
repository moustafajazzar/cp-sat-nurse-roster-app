import { useBlocker, useMatch } from "@tanstack/react-router"
import { useEffect, useRef, useState } from "react"

import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { useAbandonAwaitedPassword } from "@/lib/use-awaiting-password"

/** The password from a creation or a reset. It lives in this state and nowhere else, until Done. */
export type Shown = { username: string; full_name: string; password: string }

export function LeavePageDialog({ open, title, description, action, onStay, onLeave }: {
  open: boolean
  title: string
  description: string
  action: string
  onStay: () => void
  onLeave: () => void
}) {
  return (
    <AlertDialog open={open} onOpenChange={(next) => { if (!next) onStay() }}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{description}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <Button variant="outline" onClick={onStay}>Stay</Button>
          <Button variant="destructive" onClick={onLeave}>{action}</Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

/** Asks before leaving the page while `holding`, and the browser asks before a reload or closing the tab, which is as
 *  far as a browser lets a page go. A way out rather than a wall, because a request can hang. Moving within the page,
 *  such as narrowing its list, unmounts nothing, so it is not asked about. */
export function LeavePageGuard({ holding, title = "Leave without the password?", description = "The change is still being saved, and the new password is shown only on this page. If you leave, nobody can see it again, and they will need a password reset." }: {
  holding: boolean
  title?: string
  description?: string
}) {
  const page = useMatch({ strict: false, select: (match) => match.routeId })
  const blocker = useBlocker({
    // Only from this page. Once Leave has moved the address on, this page stays mounted a little longer with `holding`
    // as it was, and the next link followed must not be caught, with no question left to answer.
    shouldBlockFn: ({ current, next }) => holding && current.routeId === page && next.routeId !== page,
    enableBeforeUnload: () => holding,
    withResolver: true,
    disabled: !holding,
  })
  const abandon = useAbandonAwaitedPassword()
  const asking = blocker.status === "blocked"
  const stay = blocker.reset

  // The answer came while asking, so the question is over, and whatever it brought is on this page.
  useEffect(() => { if (asking && !holding) stay?.() }, [asking, holding, stay])

  return (
    <LeavePageDialog open={asking} title={title} description={description} action="Leave" onStay={() => stay?.()}
      onLeave={() => { blocker.proceed?.(); abandon() }} />
  )
}

export function PasswordDialog({ shown, onDone }: { shown: Shown | null; onDone: () => void }) {
  const [copied, setCopied] = useState("")
  const passwordBox = useRef<HTMLInputElement>(null)
  // Going back or reloading would lose a password nobody can see again.
  useBlocker({ shouldBlockFn: () => shown !== null, enableBeforeUnload: () => shown !== null, disabled: shown === null })

  async function copy() {
    try {
      await navigator.clipboard.writeText(shown?.password ?? "")
      setCopied("Copied.")
    } catch {
      setCopied("It could not be copied. It is selected in the box: copy it by hand.")
      passwordBox.current?.select()
    }
  }

  // Only Done closes this: nobody can see the password again, so Escape must not lose it either.
  return (
    <AlertDialog open={shown !== null}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Password for {shown?.username}</AlertDialogTitle>
          <AlertDialogDescription>
            This is the only time it is shown. Give it to {shown?.full_name}, who must choose a new one at their next login.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <Input ref={passwordBox} readOnly translate="no" aria-label="Generated password" value={shown?.password ?? ""}
          autoComplete="off" spellCheck={false} onFocus={(event) => event.target.select()}
          className="text-center font-mono text-base md:text-base" />
        <p role="status" className="text-sm text-muted-foreground empty:sr-only">{copied}</p>
        <AlertDialogFooter>
          <Button variant="outline" onClick={copy}>Copy password</Button>
          <Button onClick={() => { setCopied(""); onDone() }}>Done</Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
