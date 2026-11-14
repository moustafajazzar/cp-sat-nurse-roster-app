/* PROTOTYPE, throw away. The decided Staff Nurse phone shell (top bar, bottom bar of icons with short labels, Ward tokens) and a
   variant switcher, around the My month and Swaps prototypes. */
import { Link, useNavigate, useRouterState } from "@tanstack/react-router"
import { BellIcon, CalendarDaysIcon, CalendarIcon, HomeIcon, RepeatIcon, ShieldIcon } from "lucide-react"
import { type ReactNode, useEffect, useState } from "react"

import { cn } from "@/lib/utils"

export type PhoneVariant = "a" | "b" | "c"
const KEYS: PhoneVariant[] = ["a", "b", "c"]

export function PhoneShell({ title, current, variant, names, children }: { title: string; current: string; variant: PhoneVariant; names: Record<PhoneVariant, string>; children: ReactNode }) {
  const [dark, setDark] = useState(() => matchMedia("(prefers-color-scheme: dark)").matches)
  const navigate = useNavigate()
  const location = useRouterState({ select: (state) => state.location })
  const go = (by: number) => ({ ...(location.search as Record<string, unknown>), variant: KEYS[(KEYS.indexOf(variant) + by + 3) % 3] })
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.target as HTMLElement).closest("input, textarea, select, [contenteditable]")) return
      const by = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0
      if (by) void navigate({ to: location.pathname, search: go(by) as never, replace: true })
    }
    addEventListener("keydown", onKey)
    return () => removeEventListener("keydown", onKey)
  })
  const tabs: [string, string, ReactNode][] = [["/", "Home", <HomeIcon />], ["/my-month", "Month", <CalendarDaysIcon />], ["/my-roster", "Roster", <CalendarIcon />], ["/swaps", "Swaps", <RepeatIcon />], ["/covers", "Covers", <ShieldIcon />]]
  return (
    <div className={cn("ward fixed inset-0 z-40 overflow-y-auto bg-background pb-24 text-foreground", dark && "dark")}>
      <header className="sticky top-0 z-20 flex items-center gap-2 border-b bg-[var(--ward-surface)] px-4 py-3">
        <h1 className="text-base font-semibold">{title}</h1><span className="flex-1" />
        <span className="relative grid size-10 place-items-center text-muted-foreground"><BellIcon className="size-5" /></span>
        <span className="grid size-9 place-items-center rounded-full bg-accent text-xs font-semibold text-primary">ME</span>
      </header>
      <div className="mx-auto max-w-xl">{children}</div>
      <nav aria-label="Main" className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-5 border-t bg-[var(--ward-surface)] pb-[max(6px,env(safe-area-inset-bottom))]">
        {tabs.map(([to, label, icon]) => (
          <Link key={to} to={to} aria-current={to === current ? "page" : undefined}
            className={cn("flex min-h-12 flex-col items-center justify-center gap-0.5 pt-1.5 text-[10.5px] text-muted-foreground [&_svg]:size-5", to === current && "font-semibold text-primary")}>{icon}{label}</Link>
        ))}
      </nav>
      <div role="toolbar" aria-label="Prototype controls" className="fixed top-2 left-1/2 z-50 flex -translate-x-1/2 items-center gap-0.5 rounded-full bg-neutral-900 p-1 text-[12px] text-white shadow-xl">
        <Link to={location.pathname} replace search={go(-1) as never} className="rounded-full px-2.5 py-1 hover:bg-neutral-700" aria-label="Previous variant">←</Link>
        <span className="px-1.5 whitespace-nowrap">{variant.toUpperCase()} · {names[variant]}</span>
        <Link to={location.pathname} replace search={go(1) as never} className="rounded-full px-2.5 py-1 hover:bg-neutral-700" aria-label="Next variant">→</Link>
        <button type="button" className="rounded-full px-2.5 py-1 hover:bg-neutral-700" onClick={() => setDark(!dark)}>{dark ? "Dark" : "Light"}</button>
      </div>
    </div>
  )
}
