const LAST_RELOAD = "stale-build-reloaded-at"
/** Long enough that the new build has loaded and failed again if it is itself broken; short beside the time between deploys. */
const AGAIN_AFTER_MS = 10_000

type Tab = { addEventListener: Window["addEventListener"]; location: Pick<Location, "reload"> }
type Store = Pick<Storage, "getItem" | "setItem">

/** Each page's code is a file named by its content, and a deploy replaces them all. A tab opened before the deploy,
 *  an installed app above all, still asks for a page's code by its old name, which the server no longer has; Vite says so with
 *  `vite:preloadError`. Then the tab loads again, once, and runs the new build. If that fails the same way within seconds, the
 *  deploy itself is broken and the error is left to show, rather than reloading for ever. */
export function reloadOnStaleBuild(tab: Tab = window, store: Store = sessionStorage, now: () => number = Date.now) {
  tab.addEventListener("vite:preloadError", (event) => {
    let last = 0
    try { last = Number(store.getItem(LAST_RELOAD) ?? 0) } catch { /* no session store: reload all the same */ }
    if (now() - last < AGAIN_AFTER_MS) return
    try { store.setItem(LAST_RELOAD, String(now())) } catch { /* as above */ }
    event.preventDefault()
    tab.location.reload()
  })
}
