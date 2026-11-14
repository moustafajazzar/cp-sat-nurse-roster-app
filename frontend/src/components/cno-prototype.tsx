/* PROTOTYPE, throw away. Question: how should the CNO and Deputy CNO read the hospital across units: Rosters, Day view,
   Expiries, their "needs you now" Home, and what of it works on a phone?
   Three variants on the real "/" route behind ?variant=a|b|c, with the seeded stack's real overview, day and expiries:
   A "Three pages"  Home is a short "needs you now" list; Rosters, Day view and Expiries stay separate pages (tabs here stand
                    in for the sidebar), each restyled: a units × months state matrix, a units × shifts headcount table,
                    an expiries table
   B "Board"        one hospital board: a row per unit with this month's and next month's roster state, today's headcount
                    per shift against its minimum, leave, overtime against the cap and expiries; a row opens a unit drawer
   C "Today first"  what waits for a decision on top, then today's staffing as a card per unit with the short units first,
                    then a months timeline of roster states
   Decided shell and "Ward" tokens (index.css .ward). Read-only; links go to the real pages. */
import { Link, useNavigate } from "@tanstack/react-router"
import { AlertTriangleIcon, ChevronRightIcon, UsersIcon, XIcon } from "lucide-react"
import { type ReactNode, useEffect, useMemo, useState } from "react"

import { useGetHospitalDay, useGetRosterOverview, useListExpiries, useListSubmittedRosters } from "@/api/generated/endpoints"
import type { DayUnit, HospitalDayAnswer, Me, Overview } from "@/api/generated/model"
import { cn, monthName } from "@/lib/utils"

type Variant = "a" | "b" | "c"
const NAMES: Record<Variant, string> = { a: "Three pages", b: "Board", c: "Today first" }
const KEYS: Variant[] = ["a", "b", "c"]
type State = "none" | "draft" | "submitted" | "approved" | "closed"
const STATE: Record<State, [string, string]> = {
  none: ["No roster", "bg-muted text-muted-foreground"],
  draft: ["Draft", "bg-[var(--ward-warn-soft)] text-[var(--ward-warn)]"],
  submitted: ["Waiting for you", "bg-accent text-primary ring-1 ring-primary/40"],
  approved: ["In force", "bg-[var(--ward-ok-soft)] text-[var(--ward-ok)]"],
  closed: ["Closed", "bg-muted text-foreground"],
}
const short = (month: string) => new Intl.DateTimeFormat("en-GB", { month: "short", year: "2-digit", timeZone: "UTC" }).format(new Date(`${month}-01T00:00:00Z`))
const longDay = new Intl.DateTimeFormat("en-GB", { weekday: "long", day: "numeric", month: "long", timeZone: "UTC" })

function StateChip({ state, compact = false }: { state: State; compact?: boolean }) {
  const [says, cls] = STATE[state]
  return <span className={cn("inline-flex items-center justify-center rounded-full font-medium whitespace-nowrap", compact ? "h-6 px-2 text-[11px]" : "px-2 py-0.5 text-xs", cls)}>{says}</span>
}

function Headcount({ cell }: { cell: DayUnit["figures"] extends infer F ? F extends { shifts: (infer S)[] } ? S : never : never }) {
  const bad = cell.short || cell.team_leader === false
  return (
    <span className={cn("inline-flex items-center gap-1 rounded-[5px] px-1.5 py-0.5 text-sm tabular-nums", bad ? "bg-[var(--ward-bad-soft)] font-semibold text-destructive" : cell.over ? "bg-[var(--ward-warn-soft)] text-[var(--ward-warn)]" : "")}
      title={`${cell.present} on${cell.minimum !== null ? `, needs ${cell.minimum}` : ""}${cell.team_leader === false ? ", no team leader" : ""}`}>
      {cell.present}{cell.minimum !== null && <span className="text-muted-foreground">/{cell.minimum}</span>}
      {cell.team_leader === false && <span className="text-[10px] font-bold">TL</span>}
    </span>
  )
}

