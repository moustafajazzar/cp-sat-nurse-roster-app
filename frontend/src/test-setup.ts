import "@testing-library/jest-dom/vitest"
import { createMemoryHistory } from "@tanstack/react-router"
import { cleanup, configure } from "@testing-library/react"
import { afterEach, beforeAll, vi } from "vitest"

import { createAppRouter, createQueryClient } from "./router"

afterEach(cleanup)

// Every page is a chunk of its own, imported the first time a test visits it. Left to that, a page's
// first test also pays for importing it, inside its `findBy`, and on a busy machine that alone runs
// out the wait. Importing them here keeps the code split as it is in production. Only
// imports happen in this hook, and it scales with the machine, hence its own limit.
beforeAll(async () => {
  const router = createAppRouter(createQueryClient(), createMemoryHistory())
  await Promise.all(Object.values(router.routesById).map((route) => router.loadRouteChunk(route)))
}, 60_000)

// `fetch` is stubbed and nothing here waits on anything real, so these two timeouts only measure how
// busy the machine is. A `findBy` still fails by saying what it could not find, because its wait is
// shorter than the test's. Set here because changing `vite.config.ts` restarts a running dev server.
configure({ asyncUtilTimeout: 5_000 })
vi.setConfig({ testTimeout: 20_000 })
