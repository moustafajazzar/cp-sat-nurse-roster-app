import { expect, type Page } from "@playwright/test"

import { fire, monthName, signIn, stack, test } from "../../support"

// The manager's month ends on the Roster page's "Carry-over and month close" card. The seed approved
// ER's last month and left it open; the hourly catch-up, run now by the stack's own worker, closes it on the schedule's account
// and tells the manager. Leave recorded late then makes the card say the facts changed, and "Close … again" takes it in.
// Removing that leave and closing again puts the month back as the facts were, so this runs again; on a second run the card
// already says the month closed, and the late leave and the two closes are as on the first. A nurse then reads My month of the
// closed month.

const past = stack.past_month
const nurse = stack.swap.colleague
const lateDay = `${past}-15`

/** A page of the manager's at last month, by its address: the pages open on the coming month. */
async function atPastMonth(page: Page, path: "/roster" | "/leave") {
  await page.goto(`${path}?month=${past}`)
  await expect(page.getByRole("navigation", { name: "Month" }).getByRole("heading", { level: 2, name: monthName(past) })).toBeVisible()
}

async function openPastMonth(page: Page) {
  await atPastMonth(page, "/roster")
  return page.getByRole("region", { name: "Carry-over and month close" })
}

test("a month over closes on its own by the hourly catch-up; late leave, then Close again, takes it in", async ({ page }) => {
  fire(stack, "closes.tasks.close_month")
  await signIn(page, "mona")
  await page.getByRole("link", { name: /^Notifications/ }).click()
  await expect(page.getByRole("listitem").filter({ hasText: new RegExp(`^(New )?The ER roster for ${monthName(past)} has closed\\. Its recorded facts broke`) }).first())
    .toBeVisible()

  const card = await openPastMonth(page)
  await expect(card.getByText(/^Closed .* with version 1 as the roster in force\.$/)).toBeVisible()
  await expect(card.getByRole("button", { name: `Close ${monthName(past)} again` })).toBeVisible()

  // Late leave, on the Leave page of that month.
  await atPastMonth(page, "/leave")
  await page.getByRole("button", { name: "Record leave" }).click()
  await page.getByLabel("Nurse").selectOption({ label: stack.names[nurse] })
  await page.getByLabel("Leave type").selectOption({ label: "Annual" })
  await page.getByLabel("First day").fill(lateDay)
  await page.getByRole("button", { name: "Save" }).click()
  const leave = page.getByRole("button", { name: new RegExp(`^Remove leave of ${stack.names[nurse]}, `) })
  await expect(leave).toBeVisible()

  const changed = "Leave or a nurse's dates have changed since it closed. Close it again to take them in."
  const closedAgain = `${monthName(past)} is closed. Each nurse's carry-over below is this close's.`
  let again = await openPastMonth(page)
  await expect(again.getByText(changed)).toBeVisible()
  await again.getByRole("button", { name: `Close ${monthName(past)} again` }).click()
  await expect(again.getByText(closedAgain)).toBeVisible()
  await expect(again.getByText(/^Closed .* by Mona Ali, with version 1 as the roster in force\.$/)).toBeVisible()
  await expect(again.getByText(changed)).toHaveCount(0)

  // Put back: the leave removed, and the month closed again on the facts as they were.
  await atPastMonth(page, "/leave")
  await leave.click()
  await page.getByRole("alertdialog").getByRole("button", { name: "Remove leave" }).click()
  await expect(leave).toHaveCount(0)
  again = await openPastMonth(page)
  await expect(again.getByText(changed)).toBeVisible()
  await again.getByRole("button", { name: `Close ${monthName(past)} again` }).click()
  await expect(again.getByText(closedAgain)).toBeVisible()
})

test("a nurse reads My month of a closed month: what really happened", async ({ page }) => {
  await signIn(page, nurse)
  await page.goto(`/my-month?month=${past}`)
  await expect(page.getByRole("heading", { level: 2, name: monthName(past) })).toBeVisible()
  await expect(page.getByText(/The month has closed: these are what really happened, with your leave\./)).toBeVisible()
  await expect(page.getByRole("term").filter({ hasText: "Hours worked" })).toBeVisible()
})
