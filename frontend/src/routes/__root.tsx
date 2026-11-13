import type { QueryClient } from "@tanstack/react-query"
import { createRootRouteWithContext, Outlet, useMatches, useNavigate, useRouterState } from "@tanstack/react-router"
import { useEffect, useRef } from "react"

import { openPagesTappedInPushes } from "@/lib/service-worker"

export const Route = createRootRouteWithContext<{ queryClient: QueryClient }>()({
  component: Root,
})

/** A client-side navigation resets nothing by itself: the tab keeps the old title and focus stays
 *  on the link that was clicked, so a screen reader hears nothing of the new page. */
function Root() {
  const title = useMatches({ select: (matches) => matches.findLast((match) => match.staticData.title)?.staticData.title })
  useEffect(() => {
    document.title = title ? `${title} · Nurse roster` : "Nurse roster"
  }, [title])

  const pathname = useRouterState({ select: (state) => state.resolvedLocation?.pathname })
  const previous = useRef(pathname)
  useEffect(() => {
    const arrived = previous.current !== undefined && pathname !== previous.current
    previous.current = pathname
    // The login and password pages focus their first field themselves and have no such <main>.
    if (arrived) document.querySelector<HTMLElement>("main[tabindex]")?.focus()
  }, [pathname])

  // A push tapped while the app is open here sends this window to its page.
  const navigate = useNavigate()
  useEffect(() => openPagesTappedInPushes((path) => void navigate({ href: path })), [navigate])

  return <Outlet />
}
