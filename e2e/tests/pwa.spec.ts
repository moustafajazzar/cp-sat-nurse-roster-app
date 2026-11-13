import { expect, request, type CDPSession, type Page } from "@playwright/test"

import { anotherPhone, signIn, stack, test } from "../support"

// The app installs as a progressive web app, and its service worker keeps nothing. These read what Chrome itself
// reads: the manifest as it parsed it, the worker it registered, and for every response whether it came from the worker or a
// cache (the DevTools protocol's Network events). The worker has no `fetch` listener (src/sw.test.ts holds it to that), so
// "not from the worker" cannot fail today; "not from the disk cache" on a second fetch of the same file is the check that
// `no-store` is honoured. Chrome's own `Page.getInstallabilityErrors` is not used: headless, it answers [] even with no
// manifest at all, so it proves nothing here.

/** Waits until the service worker controls the page: from the first visit, since it claims its clients when it activates. */
async function controlled(page: Page) {
  await page.waitForFunction(() => navigator.serviceWorker.controller?.scriptURL.endsWith("/sw.js"))
}

/** Every response the page receives from here on, as Chrome reports where it came from. */
async function watchResponses(page: Page) {
  const cdp: CDPSession = await page.context().newCDPSession(page)
  const seen: { url: string; status: number; fromServiceWorker: boolean; fromDiskCache: boolean; cacheControl?: string }[] = []
  cdp.on("Network.responseReceived", ({ response }) => seen.push({
    url: response.url, status: response.status, fromServiceWorker: !!response.fromServiceWorker, fromDiskCache: !!response.fromDiskCache,
    cacheControl: Object.entries(response.headers as Record<string, string>).find(([name]) => name.toLowerCase() === "cache-control")?.[1],
  }))
  await cdp.send("Network.enable")
  return seen
}

/** Fetches a path from inside the page, as the app does, and waits for the whole body. */
const fetchInPage = (page: Page, path: string) => page.evaluate(async (path) => {
  const response = await fetch(path, { credentials: "same-origin" })
  await response.arrayBuffer()
  return response.status
}, path)

test("Chrome reads the manifest whole, its icons are there, and the worker controls the page", async ({ page }) => {
  await page.goto("/login")
  await controlled(page)

  const cdp = await page.context().newCDPSession(page)
  const manifest = await cdp.send("Page.getAppManifest")
  expect(manifest.url).toBe(`${stack.baseURL}/manifest.webmanifest`)
  expect(manifest.errors).toEqual([])
  const data = JSON.parse(manifest.data!)
  expect(data).toMatchObject({ name: "Nurse roster", short_name: "Roster", start_url: "/", scope: "/", display: "standalone" })

  for (const icon of [...data.icons, { src: "/apple-touch-icon.png", sizes: "180x180" }]) {
    const response = await page.request.get(icon.src)
    expect(response.status(), icon.src).toBe(200)
    expect(response.headers()["content-type"]).toBe("image/png")
    const png = await response.body()
    expect(`${png.readUInt32BE(16)}x${png.readUInt32BE(20)}`, icon.src).toBe(icon.sizes)
  }

  const registration = await page.evaluate(async () => {
    const found = await navigator.serviceWorker.getRegistration()
    return { scope: found?.scope, script: found?.active?.scriptURL, updateViaCache: found?.updateViaCache }
  })
  expect(registration).toEqual({ scope: `${stack.baseURL}/`, script: `${stack.baseURL}/sw.js`, updateViaCache: "none" })

  for (const path of ["/sw.js", "/manifest.webmanifest", "/"]) {
    expect((await page.request.get(path)).headers()["cache-control"], path).toBe("no-cache")
  }
})

