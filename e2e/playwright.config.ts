import { readdirSync, readFileSync } from "node:fs"

import { defineConfig, type PlaywrightTestConfig } from "@playwright/test"

import { desktop, has, journeyStack, phone, stack } from "./support"

/** The browser checks, against stacks e2e/stack.sh started: `E2E_STACK=<seeded> E2E_JOURNEY_STACK=<empty> npx playwright test`.
 *  Either variable alone runs only its own projects.
 *
 *  - The seeded stack (`stack.sh up <project>`): the phone checks at 360 and 320 px, the desktop checks of
 *    every role's reading and the operations, and the manager's pages at 375 px. `deploy` restarts `web`, so it runs last.
 *  - The empty stack (`stack.sh up <project> --empty`): the journey, from the System Admin's first login to a swap in
 *    force, on a phone and then on a desktop, each from an emptied database (`stack.sh reset`). `after-journey` then runs on
 *    what the desktop journey left: races, the clock, a solve whose solver is gone.
 *
 *  The checks share their stack and change it, so they run one at a time; each puts back what it changed, or runs on the
 *  journey's stack, which is emptied before each journey. 320 px is the narrowest the app holds to, which is also a 640 px
 *  window at 200% zoom. */
const PHONE = phone(360, 780)
const SEEDED_ELSEWHERE = /(journey|desktop|phone-375)\/|deploy\.spec/

const seededProjects = (): PlaywrightTestConfig["projects"] => [
  { name: "phone-360", use: { ...PHONE, baseURL: stack.baseURL }, testIgnore: SEEDED_ELSEWHERE },
  { name: "phone-320", use: { ...phone(320, 640), baseURL: stack.baseURL }, testIgnore: SEEDED_ELSEWHERE },
  { name: "phone-375", use: { ...phone(375, 812), baseURL: stack.baseURL }, testMatch: /phone-375\/.*\.spec\.ts/ },
  { name: "desktop", use: { ...desktop, baseURL: stack.baseURL }, testMatch: /desktop\/.*\.spec\.ts/ },
  { name: "deploy", use: { ...PHONE, baseURL: stack.baseURL }, testMatch: /deploy\.spec/,
    dependencies: ["phone-360", "phone-320", "phone-375", "desktop"] },
]

const journeyProjects = (): PlaywrightTestConfig["projects"] => [
  { name: "journey-phone", use: { ...PHONE, baseURL: journeyStack.baseURL }, testMatch: /journey\/journey\.spec\.ts/ },
  { name: "journey-desktop", use: { ...desktop, baseURL: journeyStack.baseURL }, testMatch: /journey\/journey\.spec\.ts/,
    dependencies: ["journey-phone"] },
  { name: "after-journey", use: { ...desktop, baseURL: journeyStack.baseURL }, testMatch: /journey\/after\/.*\.spec\.ts/,
    dependencies: ["journey-desktop"] },
]

// support.ts's `test` watches every page for content security policy violations; a spec taking Playwright's own
// would run unwatched.
const tests = new URL("tests/", import.meta.url)
const unwatched = readdirSync(tests, { recursive: true, encoding: "utf8" }).filter((file) => file.endsWith(".spec.ts"))
  .filter((file) => /import\s*\{[^}]*\btest\b[^}]*\}\s*from\s*"@playwright\/test"/.test(readFileSync(new URL(file, tests), "utf8")))
if (unwatched.length) {
  throw new Error(`Import test from support.ts, not from @playwright/test, so the content security policy is watched: ${unwatched.join(", ")}`)
}

if (!has.seeded && !has.journey) {
  throw new Error("Set E2E_STACK (a seeded stack) or E2E_JOURNEY_STACK (an --empty one), or both: see e2e/stack.sh")
}

export default defineConfig({
  testDir: "tests",
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: [["list"]],
  // A control that never becomes actionable fails in its own words, not as the whole test timing out.
  use: { trace: "retain-on-failure", actionTimeout: 10_000 },
  projects: [...(has.journey ? journeyProjects()! : []), ...(has.seeded ? seededProjects()! : [])],
})
