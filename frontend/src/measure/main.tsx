/* MEASUREMENT ONLY. Opens on a phone, runs the roster grid through arrow keys, an edit and a mount, today's grid against the
   memoised one, at 20 and 64 nurses × 31 days, and shows the medians. Nothing leaves the page: read the numbers off the screen.
   The parent is wired the way roster.tsx feeds the grid: marks close over a pins map and a pending-edits record, and opening
   a cell sets parent state. */
import "@fontsource-variable/geist"
import "../index.css"

import { useState } from "react"
import { createRoot, type Root } from "react-dom/client"
import { flushSync } from "react-dom"

import type { GridView } from "@/api/generated/model"
import { type CellMarks, RosterGrid } from "@/components/roster-grid"
import { RosterGridMemo } from "./grid-memo"

const SHIFTS = [["D", "Day", "#0284c7", 12], ["N", "Night", "#4338ca", 12], ["A", "Morning", "#16a34a", 8], ["B", "Evening", "#d97706", 8], ["C", "Late night", "#7c3aed", 8]] as const
const PATTERN = ["DDNN..L", "NN..DDD", "..DDNN.", "D..NNN.", "BBA..AA", "AABB..C", ".CCC..B"]

function makeGrid(rows: number): GridView {
  const days = Array.from({ length: 31 }, (_, i) => { const date = `2026-12-${String(i + 1).padStart(2, "0")}`; const wd = new Date(`${date}T00:00:00Z`).getUTCDay(); return { date, weekend: wd === 5 || wd === 6, holiday: false } })
  return {
    days, shifts: SHIFTS.map(([code, display_name, colour, hours]) => ({ code, display_name, colour, hours })),
    rows: Array.from({ length: rows }, (_, r) => ({
      hospital_id: `n${r}`, full_name: `Nurse ${String(r + 1).padStart(2, "0")}`, on_probation: false, preceptor_name: null,
      cells: days.map((_, d) => { const ch = PATTERN[r % 7][(d + r) % 7]; return ch === "L" ? { shift: "", kind: "leave" as const } : { shift: ch === "." ? "" : ch, kind: ch === "." ? "off" as const : "shift" as const } }),
    })),
  }
}

type Impl = "today" | "memo"
type Setup = { impl: Impl; rows: number; grid: GridView }

let setPending: (p: Record<string, string>) => void = () => {}
let setOpen: (k: string | null) => void = () => {}

function Bench({ setup }: { setup: Setup }) {
  const [pending, _setPending] = useState<Record<string, string>>({})
  const [open, _setOpen] = useState<string | null>(null)
  setPending = _setPending
  setOpen = _setOpen
  const pins = new Map(setup.grid.rows.slice(0, 10).map((row, i) => [`${row.hospital_id}|${setup.grid.days[i * 3].date}`, "pinned"]))
  const marks = (hid: string, date: string): CellMarks => {
    const key = `${hid}|${date}`
    const edit = pending[key]
    return { pinned: pins.get(key), pending: edit !== undefined ? { shows: edit, says: "to be pinned, not saved" } : undefined, refused: open === key }
  }
  const onOpen = (hid: string, date: string) => _setOpen(`${hid}|${date}`)
  return setup.impl === "today"
    ? <RosterGrid grid={setup.grid} label="Roster" editing={{ marks, onOpen }} />
    : <RosterGridMemo grid={setup.grid} label="Roster" marks={marks} onOpen={onOpen} />
}

// A hidden page gets no animation frames: give up on one after a second, and show "–" rather than hang.
const frame = () => Promise.race([
  new Promise<number>((resolve) => requestAnimationFrame(() => setTimeout(() => resolve(performance.now()), 0))),
  new Promise<number>((resolve) => setTimeout(() => resolve(NaN), 1000)),
])
const median = (xs: number[]) => { const s = [...xs].sort((a, b) => a - b); return s[Math.floor(s.length / 2)] }
const settle = () => new Promise((r) => setTimeout(r, 150))