function useData() {
  const overview = useGetRosterOverview()
  const day = useGetHospitalDay()
  const expiries = useListExpiries()
  const waiting = useListSubmittedRosters()
  const expiriesByUnit = useMemo(() => {
    const map = new Map<string, number>()
    for (const row of expiries.data?.rows ?? []) map.set(row.unit.name, (map.get(row.unit.name) ?? 0) + 1)
    return map
  }, [expiries.data])
  // Nothing is submitted in the seed, so one labelled example stands in for a roster waiting for a decision.
  const first = overview.data?.units[0]
  const realWaiting = waiting.data ?? []
  const shown = realWaiting.length || !first || !overview.data ? realWaiting
    : [{ unit_id: first.id, unit: first.name, month: overview.data.months[1], submitted_by: "its manager (example)", submitted_at: "", changed_since: true }]
  return { overview: overview.data, day: day.data, expiries: expiries.data, waiting: shown, expiriesByUnit, loading: overview.isPending || day.isPending }
}
type Data = ReturnType<typeof useData>

const shortUnits = (day?: HospitalDayAnswer) => (day?.units ?? []).filter((unit) => unit.figures?.shifts.some((cell) => cell.short || cell.team_leader === false))

function NeedsYou({ data }: { data: Data }) {
  const shortToday = shortUnits(data.day)
  const items: [ReactNode, string, ReactNode][] = [
    ...data.waiting.map((w): [ReactNode, string, ReactNode] => [<>{w.unit}, {monthName(w.month)} waits for your decision{w.changed_since && <span className="ms-1 text-[var(--ward-warn)]">· changed since</span>}</>, "/rosters", <StateChip state="submitted" />]),
    ...shortToday.map((unit): [ReactNode, string, ReactNode] => [<>{unit.name} is short today: {unit.figures!.shifts.filter((c) => c.short || c.team_leader === false).map((c) => `${c.code} ${c.present}${c.minimum !== null ? `/${c.minimum}` : ""}${c.team_leader === false ? " no TL" : ""}`).join(", ")}</>, "/day", <AlertTriangleIcon className="size-4 text-destructive" />]),
    ...(data.expiries?.count ? [[<>{data.expiries.count} skills expired or expiring within {data.expiries.horizon_days} days</>, "/expiries", <span className="rounded-full bg-[var(--ward-warn-soft)] px-2 text-xs text-[var(--ward-warn)] tabular-nums">{data.expiries.count}</span>] as [ReactNode, string, ReactNode]] : []),
  ]
  return (
    <ul className="flex flex-col divide-y rounded-lg border bg-[var(--ward-surface)]">
      {items.length === 0 && <li className="px-3 py-3 text-sm text-muted-foreground">Nothing waits for you.</li>}
      {items.map(([text, to, mark], index) => (
        <li key={index}><Link to={to} className="flex items-center gap-3 px-3 py-2.5 text-sm hover:bg-muted"><span className="grid w-5 shrink-0 place-items-center">{mark}</span><span className="min-w-0 flex-1">{text}</span><ChevronRightIcon className="size-4 shrink-0 text-muted-foreground" /></Link></li>
      ))}
    </ul>
  )
}

function Matrix({ overview }: { overview?: Overview }) {
  if (!overview) return null
  return (
    <div className="overflow-x-auto rounded-lg border bg-[var(--ward-surface)]">
      <table className="w-full min-w-max text-sm">
        <thead><tr className="border-b text-xs text-muted-foreground"><th className="sticky start-0 bg-[var(--ward-surface)] px-3 py-2 text-start font-medium">Unit</th>{overview.months.map((m) => <th key={m} className="px-2 py-2 font-medium">{short(m)}</th>)}</tr></thead>
        <tbody>{overview.units.map((unit) => (
          <tr key={unit.id} className="border-b last:border-0">
            <th scope="row" className="sticky start-0 bg-[var(--ward-surface)] px-3 py-2 text-start font-medium whitespace-nowrap">{unit.name}{unit.archived && <span className="ms-1 text-xs text-muted-foreground">(archived)</span>}</th>
            {unit.states.map((state, i) => <td key={i} className="px-1.5 py-1.5 text-center"><Link to="/unit-roster" search={{ unit: unit.id, month: overview.months[i] }}><StateChip state={state} compact /></Link></td>)}
          </tr>
        ))}</tbody>
      </table>
    </div>
  )
}

