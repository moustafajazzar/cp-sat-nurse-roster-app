# Framework options for the frontend redesign

Research for the ticket "Which framework stack best fits a data-heavy, accessible, CSP-strict PWA?", on the map
"Wayfinder: upgrade the frontend UI/UX". This is research, not the decision. A later grilling ticket decides.

Researched 2026-11-14 against `main` at `b0e1214`. Every claim carries one of three tags:

- **[measured]**: run here against this repo's code (method and scripts in the appendix).
- **[source]**: read in a primary source, linked under Sources.
- **[inferred]**: my reasoning from the two above. Treat it as a hypothesis.

## The answer in brief

- **Upgraded React wins on every axis except raw runtime cost, and that one is not where this app's time goes.**
  - **[measured]** At 4× CPU slowdown, an arrow-key move in today's editable 60 × 31 grid takes about 35 ms of script and
    about 49 ms to the next frame. A hand-memoised row component, with no framework change, brings that to about 5 ms and
    about 19 ms. Moving focus directly in the DOM, which no framework can beat, costs 0.6 ms and about 15 ms.
  - **[measured]** On a phone-width read-only grid, React's commit is about a quarter of the time to first frame. The rest
    is browser style, layout and paint, and every framework pays it.
- **Svelte 5 and SolidJS are genuinely lighter and faster at the runtime level.**
  - **[source]** In the js-framework-benchmark, React 19 has a weighted geometric mean of about 1.56. Solid 1.9 is about
    1.14 and Svelte 5 about 1.19, where 1.0 is the fastest entry.
  - **[source]** Benchmark app size is about 51 kB brotli for React, against about 4.5 kB for Solid and about 9.7 kB for Svelte.
  - **[inferred]** For this app, that gap is at most about 60 kB gzip off a 193 kB first load, which a hashed asset caches
    once per deploy. It also saves single-digit milliseconds per interaction, and only once the React code stops
    re-rendering the whole grid.
- **Vue 3 is a middle option with a weaker case.** Stable Vue scores about 1.29 and about 23 kB **[source]**. Its
  CSP-friendly Vapor mode is still a release candidate **[source]**.
- **Vanilla TypeScript with web components fits worst.**
  - **[source]** IDREF ARIA attributes cannot cross shadow roots. The fix, Reference Target, is Chromium-first.
  - **[source]** Tailwind's document styles don't reach into shadow trees.
  - **[inferred]** The 11k-line Testing Library suite would stop seeing anything rendered inside a shadow root.
- **The cost of moving is mostly the 15.1k lines of hand-written UI, not the tests.**
  - **[measured]** The 10.8k-line `app.test.tsx` drives the whole app through one `start()` helper using DOM queries.
  - **[inferred]** A port that keeps roles, names and wording keeps most test bodies.
  - **[measured]** The real lock-in is `react-hook-form` (28 files), Base UI (6 files) and TanStack Router's React
    file routes (36 route files).

Leaving React pays off only if the conditions in "What would have to be true" below hold. None of them holds today.

## What the app actually asks of a framework

- **Size.** **[measured]** 15,138 lines of hand-written TS/TSX outside tests and generated code. 11,053 lines of tests, of
  which `src/app.test.tsx` is 10,769 lines and 672 tests. 28.8k lines of Orval output, which is regenerated, not ported.
  The ticket's "27k lines of UI" counts the tests too.
- **The roster grid** (`src/components/roster-grid.tsx`, 271 lines). **[measured]**
  - It is a hand-built ARIA `grid`. One Tab stop with a roving `tabIndex`; arrows, Home, End, PageUp and PageDown move
    within it; Enter or Space opens a Base UI `Menu` (`cell-editor.tsx`).
  - Right-to-left mirroring is read from `getComputedStyle`. `aria-rowcount` and `aria-rowindex` hold through a separate
    "On probation" row group, and the name column and header are sticky.
  - Rows are windowed only above 60 (`WINDOWED_ABOVE = 60`, a strict `>`). A usual month therefore draws every cell,
    1,860 buttons for 60 nurses. Columns are never virtualised.
  - TanStack Table is used with no features (`tableFeatures({})`), only as a column and row model.
  - Shift colours are user-chosen, so each cell gets a `backgroundColor` and `color` style at runtime.
