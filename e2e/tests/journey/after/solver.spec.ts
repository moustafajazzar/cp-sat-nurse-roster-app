import { expect } from "@playwright/test"

import { compose, go, journeyStack as on, monthName, test, toMonth } from "../../../support"
import { alongside, as, notebook, people, SOLVED, UNIT } from "../people"

// A solve whose solver is gone, on what the desktop journey left. The `solver` service is stopped before the solve
// is started, which gives the same failure as one stopped during it (the worker cannot reach it, tries once more, and gives up)
// without depending on when the stop lands. The manager reads that it failed; the System Admin's solve history reads which
// kind of failure it was, in its fixed words. The solver is started again whatever happened.

test("a solve fails when the solver cannot be reached, and the System Admin's solve history says so", async ({ browser, page }) => {
  test.setTimeout(120_000)
  const { requested } = notebook.read()
  compose(on, "stop", "solver")
  try {
    await as(page, people.manager.username)
    await go(page, "Roster")
    await toMonth(page, "Next month", requested)
    await page.getByLabel("Time limit (seconds)").fill("5")
    await page.getByRole("button", { name: "Start solve" }).click()
    await expect(page.getByText(/^The solve failed, and the draft is as it was\. You can run it again\. It says: /)).toBeVisible(SOLVED)
  } finally {
    compose(on, "up", "-d", "--wait", "solver")
  }

  const admin = await alongside(browser, page, "admin")
  await go(admin, "Solve history")
  const row = admin.getByRole("row").filter({ hasText: UNIT }).filter({ hasText: monthName(requested) })
  await expect(row.first()).toContainText("Failed")
  await expect(row.first()).toContainText("The solver could not be reached.")
  await admin.context().close()
})
