/* PROTOTYPE, throw away. Question: how should a Staff Nurse use the app on a phone, down to 320 px: what is Home, how do they
   move between their pages, and do five bottom-bar labels fit?
   Three variants of the phone Home, on the real "/" route behind ?variant=a|b|c, with the nurse's real roster, covers and swaps
   from the seeded stack (examples where the seed has none, labelled):
   A "Agenda"      what needs you, then your next shifts as a list; bar with icons and full labels ("My month", "My roster")
   B "Calendar"    the month as a calendar of your shifts, tap a day for its detail and who works with you; bar of short
                   labels without icons; what needs you only as badges
   C "Next shift"  a large next-shift card with who is on with you, then an inbox you answer in place, then the week; bar
                   with icons and short labels, and badges
   Uses the decided "Ward" tokens (index.css .ward). Answer buttons are stubs. */
import { Link, useNavigate } from "@tanstack/react-router"
import { BellIcon, CalendarDaysIcon, CalendarIcon, ChevronRightIcon, HomeIcon, RepeatIcon, ShieldIcon } from "lucide-react"
import { type ReactNode, useEffect, useMemo, useState } from "react"

import { useGetMyRoster, useGetMyUnit, useListMyCovers, useListMySwaps } from "@/api/generated/endpoints"
import type { Me, NurseRoster } from "@/api/generated/model"
import { waitsForMe } from "@/lib/cover-words"
import { cn, readableOn } from "@/lib/utils"

type Variant = "a" | "b" | "c"
const NAMES: Record<Variant, string> = { a: "Agenda", b: "Calendar", c: "Next shift" }
const KEYS: Variant[] = ["a", "b", "c"]
const TIMES: Record<string, string> = { D: "07:00–19:00", N: "19:00–07:00", A: "07:00–15:00", B: "15:00–23:00", C: "23:00–07:00" }
const wd = new Intl.DateTimeFormat("en-GB", { weekday: "short", timeZone: "UTC" })
const longDate = new Intl.DateTimeFormat("en-GB", { weekday: "long", day: "numeric", month: "long", timeZone: "UTC" })
const asDate = (iso: string) => new Date(`${iso}T00:00:00Z`)
const nextMonth = (month: string) => { const [y, m] = month.split("-").map(Number); return m === 12 ? `${y + 1}-01` : `${y}-${String(m + 1).padStart(2, "0")}` }

type Day = { date: string; kind: "shift" | "off" | "leave" | "outside"; code: string; name: string; colour: string; hours: number; with: string[] }

function daysOf(roster: NurseRoster | null | undefined, me: string): Day[] {
  if (!roster) return []
  const mine = roster.rows.find((row) => row.hospital_id === me)
  return roster.days.map((day, index) => {
    const code = mine?.shifts[index] ?? ""
    const shift = roster.shifts.find((candidate) => candidate.code === code)
    const kind = roster.mine?.[index]?.kind ?? (code ? "shift" : "off")
    return {
      date: day.date, kind, code, name: shift?.display_name ?? "", colour: shift?.colour ?? "#888888", hours: shift?.hours ?? 0,
      with: code ? roster.rows.filter((row) => row.hospital_id !== me && row.shifts[index] === code).map((row) => row.full_name) : [],
    }
  })
}

function ShiftChip({ day, size = "md" }: { day: Day; size?: "sm" | "md" | "lg" }) {
  if (day.kind === "leave") return <span className={cn("grid place-items-center rounded-[5px] bg-[#e9e4f2] font-bold text-[#4c3d6b]", size === "lg" ? "size-14 text-xl" : size === "sm" ? "h-6 min-w-6 text-[11px]" : "size-9 text-sm")}>L</span>
  if (!day.code) return <span className={cn("grid place-items-center rounded-[5px] text-muted-foreground", size === "lg" ? "size-14" : size === "sm" ? "h-6 min-w-6 text-[11px]" : "size-9 text-sm")}>–</span>
  return (
    <span className={cn("grid place-items-center rounded-[5px] font-bold", size === "lg" ? "size-14 text-xl" : size === "sm" ? "h-6 min-w-6 text-[11px]" : "size-9 text-sm")}
      ref={(el) => { if (el) { el.style.backgroundColor = day.colour; el.style.color = readableOn(day.colour) } }}>{day.code}</span>
  )
}