- **CSP.** **[measured]** The Caddyfile sends `script-src 'self'; style-src 'self'` with no `unsafe-eval` and no
  `unsafe-inline`. `style-src 'self'` is as binding as the eval ban: it decides how a framework may write those shift
  colours. `e2e/support.ts` fails any browser check that sees a CSP violation. Zod is already aliased to a `jitless` build
  for the eval half.
- **PWA.** **[measured]** `public/sw.js` is hand-written, framework-free and has no `fetch` listener. It only handles
  install, push and notification clicks. Every option inherits it unchanged.

## Measurements on this app

### First-load bundle

**[measured]** This is a `vite build` of `main` (Vite 8, `autoCodeSplitting`). The sizes are from `gzip -9` on the emitted files.

| What | gzip |
|---|---|
| All JS in `index.html`'s modulepreload list (17 chunks) | 192.5 kB |
| React and ReactDOM, minified on their own | 68.8 kB (about 36% of the above) |
| The `zod` library chunk | 25.9 kB |
| The generated Orval zod schemas (`src/api/generated/zod.ts`, emitted as one chunk) | 24.0 kB |
| CSS | 10.5 kB |

**[inferred]** The generated zod schemas are about as heavy as React's whole runtime minus a Svelte or Solid runtime.
They are framework-independent: Orval's `mode: "single"` output lands in a chunk the entry preloads. A split output mode
or a lazy import is a cheaper win than a framework change.

### The roster grid under CPU throttling

**[measured]** Method: the app's own `RosterGrid`, mounted from the repo source in a production Vite build, with a
synthetic 31-day month.

- **Wiring.** It is fed the way `roster.tsx` feeds it: marks looked up in maps, and the editor's open state and pending
  edits held by the parent.
- **Run.** Chromium 156 headless on a desktop Mac, CPU slowed with `Emulation.setCPUThrottlingRate`, under the app's CSP.
- **Statistics.** Each figure is a median of 13 to 23 runs.
- **What is timed.** "Script" is the time from the keydown or state change until focus moves or React commits. "To
  frame" runs until the next animation frame has run.

The editable grid at 1440 × 900:

| Variant | CPU | Rows (buttons) | Arrow: script | Arrow: to frame | Edit or open editor: commit | Mount: commit → frame |
|---|---|---|---|---|---|---|
| Today's grid | 1× | 60 (1,860) | 7.6 ms | 15.7 ms | 7.3–8.1 ms | 19.5 → 54 ms |
| Today's grid | 4× | 60 (1,860) | 34.6 ms | 49.0 ms | 33.8–34.8 ms | 84 → 231 ms |
| Today's grid | 6× | 60 (1,860) | 52.1 ms | 72.1 ms | 53.2–53.5 ms | 142 → 369 ms |
| Today's grid | 4× | 120 (868, windowed) | 17.9 ms | 24.7 ms | 15.6–17.7 ms | 91 → 114 ms |
| Memoised rows | 4× | 60 (1,860) | 5.0 ms | 19.1 ms | 3.9–5.9 ms | 72 → 211 ms |
| Memoised rows | 6× | 60 (1,860) | 8.4 ms | 31.0 ms | 6.8–9.3 ms | 112 → 333 ms |
| Raw DOM move (floor) | 4× | 60 | 0.6 ms | 15.5 ms | n/a | n/a |
| Raw DOM move (floor) | 6× | 60 | 1.2 ms | 31.3 ms | n/a | n/a |

The read-only grid, the Staff Nurse's "My roster", at 375 × 812, mobile, DPR 3:

| CPU | Rows (cells) | Mount: commit | Mount: to frame |
|---|---|---|---|
| 1× | 60 (1,860) | 12.2 ms | 43 ms |
| 4× | 30 (930) | 30.7 ms | 102 ms |
| 4× | 60 (1,860) | 45.9 ms | 181 ms |
| 6× | 60 (1,860) | 77.8 ms | 289 ms |

What this shows:

- **Every keystroke and every parent state change re-renders all 1,860 cells.** **[measured]** Every `setActive`, every
  opened editor and every pending edit triggers this. Chromium's sampling profiler shows the time in React's
  reconcile and commit, in `EditableCell`, and in one `Intl.DateTimeFormat` call per cell per render.
