import { useQueryClient } from "@tanstack/react-query"
import { createFileRoute, Link, Outlet, redirect, useRouteContext, useRouterState } from "@tanstack/react-router"
import { MenuIcon, XIcon } from "lucide-react"
import { useEffect, useRef, useState, type KeyboardEvent } from "react"

import { NotificationBell } from "@/components/notification-bell"
import { Button } from "@/components/ui/button"
import { PUSH_STATE_KEY, renewAfterARotation } from "@/lib/push"
import { cn } from "@/lib/utils"
import { useAwaitingPassword } from "@/lib/use-awaiting-password"
import { navigation, ROLE_NAMES } from "@/roles"
import { currentUser, useSignOut } from "@/session"

/** Everything past the login. */
export const Route = createFileRoute("/_signed-in")({
  beforeLoad: async ({ context }) => {
    const user = await currentUser(context.queryClient)
    if (!user) throw redirect({ to: "/login" })
    if (user.must_change_password) throw redirect({ to: "/change-password" })
    return { user }
  },
  component: SignedInLayout,
})

const linkClass = "text-sm text-muted-foreground hover:text-foreground [&.active]:font-medium [&.active]:text-foreground"
/** A row of the phone's menu: the whole width, and tall enough for a thumb. */
const menuLinkClass = cn("flex min-h-11 items-center rounded-md px-2 text-base text-muted-foreground hover:bg-muted hover:text-foreground",
  "outline-none focus-visible:ring-3 focus-visible:ring-ring [&.active]:font-medium [&.active]:text-foreground")

/** The header has two shapes, chosen by CSS alone at Tailwind's `sm` (640 px). Wider, it is one row: the pages, the bell, who is
 *  signed in and Log out. On a phone it is the name, the bell and a Menu button, whose list of the same pages, the password and
 *  Log out is in the page only while it is open, so the links are never there twice for a screen reader or a test. */
function LogOutButton({ className }: { className?: string }) {
  const signOut = useSignOut()
  // Logging out clears the mutation cache and leaves the page, so a password on its way would reach nobody.
  const awaitingPassword = useAwaitingPassword()
  return <Button variant="outline" size="sm" className={className} onClick={signOut.run} disabled={signOut.pending || awaitingPassword}>Log out</Button>
}

/** Once a start of the app for a signed-in user: a device bound to a replaced key re-subscribes with no question. */
function useRenewedPushes() {
  const queryClient = useQueryClient()
  const asked = useRef(false)
  useEffect(() => {
    if (asked.current) return
    asked.current = true
    void renewAfterARotation().then((renewed) => {
      if (renewed) void queryClient.invalidateQueries({ queryKey: PUSH_STATE_KEY })
    })
  }, [queryClient])
}

function SignedInLayout() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  const [menuOpen, setMenuOpen] = useState(false)
  const menuButton = useRef<HTMLButtonElement>(null)
  const path = useRouterState({ select: (state) => state.location.pathname })
  const pages = [...navigation(user), { to: "/change-password", label: "Change password" } as const]
  const who = `${user.username} · ${ROLE_NAMES[user.role]}`

  useRenewedPushes()

  // Choosing a page closes it: from the menu, the page already open too (below), or anywhere else, by the path changing.
  useEffect(() => setMenuOpen(false), [path])

  const closeOnEscape = (event: KeyboardEvent) => {
    if (event.key !== "Escape") return
    setMenuOpen(false)
    menuButton.current?.focus()
  }

  return (
    <div className="min-h-svh">
      <header className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b px-4 py-3 print:hidden" onKeyDown={menuOpen ? closeOnEscape : undefined}>
        <span className="font-medium">Nurse roster</span>
        <nav aria-label="Main" className="flex flex-wrap items-center gap-4 max-sm:hidden">
          {pages.map(({ label, ...link }) => (
            <Link key={link.to} {...link} className={linkClass}>{label}</Link>
          ))}
        </nav>
        <div className="ms-auto flex items-center gap-3">
          <NotificationBell />
          <span className="text-sm text-muted-foreground max-sm:hidden">{who}</span>
          <LogOutButton className="max-sm:hidden" />
          <Button ref={menuButton} variant="outline" className="sm:hidden" aria-expanded={menuOpen} aria-controls="main-menu"
            onClick={() => setMenuOpen((open) => !open)}>
            {menuOpen ? <XIcon aria-hidden /> : <MenuIcon aria-hidden />}
            Menu
          </Button>
        </div>
        {menuOpen && (
          <div id="main-menu" className="flex w-full flex-col gap-2 border-t pt-2 sm:hidden">
            <nav aria-label="Main menu">
              <ul className="flex flex-col">
                {pages.map(({ label, ...link }) => (
                  <li key={link.to}><Link {...link} className={menuLinkClass} onClick={() => setMenuOpen(false)}>{label}</Link></li>
                ))}
              </ul>
            </nav>
            <div className="flex flex-wrap items-center justify-between gap-2 border-t pt-2">
              <span className="text-sm text-muted-foreground">{who}</span>
              <LogOutButton />
            </div>
          </div>
        )}
      </header>
      <main tabIndex={-1} className="p-4 outline-none">
        <Outlet />
      </main>
    </div>
  )
}
