import { expect } from "@playwright/test"

import { another, monthName, proposeSwap, shortDay, signIn, soundSwap, stack, test } from "../../support"
import { replanTheLargeUnit } from "../replan"

// A large unit, ICU's sixty-four nurses (e2e/seed.py), as its manager and nurses use it. Its draft's grid is long
// enough to be windowed, and is moved through and edited with the keyboard; a re-plan of its approved month runs with the real
// solver and shows its comparison; a swap there is checked on the spot. How long these take is for an installation to time,
// on the real server: nothing here times them. Each puts back what it changed, or leaves what any run may leave (a re-plan's new
// version).

const ICU = stack.large_unit
const NURSES = 64

test("a sixty-four-nurse draft is windowed, and is moved through and edited with the arrow keys", async ({ page }) => {
  await signIn(page, "omar")
  await page.goto(`/roster?month=${stack.window_month}`)
  const grid = page.getByRole("grid", { name: `Draft of ${ICU}, ${monthName(stack.window_month)}` })
  await expect(grid.getByRole("rowheader").first()).toBeVisible()
  // Windowed: only the rows in view and a few beyond are in the page, out of sixty-four.
  expect(await grid.getByRole("rowheader").count()).toBeLessThan(NURSES)

  // From the first cell, a key at a time: PageDown is the last nurse's row, which was not in the page before.
  const first = grid.locator("#cell-0-0")
  await first.focus()
  await page.keyboard.press("ArrowRight")
  await page.keyboard.press("ArrowDown")
  await expect(grid.locator("#cell-1-1")).toBeFocused()
  await page.keyboard.press("PageDown")
  const last = grid.locator(`#cell-${NURSES - 1}-1`)
  await expect(last).toBeFocused()
  await expect(last).toBeInViewport()
  await page.keyboard.press("End")
  const corner = grid.locator(`[id^="cell-${NURSES - 1}-"]`).last()
  await expect(corner).toBeFocused()

  // Enter opens the cell's menu, as a click would; a pin is one change waiting to be saved, and Discard puts it back.
  await page.keyboard.press("Enter")
  await page.getByRole("menuitem", { name: "Pin a day off" }).click()
  const unsaved = page.getByRole("region", { name: "1 change not saved yet" })
  await expect(unsaved).toBeVisible()
  await expect(corner).toHaveAccessibleName(/to be pinned off, not saved$/)
  await unsaved.getByRole("button", { name: "Discard changes" }).click()
  await expect(unsaved).toHaveCount(0)
})

test("a re-plan of the sixty-four-nurse month runs for real and shows what it changed and what that cost", async ({ page }) => {
  test.setTimeout(180_000)
  await signIn(page, "omar")
  const result = await replanTheLargeUnit(page)
  await expect(result.getByText(/^Cost before/)).toBeVisible()
})

test("a swap in the sixty-four-nurse unit is checked on the spot when proposed, then withdrawn", async ({ browser, page }) => {
  const swap = soundSwap(stack, ICU, stack.approved_month)
  await signIn(page, swap.proposer)
  await page.goto(`/swaps?month=${stack.approved_month}`)
  await proposeSwap(page, swap, stack.names[swap.colleague])

  // The manager reads it checked on the month as it stands, before its colleague has answered.
  const manager = await another(browser, page, "omar")
  await manager.goto(`/unit-swaps?month=${stack.approved_month}`)
  // Newest first: a run before may have left a withdrawn one on the same day.
  await manager.getByRole("link", { name: `Show ${stack.names[swap.proposer]}'s swap on ${shortDay(swap.date)}` }).first().click()
  await expect(manager.getByText(/^With its cells the roster breaks no hard rule\./)).toBeVisible()
  await manager.context().close()

  await page.getByRole("button", { name: `Withdraw your swap on ${shortDay(swap.date)}` }).click()
  await page.getByRole("alertdialog").getByRole("button", { name: "Withdraw the swap" }).click()
  await expect(page.getByText("You withdrew your swap. Everyone in it is told.")).toBeVisible()
})
