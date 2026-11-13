import { expect } from "@playwright/test"

import { another, expectNoSidewaysScroll, go, monthName, proposeSwap, shortDay, signIn, soundSwap, stack, test } from "../../support"
import { replanTheLargeUnit } from "../replan"

// A unit manager's Requests and Swaps pages, and a re-plan's result, on a 375 px phone (an
// iPhone's width; the `phone-375` project). A manager's pages are not made for a phone, so what holds is that none makes the
// page itself scroll sideways: their tables scroll inside their own regions, which are in view.

test("the manager's Requests page, with its windows and requests, keeps within a phone's width", async ({ page }) => {
  await signIn(page, "mona")
  await go(page, "Requests")
  const windows = page.getByRole("region", { name: "Request windows, newest first" })
  await expect(windows.getByRole("row", { name: new RegExp(monthName(stack.window_month)) })).toBeVisible()
  await expect(page.getByRole("region", { name: `Requests for ${monthName(stack.window_month)}, by date` })
    .or(page.getByText("No requests were made in this window."))).toBeVisible()
  await expectNoSidewaysScroll(page)
})

test("the manager's Swaps page, a swap before and after among it, keeps within a phone's width", async ({ browser, page }) => {
  // A swap of its own to read, withdrawn at the end.
  const swap = soundSwap(stack, stack.unit, stack.approved_month)
  const nurse = await another(browser, page, swap.proposer)
  await nurse.goto(`/swaps?month=${stack.approved_month}`)
  await proposeSwap(nurse, swap, stack.names[swap.colleague])

  await signIn(page, "mona")
  await go(page, "Swaps")
  await page.goto(`/unit-swaps?month=${stack.approved_month}`)
  await expect(page.getByRole("heading", { level: 2, name: monthName(stack.approved_month) })).toBeVisible()
  await expect(page.getByRole("region", { name: "The unit's swaps, newest first" })).toBeVisible()
  await page.getByRole("link", { name: `Show ${stack.names[swap.proposer]}'s swap on ${shortDay(swap.date)}` }).first().click()
  await expect(page.getByRole("region", { name: `${stack.names[swap.proposer]}'s swap, before and after` })).toBeVisible()
  await expectNoSidewaysScroll(page)

  await nurse.getByRole("button", { name: `Withdraw your swap on ${shortDay(swap.date)}` }).click()
  await nurse.getByRole("alertdialog").getByRole("button", { name: "Withdraw the swap" }).click()
  await expect(nurse.getByText("You withdrew your swap. Everyone in it is told.")).toBeVisible()
  await nurse.context().close()
})

test("a re-plan's result keeps within a phone's width, its comparison tables scrolling in their own regions", async ({ page }) => {
  test.setTimeout(180_000)
  await signIn(page, "omar")
  const result = await replanTheLargeUnit(page)
  await expectNoSidewaysScroll(page)
  const rules = result.getByRole("table", { name: "Each rule, before and after" })
  await rules.scrollIntoViewIfNeeded()
  await expect(rules.getByRole("columnheader", { name: "Rule" })).toBeInViewport()
  await expectNoSidewaysScroll(page)
})
