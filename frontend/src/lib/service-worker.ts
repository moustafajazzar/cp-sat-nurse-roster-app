/** Registers public/sw.js, which is there so the app installs to a phone's home screen and handles no request.
 *  `updateViaCache: "none"` has the browser ask the server for the worker on every check, so a deploy's new one is found at
 *  the next visit. A browser without service workers, or one that refuses (a certificate it does not trust), simply has none:
 *  the app works the same, only not installable. */
export function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js", { scope: "/", updateViaCache: "none" }).catch(() => undefined)
  })
}

/** The service worker's word for "go to this page", posted when a push is tapped while a window of the app is open
 *  (public/sw.js). The window goes there through its own router, so a page with unsaved changes asks first, as for a link. */
const OPEN_PAGE = "open-page"

/** Listens for the taps; returns what stops listening. A path is the app's own, never another site's. */
export function openPagesTappedInPushes(open: (path: string) => void): () => void {
  const worker = typeof navigator === "undefined" ? undefined : navigator.serviceWorker
  if (typeof worker?.addEventListener !== "function") return () => undefined
  const onMessage = (event: MessageEvent) => {
    const { type, path } = (event.data ?? {}) as { type?: unknown; path?: unknown }
    if (type === OPEN_PAGE && typeof path === "string" && path.startsWith("/") && !path.startsWith("//")) open(path)
  }
  worker.addEventListener("message", onMessage)
  return () => worker.removeEventListener("message", onMessage)
}
