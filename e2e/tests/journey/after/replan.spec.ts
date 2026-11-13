import { expect } from "@playwright/test"

import { go, monthName, test } from "../../../support"
import { alongside, as, notebook, openFromRosters, people, SOLVED } from "../people"

// A re-plan that changes the roster in force, and the Deputy CNO reading it, on what the desktop journey left. Leave
// recorded late on a day a nurse works takes her off it, so the day falls short of its headcount; the manager re-plans the
// month with the real solver, which covers it, and the re-plan's roster is a new version in force. The Deputy CNO reads it
// among the versions, with its reason, and compares it with the one before.

test("leave recorded late, a re-plan that covers it, and the Deputy CNO reading the re-plan's version and comparison", async ({ browser, page }) => {
  test.setTimeout(180_000)
  const { rostered } = notebook.read()
  const month = monthName(rostered).split(" ")[0]
  await as(page, people.manager.username)
  await go(page, "Roster")
  await expect(page.getByRole("heading", { level: 2, name: monthName(rostered) })).toBeVisible()
  // Whoever works the 16th, on the grid as the manager reads it.
  const working = page.getByRole("grid").getByRole("button", { name: new RegExp(`, 16 ${month}: (Day|Night)`) }).first()
  const nurse = (await working.getAttribute("aria-label"))!.split(",")[0]

  await page.goto(`/leave?month=${rostered}`)
  await page.getByRole("button", { name: "Record leave" }).click()
  await page.getByLabel("Nurse").selectOption({ label: nurse })
  await page.getByLabel("Leave type").selectOption({ label: "Sick" })
  await page.getByLabel("First day").fill(`${rostered}-16`)
  await page.getByRole("button", { name: "Save" }).click()
  await expect(page.getByRole("button", { name: new RegExp(`^Remove leave of ${nurse}, `) })).toBeVisible()

  await go(page, "Roster")
  const replan = page.getByRole("region", { name: "Re-plan from a date" })
  await replan.getByLabel("Time limit (seconds)").fill("10")
  await replan.getByRole("button", { name: /^Re-plan/ }).click()
  await expect(page.getByRole("status").filter({ hasText: /^The re-plan (finished|stopped) with a cost of \d+, and its roster is now in force as a new version\./ }))
    .toBeVisible(SOLVED)
  const versions = page.getByRole("table", { name: "Versions of this roster, newest first" })
  const replanned = versions.getByRole("row", { name: /^\d+ \(in force\) Re-planned / })
  await expect(replanned).toBeVisible()
  const number = Number((await replanned.getByRole("cell").first().textContent())!.match(/^\d+/)![0])

  const deputy = await alongside(browser, page, people.deputy.username)
  await openFromRosters(deputy, "Approved")
  const read = deputy.getByRole("table", { name: "Versions of this roster, newest first" }).getByRole("row", { name: new RegExp(`^${number} \\(in force\\) Re-planned `) })
  await expect(read).toContainText(people.manager.name)
  await expect(read).toContainText(`Re-planned from 1 ${month}`)
  // The latest change is the pair shown first: the version before against the re-plan's.
  await expect(deputy.getByRole("status").filter({ hasText: new RegExp(`^\\d+ cells? differs? from version ${number - 1} to version ${number}\\.$`) })).toBeVisible()
  await expect(deputy.getByRole("table", { name: `Cells that differ from version ${number - 1} to version ${number}` }).getByRole("row").nth(1)).toBeVisible()
  await deputy.context().close()
})
