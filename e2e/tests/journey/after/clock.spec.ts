import { expect, type Page } from "@playwright/test"

import { addDays, daysBetween, fire, go, hospitalToday, journeyStack as on, longDay, monthAfter, monthName, moveClock, proposeSwap, shortDay, soundSwap, test } from "../../../support"
import { alongside, as, nameOf, notebook, people, UNIT } from "../people"

// What happens by date alone, on what the desktop journey left. The checks' clock (`e2e/stack.sh clock`, the
// E2E_CLOCK_FILE that only e2e/compose.e2e.yaml sets) moves the hospital's date for `web` and the worker; the hourly jobs are then
// run by the stack's own worker at once (`e2e/stack.sh fire`), rather than waited for until their minute. Every page reads the
// dates itself, so a page says what the date says before the job runs; who is told waits for the job, which is what each check
// reads last. The clock is put back after each, whatever happened; the journey's stack is emptied before its next journey.

test.describe.configure({ mode: "serial", timeout: 120_000 })
test.afterEach(() => moveClock(on, 0))

const today = hospitalToday()
const [, , , karim] = people.nurses

/** The notification a person was sent, on their Notifications page ("New" before it while unread). */
async function told(page: Page, words: string) {
  await page.getByRole("link", { name: /^Notifications/ }).click()
  await expect(page.getByRole("heading", { level: 1, name: "Notifications" })).toBeVisible()
  await expect(page.getByRole("list", { name: "Notifications, newest first" }).getByRole("listitem").filter({ hasText: words }).first()).toBeVisible()
}

test("a request window closes by its date and the next opens by its own, each told by the hourly job in the worker", async ({ browser, page }) => {
  const { requested, windowCloses } = notebook.read()
  const roster = `${UNIT} roster for ${monthName(requested)}`
  await as(page, karim.username)
  await page.goto(`/my-month?month=${requested}`)
  await expect(page.getByText(`Requests for ${monthName(requested)} are open until the end of ${shortDay(windowCloses)}.`, { exact: false })).toBeVisible()
  const requestDay = `${requested}-12`
  await page.getByLabel("Date").fill(requestDay)
  await page.getByLabel("What you ask for").selectOption({ label: "A day off" })
  await page.getByRole("button", { name: "Make the request" }).click()
  await expect(page.getByText(`Your request for a day off on ${shortDay(requestDay)} is made. Your manager decides it.`)).toBeVisible()

  // The day after its last day: closed on every page at once, and the manager told of the request waiting once the job ran.
  const closedOn = daysBetween(today, windowCloses) + 1
  moveClock(on, closedOn)
  await page.reload()
  await expect(page.getByText(`The request window for ${monthName(requested)} has closed. Your manager decides any request still waiting.`)).toBeVisible()
  fire(on, "requesting.tasks.open_and_close_request_windows")
  const manager = await alongside(browser, page, people.manager.username)
  await told(manager, `The request window for the ${roster} has closed, and 1 request waits for your decision.`)

  // The next month's window, opening two days on: scheduled, then open on its first day, and its nurses told by the job.
  const next = monthAfter(`${requested}-01`, 1)
  const opens = addDays(today, closedOn + 2)
  const closes = addDays(opens, 1)
  await go(manager, "Requests")
  await manager.getByLabel("Month").fill(next)
  await manager.getByLabel("First day").fill(opens)
  await manager.getByLabel("Last day").fill(closes)
  await manager.getByRole("button", { name: "Open the window" }).click()
  await expect(manager.getByText(`Requests for ${monthName(next)} open on ${shortDay(opens)}. The unit's nurses of that month are told then.`)).toBeVisible()
  await page.goto(`/my-month?month=${next}`)
  await expect(page.getByText(`Requests for ${monthName(next)} open on ${shortDay(opens)} and are taken until the end of ${shortDay(closes)}.`)).toBeVisible()

  moveClock(on, closedOn + 2)
  await page.reload()
  await expect(page.getByText(`Requests for ${monthName(next)} are open until the end of ${shortDay(closes)}.`, { exact: false })).toBeVisible()
  fire(on, "requesting.tasks.open_and_close_request_windows")
  await told(page, `${people.manager.name} opened requests for the ${UNIT} roster for ${monthName(next)}, until ${longDay(closes)}.`)
  await manager.context().close()
})

test("a swap still waiting when its date passes expires, and the hourly job tells the colleague in it", async ({ browser, page }) => {
  const { rostered } = notebook.read()
  const swap = soundSwap(on, UNIT, rostered)
  await as(page, swap.proposer)
  await page.goto(`/swaps?month=${rostered}`)
  await proposeSwap(page, swap, nameOf(swap.colleague))
  const mine = page.getByRole("heading", { level: 3, name: `Your swap on ${shortDay(swap.date)}: Waiting for its colleagues` })
  await expect(mine).toBeVisible()

  // The day after its date: expired on the page at once, the colleague told once the job ran.
  moveClock(on, daysBetween(today, swap.date) + 1)
  await page.reload()
  await expect(page.getByRole("heading", { level: 3, name: `Your swap on ${shortDay(swap.date)}: Expired` })).toBeVisible()
  fire(on, "swaps.tasks.expire_swaps")
  const colleague = await alongside(browser, page, swap.colleague)
  await told(colleague, `The swap ${nameOf(swap.proposer)} proposed on the ${UNIT} roster for ${monthName(rostered)} has expired, so it changes nothing.`)
  await colleague.context().close()
})