- **"Memoised rows" is a hand restructure, not React Compiler.** **[measured]** It has a `memo` row component with a
  signature compare and stable handlers through a ref, plus per-column date labels computed once. It removes about 85% of
  the arrow-key script time, which leaves about 4 ms at 4×, one frame. React Compiler was not run on this grid. **[source]**
  The benchmark's `react-compiler-hooks` entry scores no better than plain hooks, but it runs React 19.0, not 19.2.
- **The remaining cost is the browser's.** **[measured]** The raw-DOM floor shows a frame costs about 15 ms at 4× even
  with zero framework work. Mount time to frame is dominated by work after the commit, about 150 ms of 231 ms at 4×. That
  work is style, layout and paint of a large sticky table. **[inferred]** No framework removes it. Fewer DOM nodes do:
  windowing below 60 rows, column virtualisation, or a lighter cell than `<td><button>`.
- **No competitor framework was run on this grid.** **[inferred]** Applying the benchmark's create-rows ratio (React 24.3
  ms against vanilla 19.9 ms), a zero-overhead framework would save roughly 15–20% of the 46 ms phone commit at 4×. That
  is under 10 ms of the 181 ms to first frame.
- **None of these numbers come from a real phone.** CDP throttling of a desktop CPU is an approximation. Real devices,
  especially mid-range Android, may differ in either direction.

### What the CSP allows for styles

**[measured]** A probe page under the app's exact policy, in Chromium 156. Firefox and WebKit binaries for the installed
Playwright were missing, so they are untested.

| How the style is set | Result |
|---|---|
| `el.style.backgroundColor = …` and `el.style.setProperty(…)` (what React does for `style={{…}}`) | applied, no violation |
| `el.style.cssText = …` | applied, no violation (MDN says this is blocked; Chromium allowed it) |
| `el.setAttribute("style", …)` | blocked, `style-src-attr` violation |
| `innerHTML` containing `style="…"` | blocked, `style-src-attr` violation |
| `<template>` parsed with `style="…"`, then cloned and inserted | applied, but a `style-src-attr` violation was reported |
| `<style>` element added at runtime | blocked, `style-src-elem` violation |
| Constructable stylesheet via `adoptedStyleSheets` | applied, no violation |

How each framework sets dynamic styles **[source]**:

- **Svelte 5** (`style.js`): `setProperty` for `style:` directives, and `style.cssText` for a whole `style` attribute.
- **Vue 3** (`runtime-dom/modules/style.ts`): `cssText` for a string style, `setProperty` for an object.
- **Solid** (dom-expressions `client.js`): `cssText` for a string, `setProperty` for an object.
- **Lit**: its `styleMap` uses `setProperty`, and component styles go through `adoptedStyleSheets`. It falls back to
  `<style>` elements with `window.litNonce` only where those are unsupported.

Consequences:

- **[inferred]** Every candidate can write shift colours under `style-src 'self'` if the code keeps to object or directive
  forms (`setProperty`). String styles depend on `cssText`, which MDN says is blocked. Chromium allows it; WebKit, the
  phone engine on iOS, is unverified. Treat that as open.
- **[inferred]** Svelte, Solid and Vue Vapor build DOM by cloning parsed HTML templates. A *static* `style="…"` written in
  a component would sit in that template, and Chromium reported a violation for exactly that case. `e2e/support.ts` would
  fail on it. The rule under these frameworks would be "no static style attributes, only classes or bound styles". React
  never parses markup, so it has no such trap.
- **Eval.** **[source]** Vue's in-browser template compiler calls `new Function`, so Vue must use the default runtime-only
  build with SFCs compiled ahead of time. **[source]** SvelteKit generates inline scripts and styles and handles them with
  hashes or nonces through its `csp` option. **[inferred]** Caddy's fixed `script-src 'self'` would need the hash added,
  or the app would use plain Svelte with Vite and a community router, without SvelteKit. Eval use by the Svelte, Solid
  and Lit runtimes was not checked.
- **Unverified for every alternative component library.** None of Bits UI, Kobalte, Reka UI, Ark UI or Web Awesome was
  checked for runtime `<style>` injection or static style attributes. Base UI runs under today's e2e CSP watcher in CI
  **[inferred]**.

