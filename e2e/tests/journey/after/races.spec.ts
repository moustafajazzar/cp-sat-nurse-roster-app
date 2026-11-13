import { expect, type Browser, type Page } from "@playwright/test"

import { choose, compose, go, journeyStack as on, monthName, proposeSwap, shortDay, soundSwap, type Swap, test } from "../../../support"
import { alongside, as, nameOf, notebook, people, SOLVED, UNIT } from "../people"

// Races, on what the desktop journey left. The server's tests force the order on real connections; here the order is
// forced by the test itself, never by timing: the loser's page is loaded first, the winner acts and is awaited, then the loser
// acts on a page that no longer holds. What is checked is that the loser's page says what happened. (Two approvers at once is
// the journey's own step.)

test.describe.configure({ mode: "serial", timeout: 120_000 })

/** The manager's edit of two cells of the approved roster, as the swap's: the proposer's shift to the colleague. */
async function editAsTheSwap(page: Page, swap: Swap) {
  const grid = page.getByRole("grid")
  const day = Number(swap.date.slice(8))
  await grid.getByRole("button", { name: new RegExp(`^${nameOf(swap.proposer)}, ${day} [A-Z][a-z]+:`) }).click()
  await page.getByRole("menuitem", { name: "Pin a day off", exact: true }).click()
  // A shift's item shows its code, then "Pin" and its name.
  await grid.getByRole("button", { name: new RegExp(`^${nameOf(swap.colleague)}, ${day} [A-Z][a-z]+:`) }).click()
  await page.getByRole("menuitem").filter({ hasText: new RegExp(`^${swap.shift}Pin `) }).click()
}

test("two changes to the approved roster at once: the second, from a page loaded before the first, is the next version", async ({ browser, page }) => {
  const { rostered } = notebook.read()
  await as(page, people.manager.username)
  await go(page, "Roster")
  await expect(page.getByRole("heading", { level: 2, name: monthName(rostered) })).toBeVisible()
  const other = await alongside(browser, page, people.manager.username)
  await go(other, "Roster")
  await expect(other.getByRole("heading", { level: 2, name: monthName(rostered) })).toBeVisible()

  // Late in the month, away from the cells the journey's swap pinned, which only that swap may change.
  await editAsTheSwap(other, soundSwap(on, UNIT, rostered, `${rostered}-20`))
  await other.getByLabel("Why", { exact: true }).fill("Covering a night")
  await other.getByRole("button", { name: "Save as a new version" }).click()
  const first = other.getByRole("status").filter({ hasText: /^2 changes saved as version \d+, now in force\./ })
  await expect(first).toBeVisible()
  const version = Number((await first.textContent())!.match(/version (\d+)/)![1])
  await other.context().close()

  // This page still shows the roster as it was before that version. Its change is sound on the roster as it now is.
  await expect(page.getByText(`Version ${version - 1} is in force, and the unit's nurses see it.`, { exact: false })).toBeVisible()
  await editAsTheSwap(page, soundSwap(on, UNIT, rostered, `${rostered}-24`))
  await page.getByLabel("Why", { exact: true }).fill("Another cover, from another tab")
  await page.getByRole("button", { name: "Save as a new version" }).click()
  await expect(page.getByRole("status").filter({ hasText: `2 changes saved as version ${version + 1}, now in force.` })).toBeVisible()
  const versions = page.getByRole("table", { name: "Versions of this roster, newest first" })
  await expect(versions.getByRole("row", { name: new RegExp(`^${version + 1} \\(in force\\) Changed by hand .* Another cover, from another tab`) })).toBeVisible()
  await expect(versions.getByRole("row", { name: new RegExp(`^${version} Changed by hand .* Covering a night`) })).toBeVisible()
})

/** A swap proposed and accepted by its two nurses, waiting for the manager. */
async function aSwapWaitingForTheManager(browser: Browser, page: Page, swap: Swap, month: string) {
  const proposer = await alongside(browser, page, swap.proposer)
  await proposer.goto(`/swaps?month=${month}`)
  await proposeSwap(proposer, swap, nameOf(swap.colleague))
  await proposer.context().close()
  const colleague = await alongside(browser, page, swap.colleague)
  await colleague.goto(`/swaps?month=${month}`)
  await colleague.getByRole("button", { name: `Accept ${nameOf(swap.proposer)}'s swap on ${shortDay(swap.date)}` }).click()
  await expect(colleague.getByText(/Everyone in it has now accepted, so it waits for the manager\.$/)).toBeVisible()
  await colleague.context().close()
}

const OUTDATED = "The roster in force or the month changed while the re-plan ran, so its roster was not put in force: it would have undone that change. Re-plan again to take it in."

/** The manager starts a re-plan with the worker stopped, so it stays on the queue while `meanwhile` changes the month; the
 *  worker is then started, and the re-plan ends. The order is the test's, not a race's. */
async function whileARePlanWaits(browser: Browser, page: Page, meanwhile: (other: Page) => Promise<void>) {
  const { rostered } = notebook.read()
  compose(on, "stop", "worker")
  try {
    await as(page, people.manager.username)
    await go(page, "Roster")
    await expect(page.getByRole("heading", { level: 2, name: monthName(rostered) })).toBeVisible()
    await page.getByRole("region", { name: "Re-plan from a date" }).getByLabel("Time limit (seconds)").fill("5")
    await page.getByRole("button", { name: /^Re-plan/ }).click()
    await expect(page.getByText("Next in the hospital's queue.")).toBeVisible()
    const other = await alongside(browser, page, people.manager.username)
    await meanwhile(other)
    await other.context().close()
  } finally {
    compose(on, "up", "-d", "--wait", "worker")
  }
  await expect(page.getByText(OUTDATED)).toBeVisible(SOLVED)
}

