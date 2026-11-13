import { expect, type Browser, type Page } from "@playwright/test"

import { firstSignIn, go, journeyStack as on, newBrowser, Notebook, signIn, type Swap } from "../../support"

/** Who the journey makes, what it keeps in its notebook (e2e/.stack/<project>.journey.json) for the steps after. */
export const UNIT = "Maternity"

export const people = {
  cno: { username: "nadia", name: "Nadia Faris", role: "CNO" },
  deputy: { username: "yusuf", name: "Yusuf Rahman", role: "Deputy CNO" },
  manager: { username: "rana", name: "Rana Haddad", role: "Unit Manager" },
  // The solver's four-nurse sample, as hospital IDs of their own: two women and two men.
  nurses: [
    { username: "W101", name: "Sara Amin", gender: "Female" },
    { username: "W102", name: "Tariq Aziz", gender: "Male" },
    { username: "W103", name: "Lina Odeh", gender: "Female" },
    { username: "W104", name: "Karim Saad", gender: "Male" },
  ],
}

export const nameOf = (username: string) =>
  [people.cno, people.deputy, people.manager, ...people.nurses].find((person) => person.username === username)?.name ?? username

export type Journey = {
  /** "2026-12": the month the manager rosters; the month after, whose requests the nurses make. */
  rostered: string
  requested: string
  /** The last day of the window the journey opened for `requested`, open from the journey's day. */
  windowCloses: string
  /** The password each was given when made, and the one they chose at their first login. */
  given: Record<string, string>
  chosen: Record<string, string>
  /** The swap the journey's nurses made, which the manager approved: version 2. */
  swap: Swap
}

export const notebook = new Notebook<Journey>(on, "journey")

/** Signs in as one of the journey's people, the first time with the password they were given. */
export async function as(page: Page, username: string) {
  const book = notebook.read()
  const chosen = book.chosen?.[username]
  if (chosen) return signIn(page, username, chosen)
  const password = await firstSignIn(page, username, book.given[username])
  notebook.write({ chosen: { ...book.chosen, [username]: password } })
}

/** Someone else of the journey on a browser of their own, signed in with the password they chose. */
export async function alongside(browser: Browser, page: Page, username: string) {
  const theirs = await newBrowser(browser, page, on.baseURL)
  await as(theirs, username)
  return theirs
}

/** From the Rosters overview, as the CNO and the Deputy CNO reach a unit's month: its state's link in the unit's row. */
export async function openFromRosters(page: Page, state: string) {
  await go(page, "Rosters")
  const row = page.getByRole("row", { name: new RegExp(`^${UNIT}`) })
  await row.locator(`a[href*="month=${notebook.read().rostered}"]`).filter({ hasText: state }).click()
  await expect(page.getByRole("heading", { level: 1, name: `Roster of ${UNIT}` })).toBeVisible()
}

/** A solve with the real solver and worker: the wait is for its draft, up to the time limit and the queue's turn. */
export const SOLVED = { timeout: 90_000 }
