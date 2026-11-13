import { expect, type Page } from "@playwright/test"

import { go, monthName, stack } from "../support"

/** A re-plan and the real solver at sixty-four nurses: the wait is for its end, up to its time limit and the queue's turn. */
export const REPLANNED = { timeout: 120_000 }

/** Omar re-plans ICU's approved month with the real solver and worker, from the earliest date it may (the month's first day,
 *  while it is still to come), then opens the result from the solve history: its comparison with the roster in force it
 *  started from. Each run is a new re-plan, on whatever version is in force then. */
export async function replanTheLargeUnit(page: Page) {
  await go(page, "Roster")
  await expect(page.getByRole("heading", { level: 2, name: monthName(stack.approved_month) })).toBeVisible()
  const replan = page.getByRole("region", { name: "Re-plan from a date" })
  await replan.getByLabel("Time limit (seconds)").fill("10")
  await replan.getByRole("button", { name: /^Re-plan/ }).click()
  await expect(page.getByRole("status").filter({ hasText: /^The re-plan (finished|stopped|ended)/ })).toBeVisible(REPLANNED)

  await go(page, "Solve history")
  // Newest first: the re-plan just ended.
  await page.getByRole("link", { name: /^Open the result of the solve asked for at / }).first().click()
  const result = page.getByRole("region", { name: "Against the roster in force" })
  await expect(result).toContainText(/Re-planned from \d+ /)
  await expect(result.getByRole("table", { name: "Each rule, before and after" })).toBeVisible()
  return result
}