function DayTable({ day }: { day?: HospitalDayAnswer }) {
  if (!day) return null
  return (
    <div className="overflow-x-auto rounded-lg border bg-[var(--ward-surface)]">
      <table className="w-full min-w-max text-sm">
        <thead><tr className="border-b text-xs text-muted-foreground"><th className="sticky start-0 bg-[var(--ward-surface)] px-3 py-2 text-start font-medium">Unit</th>{day.shifts.map((s) => <th key={s.code} className="px-2 py-2 font-medium">{s.display_name}</th>)}<th className="px-2 py-2 font-medium">Leave</th><th className="px-2 py-2 font-medium">Overtime</th></tr></thead>
        <tbody>{day.units.map((unit) => (
          <tr key={unit.id} className="border-b last:border-0">
            <th scope="row" className="sticky start-0 bg-[var(--ward-surface)] px-3 py-2 text-start font-medium whitespace-nowrap">{unit.name}</th>
            {day.shifts.map((s) => { const cell = unit.figures?.shifts.find((c) => c.code === s.code); return <td key={s.code} className="px-2 py-1.5 text-center">{cell ? <Headcount cell={cell} /> : <span className="text-muted-foreground">–</span>}</td> })}
            <td className="px-2 text-center tabular-nums">{unit.figures?.on_leave.length ?? "–"}</td>
            <td className="px-2 text-center tabular-nums">{unit.figures ? <span className={cn(unit.figures.overtime > unit.figures.overtime_cap && "font-semibold text-destructive")}>{unit.figures.overtime}<span className="text-muted-foreground">/{unit.figures.overtime_cap} h</span></span> : <StateChip state={unit.state} compact />}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  )
}

function ExpiryTable({ data }: { data: Data }) {
  const rows = data.expiries?.rows ?? []
  return (
    <div className="overflow-x-auto rounded-lg border bg-[var(--ward-surface)]">
      <table className="w-full min-w-max text-sm">
        <thead><tr className="border-b text-xs text-muted-foreground"><th className="px-3 py-2 text-start font-medium">Nurse</th><th className="px-3 py-2 text-start font-medium">Unit</th><th className="px-3 py-2 text-start font-medium">Skill</th><th className="px-3 py-2 text-start font-medium">State</th><th className="px-3 py-2 text-start font-medium">Last day</th></tr></thead>
        <tbody>{rows.slice(0, 25).map((row, i) => (
          <tr key={i} className="border-b last:border-0"><td className="px-3 py-2">{row.full_name}</td><td className="px-3">{row.unit.name}</td><td className="px-3">{row.skill}</td>
            <td className="px-3"><span className={cn("rounded-full px-2 py-0.5 text-xs font-medium", row.state === "expiring" ? "bg-[var(--ward-warn-soft)] text-[var(--ward-warn)]" : "bg-[var(--ward-bad-soft)] text-destructive")}>{row.state === "expired" ? "Expired" : row.state === "expiring" ? "Expiring" : "No record"}</span></td>
            <td className="px-3 tabular-nums">{row.expiry ?? "–"}</td></tr>
        ))}</tbody>
      </table>
      {rows.length === 0 && <p className="p-3 text-sm text-muted-foreground">Nothing expires within the horizon.</p>}
    </div>
  )
}

function UnitDrawer({ unit, day, overview, expiries, onClose }: { unit: DayUnit; day: HospitalDayAnswer; overview?: Overview; expiries: number; onClose: () => void }) {
  const states = overview?.units.find((u) => u.id === unit.id)
  return (
    <aside aria-label={unit.name} className="fixed inset-y-0 end-0 z-50 flex w-[24rem] max-w-full flex-col border-s bg-[var(--ward-surface)] shadow-xl">
      <div className="flex items-center gap-2 border-b px-4 py-3"><h2 className="font-semibold">{unit.name}</h2><StateChip state={unit.state} /><span className="flex-1" />
        <button type="button" aria-label="Close" onClick={onClose} className="grid size-8 place-items-center rounded-[5px] hover:bg-muted"><XIcon className="size-4" /></button></div>
      <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-4 text-sm">
        <p className="text-muted-foreground">{longDay.format(new Date(`${day.date}T00:00:00Z`))}</p>
        {unit.figures?.shifts.map((cell) => (
          <section key={cell.code}>
            <div className="flex items-center gap-2"><b>{day.shifts.find((s) => s.code === cell.code)?.display_name ?? cell.code}</b><Headcount cell={cell} /></div>
            <p className="mt-1 text-muted-foreground">{cell.nurses.map((n) => n.full_name + (n.team_leader ? " (TL)" : "")).join(", ") || "Nobody"}</p>
          </section>
        ))}
        {unit.unchecked && <p className="text-destructive">{unit.unchecked}</p>}
        <p>On leave: {unit.figures?.on_leave.map((l) => l.full_name).join(", ") || "nobody"}</p>
        <p>Expiries: {expiries}</p>
        {states && overview && <div className="flex flex-wrap gap-1.5">{states.states.map((s, i) => <Link key={i} to="/unit-roster" search={{ unit: unit.id, month: overview.months[i] }} className="flex items-center gap-1 text-xs">{short(overview.months[i])} <StateChip state={s} compact /></Link>)}</div>}
        <Link to="/unit-roster" search={{ unit: unit.id }} className="font-medium text-primary">Open this unit's roster</Link>
      </div>
    </aside>
  )
}

export function CnoPrototype({ variant, user }: { variant: Variant; user: Me }) {
  const navigate = useNavigate()
  const data = useData()
  const [tab, setTab] = useState<"home" | "rosters" | "day" | "expiries">("home")
  const [dark, setDark] = useState(() => matchMedia("(prefers-color-scheme: dark)").matches)
  const [open, setOpen] = useState<string | null>(null)
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.target as HTMLElement).closest("input, textarea, select, table")) return
      const by = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0
      if (by) void navigate({ to: "/", search: { variant: KEYS[(KEYS.indexOf(variant) + by + 3) % 3] }, replace: true })
    }
    addEventListener("keydown", onKey)
    return () => removeEventListener("keydown", onKey)
  }, [navigate, variant])
  const day = data.day
  const thisMonth = data.overview?.months[0]
  const nextMonth = data.overview?.months[1]

  let body: ReactNode
  if (data.loading) body = <p className="p-4">Loading…</p>
  else if (variant === "a") body = (
    <div className="flex flex-col gap-4 p-4">
      <div role="tablist" className="flex gap-1 overflow-x-auto border-b">{([["home", "Home"], ["rosters", "Rosters"], ["day", "Day view"], ["expiries", "Expiries"]] as const).map(([key, label]) => (
        <button key={key} role="tab" type="button" aria-selected={tab === key} onClick={() => setTab(key)} className={cn("border-b-2 border-transparent px-3 py-2 text-sm whitespace-nowrap text-muted-foreground", tab === key && "border-primary font-medium text-foreground")}>{label}</button>))}
        <span className="self-center px-2 text-xs text-muted-foreground">(tabs stand in for the sidebar)</span></div>
      {tab === "home" && <><h1 className="text-lg font-semibold">Needs you now</h1><NeedsYou data={data} /></>}
      {tab === "rosters" && <><h1 className="text-lg font-semibold">Rosters</h1><Matrix overview={data.overview} /></>}
      {tab === "day" && <><h1 className="text-lg font-semibold">Day view · {day && longDay.format(new Date(`${day.date}T00:00:00Z`))}</h1><DayTable day={day} /></>}
      {tab === "expiries" && <><h1 className="text-lg font-semibold">Expiries</h1><ExpiryTable data={data} /></>}
    </div>
  )
  else if (variant === "b") {
    const drawer = day?.units.find((u) => u.id === open)
    body = (
      <div className="flex flex-col gap-3 p-4">
        <div className="flex flex-wrap items-baseline gap-x-3"><h1 className="text-lg font-semibold">Hospital</h1><span className="text-sm text-muted-foreground">{day && longDay.format(new Date(`${day.date}T00:00:00Z`))}{day?.holiday ? ` · ${day.holiday}` : ""} · {data.waiting.length} waiting for you · {data.expiries?.count ?? 0} expiries</span></div>
        <div className="hidden overflow-x-auto rounded-lg border bg-[var(--ward-surface)] md:block">
          <table className="w-full min-w-max text-sm">
            <thead><tr className="border-b text-xs text-muted-foreground">
              <th className="sticky start-0 bg-[var(--ward-surface)] px-3 py-2 text-start font-medium">Unit</th>
              <th className="px-2 py-2 font-medium">{thisMonth && short(thisMonth)}</th><th className="px-2 py-2 font-medium">{nextMonth && short(nextMonth)}</th>
              {day?.shifts.map((s) => <th key={s.code} className="px-2 py-2 font-medium">{s.display_name} today</th>)}
              <th className="px-2 py-2 font-medium">Leave</th><th className="px-2 py-2 font-medium">Overtime</th><th className="px-2 py-2 font-medium">Expiries</th><th />
            </tr></thead>
            <tbody>{day?.units.map((unit) => {
              const states = data.overview?.units.find((u) => u.id === unit.id)?.states ?? []
              return (
                <tr key={unit.id} onClick={() => setOpen(unit.id)} className={cn("cursor-pointer border-b last:border-0 hover:bg-muted", open === unit.id && "bg-accent")}>
                  <th scope="row" className="sticky start-0 bg-inherit px-3 py-2 text-start font-medium whitespace-nowrap"><button type="button" className="text-start" onClick={() => setOpen(unit.id)}>{unit.name}</button></th>
                  <td className="px-1.5 text-center">{states[0] && <StateChip state={states[0]} compact />}</td><td className="px-1.5 text-center">{states[1] && <StateChip state={states[1]} compact />}</td>
                  {day.shifts.map((s) => { const cell = unit.figures?.shifts.find((c) => c.code === s.code); return <td key={s.code} className="px-2 text-center">{cell ? <Headcount cell={cell} /> : <span className="text-muted-foreground">–</span>}</td> })}
                  <td className="px-2 text-center tabular-nums">{unit.figures?.on_leave.length ?? "–"}</td>
                  <td className="px-2 text-center tabular-nums">{unit.figures ? `${unit.figures.overtime}/${unit.figures.overtime_cap} h` : "–"}</td>
                  <td className="px-2 text-center tabular-nums">{data.expiriesByUnit.get(unit.name) ?? 0}</td>
                  <td className="px-2"><ChevronRightIcon className="size-4 text-muted-foreground" /></td>
                </tr>
              )
            })}</tbody>
          </table>
        </div>
        <ul className="flex flex-col gap-2 md:hidden">{day?.units.map((unit) => {
          const states = data.overview?.units.find((u) => u.id === unit.id)?.states ?? []
          return (
            <li key={unit.id}><button type="button" onClick={() => setOpen(unit.id)} className="flex w-full flex-col gap-2 rounded-lg border bg-[var(--ward-surface)] p-3 text-start">
              <span className="flex items-center gap-2"><b>{unit.name}</b><span className="flex-1" />{states[0] && <StateChip state={states[0]} compact />}</span>
              <span className="flex flex-wrap gap-2 text-xs">{unit.figures?.shifts.map((cell) => <span key={cell.code} className="flex items-center gap-1">{cell.code}<Headcount cell={cell} /></span>) ?? <span className="text-muted-foreground">No roster in force</span>}</span>
            </button></li>
          )
        })}</ul>
        {drawer && day && <UnitDrawer unit={drawer} day={day} overview={data.overview} expiries={data.expiriesByUnit.get(drawer.name) ?? 0} onClose={() => setOpen(null)} />}
      </div>
    )
  }
  else {
    const units = [...(day?.units ?? [])].sort((a, b) => Number(shortUnits(day).includes(b)) - Number(shortUnits(day).includes(a)))
    body = (
      <div className="flex flex-col gap-5 p-4">
        <section className="flex flex-col gap-2"><h2 className="text-sm font-semibold">Waiting for your decision</h2>
          {data.waiting.length === 0 ? <p className="text-sm text-muted-foreground">No roster waits for you.</p> : (
            <div className="flex gap-2 overflow-x-auto pb-1">{data.waiting.map((w) => (
              <Link key={w.unit_id + w.month} to="/unit-roster" search={{ unit: w.unit_id, month: w.month }} className="flex min-w-56 flex-col gap-1 rounded-lg border bg-[var(--ward-surface)] p-3 text-sm">
                <b>{w.unit} · {monthName(w.month)}</b><span className="text-muted-foreground">from {w.submitted_by}</span>{w.changed_since && <span className="text-[var(--ward-warn)]">Changed since it was submitted</span>}<span className="mt-1 font-medium text-primary">Decide</span></Link>))}</div>
          )}
        </section>
        <section className="flex flex-col gap-2"><h2 className="text-sm font-semibold">Today · {day && longDay.format(new Date(`${day.date}T00:00:00Z`))}</h2>
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">{units.map((unit) => {
            const bad = shortUnits(day).includes(unit)
            return (
              <Link key={unit.id} to="/day" className={cn("flex flex-col gap-2 rounded-lg border bg-[var(--ward-surface)] p-3", bad && "border-destructive/60")}>
                <span className="flex items-center gap-2"><b>{unit.name}</b>{bad && <span className="rounded-full bg-[var(--ward-bad-soft)] px-2 text-xs font-medium text-destructive">Short</span>}<span className="flex-1" /><span className="flex items-center gap-1 text-xs text-muted-foreground"><UsersIcon className="size-3.5" />{unit.figures?.shifts.reduce((n, c) => n + c.present, 0) ?? 0} on</span></span>
                {unit.figures ? <span className="flex flex-col gap-1">{unit.figures.shifts.map((cell) => {
                  const need = cell.minimum ?? cell.present
                  return (
                    <span key={cell.code} className="grid grid-cols-[3rem_1fr_auto] items-center gap-2 text-xs">
                      <span>{day!.shifts.find((s) => s.code === cell.code)?.display_name ?? cell.code}</span>
                      <span className="h-2 overflow-hidden rounded-full bg-muted"><span className={cn("block h-full", cell.short ? "bg-destructive" : "bg-primary")} style={{ width: `${Math.min(100, (cell.present / Math.max(need, 1)) * 100)}%` }} /></span>
                      <Headcount cell={cell} />
                    </span>
                  )
                })}</span> : <span className="text-xs text-muted-foreground">No roster in force: <StateChip state={unit.state} compact /></span>}
              </Link>
            )
          })}</div>
        </section>
        <section className="flex flex-col gap-2"><h2 className="text-sm font-semibold">Months</h2><Matrix overview={data.overview} /></section>
      </div>
    )
  }

  return (
    <div className={cn("ward fixed inset-0 z-40 overflow-y-auto bg-background pb-20 text-foreground", dark && "dark")}>
      <header className="sticky top-0 z-20 flex items-center gap-2 border-b bg-[var(--ward-surface)] px-4 py-2.5">
        <span className="grid size-6 place-items-center rounded-[5px] bg-primary text-xs font-bold text-primary-foreground">NR</span><b className="text-sm">Nurse roster</b>
        <span className="flex-1" /><span className="rounded-full bg-muted px-2.5 py-0.5 text-sm">All units</span>
        <span className="grid size-8 place-items-center rounded-full bg-accent text-xs font-semibold text-primary">{(user.full_name || user.username).slice(0, 2).toUpperCase()}</span>
      </header>
      {body}
      <div role="toolbar" aria-label="Prototype controls" className="fixed bottom-4 left-1/2 z-50 flex -translate-x-1/2 items-center gap-0.5 rounded-full bg-neutral-900 p-1 text-[12px] text-white shadow-xl">
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
