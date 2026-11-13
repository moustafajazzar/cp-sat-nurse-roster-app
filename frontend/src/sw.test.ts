import source from "../public/sw.js?raw"

type WorkerEvent = { waitUntil: (work: Promise<unknown>) => void } & Record<string, unknown>
type Posted = { url: string; body: unknown; token: string | null }

/** A window of the app the worker can find: what was done to it, in order. */
class AppWindow {
  did: unknown[] = []
  async focus() { this.did.push("focus"); return this }
  postMessage(message: unknown) { this.did.push(message) }
}

/** A push as the browser hands it to the worker: its body read as JSON, which throws for one that is not. */
const pushOf = (body: string | null) => ({ data: body === null ? null : { json: () => JSON.parse(body) } })

/** A notification shown, as a tap hands it back. */
function tapped(data: unknown) {
  const notification = { data, closed: false, close() { notification.closed = true } }
  return notification
}

/** A browser subscription as the worker's push manager hands it out. */
function subscription(endpoint: string, unsubscribed: string[]) {
  return {
    endpoint,
    unsubscribe: async () => { unsubscribed.push(endpoint); return true },
    toJSON: () => ({ endpoint, keys: { p256dh: `p256dh-of-${endpoint}`, auth: "auth" } }),
  }
}

/** The service worker run against a stand-in for its global scope: what it listens for, and what it does when it does.
 *  `server` answers the worker's POSTs by their last path segment. */
function runWorker(cacheNames: string[] = [], { csrf = "the-token" as string | null, server = {} as Record<string, unknown>,
  current = null as string | null, windows = [] as AppWindow[] } = {}) {
  const listeners = new Map<string, (event: WorkerEvent) => void>()
  const posted: Posted[] = []
  const unsubscribed: string[] = []
  const subscribedWith: Uint8Array[] = []
  const deleted: string[] = []
  const calls: string[] = []
  const shown: { title: string; options: NotificationOptions }[] = []
  const opened: string[] = []
  const scope = {
    addEventListener: (type: string, listener: (event: WorkerEvent) => void) => listeners.set(type, listener),
    skipWaiting: async () => { calls.push("skipWaiting") },
    clients: {
      claim: async () => { calls.push("claim") },
      matchAll: async (options: { type: string; includeUncontrolled: boolean }) => {
        expect(options).toEqual({ type: "window", includeUncontrolled: true })
        return windows
      },
      openWindow: async (url: string) => { opened.push(url); return null },
    },
    cookieStore: csrf === null ? undefined : { get: async (name: string) => (name === "csrftoken" ? { value: csrf } : null) },
    registration: {
      showNotification: async (title: string, options: NotificationOptions) => { shown.push({ title, options }) },
      pushManager: {
        getSubscription: async () => (current ? subscription(current, unsubscribed) : null),
        subscribe: async (options: { userVisibleOnly: boolean; applicationServerKey: Uint8Array }) => {
          expect(options.userVisibleOnly).toBe(true)
          subscribedWith.push(options.applicationServerKey)
          return subscription("https://push.example/renewed", unsubscribed)
        },
      },
    },
  }
  const fetch = async (url: string, init: RequestInit) => {
    posted.push({ url, body: JSON.parse(init.body as string), token: new Headers(init.headers).get("X-CSRFToken") })
    const answer = server[url.split("/").pop()!]
    return new Response(JSON.stringify(answer ?? {}), { status: answer ? 200 : 404 })
  }
  const caches = {
    keys: async () => cacheNames,
    delete: async (name: string) => { deleted.push(name); return true },
    open: async () => { throw new Error("the worker must not open a cache") },
    match: async () => { throw new Error("the worker must not read a cache") },
  }
  new Function("self", "caches", "fetch", source)(scope, caches, fetch)
  const fire = async (type: string, detail: Record<string, unknown> = {}) => {
    const work: Promise<unknown>[] = []
    listeners.get(type)?.({ ...detail, waitUntil: (promise) => work.push(promise) })
    await Promise.all(work)
  }
  return { listeners, deleted, calls, fire, posted, unsubscribed, subscribedWith, shown, opened }
}

test("the service worker handles no request, so nothing under /api, no export, print sheet, backup or /metrics passes through it", () => {
  const { listeners } = runWorker()

  expect(listeners.has("fetch")).toBe(false)
  expect(listeners.size).toBeGreaterThan(0)
})