## Comparison by axis

| Axis | Upgraded React | Svelte 5 | SolidJS | Vue 3 | Vanilla TS + web components |
|---|---|---|---|---|---|
| **Editable grid, 60 × 31** | Today's grid works. Memoised rows bring an arrow move within one frame at 4× **[measured]** | Fine-grained updates by default **[inferred]**. TanStack Table 9 and Virtual have Svelte 5 adapters **[source]** | Fine-grained by default **[inferred]**. Table, Virtual and Router have Solid adapters **[source]** | Table and Virtual adapters **[source]**. Vapor (fine-grained) is a release candidate **[source]** | Hand-written. Lit adapters exist for Table and Virtual **[source]**. ARIA IDREFs can't cross shadow roots **[source]** |
| **Runtime speed** (js-framework-benchmark, weighted geomean; 1.0 is fastest) **[source]** | 1.56 (`react-hooks` 19.2) | 1.19 (5.42) | 1.14 (1.9); 1.14 (2.0 release candidate) | 1.29 (3.5); 1.12 (Vapor release candidate) | 1.02–1.10 (vanilla, vanilla-wc); 1.29 (Lit) |
| **Bundle** (benchmark app, brotli) **[source]** | 51.4 kB | 9.7 kB | 4.5 kB | 23.3 kB (Vapor 18.7 kB) | 2.5 kB (vanilla); 7.3 kB (Lit) |
| **Accessible headless components** **[source]** | Base UI 1.9 (in use); React Aria (has a keyboard-navigable Table); Ark UI | Bits UI 2.x (behind shadcn-svelte, an unofficial port); Ark UI | Kobalte 0.13 (pre-1.0); Ark UI | Reka UI 2.x (behind shadcn-vue); Ark UI | Web Awesome 3 (Lit-based successor of Shoelace, which is sunset); building it yourself |
| **Orval client** **[source]** | `react-query` (in use) | `svelte-query` | `solid-query` | `vue-query`, `pinia-colada` | `fetch` (no cache layer) |
| **CSP: eval** | None today (zod `jitless`) **[measured]** | Compiled; SvelteKit needs a hash for its inline boot script **[source]** | Compiled **[inferred]** | Runtime-only build required **[source]** | None **[inferred]** |
| **CSP: styles** | CSSOM only; never parses markup **[measured, source]** | Template-clone trap; string styles go through `cssText` **[inferred]** | Same as Svelte **[inferred]** | Same; Vapor clones templates **[inferred]** | `adoptedStyleSheets` is fine; `style="${}"` attribute bindings are blocked **[measured]** |
| **PWA** | Unchanged (hand-written worker) **[measured]** | Unchanged | Unchanged | Unchanged | Unchanged |
| **Router** | TanStack Router (in use, file routes and typed search) | SvelteKit or a community router; no TanStack Router **[source]** | TanStack Router has a Solid adapter **[source]** | Vue Router; TanStack's repo ships a `vue-router` package, but its overview names only React and Solid **[source]** | Build or pick one |
| **Forms** | react-hook-form (28 files) **[measured]** | TanStack Form, Superforms | TanStack Form, Modular Forms | TanStack Form, VeeValidate | Build it yourself |
| **Testing** | Testing Library and Vitest (in use) | `@testing-library/svelte` (Svelte 5 peer) **[source]** | `@solidjs/testing-library` **[source]** | `@testing-library/vue` **[source]** | DOM Testing Library can't see into shadow roots without an add-on such as `shadow-dom-testing-library` **[inferred]** |
| **Cost of moving** | None | Rewrite 15.1k lines into `.svelte` templates | JSX survives, but component semantics change: components run once, destructured props lose reactivity, effects differ **[inferred]** | Rewrite into SFCs | Rewrite, and also build a component library |

Benchmark details **[source]**:

- Recomputed from `webdriver-ts-results/src/results.ts` at the "incremental run" of 2026-11-02, after the Chrome 156 run.
- Uses the site's weights and the mean of runs, with the factor taken against the fastest of all 207 keyed entries, so
  the site's own table may differ slightly.
- The benchmarked versions trail npm latest: React 19.3.1, Svelte 5.57, Solid 1.9.19 and Vue 3.5.45 at the time of writing.
- Selected raw figures. "Create rows" and "partial update" are ms; "select row" is ms at 4× slowdown; size is kB brotli.

