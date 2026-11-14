/* PROTOTYPE, throw away. Question: how should the Unit Manager's month be worked on one screen?
   Three variants of the roster workspace on the real /roster route, switched by ?variant=a|b|c and the floating bar:
   A "Side panel"    rail sidebar, toolbar with inline solve progress, grid beside a panel of breaks / waiting / versions,
                     cover totals pinned under the grid.
   B "Bottom dock"   expanded sidebar, solve progress as a banner, cover totals pinned above the nurses, breaks and waiting
                     items in a dock under the grid.
   C "Check, then submit"  rail sidebar, the grid takes the whole workspace with breaks marked on cells only; a review sheet
                     (safety, fairness, waiting) holds Submit.
   Real month data from the seeded stack; the solve, submit and waiting counts are stubs. Read-only: cell edits work on the
   normal page. Uses the decided shell (grouped sidebar, slim top bar) and "Ward" tokens (index.css .ward). */
import { Link, useNavigate } from "@tanstack/react-router"
import {
  BellIcon, BookIcon, CalendarDaysIcon, CalendarIcon, ChevronLeftIcon, ChevronRightIcon, ClipboardListIcon, HistoryIcon, HomeIcon,
  InboxIcon, LayoutGridIcon, ListChecksIcon, PanelLeftIcon, PlayIcon, RepeatIcon, SearchIcon, ShieldIcon, SquareIcon, TagIcon, UsersIcon, XIcon,
} from "lucide-react"
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react"

import { useGetRosterMonth } from "@/api/generated/endpoints"
import type { Draft, GridViolation, Me, RosterMonth } from "@/api/generated/model"
import { type CellMarks, RosterGrid } from "@/components/roster-grid"
import { RosterVersions } from "@/components/roster-versions"
import { Button } from "@/components/ui/button"
import { cn, monthName } from "@/lib/utils"

type Variant = "a" | "b" | "c"
const NAMES: Record<Variant, string> = { a: "Side panel", b: "Bottom dock", c: "Check, then submit" }
const KEYS: Variant[] = ["a", "b", "c"]
const STATE_SAYS: Record<RosterMonth["state"], [string, string]> = {
  none: ["No roster yet", "bg-muted text-muted-foreground"],
  draft: ["Draft", "bg-[var(--ward-warn-soft)] text-[var(--ward-warn)]"],
  submitted: ["Waiting for a decision", "bg-accent text-primary"],
  approved: ["In force", "bg-[var(--ward-ok-soft)] text-[var(--ward-ok)]"],
}

