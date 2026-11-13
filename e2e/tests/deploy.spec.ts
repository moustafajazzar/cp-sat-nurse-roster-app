import { expect } from "@playwright/test"

import { redeploy, test } from "../support"

// A new deploy is what a phone loads next, never a stale page from the worker or a cache. e2e/stack.sh redeploy
// changes index.html and the service worker inside `web` and restarts it, which is what a browser sees of a new image. It runs
// last (the `deploy` project), since it restarts `web`.

test("after a deploy, the next load is the new page and the new worker takes over", async ({ page }) => {
  const cdp = await page.context().newCDPSession(page)
  const activated = new Set<string>()
  cdp.on("ServiceWorker.workerVersionUpdated", ({ versions }) => {
    for (const version of versions) if (version.status === "activated") activated.add(version.versionId)
  })
  await cdp.send("ServiceWorker.enable")
  await page.goto("/login")
  await page.waitForFunction(() => navigator.serviceWorker.controller !== null)
  await expect.poll(() => activated.size).toBeGreaterThan(0)
  const before = new Set(activated)
  // Each redeploy puts its marker first in <head>; a run before may have left one.
  const deployed = page.locator('meta[name="e2e-deploy"]').first()

  const marker = redeploy()
  await page.reload()

  // The page from the server, and the browser's own check of the worker on that navigation finds the new one: nothing here
  // asks it to look.
  await expect(deployed).toHaveAttribute("content", marker)
  await expect.poll(() => [...activated].some((id) => !before.has(id)), { message: "a new version of the worker activated" }).toBe(true)
})