test("nothing reaches the page from the worker or a cache: the API, a roster's files and print sheets, /metrics", async ({ browser, page }) => {
  await signIn(page, stack.swap.colleague)
  await controlled(page)
  const nurseSaw = await watchResponses(page)

  await page.goto(`/my-roster?month=${stack.approved_month}`)
  await expect(page.getByRole("region", { name: /^Roster of / })).toBeVisible()
  await controlled(page)
  // Each file twice, on purpose: the second would be the one a cache answered.
  for (const path of [`/api/rosters/mine/sheet?month=${stack.approved_month}`, `/api/rosters/mine/sheet?month=${stack.approved_month}`, "/metrics"]) {
    await fetchInPage(page, path)
  }

  const manager = await anotherPhone(browser, page, "mona")
  await controlled(manager)
  const managerSaw = await watchResponses(manager)
  const unit = (await (await manager.request.get("/api/units")).json() as { id: string; name: string }[]).find((row) => row.name === stack.unit)!
  const roster = `/api/units/${unit.id}/roster`
  const version = `month=${stack.approved_month}&version=1`
  for (const path of [`${roster}/export?format=csv&${version}`, `${roster}/export?format=csv&${version}`, `${roster}/export?format=xlsx&${version}`,
    `${roster}/sheet?${version}`, `${roster}/sheet?${version}`]) {
    expect(await fetchInPage(manager, path), path).toBe(200)
  }
  await manager.context().close()

  const admin = await anotherPhone(browser, page, "admin")
  await controlled(admin)
  const adminSaw = await watchResponses(admin)
  const health = await (await admin.request.get("/api/ops/health")).json() as { backups: { name: string }[] }
  expect(health.backups.length, "e2e/stack.sh up takes a backup").toBeGreaterThan(0)
  for (const path of ["/api/ops/health", "/api/ops/health", `/api/ops/backups/${health.backups[0].name}`, `/api/ops/backups/${health.backups[0].name}`]) {
    expect(await fetchInPage(admin, path), path).toBe(200)
  }
  await admin.context().close()

  const seen = [...nurseSaw, ...managerSaw, ...adminSaw]
  const ours = seen.filter((response) => /\/(api|metrics)/.test(new URL(response.url).pathname))
  expect(ours.filter((response) => response.url.includes("/mine/sheet"))).toHaveLength(2)
  expect(ours.filter((response) => response.url.includes("/roster/export"))).toHaveLength(3)
  expect(ours.filter((response) => response.url.includes("/roster/sheet"))).toHaveLength(2)
  expect(ours.filter((response) => response.url.endsWith("/metrics"))).toHaveLength(1)
  // The System Admin's home is the Health page, which reads it too, before or after the watch began: at least our two.
  expect(ours.filter((response) => response.url.endsWith("/api/ops/health")).length).toBeGreaterThanOrEqual(2)
  expect(ours.filter((response) => response.url.includes("/api/ops/backups/"))).toHaveLength(2)
  expect(ours.filter((response) => response.fromServiceWorker || response.fromDiskCache)).toEqual([])
  for (const response of ours.filter((response) => /\/roster\/export|\/api\/ops\/backups\//.test(response.url))) {
    expect(response.cacheControl).toContain("no-store")
  }
  // The page itself too: every navigation went to the server.
  expect(seen.filter((response) => !/\/(api|metrics|assets)/.test(new URL(response.url).pathname) && response.fromServiceWorker)).toEqual([])
})

test("through Caddy, as a hospital reaches it: the same headers, and /metrics refused", async () => {
  // The browser will not register a worker behind Caddy's self-signed certificate, so this reads the answers themselves.
  const caddy = await request.newContext({ baseURL: stack.caddyURL, ignoreHTTPSErrors: true })
  for (const [path, type] of [["/", "text/html"], ["/my-roster", "text/html"], ["/sw.js", "text/javascript"],
    ["/manifest.webmanifest", "application/manifest+json"], ["/icon-192.png", "image/png"]]) {
    const response = await caddy.get(path)
    expect(response.status(), path).toBe(200)
    expect(response.headers()["cache-control"], path).toBe("no-cache")
    expect(response.headers()["content-type"], path).toContain(type)
  }
  const entry = (await (await caddy.get("/")).text()).match(/src="(\/assets\/[^"]+\.js)"/)![1]
  expect((await caddy.get(entry)).headers()["cache-control"]).toContain("immutable")
  for (const path of ["/metrics", "/metrics/"]) expect((await caddy.get(path)).status(), path).toBe(404)
  await caddy.dispose()
})
