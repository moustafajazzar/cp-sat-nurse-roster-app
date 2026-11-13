import { expect, type Page } from "@playwright/test"

import { addDays, choose, dayBefore, firstSignIn, go, hospitalToday, journeyStack as on, longDay, monthAfter, monthName, proposeSwap, reset, shortDay, soundSwap, test, toMonth } from "../../support"
import { alongside, as, nameOf, notebook, openFromRosters, people, SOLVED, UNIT } from "./people"

// The hospital's first month, from an empty stack, as the hospital will use it. The System Admin logs in for the first
// time and makes the CNO, the Deputy CNO and a unit manager; the CNO makes a unit and its four nurses; the manager sets the unit
// rules and generates next month's roster with the real solver and worker, and submits it; the Deputy CNO approves it while the
// CNO looks on; a nurse reads it, makes a request that the manager accepts, and proposes a swap a colleague and the manager
// accept; the roster in force is then a second version, which the Deputy CNO reads with its reason.
//
// It runs twice, on a phone (`journey-phone`) and on a desktop (`journey-desktop`), each from an emptied database. Each step is
// a test of its own, in order; what one makes (who, with which password) is in the notebook for the next and for
// `after-journey`, which runs on what the desktop's left.

// A step signs in several people, some for the first time, and waits for a real solve: more than the 30 s default.
test.describe.configure({ mode: "serial", timeout: 120_000 })

const today = hospitalToday()
/** Next month, which the manager rosters; the month after, whose requests the nurses make. */
const rostered = monthAfter(today, 1)
const requested = monthAfter(today, 2)

/** The window the nurses make their requests in: from today, for a few days, and over before the month it is for begins. */
const windowCloses = [addDays(today, 3), dayBefore(requested)].sort()[0]

test.beforeAll(() => {
  reset(on)
  notebook.clear()
  notebook.write({ rostered, requested, windowCloses })
})

/** The password dialog shown once after a creation: read it, then Done. */
async function givenPassword(page: Page, username: string): Promise<string> {
  const dialog = page.getByRole("alertdialog", { name: `Password for ${username}` })
  const password = await dialog.getByLabel("Generated password").inputValue()
  expect(password.length).toBeGreaterThanOrEqual(10)
  await dialog.getByRole("button", { name: "Done" }).click()
  await expect(dialog).toBeHidden()
  return password
}

test("the System Admin logs in for the first time, changes the password, and makes the CNO, the Deputy CNO and a manager", async ({ page }) => {
  const password = await firstSignIn(page, "admin", on.initialAdminPassword)
  notebook.write({ chosen: { admin: password }, given: {} })

  await go(page, "Users")
  const given: Record<string, string> = {}
  for (const person of [people.cno, people.deputy, people.manager]) {
    await page.getByLabel("Role").selectOption({ label: person.role })
    await page.getByLabel("Full name").fill(person.name)
    await page.getByLabel("Username").fill(person.username)
    await page.getByRole("button", { name: "Create user" }).click()
    given[person.username] = await givenPassword(page, person.username)
    await expect(page.getByRole("region", { name: "Users" }).getByRole("row", { name: new RegExp(person.name) })).toBeVisible()
  }
  notebook.write({ given })
})

test("the CNO makes the unit with its manager, and its four nurses", async ({ page }) => {
  await as(page, people.cno.username)
  await go(page, "Units")
  await page.getByLabel("Name").fill(UNIT)
  await choose(page.getByLabel("Manager"), new RegExp(people.manager.name))
  await page.getByRole("button", { name: "Add unit" }).click()
  await expect(page.getByRole("row", { name: new RegExp(`${UNIT}.*${people.manager.name}`) })).toBeVisible()

  await go(page, "Nurses")
  const given = { ...notebook.read().given }
  for (const nurse of people.nurses) {
    await page.getByRole("button", { name: "Add a nurse" }).click()
    await page.getByLabel("Hospital ID").fill(nurse.username)
    await page.getByLabel("Full name").fill(nurse.name)
    await page.getByLabel("Gender").selectOption({ label: nurse.gender })
    await page.getByLabel("Unit").selectOption({ label: UNIT })
    await page.getByLabel("Start date").fill(`${today.slice(0, 7)}-01`)
    await page.getByRole("button", { name: "Add nurse" }).click()
    given[nurse.username] = await givenPassword(page, nurse.username)
  }
  notebook.write({ given })
  for (const nurse of people.nurses) await expect(page.getByRole("cell", { name: nurse.name, exact: true })).toBeVisible()
})

