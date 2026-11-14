import { QueryClientProvider } from "@tanstack/react-query"
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router"
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"

import { createAppRouter, createQueryClient } from "./router"

/** The real routes, the real generated client and the real fetcher, with only `fetch` replaced. */
type Answer = { status?: number; body?: unknown }
type Handler = (request: { url: string; method: string; body: unknown }) => Answer

const ME = { username: "admin", full_name: "System Admin", role: "system_admin", must_change_password: false, has_nurse_profile: false }
const FORCED = { ...ME, must_change_password: true }
const NURSE = { ...ME, username: "h123", role: "staff_nurse", has_nurse_profile: true }
const SETTINGS = { time_limit_cap_seconds: 300, backup_time: "02:00", backup_retention_days: 30, close_time: "00:30" }
const HEALTH = {
  database: true,
  solver: { reachable: true, version: "0.1.0", workers: 8 },
  database_size_bytes: 5 * 1024 * 1024,
  disk: { total_bytes: 100 * 1024 ** 3, free_bytes: 60 * 1024 ** 3 },
  queue: { waiting: 2, running: 1, failed: 3, workers: 1 },
  push: { sent: 0, failed: 0, last_failure: null },
  last_backup: null,
  backups: [],
  backup_schedule: { time: "02:00", retention_days: 30 },
}

/** What the header's bell polls on every signed-in page. A test that needs other answers gives its own. */
const BELL = "/api/notifications/unread"
const NO_BELL = { [`GET ${BELL}`]: { body: { unread: 0 } } }
/** What the Notifications page's "On this device" reads, where a test has no other answer. */
const PUSH_KEY = "BAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8gISIjJCUmJygpKissLS4vMDEyMzQ1Njc4OTo7PD0-P0A"
const NO_PUSH = { "POST /api/notifications/push/state": { body: { public_key: PUSH_KEY, this_device: false, devices: 0 } } }
/** The addresses a page read, without the bell's polls, whose number depends on how long the test took. */
const pageReads = (calls: { url: string }[]) => calls.map((call) => call.url).filter((url) => url !== BELL)

function start(path: string, given: Record<string, Handler | Answer>) {
  const handlers: Record<string, Handler | Answer> = { ...NO_BELL, ...NO_PUSH, ...given }
  const calls: { url: string; method: string; body: unknown; headers: Headers }[] = []
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const method = init.method ?? "GET"
      const body = init.body instanceof FormData ? init.body : init.body ? JSON.parse(init.body as string) : undefined
      calls.push({ url, method, body, headers: new Headers(init.headers) })
      // An exact match first, then one that ignores the query string.
      const handler = handlers[`${method} ${url}`] ?? handlers[`${method} ${url.split("?")[0]}`]
      if (!handler) throw new Error(`no answer for ${method} ${url}`)
      const answer = typeof handler === "function" ? handler({ url, method, body }) : handler
      const status = answer.status ?? 200
      return new Response(status === 204 ? null : JSON.stringify(answer.body ?? {}), { status })
    }),
  )
  const queryClient = createQueryClient()
  queryClient.setDefaultOptions({ queries: { retry: false } })
  const router = createAppRouter(queryClient, createMemoryHistory({ initialEntries: [path] }))
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { router, calls, user: userEvent.setup() }
}

/** A signed-in user's session carries the hospital's time zone, as the API's does. */
const session = (user: unknown) => ({ "GET /api/auth/session": { body: { user: user && { time_zone: "Asia/Riyadh", ...(user as object) } } } })
/** A staff nurse's month with no approved roster, as their home and My roster first read it. */
const NO_ROSTER_YET = { month: "2026-12", first_month: "2026-11", unit: "ER", roster: null }
/** A nurse's own month with no approved roster yet. */
const NO_HOURS_YET = { month: "2026-11", first_month: "2026-11", unit: "ER", roster: null }
/** A nurse's requests for a month with no window. */
const NO_WINDOW = {
  month: "2026-11", window: null, request_cap: null, counted: 0, refusal: "No request window is open for November 2026.",
  shifts: [], first_day: null, last_day: null, requests: [],
}
/** A nurse's preferences for a month with no window. */
const NO_PREFERENCE_WINDOW = {
  month: "2026-11", window: null, preference_cap: null, request_cap: null, counted: 0, refusal: "No request window is open for November 2026.",
  shifts: [], first_day: null, last_day: null, preferences: [],
}
const NO_REQUESTS = { "GET /api/requests/mine": { body: NO_WINDOW }, "GET /api/preferences/mine": { body: NO_PREFERENCE_WINDOW } }
const nurseHome = (unitId = "u1", extra = {}) => ({
  ...session(NURSE),
  "GET /api/nurses/my-unit": { body: { id: unitId, name: "ER" } },
  "GET /api/rosters/mine": { body: NO_ROSTER_YET },
  ...extra,
})
const admin = (extra = {}) => ({
  ...session(ME),
  "GET /api/ops/health": { body: HEALTH },
  "GET /api/ops/technical-settings": { body: SETTINGS },
  ...extra,
})

/** Holds every request to one address open until the test answers it. Everything else goes on as `start` set it up. */
function hold(request: string) {
  const through = globalThis.fetch
  const waiting: ((answer: Answer) => void)[] = []
  vi.stubGlobal("fetch", vi.fn((url: string, init: RequestInit = {}) => {
    if (`${init.method ?? "GET"} ${url}` !== request) return through(url, init)
    return new Promise<Response>((resolve) => {
      waiting.push(({ status = 200, body = {} }) => resolve(new Response(JSON.stringify(body), { status })))
    })
  }))
  const answered = new Set<number>()
  return {
    answer: async (answer: Answer) => {
      await waitFor(() => expect(waiting.length).toBeGreaterThan(0))
      await act(async () => waiting.shift()?.(answer))
    },
    /** Until this many have been asked for, however they were answered. */
    waitFor: async (count: number) => waitFor(() => expect(waiting.length + answered.size).toBeGreaterThanOrEqual(count)),
    /** Answers the request asked for `index`th, from 0, in whatever order the test needs. Not to be mixed with `answer`. */
    answerAt: async (index: number, answer: Answer) => {
      await waitFor(() => expect(waiting.length).toBeGreaterThan(index))
      answered.add(index)
      await act(async () => waiting[index](answer))
    },
  }
}

const LEAVE_QUESTION = "Leave without the password?"

/** Chooses Leave and does something straight after, as soon as the address has changed and before anything has rendered
 *  since. Only microtasks pass, which `user` could not do, so this lands every time in the moment a person would rarely
 *  hit. The question's dialog is still up, so a link is found behind it, with `hidden`. */
async function leaveThen(router: ReturnType<typeof start>["router"], question: HTMLElement, next: () => void) {
  const from = router.state.location.pathname
  await act(async () => {
    fireEvent.click(within(question).getByRole("button", { name: "Leave" }))
    for (let turn = 0; turn < 100 && router.state.location.pathname === from; turn++) await Promise.resolve()
    expect(router.state.location.pathname).not.toBe(from)
    next()
  })
}
const follow = (name: string) => () => void fireEvent.click(screen.getByRole("link", { name, hidden: true }))
const onChangePassword = async (router: ReturnType<typeof start>["router"]) => {
  await screen.findByLabelText("Current password")
  expect(router.state.location.pathname).toBe("/change-password")
  expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument()
}

/** A nurse's skills as the API reads them, to a reader who reads their details: the CNO, the Deputy
 *  CNO and the nurse herself. A unit manager reads them with `details` empty (`asManagers`). */
const NO_DETAILS = { issuing_body: "", identifier: null, issue_date: null }
const BLS_HELD = { skill_id: "s1", name: "BLS", kind: "skill", expires: true, required_to_work: true, active: true, expiry: "2027-06-30", details: NO_DETAILS }
const ICU_HELD = { skill_id: "s2", name: "ICU", kind: "skill", expires: false, required_to_work: false, active: true, expiry: null, details: NO_DETAILS }
const SCFHS_HELD = { skill_id: "s4", name: "SCFHS", kind: "licence", expires: true, required_to_work: true, active: true, expiry: "2028-03-31",
  details: { issuing_body: "Saudi Commission for Health Specialties", identifier: "21-RN-0412", issue_date: "2026-04-01" } }
const BSN_HELD = { skill_id: "s5", name: "BSN", kind: "certificate", expires: false, required_to_work: false, active: true, expiry: null,
  details: { issuing_body: "King Saud University", identifier: null, issue_date: "2019-06-15" } }
const asManagers = <Row extends { details: unknown }>(rows: Row[]) => rows.map((row) => ({ ...row, details: null }))
/** The expiries list as the API reads it: oldest date first, then the skills nobody has recorded. */
const NO_EXPIRIES = { horizon_days: 90, count: 0, rows: [] }
const EXPIRY = { nurse_id: "n1", hospital_id: "h123", full_name: "Amal Saleh", unit: { id: "u1", name: "ER" }, skill: "BLS", kind: "skill", state: "expired", expiry: "2026-10-31" }
const EXPIRIES = { horizon_days: 90, count: 3, rows: [
  EXPIRY,
  { ...EXPIRY, nurse_id: "n2", hospital_id: "h124", full_name: "Badr Omar", unit: { id: "u2", name: "ICU" }, skill: "SCFHS", kind: "licence", state: "expiring", expiry: "2027-01-15" },
  { ...EXPIRY, nurse_id: "n3", hospital_id: "h125", full_name: "Dana Ali", state: "no_record", expiry: null },
] }

afterEach(() => {
  vi.unstubAllGlobals()
  document.cookie = "csrftoken=; expires=Thu, 01 Jan 1970 00:00:00 GMT"
})

describe("route guards", () => {
  it.each(["/", "/health", "/settings", "/users", "/audit-log", "/hospital-rules", "/shifts", "/leave-types", "/skills", "/holidays", "/units", "/unit-rules", "/rostering-attributes", "/nurses", "/expiries", "/roster", "/solve-history", "/leave", "/my-leave", "/my-month", "/rosters", "/unit-roster", "/my-roster", "/roster/print", "/my-roster/print", "/notifications", "/requests", "/swaps", "/unit-swaps", "/unit-covers", "/covers", "/change-password"])("%s sends a stranger to the login page", async (path) => {
    const { router } = start(path, session(null))

    expect(await screen.findByRole("button", { name: "Log in" })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe("/login")
  })

  it.each(["/", "/health", "/settings", "/users", "/audit-log", "/hospital-rules", "/shifts", "/leave-types", "/skills", "/holidays", "/units", "/unit-rules", "/rostering-attributes", "/nurses", "/expiries", "/roster", "/solve-history", "/leave", "/my-leave", "/my-month", "/rosters", "/unit-roster", "/my-roster", "/roster/print", "/my-roster/print", "/notifications", "/requests", "/swaps", "/unit-swaps", "/unit-covers", "/covers"])("%s sends a forced change to the password page", async (path) => {
    const { router } = start(path, session(FORCED))

    expect(await screen.findByText("Choose a new password")).toBeInTheDocument()
    expect(router.state.location.pathname).toBe("/change-password")
  })

  it("the System Admin's home is the health page", async () => {
    const { router } = start("/", admin())

    expect(await screen.findByRole("heading", { name: "Health" })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe("/health")
  })

  it("the login page sends someone already signed in home", async () => {
    const { router } = start("/login", admin())

    expect(await screen.findByRole("heading", { name: "Health" })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe("/health")
  })

  it.each(["/health", "/settings", "/users", "/audit-log", "/units", "/nurses", "/expiries", "/roster/print"])("%s is not for another role", async (path) => {
    const { router, calls } = start(path, nurseHome("u1"))

    expect(await screen.findByText("No roster yet for December 2026.")).toBeInTheDocument()
    expect(router.state.location.pathname).toBe("/")
    expect(screen.queryByRole("link", { name: "Technical settings" })).not.toBeInTheDocument()
    expect(pageReads(calls).sort()).toEqual(["/api/auth/session", "/api/nurses/my-unit", "/api/rosters/mine"])
  })

  it("says so when the application cannot be reached", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("network down") }))
    const queryClient = createQueryClient()
    queryClient.setDefaultOptions({ queries: { retry: false } })
    const router = createAppRouter(queryClient, createMemoryHistory({ initialEntries: ["/"] }))
    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(await screen.findByRole("alert")).toHaveTextContent("cannot be reached")
  })
})

describe("arriving on a page", () => {
  it("names the page in the title and moves focus to the new page", async () => {
    const { user } = start("/health", admin())
    await screen.findByText("Solver")
    expect(document.title).toBe("Health · Nurse roster")
    // Not on the first load: the keyboard starts from the top of the page.
    expect(document.activeElement).toBe(document.body)

    await user.click(within(screen.getByRole("navigation")).getByRole("link", { name: "Technical settings" }))

    await screen.findByLabelText("Backup time")
    await waitFor(() => expect(document.title).toBe("Technical settings · Nurse roster"))
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("main")))
  })

  it("the login page keeps focus on its first field and has a heading", async () => {
    start("/login", session(null))

    expect(await screen.findByLabelText("Username")).toHaveFocus()
    expect(screen.getByRole("heading", { level: 1, name: "Nurse roster" })).toBeInTheDocument()
    await waitFor(() => expect(document.title).toBe("Log in · Nurse roster"))
  })

  it("shows that it is loading while the session is read", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})))
    const queryClient = createQueryClient()
    const router = createAppRouter(queryClient, createMemoryHistory({ initialEntries: ["/"] }))
    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(await screen.findByText("Loading…")).toBeInTheDocument()
  })

  it("a page that does not exist offers the way home", async () => {
    start("/no-such-page", session(ME))

    expect(await screen.findByText("There is no such page.")).toBeInTheDocument()
    expect(screen.getByRole("link", { name: "Go to the home page" })).toHaveAttribute("href", "/")
  })
})

describe("login", () => {
  async function fill(user: ReturnType<typeof userEvent.setup>, username = "admin", password = "secret-password") {
    await user.type(await screen.findByLabelText("Username"), username)
    await user.type(screen.getByLabelText("Password"), password)
    await user.click(screen.getByRole("button", { name: "Log in" }))
  }

  it("posts the credentials with the CSRF token and lands on the home page", async () => {
    document.cookie = "csrftoken=the-token"
    const { user, calls, router } = start("/login", admin({
      ...session(null),
      "POST /api/auth/login": { body: ME },
    }))

    await fill(user)

    expect(await screen.findByRole("heading", { name: "Health" })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe("/health")
    const post = calls.find((call) => call.method === "POST")!
    expect(post.body).toEqual({ username: "admin", password: "secret-password" })
    expect(post.headers.get("X-CSRFToken")).toBe("the-token")
  })

  it("a forced change lands on the password page", async () => {
    const { user, router } = start("/login", { ...session(null), "POST /api/auth/login": { body: FORCED } })

    await fill(user)

    expect(await screen.findByText("Choose a new password")).toBeInTheDocument()
    expect(router.state.location.pathname).toBe("/change-password")
  })

  it("shows a wrong password and stays", async () => {
    const { user, router } = start("/login", {
      ...session(null),
      "POST /api/auth/login": { status: 401, body: { detail: "Wrong username or password." } },
    })

    await fill(user)

    expect(await screen.findByRole("alert")).toHaveTextContent("Wrong username or password")
    expect(router.state.location.pathname).toBe("/login")
  })

  it("shows the lockout and its time on the hospital's clock, whatever the browser's", async () => {
    // An offset no browser here is in, so the time shown can only be the one the server sent.
    const { user } = start("/login", {
      ...session(null),
      "POST /api/auth/login": {
        status: 423,
        body: { detail: "Too many failed logins.", locked_until: "2026-11-05T10:15:00.123+05:45" },
      },
    })

    await fill(user)

    const alert = await screen.findByRole("alert")
    expect(alert).toHaveTextContent(/^Too many failed logins\. Try again after 10:15\.$/)
    expect(alert).not.toHaveTextContent(/account/i)
  })

  it("shows the lockout's words without a time when a 423 carries none", async () => {
    const { user } = start("/login", { ...session(null), "POST /api/auth/login": { status: 423, body: {} } })

    await fill(user)

    expect(await screen.findByRole("alert")).toHaveTextContent(/^Too many failed logins\. Try again later\.$/)
  })

  it("shows the throttle and its time on the hospital's clock, whatever the browser's", async () => {
    // An offset no browser here is in, so the time shown can only be the one the server sent.
    const { user, router } = start("/login", {
      ...session(null),
      "POST /api/auth/login": {
        status: 429,
        body: { detail: "Too many failed logins from this network.", throttled_until: "2026-11-05T10:15:00.123+05:45" },
      },
    })

    await fill(user)

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Too many failed logins from this network. Try again at 10:15.",
    )
    expect(router.state.location.pathname).toBe("/login")
  })

  it("shows the throttle's words without a time when a 429 carries none", async () => {
    const { user } = start("/login", { ...session(null), "POST /api/auth/login": { status: 429, body: {} } })

    await fill(user)

    expect(await screen.findByRole("alert")).toHaveTextContent("Too many failed logins from this network. Try again later.")
  })

  it("asks for both fields before calling anything", async () => {
    const { user, calls } = start("/login", session(null))

    await user.click(await screen.findByRole("button", { name: "Log in" }))

    expect(await screen.findByText("Enter your username.")).toBeInTheDocument()
    expect(screen.getByText("Enter your password.")).toBeInTheDocument()
    expect(calls.filter((call) => call.method === "POST")).toEqual([])
  })
})

describe("logout", () => {
  it("ends the session and forgets what was on screen", async () => {
    const { user, router, calls } = start("/health", admin({ "POST /api/auth/logout": { status: 204 } }))

    await user.click(await screen.findByRole("button", { name: "Log out" }))

    expect(await screen.findByRole("button", { name: "Log in" })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe("/login")
    expect(calls.some((call) => call.method === "POST" && call.url === "/api/auth/logout")).toBe(true)
  })

  it("is available during a forced change", async () => {
    const { user } = start("/change-password", { ...session(FORCED), "POST /api/auth/logout": { status: 204 } })

    await user.click(await screen.findByRole("button", { name: "Log out" }))

    expect(await screen.findByRole("button", { name: "Log in" })).toBeInTheDocument()
  })
})

// Behind the user's back: the 12-hour idle limit, a deactivation or a password changed elsewhere. The browser cannot
// tell which, so there is one message.
const SIGNED_OUT = "You were signed out. Changes you had not saved are lost. Sign in to carry on."
const UNAUTHORIZED = { status: 401, body: { detail: "Unauthorized" } }
/** Refused until someone signs in again, as a page's reads are once the session has ended. */
const untilSignedIn = (answer: Answer) => {
  let signedIn = false
  return {
    read: () => (signedIn ? answer : UNAUTHORIZED),
    /** The login's answer: from then on, the reads are answered. */
    loginAs: (me: unknown) => () => { signedIn = true; return { body: { time_zone: "Asia/Riyadh", ...(me as object) } } },
  }
}
async function signInAgain(user: ReturnType<typeof userEvent.setup>) {
  await user.type(await screen.findByLabelText("Username"), "admin")
  await user.type(screen.getByLabelText("Password"), "secret-password")
  await user.click(screen.getByRole("button", { name: "Log in" }))
}

describe("a session that ends behind the user's back", () => {
  it("a 401 on a page goes to the login page, which says so, with focus on the username", async () => {
    const { router } = start("/health", { ...session(ME), "GET /api/ops/health": UNAUTHORIZED })

    expect(await screen.findByRole("alert")).toHaveTextContent(SIGNED_OUT)
    expect(router.state.location.pathname).toBe("/login")
    await waitFor(() => expect(screen.getByLabelText("Username")).toHaveFocus())
    expect(screen.getByLabelText("Username")).toHaveAccessibleDescription(SIGNED_OUT)
  })

  it("signing in goes back to the page and its address, and the message is gone", async () => {
    const audit = untilSignedIn({ body: { entries: [], older_than: null } })
    const { user, router } = start("/audit-log?action=auth.login", admin({
      "GET /api/audit/entries": audit.read,
      "GET /api/audit/filter-options": { body: { actions: ["auth.login"], users: ["admin"] } },
      "POST /api/auth/login": audit.loginAs(ME),
    }))
    await screen.findByText(SIGNED_OUT)

    await signInAgain(user)

    expect(await screen.findByRole("heading", { level: 1, name: "Audit log" })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe("/audit-log")
    expect(router.state.location.search).toEqual({ action: "auth.login" })
    expect(screen.queryByText(SIGNED_OUT)).not.toBeInTheDocument()
  })

  it("signing in as a role that may not open the page goes home", async () => {
    const audit = untilSignedIn({ body: { entries: [], older_than: null } })
    const { user, router } = start("/audit-log", admin({
      ...nurseHome(),
      ...session(ME),
      "GET /api/audit/entries": audit.read,
      "GET /api/audit/filter-options": { body: { actions: ["auth.login"], users: ["admin"] } },
      "POST /api/auth/login": audit.loginAs(NURSE),
    }))
    await screen.findByText(SIGNED_OUT)

    await signInAgain(user)

    expect(await screen.findByText("Your unit is ER.")).toBeInTheDocument()
    expect(router.state.location.pathname).toBe("/")
  })

  it("the forced password change still comes first", async () => {
    const health = untilSignedIn({ body: HEALTH })
    const { user, router } = start("/health", admin({ "GET /api/ops/health": health.read, "POST /api/auth/login": health.loginAs(FORCED) }))
    await screen.findByText(SIGNED_OUT)

    await signInAgain(user)

    await onChangePassword(router)
  })

  it("a wrong password then says only that", async () => {
    const { user } = start("/health", {
      ...session(ME), "GET /api/ops/health": UNAUTHORIZED,
      "POST /api/auth/login": { status: 401, body: { detail: "Wrong username or password." } },
    })
    await screen.findByText(SIGNED_OUT)

    await signInAgain(user)

    expect(await screen.findByText("Wrong username or password.")).toBeInTheDocument()
    expect(screen.getAllByRole("alert").map((alert) => alert.textContent)).toEqual(["Wrong username or password."])
  })

  it("keeps nothing in the browser's storage", async () => {
    start("/health", { ...session(ME), "GET /api/ops/health": UNAUTHORIZED })

    await screen.findByText(SIGNED_OUT)
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
  })

  it.each([
    ["a full address", "https://elsewhere.example/health"],
    ["an address with no scheme", "//elsewhere.example/health"],
    ["a path that is not absolute", "health"],
  ])("ignores %s to go back to, and goes home", async (_, address) => {
    const { user, router } = start(`/login?ended=true&back=${encodeURIComponent(address)}`, admin({ ...session(null), "POST /api/auth/login": { body: ME } }))
    await screen.findByText(SIGNED_OUT)

    await signInAgain(user)

    expect(await screen.findByRole("heading", { name: "Health" })).toBeInTheDocument()
    expect(router.state.location.href).toBe("/health")
  })
})

describe("the login page says nothing of a session ending", () => {
  it("on a first visit", async () => {
    start("/login", session(null))

    expect(await screen.findByRole("button", { name: "Log in" })).toBeInTheDocument()
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
    expect(screen.getByLabelText("Username")).not.toHaveAccessibleDescription()
  })

  it("after Log out, nor after a plain login that then logs out", async () => {
    const { user, router } = start("/login", admin({ ...session(null), "POST /api/auth/login": { body: ME }, "POST /api/auth/logout": { status: 204 } }))
    await signInAgain(user)
    expect(await screen.findByRole("heading", { name: "Health" })).toBeInTheDocument()

    await user.click(screen.getByRole("button", { name: "Log out" }))

    expect(await screen.findByRole("button", { name: "Log in" })).toBeInTheDocument()
    expect(router.state.location.href).toBe("/login")
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
  })
})

describe("changing the password", () => {
  async function fill(user: ReturnType<typeof userEvent.setup>, current: string, next: string, repeat = next) {
    await user.type(await screen.findByLabelText("Current password"), current)
    await user.type(screen.getByLabelText("New password"), next)
    await user.type(screen.getByLabelText("Repeat the new password"), repeat)
    await user.click(screen.getByRole("button", { name: "Change password" }))
  }

  it("clears the force and goes home", async () => {
    const { user, calls, router } = start("/change-password", admin({
      ...session(FORCED),
      "POST /api/auth/change-password": { body: ME },
    }))

    await fill(user, "one-time-password", "a-brand-new-passphrase")

    expect(await screen.findByRole("heading", { name: "Health" })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe("/health")
    expect(calls.find((call) => call.method === "POST")!.body).toEqual({
      current_password: "one-time-password",
      new_password: "a-brand-new-passphrase",
    })
  })

  it("shows the server's policy refusal on the field", async () => {
    const { user, router } = start("/change-password", {
      ...session(FORCED),
      "POST /api/auth/change-password": {
        status: 400,
        body: { current_password: [], new_password: ["This password is too common."] },
      },
    })

    await fill(user, "one-time-password", "password1234")

    const refusal = await screen.findByText("This password is too common.")
    expect(router.state.location.pathname).toBe("/change-password")
    const field = screen.getByLabelText("New password")
    await waitFor(() => expect(field).toHaveFocus())
    expect(field).toHaveAttribute("aria-invalid", "true")
    expect(field.getAttribute("aria-describedby")?.split(" ")).toContain(refusal.id)
  })

  it("after a refusal the user can try again with a better password", async () => {
    let attempts = 0
    const { user, router } = start("/change-password", admin({
      ...session(FORCED),
      "POST /api/auth/change-password": () =>
        attempts++ === 0
          ? { status: 400, body: { current_password: [], new_password: ["This password is too common."] } }
          : { body: ME },
    }))

    await fill(user, "one-time-password", "password1234")
    await screen.findByText("This password is too common.")
    await user.clear(screen.getByLabelText("New password"))
    await user.type(screen.getByLabelText("New password"), "a-brand-new-passphrase")
    await user.clear(screen.getByLabelText("Repeat the new password"))
    await user.type(screen.getByLabelText("Repeat the new password"), "a-brand-new-passphrase{Enter}")

    expect(await screen.findByRole("heading", { name: "Health" })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe("/health")
    expect(attempts).toBe(2)
  })

  it("shows a wrong current password on its field", async () => {
    const { user } = start("/change-password", {
      ...session(FORCED),
      "POST /api/auth/change-password": {
        status: 400,
        body: { current_password: ["This is not your current password."], new_password: [] },
      },
    })

    await fill(user, "nope", "a-brand-new-passphrase")

    expect(await screen.findByText("This is not your current password.")).toBeInTheDocument()
  })

  it("shows the throttle and its time on the hospital's clock", async () => {
    const { user, router } = start("/change-password", {
      ...session(FORCED),
      "POST /api/auth/change-password": {
        status: 429,
        body: { detail: "Too many failed logins from this network.", throttled_until: "2026-11-05T10:15:00+05:45" },
      },
    })

    await fill(user, "one-time-password", "a-brand-new-passphrase")

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Too many failed logins from this network. Try again at 10:15.",
    )
    expect(router.state.location.pathname).toBe("/change-password")
  })

  it("refuses a short password and a mismatch without calling anything", async () => {
    const { user, calls } = start("/change-password", session(FORCED))

    await fill(user, "one-time-password", "short", "different")

    expect(await screen.findByText("Use at least 10 characters.")).toBeInTheDocument()
    expect(screen.getByText("The two passwords differ.")).toBeInTheDocument()
    expect(calls.filter((call) => call.method === "POST")).toEqual([])
  })

  it("offers a way back when it is not forced", async () => {
    start("/change-password", session(ME))

    expect(await screen.findByText("Change your password")).toBeInTheDocument()
    expect(screen.getByRole("link", { name: "Back to home" })).toBeInTheDocument()
  })
})

describe("the health page", () => {
  it("shows the solver, the queue, the disk and the database size", async () => {
    start("/health", admin())

    expect(await screen.findByText("Solver")).toBeInTheDocument()
    expect(screen.getByRole("heading", { level: 2, name: "Services" })).toBeInTheDocument()
    expect(screen.getAllByText("Reachable")).toHaveLength(2)
    expect(screen.getByText("0.1.0")).toBeInTheDocument()
    expect(screen.getByText("Jobs waiting").nextSibling).toHaveTextContent("2")
    expect(screen.getByText("Jobs running").nextSibling).toHaveTextContent("1")
    expect(screen.getByText("Jobs failed, last 30 days").nextSibling).toHaveTextContent("3")
    expect(screen.getByText("Workers alive").nextSibling).toHaveTextContent("1")
    expect(screen.getByText("Database size").nextSibling).toHaveTextContent("5.0 MB")
    expect(screen.getByText("Disk free").nextSibling).toHaveTextContent("60.0 GB")
    expect(screen.getByText("Disk size").nextSibling).toHaveTextContent("100 GB")
    expect(screen.getByText("Last backup").nextSibling).toHaveTextContent("None yet")
  })

  it("shows a solver that is down and a disk it could not read", async () => {
    start("/health", admin({
      "GET /api/ops/health": {
        body: { ...HEALTH, solver: { reachable: false, version: null, workers: null }, disk: null },
      },
    }))

    expect(await screen.findByText("Unreachable")).toBeInTheDocument()
    expect(screen.getByText("Disk").nextSibling).toHaveTextContent("Unknown")
  })

  it("says no push was sent or failed in the last 24 hours when none was", async () => {
    start("/health", admin())

    expect(await screen.findByRole("heading", { level: 2, name: "Pushes" })).toBeInTheDocument()
    expect(screen.getByText("Sent, last 24 hours").nextSibling).toHaveTextContent("0")
    expect(screen.getByText("Failed, last 24 hours").nextSibling).toHaveTextContent("0")
    expect(screen.getByText("Last failure").nextSibling).toHaveTextContent("None")
    expect(screen.getByText(/Every send to one device is counted/)).toBeInTheDocument()
  })

  it.each([
    ["unreachable", "Unreachable", /could not be reached, or answered that it was overloaded.*firewall.*installation instructions/],
    ["refused", "Refused", /refused the push/],
    ["gone", "Gone", /no longer exists.*turned off/],
  ])("counts the pushes sent and failed and says why the last one failed: %s", async (last_failure, word, why) => {
    start("/health", admin({ "GET /api/ops/health": { body: { ...HEALTH, push: { sent: 41, failed: 7, last_failure } } } }))

    expect(await screen.findByText("Sent, last 24 hours")).toBeInTheDocument()
    expect(screen.getByText("Sent, last 24 hours").nextSibling).toHaveTextContent("41")
    expect(screen.getByText("Failed, last 24 hours").nextSibling).toHaveTextContent("7")
    expect(screen.getByText("Last failure").nextSibling).toHaveTextContent(word)
    expect(screen.getByText(why)).toBeInTheDocument()
  })

  it("does not poll; Refresh reads it again", async () => {
    const { user, calls } = start("/health", admin())
    await screen.findByText("Solver")
    const reads = () => calls.filter((call) => call.url === "/api/ops/health").length
    expect(reads()).toBe(1)

    await user.click(screen.getByRole("button", { name: "Refresh" }))

    await waitFor(() => expect(reads()).toBe(2))
    expect(screen.getByRole("button", { name: "Refresh" })).toHaveFocus()
    expect(await screen.findByText("Checked.")).toHaveAttribute("role", "status")
  })

  it("lists the backups newest first, each with its size and a download", async () => {
    const backups = [
      { name: "nurse-roster-20261105T230000Z.dump", size_bytes: 3 * 1024 * 1024, taken_at: "2026-11-06T02:00:00+03:00" },
      { name: "nurse-roster-20261104T230000Z.dump", size_bytes: 2900 * 1024, taken_at: "2026-11-05T02:00:00+03:00" },
    ]
    start("/health", admin({ "GET /api/ops/health": { body: { ...HEALTH, backups, last_backup: backups[0].taken_at } } }))

    expect(await screen.findByText("Last backup")).toBeInTheDocument()
    expect(screen.getByText("Last backup").nextSibling).toHaveTextContent("2026-11-06 02:00:00")
    expect(screen.getByText(/Taken every day at 02:00 hospital time and kept 30 days/)).toBeInTheDocument()
    const table = screen.getByRole("region", { name: "Backups kept" })
    const rows = within(table).getAllByRole("row").slice(1)
    expect(rows.map((row) => within(row).getAllByRole("cell").map((cell) => cell.textContent))).toEqual([
      ["3.0 MB", "Download"], ["2.8 MB", "Download"]])
    expect(rows.map((row) => within(row).getByRole("rowheader").textContent)).toEqual(["2026-11-06 02:00:00", "2026-11-05 02:00:00"])
    const link = within(rows[0]).getByRole("link", { name: "Download the backup of 2026-11-06 02:00:00" })
    expect(link).toHaveAttribute("href", "/api/ops/backups/nurse-roster-20261105T230000Z.dump")
    expect(link).toHaveAttribute("download")
    expect(screen.getByText(/holds every account and every nurse's leave/)).toBeInTheDocument()
  })

  it("says when there is no backup yet", async () => {
    start("/health", admin())

    expect(await screen.findByText("Last backup")).toBeInTheDocument()
    expect(screen.getByText("Last backup").nextSibling).toHaveTextContent("None yet")
    expect(screen.queryByRole("region", { name: "Backups kept" })).not.toBeInTheDocument()
    expect(within(screen.getByText(/Taken every day/)).getByRole("link", { name: "Technical settings" })).toHaveAttribute("href", "/settings")
  })

  it("says so when the health cannot be read", async () => {
    start("/health", admin({ "GET /api/ops/health": { status: 500, body: {} } }))

    expect(await screen.findByRole("alert")).toHaveTextContent("Refresh to try again")
  })
})

describe("the technical settings page", () => {
  it("shows the current values and saves a change", async () => {
    const saved = { ...SETTINGS, time_limit_cap_seconds: 600, close_time: "01:00" }
    const { user, calls } = start("/settings", admin({ "PUT /api/ops/technical-settings": { body: saved } }))

    const cap = await screen.findByLabelText("Time limit cap (seconds)")
    expect(cap).toHaveValue(300)
    expect(screen.getByLabelText("Backup time")).toHaveValue("02:00")
    expect(screen.getByLabelText("Backup retention (days)")).toHaveValue(30)
    expect(screen.getByLabelText("Month close time")).toHaveValue("00:30")
    expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute("aria-disabled", "true")

    await user.clear(cap)
    await user.type(cap, "600")
    await user.clear(screen.getByLabelText("Month close time"))
    await user.type(screen.getByLabelText("Month close time"), "01:00")
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByText("Saved.")).toHaveAttribute("role", "status")
    // The button turned unavailable under the pointer or keyboard without dropping focus.
    expect(screen.getByRole("button", { name: "Save" })).toHaveFocus()
    expect(calls.find((call) => call.method === "PUT")!.body).toEqual(saved)
    expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute("aria-disabled", "true")
  })

  it.each([["0", "At least 1 second."], ["86401", "At most 86400 seconds."]])("refuses a cap of %s", async (value, says) => {
    const { user, calls } = start("/settings", admin())

    const cap = await screen.findByLabelText("Time limit cap (seconds)")
    await user.clear(cap)
    await user.type(cap, value)
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByText(says)).toBeInTheDocument()
    expect(calls.filter((call) => call.method === "PUT")).toEqual([])
  })

  it("an empty number is an error, not a NaN sent to the server", async () => {
    const { user, calls } = start("/settings", admin())

    const days = await screen.findByLabelText("Backup retention (days)")
    await user.clear(days)
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByText("Enter a whole number of days.")).toBeInTheDocument()
    expect(calls.filter((call) => call.method === "PUT")).toEqual([])
  })

  it("says so when the save fails", async () => {
    const { user } = start("/settings", admin({ "PUT /api/ops/technical-settings": { status: 500, body: {} } }))

    const cap = await screen.findByLabelText("Time limit cap (seconds)")
    await user.clear(cap)
    await user.type(cap, "600")
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("save again")
  })
})

const RULES = {
  hours_per_day: 8,
  ramadan_hours_per_day: 6,
  breastfeeding_reduction: 1,
  weekend_weekdays: [4],
  forbidden_successions: [["N", "D"], ["D", "C"]],
  forbidden_successions_enabled: true,
  hours_rule_enabled: true,
  overtime_caps_enabled: true,
  overtime_month_cap: 40,
  overtime_year_cap: 720,
  dept_overtime_cap: 400,
  weights: {
    night_rest: 5, dept_overtime: 1, nurse_overtime: 1, nurse_shortfall: 1, avoidable_shortfall: 1000, fair_day: 1,
    fair_night: 1, fair_weekend: 1, overtime_above_share: 1, score_deficit: 2, score_spread: 1, excess_weekend: 6,
    excess_full_run: 4, shift_switch: 8, undesired_day: 2, desired_day: 1, change: 3, skill_shortfall: 0,
  },
}
const WEIGHT_COUNT = Object.keys(RULES.weights).length
const RULES_STATE = { rules: RULES, needs_review: true, shift_codes: ["D", "N", "A", "B", "C"] }
const CNO_ME = { ...ME, username: "cno", role: "cno" }
const DEPUTY = { ...ME, username: "deputy", role: "deputy_cno" }
const FLAG = "Defaults, review before the first solve"

describe("the hospital rules page", () => {
  /** Answers a save with what was sent, as the API does. */
  const saves = { "PUT /api/hospital/rules": ({ body }: { body: unknown }) => ({ body: { ...RULES_STATE, rules: body, needs_review: false } }) }
  const asCno = (state: unknown = RULES_STATE, extra = {}) => ({
    ...session(CNO_ME),
    "GET /api/hospital/rules": { body: state },
    ...extra,
  })
  const put = (calls: { method: string; body: unknown }[]) => calls.find((call) => call.method === "PUT")?.body as typeof RULES

  it("shows every rule and every weight with its explanation, and flags the defaults", async () => {
    start("/hospital-rules", asCno())

    expect(await screen.findByText(FLAG)).toBeInTheDocument()
    expect(document.title).toBe("Hospital rules · Nurse roster")
    expect(screen.getByLabelText("Hours per day")).toHaveValue(8)
    expect(screen.getByLabelText("Hours per day")).toHaveAccessibleDescription(/owes for each working day/)
    expect(screen.getByLabelText("Ramadan hours per day")).toHaveValue(6)
    expect(screen.getByLabelText("Breastfeeding reduction (hours)")).toHaveValue(1)
    expect(screen.getByRole("checkbox", { name: "Friday" })).toBeChecked()
    expect(screen.getByRole("checkbox", { name: "Saturday" })).not.toBeChecked()
    expect(screen.getByRole("checkbox", { name: "Forbid these successions" })).toBeChecked()
    expect(screen.getByRole("checkbox", { name: "Count hours" })).toHaveAccessibleDescription(/any number of shifts/)
    expect(screen.getByRole("checkbox", { name: "Cap each nurse's overtime" })).toBeChecked()
    expect(screen.getByLabelText("Monthly overtime cap (hours)")).toHaveValue(40)
    expect(screen.getByLabelText("Yearly overtime cap (hours)")).toHaveValue(720)
    expect(screen.getByLabelText("Unit overtime cap (hours)")).toHaveValue(400)
    expect(within(screen.getByRole("group", { name: "Pair 1" })).getByLabelText("After")).toHaveValue("N")
    expect(within(screen.getByRole("group", { name: "Pair 1" })).getByLabelText("the next day cannot be")).toHaveValue("D")

    const weights = within(screen.getByRole("region", { name: "Weights" }))
    expect(weights.getByText(/0 switches the rule off/)).toBeInTheDocument()
    const inputs = weights.getAllByRole("spinbutton")
    expect(inputs).toHaveLength(WEIGHT_COUNT)
    for (const input of inputs) expect(input).toHaveAccessibleDescription(/\S/)
    expect(weights.getByLabelText("Night rest")).toHaveValue(5)
    expect(weights.getByLabelText("Changes in a re-plan")).toHaveAccessibleDescription(/Each cell that differs/)
    expect(weights.getByLabelText("Working a day asked off")).toHaveAccessibleDescription(
      "Each day a nurse preferred to have off but works; twice that for a nurse whose preference last month was not kept.")
    expect(weights.getByLabelText("Off on a day asked to work")).toHaveAccessibleDescription(
      "Each day a nurse preferred to work but is off; twice that for a nurse whose preference last month was not kept.")
    expect(weights.queryByText(/Nothing reaches this/)).not.toBeInTheDocument()
  })

  it("the price of a full run above the most is a weight like the others, and 0 switches it off", async () => {
    const { user, calls } = start("/hospital-rules", asCno(RULES_STATE, saves))

    const fullRun = within(await screen.findByRole("region", { name: "Weights" })).getByLabelText("Full run above the most")
    expect(fullRun).toHaveValue(4)
    expect(fullRun).toHaveAccessibleDescription(
      "Each full run a nurse works in a month above the unit's most. A full run is as many shifts in a row as the unit's longest run allows.")
    await user.clear(fullRun)
    await user.type(fullRun, "0")
    await user.click(screen.getByRole("button", { name: "Save" }))

    await screen.findByText("Saved.")
    expect(put(calls).weights).toEqual({ ...RULES.weights, excess_full_run: 0 })
  })

  it("the price of an hour of overtime above a fair share is a weight like the others, and 0 switches it off", async () => {
    const { user, calls } = start("/hospital-rules", asCno(RULES_STATE, saves))

    const share = within(await screen.findByRole("region", { name: "Weights" })).getByLabelText("Overtime away from the fair share")
    expect(share).toHaveValue(1)
    expect(share).toHaveAccessibleDescription(
      "Each hour of overtime a nurse works above their overtime share, beyond the unit's overtime tolerance. At most the weight of Nurse overtime, so that adding overtime never lowers this cost.")
    await user.clear(share)
    await user.type(share, "0")
    await user.click(screen.getByRole("button", { name: "Save" }))

    await screen.findByText("Saved.")
    expect(put(calls).weights).toEqual({ ...RULES.weights, overtime_above_share: 0 })
  })

  it("refuses an overtime share weight above Nurse overtime beside it, saying why, and sends nothing", async () => {
    const { user, calls } = start("/hospital-rules", asCno(RULES_STATE, saves))

    const weights = within(await screen.findByRole("region", { name: "Weights" }))
    const share = weights.getByLabelText("Overtime away from the fair share")
    await user.clear(share)
    await user.type(share, "2")
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByText("Cannot be above Nurse overtime, or the solver could lower this cost by adding overtime.")).toBeInTheDocument()
    expect(share).toHaveAttribute("aria-invalid", "true")
    expect(share).toHaveFocus()
    expect(calls.some((call) => call.method === "PUT")).toBe(false)

    const overtime = weights.getByLabelText("Nurse overtime")
    await user.clear(overtime)
    await user.type(overtime, "2")
    await user.click(screen.getByRole("button", { name: "Save" }))

    await screen.findByText("Saved.")
    expect(put(calls).weights).toEqual({ ...RULES.weights, nurse_overtime: 2, overtime_above_share: 2 })
  })

  it("puts the API's refusal of an overtime share weight beside it", async () => {
    const { user } = start("/hospital-rules", asCno(RULES_STATE, {
      "PUT /api/hospital/rules": {
        status: 422,
        body: { detail: [{ loc: ["body", "body", "weights", "overtime_above_share"],
          msg: "Value error, Cannot be above Nurse overtime, or the solver could lower this cost by adding overtime.", type: "value_error" }] },
      },
    }))

    await screen.findByText(FLAG)
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByText("Cannot be above Nurse overtime, or the solver could lower this cost by adding overtime.")).toBeInTheDocument()
    expect(screen.getByLabelText("Overtime away from the fair share")).toHaveAttribute("aria-invalid", "true")
  })

  it("the price of a nurse short of a skill need is a weight like the others", async () => {
    const { user, calls } = start("/hospital-rules", asCno(RULES_STATE, saves))

    const shortfall = within(await screen.findByRole("region", { name: "Weights" })).getByLabelText("Short of a skill need")
    expect(shortfall).toHaveValue(0)
    expect(shortfall).toHaveAccessibleDescription(
      "Each nurse short of a unit's skill need on a shift and day. Applies only in a unit that lets its skill needs be broken.")
    await user.clear(shortfall)
    await user.type(shortfall, "50")
    await user.click(screen.getByRole("button", { name: "Save" }))

    await screen.findByText("Saved.")
    expect(put(calls).weights).toEqual({ ...RULES.weights, skill_shortfall: 50 })
  })

  it("saving the defaults untouched is the review: the button is open and the flag goes", async () => {
    const { user, calls } = start("/hospital-rules", asCno(RULES_STATE, saves))

    await screen.findByText(FLAG)
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByText("Saved.")).toHaveAttribute("role", "status")
    expect(put(calls)).toEqual(RULES)
    expect(screen.queryByText(FLAG)).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute("aria-disabled", "true")
  })

  it("reviewed rules show no flag and need a change to save", async () => {
    start("/hospital-rules", asCno({ ...RULES_STATE, needs_review: false }))

    await screen.findByLabelText("Hours per day")
    expect(screen.queryByText(FLAG)).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute("aria-disabled", "true")
  })

  it("saves every kind of change", async () => {
    const { user, calls } = start("/hospital-rules", asCno(RULES_STATE, saves))

    const hours = await screen.findByLabelText("Hours per day")
    await user.clear(hours)
    await user.type(hours, "7")
    await user.click(screen.getByRole("checkbox", { name: "Saturday" }))
    await user.click(screen.getByRole("checkbox", { name: "Count hours" }))
    const nightRest = screen.getByLabelText("Night rest")
    await user.clear(nightRest)
    await user.type(nightRest, "0")
    await user.selectOptions(within(screen.getByRole("group", { name: "Pair 1" })).getByLabelText("the next day cannot be"), "A")
    await user.click(within(screen.getByRole("group", { name: "Pair 2" })).getByRole("button", { name: "Remove pair 2" }))
    await user.click(screen.getByRole("button", { name: "Save" }))

    await screen.findByText("Saved.")
    expect(put(calls)).toEqual({
      ...RULES,
      hours_per_day: 7,
      weekend_weekdays: [4, 5],
      hours_rule_enabled: false,
      forbidden_successions: [["N", "A"]],
      weights: { ...RULES.weights, night_rest: 0 },
    })
  })

  it("adds a pair not yet listed, and moves focus to it", async () => {
    const { user, calls } = start("/hospital-rules", asCno(RULES_STATE, saves))

    await user.click(await screen.findByRole("button", { name: "Add a pair" }))

    const added = within(screen.getByRole("group", { name: "Pair 3" }))
    expect(added.getByLabelText("After")).toHaveFocus()
    await user.click(screen.getByRole("button", { name: "Save" }))
    await screen.findByText("Saved.")
    expect(put(calls).forbidden_successions).toEqual([["N", "D"], ["D", "C"], ["D", "N"]])
  })

  it("removing the last pair leaves focus on the way to add one", async () => {
    const one = { ...RULES_STATE, rules: { ...RULES, forbidden_successions: [["N", "D"]] } }
    const { user } = start("/hospital-rules", asCno(one))

    await user.click(await screen.findByRole("button", { name: "Remove pair 1" }))

    expect(screen.getByRole("button", { name: "Add a pair" })).toHaveFocus()
    expect(screen.getByText("No successions are forbidden.")).toBeInTheDocument()
  })

  it.each([
    ["Hours per day", "25", "Between 0 and 24."],
    ["Hours per day", "", "Enter a whole number."],
    ["Monthly overtime cap (hours)", "-1", "Between 0 and 1000000."],
    ["Night rest", "1000001", "Between 0 and 1000000."],
    ["Night rest", "1.5", "Enter a whole number."],
  ])("refuses %s of '%s' beside the field, unsent", async (label, value, says) => {
    const { user, calls } = start("/hospital-rules", asCno())

    const input = await screen.findByLabelText(label)
    await user.clear(input)
    if (value) await user.type(input, value)
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByText(says)).toBeInTheDocument()
    expect(input).toHaveAttribute("aria-invalid", "true")
    expect(input).toHaveFocus()
    expect(calls.some((call) => call.method === "PUT")).toBe(false)
  })

  it("refuses Ramadan hours above a normal day, and a reduction above Ramadan hours", async () => {
    const { user, calls } = start("/hospital-rules", asCno())

    const ramadan = await screen.findByLabelText("Ramadan hours per day")
    await user.clear(ramadan)
    await user.type(ramadan, "9")
    await user.click(screen.getByRole("button", { name: "Save" }))
    expect(await screen.findByText("Cannot be more than the hours per day.")).toBeInTheDocument()
    expect(ramadan).toHaveAttribute("aria-invalid", "true")

    await user.clear(ramadan)
    await user.type(ramadan, "1")
    const reduction = screen.getByLabelText("Breastfeeding reduction (hours)")
    await user.clear(reduction)
    await user.type(reduction, "2")
    await user.click(screen.getByRole("button", { name: "Save" }))
    expect(await screen.findByText("Cannot be more than the Ramadan hours per day.")).toBeInTheDocument()
    expect(calls.some((call) => call.method === "PUT")).toBe(false)
  })

  it("refuses the same pair twice and names it", async () => {
    const { user, calls } = start("/hospital-rules", asCno())

    const second = within(await screen.findByRole("group", { name: "Pair 2" }))
    await user.selectOptions(second.getByLabelText("After"), "N")
    await user.selectOptions(second.getByLabelText("the next day cannot be"), "D")
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByText("N then D is listed twice. Remove one.")).toBeInTheDocument()
    expect(calls.some((call) => call.method === "PUT")).toBe(false)
  })

  it("puts a refusal from the API beside the field it names", async () => {
    const { user } = start("/hospital-rules", asCno(RULES_STATE, {
      "PUT /api/hospital/rules": {
        status: 422,
        body: { detail: [{ loc: ["body", "body", "weights", "change"], msg: "Input should be less than 5", type: "x" }] },
      },
    }))

    await screen.findByText(FLAG)
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByText("Input should be less than 5")).toBeInTheDocument()
    expect(screen.getByLabelText("Changes in a re-plan")).toHaveAttribute("aria-invalid", "true")
    expect(screen.getByText(FLAG)).toBeInTheDocument()
  })

  it("shows a validator's refusal on a field the form cannot get wrong, in its own words", async () => {
    const { user } = start("/hospital-rules", asCno(RULES_STATE, {
      "PUT /api/hospital/rules": {
        status: 422,
        body: { detail: [{ loc: ["body", "body", "weekend_weekdays"], msg: "Value error, lists a day twice", type: "x" }] },
      },
    }))

    await screen.findByText(FLAG)
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByText("lists a day twice")).toBeInTheDocument()
    expect(screen.getByRole("group", { name: "Weekend days" })).toHaveFocus()
  })

  it("says so when a save fails", async () => {
    const { user } = start("/hospital-rules", asCno(RULES_STATE, { "PUT /api/hospital/rules": { status: 500 } }))

    await screen.findByText(FLAG)
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("save again")
    expect(screen.getByText(FLAG)).toBeInTheDocument()
  })

  it("says so when the rules cannot be read", async () => {
    start("/hospital-rules", asCno(RULES_STATE, { "GET /api/hospital/rules": { status: 500 } }))

    expect(await screen.findByRole("alert")).toHaveTextContent("could not be read")
  })

  it("the Deputy CNO reads every value and explanation, and can change nothing", async () => {
    start("/hospital-rules", { ...session(DEPUTY), "GET /api/hospital/rules": { body: RULES_STATE } })

    expect(await screen.findByText(FLAG)).toBeInTheDocument()
    expect(screen.getByText(/Only the CNO can change these/)).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Add a pair" })).not.toBeInTheDocument()
    expect(screen.queryAllByRole("spinbutton")).toEqual([])
    expect(screen.queryAllByRole("checkbox")).toEqual([])
    expect(screen.queryAllByRole("combobox")).toEqual([])

    const hours = screen.getByText("Hours per day").closest("div")!
    expect(hours).toHaveTextContent("8")
    expect(hours).toHaveTextContent(/owes for each working day/)
    expect(screen.getByText("Friday")).toBeInTheDocument()
    expect(screen.getByText("After N, not D")).toBeInTheDocument()
    expect(screen.getByText("Avoidable hours short").closest("div")).toHaveTextContent("1000")
    const weights = within(screen.getByRole("region", { name: "Weights" }))
    expect(weights.getAllByRole("definition").length).toBeGreaterThanOrEqual(WEIGHT_COUNT)
  })

  it.each(["system_admin", "unit_manager", "staff_nurse"])("is not for the %s", async (role) => {
    const { router } = start("/hospital-rules", {
      ...session({ ...ME, role }),
      "GET /api/ops/health": { body: HEALTH },
    })

    await waitFor(() => expect(router.state.location.pathname).not.toBe("/hospital-rules"))
  })

})

const CATALOGUE_LINKS = ["Shifts", "Leave types", "Skills", "Holidays and Ramadan"]

describe("each role's navigation and home", () => {
  const navLinks = () => within(screen.getByRole("navigation", { name: "Main" })).getAllByRole("link").map((link) => link.textContent)

  it("the System Admin reaches their pages and nothing of the roster", async () => {
    start("/", admin())

    await screen.findByRole("heading", { name: "Health" })
    expect(navLinks()).toEqual(["Health", "Technical settings", "Users", "Audit log", "Solve history", "Change password"])
    expect(screen.getByText("admin · System Admin")).toBeInTheDocument()
  })

  it.each([
    ["cno", "CNO", /under Hospital rules/, ["Home", "Rosters", "Day view", "Units", "Unit rules", "Rostering attributes", "Nurses", "Expiries", "Leave", "Hospital rules", ...CATALOGUE_LINKS, "Change password"], ["/api/rosters/submitted", "/api/units", "/api/nurses/expiries"]],
    ["deputy_cno", "Deputy CNO", "No roster is waiting for a decision.", ["Home", "Rosters", "Day view", "Unit rules", "Rostering attributes", "Import skills", "Expiries", "Leave", "Hospital rules", ...CATALOGUE_LINKS, "Change password"], ["/api/rosters/submitted"]],
    ["unit_manager", "Unit Manager", /No unit is assigned to you yet/, ["Home", "Roster", "Solve history", "Requests", "Swaps", "Covers", "Unit rules", "Rostering attributes", "Nurses", "Leave", ...CATALOGUE_LINKS, "Change password"], ["/api/units"]],
    ["staff_nurse", "Staff Nurse", "No roster yet for December 2026.", ["Home", "My month", "My roster", "Swaps", "Covers", ...CATALOGUE_LINKS, "Change password"], ["/api/nurses/my-unit", "/api/rosters/mine"]],
  ])("%s has a home of its own and none of the System Admin's pages", async (role, name, says, links, reads) => {
    const { router, calls } = start("/", {
      ...session({ ...ME, username: "someone", full_name: "Some One", role }),
      "GET /api/units": { body: [] },
      "GET /api/nurses/my-unit": { body: { id: "u1", name: "ER" } },
      "GET /api/rosters/mine": { body: NO_ROSTER_YET },
      "GET /api/rosters/submitted": { body: [] },
      "GET /api/nurses/expiries": { body: NO_EXPIRIES },
    })

    expect(await screen.findByText(says)).toBeInTheDocument()
    expect(router.state.location.pathname).toBe("/")
    expect(screen.getByRole("heading", { level: 1, name: "Some One" })).toBeInTheDocument()
    expect(screen.getByText(`someone · ${name}`)).toBeInTheDocument()
    expect(navLinks()).toEqual(links)
    await waitFor(() => expect(pageReads(calls).sort()).toEqual(["/api/auth/session", ...reads].sort()))
  })

  // A Unit Manager with a nurse profile is on their unit's rosters, so their own roster and month are theirs to
  // read too: first, apart from the unit's pages, and no request or swap of a Staff Nurse's.
  describe("a Unit Manager with a nurse profile", () => {
    const MANAGER_NURSE = { ...ME, username: "omar", full_name: "Omar Said", role: "unit_manager", has_nurse_profile: true }
    const MANAGER_LINKS = ["Home", "My roster", "My month", "Roster", "Solve history", "Requests", "Swaps", "Covers", "Unit rules", "Rostering attributes", "Nurses", "Leave", ...CATALOGUE_LINKS, "Change password"]
    const managerNurse = (extra = {}) => ({
      ...session(MANAGER_NURSE),
      "GET /api/units": { body: [{ id: "u1", name: "ER", archived: false, manager: null, rules_need_review: false }] },
      ...extra,
    })
    const home = () => screen.getByRole("main")

    it("has My roster and My month in the header and on Home", async () => {
      const { user, router } = start("/", managerNurse({ "GET /api/rosters/mine": { body: NO_ROSTER_YET } }))

      expect(await screen.findByText("Your unit is ER.")).toBeInTheDocument()
      expect(navLinks()).toEqual(MANAGER_LINKS)
      expect(within(home()).getByRole("link", { name: "My month" })).toHaveAttribute("href", "/my-month")
      await user.click(within(home()).getByRole("link", { name: "My roster" }))

      expect(await screen.findByRole("heading", { level: 1, name: "Roster of ER" })).toBeInTheDocument()
      expect(router.state.location.pathname).toBe("/my-roster")
    })

    it("has them on Home even with no unit to manage", async () => {
      start("/", managerNurse({ "GET /api/units": { body: [] } }))

      expect(await screen.findByText(/No unit is assigned to you yet/)).toBeInTheDocument()
      expect(within(home()).getByRole("link", { name: "My roster" })).toBeInTheDocument()
      expect(within(home()).getByRole("link", { name: "My month" })).toBeInTheDocument()
    })

    it("has them in the phone's Menu, and My month opens from it", async () => {
      const { user, router } = start("/", managerNurse({ "GET /api/rosters/mine/month": { body: NO_HOURS_YET }, ...NO_REQUESTS,
        "GET /api/nurses/my-skills": { body: [] }, "GET /api/nurses/my-leave": { body: { month: "2026-11", runs: [] } } }))
      await user.click(await screen.findByRole("button", { name: "Menu" }))
      const menu = screen.getByRole("navigation", { name: "Main menu" })
      expect(within(menu).getAllByRole("link").map((link) => link.textContent)).toEqual(MANAGER_LINKS)

      await user.click(within(menu).getByRole("link", { name: "My month" }))

      expect(await screen.findByRole("heading", { level: 1, name: "My month" })).toBeInTheDocument()
      expect(router.state.location.pathname).toBe("/my-month")
    })

    it("a manager without one has neither, on Home or in the header", async () => {
      start("/", managerNurse({ ...session({ ...MANAGER_NURSE, has_nurse_profile: false }) }))

      expect(await screen.findByText("Your unit is ER.")).toBeInTheDocument()
      expect(navLinks()).not.toContain("My roster")
      expect(navLinks()).not.toContain("My month")
      expect(within(home()).queryByRole("link")).not.toBeInTheDocument()
    })
  })

  // On a phone the header is the name, the bell and a Menu button: the pages, the password and Log out are behind it.
  // Which header shows is CSS alone (below 640 px), so here, with no CSS, the menu is only in the page once opened.
  describe("on a phone", () => {
    const menu = () => screen.getByRole("navigation", { name: "Main menu" })

    it("the Menu button opens the same pages, the password and Log out, and choosing a page closes it", async () => {
      const { user, router } = start("/", { ...nurseHome(), "GET /api/rosters/mine/month": { body: NO_HOURS_YET }, ...NO_REQUESTS,
        "GET /api/nurses/my-skills": { body: [] }, "GET /api/nurses/my-leave": { body: { month: "2026-11", runs: [] } } })
      const button = await screen.findByRole("button", { name: "Menu" })
      expect(button).toHaveAttribute("aria-expanded", "false")
      expect(screen.queryByRole("navigation", { name: "Main menu" })).not.toBeInTheDocument()

      await user.click(button)

      expect(button).toHaveAttribute("aria-expanded", "true")
      expect(within(menu()).getAllByRole("link").map((link) => link.textContent)).toEqual(["Home", "My month", "My roster", "Swaps", "Covers", ...CATALOGUE_LINKS, "Change password"])
      // What the button opens: the pages, then who is signed in and Log out.
      const opened = within(document.getElementById(button.getAttribute("aria-controls")!)!)
      expect(opened.getByText("h123 · Staff Nurse")).toBeInTheDocument()
      expect(opened.getByRole("button", { name: "Log out" })).toBeInTheDocument()

      await user.click(within(menu()).getByRole("link", { name: "My month" }))

      expect(await screen.findByRole("heading", { level: 1, name: "My month" })).toBeInTheDocument()
      expect(router.state.location.pathname).toBe("/my-month")
      expect(button).toHaveAttribute("aria-expanded", "false")
      expect(screen.queryByRole("navigation", { name: "Main menu" })).not.toBeInTheDocument()
    })

    it("choosing the page already open closes the menu too", async () => {
      const { user } = start("/", nurseHome())
      const button = await screen.findByRole("button", { name: "Menu" })
      await user.click(button)

      await user.click(within(menu()).getByRole("link", { name: "Home" }))

      expect(button).toHaveAttribute("aria-expanded", "false")
      expect(screen.queryByRole("navigation", { name: "Main menu" })).not.toBeInTheDocument()
    })

    it("Escape closes the menu and puts focus back on its button", async () => {
      const { user } = start("/", nurseHome())
      const button = await screen.findByRole("button", { name: "Menu" })
      await user.click(button)
      await user.tab()
      expect(within(menu()).getByRole("link", { name: "Home" })).toHaveFocus()

      await user.keyboard("{Escape}")

      expect(screen.queryByRole("navigation", { name: "Main menu" })).not.toBeInTheDocument()
      expect(button).toHaveFocus()
    })
  })
})

const CNO = {
  id: "019a0000-0000-7000-8000-000000000001", username: "fatimah", full_name: "Fatimah Al-Otaibi", role: "cno",
  is_active: true, must_change_password: false, last_login: null,
}
const MANAGER = { ...CNO, id: "019a0000-0000-7000-8000-000000000002", username: "khalid", full_name: "Khalid Omar", role: "unit_manager" }

describe("the users page", () => {
  const page = (users: unknown[], extra = {}) => admin({ "GET /api/users": { body: users }, ...extra })

  it("lists the users with their state", async () => {
    start("/users", page([CNO, { ...MANAGER, is_active: false, must_change_password: true }]))

    const row = (await screen.findByRole("cell", { name: "khalid" })).closest("tr")!
    expect(within(row).getByText("Deactivated")).toBeInTheDocument()
    expect(within(row).getByText("Password change due")).toBeInTheDocument()
    expect(within(row).getByText("Never")).toBeInTheDocument()
    expect(document.title).toBe("Users · Nurse roster")
  })

  it("offers no nurse and no second System Admin to create", async () => {
    start("/users", page([]))

    const options = within(await screen.findByLabelText("Role")).getAllByRole("option").map((option) => option.textContent)
    expect(options).toEqual(["Choose a role", "CNO", "Deputy CNO", "Unit Manager"])
    expect(screen.getByText(/There are no users yet/)).toBeInTheDocument()
  })

  it("a role already held cannot be chosen, and says who holds it", async () => {
    start("/users", page([CNO, MANAGER]))

    const select = await screen.findByLabelText("Role")
    expect(within(select).getByRole("option", { name: "CNO (fatimah holds this role)" })).toBeDisabled()
    expect(within(select).getByRole("option", { name: "Unit Manager" })).toBeEnabled()
  })

  async function fillNewUser(user: ReturnType<typeof userEvent.setup>, role = "cno") {
    await user.selectOptions(await screen.findByLabelText("Role"), role)
    await user.type(screen.getByLabelText("Full name"), "Fatimah Al-Otaibi")
    await user.type(screen.getByLabelText("Username"), "fatimah")
    await user.click(screen.getByRole("button", { name: "Create user" }))
  }

  it("creates a user and shows the password until Done, and then never again", async () => {
    let users: unknown[] = []
    const { user, calls, router } = start("/users", admin({
      "GET /api/users": () => ({ body: users }),
      "POST /api/users": () => {
        users = [CNO]
        return { status: 201, body: { user: { ...CNO, must_change_password: true }, generated_password: "abcd-efgh-jkmn-pqrs" } }
      },
    }))

    await fillNewUser(user)

    const dialog = await screen.findByRole("alertdialog", { name: "Password for fatimah" })
    expect(within(dialog).getByLabelText("Generated password")).toHaveValue("abcd-efgh-jkmn-pqrs")
    expect(within(dialog).getByText(/only time it is shown/)).toBeInTheDocument()
    expect(calls.find((call) => call.method === "POST")!.body).toEqual({ role: "cno", full_name: "Fatimah Al-Otaibi", username: "fatimah" })

    await user.keyboard("{Escape}")
    expect(screen.getByRole("alertdialog", { name: "Password for fatimah" })).toBeInTheDocument()
    // A blocked navigation never settles, so this one cannot be awaited.
    act(() => void router.navigate({ to: "/health" }))
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(router.state.location.pathname).toBe("/users")

    await user.click(within(dialog).getByRole("button", { name: "Done" }))

    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument())
    expect(screen.queryByDisplayValue("abcd-efgh-jkmn-pqrs")).not.toBeInTheDocument()
    expect(await screen.findByRole("cell", { name: "fatimah" })).toBeInTheDocument()
    expect(screen.getByLabelText("Username")).toHaveValue("")
  })

  it("says the password was copied", async () => {
    const writeText = vi.fn(async () => {})
    const { user } = start("/users", page([], {
      "POST /api/users": { status: 201, body: { user: CNO, generated_password: "abcd-efgh-jkmn-pqrs" } },
    }))
    await fillNewUser(user)
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true })

    await user.click(await screen.findByRole("button", { name: "Copy password" }))

    expect(await screen.findByText("Copied.")).toBeInTheDocument()
    expect(writeText).toHaveBeenCalledWith("abcd-efgh-jkmn-pqrs")
  })

  it("checks the form before asking the server", async () => {
    const { user, calls } = start("/users", page([]))

    await user.click(await screen.findByRole("button", { name: "Create user" }))

    expect(await screen.findByText("Choose a role.")).toBeInTheDocument()
    expect(screen.getByText("Enter their full name.")).toBeInTheDocument()
    expect(screen.getByText("Enter a username.")).toBeInTheDocument()
    await user.type(screen.getByLabelText("Username"), "has space")
    expect(await screen.findByText(/only letters, digits/)).toBeInTheDocument()
    expect(calls.some((call) => call.method === "POST")).toBe(false)
  })

  it("shows a second CNO's refusal on the role, focused", async () => {
    const { user } = start("/users", page([], {
      "POST /api/users": { status: 409, body: { role: ["fatimah is already the CNO. Deactivate that user first."], username: [] } },
    }))

    await fillNewUser(user)

    expect(await screen.findByText(/fatimah is already the CNO/)).toBeInTheDocument()
    expect(screen.getByLabelText("Role")).toHaveFocus()
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument()
  })

  it("shows a taken username on the username, focused", async () => {
    const { user } = start("/users", page([], {
      "POST /api/users": { status: 409, body: { role: [], username: ["This username is taken."] } },
    }))

    await fillNewUser(user)

    expect(await screen.findByText("This username is taken.")).toBeInTheDocument()
    expect(screen.getByLabelText("Username")).toHaveFocus()
  })

  it("deactivates after asking, and Cancel does nothing", async () => {
    let users = [CNO]
    const url = `/api/users/${CNO.id}/deactivate`
    const { user, calls } = start("/users", admin({
      "GET /api/users": () => ({ body: users }),
      [`POST ${url}`]: () => {
        users = [{ ...CNO, is_active: false }]
        return { body: users[0] }
      },
    }))

    await user.click(await screen.findByRole("button", { name: "Deactivate fatimah" }))
    expect(screen.getByRole("alertdialog", { name: "Deactivate fatimah?" })).toBeInTheDocument()
    await user.click(screen.getByRole("button", { name: "Cancel" }))
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument())
    expect(calls.some((call) => call.method === "POST")).toBe(false)

    await user.click(screen.getByRole("button", { name: "Deactivate fatimah" }))
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Deactivate" }))

    expect(await screen.findByRole("button", { name: "Reactivate fatimah" })).toBeInTheDocument()
    expect(screen.getByRole("status")).toHaveTextContent("fatimah is deactivated.")
  })

  it("a refused reactivation says why and stays open", async () => {
    const { user } = start("/users", page([{ ...CNO, is_active: false }], {
      [`POST /api/users/${CNO.id}/reactivate`]: { status: 409, body: { detail: "successor is already the CNO. Deactivate that user first." } },
    }))

    await user.click(await screen.findByRole("button", { name: "Reactivate fatimah" }))
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Reactivate" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("successor is already the CNO")
    expect(screen.getByRole("alertdialog", { name: "Reactivate fatimah?" })).toBeInTheDocument()
  })

  it("a reset shows the new password once", async () => {
    const { user } = start("/users", page([CNO], {
      [`POST /api/users/${CNO.id}/reset-password`]: { body: { user: CNO, generated_password: "wxyz-wxyz-wxyz-wxyz" } },
    }))

    await user.click(await screen.findByRole("button", { name: "Reset password of fatimah" }))
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Reset password" }))

    const dialog = await screen.findByRole("alertdialog", { name: "Password for fatimah" })
    expect(within(dialog).getByLabelText("Generated password")).toHaveValue("wxyz-wxyz-wxyz-wxyz")
    await user.click(within(dialog).getByRole("button", { name: "Done" }))
    await waitFor(() => expect(screen.queryByDisplayValue("wxyz-wxyz-wxyz-wxyz")).not.toBeInTheDocument())
  })

  describe("while a new user's password is on its way", () => {
    const CREATED = { status: 201, body: { user: { ...CNO, must_change_password: true }, generated_password: "abcd-efgh-jkmn-pqrs" } }
    const open = async (extra = {}) => {
      const started = start("/users", page([MANAGER], extra))
      await screen.findByRole("cell", { name: "khalid" })
      return { ...started, creating: hold("POST /api/users") }
    }
    const passwordShown = async () => expect(within(await screen.findByRole("alertdialog", { name: "Password for fatimah" })).getByLabelText("Generated password")).toHaveValue("abcd-efgh-jkmn-pqrs")

    it("correcting a name cannot replace the form, or the answer would find nobody to show it", async () => {
      const { user, creating } = await open()
      await fillNewUser(user)

      await user.click(screen.getByRole("button", { name: "Correct name of khalid" }))
      expect(screen.getByRole("button", { name: "Correct name of khalid" })).toHaveAttribute("aria-disabled", "true")
      expect(screen.getByRole("heading", { name: "Create a user" })).toBeInTheDocument()

      await creating.answer(CREATED)
      await passwordShown()
    })

    it("nothing else that gives a password can start, or one answer would replace the other", async () => {
      const { user, creating } = await open()
      await fillNewUser(user)

      expect(screen.getByRole("button", { name: "Reset password of khalid" })).toHaveAttribute("aria-disabled", "true")
      expect(screen.getByRole("button", { name: "Create user" })).toHaveAttribute("aria-disabled", "true")

      await creating.answer(CREATED)
      await passwordShown()
      await user.click(screen.getByRole("button", { name: "Done" }))
      expect(screen.getByRole("button", { name: "Reset password of khalid" })).toHaveAttribute("aria-disabled", "false")
    })

    it("asks before leaving, and shows the password to one who stays", async () => {
      const { user, router, creating } = await open()
      await fillNewUser(user)

      await user.click(screen.getByRole("link", { name: "Technical settings" }))

      const question = await screen.findByRole("alertdialog", { name: LEAVE_QUESTION })
      expect(router.state.location.pathname).toBe("/users")
      await user.click(within(question).getByRole("button", { name: "Stay" }))
      await creating.answer(CREATED)
      await passwordShown()
    })

    it("follows a link straight after Leave", async () => {
      const { user, router } = await open()
      await fillNewUser(user)
      await user.click(screen.getByRole("link", { name: "Technical settings" }))

      await leaveThen(router, await screen.findByRole("alertdialog", { name: LEAVE_QUESTION }), follow("Change password"))

      await onChangePassword(router)
    })

    it("still guards the page when a link straight after Leave comes back to it", async () => {
      const { user, router, creating } = await open()
      await fillNewUser(user)
      await user.click(screen.getByRole("link", { name: "Technical settings" }))

      await leaveThen(router, await screen.findByRole("alertdialog", { name: LEAVE_QUESTION }), follow("Users"))
      await waitFor(() => expect(router.state.status).toBe("idle"))
      expect(router.state.location.pathname).toBe("/users")
      await creating.answer({ status: 422, body: { detail: "No." } })
      await user.click(await screen.findByRole("button", { name: "Create user" }))
      await waitFor(() => expect(screen.getByRole("button", { name: "Log out" })).toHaveAttribute("aria-disabled", "true"))

      await user.click(screen.getByRole("link", { name: "Technical settings" }))
      expect(await screen.findByRole("alertdialog", { name: LEAVE_QUESTION })).toBeInTheDocument()
      expect(router.state.location.pathname).toBe("/users")
    })

    it("cannot log out from under it", async () => {
      const { user, calls, creating } = await open()
      await fillNewUser(user)

      expect(screen.getByRole("button", { name: "Log out" })).toHaveAttribute("aria-disabled", "true")
      await user.click(screen.getByRole("button", { name: "Log out" }))
      expect(calls.some((call) => call.url === "/api/auth/logout")).toBe(false)

      await creating.answer(CREATED)
      await passwordShown()
    })

    it("lets go when the user is refused", async () => {
      const { user, creating } = await open()
      await fillNewUser(user)

      await creating.answer({ status: 409, body: { username: ["This username is taken."] } })

      expect(await screen.findByText("This username is taken.")).toBeInTheDocument()
      expect(screen.getByRole("button", { name: "Correct name of khalid" })).toHaveAttribute("aria-disabled", "false")
      expect(screen.getByRole("button", { name: "Log out" })).toHaveAttribute("aria-disabled", "false")
    })

    it("a reset refuses to close, and asks before leaving", async () => {
      const { user, router } = start("/users", page([CNO]))
      await user.click(await screen.findByRole("button", { name: "Reset password of fatimah" }))
      const resetting = hold(`POST /api/users/${CNO.id}/reset-password`)
      const dialog = await screen.findByRole("alertdialog", { name: /Reset the password/ })
      await user.click(within(dialog).getByRole("button", { name: "Reset password" }))

      await user.click(within(dialog).getByRole("button", { name: "Cancel" }))
      await user.keyboard("{Escape}")
      expect(screen.getByRole("alertdialog", { name: /Reset the password/ })).toBeInTheDocument()

      act(() => void router.navigate({ to: "/health" }))
      await user.click(within(await screen.findByRole("alertdialog", { name: LEAVE_QUESTION })).getByRole("button", { name: "Stay" }))
      expect(router.state.location.pathname).toBe("/users")

      await resetting.answer({ body: { user: CNO, generated_password: "wxyz-wxyz-wxyz-wxyz" } })
      expect(within(await screen.findByRole("alertdialog", { name: "Password for fatimah" })).getByLabelText("Generated password")).toHaveValue("wxyz-wxyz-wxyz-wxyz")
    })
  })

  describe("correcting a name", () => {
    const url = `/api/users/${MANAGER.id}/full-name`

    it("saves the name, says so and puts focus back on the row, with the create form back too", async () => {
      let users = [CNO, MANAGER]
      const { user, calls } = start("/users", admin({
        "GET /api/users": () => ({ body: users }),
        [`PUT ${url}`]: () => {
          users = [CNO, { ...MANAGER, full_name: "Khalid Omari" }]
          return { body: users[1] }
        },
      }))

      const button = await screen.findByRole("button", { name: "Correct name of khalid" })
      await user.click(button)

      expect(screen.getByRole("heading", { name: "Correct the name of khalid" })).toBeInTheDocument()
      expect(screen.queryByRole("heading", { name: "Create a user" })).not.toBeInTheDocument()
      const field = screen.getByLabelText("Full name")
      expect(field).toHaveValue("Khalid Omar")
      expect(field).toHaveFocus()
      expect(screen.getByText("khalid", { selector: "span" })).toBeInTheDocument()

      await user.clear(field)
      await user.type(field, "  Khalid Omari ")
      await user.click(screen.getByRole("button", { name: "Save name" }))

      expect(await screen.findByRole("status")).toHaveTextContent("The name of khalid is now Khalid Omari.")
      expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ full_name: "Khalid Omari" })
      expect(await screen.findByRole("cell", { name: "Khalid Omari" })).toBeInTheDocument()
      expect(screen.getByRole("heading", { name: "Create a user" })).toBeInTheDocument()
      await waitFor(() => expect(screen.getByRole("button", { name: "Correct name of khalid" })).toHaveFocus())
    })

    it("says so when nothing changed", async () => {
      const { user } = start("/users", page([CNO, MANAGER], { [`PUT ${url}`]: { body: MANAGER } }))

      await user.click(await screen.findByRole("button", { name: "Correct name of khalid" }))
      await user.click(screen.getByRole("button", { name: "Save name" }))

      expect(await screen.findByRole("status")).toHaveTextContent("Nothing changed for khalid.")
    })

    it("refuses a blank name itself, beside the field with focus on it", async () => {
      const { user, calls } = start("/users", page([CNO, MANAGER]))

      await user.click(await screen.findByRole("button", { name: "Correct name of khalid" }))
      await user.clear(screen.getByLabelText("Full name"))
      await user.click(screen.getByRole("button", { name: "Save name" }))

      expect(await screen.findByText("Enter their full name.")).toBeInTheDocument()
      expect(screen.getByLabelText("Full name")).toHaveFocus()
      expect(calls.some((call) => call.method === "PUT")).toBe(false)
    })

    it("shows the API's refusal beside the field with focus on it", async () => {
      const { user } = start("/users", page([CNO, MANAGER], {
        [`PUT ${url}`]: { status: 422, body: { detail: [{ loc: ["body", "body", "full_name"], msg: "Value error, not a possible name" }] } },
      }))

      await user.click(await screen.findByRole("button", { name: "Correct name of khalid" }))
      await user.click(screen.getByRole("button", { name: "Save name" }))

      expect(await screen.findByText("not a possible name")).toBeInTheDocument()
      expect(screen.getByLabelText("Full name")).toHaveFocus()
      expect(screen.queryByRole("alert")).not.toBeInTheDocument()
    })

    it("says so when the save fails for any other reason, and stays open", async () => {
      const { user } = start("/users", page([CNO, MANAGER], { [`PUT ${url}`]: { status: 500, body: {} } }))

      await user.click(await screen.findByRole("button", { name: "Correct name of khalid" }))
      await user.click(screen.getByRole("button", { name: "Save name" }))

      expect(await screen.findByRole("alert")).toHaveTextContent("The name could not be saved.")
      expect(screen.getByRole("heading", { name: "Correct the name of khalid" })).toBeInTheDocument()
    })

    it("Cancel saves nothing and puts focus back on the row", async () => {
      const { user, calls } = start("/users", page([CNO, MANAGER]))

      await user.click(await screen.findByRole("button", { name: "Correct name of khalid" }))
      await user.click(screen.getByRole("button", { name: "Cancel" }))

      expect(screen.getByRole("heading", { name: "Create a user" })).toBeInTheDocument()
      expect(screen.getByRole("button", { name: "Correct name of khalid" })).toHaveFocus()
      expect(calls.some((call) => call.method === "PUT")).toBe(false)
    })

    it("is offered for a deactivated account", async () => {
      start("/users", page([{ ...MANAGER, is_active: false }]))

      expect(await screen.findByRole("button", { name: "Correct name of khalid" })).toBeEnabled()
    })
  })
})

describe("the audit log page", () => {
  const ENTRY = {
    id: 7, at: "2026-11-06T00:30:15+03:00", actor: "admin", action: "user.deactivated", target: "fatimah",
    before: { is_active: true }, after: { is_active: false }, ip: "10.1.2.3",
  }
  const OPTIONS = { "GET /api/audit/filter-options": { body: { actions: ["auth.login", "user.deactivated", "later.action"], users: ["admin", "fatimah"] } } }
  function auditStart(path: string, answer: (params: URLSearchParams) => Answer) {
    return start(path, admin({
      ...OPTIONS,
      "GET /api/audit/entries": ({ url }: { url: string }) => answer(new URL(url, "http://x").searchParams),
    }))
  }

  it("shows each entry in hospital time, with what changed", async () => {
    auditStart("/audit-log", () => ({ body: { entries: [ENTRY, { ...ENTRY, id: 6, actor: null, action: "auth.login_failed", target: "", before: null, after: { reason: "wrong_credentials", failures: 1 }, ip: null }, { ...ENTRY, id: 5, actor: null, action: "later.action" }], older_than: null } }))

    const row = (await screen.findAllByText("2026-11-06 00:30:15"))[0].closest("tr")!
    expect(within(row).getByText("admin")).toBeInTheDocument()
    expect(within(row).getByText("Deactivated user")).toBeInTheDocument()
    expect(within(row).getByText("fatimah")).toBeInTheDocument()
    expect(within(row).getByText("is_active: true → false")).toBeInTheDocument()
    expect(within(row).getByText("10.1.2.3")).toBeInTheDocument()
    expect(screen.getByText("Unknown")).toBeInTheDocument()
    expect(screen.getByText("System")).toBeInTheDocument()
    expect(document.title).toBe("Audit log · Nurse roster")
  })

  it("shows a failed login at an unknown name with no one to name and nothing in its target", async () => {
    auditStart("/audit-log", () => ({ body: { entries: [{ ...ENTRY, actor: null, action: "auth.login_failed", target: "", before: null, after: { reason: "locked" } }], older_than: null } }))

    const row = (await screen.findByText("Failed login")).closest("tr")!
    // Time, Who, Action, On, Details, IP address: "On" is empty.
    expect(within(row).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(
      ["2026-11-06 00:30:15", "Unknown", "Failed login", "", 'reason: "locked"', "10.1.2.3"])
  })

  it("names a throttle, with no one to name and the window's end", async () => {
    auditStart("/audit-log", () => ({ body: { entries: [{ ...ENTRY, actor: null, action: "auth.login_throttled", target: "", before: null, after: { until: "2026-11-06T00:45:15+03:00" } }], older_than: null } }))

    const row = (await screen.findByText("Logins throttled")).closest("tr")!
    expect(within(row).getByText("Unknown")).toBeInTheDocument()
    expect(within(row).getByText('until: "2026-11-06T00:45:15+03:00"')).toBeInTheDocument()
    expect(within(row).getByText("10.1.2.3")).toBeInTheDocument()
  })

  it("names a roster version's file taken, with who took which version and nothing from the file", async () => {
    auditStart("/audit-log", () => ({ body: { entries: [{ ...ENTRY, actor: "mona", action: "roster.exported", target: "ER, 2026-06", before: null, after: { version: 2, format: "xlsx" } }], older_than: null } }))

    const row = (await screen.findAllByText("2026-11-06 00:30:15"))[0].closest("tr")!
    expect(within(row).getByText("Exported a roster version")).toBeInTheDocument()
    expect(within(row).getByText("ER, 2026-06")).toBeInTheDocument()
  })

  it("applies the filters to the address and the request, and clears them", async () => {
    const seen: string[] = []
    const { user, router } = auditStart("/audit-log", (params) => {
      seen.push(params.toString())
      return { body: { entries: [ENTRY], older_than: null } }
    })

    await user.type(await screen.findByLabelText("User"), "fatimah")
    await waitFor(() => expect(within(screen.getByLabelText("Action")).getByRole("option", { name: "Logged in" })).toBeInTheDocument())
    await user.selectOptions(screen.getByLabelText("Action"), "auth.login")
    await user.type(screen.getByLabelText("From"), "2026-11-01")
    await user.type(screen.getByLabelText("To"), "2026-11-05")
    await user.click(screen.getByRole("button", { name: "Apply filters" }))

    await waitFor(() => expect(seen.at(-1)).toBe("user=fatimah&action=auth.login&date_from=2026-11-01&date_to=2026-11-05"))
    expect(router.state.location.search).toEqual({ user: "fatimah", action: "auth.login", date_from: "2026-11-01", date_to: "2026-11-05" })

    await user.click(screen.getByRole("link", { name: "Clear filters" }))

    await waitFor(() => expect(seen.at(-1)).toBe(""))
    expect(screen.getByLabelText("User")).toHaveValue("")
  })

  it("the form follows the address, including a username of digits", async () => {
    const seen: string[] = []
    auditStart("/audit-log?user=12345&action=auth.login", (params) => {
      seen.push(params.toString())
      return { body: { entries: [], older_than: null } }
    })

    expect(await screen.findByText(/No entries match/)).toBeInTheDocument()
    expect(screen.getByLabelText("User")).toHaveValue("12345")
    expect(screen.getByLabelText("Action")).toHaveValue("auth.login")
    expect(seen.at(-1)).toBe("user=12345&action=auth.login")
  })

  it("drops a date that is not one from the address", async () => {
    const seen: string[] = []
    auditStart("/audit-log?date_from=nonsense&date_to=2026-11-05", (params) => {
      seen.push(params.toString())
      return { body: { entries: [], older_than: null } }
    })

    await screen.findByText(/No entries match/)
    expect(seen.at(-1)).toBe("date_to=2026-11-05")
  })

  it("the table can be scrolled from the keyboard", async () => {
    auditStart("/audit-log", () => ({ body: { entries: [ENTRY], older_than: null } }))

    expect(await screen.findByRole("region", { name: "Audit entries" })).toHaveAttribute("tabindex", "0")
  })

  it("refuses an end date before the start date", async () => {
    const seen: string[] = []
    const { user } = auditStart("/audit-log", (params) => {
      seen.push(params.toString())
      return { body: { entries: [], older_than: null } }
    })

    await user.type(await screen.findByLabelText("From"), "2026-11-05")
    await user.type(screen.getByLabelText("To"), "2026-11-01")
    await user.click(screen.getByRole("button", { name: "Apply filters" }))

    expect(await screen.findByText("This is before the start date.")).toBeInTheDocument()
    expect(seen.every((params) => params === "")).toBe(true)
  })

  it("walks to older entries keeping the filters, and back to the newest", async () => {
    const seen: string[] = []
    const { user } = auditStart("/audit-log?action=auth.login", (params) => {
      seen.push(params.toString())
      return params.get("older_than")
        ? { body: { entries: [{ ...ENTRY, id: 3, target: "older-one" }], older_than: null } }
        : { body: { entries: [ENTRY], older_than: 7 } }
    })

    await user.click(await screen.findByRole("link", { name: "Older entries" }))

    expect(await screen.findByText("older-one")).toBeInTheDocument()
    expect(seen.at(-1)).toBe("action=auth.login&older_than=7")
    expect(screen.queryByRole("link", { name: "Older entries" })).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole("heading", { name: "Entries" })).toHaveFocus())

    await user.click(screen.getByRole("link", { name: "Newest entries" }))

    await waitFor(() => expect(seen.at(-1)).toBe("action=auth.login"))
  })

  it("draws its page links with an outline's border", async () => {
    const { user } = auditStart("/audit-log", (params) => (params.get("older_than")
      ? { body: { entries: [ENTRY], older_than: 3 } }
      : { body: { entries: [ENTRY], older_than: 7 } }))

    await user.click(await screen.findByRole("link", { name: "Older entries" }))

    expect(await screen.findByRole("link", { name: "Newest entries" })).not.toHaveClass("border-transparent")
    expect(screen.getByRole("link", { name: "Older entries" })).not.toHaveClass("border-transparent")
  })
})

const SHIFT_D ={ id: "s1", code: "D", display_name: "Day", hours: 12, start_time: "07:00", end_time: "19:00", colour: "#0284c7", active: true, renames: [] }
const SHIFT_N = { id: "s2", code: "N", display_name: "Night", hours: 12, start_time: "19:00", end_time: "07:00", colour: "#4338ca", active: true, renames: [] }
const refusal = (field: (string | number)[], msg: string) => ({ status: 422, body: { detail: [{ loc: ["body", "body", ...field], msg: `Value error, ${msg}`, type: "value_error" }] } })
/** The CNO's home lists the rosters waiting for a decision: none, unless a test says otherwise. */
const asCno = (extra = {}) => ({ ...session(CNO_ME), "GET /api/rosters/submitted": { body: [] }, "GET /api/nurses/expiries": { body: NO_EXPIRIES }, ...extra })

describe("the shifts page", () => {
  const listed = { "GET /api/hospital/shifts": { body: [SHIFT_D, SHIFT_N] } }

  it("lists the catalogue for the CNO, with a shift's former codes", async () => {
    const renamed = { ...SHIFT_D, code: "DAY", renames: [{ old_code: "D", new_code: "DAY", at: "2026-11-05T10:00:00+03:00" }] }
    start("/shifts", asCno({ "GET /api/hospital/shifts": { body: [renamed, { ...SHIFT_N, active: false }] } }))

    const row = (await screen.findByText("DAY")).closest("tr") as HTMLElement
    expect(within(row).getByText("Day")).toBeInTheDocument()
    expect(within(row).getByText("07:00 to 19:00")).toBeInTheDocument()
    expect(within(row).getByText(/until 2026-11-05/)).toHaveTextContent("Was D until 2026-11-05")
    expect(within(row).getByText("Active")).toBeInTheDocument()
    expect(screen.getByText("Retired")).toBeInTheDocument()
  })

  it("adds a shift", async () => {
    const { user, calls } = start("/shifts", asCno({
      ...listed,
      "POST /api/hospital/shifts": ({ body }: { body: unknown }) => ({ status: 201, body: { ...(body as object), id: "s3", renames: [] } }),
    }))

    await user.type(await screen.findByLabelText("Code"), "T")
    await user.type(screen.getByLabelText("Display name"), "Twilight")
    await user.type(screen.getByLabelText("Hours"), "6")
    await user.type(screen.getByLabelText("Start time"), "16:00")
    await user.type(screen.getByLabelText("End time"), "22:00")
    await user.click(screen.getByRole("button", { name: "Add shift" }))

    expect(await screen.findByText("T is added.")).toBeInTheDocument()
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({
      code: "T", display_name: "Twilight", hours: 6, start_time: "16:00", end_time: "22:00", colour: "#64748b", active: true,
    })
    expect(screen.getByLabelText("Code")).toHaveValue("")
  })

  it("says what is wrong before asking the API", async () => {
    const { user, calls } = start("/shifts", asCno(listed))

    await user.type(await screen.findByLabelText("Code"), "=X")
    await user.click(screen.getByRole("button", { name: "Add shift" }))

    expect(await screen.findByText(/starting with a letter or digit/)).toBeInTheDocument()
    expect(screen.getByText("Enter a name.")).toBeInTheDocument()
    expect(screen.getByText(/whole hours from 1 to 24/)).toBeInTheDocument()
    expect(calls.some((call) => call.method === "POST")).toBe(false)
  })

  it("edits a shift in the form and gives focus back to its button", async () => {
    const { user, calls } = start("/shifts", asCno({
      ...listed,
      "PUT /api/hospital/shifts/s1": ({ body }: { body: unknown }) => ({ body: { ...SHIFT_D, ...(body as object) } }),
    }))

    const edit = await screen.findByRole("button", { name: "Edit D" })
    await user.click(edit)
    expect(screen.getByRole("heading", { name: "Edit D" })).toBeInTheDocument()
    expect(screen.getByLabelText("Code")).toHaveFocus()
    expect(screen.getByLabelText("Code")).toHaveAccessibleDescription(/forbidden successions change with it/)

    await user.clear(screen.getByLabelText("Code"))
    await user.type(screen.getByLabelText("Code"), "DAY")
    await user.click(screen.getByRole("button", { name: "Save shift" }))

    expect(await screen.findByText("DAY is saved.")).toBeInTheDocument()
    expect(calls.find((call) => call.method === "PUT")?.body).toEqual({
      code: "DAY", display_name: "Day", hours: 12, start_time: "07:00", end_time: "19:00", colour: "#0284c7", active: true,
    })
    expect(screen.getByRole("heading", { name: "Add a shift" })).toBeInTheDocument()
    await waitFor(() => expect(edit).toHaveFocus())
  })

  it("cancelling leaves the form empty and changes nothing", async () => {
    const { user, calls } = start("/shifts", asCno(listed))

    const edit = await screen.findByRole("button", { name: "Edit N" })
    await user.click(edit)
    await user.click(screen.getByRole("button", { name: "Cancel" }))

    expect(screen.getByLabelText("Code")).toHaveValue("")
    await waitFor(() => expect(edit).toHaveFocus())
    expect(calls.some((call) => call.method === "PUT")).toBe(false)
  })

  it("shows why a shift cannot be retired beside the switch", async () => {
    const { user } = start("/shifts", asCno({
      ...listed,
      "PUT /api/hospital/shifts/s2": refusal(["active"], "N is named in the forbidden successions of the hospital rules. Remove those pairs first."),
    }))

    await user.click(await screen.findByRole("button", { name: "Edit N" }))
    await user.click(screen.getByLabelText("Active"))
    await user.click(screen.getByRole("button", { name: "Save shift" }))

    await waitFor(() => expect(screen.getByLabelText("Active")).toHaveFocus())
    expect(screen.getByLabelText("Active")).toHaveAccessibleDescription(/Remove those pairs first/)
    expect(screen.getByLabelText("Active")).toBeInvalid()
  })

  it("shows a taken code beside its field", async () => {
    const { user } = start("/shifts", asCno({ ...listed, "POST /api/hospital/shifts": refusal(["code"], "D is already in use") }))

    await user.type(await screen.findByLabelText("Code"), "D")
    await user.type(screen.getByLabelText("Display name"), "Other")
    await user.type(screen.getByLabelText("Hours"), "6")
    await user.type(screen.getByLabelText("Start time"), "16:00")
    await user.type(screen.getByLabelText("End time"), "22:00")
    await user.click(screen.getByRole("button", { name: "Add shift" }))

    await waitFor(() => expect(screen.getByLabelText("Code")).toHaveFocus())
    expect(screen.getByLabelText("Code")).toHaveAccessibleDescription(/D is already in use/)
  })

  it.each(["deputy_cno", "unit_manager", "staff_nurse", "system_admin"])("%s reads the catalogue and can change nothing", async (role) => {
    start("/shifts", { ...session({ ...ME, role }), ...listed })

    expect(await screen.findByText("Night")).toBeInTheDocument()
    expect(screen.getByText(/Only the CNO can change these/)).toBeInTheDocument()
    expect(screen.getByRole("region", { name: "Shifts" })).toHaveAttribute("tabindex", "0")
    expect(screen.queryByRole("button", { name: /Edit/ })).not.toBeInTheDocument()
    expect(screen.queryByLabelText("Code")).not.toBeInTheDocument()
  })
})

describe("the leave types page", () => {
  const SICK = { id: "l1", name: "Sick", active: true }
  const listed = { "GET /api/hospital/leave-types": { body: [SICK, { id: "l2", name: "Hajj", active: false }] } }

  it("adds a type and shows a taken name beside its field", async () => {
    const answers = [refusal(["name"], "Sick is already a leave type"), { status: 201, body: { id: "l3", name: "Study", active: true } }]
    const { user } = start("/leave-types", asCno({ ...listed, "POST /api/hospital/leave-types": () => answers.shift() as Answer }))

    await user.type(await screen.findByLabelText("Name"), "Sick")
    await user.click(screen.getByRole("button", { name: "Add leave type" }))
    await waitFor(() => expect(screen.getByLabelText("Name")).toHaveAccessibleDescription(/already a leave type/))

    await user.clear(screen.getByLabelText("Name"))
    await user.type(screen.getByLabelText("Name"), "Study")
    await user.click(screen.getByRole("button", { name: "Add leave type" }))

    expect(await screen.findByText("Study is added.")).toBeInTheDocument()
  })

  it("edits and retires a type", async () => {
    const { user, calls } = start("/leave-types", asCno({
      ...listed,
      "PUT /api/hospital/leave-types/l1": ({ body }: { body: unknown }) => ({ body: { id: "l1", ...(body as object) } }),
    }))

    const edit = await screen.findByRole("button", { name: "Edit Sick" })
    await user.click(edit)
    await user.click(screen.getByLabelText("Active"))
    await user.click(screen.getByRole("button", { name: "Save leave type" }))

    expect(await screen.findByText("Sick is saved.")).toBeInTheDocument()
    expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ name: "Sick", active: false })
    await waitFor(() => expect(edit).toHaveFocus())
  })

  it("another role reads and can change nothing", async () => {
    start("/leave-types", { ...session(NURSE), ...listed })

    expect(await screen.findByText("Sick")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /Edit/ })).not.toBeInTheDocument()
    expect(screen.queryByLabelText("Name")).not.toBeInTheDocument()
  })
})

// The CNO keeps the skill catalogue; every other role reads it, and the System Admin has no link to it.
describe("the skills page", () => {
  const ICU = { id: "k1", name: "ICU", kind: "skill", issuing_body: "", expires: false, required_to_work: false, lets_a_nurse_precept: false, active: true, renames: [] }
  const BLS = { id: "k2", name: "BLS", kind: "skill", issuing_body: "", expires: true, required_to_work: true, lets_a_nurse_precept: false, active: true, renames: [] }
  const listed = { "GET /api/hospital/skills": { body: [BLS, ICU] } }
  const rowOf = async (name: string) => (await screen.findByText(name)).closest("tr") as HTMLElement
  /** The "Lets a nurse precept" column, the fifth. */
  const precepts = async (name: string) => within(await rowOf(name)).getAllByRole("cell")[4]

  it("lists the catalogue for the CNO, with both flags and a skill's former names", async () => {
    const renamed = { ...BLS, name: "Basic life support", renames: [{ old_name: "BLS", new_name: "Basic life support", at: "2026-11-05T10:00:00+03:00" }] }
    start("/skills", asCno({ "GET /api/hospital/skills": { body: [renamed, { ...ICU, active: false }] } }))

    const table = await screen.findByRole("table")
    expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["Name", "Issuing body", "Expires", "Required to work", "Lets a nurse precept", "Status", "Actions"])
    const bls = await rowOf("Basic life support")
    expect(within(bls).getByText(/until 2026-11-05/)).toHaveTextContent("Was BLS until 2026-11-05")
    expect(within(bls).getAllByRole("cell").map((cell) => cell.textContent).slice(1, 6)).toEqual(["", "Yes", "Yes", "No", "Active"])
    expect(within(await rowOf("ICU")).getAllByRole("cell").map((cell) => cell.textContent).slice(1, 6)).toEqual(["", "No", "No", "No", "Retired"])
  })

  it("adds a skill with both flags and empties the form", async () => {
    const { user, calls } = start("/skills", asCno({
      ...listed,
      "POST /api/hospital/skills": ({ body }: { body: unknown }) => ({ status: 201, body: { ...(body as object), id: "k3", renames: [] } }),
    }))

    await user.type(await screen.findByLabelText("Name"), "ACLS")
    expect(screen.getByLabelText("Expires")).not.toBeChecked()
    expect(screen.getByLabelText("Required to work")).not.toBeChecked()
    expect(screen.getByLabelText("Lets a nurse precept")).not.toBeChecked()
    expect(screen.getByLabelText("Active")).toBeChecked()
    await user.click(screen.getByLabelText("Expires"))
    await user.click(screen.getByLabelText("Required to work"))
    await user.click(screen.getByRole("button", { name: "Add skill" }))

    expect(await screen.findByText("ACLS is added.")).toBeInTheDocument()
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({ name: "ACLS", kind: "skill", issuing_body: "", expires: true, required_to_work: true, lets_a_nurse_precept: false, active: true })
    expect(screen.getByLabelText("Name")).toHaveValue("")
    expect(screen.getByLabelText("Expires")).not.toBeChecked()
  })

  it("says what is wrong before asking the API, and shows a taken name beside its field", async () => {
    const { user, calls } = start("/skills", asCno({ ...listed, "POST /api/hospital/skills": refusal(["name"], "ICU is already a skill") }))

    await user.click(await screen.findByRole("button", { name: "Add skill" }))
    expect(await screen.findByText("Enter a name.")).toBeInTheDocument()
    expect(calls.some((call) => call.method === "POST")).toBe(false)

    await user.type(screen.getByLabelText("Name"), "icu")
    await user.click(screen.getByRole("button", { name: "Add skill" }))

    await waitFor(() => expect(screen.getByLabelText("Name")).toHaveFocus())
    expect(screen.getByLabelText("Name")).toHaveAccessibleDescription(/ICU is already a skill/)
  })

  it("renames and retires a skill, then gives focus back to its button", async () => {
    const { user, calls } = start("/skills", asCno({
      ...listed,
      "PUT /api/hospital/skills/k2": ({ body }: { body: unknown }) => ({ body: { ...BLS, ...(body as object) } }),
    }))

    const edit = await screen.findByRole("button", { name: "Edit BLS" })
    await user.click(edit)
    expect(screen.getByRole("heading", { name: "Edit BLS" })).toBeInTheDocument()
    expect(screen.getByLabelText("Name")).toHaveFocus()
    expect(screen.getByLabelText("Name")).toHaveAccessibleDescription(/old name is recorded/)
    expect(screen.getByLabelText("Expires")).toBeChecked()

    await user.clear(screen.getByLabelText("Name"))
    await user.type(screen.getByLabelText("Name"), "Basic life support")
    await user.click(screen.getByLabelText("Active"))
    await user.click(screen.getByRole("button", { name: "Save skill" }))

    expect(await screen.findByText("Basic life support is saved.")).toBeInTheDocument()
    expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ name: "Basic life support", kind: "skill", issuing_body: "", expires: true, required_to_work: true, lets_a_nurse_precept: false, active: false })
    expect(screen.getByRole("heading", { name: "Add a skill" })).toBeInTheDocument()
    await waitFor(() => expect(edit).toHaveFocus())
  })

  // The third mark: a nurse holding a marked skill may be chosen as a preceptor.
  it("marks a skill as letting a nurse precept, on more than one skill, and clears it", async () => {
    const MARKED = { ...ICU, lets_a_nurse_precept: true }
    const { user, calls } = start("/skills", asCno({
      "GET /api/hospital/skills": { body: [BLS, MARKED] },
      "PUT /api/hospital/skills/k1": ({ body }: { body: unknown }) => ({ body: { ...MARKED, ...(body as object) } }),
      "PUT /api/hospital/skills/k2": ({ body }: { body: unknown }) => ({ body: { ...BLS, ...(body as object) } }),
    }))

    expect(await precepts("ICU")).toHaveTextContent("Yes")
    expect(await precepts("BLS")).toHaveTextContent("No")

    await user.click(screen.getByRole("button", { name: "Edit BLS" }))
    const mark = screen.getByLabelText("Lets a nurse precept")
    expect(mark).not.toBeChecked()
    expect(mark).toHaveAccessibleDescription("A nurse holding this skill can be assigned as the preceptor of a nurse on probation, unless it has expired.")
    expect(screen.getByText(/checked only when a preceptor is assigned/)).toHaveTextContent("A skill that lets a nurse precept is checked only when a preceptor is assigned, so turning that off later changes no preceptor already assigned.")
    await user.click(mark)
    await user.click(screen.getByRole("button", { name: "Save skill" }))
    expect(await screen.findByText("BLS is saved.")).toBeInTheDocument()
    expect(calls.filter((call) => call.method === "PUT").at(-1)?.body).toEqual({ name: "BLS", kind: "skill", issuing_body: "", expires: true, required_to_work: true, lets_a_nurse_precept: true, active: true })

    await user.click(screen.getByRole("button", { name: "Edit ICU" }))
    expect(screen.getByLabelText("Lets a nurse precept")).toBeChecked()
    await user.click(screen.getByLabelText("Lets a nurse precept"))
    await user.click(screen.getByRole("button", { name: "Save skill" }))
    expect(await screen.findByText("ICU is saved.")).toBeInTheDocument()
    expect(calls.filter((call) => call.method === "PUT").at(-1)?.body).toEqual({ name: "ICU", kind: "skill", issuing_body: "", expires: false, required_to_work: false, lets_a_nurse_precept: false, active: true })
  })

  it("adds a skill that lets a nurse precept", async () => {
    const { user, calls } = start("/skills", asCno({
      ...listed,
      "POST /api/hospital/skills": ({ body }: { body: unknown }) => ({ status: 201, body: { ...(body as object), id: "k3", renames: [] } }),
    }))

    await user.type(await screen.findByLabelText("Name"), "Preceptor course")
    await user.click(screen.getByLabelText("Lets a nurse precept"))
    await user.click(screen.getByRole("button", { name: "Add skill" }))

    expect(await screen.findByText("Preceptor course is added.")).toBeInTheDocument()
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({ name: "Preceptor course", kind: "skill", issuing_body: "", expires: false, required_to_work: false, lets_a_nurse_precept: true, active: true })
    expect(screen.getByLabelText("Lets a nurse precept")).not.toBeChecked()
  })

  // Each entry is a skill, a certificate or a licence, and names the body that issues it.
  const DEGREE = { id: "k3", name: "BSc Nursing", kind: "certificate", issuing_body: "King Saud University", expires: false, required_to_work: false, lets_a_nurse_precept: false, active: true, renames: [] }
  const SCFHS = { id: "k4", name: "SCFHS registration", kind: "licence", issuing_body: "Saudi Commission for Health Specialties", expires: true, required_to_work: true, lets_a_nurse_precept: false, active: true, renames: [] }
  const namesIn = (group: HTMLElement) => within(group).getAllByRole("row").slice(1).map((row) => within(row).getAllByRole("cell")[0].textContent)

  it("groups the catalogue under Skills, Certificates and Licences, each with its entries and their issuing bodies", async () => {
    start("/skills", asCno({ "GET /api/hospital/skills": { body: [DEGREE, BLS, ICU, SCFHS] } }))

    const table = await screen.findByRole("table")
    expect(within(table).getAllByRole("rowgroup").slice(1).map((group) => within(group).getAllByRole("row")[0].textContent)).toEqual(["Skills", "Certificates", "Licences"])
    expect(namesIn(within(table).getByRole("rowgroup", { name: "Skills" }))).toEqual(["BLS", "ICU"])
    expect(namesIn(within(table).getByRole("rowgroup", { name: "Certificates" }))).toEqual(["BSc Nursing"])
    expect(namesIn(within(table).getByRole("rowgroup", { name: "Licences" }))).toEqual(["SCFHS registration"])
    expect(within(await rowOf("SCFHS registration")).getAllByRole("cell")[1]).toHaveTextContent("Saudi Commission for Health Specialties")
  })

  it("leaves out a heading that has no entries", async () => {
    start("/skills", asCno({ "GET /api/hospital/skills": { body: [SCFHS, ICU] } }))

    const table = await screen.findByRole("table")
    expect(within(table).getByRole("rowgroup", { name: "Skills" })).toBeInTheDocument()
    expect(within(table).getByRole("rowgroup", { name: "Licences" })).toBeInTheDocument()
    expect(within(table).queryByRole("rowgroup", { name: "Certificates" })).not.toBeInTheDocument()
  })

  it("adds a licence with its issuing body, Expires on and fixed while the kind is licence", async () => {
    const { user, calls } = start("/skills", asCno({
      ...listed,
      "POST /api/hospital/skills": ({ body }: { body: unknown }) => ({ status: 201, body: { ...(body as object), id: "k4", renames: [] } }),
    }))

    const kind = await screen.findByLabelText("Kind")
    expect(kind).toHaveValue("skill")
    expect(kind).toHaveAccessibleDescription(/A licence permits a nurse to practise and always expires/)
    expect(screen.getByLabelText("Issuing body")).toHaveAccessibleDescription(/Saudi Commission for Health Specialties/)
    await user.type(screen.getByLabelText("Name"), "SCFHS registration")
    await user.selectOptions(kind, "Licence")
    expect(screen.getByLabelText("Expires")).toBeChecked()
    expect(screen.getByLabelText("Expires")).toBeDisabled()
    expect(screen.getByLabelText("Expires")).toHaveAccessibleDescription(/A licence always expires/)
    await user.type(screen.getByLabelText("Issuing body"), "Saudi Commission for Health Specialties")
    await user.click(screen.getByLabelText("Required to work"))
    await user.click(screen.getByRole("button", { name: "Add skill" }))

    expect(await screen.findByText("SCFHS registration is added.")).toBeInTheDocument()
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({ name: "SCFHS registration", kind: "licence", issuing_body: "Saudi Commission for Health Specialties", expires: true, required_to_work: true, lets_a_nurse_precept: false, active: true })
    expect(screen.getByLabelText("Kind")).toHaveValue("skill")
    expect(screen.getByLabelText("Expires")).toBeEnabled()
  })

  it("frees Expires again when the kind changes away from licence", async () => {
    const { user, calls } = start("/skills", asCno({
      "GET /api/hospital/skills": { body: [SCFHS] },
      "PUT /api/hospital/skills/k4": ({ body }: { body: unknown }) => ({ body: { ...SCFHS, ...(body as object) } }),
    }))

    await user.click(await screen.findByRole("button", { name: "Edit SCFHS registration" }))
    expect(screen.getByLabelText("Kind")).toHaveValue("licence")
    expect(screen.getByLabelText("Expires")).toBeDisabled()
    await user.selectOptions(screen.getByLabelText("Kind"), "Certificate")
    expect(screen.getByLabelText("Expires")).toBeEnabled()
    expect(screen.getByLabelText("Expires")).toBeChecked()
    await user.click(screen.getByLabelText("Expires"))
    await user.click(screen.getByRole("button", { name: "Save skill" }))

    expect(await screen.findByText("SCFHS registration is saved.")).toBeInTheDocument()
    expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ name: "SCFHS registration", kind: "certificate", issuing_body: "Saudi Commission for Health Specialties", expires: false, required_to_work: true, lets_a_nurse_precept: false, active: true })
  })

  it("shows a refused change to licence beside the kind", async () => {
    const words = "Amal Saleh holds ICU with no expiry, and a licence always expires. Take it off them first, then record it again with its expiry."
    const { user } = start("/skills", asCno({ ...listed, "PUT /api/hospital/skills/k1": refusal(["kind"], words) }))

    await user.click(await screen.findByRole("button", { name: "Edit ICU" }))
    await user.selectOptions(screen.getByLabelText("Kind"), "Licence")
    await user.click(screen.getByRole("button", { name: "Save skill" }))

    await waitFor(() => expect(screen.getByLabelText("Kind")).toHaveFocus())
    expect(screen.getByLabelText("Kind")).toHaveAccessibleDescription(expect.stringContaining(words))
  })

  it("cancelling an edit changes nothing", async () => {
    const { user, calls } = start("/skills", asCno(listed))

    const edit = await screen.findByRole("button", { name: "Edit ICU" })
    await user.click(edit)
    await user.click(screen.getByRole("button", { name: "Cancel" }))

    expect(screen.getByLabelText("Name")).toHaveValue("")
    await waitFor(() => expect(edit).toHaveFocus())
    expect(calls.some((call) => call.method === "PUT")).toBe(false)
  })

  it("an empty catalogue says so, and the CNO can add the first", async () => {
    start("/skills", asCno({ "GET /api/hospital/skills": { body: [] } }))

    expect(await screen.findByText("No skills yet.")).toBeInTheDocument()
    expect(screen.queryByRole("table")).not.toBeInTheDocument()
    expect(screen.getByRole("heading", { name: "Add a skill" })).toBeInTheDocument()
  })

  it("says when the catalogue cannot be read", async () => {
    start("/skills", asCno({ "GET /api/hospital/skills": { status: 500, body: {} } }))

    expect(await screen.findByRole("alert")).toHaveTextContent("The skills could not be read. Reload the page to try again.")
  })

  it.each(["deputy_cno", "unit_manager", "staff_nurse", "system_admin"])("%s reads the catalogue and can change nothing", async (role) => {
    start("/skills", { ...session({ ...ME, role }), "GET /api/hospital/skills": { body: [{ ...BLS, lets_a_nurse_precept: true }, ICU] } })

    expect(await screen.findByText("BLS")).toBeInTheDocument()
    expect(screen.getByRole("columnheader", { name: "Lets a nurse precept" })).toBeInTheDocument()
    expect(screen.getByRole("columnheader", { name: "Issuing body" })).toBeInTheDocument()
    expect(screen.getByRole("rowgroup", { name: "Skills" })).toBeInTheDocument()
    expect(screen.queryByLabelText("Kind")).not.toBeInTheDocument()
    expect(await precepts("BLS")).toHaveTextContent("Yes")
    expect(await precepts("ICU")).toHaveTextContent("No")
    expect(screen.getByText(/checked only when a preceptor is assigned/)).toBeInTheDocument()
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument()
    expect(screen.getByText(/Only the CNO can change these/)).toBeInTheDocument()
    expect(screen.getByRole("region", { name: "Skills" })).toHaveAttribute("tabindex", "0")
    expect(screen.queryByRole("button", { name: /Edit/ })).not.toBeInTheDocument()
    expect(screen.queryByLabelText("Name")).not.toBeInTheDocument()
    expect(screen.queryByRole("columnheader", { name: "Actions" })).not.toBeInTheDocument()
  })
  it("the audit log names what was done to the catalogue", async () => {
    const entry = (id: number, action: string) => ({ id, action, actor: "the-cno", target: "BLS", at: "2026-11-05T07:00:00+03:00", ip: null, before: null, after: null })
    start("/audit-log", admin({
      "GET /api/audit/entries": { body: { entries: [entry(1, "skill.created"), entry(2, "skill.changed"), { ...entry(3, "nurse.skills_changed"), target: "h123" }, { ...entry(4, "nurse.skills_imported"), target: "" }], next_before: null } },
      "GET /api/audit/filter-options": { body: { users: [], actions: [] } },
    }))

    expect(await screen.findByText("Added a skill")).toBeInTheDocument()
    expect(screen.getByText("Changed a skill")).toBeInTheDocument()
    expect(screen.getByText("Changed a nurse's skills")).toBeInTheDocument()
    expect(screen.getByText("Imported skills")).toBeInTheDocument()
  })
})

describe("the holidays and Ramadan page", () => {
  const NATIONAL = { id: "h1", date: "2026-09-23", name: "National Day" }
  const days = (first: number, last: number) =>
    Array.from({ length: last - first + 1 }, (_, index) => `2026-03-${String(first + index).padStart(2, "0")}`)
  const MARCH = { month: "2026-03", source: "derived", dates: days(1, 19), derived_dates: days(1, 19) }
  const months = (march: unknown = MARCH) => Array.from({ length: 12 }, (_, index) => {
    const month = `2026-${String(index + 1).padStart(2, "0")}`
    return month === "2026-03" ? march : { month, source: "derived", dates: [], derived_dates: [] }
  })
  // Umm al-Qura's Ramadan 1447 runs from 18 February to 19 March 2026.
  const CARD_1447 = { hijri_year: 1447, first: "2026-02-18", last: "2026-03-19", days: 30, source: "derived", calendar_first: "2026-02-18", calendar_last: "2026-03-19" }
  // What the calendar proposes for 2026 beside National Day, entered already: Arafat Day 1447 is 26 May 2026.
  const FOUNDING = { name: "Founding Day", first: "2026-02-22", length: 1 }
  const FITR = { name: "Eid al-Fitr", first: "2026-03-19", length: 4 }
  const ADHA = { name: "Eid al-Adha", first: "2026-05-26", length: 4 }
  const year2026 = (march?: unknown, holidays: unknown[] = [NATIONAL], cards: unknown[] = [CARD_1447], proposals: unknown[] = [FOUNDING, FITR, ADHA]) => ({
    "GET /api/hospital/public-holidays": { body: holidays },
    "GET /api/hospital/ramadan": { body: months(march) },
    "GET /api/hospital/ramadans": { body: cards },
    "GET /api/hospital/holiday-proposals": { body: proposals },
  })
  const row = async (name: string) => (await screen.findByRole("cell", { name })).closest("tr") as HTMLElement

  it("shows the year's holidays and each month's Ramadan days", async () => {
    const { calls } = start("/holidays?year=2026", asCno(year2026()))

    expect(await screen.findByText(/23 September 2026/)).toBeInTheDocument()
    const march = await row("March 2026")
    expect(within(march).getByText("1 to 19")).toBeInTheDocument()
    expect(within(march).getByText("The calendar")).toBeInTheDocument()
    expect(within(await row("April 2026")).getByText("None")).toBeInTheDocument()
    expect(calls.map((call) => call.url)).toContain("/api/hospital/ramadan?year=2026")
    expect(calls.map((call) => call.url)).toContain("/api/hospital/public-holidays?year=2026")
  })

  it("moves between years", async () => {
    const { user, router, calls } = start("/holidays?year=2026", asCno(year2026()))

    await user.click(await screen.findByRole("link", { name: "2027" }))

    await waitFor(() => expect(router.state.location.search).toEqual({ year: 2027 }))
    await waitFor(() => expect(calls.map((call) => call.url)).toContain("/api/hospital/ramadan?year=2027"))
    expect(await screen.findByRole("heading", { name: "Public holidays in 2027" })).toBeInTheDocument()
  })

  describe("with no year in the address", () => {
    afterEach(() => vi.useRealTimers())

    it.each([
      ["Pacific/Kiritimati", "2026-12-31T12:00:00Z", 2027],
      ["Pacific/Pago_Pago", "2027-01-01T05:00:00Z", 2026],
    ])("opens on the year it is in the hospital's time zone (%s at %s), not the browser's", async (zone, now, year) => {
      vi.setSystemTime(new Date(now))
      const { calls } = start("/holidays", { ...asCno(year2026()), ...session({ ...CNO_ME, time_zone: zone }) })

      expect(await screen.findByRole("heading", { name: `Public holidays in ${year}` })).toBeInTheDocument()
      expect(calls.map((call) => call.url)).toContain(`/api/hospital/public-holidays?year=${year}`)
      expect(screen.getByRole("link", { name: String(year + 1) })).toBeInTheDocument()
    })
  })

  it("draws its year links with an outline's border", async () => {
    start("/holidays?year=2026", asCno(year2026()))

    expect(await screen.findByRole("link", { name: "2025" })).not.toHaveClass("border-transparent")
    expect(screen.getByRole("link", { name: "2027" })).not.toHaveClass("border-transparent")
  })

  it("drops a hand-edited year from the address", async () => {
    const { calls } = start("/holidays?year=1800", asCno(year2026()))

    await screen.findByRole("heading", { name: "Holidays and Ramadan" })
    await waitFor(() => expect(calls.some((call) => call.url.includes("year=1800"))).toBe(false))
  })

  it("adds a holiday, and a date entered twice is refused beside the date", async () => {
    const answers = [refusal(["date"], "2026-09-23 is already a public holiday"), { status: 201, body: { id: "h2", date: "2026-02-22", name: "Founding Day" } }]
    const { user, calls } = start("/holidays?year=2026", asCno({ ...year2026(), "POST /api/hospital/public-holidays": () => answers.shift() as Answer }))

    await user.type(await screen.findByLabelText("Date"), "2026-09-23")
    await user.type(screen.getByLabelText("Name"), "Again")
    await user.click(screen.getByRole("button", { name: "Add holiday" }))
    await waitFor(() => expect(screen.getByLabelText("Date")).toHaveAccessibleDescription(/already a public holiday/))

    await user.clear(screen.getByLabelText("Date"))
    await user.type(screen.getByLabelText("Date"), "2026-02-22")
    await user.click(screen.getByRole("button", { name: "Add holiday" }))

    expect(await screen.findByText("Again is added.".replace("Again", "Founding Day"))).toBeInTheDocument()
    expect(calls.filter((call) => call.method === "POST").at(-1)?.body).toEqual({ date: "2026-02-22", name: "Again" })
  })

  it("refuses a date outside the year on the page, before asking the API", async () => {
    const { user, calls } = start("/holidays?year=2026", asCno(year2026()))

    await user.type(await screen.findByLabelText("Date"), "2027-01-01")
    await user.type(screen.getByLabelText("Name"), "New Year")
    await user.click(screen.getByRole("button", { name: "Add holiday" }))

    expect(await screen.findByText(/not in 2026/)).toBeInTheDocument()
    expect(calls.some((call) => call.method === "POST")).toBe(false)
  })

  it("edits a holiday and removes one, with focus staying on the form", async () => {
    const { user, calls } = start("/holidays?year=2026", asCno({
      ...year2026(),
      "PUT /api/hospital/public-holidays/h1": ({ body }: { body: unknown }) => ({ body: { id: "h1", ...(body as object) } }),
      "DELETE /api/hospital/public-holidays/h1": { status: 204 },
    }))

    await user.click(await screen.findByRole("button", { name: "Edit National Day" }))
    expect(screen.getByLabelText("Date")).toHaveFocus()
    await user.clear(screen.getByLabelText("Name"))
    await user.type(screen.getByLabelText("Name"), "Saudi National Day")
    await user.click(screen.getByRole("button", { name: "Save holiday" }))
    expect(await screen.findByText("Saudi National Day is saved.")).toBeInTheDocument()

    await user.click(screen.getByRole("button", { name: "Remove National Day" }))
    expect(calls.some((call) => call.method === "DELETE")).toBe(false)
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Remove holiday" }))

    expect(await screen.findByText("National Day is removed.")).toBeInTheDocument()
    await waitFor(() => expect(screen.getByLabelText("Date")).toHaveFocus())
    expect(calls.some((call) => call.method === "DELETE")).toBe(true)
  })

  it("fixes a month as a range, starting from what is in force", async () => {
    const fixed = { ...MARCH, source: "fixed", dates: days(2, 20) }
    const { user, calls } = start("/holidays?year=2026", asCno({ ...year2026(), "PUT /api/hospital/ramadan/2026-03": { body: fixed } }))

    const button = await screen.findByRole("button", { name: "Fix the dates of March 2026" })
    await user.click(button)
    expect(screen.getByRole("heading", { name: "Fix the Ramadan dates of March 2026" })).toHaveFocus()
    // The Ramadan card above has fields of these names too.
    const form = within(screen.getByRole("heading", { name: /Fix the Ramadan dates/ }).closest("[data-slot=card]") as HTMLElement)
    expect(form.getByLabelText("First day")).toHaveValue("2026-03-01")
    expect(form.getByLabelText("Last day")).toHaveValue("2026-03-19")

    await user.clear(form.getByLabelText("First day"))
    await user.type(form.getByLabelText("First day"), "2026-03-02")
    await user.clear(form.getByLabelText("Last day"))
    await user.type(form.getByLabelText("Last day"), "2026-03-20")
    await user.click(form.getByRole("button", { name: "Fix dates" }))

    expect(await screen.findByText("The Ramadan dates of March 2026 are fixed.")).toBeInTheDocument()
    expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ first: "2026-03-02", last: "2026-03-20" })
    await waitFor(() => expect(button).toHaveFocus())
  })

  it("refuses a backwards range on the page", async () => {
    const { user, calls } = start("/holidays?year=2026", asCno(year2026()))

    await user.click(await screen.findByRole("button", { name: "Fix the dates of March 2026" }))
    const card = screen.getByRole("heading", { name: /Fix the Ramadan dates/ }).closest("[data-slot=card]") as HTMLElement
    await user.clear(within(card).getByLabelText("Last day"))
    await user.type(within(card).getByLabelText("Last day"), "2026-02-20")
    await user.click(within(card).getByRole("button", { name: "Fix dates" }))

    expect(await screen.findByText("Enter a day in March 2026.")).toBeInTheDocument()
    expect(calls.some((call) => call.method === "PUT")).toBe(false)
  })

  it("fixes a month as a list, and none ticked means no Ramadan days", async () => {
    const { user, calls } = start("/holidays?year=2026", asCno({ ...year2026(), "PUT /api/hospital/ramadan/2026-03": { body: { ...MARCH, source: "fixed", dates: [] } } }))

    await user.click(await screen.findByRole("button", { name: "Fix the dates of March 2026" }))
    await user.click(screen.getByLabelText("A list of days"))
    const days31 = within(screen.getByRole("group", { name: "Days of March 2026" })).getAllByRole("checkbox")
    expect(days31).toHaveLength(31)
    for (const box of days31.filter((box) => (box as HTMLInputElement).checked)) await user.click(box)
    await user.click(within(screen.getByRole("heading", { name: /Fix the Ramadan dates/ }).closest("[data-slot=card]") as HTMLElement).getByRole("button", { name: "Fix dates" }))

    await screen.findByText("The Ramadan dates of March 2026 are fixed.")
    expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ dates: [] })
  })

  it("a fixed month shows the calendar's days too and can return to them", async () => {
    const fixed = { ...MARCH, source: "fixed", dates: days(2, 20) }
    const { user, calls } = start("/holidays?year=2026", asCno({ ...year2026(fixed), "DELETE /api/hospital/ramadan/2026-03": { body: MARCH } }))

    const march = await row("March 2026")
    expect(within(march).getByText("2 to 20")).toBeInTheDocument()
    expect(within(march).getByText("The calendar says: 1 to 19")).toBeInTheDocument()
    expect(within(march).getByText("The CNO")).toBeInTheDocument()

    await user.click(within(march).getByRole("button", { name: "Use the calendar for March 2026" }))
    const dialog = await screen.findByRole("alertdialog")
    expect(dialog).toHaveTextContent("The fixed dates (2 to 20) are dropped")
    expect(calls.some((call) => call.method === "DELETE")).toBe(false)
    await user.click(within(dialog).getByRole("button", { name: "Use the calendar" }))

    expect(await screen.findByText("March 2026 follows the calendar again.")).toBeInTheDocument()
    expect(calls.some((call) => call.method === "DELETE")).toBe(true)
    await waitFor(() => expect(screen.getByRole("button", { name: "Fix the dates of March 2026" })).toHaveFocus())
  })

  it("cancelling a removal changes nothing and keeps a failure from going unnoticed", async () => {
    const { user, calls } = start("/holidays?year=2026", asCno({ ...year2026(), "DELETE /api/hospital/public-holidays/h1": { status: 500 } }))

    const remove = await screen.findByRole("button", { name: "Remove National Day" })
    await user.click(remove)
    const dialog = await screen.findByRole("alertdialog")
    await user.click(within(dialog).getByRole("button", { name: "Remove holiday" }))
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/did not work/)
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }))

    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument())
    await waitFor(() => expect(remove).toHaveFocus())
    expect(screen.getByText("National Day")).toBeInTheDocument()
    expect(calls.filter((call) => call.method === "DELETE")).toHaveLength(1)
  })

  it("a month fixed to no days reopens as a list", async () => {
    const { user } = start("/holidays?year=2026", asCno(year2026({ ...MARCH, source: "fixed", dates: [] })))

    await user.click(await screen.findByRole("button", { name: "Fix the dates of March 2026" }))

    expect(screen.getByLabelText("A list of days")).toBeChecked()
  })

  describe("the Ramadan cards", () => {
    const cardOf = async (name: string) => (await screen.findByRole("heading", { name })).closest("[data-slot=card]") as HTMLElement
    const confirmed = ({ body }: { body: unknown }) => {
      const { first, last } = body as { first: string; last: string }
      const days = (Date.parse(last) - Date.parse(first)) / 86_400_000 + 1
      return { body: { ...CARD_1447, first, last, days, source: "fixed" } }
    }

    it("shows the CNO each Ramadan of the year with the calendar's days, ready to confirm", async () => {
      const { calls } = start("/holidays?year=2026", asCno(year2026()))

      const card = await cardOf("Ramadan 1447")
      expect(card).toHaveTextContent("18 February – 19 March 2026, 30 days. The calendar's.")
      expect(within(card).getByLabelText("First day of Ramadan 1447")).toHaveValue("2026-02-18")
      expect(within(card).getByLabelText("Last day of Ramadan 1447")).toHaveValue("2026-03-19")
      expect(within(card).getByRole("button", { name: "Confirm Ramadan 1447" })).toBeInTheDocument()
      expect(calls.map((call) => call.url)).toContain("/api/hospital/ramadans?year=2026")
    })

    it("gives each of two Ramadans in a year its own card and fields", async () => {
      const across = { ...CARD_1447, hijri_year: 1448, first: "2026-12-20", last: "2027-01-18", days: 30, calendar_first: "2026-12-20", calendar_last: "2027-01-18" }
      start("/holidays?year=2026", asCno(year2026(undefined, [NATIONAL], [CARD_1447, across])))

      expect(await cardOf("Ramadan 1448")).toHaveTextContent("20 December 2026 – 18 January 2027, 30 days. The calendar's.")
      expect(screen.getByLabelText("First day of Ramadan 1447")).not.toBe(screen.getByLabelText("First day of Ramadan 1448"))
    })

    it("confirms the calendar's own dates as prefilled, says so, and keeps focus on the button", async () => {
      const { user, calls } = start("/holidays?year=2026", asCno({ ...year2026(), "PUT /api/hospital/ramadans/1447": confirmed }))

      const button = within(await cardOf("Ramadan 1447")).getByRole("button", { name: "Confirm Ramadan 1447" })
      await user.click(button)

      expect(await screen.findByText("Ramadan 1447 is confirmed: 18 February – 19 March 2026.")).toBeInTheDocument()
      expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ first: "2026-02-18", last: "2026-03-19" })
      expect(button).toHaveFocus()
      // The month table follows: both lists are read again.
      await waitFor(() => expect(calls.filter((call) => call.url === "/api/hospital/ramadan?year=2026")).toHaveLength(2))
      await waitFor(() => expect(calls.filter((call) => call.url === "/api/hospital/ramadans?year=2026")).toHaveLength(2))
    })

    it("closes a month form left open without taking focus from Confirm", async () => {
      const { user } = start("/holidays?year=2026", asCno({ ...year2026(), "PUT /api/hospital/ramadans/1447": confirmed }))

      await user.click(await screen.findByRole("button", { name: "Fix the dates of March 2026" }))
      const button = within(await cardOf("Ramadan 1447")).getByRole("button", { name: "Confirm Ramadan 1447" })
      await user.click(button)

      await screen.findByText("Ramadan 1447 is confirmed: 18 February – 19 March 2026.")
      expect(screen.queryByRole("heading", { name: "Fix the Ramadan dates of March 2026" })).not.toBeInTheDocument()
      expect(button).toHaveFocus()
    })

    it("confirms a Ramadan shifted by a day, and the card then reads the CNO's days beside the calendar's", async () => {
      const shifted = { ...CARD_1447, first: "2026-02-19", last: "2026-03-20", source: "fixed" }
      const answers = [[CARD_1447], [shifted]]
      const { user, calls } = start("/holidays?year=2026", asCno({
        ...year2026(),
        "GET /api/hospital/ramadans": () => ({ body: answers.length > 1 ? answers.shift() : answers[0] }),
        "PUT /api/hospital/ramadans/1447": confirmed,
      }))

      const card = await cardOf("Ramadan 1447")
      await user.clear(within(card).getByLabelText("First day of Ramadan 1447"))
      await user.type(within(card).getByLabelText("First day of Ramadan 1447"), "2026-02-19")
      await user.clear(within(card).getByLabelText("Last day of Ramadan 1447"))
      await user.type(within(card).getByLabelText("Last day of Ramadan 1447"), "2026-03-20")
      await user.click(within(card).getByRole("button", { name: "Confirm Ramadan 1447" }))

      expect(await screen.findByText("Ramadan 1447 is confirmed: 19 February – 20 March 2026.")).toBeInTheDocument()
      expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ first: "2026-02-19", last: "2026-03-20" })
      await waitFor(() => expect(card).toHaveTextContent("The CNO: 19 February – 20 March 2026, 30 days. The calendar says 18 February – 19 March."))
      expect(card).not.toHaveTextContent("The calendar's.")
    })

    it("a card whose months are fixed to none says it has no days", async () => {
      start("/holidays?year=2026", asCno(year2026(undefined, [NATIONAL], [{ ...CARD_1447, first: null, last: null, days: 0, source: "fixed" }])))

      const card = await cardOf("Ramadan 1447")
      expect(card).toHaveTextContent("The CNO: no days. The calendar says 18 February – 19 March 2026.")
      expect(within(card).getByLabelText("First day of Ramadan 1447")).toHaveValue("2026-02-18")
    })

    it("shows the API's refusal beside the field, with focus on it", async () => {
      const answers = [confirmed({ body: { first: "2026-02-18", last: "2026-03-19" } }), refusal(["last"], "That makes 36 days. A Ramadan has 29 or 30, so at most 35 are taken.")]
      const { user } = start("/holidays?year=2026", asCno({ ...year2026(), "PUT /api/hospital/ramadans/1447": () => answers.shift() as Answer }))

      const card = await cardOf("Ramadan 1447")
      // A first confirmation leaves its line, which the refused one must not leave standing.
      await user.click(within(card).getByRole("button", { name: "Confirm Ramadan 1447" }))
      await screen.findByText(/is confirmed/)
      const last = within(card).getByLabelText("Last day of Ramadan 1447")
      await user.clear(last)
      await user.type(last, "2026-03-25")
      await user.click(within(card).getByRole("button", { name: "Confirm Ramadan 1447" }))

      await waitFor(() => expect(last).toHaveAccessibleDescription("That makes 36 days. A Ramadan has 29 or 30, so at most 35 are taken."))
      expect(last).toHaveFocus()
      expect(screen.queryByText(/is confirmed/)).not.toBeInTheDocument()
      expect(last).toHaveAttribute("aria-invalid", "true")
    })

    it("refuses a last day before the first on the page, before asking the API", async () => {
      const { user, calls } = start("/holidays?year=2026", asCno(year2026()))

      const card = await cardOf("Ramadan 1447")
      const last = within(card).getByLabelText("Last day of Ramadan 1447")
      await user.clear(last)
      await user.type(last, "2026-02-10")
      await user.click(within(card).getByRole("button", { name: "Confirm Ramadan 1447" }))

      await waitFor(() => expect(last).toHaveAccessibleDescription("This is before the first day."))
      expect(calls.some((call) => call.method === "PUT")).toBe(false)
    })

    it("says when the cards cannot be read, and the month table still shows", async () => {
      start("/holidays?year=2026", asCno({ ...year2026(), "GET /api/hospital/ramadans": { status: 500, body: {} } }))

      expect(await screen.findByText("The Ramadans of 2026 could not be read. Reload the page to try again.")).toBeInTheDocument()
      expect(await screen.findByRole("cell", { name: "March 2026" })).toBeInTheDocument()
    })
  })

  describe("the holidays card", () => {
    const proposalsCard = async () => (await screen.findByRole("heading", { name: "Proposed by the calendar" })).closest("[data-slot=card]") as HTMLElement
    const lineOf = async (name: string) => (await screen.findByRole("group", { name })) as HTMLElement
    /** The proposals as they are read, the first time and after: one list per read, the last one kept. */
    const reads = (...lists: unknown[][]) => () => ({ body: lists.length > 1 ? lists.shift() : lists[0] })

    it("proposes to the CNO what the calendar gives and the holidays do not hold, each line editable", async () => {
      const { calls } = start("/holidays?year=2026", asCno(year2026()))

      const card = await proposalsCard()
      // In date order, as the API gives them.
      expect(within(card).getAllByRole("button").map((button) => button.getAttribute("aria-label"))).toEqual(["Add Founding Day", "Add Eid al-Fitr", "Add Eid al-Adha"])
      const fitr = await lineOf("Eid al-Fitr")
      expect(fitr).toHaveTextContent("From Thursday, 19 March 2026, 4 days.")
      expect(within(fitr).getByLabelText("First day of Eid al-Fitr")).toHaveValue("2026-03-19")
      expect(within(fitr).getByLabelText("Days of Eid al-Fitr")).toHaveValue(4)
      expect(within(await lineOf("Founding Day")).getByRole("button", { name: "Add Founding Day" })).toBeInTheDocument()
      expect(calls.map((call) => call.url)).toContain("/api/hospital/holiday-proposals?year=2026")
    })

    it("says so when every proposal of the year is entered", async () => {
      start("/holidays?year=2026", asCno(year2026(undefined, [NATIONAL], [CARD_1447], [])))

      expect(await proposalsCard()).toHaveTextContent("Every holiday the calendar proposes for 2026 is entered.")
    })

    it("adds a proposal, says what was added and skipped, and the line goes with focus on the next one", async () => {
      const { user, calls } = start("/holidays?year=2026", asCno({
        ...year2026(),
        "GET /api/hospital/holiday-proposals": reads([FOUNDING, FITR, ADHA], [FOUNDING, ADHA]),
        "POST /api/hospital/holiday-proposals": { body: { added: ["2026-03-19", "2026-03-21", "2026-03-22"], skipped: ["2026-03-20"] } },
      }))

      await user.click(within(await lineOf("Eid al-Fitr")).getByRole("button", { name: "Add Eid al-Fitr" }))

      expect(await screen.findByText("Eid al-Fitr is added on 19 March 2026, 21 March 2026 and 22 March 2026. 20 March 2026 was already a holiday and is unchanged.")).toBeInTheDocument()
      const post = calls.find((call) => call.method === "POST")
      expect(post?.url).toBe("/api/hospital/holiday-proposals?year=2026")
      expect(post?.body).toEqual({ name: "Eid al-Fitr", first: "2026-03-19", length: 4 })
      await waitFor(() => expect(screen.queryByRole("group", { name: "Eid al-Fitr" })).not.toBeInTheDocument())
      await waitFor(() => expect(within(screen.getByRole("group", { name: "Eid al-Adha" })).getByLabelText("First day of Eid al-Adha")).toHaveFocus())
      await waitFor(() => expect(calls.filter((call) => call.url === "/api/hospital/public-holidays?year=2026")).toHaveLength(2))
    })

    it("adds the last proposal left, and focus goes to the card", async () => {
      const { user } = start("/holidays?year=2026", asCno({
        ...year2026(undefined, [NATIONAL], [CARD_1447], [ADHA]),
        "GET /api/hospital/holiday-proposals": reads([ADHA], []),
        "POST /api/hospital/holiday-proposals": { body: { added: ["2026-05-26", "2026-05-27", "2026-05-28", "2026-05-29"], skipped: [] } },
      }))

      await user.click(within(await lineOf("Eid al-Adha")).getByRole("button", { name: "Add Eid al-Adha" }))

      expect(await screen.findByText("Eid al-Adha is added: 26 May – 29 May 2026, 4 days.")).toBeInTheDocument()
      await waitFor(() => expect(screen.getByRole("heading", { name: "Proposed by the calendar" })).toHaveFocus())
      expect(await proposalsCard()).toHaveTextContent("Every holiday the calendar proposes for 2026 is entered.")
    })

    it("adds a proposal with its first day and length changed; the line stays, as the calendar gives it", async () => {
      const { user, calls } = start("/holidays?year=2026", asCno({
        ...year2026(),
        "POST /api/hospital/holiday-proposals": { body: { added: ["2026-05-27", "2026-05-28", "2026-05-29", "2026-05-30", "2026-05-31", "2026-06-01"], skipped: [] } },
      }))

      const adha = await lineOf("Eid al-Adha")
      const first = within(adha).getByLabelText("First day of Eid al-Adha")
      const length = within(adha).getByLabelText("Days of Eid al-Adha")
      await user.clear(first)
      await user.type(first, "2026-05-27")
      await user.clear(length)
      await user.type(length, "6")
      const add = within(adha).getByRole("button", { name: "Add Eid al-Adha" })
      await user.click(add)

      expect(await screen.findByText("Eid al-Adha is added: 27 May – 1 June 2026, 6 days.")).toBeInTheDocument()
      expect(calls.find((call) => call.method === "POST")?.body).toEqual({ name: "Eid al-Adha", first: "2026-05-27", length: 6 })
      await waitFor(() => expect(first).toHaveValue("2026-05-26"))
      expect(length).toHaveValue(4)
      expect(add).toHaveFocus()
    })

    it("says when nothing was added because every day is a holiday already", async () => {
      const { user } = start("/holidays?year=2026", asCno({
        ...year2026(),
        "POST /api/hospital/holiday-proposals": { body: { added: [], skipped: ["2026-02-22"] } },
      }))

      await user.click(within(await lineOf("Founding Day")).getByRole("button", { name: "Add Founding Day" }))

      expect(await screen.findByText("Founding Day is not added: 22 February 2026 is already a holiday.")).toBeInTheDocument()
    })

    it("shows the API's refusal beside the field, with focus on it", async () => {
      const { user } = start("/holidays?year=2026", asCno({
        ...year2026(),
        "POST /api/hospital/holiday-proposals": refusal(["length"], "This runs past 2076, the last year the calendar covers."),
      }))

      const fitr = await lineOf("Eid al-Fitr")
      await user.click(within(fitr).getByRole("button", { name: "Add Eid al-Fitr" }))

      const length = within(fitr).getByLabelText("Days of Eid al-Fitr")
      await waitFor(() => expect(length).toHaveAccessibleDescription("This runs past 2076, the last year the calendar covers."))
      expect(length).toHaveFocus()
      expect(length).toHaveAttribute("aria-invalid", "true")
    })

    it.each([
      ["First day", "2027-01-02", "This is not in 2026. Go to that year to add it."],
      ["Days", "15", "Enter a number of days from 1 to 14."],
      ["Days", "0", "Enter a number of days from 1 to 14."],
    ])("refuses a %s of %s on the page, before asking the API", async (field, value, words) => {
      const { user, calls } = start("/holidays?year=2026", asCno(year2026()))

      const fitr = await lineOf("Eid al-Fitr")
      const input = within(fitr).getByLabelText(`${field} of Eid al-Fitr`)
      await user.clear(input)
      await user.type(input, value)
      await user.click(within(fitr).getByRole("button", { name: "Add Eid al-Fitr" }))

      await waitFor(() => expect(input).toHaveAccessibleDescription(words))
      expect(calls.some((call) => call.method === "POST")).toBe(false)
    })

    it("reads the proposals again after a holiday is entered by hand", async () => {
      const { user, calls } = start("/holidays?year=2026", asCno({
        ...year2026(),
        "POST /api/hospital/public-holidays": { status: 201, body: { id: "h2", date: "2026-02-22", name: "Founding Day" } },
      }))

      await user.type(await screen.findByLabelText("Date"), "2026-02-22")
      await user.type(screen.getByLabelText("Name"), "Founding Day")
      await user.click(screen.getByRole("button", { name: "Add holiday" }))

      await screen.findByText("Founding Day is added.")
      await waitFor(() => expect(calls.filter((call) => call.url === "/api/hospital/holiday-proposals?year=2026")).toHaveLength(2))
    })

    it("reads the proposals again after a Ramadan is confirmed, since Eid al-Fitr follows it", async () => {
      const { user, calls } = start("/holidays?year=2026", asCno({ ...year2026(), "PUT /api/hospital/ramadans/1447": { body: CARD_1447 } }))

      await user.click(await screen.findByRole("button", { name: "Confirm Ramadan 1447" }))

      await screen.findByText(/Ramadan 1447 is confirmed/)
      await waitFor(() => expect(calls.filter((call) => call.url === "/api/hospital/holiday-proposals?year=2026")).toHaveLength(2))
    })
  })

  it("another role reads and can change nothing", async () => {
    const { calls } = start("/holidays?year=2026", { ...session(NURSE), ...year2026() })

    expect(await screen.findByText("National Day")).toBeInTheDocument()
    expect(await screen.findByRole("cell", { name: "March 2026" })).toBeInTheDocument()
    expect(screen.getByRole("region", { name: "Ramadan dates in 2026" })).toHaveAttribute("tabindex", "0")
    expect(screen.queryByRole("button", { name: /Fix|Edit|Remove|Add|Confirm/ })).not.toBeInTheDocument()
    expect(screen.queryByLabelText("Date")).not.toBeInTheDocument()
    expect(screen.queryByRole("heading", { name: /Ramadan 1447/ })).not.toBeInTheDocument()
    expect(calls.some((call) => call.url.startsWith("/api/hospital/ramadans"))).toBe(false)
    expect(screen.queryByRole("heading", { name: "Proposed by the calendar" })).not.toBeInTheDocument()
    expect(calls.some((call) => call.url.startsWith("/api/hospital/holiday-proposals"))).toBe(false)
  })
})

const ER = { id: "u1", name: "ER", archived: false, manager: null, rules_need_review: true }
const MONA = { id: "m1", username: "mona", full_name: "Mona Ali" }
const OMAR = { id: "m2", username: "omar", full_name: "Omar Said" }

describe("a unit manager's home", () => {
  const asManager = (units: Answer) => ({
    ...session({ ...ME, username: "mona", full_name: "Mona Ali", role: "unit_manager" }),
    "GET /api/units": units,
  })

  it("shows the unit they hold and that its rules are still to be reviewed", async () => {
    start("/", asManager({ body: [{ ...ER, manager: MONA }] }))

    expect(await screen.findByText("Your unit is ER.")).toBeInTheDocument()
    expect(screen.getByText(/still the seeded defaults/)).toBeInTheDocument()
    expect(screen.queryByText(/No unit is assigned/)).not.toBeInTheDocument()
  })

  it("says nothing of a review once the rules are saved", async () => {
    start("/", asManager({ body: [{ ...ER, manager: MONA, rules_need_review: false }] }))

    expect(await screen.findByText("Your unit is ER.")).toBeInTheDocument()
    expect(screen.queryByText(/seeded defaults/)).not.toBeInTheDocument()
  })

  it("says no unit is assigned", async () => {
    start("/", asManager({ body: [] }))

    expect(await screen.findByText(/No unit is assigned to you yet/)).toBeInTheDocument()
  })

  it("does not claim no unit when the answer could not be read", async () => {
    start("/", asManager({ status: 500 }))

    expect(await screen.findByRole("alert")).toHaveTextContent(/could not be read/)
    expect(screen.queryByText(/No unit is assigned/)).not.toBeInTheDocument()
  })
})

describe("the CNO's home", () => {
  it("names the units with no manager and links to them", async () => {
    start("/", asCno({ "GET /api/units": { body: [ER, { ...ER, id: "u2", name: "ICU", manager: MONA }, { ...ER, id: "u3", name: "Old", archived: true }] } }))

    const warning = await screen.findByText(/have no manager|has no manager/)
    expect(warning).toHaveTextContent("ER has no manager.")
    expect(within(warning).getByRole("link", { name: "Assign one under Units" })).toHaveAttribute("href", "/units")
  })

  it("says nothing when every unit has one", async () => {
    start("/", asCno({ "GET /api/units": { body: [{ ...ER, manager: MONA }] } }))

    await screen.findByText(/under Hospital rules/)
    await waitFor(() => expect(vi.mocked(fetch)).toHaveBeenCalledWith("/api/units", expect.anything()))
    expect(screen.queryByText(/no manager/)).not.toBeInTheDocument()
  })
})

describe("the CNO's home: the expiries count", () => {
  it("shows how many rows the Expiries page has, and links to it", async () => {
    const { user, router } = start("/", asCno({ "GET /api/units": { body: [] }, "GET /api/nurses/expiries": { body: EXPIRIES } }))

    const count = await screen.findByText(/entries are on/)
    expect(count).toHaveTextContent("3 entries are on the Expiries page: skills that have expired or expire within 90 days, and skills required to work that a nurse has no record of.")
    await user.click(within(count).getByRole("link", { name: "the Expiries page" }))

    expect(await screen.findByRole("heading", { level: 1, name: "Expiries" })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe("/expiries")
  })

  it("says one in the singular", async () => {
    start("/", asCno({ "GET /api/units": { body: [] }, "GET /api/nurses/expiries": { body: { horizon_days: 90, count: 1, rows: [EXPIRY] } } }))

    expect(await screen.findByText(/entry is on/)).toHaveTextContent("1 entry is on the Expiries page")
  })

  it("says when nothing is on the page", async () => {
    start("/", asCno({ "GET /api/units": { body: [] } }))

    expect(await screen.findByText(/Nothing is on/)).toHaveTextContent(
      "Nothing is on the Expiries page: no skill has expired or expires within 90 days, and every nurse has a record of each skill required to work.")
  })

  it("says so when the count cannot be read, rather than showing none", async () => {
    start("/", asCno({ "GET /api/units": { body: [] }, "GET /api/nurses/expiries": { status: 500 } }))

    expect(await screen.findByRole("alert")).toHaveTextContent("The expiries could not be read, so their count is not shown.")
    expect(screen.queryByText(/Nothing is on/)).not.toBeInTheDocument()
  })
})

describe("the expiries page", () => {
  const DEPUTY_ME = { ...ME, username: "hala", full_name: "Hala Noor", role: "deputy_cno" }
  const rowOf = (name: string) => screen.getByRole("cell", { name }).closest("tr") as HTMLElement
  const cells = (row: HTMLElement) => within(row).getAllByRole("cell").map((cell) => cell.textContent)

  it("lists each kind of row for the CNO, with the nurse, her unit, the skill, its kind, state and date, in the API's order", async () => {
    start("/expiries", asCno({ "GET /api/nurses/expiries": { body: EXPIRIES } }))

    expect(await screen.findByRole("heading", { level: 1, name: "Expiries" })).toBeInTheDocument()
    await screen.findByRole("cell", { name: "Amal Saleh" })
    expect(document.title).toBe("Expiries · Nurse roster")
    expect(within(screen.getByRole("table")).getAllByRole("columnheader").map((head) => head.textContent)).toEqual(
      ["Name", "Hospital ID", "Unit", "Skill", "Kind", "State", "Expiry"])
    expect(cells(rowOf("Amal Saleh"))).toEqual(["Amal Saleh", "h123", "ER", "BLS", "Skill", "Expired", "31 Oct 2026"])
    expect(cells(rowOf("Badr Omar"))).toEqual(["Badr Omar", "h124", "ICU", "SCFHS", "Licence", "Expiring", "15 Jan 2027"])
    expect(cells(rowOf("Dana Ali"))).toEqual(["Dana Ali", "h125", "ER", "BLS", "Skill", "No record", "Required to work"])
    expect(within(screen.getByRole("table")).getAllByRole("row").slice(1).map((row) => within(row).getAllByRole("cell")[0].textContent))
      .toEqual(["Amal Saleh", "Badr Omar", "Dana Ali"])
    expect(screen.getByText(/Deactivated nurses and those past their end date are left out/)).toHaveTextContent(/You record and renew skills on the Nurses page, and the Deputy CNO on Rostering attributes\.$/)
  })

  it("is the Deputy CNO's too, from the header", async () => {
    const { user, router } = start("/", {
      ...session(DEPUTY_ME), "GET /api/rosters/submitted": { body: [] }, "GET /api/nurses/expiries": { body: EXPIRIES },
    })
    await screen.findByText("No roster is waiting for a decision.")

    await user.click(within(screen.getByRole("navigation", { name: "Main" })).getByRole("link", { name: "Expiries" }))

    expect(await screen.findByRole("cell", { name: "Badr Omar" })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe("/expiries")
    expect(screen.getByText(/You record and renew skills on Rostering attributes, and the CNO on the Nurses page\./)).toBeInTheDocument()
  })

  it("says when there is nothing to list", async () => {
    start("/expiries", asCno())

    expect(await screen.findByText(/Nothing to list: no skill has expired or expires within 90 days/)).toBeInTheDocument()
    expect(screen.queryByRole("table")).not.toBeInTheDocument()
  })

  it("says so when the list cannot be read", async () => {
    start("/expiries", asCno({ "GET /api/nurses/expiries": { status: 500 } }))

    expect(await screen.findByRole("alert")).toHaveTextContent("The expiries could not be read. Reload the page to try again.")
  })

  it("is not a unit manager's, who reads his own on his Nurses page", async () => {
    const { router } = start("/expiries", {
      ...session({ ...ME, username: "mona", full_name: "Mona Ali", role: "unit_manager" }), "GET /api/units": { body: [] },
    })

    expect(await screen.findByText(/No unit is assigned to you yet/)).toBeInTheDocument()
    expect(router.state.location.pathname).toBe("/")
  })
})

describe("the CNO's home when the units cannot be read", () => {
  it("says so rather than looking as though every unit has a manager", async () => {
    start("/", asCno({ "GET /api/units": { status: 500 } }))

    expect(await screen.findByRole("alert")).toHaveTextContent(/could not be read/)
  })
})

describe("the units page", () => {
  const CHOICES = [{ ...MONA, unit: "ICU" }, { ...OMAR, unit: null }]
  const ICU = { ...ER, id: "u2", name: "ICU", manager: MONA }
  const OLD = { ...ER, id: "u3", name: "Old", archived: true }
  const page = (extra = {}, units: unknown[] = [ER, ICU, OLD]) => asCno({
    "GET /api/units": { body: units },
    "GET /api/units/managers": { body: CHOICES },
    ...extra,
  })

  it("lists the units with their managers, and which have none", async () => {
    start("/units", page())

    const er = (await screen.findByRole("cell", { name: "ER" })).closest("tr")!
    expect(within(er).getByText("No manager")).toBeInTheDocument()
    const icu = screen.getByRole("cell", { name: "ICU" }).closest("tr")!
    expect(within(icu).getByText("Mona Ali")).toBeInTheDocument()
    expect(document.title).toBe("Units · Nurse roster")
  })

  it("an archived unit is on record and has nothing left to do", async () => {
    start("/units", page())

    const old = (await screen.findByRole("cell", { name: "Old" })).closest("tr")!
    expect(within(old).getByText("Archived")).toBeInTheDocument()
    expect(within(old).queryByRole("button")).not.toBeInTheDocument()
    expect(within(old).queryByText("No manager")).not.toBeInTheDocument()
  })

  it("says so when there are no units yet", async () => {
    start("/units", page({}, []))

    expect(await screen.findByText(/There are no units yet/)).toBeInTheDocument()
  })

  it("a manager who holds another unit cannot be chosen, and says which", async () => {
    start("/units", page())

    const select = await screen.findByLabelText("Manager")
    expect(within(select).getByRole("option", { name: "No manager yet" })).toBeEnabled()
    expect(within(select).getByRole("option", { name: "Mona Ali (mona, manages ICU)" })).toBeDisabled()
    expect(within(select).getByRole("option", { name: "Omar Said (omar)" })).toBeEnabled()
  })

  it("says who to ask when there are no manager accounts to choose", async () => {
    start("/units", page({ "GET /api/units/managers": { body: [] } }))

    expect(await screen.findByLabelText("Manager")).toHaveAccessibleDescription(/The System Admin creates unit manager accounts/)
  })

  it("adds a unit with its manager, and shows a taken name beside its field", async () => {
    const answers = [refusal(["name"], "ER is already a unit"), { status: 201, body: { ...ER, id: "u4", name: "CCU", manager: OMAR } }]
    const { user, calls } = start("/units", page({ "POST /api/units": () => answers.shift() as Answer }))

    await user.type(await screen.findByLabelText("Name"), "ER")
    await user.selectOptions(screen.getByLabelText("Manager"), "m2")
    await user.click(screen.getByRole("button", { name: "Add unit" }))
    await waitFor(() => expect(screen.getByLabelText("Name")).toHaveAccessibleDescription(/already a unit/))
    expect(screen.getByLabelText("Name")).toHaveFocus()

    await user.clear(screen.getByLabelText("Name"))
    await user.type(screen.getByLabelText("Name"), "CCU")
    await user.click(screen.getByRole("button", { name: "Add unit" }))

    expect(await screen.findByText("CCU is added.")).toBeInTheDocument()
    expect(calls.filter((call) => call.method === "POST").at(-1)?.body).toEqual({ name: "CCU", manager_id: "m2" })
    expect(screen.getByLabelText("Name")).toHaveValue("")
  })

  it("adding sends no manager as none, and needs a name", async () => {
    const { user, calls } = start("/units", page({ "POST /api/units": { status: 201, body: { ...ER, name: "CCU" } } }))

    await screen.findByLabelText("Name")
    await user.click(screen.getByRole("button", { name: "Add unit" }))
    expect(await screen.findByText("Enter a name.")).toBeInTheDocument()
    expect(calls.some((call) => call.method === "POST")).toBe(false)

    await user.type(screen.getByLabelText("Name"), "CCU")
    await user.click(screen.getByRole("button", { name: "Add unit" }))

    await screen.findByText("CCU is added.")
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({ name: "CCU", manager_id: null })
  })

  it("renames and reassigns a unit, with its current manager offered and focus returned", async () => {
    const { user, calls } = start("/units", page({
      "PUT /api/units/u2": { body: { ...ICU, name: "Intensive care", manager: OMAR } },
    }))

    const edit = await screen.findByRole("button", { name: "Edit ICU" })
    await user.click(edit)
    expect(screen.getByRole("heading", { name: "Edit ICU" })).toBeInTheDocument()
    expect(screen.getByLabelText("Manager")).toHaveValue("m1")
    expect(within(screen.getByLabelText("Manager")).getByRole("option", { name: /Mona Ali/ })).toBeEnabled()

    await user.clear(screen.getByLabelText("Name"))
    await user.type(screen.getByLabelText("Name"), "Intensive care")
    await user.selectOptions(screen.getByLabelText("Manager"), "m2")
    await user.click(screen.getByRole("button", { name: "Save unit" }))

    expect(await screen.findByText("Intensive care is saved.")).toBeInTheDocument()
    expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ name: "Intensive care", manager_id: "m2" })
    await waitFor(() => expect(edit).toHaveFocus())
  })

  it("shows a refused manager beside the select", async () => {
    const { user } = start("/units", page({
      "PUT /api/units/u1": refusal(["manager_id"], "omar already manages CCU."),
    }))

    await user.click(await screen.findByRole("button", { name: "Edit ER" }))
    await user.selectOptions(screen.getByLabelText("Manager"), "m2")
    await user.click(screen.getByRole("button", { name: "Save unit" }))

    await waitFor(() => expect(screen.getByLabelText("Manager")).toHaveAccessibleDescription(/already manages CCU/))
    expect(screen.getByLabelText("Manager")).toHaveFocus()
  })

  it("archives after asking, and focus lands where the next unit is entered", async () => {
    const { user, calls } = start("/units", page({ "POST /api/units/u2/archive": { body: { ...ICU, archived: true, manager: null } } }))

    await user.click(await screen.findByRole("button", { name: "Archive ICU" }))
    const dialog = screen.getByRole("alertdialog", { name: "Archive ICU?" })
    expect(within(dialog).getByText(/Mona Ali can then hold another unit/)).toBeInTheDocument()
    expect(calls.some((call) => call.url.endsWith("/archive"))).toBe(false)

    await user.click(within(dialog).getByRole("button", { name: "Archive" }))

    expect(await screen.findByText("ICU is archived.")).toBeInTheDocument()
    await waitFor(() => expect(screen.getByLabelText("Name")).toHaveFocus())
  })

  it("cancelling an archive changes nothing and returns focus to the row", async () => {
    const { user, calls } = start("/units", page())

    const button = await screen.findByRole("button", { name: "Archive ER" })
    await user.click(button)
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Cancel" }))

    await waitFor(() => expect(button).toHaveFocus())
    expect(calls.some((call) => call.method === "POST")).toBe(false)
  })

  it("shows why an archive was refused and keeps the question open", async () => {
    const { user } = start("/units", page({
      "POST /api/units/u1/archive": refusal(["archived"], "2 nurses belong to ER. Transfer or deactivate them first."),
    }))

    await user.click(await screen.findByRole("button", { name: "Archive ER" }))
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Archive" }))

    expect(await within(screen.getByRole("alertdialog")).findByRole("alert")).toHaveTextContent("2 nurses belong to ER")
  })

  it("leaves the form when the unit it is editing is archived", async () => {
    const { user } = start("/units", page({ "POST /api/units/u1/archive": { body: { ...ER, archived: true } } }))

    await user.click(await screen.findByRole("button", { name: "Edit ER" }))
    await user.click(screen.getByRole("button", { name: "Archive ER" }))
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Archive" }))

    expect(await screen.findByRole("heading", { name: "Add a unit" })).toBeInTheDocument()
  })
})

describe("deactivating a manager who holds a unit", () => {
  it("the users page says which unit they hold and what becomes of it", async () => {
    const { user } = start("/users", admin({ "GET /api/users": { body: [{ ...MANAGER, unit: "ER" }] } }))

    expect(await screen.findByText("ER")).toBeInTheDocument()
    await user.click(screen.getByRole("button", { name: "Deactivate khalid" }))

    const dialog = screen.getByRole("alertdialog", { name: "Deactivate khalid?" })
    expect(within(dialog).getByText(/ER is left with no manager until the CNO assigns one/)).toBeInTheDocument()
    expect(within(dialog).getByText(/Reactivating them does not give it back/)).toBeInTheDocument()
  })

  it("says nothing of a unit for anyone else", async () => {
    const { user } = start("/users", admin({ "GET /api/users": { body: [{ ...MANAGER, unit: null }] } }))

    await user.click(await screen.findByRole("button", { name: "Deactivate khalid" }))

    expect(within(screen.getByRole("alertdialog")).queryByText(/no manager/)).not.toBeInTheDocument()
  })
})

describe("a staff nurse's home", () => {
  it("shows their unit and that there is no roster yet", async () => {
    start("/", nurseHome("u1"))

    expect(await screen.findByText("Your unit is ER.")).toBeInTheDocument()
    expect(screen.getByText("No roster yet for December 2026.")).toBeInTheDocument()
  })

  it("does not claim a unit or a roster when the answer could not be read", async () => {
    start("/", { ...session(NURSE), "GET /api/nurses/my-unit": { status: 500 } })

    expect(await screen.findByRole("alert")).toHaveTextContent(/could not be read/)
    expect(screen.queryByText("No roster yet for December 2026.")).not.toBeInTheDocument()
  })
})

describe("the nurses page", () => {
  const UNIT_ER = { id: "u1", name: "ER", archived: false, manager: null, rules_need_review: true }
  const UNIT_ICU = { ...UNIT_ER, id: "u2", name: "ICU" }
  const UNIT_OLD = { ...UNIT_ER, id: "u3", name: "Old", archived: true }
  const AMAL = {
    id: "n1", hospital_id: "h123", full_name: "Amal Saleh", role: "staff_nurse", gender: "F", start_date: "2026-01-10",
    end_date: null, is_active: true, must_change_password: false, unit: { id: "u1", name: "ER" }, transfers: [], skills: [],
    on_probation: false, probation: null,
  }
  const BADR = { ...AMAL, id: "n2", hospital_id: "h124", full_name: "Badr Omar", gender: "M", unit: { id: "u2", name: "ICU" } }
  const MONA_NURSE = { ...AMAL, id: "n3", hospital_id: "mona", full_name: "Mona Ali", role: "unit_manager" }
  const CHOICE = { id: "m1", username: "mona", full_name: "Mona Ali", unit: { id: "u1", name: "ER" } }
  const refusal = (loc: string[], msg: string) => ({ status: 422, body: { detail: [{ type: "value_error", loc: ["body", "body", ...loc], msg }] } })

  const asCnoHere = (extra = {}, nurses: unknown[] = [AMAL, BADR]) => ({
    ...session({ ...ME, username: "chief", full_name: "The Chief", role: "cno" }),
    "GET /api/nurses": { body: nurses },
    "GET /api/nurses/managers": { body: [CHOICE] },
    "GET /api/units": { body: [UNIT_ER, UNIT_ICU, UNIT_OLD] },
    ...extra,
  })
  const asManagerHere = (nurses: unknown[], units: unknown[] = [UNIT_ER], extra = {}) => ({
    ...session({ ...ME, username: "mona", full_name: "Mona Ali", role: "unit_manager" }),
    "GET /api/nurses": { body: nurses },
    "GET /api/units": { body: units },
    "GET /api/nurses/expiries": { body: NO_EXPIRIES },
    ...extra,
  })

  it("lists every nurse with their unit for the CNO, and names the page", async () => {
    start("/nurses", asCnoHere())

    const row = (await screen.findByText("Amal Saleh")).closest("tr") as HTMLElement
    expect(within(row).getByText("h123")).toBeInTheDocument()
    expect(within(row).getByText("ER")).toBeInTheDocument()
    expect(within(row).getByText("Female")).toBeInTheDocument()
    expect(within(row).getByText("10 Jan 2026")).toBeInTheDocument()
    expect(within(row).getByText("Active")).toBeInTheDocument()
    expect(screen.getByRole("heading", { level: 1, name: "Nurses" })).toBeInTheDocument()
    expect(document.title).toBe("Nurses · Nurse roster")
  })

  describe("the manager's expiries", () => {
    const expiries = () => screen.getByRole("region", { name: "Expiries of ER" })

    it("lists his own unit's expiries above the nurses, with the kind and without a unit column", async () => {
      start("/nurses", asManagerHere([AMAL], [UNIT_ER], { "GET /api/nurses/expiries": { body: {
        horizon_days: 90, count: 2, rows: [EXPIRY, { ...EXPIRY, nurse_id: "n3", hospital_id: "h125", full_name: "Dana Ali", state: "no_record", expiry: null }] } } }))

      expect(await screen.findByRole("heading", { level: 2, name: "Expiries" })).toBeInTheDocument()
      const row = (await within(await screen.findByRole("region", { name: "Expiries of ER" })).findByRole("cell", { name: "Dana Ali" })).closest("tr") as HTMLElement
      expect(within(row).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["Dana Ali", "h125", "BLS", "Skill", "No record", "Required to work"])
      expect(within(expiries()).getAllByRole("columnheader").map((head) => head.textContent)).toEqual(["Name", "Hospital ID", "Skill", "Kind", "State", "Expiry"])
      expect(within(expiries()).getByRole("cell", { name: "31 Oct 2026" })).toBeInTheDocument()
      // Above the list, which can be sixty nurses long, so it is seen before a solve.
      const nurseRow = screen.getByRole("cell", { name: "Female" })
      expect(expiries().compareDocumentPosition(nurseRow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    })

    it("says when nothing in his unit waits", async () => {
      start("/nurses", asManagerHere([AMAL]))

      expect(await screen.findByText(/Nothing to list: no skill has expired or expires within 90 days/)).toBeInTheDocument()
    })

    it("is not on the CNO's Nurses page, which has a page of every unit's", async () => {
      const { calls } = start("/nurses", asCnoHere())
      await screen.findByText("Amal Saleh")

      expect(screen.queryByRole("heading", { level: 2, name: "Expiries" })).not.toBeInTheDocument()
      expect(pageReads(calls)).not.toContain("/api/nurses/expiries")
    })
  })

  it("narrows the list to one unit", async () => {
    const { user, router } = start("/nurses", asCnoHere())
    await screen.findByText("Amal Saleh")

    await user.selectOptions(screen.getByLabelText("Show"), "ICU")

    expect(screen.queryByText("Amal Saleh")).not.toBeInTheDocument()
    expect(screen.getByText("Badr Omar")).toBeInTheDocument()
    await waitFor(() => expect(router.state.location.search).toEqual({ unit: "u2" }))
  })

  it("keeps the unit it narrows to in the address, and counts a nurse due to join", async () => {
    start("/nurses?unit=u2", asCnoHere({}, [{ ...AMAL, transfers: [{ from_month: "2027-01", unit: { id: "u2", name: "ICU" } }] }, MONA_NURSE]))

    expect(await screen.findByText("Amal Saleh")).toBeInTheDocument()
    expect(screen.getByLabelText("Show")).toHaveValue("u2")
    expect(screen.queryByText("Mona Ali")).not.toBeInTheDocument()
  })

  it("does not take a unit in the address that is not there", async () => {
    start("/nurses?unit=gone", asCnoHere())

    expect(await screen.findByText("Amal Saleh")).toBeInTheDocument()
    expect(screen.getByText("Badr Omar")).toBeInTheDocument()
    expect(screen.getByLabelText("Show")).toHaveValue("")
  })

  it("says so when a unit it narrows to has nobody this month", async () => {
    const { user } = start("/nurses", asCnoHere({}, [AMAL]))
    await screen.findByText("Amal Saleh")

    await user.selectOptions(screen.getByLabelText("Show"), "ICU")

    expect(screen.getByText("No nurse is in this unit or due to join it.")).toBeInTheDocument()
    expect(screen.queryByRole("table")).not.toBeInTheDocument()
  })

  it("says so when the nurses cannot be read", async () => {
    start("/nurses", asCnoHere({ "GET /api/nurses": { status: 500 } }))

    expect(await screen.findByRole("alert")).toHaveTextContent(/could not be read/)
  })

  it("says so when there are no nurses yet", async () => {
    start("/nurses", asCnoHere({}, []))

    expect(await screen.findByText(/There are no nurses yet/)).toBeInTheDocument()
  })

  it("adds a nurse, shows the password once and then forgets it", async () => {
    const { user, calls } = start("/nurses", asCnoHere({
      "POST /api/nurses": { status: 201, body: { nurse: AMAL, generated_password: "abcd-efgh-jkmn-pqrs" } },
    }))

    const opener = await screen.findByRole("button", { name: "Add a nurse" })
    await user.click(opener)
    await waitFor(() => expect(screen.getByLabelText("Hospital ID")).toHaveFocus())
    await user.type(screen.getByLabelText("Hospital ID"), "h123")
    await user.type(screen.getByLabelText("Full name"), "Amal Saleh")
    await user.selectOptions(screen.getByLabelText("Gender"), "Female")
    expect(within(screen.getByLabelText("Unit")).queryByRole("option", { name: "Old" })).not.toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText("Unit"), "ER")
    await user.type(screen.getByLabelText("Start date"), "2026-01-10")
    await user.click(screen.getByRole("button", { name: "Add nurse" }))

    const dialog = await screen.findByRole("alertdialog", { name: "Password for h123" })
    expect(within(dialog).getByLabelText("Generated password")).toHaveValue("abcd-efgh-jkmn-pqrs")
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({
      hospital_id: "h123", full_name: "Amal Saleh", gender: "F", unit_id: "u1", start_date: "2026-01-10", on_probation: false,
    })

    await user.click(within(dialog).getByRole("button", { name: "Done" }))

    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument()
    expect(screen.queryByDisplayValue("abcd-efgh-jkmn-pqrs")).not.toBeInTheDocument()
    expect(screen.queryByLabelText("Hospital ID")).not.toBeInTheDocument()
  })

  describe("probation", () => {
    const PROBATION = { last_day: "2027-03-31", months: 3, ticked: true, ended_early: false }

    it("is ticked when adding a nurse, and the list says until when", async () => {
      const layan = { ...AMAL, full_name: "Layan Alharbi", on_probation: true, probation: PROBATION }
      const nurses = { body: [AMAL, BADR] as unknown[] }
      const { user, calls } = start("/nurses", asCnoHere({
        "GET /api/nurses": nurses,
        "POST /api/nurses": () => { nurses.body = [layan, BADR]; return { status: 201, body: { nurse: layan, generated_password: "abcd-efgh-jkmn-pqrs" } } },
      }))

      await user.click(await screen.findByRole("button", { name: "Add a nurse" }))
      await user.type(screen.getByLabelText("Hospital ID"), "h123")
      await user.type(screen.getByLabelText("Full name"), "Layan Alharbi")
      await user.selectOptions(screen.getByLabelText("Gender"), "Female")
      await user.selectOptions(screen.getByLabelText("Unit"), "ER")
      await user.type(screen.getByLabelText("Start date"), "2026-12-15")
      expect(screen.getByRole("checkbox", { name: "On probation" })).not.toBeChecked()
      expect(screen.getByText(/her unit manager sets how long it lasts/)).toBeInTheDocument()
      await user.click(screen.getByRole("checkbox", { name: "On probation" }))
      await user.click(screen.getByRole("button", { name: "Add nurse" }))

      const dialog = await screen.findByRole("alertdialog", { name: "Password for h123" })
      expect(calls.find((call) => call.method === "POST")?.body).toMatchObject({ start_date: "2026-12-15", on_probation: true })
      await user.click(within(dialog).getByRole("button", { name: "Done" }))
      const row = within((await screen.findByRole("cell", { name: "Layan Alharbi" })).closest("tr")!)
      expect(row.getByText("On probation until 31 Mar 2027")).toBeInTheDocument()
      expect(within(screen.getByRole("cell", { name: "Badr Omar" }).closest("tr")!).queryByText(/On probation/)).not.toBeInTheDocument()
    })

    it("is corrected on a staff nurse's details, sent only when it changes", async () => {
      // Run out by itself: the tick reads as stored, so it can still be taken off.
      const layan = { ...AMAL, on_probation: true, probation: null }
      const { user, calls } = start("/nurses", asCnoHere({
        "PUT /api/nurses/n1/details": { body: { nurse: { ...layan, on_probation: false }, first_placement_extended: null } },
      }, [layan, BADR]))

      await user.click(await screen.findByRole("button", { name: "Correct details of Amal Saleh" }))
      const tick = screen.getByRole("checkbox", { name: "On probation" })
      expect(tick).toBeChecked()
      expect(screen.getByText(/from the first month whose roster is not approved yet/)).toBeInTheDocument()
      await user.click(tick)
      await user.click(screen.getByRole("button", { name: "Save details" }))

      expect(await screen.findByText("The details of Amal Saleh are corrected.")).toBeInTheDocument()
      expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ full_name: "Amal Saleh", gender: "F", start_date: "2026-01-10", on_probation: false })
    })

    it("shows the refusal of a tick beside it", async () => {
      const { user, calls } = start("/nurses", asCnoHere({
        "PUT /api/nurses/n1/details": refusal(["on_probation"], "Value error, Amal Saleh started on 10 January 2026, more than six months ago, so she can no longer be put on probation."),
      }))

      await user.click(await screen.findByRole("button", { name: "Correct details of Amal Saleh" }))
      await user.click(screen.getByRole("checkbox", { name: "On probation" }))
      await user.click(screen.getByRole("button", { name: "Save details" }))

      expect(await screen.findByText(/more than six months ago, so she can no longer be put on probation/)).toBeInTheDocument()
      await waitFor(() => expect(screen.getByRole("checkbox", { name: "On probation" })).toHaveFocus())
      expect(calls.find((call) => call.method === "PUT")?.body).toMatchObject({ on_probation: true })
    })

    it("is not on a unit manager's details, and a save of the rest leaves it out", async () => {
      const { user, calls } = start("/nurses", asCnoHere({
        "PUT /api/nurses/n3/details": { body: { nurse: { ...MONA_NURSE, gender: "M" }, first_placement_extended: null } },
      }, [MONA_NURSE]))

      await user.click(await screen.findByRole("button", { name: "Correct details of Mona Ali" }))
      expect(screen.queryByRole("checkbox", { name: "On probation" })).not.toBeInTheDocument()
      await user.selectOptions(screen.getByLabelText("Gender"), "Male")
      await user.click(screen.getByRole("button", { name: "Save details" }))

      expect(await screen.findByText("The details of Mona Ali are corrected.")).toBeInTheDocument()
      expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ gender: "M", start_date: "2026-01-10" })
    })
  })

  describe("while a new nurse's password is on its way", () => {
    const CREATED = { status: 201, body: { nurse: AMAL, generated_password: "abcd-efgh-jkmn-pqrs" } }

    async function sendNewNurse(extra = {}) {
      const started = start("/nurses", asCnoHere(extra))
      const { user } = started
      await user.click(await screen.findByRole("button", { name: "Add a nurse" }))
      const creating = hold("POST /api/nurses")
      await user.type(screen.getByLabelText(/Hospital ID/), "h123")
      await user.type(screen.getByLabelText("Full name"), "Amal Saleh")
      await user.selectOptions(screen.getByLabelText("Gender"), "F")
      await user.selectOptions(screen.getByLabelText("Unit"), "ER")
      await user.type(screen.getByLabelText("Start date"), "2026-01-10")
      await user.click(screen.getByRole("button", { name: "Add nurse" }))
      await waitFor(() => expect(screen.getByRole("button", { name: "Add nurse" })).toHaveAttribute("aria-disabled", "true"))
      return { ...started, creating }
    }
    const passwordShown = async () => expect(within(await screen.findByRole("alertdialog", { name: "Password for h123" })).getByLabelText("Generated password")).toHaveValue("abcd-efgh-jkmn-pqrs")

    it.each([
      "Cancel",
      "Give a unit manager a nurse profile",
      "Correct details of Amal Saleh",
      "Transfer Amal Saleh",
      "Set end date of Amal Saleh",
      "Add a nurse",
      "Reset password of Amal Saleh",
    ])("%s cannot take the form away, or start another password", async (name) => {
      const { user, creating } = await sendNewNurse()

      expect(screen.getByRole("button", { name })).toHaveAttribute("aria-disabled", "true")
      await user.click(screen.getByRole("button", { name }))
      expect(screen.getByRole("button", { name: "Add nurse" })).toBeInTheDocument()

      await creating.answer(CREATED)
      await passwordShown()
    })

    it("asks before leaving, and shows the password to one who stays", async () => {
      const { user, router, creating } = await sendNewNurse()

      await user.click(screen.getByRole("link", { name: "Home" }))

      const question = await screen.findByRole("alertdialog", { name: LEAVE_QUESTION })
      expect(router.state.location.pathname).toBe("/nurses")
      await user.click(within(question).getByRole("button", { name: "Stay" }))
      expect(router.state.location.pathname).toBe("/nurses")
      await creating.answer(CREATED)
      await passwordShown()
    })

    it("drops the question when the answer comes while it is asked, so the password is not left behind", async () => {
      const { user, router, creating } = await sendNewNurse()
      await user.click(screen.getByRole("link", { name: "Home" }))
      await screen.findByRole("alertdialog", { name: LEAVE_QUESTION })

      await creating.answer(CREATED)

      await passwordShown()
      await waitFor(() => expect(screen.queryByRole("alertdialog", { name: LEAVE_QUESTION })).not.toBeInTheDocument())
      expect(router.state.location.pathname).toBe("/nurses")
    })

    it("leaves when told to, whatever the answer then says", async () => {
      const { user, router, creating } = await sendNewNurse({ "GET /api/nurses/my-unit": { body: { id: "u1", name: "ER" } } })
      await user.click(screen.getByRole("link", { name: "Home" }))

      await user.click(within(await screen.findByRole("alertdialog", { name: LEAVE_QUESTION })).getByRole("button", { name: "Leave" }))

      await waitFor(() => expect(router.state.location.pathname).toBe("/"))
      // Nothing waits for it now: neither the header, nor this page on the way back.
      expect(screen.getByRole("button", { name: "Log out" })).toHaveAttribute("aria-disabled", "false")
      await user.click(within(screen.getByRole("navigation", { name: "Main" })).getByRole("link", { name: "Nurses" }))
      expect(await screen.findByRole("button", { name: "Add a nurse" })).toHaveAttribute("aria-disabled", "false")
      await user.click(screen.getByRole("link", { name: "Home" }))
      await waitFor(() => expect(router.state.location.pathname).toBe("/"))
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument()

      await creating.answer(CREATED)
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument()
    })

    it("follows a link straight after Leave", async () => {
      const { user, router } = await sendNewNurse({ "GET /api/nurses/my-unit": { body: { id: "u1", name: "ER" } } })
      await user.click(screen.getByRole("link", { name: "Home" }))

      await leaveThen(router, await screen.findByRole("alertdialog", { name: LEAVE_QUESTION }), follow("Change password"))

      await onChangePassword(router)
    })

    it("still guards the page when a link straight after Leave comes back to it", async () => {
      const { user, router, creating } = await sendNewNurse({ "GET /api/nurses/my-unit": { body: { id: "u1", name: "ER" } } })
      await user.click(screen.getByRole("link", { name: "Home" }))

      await leaveThen(router, await screen.findByRole("alertdialog", { name: LEAVE_QUESTION }), follow("Nurses"))
      await waitFor(() => expect(router.state.status).toBe("idle"))
      expect(router.state.location.pathname).toBe("/nurses")
      await creating.answer({ status: 422, body: { detail: "No." } })
      await user.click(await screen.findByRole("button", { name: "Add nurse" }))
      await waitFor(() => expect(screen.getByRole("button", { name: "Log out" })).toHaveAttribute("aria-disabled", "true"))

      await user.click(screen.getByRole("link", { name: "Home" }))
      expect(await screen.findByRole("alertdialog", { name: LEAVE_QUESTION })).toBeInTheDocument()
      expect(router.state.location.pathname).toBe("/nurses")
    })

    it("narrowing the list asks nothing, because the form stays", async () => {
      const { user, router, creating } = await sendNewNurse()

      await user.selectOptions(screen.getByLabelText("Show"), "ICU")

      await waitFor(() => expect(router.state.location.search).toEqual({ unit: "u2" }))
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument()
      await creating.answer(CREATED)
      await passwordShown()
    })

    it("cannot log out from under it", async () => {
      const { user, calls, creating } = await sendNewNurse()

      expect(screen.getByRole("button", { name: "Log out" })).toHaveAttribute("aria-disabled", "true")
      await user.click(screen.getByRole("button", { name: "Log out" }))
      expect(calls.some((call) => call.url === "/api/auth/logout")).toBe(false)

      await creating.answer(CREATED)
      await passwordShown()
    })

    it("lets go when the nurse is refused, keeping the form and what it says", async () => {
      const { user, creating } = await sendNewNurse()

      await creating.answer(refusal(["hospital_id"], "h123 is already in use."))

      expect(await screen.findByText("h123 is already in use.")).toBeInTheDocument()
      expect(screen.getByRole("button", { name: "Cancel" })).toHaveAttribute("aria-disabled", "false")
      expect(screen.getByRole("button", { name: "Transfer Amal Saleh" })).toHaveAttribute("aria-disabled", "false")
      await user.click(screen.getByRole("link", { name: "Home" }))
      await waitFor(() => expect(screen.queryByRole("button", { name: "Add nurse" })).not.toBeInTheDocument())
    })

    it("returns focus to the button that opened the form once the password is done with", async () => {
      const { user, creating } = await sendNewNurse()

      await creating.answer(CREATED)
      await passwordShown()
      await user.click(screen.getByRole("button", { name: "Done" }))

      await waitFor(() => expect(screen.getByRole("button", { name: "Add a nurse" })).toHaveFocus())
    })
  })

  it("a reset refuses to close, and asks before leaving", async () => {
    const { user, router } = start("/nurses", asCnoHere())
    await user.click(await screen.findByRole("button", { name: "Reset password of Amal Saleh" }))
    const resetting = hold("POST /api/nurses/n1/reset-password")
    const dialog = await screen.findByRole("alertdialog", { name: /Reset the password/ })
    await user.click(within(dialog).getByRole("button", { name: "Reset password" }))

    await user.click(within(dialog).getByRole("button", { name: "Cancel" }))
    await user.keyboard("{Escape}")
    expect(screen.getByRole("alertdialog", { name: /Reset the password/ })).toBeInTheDocument()

    act(() => void router.navigate({ to: "/" }))
    await user.click(within(await screen.findByRole("alertdialog", { name: LEAVE_QUESTION })).getByRole("button", { name: "Stay" }))
    expect(router.state.location.pathname).toBe("/nurses")

    await resetting.answer({ body: { nurse: AMAL, generated_password: "wxyz-wxyz-wxyz-wxyz" } })
    expect(within(await screen.findByRole("alertdialog", { name: "Password for h123" })).getByLabelText("Generated password")).toHaveValue("wxyz-wxyz-wxyz-wxyz")
  })

  it("cancelling means nothing once a save is on its way, so it waits", async () => {
    const { user } = start("/nurses", asCnoHere())
    await user.click(await screen.findByRole("button", { name: "Transfer Amal Saleh" }))
    const transferring = hold("POST /api/nurses/n1/transfer")
    await user.selectOptions(screen.getByLabelText("To unit"), "ICU")
    await user.selectOptions(screen.getByLabelText("Effective from"), screen.getAllByRole("option").find((option) => /\d{4}$/.test(option.textContent ?? "")) as HTMLOptionElement)
    await user.click(screen.getByRole("button", { name: "Transfer" }))

    await waitFor(() => expect(screen.getByRole("button", { name: "Cancel" })).toHaveAttribute("aria-disabled", "true"))

    await transferring.answer({ body: AMAL })
    expect(await screen.findByText(/transfers to ICU/)).toBeInTheDocument()
  })

  it("asks for what is missing before sending anything", async () => {
    const { user, calls } = start("/nurses", asCnoHere())

    await user.click(await screen.findByRole("button", { name: "Add a nurse" }))
    await user.click(screen.getByRole("button", { name: "Add nurse" }))

    expect(await screen.findByText("Enter their hospital ID.")).toBeInTheDocument()
    expect(screen.getByText("Choose a unit.")).toBeInTheDocument()
    expect(calls.some((call) => call.method === "POST")).toBe(false)
  })

  it("puts a taken hospital ID beside its field with focus on it", async () => {
    const { user } = start("/nurses", asCnoHere({
      "POST /api/nurses": refusal(["hospital_id"], "h123 is already in use."),
    }))

    await user.click(await screen.findByRole("button", { name: "Add a nurse" }))
    await user.type(screen.getByLabelText("Hospital ID"), "h123")
    await user.type(screen.getByLabelText("Full name"), "Amal Saleh")
    await user.selectOptions(screen.getByLabelText("Gender"), "Male")
    await user.selectOptions(screen.getByLabelText("Unit"), "ICU")
    await user.type(screen.getByLabelText("Start date"), "2026-01-10")
    await user.click(screen.getByRole("button", { name: "Add nurse" }))

    expect(await screen.findByText("h123 is already in use.")).toBeInTheDocument()
    expect(screen.getByLabelText("Hospital ID")).toHaveFocus()
  })

  it("gives a unit manager a nurse profile, starting from the unit they hold, with no password", async () => {
    const { user, calls } = start("/nurses", asCnoHere({
      "POST /api/nurses/managers": { status: 201, body: MONA_NURSE },
    }))

    await user.click(await screen.findByRole("button", { name: "Give a unit manager a nurse profile" }))
    await user.selectOptions(await screen.findByLabelText("Unit manager"), "Mona Ali (mona)")
    expect(screen.getByLabelText("Unit")).toHaveValue("u1")
    await user.selectOptions(screen.getByLabelText("Gender"), "Female")
    await user.type(screen.getByLabelText("Start date"), "2026-01-10")
    await user.click(screen.getByRole("button", { name: "Give profile" }))

    expect(await screen.findByText(/Mona Ali now has a nurse profile/)).toBeInTheDocument()
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument()
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({
      user_id: "m1", gender: "F", unit_id: "u1", start_date: "2026-01-10",
    })
  })

  it("transfers from a chosen month and returns focus to the row", async () => {
    const waiting = { ...AMAL, transfers: [{ from_month: "2027-01", unit: { id: "u2", name: "ICU" } }] }
    const { user, calls } = start("/nurses", asCnoHere({ "POST /api/nurses/n1/transfer": { body: waiting } }))

    const opener = await screen.findByRole("button", { name: "Transfer Amal Saleh" })
    await user.click(opener)
    await waitFor(() => expect(screen.getByLabelText("To unit")).toHaveFocus())
    expect(within(screen.getByLabelText("To unit")).queryByRole("option", { name: "Old" })).not.toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText("To unit"), "ICU")
    const months = within(screen.getByLabelText("Effective from")).getAllByRole("option")
    await user.selectOptions(screen.getByLabelText("Effective from"), months[2])
    await user.click(screen.getByRole("button", { name: "Transfer" }))

    await waitFor(() => expect(calls.find((call) => call.method === "POST")?.body).toMatchObject({ unit_id: "u2" }))
    const body = calls.find((call) => call.method === "POST")?.body as { from_month: string }
    expect(body.from_month).toMatch(/^\d{4}-\d{2}$/)
    expect(await screen.findByText(/Amal Saleh transfers to ICU from/)).toBeInTheDocument()
    await waitFor(() => expect(opener).toHaveFocus())
  })

  it("shows a transfer waiting on the row", async () => {
    start("/nurses", asCnoHere({}, [{ ...AMAL, transfers: [{ from_month: "2027-01", unit: { id: "u2", name: "ICU" } }] }]))

    expect(await screen.findByText("ICU from January 2027")).toBeInTheDocument()
  })

  it("puts a refused transfer beside its field", async () => {
    const { user } = start("/nurses", asCnoHere({
      "POST /api/nurses/n1/transfer": refusal(["unit_id"], "Amal Saleh is already in ER from that month."),
    }))

    await user.click(await screen.findByRole("button", { name: "Transfer Amal Saleh" }))
    await user.selectOptions(screen.getByLabelText("To unit"), "ER")
    await user.selectOptions(screen.getByLabelText("Effective from"), within(screen.getByLabelText("Effective from")).getAllByRole("option")[1])
    await user.click(screen.getByRole("button", { name: "Transfer" }))

    expect(await screen.findByText(/already in ER from that month/)).toBeInTheDocument()
  })

  it("sets an end date and says what became of the account", async () => {
    const { user, calls } = start("/nurses", asCnoHere({
      "PUT /api/nurses/n1/end-date": { body: { ...AMAL, end_date: "2026-11-05", is_active: false } },
    }))

    await user.click(await screen.findByRole("button", { name: "Set end date of Amal Saleh" }))
    expect(screen.getByText(/deactivates them at once/)).toBeInTheDocument()
    await user.type(screen.getByLabelText("End date"), "2026-11-05")
    await user.click(screen.getByRole("button", { name: "Save end date" }))

    expect(await screen.findByText("Amal Saleh is deactivated.")).toBeInTheDocument()
    expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ end_date: "2026-11-05" })
  })

  it("says a later end date leaves them active until then", async () => {
    const { user } = start("/nurses", asCnoHere({
      "PUT /api/nurses/n1/end-date": { body: { ...AMAL, end_date: "2076-11-05" } },
    }))

    await user.click(await screen.findByRole("button", { name: "Set end date of Amal Saleh" }))
    await user.type(screen.getByLabelText("End date"), "2076-11-05")
    await user.click(screen.getByRole("button", { name: "Save end date" }))

    expect(await screen.findByText(/stays active until 5 Nov 2076/)).toBeInTheDocument()
  })

  it("says a manager's profile ends with their account as it was", async () => {
    const { user } = start("/nurses", asCnoHere({
      "PUT /api/nurses/n3/end-date": { body: { ...MONA_NURSE, end_date: "2026-01-20" } },
    }, [MONA_NURSE]))

    await user.click(await screen.findByRole("button", { name: "Set end date of Mona Ali" }))
    expect(screen.getByText(/Their account stays as it is/)).toBeInTheDocument()
    await user.type(screen.getByLabelText("End date"), "2026-01-20")
    await user.click(screen.getByRole("button", { name: "Save end date" }))

    expect(await screen.findByText("The end date of Mona Ali is 20 Jan 2026. Their account stays active.")).toBeInTheDocument()
  })

  it("clears an end date, and a refusal stays on the field", async () => {
    const ended = { ...AMAL, end_date: "2026-11-05", is_active: false }
    const { user, calls } = start("/nurses", asCnoHere({
      "PUT /api/nurses/n1/end-date": refusal(["end_date"], "ER is archived. Transfer Amal Saleh first."),
    }, [ended]))

    expect(await screen.findByText("Deactivated")).toBeInTheDocument()
    expect(screen.getByText("End date 5 Nov 2026")).toBeInTheDocument()
    await user.click(screen.getByRole("button", { name: "Change end date of Amal Saleh" }))
    expect(screen.getByLabelText("End date")).toHaveValue("2026-11-05")
    await user.click(screen.getByRole("button", { name: "Clear end date" }))

    expect(await screen.findByText(/ER is archived/)).toBeInTheDocument()
    expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ end_date: null })
  })

  it("opens with what the nurse has, corrects all three and says so, with focus back on the row", async () => {
    const { user, calls } = start("/nurses", asCnoHere({
      "PUT /api/nurses/n1/details": { body: { nurse: { ...AMAL, full_name: "Amal Salih", gender: "M", start_date: "2026-02-01" }, first_placement_extended: null } },
    }))

    const opener = await screen.findByRole("button", { name: "Correct details of Amal Saleh" })
    await user.click(opener)
    expect(screen.getByRole("heading", { name: "Correct the details of Amal Saleh" })).toBeInTheDocument()
    expect(screen.getByLabelText("Full name")).toHaveFocus()
    expect(screen.getByLabelText("Full name")).toHaveValue("Amal Saleh")
    expect(screen.getByLabelText("Gender")).toHaveValue("F")
    expect(screen.getByLabelText("Start date")).toHaveValue("2026-01-10")
    expect(screen.getByText(/cannot be corrected, because it is their username/)).toHaveTextContent("Hospital ID h123")
    expect(screen.queryByRole("textbox", { name: /Hospital ID/ })).not.toBeInTheDocument()

    await user.clear(screen.getByLabelText("Full name"))
    await user.type(screen.getByLabelText("Full name"), "  Amal Salih ")
    await user.selectOptions(screen.getByLabelText("Gender"), "Male")
    await user.clear(screen.getByLabelText("Start date"))
    await user.type(screen.getByLabelText("Start date"), "2026-02-01")
    await user.click(screen.getByRole("button", { name: "Save details" }))

    expect(await screen.findByText("The details of Amal Salih are corrected.")).toBeInTheDocument()
    expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ full_name: "Amal Salih", gender: "M", start_date: "2026-02-01" })
    expect(screen.queryByRole("heading", { name: /Correct the details/ })).not.toBeInTheDocument()
    await waitFor(() => expect(opener).toHaveFocus())
  })

  it("keeps focus on the row when a new name moves it down the list", async () => {
    const renamed = { ...AMAL, full_name: "Zaina Saleh" }
    const nurses = { body: [AMAL, BADR] }
    const { user } = start("/nurses", asCnoHere({
      "GET /api/nurses": nurses,
      "PUT /api/nurses/n1/details": () => { nurses.body = [BADR, renamed]; return { body: { nurse: renamed, first_placement_extended: null } } },
    }))

    const opener = await screen.findByRole("button", { name: "Correct details of Amal Saleh" })
    await user.click(opener)
    await user.clear(screen.getByLabelText("Full name"))
    await user.type(screen.getByLabelText("Full name"), "Zaina Saleh")
    await user.click(screen.getByRole("button", { name: "Save details" }))

    const moved = await screen.findByRole("button", { name: "Correct details of Zaina Saleh" })
    await waitFor(() => expect(moved).toHaveFocus())
  })

  it("says when a start date began their time in a unit earlier", async () => {
    const { user } = start("/nurses", asCnoHere({
      "PUT /api/nurses/n1/details": { body: { nurse: { ...AMAL, start_date: "2025-10-05" }, first_placement_extended: { unit: { id: "u1", name: "ER" }, from_month: "2025-10" } } },
    }))

    await user.click(await screen.findByRole("button", { name: "Correct details of Amal Saleh" }))
    expect(screen.getByText(/before their first month in a unit/)).toBeInTheDocument()
    await user.clear(screen.getByLabelText("Start date"))
    await user.type(screen.getByLabelText("Start date"), "2025-10-05")
    await user.click(screen.getByRole("button", { name: "Save details" }))

    expect(await screen.findByText("The details of Amal Saleh are corrected. Their time in ER now begins in October 2025.")).toBeInTheDocument()
  })

  it("says when a save changed nothing", async () => {
    const { user } = start("/nurses", asCnoHere({
      "PUT /api/nurses/n1/details": { body: { nurse: AMAL, first_placement_extended: null } },
    }))

    await user.click(await screen.findByRole("button", { name: "Correct details of Amal Saleh" }))
    await user.click(screen.getByRole("button", { name: "Save details" }))

    expect(await screen.findByText("Nothing changed for Amal Saleh.")).toBeInTheDocument()
  })

  it("asks for what is empty, without calling the API", async () => {
    const { user, calls } = start("/nurses", asCnoHere())

    await user.click(await screen.findByRole("button", { name: "Correct details of Amal Saleh" }))
    await user.clear(screen.getByLabelText("Full name"))
    await user.clear(screen.getByLabelText("Start date"))
    await user.click(screen.getByRole("button", { name: "Save details" }))

    expect(await screen.findByText("Enter their full name.")).toBeInTheDocument()
    expect(screen.getByText("Enter the start date.")).toBeInTheDocument()
    expect(screen.getByLabelText("Full name")).toHaveFocus()
    expect(calls.some((call) => call.method === "PUT")).toBe(false)
  })

  it("puts a refused start date beside its field, with focus on it, and stays open", async () => {
    const { user } = start("/nurses", asCnoHere({
      "PUT /api/nurses/n1/details": refusal(["start_date"], "The start date cannot be after the end date. Change the end date first."),
    }))

    await user.click(await screen.findByRole("button", { name: "Correct details of Amal Saleh" }))
    await user.clear(screen.getByLabelText("Start date"))
    await user.type(screen.getByLabelText("Start date"), "2027-01-01")
    await user.click(screen.getByRole("button", { name: "Save details" }))

    expect(await screen.findByText(/cannot be after the end date/)).toBeInTheDocument()
    expect(screen.getByLabelText("Start date")).toHaveFocus()
    expect(screen.getByLabelText("Start date")).toHaveAttribute("aria-invalid", "true")
  })

  it("says plainly when a correction could not be saved", async () => {
    const { user } = start("/nurses", asCnoHere({ "PUT /api/nurses/n1/details": { status: 500, body: {} } }))

    await user.click(await screen.findByRole("button", { name: "Correct details of Amal Saleh" }))
    await user.click(screen.getByRole("button", { name: "Save details" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("That did not work")
  })

  it("keeps a manager's name as it is, because it is on their account, and starts from their gender", async () => {
    const { user, calls } = start("/nurses", asCnoHere({
      "PUT /api/nurses/n3/details": { body: { nurse: { ...MONA_NURSE, gender: "M" }, first_placement_extended: null } },
    }, [MONA_NURSE]))

    await user.click(await screen.findByRole("button", { name: "Correct details of Mona Ali" }))
    expect(screen.getByLabelText("Full name")).toHaveAttribute("readonly")
    expect(screen.getByLabelText("Full name")).toHaveAccessibleDescription(/The System Admin can correct it/)
    expect(screen.getByLabelText("Gender")).toHaveFocus()
    await user.selectOptions(screen.getByLabelText("Gender"), "Male")
    await user.click(screen.getByRole("button", { name: "Save details" }))

    expect(await screen.findByText("The details of Mona Ali are corrected.")).toBeInTheDocument()
    expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ gender: "M", start_date: MONA_NURSE.start_date })
  })

  it("offers a manager no correction", async () => {
    start("/nurses", asManagerHere([AMAL]))

    expect(await screen.findByRole("button", { name: "Reset password of Amal Saleh" })).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /Correct details/ })).not.toBeInTheDocument()
  })

  it("asks before resetting a password, then shows the new one once", async () => {
    const { user, calls } = start("/nurses", asCnoHere({
      "POST /api/nurses/n1/reset-password": { body: { nurse: AMAL, generated_password: "wxyz-wxyz-wxyz-wxyz" } },
    }))

    await user.click(await screen.findByRole("button", { name: "Reset password of Amal Saleh" }))
    const question = screen.getByRole("alertdialog", { name: "Reset the password of h123?" })
    expect(calls.some((call) => call.method === "POST")).toBe(false)
    await user.click(within(question).getByRole("button", { name: "Reset password" }))

    const dialog = await screen.findByRole("alertdialog", { name: "Password for h123" })
    expect(within(dialog).getByLabelText("Generated password")).toHaveValue("wxyz-wxyz-wxyz-wxyz")
  })

  it("offers no reset for a manager, whose account is the System Admin's", async () => {
    start("/nurses", asCnoHere({}, [MONA_NURSE]))

    expect(await screen.findByText("Unit Manager")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Reset password of Mona Ali" })).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Transfer Mona Ali" })).toBeInTheDocument()
  })

  it("gives a manager their unit's list with resets only, and nothing the CNO changes", async () => {
    const { calls } = start("/nurses", asManagerHere([AMAL]))

    expect(await screen.findByRole("heading", { level: 1, name: "Nurses of ER" })).toBeInTheDocument()
    expect(await screen.findByRole("button", { name: "Reset password of Amal Saleh" })).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Add a nurse" })).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /Transfer/ })).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /end date/ })).not.toBeInTheDocument()
    expect(screen.queryByRole("columnheader", { name: "Unit" })).not.toBeInTheDocument()
    expect(calls.some((call) => call.url === "/api/nurses/managers")).toBe(false)
  })

  it("tells a manager who is joining and who is leaving, and offers no reset for one not yet theirs", async () => {
    start("/nurses", asManagerHere([
      { ...AMAL, unit: null, transfers: [{ from_month: "2027-01", unit: { id: "u1", name: "ER" } }] },
      { ...BADR, unit: { id: "u1", name: "ER" }, transfers: [{ from_month: "2027-02", unit: null }] },
    ]))

    expect(await screen.findByText("Joins from January 2027")).toBeInTheDocument()
    expect(screen.getByText("Leaves from February 2027")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Reset password of Amal Saleh" })).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Reset password of Badr Omar" })).toBeInTheDocument()
  })

  it("says nothing to a manager of transfers that do not touch their unit", async () => {
    start("/nurses", asManagerHere([{
      ...AMAL,
      unit: null,
      transfers: [{ from_month: "2027-01", unit: null }, { from_month: "2027-03", unit: { id: "u1", name: "ER" } }],
    }]))

    expect(await screen.findByText("Joins from March 2027")).toBeInTheDocument()
    expect(screen.queryByText(/Leaves from/)).not.toBeInTheDocument()
  })

  it("lets a keyboard scroll a manager's table when nothing in it can take focus", async () => {
    start("/nurses", asManagerHere([MONA_NURSE]))

    expect(await screen.findByRole("region", { name: "Nurses of ER" })).toHaveAttribute("tabindex", "0")
  })

  it("says so to a manager with no unit, or with no nurses yet", async () => {
    start("/nurses", asManagerHere([], []))

    expect(await screen.findByText(/No unit is assigned to you yet/)).toBeInTheDocument()
  })

  it("says so to a manager whose unit has no nurses yet", async () => {
    start("/nurses", asManagerHere([]))

    expect(await screen.findByText(/ER has no nurses yet/)).toBeInTheDocument()
  })

  describe("a nurse's skills", () => {
    const CATALOGUE = [
      { id: "s3", name: "ACLS", kind: "skill", issuing_body: "", expires: true, required_to_work: false, lets_a_nurse_precept: false, active: false, renames: [] },
      { id: "s1", name: "BLS", kind: "skill", issuing_body: "", expires: true, required_to_work: true, lets_a_nurse_precept: false, active: true, renames: [] },
      { id: "s2", name: "ICU", kind: "skill", issuing_body: "", expires: false, required_to_work: false, lets_a_nurse_precept: false, active: true, renames: [] },
    ]
    /** The list as the API reads it after each write, so the page is seen to take the fresh one. */
    function withSkills(first: unknown[], extra: Record<string, Handler | Answer> = {}) {
      let skills = first
      const after = (next: unknown[], answer: Answer = {}) => () => { skills = next; return { body: next, ...answer } }
      return {
        handlers: asCnoHere({
          "GET /api/nurses": () => ({ body: [{ ...AMAL, skills }, BADR] }),
          "GET /api/hospital/skills": { body: CATALOGUE },
          ...extra,
        }),
        after,
      }
    }
    const openSkills = async (user: ReturnType<typeof start>["user"]) => {
      await user.click(await screen.findByRole("button", { name: "Skills of Amal Saleh" }))
      return screen.findByRole("heading", { level: 2, name: "Skills of Amal Saleh" })
    }
    const status = () => screen.getAllByRole("status").find((region) => region.textContent)

    it("shows each nurse's skills on their row, with the last day each counts", async () => {
      start("/nurses", asCnoHere({}, [{ ...AMAL, skills: [BLS_HELD, ICU_HELD] }, BADR]))

      const row = (await screen.findByText("Amal Saleh")).closest("tr") as HTMLElement
      expect(within(row).getByText("until 30 Jun 2027")).toBeInTheDocument()
      expect(within(row).getByText("ICU")).toBeInTheDocument()
      expect(within(screen.getByText("Badr Omar").closest("tr") as HTMLElement).getByText("None")).toBeInTheDocument()
      expect(screen.getByRole("columnheader", { name: "Skills" })).toBeInTheDocument()
    })

    it("lets a manager read them and change none", async () => {
      const { calls } = start("/nurses", asManagerHere([{ ...AMAL, skills: [{ ...BLS_HELD, active: false }] }]))

      const row = (await screen.findByText("Amal Saleh")).closest("tr") as HTMLElement
      expect(within(row).getByText("BLS")).toBeInTheDocument()
      expect(within(row).getByText("Retired")).toBeInTheDocument()
      expect(screen.queryByRole("button", { name: /Skills of/ })).not.toBeInTheDocument()
      expect(screen.getByText(/the CNO and the Deputy CNO record their skills/)).toBeInTheDocument()
      expect(calls.some((call) => call.url === "/api/hospital/skills")).toBe(false)
    })

    it("records a skill that expires with its expiry, and offers only active skills not held", async () => {
      const { handlers, after } = withSkills([ICU_HELD])
      const { user, calls } = start("/nurses", { ...handlers, "POST /api/nurses/n1/skills": after([BLS_HELD, ICU_HELD], { status: 201 }) })
      await openSkills(user)

      const skill = await screen.findByLabelText("Skill")
      expect(within(skill).getAllByRole("option").map((option) => option.textContent)).toEqual(["Choose a skill", "BLS"])
      expect(screen.queryByLabelText("Expiry")).not.toBeInTheDocument()
      await user.selectOptions(skill, "BLS")
      await user.click(screen.getByRole("button", { name: "Record skill" }))
      expect(screen.getByLabelText("Expiry")).toHaveAccessibleDescription(/BLS expires. Enter the last day it counts./)
      expect(screen.getByLabelText("Expiry")).toHaveFocus()
      expect(calls.some((call) => call.method === "POST")).toBe(false)

      await user.type(screen.getByLabelText("Expiry"), "2027-06-30")
      await user.click(screen.getByRole("button", { name: "Record skill" }))

      await waitFor(() => expect(status()).toHaveTextContent("BLS is recorded for Amal Saleh."))
      expect(calls.find((call) => call.method === "POST")?.body).toEqual({ skill_id: "s1", expiry: "2027-06-30", identifier: null, issue_date: null })
      expect(within(await screen.findByRole("table", { name: "Skills of Amal Saleh" })).getByRole("cell", { name: "30 Jun 2027" })).toBeInTheDocument()
      expect(screen.getByText("Amal Saleh holds every skill in the catalogue.")).toBeInTheDocument()
    })

    it("records a skill that does not expire with no expiry", async () => {
      const { handlers, after } = withSkills([])
      const { user, calls } = start("/nurses", { ...handlers, "POST /api/nurses/n1/skills": after([ICU_HELD], { status: 201 }) })
      await openSkills(user)
      expect(screen.getByText("No skills are recorded for Amal Saleh.")).toBeInTheDocument()

      await user.selectOptions(await screen.findByLabelText("Skill"), "ICU")
      expect(screen.queryByLabelText("Expiry")).not.toBeInTheDocument()
      await user.click(screen.getByRole("button", { name: "Record skill" }))

      await waitFor(() => expect(status()).toHaveTextContent("ICU is recorded for Amal Saleh."))
      expect(calls.find((call) => call.method === "POST")?.body).toEqual({ skill_id: "s2", expiry: null, identifier: null, issue_date: null })
      expect(await screen.findByRole("table", { name: "Skills of Amal Saleh" })).toHaveTextContent("Does not expire")
    })

    it("shows the API's refusal beside its field", async () => {
      const { handlers } = withSkills([])
      const { user } = start("/nurses", { ...handlers, "POST /api/nurses/n1/skills": refusal(["skill_id"], "ICU is retired, so it can no longer be recorded.") })
      await openSkills(user)

      await user.selectOptions(await screen.findByLabelText("Skill"), "ICU")
      await user.click(screen.getByRole("button", { name: "Record skill" }))

      await waitFor(() => expect(screen.getByLabelText("Skill")).toHaveAccessibleDescription("ICU is retired, so it can no longer be recorded."))
      expect(screen.getByLabelText("Skill")).toHaveFocus()
    })

    it("renews a skill by its expiry, and keeps the buttons still while it saves", async () => {
      const renewed = { ...BLS_HELD, expiry: "2029-06-30" }
      const { handlers } = withSkills([BLS_HELD])
      const { user, calls } = start("/nurses", handlers)
      await openSkills(user)
      const renewing = hold("PUT /api/nurses/n1/skills/s1")
      await user.click(screen.getByRole("button", { name: "Renew BLS" }))

      await user.clear(screen.getByLabelText("Expiry of BLS"))
      await user.type(screen.getByLabelText("Expiry of BLS"), "2029-06-30")
      await user.click(screen.getByRole("button", { name: "Save BLS" }))

      await waitFor(() => expect(screen.getByRole("button", { name: "Save BLS" })).toHaveAttribute("aria-disabled", "true"))
      expect(screen.getByRole("button", { name: "Cancel renewing BLS" })).toHaveAttribute("aria-disabled", "true")
      calls.length = 0
      await renewing.answer({ body: [renewed] })

      await waitFor(() => expect(status()).toHaveTextContent("BLS of Amal Saleh now counts until 30 Jun 2029."))
      await waitFor(() => expect(calls.some((call) => call.url === "/api/nurses")).toBe(true))
    })

    it("sends what the renewal form says, closes it and puts focus back on its row's button", async () => {
      const { handlers, after } = withSkills([BLS_HELD])
      const { user, calls } = start("/nurses", { ...handlers, "PUT /api/nurses/n1/skills/s1": after([{ ...BLS_HELD, expiry: "2029-06-30" }]) })
      await openSkills(user)
      await user.click(screen.getByRole("button", { name: "Renew BLS" }))
      expect(screen.getByLabelText("Expiry of BLS")).toHaveFocus()

      await user.clear(screen.getByLabelText("Expiry of BLS"))
      await user.type(screen.getByLabelText("Expiry of BLS"), "2029-06-30")
      await user.click(screen.getByRole("button", { name: "Save BLS" }))

      await waitFor(() => expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ expiry: "2029-06-30", identifier: null, issue_date: null }))
      expect(within(await screen.findByRole("table", { name: "Skills of Amal Saleh" })).findByRole("cell", { name: "30 Jun 2029" })).toBeTruthy()
      await waitFor(() => expect(screen.getByRole("button", { name: "Renew BLS" })).toHaveFocus())
      expect(screen.queryByLabelText("Expiry of BLS")).not.toBeInTheDocument()
    })

    it("asks for an expiry before renewing to none", async () => {
      const { handlers } = withSkills([BLS_HELD])
      const { user, calls } = start("/nurses", handlers)
      await openSkills(user)
      await user.click(screen.getByRole("button", { name: "Renew BLS" }))

      await user.clear(screen.getByLabelText("Expiry of BLS"))
      await user.click(screen.getByRole("button", { name: "Save BLS" }))

      expect(screen.getByLabelText("Expiry of BLS")).toHaveAccessibleDescription(/BLS expires. Enter the last day it counts./)
      expect(calls.some((call) => call.method === "PUT")).toBe(false)
    })

    it("takes a skill off, and moves focus on to recording one", async () => {
      const { handlers, after } = withSkills([BLS_HELD, ICU_HELD])
      const { user, calls } = start("/nurses", { ...handlers, "POST /api/nurses/n1/skills/s1/remove": after([ICU_HELD]) })
      await openSkills(user)

      await user.click(screen.getByRole("button", { name: "Take off BLS" }))

      await waitFor(() => expect(status()).toHaveTextContent("BLS is taken off Amal Saleh."))
      await waitFor(() => expect(screen.queryByRole("button", { name: "Take off BLS" })).not.toBeInTheDocument())
      expect(screen.getByRole("heading", { level: 3, name: "Record a skill" })).toHaveFocus()
      expect(calls.find((call) => call.url === "/api/nurses/n1/skills/s1/remove")?.method).toBe("POST")
    })

    it("says so when the page was out of date", async () => {
      const { handlers } = withSkills([ICU_HELD])
      const { user } = start("/nurses", { ...handlers,
        "POST /api/nurses/n1/skills/s2/remove": { status: 409, body: { detail: "Amal Saleh does not hold this skill now. Reload the page to see their skills as they are." } } })
      await openSkills(user)

      await user.click(screen.getByRole("button", { name: "Take off ICU" }))

      expect(await screen.findByRole("alert")).toHaveTextContent("Amal Saleh does not hold this skill now.")
    })

    it("offers a retired skill only to take off, and a skill that no longer expires no date", async () => {
      const { handlers } = withSkills([{ ...BLS_HELD, active: false }, ICU_HELD])
      const { user } = start("/nurses", handlers)
      await openSkills(user)

      const table = screen.getByRole("table", { name: "Skills of Amal Saleh" })
      expect(within(table).getByText("Retired")).toBeInTheDocument()
      expect(within(table).getByRole("cell", { name: "30 Jun 2027" })).toBeInTheDocument()
      expect(screen.getByText(/can no longer be renewed/)).toBeInTheDocument()
      expect(screen.queryByRole("button", { name: "Renew BLS" })).not.toBeInTheDocument()
      expect(screen.getByRole("button", { name: "Renew ICU" })).toBeInTheDocument()
      expect(screen.getByRole("button", { name: "Take off BLS" })).toBeInTheDocument()
      expect(screen.getByRole("button", { name: "Take off ICU" })).toBeInTheDocument()
    })

    describe("a record's identifier and issue date", () => {
      const LICENCE = { id: "s4", name: "SCFHS", kind: "licence", issuing_body: "Saudi Commission for Health Specialties", expires: true,
        required_to_work: true, lets_a_nurse_precept: false, active: true, renames: [] }
      const DEGREE = { id: "s5", name: "BSN", kind: "certificate", issuing_body: "King Saud University", expires: false,
        required_to_work: false, lets_a_nurse_precept: false, active: true, renames: [] }
      const withDetails = (first: unknown[], extra: Record<string, Handler | Answer> = {}) =>
        withSkills(first, { "GET /api/hospital/skills": { body: [...CATALOGUE, LICENCE, DEGREE] }, ...extra })

      it("lists her records under Skills, Certificates and Licences, with the identifier and the issue date", async () => {
        const { handlers } = withDetails([BLS_HELD, SCFHS_HELD, BSN_HELD])
        const { user } = start("/nurses", handlers)
        await openSkills(user)

        const table = screen.getByRole("table", { name: "Skills of Amal Saleh" })
        expect(within(table).getAllByRole("columnheader").map((head) => head.textContent)).toEqual(["Name", "Expiry", "Identifier", "Issue date", "Actions"])
        expect(within(table).getAllByRole("rowheader").map((head) => head.textContent)).toEqual(["Skills", "Certificates", "Licences"])
        expect(within(within(table).getByRole("rowgroup", { name: "Licences" })).getAllByRole("cell").map((cell) => cell.textContent).slice(0, 4))
          .toEqual(["SCFHS", "31 Mar 2028", "21-RN-0412", "1 Apr 2026"])
        expect(within(table).getByRole("rowgroup", { name: "Certificates" })).toHaveTextContent("BSNDoes not expireNone15 Jun 2019")
        expect(table.closest("[role=region]")).toBeNull()
        const row = within((await screen.findAllByText("Amal Saleh"))[0].closest("tr") as HTMLElement)
        expect(row.queryByText("21-RN-0412")).not.toBeInTheDocument()
      })

      it("marks the identifier required for a licence and asks for it before recording", async () => {
        const { handlers, after } = withDetails([])
        const { user, calls } = start("/nurses", { ...handlers, "POST /api/nurses/n1/skills": after([SCFHS_HELD], { status: 201 }) })
        await openSkills(user)

        const skill = await screen.findByLabelText("Skill")
        expect(within(skill).getAllByRole("group").map((group) => group.getAttribute("label"))).toEqual(["Skills", "Certificates", "Licences"])
        await user.selectOptions(skill, "BSN")
        expect(screen.getByLabelText("Identifier (optional)")).not.toHaveAttribute("aria-required")
        await user.selectOptions(skill, "SCFHS")
        expect(screen.getByLabelText("Identifier")).toHaveAttribute("aria-required", "true")
        expect(screen.getByLabelText("Identifier")).toHaveAccessibleDescription(/A licence needs its identifier/)
        await user.type(screen.getByLabelText("Expiry"), "2028-03-31")
        await user.type(screen.getByLabelText("Identifier"), "   ")
        await user.click(screen.getByRole("button", { name: "Record skill" }))
        expect(screen.getByLabelText("Identifier")).toHaveAccessibleDescription(/SCFHS is a licence, so it needs its identifier./)
        expect(screen.getByLabelText("Identifier")).toHaveFocus()
        expect(calls.some((call) => call.method === "POST")).toBe(false)

        await user.type(screen.getByLabelText("Identifier"), "21-RN-0412 ")
        await user.type(screen.getByLabelText("Issue date (optional)"), "2026-04-01")
        await user.click(screen.getByRole("button", { name: "Record skill" }))

        await waitFor(() => expect(status()).toHaveTextContent("SCFHS is recorded for Amal Saleh."))
        expect(calls.find((call) => call.method === "POST")?.body).toEqual(
          { skill_id: "s4", expiry: "2028-03-31", identifier: "21-RN-0412", issue_date: "2026-04-01" })
      })

      it("shows the API's refusals beside the identifier and the issue date", async () => {
        const { handlers } = withDetails([], { "POST /api/nurses/n1/skills": { status: 422, body: { detail: [
          { type: "value_error", loc: ["body", "body", "identifier"], msg: "Badr Omar already holds BSN with this identifier. Check the number: each one is one nurse's." },
          { type: "value_error", loc: ["body", "body", "issue_date"], msg: "The issue date cannot be after today." },
        ] } } })
        const { user } = start("/nurses", handlers)
        await openSkills(user)

        await user.selectOptions(await screen.findByLabelText("Skill"), "BSN")
        await user.type(screen.getByLabelText("Identifier (optional)"), "77")
        await user.type(screen.getByLabelText("Issue date (optional)"), "2030-01-01")
        await user.click(screen.getByRole("button", { name: "Record skill" }))

        await waitFor(() => expect(screen.getByLabelText("Identifier (optional)")).toHaveAccessibleDescription(/Badr Omar already holds BSN with this identifier/))
        expect(screen.getByLabelText("Issue date (optional)")).toHaveAccessibleDescription(/The issue date cannot be after today./)
      })

      it("renews a licence with a new identifier and issue date, its fields filled with what is recorded", async () => {
        const renewed = { ...SCFHS_HELD, details: { ...SCFHS_HELD.details, identifier: "26-RN-9001", issue_date: "2026-11-01" } }
        const { handlers, after } = withDetails([SCFHS_HELD])
        const { user, calls } = start("/nurses", { ...handlers, "PUT /api/nurses/n1/skills/s4": after([renewed]) })
        await openSkills(user)

        await user.click(screen.getByRole("button", { name: "Renew SCFHS" }))
        expect(screen.getByRole("button", { name: "Renew SCFHS" })).toHaveAttribute("aria-expanded", "true")
        expect(screen.getByLabelText("Expiry of SCFHS")).toHaveValue("2028-03-31")
        expect(screen.getByLabelText("Identifier of SCFHS")).toHaveValue("21-RN-0412")
        expect(screen.getByLabelText("Issue date of SCFHS (optional)")).toHaveValue("2026-04-01")
        await user.clear(screen.getByLabelText("Identifier of SCFHS"))
        await user.click(screen.getByRole("button", { name: "Save SCFHS" }))
        expect(screen.getByLabelText("Identifier of SCFHS")).toHaveAccessibleDescription(/SCFHS is a licence, so it needs its identifier./)

        await user.type(screen.getByLabelText("Identifier of SCFHS"), "26-RN-9001")
        await user.clear(screen.getByLabelText("Issue date of SCFHS (optional)"))
        await user.type(screen.getByLabelText("Issue date of SCFHS (optional)"), "2026-11-01")
        await user.click(screen.getByRole("button", { name: "Save SCFHS" }))

        await waitFor(() => expect(status()).toHaveTextContent("SCFHS of Amal Saleh is renewed."))
        expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ expiry: "2028-03-31", identifier: "26-RN-9001", issue_date: "2026-11-01" })
        expect(await within(screen.getByRole("table", { name: "Skills of Amal Saleh" })).findByText("26-RN-9001")).toBeInTheDocument()
      })

      it("corrects a certificate held for good through its renewal, which has no expiry", async () => {
        const { handlers, after } = withDetails([BSN_HELD])
        const { user, calls } = start("/nurses", { ...handlers, "PUT /api/nurses/n1/skills/s5": after([{ ...BSN_HELD, details: { ...BSN_HELD.details, identifier: "KSU-77" } }]) })
        await openSkills(user)

        await user.click(screen.getByRole("button", { name: "Renew BSN" }))
        expect(screen.queryByLabelText(/Expiry of/)).not.toBeInTheDocument()
        expect(screen.getByLabelText("Identifier of BSN (optional)")).toHaveFocus()
        await user.type(screen.getByLabelText("Identifier of BSN (optional)"), "KSU-77")
        await user.click(screen.getByRole("button", { name: "Save BSN" }))

        await waitFor(() => expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ expiry: null, identifier: "KSU-77", issue_date: "2019-06-15" }))
      })

      it("closes a renewal on Cancel and puts focus back on its button", async () => {
        const { handlers } = withDetails([SCFHS_HELD])
        const { user, calls } = start("/nurses", handlers)
        await openSkills(user)

        await user.click(screen.getByRole("button", { name: "Renew SCFHS" }))
        await user.click(screen.getByRole("button", { name: "Cancel renewing SCFHS" }))

        await waitFor(() => expect(screen.getByRole("button", { name: "Renew SCFHS" })).toHaveFocus())
        expect(screen.queryByLabelText("Identifier of SCFHS")).not.toBeInTheDocument()
        expect(calls.some((call) => call.method === "PUT")).toBe(false)
      })

      it("shows a manager names and expiries by kind, and no identifier or issue date", async () => {
        start("/nurses", asManagerHere([{ ...AMAL, skills: asManagers([SCFHS_HELD, BLS_HELD]) }]))

        const row = within((await screen.findByText("Amal Saleh")).closest("tr") as HTMLElement)
        expect(row.getByRole("list", { name: "Licences" })).toHaveTextContent("SCFHSuntil 31 Mar 2028")
        expect(row.getByRole("list", { name: "Skills" })).toHaveTextContent("BLSuntil 30 Jun 2027")
        expect(screen.queryByText("21-RN-0412")).not.toBeInTheDocument()
      })
    })

    it("says where to add skills when the catalogue is empty", async () => {
      const { user } = start("/nurses", asCnoHere({ "GET /api/hospital/skills": { body: [] } }, [{ ...AMAL, skills: [] }]))
      await openSkills(user)

      expect(await screen.findByRole("link", { name: "Add skills under Skills" })).toHaveAttribute("href", "/skills")
    })

    it("closes with Done and gives focus back to the row's button", async () => {
      const { handlers } = withSkills([])
      const { user } = start("/nurses", handlers)
      await openSkills(user)

      await user.click(screen.getByRole("button", { name: "Done" }))

      expect(screen.queryByRole("heading", { level: 2, name: "Skills of Amal Saleh" })).not.toBeInTheDocument()
      expect(screen.getByRole("button", { name: "Skills of Amal Saleh" })).toHaveFocus()
    })
  })
})

describe("importing nurses", () => {
  const CHIEF = { ...ME, username: "chief", full_name: "The Chief", role: "cno" }
  const here = (extra = {}) => ({ ...session(CHIEF), ...extra })
  const ROW = { line: 2, hospital_id: "h1", full_name: "Amal Saleh", gender: "F", unit: "ER", start_date: "2026-01-10", problems: [] }
  const ROW_2 = { ...ROW, line: 3, hospital_id: "h2", full_name: "Badr Omar", gender: "M" }
  const BAD = { ...ROW_2, gender: "X", problems: [{ column: "gender", message: "Use F or M." }, { column: null, message: "Something about the line." }] }
  const GOOD = { rows: [ROW, ROW_2], token: "the-token" }
  const SHEET = { rows: [
    { hospital_id: "h1", full_name: "Amal Saleh", unit: "ER", generated_password: "aaaa-bbbb-cccc-dddd" },
    { hospital_id: "h2", full_name: "=Badr\t=1+1", unit: "ER", generated_password: "eeee-ffff-gggg-hhhh" },
  ] }
  const csv = (content = "a file", name = "nurses.csv") => new File([content], name, { type: "text/csv" })

  async function check(user: ReturnType<typeof userEvent.setup>, file = csv()) {
    await user.upload(await screen.findByLabelText("CSV file"), file)
    await user.click(screen.getByRole("button", { name: "Check file" }))
  }

  it("offers the CNO the template", async () => {
    const { router } = start("/nurses/import", here())

    const link = await screen.findByRole("link", { name: "Download the template" })
    expect(link).toHaveAttribute("href", "/api/nurses/import/template")
    expect(link).toHaveAttribute("download")
    expect(document.title).toBe("Import nurses · Nurse roster")
    expect(router.state.location.pathname).toBe("/nurses/import")
  })

  it("sends a unit manager home", async () => {
    const { router } = start("/nurses/import", {
      ...session({ ...ME, username: "mona", role: "unit_manager" }),
      "GET /api/units": { body: [] },
    })

    await waitFor(() => expect(router.state.location.pathname).toBe("/"))
  })

  it("asks for a file, and refuses a large one before sending it", async () => {
    const { user, calls } = start("/nurses/import", here())
    await screen.findByLabelText("CSV file")

    await user.click(screen.getByRole("button", { name: "Check file" }))
    expect(await screen.findByText("Choose a CSV file.")).toBeInTheDocument()

    await check(user, csv("x".repeat(1024 * 1024 + 1)))
    expect(await screen.findByText(/larger than 1 MB/)).toBeInTheDocument()
    expect(screen.getByLabelText("CSV file")).toHaveFocus()
    expect(calls.filter((call) => call.method === "POST")).toEqual([])
  })

  it("shows a refusal of the whole file beside the field", async () => {
    const { user } = start("/nurses/import", here({
      "POST /api/nurses/import/preview": { status: 422, body: { detail: [{ type: "value_error", loc: ["body", "body", "file"], msg: "This is not a CSV file in UTF-8." }] } },
    }))

    await check(user)

    expect(await screen.findByText("This is not a CSV file in UTF-8.")).toBeInTheDocument()
    expect(screen.getByLabelText("CSV file")).toHaveFocus()
  })

  it("says so when the check itself fails", async () => {
    const { user } = start("/nurses/import", here({ "POST /api/nurses/import/preview": { status: 500 } }))

    await check(user)

    expect(await screen.findByRole("alert")).toHaveTextContent("That did not work")
  })

  it("shows only the lines with problems, each named, and offers nothing to confirm", async () => {
    const { user, calls } = start("/nurses/import", here({
      "POST /api/nurses/import/preview": { body: { rows: [ROW, BAD], token: null } },
    }))

    await check(user)

    const heading = await screen.findByRole("heading", { name: "1 of 2 lines has a problem" })
    expect(heading).toHaveFocus()
    expect(screen.getByText("Gender: Use F or M.")).toBeInTheDocument()
    expect(screen.getByText("Something about the line.")).toBeInTheDocument()
    expect(screen.getByText("Badr Omar")).toBeInTheDocument()
    expect(screen.queryByText("Amal Saleh")).not.toBeInTheDocument()
    expect(screen.getByText(/The other line has no problem/)).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /^Add/ })).not.toBeInTheDocument()
    const sent = calls.find((call) => call.method === "POST")
    expect((sent?.body as FormData).get("file")).toBeInstanceOf(Blob)
    expect(sent?.headers.has("Content-Type")).toBe(false)
  })

  it("confirms the very bytes it checked, with the preview's token, and shows the sheet with no way back to it", async () => {
    let confirmed: FormData | undefined
    const { user, calls } = start("/nurses/import", here({
      "POST /api/nurses/import/preview": { body: GOOD },
      "POST /api/nurses/import/confirm": ({ body }: { body: unknown }) => { confirmed = body as FormData; return { status: 201, body: SHEET } },
    }))

    await check(user, csv("the bytes"))
    expect(await screen.findByRole("heading", { name: "2 nurses ready to add" })).toHaveFocus()
    expect(calls.filter((call) => call.url.includes("confirm"))).toEqual([])
    await user.click(screen.getByRole("button", { name: "Add 2 nurses" }))

    expect(await screen.findByRole("heading", { name: "Passwords for 2 nurses" })).toHaveFocus()
    expect(screen.getByText("aaaa-bbbb-cccc-dddd")).toBeInTheDocument()
    expect(confirmed?.get("token")).toBe("the-token")
    expect(await (confirmed?.get("file") as Blob).text()).toBe("the bytes")

    await user.click(screen.getByRole("button", { name: "Done" }))
    const dialog = await screen.findByRole("alertdialog")
    await user.click(within(dialog).getByRole("button", { name: "Stay" }))
    expect(screen.getByText("aaaa-bbbb-cccc-dddd")).toBeInTheDocument()

    await user.click(screen.getByRole("button", { name: "Done" }))
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Close passwords" }))

    await waitFor(() => expect(screen.queryByText("aaaa-bbbb-cccc-dddd")).not.toBeInTheDocument())
    expect(screen.getByText("2 nurses added.")).toBeInTheDocument()
    expect(screen.getByRole("heading", { name: "Import nurses" })).toHaveFocus()
  })

  it("asks before leaving a sheet, and leaves only when told to", async () => {
    const { user, router } = start("/nurses/import", here({
      "POST /api/nurses/import/preview": { body: GOOD },
      "POST /api/nurses/import/confirm": { status: 201, body: SHEET },
      "GET /api/nurses": { body: [] },
      "GET /api/units": { body: [] },
    }))
    await check(user)
    await user.click(await screen.findByRole("button", { name: "Add 2 nurses" }))
    await screen.findByText("aaaa-bbbb-cccc-dddd")

    await user.click(screen.getByRole("link", { name: "Home" }))
    const dialog = await screen.findByRole("alertdialog")
    expect(within(dialog).getByText("Leave without the passwords?")).toBeInTheDocument()
    await user.click(within(dialog).getByRole("button", { name: "Stay" }))
    expect(router.state.location.pathname).toBe("/nurses/import")
    expect(screen.getByText("aaaa-bbbb-cccc-dddd")).toBeInTheDocument()

    await user.click(screen.getByRole("link", { name: "Home" }))
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Leave" }))
    await waitFor(() => expect(router.state.location.pathname).toBe("/"))
  })

  describe("straight after Leave", () => {
    async function sheetOnScreen() {
      const started = start("/nurses/import", here({
        "POST /api/nurses/import/preview": { body: GOOD },
        "POST /api/nurses/import/confirm": { status: 201, body: SHEET },
        "GET /api/units": { body: [] },
      }))
      await check(started.user)
      await started.user.click(await screen.findByRole("button", { name: "Add 2 nurses" }))
      await screen.findByText("aaaa-bbbb-cccc-dddd")
      await started.user.click(screen.getByRole("link", { name: "Home" }))
      return { ...started, question: await screen.findByRole("alertdialog") }
    }

    it("still asks about a sheet on an address with a slash at the end", async () => {
      const { user, router } = start("/nurses/import/", here({
        "POST /api/nurses/import/preview": { body: GOOD },
        "POST /api/nurses/import/confirm": { status: 201, body: SHEET },
        "GET /api/units": { body: [] },
      }))
      await check(user)
      await user.click(await screen.findByRole("button", { name: "Add 2 nurses" }))
      await screen.findByText("aaaa-bbbb-cccc-dddd")

      await user.click(screen.getByRole("link", { name: "Home" }))

      expect(within(await screen.findByRole("alertdialog")).getByText("Leave without the passwords?")).toBeInTheDocument()
      expect(router.state.location.pathname).toBe("/nurses/import/")
    })

    it("follows a link, from the sheet", async () => {
      const { router, question } = await sheetOnScreen()

      await leaveThen(router, question, follow("Change password"))

      await onChangePassword(router)
    })

    it("follows a link, while a confirmation is on its way", async () => {
      const { user, router } = start("/nurses/import", here({
        "POST /api/nurses/import/preview": { body: GOOD },
        "GET /api/units": { body: [] },
      }))
      await check(user)
      hold("POST /api/nurses/import/confirm")
      await user.click(await screen.findByRole("button", { name: "Add 2 nurses" }))
      await user.click(screen.getByRole("link", { name: "Home" }))

      await leaveThen(router, await screen.findByRole("alertdialog"), follow("Change password"))

      await onChangePassword(router)
    })

    it("still guards a sheet that going back keeps on screen", async () => {
      const { user, router, question } = await sheetOnScreen()

      await leaveThen(router, question, () => router.history.back())
      await waitFor(() => expect(router.state.status).toBe("idle"))
      expect(router.state.location.pathname).toBe("/nurses/import")
      expect(await screen.findByText("aaaa-bbbb-cccc-dddd")).toBeInTheDocument()

      await user.click(screen.getByRole("link", { name: "Home" }))
      expect(within(await screen.findByRole("alertdialog")).getByText("Leave without the passwords?")).toBeInTheDocument()
      expect(router.state.location.pathname).toBe("/nurses/import")
    })
  })

  it("copies the sheet with any cell that looks like a formula made safe", async () => {
    const { user } = start("/nurses/import", here({
      "POST /api/nurses/import/preview": { body: GOOD },
      "POST /api/nurses/import/confirm": { status: 201, body: SHEET },
    }))
    await check(user)
    await user.click(await screen.findByRole("button", { name: "Add 2 nurses" }))
    await screen.findByText("aaaa-bbbb-cccc-dddd")
    const writeText = vi.fn(async () => {})
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true })

    await user.click(screen.getByRole("button", { name: "Copy all" }))

    expect(await screen.findByText("Copied.")).toBeInTheDocument()
    expect(writeText).toHaveBeenCalledWith(
      "Hospital ID\tFull name\tUnit\tPassword\nh1\tAmal Saleh\tER\taaaa-bbbb-cccc-dddd\nh2\t'=Badr =1+1\tER\teeee-ffff-gggg-hhhh")
  })

  it("prints the sheet", async () => {
    const print = vi.fn()
    vi.stubGlobal("print", print)
    const { user } = start("/nurses/import", here({
      "POST /api/nurses/import/preview": { body: GOOD },
      "POST /api/nurses/import/confirm": { status: 201, body: SHEET },
    }))
    await check(user)
    await user.click(await screen.findByRole("button", { name: "Add 2 nurses" }))

    await user.click(await screen.findByRole("button", { name: "Print" }))

    expect(print).toHaveBeenCalled()
  })

  it("shows what changed when the file no longer means what was previewed, and saves nothing", async () => {
    const { user } = start("/nurses/import", here({
      "POST /api/nurses/import/preview": { body: GOOD },
      "POST /api/nurses/import/confirm": { status: 409, body: { rows: [ROW, BAD], token: null } },
    }))
    await check(user)

    await user.click(await screen.findByRole("button", { name: "Add 2 nurses" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("Something changed since the last check, so nothing was saved.")
    expect(await screen.findByRole("heading", { name: "1 of 2 lines has a problem" })).toHaveFocus()
    expect(screen.queryByRole("button", { name: /^Add/ })).not.toBeInTheDocument()
  })

  it("lets a changed file that is still good be confirmed with its new token", async () => {
    const tokens: unknown[] = []
    const { user } = start("/nurses/import", here({
      "POST /api/nurses/import/preview": { body: GOOD },
      "POST /api/nurses/import/confirm": ({ body }: { body: unknown }) => {
        tokens.push((body as FormData).get("token"))
        return tokens.length === 1 ? { status: 409, body: { ...GOOD, token: "new-token" } } : { status: 201, body: SHEET }
      },
    }))
    await check(user)
    await user.click(await screen.findByRole("button", { name: "Add 2 nurses" }))
    await screen.findByRole("alert")

    await user.click(screen.getByRole("button", { name: "Add 2 nurses" }))

    await screen.findByText("aaaa-bbbb-cccc-dddd")
    expect(tokens).toEqual(["the-token", "new-token"])
  })

  it("says plainly when it cannot tell whether the nurses were added", async () => {
    const { user } = start("/nurses/import", here({
      "POST /api/nurses/import/preview": { body: GOOD },
      "POST /api/nurses/import/confirm": { status: 502 },
    }))
    await check(user)

    await user.click(await screen.findByRole("button", { name: "Add 2 nurses" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("It is not known whether these nurses were added.")
  })

  it("holds back Add while another file is being checked, so an import cannot be cut off by the check's answer", async () => {
    const { user, calls } = start("/nurses/import", here({
      "POST /api/nurses/import/preview": { body: GOOD },
      "POST /api/nurses/import/confirm": { status: 201, body: SHEET },
    }))
    await check(user)
    await screen.findByRole("button", { name: "Add 2 nurses" })
    const checking = hold("POST /api/nurses/import/preview")

    await check(user, csv("another file"))

    await waitFor(() => expect(screen.getByRole("button", { name: "Add 2 nurses" })).toHaveAttribute("aria-disabled", "true"))
    await user.click(screen.getByRole("button", { name: "Add 2 nurses" }))
    expect(calls.some((call) => call.url === "/api/nurses/import/confirm")).toBe(false)

    await checking.answer({ body: GOOD })
    await waitFor(() => expect(screen.getByRole("button", { name: "Add 2 nurses" })).toHaveAttribute("aria-disabled", "false"))
    await user.click(screen.getByRole("button", { name: "Add 2 nurses" }))
    expect(await screen.findByText("aaaa-bbbb-cccc-dddd")).toBeInTheDocument()
  })

  it("holds back Add from the moment Check file is pressed, while the file is still being read", async () => {
    const { user, calls } = start("/nurses/import", here({ "POST /api/nurses/import/preview": { body: GOOD } }))
    await check(user)
    await screen.findByRole("button", { name: "Add 2 nurses" })
    const slow = csv("another file")
    let read: (bytes: ArrayBuffer) => void = () => {}
    slow.arrayBuffer = () => new Promise((resolve) => { read = resolve })

    await check(user, slow)

    await waitFor(() => expect(screen.getByRole("button", { name: "Add 2 nurses" })).toHaveAttribute("aria-disabled", "true"))
    await user.click(screen.getByRole("button", { name: "Add 2 nurses" }))
    expect(calls.some((call) => call.url === "/api/nurses/import/confirm")).toBe(false)
    expect(screen.getByRole("button", { name: "Checking…" })).toHaveAttribute("aria-disabled", "true")

    await act(async () => read(new ArrayBuffer(1)))
    await waitFor(() => expect(screen.getByRole("button", { name: "Add 2 nurses" })).toHaveAttribute("aria-disabled", "false"))
  })

  it("holds back Check file while the nurses are being added, so the check's answer cannot cut the import off", async () => {
    const { user, calls } = start("/nurses/import", here({ "POST /api/nurses/import/preview": { body: GOOD } }))
    await check(user)
    const adding = hold("POST /api/nurses/import/confirm")
    await user.click(await screen.findByRole("button", { name: "Add 2 nurses" }))
    await screen.findByRole("button", { name: "Adding…" })
    const previews = calls.filter((call) => call.url === "/api/nurses/import/preview").length

    await user.upload(screen.getByLabelText("CSV file"), csv("another file"))
    await user.click(screen.getByRole("button", { name: "Check file" }))

    expect(screen.getByRole("button", { name: "Check file" })).toHaveAttribute("aria-disabled", "true")
    expect(calls.filter((call) => call.url === "/api/nurses/import/preview")).toHaveLength(previews)
    await adding.answer({ status: 201, body: SHEET })
    expect(await screen.findByText("aaaa-bbbb-cccc-dddd")).toBeInTheDocument()
  })

  it("lets Add go again when a check fails", async () => {
    const { user } = start("/nurses/import", here({ "POST /api/nurses/import/preview": { body: GOOD } }))
    await check(user)
    await screen.findByRole("button", { name: "Add 2 nurses" })
    const checking = hold("POST /api/nurses/import/preview")

    await check(user, csv("another file"))
    await checking.answer({ status: 502 })

    expect(await screen.findByRole("alert")).toHaveTextContent(/did not work/)
    expect(screen.getByRole("button", { name: "Add 2 nurses" })).toHaveAttribute("aria-disabled", "false")
    expect(screen.getByRole("button", { name: "Check file" })).toHaveAttribute("aria-disabled", "false")
  })

  it("cannot log out from under an import", async () => {
    const { user, calls } = start("/nurses/import", here({ "POST /api/nurses/import/preview": { body: GOOD } }))
    await check(user)
    const adding = hold("POST /api/nurses/import/confirm")
    await user.click(await screen.findByRole("button", { name: "Add 2 nurses" }))

    await waitFor(() => expect(screen.getByRole("button", { name: "Log out" })).toHaveAttribute("aria-disabled", "true"))
    await user.click(screen.getByRole("button", { name: "Log out" }))
    expect(calls.some((call) => call.url === "/api/auth/logout")).toBe(false)

    await adding.answer({ status: 201, body: SHEET })
    expect(await screen.findByText("aaaa-bbbb-cccc-dddd")).toBeInTheDocument()
  })

  it("asks before leaving while the nurses are being added", async () => {
    let finish: (answer: Answer) => void = () => {}
    const { user, calls, router } = start("/nurses/import", here({
      "POST /api/nurses/import/preview": { body: GOOD },
    }))
    await check(user)
    await screen.findByRole("button", { name: "Add 2 nurses" })
    // Everything else goes on as before: the header's bell polls meanwhile.
    const through = globalThis.fetch
    vi.stubGlobal("fetch", vi.fn((url: string, init: RequestInit = {}) => url !== "/api/nurses/import/confirm" ? through(url, init) : new Promise((resolve) => {
      finish = (answer) => resolve(new Response(JSON.stringify(answer.body), { status: answer.status }))
    })))

    await user.click(screen.getByRole("button", { name: "Add 2 nurses" }))
    expect(await screen.findByText(/can take a few minutes/)).toBeInTheDocument()
    await user.click(screen.getByRole("link", { name: "Home" }))

    expect(await screen.findByRole("alertdialog")).toBeInTheDocument()
    expect(router.state.location.pathname).toBe("/nurses/import")
    expect(calls.length).toBeGreaterThan(0)
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Stay" }))
    await act(async () => finish({ status: 201, body: SHEET }))
    expect(await screen.findByText("aaaa-bbbb-cccc-dddd")).toBeInTheDocument()
  })

  it("links the CNO to the imports and the staff list from the nurses page, and not a manager", async () => {
    start("/nurses", here({ "GET /api/nurses": { body: [] }, "GET /api/units": { body: [] } }))

    expect(await screen.findByRole("link", { name: "Import nurses from a file" })).toHaveAttribute("href", "/nurses/import")
    expect(screen.getByRole("link", { name: "Import skills from a file" })).toHaveAttribute("href", "/nurses/import-skills")
    expect(screen.getByRole("link", { name: "Export staff list" })).toHaveAttribute("href", "/api/nurses/export")
  })

  it("has neither for a unit manager", async () => {
    start("/nurses", {
      ...session({ ...ME, username: "mona", role: "unit_manager" }),
      "GET /api/nurses": { body: [] },
      "GET /api/units": { body: [] },
    })

    await screen.findByRole("heading", { name: "Nurses" })
    expect(screen.queryByRole("link", { name: /^Import/ })).not.toBeInTheDocument()
    expect(screen.queryByRole("link", { name: "Export staff list" })).not.toBeInTheDocument()
  })
})

describe("importing skills", () => {
  /** The cells of each line in a group, its heading row left out. */
  const cellsOf = (group: HTMLElement) => within(group).getAllByRole("row").slice(1)
    .map((line) => within(line).getAllByRole("cell").map((cell) => cell.textContent))
  const CHIEF = { ...ME, username: "chief", full_name: "The Chief", role: "cno" }
  const here = (extra = {}) => ({ ...session(CHIEF), ...extra })
  const ROW = {
    line: 2, hospital_id: "h1", full_name: "Amal Saleh", skill: "BLS", kind: "skill", expiry: "2027-12-31", identifier: "", issue_date: "",
    change: "renew", held_expiry: "2026-12-31", held_identifier: null, held_issue_date: null, problems: [],
  }
  const NEW = { ...ROW, line: 3, hospital_id: "h2", full_name: "Badr Omar", skill: "ACLS", expiry: "2027-03-01", change: "record", held_expiry: null }
  const SAME = { ...ROW, line: 4, skill: "ICU", expiry: "", change: "none", held_expiry: null }
  const BAD = { ...NEW, full_name: "", change: null, problems: [
    { column: "hospital_id", message: "No nurse has this hospital ID. Use the one the Nurses page or Rostering attributes shows." },
    { column: "expiry", message: "ACLS expires. Enter the last day it counts, as YYYY-MM-DD." },
  ] }
  const GOOD = { rows: [ROW, NEW, SAME], token: "the-token" }
  const csv = (content = "a file") => new File([content], "skills.csv", { type: "text/csv" })

  async function check(user: ReturnType<typeof userEvent.setup>, file = csv()) {
    await user.upload(await screen.findByLabelText("CSV file"), file)
    await user.click(screen.getByRole("button", { name: "Check file" }))
  }

  it("offers the CNO the template and says what each column holds", async () => {
    const { router } = start("/nurses/import-skills", here())

    const link = await screen.findByRole("link", { name: "Download the template" })
    expect(link).toHaveAttribute("href", "/api/nurses/skills-import/template")
    expect(link).toHaveAttribute("download")
    expect(document.title).toBe("Import skills · Nurse roster")
    expect(screen.getByText(/Empty for a skill that does not expire/)).toBeInTheDocument()
    expect(screen.getByText("Identifier")).toBeInTheDocument()
    expect(screen.getByText(/Required on a licence/)).toBeInTheDocument()
    expect(screen.getByText("Issue date")).toBeInTheDocument()
    expect(router.state.location.pathname).toBe("/nurses/import-skills")
  })

  it("sends a unit manager home", async () => {
    const { router } = start("/nurses/import-skills", {
      ...session({ ...ME, username: "mona", role: "unit_manager" }),
      "GET /api/units": { body: [] },
    })

    await waitFor(() => expect(router.state.location.pathname).toBe("/"))
  })

  it("is the Deputy CNO's too, from the link beside Rostering attributes, and saves as the CNO's does", async () => {
    const { user, router, calls } = start("/", {
      ...session(ME_DEPUTY), "GET /api/rosters/submitted": { body: [] },
      "POST /api/nurses/skills-import/preview": { body: GOOD },
      "POST /api/nurses/skills-import/confirm": { body: { recorded: 1, renewed: 1, unchanged: 1 } },
    })
    await screen.findByText("No roster is waiting for a decision.")
    const links = within(screen.getByRole("navigation", { name: "Main" })).getAllByRole("link").map((link) => link.textContent)
    expect(links.slice(links.indexOf("Rostering attributes"), links.indexOf("Rostering attributes") + 2)).toEqual(["Rostering attributes", "Import skills"])

    await user.click(within(screen.getByRole("navigation", { name: "Main" })).getByRole("link", { name: "Import skills" }))

    expect(await screen.findByRole("heading", { level: 1, name: "Import skills" })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe("/nurses/import-skills")
    expect(screen.getByRole("link", { name: "Download the template" })).toHaveAttribute("href", "/api/nurses/skills-import/template")
    const back = within(screen.getByText(/Or go back to/))
    expect(back.getByRole("link", { name: "Rostering attributes" })).toHaveAttribute("href", "/rostering-attributes")
    expect(back.queryByRole("link", { name: "Nurses" })).not.toBeInTheDocument()
    expect(screen.getByText("As on Rostering attributes.")).toBeInTheDocument()
    await check(user)
    expect(await screen.findByRole("heading", { name: "3 lines ready to save" })).toBeInTheDocument()
    await user.click(screen.getByRole("button", { name: "Save 2 skills" }))

    expect(await screen.findByText("1 skill recorded and 1 skill renewed.")).toBeInTheDocument()
    expect(calls.filter((call) => call.method === "POST").map((call) => call.url))
      .toEqual(["/api/nurses/skills-import/preview", "/api/nurses/skills-import/confirm"])
  })

  it("sends the CNO back to Nurses, where her IDs are, and gives her no link to it in the header", async () => {
    start("/nurses/import-skills", here())

    expect(within(await screen.findByText(/Or go back to/)).getByRole("link", { name: "Nurses" })).toHaveAttribute("href", "/nurses")
    expect(screen.getByText("As on the Nurses page.")).toBeInTheDocument()
    expect(within(screen.getByRole("navigation", { name: "Main" })).queryByRole("link", { name: "Import skills" })).not.toBeInTheDocument()
  })

  it("refuses a large file before sending it, in the skills' words", async () => {
    const { user, calls } = start("/nurses/import-skills", here())

    await check(user, csv("x".repeat(1024 * 1024 + 1)))

    expect(await screen.findByText("This file is larger than 1 MB, which is more than 500 skills need.")).toBeInTheDocument()
    expect(calls.filter((call) => call.method === "POST")).toEqual([])
  })

  it("shows a refusal of the whole file beside the field", async () => {
    const { user } = start("/nurses/import-skills", here({
      "POST /api/nurses/skills-import/preview": { status: 422, body: { detail: [{ type: "value_error", loc: ["body", "body", "file"], msg: "This is not a CSV file in UTF-8." }] } },
    }))

    await check(user)

    expect(await screen.findByText("This is not a CSV file in UTF-8.")).toBeInTheDocument()
    expect(screen.getByLabelText("CSV file")).toHaveFocus()
  })

  it("shows only the lines with problems, each beside its column's name, and offers nothing to save", async () => {
    const { user } = start("/nurses/import-skills", here({
      "POST /api/nurses/skills-import/preview": { body: { rows: [ROW, BAD], token: null } },
    }))

    await check(user)

    expect(await screen.findByRole("heading", { name: "1 of 2 lines has a problem" })).toHaveFocus()
    expect(screen.getByText("Hospital ID: No nurse has this hospital ID. Use the one the Nurses page or Rostering attributes shows.")).toBeInTheDocument()
    expect(screen.getByText("Expiry: ACLS expires. Enter the last day it counts, as YYYY-MM-DD.")).toBeInTheDocument()
    expect(screen.queryByText("Amal Saleh")).not.toBeInTheDocument()
    expect(screen.getByText(/The other line has no problem/)).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /^Save/ })).not.toBeInTheDocument()
  })

  it("shows what saving does with each line, saves the very bytes it checked with the token, and says what was saved", async () => {
    let sent: FormData | undefined
    const { user } = start("/nurses/import-skills", here({
      "POST /api/nurses/skills-import/preview": { body: GOOD },
      "POST /api/nurses/skills-import/confirm": ({ body }: { body: unknown }) => {
        sent = body as FormData
        return { body: { recorded: 1, renewed: 1, unchanged: 1 } }
      },
    }))

    await check(user, csv("the bytes"))

    expect(await screen.findByRole("heading", { name: "3 lines ready to save" })).toHaveFocus()
    expect(screen.getByText("Saving records 1 skill and renews 1 skill. Nothing is saved until you do. 1 line says what is already recorded.")).toBeInTheDocument()
    expect(cellsOf(within(screen.getByRole("region", { name: "Lines of the file" })).getByRole("rowgroup", { name: "Skills" }))).toEqual([
      ["2", "Renewal, was until 31 Dec 2026", "h1", "Amal Saleh", "BLS", "2027-12-31", "", ""],
      ["3", "New", "h2", "Badr Omar", "ACLS", "2027-03-01", "", ""],
      ["4", "Already recorded", "h1", "Amal Saleh", "ICU", "", "", ""],
    ])

    await user.click(screen.getByRole("button", { name: "Save 2 skills" }))

    expect(await screen.findByText("1 skill recorded and 1 skill renewed.")).toBeInTheDocument()
    expect(screen.getByRole("heading", { name: "Import skills" })).toHaveFocus()
    expect(screen.queryByRole("heading", { name: "3 lines ready to save" })).not.toBeInTheDocument()
    expect(sent?.get("token")).toBe("the-token")
    expect(await (sent?.get("file") as Blob).text()).toBe("the bytes")
  })

  it("groups the lines under Skills, Certificates and Licences, with each one's identifier and issue date and what a renewal changes", async () => {
    const LICENCE = { ...ROW, line: 5, skill: "SCFHS", kind: "licence", expiry: "2026-12-31", identifier: "21-RN-0413", issue_date: "2024-12-15",
      held_expiry: "2026-12-31", held_identifier: "21-RN-0412", held_issue_date: "2024-12-15" }
    const DEGREE = { ...ROW, line: 6, skill: "BSN", kind: "certificate", expiry: "", identifier: "", issue_date: "2019-07-01",
      held_expiry: null, held_identifier: "KSU-7", held_issue_date: null }
    const RENEWED = { ...LICENCE, line: 7, hospital_id: "h2", full_name: "Badr Omar", expiry: "2028-12-31", identifier: "21-RN-0777", issue_date: "2026-11-01",
      held_identifier: "21-RN-0777", held_issue_date: "2024-11-01" }
    const { user } = start("/nurses/import-skills", here({
      "POST /api/nurses/skills-import/preview": { body: { rows: [ROW, LICENCE, DEGREE, RENEWED], token: "the-token" } },
    }))

    await check(user)

    const table = await screen.findByRole("region", { name: "Lines of the file" })
    expect(within(within(table).getAllByRole("row")[0]).getAllByRole("columnheader").map((head) => head.textContent)).toEqual(
      ["Line", "Change", "Hospital ID", "Nurse", "Skill", "Expiry", "Identifier", "Issue date"])
    expect(within(table).getAllByRole("rowgroup").slice(1).map((group) => within(group).getAllByRole("row")[0].textContent))
      .toEqual(["Skills", "Certificates", "Licences"])
    expect(cellsOf(within(table).getByRole("rowgroup", { name: "Certificates" }))).toEqual([
      ["6", "Renewal, identifier was KSU-7, no issue date before", "h1", "Amal Saleh", "BSN", "", "", "2019-07-01"],
    ])
    expect(cellsOf(within(table).getByRole("rowgroup", { name: "Licences" }))).toEqual([
      ["5", "Renewal, identifier was 21-RN-0412", "h1", "Amal Saleh", "SCFHS", "2026-12-31", "21-RN-0413", "2024-12-15"],
      ["7", "Renewal, was until 31 Dec 2026, issue date was 1 Nov 2024", "h2", "Badr Omar", "SCFHS", "2028-12-31", "21-RN-0777", "2026-11-01"],
    ])
  })

  it("names the identifier's and the issue date's problems, and shows a line whose skill is unknown apart", async () => {
    const LICENCE = { ...ROW, line: 3, skill: "SCFHS", kind: "licence", identifier: "21-RN-0412", issue_date: "2027-02-01", change: null, problems: [
      { column: "identifier", message: "Amal Saleh already holds SCFHS with this identifier. Check the number: each one is one nurse's." },
      { column: "issue_date", message: "The issue date cannot be after the expiry." },
    ] }
    const UNKNOWN = { ...ROW, line: 4, skill: "Nothing", kind: null, change: null, problems: [
      { column: "skill", message: "There is no such skill. Use its name on the Skills page." },
    ] }
    const { user } = start("/nurses/import-skills", here({
      "POST /api/nurses/skills-import/preview": { body: { rows: [ROW, LICENCE, UNKNOWN], token: null } },
    }))

    await check(user)

    expect(await screen.findByRole("heading", { name: "2 of 3 lines have a problem" })).toBeInTheDocument()
    const table = screen.getByRole("region", { name: "Lines of the file" })
    expect(within(within(table).getByRole("rowgroup", { name: "Licences" })).getByText(
      "Identifier: Amal Saleh already holds SCFHS with this identifier. Check the number: each one is one nurse's.")).toBeInTheDocument()
    expect(within(table).getByText("Issue date: The issue date cannot be after the expiry.")).toBeInTheDocument()
    expect(cellsOf(within(table).getByRole("rowgroup", { name: "Other lines" }))[0].slice(2, 5)).toEqual(["h1", "Amal Saleh", "Nothing"])
    expect(within(table).queryByRole("rowgroup", { name: "Skills" })).not.toBeInTheDocument()
  })

  it("offers nothing to save when every line is already recorded", async () => {
    const { user } = start("/nurses/import-skills", here({
      "POST /api/nurses/skills-import/preview": { body: { rows: [SAME], token: "the-token" } },
    }))

    await check(user)

    expect(await screen.findByText("Every line says what is already recorded, so there is nothing to save.")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /^Save/ })).not.toBeInTheDocument()
  })

  it("shows what changed when the file no longer means what was previewed, and saves nothing", async () => {
    const { user } = start("/nurses/import-skills", here({
      "POST /api/nurses/skills-import/preview": { body: GOOD },
      "POST /api/nurses/skills-import/confirm": { status: 409, body: { rows: [ROW, BAD], token: null } },
    }))
    await check(user)

    await user.click(await screen.findByRole("button", { name: "Save 2 skills" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("Something changed since the last check, so nothing was saved.")
    expect(await screen.findByRole("heading", { name: "1 of 2 lines has a problem" })).toHaveFocus()
    expect(screen.queryByRole("button", { name: /^Save/ })).not.toBeInTheDocument()
  })

  it("says plainly when it cannot tell whether the skills were saved", async () => {
    const { user } = start("/nurses/import-skills", here({
      "POST /api/nurses/skills-import/preview": { body: GOOD },
      "POST /api/nurses/skills-import/confirm": { status: 502 },
    }))
    await check(user)

    await user.click(await screen.findByRole("button", { name: "Save 2 skills" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("It is not known whether these skills were saved.")
  })

  it("holds back Save while it saves and Check file with it", async () => {
    const { user } = start("/nurses/import-skills", here({ "POST /api/nurses/skills-import/preview": { body: GOOD } }))
    await check(user)
    const saving = hold("POST /api/nurses/skills-import/confirm")

    await user.click(await screen.findByRole("button", { name: "Save 2 skills" }))

    expect(await screen.findByRole("button", { name: "Saving…" })).toHaveAttribute("aria-disabled", "true")
    expect(screen.getByRole("button", { name: "Check file" })).toHaveAttribute("aria-disabled", "true")
    await saving.answer({ body: { recorded: 1, renewed: 1, unchanged: 1 } })
    expect(await screen.findByText("1 skill recorded and 1 skill renewed.")).toBeInTheDocument()
  })
})

const HEADCOUNT = { weekday_min: 7, weekday_max: 9, weekend_min: 8, weekend_max: 9, also_counts: [] as string[] }
const UNIT_RULES = {
  shifts: ["D", "N"],
  headcounts: [{ shift: "D", ...HEADCOUNT }, { shift: "N", ...HEADCOUNT }],
  headcount_enabled: true,
  require_team_leader: true,
  require_both_genders: true,
  score_target: 0.7,
  score_target_weekend: 0.8,
  score_balance: true,
  score_balance_mode: "band",
  score_band_hi: 0.78,
  score_band_hi_weekend: 0.88,
  night_shift: "N",
  day_shift: "D",
  night_rest_days: 2,
  night_rest_hard: true,
  fair_shifts: ["D", "N"],
  fair_tolerance: 0.5,
  fair_weekends: true,
  carry_fairness: true,
  carry_cap: 2,
  share_overtime: true,
  overtime_tolerance: 12,
  overtime_carry_cap: 12,
  max_consecutive_shifts: 3,
  double_cycle_rest: 2,
  max_weekends: 3,
  max_full_runs: 3,
  no_mixed_blocks: true,
  max_switches: 2,
  switches_hard: false,
  skill_needs: [] as { shift: string; skill: string; weekday_min: number; weekend_min: number }[],
  skill_needs_hard: true,
}
const UNIT_RULES_STATE = {
  rules: UNIT_RULES,
  needs_review: true,
  request_cap: 3,
  preference_cap: 10,
  default_time_limit: 60,
  time_limit_cap: 300,
  active_shifts: [
    { code: "D", display_name: "Day", hours: 12 },
    { code: "N", display_name: "Night", hours: 12 },
    { code: "A", display_name: "Morning", hours: 8 },
  ],
  active_skills: ["BLS", "ICU"],
}
const ER_UNIT = { id: "er", name: "ER", archived: false, manager: null, rules_need_review: true }
const ICU = { id: "icu", name: "ICU", archived: false, manager: null, rules_need_review: false }
const CLOSED = { id: "old", name: "Old", archived: true, manager: null, rules_need_review: false }
const ME_MANAGER = { ...ME, username: "mona", full_name: "Mona Saleh", role: "unit_manager" }
const ME_CNO = { ...ME, username: "chief", full_name: "Fatimah", role: "cno" }
const ME_DEPUTY = { ...ME, username: "deputy", full_name: "Deputy", role: "deputy_cno" }
const apiRefusal = (loc: (string | number)[], msg: string) => ({ status: 422, body: { detail: [{ type: "value_error", loc: ["body", "body", ...loc], msg: `Value error, ${msg}` }] } })

describe("the unit rules page", () => {
  const FLAG = "Defaults, review before the first solve"
  const saves = { "PUT /api/units/er/rules": ({ body }: { body: unknown }) => ({ body: { ...UNIT_RULES_STATE, ...(body as object), needs_review: false } }) }
  const asManager = (state: unknown = UNIT_RULES_STATE, extra = {}) => ({
    ...session(ME_MANAGER),
    "GET /api/units": { body: [ER_UNIT] },
    "GET /api/units/er/rules": { body: state },
    ...extra,
  })
  const put = (calls: { method: string; body: unknown }[]) => calls.find((call) => call.method === "PUT")?.body as { rules: typeof UNIT_RULES; request_cap: number; default_time_limit: number }

  it("shows every field of the solver's block with its explanation, and flags the defaults", async () => {
    start("/unit-rules", asManager())

    expect(await screen.findByText(FLAG)).toBeInTheDocument()
    expect(document.title).toBe("Unit rules · Nurse roster")
    expect(screen.getByRole("heading", { level: 1, name: "Unit rules of ER" })).toBeInTheDocument()
    expect(screen.getByRole("checkbox", { name: "D, Day, 12 hours" })).toBeChecked()
    expect(screen.getByRole("checkbox", { name: "A, Morning, 8 hours" })).not.toBeChecked()
    expect(screen.getByLabelText("Night shift")).toHaveValue("N")
    expect(screen.getByLabelText("Day shift")).toHaveValue("D")
    const day = within(screen.getByRole("region", { name: "D, Day" }))
    expect(day.getByLabelText("Weekday, at least")).toHaveValue(7)
    expect(day.getByLabelText("Weekend day, at most")).toHaveValue(9)
    expect(day.getByRole("checkbox", { name: "N" })).not.toBeChecked()
    for (const [name, value] of [["Score target on a weekday", 0.7], ["Score target on a weekend day", 0.8], ["Score ceiling on a weekday", 0.78],
      ["Score ceiling on a weekend day", 0.88], ["Days off after a night shift", 2], ["Longest run of shifts", 3], ["Most full runs in a month", 3], ["Days off after two runs", 2],
      ["Free difference from a fair share (shifts)", 0.5], ["Most a month's share can move (shifts)", 2], ["Weekend days a month", 3],
      ["Switches between day and night a month", 2], ["Requests per nurse per month", 3], ["Preferences per nurse per month", 10], ["Default time limit (seconds)", 60]] as const) {
      expect(screen.getByLabelText(name)).toHaveValue(value)
      expect(screen.getByLabelText(name)).toHaveAccessibleDescription(/\S/)
    }
    for (const name of ["Hold every shift to its headcount", "A team leader on every shift", "Both genders on every shift", "Keep the strength of shifts balanced",
      "Never break the night rest", "Keep runs unmixed", "Spread weekend days fairly", "Carry fairness between months"]) {
      expect(screen.getByRole("checkbox", { name })).toBeChecked()
      expect(screen.getByRole("checkbox", { name })).toHaveAccessibleDescription(/\S/)
    }
    expect(screen.getByRole("checkbox", { name: "Never go over the switch limit" })).not.toBeChecked()
    expect(screen.getByLabelText("Way of balancing")).toHaveValue("band")
    expect(screen.getByLabelText("Default time limit (seconds)")).toHaveAccessibleDescription(/cap is 300 seconds/)
  })

  it("saving the defaults untouched is the review and sends the solver's block", async () => {
    const { user, calls } = start("/unit-rules", asManager(UNIT_RULES_STATE, saves))

    await screen.findByText(FLAG)
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByText("Saved.")).toHaveAttribute("role", "status")
    expect(put(calls)).toEqual({ rules: UNIT_RULES, request_cap: 3, preference_cap: 10, default_time_limit: 60 })
    expect(screen.queryByText(FLAG)).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute("aria-disabled", "true")
  })

  it("the most full runs show beside the longest run only while it is above 1, and keep their value while hidden", async () => {
    const { user, calls } = start("/unit-rules", asManager(UNIT_RULES_STATE, saves))

    const fullRuns = await screen.findByLabelText("Most full runs in a month")
    expect(fullRuns).toHaveAccessibleDescription(
      "A full run is as many shifts in a row as the longest run allows. Each one beyond this costs the solver, which then breaks the work into shorter runs.")
    const longest = screen.getByLabelText("Longest run of shifts")
    const runs = within(screen.getByRole("region", { name: "Runs of shifts" })).getAllByRole("spinbutton")
    expect(runs.slice(0, 2)).toEqual([longest, fullRuns])
    await user.clear(fullRuns)
    await user.type(fullRuns, "2")
    for (const hidden of ["1", "0"]) {
      await user.clear(longest)
      await user.type(longest, hidden)
      expect(screen.getByLabelText("Longest run of shifts")).toHaveValue(Number(hidden))
      expect(screen.queryByLabelText("Most full runs in a month")).not.toBeInTheDocument()
    }
    await user.clear(longest)
    await user.type(longest, "4")

    expect(screen.getByLabelText("Most full runs in a month")).toHaveValue(2)
    await user.click(screen.getByRole("button", { name: "Save" }))
    await screen.findByText("Saved.")
    expect(put(calls).rules).toEqual({ ...UNIT_RULES, max_consecutive_shifts: 4, max_full_runs: 2 })
  })

  it("the most full runs are a whole number from 0 to 31, and one left wrong behind a hidden field is not sent", async () => {
    const { user, calls } = start("/unit-rules", asManager(UNIT_RULES_STATE, saves))

    const fullRuns = await screen.findByLabelText("Most full runs in a month")
    await user.clear(fullRuns)
    await user.type(fullRuns, "32")
    await user.click(screen.getByRole("button", { name: "Save" }))
    expect(await screen.findByText("Between 0 and 31.")).toBeInTheDocument()
    expect(screen.getByLabelText("Most full runs in a month")).toHaveFocus()
    expect(calls.some((call) => call.method === "PUT")).toBe(false)

    const longest = screen.getByLabelText("Longest run of shifts")
    await user.clear(longest)
    await user.type(longest, "1")
    await user.click(screen.getByRole("button", { name: "Save" }))

    await screen.findByText("Saved.")
    expect(put(calls).rules).toEqual({ ...UNIT_RULES, max_consecutive_shifts: 1, max_full_runs: 3 })
  })

  it("the manager adds, changes and removes skill needs, and makes them soft", async () => {
    const { user, calls } = start("/unit-rules", asManager(UNIT_RULES_STATE, saves))

    const region = within(await screen.findByRole("region", { name: "Skill needs" }))
    expect(region.getByText("No skill needs. The solver staffs every shift without looking at skills.")).toBeInTheDocument()
    const hard = region.getByRole("checkbox", { name: "Never break the skill needs" })
    expect(hard).toBeChecked()
    expect(hard).toHaveAccessibleDescription(/at the price of the hospital's weight for each nurse short/)

    await user.click(region.getByRole("button", { name: "Add a skill need" }))
    const first = within(region.getByRole("group", { name: "Skill need 1" }))
    expect(first.getByLabelText("Shift")).toHaveFocus()
    expect([first.getByLabelText("Shift"), first.getByLabelText("Skill")].map((select) => (select as HTMLSelectElement).value)).toEqual(["D", "BLS"])
    expect([...first.getByLabelText("Shift").querySelectorAll("option")].map((option) => option.value)).toEqual(["D", "N"])
    expect([...first.getByLabelText("Skill").querySelectorAll("option")].map((option) => option.value)).toEqual(["BLS", "ICU"])
    await user.selectOptions(first.getByLabelText("Shift"), "N")
    await user.selectOptions(first.getByLabelText("Skill"), "ICU")
    await user.clear(first.getByLabelText("Weekday, at least"))
    await user.type(first.getByLabelText("Weekday, at least"), "2")

    await user.click(region.getByRole("button", { name: "Add a skill need" }))
    const second = within(region.getByRole("group", { name: "Skill need 2" }))
    expect((second.getByLabelText("Skill") as HTMLSelectElement).value).toBe("BLS")
    await user.click(second.getByRole("button", { name: "Remove skill need 2" }))
    expect(region.queryByRole("group", { name: "Skill need 2" })).not.toBeInTheDocument()
    expect(region.getByRole("button", { name: "Add a skill need" })).toHaveFocus()

    await user.click(hard)
    await user.click(screen.getByRole("button", { name: "Save" }))

    await screen.findByText("Saved.")
    expect(put(calls).rules).toEqual({ ...UNIT_RULES, skill_needs: [{ shift: "N", skill: "ICU", weekday_min: 2, weekend_min: 1 }], skill_needs_hard: false })
  })

  it("a second need for one shift and skill, and a shift taken out while a need names it, are refused before saving", async () => {
    const needs = [{ shift: "N", skill: "ICU", weekday_min: 1, weekend_min: 1 }]
    const { user, calls } = start("/unit-rules", asManager({ ...UNIT_RULES_STATE, rules: { ...UNIT_RULES, skill_needs: needs } }, saves))

    const region = within(await screen.findByRole("region", { name: "Skill needs" }))
    await user.click(region.getByRole("button", { name: "Add a skill need" }))
    const second = within(region.getByRole("group", { name: "Skill need 2" }))
    await user.selectOptions(second.getByLabelText("Shift"), "N")
    await user.selectOptions(second.getByLabelText("Skill"), "ICU")
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByText("N already has a need for ICU.")).toBeInTheDocument()
    expect(second.getByLabelText("Skill")).toHaveFocus()
    await user.click(second.getByRole("button", { name: "Remove skill need 2" }))

    await user.click(screen.getByRole("checkbox", { name: "N, Night, 12 hours" }))
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByText("A skill need names a shift you are taking out: ICU on N. Take the need out first.")).toBeInTheDocument()
    expect(within(screen.getByRole("group", { name: "Skill need 1" })).getByLabelText("Shift")).toHaveValue("N")
    expect(calls.some((call) => call.method === "PUT")).toBe(false)
  })

  it("a need's minimums are whole numbers from 0, and a refusal from the API on a need takes focus", async () => {
    const needs = [{ shift: "N", skill: "ICU", weekday_min: 1, weekend_min: 1 }]
    const { user, calls } = start("/unit-rules", asManager({ ...UNIT_RULES_STATE, rules: { ...UNIT_RULES, skill_needs: needs } }, {
      "PUT /api/units/er/rules": apiRefusal(["rules", "skill_needs", 0, "skill"], "ICU is not an active skill in the catalogue."),
    }))

    const need = within(await screen.findByRole("group", { name: "Skill need 1" }))
    await user.clear(need.getByLabelText("Weekend day, at least"))
    await user.type(need.getByLabelText("Weekend day, at least"), "-1")
    await user.click(screen.getByRole("button", { name: "Save" }))
    expect(await need.findByText("Between 0 and 1000.")).toBeInTheDocument()
    expect(need.getByLabelText("Weekend day, at least")).toHaveFocus()
    expect(calls.some((call) => call.method === "PUT")).toBe(false)

    await user.clear(need.getByLabelText("Weekend day, at least"))
    await user.type(need.getByLabelText("Weekend day, at least"), "0")
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await need.findByText("ICU is not an active skill in the catalogue.")).toBeInTheDocument()
    expect(need.getByLabelText("Skill")).toHaveFocus()
  })

  it("a need on a skill no longer offered still shows as it is", async () => {
    const needs = [{ shift: "D", skill: "PALS", weekday_min: 1, weekend_min: 0 }]
    start("/unit-rules", asManager({ ...UNIT_RULES_STATE, rules: { ...UNIT_RULES, skill_needs: needs } }))

    const skill = within(await screen.findByRole("group", { name: "Skill need 1" })).getByLabelText("Skill")
    expect(skill).toHaveValue("PALS")
    expect([...skill.querySelectorAll("option")].map((option) => option.value)).toEqual(["PALS", "BLS", "ICU"])
  })

  it("with no skill in the catalogue a need cannot be added, and the page says why", async () => {
    start("/unit-rules", asManager({ ...UNIT_RULES_STATE, active_skills: [] }))

    const region = within(await screen.findByRole("region", { name: "Skill needs" }))
    expect(region.getByText("The skill catalogue has no skills yet. The CNO adds them.")).toBeInTheDocument()
    expect(region.getByRole("button", { name: "Add a skill need" })).toHaveAttribute("aria-disabled", "true")
  })

  it("a refusal from the API appears beside its field, with focus on it", async () => {
    const { user } = start("/unit-rules", asManager(UNIT_RULES_STATE, {
      "PUT /api/units/er/rules": apiRefusal(["rules", "night_shift"], "A is not one of this unit's shifts."),
    }))

    await user.selectOptions(await screen.findByLabelText("Night shift"), "A")
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByText("A is not one of this unit's shifts.")).toBeInTheDocument()
    expect(screen.getByLabelText("Night shift")).toHaveFocus()
    expect(screen.getByLabelText("Night shift")).toHaveAccessibleDescription(/not one of this unit's shifts/)
    expect(screen.getByRole("alert")).toHaveTextContent("Some values were refused")
  })

  it("a refusal on the unit's shifts appears on the list, with its position dropped, and takes focus", async () => {
    const { user } = start("/unit-rules", asManager(UNIT_RULES_STATE, {
      "PUT /api/units/er/rules": apiRefusal(["rules", "shifts", 1], "Amal Saleh (h1) could work none of these shifts. Change their allowed shifts first."),
    }))

    await screen.findByText(FLAG)
    await user.click(screen.getByRole("button", { name: "Save" }))

    const message = await screen.findByText(/could work none of these shifts/)
    expect(message).toBeInTheDocument()
    expect(screen.getByRole("group", { name: "Shifts this unit works" })).toHaveFocus()
  })

  it("a refusal on a shift counting towards another appears on that shift's list", async () => {
    const { user } = start("/unit-rules", asManager(UNIT_RULES_STATE, {
      "PUT /api/units/er/rules": apiRefusal(["rules", "headcounts", 0, "also_counts", 0], "N cannot count."),
    }))

    await screen.findByText(FLAG)
    await user.click(screen.getByRole("button", { name: "Save" }))
    await screen.findByText("N cannot count.")

    const day = within(screen.getByRole("region", { name: "D, Day" }))
    expect(within(day.getByRole("group", { name: "Shifts that also count towards it" })).getByText("N cannot count.")).toBeInTheDocument()
  })

  it("says what is wrong before asking the API", async () => {
    const { user, calls } = start("/unit-rules", asManager())

    const target = await screen.findByLabelText("Score target on a weekday")
    await user.clear(target)
    await user.type(target, "1.5")
    const low = within(screen.getByRole("region", { name: "D, Day" })).getByLabelText("Weekday, at most")
    await user.clear(low)
    await user.type(low, "1")
    await user.selectOptions(screen.getByLabelText("Day shift"), "N")
    const limit = screen.getByLabelText("Default time limit (seconds)")
    await user.clear(limit)
    await user.type(limit, "301")
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByText("Between 0 and 1.")).toBeInTheDocument()
    expect(screen.getByText("Cannot be below the weekday minimum.")).toBeInTheDocument()
    expect(screen.getByText("Cannot be the night shift too.")).toBeInTheDocument()
    expect(screen.getByText("Between 1 and 300.")).toBeInTheDocument()
    expect(calls.some((call) => call.method === "PUT")).toBe(false)
  })

  it("a ceiling below its target is refused only when the ceilings apply", async () => {
    const { user, calls } = start("/unit-rules", asManager(UNIT_RULES_STATE, saves))

    const ceiling = await screen.findByLabelText("Score ceiling on a weekday")
    await user.clear(ceiling)
    await user.type(ceiling, "0.5")
    await user.click(screen.getByRole("button", { name: "Save" }))
    expect(await screen.findByText("Cannot be below the weekday score target.")).toBeInTheDocument()
    expect(calls.some((call) => call.method === "PUT")).toBe(false)

    await user.selectOptions(screen.getByLabelText("Way of balancing"), "spread")
    await user.click(screen.getByRole("button", { name: "Save" }))

    await screen.findByText("Saved.")
    expect(put(calls).rules.score_balance_mode).toBe("spread")
  })

  it("a shift the unit stops working leaves its headcount, fair shares and what counts towards others", async () => {
    const state = { ...UNIT_RULES_STATE, rules: { ...UNIT_RULES, headcounts: [{ shift: "D", ...HEADCOUNT, also_counts: ["N"] }, { shift: "N", ...HEADCOUNT }] } }
    const { user, calls } = start("/unit-rules", asManager(state, saves))

    await user.click(await screen.findByRole("checkbox", { name: "N, Night, 12 hours" }))
    expect(screen.queryByRole("region", { name: "N, Night" })).not.toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText("Night shift"), "A")
    await user.click(screen.getByRole("checkbox", { name: "A, Morning, 8 hours" }))
    await user.click(screen.getByRole("button", { name: "Save" }))

    await screen.findByText("Saved.")
    const { rules } = put(calls)
    expect(rules.shifts).toEqual(["D", "A"])
    expect(rules.headcounts).toEqual([{ shift: "D", ...HEADCOUNT }])
    expect(rules.fair_shifts).toEqual(["D"])
    expect(rules.night_shift).toBe("A")
  })

  it("a headcount is added and removed with focus staying in its shift, and a shift without one cannot be spread fairly", async () => {
    const { user, calls } = start("/unit-rules", asManager(UNIT_RULES_STATE, saves))

    await user.click(await screen.findByRole("checkbox", { name: "A, Morning, 8 hours" }))
    const morning = within(screen.getByRole("region", { name: "A, Morning" }))
    const fair = within(screen.getByRole("group", { name: "Shifts to spread fairly" }))
    expect(fair.queryByRole("checkbox", { name: "A" })).not.toBeInTheDocument()
    await user.click(morning.getByRole("button", { name: "Add a headcount for A" }))
    await waitFor(() => expect(morning.getByLabelText("Weekday, at least")).toHaveFocus())
    await user.click(fair.getByRole("checkbox", { name: "A" }))
    await user.click(morning.getByRole("button", { name: "Remove the headcount of A" }))
    await waitFor(() => expect(morning.getByRole("button", { name: "Add a headcount for A" })).toHaveFocus())
    await user.click(screen.getByRole("button", { name: "Save" }))

    await screen.findByText("Saved.")
    expect(put(calls).rules.headcounts.map((row) => row.shift)).toEqual(["D", "N"])
    expect(put(calls).rules.fair_shifts).toEqual(["D", "N"])
  })

  it("a unit seeded with a gap opens from blank choices and cannot be saved until they are made", async () => {
    const gap = { ...UNIT_RULES, shifts: [], headcounts: [], fair_shifts: [], night_shift: "", day_shift: "" }
    const { user, calls } = start("/unit-rules", asManager({ ...UNIT_RULES_STATE, rules: gap }))

    expect(await screen.findByLabelText("Night shift")).toHaveValue("")
    expect(screen.getByLabelText("Day shift")).toHaveValue("")
    expect(screen.getByText("Choose the unit's shifts first.")).toBeInTheDocument()
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByText("Choose at least one shift.")).toBeInTheDocument()
    expect(screen.getByText("Choose the night shift.")).toBeInTheDocument()
    expect(screen.getByText("Choose the day shift.")).toBeInTheDocument()
    expect(calls.some((call) => call.method === "PUT")).toBe(false)
  })

  it("a failure that is no refusal says to try again", async () => {
    const { user } = start("/unit-rules", asManager(UNIT_RULES_STATE, { "PUT /api/units/er/rules": { status: 500 } }))

    await screen.findByText(FLAG)
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("could not be saved")
  })

  it("a manager with no unit is told so", async () => {
    start("/unit-rules", { ...session(ME_MANAGER), "GET /api/units": { body: [] } })

    expect(await screen.findByText(/No unit is assigned to you yet/)).toBeInTheDocument()
  })

  it.each([["the CNO", ME_CNO], ["the Deputy CNO", ME_DEPUTY]])("%s reads with no input, and chooses the unit in the address", async (_name, me) => {
    const { user, router } = start("/unit-rules", {
      ...session(me),
      "GET /api/units": { body: [CLOSED, ER_UNIT, ICU] },
      "GET /api/units/er/rules": { body: UNIT_RULES_STATE },
      "GET /api/units/icu/rules": { body: { ...UNIT_RULES_STATE, needs_review: false, rules: { ...UNIT_RULES, score_target: 0.42 } } },
    })

    const longest = await screen.findByText("Longest run of shifts")
    expect(longest).toBeInTheDocument()
    expect(screen.getByLabelText("Unit")).toHaveValue("er")
    expect(within(screen.getByLabelText("Unit")).getByRole("option", { name: "Old (archived)" })).toBeInTheDocument()
    expect(screen.getByText("Defaults, review before the first solve")).toBeInTheDocument()
    expect(screen.queryByText(/Saving this page/)).not.toBeInTheDocument()
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument()
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument()
    expect(screen.getByText(/Only the unit's manager can change these/)).toBeInTheDocument()

    await user.selectOptions(screen.getByLabelText("Unit"), "icu")

    expect(await screen.findByText("0.42")).toBeInTheDocument()
    expect(router.state.location.search).toEqual({ unit: "icu" })
  })

  it("the overtime share's three rules sit in the fairness group beside the weekend spread, with their help, and save", async () => {
    const { user, calls } = start("/unit-rules", asManager(UNIT_RULES_STATE, saves))

    const fairness = within(await screen.findByRole("region", { name: "Fairness" }))
    const share = fairness.getByRole("checkbox", { name: "Share overtime evenly" })
    const tolerance = fairness.getByLabelText("Overtime tolerance (hours)")
    const carryCap = fairness.getByLabelText("Most a month's overtime share can move (hours)")
    expect(share).toBeChecked()
    expect(share).toHaveAccessibleDescription("Overtime is spread among the unit's nurses in proportion to the hours each owes.")
    expect(tolerance).toHaveValue(12)
    expect(tolerance).toHaveAccessibleDescription("How many hours of overtime a nurse may work above their fair share before it starts to cost.")
    expect(carryCap).toHaveValue(12)
    expect(carryCap).toHaveAccessibleDescription("The limit on how far what is carried can move a nurse's overtime share in one month.")
    expect(fairness.getByRole("checkbox", { name: "Carry fairness between months" })).toHaveAccessibleDescription(
      "Nights, weekend days and overtime above or below a nurse's fair share so far move this month's fair share.")
    const order = [fairness.getByRole("checkbox", { name: "Spread weekend days fairly" }), share, tolerance,
      fairness.getByRole("checkbox", { name: "Carry fairness between months" }), fairness.getByLabelText("Most a month's share can move (shifts)"), carryCap]
    for (const [before, after] of order.slice(0, -1).map((field, index) => [field, order[index + 1]]))
      expect(before.compareDocumentPosition(after)).toBe(Node.DOCUMENT_POSITION_FOLLOWING)

    await user.click(share)
    await user.clear(tolerance)
    await user.type(tolerance, "8")
    await user.clear(carryCap)
    await user.type(carryCap, "744")
    await user.click(screen.getByRole("button", { name: "Save" }))

    await screen.findByText("Saved.")
    expect(put(calls).rules).toEqual({ ...UNIT_RULES, share_overtime: false, overtime_tolerance: 8, overtime_carry_cap: 744 })
  })

  it.each([["745", "Between 0 and 744."], ["2.5", "Enter a whole number."], ["-1", "Between 0 and 744."]])(
    "refuses %s hours of overtime tolerance beside the field and sends nothing", async (typed, says) => {
      const { user, calls } = start("/unit-rules", asManager(UNIT_RULES_STATE, saves))

      const tolerance = await screen.findByLabelText("Overtime tolerance (hours)")
      await user.clear(tolerance)
      await user.type(tolerance, typed)
      await user.click(screen.getByRole("button", { name: "Save" }))

      expect(await screen.findByText(says)).toBeInTheDocument()
      expect(tolerance).toHaveAttribute("aria-invalid", "true")
      expect(tolerance).toHaveFocus()
      expect(calls.some((call) => call.method === "PUT")).toBe(false)
    })

  it("puts the API's refusal of the most an overtime share can move beside the field", async () => {
    const { user } = start("/unit-rules", asManager(UNIT_RULES_STATE, {
      "PUT /api/units/er/rules": {
        status: 422,
        body: { detail: [{ type: "less_than_equal", loc: ["body", "body", "rules", "overtime_carry_cap"], msg: "Input should be less than or equal to 744" }] },
      },
    }))

    await screen.findByText(FLAG)
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByText("Input should be less than or equal to 744")).toBeInTheDocument()
    expect(screen.getByLabelText("Most a month's overtime share can move (hours)")).toHaveAttribute("aria-invalid", "true")
  })

  it.each([["CNO", ME_CNO], ["Deputy CNO", ME_DEPUTY]])("the %s reads the overtime share's three rules and can change none", async (_, me) => {
    start("/unit-rules", {
      ...session(me),
      "GET /api/units": { body: [ER_UNIT] },
      "GET /api/units/er/rules": { body: { ...UNIT_RULES_STATE, rules: { ...UNIT_RULES, share_overtime: false, overtime_tolerance: 8, overtime_carry_cap: 30 } } },
    })

    const fairness = within(await screen.findByRole("region", { name: "Fairness" }))
    expect(fairness.getByText("Share overtime evenly").nextElementSibling).toHaveTextContent(/^Off$/)
    expect(fairness.getByText("Overtime tolerance (hours)").nextElementSibling).toHaveTextContent(/^8$/)
    expect(fairness.getByText("Most a month's overtime share can move (hours)").nextElementSibling).toHaveTextContent(/^30$/)
    expect(fairness.getByText("Overtime is spread among the unit's nurses in proportion to the hours each owes.")).toBeInTheDocument()
    expect(fairness.queryAllByRole("spinbutton")).toEqual([])
    expect(fairness.queryAllByRole("checkbox")).toEqual([])
  })

  it("the CNO reads the most full runs beside the longest run, and not at a longest run of 1", async () => {
    const { user } = start("/unit-rules", {
      ...session(ME_CNO),
      "GET /api/units": { body: [ER_UNIT, ICU] },
      "GET /api/units/er/rules": { body: { ...UNIT_RULES_STATE, rules: { ...UNIT_RULES, max_full_runs: 2 } } },
      "GET /api/units/icu/rules": { body: { ...UNIT_RULES_STATE, rules: { ...UNIT_RULES, max_consecutive_shifts: 1, score_target: 0.42 } } },
    })

    const runs = within(await screen.findByRole("region", { name: "Runs of shifts" }))
    expect(runs.getByText("Most full runs in a month").nextElementSibling).toHaveTextContent(/^2$/)
    expect(runs.getByText("Longest run of shifts").compareDocumentPosition(runs.getByText("Most full runs in a month")))
      .toBe(Node.DOCUMENT_POSITION_FOLLOWING)

    await user.selectOptions(screen.getByLabelText("Unit"), "icu")

    await screen.findByText("0.42")
    expect(screen.queryByText("Most full runs in a month")).not.toBeInTheDocument()
  })

  it.each([["CNO", ME_CNO], ["Deputy CNO", ME_DEPUTY]])("the %s reads a unit's skill needs and whether they are hard", async (_, me) => {
    const needs = [{ shift: "N", skill: "ICU", weekday_min: 2, weekend_min: 1 }, { shift: "D", skill: "BLS", weekday_min: 1, weekend_min: 0 }]
    {
      start("/unit-rules", {
        ...session(me),
        "GET /api/units": { body: [ER_UNIT] },
        "GET /api/units/er/rules": { body: { ...UNIT_RULES_STATE, rules: { ...UNIT_RULES, skill_needs: needs, skill_needs_hard: false } } },
      })

      const region = within(await screen.findByRole("region", { name: "Skill needs" }))
      expect(region.getAllByRole("listitem").map((item) => item.textContent)).toEqual([
        "ICU on N: at least 2 on a weekday, 1 on a weekend day", "BLS on D: at least 1 on a weekday, 0 on a weekend day"])
      expect(region.getByText("Never break the skill needs").nextElementSibling).toHaveTextContent(/^Off$/)
      expect(region.queryByRole("button")).not.toBeInTheDocument()
    }
  })

  it("a read-only unit with a gap says what is not chosen", async () => {
    const gap = { ...UNIT_RULES, shifts: [], headcounts: [], fair_shifts: [], night_shift: "", day_shift: "" }
    start("/unit-rules", { ...session(ME_DEPUTY), "GET /api/units": { body: [ER_UNIT] }, "GET /api/units/er/rules": { body: { ...UNIT_RULES_STATE, rules: gap } } })

    expect(await screen.findByText("None chosen")).toBeInTheDocument()
    expect(screen.getAllByText("Not chosen")).toHaveLength(2)
    expect(screen.getByText("No shift has a headcount.")).toBeInTheDocument()
  })

  it("a staff nurse and the System Admin have no way in", async () => {
    const { router } = start("/unit-rules", nurseHome("er"))

    await screen.findByText("No roster yet for December 2026.")
    expect(router.state.location.pathname).toBe("/")
  })

  it("the manager's home links to the form while the defaults wait", async () => {
    start("/", { ...session(ME_MANAGER), "GET /api/units": { body: [ER_UNIT] } })

    expect(await screen.findByRole("link", { name: "Review them before its first solve" })).toHaveAttribute("href", "/unit-rules")
  })
})

describe("the rostering attributes page", () => {
  const AMAL = {
    id: "n1", hospital_id: "h1", full_name: "Amal Saleh", is_active: true, gender: "F", joins_from: null, leaves_from: null,
    team_leader: true, score: 0.9, ramadan_reduced: false, breastfeeding: true, ytd_overtime: 12, hours_balance: -3, night_balance: 1.5, friday_balance: -0.5,
    overtime_balance: 6.25, allowed_shifts: ["D"], allowed_shifts_to_check: false, opening_values_locked: false, skills: [],
  }
  const BADR = {
    ...AMAL, id: "n2", hospital_id: "h2", full_name: "Badr Omar", gender: "M", joins_from: "2030-01", team_leader: false, score: 0.8, breastfeeding: false,
    ytd_overtime: 0, hours_balance: 0, night_balance: 0, friday_balance: 0, overtime_balance: 0, allowed_shifts: null, allowed_shifts_to_check: true,
  }
  const pageOf = (me: unknown, nurses: unknown[] = [AMAL, BADR], extra = {}) => ({
    ...session(me),
    "GET /api/units": { body: [ER_UNIT] },
    "GET /api/units/er/nurses": { body: nurses },
    "GET /api/units/er/rules": { body: UNIT_RULES_STATE },
    ...extra,
  })
  const put = (calls: { method: string; url: string; body: unknown }[]) => calls.find((call) => call.method === "PUT")!

  it("lists what the solver needs to know of each nurse", async () => {
    start("/rostering-attributes", pageOf(ME_MANAGER, [{ ...AMAL, skills: asManagers([BLS_HELD]) }, BADR]))

    const row = within((await screen.findByRole("cell", { name: "Amal Saleh" })).closest("tr")!)
    expect(document.title).toBe("Rostering attributes · Nurse roster")
    expect(row.getAllByRole("cell").map((cell) => cell.textContent)).toEqual(
      ["Amal Saleh", "Set attributes", "h1", "In the unit", "No", "Female", "Yes", "0.9", "D", "No", "Yes", "12", "-3", "1.5", "-0.5", "6.25", "SkillsBLSuntil 30 Jun 2027"])
    expect(screen.getByRole("columnheader", { name: "Overtime balance" })).toBeInTheDocument()
    expect(within(screen.getByRole("cell", { name: "Badr Omar" }).closest("tr")!).getByText("None")).toBeInTheDocument()
    const badr = within(screen.getByRole("cell", { name: "Badr Omar" }).closest("tr")!)
    expect(badr.getByText("Joins from January 2030")).toBeInTheDocument()
    expect(badr.getByText("Any")).toBeInTheDocument()
    expect(badr.getByText("To check")).toBeInTheDocument()
    expect(screen.getByText(/Badr Omar came with allowed shifts this unit does not work/)).toBeInTheDocument()
  })

  describe("a nurse on probation", () => {
    const PROBATION = {
      last_day: "2027-03-31", months: 3, ticked: true, ended_early: false,
      lengths: [{ months: 1, last_day: "2027-01-31" }, { months: 2, last_day: "2027-02-28" }, { months: 3, last_day: "2027-03-31" }],
      if_extended: "2027-04-30", if_ended: null, preceptors: [], preceptor_months: [],
    }
    const LAYAN = { ...AMAL, full_name: "Layan Alharbi", probation: PROBATION }
    const cellOf = (name: string) => within(screen.getByRole("cell", { name }).closest("tr")!)

    describe("her preceptor", () => {
      const MONTHS = ["2026-12", "2027-01", "2027-02", "2027-03"]
      const CHOOSING = { ...LAYAN, probation: { ...PROBATION, preceptor_months: MONTHS } }
      const PRECEPTED = {
        ...LAYAN, probation: { ...PROBATION, preceptor_months: MONTHS,
          preceptors: [{ hospital_id: "h2", full_name: "Amal Hassan", from_month: "2026-12", to_month: "2027-03" }] },
      }
      const OFFERED = [{ id: "p2", hospital_id: "h2", full_name: "Amal Hassan" }, { id: "p5", hospital_id: "h5", full_name: "Eman Noor" }]
      const choices = ({ url }: { url: string }) => {
        const month = new URL(url, "http://x").searchParams.get("month")
        return month === "2027-03"
          ? { body: { month, refusal: "No nurse of ER can precept Layan Alharbi from March 2027: none holds a skill marked \"Lets a nurse precept\", unexpired today, without being on probation or precepting another nurse then.", nurses: [] } }
          : { body: { month, refusal: null, nurses: OFFERED } }
      }

      it("shows who precepts her and over which months, and none when no one does", async () => {
        start("/rostering-attributes", pageOf(ME_MANAGER, [PRECEPTED, { ...CHOOSING, id: "n3", full_name: "Huda Saad" }]))

        const row = within((await screen.findByRole("cell", { name: "Layan Alharbi" })).closest("tr")!)
        expect(row.getByText("Preceptor: Amal Hassan,")).toBeInTheDocument()
        expect(row.getByText("December 2026 to March 2027")).toBeInTheDocument()
        expect(row.getByRole("button", { name: "Change preceptor for Layan Alharbi" })).toBeInTheDocument()
        const huda = cellOf("Huda Saad")
        expect(huda.getByText("No preceptor")).toBeInTheDocument()
        expect(huda.getByRole("button", { name: "Choose preceptor for Huda Saad" })).toBeInTheDocument()
      })

      it.each([["the CNO", ME_CNO], ["the Deputy CNO", ME_DEPUTY]])("%s reads it and cannot change it", async (_name, me) => {
        start("/rostering-attributes?unit=er", pageOf(me, [PRECEPTED]))

        const row = within((await screen.findByRole("cell", { name: "Layan Alharbi" })).closest("tr")!)
        expect(row.getByText("Preceptor: Amal Hassan,")).toBeInTheDocument()
        expect(row.queryByRole("button", { name: /preceptor/ })).not.toBeInTheDocument()
      })

      it("is chosen from a month, from the nurses the server offers then, after asking, and says so", async () => {
        const { user, calls } = start("/rostering-attributes", pageOf(ME_MANAGER, [CHOOSING], {
          "GET /api/units/er/nurses/n1/preceptor-choices": choices,
          "PUT /api/units/er/nurses/n1/preceptor": { body: PRECEPTED },
        }))

        await user.click(await screen.findByRole("button", { name: "Choose preceptor for Layan Alharbi" }))
        const question = await screen.findByRole("alertdialog", { name: "Choose a preceptor for Layan Alharbi?" })
        expect(question).toHaveTextContent("The list offers this unit's nurses who hold a skill marked “Lets a nurse precept”, unexpired today, are not on probation and precept no other nurse in those months. Both nurses are told.")
        const from = within(question).getByRole("combobox", { name: "From" })
        expect(within(from).getAllByRole("option").map((option) => option.textContent)).toEqual(["December 2026", "January 2027", "February 2027", "March 2027"])
        const who = within(question).getByRole("combobox", { name: "Preceptor" })
        await waitFor(() => expect(within(who).getAllByRole("option").map((option) => option.textContent)).toEqual(["Choose a nurse", "Amal Hassan", "Eman Noor"]))
        expect(within(question).getByRole("button", { name: "Choose preceptor" })).toHaveAttribute("aria-disabled", "true")

        await user.selectOptions(from, "2027-03")
        expect(await within(question).findByText(/No nurse of ER can precept Layan Alharbi from March 2027/)).toBeInTheDocument()
        expect(who).toBeDisabled()
        await user.selectOptions(from, "2027-01")
        await waitFor(() => expect(who).toBeEnabled())
        await user.selectOptions(who, "p2")
        await user.click(within(question).getByRole("button", { name: "Choose preceptor" }))

        expect(await screen.findByText("From January 2027, Amal Hassan precepts Layan Alharbi.")).toBeInTheDocument()
        expect(put(calls).body).toEqual({ preceptor_id: "p2", from_month: "2027-01" })
        await waitFor(() => expect(screen.getByRole("button", { name: "Set attributes of Layan Alharbi" })).toHaveFocus())
      })

      it("says a refusal from the save in the question", async () => {
        const { user } = start("/rostering-attributes", pageOf(ME_MANAGER, [PRECEPTED], {
          "GET /api/units/er/nurses/n1/preceptor-choices": choices,
          "PUT /api/units/er/nurses/n1/preceptor": { status: 422, body: { detail: [{ loc: ["body", "body", "preceptor_id"], msg: "Eman Noor already precepts Huda Saad in December 2026, and a preceptor has one nurse on probation at a time.", type: "value_error" }] } },
        }))

        await user.click(await screen.findByRole("button", { name: "Change preceptor for Layan Alharbi" }))
        const question = await screen.findByRole("alertdialog", { name: "Change the preceptor of Layan Alharbi?" })
        expect(question).toHaveTextContent("Both nurses are told, and so is the nurse who precepted her.")
        const who = within(question).getByRole("combobox", { name: "Preceptor" })
        await waitFor(() => expect(who).toBeEnabled())
        await user.selectOptions(who, "p5")
        await user.click(within(question).getByRole("button", { name: "Change preceptor" }))

        expect(await within(question).findByRole("alert")).toHaveTextContent("Eman Noor already precepts Huda Saad in December 2026")
      })
    })

    it("shows the length and the last day, and her unit manager only the actions he may take", async () => {
      start("/rostering-attributes", pageOf(ME_MANAGER, [LAYAN, AMAL]))

      const row = within((await screen.findByRole("cell", { name: "Layan Alharbi" })).closest("tr")!)
      expect(row.getByText("Until 31 Mar 2027")).toBeInTheDocument()
      expect(row.getByText("3 months")).toBeInTheDocument()
      expect(row.getByRole("button", { name: "Length of Layan Alharbi's probation" })).toBeInTheDocument()
      expect(row.getByRole("button", { name: "Extend the probation of Layan Alharbi" })).toBeInTheDocument()
      expect(row.queryByRole("button", { name: /End the probation/ })).not.toBeInTheDocument()
      expect(within(cellOf("Amal Saleh").getAllByRole("cell")[4]).getByText("No")).toBeInTheDocument()
    })

    it.each([["the CNO", ME_CNO], ["the Deputy CNO", ME_DEPUTY]])("%s reads it and has no probation action", async (_name, me) => {
      start("/rostering-attributes?unit=er", pageOf(me, [{ ...LAYAN, probation: { ...PROBATION, ended_early: true, lengths: [], if_extended: null } }]))

      const row = within((await screen.findByRole("cell", { name: "Layan Alharbi" })).closest("tr")!)
      expect(row.getByText("Until 31 Mar 2027")).toBeInTheDocument()
      expect(row.getByText("Ended early")).toBeInTheDocument()
      // The Deputy CNO's one button on the row is her skills'; the probation's are the unit manager's alone.
      expect(row.queryAllByRole("button").map((button) => button.getAttribute("aria-label")))
        .toEqual(me === ME_DEPUTY ? ["Skills of Layan Alharbi"] : [])
    })

    it("gives the Deputy CNO, who records skills on the row, none of the probation's actions her manager has", async () => {
      start("/rostering-attributes?unit=er", pageOf(ME_DEPUTY, [LAYAN]))

      const row = within((await screen.findByRole("cell", { name: "Layan Alharbi" })).closest("tr")!)
      expect(row.getByText("3 months")).toBeInTheDocument()
      expect(row.getByRole("button", { name: "Skills of Layan Alharbi" })).toBeInTheDocument()
      expect(row.queryByRole("button", { name: /probation|preceptor/i })).not.toBeInTheDocument()
    })

    it("extends it after asking, waiting on the answer, and says so with focus on her row", async () => {
      const extended = { ...LAYAN, probation: { ...PROBATION, months: 4, last_day: "2027-04-30", lengths: [], if_extended: "2027-05-31" } }
      const { user } = start("/rostering-attributes", pageOf(ME_MANAGER, [LAYAN]))
      const held = hold("POST /api/units/er/nurses/n1/probation/extend")

      await user.click(await screen.findByRole("button", { name: "Extend the probation of Layan Alharbi" }))
      const question = await screen.findByRole("alertdialog", { name: "Extend the probation of Layan Alharbi by a month?" })
      expect(question).toHaveTextContent("It then ends on 30 Apr 2027 instead of 31 Mar 2027, 4 months in all.")
      await user.click(within(question).getByRole("button", { name: "Extend by a month" }))
      await held.waitFor(1)
      expect(within(question).getByRole("button", { name: "Extend by a month" })).toHaveAttribute("aria-disabled", "true")
      await held.answer({ body: extended })

      expect(await screen.findByText("The probation of Layan Alharbi now ends on 30 Apr 2027.")).toBeInTheDocument()
      await waitFor(() => expect(screen.getByRole("button", { name: "Set attributes of Layan Alharbi" })).toHaveFocus())
    })

    it("ends it after asking", async () => {
      const ending = { ...LAYAN, probation: { ...PROBATION, if_ended: "2027-01-31" } }
      const ended = { ...LAYAN, probation: { ...PROBATION, last_day: "2027-01-31", ended_early: true, lengths: [], if_extended: null, if_ended: null } }
      const { user, calls } = start("/rostering-attributes", pageOf(ME_MANAGER, [ending], { "POST /api/units/er/nurses/n1/probation/end": { body: ended } }))

      await user.click(await screen.findByRole("button", { name: "End the probation of Layan Alharbi" }))
      const question = await screen.findByRole("alertdialog", { name: "End the probation of Layan Alharbi?" })
      expect(question).toHaveTextContent("Her last day of probation becomes 31 Jan 2027, the last day of this month, so this month keeps its roster. An ended probation cannot be extended.")
      await user.click(within(question).getByRole("button", { name: "End probation" }))

      expect(await screen.findByText("The probation of Layan Alharbi ends on 31 Jan 2027.")).toBeInTheDocument()
      expect(calls.filter((call) => call.method === "POST").map((call) => call.url)).toEqual(["/api/units/er/nurses/n1/probation/end"])
    })

    it("sets its length from the choices with their last days, and cancelling changes nothing", async () => {
      const shorter = { ...LAYAN, probation: { ...PROBATION, months: 2, last_day: "2027-02-28" } }
      const { user, calls } = start("/rostering-attributes", pageOf(ME_MANAGER, [LAYAN], { "PUT /api/units/er/nurses/n1/probation/length": { body: shorter } }))

      const opener = await screen.findByRole("button", { name: "Length of Layan Alharbi's probation" })
      await user.click(opener)
      let question = await screen.findByRole("alertdialog", { name: "How long is the probation of Layan Alharbi?" })
      await user.click(within(question).getByRole("button", { name: "Cancel" }))
      await waitFor(() => expect(opener).toHaveFocus())

      await user.click(opener)
      question = await screen.findByRole("alertdialog", { name: "How long is the probation of Layan Alharbi?" })
      expect(within(question).getByRole("radio", { name: "3 months, until 31 Mar 2027" })).toBeChecked()
      await user.click(within(question).getByRole("radio", { name: "2 months, until 28 Feb 2027" }))
      await user.click(within(question).getByRole("button", { name: "Set length" }))

      expect(await screen.findByText("The probation of Layan Alharbi is 2 months, until 28 Feb 2027.")).toBeInTheDocument()
      expect(calls.filter((call) => call.method === "PUT").map((call) => call.body)).toEqual([{ months: 2 }])
    })

    it("says in the question why it was refused", async () => {
      const { user } = start("/rostering-attributes", pageOf(ME_MANAGER, [LAYAN], {
        "POST /api/units/er/nurses/n1/probation/extend": apiRefusal(["probation"], "Layan Alharbi's probation is 6 months, the longest it can be."),
      }))

      await user.click(await screen.findByRole("button", { name: "Extend the probation of Layan Alharbi" }))
      const question = await screen.findByRole("alertdialog")
      await user.click(within(question).getByRole("button", { name: "Extend by a month" }))
      expect(await within(question).findByRole("alert")).toHaveTextContent("Layan Alharbi's probation is 6 months, the longest it can be.")
    })
  })

  it("keeps the opening values as they are once a nurse's first month has closed", async () => {
    const locked = { ...AMAL, opening_values_locked: true }
    const { user, calls } = start("/rostering-attributes", pageOf(ME_MANAGER, [locked], { "PUT /api/units/er/nurses/n1/attributes": { body: locked } }))

    await user.click(await screen.findByRole("button", { name: "Set attributes of Amal Saleh" }))
    expect(screen.getByText(/Their first month has closed, so these can no longer change/)).toBeInTheDocument()
    expect(screen.getByLabelText("Hours balance (hours)")).toHaveAttribute("readonly")
    expect(screen.getByLabelText("Overtime above or below their overtime share (hours)")).toHaveAttribute("readonly")
    await user.type(screen.getByLabelText("Hours balance (hours)"), "9")
    await user.type(screen.getByLabelText("Overtime above or below their overtime share (hours)"), "9")
    await user.click(screen.getByRole("button", { name: "Save" }))

    await screen.findByText("The rostering attributes of Amal Saleh are saved.")
    expect(put(calls).body).toMatchObject({ ytd_overtime: 12, hours_balance: -3, night_balance: 1.5, friday_balance: -0.5, overtime_balance: 6.25 })
  })

  it("saves everything, with focus returning to the row's button", async () => {
    const { user, calls } = start("/rostering-attributes", pageOf(ME_MANAGER, [AMAL], { "PUT /api/units/er/nurses/n1/attributes": { body: AMAL } }))

    const button = await screen.findByRole("button", { name: "Set attributes of Amal Saleh" })
    await user.click(button)
    expect(screen.getByRole("checkbox", { name: "Team leader" })).toHaveFocus()
    expect(screen.getByLabelText("Score")).toHaveValue(0.9)
    await user.click(screen.getByRole("checkbox", { name: "Team leader" }))
    await user.clear(screen.getByLabelText("Score"))
    await user.type(screen.getByLabelText("Score"), "0.61")
    await user.click(screen.getByRole("checkbox", { name: "N" }))
    await user.click(screen.getByRole("checkbox", { name: "Ramadan-reduced" }))
    await user.clear(screen.getByLabelText("Overtime this year (hours)"))
    await user.type(screen.getByLabelText("Overtime this year (hours)"), "40")
    await user.clear(screen.getByLabelText("Weekend fairness balance (shifts)"))
    await user.type(screen.getByLabelText("Weekend fairness balance (shifts)"), "-2.5")
    const overtime = screen.getByLabelText("Overtime above or below their overtime share (hours)")
    expect(overtime).toHaveValue(6.25)
    expect(overtime).toHaveAccessibleDescription("Hours of overtime above their share of the unit's overtime so far this year, or below zero, under it.")
    await user.clear(overtime)
    await user.type(overtime, "-7.5")
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByText("The rostering attributes of Amal Saleh are saved.")).toHaveAttribute("role", "status")
    expect(put(calls).url).toBe("/api/units/er/nurses/n1/attributes")
    expect(put(calls).body).toEqual({
      team_leader: false, score: 0.61, ramadan_reduced: true, breastfeeding: true, allowed_shifts: ["D", "N"], ytd_overtime: 40, hours_balance: -3, night_balance: 1.5, friday_balance: -2.5,
      overtime_balance: -7.5,
    })
    await waitFor(() => expect(screen.getByRole("button", { name: "Set attributes of Amal Saleh" })).toHaveFocus())
  })

  it("offers only the unit's shifts, and ticking none means any", async () => {
    const { user, calls } = start("/rostering-attributes", pageOf(ME_MANAGER, [AMAL], { "PUT /api/units/er/nurses/n1/attributes": { body: AMAL } }))

    await user.click(await screen.findByRole("button", { name: "Set attributes of Amal Saleh" }))
    const shifts = within(screen.getByRole("group", { name: "Allowed shifts" }))
    expect(shifts.getAllByRole("checkbox").map((box) => box.closest("label")!.textContent)).toEqual(["D", "N"])
    expect(screen.getByRole("group", { name: "Allowed shifts" })).toHaveAccessibleDescription(/Tick none for any shift/)
    await user.click(shifts.getByRole("checkbox", { name: "D" }))
    await user.click(screen.getByRole("button", { name: "Save" }))

    await screen.findByText("The rostering attributes of Amal Saleh are saved.")
    expect((put(calls).body as { allowed_shifts: unknown }).allowed_shifts).toBeNull()
  })

  it("says what is wrong before asking the API, and a refusal appears beside its field", async () => {
    const { user, calls } = start("/rostering-attributes", pageOf(ME_MANAGER, [AMAL], {
      "PUT /api/units/er/nurses/n1/attributes": apiRefusal(["allowed_shifts", 0], "A is not one of this unit's shifts."),
    }))

    await user.click(await screen.findByRole("button", { name: "Set attributes of Amal Saleh" }))
    await user.clear(screen.getByLabelText("Score"))
    await user.type(screen.getByLabelText("Score"), "2")
    await user.clear(screen.getByLabelText("Hours balance (hours)"))
    await user.type(screen.getByLabelText("Hours balance (hours)"), "1.5")
    await user.clear(screen.getByLabelText("Overtime above or below their overtime share (hours)"))
    await user.type(screen.getByLabelText("Overtime above or below their overtime share (hours)"), "1000001")
    await user.click(screen.getByRole("button", { name: "Save" }))
    expect(await screen.findByText("Between 0 and 1.")).toBeInTheDocument()
    expect(screen.getByText("Enter a whole number of hours.")).toBeInTheDocument()
    expect(screen.getByLabelText("Overtime above or below their overtime share (hours)")).toHaveAccessibleDescription(/Between -1000000 and 1000000\./)
    expect(calls.some((call) => call.method === "PUT")).toBe(false)

    await user.clear(screen.getByLabelText("Score"))
    await user.type(screen.getByLabelText("Score"), "0.5")
    await user.clear(screen.getByLabelText("Hours balance (hours)"))
    await user.type(screen.getByLabelText("Hours balance (hours)"), "1")
    await user.clear(screen.getByLabelText("Overtime above or below their overtime share (hours)"))
    await user.type(screen.getByLabelText("Overtime above or below their overtime share (hours)"), "-1000000")
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByText("A is not one of this unit's shifts.")).toBeInTheDocument()
    expect(screen.getByRole("group", { name: "Allowed shifts" })).toHaveFocus()
  })

  it("a nurse due to join shows only their allowed shifts", async () => {
    const { user, calls } = start("/rostering-attributes", pageOf(ME_MANAGER, [BADR], { "PUT /api/units/er/nurses/n2/allowed-shifts": { body: BADR } }))

    await user.click(await screen.findByRole("button", { name: "Set allowed shifts of Badr Omar" }))
    expect(screen.getByText(/joins from January 2030/)).toBeInTheDocument()
    expect(screen.queryByLabelText("Score")).not.toBeInTheDocument()
    expect(screen.getByRole("group", { name: "Allowed shifts" })).toHaveFocus()
    await user.click(screen.getByRole("checkbox", { name: "N" }))
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByText("The allowed shifts of Badr Omar are saved.")).toBeInTheDocument()
    expect(put(calls)).toMatchObject({ url: "/api/units/er/nurses/n2/allowed-shifts", body: { allowed_shifts: ["N"] } })
  })

  it("a manager setting their own is told who can see it", async () => {
    const { user } = start("/rostering-attributes", pageOf(ME_MANAGER, [{ ...AMAL, hospital_id: "mona", full_name: "Mona Saleh" }]))

    await user.click(await screen.findByRole("button", { name: "Set attributes of Mona Saleh" }))

    expect(screen.getByText(/These are your own/)).toBeInTheDocument()
  })

  it("cancelling returns focus to the row", async () => {
    const { user } = start("/rostering-attributes", pageOf(ME_MANAGER, [AMAL]))

    await user.click(await screen.findByRole("button", { name: "Set attributes of Amal Saleh" }))
    await user.click(screen.getByRole("button", { name: "Cancel" }))

    expect(screen.getByRole("button", { name: "Set attributes of Amal Saleh" })).toHaveFocus()
    expect(screen.queryByLabelText("Score")).not.toBeInTheDocument()
  })

  it("a unit with no shifts says where to choose them", async () => {
    start("/rostering-attributes", pageOf(ME_MANAGER, [AMAL], {
      "GET /api/units/er/rules": { body: { ...UNIT_RULES_STATE, rules: { ...UNIT_RULES, shifts: [] } } },
    }))

    expect(await screen.findByRole("link", { name: "Choose them under Unit rules" })).toHaveAttribute("href", "/unit-rules")
  })

  it("a unit with no nurses says who adds them", async () => {
    start("/rostering-attributes", pageOf(ME_MANAGER, []))

    expect(await screen.findByText("ER has no nurses yet. The CNO adds them.")).toBeInTheDocument()
  })

  it("the CNO reads with no way to change anything, by keyboard too", async () => {
    start("/rostering-attributes", pageOf(ME_CNO))

    await screen.findByRole("cell", { name: "Amal Saleh" })
    expect(screen.queryByRole("button", { name: /Set |Skills of/ })).not.toBeInTheDocument()
    expect(screen.getByRole("region", { name: "Rostering attributes of ER" })).toHaveAttribute("tabindex", "0")
    expect(screen.getByText(/Only the unit's manager can change these. You record skills, certificates and licences on the Nurses page, and the Deputy CNO records them here./)).toBeInTheDocument()
    expect(screen.queryByText(/came with allowed shifts/)).not.toBeInTheDocument()
  })

  it("the Deputy CNO changes nothing but each nurse's skills, from a button on her row", async () => {
    start("/rostering-attributes", pageOf(ME_DEPUTY))

    const row = within((await screen.findByRole("cell", { name: "Amal Saleh" })).closest("tr")!)
    expect(row.getAllByRole("button").map((button) => button.getAttribute("aria-label"))).toEqual(["Skills of Amal Saleh"])
    expect(screen.queryByRole("button", { name: /Set / })).not.toBeInTheDocument()
    // The buttons scroll the table for a keyboard, so it is no region of its own to scroll.
    expect(screen.queryByRole("region", { name: "Rostering attributes of ER" })).not.toBeInTheDocument()
    expect(screen.getByText(/Only the unit's manager can change these, but for skills, certificates and licences, which you record here and the CNO on the Nurses page./)).toBeInTheDocument()
    expect(screen.queryByText(/came with allowed shifts/)).not.toBeInTheDocument()
  })

  it("gives the unit manager no skills button: the CNO and the Deputy CNO record them", async () => {
    start("/rostering-attributes", pageOf(ME_MANAGER, [{ ...AMAL, skills: asManagers([BLS_HELD]) }]))

    const row = within((await screen.findByRole("cell", { name: "Amal Saleh" })).closest("tr")!)
    expect(row.getAllByRole("button").map((button) => button.textContent)).toEqual(["Set attributes"])
    expect(screen.queryByRole("button", { name: /Skills of/ })).not.toBeInTheDocument()
    expect(screen.getByText(/skills the CNO's and the Deputy CNO's to record/)).toBeInTheDocument()
  })

  it.each([["the Deputy CNO", ME_DEPUTY], ["the CNO", ME_CNO]])("shows %s each nurse's records under the three headings, with identifiers and issue dates", async (_name, me) => {
    start("/rostering-attributes", pageOf(me, [{ ...AMAL, skills: [BLS_HELD, ICU_HELD, SCFHS_HELD, BSN_HELD] }]))

    const records = await screen.findByRole("table", { name: "Skills of Amal Saleh" })
    expect(within(records).getAllByRole("columnheader").map((head) => head.textContent)).toEqual(["Name", "Expiry", "Identifier", "Issue date"])
    expect(within(records).getAllByRole("rowgroup").slice(1).map((group) => group.getAttribute("aria-labelledby") && within(group).getAllByRole("rowheader")[0].textContent))
      .toEqual(["Skills", "Certificates", "Licences"])
    expect(within(records).getByRole("rowgroup", { name: "Licences" })).toHaveTextContent("SCFHS31 Mar 202821-RN-04121 Apr 2026")
    expect(within(records).getByRole("rowgroup", { name: "Certificates" })).toHaveTextContent("BSNDoes not expireNone15 Jun 2019")
    expect(within(records).getByRole("rowgroup", { name: "Skills" })).toHaveTextContent("BLS30 Jun 2027NoneNot recorded")
    // The CNO's page has no buttons, so its table is a region a keyboard scrolls; the Deputy CNO's buttons scroll hers.
    expect(screen.queryByRole("region", { name: "Rostering attributes of ER" })?.contains(records) ?? false).toBe(me === ME_CNO)
    expect(within(records).queryByRole("button")).not.toBeInTheDocument()
  })

  describe("the Deputy CNO's records", () => {
    const CATALOGUE = [
      { id: "s1", name: "BLS", kind: "skill", issuing_body: "", expires: true, required_to_work: true, lets_a_nurse_precept: false, active: true, renames: [] },
      { id: "s2", name: "ICU", kind: "skill", issuing_body: "", expires: false, required_to_work: false, lets_a_nurse_precept: false, active: true, renames: [] },
      { id: "s4", name: "SCFHS", kind: "licence", issuing_body: "Saudi Commission for Health Specialties", expires: true,
        required_to_work: true, lets_a_nurse_precept: false, active: true, renames: [] },
    ]
    /** The list as the API reads it after each write, so the panel is seen to take the fresh one. */
    function withRecords(first: unknown[], extra: Record<string, Handler | Answer> = {}) {
      let skills = first
      const after = (next: unknown[], answer: Answer = {}) => () => { skills = next; return { body: next, ...answer } }
      return {
        handlers: pageOf(ME_DEPUTY, [], {
          "GET /api/units/er/nurses": () => ({ body: [{ ...AMAL, skills }, BADR] }),
          "GET /api/hospital/skills": { body: CATALOGUE },
          ...extra,
        }),
        after,
      }
    }
    const openRecords = async (user: ReturnType<typeof start>["user"]) => {
      await user.click(await screen.findByRole("button", { name: "Skills of Amal Saleh" }))
      return screen.findByRole("heading", { level: 2, name: "Skills of Amal Saleh" })
    }
    const status = () => screen.getAllByRole("status").find((region) => region.textContent)

    it("opens her records above the list with focus on its heading, and Done puts focus back on her row", async () => {
      const { handlers } = withRecords([BLS_HELD])
      const { user } = start("/rostering-attributes", handlers)

      expect(await openRecords(user)).toHaveFocus()
      const panel = within(screen.getByRole("heading", { level: 2, name: "Skills of Amal Saleh" }).closest("[data-slot=card]") as HTMLElement)
      expect(panel.getAllByRole("columnheader").map((head) => head.textContent)).toEqual(["Name", "Expiry", "Identifier", "Issue date", "Actions"])
      expect(panel.getByRole("button", { name: "Renew BLS" })).toBeInTheDocument()
      expect(panel.getByRole("button", { name: "Take off BLS" })).toBeInTheDocument()
      expect(panel.getByRole("heading", { level: 3, name: "Record a skill" })).toBeInTheDocument()

      await user.click(panel.getByRole("button", { name: "Done" }))

      await waitFor(() => expect(screen.getByRole("button", { name: "Skills of Amal Saleh" })).toHaveFocus())
      expect(screen.queryByRole("heading", { level: 2, name: "Skills of Amal Saleh" })).not.toBeInTheDocument()
    })

    it("records a licence with the CNO's fields and refusals, and the row reads the fresh list", async () => {
      const { handlers, after } = withRecords([])
      const { user, calls } = start("/rostering-attributes", { ...handlers, "POST /api/nurses/n1/skills": after([SCFHS_HELD], { status: 201 }) })
      await openRecords(user)

      const skill = await screen.findByLabelText("Skill")
      expect(within(skill).getAllByRole("group").map((group) => group.getAttribute("label"))).toEqual(["Skills", "Licences"])
      await user.selectOptions(skill, "SCFHS")
      expect(screen.getByLabelText("Identifier")).toHaveAttribute("aria-required", "true")
      await user.type(screen.getByLabelText("Expiry"), "2028-03-31")
      await user.click(screen.getByRole("button", { name: "Record skill" }))
      expect(screen.getByLabelText("Identifier")).toHaveAccessibleDescription(/SCFHS is a licence, so it needs its identifier./)
      expect(calls.some((call) => call.method === "POST")).toBe(false)

      await user.type(screen.getByLabelText("Identifier"), "21-RN-0412")
      await user.type(screen.getByLabelText("Issue date (optional)"), "2026-04-01")
      calls.length = 0
      await user.click(screen.getByRole("button", { name: "Record skill" }))

      await waitFor(() => expect(status()).toHaveTextContent("SCFHS is recorded for Amal Saleh."))
      expect(calls.find((call) => call.method === "POST")?.body).toEqual(
        { skill_id: "s4", expiry: "2028-03-31", identifier: "21-RN-0412", issue_date: "2026-04-01" })
      await waitFor(() => expect(calls.some((call) => call.url === "/api/units/er/nurses")).toBe(true))
      const row = within(screen.getByRole("cell", { name: "Amal Saleh" }).closest("tr")!)
      expect(await row.findByRole("table", { name: "Skills of Amal Saleh" })).toHaveTextContent("21-RN-0412")
    })

    it("shows the API's refusal beside its field", async () => {
      const { handlers } = withRecords([], { "POST /api/nurses/n1/skills": refusal(["identifier"],
        "Badr Omar already holds SCFHS with this identifier. Check the number: each one is one nurse's.") })
      const { user } = start("/rostering-attributes", handlers)
      await openRecords(user)

      await user.selectOptions(await screen.findByLabelText("Skill"), "SCFHS")
      await user.type(screen.getByLabelText("Expiry"), "2028-03-31")
      await user.type(screen.getByLabelText("Identifier"), "21-RN-0412")
      await user.click(screen.getByRole("button", { name: "Record skill" }))

      await waitFor(() => expect(screen.getByLabelText("Identifier")).toHaveAccessibleDescription(/Badr Omar already holds SCFHS with this identifier./))
    })

    it("renews one, with the form under the table and focus back on its button", async () => {
      const { handlers, after } = withRecords([BLS_HELD])
      const { user, calls } = start("/rostering-attributes", { ...handlers, "PUT /api/nurses/n1/skills/s1": after([{ ...BLS_HELD, expiry: "2029-06-30" }]) })
      await openRecords(user)

      await user.click(screen.getByRole("button", { name: "Renew BLS" }))
      expect(screen.getByLabelText("Expiry of BLS")).toHaveFocus()
      await user.clear(screen.getByLabelText("Expiry of BLS"))
      await user.type(screen.getByLabelText("Expiry of BLS"), "2029-06-30")
      await user.click(screen.getByRole("button", { name: "Save BLS" }))

      await waitFor(() => expect(status()).toHaveTextContent("BLS of Amal Saleh now counts until 30 Jun 2029."))
      expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ expiry: "2029-06-30", identifier: null, issue_date: null })
      await waitFor(() => expect(screen.getByRole("button", { name: "Renew BLS" })).toHaveFocus())
    })

    it("takes one off, and says so when the page was out of date", async () => {
      const { handlers, after } = withRecords([BLS_HELD, ICU_HELD], {
        "POST /api/nurses/n1/skills/s2/remove": { status: 409, body: { detail: "Amal Saleh does not hold this skill now. Reload the page to see their skills as they are." } },
      })
      const { user } = start("/rostering-attributes", { ...handlers, "POST /api/nurses/n1/skills/s1/remove": after([ICU_HELD]) })
      await openRecords(user)

      await user.click(screen.getByRole("button", { name: "Take off BLS" }))
      await waitFor(() => expect(status()).toHaveTextContent("BLS is taken off Amal Saleh."))
      await waitFor(() => expect(screen.getByRole("heading", { level: 3, name: "Record a skill" })).toHaveFocus())

      await user.click(screen.getByRole("button", { name: "Take off ICU" }))
      expect(await screen.findByRole("alert")).toHaveTextContent("Amal Saleh does not hold this skill now.")
    })

    it("says who fills an empty catalogue, with no link to a page that is not hers to change", async () => {
      const { handlers } = withRecords([], { "GET /api/hospital/skills": { body: [] } })
      const { user } = start("/rostering-attributes", handlers)
      await openRecords(user)

      expect(await screen.findByText("The skill catalogue is empty. The CNO adds skills, certificates and licences to it under Skills.")).toBeInTheDocument()
      expect(screen.queryByRole("link", { name: /Add skills/ })).not.toBeInTheDocument()
    })
  })

  it("shows a manager his nurses' records by kind, name and expiry, and no identifier", async () => {
    start("/rostering-attributes", pageOf(ME_MANAGER, [{ ...AMAL, skills: asManagers([SCFHS_HELD, BLS_HELD]) }]))

    const row = within((await screen.findByRole("cell", { name: "Amal Saleh" })).closest("tr")!)
    expect(row.getByRole("list", { name: "Licences" })).toHaveTextContent("SCFHSuntil 31 Mar 2028")
    expect(row.getByRole("list", { name: "Skills" })).toHaveTextContent("BLSuntil 30 Jun 2027")
    expect(screen.queryByRole("table", { name: "Skills of Amal Saleh" })).not.toBeInTheDocument()
    expect(screen.queryByRole("columnheader", { name: "Identifier" })).not.toBeInTheDocument()
  })

  it("a staff nurse has no way in", async () => {
    const { router } = start("/rostering-attributes", nurseHome("er"))

    await screen.findByText("No roster yet for December 2026.")
    expect(router.state.location.pathname).toBe("/")
  })
})

describe("the leave pages", () => {
  const AMAL = { id: "n1", hospital_id: "h1", full_name: "Amal Saleh" }
  const BADR = { id: "n2", hospital_id: "h2", full_name: "Badr Omar" }
  const SICK = { id: "sick", name: "Sick", active: true }
  const OLD = { id: "old", name: "Study", active: false }
  const ANNUAL = { id: "annual", name: "Annual", active: true }
  const RUN = { leave_ids: ["l1", "l2"], nurse: AMAL, leave_type: SICK, first: "2026-11-10", last: "2026-11-11", days: 2, outside_dates: false }
  const SAVED = { ...RUN, pins_removed: [], shifts_taken_off: [] }
  const BADR_RUN = { leave_ids: ["l3"], nurse: BADR, leave_type: OLD, first: "2026-11-20", last: "2026-11-20", days: 1, outside_dates: true }
  const MONTH = { month: "2026-11", nurses: [AMAL, BADR], runs: [RUN, BADR_RUN] }
  const pageOf = (extra = {}) => ({
    ...session(ME_MANAGER),
    "GET /api/units": { body: [ER_UNIT] },
    "GET /api/units/er/leave": { body: MONTH },
    "GET /api/hospital/leave-types": { body: [ANNUAL, SICK, OLD] },
    ...extra,
  })
  const refused = (field: string, msg: string) => ({ status: 422, body: { detail: [{ loc: ["body", "body", field], msg }] } })

  it("says a closed month's leave is taken in by closing it again, and when it is fixed for good", async () => {
    start("/leave?month=2026-11", pageOf({ "GET /api/units/er/leave": { body: { ...MONTH, closed: "closed" } } }))
    expect(await screen.findByText("November 2026 has closed. Leave recorded or changed here is taken in when you close it again on the Roster page, which can be done until December 2026 closes.")).toBeInTheDocument()
    cleanup()

    start("/leave?month=2026-11", pageOf({ "GET /api/units/er/leave": { body: { ...MONTH, closed: "fixed" } } }))
    expect(await screen.findByText("November 2026 has closed, and so has the month after it, so its leave can no longer change.")).toBeInTheDocument()
  })

  it("lists the unit's month with what a manager can do to each leave", async () => {
    start("/leave", pageOf())

    const row = within((await screen.findByRole("link", { name: "Amal Saleh" })).closest("tr")!)
    expect(document.title).toBe("Leave · Nurse roster")
    expect(screen.getByRole("heading", { name: "November 2026" })).toBeInTheDocument()
    expect(row.getAllByRole("cell").map((cell) => cell.textContent).slice(0, 4)).toEqual(["Amal Saleh", "10 to 11 Nov 2026", "2", "Sick"])
    expect(row.getByRole("button", { name: "Change leave of Amal Saleh, 10 to 11 Nov 2026" })).toBeInTheDocument()
    expect(row.getByRole("button", { name: "Remove leave of Amal Saleh, 10 to 11 Nov 2026" })).toBeInTheDocument()
    expect(screen.getByText("Outside their dates")).toBeInTheDocument()
    expect(screen.getByText(/left the days marked Outside their dates/)).toBeInTheDocument()
  })

  it("moves by month in the address, keeping the nurse", async () => {
    const { router, user, calls } = start("/leave?month=2026-11&nurse=n1", pageOf())

    await user.click(await screen.findByRole("link", { name: "Next month" }))

    await waitFor(() => expect(router.state.location.search).toEqual({ month: "2026-12", nurse: "n1" }))
    expect(calls.some((call) => call.url.includes("/api/units/er/leave?month=2026-12"))).toBe(true)
  })

  it("keeps focus on the link while the next month loads", async () => {
    const answer = ({ url }: { url: string }) => ({ body: { ...MONTH, month: new URL(url, "http://here").searchParams.get("month") } })
    const { user } = start("/leave?month=2026-11", pageOf({ "GET /api/units/er/leave": answer }))

    const next = await screen.findByRole("link", { name: "Next month" })
    await user.click(next)

    await waitFor(() => expect(next).toHaveAttribute("href", "/leave?month=2027-01"))
    expect(next).toHaveFocus()
  })

  it("says so when the nurse chosen is not in the unit that month, and keeps them for the next", async () => {
    start("/leave?month=2026-11&nurse=gone", pageOf())

    expect(await screen.findByText("The nurse you chose is not in ER in November 2026, so this shows everyone.")).toBeInTheDocument()
    expect(screen.getByRole("link", { name: "Next month" })).toHaveAttribute("href", "/leave?month=2026-12&nurse=gone")
    expect(screen.getByRole("link", { name: "Amal Saleh" })).toBeInTheDocument()
  })

  it("shows one nurse's month, with the count", async () => {
    const { router, user } = start("/leave?month=2026-11", pageOf())

    await user.selectOptions(await screen.findByLabelText("Show"), "n1")

    expect(await screen.findByText("Amal Saleh has 2 days of leave in November 2026.")).toBeInTheDocument()
    expect(screen.queryByText("Badr Omar", { selector: "a" })).not.toBeInTheDocument()
    expect(screen.queryByRole("columnheader", { name: "Nurse" })).not.toBeInTheDocument()
    expect(router.state.location.search).toEqual({ month: "2026-11", nurse: "n1" })
  })

  it("says when a month has none", async () => {
    start("/leave", pageOf({ "GET /api/units/er/leave": { body: { ...MONTH, runs: [] } } }))
    expect(await screen.findByText("No leave is recorded in November 2026.")).toBeInTheDocument()
  })

  it("says when the unit has nobody that month", async () => {
    start("/leave", pageOf({ "GET /api/units/er/leave": { body: { month: "2026-11", nurses: [], runs: [] } } }))
    expect(await screen.findByText("ER has no nurses in November 2026.")).toBeInTheDocument()
  })

  it("records leave, offering only active types, then returns focus", async () => {
    const { user, calls } = start("/leave", pageOf({ "POST /api/units/er/leave": { status: 201, body: SAVED } }))

    await user.click(await screen.findByRole("button", { name: "Record leave" }))
    const types = within(await screen.findByLabelText("Leave type")).getAllByRole("option").map((option) => option.textContent)
    expect(types).toEqual(["Choose a type", "Annual", "Sick"])
    expect(screen.getByLabelText("Nurse")).toHaveFocus()

    await user.selectOptions(screen.getByLabelText("Nurse"), "n1")
    await user.selectOptions(screen.getByLabelText("Leave type"), "sick")
    await user.type(screen.getByLabelText("First day"), "2026-11-10")
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByText("The leave of Amal Saleh is saved: 10 Nov 2026.")).toBeInTheDocument()
    expect(calls.find((call) => call.method === "POST")!.body).toEqual({ nurse_id: "n1", leave_type_id: "sick", first: "2026-11-10", last: "2026-11-10" })
    expect(screen.getByRole("button", { name: "Record leave" })).toHaveFocus()
  })

  it("goes to the month just saved when it is another", async () => {
    const { user, router } = start("/leave?month=2026-11", pageOf({ "POST /api/units/er/leave": { status: 201, body: SAVED } }))

    await user.click(await screen.findByRole("button", { name: "Record leave" }))
    await user.selectOptions(await screen.findByLabelText("Nurse"), "n1")
    await user.selectOptions(screen.getByLabelText("Leave type"), "sick")
    await user.type(screen.getByLabelText("First day"), "2027-01-10")
    await user.click(screen.getByRole("button", { name: "Save" }))

    await waitFor(() => expect(router.state.location.search).toMatchObject({ month: "2027-01" }))
  })

  it("asks for what is missing on the client", async () => {
    const { user, calls } = start("/leave", pageOf())

    await user.click(await screen.findByRole("button", { name: "Record leave" }))
    await user.click(await screen.findByRole("button", { name: "Save" }))

    expect(await screen.findByText("Choose a nurse.")).toBeInTheDocument()
    expect(screen.getByText("Enter the first day.")).toBeInTheDocument()
    expect(calls.some((call) => call.method === "POST")).toBe(false)
  })

  it("shows a refusal beside its field with focus on it", async () => {
    const { user } = start("/leave", pageOf({ "POST /api/units/er/leave": refused("first", "Amal Saleh is already on leave on 10 November 2026. Change that leave instead.") }))

    await user.click(await screen.findByRole("button", { name: "Record leave" }))
    await user.selectOptions(await screen.findByLabelText("Nurse"), "n1")
    await user.selectOptions(screen.getByLabelText("Leave type"), "sick")
    await user.type(screen.getByLabelText("First day"), "2026-11-10")
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByText(/already on leave on 10 November 2026/)).toBeInTheDocument()
    expect(screen.getByLabelText("First day")).toHaveFocus()
    expect(screen.getByLabelText("First day")).toHaveAttribute("aria-invalid", "true")
  })

  it("says which shifts the leave took off an approved roster, and that the nurses are told only of a change", async () => {
    const taken = { ...SAVED, shifts_taken_off: [{ date: "2026-11-10", shift: "N", version: 3 }, { date: "2026-11-11", shift: "D", version: 3 }] }
    const { user } = start("/leave", pageOf({ "POST /api/units/er/leave": { status: 201, body: taken } }))

    await user.click(await screen.findByRole("button", { name: "Record leave" }))
    await user.selectOptions(await screen.findByLabelText("Nurse"), "n1")
    await user.selectOptions(screen.getByLabelText("Leave type"), "sick")
    await user.type(screen.getByLabelText("First day"), "2026-11-10")
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByRole("status")).toHaveTextContent(
      "It took 2 shifts off the approved roster: N on 10 Nov 2026, D on 11 Nov 2026. Version 3 is now in force, and the unit's nurses are told the roster has changed, not why.")
  })

  it("links each cover the leave opened, and none for a day passed", async () => {
    const taken = { ...SAVED, shifts_taken_off: [{ date: "2026-11-06", shift: "N", version: 3, cover: null },
      { date: "2026-11-10", shift: "N", version: 3, cover: "c1" }, { date: "2026-11-11", shift: "D", version: 3, cover: "c2" }] }
    const { user } = start("/leave", pageOf({ "POST /api/units/er/leave": { status: 201, body: taken } }))

    await user.click(await screen.findByRole("button", { name: "Record leave" }))
    await user.selectOptions(await screen.findByLabelText("Nurse"), "n1")
    await user.selectOptions(screen.getByLabelText("Leave type"), "sick")
    await user.type(screen.getByLabelText("First day"), "2026-11-06")
    await user.type(screen.getByLabelText("Last day"), "2026-11-11")
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByRole("status")).toHaveTextContent(
      "It opened 2 covers for one more nurse on those shifts, not offered to anyone yet: they are linked below.")
    const links = within(screen.getByRole("list", { name: "Covers the leave opened" })).getAllByRole("link")
    expect(links.map((link) => [link.textContent, link.getAttribute("href")])).toEqual([
      ["Cover for N on 10 Nov 2026", "/unit-covers?month=2026-11&cover=c1"], ["Cover for D on 11 Nov 2026", "/unit-covers?month=2026-11&cover=c2"]])
  })

  it("says which pins on the draft the leave replaced, as leave wins over a pin", async () => {
    const replaced = { ...SAVED, pins_removed: [{ date: "2026-11-10", shift: "N", origin: "manager", roster: "draft" }, { date: "2026-11-11", shift: "", origin: "manager", roster: "draft" }] }
    const { user } = start("/leave", pageOf({ "POST /api/units/er/leave": { status: 201, body: replaced } }))

    await user.click(await screen.findByRole("button", { name: "Record leave" }))
    await user.selectOptions(await screen.findByLabelText("Nurse"), "n1")
    await user.selectOptions(screen.getByLabelText("Leave type"), "sick")
    await user.type(screen.getByLabelText("First day"), "2026-11-10")
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByRole("status")).toHaveTextContent(
      "It replaced 2 pins on the draft: N on 10 Nov 2026, a day off on 11 Nov 2026. A shift the draft still holds on a day of leave can be taken off on the Roster page, or solve again.")
  })

  it("names the roster in force, not the draft, for a pin on an approved roster, beside the shift it took off", async () => {
    const replaced = {
      ...SAVED,
      pins_removed: [{ date: "2026-11-10", shift: "N", origin: "manager", roster: "approved" }],
      shifts_taken_off: [{ date: "2026-11-10", shift: "N", version: 3 }],
    }
    const { user } = start("/leave", pageOf({ "POST /api/units/er/leave": { status: 201, body: replaced } }))

    await user.click(await screen.findByRole("button", { name: "Record leave" }))
    await user.selectOptions(await screen.findByLabelText("Nurse"), "n1")
    await user.selectOptions(screen.getByLabelText("Leave type"), "sick")
    await user.type(screen.getByLabelText("First day"), "2026-11-10")
    await user.click(screen.getByRole("button", { name: "Save" }))

    const said = await screen.findByRole("status")
    expect(said).toHaveTextContent(
      "It replaced a pin on the roster in force: N on 10 Nov 2026. It took a shift off the approved roster: N on 10 Nov 2026. Version 3 is now in force")
    expect(said).not.toHaveTextContent(/draft|solve again/)
  })

  it("says a submitted roster keeps the shift of a pin it replaced until the decision, and one on the draft as before", async () => {
    const replaced = {
      ...SAVED,
      pins_removed: [{ date: "2026-11-30", shift: "N", origin: "manager", roster: "submitted" }, { date: "2026-12-01", shift: "D", origin: "manager", roster: "draft" }],
    }
    const { user } = start("/leave", pageOf({ "POST /api/units/er/leave": { status: 201, body: replaced } }))

    await user.click(await screen.findByRole("button", { name: "Record leave" }))
    await user.selectOptions(await screen.findByLabelText("Nurse"), "n1")
    await user.selectOptions(screen.getByLabelText("Leave type"), "sick")
    await user.type(screen.getByLabelText("First day"), "2026-11-30")
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByRole("status")).toHaveTextContent(
      "It replaced a pin on the draft: D on 1 Dec 2026. It replaced a pin on the submitted roster: N on 30 Nov 2026. " +
      "A shift the draft still holds on a day of leave can be taken off on the Roster page, or solve again. " +
      "The submitted roster keeps its shift until the decision, which checks it again.")
  })

  it("says an accepted request's pin it replaced stays on record and its nurse is told", async () => {
    const replaced = { ...SAVED, pins_removed: [{ date: "2026-11-10", shift: "N", origin: "request", roster: "none" }] }
    const { user } = start("/leave", pageOf({ "POST /api/units/er/leave": { status: 201, body: replaced } }))

    await user.click(await screen.findByRole("button", { name: "Record leave" }))
    await user.selectOptions(await screen.findByLabelText("Nurse"), "n1")
    await user.selectOptions(screen.getByLabelText("Leave type"), "sick")
    await user.type(screen.getByLabelText("First day"), "2026-11-10")
    await user.click(screen.getByRole("button", { name: "Save" }))

    expect(await screen.findByRole("status")).toHaveTextContent(
      "It replaced a pin in a month with no roster yet: N on 10 Nov 2026. One was an accepted request, which stays on record as replaced by leave; its nurse is told.")
    expect(screen.getByRole("status")).not.toHaveTextContent(/draft|solve again/)
  })

  it("changes leave, naming the ids it replaces, with focus on the button that stays", async () => {
    const { user, calls } = start("/leave", pageOf({ "POST /api/units/er/leave/change": { body: SAVED } }))

    await user.click(await screen.findByRole("button", { name: "Change leave of Amal Saleh, 10 to 11 Nov 2026" }))
    expect(await screen.findByRole("heading", { name: "Change leave of Amal Saleh" })).toBeInTheDocument()
    expect(screen.queryByLabelText("Nurse")).not.toBeInTheDocument()
    expect(screen.getByLabelText("First day")).toHaveValue("2026-11-10")
    expect(screen.getByLabelText("Last day")).toHaveValue("2026-11-11")
    await waitFor(() => expect(screen.getByLabelText("Leave type")).toHaveValue("sick"))

    await user.selectOptions(screen.getByLabelText("Leave type"), "annual")
    await user.click(screen.getByRole("button", { name: "Save" }))

    await screen.findByText("The leave of Amal Saleh is saved: 10 to 11 Nov 2026.")
    expect(calls.find((call) => call.url.endsWith("/change"))!.body).toEqual({ leave_ids: ["l1", "l2"], leave_type_id: "annual", first: "2026-11-10", last: "2026-11-11" })
    expect(screen.getByRole("button", { name: "Record leave" })).toHaveFocus()
  })

  it("lets leave of a retired type keep it and offers no other retired type", async () => {
    const { user } = start("/leave", pageOf())

    await user.click(await screen.findByRole("button", { name: "Change leave of Badr Omar, 20 Nov 2026" }))

    await waitFor(() => expect(screen.getByLabelText("Leave type")).toHaveValue("old"))
    expect(within(screen.getByLabelText("Leave type")).getByRole("option", { name: "Study (retired)" })).toBeInTheDocument()
  })

  it("says so when the leave has changed since the page opened", async () => {
    const stale = "This leave has changed since the page was opened. Reload the page to see it as it is."
    const { user } = start("/leave", pageOf({ "POST /api/units/er/leave/change": { status: 409, body: { detail: stale } } }))

    await user.click(await screen.findByRole("button", { name: "Change leave of Amal Saleh, 10 to 11 Nov 2026" }))
    await user.click(await screen.findByRole("button", { name: "Save" }))

    expect((await screen.findByRole("alert")).textContent).toBe(stale)
  })

  it("asks before removing, then keeps focus on the page", async () => {
    const { user, calls } = start("/leave", pageOf({ "POST /api/units/er/leave/remove": { status: 204 } }))

    await user.click(await screen.findByRole("button", { name: "Remove leave of Amal Saleh, 10 to 11 Nov 2026" }))
    expect(await screen.findByText("Remove this leave of Amal Saleh?")).toBeInTheDocument()
    expect(calls.some((call) => call.url.endsWith("/remove"))).toBe(false)

    await user.click(screen.getByRole("button", { name: "Remove leave" }))

    expect(await screen.findByText("The leave of Amal Saleh is removed: 10 to 11 Nov 2026.")).toBeInTheDocument()
    expect(calls.find((call) => call.url.endsWith("/remove"))!.body).toEqual({ leave_ids: ["l1", "l2"] })
    await waitFor(() => expect(screen.getByRole("button", { name: "Record leave" })).toHaveFocus())
  })

  it("tells a manager with no unit where their own leave is", async () => {
    start("/leave", { ...session(ME_MANAGER), "GET /api/units": { body: [] } })

    expect(await screen.findByRole("link", { name: "My month" })).toHaveAttribute("href", "/my-month")
    expect(screen.getByText(/No unit is assigned to you yet/)).toBeInTheDocument()
  })

  describe.each([["the CNO", ME_CNO], ["the Deputy CNO", ME_DEPUTY]])("%s", (_name, me) => {
    const ICU_UNIT = { ...ER_UNIT, id: "icu", name: "ICU" }
    const readerPage = (extra = {}) => pageOf({
      ...session(me),
      "GET /api/units": { body: [ER_UNIT, ICU_UNIT] },
      "GET /api/units/icu/leave": { body: { month: "2026-11", nurses: [BADR], runs: [{ ...BADR_RUN, outside_dates: false, leave_type: ANNUAL }] } },
      ...extra,
    })

    it("reads a unit's month with its types and no way to change anything", async () => {
      const { calls } = start("/leave", readerPage())

      const row = within((await screen.findByRole("link", { name: "Amal Saleh" })).closest("tr")!)
      expect(row.getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["Amal Saleh", "10 to 11 Nov 2026", "2", "Sick"])
      expect(screen.getByRole("heading", { level: 1, name: "Leave" })).toBeInTheDocument()
      expect(screen.getByLabelText("Unit")).toHaveValue("er")
      expect(screen.getByText(/Only the unit's manager can change it/)).toBeInTheDocument()
      expect(within(screen.getByRole("main")).queryAllByRole("button")).toEqual([])
      expect(document.querySelector("form")).toBeNull()
      expect(screen.queryByText(/Actions/)).not.toBeInTheDocument()
      expect(screen.getByRole("region", { name: "Leave in ER, November 2026" })).toHaveAttribute("tabindex", "0")
      expect(screen.getByText(/The unit's manager removes them/)).toBeInTheDocument()
      expect(calls.some((call) => call.url.includes("leave-types"))).toBe(false)
      expect(calls.every((call) => call.method === "GET")).toBe(true)
    })

    it("chooses a unit, keeping the month and letting go of the nurse", async () => {
      const { router, user, calls } = start("/leave?month=2026-11&nurse=n1", readerPage())

      expect(await screen.findByText("Amal Saleh has 2 days of leave in November 2026.")).toBeInTheDocument()
      await user.selectOptions(screen.getByLabelText("Unit"), "icu")

      expect(await screen.findByRole("link", { name: "Badr Omar" })).toBeInTheDocument()
      expect(screen.getByLabelText("Unit")).toHaveFocus()
      expect(screen.getByRole("heading", { name: "November 2026" })).toBeInTheDocument()
      expect(router.state.location.search).toEqual({ unit: "icu", month: "2026-11" })
      expect(calls.some((call) => call.url.includes("/api/units/icu/leave?month=2026-11"))).toBe(true)
    })

    it("keeps the unit through a month and a nurse's month", async () => {
      const { router, user, calls } = start("/leave?unit=icu&month=2026-11", readerPage({ "GET /api/units/icu/leave": { body: { month: "2026-11", nurses: [BADR], runs: [BADR_RUN] } } }))

      await user.click(await screen.findByRole("link", { name: "Badr Omar" }))
      expect(await screen.findByText("Badr Omar has 1 day of leave in November 2026.")).toBeInTheDocument()
      expect(router.state.location.search).toEqual({ unit: "icu", month: "2026-11", nurse: "n2" })
      expect(within(screen.getByRole("main")).queryAllByRole("button")).toEqual([])

      await user.click(screen.getByRole("link", { name: "Next month" }))
      await waitFor(() => expect(router.state.location.search).toEqual({ unit: "icu", month: "2026-12", nurse: "n2" }))
      expect(calls.some((call) => call.url.includes("/api/units/icu/leave?month=2026-12"))).toBe(true)
      expect(calls.some((call) => call.url.includes("/api/units/er/leave"))).toBe(false)
    })

    it("says so when there are no units, and offers them no leave of their own", async () => {
      start("/leave", readerPage({ "GET /api/units": { body: [] } }))

      expect(await screen.findByText("There are no units yet.")).toBeInTheDocument()
      expect(screen.queryByRole("link", { name: "My leave" })).not.toBeInTheDocument()
    })

    it("says so when the units cannot be read", async () => {
      start("/leave", readerPage({ "GET /api/units": { status: 500 } }))

      expect(await screen.findByRole("alert")).toHaveTextContent("The units could not be read.")
    })
  })

  it("tells a manager who else can see a type, and offers them no unit to choose", async () => {
    start("/leave", pageOf())

    expect(await screen.findByText(/Only you, the nurse, the CNO and the Deputy CNO can see a leave's type/)).toBeInTheDocument()
    expect(screen.queryByLabelText("Unit")).not.toBeInTheDocument()
  })

  it("the System Admin has no way in", async () => {
    const { router } = start("/leave", admin())

    await screen.findByRole("heading", { name: "Health" })
    expect(router.state.location.pathname).not.toBe("/leave")
  })

  it.each(["/leave"])("%s has no way in for a staff nurse", async (path) => {
    const { router } = start(path, nurseHome("er"))

    await screen.findByText("No roster yet for December 2026.")
    expect(router.state.location.pathname).toBe("/")
  })

  it("a staff nurse's home and header lead to their own leave", async () => {
    start("/", nurseHome("er"))

    await screen.findByText("No roster yet for December 2026.")
    expect(within(screen.getByRole("navigation", { name: "Main" })).getByRole("link", { name: "My month" })).toHaveAttribute("href", "/my-month")
    expect(within(screen.getByRole("main")).getByRole("link", { name: "My month" })).toHaveAttribute("href", "/my-month")
  })

  it("a nurse reads their own leave with its type", async () => {
    const { user, router } = start("/my-month", {
      ...session(NURSE), ...NO_REQUESTS,
      "GET /api/rosters/mine/month": { body: NO_HOURS_YET },
      "GET /api/nurses/my-skills": { body: [] }, "GET /api/nurses/my-leave": { body: { month: "2026-11", runs: [{ first: "2026-11-10", last: "2026-11-12", days: 3, leave_type: "Annual" }] } },
    })

    const row = within((await screen.findByRole("cell", { name: "Annual" })).closest("tr")!)
    expect(document.title).toBe("My month · Nurse roster")
    expect(row.getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["10 to 12 Nov 2026", "3", "Annual"])
    expect(screen.getByText(/Only you, your manager, the CNO and the Deputy CNO can see your leave's type: colleagues see a day off/)).toBeInTheDocument()

    await user.click(screen.getByRole("link", { name: "Previous month" }))
    await waitFor(() => expect(router.state.location.search).toEqual({ month: "2026-10" }))
  })

  it("the audit log names what was done to leave", async () => {
    const entry = (id: number, action: string) => ({ id, action, actor: "mona", target: "h1", at: "2026-11-05T07:00:00+03:00", ip: null, before: null, after: null })
    start("/audit-log", admin({
      "GET /api/audit/entries": { body: { entries: [entry(1, "leave.recorded"), entry(2, "leave.changed"), entry(3, "leave.removed")], next_before: null } },
      "GET /api/audit/filter-options": { body: { users: [], actions: [] } },
    }))

    expect(await screen.findByText("Recorded a nurse's leave")).toBeInTheDocument()
    expect(screen.getByText("Changed a nurse's leave")).toBeInTheDocument()
    expect(screen.getByText("Removed a nurse's leave")).toBeInTheDocument()
  })

  it("a nurse with none in a month is told so", async () => {
    start("/my-month", { ...session(NURSE), ...NO_REQUESTS, "GET /api/rosters/mine/month": { body: NO_HOURS_YET }, "GET /api/nurses/my-skills": { body: [] }, "GET /api/nurses/my-leave": { body: { month: "2026-11", runs: [] } } })

    expect(await screen.findByText("You have no leave recorded in November 2026.")).toBeInTheDocument()
  })

  it("a failed read says so", async () => {
    start("/my-month", { ...session(NURSE), ...NO_REQUESTS, "GET /api/rosters/mine/month": { body: NO_HOURS_YET }, "GET /api/nurses/my-skills": { body: [] }, "GET /api/nurses/my-leave": { status: 500 } })

    expect((await screen.findByRole("alert")).textContent).toMatch(/leave could not be read/)
  })
})

describe("the roster page", () => {
  const MANAGER = { ...ME, username: "mona", full_name: "Mona Ali", role: "unit_manager" }
  const UNIT = "/api/units/u1"
  const SOLVE_ID = "019a0000-0000-7000-8000-00000000abcd"
  const PREVIEW = {
    month: "2026-06",
    summary: { nurses: 4, leave_days: 3, outside_days: 2, pins: 0 },
    warnings: { hospital_rules_need_review: false, unit_rules_need_review: false, unreviewed_scores: 0, left_out: [] },
    precheck: [],
    rules_refusal: null,
    refusal: null,
    time_limit_default: 60,
    time_limit_cap: 300,
  }
  const QUEUED = {
    id: SOLVE_ID, month: "2026-06", state: "queued", queue_ahead: 2, time_limit_seconds: 60, stop_requested: false, elapsed_seconds: 0,
    best_cost: null, improvements: 0, attempts: 0, requested_at: "2026-05-15T10:00:00+03:00", finished_at: null, failure: null, error: "",
    found_roster: false, message: "", precheck: [],
  }
  const RUNNING = { ...QUEUED, state: "running", queue_ahead: null, elapsed_seconds: 12.4, best_cost: 30, improvements: 4, attempts: 1 }
  const DONE = { ...RUNNING, state: "succeeded", best_cost: 12, found_roster: true, finished_at: "2026-05-15T10:01:00+03:00" }
  const SHIFTS = [
    { code: "D", display_name: "Day", colour: "#fde047", hours: 12 },
    { code: "N", display_name: "Night", colour: "#4338ca", hours: 12 },
  ]
  const DAYS = [
    { date: "2026-06-01", weekend: false, holiday: false },
    { date: "2026-06-05", weekend: true, holiday: false },
    { date: "2026-06-06", weekend: false, holiday: true },
    { date: "2026-06-07", weekend: false, holiday: false },
  ]
  const EDITOR = {
    refusal: null,
    shifts: [...SHIFTS, { code: "E", display_name: "Evening", colour: "#16a34a", hours: 8 }],
    nurses: [{ hospital_id: "h1", allowed_shifts: null, credited: ["2026-06-06", "2026-06-07"] }, { hospital_id: "h2", allowed_shifts: ["N"], credited: [] }],
    gone: [] as string[],
  }
  const DRAFT = {
    solve_id: SOLVE_ID, solved_at: "2026-05-15T10:01:00+03:00", restored_at: null, cost: 12, stopped_early: false, days: DAYS, shifts: SHIFTS, changes: [],
    edited: false, solved_cost: 12, check: null, pins: [] as object[], pins_lost_to_leave: [] as object[], editor: EDITOR as object,
    rows: [{
      hospital_id: "h1", full_name: "Amal Saleh",
      cells: [{ shift: "D", kind: "shift" }, { shift: "", kind: "off" }, { shift: "", kind: "leave" }, { shift: "", kind: "outside" }],
    }, { hospital_id: "h2", full_name: "Badr Noor", cells: DAYS.map(() => ({ shift: "N", kind: "shift" })) }],
  }
  const ROW = {
    id: SOLVE_ID, month: "2026-06", state: "succeeded", failure: null, requested_by: "Mona Ali", requested_at: "2026-05-15T10:00:00+03:00",
    finished_at: "2026-05-15T10:01:03+03:00", elapsed_seconds: 62.5, time_limit_seconds: 60, attempts: 1, cost: 12,
    found_roster: true, in_draft: true,
  }
  const STATISTICS = {
    status: "FEASIBLE", stopped_by: "time_limit", objective: 12, best_bound: 3, solve_seconds: 60.1, build_seconds: 0.07, deterministic_time: 11.3,
    improvements: 4, workers: 8, time_limit_seconds: 60, variables: 5169, constraints: 10218, objective_terms: 300,
  }
  const RESULT = {
    solve: ROW, error: "", message: "", precheck: [], violations: [], elapsed_violations: [], unit_cost: 2,
    rules: [
      {
        rule: "night_rest", penalty: 9, weight: 3, amount: 3, counted_in: "day shift too soon after a night",
        largest: [{ hospital_id: "h1", full_name: "Amal Saleh", date: "2026-06-05", amount: 2, text: "night rest: h1 has too little rest" }], others: 4,
      },
      { rule: "fair_day", penalty: 5, weight: null, amount: null, counted_in: null, largest: [], others: 0 },
    ],
    explained: true, following: [],
    nurses: [{ hospital_id: "h1", full_name: "Amal Saleh", contract: 192, leave_credit: 24, required: 168, hours_worked: 180, overtime: 12, shortfall: 0, shifts: { D: 15, N: 0 }, cost: 3 }],
    statistics: STATISTICS, grid: { days: DAYS, rows: DRAFT.rows, shifts: SHIFTS }, restore_refusal: "This result is the draft already.",
  }
  const NOT_THEIRS = "Only the CNO or the Deputy CNO approve or reject a roster."
  const EMPTY = {
    month: "2026-06", state: "none", submitted: null, last_rejection: null, approved: null, submit_refusal: "This month has no roster yet.",
    approve_refusal: NOT_THEIRS, reject_refusal: NOT_THEIRS, active_solve: null, last_solve: null, draft: null,
  }
  /** A month with a draft that can be submitted. */
  const DRAFTED = { ...EMPTY, state: "draft", submit_refusal: null }
  const OPENING = { hours_balance: 0, ytd_overtime: 0, night_balance: 0, friday_balance: 0, source: "opening", out_of: null }
  /** The month's carry-over before anything has closed: every nurse starts from their opening values. */
  const NOT_CLOSED = {
    month: "2026-06", closed: null, close_refusal: "June 2026 is not over yet, so it cannot close.",
    carry_over: [{ hospital_id: "h1", full_name: "Amal Saleh", into: OPENING, out: null }],
  }

  const manager = (extra = {}) => ({
    ...session(MANAGER),
    "GET /api/units": { body: [{ id: "u1", name: "ER", archived: false, manager: null, rules_need_review: false }] },
    [`GET ${UNIT}/roster`]: { body: EMPTY },
    [`GET ${UNIT}/solves/preview`]: { body: PREVIEW },
    [`GET ${UNIT}/solve-history/${SOLVE_ID}`]: { body: RESULT },
    [`GET ${UNIT}/roster/versions`]: { body: [] },
    [`GET ${UNIT}/close`]: { body: NOT_CLOSED },
    ...extra,
  })

  it("is for a unit's manager", async () => {
    const { router } = start("/roster", nurseHome("u1"))

    await screen.findByText("No roster yet for December 2026.")
    expect(router.state.location.pathname).toBe("/")
  })

  it("says so when no unit is assigned", async () => {
    start("/roster", manager({ "GET /api/units": { body: [] } }))

    expect(await screen.findByText(/No unit is assigned to you yet/)).toBeInTheDocument()
  })

  it("sums up the month, with the time limit at the unit's default", async () => {
    start("/roster", manager())

    await screen.findByRole("heading", { name: "What the solver will receive" })
    const summary = await screen.findByRole("region", { name: "What the solver will receive" })
    expect(await within(summary).findByText("Nurses")).toBeInTheDocument()
    expect(within(summary).getByText("Days of leave").nextElementSibling).toHaveTextContent("3")
    expect(within(summary).getByText("Pinned cells").nextElementSibling).toHaveTextContent("0")
    expect(within(summary).getByLabelText("Time limit (seconds)")).toHaveValue(60)
    expect(within(summary).getByText(/check found no reason/)).toBeInTheDocument()
    expect(screen.getByText("No draft yet for June 2026.")).toBeInTheDocument()
  })

  it("keeps the month in the address, and asks for that month", async () => {
    const { calls, user } = start("/roster?month=2026-07", manager({
      [`GET ${UNIT}/roster`]: ({ url }: { url: string }) => ({ body: { ...EMPTY, month: url.split("month=")[1] ?? "2026-07" } }),
      [`GET ${UNIT}/solves/preview`]: ({ url }: { url: string }) => ({ body: { ...PREVIEW, month: url.split("month=")[1] } }),
    }))

    await screen.findByRole("heading", { name: "July 2026" })
    await user.click(screen.getByRole("link", { name: "Next month" }))

    await screen.findByRole("heading", { name: "August 2026" })
    await waitFor(() => expect(calls.map((call) => call.url)).toContain(`${UNIT}/solves/preview?month=2026-08`))
  })

  it("shows what the precheck found and every warning beside it, and they do not stop a solve", async () => {
    start("/roster", manager({
      [`GET ${UNIT}/solves/preview`]: {
        body: {
          ...PREVIEW, precheck: ["Too few nurses for the night shift"],
          warnings: { hospital_rules_need_review: true, unit_rules_need_review: true, unreviewed_scores: 2, left_out: ["Sara Noor"] },
        },
      },
    }))

    expect(await screen.findByText("Too few nurses for the night shift")).toBeInTheDocument()
    expect(screen.getByText(/hospital rules are still the seeded defaults/)).toBeInTheDocument()
    expect(screen.getByText(/unit rules are still the seeded defaults/)).toBeInTheDocument()
    expect(screen.getByText(/2 nurses' attributes have never been saved/)).toBeInTheDocument()
    expect(screen.getByText(/Sara Noor is left out of this solve/)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Start solve" })).toBeEnabled()
  })

  it("says when the month could not be checked", async () => {
    start("/roster", manager({ [`GET ${UNIT}/solves/preview`]: { body: { ...PREVIEW, precheck: null } } }))

    expect(await screen.findByText(/could not be checked in advance/)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Start solve" })).toBeEnabled()
  })

  it("names who blocks a solve until their shifts are checked", async () => {
    start("/roster", manager({ [`GET ${UNIT}/solves/preview`]: { body: { ...PREVIEW, refusal: "Check the allowed shifts of Amal Saleh first." } } }))

    expect(await screen.findByText("Check the allowed shifts of Amal Saleh first.")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Start solve" })).not.toBeInTheDocument()
  })

  it("names the month that holds the unit's solve before Solve is offered", async () => {
    const held = "A solve of March 2027 is already queued or running. A unit solves one month at a time, so stop it or wait for it first."
    start("/roster", manager({ [`GET ${UNIT}/solves/preview`]: { body: { ...PREVIEW, refusal: held } } }))

    expect(await screen.findByRole("alert")).toHaveTextContent(held)
    expect(screen.queryByRole("button", { name: "Start solve" })).not.toBeInTheDocument()
  })

  it("shows rules the solver refuses as rules to review, not as a failed solve", async () => {
    start("/roster", manager({ [`GET ${UNIT}/solves/preview`]: { body: { ...PREVIEW, precheck: null, rules_refusal: "request: department: night_shift" } } }))

    expect(await screen.findByRole("link", { name: "Review the unit rules" })).toBeInTheDocument()
    expect(screen.getByText(/It says: request: department: night_shift/)).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Start solve" })).not.toBeInTheDocument()
    expect(screen.queryByText(/failed/)).not.toBeInTheDocument()
  })

  it("holds the time limit to the cap on the client, with focus on it", async () => {
    const { calls, user } = start("/roster", manager())
    const limit = await screen.findByLabelText("Time limit (seconds)")

    await user.clear(limit)
    await user.type(limit, "301")
    await user.click(screen.getByRole("button", { name: "Start solve" }))

    await waitFor(() => expect(limit).toHaveAccessibleDescription(/cap is 300 seconds/))
    expect(limit).toHaveFocus()
    expect(calls.some((call) => call.method === "POST")).toBe(false)
  })

  it("shows the API's refusal of a time limit on the field, whose cap may just have been lowered", async () => {
    const { user } = start("/roster", manager({
      [`POST ${UNIT}/solves`]: { status: 422, body: { detail: [{ loc: ["body", "body", "time_limit_seconds"], msg: "The System Admin's cap is 30 seconds." }] } },
    }))

    await user.click(await screen.findByRole("button", { name: "Start solve" }))

    await waitFor(() => expect(screen.getByLabelText("Time limit (seconds)")).toHaveAccessibleDescription(/cap is 30 seconds/))
  })

  it("shows a refusal the API explains", async () => {
    const { user } = start("/roster", manager({
      [`POST ${UNIT}/solves`]: { status: 409, body: { detail: "A solve of this month is already queued or running." } },
    }))

    await user.click(await screen.findByRole("button", { name: "Start solve" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("already queued or running")
  })

  it("goes from the queue, through the search, to the draft, as the answers change", async () => {
    let solving = false
    let progress: object = QUEUED
    const { user, calls } = start("/roster", manager({
      [`POST ${UNIT}/solves`]: () => { solving = true; return { status: 201, body: QUEUED } },
      [`GET ${UNIT}/roster`]: () => {
        if (!solving) return { body: EMPTY }
        return { body: progress === DONE ? { ...DRAFTED, draft: DRAFT } : { ...EMPTY, active_solve: QUEUED } }
      },
      [`GET ${UNIT}/solves/${SOLVE_ID}`]: () => ({ body: progress }),
    }))

    await user.click(await screen.findByRole("button", { name: "Start solve" }))

    expect(await screen.findByText(/2 solves ahead of this one/)).toBeInTheDocument()
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({ month: "2026-06", time_limit_seconds: 60 })
    await waitFor(() => expect(screen.getByRole("heading", { name: "Solving June 2026" })).toHaveFocus())

    progress = { ...QUEUED, queue_ahead: 0 }
    expect(await screen.findByText("Next in the hospital's queue.")).toBeInTheDocument()

    progress = RUNNING
    expect(await screen.findByText("Best cost so far")).toBeInTheDocument()
    expect(screen.getByText("Best cost so far").nextElementSibling).toHaveTextContent("30")
    expect(screen.getByText("Improving rosters").nextElementSibling).toHaveTextContent("4")
    expect(screen.getByText("Searched for").nextElementSibling).toHaveTextContent("12 of 60 seconds")

    progress = DONE
    expect(await screen.findByRole("region", { name: "Draft of ER, June 2026" })).toBeInTheDocument()
    expect(screen.getByRole("status")).toHaveTextContent("finished with a cost of 12")
    expect(screen.queryByRole("button", { name: /Stop/ })).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole("heading", { name: "Draft" })).toHaveFocus())
  })

  it("picks up a solve already under way when the page is opened", async () => {
    start("/roster", manager({
      [`GET ${UNIT}/roster`]: { body: { ...EMPTY, active_solve: RUNNING } },
      [`GET ${UNIT}/solves/${SOLVE_ID}`]: { body: RUNNING },
    }))

    expect(await screen.findByRole("button", { name: "Stop and keep the best roster" })).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Start solve" })).not.toBeInTheDocument()
  })

  it("stops and keeps the best roster found", async () => {
    let answer: object = RUNNING
    const stopped = { ...DONE, state: "cancelled", best_cost: 30 }
    const { user, calls } = start("/roster", manager({
      [`GET ${UNIT}/roster`]: () => ({ body: answer === stopped ? { ...EMPTY, draft: { ...DRAFT, cost: 30, stopped_early: true } } : { ...EMPTY, active_solve: RUNNING } }),
      [`GET ${UNIT}/solves/${SOLVE_ID}`]: () => ({ body: answer }),
      [`POST ${UNIT}/solves/${SOLVE_ID}/stop`]: () => {
        answer = { ...RUNNING, stop_requested: true }
        return { body: answer }
      },
    }))

    await user.click(await screen.findByRole("button", { name: "Stop and keep the best roster" }))

    expect(calls.some((call) => call.method === "POST" && call.url.endsWith("/stop"))).toBe(true)
    expect(await screen.findByRole("button", { name: "Stopping…" })).toHaveAttribute("aria-disabled", "true")

    answer = stopped
    expect(await screen.findByText(/You stopped this solve early/)).toBeInTheDocument()
    expect(screen.getByRole("status")).toHaveTextContent("stopped with a cost of 30")
  })

  it("takes a queued solve out of the queue", async () => {
    const taken = { ...QUEUED, state: "cancelled", queue_ahead: null, stop_requested: true }
    let stopped = false
    const { user } = start("/roster", manager({
      [`GET ${UNIT}/roster`]: () => ({ body: stopped ? { ...EMPTY, last_solve: taken } : { ...EMPTY, active_solve: QUEUED } }),
      [`GET ${UNIT}/solves/${SOLVE_ID}`]: { body: QUEUED },
      [`POST ${UNIT}/solves/${SOLVE_ID}/stop`]: () => { stopped = true; return { body: taken } },
    }))

    await user.click(await screen.findByRole("button", { name: "Take out of the queue" }))

    expect(await screen.findByText(/had found no roster yet/)).toBeInTheDocument()
    expect(await screen.findByRole("button", { name: "Start solve" })).toBeInTheDocument()
  })

  it("stops asking once a progress answer is a refusal", async () => {
    const { router } = start("/roster", manager({
      [`GET ${UNIT}/roster`]: { body: { ...EMPTY, active_solve: RUNNING } },
      [`GET ${UNIT}/solves/${SOLVE_ID}`]: { status: 403, body: { detail: "This is not your unit." } },
    }))

    expect(await screen.findByRole("alert")).toHaveTextContent("can no longer be read")
    // The query's own answer about its next poll, because waiting to see that none comes could only pass more easily when busy.
    const [query] = router.options.context.queryClient.getQueryCache().findAll({ queryKey: [`${UNIT}/solves/${SOLVE_ID}`] })
    const next = (query.observers[0].options as { refetchInterval: (of: typeof query) => number | false }).refetchInterval
    expect(next(query)).toBe(false)
  })

  it("moves focus from Take out of the queue to what replaces the card", async () => {
    const taken = { ...QUEUED, state: "cancelled", queue_ahead: null, stop_requested: true }
    let stopped = false
    const { user } = start("/roster", manager({
      [`GET ${UNIT}/roster`]: () => ({ body: stopped ? { ...EMPTY, last_solve: taken } : { ...EMPTY, active_solve: QUEUED } }),
      [`GET ${UNIT}/solves/${SOLVE_ID}`]: { body: QUEUED },
      [`POST ${UNIT}/solves/${SOLVE_ID}/stop`]: () => { stopped = true; return { body: taken } },
    }))

    await user.click(await screen.findByRole("button", { name: "Take out of the queue" }))

    await waitFor(() => expect(screen.getByRole("heading", { name: "What the solver will receive" })).toHaveFocus())
  })

  it("leaves focus alone when a solve ends with the manager elsewhere on the page", async () => {
    let answer: object = RUNNING
    start("/roster", manager({
      [`GET ${UNIT}/roster`]: () => ({ body: answer === DONE ? { ...DRAFTED, draft: DRAFT } : { ...EMPTY, active_solve: RUNNING } }),
      [`GET ${UNIT}/solves/${SOLVE_ID}`]: () => ({ body: answer }),
    }))
    const next = await screen.findByRole("link", { name: "Next month" })
    await screen.findByText("Best cost so far")
    next.focus()

    answer = DONE
    await screen.findByRole("heading", { name: "Draft" })

    expect(next).toHaveFocus()
  })

  it.each([
    ["a failed solve, with its reason", { ...DONE, state: "failed", failure: "solver", error: "MemoryError: out", found_roster: false }, /The solve failed.*MemoryError: out/],
    ["no roster found, with why", { ...DONE, found_roster: false, message: "No roster exists.", precheck: [] }, /found no roster.*No roster exists\./],
  ])("explains %s, and offers to run it again", async (_, last, says) => {
    start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...EMPTY, last_solve: last } } }))

    expect(await screen.findByRole("alert")).toHaveTextContent(says)
    expect(await screen.findByRole("button", { name: "Start solve" })).toBeInTheDocument()
  })

  it("shows a solve the solver refused as rules to review", async () => {
    start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...EMPTY, last_solve: { ...DONE, state: "failed", failure: "refused", error: "request: nope", found_roster: false } } } }))

    expect(await screen.findByRole("link", { name: "Review the unit rules" })).toBeInTheDocument()
    expect(screen.queryByText(/failed/)).not.toBeInTheDocument()
  })

  it("draws the grid with colours, leave, days outside the dates and days off told apart", async () => {
    const over = { ...DRAFT, editor: { ...EDITOR, refusal: "This month is over, so its draft cannot change." } }
    start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...EMPTY, draft: over } } }))

    const grid = within(await screen.findByRole("region", { name: "Draft of ER, June 2026" }))
    const amal = grid.getByRole("rowheader", { name: "Amal Saleh" }).closest("tr") as HTMLElement
    const cells = within(amal).getAllByRole("cell")
    expect(cells[0]).toHaveTextContent("D")
    expect(cells[0]).toHaveAttribute("title", "Amal Saleh, 1 June: Day")
    expect(cells[0]).toHaveStyle({ backgroundColor: "#fde047", color: "#000000" })
    expect(cells[1]).toHaveAttribute("title", "Amal Saleh, 5 June: Off")
    expect(cells[1]).toHaveTextContent("Off")
    expect(cells[2]).toHaveAttribute("title", "Amal Saleh, 6 June: Leave")
    expect(cells[2]).toHaveTextContent("L")
    expect(cells[3]).toHaveAttribute("title", "Amal Saleh, 7 June: Outside their dates")
    const badr = grid.getByRole("rowheader", { name: "Badr Noor" }).closest("tr") as HTMLElement
    expect(within(badr).getAllByRole("cell")[0]).toHaveStyle({ backgroundColor: "#4338ca", color: "#ffffff" })
    expect(grid.getByRole("columnheader", { name: "Nurse" })).toBeInTheDocument()
    expect(grid.getAllByRole("columnheader")).toHaveLength(DAYS.length + 1)
    expect(grid.getAllByRole("columnheader")[2]).toHaveTextContent("5Fri")
    expect(grid.getAllByRole("columnheader")[3]).toHaveTextContent("6Hol")
    expect(screen.getByRole("list", { name: "What the cells mean" })).toHaveTextContent(/Night, 12 hours/)
  })

  it("beside the draft, lists the penalties per rule, largest first, under the names the hospital rules give them", async () => {
    start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, draft: DRAFT } } }))

    const rules = await screen.findByRole("region", { name: "Penalties per rule" })
    expect(within(rules).getAllByRole("listitem")[0]).toHaveTextContent(/^Night rest.*9/)
    expect(within(rules).getByText("Fair share of day shifts").parentElement).toHaveTextContent("5")
    expect(within(rules).getByText(/3 × 3/)).toHaveTextContent("day shift too soon after a night")
    expect(within(rules).getByText(/Amal Saleh, 5 Jun 2026/)).toHaveTextContent("night rest: h1 has too little rest")
    expect(within(rules).getByText("and 4 more like these")).toBeInTheDocument()
    expect(within(rules).getByText("Every other soft rule costs nothing.")).toBeInTheDocument()
  })

  it("names a short skill need as the hospital rules do, its occurrences with dates", async () => {
    const short = {
      rule: "skill_need", penalty: 6, weight: 3, amount: 2, counted_in: "nurse short of a skill need on a shift-day",
      largest: [{ hospital_id: null, full_name: "", date: "2026-06-03", amount: 2, text: "skill need: 2026-06-03 D has 0 holding ICU, needs 2 (2 short)" }], others: 0,
    }
    start("/roster", manager({
      [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, draft: DRAFT } },
      [`GET ${UNIT}/solve-history/${SOLVE_ID}`]: { body: { ...RESULT, rules: [short] } },
    }))

    const rules = await screen.findByRole("region", { name: "Penalties per rule" })
    expect(within(rules).getAllByRole("listitem")[0]).toHaveTextContent(/^Short of a skill need.*6/)
    expect(within(rules).getByText(/2 × 3/)).toHaveTextContent("nurse short of a skill need on a shift-day")
    expect(within(rules).getByText(/^The unit: skill need/)).toHaveTextContent("skill need: 2026-06-03 D has 0 holding ICU, needs 2 (2 short)")
  })

  it("beside the draft, gives each nurse's hours and cost, and what the unit costs apart from them", async () => {
    start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, draft: DRAFT } } }))

    const hours = await screen.findByRole("region", { name: "Hours and cost per nurse" })
    const cells = within(within(hours).getByRole("row", { name: /Amal Saleh/ })).getAllByRole("cell").map((cell) => cell.textContent)
    expect(cells).toEqual(["192", "24", "168", "180", "12", "0", "D 15", "3"])
    expect(within(hours).getByText(/The unit as a whole costs 2 more/)).toBeInTheDocument()
  })

  it("beside the draft, gives the solver's statistics and what stopped it", async () => {
    start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, draft: DRAFT } } }))

    const statistics = await screen.findByRole("region", { name: "Solver statistics" })
    expect(within(statistics).getByText("What stopped it").nextElementSibling).toHaveTextContent("The time limit")
    expect(within(statistics).getByText("Improving rosters").nextElementSibling).toHaveTextContent("4")
    expect(within(statistics).getByText("Model variables").nextElementSibling).toHaveTextContent("5169")
  })

  it("says what the precheck warned of, and that it warned of nothing", async () => {
    const warned = { ...RESULT, precheck: ["capacity: too few"] }
    start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, draft: DRAFT } }, [`GET ${UNIT}/solve-history/${SOLVE_ID}`]: { body: warned } }))

    expect(within(await screen.findByRole("region", { name: "The solver's check" })).getByText("capacity: too few")).toBeInTheDocument()
    cleanup()

    start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, draft: DRAFT } } }))
    expect(within(await screen.findByRole("region", { name: "The solver's check" })).getByText(/warned of nothing/)).toBeInTheDocument()
  })

  it("shows recorded facts that broke rules, and only when there are some", async () => {
    const fact = { rule: "consecutive", hospital_id: "h1", full_name: "Amal Saleh", date: "2026-06-05", text: "consecutive: 6 shifts in a row" }
    start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, draft: DRAFT } }, [`GET ${UNIT}/solve-history/${SOLVE_ID}`]: { body: { ...RESULT, elapsed_violations: [fact] } } }))

    const facts = await screen.findByRole("region", { name: "Recorded facts that broke rules" })
    expect(within(facts).getByText(/Amal Saleh, 5 Jun 2026/)).toHaveTextContent("6 shifts in a row")
    expect(screen.queryByRole("region", { name: "Hard rules this roster breaks" })).not.toBeInTheDocument()
  })

  it("names a full run above the most by its weight, with each nurse in the solver's words", async () => {
    const rules = [{
      rule: "excess_full_run", penalty: 8, weight: 4, amount: 2, counted_in: "full run above the maximum",
      largest: [{ hospital_id: "h1", full_name: "Amal Saleh", date: null, amount: 2, text: "h1: 5 full runs of 3 shifts, max 3" }], others: 0,
    }]
    start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, draft: DRAFT } }, [`GET ${UNIT}/solve-history/${SOLVE_ID}`]: { body: { ...RESULT, rules } } }))

    const region = within(await screen.findByRole("region", { name: "Penalties per rule" }))
    expect(region.getByText("Full run above the most").parentElement).toHaveTextContent("8")
    expect(region.getByText(/2 × 4/)).toHaveTextContent("full run above the maximum")
    expect(region.getByText("Amal Saleh: 5 full runs of 3 shifts, max 3")).toBeInTheDocument()
  })

  it("names overtime above a fair share by its weight, with each nurse's overtime, share and hours priced in the solver's words", async () => {
    const rules = [{
      rule: "overtime_above_share", penalty: 3, weight: 1, amount: 3, counted_in: "hour of overtime above the share",
      largest: [{ hospital_id: "h1", full_name: "Amal Saleh", date: null, amount: 3, text: "h1: 20 h overtime, share 4.7 h, 3 h priced" }], others: 0,
    }]
    start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, draft: DRAFT } }, [`GET ${UNIT}/solve-history/${SOLVE_ID}`]: { body: { ...RESULT, rules } } }))

    const region = within(await screen.findByRole("region", { name: "Penalties per rule" }))
    expect(region.getByText("Overtime away from the fair share").parentElement).toHaveTextContent("3")
    expect(region.getByText(/3 × 1/)).toHaveTextContent("hour of overtime above the share")
    expect(region.getByText("Amal Saleh: 20 h overtime, share 4.7 h, 3 h priced")).toBeInTheDocument()
  })

  it("says the nurse's name once on each line of the solver's, leaving words that begin with another id as they are", async () => {
    const facts = [
      { rule: "consecutive", hospital_id: "h1", full_name: "Amal Saleh", date: null, text: "h1: 6 shifts in a row" },
      { rule: "consecutive", hospital_id: "h1", full_name: "Amal Saleh", date: null, text: "h10: 7 shifts in a row" },
      { rule: "consecutive", hospital_id: "h2", full_name: "", date: null, text: "h2: 8 shifts in a row" },
    ]
    start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, draft: DRAFT } }, [`GET ${UNIT}/solve-history/${SOLVE_ID}`]: { body: { ...RESULT, elapsed_violations: facts } } }))

    const region = within(await screen.findByRole("region", { name: "Recorded facts that broke rules" }))
    expect(region.getByText("Amal Saleh: 6 shifts in a row")).toBeInTheDocument()
    expect(region.getByText("Amal Saleh: h10: 7 shifts in a row")).toBeInTheDocument()
    expect(region.getByText("h2: 8 shifts in a row")).toBeInTheDocument()
  })

  it("does not repeat a date the solver's words already hold", async () => {
    const rules = [{ ...RESULT.rules[0], largest: [{ hospital_id: null, full_name: "", date: "2026-06-05", amount: 2, text: "2026-06-05 D: avg 0.80 above band 0.78" }] }]
    start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, draft: DRAFT } }, [`GET ${UNIT}/solve-history/${SOLVE_ID}`]: { body: { ...RESULT, rules } } }))

    expect(await screen.findByText("The unit: 2026-06-05 D: avg 0.80 above band 0.78")).toBeInTheDocument()
  })

  it("says so when the explanation cannot be worked out, and still shows the penalties", async () => {
    const rules = RESULT.rules.map((rule) => ({ ...rule, weight: null, amount: null, counted_in: null, largest: [], others: 0 }))
    start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, draft: DRAFT } }, [`GET ${UNIT}/solve-history/${SOLVE_ID}`]: { body: { ...RESULT, rules, explained: false } } }))

    const region = await screen.findByRole("region", { name: "Penalties per rule" })
    expect(within(region).getByText(/could not be explained/)).toBeInTheDocument()
    expect(within(region).getByText("Night rest")).toBeInTheDocument()
  })

  it("keeps the grid when the result's details cannot be read", async () => {
    start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, draft: DRAFT } }, [`GET ${UNIT}/solve-history/${SOLVE_ID}`]: { status: 500 } }))

    expect(await screen.findByRole("alert")).toHaveTextContent("The details of this result could not be read")
    expect(screen.getByRole("region", { name: "Draft of ER, June 2026" })).toBeInTheDocument()
  })

  it("says when the draft is a restored result, and when it was solved", async () => {
    start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...EMPTY, draft: { ...DRAFT, restored_at: "2026-05-16T09:00:00+03:00" } } } }))

    expect(await screen.findByText(/Restored 2026-05-16 09:00:00 from the solve of 2026-05-15 10:01:00\./)).toBeInTheDocument()
  })

  it("links to the month's history and to a result with no draft of its own", async () => {
    const nothing = { ...DONE, found_roster: false, best_cost: null, message: "no roster found within the time limit" }
    start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...EMPTY, last_solve: nothing } } }))

    expect(await screen.findByRole("link", { name: "Solve history of June 2026" })).toHaveAttribute("href", "/solve-history?month=2026-06")
    expect(screen.getByRole("link", { name: "Open this result" })).toHaveAttribute("href", `/solve-history/${SOLVE_ID}`)
  })

  it.each([
    ["failed", { state: "failed", failure: "solver", error: "down" }],
    ["refused", { state: "failed", failure: "refused", error: "night_shift is not a shift" }],
    ["stopped", { state: "cancelled" }],
  ])("links to a %s solve's result too", async (_name, fields) => {
    start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...EMPTY, last_solve: { ...DONE, found_roster: false, best_cost: null, ...fields } } } }))

    expect(await screen.findByRole("link", { name: "Open this result" })).toHaveAttribute("href", `/solve-history/${SOLVE_ID}`)
  })

  it("says how a roster was planned when a nurse's probation or preceptor has changed since", async () => {
    const changes = [
      { hospital_id: "h3", full_name: "Faten Ali", what: "on_probation" },
      { hospital_id: "h4", full_name: "Hind Zaki", what: "off_probation" },
      { hospital_id: "h5", full_name: "Sara Adel", what: "preceptor" },
    ]
    start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...EMPTY, draft: { ...DRAFT, changes } } } }))

    const warning = await screen.findByRole("alert")
    expect(warning).toHaveTextContent("Faten Ali: is on probation this month, but it was planned with her counted")
    expect(warning).toHaveTextContent("Hind Zaki: is no longer on probation this month, but it was planned with her on probation")
    expect(warning).toHaveTextContent("Sara Adel: has a different preceptor this month from the one it was planned with")
  })

  it("beside the draft, says which of her preceptor's day shifts a nurse on probation did not follow, and why", async () => {
    const following = [
      { hospital_id: "h3", full_name: "Faten Ali", preceptor: "h1", preceptor_name: "Amal Saleh", followed: 6,
        missed: [{ date: "2026-06-07", shift: "D", why: "leave" }, { date: "2026-06-10", shift: "D", why: "own_rules" }] },
      { hospital_id: "h4", full_name: "Hind Zaki", preceptor: null, preceptor_name: "", followed: 0, missed: [] },
    ]
    start("/roster", manager({
      [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, draft: DRAFT } },
      [`GET ${UNIT}/solve-history/${SOLVE_ID}`]: { body: { ...RESULT, following } },
    }))

    const panel = await screen.findByRole("region", { name: "Nurses on probation" })
    expect(panel).toHaveTextContent("Faten Ali works beside Amal Saleh, her preceptor, on 6 of their 8 day shifts. Not on:")
    expect(within(panel).getAllByRole("listitem").slice(1, 3).map((item) => item.textContent)).toEqual([
      "7 June, D: she was on leave or outside her dates", "10 June, D: her hours, or her own rest and run rules, held no more"])
    expect(panel).toHaveTextContent("Hind Zaki had no preceptor this month, so her day shifts were placed freely.")
  })

  it("warns of what has changed since the draft was solved", async () => {
    const changes = [
      { hospital_id: "h1", full_name: "Amal Saleh", what: "gender" },
      { hospital_id: "h2", full_name: "Badr Noor", what: "leave" },
    ]
    start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...EMPTY, draft: { ...DRAFT, changes } } } }))

    const warning = await screen.findByRole("alert")
    expect(warning).toHaveTextContent("Amal Saleh: gender was corrected")
    expect(warning).toHaveTextContent("Badr Noor: leave or dates changed")
    expect(warning).toHaveTextContent("checked again when you submit")
  })

  it("draws only the rows in view of a long grid, and says how many there are", async () => {
    const rows = Array.from({ length: 200 }, (_, index) => ({
      hospital_id: `h${index}`, full_name: `Nurse ${index}`, cells: DAYS.map(() => ({ shift: "D", kind: "shift" })),
    }))
    // jsdom lays nothing out, so the scroller and its rows are given a size.
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(33)
    vi.spyOn(Element.prototype, "clientHeight", "get").mockReturnValue(400)
    vi.spyOn(Element.prototype, "clientWidth", "get").mockReturnValue(800)
    try {
      start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...EMPTY, draft: { ...DRAFT, rows } } } }))

      const grid = within(await screen.findByRole("region", { name: "Draft of ER, June 2026" }))
      expect(await grid.findByRole("rowheader", { name: "Nurse 0" })).toBeInTheDocument()
      expect(grid.getByRole("grid")).toHaveAttribute("aria-rowcount", "201")
      expect(grid.getAllByRole("rowheader").length).toBeLessThan(100)
    } finally {
      vi.restoreAllMocks()
    }
  })

  describe("editing the draft", () => {
    const CHECK = { reason: null, ok: true, violations: [], cost: 15, cost_before: 12, delta: 3, refusals: [] }
    const cellNamed = (name: RegExp | string) => screen.findByRole("button", { name })
    const editing = (extra = {}) => manager({
      [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, draft: DRAFT } },
      [`POST ${UNIT}/roster/check`]: { body: CHECK },
      ...extra,
    })

    it("pins a shift from a cell's menu, checks the grid it would make, and saves it", async () => {
      const rows = [{ ...DRAFT.rows[0], cells: DRAFT.rows[0].cells.map((cell, index) => (index === 1 ? { shift: "N", kind: "shift" } : cell)) }, DRAFT.rows[1]]
      const saved = { ...DRAFT, rows, pins: [{ hospital_id: "h1", full_name: "Amal Saleh", date: "2026-06-05", shift: "N", origin: "manager", set_aside: null }], edited: true, cost: 15,
        check: { reason: null, ok: true, violations: [], cost: 15 } }
      const { user, calls } = start("/roster", editing({ [`POST ${UNIT}/roster/pins`]: { body: { ...EMPTY, draft: saved } } }))

      const cell = await cellNamed("Amal Saleh, 5 June: Off")
      await user.click(cell)
      const pinNight = await screen.findByRole("menuitem", { name: /Pin Night/ })
      expect(screen.queryByRole("menuitem", { name: /Open a cover/ })).not.toBeInTheDocument()  // A draft is offered to nobody.
      await user.click(pinNight)

      expect(await cellNamed("Amal Saleh, 5 June: Night, to be pinned to Night, not saved")).toHaveFocus()
      const unsaved = screen.getByRole("region", { name: "1 change not saved yet" })
      expect(await within(unsaved).findByText("With these changes the draft breaks no hard rule, and costs 15 (+3).")).toBeInTheDocument()
      expect(calls.filter((call) => call.url.endsWith("/roster/check")).at(-1)!.body).toEqual({
        month: "2026-06", edits: [{ hospital_id: "h1", date: "2026-06-05", action: "shift", shift: "N" }] })

      await user.click(within(unsaved).getByRole("button", { name: "Save changes" }))

      expect(await screen.findByText("1 change saved.")).toBeInTheDocument()
      expect(calls.find((call) => call.url.endsWith("/roster/pins"))!.body).toEqual({
        month: "2026-06", edits: [{ hospital_id: "h1", date: "2026-06-05", action: "shift", shift: "N" }] })
      expect(screen.queryByRole("region", { name: /not saved yet/ })).not.toBeInTheDocument()
      expect(await cellNamed("Amal Saleh, 5 June: Night, pinned")).toBeInTheDocument()
      await waitFor(() => expect(screen.getByRole("button", { name: "Amal Saleh, 5 June: Night, pinned" })).toHaveFocus())
      expect(screen.getByText(/Edited by hand since. Cost 15 on the month as it stands, against 12 as solved./)).toBeInTheDocument()
    })

    it("puts a nurse on probation last, under its own heading with her preceptor's name, and the arrows pass over it", async () => {
      const faten = { hospital_id: "h3", full_name: "Faten Ali", on_probation: true, preceptor_name: "Amal Saleh", cells: DAYS.map(() => ({ shift: "D", kind: "shift" })) }
      const { user } = start("/roster", editing({ [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, draft: { ...DRAFT, rows: [...DRAFT.rows, faten] } } } }))

      const grid = within(await screen.findByRole("region", { name: "Draft of ER, June 2026" }))
      const group = within(grid.getByRole("rowgroup", { name: "On probation" }))
      expect(group.getByRole("rowheader", { name: "Faten Ali, Preceptor: Amal Saleh" })).toBeInTheDocument()
      expect(group.queryByRole("rowheader", { name: "Badr Noor" })).not.toBeInTheDocument()
      expect(grid.getByRole("grid")).toHaveAttribute("aria-rowcount", "5")
      expect(screen.getByRole("list", { name: "What the cells mean" })).toHaveTextContent("On probation: works beside their preceptor and is not counted toward any shift")

      ;(await cellNamed("Badr Noor, 1 June: Night")).focus()
      await user.keyboard("{ArrowDown}")
      expect(await cellNamed("Faten Ali, 1 June: Day")).toHaveFocus()
      await user.keyboard("{ArrowUp}")
      expect(await cellNamed("Badr Noor, 1 June: Night")).toHaveFocus()
    })

    it("is one stop for Tab, moved through by the arrow keys, and Escape gives focus back to the cell", async () => {
      const { user } = start("/roster", editing())

      const first = await cellNamed("Amal Saleh, 1 June: Day")
      expect(first).toHaveAttribute("tabindex", "0")
      expect(await cellNamed("Amal Saleh, 5 June: Off")).toHaveAttribute("tabindex", "-1")
      first.focus()
      await user.keyboard("{ArrowRight}")
      expect(await cellNamed("Amal Saleh, 5 June: Off")).toHaveFocus()
      await user.keyboard("{ArrowDown}")
      expect(await cellNamed("Badr Noor, 5 June: Night")).toHaveFocus()
      await user.keyboard("{Home}")
      expect(await cellNamed("Badr Noor, 1 June: Night")).toHaveFocus()

      await user.keyboard("{Enter}")
      expect(await screen.findByRole("menu")).toBeInTheDocument()
      await user.keyboard("{Escape}")
      await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument())
      expect(await cellNamed("Badr Noor, 1 June: Night")).toHaveFocus()
    })

    it("in a long grid, moves focus to a row out of view, which is drawn first", async () => {
      const rows = Array.from({ length: 200 }, (_, index) => ({
        hospital_id: `h${index}`, full_name: `Nurse ${index}`, cells: DAYS.map(() => ({ shift: "D", kind: "shift" })),
      }))
      // jsdom lays nothing out and never scrolls, so the scroller and its rows are given a size, and a scroll that moves it.
      vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(33)
      vi.spyOn(Element.prototype, "clientHeight", "get").mockReturnValue(400)
      vi.spyOn(Element.prototype, "clientWidth", "get").mockReturnValue(800)
      vi.spyOn(Element.prototype, "scrollHeight", "get").mockReturnValue(201 * 33)
      const scrolled = new WeakMap<Element, number>()
      vi.spyOn(Element.prototype, "scrollTop", "get").mockImplementation(function (this: Element) { return scrolled.get(this) ?? 0 })
      // jsdom has no scrollTo at all.
      Element.prototype.scrollTo = function (this: Element, options?: ScrollToOptions | number) {
        scrolled.set(this, typeof options === "object" ? (options.top ?? 0) : 0)
        this.dispatchEvent(new Event("scroll"))
      } as Element["scrollTo"]
      try {
        const { user } = start("/roster", editing({ [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, draft: { ...DRAFT, rows } } } }))

        const first = await cellNamed("Nurse 0, 1 June: Day")
        expect(screen.queryByRole("button", { name: "Nurse 199, 1 June: Day" })).not.toBeInTheDocument()
        first.focus()
        // The row is drawn after the scroll, and focus moves to its cell once it is: each is waited for.
        const focused = (name: string) => waitFor(() => expect(screen.getByRole("button", { name })).toHaveFocus())
        await user.keyboard("{PageDown}")
        await focused("Nurse 199, 1 June: Day")
        await user.keyboard("{PageUp}")
        await focused("Nurse 0, 1 June: Day")
      } finally {
        vi.restoreAllMocks()
        delete (Element.prototype as Partial<Element>).scrollTo
      }
    })

    it("offers only a nurse's allowed shifts, and nothing to pin on a day of leave or outside their dates", async () => {
      const { user } = start("/roster", editing())

      await user.click(await cellNamed("Badr Noor, 1 June: Night"))
      const menu = await screen.findByRole("menu")
      expect(within(menu).getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["NPin Night", "Pin a day off"])
      await user.keyboard("{Escape}")
      await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument())

      await user.click(await cellNamed("Amal Saleh, 6 June: Leave"))
      const leave = await screen.findByRole("menu")
      expect(leave).toHaveTextContent("On leave this day, and leave wins over a pin.")
      expect(within(leave).queryByRole("menuitem", { name: /Pin/ })).not.toBeInTheDocument()
    })

    it("takes a shift off a day that became leave after the solve", async () => {
      const draft = { ...DRAFT, rows: [{ ...DRAFT.rows[0], cells: [DRAFT.rows[0].cells[0], DRAFT.rows[0].cells[1], { shift: "D", kind: "shift" }, DRAFT.rows[0].cells[3]] }, DRAFT.rows[1]] }
      const { user } = start("/roster", editing({ [`GET ${UNIT}/roster`]: { body: { ...EMPTY, draft } } }))

      await user.click(await cellNamed("Amal Saleh, 6 June: Day"))
      await user.click(await screen.findByRole("menuitem", { name: "Take the shift off this day" }))

      expect(await cellNamed(/Amal Saleh, 6 June: Day, pin to be cleared, not saved/)).toBeInTheDocument()
    })

    it("clears a pin, and choosing what is saved already is no change", async () => {
      const pinned = { ...DRAFT, pins: [{ hospital_id: "h1", full_name: "Amal Saleh", date: "2026-06-01", shift: "D", origin: "manager", set_aside: null }] }
      const { user } = start("/roster", editing({ [`GET ${UNIT}/roster`]: { body: { ...EMPTY, draft: pinned } } }))

      await user.click(await cellNamed("Amal Saleh, 1 June: Day, pinned"))
      await user.click(await screen.findByRole("menuitem", { name: /Pin Night/ }))
      expect(await screen.findByRole("region", { name: "1 change not saved yet" })).toBeInTheDocument()
      await user.click(await cellNamed(/Amal Saleh, 1 June: Night/))
      await user.click(await screen.findByRole("menuitem", { name: /Pin Day/ }))
      await waitFor(() => expect(screen.queryByRole("region", { name: /not saved yet/ })).not.toBeInTheDocument())

      await user.click(await cellNamed("Amal Saleh, 1 June: Day, pinned"))
      await user.click(await screen.findByRole("menuitem", { name: "Clear the pin" }))
      expect(await cellNamed("Amal Saleh, 1 June: Day, pin to be cleared, not saved")).toBeInTheDocument()
    })

    it("a pin from an accepted request is changed where it was accepted", async () => {
      const pinned = { ...DRAFT, pins: [{ hospital_id: "h1", full_name: "Amal Saleh", date: "2026-06-01", shift: "D", origin: "request", set_aside: null }] }
      const { user } = start("/roster", editing({ [`GET ${UNIT}/roster`]: { body: { ...EMPTY, draft: pinned } } }))

      await user.click(await cellNamed("Amal Saleh, 1 June: Day, pinned by an accepted request"))

      const menu = await screen.findByRole("menu")
      expect(menu).toHaveTextContent("Pinned by an accepted request. Change it there.")
      expect(within(menu).queryAllByRole("menuitem")).toEqual([])
    })

    it.each(["swap", "cover"])("a pin from an accepted %s is the manager's to change or clear here", async (origin) => {
      const pinned = { ...DRAFT, pins: [{ hospital_id: "h1", full_name: "Amal Saleh", date: "2026-06-01", shift: "D", origin, set_aside: null }] }
      const { user } = start("/roster", editing({ [`GET ${UNIT}/roster`]: { body: { ...EMPTY, draft: pinned } } }))

      await user.click(await cellNamed(`Amal Saleh, 1 June: Day, pinned by an accepted ${origin}`))

      const menu = await screen.findByRole("menu")
      expect(menu).not.toHaveTextContent("Change it there")
      expect(within(menu).getByRole("menuitem", { name: /Pin Night/ })).toBeInTheDocument()
      expect(within(menu).getByRole("menuitem", { name: "Pin a day off" })).toBeInTheDocument()
      await user.click(within(menu).getByRole("menuitem", { name: "Clear the pin" }))
      expect(await cellNamed(`Amal Saleh, 1 June: Day, pin to be cleared, not saved`)).toBeInTheDocument()
    })

    it("shows the answer for the edits as they are, never an older one that arrives late", async () => {
      const { user, router } = start("/roster", editing())
      const checks = hold(`POST ${UNIT}/roster/check`)

      await user.click(await cellNamed("Amal Saleh, 5 June: Off"))
      await user.click(await screen.findByRole("menuitem", { name: /Pin Night/ }))
      await checks.waitFor(1)
      await user.click(await cellNamed(/Badr Noor, 5 June/))
      await user.click(await screen.findByRole("menuitem", { name: "Pin a day off" }))
      await checks.waitFor(2)

      await checks.answerAt(1, { body: { ...CHECK, cost: 20, delta: 8 } })
      expect(await screen.findByText(/costs 20 \(\+8\)/)).toBeInTheDocument()
      await checks.answerAt(0, { body: { ...CHECK, cost: 99, delta: 87 } })

      // Proved, not waited for: no check is being fetched any more, so the older answer has been dealt with, and the page
      // still shows the newer one.
      const queries = router.options.context.queryClient.getQueryCache().findAll({ queryKey: ["checkDraftEdits"] })
      await waitFor(() => expect(queries.map((query) => query.state.fetchStatus)).toEqual(queries.map(() => "idle")))
      expect(screen.getByText(/costs 20 \(\+8\)/)).toBeInTheDocument()
      expect(screen.queryByText(/costs 99/)).not.toBeInTheDocument()
    })

    it("says so honestly when the grid cannot be checked, or the check does not answer", async () => {
      const { user } = start("/roster", editing({
        [`POST ${UNIT}/roster/check`]: ({ body }: { body: unknown }) => (body as { edits: unknown[] }).edits.length === 1
          ? { body: { ...CHECK, reason: "roster: unknown shift 'X'", ok: null, cost: null, delta: null, cost_before: null } }
          : { status: 502 },
      }))

      await user.click(await cellNamed("Amal Saleh, 5 June: Off"))
      await user.click(await screen.findByRole("menuitem", { name: /Pin Night/ }))
      expect(await screen.findByText("The solver's code could not check these changes.")).toBeInTheDocument()
      expect(screen.getByText("It says: roster: unknown shift 'X'")).toBeInTheDocument()

      await user.click(await cellNamed(/Badr Noor, 5 June/))
      await user.click(await screen.findByRole("menuitem", { name: "Pin a day off" }))
      expect(await screen.findByText(/could not be checked: the server did not answer/)).toBeInTheDocument()
    })

    it("does not call the cost change none when the saved grid's cost is not known", async () => {
      const { user } = start("/roster", editing({ [`POST ${UNIT}/roster/check`]: { body: { ...CHECK, cost_before: null, delta: null } } }))

      await user.click(await cellNamed("Amal Saleh, 5 June: Off"))
      await user.click(await screen.findByRole("menuitem", { name: /Pin Night/ }))

      expect(await screen.findByText(/costs 15, against a saved grid whose cost could not be worked out/)).toBeInTheDocument()
      expect(screen.queryByText(/no change/)).not.toBeInTheDocument()
    })

    it("keeps the edits when a save is refused, marking the cells and saying why", async () => {
      const { user } = start("/roster", editing({
        [`POST ${UNIT}/roster/pins`]: { status: 422, body: { detail: [{ loc: ["body", "body", "edits", 0], msg: "Amal Saleh is on leave on 5 June 2026, and leave wins over a pin.", type: "value_error" }] } },
      }))

      await user.click(await cellNamed("Amal Saleh, 5 June: Off"))
      await user.click(await screen.findByRole("menuitem", { name: /Pin Night/ }))
      await user.click(await screen.findByRole("button", { name: "Save changes" }))

      const refused = await screen.findByRole("alert")
      expect(refused).toHaveTextContent("Nothing was saved")
      expect(refused).toHaveTextContent("Amal Saleh, 5 June: Amal Saleh is on leave on 5 June 2026")
      expect(await cellNamed(/Amal Saleh, 5 June: Night, to be pinned to Night, not saved, refused/)).toBeInTheDocument()
      await waitFor(() => expect(refused).toHaveFocus())
      expect(screen.getByRole("region", { name: "1 change not saved yet" })).toBeInTheDocument()
    })

    it("asks before leaving the month or the page with edits not saved, and holds back a solve", async () => {
      const { user, router } = start("/roster", editing())

      await user.click(await cellNamed("Amal Saleh, 5 June: Off"))
      await user.click(await screen.findByRole("menuitem", { name: "Pin a day off" }))
      expect(await screen.findByRole("button", { name: "Solve again" })).toHaveAttribute("aria-disabled", "true")
      expect(screen.getByText(/Save or discard your changes to the draft first/)).toBeInTheDocument()

      await user.click(screen.getByRole("link", { name: "Next month" }))
      const question = await screen.findByRole("alertdialog", { name: "Leave without saving your changes?" })
      expect(question).toHaveTextContent("Your changes to the roster of June 2026 are not saved yet. If you leave, they are lost.")
      await user.click(within(question).getByRole("button", { name: "Stay" }))
      await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument())
      expect(router.state.location.search).toEqual({})
      expect(screen.getByRole("region", { name: "1 change not saved yet" })).toBeInTheDocument()

      await user.click(screen.getByRole("link", { name: "Next month" }))
      await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Leave without saving" }))
      await waitFor(() => expect(router.state.location.search).toEqual({ month: "2026-07" }))
      expect(screen.queryByRole("region", { name: /not saved yet/ })).not.toBeInTheDocument()
      await user.click(screen.getByRole("link", { name: "Previous month" }))
      await user.click(await cellNamed("Amal Saleh, 5 June: Off"))
      await user.click(await screen.findByRole("menuitem", { name: "Pin a day off" }))

      await user.click(screen.getByRole("button", { name: "Discard changes" }))
      expect(screen.queryByRole("region", { name: /not saved yet/ })).not.toBeInTheDocument()
      await waitFor(() => expect(screen.getByRole("button", { name: "Amal Saleh, 5 June: Off" })).toHaveFocus())
      await user.click(screen.getByRole("link", { name: "Next month" }))
      await waitFor(() => expect(router.state.location.search).toEqual({ month: "2026-07" }))
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument()
    })

    // Without a session, staying could save nothing, so nothing is asked.
    it("a session that ends on saving goes straight to the login page, and signing in comes back to the month", async () => {
      // The session ends at the save: every read after it is refused until the manager signs in again.
      let signedIn = true
      const { user, router } = start("/roster?month=2026-06", editing({
        "GET /api/units": () => (signedIn ? { body: [{ id: "u1", name: "ER", archived: false, manager: null, rules_need_review: false }] } : UNAUTHORIZED),
        [`POST ${UNIT}/roster/pins`]: () => { signedIn = false; return UNAUTHORIZED },
        "POST /api/auth/login": () => { signedIn = true; return { body: { time_zone: "Asia/Riyadh", ...MANAGER } } },
      }))
      await user.click(await cellNamed("Amal Saleh, 5 June: Off"))
      await user.click(await screen.findByRole("menuitem", { name: "Pin a day off" }))

      await user.click(within(screen.getByRole("region", { name: "1 change not saved yet" })).getByRole("button", { name: "Save changes" }))

      expect(await screen.findByText(SIGNED_OUT)).toBeInTheDocument()
      expect(router.state.location.pathname).toBe("/login")
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument()
      expect(screen.queryByRole("grid")).not.toBeInTheDocument()

      await signInAgain(user)

      expect(await cellNamed("Amal Saleh, 5 June: Off")).toBeInTheDocument()
      expect(router.state.location.pathname).toBe("/roster")
      expect(router.state.location.search).toEqual({ month: "2026-06" })
      expect(screen.queryByRole("region", { name: /not saved yet/ })).not.toBeInTheDocument()
    })

    it("says what pins leave replaced, which pins count for nothing, and what the edited draft breaks", async () => {
      const draft = {
        ...DRAFT, edited: true, cost: null,
        check: { reason: null, ok: false, violations: [
          { rule: "unavailable", hospital_id: "h1", full_name: "Amal Saleh", date: "2026-06-06", text: "h1 works on a leave day" },
          { rule: "max_hours", hospital_id: "h2", full_name: "Badr Noor", date: null, text: "h2: 300 h in the month, max 240" },
        ], cost: 40 },
        pins: [{ hospital_id: "h2", full_name: "Badr Noor", date: "2026-06-07", shift: "N", origin: "manager", set_aside: "outside_dates" }],
        pins_lost_to_leave: [{ hospital_id: "h1", full_name: "Amal Saleh", date: "2026-06-06", shift: "D", by: "Omar Said" }],
        changes: [{ hospital_id: "h1", full_name: "Amal Saleh", what: "pins" }],
      }
      start("/roster", editing({ [`GET ${UNIT}/roster`]: { body: { ...EMPTY, draft } } }))

      expect(await screen.findByText(/Leave recorded since this solve replaced these pins/)).toBeInTheDocument()
      expect(screen.getByText("Amal Saleh, 6 June: D, by the leave Omar Said recorded")).toBeInTheDocument()
      expect(screen.getByText("Badr Noor, 7 June: N, outside their start and end dates")).toBeInTheDocument()
      expect(screen.getByText("The draft as it stands breaks 2 hard rules:")).toBeInTheDocument()
      expect(screen.getByText("Amal Saleh, 6 June: h1 works on a leave day")).toBeInTheDocument()
      expect(screen.getByText("Badr Noor: 300 h in the month, max 240")).toBeInTheDocument()
      expect(screen.getByText(/Cost not known on the month as it stands, against 12 as solved/)).toBeInTheDocument()
      expect(screen.getByRole("alert")).toHaveTextContent("Amal Saleh: pinned cells changed")
    })

    it("says a pin on a day an expired required skill bars counts for nothing, and comes back once that changes", async () => {
      const draft = {
        ...DRAFT,
        pins: [{ hospital_id: "h1", full_name: "Amal Saleh", date: "2026-06-25", shift: "D", origin: "manager", set_aside: "barred" }],
      }
      start("/roster", editing({ [`GET ${UNIT}/roster`]: { body: { ...EMPTY, draft } } }))

      expect(await screen.findByText(/These pins count for nothing at present/)).toHaveTextContent("They come back if that changes")
      expect(screen.getByText("Amal Saleh, 25 June: D, a day after a skill required to work expired, so they can work no shift")).toBeInTheDocument()
    })

    it("says her skills and the unit's skill rules changed since the solve, and the skill rules the draft breaks, with dates", async () => {
      const draft = {
        ...DRAFT, cost: null,
        check: {
          reason: null, ok: false, cost: null, violations: [
            { rule: "skill_expired", hospital_id: "h1", full_name: "Amal Saleh", date: "2026-06-21", text: "skill expired: h1 works D on 2026-06-21, BLS is required to work and its last day was 2026-06-20" },
            { rule: "skill_need", hospital_id: null, full_name: "", date: "2026-06-03", text: "skill need: 2026-06-03 N has 0 holding ICU, needs 1 (1 short)" },
          ],
        },
        changes: [{ hospital_id: "", full_name: "", what: "skills" }, { hospital_id: "h1", full_name: "Amal Saleh", what: "skills" }],
      }
      start("/roster", editing({ [`GET ${UNIT}/roster`]: { body: { ...EMPTY, draft } } }))

      const changes = await screen.findByRole("alert")
      expect(within(changes).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
        "The skill needs, the skills required to work or the skill shortfall weight changed", "Amal Saleh: skills changed"])
      expect(screen.getByText("The draft as it stands breaks 2 hard rules:")).toBeInTheDocument()
      expect(screen.getByText("Amal Saleh, 21 June: skill expired: h1 works D on 2026-06-21, BLS is required to work and its last day was 2026-06-20")).toBeInTheDocument()
      expect(screen.getByText("3 June: skill need: 2026-06-03 N has 0 holding ICU, needs 1 (1 short)")).toBeInTheDocument()
    })

    it("says the month's Ramadan days and public holidays changed since the solve, and still offers Submit", async () => {
      const draft = { ...DRAFT, changes: [
        { hospital_id: "", full_name: "", what: "ramadan_days" }, { hospital_id: "", full_name: "", what: "public_holidays" },
        { hospital_id: "h1", full_name: "Amal Saleh", what: "leave" },
      ] }
      start("/roster", editing({ [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, draft } } }))

      const changes = await screen.findByRole("alert")
      expect(changes).toHaveTextContent("This draft was solved before these changes:")
      expect(within(changes).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
        "The Ramadan days of June 2026 changed since this draft was solved",
        "The public holidays of June 2026 changed since this draft was solved", "Amal Saleh: leave or dates changed"])
      expect(screen.getByRole("button", { name: "Submit for approval" })).toBeEnabled()
    })

    it("offers no editing in a month that is over, and says why", async () => {
      const over = { ...DRAFT, editor: { ...EDITOR, refusal: "This month is over, so its draft cannot change." } }
      start("/roster", editing({ [`GET ${UNIT}/roster`]: { body: { ...EMPTY, draft: over } } }))

      expect(await screen.findByText("This month is over, so its draft cannot change.")).toBeInTheDocument()
      expect(screen.queryByRole("grid")).not.toBeInTheDocument()
      expect(screen.queryByRole("button", { name: /Amal Saleh, 1 June/ })).not.toBeInTheDocument()
    })

    it("says a solve under way will not have the changes saved now", async () => {
      start("/roster", editing({ [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, draft: DRAFT, active_solve: RUNNING } }, [`GET ${UNIT}/solves/${SOLVE_ID}`]: { body: RUNNING } }))

      expect(await screen.findByText(/changes you save now are put on top of its roster when it ends/)).toBeInTheDocument()
    })
  })

  it("offers to solve again over a draft, saying what that does", async () => {
    start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, draft: DRAFT } } }))

    expect(await screen.findByRole("button", { name: "Solve again" })).toBeInTheDocument()
    expect(screen.getByText(/Solving again replaces the draft's grid/)).toBeInTheDocument()
  })
  describe("its history", () => {
    const OLDER = "019a0000-0000-7000-8000-000000000001"
    const FAILED = { ...ROW, id: "f1", state: "failed", failure: "solver", cost: null, found_roster: false, in_draft: false }
    const REFUSED = { ...FAILED, id: "f2", failure: "refused" }
    const NOTHING = { ...FAILED, id: "f3", state: "succeeded", failure: null }
    const STOPPED = { ...ROW, id: "f4", state: "cancelled", in_draft: false, elapsed_seconds: 8 }
    const STOPPED_EMPTY = { ...FAILED, id: "f5", state: "cancelled", failure: null, attempts: 0, elapsed_seconds: 0 }
    const QUEUED_ROW = { ...FAILED, id: "f6", state: "queued", failure: null, finished_at: null, attempts: 0, elapsed_seconds: 0 }
    const withHistory = (solves: unknown[], extra = {}) => manager({ [`GET ${UNIT}/solve-history`]: { body: { solves, older_than: null } }, ...extra })
    const statusOf = (solve: string) => within(screen.getByRole("row", { name: new RegExp(solve) })).getAllByRole("cell")[1].textContent

    it("lists each solve with its status, who asked, how long and its cost", async () => {
      start("/solve-history", withHistory([{ ...ROW, id: "a1" }]))

      await screen.findByRole("heading", { level: 1, name: "Solve history of ER" })
      const row = await screen.findByRole("row", { name: /June 2026/ })
      expect(within(row).getAllByRole("cell").map((cell) => cell.textContent)).toEqual([
        "June 2026", "Finished, the draft", "Mona Ali", "2026-05-15 10:00:00", "1 min 3 s", "12", expect.stringContaining("Open"),
      ])
      expect(within(row).getByRole("link", { name: /Open/ })).toHaveAttribute("href", "/solve-history/a1")
    })

    it("words every way a solve can end", async () => {
      const months = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]
      const solves = [FAILED, REFUSED, NOTHING, STOPPED, STOPPED_EMPTY, QUEUED_ROW].map((row, index) => ({ ...row, month: months[index] }))
      start("/solve-history", withHistory(solves))

      await screen.findByRole("row", { name: /January 2026/ })
      expect(statusOf("January 2026")).toBe("Failed")
      expect(statusOf("February 2026")).toBe("Rules to review")
      expect(statusOf("March 2026")).toBe("No roster to keep")
      expect(statusOf("April 2026")).toBe("Stopped, roster kept")
      expect(statusOf("May 2026")).toBe("Stopped, no roster")
      expect(statusOf("June 2026")).toBe("In the queue")
      expect(within(screen.getByRole("row", { name: /May 2026/ })).getAllByRole("cell")[4]).toHaveTextContent("Did not start")
      expect(within(screen.getByRole("row", { name: /May 2026/ })).getAllByRole("cell")[5]).toHaveTextContent("None")
    })

    it("narrows to the month in the address, and can show them all", async () => {
      const { calls, user } = start("/solve-history?month=2026-06", withHistory([ROW]))

      await screen.findByText("Solves of June 2026 only.")
      expect(calls.map((call) => call.url)).toContain(`${UNIT}/solve-history?month=2026-06`)
      await user.click(screen.getByRole("link", { name: "Show every month" }))

      await waitFor(() => expect(calls.map((call) => call.url)).toContain(`${UNIT}/solve-history`))
      expect(screen.queryByText("Solves of June 2026 only.")).not.toBeInTheDocument()
    })

    it("goes back a page at a time", async () => {
      const { calls, user } = start("/solve-history", manager({
        [`GET ${UNIT}/solve-history`]: { body: { solves: [ROW], older_than: OLDER } },
        [`GET ${UNIT}/solve-history?older_than=${OLDER}`]: { body: { solves: [{ ...ROW, id: OLDER, month: "2026-01" }], older_than: null } },
      }))

      await user.click(await screen.findByRole("link", { name: "Older solves" }))

      await screen.findByRole("row", { name: /January 2026/ })
      expect(calls.map((call) => call.url)).toContain(`${UNIT}/solve-history?older_than=${OLDER}`)
      expect(screen.queryByRole("link", { name: "Older solves" })).not.toBeInTheDocument()
    })

    it("says when there is none, and when it cannot be read", async () => {
      start("/solve-history", withHistory([]))
      expect(await screen.findByText("This unit has not solved a month yet.")).toBeInTheDocument()
      cleanup()

      start("/solve-history", manager({ [`GET ${UNIT}/solve-history`]: { status: 500 } }))
      expect(await screen.findByRole("alert")).toHaveTextContent("could not be read")
    })

    it("is for a manager and the System Admin only", async () => {
      const { router } = start("/solve-history", nurseHome("u1"))

      await screen.findByText("No roster yet for December 2026.")
      expect(router.state.location.pathname).toBe("/")
    })

    describe("comparing two results", () => {
      const NEWEST = { ...ROW, id: "019a0000-0000-7000-8000-000000000003", requested_at: "2026-05-17T09:00:00+03:00", cost: 20 }
      const MIDDLE = { ...ROW, id: "019a0000-0000-7000-8000-000000000002", requested_at: "2026-05-16T09:00:00+03:00", cost: 15, in_draft: false }
      const OLDEST = { ...ROW, id: "019a0000-0000-7000-8000-000000000001", requested_at: "2026-05-15T09:00:00+03:00", cost: 12, in_draft: false }
      const NO_ROSTER = { ...FAILED, id: "019a0000-0000-7000-8000-0000000000ff", month: "2026-06" }
      const MONTH_ROWS = [NEWEST, NO_ROSTER, MIDDLE, OLDEST]
      const ACCOUNT = { hospital_id: "h1", full_name: "Amal Saleh", contract: 192, leave_credit: 24, required: 168, hours_worked: 180, overtime: 12, shortfall: 0, shifts: { D: 15, N: 0 }, cost: 3 }
      const side = (row: typeof ROW, extra = {}) => ({ ...row, unit_cost: 2, replan_of: null, replan_from: null, replan_outcome: null, statistics: STATISTICS, ...extra })
      const violation = (rule: string, who: string) => ({ rule, hospital_id: "h1", full_name: who, date: "2026-06-05", text: `${rule}: ${who} on 2026-06-05` })
      const COMPARISON = {
        month: "2026-06",
        older: side(OLDEST),
        newer: side(NEWEST, { replan_from: "2026-06-12", replan_of: 1, replan_outcome: "version" }),
        inputs: {
          changes: [
            { group: "hospital_rules", field: "weights.night_rest", older: 5, newer: 8 },
            { group: "hospital_rules", field: "required_skills", older: null, newer: [] },
            { group: "unit_rules", field: "max_consecutive_shifts", older: 5, newer: 6 },
            { group: "time_limit", field: "time_limit_seconds", older: 60, newer: 120 },
            { group: "calendar", field: "public_holidays", older: [], newer: ["2026-06-10"] },
          ],
          nurses: [
            { hospital_id: "h3", full_name: "Faris Haddad", kinds: ["joined"] },
            { hospital_id: "h1", full_name: "Amal Saleh", kinds: ["leave", "pins"] },
          ],
        },
        rules: [
          { rule: "night_rest", older: { amount: 2, weight: 5, penalty: 10 }, newer: { amount: 1, weight: 8, penalty: 8 }, weight_differs: true },
          { rule: "change", older: { amount: null, weight: null, penalty: null }, newer: { amount: 2, weight: 3, penalty: 6 }, weight_differs: false },
          { rule: "fair_day", older: { amount: 5, weight: 1, penalty: 5 }, newer: { amount: 5, weight: 1, penalty: 5 }, weight_differs: false },
        ],
        cells: [{ hospital_id: "h1", full_name: "Amal Saleh", date: "2026-06-15", older: "", newer: "D" }],
        nurses: [
          { hospital_id: "h1", full_name: "Amal Saleh", older: ACCOUNT, newer: { ...ACCOUNT, hours_worked: 168, overtime: 0 } },
          { hospital_id: "h3", full_name: "Faris Haddad", older: null, newer: ACCOUNT },
        ],
        violations: { both: [], only_older: [], only_newer: [] },
        facts: { both: [violation("rest", "Amal Saleh")], only_older: [violation("consecutive", "Badr Noor")], only_newer: [] },
        explained_older: true,
        explained_newer: true,
      }
      const compareUrl = (a: string, b: string) => `${UNIT}/solve-history/compare?a=${a}&b=${b}`
      const comparing = (extra = {}, rows: unknown[] = MONTH_ROWS) => manager({
        [`GET ${UNIT}/solve-history`]: { body: { solves: rows, older_than: null } },
        [`GET ${UNIT}/solve-history/compare`]: { body: COMPARISON },
        ...extra,
      })
      const options = (label: string) => within(screen.getByLabelText(label)).getAllByRole("option").map((option) => (option as HTMLOptionElement).value)

      it("is absent without a month", async () => {
        start("/solve-history", comparing())

        await screen.findByRole("row", { name: /2026-05-17 09:00:00/ })
        expect(screen.queryByRole("heading", { name: "Compare two results" })).not.toBeInTheDocument()
      })

      it("compares the two newest results with a roster, each picker leaving out the other's choice and any without a roster", async () => {
        const { calls } = start("/solve-history?month=2026-06", comparing())

        await screen.findByRole("heading", { level: 2, name: "Compare two results" })
        expect(screen.getByLabelText("One result")).toHaveValue(NEWEST.id)
        expect(screen.getByLabelText("The other")).toHaveValue(MIDDLE.id)
        expect(options("One result")).toEqual([NEWEST.id, OLDEST.id])
        expect(options("The other")).toEqual([MIDDLE.id, OLDEST.id])
        expect(within(screen.getByLabelText("One result")).getByRole("option", { name: "2026-05-17 09:00:00, cost 20" })).toBeInTheDocument()
        await screen.findByRole("heading", { level: 3, name: "The two solves" })
        expect(calls.map((call) => call.url)).toContain(compareUrl(NEWEST.id, MIDDLE.id))
      })

      it("writes a chosen pair to the address", async () => {
        const { calls, user, router } = start("/solve-history?month=2026-06", comparing())

        await user.selectOptions(await screen.findByLabelText("The other"), OLDEST.id)

        await waitFor(() => expect(router.state.location.search).toMatchObject({ month: "2026-06", a: NEWEST.id, b: OLDEST.id }))
        await waitFor(() => expect(calls.map((call) => call.url)).toContain(compareUrl(NEWEST.id, OLDEST.id)))
        expect(options("One result")).toEqual([NEWEST.id, MIDDLE.id])
      })

      it("takes the pair from the address, one from an older page included", async () => {
        const PAST = "019a0000-0000-7000-8000-0000000000aa"
        const { calls } = start(`/solve-history?month=2026-06&a=${OLDEST.id}&b=${PAST}`, comparing())

        await screen.findByRole("heading", { level: 3, name: "The two solves" })
        expect(calls.map((call) => call.url)).toContain(compareUrl(OLDEST.id, PAST))
        expect(screen.getByLabelText("The other")).toHaveValue(PAST)
        expect(within(screen.getByLabelText("The other")).getByRole("option", { name: "A result from an older page of the history" })).toBeInTheDocument()
      })

      it("with one result in the address, compares it with the newest other", async () => {
        const { calls } = start(`/solve-history?month=2026-06&a=${NEWEST.id}`, comparing())

        await screen.findByRole("heading", { level: 3, name: "The two solves" })
        expect(calls.map((call) => call.url)).toContain(compareUrl(NEWEST.id, MIDDLE.id))
      })

      it("reads older left and newer right, in the spec's order, with the rules pages' labels and differences newer minus older", async () => {
        start("/solve-history?month=2026-06", comparing())

        await screen.findByRole("heading", { level: 3, name: "The two solves" })
        const section = screen.getByRole("region", { name: "Compare two results" })
        expect(within(section).getAllByRole("heading", { level: 3 }).map((heading) => heading.textContent)).toEqual([
          "The two solves", "What changed between their inputs", "Each rule, side by side", "Cells that differ", "Hours and cost per nurse",
          "Hard rules each roster breaks", "Recorded facts that broke rules", "Solver statistics",
        ])
        const facts = within(screen.getByRole("region", { name: "The two solves" }))
        expect(facts.getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["", "Older, asked for at 2026-05-15 09:00:00", "Newer, asked for at 2026-05-17 09:00:00"])
        expect(within(facts.getByRole("row", { name: /Re-planned from/ })).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["Not a re-plan", "12 June, of version 1"])

        const inputs = within(screen.getByRole("region", { name: "What changed between their inputs" }))
        const valuesOf = (label: string) => within(inputs.getByRole("row", { name: new RegExp(`^${label}`) })).getAllByRole("cell").map((cell) => cell.textContent)
        expect(valuesOf("Weight: Night rest")).toEqual(["5", "8"])
        expect(valuesOf("Skills required to work")).toEqual(["Not in its input", "None"])
        expect(valuesOf("Longest run of shifts")).toEqual(["5", "6"])
        expect(valuesOf("Time limit")).toEqual(["60 s", "120 s"])
        expect(valuesOf("Public holidays")).toEqual(["None", expect.stringContaining("10")])
        expect(inputs.getByText("Faris Haddad: joined the month")).toBeInTheDocument()
        expect(inputs.getByText("Amal Saleh: leave, pins")).toBeInTheDocument()

        const rules = within(screen.getByRole("region", { name: "Each rule, side by side" }))
        const night = within(rules.getByRole("row", { name: /Night rest/ }))
        expect(night.getByText("Weight differs")).toBeInTheDocument()
        expect(night.getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["2", "5", "10", "1", "8", "8", "−2"])
        expect(within(rules.getByRole("row", { name: /Changes in a re-plan/ })).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["None", "", "", "2", "3", "6", "+6"])
        expect(rules.getByText(/compare the amounts/)).toBeInTheDocument()
        expect(rules.getByText("Every other soft rule costs nothing in both.")).toBeInTheDocument()

        const cells = within(screen.getByRole("region", { name: "Cells that differ" }))
        expect(cells.getByText("1 cell differs.")).toBeInTheDocument()
        expect(within(cells.getByRole("row", { name: /Amal Saleh/ })).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["15 June", "Off", "D"])
        expect(cells.getByRole("rowheader", { name: "Amal Saleh1 cell" })).toBeInTheDocument()

        const hours = within(screen.getByRole("region", { name: "Hours and cost per nurse" }))
        const amal = within(hours.getByRole("row", { name: /Amal Saleh/ })).getAllByRole("cell").map((cell) => cell.textContent)
        expect(amal).toEqual(["192", "168", "180 → 168, −12", "12 → 0, −12", "0", "3"])
        expect(within(hours.getByRole("row", { name: /Faris Haddad/ })).getAllByRole("cell")[0]).toHaveTextContent("Not in the older → 192")
        expect(screen.queryByRole("button", { name: /Restore/ })).not.toBeInTheDocument()
      })

      it("groups the facts that broke rules in three", async () => {
        start("/solve-history?month=2026-06", comparing())

        const facts = within(await screen.findByRole("region", { name: "Recorded facts that broke rules" }))
        expect(facts.getByText("In both").nextElementSibling).toHaveTextContent("Amal Saleh: rest: Amal Saleh on 2026-06-05")
        expect(facts.getByText("Only in the older").nextElementSibling).toHaveTextContent("Badr Noor: consecutive")
        expect(facts.getByText("Only in the newer").nextElementSibling).toHaveTextContent("None.")
        expect(screen.getByRole("region", { name: "Hard rules each roster breaks" })).toHaveTextContent("Neither roster breaks a hard rule.")
      })

      it("says when one side cannot be explained", async () => {
        const unexplained = { ...COMPARISON, explained_older: false, rules: [{ ...COMPARISON.rules[2], older: { amount: null, weight: null, penalty: 5 } }] }
        start("/solve-history?month=2026-06", comparing({ [`GET ${UNIT}/solve-history/compare`]: { body: unexplained } }))

        const rules = within(await screen.findByRole("region", { name: "Each rule, side by side" }))
        expect(rules.getByText(/The older result's amounts and weights are not known/)).toBeInTheDocument()
        expect(within(rules.getByRole("row", { name: /Fair share of day shifts/ })).getAllByRole("cell").map((cell) => cell.textContent).slice(0, 3)).toEqual(["Not known", "Not known", "5"])
      })

      it("says the month has nothing to compare with fewer than two results holding a roster", async () => {
        start("/solve-history?month=2026-06", comparing({}, [NEWEST, NO_ROSTER]))

        expect(await screen.findByText("Fewer than two solves of June 2026 hold a roster, so there is nothing to compare yet.")).toBeInTheDocument()
        expect(screen.queryByLabelText("One result")).not.toBeInTheDocument()
      })

      it("says when the comparison cannot be read", async () => {
        start("/solve-history?month=2026-06", comparing({ [`GET ${UNIT}/solve-history/compare`]: { status: 500 } }))

        expect(await screen.findByRole("alert")).toHaveTextContent("These two results could not be compared. Reload the page to try again")
      })

      it("is linked from a result's page, with that result as one side", async () => {
        start(`/solve-history/${SOLVE_ID}`, manager())

        expect(await screen.findByRole("link", { name: "Compare with another result" })).toHaveAttribute("href", `/solve-history?month=2026-06&a=${SOLVE_ID}`)
      })

      it("is not linked from a result with no roster", async () => {
        start(`/solve-history/${SOLVE_ID}`, manager({ [`GET ${UNIT}/solve-history/${SOLVE_ID}`]: { body: { ...RESULT, solve: { ...ROW, found_roster: false }, grid: null } } }))

        await screen.findByRole("link", { name: "Solve history of June 2026" })
        expect(screen.queryByRole("link", { name: "Compare with another result" })).not.toBeInTheDocument()
      })

      it.each([["the CNO", CNO_ME], ["the Deputy CNO", DEPUTY]])("keeps %s out", async (_, who) => {
        const { router } = start(`/solve-history?month=2026-06&a=${NEWEST.id}&b=${MIDDLE.id}`, {
          ...session(who), "GET /api/rosters/submitted": { body: [] }, "GET /api/nurses/expiries": { body: NO_EXPIRIES }, "GET /api/units": { body: [{ ...ER, manager: MONA }] },
        })

        await screen.findByRole("heading", { level: 1, name: who.full_name })
        expect(router.state.location.pathname).toBe("/")
      })
    })

    describe("a result", () => {
      const NOT_DRAFT = { ...RESULT, solve: { ...ROW, in_draft: false }, restore_refusal: null }
      const result = (body: unknown) => ({ [`GET ${UNIT}/solve-history/${SOLVE_ID}`]: { body } })
      const open = (body: unknown, extra = {}) => start(`/solve-history/${SOLVE_ID}`, manager({ ...result(body), ...extra }))

      it("shows what it asked for, its grid and everything it holds", async () => {
        open(NOT_DRAFT)

        await screen.findByRole("heading", { level: 1, name: "Result for June 2026" })
        expect(screen.getByText("Asked for by").nextElementSibling).toHaveTextContent("Mona Ali")
        expect(screen.getByText("Status").nextElementSibling).toHaveTextContent("Finished")
        expect(screen.getByRole("region", { name: "Roster of this result, June 2026" })).toBeInTheDocument()
        expect(screen.getByRole("region", { name: "Hours and cost per nurse" })).toBeInTheDocument()
        expect(screen.getByRole("region", { name: "Solver statistics" })).toBeInTheDocument()
        expect(screen.getByRole("link", { name: "Solve history of June 2026" })).toHaveAttribute("href", "/solve-history?month=2026-06")
      })

      it("restores it once asked, and says it is the draft", async () => {
        const restored = { ...RESULT, solve: { ...ROW, in_draft: true } }
        const { calls, user } = open(NOT_DRAFT, {
          [`POST ${UNIT}/solve-history/${SOLVE_ID}/restore`]: { body: restored },
          [`GET ${UNIT}/solve-history/${SOLVE_ID}`]: () => ({ body: calls.some((call) => call.method === "POST") ? restored : NOT_DRAFT }),
        })

        await user.click(await screen.findByRole("button", { name: "Restore as the draft" }))
        const dialog = await screen.findByRole("alertdialog")
        expect(dialog).toHaveTextContent(/replaces the draft of June 2026/)
        expect(calls.some((call) => call.method === "POST")).toBe(false)
        await user.click(within(dialog).getByRole("button", { name: "Restore" }))

        expect(await screen.findByText(/This result is now the draft of June 2026/)).toHaveAttribute("role", "status")
        expect(screen.queryByRole("button", { name: "Restore as the draft" })).not.toBeInTheDocument()
        expect(calls.filter((call) => call.method === "POST").map((call) => call.url)).toEqual([`${UNIT}/solve-history/${SOLVE_ID}/restore`])
        expect(screen.getByRole("link", { name: "See the draft" })).toHaveAttribute("href", "/roster?month=2026-06")
        await waitFor(() => expect(document.activeElement).toHaveAttribute("id", "restore-state"))
      })

      it("fetches the month's draft again after a restore", async () => {
        const restored = { ...RESULT, solve: { ...ROW, in_draft: true } }
        let draft: unknown = { ...DRAFT, solve_id: OLDER, cost: 99 }
        const { user } = open(NOT_DRAFT, {
          [`GET ${UNIT}/roster`]: () => ({ body: { ...EMPTY, draft } }),
          [`GET ${UNIT}/solve-history/${OLDER}`]: { body: RESULT },
          [`POST ${UNIT}/solve-history/${SOLVE_ID}/restore`]: () => {
            draft = { ...DRAFT, restored_at: "2026-05-16T09:00:00+03:00" }
            return { body: restored }
          },
        })

        await user.click(await screen.findByRole("button", { name: "Restore as the draft" }))
        await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Restore" }))
        await user.click(await screen.findByRole("link", { name: "See the draft" }))

        expect(await screen.findByText(/Restored 2026-05-16 09:00:00/)).toBeInTheDocument()
      })

      it("keeps the dialog and says why when the restore is refused", async () => {
        const reason = "A solve of this month is queued or running, and would replace this when it ends. Stop it or wait for it first."
        const { user } = open(NOT_DRAFT, { [`POST ${UNIT}/solve-history/${SOLVE_ID}/restore`]: { status: 409, body: { detail: reason } } })

        await user.click(await screen.findByRole("button", { name: "Restore as the draft" }))
        const dialog = await screen.findByRole("alertdialog")
        await user.click(within(dialog).getByRole("button", { name: "Restore" }))

        expect(await within(dialog).findByRole("alert")).toHaveTextContent(reason)
      })

      it("says why it cannot be restored instead of offering to", async () => {
        const reason = "D was 12 hours at this solve and is 8 now, so this roster's cost and hours no longer hold. Solve the month again."
        open({ ...NOT_DRAFT, restore_refusal: reason })

        expect(await screen.findByText(reason)).toBeInTheDocument()
        expect(screen.queryByRole("button", { name: "Restore as the draft" })).not.toBeInTheDocument()
      })

      it("says when it is the draft", async () => {
        open(RESULT)

        expect(await screen.findByText(/This result is the draft of June 2026/)).toBeInTheDocument()
        expect(screen.queryByText("This result is the draft already.")).not.toBeInTheDocument()
      })

      it("says why a failed solve has nothing to show", async () => {
        const failed = {
          ...RESULT, solve: { ...ROW, ...FAILED, id: SOLVE_ID }, error: "The solver restarted and lost this solve.", rules: [], explained: true, nurses: [],
          statistics: null, grid: null, unit_cost: null, restore_refusal: "This solve left no roster to restore.",
        }
        open(failed)

        expect(await screen.findByText(/The solver restarted and lost this solve\./)).toBeInTheDocument()
        expect(screen.queryByRole("region", { name: "Hours and cost per nurse" })).not.toBeInTheDocument()
        expect(screen.queryByRole("region", { name: "Solver statistics" })).not.toBeInTheDocument()
        expect(screen.getByText("This solve left no roster to restore.")).toBeInTheDocument()
      })

      it("reads rules the solver refused as rules to review", async () => {
        open({ ...RESULT, solve: { ...ROW, ...REFUSED, id: SOLVE_ID }, error: "night_shift is not a shift", rules: [], nurses: [], statistics: null, grid: null, unit_cost: null, restore_refusal: "This solve left no roster to restore." })

        expect(await screen.findByRole("link", { name: "Review the unit rules" })).toBeInTheDocument()
        expect(screen.getByText(/night_shift is not a shift/)).toBeInTheDocument()
      })

      it("gives the solver's reason when it found no roster, and any hard rule a roster it would not keep breaks", async () => {
        const broken = { rule: "headcount", hospital_id: null, full_name: "", date: "2026-06-02", text: "headcount: D = 4, want 1..2" }
        open({ ...NOT_DRAFT, solve: { ...ROW, found_roster: false, in_draft: false }, message: "the roster breaks 1 hard rule(s)", violations: [broken], restore_refusal: "This solve left no roster to restore." })

        expect(await screen.findByText(/the roster breaks 1 hard rule/)).toBeInTheDocument()
        expect(within(screen.getByRole("region", { name: "Hard rules this roster breaks" })).getByText(/The unit, 2 Jun 2026/)).toHaveTextContent("want 1..2")
      })

      it("claims nothing about soft rules when it found no roster", async () => {
        open({ ...NOT_DRAFT, solve: { ...ROW, found_roster: false, in_draft: false, cost: null }, message: "no roster found within the time limit", rules: [], nurses: [], grid: null, unit_cost: null, restore_refusal: "This solve left no roster to restore." })

        await screen.findByRole("region", { name: "Solver statistics" })
        expect(screen.queryByRole("region", { name: "Penalties per rule" })).not.toBeInTheDocument()
        expect(screen.queryByText("No soft rule is broken.")).not.toBeInTheDocument()
      })

      it("cancelling leaves focus on the button that asked", async () => {
        const { user, calls } = open(NOT_DRAFT)

        const button = await screen.findByRole("button", { name: "Restore as the draft" })
        await user.click(button)
        await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Cancel" }))

        await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument())
        await waitFor(() => expect(document.activeElement).toBe(button))
        expect(calls.some((call) => call.method === "POST")).toBe(false)
      })

      it("does not go on saying it is the draft once something else has replaced it", async () => {
        const restored = { ...RESULT, solve: { ...ROW, in_draft: true } }
        let answer: unknown = NOT_DRAFT
        const { router, user } = open(NOT_DRAFT, {
          [`GET ${UNIT}/solve-history/${SOLVE_ID}`]: () => ({ body: answer }),
          [`POST ${UNIT}/solve-history/${SOLVE_ID}/restore`]: () => {
            answer = restored
            return { body: restored }
          },
        })

        await user.click(await screen.findByRole("button", { name: "Restore as the draft" }))
        await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Restore" }))
        await screen.findByText(/This result is now the draft/)
        answer = NOT_DRAFT
        await act(() => router.options.context.queryClient.invalidateQueries())

        await screen.findByRole("button", { name: "Restore as the draft" })
        expect(screen.queryByText(/This result is now the draft/)).not.toBeInTheDocument()
      })

      it("says when it cannot be read", async () => {
        start(`/solve-history/${SOLVE_ID}`, manager({ [`GET ${UNIT}/solve-history/${SOLVE_ID}`]: { status: 404, body: { detail: "Not Found" } } }))

        expect(await screen.findByRole("alert")).toHaveTextContent("This result could not be read")
      })

      it("is for a manager", async () => {
        const { router } = start(`/solve-history/${SOLVE_ID}`, admin({ "GET /api/solve-history": { body: { solves: [], older_than: null } } }))

        await screen.findByRole("heading", { name: "Health" })
        expect(router.state.location.pathname).toBe("/health")
      })
    })

    describe("the System Admin's view", () => {
      const { in_draft: _inDraft, ...plain } = ROW
      const HOSPITAL_ROW = { ...plain, unit: "ER" }
      const everything = (solves: unknown[], extra = {}) => admin({ "GET /api/solve-history": { body: { solves, older_than: null } }, ...extra })

      it("lists every unit's solves and offers nothing to open", async () => {
        const { calls } = start("/solve-history", everything([HOSPITAL_ROW, { ...HOSPITAL_ROW, id: "b", unit: "ICU", state: "failed", failure: "unreachable", found_roster: false, cost: null }]))

        await screen.findByRole("heading", { level: 1, name: "Solve history" })
        const row = await screen.findByRole("row", { name: /ICU/ })
        expect(within(row).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["June 2026", "Failed", "Mona Ali", "2026-05-15 10:00:00", "1 min 3 s", "The solver could not be reached.", "60 s", "1", "None"])
        expect(within(screen.getByRole("row", { name: /ER/ })).getAllByRole("cell")[5]).toHaveTextContent("")
        expect(screen.queryByRole("link", { name: /Open/ })).not.toBeInTheDocument()
        expect(screen.getByText(/says only which kind of failure it was; its unit's manager reads the rest and can run it again/)).toBeInTheDocument()
        expect(calls.map((call) => call.url)).not.toContain("/api/units")
      })

      it.each([
        ["refused", "The solver refused the rules it was given: the unit's or the hospital's rules need review."],
        ["unreachable", "The solver could not be reached."],
        ["lost", "The solver restarted and lost the solve."],
        ["overrun", "The solver ran well past the time limit and was given up on."],
        ["solver_error", "The solver stopped with an error of its own."],
        ["stuck", "The solver would not stop an earlier job, so this one could not start."],
        ["worker_lost", "The worker stopped during the solve and could not take it up again."],
        ["unexpected", "The worker met an error of its own."],
        ["solver", "The solver failed or could not be reached (from before the kinds were told apart)."],
      ])("says why one failed in fixed words: %s", async (failure, words) => {
        start("/solve-history", everything([{ ...HOSPITAL_ROW, state: "failed", failure, found_roster: false, cost: null }]))

        expect(await screen.findByRole("cell", { name: words })).toBeInTheDocument()
      })

      it("goes back a page at a time", async () => {
        const { user } = start("/solve-history", admin({
          "GET /api/solve-history": { body: { solves: [HOSPITAL_ROW], older_than: OLDER } },
          [`GET /api/solve-history?older_than=${OLDER}`]: { body: { solves: [{ ...HOSPITAL_ROW, id: OLDER, unit: "ICU" }], older_than: null } },
        }))

        await user.click(await screen.findByRole("link", { name: "Older solves" }))

        expect(await screen.findByRole("row", { name: /ICU/ })).toBeInTheDocument()
      })

      it("says when there is none", async () => {
        start("/solve-history", everything([]))

        expect(await screen.findByText("No unit has solved a month yet.")).toBeInTheDocument()
      })
    })
  })

  describe("changes after approval", () => {
    const IN_FORCE = {
      ...DRAFTED, state: "approved", submit_refusal: "This roster is approved and in force, so no solve or restore can change it.",
      approved: { approved_by: "Dina Deputy", approved_at: "2026-05-16T12:00:00+03:00", version: 1 }, draft: DRAFT,
    }
    const VERSION_1 = { number: 1, origin: "solve", created_by: "Dina Deputy", created_at: "2026-05-16T12:00:00+03:00", reason: "", cells_changed: null }
    const VERSION_2 = { number: 2, origin: "edit", created_by: "Mona Ali", created_at: "2026-06-03T08:30:00+03:00", reason: "Badr covers Amal's night.", cells_changed: 2 }
    const VERSION_3 = { number: 3, origin: "edit", created_by: "Mona Ali", created_at: "2026-06-04T09:00:00+03:00", reason: "Leave recorded for Amal Saleh took their shift off 7 June 2026.", cells_changed: 1 }
    const CHECK = { reason: null, ok: true, violations: [], cost: 15, cost_before: 12, delta: 3, refusals: [] }
    const comparison = (from: number, to: number) => ({
      from_version: from, to_version: to,
      changes: [{ hospital_id: "h1", full_name: "Amal Saleh", date: "2026-06-07", before: "N", after: "" }],
    })
    const compared = ({ url }: { url: string }) => {
      const query = new URL(url, "http://here").searchParams
      return { body: comparison(Number(query.get("from_version")), Number(query.get("to_version"))) }
    }
    const cellNamed = (name: RegExp | string) => screen.findByRole("button", { name })

    it("saves a change as a new version, needing a reason, and tells the manager the nurses are told", async () => {
      const rows = [{ ...DRAFT.rows[0], cells: DRAFT.rows[0].cells.map((cell, index) => (index === 1 ? { shift: "N", kind: "shift" } : cell)) }, DRAFT.rows[1]]
      const after = { ...IN_FORCE, approved: { ...IN_FORCE.approved, version: 2 }, draft: { ...DRAFT, rows, edited: true,
        pins: [{ hospital_id: "h1", full_name: "Amal Saleh", date: "2026-06-05", shift: "N", origin: "manager", set_aside: null }] } }
      let versions: object[] = [VERSION_1]
      const { user, calls } = start("/roster", manager({
        [`GET ${UNIT}/roster`]: { body: IN_FORCE },
        [`POST ${UNIT}/roster/check`]: { body: CHECK },
        [`POST ${UNIT}/roster/versions`]: () => { versions = [VERSION_2, VERSION_1]; return { body: after } },
        [`GET ${UNIT}/roster/versions`]: () => ({ body: versions }),
      }))

      expect(await screen.findByText(/Each save is a new version in force at once, with your reason/)).toBeInTheDocument()
      await user.click(await cellNamed("Amal Saleh, 5 June: Off"))
      await user.click(await screen.findByRole("menuitem", { name: /Pin Night/ }))
      const unsaved = screen.getByRole("region", { name: "1 change not saved yet" })
      expect(await within(unsaved).findByText("With these changes the roster breaks no hard rule, and costs 15 (+3).")).toBeInTheDocument()
      await user.click(within(unsaved).getByRole("button", { name: "Save as a new version" }))

      expect(await within(unsaved).findByText("Say why, for the version list.")).toBeInTheDocument()
      expect(within(unsaved).getByLabelText("Why")).toHaveFocus()
      expect(calls.some((call) => call.url.endsWith("/roster/versions") && call.method === "POST")).toBe(false)

      await user.type(within(unsaved).getByLabelText("Why"), "Amal covers the night.")
      await user.click(within(unsaved).getByRole("button", { name: "Save as a new version" }))

      expect(await screen.findByText("1 change saved as version 2, now in force. The unit's nurses are told the roster has changed.")).toBeInTheDocument()
      expect(calls.find((call) => call.method === "POST" && call.url.endsWith("/roster/versions"))!.body).toEqual({
        month: "2026-06", edits: [{ hospital_id: "h1", date: "2026-06-05", action: "shift", shift: "N" }], reason: "Amal covers the night." })
      expect(calls.some((call) => call.url.endsWith("/roster/pins"))).toBe(false)
      await waitFor(() => expect(screen.getByRole("button", { name: "Amal Saleh, 5 June: Night, pinned" })).toHaveFocus())
      const list = await screen.findByRole("region", { name: "Versions" })
      expect(await within(list).findByText("Badr covers Amal's night.")).toBeInTheDocument()
    })

    it("a cell's menu offers a cover for each of the unit's shifts that day, which opens the form filled in", async () => {
      const { user, router } = start("/roster", manager({
        [`GET ${UNIT}/roster`]: { body: IN_FORCE },
        "GET /api/hospital/shifts": { body: [] }, [`GET ${UNIT}/rules`]: { body: { rules: { shifts: ["D", "N", "E"] } } },
        [`GET ${UNIT}/covers`]: { body: { month: "2026-06", covers: [] } },
      }))

      await user.click(await cellNamed("Amal Saleh, 5 June: Off"))
      const menu = await screen.findByRole("menu")
      expect(within(menu).getByText("One more nurse on 5 June")).toBeInTheDocument()
      expect(within(menu).getAllByRole("menuitem", { name: /^Open a cover/ }).map((item) => item.textContent)).toEqual([
        "Open a cover for Day", "Open a cover for Night", "Open a cover for Evening"])
      await user.click(within(menu).getByRole("menuitem", { name: "Open a cover for Night" }))

      expect(await screen.findByLabelText("Date")).toHaveValue("2026-06-05")
      await waitFor(() => expect(screen.getByLabelText("Shift")).toHaveValue("N"))
      expect(router.state.location.pathname).toBe("/unit-covers")
    })

    it("asks before leaving the roster in force with changes not saved, naming its month and not a draft", async () => {
      const { user } = start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: IN_FORCE }, [`POST ${UNIT}/roster/check`]: { body: CHECK } }))
      await user.click(await cellNamed("Amal Saleh, 5 June: Off"))
      await user.click(await screen.findByRole("menuitem", { name: /Pin Night/ }))

      await user.click(screen.getByRole("link", { name: "Solve history" }))

      const question = await screen.findByRole("alertdialog", { name: "Leave without saving your changes?" })
      expect(question).toHaveTextContent("Your changes to the roster of June 2026 are not saved yet. If you leave, they are lost.")
      // The page's opening lines say how drafts work, whatever the month's state; nothing else speaks of one.
      const intro = screen.getByText(/^Solve a month to get its draft/)
      expect(screen.queryAllByText(/draft/i).filter((element) => element !== intro)).toEqual([])
    })

    it("shows the refusal of changes that leave every cell as it is, keeping them", async () => {
      const nothing = "These changes leave every cell of the roster in force as it is, so there is no new version to save. Discard them, or change a cell."
      const { user } = start("/roster", manager({
        [`GET ${UNIT}/roster`]: { body: IN_FORCE },
        [`POST ${UNIT}/roster/check`]: { body: CHECK },
        [`POST ${UNIT}/roster/versions`]: { status: 409, body: { detail: nothing } },
      }))

      await user.click(await cellNamed("Amal Saleh, 1 June: Day"))
      await user.click(await screen.findByRole("menuitem", { name: /Pin Day/ }))
      const unsaved = screen.getByRole("region", { name: "1 change not saved yet" })
      await user.type(within(unsaved).getByLabelText("Why"), "Amal keeps her day.")
      await user.click(within(unsaved).getByRole("button", { name: "Save as a new version" }))

      expect(await within(unsaved).findByRole("alert")).toHaveTextContent(nothing)
      expect(screen.getByRole("region", { name: "1 change not saved yet" })).toBeInTheDocument()
      expect(within(unsaved).getByLabelText("Why")).toHaveValue("Amal keeps her day.")
      expect(screen.queryByText(/saved as version/)).not.toBeInTheDocument()
    })

    it("shows a refusal of the reason beside it, keeping the changes", async () => {
      const { user } = start("/roster", manager({
        [`GET ${UNIT}/roster`]: { body: IN_FORCE },
        [`POST ${UNIT}/roster/check`]: { body: CHECK },
        [`POST ${UNIT}/roster/versions`]: { status: 422, body: { detail: [{ loc: ["body", "body", "reason"], msg: "Say why, for the version list.", type: "value_error" }] } },
      }))

      await user.click(await cellNamed("Amal Saleh, 5 June: Off"))
      await user.click(await screen.findByRole("menuitem", { name: /Pin Night/ }))
      const unsaved = screen.getByRole("region", { name: "1 change not saved yet" })
      await user.type(within(unsaved).getByLabelText("Why"), "   x")
      await user.click(within(unsaved).getByRole("button", { name: "Save as a new version" }))

      expect(await within(unsaved).findByText("Say why, for the version list.")).toBeInTheDocument()
      expect(screen.getByRole("region", { name: "1 change not saved yet" })).toBeInTheDocument()
    })

    it("lists the versions with who, when, why and how much changed, and compares the latest two", async () => {
      const { user, calls } = start("/roster", manager({
        [`GET ${UNIT}/roster`]: { body: { ...IN_FORCE, approved: { ...IN_FORCE.approved, version: 3 } } },
        [`GET ${UNIT}/roster/versions`]: { body: [VERSION_3, VERSION_2, VERSION_1] },
        [`GET ${UNIT}/roster/versions/compare`]: compared,
      }))

      const list = await screen.findByRole("region", { name: "Versions" })
      const rows = within(within(list).getAllByRole("table")[0]).getAllByRole("row").slice(1)
      expect(rows.map((row) => within(row).getAllByRole("cell").map((cell) => cell.textContent))).toEqual([
        ["3 (in force)", "Changed by hand", "Mona Ali", "2026-06-04 09:00:00", "1", "Leave recorded for Amal Saleh took their shift off 7 June 2026.", "PrintCSVExcel"],
        ["2", "Changed by hand", "Mona Ali", "2026-06-03 08:30:00", "2", "Badr covers Amal's night.", "PrintCSVExcel"],
        ["1", "Approved from a solve", "Dina Deputy", "2026-05-16 12:00:00", "—", "—", "PrintCSVExcel"],
      ])
      expect(within(list).getByLabelText("From")).toHaveValue("2")
      expect(within(list).getByLabelText("To")).toHaveValue("3")
      expect(await within(list).findByText("1 cell differs from version 2 to version 3.")).toBeInTheDocument()
      const changed = within(list).getByRole("table", { name: "Cells that differ from version 2 to version 3" })
      expect(within(changed).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["Amal Saleh", "7 June", "N", "Off"])

      await user.selectOptions(within(list).getByLabelText("From"), "1")

      expect(await within(list).findByRole("table", { name: "Cells that differ from version 1 to version 3" })).toBeInTheDocument()
      expect(calls.map((call) => call.url)).toContain(`${UNIT}/roster/versions/compare?month=2026-06&from_version=1&to_version=3`)
    })

    it("offers each version's print page and its two files", async () => {
      start("/roster", manager({
        [`GET ${UNIT}/roster`]: { body: { ...IN_FORCE, approved: { ...IN_FORCE.approved, version: 2 } } },
        [`GET ${UNIT}/roster/versions`]: { body: [VERSION_2, VERSION_1] },
        [`GET ${UNIT}/roster/versions/compare`]: compared,
      }))

      const list = await screen.findByRole("region", { name: "Versions" })
      expect(within(list).getByRole("link", { name: "Print version 1" })).toHaveAttribute("href", "/roster/print?unit=u1&month=2026-06&version=1")
      const csv = within(list).getByRole("link", { name: "Version 1 as CSV" })
      expect(csv).toHaveAttribute("href", `${UNIT}/roster/export?month=2026-06&version=1&format=csv`)
      expect(csv).toHaveAttribute("download")
      expect(within(list).getByRole("link", { name: "Version 2 as Excel" })).toHaveAttribute("href", `${UNIT}/roster/export?month=2026-06&version=2&format=xlsx`)
    })

    it("offers to take a shift off a nurse no longer in the unit's month, and nothing to pin", async () => {
      const { user } = start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...IN_FORCE, draft: { ...DRAFT, editor: { ...EDITOR, gone: ["h2"] } } } } }))

      await user.click(await cellNamed("Badr Noor, 1 June: Night"))
      const menu = await screen.findByRole("menu")
      expect(menu).toHaveTextContent("No longer in this unit's month, so they work nothing here.")
      // The cell's own choices; the shift-day's covers follow them.
      expect(within(menu).getAllByRole("menuitem").map((item) => item.textContent).filter((text) => !text?.startsWith("Open a cover")))
        .toEqual(["Take the shift off this day"])
    })

    it("lets an approver read the versions and compare them, with nothing to edit", async () => {
      start("/unit-roster?unit=u1&month=2026-06", {
        ...session(ME_DEPUTY),
        "GET /api/units": { body: [{ id: "u1", name: "ER", archived: false, manager: null, rules_need_review: false }] },
        [`GET ${UNIT}/roster`]: { body: { ...IN_FORCE, approve_refusal: "This roster has been approved already by Dina Deputy.",
          draft: { ...DRAFT, editor: { ...EDITOR, refusal: "Only the unit's manager changes its roster or submits it." } } } },
        [`GET ${UNIT}/solve-history/${SOLVE_ID}`]: { body: RESULT },
        [`GET ${UNIT}/roster/versions`]: { body: [VERSION_2, VERSION_1] },
        [`GET ${UNIT}/roster/versions/compare`]: compared,
      })

      const list = await screen.findByRole("region", { name: "Versions" })
      expect(within(list).getByText("Badr covers Amal's night.")).toBeInTheDocument()
      expect(await within(list).findByRole("table", { name: "Cells that differ from version 1 to version 2" })).toBeInTheDocument()
      expect(screen.queryByRole("button", { name: /Amal Saleh, 1 June/ })).not.toBeInTheDocument()
    })
  })

  describe("re-planning from a date", () => {
    const IN_FORCE = {
      ...DRAFTED, state: "approved", submit_refusal: "This roster is approved and in force, so no solve or restore can change it.",
      approved: { approved_by: "Dina Deputy", approved_at: "2026-05-16T12:00:00+03:00", version: 2 }, draft: DRAFT,
    }
    const REPLAN_PREVIEW = {
      month: "2026-06", replan_from: "2026-06-12", earliest: "2026-06-12", latest: "2026-06-30", in_force: 2,
      summary: { nurses: 4, leave_days: 3, outside_days: 0, pins: 2 }, recorded_cells_differ: 1, change_weight: 3, precheck: [],
      rules_refusal: null, refusal: null, date_refusal: null, time_limit_default: 60, time_limit_cap: 300,
    }
    const REPLANNING = { ...RUNNING, replan_from: "2026-06-12", replan_outcome: null }
    const replanner = (extra = {}) => manager({
      [`GET ${UNIT}/roster`]: { body: IN_FORCE },
      [`GET ${UNIT}/replans/preview`]: { body: REPLAN_PREVIEW },
      ...extra,
    })

    it("offers a re-plan of the approved month from today, saying what it keeps and what it is given", async () => {
      start("/roster", replanner())

      const card = await screen.findByRole("region", { name: "Re-plan from a date" })
      expect(await within(card).findByLabelText("Re-plan from")).toHaveValue("2026-06-12")
      expect(within(card).getByLabelText("Re-plan from")).toHaveAttribute("min", "2026-06-12")
      expect(within(card).getByLabelText("Re-plan from")).toHaveAttribute("max", "2026-06-30")
      expect(within(card).getByText(/Days before the date are kept as they happened/)).toBeInTheDocument()
      expect(within(card).getByText(/1 cell before the date already differs from version 2/)).toBeInTheDocument()
      expect(within(card).getByLabelText("Time limit (seconds)")).toHaveValue(60)
      expect(screen.queryByRole("region", { name: "What the solver will receive" })).not.toBeInTheDocument()
    })

    it("starts one with the date, the time limit and the reason, and follows it as a re-plan", async () => {
      let started = false
      const { user, calls } = start("/roster", replanner({
        [`GET ${UNIT}/roster`]: () => ({ body: started ? { ...IN_FORCE, active_solve: REPLANNING } : IN_FORCE }),
        [`POST ${UNIT}/replans`]: () => { started = true; return { status: 201, body: REPLANNING } },
        [`GET ${UNIT}/solves/${SOLVE_ID}`]: { body: REPLANNING },
      }))

      const card = await screen.findByRole("region", { name: "Re-plan from a date" })
      await user.type(await within(card).findByLabelText("Why (optional)"), "Badr is off sick.")
      await user.click(within(card).getByRole("button", { name: "Re-plan from 12 June" }))

      expect(await screen.findByRole("heading", { name: "Re-planning June 2026 from 12 June" })).toBeInTheDocument()
      expect(calls.find((call) => call.method === "POST" && call.url.endsWith("/replans"))!.body).toEqual({
        month: "2026-06", replan_from: "2026-06-12", time_limit_seconds: 60, reason: "Badr is off sick." })
      expect(screen.getByRole("button", { name: "Stop and put the best roster in force" })).toBeInTheDocument()
      expect(screen.getByText(/A re-plan of this month is under way/)).toBeInTheDocument()
    })

    it("asks the preview again for the date chosen, and shows why a date cannot be one", async () => {
      const over = "11 June 2026 is over. Re-plan from 12 June 2026 or later: the days before then are facts."
      const { user, calls } = start("/roster", replanner({
        [`GET ${UNIT}/replans/preview`]: ({ url }: { url: string }) => (url.includes("replan_from=2026-06-11")
          ? { body: { ...REPLAN_PREVIEW, replan_from: "2026-06-11", date_refusal: over, recorded_cells_differ: null } }
          : { body: REPLAN_PREVIEW }),
      }))

      const card = await screen.findByRole("region", { name: "Re-plan from a date" })
      const date = await within(card).findByLabelText("Re-plan from")
      await user.clear(date)
      await user.type(date, "2026-06-11")

      expect(await within(card).findByText(over)).toBeInTheDocument()
      expect(calls.map((call) => call.url)).toContain(`${UNIT}/replans/preview?month=2026-06&replan_from=2026-06-11`)
      expect(within(card).getByRole("button", { name: /Re-plan from/ })).toHaveAttribute("aria-disabled", "true")
    })

    it("says why no re-plan can start, and offers none", async () => {
      start("/roster", replanner({
        [`GET ${UNIT}/replans/preview`]: { body: { ...REPLAN_PREVIEW, refusal: "This month is over, so its roster in force cannot be re-planned." } },
      }))

      const card = await screen.findByRole("region", { name: "Re-plan from a date" })
      expect(await within(card).findByText("This month is over, so its roster in force cannot be re-planned.")).toBeInTheDocument()
      expect(within(card).queryByRole("button")).not.toBeInTheDocument()
    })

    it("warns when nothing keeps the re-plan close to the roster in force", async () => {
      start("/roster", replanner({ [`GET ${UNIT}/replans/preview`]: { body: { ...REPLAN_PREVIEW, change_weight: 0 } } }))

      expect(await screen.findByText(/The change weight is 0/)).toBeInTheDocument()
    })

    it("says what each end of a re-plan did to the roster in force", async () => {
      const ended = { ...REPLANNING, state: "succeeded", finished_at: "2026-06-12T10:01:00+03:00" }
      const words: [object, RegExp][] = [
        [{ ...ended, found_roster: false }, /ended with no roster to keep, so the roster in force is as it was/],
        [{ ...ended, found_roster: true, replan_outcome: "unchanged" }, /changed no cell, so the roster in force is as it was/],
        [{ ...ended, found_roster: true, replan_outcome: "outdated" }, /roster in force or the month changed while the re-plan ran/],
        [{ ...ended, found_roster: true, replan_outcome: "month_over" }, /month ended while the re-plan ran/],
        [{ ...ended, state: "cancelled", found_roster: false, best_cost: null }, /re-plan you stopped had found no roster yet/],
        [{ ...ended, state: "cancelled", found_roster: false, best_cost: 90 }, /re-plan you stopped had found no roster it could keep/],
      ]
      for (const [last, said] of words) {
        start("/roster", replanner({ [`GET ${UNIT}/roster`]: { body: { ...IN_FORCE, last_solve: last } } }))
        expect(await screen.findByText(said)).toBeInTheDocument()
        cleanup()
      }
    })

    it("tells the manager when a re-plan put its roster in force", async () => {
      let progress: object = REPLANNING
      start("/roster", replanner({
        [`GET ${UNIT}/roster`]: () => ({ body: progress === REPLANNING ? { ...IN_FORCE, active_solve: REPLANNING }
          : { ...IN_FORCE, approved: { ...IN_FORCE.approved, version: 3 } } }),
        [`GET ${UNIT}/solves/${SOLVE_ID}`]: () => ({ body: progress }),
      }))

      await screen.findByRole("heading", { name: "Re-planning June 2026 from 12 June" })
      progress = { ...REPLANNING, state: "succeeded", found_roster: true, best_cost: 40, replan_outcome: "version" }

      expect(await screen.findByText("The re-plan finished with a cost of 40, and its roster is now in force as a new version. The unit's nurses are told the roster has changed.")).toBeInTheDocument()
    })
  })

  describe("a re-plan's result", () => {
    const COMPARISON = {
      replan_from: "2026-06-12", recorded_cells_differ: 1, based_on: 2, version: 3, outcome: "version",
      changes: [
        { hospital_id: "h1", full_name: "Amal Saleh", date: "2026-06-15", before: "", after: "D" },
        { hospital_id: "h2", full_name: "Badr Noor", date: "2026-06-15", before: "D", after: "" },
      ],
      cost_before: 116, cost_after: 120,
      rules: [{ rule: "change", before: 0, after: 6 }, { rule: "fair_day", before: 10, after: 8 }],
    }
    const REPLAN_ROW = { ...ROW, replan_from: "2026-06-12", replan_outcome: "version" }
    const FACT = { rule: "night_rest", hospital_id: "h1", full_name: "Amal Saleh", date: "2026-06-05", text: "Amal Saleh works the 5th after a night" }
    const REPLANNED = { ...RESULT, solve: REPLAN_ROW, replan: COMPARISON, elapsed_violations: [FACT], restore_refusal: "This roster is approved and in force." }

    it("compares it with the roster in force it started from, and lists elapsed violations as facts", async () => {
      start(`/solve-history/${SOLVE_ID}`, manager({ [`GET ${UNIT}/solve-history/${SOLVE_ID}`]: { body: REPLANNED } }))

      const panel = await screen.findByRole("region", { name: "Against the roster in force" })
      expect(within(panel).getByText(/Re-planned from 12 June, keeping every earlier day of version 2 as it happened/)).toBeInTheDocument()
      expect(within(panel).getByText(/Its roster is version 3, in force/)).toBeInTheDocument()
      expect(within(panel).getByText("Cost before").nextElementSibling).toHaveTextContent("116")
      expect(within(panel).getByText("Cost after").nextElementSibling).toHaveTextContent("120")
      const cells = within(panel).getByRole("table", { name: "Cells it changed" })
      expect(within(cells).getAllByRole("row")).toHaveLength(3)
      expect(within(cells).getByRole("rowheader", { name: "Badr Noor" }).parentElement).toHaveTextContent("Badr Noor15 JuneDayOff")
      const rules = within(panel).getByRole("table", { name: "Each rule, before and after" })
      expect(within(rules).getByRole("rowheader", { name: /Changes in a re-plan/ }).parentElement).toHaveTextContent("06+6")
      expect(within(panel).getByText(/1 cell before the date differed from version 2/)).toBeInTheDocument()
      const facts = screen.getByRole("region", { name: "Recorded facts that broke rules" })
      expect(within(facts).getByText(/Amal Saleh works the 5th after a night/)).toBeInTheDocument()
      expect(screen.getByText("Re-planned from").nextElementSibling).toHaveTextContent("12 June")
      expect(screen.queryByText(/the draft/)).not.toBeInTheDocument()
      expect(screen.getByRole("link", { name: "See the roster in force" })).toHaveAttribute("href", "/roster?month=2026-06")
    })

    it("says when it was not put in force, and why", async () => {
      start(`/solve-history/${SOLVE_ID}`, manager({ [`GET ${UNIT}/solve-history/${SOLVE_ID}`]: {
        body: { ...REPLANNED, solve: { ...REPLAN_ROW, replan_outcome: "outdated", in_draft: false }, replan: { ...COMPARISON, version: null, outcome: "outdated" } },
      } }))

      const panel = await screen.findByRole("region", { name: "Against the roster in force" })
      expect(within(panel).getByText(/was not put in force: the roster in force or the month changed while it ran/)).toBeInTheDocument()
    })

    it("names each re-plan in the history with what it did", async () => {
      start("/solve-history?month=2026-06", manager({
        [`GET ${UNIT}/solve-history`]: { body: { solves: [REPLAN_ROW, { ...REPLAN_ROW, id: "r2", replan_outcome: "outdated", in_draft: false }, { ...ROW, in_draft: false, replan_from: null, replan_outcome: null }], older_than: null } },
      }))

      expect(await screen.findByText("Re-plan from 12 June: made a new version")).toBeInTheDocument()
      expect(screen.getByText("Re-plan from 12 June: not applied, the roster changed meanwhile")).toBeInTheDocument()
      expect(screen.getByText("Finished")).toBeInTheDocument()
    })
  })

  describe("its approval", () => {
    const FROZEN = "This roster is submitted and waits for the CNO or the Deputy CNO, so nothing can change it until they decide. A rejection returns it to draft."
    const WAITING = { submitted_by: "Mona Ali", submitted_at: "2026-05-16T09:00:00+03:00", new_since: [] as object[] }
    const SUBMITTED = {
      ...DRAFTED, state: "submitted", submitted: WAITING, submit_refusal: FROZEN, draft: { ...DRAFT, editor: { ...EDITOR, refusal: FROZEN } },
    }
    const REJECTION = { rejected_by: "Dina Deputy", rejected_at: "2026-05-16T11:00:00+03:00", comment: "Too many nights for Amal.\nSee the 5th." }
    const APPROVED = {
      ...SUBMITTED, state: "approved", submitted: null, approved: { approved_by: "Dina Deputy", approved_at: "2026-05-16T12:00:00+03:00", version: 1 },
      submit_refusal: "This roster is approved and in force, so it cannot change here.",
    }

    it("submits the draft, freezes it, and moves focus to where it now stands", async () => {
      const { user, calls } = start("/roster", manager({
        [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, draft: DRAFT } },
        [`POST ${UNIT}/roster/submit`]: { body: SUBMITTED },
      }))

      const form = await screen.findByRole("region", { name: "Submit for approval" })
      await user.click(within(form).getByRole("button", { name: "Submit for approval" }))

      const status = await screen.findByText(/^Submitted 2026-05-16 09:00:00 by Mona Ali, waiting for a decision/)
      await waitFor(() => expect(status).toHaveFocus())
      expect(status).toHaveTextContent("Nothing can change it until they decide.")
      expect(calls.find((call) => call.method === "POST")?.body).toEqual({ month: "2026-06" })
      expect(screen.getByRole("status")).toHaveTextContent("Submitted. The CNO and the Deputy CNO can now see it and decide.")
      expect(screen.getByRole("heading", { name: "Submitted roster" })).toBeInTheDocument()
      expect(screen.getByText(FROZEN)).toBeInTheDocument()
      expect(screen.queryByRole("region", { name: "Submit for approval" })).not.toBeInTheDocument()
      expect(screen.queryByRole("region", { name: "What the solver will receive" })).not.toBeInTheDocument()
      expect(screen.queryByRole("button", { name: /Amal Saleh, 1 June/ })).not.toBeInTheDocument()
    })

    it("says why a draft cannot be submitted, before the press", async () => {
      const refusal = "This draft breaks 1 hard rule on the month as it stands, listed with it. Change it by hand or solve again, then submit it."
      start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, submit_refusal: refusal, draft: DRAFT } } }))

      const form = await screen.findByRole("region", { name: "Submit for approval" })
      expect(within(form).getByText(refusal)).toBeInTheDocument()
      expect(within(form).getByRole("button", { name: "Submit for approval" })).toHaveAttribute("aria-disabled", "true")
    })

    it("shows a refusal the server gives at the press", async () => {
      const { user } = start("/roster", manager({
        [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, draft: DRAFT } },
        [`POST ${UNIT}/roster/submit`]: { status: 409, body: { detail: "A solve of this month is queued or running." } },
      }))

      await user.click(await screen.findByRole("button", { name: "Submit for approval" }))

      expect(await screen.findByRole("alert")).toHaveTextContent("A solve of this month is queued or running.")
    })

    it("shows the manager the rejection's comment with the draft, and keeps it through the next submit", async () => {
      start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...DRAFTED, last_rejection: REJECTION, draft: DRAFT } } }))

      const comment = await screen.findByText(/Too many nights for Amal/)
      expect(comment.closest("figure")).toHaveTextContent("Rejected 2026-05-16 11:00:00 by Dina Deputy, who said:")
      expect(comment).toHaveTextContent("See the 5th.")
      cleanup()

      start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...SUBMITTED, last_rejection: REJECTION } } }))
      expect((await screen.findByText(/Too many nights for Amal/)).closest("figure")).toHaveTextContent("Rejected before this submit")
    })

    it("says when it was approved and which version is in force", async () => {
      start("/roster", manager({ [`GET ${UNIT}/roster`]: { body: { ...APPROVED, last_rejection: REJECTION } } }))

      expect(await screen.findByText(/^Approved 2026-05-16 12:00:00 by Dina Deputy. Version 1 is in force/)).toBeInTheDocument()
      expect(screen.getByRole("heading", { name: "Approved roster" })).toBeInTheDocument()
      expect(screen.queryByText(/Too many nights/)).not.toBeInTheDocument()
    })

    const ER_UNIT = { id: "u1", name: "ER", archived: false, manager: null, rules_need_review: false }
    const ICU_UNIT = { ...ER_UNIT, id: "u2", name: "ICU" }
    const FOR_APPROVER = {
      ...SUBMITTED, submit_refusal: "Only the unit's manager changes its roster or submits it.", approve_refusal: null, reject_refusal: null,
      draft: { ...DRAFT, editor: { ...EDITOR, refusal: "Only the unit's manager changes its roster or submits it." } },
    }
    const approver = (extra = {}) => ({
      ...session(ME_DEPUTY),
      "GET /api/units": { body: [ER_UNIT, ICU_UNIT] },
      [`GET ${UNIT}/roster`]: { body: FOR_APPROVER },
      [`GET ${UNIT}/solve-history/${SOLVE_ID}`]: { body: { ...RESULT, restore_refusal: "Only the unit's manager can restore a result." } },
      [`GET ${UNIT}/roster/versions`]: { body: [] },
      ...extra,
    })

    it("lists what waits on the approvers' home, oldest first as the answer has it, each a link to its roster", async () => {
      start("/", approver({
        "GET /api/rosters/submitted": { body: [
          { unit_id: "u2", unit: "ICU", month: "2026-06", submitted_by: "Omar Said", submitted_at: "2026-05-14T08:00:00+03:00", changed_since: true },
          { unit_id: "u1", unit: "ER", month: "2026-06", submitted_by: "Mona Ali", submitted_at: "2026-05-16T09:00:00+03:00", changed_since: false },
        ] },
      }))

      const waiting = await screen.findByRole("region", { name: "Waiting for a decision" })
      const items = within(waiting).getAllByRole("listitem")
      expect(items.map((item) => item.textContent)).toEqual([
        "ICU, June 2026, submitted 2026-05-14 08:00:00 by Omar Said. Something has changed since it was submitted.",
        "ER, June 2026, submitted 2026-05-16 09:00:00 by Mona Ali.",
      ])
      expect(within(items[1]).getByRole("link", { name: "ER, June 2026" })).toHaveAttribute("href", "/unit-roster?unit=u1&month=2026-06")
    })

    it("gives the approvers every unit's state per month, linking only what they may read", async () => {
      start("/rosters", approver({
        "GET /api/rosters/overview": { body: { months: ["2026-05", "2026-06", "2026-07", "2026-08"], units: [
          { id: "u1", name: "ER", archived: false, states: ["closed", "approved", "draft", "none"] },
          { id: "u2", name: "ICU", archived: false, states: ["none", "submitted", "none", "none"] },
        ] } },
      }))

      const er = (await screen.findByRole("rowheader", { name: "ER" })).closest("tr") as HTMLElement
      expect(within(er).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["Closed", "Approved", "Draft", "No roster"])
      expect(within(er).getByRole("link", { name: "Closed" })).toHaveAttribute("href", "/unit-roster?unit=u1&month=2026-05")
      expect(within(er).getByRole("link", { name: "Approved" })).toHaveAttribute("href", "/unit-roster?unit=u1&month=2026-06")
      expect(within(er).queryByRole("link", { name: "Draft" })).not.toBeInTheDocument()
      const icu = screen.getByRole("rowheader", { name: "ICU" }).closest("tr") as HTMLElement
      expect(within(icu).getByRole("link", { name: "Submitted" })).toHaveAttribute("href", "/unit-roster?unit=u2&month=2026-06")
      expect(screen.getByRole("heading", { name: "May 2026 to August 2026" })).toBeInTheDocument()
    })

    it("lets an approver read a submitted roster, with what changed since it was submitted, and nothing to edit", async () => {
      const changes = [{ hospital_id: "h1", full_name: "Amal Saleh", what: "leave" }, { hospital_id: "h2", full_name: "Badr Noor", what: "gender" }]
      start("/unit-roster?unit=u1&month=2026-06", approver({ [`GET ${UNIT}/roster?month=2026-06`]: { body: {
        ...FOR_APPROVER, submitted: { ...WAITING, new_since: [changes[0]] }, draft: { ...FOR_APPROVER.draft, changes },
      } } }))

      const grid = await screen.findByRole("region", { name: "Submitted roster of ER, June 2026" })
      expect(within(grid).queryAllByRole("button")).toEqual([])
      expect(screen.getByText(/Amal Saleh: leave or dates changed/)).toHaveTextContent(", since it was submitted")
      expect(screen.getByText(/Badr Noor: gender was corrected/)).not.toHaveTextContent("since it was submitted")
      expect(screen.getByText(/^Submitted 2026-05-16 09:00:00 by Mona Ali/)).not.toHaveTextContent("Nothing can change it")
      expect(await screen.findByRole("region", { name: "Penalties per rule" })).toBeInTheDocument()
      expect(screen.queryByRole("button", { name: "Restore" })).not.toBeInTheDocument()
    })

    it("tells an approver the month's Ramadan days changed since it was submitted", async () => {
      const changes = [{ hospital_id: "", full_name: "", what: "ramadan_days" }, { hospital_id: "", full_name: "", what: "public_holidays" }]
      start("/unit-roster?unit=u1&month=2026-06", approver({ [`GET ${UNIT}/roster?month=2026-06`]: { body: {
        ...FOR_APPROVER, submitted: { ...WAITING, new_since: [changes[0]] }, draft: { ...FOR_APPROVER.draft, changes },
      } } }))

      expect(await screen.findByText(/^The Ramadan days of June 2026 changed/)).toHaveTextContent(", since it was submitted")
      expect(screen.getByText(/^The public holidays of June 2026 changed/)).not.toHaveTextContent("since it was submitted")
    })

    it("approves after asking, and moves focus to where it now stands", async () => {
      const { user, calls } = start("/unit-roster?unit=u1&month=2026-06", approver({
        [`GET ${UNIT}/roster?month=2026-06`]: { body: FOR_APPROVER },
        [`POST ${UNIT}/roster/approve`]: { body: { ...APPROVED, approve_refusal: "This roster has been approved already by Deputy." } },
        "GET /api/rosters/overview": { body: { months: [], units: [] } },
        "GET /api/rosters/submitted": { body: [] },
      }))

      await user.click(await screen.findByRole("button", { name: "Approve" }))
      const dialog = await screen.findByRole("alertdialog", { name: "Approve the roster of June 2026?" })
      await user.click(within(dialog).getByRole("button", { name: "Approve" }))

      const status = await screen.findByText(/^Approved 2026-05-16 12:00:00 by Dina Deputy/)
      await waitFor(() => expect(status).toHaveFocus())
      expect(calls.filter((call) => call.method === "POST").map((call) => [call.url, call.body])).toEqual([[`${UNIT}/roster/approve`, { month: "2026-06" }]])
      expect(screen.getByRole("status")).toHaveTextContent("Approved. The roster of June 2026 is in force, and the unit's nurses see it.")
      expect(screen.queryByRole("region", { name: "Your decision" })).not.toBeInTheDocument()
    })

    it("cannot approve what the server says it may not, and says why", async () => {
      const refusal = "This roster breaks 1 hard rule on the month as it stands, listed with it. Reject it with a comment, so its manager can put it right."
      start("/unit-roster?unit=u1&month=2026-06", approver({ [`GET ${UNIT}/roster?month=2026-06`]: { body: { ...FOR_APPROVER, approve_refusal: refusal } } }))

      const decision = await screen.findByRole("region", { name: "Your decision" })
      expect(within(decision).getByText(refusal)).toBeInTheDocument()
      expect(within(decision).getByRole("button", { name: "Approve" })).toHaveAttribute("aria-disabled", "true")
      expect(within(decision).getByRole("button", { name: "Reject with this comment" })).toHaveAttribute("aria-disabled", "false")
    })

    it("cannot approve a month that is over, says so beside Approve, and still offers Reject", async () => {
      const late = "This month is over, so its roster cannot be approved. Reject it with a comment, so it no longer waits for a decision."
      start("/unit-roster?unit=u1&month=2026-06", approver({ [`GET ${UNIT}/roster?month=2026-06`]: { body: { ...FOR_APPROVER, approve_refusal: late } } }))

      const decision = await screen.findByRole("region", { name: "Your decision" })
      expect(within(decision).getByText(late)).toBeInTheDocument()
      expect(within(decision).getByRole("button", { name: "Approve" })).toHaveAttribute("aria-disabled", "true")
      expect(within(decision).getByRole("button", { name: "Approve" })).toHaveAccessibleDescription(late)
      expect(within(decision).getByRole("button", { name: "Reject with this comment" })).toHaveAttribute("aria-disabled", "false")
    })

    it("says why in the dialog when the month ended while the page was open", async () => {
      const late = "This month is over, so its roster cannot be approved. Reject it with a comment, so it no longer waits for a decision."
      const { user } = start("/unit-roster?unit=u1&month=2026-06", approver({
        [`GET ${UNIT}/roster?month=2026-06`]: { body: FOR_APPROVER },
        [`POST ${UNIT}/roster/approve`]: { status: 409, body: { detail: late } },
      }))

      await user.click(await screen.findByRole("button", { name: "Approve" }))
      const dialog = await screen.findByRole("alertdialog", { name: "Approve the roster of June 2026?" })
      await user.click(within(dialog).getByRole("button", { name: "Approve" }))

      expect(await within(dialog).findByRole("alert")).toHaveTextContent(late)
    })

    it("rejects only with a comment, which it sends", async () => {
      const { user, calls } = start("/unit-roster?unit=u1&month=2026-06", approver({
        [`GET ${UNIT}/roster?month=2026-06`]: { body: FOR_APPROVER },
        [`POST ${UNIT}/roster/reject`]: { body: { ...FOR_APPROVER, state: "draft", submitted: null, draft: null } },
        "GET /api/rosters/overview": { body: { months: [], units: [] } },
        "GET /api/rosters/submitted": { body: [] },
      }))

      await user.click(await screen.findByRole("button", { name: "Reject with this comment" }))
      const comment = screen.getByLabelText("Why reject it")
      await waitFor(() => expect(comment).toHaveFocus())
      expect(comment).toHaveAccessibleDescription(/Say why, for the unit's manager./)
      expect(calls.some((call) => call.method === "POST")).toBe(false)

      await user.type(comment, "  Too many nights for Amal.  ")
      await user.click(screen.getByRole("button", { name: "Reject with this comment" }))

      const status = await screen.findByText("Rejected. The roster of June 2026 is a draft again, and its manager sees your comment.")
      await waitFor(() => expect(status).toHaveFocus())
      expect(calls.find((call) => call.method === "POST")?.body).toEqual({ month: "2026-06", comment: "Too many nights for Amal." })
      expect(screen.getByText("ER has a draft for June 2026, not submitted yet. It is its manager's until they submit it.")).toBeInTheDocument()
      expect(screen.queryByRole("region", { name: /roster of ER/ })).not.toBeInTheDocument()
    })

    it("says a month in draft is its manager's, and shows no grid", async () => {
      start("/unit-roster?unit=u1&month=2026-06", approver({ [`GET ${UNIT}/roster?month=2026-06`]: { body: {
        ...EMPTY, state: "draft", submit_refusal: "Only the unit's manager changes its roster or submits it.",
        approve_refusal: "This month has no roster waiting for a decision.", reject_refusal: "This month has no roster waiting for a decision.",
      } } }))

      expect(await screen.findByText("ER has a draft for June 2026, not submitted yet. It is its manager's until they submit it.")).toBeInTheDocument()
      expect(screen.queryByRole("region", { name: "Your decision" })).not.toBeInTheDocument()
    })

    it.each(["/rosters", "/unit-roster", "/day"])("%s is for the CNO and the Deputy CNO only", async (path) => {
      const { router } = start(path, manager({ "GET /api/units": { body: [] } }))

      await screen.findByText(/No unit is assigned to you yet/)
      expect(router.state.location.pathname).toBe("/")
    })
  })

  // One date across every unit, for the CNO and the Deputy CNO.
  describe("the Day view", () => {
    const ER_UNIT = { id: "u1", name: "ER", archived: false, manager: null, rules_need_review: false }
    const NURSE = (hospital_id: string, full_name: string, team_leader = false) => ({ hospital_id, full_name, team_leader })
    const SHIFT = { minimum: 1, maximum: 1, short: false, over: false, team_leader: null, both_genders: null }
    const ER_DAY = {
      id: "u1", name: "ER", archived: false, state: "approved", unchecked: null,
      figures: {
        shifts: [
          { ...SHIFT, code: "D", present: 0, short: true, team_leader: false, both_genders: false, nurses: [] },
          { ...SHIFT, code: "N", present: 3, maximum: 2, over: true, team_leader: true, both_genders: true,
            nurses: [NURSE("h1", "Amal Saleh", true), NURSE("h3", "Dana Ali"), NURSE("h2", "Badr Noor")] },
        ],
        on_leave: [{ hospital_id: "h4", full_name: "Emad Hassan" }],
        hours_counted: true, overtime: 36, overtime_cap: 80, with_overtime: 4,
      },
    }
    const ICU_DAY = {
      id: "u2", name: "ICU", archived: true, state: "closed", unchecked: null,
      figures: {
        shifts: [{ code: "E", present: 2, minimum: null, maximum: null, short: false, over: false, team_leader: null, both_genders: null,
          nurses: [NURSE("i1", "Huda Saad")] }],
        on_leave: [], hours_counted: false, overtime: 0, overtime_cap: 80, with_overtime: 0,
      },
    }
    const STATE_ONLY = [
      { id: "u3", name: "Ward B", archived: false, state: "submitted", unchecked: null, figures: null },
      { id: "u4", name: "Ward C", archived: false, state: "draft", unchecked: null, figures: null },
      { id: "u5", name: "Ward D", archived: false, state: "none", unchecked: null, figures: null },
    ]
    const DAY = {
      date: "2026-06-04", weekday: "Thursday", weekend: false, holiday: null,
      shifts: [{ code: "D", display_name: "Day" }, { code: "E", display_name: "Evening" }, { code: "N", display_name: "Night" }],
      units: [ER_DAY, ICU_DAY, ...STATE_ONLY],
      totals: { on_duty: [{ code: "D", on_duty: 0 }, { code: "E", on_duty: 2 }, { code: "N", on_duty: 3 }], on_leave: 1, overtime: 36, units_short: 1 },
    }
    const onDay = (extra = {}) => ({ ...session(ME_CNO), "GET /api/rosters/day": { body: DAY }, ...extra })
    const row = (name: string) => screen.getByRole("rowheader", { name: new RegExp(`^${name}`) }).closest("tr") as HTMLElement
    const cells = (name: string) => within(row(name)).getAllByRole("cell").map((cell) => cell.textContent)

    it.each([["CNO", ME_CNO], ["Deputy CNO", ME_DEPUTY]])("the %s opens it from the header, on today when no date is given", async (_, me) => {
      const { user, router, calls } = start("/", { ...session(me), "GET /api/rosters/submitted": { body: [] }, "GET /api/units": { body: [] },
        "GET /api/rosters/day": { body: DAY } })

      await user.click(within(await screen.findByRole("navigation", { name: "Main" })).getByRole("link", { name: "Day view" }))

      expect(await screen.findByRole("heading", { level: 2, name: "Thursday, 4 June 2026" })).toBeInTheDocument()
      expect(router.state.location.pathname).toBe("/day")
      expect(pageReads(calls)).toContain("/api/rosters/day")
    })

    it("is in the phone's Menu for the approvers and on no Home card", async () => {
      const { user } = start("/", { ...session(ME_DEPUTY), "GET /api/rosters/submitted": { body: [] } })
      await screen.findByText("No roster is waiting for a decision.")
      expect(within(screen.getByRole("main")).queryByRole("link", { name: "Day view" })).not.toBeInTheDocument()

      await user.click(screen.getByRole("button", { name: "Menu" }))

      expect(within(screen.getByRole("navigation", { name: "Main menu" })).getByRole("link", { name: "Day view" })).toHaveAttribute("href", "/day")
    })

    it("reads each unit's shifts in words, blank where a unit does not work one", async () => {
      start("/day?date=2026-06-04", onDay())

      const table = await screen.findByRole("region", { name: "Day view, Thursday, 4 June 2026" })
      expect(within(table).getAllByRole("columnheader").map((header) => header.textContent)).toEqual(
        ["Unit", "Day (D)", "Evening (E)", "Night (N)", "On leave", "Overtime this month"])
      expect(cells("ER")).toEqual(["0 of 1shortno team leader, one gender", "", "3 of 1–2over", "1", "36 h of 80, 4 nurses"])
      expect(cells("ICU")).toEqual(["", "2", "", "0", "Hours not counted"])
      expect(screen.getByRole("rowheader", { name: /ICU \(archived\)/ })).toBeInTheDocument()
    })

    it("gives a unit with no roster in force its state alone, Submitted a link to it", async () => {
      start("/day?date=2026-06-04", onDay())

      await screen.findByRole("region", { name: /^Day view/ })
      expect(cells("Ward B")).toEqual(["Submitted"])
      expect(within(row("Ward B")).getByRole("link", { name: "Submitted" })).toHaveAttribute("href", "/unit-roster?unit=u3&month=2026-06")
      expect(cells("Ward C")).toEqual(["Draft"])
      expect(within(row("Ward C")).queryByRole("link")).not.toBeInTheDocument()
      expect(cells("Ward D")).toEqual(["No roster"])
      expect(within(row("Ward D")).queryByRole("button")).not.toBeInTheDocument()
    })

    it("expands a unit to its names, team leaders marked, with no new request", async () => {
      const { user, calls } = start("/day?date=2026-06-04", onDay())
      const button = await screen.findByRole("button", { name: "Names in ER" })
      expect(button).toHaveAttribute("aria-expanded", "false")
      expect(screen.queryByText(/Amal Saleh/)).not.toBeInTheDocument()
      const before = pageReads(calls).length

      await user.click(button)

      expect(button).toHaveAttribute("aria-expanded", "true")
      const names = document.getElementById(button.getAttribute("aria-controls")!) as HTMLElement
      expect(within(names).getAllByRole("term").map((term) => term.textContent)).toEqual(["Day", "Night", "On leave"])
      expect(within(names).getAllByRole("definition").map((definition) => definition.textContent)).toEqual(
        ["Nobody", "Amal Saleh (team leader), Dana Ali, Badr Noor", "Emad Hassan"])
      expect(pageReads(calls).length).toBe(before)

      await user.click(button)
      expect(screen.queryByText(/Amal Saleh/)).not.toBeInTheDocument()
    })

    it("sums the hospital in a totals row", async () => {
      start("/day?date=2026-06-04", onDay())

      await screen.findByRole("region", { name: /^Day view/ })
      expect(cells("All units")).toEqual(["0", "2", "3", "1", "36 h"])
      expect(screen.getByRole("rowheader", { name: /^All units/ })).toHaveTextContent("1 unit short somewhere")
    })

    it("says a weekend day and a holiday by name in its heading", async () => {
      start("/day?date=2026-06-05", onDay({ "GET /api/rosters/day": { body: { ...DAY, date: "2026-06-05", weekday: "Friday", weekend: true } } }))
      expect(await screen.findByRole("heading", { level: 2, name: "Friday, 5 June 2026, weekend day" })).toBeInTheDocument()
      cleanup()

      start("/day?date=2026-06-04", onDay({ "GET /api/rosters/day": { body: { ...DAY, holiday: "Founding Day" } } }))
      expect(await screen.findByRole("heading", { level: 2, name: "Thursday, 4 June 2026, Founding Day" })).toBeInTheDocument()
    })

    it("moves a day at a time, and to a date chosen, keeping the day shown while the next one loads", async () => {
      const { user, router } = start("/day?date=2026-06-04", onDay({
        "GET /api/rosters/day": ({ url }: { url: string }) => ({ body: { ...DAY, date: new URL(url, "http://x").searchParams.get("date") } }),
      }))
      await screen.findByRole("heading", { level: 2, name: "Thursday, 4 June 2026" })
      const held = hold("GET /api/rosters/day?date=2026-06-05")

      await user.click(screen.getByRole("link", { name: "Later day" }))

      expect(router.state.location.search).toEqual({ date: "2026-06-05" })
      await held.waitFor(1)
      expect(screen.getByRole("heading", { level: 2, name: "Thursday, 4 June 2026" })).toBeInTheDocument()
      await held.answer({ body: { ...DAY, date: "2026-06-05" } })
      expect(await screen.findByRole("heading", { level: 2, name: "Friday, 5 June 2026" })).toBeInTheDocument()

      fireEvent.change(screen.getByLabelText("Date"), { target: { value: "2026-05-20" } })
      await waitFor(() => expect(router.state.location.search).toEqual({ date: "2026-05-20" }))
      expect(await screen.findByRole("heading", { level: 2, name: "Wednesday, 20 May 2026" })).toBeInTheDocument()

      await user.click(screen.getByRole("link", { name: "Earlier day" }))
      expect(await screen.findByRole("heading", { level: 2, name: "Tuesday, 19 May 2026" })).toBeInTheDocument()
    })

    it("links a unit to its roster on that month, where the date's column is marked", async () => {
      const unitRoster = {
        month: "2026-06", state: "approved", submitted: null, last_rejection: null, approved: null, version: 1,
        submit_refusal: "x", approve_refusal: "x", reject_refusal: "x", close: null,
        draft: {
          solve_id: "s1", solved_at: "2026-05-15T10:01:00+03:00", restored_at: null, cost: 1, stopped_early: false, changes: [], edited: false,
          solved_cost: 1, check: null, pins: [], pins_lost_to_leave: [], editor: null,
          days: [{ date: "2026-06-03", weekend: false, holiday: false }, { date: "2026-06-04", weekend: false, holiday: false }],
          shifts: [{ code: "N", display_name: "Night", colour: "#4338ca", hours: 12 }],
          rows: [{ hospital_id: "h1", full_name: "Amal Saleh", cells: [{ shift: "N", kind: "shift" }, { shift: "N", kind: "shift" }] }],
        },
      }
      const { user, router } = start("/day?date=2026-06-04", onDay({
        "GET /api/units": { body: [ER_UNIT] },
        "GET /api/units/u1/roster": { body: unitRoster },
        "GET /api/units/u1/roster/versions": { body: [] },
        "GET /api/units/u1/solve-history/s1": { status: 404, body: { detail: "Not found." } },
      }))
      const link = await screen.findByRole("link", { name: "ER" })
      expect(link).toHaveAttribute("href", "/unit-roster?unit=u1&month=2026-06&date=2026-06-04")

      await user.click(link)

      const grid = await screen.findByRole("region", { name: /roster of ER, June 2026/ })
      expect(router.state.location.search).toEqual({ unit: "u1", month: "2026-06", date: "2026-06-04" })
      const marked = within(grid).getAllByRole("columnheader").filter((header) => header.getAttribute("aria-current") === "date")
      expect(marked.map((header) => header.textContent)).toEqual(["4Thu, the Day view's date"])
    })

    it("marks no column on the Unit roster without a date", async () => {
      start("/unit-roster?unit=u1&month=2026-06", { ...session(ME_CNO), "GET /api/units": { body: [ER_UNIT] },
        "GET /api/units/u1/roster": { body: { month: "2026-06", state: "none", submitted: null, last_rejection: null, approved: null, version: null,
          submit_refusal: "x", approve_refusal: "x", reject_refusal: "x", draft: null, close: null } } })

      await screen.findByText("ER has no roster for June 2026 yet.")
      expect(document.querySelector("[aria-current=date]")).toBeNull()
    })

    it("says when it is loading, could not be read or has no units", async () => {
      start("/day", { ...session(ME_CNO) })
      const held = hold("GET /api/rosters/day")
      expect(await screen.findByText("Loading…")).toBeInTheDocument()
      await held.answer({ status: 500, body: {} })
      expect(await screen.findByRole("alert")).toHaveTextContent("The day could not be read. Reload the page to try again.")
      cleanup()

      start("/day", onDay({ "GET /api/rosters/day": { body: { ...DAY, units: [], shifts: [], totals: { on_duty: [], on_leave: 0, overtime: 0, units_short: 0 } } } }))
      expect(await screen.findByText("There are no units yet.")).toBeInTheDocument()
    })
  })

  describe("its month close", () => {
    const PROJECTED = { hours_balance: 4, ytd_overtime: 12, night_balance: 0.33, friday_balance: -0.5, source: "projected", out_of: "2026-06" }
    const CLOSED_OUT = { ...PROJECTED, hours_balance: -3, source: "closed" }
    const CLOSED = {
      month: "2026-06", close_refusal: null,
      closed: {
        closed_at: "2026-07-01T00:30:04+03:00", closed_by: null, version: 2, changed_since: true,
        facts_broke: [{ rule: "headcount", hospital_id: null, full_name: "", date: "2026-06-03", text: "headcount: D on 2026-06-03 has 0, needs 1" }],
      },
      carry_over: [{ hospital_id: "h1", full_name: "Amal Saleh", into: OPENING, out: CLOSED_OUT }],
    }

    it("shows each nurse's carry-over, where it comes from, and nothing to close before the month is over", async () => {
      start("/roster", manager({
        [`GET ${UNIT}/close`]: { body: { ...NOT_CLOSED, carry_over: [{ hospital_id: "h1", full_name: "Amal Saleh", into: OPENING, out: { ...PROJECTED, out_of: "2026-06" } }] } },
      }))

      const card = await screen.findByRole("region", { name: "Carry-over and month close" })
      const table = await within(card).findByRole("region", { name: "Carry-over, June 2026" })
      const amal = within(table).getByRole("rowheader", { name: "Amal Saleh" }).closest("tr") as HTMLElement
      expect(within(amal).getAllByRole("cell").map((cell) => cell.textContent)).toEqual([
        "0", "0", "0", "0", "Opening values", "4 owed", "12", "+0.33", "−0.50", "Projected from June 2026"])
      expect(within(table).getByRole("columnheader", { name: "June 2026 starts from" })).toBeInTheDocument()
      expect(within(table).getByRole("columnheader", { name: "It hands to July 2026" })).toBeInTheDocument()
      expect(within(card).queryByRole("button")).not.toBeInTheDocument()
      expect(within(card).queryByText(/not over yet/)).not.toBeInTheDocument()
    })

    it("names the nurse once on a fact the close says broke a rule", async () => {
      const fact = { rule: "consecutive", hospital_id: "h1", full_name: "Amal Saleh", date: null, text: "h1: 6 shifts in a row" }
      start("/roster", manager({ [`GET ${UNIT}/close`]: { body: { ...CLOSED, closed: { ...CLOSED.closed, facts_broke: [fact] } } } }))

      const card = await screen.findByRole("region", { name: "Carry-over and month close" })
      expect(await within(card).findByRole("listitem")).toHaveTextContent(/^Amal Saleh: 6 shifts in a row$/)
    })

    it("says what a closed month's facts broke, that they changed since, and closes it again", async () => {
      const { calls, user } = start("/roster", manager({
        [`GET ${UNIT}/close`]: { body: CLOSED },
        [`POST ${UNIT}/close`]: { body: { ...CLOSED, closed: { ...CLOSED.closed, closed_by: "Mona Ali", version: 2, changed_since: false } } },
      }))

      const card = await screen.findByRole("region", { name: "Carry-over and month close" })
      expect(await within(card).findByText("Closed 2026-07-01 00:30:04 by the schedule, with version 2 as the roster in force.")).toBeInTheDocument()
      expect(within(card).getByText(/broke a hard rule/)).toBeInTheDocument()
      expect(within(card).getByRole("listitem")).toHaveTextContent("3 June: headcount: D on 2026-06-03 has 0, needs 1")
      expect(within(card).getByText("Leave or a nurse's dates have changed since it closed. Close it again to take them in.")).toBeInTheDocument()

      await user.click(within(card).getByRole("button", { name: "Close June 2026 again" }))

      expect(await within(card).findByText("Closed 2026-07-01 00:30:04 by Mona Ali, with version 2 as the roster in force.")).toBeInTheDocument()
      expect(within(card).getByText("June 2026 is closed. Each nurse's carry-over below is this close's.")).toBeInTheDocument()
      expect(within(card).queryByText(/changed since it closed/)).not.toBeInTheDocument()
      expect(calls.filter((call) => call.method === "POST").map((call) => [call.url, call.body])).toEqual([[`${UNIT}/close`, { month: "2026-06" }]])
    })

    it("says why a month can no longer be closed again, and shows a refusal given at the press", async () => {
      const refusal = "July 2026 has closed, so June 2026 can no longer be closed again."
      start("/roster?month=2026-06", manager({ [`GET ${UNIT}/close`]: { body: { ...CLOSED, close_refusal: refusal } } }))
      const card = await screen.findByRole("region", { name: "Carry-over and month close" })
      expect(await within(card).findByText(refusal)).toBeInTheDocument()
      expect(within(card).queryByRole("button")).not.toBeInTheDocument()
      cleanup()

      const second = start("/roster", manager({
        [`GET ${UNIT}/close`]: { body: CLOSED },
        [`POST ${UNIT}/close`]: { status: 409, body: { detail: refusal } },
      }))
      const again = await screen.findByRole("region", { name: "Carry-over and month close" })
      await second.user.click(await within(again).findByRole("button", { name: "Close June 2026 again" }))
      expect(await within(again).findByRole("alert")).toHaveTextContent(refusal)
    })
  })
})

describe("a nurse's roster page", () => {
  const ROSTER = {
    approved_at: "2026-05-16T12:00:00+03:00", version: 1,
    days: [{ date: "2026-12-01", weekend: false, holiday: false }, { date: "2026-12-02", weekend: false, holiday: false }, { date: "2026-12-03", weekend: false, holiday: false }],
    shifts: [{ code: "D", display_name: "Day", colour: "#fde047", hours: 12 }],
    rows: [
      { hospital_id: "a1", full_name: "Amal Saleh", shifts: ["D", "", ""] },
      { hospital_id: "h123", full_name: "Hana Ali", shifts: ["", "D", ""] },
    ],
    mine: [{ date: "2026-12-01", kind: "leave" }, { date: "2026-12-02", kind: "shift" }, { date: "2026-12-03", kind: "off" }],
  }
  const reading = (answer: object, month = "") => nurseHome("u1", { [`GET /api/rosters/mine${month}`]: { body: answer } })

  it("shows the approved roster with their own row first, their leave as leave, and a colleague's empty day as a day off", async () => {
    start("/my-roster", reading({ ...NO_ROSTER_YET, roster: ROSTER }))

    const grid = await screen.findByRole("region", { name: "Roster of ER, December 2026" })
    const rows = within(grid).getAllByRole("rowheader").map((row) => row.textContent)
    expect(rows).toEqual(["Hana Ali (you)", "Amal Saleh"])
    const mine = within(within(grid).getByRole("rowheader", { name: "Hana Ali (you)" }).closest("tr") as HTMLElement).getAllByRole("cell")
    expect(mine.map((cell) => cell.getAttribute("title"))).toEqual(["Hana Ali (you), 1 December: Leave", "Hana Ali (you), 2 December: Day", "Hana Ali (you), 3 December: Off"])
    const amal = within(within(grid).getByRole("rowheader", { name: "Amal Saleh" }).closest("tr") as HTMLElement).getAllByRole("cell")
    expect(amal.map((cell) => cell.getAttribute("title"))).toEqual(["Amal Saleh, 1 December: Day", "Amal Saleh, 2 December: Off", "Amal Saleh, 3 December: Off"])
    expect(screen.getByText("Approved 2026-05-16 12:00:00.")).toBeInTheDocument()
  })

  it("shows a nurse on probation last, under its own heading with her preceptor's name", async () => {
    const rows = [...ROSTER.rows, { hospital_id: "f1", full_name: "Faten Ali", on_probation: true, preceptor_name: "Amal Saleh", shifts: ["D", "", ""] }]
    start("/my-roster", reading({ ...NO_ROSTER_YET, roster: { ...ROSTER, rows } }))

    const grid = await screen.findByRole("region", { name: "Roster of ER, December 2026" })
    const group = within(within(grid).getByRole("rowgroup", { name: "On probation" }))
    expect(group.getByRole("rowheader", { name: "Faten Ali, Preceptor: Amal Saleh" })).toBeInTheDocument()
    expect(group.queryByRole("rowheader", { name: "Hana Ali (you)" })).not.toBeInTheDocument()
    const faten = within(group.getByRole("rowheader", { name: /Faten Ali/ }).closest("tr") as HTMLElement).getAllByRole("cell")
    expect(faten.map((cell) => cell.getAttribute("title"))).toEqual(["Faten Ali, 1 December: Day", "Faten Ali, 2 December: Off", "Faten Ali, 3 December: Off"])
  })

  it("keeps a reader on probation under the heading, first of its rows, with no preceptor yet", async () => {
    const rows = [
      { ...ROSTER.rows[0] },
      { hospital_id: "f1", full_name: "Faten Ali", on_probation: true, preceptor_name: "Amal Saleh", shifts: ["D", "", ""] },
      { ...ROSTER.rows[1], on_probation: true, preceptor_name: null },
    ]
    start("/my-roster", reading({ ...NO_ROSTER_YET, roster: { ...ROSTER, rows } }))

    const grid = await screen.findByRole("region", { name: "Roster of ER, December 2026" })
    const group = within(within(grid).getByRole("rowgroup", { name: "On probation" }))
    expect(group.getAllByRole("rowheader").map((row) => row.getAttribute("aria-label") ?? row.textContent)).toEqual(["On probation", "Hana Ali (you), No preceptor", "Faten Ali, Preceptor: Amal Saleh"])
  })

  it("says there is no roster yet, and offers no month before this one", async () => {
    start("/my-roster", reading({ ...NO_ROSTER_YET, month: "2026-11" }))

    expect(await screen.findByText("No roster yet for November 2026.")).toBeInTheDocument()
    expect(screen.queryByRole("link", { name: "Previous month" })).not.toBeInTheDocument()
    expect(screen.getByRole("link", { name: "Next month" })).toHaveAttribute("href", "/my-roster?month=2026-12")
  })

  it("says when the reader is not on the roster they see", async () => {
    start("/my-roster", reading({ ...NO_ROSTER_YET, roster: { ...ROSTER, rows: [ROSTER.rows[0]], mine: null } }))

    expect(await screen.findByText("Approved 2026-05-16 12:00:00. You are not on it.")).toBeInTheDocument()
  })

  it("leads there from the home once the month is approved", async () => {
    start("/", reading({ ...NO_ROSTER_YET, roster: ROSTER }))

    const said = await screen.findByText(/The roster of December 2026 is approved/)
    expect(within(said).getByRole("link", { name: "See it under My roster" })).toHaveAttribute("href", "/my-roster")
  })
})

describe("the notification bell", () => {
  const bell = (unread: number) => ({ [`GET ${BELL}`]: { body: { unread } } })
  const NOTE = {
    id: "n1", event: "roster_approved", text: "Carla CNO approved the ER roster for December 2026.",
    link: { page: "my_roster", month: "2026-12", unit_id: null }, created_at: "2026-11-06T09:00:00+03:00", read: false,
  }
  const OLDER = { ...NOTE, id: "n0", event: "roster_rejected", text: "An older one, already read.", link: null, read: true }

  it("shows how many are unread and leads to them", async () => {
    start("/", nurseHome("u1", bell(3)))

    const link = await screen.findByRole("link", { name: "Notifications, 3 unread" })
    expect(link).toHaveAttribute("href", "/notifications")
  })

  it("says nothing of a count when none are unread", async () => {
    start("/", nurseHome("u1"))

    expect(await screen.findByRole("link", { name: "Notifications" })).toHaveAttribute("href", "/notifications")
  })

  it("is in every role's header", async () => {
    start("/", admin())

    await screen.findByRole("heading", { name: "Health" })
    expect(screen.getByRole("link", { name: "Notifications" })).toBeInTheDocument()
  })

  it("asks again by itself, so a new one shows without leaving the page", async () => {
    let unread = 0
    start("/", nurseHome("u1", { [`GET ${BELL}`]: () => ({ body: { unread } }) }))
    await screen.findByRole("link", { name: "Notifications" })

    unread = 1

    expect(await screen.findByRole("link", { name: "Notifications, 1 unread" })).toBeInTheDocument()
  })

  it("stops asking once it is refused", async () => {
    const { router } = start("/", nurseHome("u1", { [`GET ${BELL}`]: { status: 403, body: { detail: "Change your password first." } } }))

    await screen.findByText("No roster yet for December 2026.")
    const [query] = router.options.context.queryClient.getQueryCache().findAll({ queryKey: [BELL] })
    await waitFor(() => expect(query.state.status).toBe("error"))
    // The query's own answer about its next poll, because waiting to see that none comes could only pass more easily when busy.
    const options = query.observers[0].options as {
      refetchInterval: (of: typeof query) => number | false; refetchOnWindowFocus: (of: typeof query) => boolean
    }
    expect(options.refetchInterval(query)).toBe(false)
    expect(options.refetchOnWindowFocus(query)).toBe(false)
  })

  it("sends a session that has ended to the login page", async () => {
    const { router } = start("/", nurseHome("u1", { [`GET ${BELL}`]: { status: 401, body: { detail: "Unauthorized" } } }))

    expect(await screen.findByRole("button", { name: "Log in" })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe("/login")
  })

  it("lists them newest first, and following one marks it read", async () => {
    let read = false
    const { user, router, calls } = start("/notifications", {
      ...session(NURSE),
      ...bell(1),
      "GET /api/notifications": () => ({ body: { unread: read ? 0 : 1, items: [{ ...NOTE, read }, OLDER] } }),
      "POST /api/notifications/n1/read": () => { read = true; return { status: 204 } },
      "GET /api/rosters/mine": { body: NO_ROSTER_YET },
    })

    const items = within(await screen.findByRole("list", { name: "Notifications, newest first" })).getAllByRole("listitem")
    expect(items.map((item) => item.textContent)).toEqual([
      expect.stringContaining("New " + NOTE.text), expect.stringContaining(OLDER.text)])
    expect(document.title).toBe("Notifications · Nurse roster")
    expect(screen.getByRole("status", { name: "" })).toHaveTextContent("1 unread.")

    await user.click(within(items[0]).getByRole("link", { name: "Open My roster" }))

    await waitFor(() => expect(router.state.location.pathname).toBe("/my-roster"))
    expect(router.state.location.search).toEqual({ month: "2026-12" })
    await waitFor(() => expect(calls.some((call) => call.method === "POST" && call.url === "/api/notifications/n1/read")).toBe(true))
  })

  it("links an approver to the unit's roster and a manager to theirs", async () => {
    start("/notifications", {
      ...session(NURSE),
      "GET /api/notifications": { body: { unread: 2, items: [
        { ...NOTE, id: "a", link: { page: "unit_roster", month: "2026-12", unit_id: "u9" } },
        { ...NOTE, id: "b", link: { page: "roster", month: "2026-12", unit_id: null } },
      ] } },
    })

    const [approver, manager] = await screen.findAllByRole("link", { name: "Open the roster" })
    expect(approver).toHaveAttribute("href", "/unit-roster?unit=u9&month=2026-12")
    expect(manager).toHaveAttribute("href", "/roster?month=2026-12")
  })

  it("shows a manager that a month's dates changed, leading to their roster of it", async () => {
    const text = "Carla CNO changed the Ramadan days of March 2027. Check the ER roster for that month: it was planned on the dates before."
    start("/notifications", {
      ...session(NURSE),
      "GET /api/notifications": { body: { unread: 1, items: [
        { ...NOTE, event: "month_dates_changed", text, link: { page: "roster", month: "2027-03", unit_id: null } },
      ] } },
    })

    const [item] = within(await screen.findByRole("list", { name: "Notifications, newest first" })).getAllByRole("listitem")
    expect(item).toHaveTextContent(text)
    expect(within(item).getByRole("link", { name: "Open the roster" })).toHaveAttribute("href", "/roster?month=2027-03")
  })

  it("marks them all read, and the bell follows", async () => {
    let read = false
    const { user } = start("/notifications", {
      ...session(NURSE),
      [`GET ${BELL}`]: () => ({ body: { unread: read ? 0 : 1 } }),
      "GET /api/notifications": () => ({ body: { unread: read ? 0 : 1, items: [{ ...NOTE, read }] } }),
      "POST /api/notifications/read-all": () => { read = true; return { status: 204 } },
    })
    await screen.findByRole("link", { name: "Notifications, 1 unread" })

    await user.click(await screen.findByRole("button", { name: "Mark all as read" }))

    expect(await screen.findByText("All read.")).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole("status", { name: "" })).toHaveFocus())
    expect(await screen.findByRole("link", { name: "Notifications" })).toBeInTheDocument()
  })

  it("marks one read that leads nowhere", async () => {
    let read = false
    const { user } = start("/notifications", {
      ...session(NURSE),
      "GET /api/notifications": () => ({ body: { unread: read ? 0 : 1, items: [{ ...NOTE, link: null, read }] } }),
      "POST /api/notifications/n1/read": () => { read = true; return { status: 204 } },
    })

    await user.click(await screen.findByRole("button", { name: "Mark as read" }))

    expect(await screen.findByText("All read.")).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole("status", { name: "" })).toHaveFocus())
  })

  it("says when there are none", async () => {
    start("/notifications", { ...session(ME), "GET /api/notifications": { body: { unread: 0, items: [] } } })

    expect(await screen.findByText("You have no notifications.")).toBeInTheDocument()
  })
})

describe("a nurse's own month", () => {
  const LEAVE = { month: "2026-11", runs: [{ first: "2026-11-10", last: "2026-11-10", days: 1, leave_type: "Sick" }] }
  const IN_FORCE = {
    month: "2026-11", first_month: "2026-11", unit: "ER",
    roster: {
      version: 2, approved_at: "2026-10-28T10:00:00+03:00",
      hours: { worked: 204, leave_credit: 8, owed: 200, overtime: 4, carried: -6, shifts: [
        { code: "D", display_name: "Day", colour: "#ffcc00", count: 6 }, { code: "N", display_name: "Night", colour: "#3355aa", count: 11 }] },
    },
  }
  const mine = (month: unknown, leave: unknown = LEAVE) => ({
    ...session(NURSE), ...NO_REQUESTS, "GET /api/rosters/mine/month": { body: month }, "GET /api/nurses/my-skills": { body: [] }, "GET /api/nurses/my-leave": { body: leave },
  })
  const terms = () => Object.fromEntries(screen.getAllByRole("term").map((term) => [term.textContent, term.nextElementSibling?.textContent]))

  it("shows the hours and shifts of the roster in force beside the leave", async () => {
    start("/my-month", mine(IN_FORCE))

    expect(await screen.findByText(/From version 2 of the ER roster/)).toBeInTheDocument()
    expect(terms()).toEqual({
      "Hours worked": "204 hours", "Leave counted as worked": "8 hours", "Hours this month asks of you": "200 hours",
      "Overtime": "4 hours", "Carried into next month": "6 hours worked ahead",
    })
    expect(screen.getByText("6 Day")).toBeInTheDocument()
    expect(screen.getByText("11 Night")).toBeInTheDocument()
    expect(screen.getByRole("cell", { name: "Sick" })).toBeInTheDocument()
    expect(screen.getByRole("link", { name: "See it under My roster" })).toHaveAttribute("href", "/my-roster?month=2026-11")
  })

  it("tells a nurse on probation her preceptor and her probation's last day", async () => {
    start("/my-month", mine({ ...IN_FORCE, probation: { last_day: "2026-11-30", preceptor_name: "Amal Saleh" }, precepts: null }))

    const card = (await screen.findByRole("heading", { level: 3, name: "Probation" })).closest("[data-slot=card]") as HTMLElement
    expect(within(card).getByText("You are on probation in November 2026. Its last day is Monday, 30 November 2026.")).toBeInTheDocument()
    expect(within(card).getByText("Your preceptor is Amal Saleh. You work beside them on their day shifts, and work no night shift.")).toBeInTheDocument()
    expect(screen.queryByRole("heading", { name: "Precepting" })).not.toBeInTheDocument()
  })

  it("says when no preceptor is assigned yet", async () => {
    start("/my-month", mine({ ...NO_HOURS_YET, probation: { last_day: "2026-12-31", preceptor_name: null }, precepts: null }))

    expect(await screen.findByText(/No preceptor is assigned to you yet\. Your unit manager chooses one/)).toBeInTheDocument()
  })

  it("tells a preceptor the nurse on probation they are paired with", async () => {
    start("/my-month", mine({ ...IN_FORCE, probation: null, precepts: "Faten Ali" }))

    expect(await screen.findByText("In November 2026 you precept Faten Ali, a nurse on probation, who works beside you on your day shifts.")).toBeInTheDocument()
    expect(screen.queryByRole("heading", { name: "Probation" })).not.toBeInTheDocument()
  })

  it("says nothing of probation to anyone else", async () => {
    start("/my-month", mine({ ...IN_FORCE, probation: null, precepts: null }))

    await screen.findByText(/From version 2 of the ER roster/)
    expect(screen.queryByRole("heading", { name: "Probation" })).not.toBeInTheDocument()
    expect(screen.queryByRole("heading", { name: "Precepting" })).not.toBeInTheDocument()
  })

  it("shows a closed month's figures as what really happened", async () => {
    start("/my-month?month=2026-10", mine({ ...IN_FORCE, month: "2026-10", first_month: "2026-10", roster: { ...IN_FORCE.roster, closed: true } },
      { ...LEAVE, month: "2026-10" }))

    expect(await screen.findByText(/The month has closed: these are what really happened, with your leave\./)).toBeInTheDocument()
    expect(terms()["Carried into next month"]).toBe("6 hours worked ahead")
    expect(screen.queryByText(/worked out again when the month closes/)).not.toBeInTheDocument()
  })

  it("says hours owed into next month", async () => {
    start("/my-month", mine({ ...IN_FORCE, roster: { ...IN_FORCE.roster, hours: { ...IN_FORCE.roster.hours, carried: 4 } } }))

    expect(await screen.findByText("4 hours owed")).toBeInTheDocument()
  })

  it("shows nothing owed with the hours rule off", async () => {
    start("/my-month", mine({ ...IN_FORCE, roster: { ...IN_FORCE.roster, hours: { ...IN_FORCE.roster.hours, owed: null, carried: null, overtime: 0 } } }))

    await screen.findByText(/From version 2/)
    expect(Object.keys(terms())).toEqual(["Hours worked", "Leave counted as worked"])
  })

  it("waits for the approval", async () => {
    start("/my-month", mine(NO_HOURS_YET))

    expect(await screen.findByText("No roster yet for November 2026. Your hours show here once it is approved.")).toBeInTheDocument()
    expect(await screen.findByRole("cell", { name: "Sick" })).toBeInTheDocument()
  })

  it("shows a month already over with its leave and no hours", async () => {
    start("/my-month?month=2026-10", mine({ ...NO_HOURS_YET, month: "2026-10" }, { ...LEAVE, month: "2026-10" }))

    expect(await screen.findByText(/for a month already over once it has closed/)).toBeInTheDocument()
    expect(await screen.findByRole("cell", { name: "Sick" })).toBeInTheDocument()
  })

  it("says so for a nurse who is not on the roster yet", async () => {
    start("/my-month", mine({ ...IN_FORCE, roster: { ...IN_FORCE.roster, hours: null } }))

    expect(await screen.findByText("You are not on this roster.")).toBeInTheDocument()
  })

  it("shows the nurse's own skills with the last day each counts", async () => {
    start("/my-month", { ...mine(IN_FORCE), "GET /api/nurses/my-skills": { body: [BLS_HELD, { ...ICU_HELD, active: false }, SCFHS_HELD, BSN_HELD] } })

    const table = await screen.findByRole("region", { name: "Your skills" })
    expect(within(table).getAllByRole("row").map((row) => row.textContent)).toEqual([
      "NameExpiryIdentifierIssue date",
      "Skills", "BLS30 Jun 2027NoneNot recorded", "ICURetiredDoes not expireNoneNot recorded",
      "Certificates", "BSNDoes not expireNone15 Jun 2019",
      "Licences", "SCFHS31 Mar 202821-RN-04121 Apr 2026"])
    expect(within(table).getByRole("rowgroup", { name: "Licences" })).toBeInTheDocument()
    expect(screen.getByRole("heading", { level: 3, name: "Skills, certificates and licences" })).toBeInTheDocument()
    expect(screen.getByText(/your manager sees no identifier or issue date/)).toBeInTheDocument()
  })

  it("says so when no skill is recorded, or they cannot be read", async () => {
    start("/my-month", mine(IN_FORCE))
    expect(await screen.findByText("No skills are recorded for you.")).toBeInTheDocument()
    cleanup()

    start("/my-month", { ...mine(IN_FORCE), "GET /api/nurses/my-skills": { status: 500 } })
    expect(await screen.findByText("Your skills could not be read. Reload the page to try again.")).toBeInTheDocument()
  })

  it("shows a manager with a nurse profile their own skills too", async () => {
    start("/my-month", { ...mine(IN_FORCE), ...session({ ...NURSE, username: "mona", role: "unit_manager" }),
      "GET /api/nurses/my-skills": { body: [ICU_HELD] } })

    expect(await screen.findByRole("cell", { name: "ICU" })).toBeInTheDocument()
  })

  it("My leave's old address leads here, keeping its month", async () => {
    const { router } = start("/my-leave?month=2026-10", mine({ ...NO_HOURS_YET, month: "2026-10" }, { ...LEAVE, month: "2026-10" }))

    expect(await screen.findByRole("heading", { level: 1, name: "My month" })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe("/my-month")
    expect(router.state.location.search).toEqual({ month: "2026-10" })
  })

  it.each(["cno", "deputy_cno", "system_admin"])("is not for the %s", async (role) => {
    const { router } = start("/my-month", {
      ...session({ ...ME, role }), "GET /api/rosters/submitted": { body: [] }, "GET /api/units": { body: [] },
      "GET /api/ops/health": { body: HEALTH },
    })

    await waitFor(() => expect(router.state.location.pathname).not.toBe("/my-month"))
  })
})

describe("printing and downloading a roster version", () => {
  const UNIT = "/api/units/u1"
  const DAYS = [{ date: "2026-06-01", weekend: false, holiday: false }, { date: "2026-06-02", weekend: false, holiday: false }, { date: "2026-06-05", weekend: true, holiday: false }]
  const SHEET = {
    unit: "ER", month: "2026-06", version: 2, in_force: 2, made_at: "2026-06-03T08:30:00+03:00", days: DAYS,
    shifts: [{ code: "D", display_name: "Day", hours: 12 }, { code: "N", display_name: "Night", hours: 12 }],
    rows: [
      { hospital_id: "h1", full_name: "Amal Saleh", shifts: ["N", "", "D"], hours: 24, counts: [1, 1] },
      { hospital_id: "h123", full_name: "Hana Ali", shifts: ["", "D", ""], hours: 12, counts: [1, 0] },
    ],
    mine: null,
  }
  const asManager = (extra = {}) => ({ ...session(MANAGER), ...extra })
  const bodyRows = (table: HTMLElement) => within(table).getAllByRole("row").slice(1).map((row) =>
    [within(row).getByRole("rowheader").textContent, ...within(row).getAllByRole("cell").map((cell) => cell.textContent)])

  it("lays out a version for paper: a row a nurse, a column a date, the hours and the shift counts", async () => {
    const print = vi.fn()
    vi.stubGlobal("print", print)
    const { user, calls } = start("/roster/print?unit=u1&month=2026-06&version=2", asManager({ [`GET ${UNIT}/roster/sheet`]: { body: SHEET } }))

    expect(await screen.findByRole("heading", { name: "ER roster, June 2026" })).toBeInTheDocument()
    expect(screen.getByText(/Version 2, made 2026-06-03 08:30:00\. It is the roster in force\./)).toBeInTheDocument()
    const table = screen.getByRole("table", { name: "ER roster, June 2026, version 2" })
    expect(within(table).getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual(["Nurse", "1Mo", "2Tu", "5Fr", "Hours", "D", "N"])
    expect(bodyRows(table)).toEqual([["Amal Saleh", "N", "", "D", "24", "1", "1"], ["Hana Ali", "", "D", "", "12", "1", "0"]])
    expect(screen.getByText(/D: Day, 12 hours\. N: Night, 12 hours\./)).toBeInTheDocument()
    expect(screen.queryByText(/your leave/)).not.toBeInTheDocument()
    expect(screen.getByRole("link", { name: "Back to the roster" })).toHaveAttribute("href", "/roster?month=2026-06")
    expect(calls.map((call) => call.url)).toContain(`${UNIT}/roster/sheet?month=2026-06&version=2`)

    await user.click(screen.getByRole("button", { name: "Print" }))

    expect(print).toHaveBeenCalled()
  })

  it("prints a nurse on probation last, under its own heading with her preceptor's name", async () => {
    const rows = [...SHEET.rows, { hospital_id: "f1", full_name: "Faten Ali", shifts: ["", "D", ""], hours: 12, counts: [1, 0], on_probation: true, preceptor_name: "Amal Saleh" }]
    start("/roster/print?unit=u1&month=2026-06&version=2", asManager({ [`GET ${UNIT}/roster/sheet`]: { body: { ...SHEET, rows } } }))

    const table = await screen.findByRole("table", { name: "ER roster, June 2026, version 2" })
    const group = within(table).getByRole("rowgroup", { name: "On probation" })
    const [heading, faten] = within(group).getAllByRole("row")
    expect(heading).toHaveTextContent(/^On probation$/)
    expect([within(faten).getByRole("rowheader").getAttribute("aria-label"), ...within(faten).getAllByRole("cell").map((cell) => cell.textContent)])
      .toEqual(["Faten Ali, Preceptor: Amal Saleh", "", "D", "", "12", "1", "0"])
    expect(screen.getByText(/On probation: works beside their preceptor and is not counted toward any shift\./)).toBeInTheDocument()
  })

  it("prints no heading with nobody on probation", async () => {
    start("/roster/print?unit=u1&month=2026-06&version=2", asManager({ [`GET ${UNIT}/roster/sheet`]: { body: SHEET } }))

    const table = await screen.findByRole("table", { name: "ER roster, June 2026, version 2" })
    expect(within(table).getAllByRole("rowgroup")).toHaveLength(2)  // the head and the counted nurses
    expect(screen.queryByText(/On probation/)).not.toBeInTheDocument()
  })

  it("says when the version printed is not the roster in force, and leads an approver back to the unit's roster", async () => {
    start("/roster/print?unit=u1&month=2026-06&version=1", { ...session(ME_DEPUTY), [`GET ${UNIT}/roster/sheet`]: { body: { ...SHEET, version: 1 } } })

    expect(await screen.findByText("Version 2 has replaced it, so this is not the roster in force.")).toBeInTheDocument()
    expect(screen.getByRole("link", { name: "Back to the roster" })).toHaveAttribute("href", "/unit-roster?unit=u1&month=2026-06")
  })

  it("says so when the version cannot be read, or the address names none", async () => {
    start("/roster/print?unit=u1&month=2026-06&version=9", asManager({ [`GET ${UNIT}/roster/sheet`]: { status: 404, body: { detail: "This month's roster has no version 9." } } }))

    expect(await screen.findByRole("alert")).toHaveTextContent("This version could not be read.")
    cleanup()

    start("/roster/print?unit=u1", asManager())

    expect(await screen.findByRole("alert")).toHaveTextContent("This address names no roster version.")
  })

  describe("a nurse's own", () => {
    const MINE = { month: "2026-06", first_month: "2026-06", unit: "ER" }
    const OWN = { ...SHEET, mine: [{ date: "2026-06-01", kind: "leave" }, { date: "2026-06-02", kind: "shift" }, { date: "2026-06-05", kind: "off" }] }
    const reading = (answer: object) => nurseHome("u1", { "GET /api/rosters/mine/sheet": { body: answer } })

    it("prints their unit's roster in force with their own leave as leave and a colleague's empty day as a day off", async () => {
      start("/my-roster/print?month=2026-06", reading({ ...MINE, sheet: OWN }))

      const table = await screen.findByRole("table", { name: "ER roster, June 2026, version 2" })
      expect(bodyRows(table)).toEqual([["Amal Saleh", "N", "", "D", "24", "1", "1"], ["Hana Ali (you)", "L", "D", "", "12", "1", "0"]])
      expect(screen.getByText(/L: your leave\. My month counts your leave as worked in your hours; these hours count shifts only\./)).toBeInTheDocument()
      expect(screen.getByRole("link", { name: "Back to My roster" })).toHaveAttribute("href", "/my-roster?month=2026-06")
    })

    it("says when there is nothing approved to print", async () => {
      start("/my-roster/print?month=2026-06", reading({ ...MINE, sheet: null }))

      expect(await screen.findByText(/There is no approved roster of June 2026 to print\./)).toBeInTheDocument()
    })

    it("is reached from My roster", async () => {
      start("/my-roster?month=2026-06", nurseHome("u1", { "GET /api/rosters/mine": { body: { ...MINE, roster: {
        approved_at: "2026-05-16T12:00:00+03:00", version: 2, days: DAYS, shifts: [], rows: [], mine: null } } } }))

      expect(await screen.findByRole("link", { name: "Print this roster" })).toHaveAttribute("href", "/my-roster/print?month=2026-06")
    })
  })
})

describe("requests", () => {
  const MANAGER = { ...ME, username: "mona", full_name: "Mona Ali", role: "unit_manager" }
  const UNIT = { id: "u1", name: "ER", archived: false, manager: null, nurse_count: 4 }
  const DAY = { id: "s1", code: "D", display_name: "Day", hours: 12, start_time: "07:00", end_time: "19:00", colour: "#ffcc00", active: true, renames: [] }
  const NIGHT = { ...DAY, id: "s2", code: "N", display_name: "Night", start_time: "19:00", end_time: "07:00" }
  const made = (at = "2026-11-10T09:00:00+03:00", reason = "") => ({ step: "made", by: "Amal Saleh", at, reason, shift: null })
  const MY_OPEN = {
    month: "2026-12", window: { unit: "ER", opens_on: "2026-11-05", closes_on: "2026-11-20", state: "open", closed_early: false },
    request_cap: 3, counted: 1, refusal: null, shifts: [{ code: "D", display_name: "Day" }, { code: "N", display_name: "Night" }],
    first_day: "2026-12-01", last_day: "2026-12-31",
    requests: [
      { id: "r1", date: "2026-12-03", shift: "", state: "pending", promised: null, steps: [made("2026-11-10T09:00:00+03:00", "A family wedding.")] },
      { id: "r2", date: "2026-12-05", shift: "N", state: "rejected", promised: null, steps: [
        made(), { step: "rejected", by: "Mona Ali", at: "2026-11-11T09:00:00+03:00", reason: "Two others are off that day.", shift: null }] },
      { id: "r3", date: "2026-12-07", shift: "N", state: "accepted", promised: "D", steps: [
        made(), { step: "accepted", by: "Mona Ali", at: "2026-11-11T09:00:00+03:00", reason: "", shift: "N" },
        { step: "edited", by: "Mona Ali", at: "2026-11-12T09:00:00+03:00", reason: "Night is full.", shift: "D" }] },
    ],
  }
  const myMonth = (requests: Handler | Answer, extra = {}) => ({
    ...session(NURSE), "GET /api/rosters/mine/month": { body: { ...NO_HOURS_YET, month: "2026-12" } },
    "GET /api/nurses/my-skills": { body: [] }, "GET /api/nurses/my-leave": { body: { month: "2026-12", runs: [] } }, "GET /api/requests/mine": requests,
    "GET /api/preferences/mine": { body: { ...NO_PREFERENCE_WINDOW, month: "2026-12" } }, ...extra,
  })

  it("a nurse reads the window, her requests and every outcome with the manager's reason", async () => {
    start("/my-month?month=2026-12", myMonth({ body: MY_OPEN }))

    expect(await screen.findByText("Requests for December 2026 are open until the end of 20 Nov 2026. You have 1 of 3 pending or accepted.")).toBeInTheDocument()
    const rows = within(screen.getByRole("region", { name: "Your requests for December 2026" })).getAllByRole("row").slice(1)
    expect(rows.map((row) => within(row).getAllByRole("cell").slice(0, 3).map((cell) => cell.textContent))).toEqual([
      ["3 Dec 2026", "A day offA family wedding.", "Waiting for a decision"],
      ["5 Dec 2026", "Night (N)", "RejectedMona Ali: Two others are off that day."],
      ["7 Dec 2026", "Night (N)", "AcceptedNow promised: Day (D)Mona Ali: Night is full."],
    ])
    expect(screen.getAllByRole("button", { name: /^Withdraw/ }).map((button) => button.getAttribute("aria-label"))).toEqual([
      "Withdraw your request for 3 Dec 2026"])
  })

  it("a nurse reads a request the month's close ended, with no one's name on it", async () => {
    const expired = { id: "r4", date: "2026-12-09", shift: "", state: "expired", promised: null, steps: [
      made(), { step: "expired", by: null, at: "2027-01-01T00:30:00+03:00", reason: "The month closed before a decision.", shift: null }] }
    start("/my-month?month=2026-12", myMonth({ body: { ...MY_OPEN, requests: [expired] } }))

    const [row] = within(await screen.findByRole("region", { name: "Your requests for December 2026" })).getAllByRole("row").slice(1)
    expect(within(row).getAllByRole("cell").slice(0, 3).map((cell) => cell.textContent)).toEqual([
      "9 Dec 2026", "A day off", "ExpiredThe month closed before a decision."])
  })

  it("a nurse makes a request and is told it is made", async () => {
    let made_: unknown = null
    const { user } = start("/my-month?month=2026-12", myMonth(() => ({ body: MY_OPEN }), {
      "POST /api/requests/mine": ({ body }: { body: unknown }) => { made_ = body; return { status: 201, body: MY_OPEN.requests[0] } },
    }))

    await user.type(await screen.findByLabelText("Date"), "2026-12-09")
    await user.selectOptions(screen.getByLabelText("What you ask for"), "N")
    await user.type(screen.getByLabelText("Reason (optional)"), "Exam on the 10th.")
    await user.click(screen.getByRole("button", { name: "Make the request" }))

    const status = await screen.findByText("Your request for Night (N) on 9 Dec 2026 is made. Your manager decides it.")
    expect(made_).toEqual({ date: "2026-12-09", shift: "N", reason: "Exam on the 10th." })
    await waitFor(() => expect(status).toHaveFocus())
  })

  it("shows a refusal beside the date it is about", async () => {
    const { user } = start("/my-month?month=2026-12", myMonth({ body: MY_OPEN }, {
      "POST /api/requests/mine": { status: 422, body: { detail: [{ loc: ["body", "body", "date"], msg: "You are on leave on 9 December 2026, so there is nothing to request that day." }] } },
    }))

    await user.type(await screen.findByLabelText("Date"), "2026-12-09")
    await user.click(screen.getByRole("button", { name: "Make the request" }))

    expect(await screen.findByText("You are on leave on 9 December 2026, so there is nothing to request that day.")).toBeInTheDocument()
    await waitFor(() => expect(screen.getByLabelText("Date")).toHaveFocus())
  })

  it("a nurse withdraws a pending request", async () => {
    let withdrawn = false
    const { user } = start("/my-month?month=2026-12", myMonth(() => ({ body: withdrawn
      ? { ...MY_OPEN, counted: 0, requests: [{ ...MY_OPEN.requests[0], state: "withdrawn" }] } : MY_OPEN }), {
      "POST /api/requests/mine/r1/withdraw": () => { withdrawn = true; return { body: { ...MY_OPEN.requests[0], state: "withdrawn" } } },
    }))

    await user.click(await screen.findByRole("button", { name: "Withdraw your request for 3 Dec 2026" }))

    const status = await screen.findByText("Your request for a day off on 3 Dec 2026 is withdrawn.")
    await waitFor(() => expect(status).toHaveFocus())
    expect(await screen.findByText("Withdrawn")).toBeInTheDocument()
  })

  it("at the cap, or with the window closed, there is no form", async () => {
    start("/my-month?month=2026-12", myMonth({ body: { ...MY_OPEN, counted: 3, refusal: "You have 3 requests for December 2026, the most this unit allows. Withdraw a pending one to make another." } }))

    expect(await screen.findByText(/the most this unit allows/)).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Make the request" })).not.toBeInTheDocument()
    cleanup()

    start("/my-month?month=2026-12", myMonth({ body: { ...MY_OPEN, window: { ...MY_OPEN.window, state: "closed", closed_early: true } } }))

    expect(await screen.findByText("The request window for December 2026 has closed early. Your manager decides any request still waiting.")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Make the request" })).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /^Withdraw/ })).not.toBeInTheDocument()
  })

  it("a manager with a nurse profile reads My month without a requests card", async () => {
    const { calls } = start("/my-month?month=2026-12", {
      ...session(MANAGER), "GET /api/rosters/mine/month": { body: { ...NO_HOURS_YET, month: "2026-12" } },
      "GET /api/nurses/my-skills": { body: [] }, "GET /api/nurses/my-leave": { body: { month: "2026-12", runs: [] } },
    })

    expect(await screen.findByText("You have no leave recorded in December 2026.")).toBeInTheDocument()
    expect(screen.queryByRole("heading", { name: "Requests" })).not.toBeInTheDocument()
    expect(screen.queryByRole("heading", { name: "Preferences" })).not.toBeInTheDocument()
    expect(pageReads(calls)).not.toContain("/api/requests/mine?month=2026-12")
    expect(pageReads(calls)).not.toContain("/api/preferences/mine?month=2026-12")
  })

  const WINDOWS = {
    request_cap: 3, open_refusal: null, windows: [
      { id: "w1", month: "2026-12", opens_on: "2026-11-05", closes_on: "2026-11-20", state: "open", closed_early_by: null, pending: 1, requests: 2 },
    ],
  }
  const ROW = {
    id: "r1", hospital_id: "Amal", full_name: "Amal Saleh", date: "2026-12-03", shift: "N", state: "pending", promised: null,
    set_aside: null, accept_refusal: null, replaces_pin: "D", steps: [made("2026-11-10T09:00:00+03:00", "Exam on the 4th.")],
  }
  const REFUSED = { ...ROW, id: "r2", hospital_id: "Badr", full_name: "Badr Harbi", date: "2026-12-04", shift: "", replaces_pin: null,
    accept_refusal: "Badr Harbi is on leave on 4 December 2026, and leave wins over a pin.", steps: [made()] }
  const manager = (extra = {}) => ({
    ...session(MANAGER), "GET /api/units": { body: [UNIT] }, "GET /api/hospital/shifts": { body: [DAY, NIGHT] },
    "GET /api/units/u1/request-windows": { body: WINDOWS }, "GET /api/units/u1/request-windows/w1/requests": { body: [ROW, REFUSED] },
    ...extra,
  })

  it("the manager opens a window", async () => {
    let sent: unknown = null
    const { user } = start("/requests", manager({
      "GET /api/units/u1/request-windows": { body: { ...WINDOWS, windows: [] } },
      "POST /api/units/u1/request-windows": ({ body }: { body: unknown }) => { sent = body; return { status: 201, body: WINDOWS.windows[0] } },
    }))

    expect(await screen.findByText("No request window has been opened yet.")).toBeInTheDocument()
    expect(document.title).toBe("Requests · Nurse roster")
    fireEvent.change(screen.getByLabelText("Month"), { target: { value: "2026-12" } })
    await user.type(screen.getByLabelText("First day"), "2026-11-05")
    await user.type(screen.getByLabelText("Last day"), "2026-11-20")
    await user.click(screen.getByRole("button", { name: "Open the window" }))

    const status = await screen.findByText("Requests for December 2026 are open until the end of 20 Nov 2026. The unit's nurses of that month are told.")
    expect(sent).toEqual({ month: "2026-12", opens_on: "2026-11-05", closes_on: "2026-11-20" })
    await waitFor(() => expect(status).toHaveFocus())
  })

  it("the manager reads why a window cannot be opened", async () => {
    start("/requests", manager({ "GET /api/units/u1/request-windows": { body: { ...WINDOWS, open_refusal: "A unit has one request window at a time: close this one first." } } }))

    expect(await screen.findByText("A unit has one request window at a time: close this one first.")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Open the window" })).not.toBeInTheDocument()
  })

  it("the manager reads each request, and one that cannot be accepted says why", async () => {
    start("/requests", manager())

    const table = await screen.findByRole("region", { name: "Requests for December 2026, by date" })
    const rows = within(table).getAllByRole("row").slice(1)
    expect(within(rows[0]).getAllByRole("cell").slice(0, 4).map((cell) => cell.textContent)).toEqual([
      "Amal Saleh", "3 Dec 2026", "Night (N)Exam on the 4th.", "Waiting for a decision"])
    expect(within(rows[1]).getByText("Cannot be accepted: Badr Harbi is on leave on 4 December 2026, and leave wins over a pin.")).toBeInTheDocument()
    expect(within(rows[1]).queryByRole("button", { name: /^Accept/ })).not.toBeInTheDocument()
    expect(within(rows[1]).getByRole("button", { name: "Reject Badr Harbi's request for 4 Dec 2026" })).toBeInTheDocument()
  })

  it("the manager accepts a request, told first that it replaces their pin, and is told a version was made", async () => {
    let sent: unknown = null
    const { user } = start("/requests", manager({
      "POST /api/units/u1/requests/r1/accept": ({ body }: { body: unknown }) => {
        sent = body
        return { body: { request: { ...ROW, state: "accepted", promised: "N", replaces_pin: null }, version: 2 } }
      },
    }))

    await user.click(await screen.findByRole("button", { name: "Accept Amal Saleh's request for 3 Dec 2026" }))
    const dialog = await screen.findByRole("alertdialog")
    expect(within(dialog).getByText(/replaces your pin of Day \(D\) on that day/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole("button", { name: "Accept the request" }))

    const status = await screen.findByText("Accepted Amal Saleh's request for Night (N) on 3 Dec 2026. Amal Saleh is told. Version 2 of the roster is now in force, and the unit's nurses are told it changed, not why.")
    expect(sent).toEqual({ reason: "" })
    await waitFor(() => expect(status).toHaveFocus())
  })

  it("the manager rejects only with a reason", async () => {
    let sent: unknown = null
    const { user } = start("/requests", manager({
      "POST /api/units/u1/requests/r2/reject": ({ body }: { body: unknown }) => { sent = body; return { body: { request: { ...REFUSED, state: "rejected" }, version: null } } },
    }))

    await user.click(await screen.findByRole("button", { name: "Reject Badr Harbi's request for 4 Dec 2026" }))
    const dialog = await screen.findByRole("alertdialog")
    await user.click(within(dialog).getByRole("button", { name: "Reject the request" }))
    expect(await within(dialog).findByText("Say why: the nurse reads it with her request.")).toBeInTheDocument()
    expect(sent).toBeNull()

    await user.type(within(dialog).getByLabelText("Why"), "Two others are off that day.")
    await user.click(within(dialog).getByRole("button", { name: "Reject the request" }))

    expect(await screen.findByText("Rejected Badr Harbi's request for a day off on 4 Dec 2026. Badr Harbi is told.")).toBeInTheDocument()
    expect(sent).toEqual({ reason: "Two others are off that day." })
  })

  it("the manager changes what an accepted request promises, and a refusal stays in the dialog", async () => {
    const accepted = { ...ROW, state: "accepted", promised: "N", replaces_pin: null }
    const { user } = start("/requests", manager({
      "GET /api/units/u1/request-windows/w1/requests": { body: [accepted] },
      "POST /api/units/u1/requests/r1/edit": { status: 409, body: { detail: "The request already promises Night (N). Choose something else, or leave it as it is." } },
    }))

    await user.click(await screen.findByRole("button", { name: "Change Amal Saleh's request for 3 Dec 2026" }))
    const dialog = await screen.findByRole("alertdialog")
    expect(within(dialog).getByLabelText("It now promises")).toHaveValue("N")
    await user.type(within(dialog).getByLabelText("Why"), "Same.")
    await user.click(within(dialog).getByRole("button", { name: "Change what it promises" }))

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("The request already promises Night (N).")
  })

  it("the manager cancels an accepted request only with a reason, after being told its pin ends and no cell changes", async () => {
    const accepted = { ...ROW, state: "accepted", promised: "N", replaces_pin: null }
    let sent: unknown = null
    const { user } = start("/requests", manager({
      "GET /api/units/u1/request-windows/w1/requests": () => ({ body: [sent ? { ...accepted, state: "cancelled", promised: null } : accepted] }),
      "POST /api/units/u1/requests/r1/cancel": ({ body }: { body: unknown }) => {
        sent = body
        return { body: { request: { ...accepted, state: "cancelled", promised: null }, version: null } }
      },
    }))

    const opener = await screen.findByRole("button", { name: "Cancel Amal Saleh's request for 3 Dec 2026" })
    expect(screen.queryByRole("button", { name: /^Accept/ })).not.toBeInTheDocument()
    await user.click(opener)
    let dialog = await screen.findByRole("alertdialog")
    expect(within(dialog).getByRole("heading", { name: "Cancel Amal Saleh's request for Night (N) on 3 Dec 2026?" })).toBeInTheDocument()
    expect(within(dialog).getByText("Its pin ends and no cell changes: the next solve, or your own edit, may change it.")).toBeInTheDocument()

    await user.click(within(dialog).getByRole("button", { name: "Back" }))
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument())
    expect(sent).toBeNull()

    await user.click(screen.getByRole("button", { name: "Cancel Amal Saleh's request for 3 Dec 2026" }))
    dialog = await screen.findByRole("alertdialog")
    await user.click(within(dialog).getByRole("button", { name: "Cancel the request" }))
    expect(await within(dialog).findByText("Say why: the nurse reads it with her request.")).toBeInTheDocument()
    expect(sent).toBeNull()

    await user.type(within(dialog).getByLabelText("Why"), "The ward is short that night.")
    await user.click(within(dialog).getByRole("button", { name: "Cancel the request" }))

    const status = await screen.findByText("Cancelled Amal Saleh's request for Night (N) on 3 Dec 2026. Amal Saleh is told.")
    expect(sent).toEqual({ reason: "The ward is short that night." })
    expect(status).not.toHaveTextContent(/Version/)
    await waitFor(() => expect(status).toHaveFocus())
  })

  it("the manager closes a window early after being asked", async () => {
    let closed = false
    const { user } = start("/requests", manager({
      "GET /api/units/u1/request-windows": () => ({ body: closed ? { ...WINDOWS, windows: [{ ...WINDOWS.windows[0], state: "closed", closed_early_by: "Mona Ali" }] } : WINDOWS }),
      "POST /api/units/u1/request-windows/w1/close": () => { closed = true; return { body: { ...WINDOWS.windows[0], state: "closed" } } },
    }))

    await user.click(await screen.findByRole("button", { name: "Close early" }))
    const dialog = await screen.findByRole("alertdialog")
    expect(within(dialog).getByText(/The nurses told it opened are told it closed early/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole("button", { name: "Close the window" }))

    expect(await screen.findByText("The request window for December 2026 is closed.")).toBeInTheDocument()
    expect(await screen.findByText("Closed, early by Mona Ali")).toBeInTheDocument()
  })

  it("is not for other roles", async () => {
    const { router } = start("/requests", { ...nurseHome() })

    await waitFor(() => expect(router.state.location.pathname).toBe("/"))
  })

  it("a nurse's notification leads to My month and the manager's to the requests", async () => {
    const note = (id: string, page: string) => ({ id, event: "x", text: id, link: { page, month: "2026-12", unit_id: null }, created_at: "2026-11-10T09:00:00+03:00", read: true })
    start("/notifications", { ...session(NURSE), "GET /api/notifications": { body: { unread: 0, items: [note("a", "my_month"), note("b", "requests")] } } })

    expect(await screen.findByRole("link", { name: "Open My month" })).toHaveAttribute("href", "/my-month?month=2026-12")
    expect(screen.getByRole("link", { name: "Open the requests" })).toHaveAttribute("href", "/requests?month=2026-12")
  })

  it("the audit log names what was done to requests", async () => {
    const entry = (id: number, action: string) => ({ id, action, actor: "mona", target: "Amal", at: "2026-11-05T07:00:00+03:00", ip: null, before: null, after: null })
    start("/audit-log", admin({
      "GET /api/audit/entries": { body: { entries: [entry(1, "request.made"), entry(2, "request.accepted"), entry(3, "request_window.opened")], next_before: null } },
      "GET /api/audit/filter-options": { body: { users: [], actions: [] } },
    }))

    expect(await screen.findByText("Made a request")).toBeInTheDocument()
    expect(screen.getByText("Accepted a nurse's request")).toBeInTheDocument()
    expect(screen.getByText("Opened a request window")).toBeInTheDocument()
  })
})

describe("preferences", () => {
  const MANAGER = { ...ME, username: "mona", full_name: "Mona Ali", role: "unit_manager" }
  const UNIT = { id: "u1", name: "ER", archived: false, manager: null, nurse_count: 4 }
  const WINDOW = { unit: "ER", opens_on: "2026-11-05", closes_on: "2026-11-20", state: "open", closed_early: false }
  const MY_OPEN = {
    month: "2026-12", window: WINDOW, preference_cap: 10, request_cap: 3, counted: 2, refusal: null,
    shifts: [{ code: "D", display_name: "Day" }, { code: "N", display_name: "Night" }], first_day: "2026-12-01", last_day: "2026-12-31",
    preferences: [
      { id: "p1", date: "2026-12-03", kind: "day_off", state: "made", outcome: null },
      { id: "p2", date: "2026-12-05", kind: "working_day", state: "made", outcome: null },
      { id: "p3", date: "2026-12-07", kind: "day_off", state: "withdrawn", outcome: null },
      { id: "p4", date: "2026-12-09", kind: "day_off", state: "asked_instead", outcome: null },
    ],
    kept: null, keepable: null, doubled_by: null,
  }
  const REQUESTS = {
    month: "2026-12", window: WINDOW, request_cap: 3, counted: 0, refusal: null, shifts: MY_OPEN.shifts,
    first_day: "2026-12-01", last_day: "2026-12-31", requests: [],
  }
  const myMonth = (preferences: Handler | Answer, extra = {}) => ({
    ...session(NURSE), "GET /api/rosters/mine/month": { body: { ...NO_HOURS_YET, month: "2026-12" } },
    "GET /api/nurses/my-skills": { body: [] }, "GET /api/nurses/my-leave": { body: { month: "2026-12", runs: [] } }, "GET /api/requests/mine": { body: REQUESTS },
    "GET /api/preferences/mine": preferences, ...extra,
  })
  const card = () => within(screen.getByRole("heading", { name: "Preferences" }).closest("[data-slot=card]") as HTMLElement)

  it("a nurse reads the window, the count against the cap and each preference with its state", async () => {
    start("/my-month?month=2026-12", myMonth({ body: MY_OPEN }))

    expect(await screen.findByText("Preferences for December 2026 are open until the end of 20 Nov 2026. You have 2 of 10 preferences.")).toBeInTheDocument()
    const rows = within(card().getByRole("region", { name: "Your preferences for December 2026" })).getAllByRole("row").slice(1)
    expect(rows.map((row) => within(row).getAllByRole("cell").slice(0, 3).map((cell) => cell.textContent))).toEqual([
      ["3 Dec 2026", "A day off", "Made"],
      ["5 Dec 2026", "A working day", "Made"],
      ["7 Dec 2026", "A day off", "Withdrawn"],
      ["9 Dec 2026", "A day off", "Asked for as a request"],
    ])
    expect(card().getAllByRole("button", { name: /preference for/ }).map((button) => button.getAttribute("aria-label"))).toEqual([
      "Withdraw your preference for 3 Dec 2026", "Ask for your preference for 3 Dec 2026 as a request",
      "Withdraw your preference for 5 Dec 2026", "Ask for your preference for 5 Dec 2026 as a request",
    ])
    expect(card().queryByLabelText(/Reason/)).not.toBeInTheDocument()
  })

  it("a nurse makes a working-day preference, with no reason, and is told it is made", async () => {
    let sent: unknown = null
    const { user } = start("/my-month?month=2026-12", myMonth(() => ({ body: MY_OPEN }), {
      "POST /api/preferences/mine": ({ body }: { body: unknown }) => { sent = body; return { status: 201, body: { id: "p5", date: "2026-12-11", kind: "working_day", state: "made" } } },
    }))

    await screen.findByText(/You have 2 of 10 preferences/)
    await user.type(card().getByLabelText("Day"), "2026-12-11")
    await user.click(card().getByRole("radio", { name: "A working day" }))
    await user.click(card().getByRole("button", { name: "Make the preference" }))

    const status = await screen.findByText("Your preference for a working day on 11 Dec 2026 is made. The solver keeps it when it can.")
    expect(sent).toEqual({ date: "2026-12-11", kind: "working_day" })
    await waitFor(() => expect(status).toHaveFocus())
  })

  it("shows a refusal beside the date it is about", async () => {
    const { user } = start("/my-month?month=2026-12", myMonth({ body: MY_OPEN }, {
      "POST /api/preferences/mine": { status: 422, body: { detail: [{ loc: ["body", "body", "date"], msg: "You have a request on 11 December 2026, and a date holds one wish. Withdraw the request to make a preference instead." }] } },
    }))

    await screen.findByText(/You have 2 of 10 preferences/)
    await user.type(card().getByLabelText("Day"), "2026-12-11")
    await user.click(card().getByRole("button", { name: "Make the preference" }))

    expect(await card().findByText(/and a date holds one wish/)).toBeInTheDocument()
    await waitFor(() => expect(card().getByLabelText("Day")).toHaveFocus())
  })

  it("a nurse withdraws one after being asked", async () => {
    let withdrawn = false
    const { user } = start("/my-month?month=2026-12", myMonth(() => ({ body: withdrawn
      ? { ...MY_OPEN, counted: 1, preferences: [{ ...MY_OPEN.preferences[0], state: "withdrawn" }, ...MY_OPEN.preferences.slice(1)] } : MY_OPEN }), {
      "POST /api/preferences/mine/p1/withdraw": () => { withdrawn = true; return { body: { ...MY_OPEN.preferences[0], state: "withdrawn" } } },
    }))

    await user.click(await screen.findByRole("button", { name: "Withdraw your preference for 3 Dec 2026" }))
    const dialog = await screen.findByRole("alertdialog", { name: "Withdraw your preference for 3 Dec 2026?" })
    await user.click(within(dialog).getByRole("button", { name: "Withdraw it" }))

    const status = await screen.findByText("Your preference for a day off on 3 Dec 2026 is withdrawn.")
    await waitFor(() => expect(status).toHaveFocus())
    expect(await screen.findByText("You have 1 of 10 preferences.", { exact: false })).toBeInTheDocument()
  })

  it("a nurse asks for a day off as a request after a question naming the request cap, and both cards are read again", async () => {
    let asked: unknown = null
    const { user, calls } = start("/my-month?month=2026-12", myMonth(() => ({ body: asked
      ? { ...MY_OPEN, counted: 1, preferences: [{ ...MY_OPEN.preferences[0], state: "asked_instead" }, ...MY_OPEN.preferences.slice(1)] } : MY_OPEN }), {
      "POST /api/preferences/mine/p1/ask-as-request": ({ body }: { body: unknown }) => {
        asked = body
        return { status: 201, body: { preference: { ...MY_OPEN.preferences[0], state: "asked_instead" },
          request: { id: "r1", date: "2026-12-03", shift: "", state: "pending", promised: null, steps: [] } } }
      },
    }))

    await user.click(await screen.findByRole("button", { name: "Ask for your preference for 3 Dec 2026 as a request" }))
    const dialog = await screen.findByRole("alertdialog", { name: "Ask for 3 Dec 2026 as a request?" })
    expect(dialog).toHaveTextContent("It counts against your unit's 3 requests a month")
    expect(within(dialog).queryByLabelText("Shift")).not.toBeInTheDocument()
    const reads = calls.filter((call) => call.url.startsWith("/api/requests/mine")).length
    await user.click(within(dialog).getByRole("button", { name: "Ask for it" }))

    const status = await screen.findByText("Your preference for 3 Dec 2026 is now a request for a day off. Your manager decides it.")
    expect(asked).toEqual({ shift: null })
    await waitFor(() => expect(status).toHaveFocus())
    await waitFor(() => expect(calls.filter((call) => call.url.startsWith("/api/requests/mine")).length).toBeGreaterThan(reads))
  })

  it("a working day is asked for as the shift she chooses, and a refusal stays in the question", async () => {
    let asked: unknown = null
    const { user } = start("/my-month?month=2026-12", myMonth({ body: MY_OPEN }, {
      "POST /api/preferences/mine/p2/ask-as-request": ({ body }: { body: unknown }) => {
        asked = body
        return { status: 409, body: { detail: "You have 3 requests for December 2026, the most this unit allows. Withdraw a pending one to make another." } }
      },
    }))

    await user.click(await screen.findByRole("button", { name: "Ask for your preference for 5 Dec 2026 as a request" }))
    const dialog = await screen.findByRole("alertdialog", { name: "Ask for 5 Dec 2026 as a request?" })
    await user.selectOptions(within(dialog).getByLabelText("Shift"), "N")
    await user.click(within(dialog).getByRole("button", { name: "Ask for it" }))

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("You have 3 requests for December 2026, the most this unit allows.")
    expect(asked).toEqual({ shift: "N" })
  })

  it("at the cap, or with the window closed, there is no form, and closed there is nothing to do", async () => {
    start("/my-month?month=2026-12", myMonth({ body: { ...MY_OPEN, counted: 10, refusal: "You have 10 preferences for December 2026, the most this unit allows. Withdraw one to make another." } }))

    expect(await screen.findByText(/You have 10 preferences for December 2026, the most/)).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Make the preference" })).not.toBeInTheDocument()
    cleanup()

    start("/my-month?month=2026-12", myMonth({ body: { ...MY_OPEN, window: { ...WINDOW, state: "closed", closed_early: false } } }))

    expect(await screen.findByText("The request window for December 2026 has closed, so your preferences are fixed. The solver keeps them when it can.")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Make the preference" })).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /preference for/ })).not.toBeInTheDocument()
  })

  const APPROVED = {
    ...MY_OPEN, window: { ...WINDOW, state: "closed" }, kept: 2, keepable: 3,
    preferences: [
      { id: "p1", date: "2026-12-03", kind: "day_off", state: "made", outcome: "kept" },
      { id: "p2", date: "2026-12-05", kind: "working_day", state: "made", outcome: "not_kept" },
      { id: "p5", date: "2026-12-06", kind: "working_day", state: "made", outcome: "kept" },
      { id: "p3", date: "2026-12-07", kind: "day_off", state: "withdrawn", outcome: null },
      { id: "p6", date: "2026-12-08", kind: "day_off", state: "made", outcome: "on_leave" },
      { id: "p7", date: "2026-12-09", kind: "working_day", state: "made", outcome: "barred" },
    ],
  }

  it("once the roster is approved the card says how many were kept, and marks each Kept, Not kept, On leave or Skill expired", async () => {
    start("/my-month?month=2026-12", myMonth({ body: APPROVED }))
    await screen.findByRole("heading", { name: "Preferences" })

    expect(await card().findByText("2 of 3 kept")).toBeInTheDocument()
    const rows = within(card().getByRole("region", { name: "Your preferences for December 2026" })).getAllByRole("row")
    expect(within(rows[0]).getAllByRole("columnheader").map((header) => header.textContent)).toEqual(["Date", "You prefer", "State", "Outcome"])
    expect(rows.slice(1).map((row) => within(row).getAllByRole("cell").map((cell) => cell.textContent))).toEqual([
      ["3 Dec 2026", "A day off", "Made", "Kept"],
      ["5 Dec 2026", "A working day", "Made", "Not kept"],
      ["6 Dec 2026", "A working day", "Made", "Kept"],
      ["7 Dec 2026", "A day off", "Withdrawn", ""],
      ["8 Dec 2026", "A day off", "Made", "On leave"],
      ["9 Dec 2026", "A working day", "Made", "Skill expired"],
    ])
    expect(card().queryByText("No roster yet")).not.toBeInTheDocument()
    expect(card().queryByText(/count double/)).not.toBeInTheDocument()
  })

  it("before the roster is approved the card says there is none yet, and marks nothing", async () => {
    start("/my-month?month=2026-12", myMonth({ body: MY_OPEN }))
    await screen.findByRole("heading", { name: "Preferences" })

    expect(await card().findByText("No roster yet")).toBeInTheDocument()
    expect(card().queryByRole("columnheader", { name: "Outcome" })).not.toBeInTheDocument()
    expect(card().queryByText(/ kept$/)).not.toBeInTheDocument()
  })

  it("tells her when her preferences count double this month, and which month caused it", async () => {
    start("/my-month?month=2026-12", myMonth({ body: { ...MY_OPEN, doubled_by: "2026-11" } }))
    await screen.findByRole("heading", { name: "Preferences" })

    expect(await card().findByText("One of your preferences was not kept in November, so this month yours count double.")).toBeInTheDocument()
  })

  it("says it of a month with no preferences of hers, and nothing of kept or no roster", async () => {
    start("/my-month?month=2026-12", myMonth({ body: { ...MY_OPEN, preferences: [], counted: 0, doubled_by: "2026-11" } }))
    await screen.findByRole("heading", { name: "Preferences" })

    expect(await card().findByText("One of your preferences was not kept in November, so this month yours count double.")).toBeInTheDocument()
    expect(card().queryByText("No roster yet")).not.toBeInTheDocument()
  })

  const WINDOWS = {
    request_cap: 3, preference_cap: 10, open_refusal: null, windows: [
      { id: "w1", month: "2026-12", opens_on: "2026-11-05", closes_on: "2026-11-20", state: "open", closed_early_by: null, pending: 0, requests: 0, preferences: 2 },
    ],
  }
  const manager = (extra = {}) => ({
    ...session(MANAGER), "GET /api/units": { body: [UNIT] }, "GET /api/hospital/shifts": { body: [] },
    "GET /api/units/u1/request-windows": { body: WINDOWS }, "GET /api/units/u1/request-windows/w1/requests": { body: [] },
    "GET /api/units/u1/request-windows/w1/preferences": { body: [
      { id: "p1", hospital_id: "Amal", full_name: "Amal Saleh", date: "2026-12-03", kind: "day_off", state: "made" },
      { id: "p2", hospital_id: "Badr", full_name: "Badr Harbi", date: "2026-12-04", kind: "working_day", state: "withdrawn" },
    ] },
    ...extra,
  })

  it("the manager reads each window's preferences beneath its requests, and decides nothing on them", async () => {
    start("/requests", manager())

    const region = await screen.findByRole("region", { name: "Preferences for December 2026, by date" })
    expect(within(region).getAllByRole("row").slice(1).map((row) => within(row).getAllByRole("cell").map((cell) => cell.textContent))).toEqual([
      ["Amal Saleh", "3 Dec 2026", "A day off", "Made"],
      ["Badr Harbi", "4 Dec 2026", "A working day", "Withdrawn"],
    ])
    expect(within(region).queryByRole("button")).not.toBeInTheDocument()
    expect(screen.getByText(/up to 3 requests pending or accepted and 10 preferences a month/)).toBeInTheDocument()
    const windows = within(screen.getByRole("region", { name: "Request windows, newest first" }))
    expect(windows.getAllByRole("columnheader").map((header) => header.textContent)).toContain("Preferences")
    expect(windows.getAllByRole("row")[1]).toHaveTextContent(/Open0\s*2/)
  })

  it("the manager reads that no preference was made", async () => {
    start("/requests", manager({ "GET /api/units/u1/request-windows/w1/preferences": { body: [] } }))

    expect(await screen.findByText("No preferences were made in this window.")).toBeInTheDocument()
  })
})

describe("swaps", () => {
  const MANAGER = { ...ME, username: "mona", full_name: "Mona Ali", role: "unit_manager" }
  const UNIT = { id: "u1", name: "ER", archived: false, manager: null, nurse_count: 4 }
  const DAY = { id: "s1", code: "D", display_name: "Day", hours: 12, start_time: "07:00", end_time: "19:00", colour: "#ffcc00", active: true, renames: [] }
  const NIGHT = { ...DAY, id: "s2", code: "N", display_name: "Night", start_time: "19:00", end_time: "07:00" }
  const DAYS = ["2026-12-09", "2026-12-10"].map((date) => ({ date, weekend: false, holiday: false }))
  const COMPOSER = {
    month: "2026-12", unit: "ER", refusal: null, days: DAYS, shifts: [{ code: "D", display_name: "Day" }, { code: "N", display_name: "Night" }],
    nurses: [
      { hospital_id: "h123", full_name: "Hana Ali", me: true, shifts: ["D", ""] },
      { hospital_id: "h2", full_name: "Emad Nasser", me: false, shifts: ["", "D"] },
    ],
  }
  const CELLS = [
    { hospital_id: "h123", full_name: "Hana Ali", date: "2026-12-09", before: "D", after: "" },
    { hospital_id: "h2", full_name: "Emad Nasser", date: "2026-12-09", before: "", after: "D" },
  ]
  const SWAP = {
    id: "w1", month: "2026-12", unit: "ER", state: "pending", proposed_at: "2026-11-06T09:00:00+03:00", proposer: "Hana Ali",
    proposed_by_me: true, note: "My daughter's school play.", cells: CELLS,
    colleagues: [{ hospital_id: "h2", full_name: "Emad Nasser", answer: "pending" }], my_answer: null, ended_by: null, ended_at: null, reason: "",
  }
  const THEIRS = { ...SWAP, id: "w2", proposer: "Emad Nasser", proposed_by_me: false, my_answer: "pending", note: "",
    colleagues: [{ hospital_id: "h123", full_name: "Hana Ali", answer: "pending" }] }
  const nurse = (swaps: Handler | Answer, extra = {}) => ({
    ...session(NURSE), "GET /api/swaps/compose": { body: COMPOSER }, "GET /api/swaps/mine": swaps,
    "GET /api/hospital/shifts": { body: [DAY, NIGHT] }, ...extra,
  })

  it("a push tapped while the app is open brings this window to Swaps through the app's own router", async () => {
    // The service worker focuses the window and posts it the page (public/sw.js); sw.test.ts covers that side.
    const worker = Object.assign(new EventTarget(), { getRegistration: async () => undefined })
    Object.defineProperty(navigator, "serviceWorker", { value: worker, configurable: true })
    try {
      start("/notifications", nurse({ body: [THEIRS] }, { "GET /api/notifications": { body: { unread: 0, items: [] } } }))
      expect(await screen.findByRole("heading", { name: "Notifications", level: 1 })).toBeInTheDocument()

      worker.dispatchEvent(new MessageEvent("message", { data: { type: "something else", path: "/swaps" } }))
      worker.dispatchEvent(new MessageEvent("message", { data: { type: "open-page", path: "//elsewhere.example/swaps" } }))
      worker.dispatchEvent(new MessageEvent("message", { data: { type: "open-page", path: "/swaps" } }))

      expect(await screen.findByRole("heading", { name: "Swaps", level: 1 })).toBeInTheDocument()
    } finally {
      delete (navigator as unknown as Record<string, unknown>).serviceWorker
    }
  })

  it("a nurse proposes a swap of her cell and a colleague's, and is told who is asked", async () => {
    let sent: unknown = null
    const { user } = start("/swaps?month=2026-12", nurse({ body: [] }, {
      "POST /api/swaps/mine": ({ body }: { body: unknown }) => { sent = body; return { status: 201, body: SWAP } },
    }))

    expect(await screen.findByText("In the roster in force: Day (D).")).toBeInTheDocument()
    expect(document.title).toBe("Swaps · Nurse roster")
    await user.selectOptions(screen.getByLabelText("Date"), "2026-12-10")
    await user.click(screen.getByRole("button", { name: "Add a cell" }))
    expect(screen.getAllByLabelText("Nurse")[1]).toHaveValue("h2")
    expect(screen.getAllByLabelText("Date")[1]).toHaveValue("2026-12-10")
    await user.selectOptions(screen.getAllByLabelText("Date")[0], "2026-12-09")
    await user.selectOptions(screen.getAllByLabelText("Date")[1], "2026-12-09")
    await user.selectOptions(screen.getAllByLabelText("Becomes")[1], "D")
    await user.type(screen.getByLabelText("Note (optional)"), "My daughter's school play.")
    await user.click(screen.getByRole("button", { name: "Propose the swap" }))

    const status = await screen.findByText("Your swap is proposed. Emad Nasser is asked to accept it, then your manager decides.")
    expect(sent).toEqual({ month: "2026-12", note: "My daughter's school play.", cells: [
      { hospital_id: "h123", date: "2026-12-09", shift: null }, { hospital_id: "h2", date: "2026-12-09", shift: "D" }] })
    await waitFor(() => expect(status).toHaveFocus())
  })

  it("shows a refusal beside the cell it is about, and one about the whole swap as an alert", async () => {
    const { user } = start("/swaps?month=2026-12", nurse({ body: [] }, {
      "POST /api/swaps/mine": { status: 422, body: { detail: [
        { loc: ["body", "body", "cells", 1], msg: "This swap cannot work for Emad Nasser on 9 December 2026." }] } },
    }))

    await user.click(await screen.findByRole("button", { name: "Add a cell" }))
    await user.click(screen.getByRole("button", { name: "Propose the swap" }))

    const said = await screen.findByText("This swap cannot work for Emad Nasser on 9 December 2026.")
    const second = screen.getAllByLabelText("Nurse")[1]
    expect(second).toHaveAttribute("aria-invalid", "true")
    expect(second.getAttribute("aria-describedby")).toContain(said.closest("[id]")?.id)
    await waitFor(() => expect(screen.getAllByLabelText("Nurse")[1]).toHaveFocus())
    cleanup()

    const again = start("/swaps?month=2026-12", nurse({ body: [] }, {
      "POST /api/swaps/mine": { status: 422, body: { detail: [{ loc: ["body", "body", "cells"], msg: "A swap changes at least one of your own cells. Add one of yours." }] } },
    }))
    await again.user.click(await screen.findByRole("button", { name: "Propose the swap" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("A swap changes at least one of your own cells. Add one of yours.")
  })

  it("says why no swap can be proposed this month", async () => {
    start("/swaps?month=2026-12", nurse({ body: [] }, {
      "GET /api/swaps/compose": { body: { ...COMPOSER, refusal: "The ER roster for December 2026 is not approved yet, so there is nothing to swap.", days: [], nurses: [], shifts: [] } },
    }))

    expect(await screen.findByText("The ER roster for December 2026 is not approved yet, so there is nothing to swap.")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Propose the swap" })).not.toBeInTheDocument()
    expect(screen.getByText("You have proposed no swap, and nobody has proposed one with you.")).toBeInTheDocument()
  })

  it("tells a nurse on probation why she proposes no swap", async () => {
    const said = "You are on probation in December 2026, and a nurse on probation takes no part in a swap: you work beside your preceptor, and the roster is planned around that."
    start("/swaps?month=2026-12", nurse({ body: [] }, { "GET /api/swaps/compose": { body: { ...COMPOSER, refusal: said } } }))

    expect(await screen.findByText(said)).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Propose the swap" })).not.toBeInTheDocument()
  })

  it("a nurse reads her swaps, with each cell, each answer and the note", async () => {
    start("/swaps?month=2026-12", nurse({ body: [SWAP, THEIRS] }))

    expect(await screen.findByRole("heading", { name: "Your swap on 9 Dec 2026: Waiting for its colleagues" })).toBeInTheDocument()
    const table = screen.getByRole("region", { name: "Your swap on 9 Dec 2026, its cells" })
    expect(within(table).getAllByRole("row").slice(1).map((row) => within(row).getAllByRole("cell").map((cell) => cell.textContent))).toEqual([
      ["Hana Ali", "9 Dec 2026", "Day (D)", "Off"], ["Emad Nasser", "9 Dec 2026", "Off", "Day (D)"]])
    expect(screen.getByText("Emad Nasser: not answered yet.")).toBeInTheDocument()
    expect(screen.getByText("Hana Ali: My daughter's school play.")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Withdraw your swap on 9 Dec 2026" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Accept Emad Nasser's swap on 9 Dec 2026" })).toBeInTheDocument()
  })

  it("a colleague accepts and is told what comes next", async () => {
    let accepted = false
    const { user } = start("/swaps", nurse(() => ({ body: [accepted ? { ...THEIRS, state: "awaiting_manager", my_answer: "accepted" } : THEIRS] }), {
      "POST /api/swaps/mine/w2/accept": () => { accepted = true; return { body: { ...THEIRS, state: "awaiting_manager", my_answer: "accepted" } } },
    }))

    await user.click(await screen.findByRole("button", { name: "Accept Emad Nasser's swap on 9 Dec 2026" }))

    const status = await screen.findByText("You accepted Emad Nasser's swap. Everyone in it has now accepted, so it waits for the manager.")
    await waitFor(() => expect(status).toHaveFocus())
    expect(await screen.findByRole("heading", { name: "Emad Nasser's swap on 9 Dec 2026: Waiting for the manager" })).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /^Accept/ })).not.toBeInTheDocument()
  })

  it("a colleague rejects only after a question, and the proposer withdraws the same way", async () => {
    const posted: string[] = []
    const { user } = start("/swaps", nurse(() => ({ body: [SWAP, THEIRS] }), {
      "POST /api/swaps/mine/w2/reject": () => { posted.push("reject"); return { body: { ...THEIRS, state: "rejected" } } },
      "POST /api/swaps/mine/w1/withdraw": () => { posted.push("withdraw"); return { body: { ...SWAP, state: "withdrawn" } } },
    }))

    await user.click(await screen.findByRole("button", { name: "Reject Emad Nasser's swap on 9 Dec 2026" }))
    const dialog = await screen.findByRole("alertdialog", { name: "Reject Emad Nasser's swap on 9 Dec 2026?" })
    await user.click(within(dialog).getByRole("button", { name: "Reject the swap" }))
    expect(await screen.findByText("You rejected Emad Nasser's swap. Everyone in it is told.")).toBeInTheDocument()

    await user.click(screen.getByRole("button", { name: "Withdraw your swap on 9 Dec 2026" }))
    await user.click(within(await screen.findByRole("alertdialog", { name: "Withdraw your swap on 9 Dec 2026?" })).getByRole("button", { name: "Withdraw the swap" }))
    expect(await screen.findByText("You withdrew your swap. Everyone in it is told.")).toBeInTheDocument()
    expect(posted).toEqual(["reject", "withdraw"])
  })

  const UNIT_ROW = { ...SWAP, state: "awaiting_manager", colleagues: [{ hospital_id: "h2", full_name: "Emad Nasser", answer: "accepted" }],
    expired_why: null, version: null }
  const DECISION = {
    swap: UNIT_ROW, cells: [{ ...CELLS[0], now: "D", replaces_pin: "D" }, { ...CELLS[1], now: "", replaces_pin: null }],
    check: { reason: null, ok: true, violations: [], cost: 130 }, cost_before: 116, cost_after: 130, delta: 14, problems: [], approve_refusal: null,
  }
  const manager = (extra = {}) => ({
    ...session(MANAGER), "GET /api/units": { body: [UNIT] }, "GET /api/hospital/shifts": { body: [DAY, NIGHT] },
    "GET /api/units/u1/swaps": { body: { month: "2026-12", swaps: [UNIT_ROW] } }, "GET /api/units/u1/swaps/w1": { body: DECISION }, ...extra,
  })

  it("the manager sees before and after, the check and the cost change, and that approving replaces a pin", async () => {
    start("/unit-swaps?month=2026-12", manager())

    expect(await screen.findByRole("heading", { name: "Hana Ali's swap: Waiting for the manager" })).toBeInTheDocument()
    const table = screen.getByRole("region", { name: "Hana Ali's swap, before and after" })
    expect(within(table).getAllByRole("row").slice(1).map((row) => within(row).getAllByRole("cell").map((cell) => cell.textContent))).toEqual([
      ["Hana Ali", "9 Dec 2026", "Day (D)", "Off", "Day (D)", "Day (D), which approving replaces"],
      ["Emad Nasser", "9 Dec 2026", "Off", "Day (D)", "Off", ""]])
    expect(screen.getByText("With its cells the roster breaks no hard rule. It costs 130, against 116 for the roster in force (+14).")).toBeInTheDocument()
    expect(screen.getByText("Emad Nasser: accepted.")).toBeInTheDocument()
  })

  it("the manager approves and is told the version it made", async () => {
    let sent: unknown = null
    const { user } = start("/unit-swaps?month=2026-12", manager({
      "POST /api/units/u1/swaps/w1/approve": ({ body }: { body: unknown }) => {
        sent = body
        return { body: { ...DECISION, swap: { ...UNIT_ROW, state: "approved", version: 3 } } }
      },
      "GET /api/units/u1/roster": { body: {} }, "GET /api/units/u1/roster/versions": { body: [] },
    }))

    await user.click(await screen.findByRole("button", { name: "Approve" }))
    const dialog = await screen.findByRole("alertdialog", { name: "Approve Hana Ali's swap on 9 Dec 2026?" })
    await user.type(within(dialog).getByLabelText("Why (optional)"), "Fine by me.")
    await user.click(within(dialog).getByRole("button", { name: "Approve the swap" }))

    const status = await screen.findByText(/^You approved Hana Ali's swap on 9 Dec 2026\. Version 3 of the roster is now in force/)
    expect(sent).toEqual({ reason: "Fine by me." })
    await waitFor(() => expect(status).toHaveFocus())
  })

  it("the manager rejects only with a reason, and one that cannot be approved says why", async () => {
    const { user } = start("/unit-swaps?month=2026-12", manager({
      "GET /api/units/u1/swaps/w1": { body: { ...DECISION, problems: ["Emad Nasser is on leave on 9 December 2026, and leave wins over a pin."],
        approve_refusal: "Emad Nasser is on leave on 9 December 2026, and leave wins over a pin." } },
    }))

    expect(await screen.findByText("Emad Nasser is on leave on 9 December 2026, and leave wins over a pin.")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument()
    await user.click(screen.getByRole("button", { name: "Reject" }))
    const dialog = await screen.findByRole("alertdialog")
    await user.click(within(dialog).getByRole("button", { name: "Reject the swap" }))

    expect(await within(dialog).findByText("Say why: everyone in the swap reads it.")).toBeInTheDocument()
    await waitFor(() => expect(within(dialog).getByLabelText("Why")).toHaveFocus())
  })

  it("an ended swap says who did not answer, and offers no decision and no pin to replace", async () => {
    const ended = { ...UNIT_ROW, state: "expired", expired_why: "cell_changed", colleagues: [{ hospital_id: "h2", full_name: "Emad Nasser", answer: "pending" }] }
    start("/unit-swaps?month=2026-12&swap=w1", manager({
      "GET /api/units/u1/swaps": { body: { month: "2026-12", swaps: [ended] } },
      "GET /api/units/u1/swaps/w1": { body: { ...DECISION, swap: ended, check: null, approve_refusal: "This swap is expired, so it cannot be decided." } },
    }))

    expect(await screen.findByText("Emad Nasser: did not answer.")).toBeInTheDocument()
    expect(screen.getByText(/It expired because a new version changed one of its cells\./)).toBeInTheDocument()
    expect(screen.queryByRole("columnheader", { name: "Your pin" })).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument()
  })

  it("says when a month has no swap, and leads to the next", async () => {
    start("/unit-swaps", manager({ "GET /api/units/u1/swaps": { body: { month: "2026-11", swaps: [] } } }))

    expect(await screen.findByText("No swap has been proposed for November 2026.")).toBeInTheDocument()
    expect(screen.getByRole("link", { name: "Next month" })).toHaveAttribute("href", "/unit-swaps?month=2026-12")
  })

  it.each([["/swaps", MANAGER], ["/unit-swaps", NURSE]])("%s is not for the other role", async (path, who) => {
    const { router } = start(path, { ...session(who), "GET /api/units": { body: [UNIT] }, "GET /api/rosters/mine": { body: NO_ROSTER_YET },
      "GET /api/nurses/my-unit": { body: { id: "u1", name: "ER" } }, "GET /api/rosters/submitted": { body: [] } })

    await waitFor(() => expect(router.state.location.pathname).not.toBe(path))
  })

  it("a swap's notification leads a nurse to Swaps and the manager to the unit's swaps", async () => {
    const note = (page: string) => ({ id: page, event: "swap_step", text: "A swap moved.", link: { page, month: "2026-12", unit_id: null },
      created_at: "2026-11-06T09:00:00+03:00", read: false })
    start("/notifications", { ...session(NURSE), "GET /api/notifications": { body: { unread: 1, items: [note("swaps")] } } })

    expect(await screen.findByRole("link", { name: "Open Swaps" })).toHaveAttribute("href", "/swaps?month=2026-12")
    cleanup()

    start("/notifications", { ...session(MANAGER), "GET /api/notifications": { body: { unread: 1, items: [note("unit_swaps")] } } })

    expect(await screen.findByRole("link", { name: "Open the swaps" })).toHaveAttribute("href", "/unit-swaps?month=2026-12")
  })
})

describe("covers", () => {
  const MANAGER = { ...ME, username: "mona", full_name: "Mona Ali", role: "unit_manager" }
  const UNIT = { id: "u1", name: "ER", archived: false, manager: null, nurse_count: 4 }
  const DAY = { id: "s1", code: "D", display_name: "Day", hours: 12, start_time: "07:00", end_time: "19:00", colour: "#ffcc00", active: true, renames: [] }
  const NIGHT = { ...DAY, id: "s2", code: "N", display_name: "Night", start_time: "19:00", end_time: "07:00" }
  const COVER = {
    id: "c1", month: "2026-12", date: "2026-12-09", shift: "D", state: "open", opened: "leave", opened_at: "2026-12-08T07:30:00+03:00",
    opened_on: 2, note: "", ended_by: null, ended_at: null, taker: null, version: null,
  }
  const OLDER = { ...COVER, id: "c0", date: "2026-12-03", shift: "N", state: "filled", opened: "manager", opened_on: 1, ended_at: "2026-12-02T10:00:00+03:00" }
  const EMAD = { hospital_id: "h2", full_name: "Emad Nasser", worked: 204, owed: 208, overtime_this_year: 0, makes_whole: true, replaces_pin: null }
  const BADR = { hospital_id: "h3", full_name: "Badr Omar", worked: 216, owed: 208, overtime_this_year: 30, makes_whole: false, replaces_pin: "manager" }
  const CHECK = {
    reason: null, full: null, nurses: [EMAD, BADR],
    lack: ["Below its minimum headcount: 0 of at least 1 (the solver's check: headcount: 2026-12-09 D = 0, want 1..1)."],
  }
  const PAGE = { cover: COVER, check: CHECK, offers: [] }
  const RULES = { rules: { shifts: ["D", "N"] }, needs_review: false, request_cap: 2, default_time_limit: 60, time_limit_cap: 300, active_shifts: [] }
  const manager = (extra = {}) => ({
    ...session(MANAGER), "GET /api/units": { body: [UNIT] }, "GET /api/hospital/shifts": { body: [DAY, NIGHT] },
    "GET /api/units/u1/rules": { body: RULES },
    "GET /api/units/u1/covers": { body: { month: "2026-12", covers: [COVER, OLDER] } }, "GET /api/units/u1/covers/c1": { body: PAGE }, ...extra,
  })
  const rowsOf = (region: HTMLElement) => within(region).getAllByRole("row").slice(1)
    .map((row) => within(row).getAllByRole("cell").map((cell) => cell.textContent))

  it("lists the month's covers newest first, and shows the first waiting one", async () => {
    start("/unit-covers?month=2026-12", manager())

    expect(await screen.findByRole("heading", { level: 1, name: "Covers in ER" })).toBeInTheDocument()
    const list = await screen.findByRole("region", { name: "The unit's covers, newest first" })
    expect(rowsOf(list)).toEqual([
      ["Day (D) on 9 Dec 2026", "Not offered yet", "Leave", "", ""],
      ["Night (N) on 3 Dec 2026", "Filled by a new version", "You", "", "Show"]])
    expect(within(list).getByRole("link", { name: "Show the cover for Night (N) on 3 Dec 2026" })).toHaveAttribute("href", "/unit-covers?month=2026-12&cover=c0")
    expect(await screen.findByRole("heading", { level: 2, name: "Day (D) on 9 Dec 2026: Not offered yet" })).toBeInTheDocument()
  })

  it("a cover's page says what its shift-day lacks and who could take it, fairest first, with the figures and the mark", async () => {
    start("/unit-covers?month=2026-12&cover=c1", manager())

    expect(await screen.findByText(/^Opened .* by leave, which took the shift off the roster in force as version 2\.$/)).toBeInTheDocument()
    expect(screen.getByText("Below its minimum headcount: 0 of at least 1 (the solver's check: headcount: 2026-12-09 D = 0, want 1..1).")).toBeInTheDocument()
    expect(rowsOf(screen.getByRole("region", { name: "Who could take it, fairest first" }))).toEqual([
      ["", "Emad Nasser", "204 of 208 hours owed, 4 hours below", "0 hours", "Yes", ""],
      ["", "Badr Omar", "216 of 208 hours owed, 8 hours over", "30 hours", "No", "Your pin to a day off that day would be replaced."]])
    expect(screen.getByText("It is not offered to anyone yet.")).toBeInTheDocument()
  })

  it("a shift-day at its maximum lists nobody and says why", async () => {
    const full = "Day on 9 December 2026 already has 1 of at most 1, so one more nurse would break its headcount. To change who works it, edit the roster."
    start("/unit-covers?month=2026-12&cover=c1", manager({
      "GET /api/units/u1/covers/c1": { body: { ...PAGE, check: { ...CHECK, lack: [], full, nurses: [] } } },
    }))

    expect(await screen.findByText(full)).toBeInTheDocument()
    expect(screen.getByText("On the roster in force this shift-day lacks nothing the solver checks.")).toBeInTheDocument()
    expect(screen.queryByRole("region", { name: "Who could take it, fairest first" })).not.toBeInTheDocument()
  })

  it("a shift-day that lacks nothing lists who could without the mark, which each would have", async () => {
    start("/unit-covers?month=2026-12&cover=c1", manager({ "GET /api/units/u1/covers/c1": { body: { ...PAGE, check: { ...CHECK, lack: [] } } } }))

    const list = await screen.findByRole("region", { name: "Who could take it, fairest first" })
    expect(within(list).getAllByRole("columnheader").map((header) => header.textContent)).toEqual(["Offer", "Nurse", "Hours this month", "Overtime this year", "Note"])
  })

  it("a roster the solver's code cannot read is said, not an error", async () => {
    start("/unit-covers?month=2026-12&cover=c1", manager({
      "GET /api/units/u1/covers/c1": { body: { ...PAGE, check: { reason: "The roster in force cannot be checked: unknown shift X.", lack: [], full: null, nurses: [] }, offers: [] } },
    }))

    expect(await screen.findByText("Who could take it cannot be worked out now. The roster in force cannot be checked: unknown shift X.")).toBeInTheDocument()
  })

  it("the manager withdraws a cover only after a question, and is told", async () => {
    let withdrawn = false
    const ended = { ...COVER, state: "withdrawn", ended_by: "Mona Ali", ended_at: "2026-12-08T09:00:00+03:00" }
    const { user } = start("/unit-covers?month=2026-12&cover=c1", manager({
      "GET /api/units/u1/covers": () => ({ body: { month: "2026-12", covers: [withdrawn ? ended : COVER, OLDER] } }),
      "GET /api/units/u1/covers/c1": () => ({ body: withdrawn ? { cover: ended, check: null, offers: [] } : PAGE }),
      "POST /api/units/u1/covers/c1/withdraw": () => { withdrawn = true; return { body: { cover: ended, check: null, offers: [] } } },
    }))

    await user.click(await screen.findByRole("button", { name: "Withdraw the cover for Day (D) on 9 Dec 2026" }))
    const dialog = await screen.findByRole("alertdialog", { name: "Withdraw the cover for Day (D) on 9 Dec 2026?" })
    expect(withdrawn).toBe(false)
    await user.click(within(dialog).getByRole("button", { name: "Withdraw the cover" }))

    const status = await screen.findByText("You withdrew the cover for Day (D) on 9 Dec 2026. Nothing on the roster changes.")
    await waitFor(() => expect(status).toHaveFocus())
    expect(await screen.findByText(/Withdrawn by Mona Ali/)).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /^Withdraw/ })).not.toBeInTheDocument()
  })

  it("a withdrawal refused says why in the question", async () => {
    const { user } = start("/unit-covers?month=2026-12&cover=c1", manager({
      "POST /api/units/u1/covers/c1/withdraw": { status: 409, body: { detail: "This cover's date has passed, so there is nothing to withdraw." } },
    }))

    await user.click(await screen.findByRole("button", { name: "Withdraw the cover for Day (D) on 9 Dec 2026" }))
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Withdraw the cover" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("This cover's date has passed, so there is nothing to withdraw.")
  })

  it("the manager opens a cover by hand, and is shown it", async () => {
    const mine = { ...COVER, id: "c2", date: "2026-12-12", shift: "N", opened: "manager", opened_on: 2 }
    let sent: unknown = null
    const { user, router } = start("/unit-covers?month=2026-12", manager({
      "POST /api/units/u1/covers": ({ body }: { body: unknown }) => { sent = body; return { status: 201, body: { cover: mine, check: CHECK, offers: [] } } },
      "GET /api/units/u1/covers/c2": { body: { cover: mine, check: CHECK, offers: [] } },
    }))

    await user.click(await screen.findByRole("button", { name: "Open a cover" }))
    await user.type(await screen.findByLabelText("Date"), "2026-12-12")
    await user.selectOptions(screen.getByLabelText("Shift"), "N")
    await user.click(screen.getByRole("button", { name: "Open the cover" }))

    const status = await screen.findByText("You opened a cover for Night (N) on 12 Dec 2026. It is not offered to anyone yet.")
    await waitFor(() => expect(status).toHaveFocus())
    expect(sent).toEqual({ date: "2026-12-12", shift: "N" })
    await waitFor(() => expect(router.state.location.search).toEqual({ month: "2026-12", cover: "c2" }))
  })

  it("a cover refused by hand says why beside its field, or for the month", async () => {
    let answer: Answer = refusal(["date"], "8 December 2026 has passed, so it cannot be covered.")
    const { user } = start("/unit-covers?month=2026-12", manager({ "POST /api/units/u1/covers": () => answer }))

    await user.click(await screen.findByRole("button", { name: "Open a cover" }))
    await user.click(screen.getByRole("button", { name: "Open the cover" }))
    expect(await screen.findByText("Choose the date.")).toBeInTheDocument()
    await user.type(screen.getByLabelText("Date"), "2026-12-08")
    await user.selectOptions(screen.getByLabelText("Shift"), "D")
    await user.click(screen.getByRole("button", { name: "Open the cover" }))

    expect(await screen.findByText("8 December 2026 has passed, so it cannot be covered.")).toBeInTheDocument()
    await waitFor(() => expect(screen.getByLabelText("Date")).toHaveFocus())

    answer = { status: 409, body: { detail: "The ER roster for January 2027 is not approved yet, so there is nothing to cover." } }
    await user.clear(screen.getByLabelText("Date"))
    await user.type(screen.getByLabelText("Date"), "2027-01-08")
    await user.click(screen.getByRole("button", { name: "Open the cover" }))

    expect(await screen.findByRole("alert")).toHaveTextContent("The ER roster for January 2027 is not approved yet, so there is nothing to cover.")
  })

  it("a shift-day in the address opens the form with it filled in, and Cancel puts focus back", async () => {
    const { user } = start("/unit-covers?month=2026-12&date=2026-12-09&shift=N", manager())

    expect(await screen.findByLabelText("Date")).toHaveValue("2026-12-09")
    await waitFor(() => expect(screen.getByLabelText("Shift")).toHaveValue("N"))
    await user.click(screen.getByRole("button", { name: "Cancel" }))

    await waitFor(() => expect(screen.getByRole("button", { name: "Open a cover" })).toHaveFocus())
  })

  it("says when a month has no cover, and leads to the next", async () => {
    start("/unit-covers", manager({ "GET /api/units/u1/covers": { body: { month: "2026-11", covers: [] } } }))

    expect(await screen.findByText("No cover has been opened for November 2026.")).toBeInTheDocument()
    expect(screen.getByRole("link", { name: "Next month" })).toHaveAttribute("href", "/unit-covers?month=2026-12")
  })

  it("is not for a staff nurse", async () => {
    const { router } = start("/unit-covers", { ...session(NURSE), "GET /api/rosters/mine": { body: NO_ROSTER_YET },
      "GET /api/nurses/my-unit": { body: { id: "u1", name: "ER" } } })

    await waitFor(() => expect(router.state.location.pathname).not.toBe("/unit-covers"))
  })

  it("the manager offers a cover to the nurses ticked, all ticked at first, with a note, and is told", async () => {
    let sent: unknown = null
    const offered = { ...COVER, state: "offered", note: "Thank you." }
    const OFFERS = [{ hospital_id: "h2", full_name: "Emad Nasser", answer: "pending", offered_at: "2026-12-08T08:00:00+03:00", answered_at: null }]
    const { user } = start("/unit-covers?month=2026-12&cover=c1", manager({
      "GET /api/units/u1/covers/c1": () => ({ body: sent ? { cover: offered, check: CHECK, offers: OFFERS } : PAGE }),
      "POST /api/units/u1/covers/c1/offer": ({ body }: { body: unknown }) => { sent = body; return { body: { cover: offered, check: CHECK, offers: OFFERS } } },
    }))

    const emad = await screen.findByRole("checkbox", { name: "Offer it to Emad Nasser" })
    expect(emad).toBeChecked()
    await user.click(screen.getByRole("checkbox", { name: "Offer it to Badr Omar" }))
    await user.type(screen.getByLabelText("Note (optional)"), "Thank you.")
    await user.click(screen.getByRole("button", { name: "Offer the cover" }))

    const status = await screen.findByText("You offered the cover for Day (D) on 9 Dec 2026 to 1 nurse. Each is told.")
    await waitFor(() => expect(status).toHaveFocus())
    expect(sent).toEqual({ nurses: ["h2"], note: "Thank you." })
    expect(rowsOf(await screen.findByRole("region", { name: "Nurses offered it, with their answers" }))).toEqual([["Emad Nasser", "Not answered yet"]])
    expect(within(screen.getByRole("region", { name: "Who could take it, fairest first" })).getByText("Offered: not answered yet")).toBeInTheDocument()
  })

  it("on a cover already offered the manager adds those not in it, without a note, and reads each answer", async () => {
    let sent: unknown = null
    const offered = { ...COVER, state: "offered", note: "Thank you." }
    const OFFERS = [{ hospital_id: "h2", full_name: "Emad Nasser", answer: "declined", offered_at: "2026-12-08T08:00:00+03:00", answered_at: "2026-12-08T08:30:00+03:00" }]
    const { user } = start("/unit-covers?month=2026-12&cover=c1", manager({
      "GET /api/units/u1/covers/c1": { body: { cover: offered, check: CHECK, offers: OFFERS } },
      "POST /api/units/u1/covers/c1/offer": ({ body }: { body: unknown }) => { sent = body; return { body: { cover: offered, check: CHECK, offers: OFFERS } } },
    }))

    expect(rowsOf(await screen.findByRole("region", { name: "Nurses offered it, with their answers" }))).toEqual([["Emad Nasser", "Declined, 2026-12-08 08:30:00"]])
    expect(screen.queryByRole("checkbox", { name: "Offer it to Emad Nasser" })).not.toBeInTheDocument()
    expect(screen.queryByLabelText("Note (optional)")).not.toBeInTheDocument()
    expect(screen.getByText("Your note to the nurses offered it: Thank you.")).toBeInTheDocument()
    await user.click(screen.getByRole("button", { name: "Add to the offer" }))

    expect(await screen.findByText("You added 1 nurse to the cover for Day (D) on 9 Dec 2026. Each is told.")).toBeInTheDocument()
    expect(sent).toEqual({ nurses: ["h3"], note: "" })
  })

  it("an offer refused names the nurse in an alert that takes focus", async () => {
    const { user } = start("/unit-covers?month=2026-12&cover=c1", manager({
      "POST /api/units/u1/covers/c1/offer": refusal(["nurses"], "Badr Omar cannot take this cover now, so it cannot be offered to them."),
    }))

    await user.click(await screen.findByRole("button", { name: "Offer the cover" }))

    const alert = await screen.findByRole("alert")
    expect(alert).toHaveTextContent("Badr Omar cannot take this cover now, so it cannot be offered to them.")
    await waitFor(() => expect(document.getElementById("cover-offer-refusal")).toHaveFocus())
  })

  it("a taken cover shows who took it and every answer, and offers nothing", async () => {
    const taken = { ...COVER, state: "taken", taker: "Emad Nasser", version: 3, ended_at: "2026-12-08T09:00:00+03:00" }
    const OFFERS = [
      { hospital_id: "h3", full_name: "Badr Omar", answer: "pending", offered_at: "2026-12-08T08:00:00+03:00", answered_at: null },
      { hospital_id: "h2", full_name: "Emad Nasser", answer: "accepted", offered_at: "2026-12-08T08:00:00+03:00", answered_at: "2026-12-08T09:00:00+03:00" }]
    start("/unit-covers?month=2026-12&cover=c1", manager({ "GET /api/units/u1/covers/c1": { body: { cover: taken, check: null, offers: OFFERS } } }))

    expect(await screen.findByText(/Taken by Emad Nasser, in version 3/)).toBeInTheDocument()
    expect(rowsOf(screen.getByRole("region", { name: "Nurses offered it, with their answers" }))).toEqual([
      ["Badr Omar", "Not answered yet"], ["Emad Nasser", "Accepted, 2026-12-08 09:00:00"]])
    expect(screen.queryByRole("button", { name: /Offer|Add to the offer|Withdraw/ })).not.toBeInTheDocument()
  })

  describe("a staff nurse's covers", () => {
    const MINE = { id: "c1", month: "2026-12", unit: "ER", date: "2026-12-09", shift: "D", hours: 12, note: "We are short, thank you.", state: "offered", my_answer: "pending", answered_at: null }
    const ENDED = { ...MINE, id: "c0", date: "2026-12-03", shift: "N", note: "", state: "taken" }
    const nurse = (covers: Handler | Answer, extra = {}) => ({
      ...session(NURSE), "GET /api/covers/mine": covers, "GET /api/hospital/shifts": { body: [DAY, NIGHT] }, ...extra,
    })

    it("reads each offer's date, shift, hours and note, waiting ones first, and nothing of why", async () => {
      start("/covers?month=2026-12", nurse({ body: { month: "2026-12", covers: [MINE, ENDED] } }))

      expect(await screen.findByRole("heading", { level: 1, name: "Covers" })).toBeInTheDocument()
      expect(document.title).toBe("Covers · Nurse roster")
      const items = within(await screen.findByRole("list", { name: "Covers offered to you: those waiting for your answer first" })).getAllByRole("heading")
      expect(items.map((item) => item.textContent)).toEqual(["Day (D) on 9 Dec 2026: Waiting for your answer", "Night (N) on 3 Dec 2026: Taken by another nurse"])
      expect(screen.getByText("07:00 to 19:00, 12 hours, in ER.")).toBeInTheDocument()
      expect(screen.getByText("Your manager's note: We are short, thank you.")).toBeInTheDocument()
      expect(screen.getAllByRole("button", { name: /^Accept/ }).map((button) => button.getAttribute("aria-label"))).toEqual(["Accept the cover for Day (D) on 9 Dec 2026"])
    })

    it("accepts with one tap, and is told it is hers", async () => {
      let accepted = false
      const { user } = start("/covers?month=2026-12", nurse(() => ({ body: { month: "2026-12", covers: [accepted ? { ...MINE, state: "taken", my_answer: "accepted" } : MINE] } }), {
        "POST /api/covers/mine/c1/accept": () => { accepted = true; return { body: { ...MINE, state: "taken", my_answer: "accepted" } } },
      }))

      await user.click(await screen.findByRole("button", { name: "Accept the cover for Day (D) on 9 Dec 2026" }))

      const status = await screen.findByText("You took the cover for Day (D) on 9 Dec 2026. It is in your cell on My roster now.")
      await waitFor(() => expect(status).toHaveFocus())
      expect(await screen.findByRole("heading", { name: "Day (D) on 9 Dec 2026: You took it" })).toBeInTheDocument()
      expect(screen.queryByRole("button", { name: /^Accept/ })).not.toBeInTheDocument()
    })

    it("a refusal is an alert in her own words that takes focus", async () => {
      const { user } = start("/covers?month=2026-12", nurse({ body: { month: "2026-12", covers: [MINE] } }, {
        "POST /api/covers/mine/c1/accept": { status: 409, body: { detail: "You are on leave on 9 December 2026, so you cannot take this cover." } },
      }))

      await user.click(await screen.findByRole("button", { name: "Accept the cover for Day (D) on 9 Dec 2026" }))

      const alert = await screen.findByRole("alert")
      expect(alert).toHaveTextContent("You are on leave on 9 December 2026, so you cannot take this cover.")
      await waitFor(() => expect(alert).toHaveFocus())
    })

    it("declines only after a question, and is told the manager is", async () => {
      let declined = false
      const { user } = start("/covers?month=2026-12", nurse(() => ({ body: { month: "2026-12", covers: [declined ? { ...MINE, my_answer: "declined" } : MINE] } }), {
        "POST /api/covers/mine/c1/decline": () => { declined = true; return { body: { ...MINE, my_answer: "declined" } } },
      }))

      await user.click(await screen.findByRole("button", { name: "Decline the cover for Day (D) on 9 Dec 2026" }))
      const dialog = await screen.findByRole("alertdialog", { name: "Decline the cover for Day (D) on 9 Dec 2026?" })
      expect(declined).toBe(false)
      await user.click(within(dialog).getByRole("button", { name: "Decline the cover" }))

      const status = await screen.findByText("You declined the cover for Day (D) on 9 Dec 2026. Your manager is told.")
      await waitFor(() => expect(status).toHaveFocus())
      expect(await screen.findByRole("heading", { name: "Day (D) on 9 Dec 2026: You declined it" })).toBeInTheDocument()
    })

    it.each([
      ["withdrawn", "pending", "Withdrawn"], ["filled", "pending", "No longer needed"], ["expired", "pending", "Expired"],
      ["offered", "declined", "You declined it"],
    ])("a cover %s that she answered %s reads %s, with nothing to do", async (state, answer, words) => {
      start("/covers?month=2026-12", nurse({ body: { month: "2026-12", covers: [{ ...MINE, state, my_answer: answer }] } }))

      expect(await screen.findByRole("heading", { name: `Day (D) on 9 Dec 2026: ${words}` })).toBeInTheDocument()
      expect(screen.queryByRole("button", { name: /^(Accept|Decline)/ })).not.toBeInTheDocument()
    })

    it("says when none was offered, and leads to the next month", async () => {
      start("/covers", nurse({ body: { month: "2026-11", covers: [] } }))

      expect(await screen.findByText("No cover waits for your answer, and none was offered to you for November 2026.")).toBeInTheDocument()
      expect(screen.getByRole("link", { name: "Next month" })).toHaveAttribute("href", "/covers?month=2026-12")
    })

    it("is not for a manager", async () => {
      const { router } = start("/covers", { ...session(MANAGER), "GET /api/units": { body: [UNIT] }, "GET /api/rosters/submitted": { body: [] } })

      await waitFor(() => expect(router.state.location.pathname).not.toBe("/covers"))
    })
  })

  it("a cover's notification leads a nurse to Covers and the manager to the cover", async () => {
    const note = (page: string, cover_id: string | null) => ({ id: page, event: "cover_step", text: "A cover moved.",
      link: { page, month: "2026-12", unit_id: null, cover_id }, created_at: "2026-12-08T09:00:00+03:00", read: false })
    start("/notifications", { ...session(NURSE), "GET /api/notifications": { body: { unread: 1, items: [note("covers", null)] } } })

    expect(await screen.findByRole("link", { name: "Open Covers" })).toHaveAttribute("href", "/covers?month=2026-12")
    cleanup()

    start("/notifications", { ...session(MANAGER), "GET /api/notifications": { body: { unread: 1, items: [note("unit_covers", "c1")] } } })

    expect(await screen.findByRole("link", { name: "Open the cover" })).toHaveAttribute("href", "/unit-covers?month=2026-12&cover=c1")
  })
})

// The browser's push machinery is stubbed per test: a service worker registration with its push manager, the
// Notification permission and, for an iPhone, Safari's `navigator.standalone`.
describe("pushes on this device", () => {
  const KEY_BYTES = Uint8Array.from({ length: 65 }, (_, index) => (index === 0 ? 4 : index))
  const OLD_KEY_BYTES = Uint8Array.from({ length: 65 }, (_, index) => (index === 0 ? 4 : 100 + index))
  const PHONE = "https://fcm.googleapis.com/fcm/send/this-phone"
  const NEW_ADDRESS = "https://fcm.googleapis.com/fcm/send/subscribed-now"
  const STATE = "POST /api/notifications/push/state"
  const ON = { public_key: PUSH_KEY, this_device: true, devices: 1 }
  const OFF = { public_key: PUSH_KEY, this_device: false, devices: 0 }
  const page = (extra = {}) => ({ ...session(NURSE), "GET /api/notifications": { body: { unread: 0, items: [] } }, ...extra })

  const defined: [object, string][] = []
  const define = (on: object, name: string, value: unknown) => {
    Object.defineProperty(on, name, { value, configurable: true, writable: true })
    defined.push([on, name])
  }
  afterEach(() => {
    for (const [on, name] of defined.splice(0)) delete (on as Record<string, unknown>)[name]
  })

  type Browser = {
    permission?: NotificationPermission
    /** What the permission question is answered with. */
    answer?: NotificationPermission
    /** The browser's own subscription, and the key it was made with. */
    subscription?: { endpoint: string; key: Uint8Array } | null
    noPush?: boolean
    iPhone?: { standalone: boolean }
    subscribeFails?: boolean
  }

  /** What the browser was asked to do, in order. */
  function browser({ permission = "default", answer = "granted", subscription = null, noPush, iPhone, subscribeFails }: Browser = {}) {
    const did: string[] = []
    const subscribedWith: Uint8Array[] = []
    let current = subscription
    const made = (endpoint: string, key: Uint8Array) => ({
      endpoint,
      options: { applicationServerKey: key.buffer },
      toJSON: () => ({ endpoint, keys: { p256dh: `p256dh-${endpoint.split("/").pop()}`, auth: "auth-secret" } }),
      unsubscribe: async () => { did.push(`unsubscribe ${endpoint}`); current = null; return true },
    })
    const pushManager = {
      getSubscription: async () => (current ? made(current.endpoint, current.key) : null),
      subscribe: async (options: { userVisibleOnly: boolean; applicationServerKey: Uint8Array }) => {
        did.push(`subscribe userVisibleOnly=${options.userVisibleOnly}`)
        subscribedWith.push(options.applicationServerKey)
        if (subscribeFails) throw new DOMException("no", "AbortError")
        current = { endpoint: NEW_ADDRESS, key: options.applicationServerKey }
        return made(NEW_ADDRESS, options.applicationServerKey)
      },
    }
    if (iPhone) {
      define(navigator, "standalone", iPhone.standalone)
      define(navigator, "maxTouchPoints", 5)
    }
    if (!noPush) {
      define(navigator, "serviceWorker", { getRegistration: async () => ({ pushManager }) })
      vi.stubGlobal("PushManager", class {})
      const notification = {
        permission,
        requestPermission: async () => { did.push("ask permission"); notification.permission = answer; return answer },
      }
      vi.stubGlobal("Notification", notification)
    }
    return { did, subscribedWith }
  }

  const section = async () => {
    const heading = await screen.findByRole("heading", { name: "On this device" })
    return within(heading.closest("section")!)
  }
  const told = async (words: string) => {
    const here = await section()
    await waitFor(() => expect(here.getByRole("status")).toHaveTextContent(words))
  }
  const pushCalls = (calls: { url: string; method: string; body: unknown }[]) =>
    calls.filter((call) => call.url.startsWith("/api/notifications/push/") || call.url === "/api/auth/logout")
      .map((call) => [call.url.replace("/api/notifications/push/", ""), call.body])

  describe("its states", () => {
    it("a browser with no push support says so, and still turns off every device", async () => {
      browser({ noPush: true })
      start("/notifications", page({ [STATE]: { body: { ...OFF, devices: 2 } } }))

      await told("This browser cannot receive pushes.")
      const here = await section()
      expect(here.queryByRole("button", { name: "Turn on" })).not.toBeInTheDocument()
      expect(here.getByText("Pushes are on on 2 devices.")).toBeInTheDocument()
      expect(here.getByRole("button", { name: "Turn off on every device" })).toBeInTheDocument()
    })

    it("an iPhone not added to the Home Screen is told to add it, with the steps", async () => {
      browser({ noPush: true, iPhone: { standalone: false } })
      start("/notifications", page())

      await told("Add the app to your Home Screen to turn these on.")
      expect((await section()).getByText("In Safari, tap Share, then Add to Home Screen.")).toBeInTheDocument()
      expect((await section()).queryByRole("button", { name: "Turn on" })).not.toBeInTheDocument()
    })

    it("an iPhone running from the Home Screen can turn them on", async () => {
      browser({ iPhone: { standalone: true } })
      start("/notifications", page())

      expect(await (await section()).findByRole("button", { name: "Turn on" })).toBeInTheDocument()
    })

    it("notifications blocked in the browser say where to undo it", async () => {
      browser({ permission: "denied" })
      start("/notifications", page())

      await told("Notifications are blocked for this site in your browser's settings.")
      expect((await section()).queryByRole("button", { name: "Turn on" })).not.toBeInTheDocument()
    })

    it("off says what a push tells, and offers Turn on", async () => {
      browser()
      start("/notifications", page())

      await told("You are told when a swap, a request window or a roster waits for you, and nothing more.")
      expect((await section()).getByRole("button", { name: "Turn on" })).toBeInTheDocument()
      expect((await section()).queryByRole("button", { name: "Turn off on every device" })).not.toBeInTheDocument()
    })

    it("on says so, offers Turn off, and counts the devices", async () => {
      browser({ permission: "granted", subscription: { endpoint: PHONE, key: KEY_BYTES } })
      const { calls } = start("/notifications", page({ [STATE]: { body: { ...ON, devices: 1 } } }))

      await told("Pushes are on for this device.")
      const here = await section()
      expect(here.getByRole("button", { name: "Turn off" })).toBeInTheDocument()
      expect(here.getByText("Pushes are on on 1 device.")).toBeInTheDocument()
      expect(calls.find((call) => call.url === "/api/notifications/push/state")?.body).toEqual({ endpoint: PHONE })
    })

    it("its controls are as tall as a thumb on a touch screen", async () => {
      browser({ permission: "granted", subscription: { endpoint: PHONE, key: KEY_BYTES } })
      start("/notifications", page({ [STATE]: { body: { ...ON, devices: 2 } } }))

      await told("Pushes are on for this device.")
      for (const name of ["Turn off", "Turn off on every device"]) {
        expect((await section()).getByRole("button", { name })).toHaveClass("pointer-coarse:h-11")
      }
    })
  })

  describe("Turn on", () => {
    it("asks the browser, subscribes with the installation's key, tells the server and shows on", async () => {
      const seen = browser()
      let on = false
      const { user, calls } = start("/notifications", page({
        [STATE]: () => ({ body: on ? ON : OFF }),
        "POST /api/notifications/push/on": () => { on = true; return { body: ON } },
      }))

      await user.click(await (await section()).findByRole("button", { name: "Turn on" }))

      await told("Pushes are on for this device.")
      expect(seen.did).toEqual(["ask permission", "subscribe userVisibleOnly=true"])
      expect([...seen.subscribedWith[0]]).toEqual([...KEY_BYTES])
      expect(calls.find((call) => call.url === "/api/notifications/push/on")?.body).toEqual({
        endpoint: NEW_ADDRESS, keys: { p256dh: "p256dh-subscribed-now", auth: "auth-secret" } })
      expect((await section()).getByText("Pushes are on on 1 device.")).toBeInTheDocument()
    })

    it("replaces a browser subscription made with another key before subscribing", async () => {
      const seen = browser({ permission: "granted", subscription: { endpoint: PHONE, key: OLD_KEY_BYTES } })
      const { user } = start("/notifications", page({ "POST /api/notifications/push/on": { body: ON } }))

      await user.click(await (await section()).findByRole("button", { name: "Turn on" }))

      await told("Pushes are on for this device.")
      expect(seen.did).toEqual(["ask permission", `unsubscribe ${PHONE}`, "subscribe userVisibleOnly=true"])
    })

    it("a refusal shows the blocked state, and nothing is subscribed", async () => {
      const seen = browser({ answer: "denied" })
      const { user, calls } = start("/notifications", page())

      await user.click(await (await section()).findByRole("button", { name: "Turn on" }))

      await told("Notifications are blocked for this site in your browser's settings.")
      expect(seen.did).toEqual(["ask permission"])
      expect(calls.some((call) => call.url === "/api/notifications/push/on")).toBe(false)
    })

    it("a question dismissed says so, and the button stays", async () => {
      browser({ answer: "default" })
      const { user } = start("/notifications", page())

      await user.click(await (await section()).findByRole("button", { name: "Turn on" }))

      await told("Pushes were not turned on: the browser was not given permission.")
      expect((await section()).getByRole("button", { name: "Turn on" })).toBeEnabled()
    })

    it("a subscription the browser cannot make is said in the status line", async () => {
      browser({ subscribeFails: true })
      const { user } = start("/notifications", page())

      await user.click(await (await section()).findByRole("button", { name: "Turn on" }))

      await told("This browser could not subscribe to pushes. Try again later.")
    })

    it("a server that cannot be told is said in the status line", async () => {
      browser()
      const { user } = start("/notifications", page({ "POST /api/notifications/push/on": { status: 500 } }))

      await user.click(await (await section()).findByRole("button", { name: "Turn on" }))

      await told("Pushes could not be turned on. Try again.")
    })
  })

  describe("Turn off", () => {
    it("tells the server, ends the browser's subscription and shows off", async () => {
      const seen = browser({ permission: "granted", subscription: { endpoint: PHONE, key: KEY_BYTES } })
      const { user, calls } = start("/notifications", page({
        [STATE]: { body: { ...ON, devices: 2 } },
        "POST /api/notifications/push/off": { body: { ...OFF, devices: 1 } },
      }))

      await user.click(await (await section()).findByRole("button", { name: "Turn off" }))

      await told("You are told when a swap, a request window or a roster waits for you, and nothing more.")
      expect(calls.find((call) => call.url === "/api/notifications/push/off")?.body).toEqual({ endpoint: PHONE })
      expect(seen.did).toEqual([`unsubscribe ${PHONE}`])
      expect((await section()).getByText("Pushes are on on 1 device.")).toBeInTheDocument()
    })

    it("a failure keeps this device on and says so", async () => {
      const seen = browser({ permission: "granted", subscription: { endpoint: PHONE, key: KEY_BYTES } })
      const { user } = start("/notifications", page({
        [STATE]: { body: ON }, "POST /api/notifications/push/off": { status: 500 },
      }))

      await user.click(await (await section()).findByRole("button", { name: "Turn off" }))

      await told("Pushes could not be turned off. Try again.")
      expect((await section()).getByText("Pushes are on for this device.")).toBeInTheDocument()
      expect(seen.did).toEqual([])
    })

    it("on every device, from this one", async () => {
      const seen = browser({ permission: "granted", subscription: { endpoint: PHONE, key: KEY_BYTES } })
      const { user, calls } = start("/notifications", page({
        [STATE]: { body: { ...ON, devices: 3 } },
        "POST /api/notifications/push/off-everywhere": { body: OFF },
      }))

      await user.click(await (await section()).findByRole("button", { name: "Turn off on every device" }))

      await told("You are told when a swap, a request window or a roster waits for you, and nothing more.")
      expect(calls.some((call) => call.url === "/api/notifications/push/off-everywhere")).toBe(true)
      expect(seen.did).toEqual([`unsubscribe ${PHONE}`])
      expect((await section()).queryByText(/Pushes are on on/)).not.toBeInTheDocument()
    })
  })

  describe("at the app's start", () => {
    it("a subscription bound to a replaced key is replaced with no question, wherever the app opens", async () => {
      const seen = browser({ permission: "granted", subscription: { endpoint: PHONE, key: OLD_KEY_BYTES } })
      const { calls } = start("/", nurseHome("u1", { [STATE]: { body: ON }, "POST /api/notifications/push/on": { body: ON } }))

      await waitFor(() => expect(calls.some((call) => call.url === "/api/notifications/push/on")).toBe(true))
      expect(seen.did).toEqual([`unsubscribe ${PHONE}`, "subscribe userVisibleOnly=true"])
      expect([...seen.subscribedWith[0]]).toEqual([...KEY_BYTES])
      expect(pushCalls(calls)).toEqual([
        ["state", { endpoint: PHONE }],
        ["on", { endpoint: NEW_ADDRESS, keys: { p256dh: "p256dh-subscribed-now", auth: "auth-secret" }, replaces: PHONE }],
      ])
    })

    // Both reads, the start's own check and the page's, are answered inside `act`, which runs everything they lead to.
    it("one the server does not hold is left alone and shows as off", async () => {
      const seen = browser({ permission: "granted", subscription: { endpoint: PHONE, key: OLD_KEY_BYTES } })
      const { calls } = start("/notifications", page())
      const reads = hold(STATE)

      await reads.answerAt(0, { body: OFF })
      await reads.answerAt(1, { body: OFF })

      await told("You are told when a swap, a request window or a roster waits for you, and nothing more.")
      expect(seen.did).toEqual([])
      expect(calls.some((call) => call.url === "/api/notifications/push/on")).toBe(false)
    })

    it("one bound to the current key is left as it is", async () => {
      const seen = browser({ permission: "granted", subscription: { endpoint: PHONE, key: KEY_BYTES } })
      start("/notifications", page())
      const reads = hold(STATE)

      await reads.answerAt(0, { body: ON })
      await reads.answerAt(1, { body: ON })

      await told("Pushes are on for this device.")
      expect(seen.did).toEqual([])
    })
  })

  describe("Log out", () => {
    it("turns this device off first, then signs out", async () => {
      const seen = browser({ permission: "granted", subscription: { endpoint: PHONE, key: KEY_BYTES } })
      const { user, calls } = start("/notifications", page({
        [STATE]: { body: ON },
        "POST /api/notifications/push/off": { body: OFF },
        "POST /api/auth/logout": { status: 204 },
      }))
      await told("Pushes are on for this device.")

      await user.click(screen.getByRole("button", { name: "Log out" }))

      expect(await screen.findByRole("button", { name: "Log in" })).toBeInTheDocument()
      expect(seen.did).toEqual([`unsubscribe ${PHONE}`])
      expect(pushCalls(calls).slice(-2)).toEqual([["off", { endpoint: PHONE }], ["/api/auth/logout", undefined]])
    })

    it("signs out even when the server could not be told", async () => {
      const seen = browser({ permission: "granted", subscription: { endpoint: PHONE, key: KEY_BYTES } })
      const { user, calls } = start("/notifications", page({
        [STATE]: { body: ON },
        "POST /api/notifications/push/off": { status: 500 },
        "POST /api/auth/logout": { status: 204 },
      }))
      await told("Pushes are on for this device.")

      await user.click(screen.getByRole("button", { name: "Log out" }))

      expect(await screen.findByRole("button", { name: "Log in" })).toBeInTheDocument()
      expect(seen.did).toEqual([`unsubscribe ${PHONE}`])
      expect(calls.some((call) => call.url === "/api/auth/logout")).toBe(true)
    })

    it("ends this device's subscription even when notifications were blocked since", async () => {
      const seen = browser({ permission: "denied", subscription: { endpoint: PHONE, key: KEY_BYTES } })
      const { user, calls } = start("/notifications", page({
        "POST /api/notifications/push/off": { body: OFF },
        "POST /api/auth/logout": { status: 204 },
      }))
      await told("Notifications are blocked for this site in your browser's settings.")

      await user.click(screen.getByRole("button", { name: "Log out" }))

      expect(await screen.findByRole("button", { name: "Log in" })).toBeInTheDocument()
      expect(seen.did).toEqual([`unsubscribe ${PHONE}`])
      expect(pushCalls(calls).slice(-2)).toEqual([["off", { endpoint: PHONE }], ["/api/auth/logout", undefined]])
    })

    it("with no subscription here, signs out as before", async () => {
      browser()
      const { user, calls } = start("/notifications", page({ "POST /api/auth/logout": { status: 204 } }))
      await told("You are told when a swap")

      await user.click(screen.getByRole("button", { name: "Log out" }))

      expect(await screen.findByRole("button", { name: "Log in" })).toBeInTheDocument()
      expect(calls.some((call) => call.url === "/api/notifications/push/off")).toBe(false)
    })
  })
})