async function measure(host: HTMLElement, setup: Setup, presses: number) {
  let root: Root | null = createRoot(host)
  // Mount: five times, the first dropped.
  const mount: number[][] = []
  for (let i = 0; i < 5; i++) {
    root?.unmount(); root = createRoot(host); await settle()
    const t0 = performance.now()
    flushSync(() => root!.render(<Bench setup={setup} />))
    const t1 = performance.now(); const t2 = await frame()
    if (i > 0) mount.push([t1 - t0, t2 - t0])
  }
  await settle()
  // Arrow keys: alternate right and left from the first cell; time to focus moving, and to the next frame.
  const first = host.querySelector<HTMLButtonElement>("[role=grid] button")!
  first.focus()
  const script: number[] = [], toFrame: number[] = []
  for (let i = 0; i < presses + 2; i++) {
    const from = document.activeElement as HTMLElement
    const moved = new Promise<number>((resolve) => host.addEventListener("focusin", () => resolve(performance.now()), { once: true }))
    const t0 = performance.now()
    from.dispatchEvent(new KeyboardEvent("keydown", { key: i % 2 === 0 ? "ArrowRight" : "ArrowLeft", bubbles: true, cancelable: true }))
    const t1 = await Promise.race([moved, new Promise<number>((r) => setTimeout(() => r(NaN), 500))])
    const t2 = await frame()
    if (i >= 2) { script.push(t1 - t0); toFrame.push(t2 - t0) }
  }
  // An edit waiting and an opened cell: parent state, as the page sets it.
  const edit: number[][] = []
  for (let i = 0; i < 9; i++) {
    const t0 = performance.now()
    flushSync(() => { if (i % 2) setOpen(`n${i}|2026-12-0${(i % 9) + 1}`); else setPending({ [`n${i}|2026-12-1${i % 9}`]: "D" }) })
    const t1 = performance.now(); const t2 = await frame()
    if (i > 0) edit.push([t1 - t0, t2 - t0])
  }
  root?.unmount()
  return {
    mountCommit: median(mount.map((m) => m[0])), mountFrame: median(mount.map((m) => m[1])),
    arrowScript: median(script), arrowFrame: median(toFrame),
    editCommit: median(edit.map((m) => m[0])), editFrame: median(edit.map((m) => m[1])),
  }
}

type Result = Awaited<ReturnType<typeof measure>> & { impl: Impl; rows: number }

function App() {
  const [results, setResults] = useState<Result[]>([])
  const [running, setRunning] = useState("")
  const run = async () => {
    setResults([])
    const host = document.getElementById("bench")!
    const out: Result[] = []
    for (const rows of [20, 64]) for (const impl of ["today", "memo"] as Impl[]) {
      setRunning(`${impl === "today" ? "Today's grid" : "Memoised grid"}, ${rows} nurses…`)
      await settle()
      out.push({ impl, rows, ...(await measure(host, { impl, rows, grid: makeGrid(rows) }, 25)) })
      setResults([...out])
    }
    setRunning("Done. Read the table, or screenshot it.")
  }
  const f = (n: number) => (Number.isNaN(n) ? "–" : n.toFixed(1))
  return (
    <div className="flex flex-col gap-3 p-3 text-sm">
      <h1 className="text-lg font-semibold">Roster grid on this phone</h1>
      <p className="text-muted-foreground">Times in ms, medians. 31 days. "Today" windows rows above 60; "memoised" draws every row. Keep the screen on and don't touch it while it runs (about a minute).</p>
      <p className="font-mono text-xs break-all">{navigator.userAgent}</p>
      <button type="button" onClick={run} disabled={!!running && !running.startsWith("Done")} className="min-h-11 rounded-md bg-primary px-4 font-medium text-primary-foreground disabled:opacity-50">Run</button>
      <p role="status">{running}</p>
      {results.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-xs tabular-nums">
            <thead><tr className="border-b text-start"><th className="p-1 text-start">Grid</th><th className="p-1">Rows</th><th className="p-1">Arrow script</th><th className="p-1">Arrow frame</th><th className="p-1">Edit commit</th><th className="p-1">Edit frame</th><th className="p-1">Mount commit</th><th className="p-1">Mount frame</th></tr></thead>
            <tbody>{results.map((r) => (
              <tr key={r.impl + r.rows} className="border-b"><td className="p-1">{r.impl}</td><td className="p-1 text-center">{r.rows}</td>
                {[r.arrowScript, r.arrowFrame, r.editCommit, r.editFrame, r.mountCommit, r.mountFrame].map((n, i) => <td key={i} className="p-1 text-center">{f(n)}</td>)}</tr>
            ))}</tbody>
          </table>
        </div>
      )}
      <div id="bench" className="max-h-[40svh] overflow-hidden" />
    </div>
  )
}

createRoot(document.getElementById("root")!).render(<App />)
