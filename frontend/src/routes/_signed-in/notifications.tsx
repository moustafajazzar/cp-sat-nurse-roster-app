import { useQueryClient } from "@tanstack/react-query"
import { createFileRoute, Link } from "@tanstack/react-router"
import { useRef, type RefObject } from "react"

import {
  getCountUnreadNotificationsQueryKey, getListNotificationsQueryKey, useListNotifications, useMarkAllNotificationsRead,
  useMarkNotificationRead,
} from "@/api/generated/endpoints"
import type { NotificationLink, NotificationRow } from "@/api/generated/model"
import { PushOnThisDevice } from "@/components/push-on-this-device"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { cn, hospitalTime } from "@/lib/utils"

/** Every role's: each user reads only their own. */
export const Route = createFileRoute("/_signed-in/notifications")({
  staticData: { title: "Notifications" },
  component: NotificationsPage,
})

/** A notification's way to its page: on a touch screen as tall as a thumb. */
const LINK = "underline underline-offset-3 pointer-coarse:inline-flex pointer-coarse:min-h-11 pointer-coarse:items-center"

function Open({ link, onOpen }: { link: NotificationLink; onOpen: () => void }) {
  switch (link.page) {
    case "roster":
      return <Link to="/roster" search={{ month: link.month }} onClick={onOpen} className={LINK}>Open the roster</Link>
    case "my_roster":
      return <Link to="/my-roster" search={{ month: link.month }} onClick={onOpen} className={LINK}>Open My roster</Link>
    case "unit_roster":
      return <Link to="/unit-roster" search={{ unit: link.unit_id ?? undefined, month: link.month }} onClick={onOpen} className={LINK}>Open the roster</Link>
    case "my_month":
      return <Link to="/my-month" search={{ month: link.month }} onClick={onOpen} className={LINK}>Open My month</Link>
    case "requests":
      return <Link to="/requests" search={{ month: link.month }} onClick={onOpen} className={LINK}>Open the requests</Link>
    case "swaps":
      return <Link to="/swaps" search={{ month: link.month }} onClick={onOpen} className={LINK}>Open Swaps</Link>
    case "unit_swaps":
      return <Link to="/unit-swaps" search={{ month: link.month }} onClick={onOpen} className={LINK}>Open the swaps</Link>
    case "covers":
      return <Link to="/covers" search={{ month: link.month }} onClick={onOpen} className={LINK}>Open Covers</Link>
    case "unit_covers":
      return <Link to="/unit-covers" search={{ month: link.month, cover: link.cover_id ?? undefined }} onClick={onOpen} className={LINK}>Open the cover</Link>
    case "rostering_attributes":
      return <Link to="/rostering-attributes" onClick={onOpen} className={LINK}>Open Rostering attributes</Link>
  }
}

/** `told` is the status line, which takes focus from a button that goes once it has done its work. */
function useRefreshed(told?: RefObject<HTMLParagraphElement | null>) {
  const queryClient = useQueryClient()
  // On the hook, not on `mutate`, so it still runs once the page has been left by the link that marked one read.
  return {
    mutation: {
      onSettled: async () => {
        await Promise.all([
          queryClient.invalidateQueries({ queryKey: getListNotificationsQueryKey() }),
          queryClient.invalidateQueries({ queryKey: getCountUnreadNotificationsQueryKey() }),
        ])
        told?.current?.focus()
      },
    },
  }
}

function Row({ note, told }: { note: NotificationRow; told: RefObject<HTMLParagraphElement | null> }) {
  const markRead = useMarkNotificationRead(useRefreshed())
  const markThis = useMarkNotificationRead(useRefreshed(told))
  const onOpen = () => { if (!note.read) markRead.mutate({ notificationId: note.id }) }
  return (
    <li className={cn("flex flex-col gap-1 border-b py-3 last:border-b-0", note.read && "text-muted-foreground")}>
      <p className={cn(!note.read && "font-medium text-foreground")}>
        {!note.read && <><Badge className="me-2 align-middle">New</Badge>{" "}</>}
        {note.text}
      </p>
      <p className="flex flex-wrap items-center gap-x-3 text-sm">
        <span className="text-muted-foreground">{hospitalTime(note.created_at)}</span>
        {note.link && <Open link={note.link} onOpen={onOpen} />}
        {!note.read && !note.link && (
          <Button variant="link" size="sm" className="h-auto p-0" onClick={() => markThis.mutate({ notificationId: note.id })}
            disabled={markThis.isPending}>Mark as read</Button>
        )}
      </p>
    </li>
  )
}

function NotificationsPage() {
  const list = useListNotifications()
  const told = useRef<HTMLParagraphElement>(null)
  const markAll = useMarkAllNotificationsRead(useRefreshed(told))
  const answer = list.data
  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4">
      <h1 className="text-xl font-medium">Notifications</h1>
      <PushOnThisDevice />
      {list.isPending && <p>Loading…</p>}
      {list.isError && <p role="alert">Your notifications could not be read. Reload the page to try again.</p>}
      {answer && answer.items.length === 0 && <p>You have no notifications.</p>}
      {answer && answer.items.length > 0 && (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <p role="status" ref={told} tabIndex={-1} className="outline-none">{answer.unread === 0 ? "All read." : `${answer.unread} unread.`}</p>
            {answer.unread > 0 && (
              <Button variant="outline" size="sm" onClick={() => markAll.mutate()} disabled={markAll.isPending}>Mark all as read</Button>
            )}
          </div>
          <ul aria-label="Notifications, newest first">
            {answer.items.map((note) => <Row key={note.id} note={note} told={told} />)}
          </ul>
        </>
      )}
    </div>
  )
}
