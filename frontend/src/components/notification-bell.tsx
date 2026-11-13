import { Link } from "@tanstack/react-router"
import { BellIcon } from "lucide-react"

import { ApiError } from "@/api/fetcher"
import { useCountUnreadNotifications } from "@/api/generated/endpoints"
import { cn } from "@/lib/utils"

/** How often the bell asks. Short in a test, so a test sees it change without waiting. The server does not count the asking as
 *  the user being there, so an open tab does not hold off the idle limit. */
export const BELL_POLL_MS = import.meta.env.MODE === "test" ? 10 : 30_000

const isRefusal = (error: unknown) => error instanceof ApiError && error.status < 500

/** The header's bell: how many notifications are unread, and the way to them. */
export function NotificationBell() {
  const unread = useCountUnreadNotifications({
    // A refusal will not change by asking again. A 401 also ends the session, which the query client handles.
    query: {
      refetchInterval: (query) => (isRefusal(query.state.error) ? false : BELL_POLL_MS),
      refetchOnWindowFocus: (query) => !isRefusal(query.state.error),
    },
  })
  const count = unread.data?.unread ?? 0
  return (
    <Link to="/notifications" aria-label={count ? `Notifications, ${count} unread` : "Notifications"}
      className={cn("relative inline-flex size-9 pointer-coarse:size-11 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground",
        "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 [&.active]:text-foreground")}>
      <BellIcon aria-hidden className="size-5" />
      {count > 0 && (
        <span aria-hidden className="absolute -end-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-primary px-1 text-[0.625rem] font-medium tabular-nums text-primary-foreground">
          {count > 99 ? "99+" : count}
        </span>
      )}
    </Link>
  )
}
