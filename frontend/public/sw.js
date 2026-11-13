// The service worker, there so the app installs to a phone's home screen, so a device keeps its pushes when the
// browser renews its subscription, and to show a push and open its page when it is tapped. It keeps
// nothing offline. It has no `fetch` listener on purpose: every
// request, the page itself, the API, a roster export, a print sheet, a backup, goes to the network exactly as it would
// without a worker, under the server's own Cache-Control (`no-store` on the files, `no-cache` on the page). Never add one
// without keeping to the list above of what must never be cached.
//
// Served at /sw.js with `no-cache` (backend/config/settings.py) and registered with updateViaCache "none" (src/main.tsx), so a
// deploy's new worker is found at the next visit. It takes over at once, and removes any cache an earlier one left.

self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting())
})

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(names.map((name) => caches.delete(name))))
      .then(() => self.clients.claim()),
  )
})

// What each kind of push says and the page a tap opens, at the page's own default month. A push carries its kind
// alone, so these words are the same for everyone and tell nothing on a lock screen. A feature with a new deadline adds its
// kind here and in backend/notifications/kinds.py, and nothing else.
const TITLE = "Nurse roster"
const KINDS = {
  swap_to_answer: { body: "You have a swap to answer", page: "/swaps" },
  swap_to_decide: { body: "A swap waits for your decision", page: "/unit-swaps" },
  requests_open: { body: "Requests are open for a coming month", page: "/my-month" },
  requests_to_decide: { body: "Requests wait for your decision", page: "/requests" },
  roster_to_approve: { body: "A roster waits for your approval", page: "/rosters" },
  roster_sent_back: { body: "A roster was sent back to you", page: "/roster" },
}
// What the app's window listens for, to go to a page through its own router (src/lib/service-worker.ts).
const OPEN_PAGE = "open-page"

const known = (kind) => typeof kind === "string" && Object.hasOwn(KINDS, kind)

function kindOf(data) {
  try {
    const kind = data?.json()?.kind
    return known(kind) ? kind : null
  } catch {
    return null
  }
}

// Every push is shown, whether or not the app is open and focused here: a browser shows a generic notice for one that is not,
// and Safari takes the permission away. A second of the same kind replaces the first, by its tag. One of a kind this worker
// does not know (a server newer than it) shows the title alone.
self.addEventListener("push", (event) => {
  const kind = kindOf(event.data)
  const options = kind ? { body: KINDS[kind].body, tag: kind, data: { kind } } : { data: { kind: null } }
  event.waitUntil(self.registration.showNotification(TITLE, options))
})

// A tap opens the kind's page: in a window of the app already open, focused and sent there by its own router, so a page with
// unsaved changes still asks first; else in a new window. A push of an unknown kind opens the app's home.
self.addEventListener("notificationclick", (event) => {
  event.notification.close()
  const kind = event.notification.data?.kind
  const page = known(kind) ? KINDS[kind].page : "/"
  event.waitUntil(openPage(page))
})

async function openPage(page) {
  const open = await self.clients.matchAll({ type: "window", includeUncontrolled: true })
  const app = open.find((client) => "focus" in client)
  if (!app) return self.clients.openWindow(page)
  // A browser may refuse the focus (no tap behind it); the window still goes to the page.
  await app.focus().catch(() => undefined)
  app.postMessage({ type: OPEN_PAGE, path: page })
}

// The browser renewed this device's subscription behind the scenes. If the server held the old address for the
// signed-in user, the device subscribes again with the server's current public key and tells it which address the new one
// replaces. One the server does not hold (turned off on every device from another) is left alone. A POST needs the CSRF
// token, which a worker can read only from `cookieStore`; without it the app's own check at its next start is what is left.
self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil(renewSubscription(event.oldSubscription))
})

// The app's own `renewAfterARotation` (src/lib/push.ts) does the same at its start, after a key rotation.
async function renewSubscription(old) {
  if (!old || !self.cookieStore) return
  const token = (await self.cookieStore.get("csrftoken"))?.value
  if (!token) return
  const post = async (path, body) => {
    const response = await fetch(`/api/notifications/push/${path}`, {
      method: "POST", credentials: "same-origin", body: JSON.stringify(body),
      headers: { "Content-Type": "application/json", "X-CSRFToken": token },
    })
    if (!response.ok) throw new Error(`The API answered ${response.status}`)
    return response.json()
  }
  const state = await post("state", { endpoint: old.endpoint })
  if (!state.this_device) return
  const pushManager = self.registration.pushManager
  const current = await pushManager.getSubscription()
  if (current) await current.unsubscribe()
  const renewed = await pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: bytesOf(state.public_key) })
  const { endpoint, keys } = renewed.toJSON()
  await post("on", { endpoint, keys: { p256dh: keys.p256dh, auth: keys.auth }, replaces: old.endpoint })
}

// As `bytesOf` in src/lib/push.ts: a worker cannot import the app's modules.
function bytesOf(base64url) {
  const base64 = base64url.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(base64url.length / 4) * 4, "=")
  return Uint8Array.from(atob(base64), (char) => char.charCodeAt(0))
}