type Tab = { to: string; icon: ReactNode; full: string; short: string; badge?: number }
function BottomBar({ style, covers, swaps }: { style: "full" | "short-no-icon" | "short"; covers: number; swaps: number }) {
  const tabs: Tab[] = [
    { to: "/", icon: <HomeIcon />, full: "Home", short: "Home" },
    { to: "/my-month", icon: <CalendarDaysIcon />, full: "My month", short: "Month" },
    { to: "/my-roster", icon: <CalendarIcon />, full: "My roster", short: "Roster" },
    { to: "/swaps", icon: <RepeatIcon />, full: "Swaps", short: "Swaps", badge: swaps },
    { to: "/covers", icon: <ShieldIcon />, full: "Covers", short: "Covers", badge: covers },
  ]
  return (
    <nav aria-label="Main" className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-5 border-t bg-[var(--ward-surface)] pb-[max(6px,env(safe-area-inset-bottom))]">
      {tabs.map((tab) => (
        <Link key={tab.to} to={tab.to} aria-current={tab.to === "/" ? "page" : undefined}
          className={cn("relative flex min-h-12 flex-col items-center justify-center gap-0.5 px-0.5 pt-1.5 text-muted-foreground [&_svg]:size-5",
            tab.to === "/" && "font-semibold text-primary", style === "short-no-icon" && "text-[13px]", style !== "short-no-icon" && "text-[10.5px]")}>
          {style !== "short-no-icon" && tab.icon}
          <span className="max-w-full truncate whitespace-nowrap">{style === "full" ? tab.full : tab.short}</span>
          {tab.to === "/" && style === "short-no-icon" && <span className="absolute inset-x-4 top-0 h-0.5 rounded-full bg-primary" />}
          {!!tab.badge && style !== "full" && <span className="absolute top-1 left-1/2 ms-2 grid h-4 min-w-4 place-items-center rounded-full bg-destructive px-1 text-[10px] font-bold text-white">{tab.badge}</span>}
        </Link>
      ))}
    </nav>
  )
}

function TopBar({ title, unit }: { title: string; unit?: string }) {
  return (
    <header className="sticky top-0 z-20 flex items-center gap-2 border-b bg-[var(--ward-surface)] px-4 py-3">
      <div className="min-w-0"><h1 className="truncate text-base font-semibold">{title}</h1>{unit && <p className="text-xs text-muted-foreground">{unit}</p>}</div>
      <span className="flex-1" />
      <Link to="/notifications" aria-label="Notifications" className="relative grid size-10 place-items-center rounded-[5px] text-muted-foreground"><BellIcon className="size-5" /><span className="absolute end-2 top-2 size-2 rounded-full bg-destructive" /></Link>
      <span className="grid size-9 place-items-center rounded-full bg-accent text-xs font-semibold text-primary">ME</span>
    </header>
  )
}

type Need = { key: string; title: string; detail: string; to: string; answer?: boolean; example?: boolean }