test("the manager sets the unit rules, solves next month for real, and submits the draft", async ({ page }) => {
  await as(page, people.manager.username)
  await expect(page.getByText(`Your unit is ${UNIT}.`)).toBeVisible()
  await go(page, "Unit rules")
  // The solver's four-nurse sample's rules: one nurse on a day, one or two on a night, and no team leader, gender or score rule
  // a unit this small could not meet.
  for (const [shift, counts] of [["D", [1, 1, 1, 1]], ["N", [1, 2, 1, 2]]] as const) {
    const block = page.getByRole("region", { name: new RegExp(`^${shift}\\b`) })
    for (const [index, label] of ["Weekday, at least", "Weekday, at most", "Weekend day, at least", "Weekend day, at most"].entries()) {
      await block.getByLabel(label).fill(String(counts[index]))
    }
  }
  await page.getByRole("checkbox", { name: "A team leader on every shift" }).uncheck()
  await page.getByRole("checkbox", { name: "Both genders on every shift" }).uncheck()
  await page.getByLabel("Score target on a weekday").fill("0")
  await page.getByLabel("Score target on a weekend day").fill("0")
  await page.getByRole("checkbox", { name: "Keep the strength of shifts balanced" }).uncheck()
  await page.getByLabel("Longest run of shifts").fill("4")
  await page.getByRole("button", { name: "Save", exact: true }).click()
  await expect(page.getByText("Saved.")).toBeVisible()

  await go(page, "Roster")
  // The page opens on the next month with no roster yet.
  await expect(page.getByRole("heading", { level: 2, name: monthName(rostered) })).toBeVisible()
  await page.getByLabel("Time limit (seconds)").fill("10")
  await page.getByRole("button", { name: "Start solve" }).click()
  const submit = page.getByRole("region", { name: "Submit for approval" }).getByRole("button", { name: "Submit for approval" })
  await expect(submit).toBeEnabled(SOLVED)
  await submit.click()
  await expect(page.getByText("Submitted. The CNO and the Deputy CNO can now see it and decide. Nothing can change it until they do.")).toBeVisible()
})

test("the Deputy CNO approves the roster, and the CNO, who had it open too, is told it already was", async ({ browser, page }) => {
  await as(page, people.cno.username)
  // Home lists what waits for a decision, for either approver.
  await expect(page.getByText(UNIT)).toBeVisible()
  await openFromRosters(page, "Submitted")
  await expect(page.getByRole("button", { name: "Approve" })).toBeEnabled()

  const deputy = await alongside(browser, page, people.deputy.username)
  await openFromRosters(deputy, "Submitted")
  await deputy.getByRole("button", { name: "Approve" }).click()
  await deputy.getByRole("alertdialog").getByRole("button", { name: "Approve" }).click()
  await expect(deputy.getByText(`Approved. The roster of ${monthName(rostered)} is in force, and the unit's nurses see it.`)).toBeVisible()
  await deputy.context().close()

  // The CNO's page is as it was before the Deputy CNO decided: the second approval is the race's loser.
  await page.getByRole("button", { name: "Approve" }).click()
  const dialog = page.getByRole("alertdialog")
  await dialog.getByRole("button", { name: "Approve" }).click()
  await expect(dialog.getByText(`This roster has been approved already by ${people.deputy.name}.`)).toBeVisible()
})

const requestDay = `${requested}-10`

test("the manager opens a request window; a nurse reads the roster in force and makes a request, which the manager accepts", async ({ browser, page }) => {
  await as(page, people.manager.username)
  await go(page, "Requests")
  await page.getByLabel("Month").fill(requested)
  await page.getByLabel("First day").fill(today)
  await page.getByLabel("Last day").fill(windowCloses)
  await page.getByRole("button", { name: "Open the window" }).click()
  await expect(page.getByText(`Requests for ${monthName(requested)} are open until the end of ${shortDay(windowCloses)}. The unit's nurses of that month are told.`)).toBeVisible()

  const [sara] = people.nurses
  const nurse = await alongside(browser, page, sara.username)
  // Home speaks of this month, which has no roster: the hospital's first is next month's.
  await expect(nurse.getByText(`No roster yet for ${monthName(today.slice(0, 7))}.`)).toBeVisible()
  await nurse.getByRole("main").getByRole("link", { name: "My roster" }).click()
  await toMonth(nurse, "Next month", rostered)
  const grid = nurse.getByRole("region", { name: `Roster of ${UNIT}, ${monthName(rostered)}` })
  await expect(grid.getByRole("rowheader", { name: `${sara.name} (you)` })).toBeVisible()
  for (const colleague of people.nurses.slice(1)) await expect(grid.getByRole("rowheader", { name: colleague.name })).toBeVisible()

  await nurse.getByRole("link", { name: /^Notifications/ }).click()
  await nurse.getByRole("link", { name: /^Open / }).first().click()
  await expect(nurse.getByText(`Requests for ${monthName(requested)} are open until`)).toBeVisible()
  await nurse.getByLabel("Date").fill(requestDay)
  await nurse.getByLabel("What you ask for").selectOption({ label: "A day off" })
  await nurse.getByLabel("Reason (optional)").fill("A family wedding")
  await nurse.getByRole("button", { name: "Make the request" }).click()
  await expect(nurse.getByText(`Your request for a day off on ${shortDay(requestDay)} is made. Your manager decides it.`)).toBeVisible()

  await page.reload()
  // The window open is the one shown, with its requests.
  await expect(page.getByRole("region", { name: "Request windows, newest first" }).getByRole("row", { name: new RegExp(monthName(requested)) }))
    .toContainText("1, 1 waiting")
  await page.getByRole("button", { name: `Accept ${sara.name}'s request for ${shortDay(requestDay)}` }).click()
  const dialog = page.getByRole("alertdialog")
  await dialog.getByRole("button", { name: "Accept the request" }).click()
  await expect(dialog).toBeHidden()
  await expect(page.getByRole("row", { name: new RegExp(sara.name) }).getByText("Accepted")).toBeVisible()

  await nurse.reload()
  const mine = nurse.getByRole("region", { name: `Your requests for ${monthName(requested)}` })
  await expect(mine.getByRole("row", { name: new RegExp(shortDay(requestDay)) }).getByText("Accepted")).toBeVisible()
  await nurse.context().close()
})

