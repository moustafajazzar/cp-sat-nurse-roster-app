import { reloadOnStaleBuild } from "@/lib/stale-build"

/** A window as far as the handler needs one: its events, a reload to count, and a session store. */
function aTab(storage: Pick<Storage, "getItem" | "setItem"> = new Map<string, string>() as never) {
  const events = new EventTarget()
  const reload = vi.fn()
  const store = storage instanceof Map
    ? { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => void storage.set(key, value) }
    : storage
  let clock = 1_000_000
  reloadOnStaleBuild({ addEventListener: events.addEventListener.bind(events), location: { reload } }, store, () => clock)
  const staleChunk = () => {
    const event = new Event("vite:preloadError", { cancelable: true })
    events.dispatchEvent(event)
    return event
  }
  return { reload, staleChunk, later: (ms: number) => { clock += ms } }
}

test("a tab from before a deploy that asks for a page's old code loads the new build instead", () => {
  const tab = aTab()

  const event = tab.staleChunk()

  expect(tab.reload).toHaveBeenCalledTimes(1)
  expect(event.defaultPrevented).toBe(true)
})

test("if the new build fails the same way at once, it does not reload again, so a broken deploy cannot loop", () => {
  const tab = aTab()
  tab.staleChunk()
  tab.later(2_000)

  const again = tab.staleChunk()

  expect(tab.reload).toHaveBeenCalledTimes(1)
  expect(again.defaultPrevented).toBe(false)
})

test("a later deploy, minutes on, reloads again", () => {
  const tab = aTab()
  tab.staleChunk()
  tab.later(5 * 60_000)

  tab.staleChunk()

  expect(tab.reload).toHaveBeenCalledTimes(2)
})

test("with no session store (a private window that refuses it) it still reloads", () => {
  const refusing = { getItem: () => { throw new Error("denied") }, setItem: () => { throw new Error("denied") } }
  const tab = aTab(refusing)

  tab.staleChunk()

  expect(tab.reload).toHaveBeenCalledTimes(1)
})
