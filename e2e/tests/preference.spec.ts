import { expect, type Page } from "@playwright/test"

import { expectNoSidewaysScroll, expectThumbSized, monthName, shortDay, signIn, stack, test } from "../support"

// On a phone, a nurse makes a day-off preference, asks for it as a request, withdraws the request, makes the
// preference again and withdraws it, on My month of the month whose window the seed opened. Withdrawn preferences and requests
// stay on record, but nothing is left live, so the stack is as it was and the check runs again.

const preferences = (page: Page) => page.getByRole("region", { name: `Your preferences for ${monthName(stack.window_month)}` })
const requests = (page: Page) => page.getByRole("region", { name: `Your requests for ${monthName(stack.window_month)}` })

test("a nurse makes a preference on her phone, asks for it as a request, and leaves nothing behind", async ({ page }) => {
  const nurse = stack.swap.proposer
  // Not request.spec.ts's day (the 10th), so the two checks never hold one date between them.
  const day = `${stack.window_month}-12`
  const date = shortDay(day)
  await signIn(page, nurse)
  await page.goto(`/my-month?month=${stack.window_month}`)
  await expect(page.getByText(`Preferences for ${monthName(stack.window_month)} are open until`)).toBeVisible()
  await expect(page.getByText(`Requests for ${monthName(stack.window_month)} are open until`)).toBeVisible()

  // A run that failed half way may have left the preference made, or the request it became waiting: those go first.
  const made = preferences(page).getByRole("row", { name: new RegExp(date) }).filter({ hasText: "Made" })
  const waiting = requests(page).getByRole("row", { name: new RegExp(date) }).filter({ hasText: "Waiting for a decision" })
  const withdrawPreference = page.getByRole("button", { name: `Withdraw your preference for ${date}` })
  const withdrawRequest = page.getByRole("button", { name: `Withdraw your request for ${date}` })
  if (await made.count()) {
    await withdrawPreference.click()
    await page.getByRole("alertdialog").getByRole("button", { name: "Withdraw it" }).click()
    await expect(made).toHaveCount(0)
  }
  if (await waiting.count()) {
    await withdrawRequest.click()
    await expect(waiting).toHaveCount(0)
  }

  const makePreference = async () => {
    const makeIt = page.getByRole("button", { name: "Make the preference" })
    await expectThumbSized(page.getByLabel("Day", { exact: true }))
    await expectThumbSized(page.locator("label", { hasText: "A day off" }))
    await expectThumbSized(makeIt)
    await page.getByLabel("Day", { exact: true }).fill(day)
    await page.getByRole("radio", { name: "A day off" }).check()
    await makeIt.click()
    await expect(page.getByText(`Your preference for a day off on ${date} is made. The solver keeps it when it can.`)).toBeVisible()
    await expect(made).toHaveCount(1)
  }

  await makePreference()
  await expectNoSidewaysScroll(page)
  // The state is in view, not past the region's edge: the table wraps on a phone.
  await made.scrollIntoViewIfNeeded()
  await expect(made.getByText("Made")).toBeInViewport({ ratio: 1 })

  const askInstead = page.getByRole("button", { name: `Ask for your preference for ${date} as a request` })
  await expectThumbSized(askInstead)
  await askInstead.click()
  const question = page.getByRole("alertdialog", { name: `Ask for ${date} as a request?` })
  await expect(question).toContainText("requests a month, pending or accepted")
  // The dialog zooms in from 95%: its size is measured once its own animations have ended, not part way.
  await question.evaluate((dialog) => Promise.all(dialog.getAnimations({ subtree: true }).map((animation) => animation.finished)))
  await expectThumbSized(question.getByRole("button", { name: "Ask for it" }))
  await question.getByRole("button", { name: "Ask for it" }).click()
  await expect(page.getByText(`Your preference for ${date} is now a request for a day off. Your manager decides it.`)).toBeVisible()
  await expect(made).toHaveCount(0)
  await expect(waiting).toHaveCount(1)

  await withdrawRequest.click()
  await expect(page.getByText(`Your request for a day off on ${date} is withdrawn.`)).toBeVisible()
  await expect(waiting).toHaveCount(0)

  await makePreference()
  await expectThumbSized(withdrawPreference)
  await withdrawPreference.click()
  await page.getByRole("alertdialog", { name: `Withdraw your preference for ${date}?` }).getByRole("button", { name: "Withdraw it" }).click()
  await expect(page.getByText(`Your preference for a day off on ${date} is withdrawn.`)).toBeVisible()
  await expect(made).toHaveCount(0)
  await expectNoSidewaysScroll(page)
})
