// What `import … from "zod"` resolves to in the build and the tests (vite.config.ts's alias), so it runs before any module
// that builds a schema, whatever the chunks. Caddy's content security policy allows no eval, and zod tries
// `new Function("")` once, when the first object schema is built, to decide whether to compile its fast parser. The browser
// refuses it and reports a violation, which zod swallows but the browser checks fail on (e2e/support.ts). Under `jitless` zod
// never tries, and parses as it would have after the refusal anyway. Types come from zod itself, as TypeScript reads "zod".
import { config } from "zod/v4"

config({ jitless: true })

export * from "zod/v4"
export { z, z as default } from "zod/v4"
