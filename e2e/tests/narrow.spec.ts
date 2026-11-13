import { expect, type Page } from "@playwright/test"

import { expectNoSidewaysScroll, monthName, signIn, stack, test } from "../support"

// Every page at phone width (the `phone-360` and `phone-320` projects). The other phone checks walk the
// nurse's, a unit manager's and the System Admin's menus; these walk the CNO's and the Deputy CNO's, and the pages no menu
// lists, reached by a link on another page: a unit's roster from Rosters, a solve's result, the print pages, the nurse
// import. None makes the page itself scroll sideways. Nothing here changes the stack.

async function walkTheMenu(page: Page) {
  const menu = page.getByRole("button", { name: "Menu" })
  await menu.click()
  const pages = (await page.getByRole("navigation", { name: "Main menu" }).getByRole("link").allTextContents()).filter((name) => name !== "Change password")
  await menu.click()
  expect(pages).toContain("Rosters")
  for (const name of pages) {
    await menu.click()
    await page.getByRole("navigation", { name: "Main menu" }).getByRole("link", { name, exact: true }).click()
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible()
    await expect(page.getByText("Loading…")).toHaveCount(0)
    await expectNoSidewaysScroll(page)
  }
}

/** A page by its address, once what it reads is on it. */
async function at(page: Page, path: string, heading: string | RegExp) {
  await page.goto(path)
  await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible()
  await expect(page.getByText(/^Loading/)).toHaveCount(0)
  await expectNoSidewaysScroll(page)
}

for (const [username, role] of [["carla", "CNO"], ["hala", "Deputy CNO"]] as const) {
  test(`the ${role}'s pages keep within a phone's width, a unit's roster from Rosters among them`, async ({ page }) => {
    await signIn(page, username)
    await walkTheMenu(page)
    await page.goto(`/rosters?month=${stack.approved_month}`)
    await page.getByRole("row", { name: new RegExp(`^${stack.large_unit} `) }).locator(`a[href*="month=${stack.approved_month}"]`).first().click()
    await expect(page.getByRole("heading", { level: 1, name: `Roster of ${stack.large_unit}` })).toBeVisible()
    await expect(page.getByText("Loading…")).toHaveCount(0)
    await expectNoSidewaysScroll(page)
  })
}

test("the pages a link reaches keep within a phone's width: a solve's result, the print pages, the nurse import", async ({ page }) => {
  await signIn(page, "mona")
  await at(page, "/solve-history", "Solve history")
  await page.getByRole("link", { name: /^Open the result of the solve asked for at / }).first().click()
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible()
  await expect(page.getByText("Loading…")).toHaveCount(0)
  await expectNoSidewaysScroll(page)
  const unit = (await (await page.request.get("/api/units")).json() as { id: string; name: string }[]).find((row) => row.name === stack.unit)!
  await at(page, `/roster/print?unit=${unit.id}&month=${stack.approved_month}&version=1`, new RegExp(stack.unit))

  await signIn(page, stack.swap.colleague)
  await at(page, `/my-roster/print?month=${stack.approved_month}`, new RegExp(monthName(stack.approved_month)))

  await signIn(page, "carla")
  await at(page, "/nurses/import", /import/i)
})
