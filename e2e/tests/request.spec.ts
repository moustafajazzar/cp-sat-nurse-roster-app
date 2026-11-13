import { expect } from "@playwright/test"

import { expectThumbSized, monthName, shortDay, signIn, stack, test } from "../support"

// A request made and withdrawn on a phone, on My month of the month whose window the seed opened. Withdrawing puts
// the stack back as it was, so the check runs again.

test("a nurse makes a request on her phone, then withdraws it", async ({ page }) => {
  const nurse = stack.swap.proposer
  const day = `${stack.window_month}-10`
  await signIn(page, nurse)
  await page.goto(`/my-month?month=${stack.window_month}`)
  await expect(page.getByText(`Requests for ${monthName(stack.window_month)} are open until`)).toBeVisible()

  // A withdrawn request stays on record, so a run before left a row of its own for the day; one that failed half way may have
  // left it waiting, which would refuse a second one on the same date. That one goes first.
  const requests = page.getByRole("region", { name: `Your requests for ${monthName(stack.window_month)}` })
  const waiting = requests.getByRole("row", { name: new RegExp(shortDay(day)) }).filter({ hasText: "Waiting for a decision" })
  const withdraw = waiting.getByRole("button", { name: `Withdraw your request for ${shortDay(day)}` })
  const withdrawn = page.getByText(`Your request for a day off on ${shortDay(day)} is withdrawn.`)
  // The open-window sentence above comes in the same answer as the rows, so they are on the page by now.
  if (await waiting.count()) {
    await withdraw.click()
    await expect(withdrawn).toBeVisible()
  }

  const make = page.getByRole("button", { name: "Make the request" })
  await expectThumbSized(page.getByLabel("Date"))
  await expectThumbSized(page.getByLabel("What you ask for"))
  await expectThumbSized(make)
  await page.getByLabel("Date").fill(day)
  await page.getByLabel("What you ask for").selectOption({ label: "A day off" })
  await page.getByLabel("Reason (optional)").fill("A family wedding")
  await make.click()

  await expect(page.getByText(`Your request for a day off on ${shortDay(day)} is made. Your manager decides it.`)).toBeVisible()
  await expect(waiting).toHaveCount(1)
  // The outcome is in view, not past the region's edge: the table wraps on a phone.
  await waiting.scrollIntoViewIfNeeded()
  await expect(waiting.getByText("Waiting for a decision")).toBeInViewport({ ratio: 1 })

  await expectThumbSized(withdraw)
  await withdraw.click()

  await expect(withdrawn).toBeVisible()
  await expect(waiting).toHaveCount(0)
})
