import { expect } from "@playwright/test"

import { monthName, signIn, stack, test } from "../../support"

// The CNO opens the Day view from the header, moves it to the first day of the seeded approved month, reads ER's
// row, expands it to its names, and clicks through to ER's roster of that month with the date's column marked. Read-only, so it
// runs again as it ran.

const day = `${stack.approved_month}-01`

test("the CNO reads a unit's day, its names, and its roster with the date marked", async ({ page }) => {
  await signIn(page, "carla")
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Day view" }).click()
  await expect(page.getByRole("heading", { level: 1, name: "Day view" })).toBeVisible()

  await page.getByLabel("Date").fill(day)
  await expect(page).toHaveURL(new RegExp(`/day\\?date=${day}$`))
  const table = page.getByRole("region", { name: /^Day view, / })
  const er = table.getByRole("row").filter({ has: page.getByRole("rowheader", { name: /ER/ }) }).first()
  // The sample's ranges are Day 1 to 1 and Night 1 to 2, so each worked shift reads against its range.
  await expect(er.getByRole("cell").first()).toHaveText(/^\d+ of 1/)
  await expect(er.getByRole("cell").last()).toHaveText(/^\d+ h of \d+, \d+ nurses?$/)

  const names = page.getByRole("button", { name: "Names in ER" })
  await expect(names).toHaveAttribute("aria-expanded", "false")
  await names.click()
  await expect(names).toHaveAttribute("aria-expanded", "true")
  const expanded = page.locator(`#${await names.getAttribute("aria-controls")}`)
  await expect(expanded.getByRole("term")).toHaveText(["Day", "Night", "On leave"])

  await er.getByRole("link", { name: "ER" }).click()
  await expect(page).toHaveURL(new RegExp(`/unit-roster\\?unit=.+&month=${stack.approved_month}&date=${day}$`))
  const grid = page.getByRole("region", { name: `Approved roster of ER, ${monthName(stack.approved_month)}` })
  const marked = grid.locator("th[aria-current=date]")
  await expect(marked).toHaveCount(1)
  await expect(marked).toContainText("the Day view's date")
  await expect(marked).toBeInViewport()
})
