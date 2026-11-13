import { z } from "zod"

test("zod, as the app imports it, never tries eval: jitless is set before any schema is built", () => {
  expect(z.config().jitless).toBe(true)
})
