import { expect, type Page } from "@playwright/test"

import { go, monthName, signIn, stack, test } from "../../support"

// Every role's own pages on a desktop, and what the approvers and a manager with a nurse profile read, on the seeded
// stack. Nothing here changes it.

/** Each page the role's header reaches opens with its heading, and none says it could not read what it shows. */
async function everyPageOf(page: Page) {
  const nav = page.getByRole("navigation", { name: "Main", exact: true })
  const pages = (await nav.getByRole("link").allTextContents()).filter((name) => name !== "Change password")
  expect(pages.length).toBeGreaterThan(1)
  for (const name of pages) {
    await go(page, name)
    await expect(page.getByRole("alert").filter({ hasText: /could not be read/ })).toHaveCount(0)
  }
  return pages
}

for (const [username, role] of [["admin", "System Admin"], ["carla", "CNO"], ["hala", "Deputy CNO"], ["mona", "Unit Manager"],
  ["omar", "Unit Manager with a nurse profile"], [stack.swap.colleague, "Staff Nurse"]] as const) {
  test(`every page of the ${role} opens on a desktop`, async ({ page }) => {
    await signIn(page, username)
    await expect(page.getByRole("navigation", { name: "Main", exact: true })).toBeVisible()
    await expect(page.getByRole("button", { name: "Menu" })).toBeHidden()
    await everyPageOf(page)
  })
}

for (const [username, role] of [["carla", "CNO"], ["hala", "Deputy CNO"]] as const) {
  test(`the ${role} reads every unit's approved roster from the Rosters overview`, async ({ page }) => {
    await signIn(page, username)
    await go(page, "Rosters")
    for (const [unit, nurses] of [[stack.unit, 4], [stack.large_unit, 64]] as const) {
      const row = page.getByRole("row", { name: new RegExp(`^${unit} `) })
      await row.locator(`a[href*="month=${stack.approved_month}"]`).filter({ hasText: "Approved" }).click()
      await expect(page.getByRole("heading", { level: 1, name: `Roster of ${unit}` })).toBeVisible()
      const grid = page.getByRole("region", { name: `Approved roster of ${unit}, ${monthName(stack.approved_month)}` })
      await expect(grid.getByRole("rowheader").first()).toBeVisible()
      // Every nurse is on it, with the header row: a long roster is windowed, so its rows are counted, not read.
      await expect(grid.locator("table")).toHaveAttribute("aria-rowcount", String(nurses + 1))
      await expect(page.getByRole("table", { name: "Versions of this roster, newest first" }).getByRole("row", { name: /^1 / })).toBeVisible()
      await page.goBack()
    }
  })
}

test("a manager with a nurse profile opens My roster and My month from the header, and reads their own row and month", async ({ page }) => {
  await signIn(page, "omar")
  // The header reaches both, before the unit's pages.
  await go(page, "My roster")
  await expect(page.getByRole("heading", { level: 1, name: `Roster of ${stack.large_unit}` })).toBeVisible()
  await go(page, "My month")
  await expect(page.getByRole("heading", { level: 1, name: "My month" })).toBeVisible()
  // Then the seeded month, which may not be the one either page opens on.
  await page.goto(`/my-roster?month=${stack.approved_month}`)
  const grid = page.getByRole("region", { name: `Roster of ${stack.large_unit}, ${monthName(stack.approved_month)}` })
  await expect(grid.getByRole("rowheader", { name: `${stack.names.omar} (you)` })).toBeVisible()
  await page.goto(`/my-month?month=${stack.approved_month}`)
  await expect(page.getByRole("heading", { level: 1, name: "My month" })).toBeVisible()
  await expect(page.getByRole("heading", { level: 3, name: "Hours and shifts" })).toBeVisible()
  // The version in force: a re-plan of ICU (large.spec) may have made a later one.
  await expect(page.getByText(new RegExp(`From version \\d+ of the ${stack.large_unit} roster, approved `))).toBeVisible()
  await expect(page.getByRole("term").filter({ hasText: "Hours worked" })).toBeVisible()
  // A staff nurse's requests are not a manager's: only their hours, shifts and leave.
  await expect(page.getByRole("region", { name: /^Your requests for / })).toHaveCount(0)
})