| Entry | Create 1k rows | Partial update | Select row | Size |
|---|---|---|---|---|
| vanillajs | 19.9 ms | 9.4 ms | 2.7 ms | 2.5 kB |
| solid 1.9.3 | 21.4 ms | 10.6 ms | 3.1 ms | 4.5 kB |
| svelte 5.42.1 | 21.9 ms | 11.1 ms | 5.0 ms | 9.7 kB |
| vue 3.5.39 | 23.9 ms | 12.6 ms | 3.7 ms | 23.3 kB |
| lit 3.2.0 | 23.2 ms | 11.9 ms | 5.3 ms | 7.3 kB |
| react-hooks 19.2.0 | 24.3 ms | 14.7 ms | 5.6 ms | 51.4 kB |

The author's caveats **[source]**: results depend on the Chrome version; implementations should be idiomatic; the
current page is a snapshot that may mix browser versions.

## The cost of moving, in detail

- **React-only surface.** **[measured]**
  - `react-hook-form` in 28 files, mostly the long rules and catalogue forms.
  - `@base-ui/react` in 6 files: the cell editor `Menu` and the dialogs.
  - TanStack Router file routes in 36 route files, with `validateSearch`, `beforeLoad` role guards and typed `Link`s.
  - `@tanstack/react-pacer`, and lucide's React icons.
  - Zustand in one file; its core is framework-free.
  - The Orval hooks (regenerated for free) and the `*-words.ts` wording modules (plain TS, carried over unchanged).