export function NursePhonePrototype({ variant, user }: { variant: Variant; user: Me }) {
  const navigate = useNavigate()
  const unit = useGetMyUnit()
  const now = useGetMyRoster()
  const month = now.data?.month
  const next = useGetMyRoster(month ? { month: nextMonth(month) } : undefined, { query: { enabled: !!month } })
  const covers = useListMyCovers()
  const swaps = useListMySwaps()
  const [dark, setDark] = useState(() => matchMedia("(prefers-color-scheme: dark)").matches)
  const [picked, setPicked] = useState<string | null>(null)
  const [said, setSaid] = useState("")
  const me = user.username

  const today = new Date().toISOString().slice(0, 10)
  const all = useMemo(() => [...daysOf(now.data?.roster, me), ...daysOf(next.data?.roster, me)], [now.data, next.data, me])
  const ahead = all.filter((day) => day.date >= today)
  const nextShift = ahead.find((day) => day.kind === "shift" && day.code)
  const shownMonth = (now.data?.roster ? now.data : next.data)
  const monthDays = daysOf(shownMonth?.roster, me)

  const coverNeeds: Need[] = (covers.data?.covers ?? []).filter(waitsForMe).map((cover) => ({
    key: cover.id, title: `Cover offered: ${cover.shift} on ${longDate.format(asDate(cover.date))}`, detail: cover.note || `${cover.hours} h · ${cover.unit}`, to: "/covers", answer: true }))
  const swapNeeds: Need[] = (swaps.data ?? []).filter((swap) => swap.my_answer === "pending" && swap.state === "pending").map((swap) => ({
    key: swap.id, title: `${swap.proposer} asks to swap`, detail: swap.cells.map((cell) => `${cell.date.slice(8)}: ${cell.before || "off"} → ${cell.after || "off"}`).join(" · "), to: "/swaps", answer: true }))
  const realNeeds = [...coverNeeds, ...swapNeeds]
  const needs: Need[] = realNeeds.length ? realNeeds : [
    { key: "x1", title: "Cover offered: Night on Sat 19 December", detail: "12 h · ICU is one short (example)", to: "/covers", answer: true, example: true },
    { key: "x2", title: "Lina Farouk asks to swap", detail: "22: D → off · 23: off → D (example)", to: "/swaps", answer: true, example: true },
    { key: "x3", title: "Requests for January are open", detail: "Until 20 November (example)", to: "/my-month", example: true },
  ]
  const coverCount = needs.filter((need) => need.to === "/covers").length
  const swapCount = needs.filter((need) => need.to === "/swaps").length
  const answer = (need: Need, yes: boolean) => setSaid(`${yes ? "Accepted" : "Declined"}: ${need.title} (prototype, nothing sent)`)

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.target as HTMLElement).closest("input, textarea, select")) return
      const by = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0
      if (by) void navigate({ to: "/", search: { variant: KEYS[(KEYS.indexOf(variant) + by + 3) % 3] }, replace: true })
    }
    addEventListener("keydown", onKey)
    return () => removeEventListener("keydown", onKey)
  }, [navigate, variant])

  const hours = monthDays.reduce((sum, day) => sum + (day.kind === "shift" ? day.hours : 0), 0)
  const shiftsLeft = ahead.filter((day) => day.kind === "shift" && day.code && day.date.slice(0, 7) === shownMonth?.month).length
  const loading = now.isPending || unit.isPending

  const needList = (inline: boolean) => (
    <ul className="flex flex-col gap-2">
      {needs.map((need) => (
        <li key={need.key} className="rounded-lg border bg-[var(--ward-surface)] p-3">
          <Link to={need.to} className="flex items-start gap-2">
            <span className="min-w-0 flex-1"><span className="block font-medium">{need.title}</span><span className="block text-sm text-muted-foreground">{need.detail}</span></span>
            <ChevronRightIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
          </Link>
          {inline && need.answer && (
            <div className="mt-2.5 grid grid-cols-2 gap-2">
              <button type="button" onClick={() => answer(need, false)} className="min-h-11 rounded-[5px] border font-medium">Decline</button>
              <button type="button" onClick={() => answer(need, true)} className="min-h-11 rounded-[5px] bg-primary font-medium text-primary-foreground">Accept</button>
            </div>
          )}
        </li>
      ))}
    </ul>
  )

  let body: ReactNode
  if (loading) body = <p className="p-4">Loading…</p>
  else if (variant === "a") body = (
    <div className="flex flex-col gap-5 p-4">
      <section className="flex flex-col gap-2">
        <h2 className="flex items-center gap-2 text-sm font-semibold">Needs you<span className="rounded-full bg-[var(--ward-warn-soft)] px-2 text-xs text-[var(--ward-warn)]">{needs.length}</span></h2>
        {needList(false)}
      </section>
      <section className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold">Your next shifts</h2>
        {ahead.length === 0 && <p className="text-sm text-muted-foreground">No approved roster ahead yet.</p>}
        <ul className="flex flex-col divide-y rounded-lg border bg-[var(--ward-surface)]">
          {ahead.filter((day) => day.kind !== "off" || day.date === today).slice(0, 8).map((day) => (
            <li key={day.date} className="flex items-center gap-3 px-3 py-2.5">
              <span className="w-10 text-center leading-tight"><span className="block text-lg font-semibold tabular-nums">{Number(day.date.slice(8))}</span><span className="block text-[11px] text-muted-foreground uppercase">{wd.format(asDate(day.date))}</span></span>
              <span className="min-w-0 flex-1"><span className="block font-medium">{day.kind === "leave" ? "Leave" : day.code ? day.name : "Off"}</span><span className="block text-sm text-muted-foreground">{day.code ? `${TIMES[day.code] ?? `${day.hours} h`} · ${day.with.length} colleagues on` : ""}</span></span>
              <ShiftChip day={day} />
            </li>
          ))}
        </ul>
        <Link to="/my-roster" className="text-sm font-medium text-primary">The whole unit's roster</Link>
      </section>
      <section className="grid grid-cols-2 gap-2">
        <div className="rounded-lg border bg-[var(--ward-surface)] p-3"><b className="block text-xl tabular-nums">{hours} h</b><span className="text-xs text-muted-foreground">rostered this month</span></div>
        <div className="rounded-lg border bg-[var(--ward-surface)] p-3"><b className="block text-xl tabular-nums">{shiftsLeft}</b><span className="text-xs text-muted-foreground">shifts left this month</span></div>
      </section>
    </div>
  )
  else if (variant === "b") {
    const first = monthDays[0] ? (asDate(monthDays[0].date).getUTCDay() + 6) % 7 : 0
    const sel = monthDays.find((day) => day.date === picked) ?? monthDays.find((day) => day.date >= today) ?? monthDays[0]
    body = (
      <div className="flex flex-col gap-4 p-3">
        <section aria-label="Your month" className="rounded-lg border bg-[var(--ward-surface)] p-2">
          <div className="grid grid-cols-7 gap-1 pb-1 text-center text-[11px] text-muted-foreground uppercase">{["M", "T", "W", "T", "F", "S", "S"].map((d, i) => <span key={i}>{d}</span>)}</div>
          <div className="grid grid-cols-7 gap-1">
            {Array.from({ length: first }, (_, i) => <span key={`pad${i}`} />)}
            {monthDays.map((day) => (
              <button key={day.date} type="button" onClick={() => setPicked(day.date)} aria-pressed={sel?.date === day.date}
                aria-label={`${longDate.format(asDate(day.date))}: ${day.kind === "leave" ? "leave" : day.code ? day.name : "off"}`}
                className={cn("flex aspect-square flex-col items-center justify-center gap-0.5 rounded-[5px] text-[11px]", day.date === today && "ring-2 ring-primary",
                  sel?.date === day.date && "bg-accent")}>
                <span className="tabular-nums text-muted-foreground">{Number(day.date.slice(8))}</span>
                <ShiftChip day={day} size="sm" />
              </button>
            ))}
          </div>
        </section>
        {sel && (
          <section className="rounded-lg border bg-[var(--ward-surface)] p-3">
            <p className="text-sm text-muted-foreground">{longDate.format(asDate(sel.date))}</p>
            <div className="mt-1 flex items-center gap-3"><ShiftChip day={sel} /><span><b className="block">{sel.kind === "leave" ? "Leave" : sel.code ? sel.name : "Off"}</b><span className="text-sm text-muted-foreground">{sel.code ? TIMES[sel.code] ?? `${sel.hours} h` : ""}</span></span></div>
            {sel.with.length > 0 && <p className="mt-2 text-sm">On with you: {sel.with.slice(0, 6).join(", ")}{sel.with.length > 6 ? ` and ${sel.with.length - 6} more` : ""}</p>}
            {sel.code && <Link to="/swaps" className="mt-2 inline-block text-sm font-medium text-primary">Ask to swap this shift</Link>}
          </section>
        )}
        <p className="px-1 text-sm text-muted-foreground tabular-nums">{hours} h rostered · {shiftsLeft} shifts left in the month</p>
      </div>
    )
  }
  else body = (
    <div className="flex flex-col gap-5 p-4">
      {nextShift ? (
        <section className="rounded-xl border bg-[var(--ward-surface)] p-4">
          <p className="text-sm text-muted-foreground">Your next shift</p>
          <div className="mt-2 flex items-center gap-3"><ShiftChip day={nextShift} size="lg" />
            <span><b className="block text-lg">{nextShift.name}</b><span className="block text-sm">{longDate.format(asDate(nextShift.date))}</span><span className="block text-sm text-muted-foreground tabular-nums">{TIMES[nextShift.code] ?? `${nextShift.hours} h`}</span></span></div>
          {nextShift.with.length > 0 && <p className="mt-3 text-sm"><span className="text-muted-foreground">On with you </span>{nextShift.with.slice(0, 5).join(", ")}{nextShift.with.length > 5 ? ` +${nextShift.with.length - 5}` : ""}</p>}
        </section>
      ) : <p className="text-sm text-muted-foreground">No shift ahead on an approved roster yet.</p>}
      <section className="flex flex-col gap-2"><h2 className="text-sm font-semibold">Needs your answer</h2>{needList(true)}</section>
      <section className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold">This week</h2>
        <div className="grid grid-cols-7 gap-1">
          {ahead.slice(0, 7).map((day) => (
            <div key={day.date} className="flex flex-col items-center gap-1 rounded-[5px] border bg-[var(--ward-surface)] py-1.5 text-[11px]">
              <span className="text-muted-foreground uppercase">{wd.format(asDate(day.date)).slice(0, 2)}</span><span className="font-semibold tabular-nums">{Number(day.date.slice(8))}</span><ShiftChip day={day} size="sm" />
            </div>
          ))}
        </div>
        <Link to="/my-month" className="text-sm font-medium text-primary">My month: hours, leave and requests</Link>
      </section>
    </div>
  )

  return (
    <div className={cn("ward fixed inset-0 z-40 overflow-y-auto bg-background pb-24 text-foreground", dark && "dark")}>
      <TopBar title={variant === "b" ? (shownMonth?.month ? new Intl.DateTimeFormat("en-GB", { month: "long", year: "numeric", timeZone: "UTC" }).format(asDate(`${shownMonth.month}-01`)) : "My month") : `Hello, ${(user.full_name || user.username).split(" ")[0]}`} unit={unit.data?.name} />
      <p role="status" className="px-4 pt-2 text-sm text-primary empty:hidden">{said}</p>
      {!realNeeds.length && !loading && <p className="px-4 pt-2 text-xs text-muted-foreground">Nothing waits for this nurse in the seed, so the waiting items are examples.</p>}
      {body}
      <BottomBar style={variant === "a" ? "full" : variant === "b" ? "short-no-icon" : "short"} covers={coverCount} swaps={swapCount} />
      <div role="toolbar" aria-label="Prototype controls" className="fixed top-2 left-1/2 z-50 flex -translate-x-1/2 items-center gap-0.5 rounded-full bg-neutral-900 p-1 text-[12px] text-white shadow-xl">
        {(["←", "→"] as const).map((arrow) => (
          <Link key={arrow} to="/" replace search={{ variant: KEYS[(KEYS.indexOf(variant) + (arrow === "→" ? 1 : 2)) % 3] }}
            className={cn("rounded-full px-2.5 py-1 hover:bg-neutral-700", arrow === "→" && "order-2")} aria-label={arrow === "→" ? "Next variant" : "Previous variant"}>{arrow}</Link>
        ))}
        <span className="order-1 px-1.5 whitespace-nowrap">{variant.toUpperCase()} · {NAMES[variant]}</span>
        <button type="button" className="order-3 rounded-full px-2.5 py-1 hover:bg-neutral-700" onClick={() => setDark(!dark)}>{dark ? "Dark" : "Light"}</button>
      </div>
    </div>
  )
}