test("a nurse proposes a swap, the colleague in it accepts, the manager approves, and the roster in force is a new version", async ({ browser, page }) => {
  // One the app's own judging finds sound on the roster the solver made: the proposer's shift goes to a colleague off that day.
  const swap = soundSwap(on, UNIT, rostered)
  notebook.write({ swap })
  await as(page, swap.proposer)
  await go(page, "Swaps")
  await toMonth(page, "Next month", rostered)
  await expect(page.getByRole("heading", { level: 2, name: "Your swaps" })).toBeVisible()
  await proposeSwap(page, swap, nameOf(swap.colleague))

  const colleague = await alongside(browser, page, swap.colleague)
  await go(colleague, "Swaps")
  // A nurse's Swaps opens on this month; the swap is on next month's roster.
  await toMonth(colleague, "Next month", rostered)
  await colleague.getByRole("button", { name: new RegExp(`^Accept ${nameOf(swap.proposer)}'s swap on `) }).click()
  await expect(colleague.getByText(`You accepted ${nameOf(swap.proposer)}'s swap. Everyone in it has now accepted, so it waits for the manager.`)).toBeVisible()

  const manager = await alongside(browser, page, people.manager.username)
  await go(manager, "Swaps")
  // It opens on the month of the swap waiting for the manager, and shows that one, before and after.
  await expect(manager.getByRole("heading", { level: 2, name: monthName(rostered) })).toBeVisible()
  await expect(manager.getByRole("region", { name: "The unit's swaps, newest first" }).getByRole("row", { name: new RegExp(nameOf(swap.proposer)) }))
    .toBeVisible()
  await expect(manager.getByText(/^With its cells the roster breaks no hard rule\. It costs /)).toBeVisible()
  await manager.getByRole("button", { name: "Approve", exact: true }).click()
  await manager.getByRole("alertdialog").getByRole("button", { name: "Approve the swap" }).click()
  await expect(manager.getByText(/^You approved .* Version 2 of the roster is now in force, its cells pinned\./)).toBeVisible()

  // The roster in force, as each reads it: the manager's version list, and the two nurses' own rows on My roster.
  await go(manager, "Roster")
  const versions = manager.getByRole("table", { name: "Versions of this roster, newest first" })
  await expect(versions.getByRole("row", { name: /^2 \(in force\) Swap/ })).toBeVisible()
  for (const [who, shown] of [[colleague, swap.shift], [page, "Off"]] as const) {
    await go(who, "My roster")
    await toMonth(who, "Next month", rostered)
    const grid = who.getByRole("region", { name: `Roster of ${UNIT}, ${monthName(rostered)}` })
    const mine = grid.getByRole("row", { name: /\(you\)/ })
    await expect(mine.getByRole("cell").nth(Number(swap.date.slice(8)) - 1)).toHaveText(shown)
  }
  for (const other of [colleague, manager]) await other.context().close()
})

test("the Deputy CNO reads the roster's versions, the swap's among them with its reason, and compares the two", async ({ page }) => {
  await as(page, people.deputy.username)
  await openFromRosters(page, "Approved")
  const versions = page.getByRole("table", { name: "Versions of this roster, newest first" })
  await expect(versions.getByRole("row", { name: /^1 Approved from a solve/ })).toContainText(people.deputy.name)
  const { swap } = notebook.read()
  const second = versions.getByRole("row", { name: /^2 \(in force\) Swap/ })
  await expect(second).toContainText(people.manager.name)
  // The reason is for the manager and the approvers: who proposed it, who was in it, and its day.
  await expect(second.getByRole("cell", { name: /^Swap proposed by/ })).toHaveText(
    `Swap proposed by ${nameOf(swap.proposer)} approved: ${nameOf(swap.proposer)}, ${nameOf(swap.colleague)} on ${longDay(swap.date)}.`)

  // The latest change is the pair shown first: version 1 against 2, the swap's two cells.
  const differ = page.getByRole("table", { name: "Cells that differ from version 1 to version 2" })
  await expect(page.getByRole("status").filter({ hasText: "2 cells differ from version 1 to version 2." })).toBeVisible()
  const day = longDay(swap.date).replace(/ \d{4}$/, "")
  await expect(differ.getByRole("row", { name: `${nameOf(swap.proposer)} ${day} ${swap.shift} Off` })).toBeVisible()
  await expect(differ.getByRole("row", { name: `${nameOf(swap.colleague)} ${day} Off ${swap.shift}` })).toBeVisible()
})