test("a swap approved while a re-plan waits: the re-plan ends without undoing it, and its page says why", async ({ browser, page }) => {
  const { rostered } = notebook.read()
  await aSwapWaitingForTheManager(browser, page, soundSwap(on, UNIT, rostered, `${rostered}-08`), rostered)
  await whileARePlanWaits(browser, page, async (swaps) => {
    await go(swaps, "Swaps")
    await swaps.getByRole("button", { name: "Approve", exact: true }).click()
    await swaps.getByRole("alertdialog").getByRole("button", { name: "Approve the swap" }).click()
    await expect(swaps.getByText(/^You approved .* is now in force, its cells pinned\./)).toBeVisible()
  })
  await expect(page.getByRole("table", { name: "Versions of this roster, newest first" }).getByRole("row").nth(1)).toContainText("Swap")
})

test("leave recorded while a re-plan waits: the re-plan ends without undoing it, and its page says why", async ({ browser, page }) => {
  const { rostered } = notebook.read()
  const [, , lina] = people.nurses
  await whileARePlanWaits(browser, page, async (leave) => {
    await leave.goto(`/leave?month=${rostered}`)
    await leave.getByRole("button", { name: "Record leave" }).click()
    await leave.getByLabel("Nurse").selectOption({ label: lina.name })
    await leave.getByLabel("Leave type").selectOption({ label: "Emergency" })
    await leave.getByLabel("First day").fill(`${rostered}-27`)
    await leave.getByRole("button", { name: "Save" }).click()
    await expect(leave.getByRole("button", { name: new RegExp(`^Remove leave of ${lina.name}, `) })).toBeVisible()
  })
})

test("a manager deactivated while editing goes, on saving, straight to the login page, which says so; signing in again returns to the roster", async ({ browser, page }) => {
  const { rostered } = notebook.read()
  await as(page, people.manager.username)
  await go(page, "Roster")
  await expect(page.getByRole("heading", { level: 2, name: monthName(rostered) })).toBeVisible()
  await editAsTheSwap(page, soundSwap(on, UNIT, rostered, `${rostered}-14`))
  await expect(page.getByRole("region", { name: "2 changes not saved yet" })).toBeVisible()

  const admin = await alongside(browser, page, "admin")
  await go(admin, "Users")
  await admin.getByRole("button", { name: `Deactivate ${people.manager.username}` }).click()
  await admin.getByRole("alertdialog").getByRole("button", { name: "Deactivate" }).click()
  await expect(admin.getByRole("region", { name: "Users" }).getByRole("row", { name: new RegExp(people.manager.name) })).toContainText("Deactivated")

  await page.getByLabel("Why", { exact: true }).fill("Cover agreed on the phone")
  await page.getByRole("button", { name: "Save as a new version" }).click()
  // The save is refused for the session that ended, and the app goes straight to the login page, with no question
  // first, since staying could save nothing. The login page says why it is there. The roster page's own alerts show until it
  // has gone, so its form is waited for first.
  await expect(page.getByRole("button", { name: "Log in" })).toBeVisible()
  await expect(page).toHaveURL((url) => url.pathname === "/login")
  await expect(page.getByRole("alert")).toHaveText("You were signed out. Changes you had not saved are lost. Sign in to carry on.")
  await expect(page.getByLabel("Username")).toBeFocused()
  await expect(page.getByRole("alertdialog")).toHaveCount(0)

  // Putting it back: reactivated by the System Admin, and given the unit again by the CNO, since deactivating took it off.
  await admin.getByRole("button", { name: `Reactivate ${people.manager.username}` }).click()
  await admin.getByRole("alertdialog").getByRole("button", { name: "Reactivate" }).click()
  await expect(admin.getByRole("region", { name: "Users" }).getByRole("row", { name: new RegExp(people.manager.name) })).toContainText("Active")
  await admin.context().close()
  const cno = await alongside(browser, page, people.cno.username)
  await go(cno, "Units")
  await expect(cno.getByRole("row", { name: new RegExp(`${UNIT}.*No manager`) })).toBeVisible()
  await cno.getByRole("button", { name: `Edit ${UNIT}` }).click()
  await choose(cno.getByLabel("Manager"), new RegExp(people.manager.name))
  await cno.getByRole("button", { name: "Save unit" }).click()
  await expect(cno.getByRole("row", { name: new RegExp(`${UNIT}.*${people.manager.name}`) })).toBeVisible()
  await cno.context().close()

  // Signing in on the same login page goes back to the Roster page, with nothing of the changes kept.
  await page.getByLabel("Username").fill(people.manager.username)
  await page.getByLabel("Password").fill(notebook.read().chosen[people.manager.username])
  await page.getByRole("button", { name: "Log in" }).click()
  await expect(page).toHaveURL((url) => url.pathname === "/roster")
  await expect(page.getByRole("heading", { level: 1, name: `Roster of ${UNIT}` })).toBeVisible()
  await expect(page.getByRole("heading", { level: 2, name: monthName(rostered) })).toBeVisible()
  await expect(page.getByRole("region", { name: /not saved yet/ })).toHaveCount(0)
  await expect(page.getByText("You were signed out.", { exact: false })).toHaveCount(0)
})
