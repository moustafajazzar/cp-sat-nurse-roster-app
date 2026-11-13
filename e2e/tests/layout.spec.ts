import { expect } from "@playwright/test"

import { expectNoSidewaysScroll, expectThumbSized, monthName, signIn, stack, test } from "../support"

// A staff nurse's pages at phone width (360 px and 320 px, the two projects). Nothing makes the page itself scroll
// sideways, the header is the name, the bell and a Menu button, and the roster grid scrolls inside its own region with the
// nurse column held in place.

test("the login page fits a phone", async ({ page }) => {
  await page.goto("/login")

  await expect(page.getByRole("button", { name: "Log in" })).toBeVisible()
  await expectNoSidewaysScroll(page)
  await expectThumbSized(page.getByLabel("Username"))
  await expectThumbSized(page.getByRole("button", { name: "Log in" }))
})

test("every page of a staff nurse fits a phone, and the menu reaches each one", async ({ page }) => {
  const nurse = stack.swap.colleague
  await signIn(page, nurse)
  const menu = page.getByRole("button", { name: "Menu" })
  await expectThumbSized(menu)
  await expectThumbSized(page.getByRole("link", { name: /^Notifications/ }))
  await expect(page.getByRole("navigation", { name: "Main", exact: true })).toBeHidden()

  for (const [link, heading] of [["My month", "My month"], ["My roster", /^Roster of|My roster/], ["Swaps", "Swaps"], ["Covers", "Covers"], ["Shifts", "Shifts"],
    ["Leave types", "Leave types"], ["Holidays and Ramadan", "Holidays and Ramadan"], ["Home", stack.names[nurse]]] as const) {
    await menu.click()
    const item = page.getByRole("navigation", { name: "Main menu" }).getByRole("link", { name: link, exact: true })
    await expectThumbSized(item)
    await item.click()
    await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible()
    await expect(page.getByRole("navigation", { name: "Main menu" })).toBeHidden()
    await expectNoSidewaysScroll(page)
  }

  await page.getByRole("link", { name: /^Notifications/ }).click()
  await expect(page.getByRole("heading", { level: 1, name: "Notifications" })).toBeVisible()
  await expectNoSidewaysScroll(page)
  // The seed's request window told every nurse, with a link to My month.
  await expectThumbSized(page.getByRole("link", { name: /^Open / }).first())

  // The password page is a card of its own, with no header: its way back is a link under the form.
  await menu.click()
  await page.getByRole("navigation", { name: "Main menu" }).getByRole("link", { name: "Change password" }).click()
  await expect(page.getByRole("heading", { level: 1, name: "Change your password" })).toBeVisible()
  await expectNoSidewaysScroll(page)
  await expectThumbSized(page.getByLabel("Current password"))
  await expectThumbSized(page.getByRole("button", { name: "Change password" }))
  const back = page.getByRole("link", { name: "Back to home" })
  await expectThumbSized(back)
  await back.click()
  await expect(page.getByRole("heading", { level: 1, name: stack.names[nurse] })).toBeVisible()
})

test("the roster grid scrolls sideways in its own region, with the nurse column held", async ({ page }) => {
  const nurse = stack.swap.colleague
  await signIn(page, nurse)
  await page.goto(`/my-roster?month=${stack.approved_month}`)
  const grid = page.getByRole("region", { name: `Roster of ${stack.unit}, ${monthName(stack.approved_month)}` })
  const me = grid.getByRole("rowheader", { name: `${stack.names[nurse]} (you)` })
  await grid.scrollIntoViewIfNeeded()
  await expect(me).toBeInViewport()
  await expectNoSidewaysScroll(page)
  await expectThumbSized(page.getByRole("link", { name: "Next month" }))

  const region = await grid.evaluate((element) => ({ scroll: element.scrollWidth, client: element.clientWidth }))
  expect(region.scroll).toBeGreaterThan(region.client)
  const before = (await me.boundingBox())!
  const lastDay = grid.getByRole("columnheader").last()
  await expect(lastDay).not.toBeInViewport()

  await grid.evaluate((element) => element.scrollTo({ left: element.scrollWidth }))

  await expect(lastDay).toBeInViewport()
  expect((await me.boundingBox())!.x).toBe(before.x)
  await expectNoSidewaysScroll(page)
})

test("a unit manager's pages are not made for a phone, but none breaks its width", async ({ page }) => {
  await signIn(page, "mona")
  const menu = page.getByRole("button", { name: "Menu" })
  await menu.click()
  // The password page is the nurse's check's; it has no header to come back by.
  const pages = (await page.getByRole("navigation", { name: "Main menu" }).getByRole("link").allTextContents()).filter((name) => name !== "Change password")
  await menu.click()
  expect(pages).toEqual(expect.arrayContaining(["Roster", "Swaps", "Covers"]))

  for (const name of pages) {
    await menu.click()
    await page.getByRole("navigation", { name: "Main menu" }).getByRole("link", { name, exact: true }).click()
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible()
    await expect(page.getByText("Loading…")).toHaveCount(0)
    await expectNoSidewaysScroll(page)
  }
})

test("the System Admin's pages keep within a phone's width, and no row's buttons cover a name on Users", async ({ page }) => {
  await signIn(page, "admin")
  const menu = page.getByRole("button", { name: "Menu" })
  await menu.click()
  const pages = (await page.getByRole("navigation", { name: "Main menu" }).getByRole("link").allTextContents()).filter((name) => name !== "Change password")
  await menu.click()
  expect(pages).toEqual(expect.arrayContaining(["Health", "Users"]))

  for (const name of pages) {
    await menu.click()
    await page.getByRole("navigation", { name: "Main menu" }).getByRole("link", { name, exact: true }).click()
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible()
    await expect(page.getByText("Loading…")).toHaveCount(0)
    await expectNoSidewaysScroll(page)
  }

  // The backup taken by e2e/stack.sh up: its table scrolls inside its own region.
  await page.goto("/health")
  await expect(page.getByRole("region", { name: /backups/i })).toBeVisible()
  await expectNoSidewaysScroll(page)

  await page.goto("/users")
  const users = page.getByRole("region", { name: "Users" })
  for (const username of ["mona", "carla"]) {
    const name = users.getByRole("cell", { name: stack.names[username], exact: true })
    await expect(name).toBeInViewport()
    const cell = (await name.boundingBox())!
    for (const label of [`Correct name of ${username}`, `Deactivate ${username}`, `Reset password of ${username}`]) {
      const button = (await users.getByRole("button", { name: label }).boundingBox())!
      const overlaps = button.x < cell.x + cell.width && cell.x < button.x + button.width && button.y < cell.y + cell.height && cell.y < button.y + button.height
      expect(overlaps, `${label} covers ${stack.names[username]}`).toBe(false)
    }
  }
})