describe("a push", () => {
  const KINDS = [
    ["swap_to_answer", "You have a swap to answer", "/swaps"],
    ["swap_to_decide", "A swap waits for your decision", "/unit-swaps"],
    ["requests_open", "Requests are open for a coming month", "/my-month"],
    ["requests_to_decide", "Requests wait for your decision", "/requests"],
    ["roster_to_approve", "A roster waits for your approval", "/rosters"],
    ["roster_sent_back", "A roster was sent back to you", "/roster"],
  ] as const

  it.each(KINDS)("of the kind %s is shown with the same words for everyone, tagged by its kind", async (kind, words) => {
    const worker = runWorker()

    await worker.fire("push", pushOf(JSON.stringify({ kind })))

    expect(worker.shown).toEqual([{ title: "Nurse roster", options: { body: words, tag: kind, data: { kind } } }])
    expect(worker.posted).toEqual([])
  })

  it.each([
    ["a kind this worker does not know", JSON.stringify({ kind: "cover_offered" })],
    ["no kind", JSON.stringify({})],
    ["a body that is not JSON", "not json"],
    ["no body", null],
    ["a kind named like an object's own property", JSON.stringify({ kind: "constructor" })],
  ])("with %s still shows a notification, of the title alone", async (_, body) => {
    const worker = runWorker()

    await worker.fire("push", pushOf(body))

    expect(worker.shown).toEqual([{ title: "Nurse roster", options: { data: { kind: null } } }])
  })

  it.each(KINDS)("of the kind %s tapped sends an open window of the app to its page through the app's own router", async (kind, _, page) => {
    const app = new AppWindow()
    const worker = runWorker([], { windows: [app] })
    const notification = tapped({ kind })

    await worker.fire("notificationclick", { notification })

    expect(notification.closed).toBe(true)
    expect(app.did).toEqual(["focus", { type: "open-page", path: page }])
    expect(worker.opened).toEqual([])
  })

  it("tapped with no window of the app open opens one on its page, with no month", async () => {
    const worker = runWorker([], { windows: [] })
    const notification = tapped({ kind: "swap_to_answer" })

    await worker.fire("notificationclick", { notification })

    expect(notification.closed).toBe(true)
    expect(worker.opened).toEqual(["/swaps"])
  })

  it("of an unknown kind tapped opens the app's home", async () => {
    const worker = runWorker([], { windows: [] })

    await worker.fire("notificationclick", { notification: tapped({ kind: null }) })

    expect(worker.opened).toEqual(["/"])
  })
})

test("a new worker takes over at once, so a deploy is not held back behind the old one", async () => {
  const worker = runWorker()

  await worker.fire("install")
  await worker.fire("activate")

  expect(worker.calls).toEqual(["skipWaiting", "claim"])
})

test("on taking over it removes every cache an earlier worker may have left", async () => {
  const worker = runWorker(["spa-v1", "api"])

  await worker.fire("activate")

  expect(worker.deleted.sort()).toEqual(["api", "spa-v1"])
})

describe("when the browser renews this device's subscription", () => {
  /** A P-256 point's 65 bytes, as the server hands out the public key. */
  const KEY_BYTES = Uint8Array.from({ length: 65 }, (_, index) => (index === 0 ? 4 : index))
  const PUBLIC_KEY = btoa(String.fromCharCode(...KEY_BYTES)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")
  const OLD = "https://push.example/old"
  const held = { state: { public_key: PUBLIC_KEY, this_device: true, devices: 1 }, on: { public_key: PUBLIC_KEY, this_device: true, devices: 1 } }

  it("subscribes again with the server's current key and tells it which address the new one replaces", async () => {
    const worker = runWorker([], { server: held, current: "https://push.example/browsers-own" })

    await worker.fire("pushsubscriptionchange", { oldSubscription: { endpoint: OLD } })

    expect(worker.posted).toEqual([
      { url: "/api/notifications/push/state", body: { endpoint: OLD }, token: "the-token" },
      { url: "/api/notifications/push/on", token: "the-token", body: {
        endpoint: "https://push.example/renewed", keys: { p256dh: "p256dh-of-https://push.example/renewed", auth: "auth" }, replaces: OLD,
      } },
    ])
    expect([...worker.subscribedWith[0]]).toEqual([...KEY_BYTES])
    expect(worker.unsubscribed).toEqual(["https://push.example/browsers-own"])
  })

  it("leaves alone a subscription the server does not hold, so turning off on every device stays done", async () => {
    const worker = runWorker([], { server: { state: { ...held.state, this_device: false, devices: 0 } } })

    await worker.fire("pushsubscriptionchange", { oldSubscription: { endpoint: OLD } })

    expect(worker.posted.map((call) => call.url)).toEqual(["/api/notifications/push/state"])
    expect(worker.subscribedWith).toEqual([])
  })

  it("does nothing without the CSRF token, which only cookieStore gives a worker", async () => {
    const worker = runWorker([], { csrf: null, server: held })

    await worker.fire("pushsubscriptionchange", { oldSubscription: { endpoint: OLD } })

    expect(worker.posted).toEqual([])
    expect(worker.subscribedWith).toEqual([])
  })

  it("does nothing when the browser says nothing of the old one", async () => {
    const worker = runWorker([], { server: held })

    await worker.fire("pushsubscriptionchange", { oldSubscription: null })

    expect(worker.posted).toEqual([])
  })
})