- **Tests.** **[measured]** `app.test.tsx` renders the real router, the real generated client and the real fetcher, with
  only `fetch` stubbed, through one `start()` helper.
  - It makes 2,786 `screen` or `within` queries and 807 user-event interactions, all DOM-level.
  - The React or TanStack-specific parts are `start()` itself, 111 `router.` references and 9 `act(` calls.
  - **[inferred]** A port that keeps the accessible tree and wording keeps most test bodies, but nothing was actually
    ported to check this. Timing differences (React's batching against Svelte's or Solid's microtask flushes) would
    surface as `waitFor` changes. Shadow DOM would break `screen` queries outright.
  - The Playwright e2e suite is framework-neutral.
- **Delivery.** **[inferred]** The map wants independent releasable slices with no long-lived branch. A strangler
  migration route by route needs one router owning the URL. TanStack Router has React and Solid adapters, but a router
  instance belongs to one framework. So a strangler means two runtimes in one bundle, with islands mounted inside the
  React tree, for the length of the migration. React to Solid is the only pairing that could share TanStack Router and
  TanStack Query concepts. Every other target is a big-bang rewrite, or a long dual-runtime period that pays both bundle
  costs.

## What would have to be true for leaving React to pay off

All of these, not one:

1. **Interaction stays slow on target phones after the React-side fixes.** That means memoised rows (or React Compiler
   once it runs on this Vite toolchain), per-column labels, a lower windowing threshold or column virtualisation, and
   lazy generated zod schemas. The measure is INP or frame time on a real mid-range Android and an iPhone. Today the
   fixed grid is within one frame at 4× **[measured]**.
2. **The remaining gap is framework overhead, not browser layout.** A profile on the device would have to show
   reconcile and commit, not style, layout and paint, dominating. Today the browser dominates mount **[measured]**.
3. **About 60 kB gzip of first-load JS matters to the users.** That would be true if first loads on poor hospital Wi-Fi
   or cellular were a measured complaint. Hashed assets are cached across visits until the next deploy, so this is a
   per-deploy cost **[inferred]**.
4. **The target keeps the accessibility bar without new risk.** The target's headless library would need to match Base
   UI for menus, dialogs and focus return, and pass the e2e CSP watcher. Kobalte is pre-1.0. Ark UI covers four
   frameworks. Bits UI and Reka UI are the shadcn bases **[source]**. None was CSP-checked here.
5. **Migration fits "independent releasable slices".** Realistically that means Solid, which shares TanStack Router and
   Query. Otherwise the team must accept a dual-runtime period or a rewrite branch, which the map rules out **[inferred]**.
6. **Maintainability improves measurably**, for example fewer lines or fewer bug classes in the grid and the forms.
   Nothing gathered here shows that. Svelte's templates are terser, but react-hook-form and zod forms would need a new
   home **[inferred]**.

If the redesign's new workspace puts much more on one screen (grid, solve progress, edits, violations, live updates),
condition 1 is the one to watch. Re-measure with the prototype on a real phone before deciding.

## Upgraded-React levers this research turned up

- **Memoise grid rows** so one arrow key or edit renders one or two rows, not all 60 **[measured]**. Pass stable props:
  per-row marks with a signature, and handlers through a ref.
- **Format the 31 column dates once per grid, not once per cell per render** **[measured]**.
- **Put the generated zod schemas behind route-level chunks** with Orval's split output or a lazy import, which saves
  about 24 kB gzip on first load **[measured]**.
- **React Compiler** **[source]**: 1.0 is stable. Under Vite 8 it runs through `@vitejs/plugin-react`'s
  `reactCompilerPreset` with `@rolldown/plugin-babel`, or the experimental oxc port with `compiler: true`. Its effect on
  this grid is unmeasured. It would not fix the inline `marks` function that `roster.tsx` passes each render.
- **Lower `WINDOWED_ABOVE` on phones, or virtualise columns.** The browser's layout of 1,860 buttons is the largest
  single cost **[measured]**. This trades away the browser's find-in-page and full screen-reader reading, which the code
  comment keeps on purpose. That is a design decision.

## Cross-framework option

ui-sdk.dev describes itself as a library of interaction behaviours (keyboard navigation, popups, selection), with
components for React, Vue, Svelte, Solid and web components **[source]**. A separate ticket researches it. If adopted,
it would lower the component-library cost of any move, but not the router, forms or rewrite costs.

## Open, not verified

- `style.cssText` under `style-src 'self'` in WebKit and Firefox. MDN says blocked; Chromium 156 allowed it.
- Runtime `<style>` or style-attribute injection by Bits UI, Kobalte, Reka UI, Ark UI and Web Awesome.
- React Compiler's effect on this grid.
- Any measurement on a real phone.
- Reference Target (cross-root ARIA): a Chromium Intent to Ship (blink-dev, mid-2026). Other engines are not committed.

## Sources

- js-framework-benchmark: [README and methodology](https://github.com/krausest/js-framework-benchmark),
  [results data](https://github.com/krausest/js-framework-benchmark/blob/master/webdriver-ts-results/src/results.ts),
  [weighted geometric mean](https://github.com/krausest/js-framework-benchmark/wiki/Computation-of-the-weighted-geometric-mean),
  [current results](https://krausest.github.io/js-framework-benchmark/current.html)
- MDN: [CSP `style-src`](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/style-src)
- React: [React Compiler 1.0](https://react.dev/blog/2025/10/07/react-compiler-1),
  [@vitejs/plugin-react README](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react/README.md)
- Vue: [In-browser template compilation](https://vuejs.org/guide/scaling-up/tooling),
  [`packages/vue/src/index.ts`](https://github.com/vuejs/core/blob/main/packages/vue/src/index.ts) (`new Function`),
  [`runtime-dom/src/modules/style.ts`](https://github.com/vuejs/core/blob/main/packages/runtime-dom/src/modules/style.ts)
- Svelte: [`dom/elements/style.js`](https://github.com/sveltejs/svelte/blob/main/packages/svelte/src/internal/client/dom/elements/style.js),
  [SvelteKit SPA mode](https://svelte.dev/docs/kit/single-page-apps), [SvelteKit `csp` option](https://svelte.dev/docs/kit/configuration)
- Solid: [dom-expressions `client.js`](https://github.com/ryansolid/dom-expressions/blob/main/packages/dom-expressions/src/client.js)
- Lit: [`css-tag.ts`](https://github.com/lit/lit/blob/main/packages/reactive-element/src/css-tag.ts) (`adoptedStyleSheets`, `litNonce`),
  [`style-map.ts`](https://github.com/lit/lit/blob/main/packages/lit-html/src/directives/style-map.ts), [Styles](https://lit.dev/docs/components/styles/)
- TanStack: package lists of [Table](https://github.com/TanStack/table/tree/main/packages),
  [Query](https://github.com/TanStack/query/tree/main/packages), [Virtual](https://github.com/TanStack/virtual/tree/main/packages),
  [Form](https://github.com/TanStack/form/tree/main/packages), [Router](https://github.com/TanStack/router/tree/main/packages);
  [Router overview](https://tanstack.com/router/latest/docs/framework/react/overview); [Svelte Query](https://tanstack.com/query/latest/docs/framework/svelte/overview)
- Orval: [output configuration (`client` values)](https://orval.dev/docs/reference/configuration/output)
- Component libraries: [Ark UI](https://github.com/chakra-ui/ark), [Bits UI](https://github.com/huntabyte/bits-ui),
  [shadcn-svelte](https://www.shadcn-svelte.com/docs), [Kobalte](https://github.com/kobaltedev/kobalte),
  [Reka UI](https://github.com/unovue/reka-ui), [Web Awesome](https://github.com/shoelace-style/webawesome),
  [Shoelace sunset notice](https://github.com/shoelace-style/shoelace), [React Aria Table](https://react-aria.adobe.com/Table)
- Cross-root ARIA: [blink-dev Intent to Ship: Reference Target](https://groups.google.com/a/chromium.org/g/blink-dev/c/eBx3sXIfJnw)
- ui-sdk.dev: [home page](https://ui-sdk.dev)
- Versions and dist-tags: `npm view` on 2026-11-14 (for example `solid-js` next `2.0.0-rc.21`, `vue` rc `3.6.0-rc.15`,
  `babel-plugin-react-compiler` `1.0.0`, `@kobalte/core` `0.13.14`).

## Appendix: method

The scripts lived in a scratch directory and are not committed. Rerunning them would need a fresh write-up.

- **Bundle.** `vite build` of `frontend/` at `b0e1214` into a scratch folder.
  - `gzip -9 | wc -c` on each chunk in `index.html`'s modulepreload list, summed.
  - React's size: `esbuild --bundle --minify` of `createRoot(...).render(null)` with `NODE_ENV=production`, then `gzip -9`.
- **Grid harness.** A Vite project outside the repo that aliases `@` to `frontend/src` and imports the real
  `RosterGrid`. It is built for production, served with Playwright's `page.route` under the app's CSP header, and driven
  in Chromium 156 headless.
  - Synthetic grid: N nurses × 31 days, three shifts with colours, some leave and days off, the last three rows on
    probation, and ten pins.
  - Parent wiring as in `roster.tsx`: `marks` closes over a `pins` map and a `pending` edits record, `onOpen` sets
    parent state, and `extraShifts` is stable.
  - Arrow keys: a `keydown` is dispatched on the focused cell. Timed to `focusin` ("script") and to the next
    `requestAnimationFrame` then `setTimeout(0)` ("to frame"). 25 presses, the first two dropped, median taken.
  - Edits and opening the editor: a parent `setState` inside `flushSync`, timed to return ("commit") and to the next
    frame.
  - Mount: `flushSync(root.render(...))` timed to commit and to the next frame. Five mounts, the first dropped.
  - Memoised variant: a copy of `roster-grid.tsx` where each row is a `memo` component. Its compare covers the row
    data, the active column (or −1), a string signature of the row's 31 marks, the shift map and the labels. Handlers go
    through a ref, and date labels are computed once per grid.
  - Raw-DOM floor: the same move done by setting `tabIndex` on two buttons and calling `focus()`, with no React involved.
  - Profiling: CDP `Profiler` sampling at 100 µs over 20 presses on an unminified build. Self time is grouped by function.
  - Throttling: `Emulation.setCPUThrottlingRate` at 1, 4 and 6.
  - Phone pass: viewport 375 × 812, `isMobile`, DPR 3, the read-only grid (no `editing`), at 30 and 60 rows.
- **CSP probe.** A page served with the Caddyfile's policy. Each case paints an element red, then reads
  `getComputedStyle` back and counts `securitypolicyviolation` events. It ran in Chromium 156 only.
- **Benchmark recomputation.** `results.ts` parsed in Python. Per CPU benchmark, the mean of `total` runs is divided by
  the minimum mean across keyed entries, and the weighted geometric mean uses the weights in
  `webdriver-ts-results/src/Common.ts`.
