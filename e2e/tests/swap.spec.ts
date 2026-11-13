import { expect } from "@playwright/test"

import { anotherPhone, expectThumbSized, signIn, stack, test } from "../support"

// A swap proposed on one phone and accepted on another, on the approved month. The seed found the swap sound with the
// app's own judging: the proposer's shift goes to a colleague who is off that day. The proposer withdraws it at the end, which
// puts the stack back as it was, so the check runs again; the manager's decision is the journey's.

test("a nurse proposes a swap on her phone and the colleague in it accepts on theirs", async ({ browser, page }) => {
  const { proposer, colleague, date, shift } = stack.swap
  await signIn(page, proposer)
  await page.goto(`/swaps?month=${stack.approved_month}`)
  await expect(page.getByRole("heading", { level: 2, name: "Your swaps" })).toBeVisible()
  await expect(page.getByText("Loading…")).toHaveCount(0)

  // A run that failed half way may have left its swap waiting, which would hold the cells: it is withdrawn first.
  const withdraw = page.getByRole("button", { name: /^Withdraw your swap on / })
  const withdrawn = page.getByText("You withdrew your swap. Everyone in it is told.")
  const confirm = page.getByRole("alertdialog").getByRole("button", { name: "Withdraw the swap" })
  for (let left = await withdraw.count(); left > 0; left -= 1) {
    await withdraw.first().click()
    await confirm.click()
    await expect(withdraw).toHaveCount(left - 1)
  }

  const cell = (index: number, field: "nurse" | "date" | "shift") => page.locator(`#swap-cell-${index}-${field}`)
  await expectThumbSized(cell(0, "nurse"))
  await cell(0, "date").selectOption(date)
  await cell(0, "shift").selectOption("")
  const add = page.getByRole("button", { name: "Add a cell" })
  await expectThumbSized(add)
  await add.click()
  await cell(1, "nurse").selectOption(colleague)
  await cell(1, "date").selectOption(date)
  await cell(1, "shift").selectOption(shift)
  await page.getByRole("button", { name: "Propose the swap" }).click()

  await expect(page.getByText(`Your swap is proposed. ${stack.names[colleague]} is asked to accept it, then your manager decides.`)).toBeVisible()

  const theirs = await anotherPhone(browser, page, colleague)
  await theirs.goto("/swaps")
  const accept = theirs.getByRole("button", { name: new RegExp(`^Accept ${stack.names[proposer]}'s swap on `) })
  await expectThumbSized(accept)
  await accept.click()
  await expect(theirs.getByText(`You accepted ${stack.names[proposer]}'s swap. Everyone in it has now accepted, so it waits for the manager.`)).toBeVisible()
  await theirs.context().close()

  await page.reload()
  // Every column of the swap's cells is in view, "Becomes" too: the table wraps on a phone.
  const cells = page.getByRole("region", { name: /^Your swap on .*, its cells$/ }).first()
  await cells.scrollIntoViewIfNeeded()
  await expect(cells.getByRole("columnheader", { name: "Becomes" })).toBeInViewport({ ratio: 1 })
  await expectThumbSized(withdraw)
  await withdraw.click()
  await confirm.click()
  await expect(withdrawn).toBeVisible()
  await expect(withdraw).toHaveCount(0)
})
