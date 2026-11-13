import { expect } from "@playwright/test"

import { anotherPhone, desktop, expectThumbSized, go, monthName, signIn, soundCover, stack, test } from "../support"

// A sick call answered by a cover. On ICU's approved month, the large unit, whose desktop checks already read the
// latest version whatever it is, so ER's swap and its readers are left alone. `e2e/stack.sh cover` finds, on the month as it
// stands, a staff nurse's shift from tomorrow on that sick leave would take off and a colleague the app's judging lists for it.
// The manager records the leave on the desktop, opens the cover from the leave form's answer and offers it to that colleague,
// who accepts on her phone; My roster then shows her cell. At the end the manager removes the leave, which puts nothing back:
// the shift-day is as full as before, with the colleague in it, and the absent nurse's day is a day off. The check runs again
// on a later day, or on whoever is next, since the finder reads the month as it stands.

/** The page says an offer or an acceptance happened only once its request and a fresh read of the cover are both back, and
 *  each of those judges the cover against ICU's nurses. On a CI runner an offer came to about five seconds (3.0 s and 1.8 s),
 *  the whole of expect's default wait, so these two waits allow for the app's work rather than for a page already drawn. */
const JUDGED = { timeout: 20_000 }

test("a sick call: the manager offers the cover on the desktop, and a colleague takes it on her phone", async ({ browser, page }) => {
  const month = stack.approved_month
  const { absent, colleague, date, shift } = soundCover(stack, stack.large_unit, month)
  const day = new Date(`${date}T00:00:00Z`).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" })

  const desk = await browser.newContext({ ...desktop, baseURL: stack.baseURL })
  const manager = await desk.newPage()
  await signIn(manager, "omar")
  await manager.goto(`/leave?month=${month}`)
  await manager.getByRole("button", { name: "Record leave" }).click()
  await manager.getByLabel("Nurse").selectOption({ label: stack.names[absent] })
  await manager.getByLabel("Leave type").selectOption({ label: "Sick" })
  await manager.getByLabel("First day").fill(date)
  await manager.getByRole("button", { name: "Save" }).click()
  await expect(manager.getByText(/It opened a cover for one more nurse on that shift, not offered to anyone yet/)).toBeVisible()

  await manager.getByRole("link", { name: `Cover for ${shift} on ${day}` }).click()
  const who = manager.getByRole("region", { name: "Who could take it, fairest first" })
  await expect(who).toBeVisible()
  const ticks = who.getByRole("checkbox")
  for (const tick of await ticks.all()) {
    const label = await tick.getAttribute("aria-label")
    if (label !== `Offer it to ${stack.names[colleague]}`) await tick.uncheck()
  }
  await expect(who.getByRole("checkbox", { name: `Offer it to ${stack.names[colleague]}` })).toBeChecked()
  await manager.getByLabel("Note (optional)").fill("Thank you for helping out.")
  await manager.getByRole("button", { name: "Offer the cover" }).click()
  await expect(manager.getByText(/^You offered the cover for .* to 1 nurse\. Each is told\.$/)).toBeVisible(JUDGED)

  const theirs = await anotherPhone(browser, page, colleague)
  // Covers from the phone's Menu, then the month the cover is in, which may not be the hospital's current one.
  await go(theirs, "Covers")
  await theirs.goto(`/covers?month=${month}`)
  await expect(theirs.getByText("Your manager's note: Thank you for helping out.").first()).toBeVisible()
  const accept = theirs.getByRole("button", { name: new RegExp(`^Accept the cover for .* on ${day}$`) })
  await expectThumbSized(accept)
  await expectThumbSized(theirs.getByRole("button", { name: new RegExp(`^Decline the cover for .* on ${day}$`) }))
  await accept.click()
  await expect(theirs.getByText(/^You took the cover for .*\. It is in your cell on My roster now\.$/)).toBeVisible(JUDGED)

  await theirs.goto(`/my-roster?month=${month}`)
  const grid = theirs.getByRole("region", { name: `Roster of ${stack.large_unit}, ${monthName(month)}` })
  const mine = grid.getByRole("row", { name: /\(you\)/ })
  await expect(mine.getByRole("cell").nth(Number(date.slice(8)) - 1)).toHaveText(shift)
  await theirs.context().close()

  await manager.reload()
  await expect(manager.getByText(new RegExp(`Taken by ${stack.names[colleague]}, in version \\d+`))).toBeVisible()
  await manager.goto(`/leave?month=${month}`)
  await manager.getByRole("button", { name: new RegExp(`^Remove leave of ${stack.names[absent]}, `) }).click()
  await manager.getByRole("alertdialog").getByRole("button", { name: "Remove leave" }).click()
  await expect(manager.getByText(new RegExp(`^The leave of ${stack.names[absent]} is removed`))).toBeVisible()
  await desk.close()
})
