import { statSync } from "node:fs"

import { expect } from "@playwright/test"

import { another, go, signIn, stack, test } from "../../support"

// The System Admin's day ends on the health page, where the Backups card lists each backup with its
// size and time and a Download, and the file comes down whole. Nobody else gets it: another role is refused the file and sent
// home from the page. e2e/stack.sh up took a backup, so there is one. Nothing here changes the stack.

test("the System Admin reads the health page and downloads a backup from its card", async ({ page }) => {
  await signIn(page, "admin")
  await go(page, "Health")
  for (const card of ["Services", "Queue", "Storage"]) await expect(page.getByRole("heading", { level: 2, name: card })).toBeVisible()
  await expect(page.getByRole("heading", { level: 2, name: "Backups" })).toBeVisible()
  const first = page.getByRole("region", { name: "Backups kept" }).getByRole("row").nth(1)
  // Its time (the row's header), its size, and its Download.
  await expect(first.getByRole("rowheader")).toHaveText(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/)
  await expect(first.getByRole("cell").first()).toHaveText(/^\d+(\.\d+)? (B|KB|MB|GB)$/)
  const link = first.getByRole("link", { name: /^Download the backup of / })
  const [download] = await Promise.all([page.waitForEvent("download"), link.click()])
  const path = await download.path()
  expect(statSync(path).size, "the backup downloaded is not empty").toBeGreaterThan(0)
  expect(download.suggestedFilename()).toMatch(/^nurse-roster-\d{8}T\d{6}Z\.dump$/)
})

test("another role is refused the backup file, and sent home from the health page", async ({ browser, page }) => {
  await signIn(page, "admin")
  const health = await (await page.request.get("/api/ops/health")).json() as { backups: { name: string }[] }
  const name = health.backups[0].name

  for (const username of ["carla", "mona", stack.swap.colleague]) {
    const theirs = await another(browser, page, username)
    expect((await theirs.request.get(`/api/ops/backups/${name}`)).status(), `${username} is refused the file`).toBe(403)
    expect((await theirs.request.get("/api/ops/health")).status(), `${username} is refused the health page's data`).toBe(403)
    await theirs.goto("/health")
    await expect(theirs).toHaveURL(new RegExp(`${stack.baseURL}/$`))
    await expect(theirs.getByRole("heading", { level: 1, name: stack.names[username] })).toBeVisible()
    await theirs.context().close()
  }
})
