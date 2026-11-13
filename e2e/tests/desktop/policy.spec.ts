import { expect, request, type Page } from "@playwright/test"

import { newBrowser, signIn, stack, takePolicyViolations, test, type PolicyViolation } from "../../support"

// One content security policy on every answer through Caddy, which the checks' origin passes through
// (e2e/compose.e2e.yaml), and the listener in support.ts that fails a test whose pages report a violation. Without the first
// the listener would pass on pages that have no policy at all; without the second, a violation would go unseen.
const POLICY = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; "
  + "form-action 'self'; frame-ancestors 'none'"

const policies = (headers: { name: string; value: string }[]) =>
  headers.filter(({ name }) => name.toLowerCase() === "content-security-policy").map(({ value }) => value)

test("the pages the checks open carry the one policy a hospital's browser gets through Caddy", async ({ page }) => {
  const caddy = await request.newContext({ baseURL: stack.caddyURL, ignoreHTTPSErrors: true })
  for (const path of ["/login", "/favicon.svg", "/api/health"]) {
    const ours = await page.request.get(path)
    const theirs = await caddy.get(path)
    expect(policies(ours.headersArray()), path).toEqual([POLICY])
    expect(policies(theirs.headersArray()), path).toEqual([POLICY])
  }
  await caddy.dispose()
  const response = await page.goto("/login")
  expect(policies(await response!.headersArray())).toEqual([POLICY])
})

/** An inline script and an inline style put into the page, which the policy refuses. */
const injectInline = (page: Page) => page.evaluate(() => {
  const script = document.createElement("script")
  script.textContent = "window.ranInline = true"
  document.head.append(script)
  const style = document.createElement("style")
  style.textContent = "body { outline: 1px solid red }"
  document.head.append(style)
  return (window as unknown as { ranInline?: boolean }).ranInline ?? false
})

test("the listener sees a violation on the test's page and on a browser the test opens", async ({ browser, page }) => {
  await signIn(page, stack.swap.colleague)
  const theirs = await newBrowser(browser, page, stack.baseURL)
  await theirs.goto("/login")
  for (const one of [page, theirs]) {
    expect(await injectInline(one), "the inline script did not run").toBe(false)
    const seen: PolicyViolation[] = []
    await expect.poll(() => {
      seen.push(...takePolicyViolations())
      return seen.map(({ directive }) => directive).sort()
    }).toEqual(["script-src-elem", "style-src-elem"])
    expect(seen.every((violation) => violation.page === one.url() && violation.blocked === "inline")).toBe(true)
  }
  await theirs.context().close()
})