const shiftMonth = (month: string, by: number) => {
  const [y, m] = month.split("-").map(Number)
  const d = new Date(Date.UTC(y, m - 1 + by, 1))
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`
}
const dayOf = (date: string) => Number(date.slice(8, 10))

/* ---------- the decided shell ---------- */
type NavItem = { to: string; label: string; icon: ReactNode; current?: boolean; count?: number }
function Sidebar({ rail, hasProfile }: { rail: boolean; hasProfile: boolean }) {
  const groups: [string | null, NavItem[]][] = [
    [null, [{ to: "/", label: "Home", icon: <HomeIcon /> }]],
    ...(hasProfile ? [["My work", [{ to: "/my-roster", label: "My roster", icon: <CalendarIcon /> }, { to: "/my-month", label: "My month", icon: <CalendarDaysIcon /> }]] as [string, NavItem[]]] : []),
    ["Rostering", [
      { to: "/roster", label: "Roster", icon: <LayoutGridIcon />, current: true }, { to: "/requests", label: "Requests", icon: <InboxIcon />, count: 4 },
      { to: "/unit-swaps", label: "Swaps", icon: <RepeatIcon />, count: 2 }, { to: "/unit-covers", label: "Covers", icon: <ShieldIcon /> },
      { to: "/solve-history", label: "Solve history", icon: <HistoryIcon /> }]],
    ["Unit setup", [
      { to: "/unit-rules", label: "Unit rules", icon: <ListChecksIcon /> }, { to: "/nurses", label: "Nurses", icon: <UsersIcon /> },
      { to: "/rostering-attributes", label: "Rostering attributes", icon: <TagIcon /> }, { to: "/leave", label: "Leave", icon: <ClipboardListIcon /> }]],
    ["Reference", [
      { to: "/shifts", label: "Shifts", icon: <BookIcon /> }, { to: "/leave-types", label: "Leave types", icon: <BookIcon /> },
      { to: "/skills", label: "Skills", icon: <BookIcon /> }, { to: "/holidays", label: "Holidays and Ramadan", icon: <BookIcon /> }]],
  ]
  return (
    <aside aria-label="Main" className={cn("flex shrink-0 flex-col gap-3 overflow-y-auto border-e bg-[var(--ward-surface)] py-3", rail ? "w-14 items-center px-2" : "w-56 px-2.5")}>
      <div className={cn("flex items-center gap-2 px-1.5 pb-1 font-semibold", rail && "px-0")}>
        <span className="grid size-6 place-items-center rounded-[5px] bg-primary text-xs font-bold text-primary-foreground">NR</span>
        {!rail && <span>Nurse roster</span>}
      </div>
      {groups.map(([name, items], index) => (
        <div key={name ?? index} className={cn("flex flex-col gap-px", rail && "w-full items-center border-t pt-2")}>
          {name && !rail && <div className="px-2 py-1 text-[11px] font-medium tracking-wider text-muted-foreground uppercase">{name}</div>}
          {items.map((item) => (
            <Link key={item.to} to={item.to} title={rail ? item.label : undefined} aria-current={item.current ? "page" : undefined}
              className={cn("flex min-h-8 items-center gap-2.5 rounded-[5px] px-2 py-1.5 text-sm text-muted-foreground hover:bg-muted hover:text-foreground [&_svg]:size-4 [&_svg]:shrink-0",
                item.current && "bg-accent font-medium text-foreground", rail && "justify-center px-0 w-10")}>
              {item.icon}
              {rail ? <span className="sr-only">{item.label}</span> : <span className="truncate">{item.label}</span>}
              {!rail && item.count && <span className="ms-auto rounded-full bg-muted px-1.5 text-[11px] tabular-nums">{item.count}</span>}
            </Link>
          ))}
        </div>
      ))}
    </aside>
  )
}

function TopBar({ unit, month, variant, user }: { unit: string; month: string; variant: Variant; user: Me }) {
  return (
    <header className="flex flex-wrap items-center gap-2.5 border-b bg-[var(--ward-surface)] px-4 py-2">
      <button type="button" className="flex min-w-56 items-center gap-2 rounded-[5px] border bg-background px-2.5 py-1 text-sm text-muted-foreground">
        <SearchIcon className="size-4" />Search pages and actions<kbd className="ms-auto rounded border px-1 font-mono text-[11px]">Ctrl K</kbd>
      </button>
      <span className="flex-1" />
      <span className="rounded-full bg-muted px-2.5 py-0.5 text-sm font-medium">{unit}</span>
      <span className="flex items-center gap-1 text-sm font-medium">
        <Link to="/roster" search={{ month: shiftMonth(month, -1), variant }} aria-label="Previous month" className="grid size-8 place-items-center rounded-[5px] text-muted-foreground hover:bg-muted"><ChevronLeftIcon className="size-4" /></Link>
        <span className="min-w-28 text-center">{monthName(month)}</span>
        <Link to="/roster" search={{ month: shiftMonth(month, 1), variant }} aria-label="Next month" className="grid size-8 place-items-center rounded-[5px] text-muted-foreground hover:bg-muted"><ChevronRightIcon className="size-4" /></Link>
      </span>
      <button type="button" aria-label="Notifications" className="relative grid size-8 place-items-center rounded-[5px] text-muted-foreground hover:bg-muted">
        <BellIcon className="size-4" /><span className="absolute end-1.5 top-1.5 size-2 rounded-full bg-destructive" />
      </button>
      <span className="grid size-8 place-items-center rounded-full bg-accent text-xs font-semibold text-primary" title={user.username}>{(user.full_name || user.username).slice(0, 2).toUpperCase()}</span>
    </header>
  )
}

/* ---------- stub solve ---------- */
function useStubSolve(base: number | null) {
  const [run, setRun] = useState<{ elapsed: number; cost: number; improvements: number } | null>(null)
  useEffect(() => {
    if (!run) return
    if (run.elapsed >= 20) { const done = setTimeout(() => setRun(null), 1500); return () => clearTimeout(done) }
    const tick = setTimeout(() => setRun((r) => r && ({ elapsed: r.elapsed + 1, cost: Math.max(base ?? 1000, Math.round(r.cost * 0.97)), improvements: r.improvements + (Math.random() < 0.5 ? 1 : 0) })), 1000)
    return () => clearTimeout(tick)
  }, [run, base])
  return { run, start: () => setRun({ elapsed: 0, cost: Math.round((base ?? 1000) * 1.5), improvements: 0 }), stop: () => setRun(null) }
}
type Solve = ReturnType<typeof useStubSolve>

function SolveInline({ solve, limit = 20 }: { solve: Solve; limit?: number }) {
  if (!solve.run) return <Button size="sm" onClick={solve.start}><PlayIcon />Solve again</Button>
  return (
    <span role="status" className="flex items-center gap-2 rounded-[5px] border bg-[var(--ward-surface)] px-2 py-1 text-sm">
      <span className="h-1.5 w-24 overflow-hidden rounded-full bg-muted"><span className="block h-full bg-primary transition-[width]" style={{ width: `${(solve.run.elapsed / limit) * 100}%` }} /></span>
      <span className="tabular-nums">{solve.run.elapsed}s · cost {solve.run.cost.toLocaleString()}</span>
      <Button size="sm" variant="outline" onClick={solve.stop}>Stop</Button>
    </span>
  )
}

/* ---------- breaks, cover, waiting ---------- */
function exampleBreaks(draft: Draft): GridViolation[] {
  const rows = draft.rows.filter((row) => !row.on_probation)
  const pick = (r: number, d: number) => ({ row: rows[r % rows.length], day: draft.days[d % draft.days.length] })
  const a = pick(2, 4), b = pick(7, 11), c = pick(11, 18)
  return [
    { rule: "cover", hospital_id: null, full_name: "", date: draft.days[5].date, text: `Night short on ${dayOf(draft.days[5].date)}: 2 of 3 nurses (example)` },
    { rule: "rest", hospital_id: a.row.hospital_id, full_name: a.row.full_name, date: a.day.date, text: `${a.row.full_name}: Night then Day, needs 11 h rest (example)` },
    { rule: "nights", hospital_id: b.row.hospital_id, full_name: b.row.full_name, date: b.day.date, text: `${b.row.full_name}: 4 nights in a row, preference is 3 (example, soft)` },
    { rule: "hours", hospital_id: c.row.hospital_id, full_name: c.row.full_name, date: c.day.date, text: `${c.row.full_name}: 156 h of 144 contracted (example, soft)` },
  ]
}
const isSoft = (violation: GridViolation) => /soft/.test(violation.text)

function coverRows(draft: Draft, short: Set<string>, compact: boolean) {
  const counted = draft.rows.filter((row) => !row.on_probation)
  return draft.shifts.map((shift) => (
    <tr key={shift.code} className="text-xs text-muted-foreground">
      <th scope="row" className="sticky start-0 z-10 border-b bg-muted px-2 py-1 text-start font-medium">{shift.display_name} on duty</th>
      {draft.days.map((day, index) => {
        const n = counted.filter((row) => row.cells[index]?.shift === shift.code).length
        return <td key={day.date} className={cn("border-b border-s bg-muted text-center tabular-nums", compact ? "h-6" : "h-7", short.has(day.date) && shift.code.startsWith("N") && "bg-[var(--ward-bad-soft)] font-bold text-destructive")}>{n}</td>
      })}
    </tr>
  ))
}

function BreakList({ breaks, example, onJump }: { breaks: GridViolation[]; example: boolean; onJump: (violation: GridViolation) => void }) {
  return (
    <div className="flex flex-col">
      {example && <p className="border-b px-3 py-2 text-xs text-muted-foreground">This roster breaks nothing; these are examples to judge the layout.</p>}
      {breaks.map((violation, index) => (
        <button key={index} type="button" onClick={() => onJump(violation)}
          className="grid grid-cols-[auto_1fr] items-start gap-x-2.5 border-b px-3 py-2.5 text-start text-sm hover:bg-muted">
          <span className={cn("mt-1.5 size-2 rounded-full", isSoft(violation) ? "bg-[var(--ward-warn)]" : "bg-destructive")} />
          <span>{violation.text}<span className="block text-xs text-muted-foreground">{violation.date ? `Day ${dayOf(violation.date)}` : "Whole month"}{violation.hospital_id ? " · jump to the cell" : " · jump to the day"}</span></span>
        </button>
      ))}
    </div>
  )
}

function Waiting() {
  const items: [string, string, number][] = [["/requests", "Requests waiting for an answer", 4], ["/unit-swaps", "Swaps to approve", 2], ["/unit-covers", "Covers open", 1]]
  return (
    <div className="flex flex-col">
      <p className="border-b px-3 py-2 text-xs text-muted-foreground">Counts are examples.</p>
      {items.map(([to, label, n]) => (
        <Link key={to} to={to} className="flex items-center gap-2 border-b px-3 py-2.5 text-sm hover:bg-muted">
          {label}<span className="ms-auto rounded-full bg-[var(--ward-warn-soft)] px-2 text-xs font-medium text-[var(--ward-warn)] tabular-nums">{n}</span>
        </Link>
      ))}
    </div>
  )
}

function Tabs({ tabs, active, onChange }: { tabs: [string, string][]; active: string; onChange: (key: string) => void }) {
  return (
    <div role="tablist" className="flex gap-1 border-b px-2">
      {tabs.map(([key, label]) => (
        <button key={key} role="tab" aria-selected={active === key} type="button" onClick={() => onChange(key)}
          className={cn("border-b-2 border-transparent px-2.5 py-2 text-sm text-muted-foreground", active === key && "border-primary font-medium text-foreground")}>{label}</button>
      ))}
    </div>
  )
}

/* ---------- the workspace ---------- */
export function WorkspacePrototype({ unit, month: inAddress, variant, user }: { unit: { id: string; name: string }; month: string | undefined; variant: Variant; user: Me }) {
  const navigate = useNavigate()
  const roster = useGetRosterMonth(unit.id, inAddress ? { month: inAddress } : undefined)
  const answer = roster.data
  const draft = answer?.draft ?? null
  const [dark, setDark] = useState(() => matchMedia("(prefers-color-scheme: dark)").matches)
  const [railOverride, setRailOverride] = useState<boolean | null>(null)
  const rail = railOverride ?? variant !== "b"
  const [tab, setTab] = useState("breaks")
  const [dockOpen, setDockOpen] = useState(true)
  const [sheet, setSheet] = useState(false)
  const [compact, setCompact] = useState(true)
  const [selected, setSelected] = useState<string | null>(null)
  const solve = useStubSolve(draft?.cost ?? null)

  const realBreaks = draft?.check?.violations ?? []
  const example = !!draft && realBreaks.length === 0
  const breaks = useMemo(() => (draft ? (realBreaks.length ? realBreaks : exampleBreaks(draft)) : []), [draft, realBreaks])
  const marked = useMemo(() => new Set(breaks.filter((v) => v.hospital_id && v.date).map((v) => `${v.hospital_id}|${v.date}`)), [breaks])
  const shortDays = useMemo(() => new Set(breaks.filter((v) => !v.hospital_id && v.date).map((v) => v.date!)), [breaks])
  const hard = breaks.filter((v) => !isSoft(v)).length

  const jump = (violation: GridViolation) => {
    const target = violation.hospital_id
      ? document.querySelector<HTMLButtonElement>(`[data-hid="${CSS.escape(violation.hospital_id)}"][data-date="${violation.date}"]`)
      : document.querySelector<HTMLButtonElement>(`[data-date="${violation.date}"]`)
    target?.scrollIntoView({ block: "center", inline: "center", behavior: "smooth" })
    target?.focus({ preventScroll: true })
    if (violation.hospital_id) setSelected(`${violation.hospital_id}|${violation.date}`)
  }

  // Column budget: how many of the month's days the grid shows without scrolling sideways.
  const gridBox = useRef<HTMLDivElement>(null)
  const [fit, setFit] = useState("")
  useEffect(() => {
    const measure = () => {
      const scroller = gridBox.current?.querySelector<HTMLElement>("[role=region], .overflow-auto")
      const table = scroller?.querySelector("table")
      if (!scroller || !table || !draft) return setFit("")
      const name = table.querySelector("thead th")?.getBoundingClientRect().width ?? 0
      const col = table.querySelectorAll("thead th")[1]?.getBoundingClientRect().width ?? 1
      const shown = Math.min(draft.days.length, Math.floor((scroller.clientWidth - name) / col))
      setFit(`${window.innerWidth} px wide · grid shows ${shown} of ${draft.days.length} days${shown < draft.days.length ? " (scrolls)" : ""}`)
    }
    measure()
    const observer = new ResizeObserver(measure)
    if (gridBox.current) observer.observe(gridBox.current)
    addEventListener("resize", measure)
    return () => { observer.disconnect(); removeEventListener("resize", measure) }
  }, [draft, variant, rail, compact, dockOpen, sheet])

  // ← → switch variants, except where those keys belong to the page.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.target as HTMLElement).closest("input, textarea, select, [contenteditable], table, [role=tablist]")) return
      const by = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0
      if (!by) return
      setRailOverride(null)
      void navigate({ to: "/roster", search: { month: inAddress, variant: KEYS[(KEYS.indexOf(variant) + by + 3) % 3] }, replace: true })
    }
    addEventListener("keydown", onKey)
    return () => removeEventListener("keydown", onKey)
  }, [navigate, inAddress, variant])

  const marks = (hid: string, date: string): CellMarks => ({ refused: marked.has(`${hid}|${date}`), pinned: selected === `${hid}|${date}` ? "selected" : undefined })
  const month = answer?.month ?? inAddress ?? ""
  const [stateSays, stateClass] = STATE_SAYS[answer?.state ?? "none"]

  const grid = draft && (
    <div ref={gridBox} className="h-full min-h-0 min-w-0">
      <RosterGrid grid={draft} label={`Roster of ${unit.name}, ${monthName(month)}`} extraShifts={draft.editor.shifts} compact={compact} fill
        editing={{ marks, onOpen: (hid, date) => setSelected(`${hid}|${date}`) }}
        headRows={variant === "b" ? coverRows(draft, shortDays, compact) : undefined}
        footRows={variant === "a" ? coverRows(draft, shortDays, compact) : variant === "c" ? coverRows(draft, shortDays, compact).slice(-1) : undefined} />
    </div>
  )
  const selectedBreaks = selected ? breaks.filter((v) => `${v.hospital_id}|${v.date}` === selected) : []

  const title = (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <h1 className="text-lg font-semibold">Roster</h1>
      <span className={cn("rounded-full px-2 py-0.5 text-xs font-medium", stateClass)}>{stateSays}</span>
      {draft && <span className="text-sm text-muted-foreground tabular-nums">cost {draft.cost?.toLocaleString() ?? "–"}{draft.edited ? " · edited by hand" : ""}</span>}
    </div>
  )

  let body: ReactNode
  if (roster.isPending) body = <p className="p-4">Loading…</p>
  else if (!draft) body = <p className="p-4">No roster for {monthName(month)} in the seeded data. Try another month with the arrows above.</p>
  else if (variant === "a") body = (
    <div className="flex min-h-0 flex-1 flex-col gap-3 p-3">
      <div className="flex flex-wrap items-center gap-2">{title}<span className="flex-1" /><SolveInline solve={solve} /><Button size="sm" variant="outline">Compare versions</Button><Button size="sm" variant="outline">Submit…</Button></div>
      <div className="flex min-h-0 flex-1 gap-3">
        <div className="min-h-0 min-w-0 flex-1">{grid}</div>
        <aside className="flex w-72 shrink-0 flex-col overflow-hidden rounded-lg border bg-[var(--ward-surface)]">
          <Tabs tabs={[["breaks", `Breaks ${breaks.length}`], ["waiting", "Waiting 7"], ["versions", "Versions"]]} active={tab} onChange={setTab} />
          <div className="min-h-0 flex-1 overflow-y-auto">
            {selectedBreaks.length > 0 && tab === "breaks" && <p className="border-b bg-accent px-3 py-2 text-xs">Selected cell breaks {selectedBreaks.length} rule{selectedBreaks.length > 1 ? "s" : ""}.</p>}
            {tab === "breaks" && <BreakList breaks={breaks} example={example} onJump={jump} />}
            {tab === "waiting" && <Waiting />}
            {tab === "versions" && (answer?.state === "approved" ? <div className="p-3"><RosterVersions unitId={unit.id} month={month} /></div> : <p className="p-3 text-sm text-muted-foreground">Versions start once the month is approved.</p>)}
          </div>
        </aside>
      </div>
    </div>
  )
  else if (variant === "b") body = (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b px-4 py-2.5">{title}<span className="flex-1" />
        {!solve.run && <Button size="sm" onClick={solve.start}><PlayIcon />Solve again</Button>}<Button size="sm" variant="outline">Compare versions</Button><Button size="sm" variant="outline">Submit…</Button></div>
      {solve.run && (
        <div role="status" className="flex items-center gap-3 border-b bg-accent px-4 py-2 text-sm">
          <span className="font-medium">Solving December</span>
          <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-background"><span className="block h-full bg-primary transition-[width]" style={{ width: `${(solve.run.elapsed / 20) * 100}%` }} /></span>
          <span className="tabular-nums">{solve.run.elapsed}s of 20s · best cost {solve.run.cost.toLocaleString()} · {solve.run.improvements} improvements</span>
          <Button size="sm" variant="outline" onClick={solve.stop}>Stop</Button>
        </div>
      )}
      <div className="min-h-0 flex-1 p-3">{grid}</div>
      <section className={cn("flex shrink-0 flex-col border-t bg-[var(--ward-surface)]", dockOpen ? "h-56" : "h-10")}>
        <div className="flex items-center">
          <Tabs tabs={[["breaks", `Breaks ${breaks.length}`], ["waiting", "Waiting 7"], ["versions", "Versions"]]} active={tab} onChange={(key) => { setTab(key); setDockOpen(true) }} />
          <span className="flex-1" />
          <button type="button" className="px-3 text-sm text-muted-foreground" onClick={() => setDockOpen(!dockOpen)}>{dockOpen ? "Hide" : "Show"}</button>
        </div>
        {dockOpen && <div className="min-h-0 flex-1 overflow-y-auto">
          {tab === "breaks" && <div className="grid grid-cols-1 md:grid-cols-2"><BreakList breaks={breaks} example={example} onJump={jump} /></div>}
          {tab === "waiting" && <Waiting />}
          {tab === "versions" && (answer?.state === "approved" ? <div className="p-3"><RosterVersions unitId={unit.id} month={month} /></div> : <p className="p-3 text-sm text-muted-foreground">Versions start once the month is approved.</p>)}
        </div>}
      </section>
    </div>
  )
  else body = (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-2 px-3 pt-3 pb-2">{title}
        <button type="button" onClick={() => setSheet(true)} className="rounded-full bg-[var(--ward-bad-soft)] px-2.5 py-0.5 text-xs font-medium text-destructive">{hard} break a rule · {breaks.length - hard} soft</button>
        <span className="flex-1" /><SolveInline solve={solve} /><Button size="sm" onClick={() => setSheet(true)}>Review and submit</Button></div>
      <div className="min-h-0 flex-1 px-3 pb-3">{grid}</div>
      {sheet && (
        <aside aria-label="Review before submitting" className="absolute inset-y-0 end-0 z-30 flex w-[26rem] max-w-full flex-col border-s bg-[var(--ward-surface)] shadow-xl">
          <div className="flex items-center gap-2 border-b px-4 py-3"><h2 className="font-semibold">Review before submitting</h2><span className="flex-1" />
            <button type="button" aria-label="Close" onClick={() => setSheet(false)} className="grid size-8 place-items-center rounded-[5px] hover:bg-muted"><XIcon className="size-4" /></button></div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {([["Safety", breaks.filter((v) => !isSoft(v)), "Hard rules and cover. Submit is refused while any is broken."], ["Fairness", breaks.filter(isSoft), "Soft rules: preferences and hours."]] as const).map(([heading, list, says]) => (
              <section key={heading} className="border-b">
                <div className="flex items-center gap-2 px-4 pt-3"><SquareIcon className={cn("size-3", list.length ? "fill-destructive text-destructive" : "fill-[var(--ward-ok)] text-[var(--ward-ok)]")} /><h3 className="font-medium">{heading}</h3><span className="text-sm text-muted-foreground">{list.length ? `${list.length} to look at` : "all clear"}</span></div>
                <p className="px-4 pb-1 text-xs text-muted-foreground">{says}</p>
                <BreakList breaks={[...list]} example={false} onJump={(v) => { setSheet(false); setTimeout(() => jump(v), 50) }} />
              </section>
            ))}
            <section className="border-b"><div className="px-4 pt-3 font-medium">Waiting on you</div><Waiting /></section>
          </div>
          <div className="flex items-center gap-2 border-t px-4 py-3">
            <span className="text-sm text-muted-foreground">{example ? "Example breaks: the real roster has none." : ""}</span><span className="flex-1" />
            <Button disabled={hard > 0 && !example}>Submit for approval</Button>
          </div>
        </aside>
      )}
    </div>
  )

  return (
    <div className={cn("ward fixed inset-0 z-40 flex bg-background text-foreground", dark && "dark")}>
      <Sidebar rail={rail} hasProfile={user.has_nurse_profile} />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar unit={unit.name} month={month} variant={variant} user={user} />
        {body}
      </div>
      <div role="toolbar" aria-label="Prototype controls" className="fixed bottom-4 left-1/2 z-50 flex -translate-x-1/2 flex-wrap items-center justify-center gap-1 rounded-full bg-neutral-900 p-1.5 text-[13px] text-white shadow-2xl">
        {(["←", "→"] as const).map((arrow) => (
          <Link key={arrow} to="/roster" replace search={{ month: inAddress, variant: KEYS[(KEYS.indexOf(variant) + (arrow === "→" ? 1 : 2)) % 3] }} onClick={() => setRailOverride(null)}
            className={cn("rounded-full px-3 py-1.5 hover:bg-neutral-700", arrow === "→" && "order-2")} aria-label={arrow === "→" ? "Next variant" : "Previous variant"}>{arrow}</Link>
        ))}
        <span className="order-1 min-w-48 px-2 text-center">{variant.toUpperCase()} · {NAMES[variant]}</span>
        <span className="order-3 mx-1 h-5 w-px bg-neutral-600" />
        <button type="button" className="order-3 rounded-full px-3 py-1.5 hover:bg-neutral-700" onClick={() => setRailOverride(!rail)}><PanelLeftIcon className="me-1 inline size-3.5" />{rail ? "Expand sidebar" : "Rail"}</button>
        <button type="button" className="order-3 rounded-full px-3 py-1.5 hover:bg-neutral-700" onClick={() => setCompact(!compact)}>{compact ? "Compact days" : "Today's widths"}</button>
        <button type="button" className="order-3 rounded-full px-3 py-1.5 hover:bg-neutral-700" onClick={() => setDark(!dark)}>{dark ? "Dark" : "Light"}</button>
        <span className="order-4 px-2 text-[11px] text-neutral-300 tabular-nums">{fit}</span>
      </div>
    </div>
  )
}
