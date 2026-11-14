# Is ui-sdk.dev a viable foundation under our constraints?

Researched 2026-11-14 for the Wayfinder map "Wayfinder: upgrade the frontend UI/UX" (ticket "Is ui-sdk.dev a viable
foundation under our constraints?"). This is a throwaway research note and is never merged.

Every source claim below links to upstream commit
[`724a7d6`](https://github.com/vercel-labs/ui/tree/724a7d6c2d0ca1251a8eb6172462ae220c484714) (main on 2026-11-13). The
project moves fast (139 commits in the last week), so line numbers drift. The CSP findings come from reading the source.
Nothing was installed or run: a prototype has to prove them.

## Verdict

**Not viable as a foundation today. Revisit at 1.0.**

- **Too young.** The repo is three weeks old. npm's `latest` tag points at `0.1.0-beta.1`, and there is no GitHub release.
- **Unclear terms.** The README puts the library under Vercel's Public Beta Agreement, on top of its Apache-2.0 license.
- **Grid pieces not ready.** Every behaviour the roster grid would need is marked `@experimental`. The data grid exists
  only as an example file and isn't a shipped component.

The CSP can work, but only if we add a meta tag to `index.html`.

It could sit beside shadcn on Base UI, because both are headless and styled through `data-*` attributes with Tailwind.
But we'd carry two overlay and focus systems, and nobody has tested them together. Its framework-free core and Web
Components adapter would make a later move off React a little easier, but only for the component-behaviour layer.

## Findings

### What it is, who maintains it

- **What it is.** A TypeScript library of UI "behaviours" (keyboard, focus, selection, popups, drag and drop, plus the
  ARIA they need). They live in `@ui-sdk/core`, with adapters for React, Vue, Svelte, Solid and Web Components
  ([README](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/README.md)). It is pitched at AI agents writing the component code: you define a primitive with
  `definePrimitive` and render it with each adapter's `createParts`.
- **Who built it.** The repo is `vercel-labs/ui`, and its homepage is ui-sdk.dev. Of 452 commits, 363 are by shadcn (the
  author of shadcn/ui), 55 by Benjamin Canac (Nuxt UI) and 32 by Shu Ding (`git shortlog` at the pinned commit). The npm
  packages have a single maintainer, `shadcn` (`npm view @ui-sdk/core maintainers`).
- **Influences.** The launch post thanks Radix, Base UI, React Aria, shadcn/ui, Nuxt UI, Geist and the WAI-ARIA APG
  ([introducing-ui-sdk.mdx L403](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/apps/docs/content/docs/core/changelog/introducing-ui-sdk.mdx#L403)).
- **shadcn/ui support.** shadcn/ui does not offer ui-sdk as a component base yet. Its 2026 changelog lists only Radix,
  Base UI (the default) and React Aria ([ui.shadcn.com/docs/changelog](https://ui.shadcn.com/docs/changelog)).

### License and terms

- **License.** The [LICENSE](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/LICENSE) is Apache-2.0, and every package's `package.json` and npm metadata say
  `Apache-2.0`.
- **Beta terms.** The README also says UI SDK is in beta and "subject to the Vercel beta terms"
  ([README L195-198](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/README.md#L195-L198)). Those terms are the
  [Vercel Public Beta Agreement](https://vercel.com/docs/release-phases/public-beta-agreement). It grants a limited
  licence for internal use during the beta period only. It forbids derivative works, assumes a Vercel account, can be
  ended by Vercel at any time, and caps liability at US $100.
- **Open-source clause.** The agreement's section 3 says open-source components stay under their own licence, which
  suggests Apache-2.0 governs the code.
- **Unresolved.** Upstream has no issue that resolves the conflict (searched "beta terms" and "license" in
  vercel-labs/ui issues). For a hospital deployment, this needs a legal read or a clear upstream statement first. This
  note is not legal advice.
- **Pricing.** None. It's an npm library.

### Maturity

| Fact | Source |
| --- | --- |
| Repo created 2026-10-26; last push 2026-11-13; 3.9k stars, 99 forks | `gh repo view vercel-labs/ui` |
| npm packages first published 2026-11-12; versions `0.0.0-stage`, `0.1.0-beta.1`, `0.1.0-beta.2`; dist-tag `latest` = beta.1, `beta` = beta.2 | `npm view @ui-sdk/core versions dist-tags time` |
| No GitHub release | `gh repo view` (`latestRelease: null`) |
| "hasn't reached 1.0 yet, so any of these can still change"; experimental APIs are "the likeliest to" | [reference.mdx L8](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/apps/docs/content/docs/core/reference.mdx#L8) |
| 30 open issues, nearly all filed 2026-11-13/14 from first real use (focus loss after dialogs, Safari < 17.2 crash in sheet animation, type export gaps) | [issues](https://github.com/vercel-labs/ui/issues) |

### Framework support

The adapters are real packages, each at `0.1.0-beta.2`. They come from the `package.json` files under
[packages/](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/packages):

| Package | Peer requirements | Note |
| --- | --- | --- |
| `@ui-sdk/react` | React and React DOM ≥ 19; `@types/react` ≥ 19.2.10 | Our React 19.3.1 qualifies |
| `@ui-sdk/vue` | Vue ≥ 3.5; Nuxt 4/5 as an optional peer | |
| `@ui-sdk/svelte` | Svelte ≥ 5.33 | |
| `@ui-sdk/solid` | `solid-js` / `@solidjs/web` `>=2.0.0-rc.14 <3` | A Solid 2.0 **release candidate**, carrying a local patch ([upstream issue "Solid: remove the @solidjs/web patch…"](https://github.com/vercel-labs/ui/issues/487)) |
| `@ui-sdk/web` | Core only | Custom elements, with styles adopted into shadow roots |

The test suite checks that every framework renders the same examples
([tests/README.md L253](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/tests/README.md#L253)).

### Components and behaviours

- **Components.** About 50 component folders under [core/src/components](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/packages/core/src/components) (accordion
  through tree, plus calendar, combobox, command, date-picker, dialog, drawer, menu, select, toast, toolbar and others).
  There is no data grid or table component.
- **Behaviours.** Core exports 36 `create*` functions (behaviours plus `createController`) from [index.ts](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/packages/core/src/index.ts). Four matter for a
  roster grid:
  - `createGridFocus`: the WAI-ARIA grid pattern (one tab stop; arrows, Home/End, Ctrl+Home/End, PageUp/PageDown). It
    works by index, and a `reveal` callback brings in rows a virtualizer hasn't rendered
    ([grid-focus.ts L1-6](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/packages/core/src/foundation/focus/grid-focus.ts#L1-L6)).
  - `createCellRange`: spreadsheet-style rectangle selection, with copy and paste as tab-separated text
    ([cell-range.ts L1-6](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/packages/core/src/foundation/selection/cell-range.ts#L1-L6)).
  - `createInlineEdit`: text editing in place (Enter/F2, typing starts over, Escape reverts)
    ([inline-edit.ts L1-5](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/packages/core/src/foundation/lists/inline-edit.ts#L1-L5)).
  - `createVirtualizer`: one axis, rows or columns via `horizontal`, not both at once
    ([virtualizer.ts L1-3, L49](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/packages/core/src/foundation/lists/virtualizer.ts#L1-L3)).
- **All experimental.** Each of those four, and its types, is tagged `@experimental`.
- **Data grid example.** A worked React data grid (sort, edit, virtual rows) exists only as
  [examples/experiments/react/data-grid.tsx](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/examples/experiments/react/data-grid.tsx), 753 lines that we'd copy and
  own.
- **Test coverage.** `createGridFocus`, `createCellRange` and `createVirtualizer` have core unit tests
  ([rebuilt-blocks.test.ts L104, L217](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/packages/core/test/rebuilt-blocks.test.ts#L217),
  [editor-blocks.test.ts L742](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/packages/core/test/editor-blocks.test.ts#L742)). Unlike the shipped components, none has
  a browser axe or screen-reader spec under [tests/specs](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/tests/specs).

### Fit for our roster grid (about 60 nurses × 31 days, editable, keyboard-navigable)

- **What we already have.** [`roster-grid.tsx`](../frontend/src/components/roster-grid.tsx) already does most of what
  `createGridFocus` offers:
  - a `role="grid"` table while editing, with `aria-rowcount`/`aria-rowindex`;
  - a roving tab stop on cell buttons;
  - arrows, Home/End and PageUp/PageDown (L157-158);
  - TanStack Virtual rows.
- **What ui-sdk would add.** Ctrl+Home/End, disabled-cell skipping, Shift-extend reporting, and `createCellRange` for
  multi-cell selection, which we don't have. Those are small gains.
- **`createInlineEdit` doesn't fit.** Our cells open a shift-picker menu (`aria-haspopup="menu"`); they aren't edited as
  text.
- **Virtualisation barely matters at this size.** About 1,900 cells is fine fully rendered on desktop, and either
  virtualizer covers rows.
- **Net.** Swapping would replace working, tested code with experimental APIs for little gain.

### Styling and Tailwind

- **Headless, styled through attributes.** Parts render unstyled, carry `data-slot`, and expose state as `data-open`,
  `data-pressed`, `data-disabled` and so on. The docs show Tailwind variants such as `data-pressed:bg-neutral-100`
  ([react/styling-and-animation.mdx L1-25](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/apps/docs/content/docs/react/styling-and-animation.mdx#L1-L25)). That is the
  same idiom as shadcn on Base UI, so our Tailwind 4 tokens would carry over.
- **Optional default stylesheet.** `@ui-sdk/core/web.css` gives a default look. A field report says Tailwind's preflight
  outranks its `@layer ui-sdk` rules
  ([upstream issue "Tailwind preflight outranks @layer ui-sdk…"](https://github.com/vercel-labs/ui/issues/474)). That
  report is a user's, unlabelled, with no maintainer reply yet. It only matters if we use `web.css`; we would style parts
  ourselves.

### Accessibility

- **Pattern-led.** Components follow WAI-ARIA APG patterns.
- **Tested in a browser.** The repo runs axe (`@axe-core/playwright`) in every state, ARIA snapshots, a `checkPattern`
  ARIA-pattern checker that users can also run, and virtual screen-reader phrase logs
  (`@guidepup/virtual-screen-reader`) that must match across frameworks
  ([tests/README.md L136, L144](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/tests/README.md#L136)). That is a stronger harness than most libraries have.
- **Early bugs.** Real-use issues are still open, for example
  ["Dialog mounted open from a menu item leaves focus on <body> when it closes"](https://github.com/vercel-labs/ui/issues/481)
  and
  ["Tooltip: an icon-only trigger has no accessible name in server-rendered HTML"](https://github.com/vercel-labs/ui/issues/493).
- **Grid not covered.** As noted above, the grid behaviours have no browser a11y spec.

### CSP (ours: `script-src 'self'; style-src 'self'`, no nonce, no `unsafe-inline`)

Our policy is in [deploy/Caddyfile L51](../deploy/Caddyfile), and
[e2e/tests/desktop/policy.spec.ts](../e2e/tests/desktop/policy.spec.ts) fails any page that reports a violation.

- **No eval.** No `eval` or `new Function` anywhere in `packages/*/src` (grep at the pinned commit). Our `unsafe-eval` ban
  is fine.
- **Injected `<style>` elements would be blocked by default.** Core writes its own CSS (popover resets, `[hidden]`
  enforcement, toast and sheet defaults) through `writeStyle`
  ([style.ts L89-131](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/packages/core/src/foundation/platform/style.ts#L89-L131)).
  - With no `<meta property="csp-nonce">` tag, `pageNonce` returns `null` and it appends a plain `<style>`. Our
    `style-src 'self'` blocks that, and the e2e listener would fail.
  - With the tag present and no nonce, it uses constructed stylesheets (`adoptedStyleSheets`), which CSP doesn't block.
  - The docs say the same
    ([react/styling-and-animation.mdx L143-177](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/apps/docs/content/docs/react/styling-and-animation.mdx#L143-L177)).
  - So adoption means adding `<meta property="csp-nonce">` to `frontend/index.html`.
- **Inline styles are fine client-side.** Parts emit a style string, which the React adapter parses into a style object
  ([use-primitive.tsx L71-98, L140](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/packages/react/src/use-primitive.tsx#L71-L98)). React then applies it through
  CSSOM, which `style-src` doesn't govern. Other runtime writes use `el.style.setProperty`/`cssText`, also CSSOM.
  - The docs' extra `style-src-attr 'unsafe-inline'` requirement applies only to server-rendered HTML, and we have none.
- **Inline scripts.** `Preferences` renders an inline `<script>` only on the server; a client-only render omits it
  ([react/src/preferences.tsx](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/packages/react/src/preferences.tsx)). So `script-src 'self'` is unaffected.
- **Status.** All of the above comes from reading the source and has not been run. A prototype under our CSP and e2e
  listener would confirm it.

### Beside or instead of shadcn on Base UI

- **Beside.** Technically possible: separate packages, both headless, both styled through `data-*` attributes. But
  we'd have two popup, dismissal and focus-return stacks on one page, plus two portal and layering schemes. Whether
  Base UI's focus trap and ui-sdk's `createPopup` behave together (a ui-sdk menu inside a Base UI dialog, say) is
  untested. Nothing upstream or here covers it.
- **Instead.** A full swap means rewriting our 11 `src/components/ui` files and every caller, against beta APIs, and
  re-verifying our 10k-line `app.test.tsx`. shadcn's CLI and registry don't target ui-sdk, so the shadcn workflow would
  be lost.

### Does it ease a move off React?

- **Partly.** A primitive defined once with `definePrimitive` renders through any adapter with the same parts
  ([README](https://github.com/vercel-labs/ui/blob/724a7d6c2d0ca1251a8eb6172462ae220c484714/README.md)), and the Web Components adapter would serve a vanilla app. So the component-behaviour layer
  would become portable.
- **Most of the move is elsewhere.** The bulk of a framework move is untouched: TanStack Router/Query/Table/Virtual,
  react-hook-form, the orval-generated react-query client, and the test suite. And Solid support rides a 2.0 release
  candidate.
- **Net.** A small easing, not a reason to adopt it now.

## What would change the verdict

- A 1.0 (or a stable `latest`), and an upstream statement that Apache-2.0 alone governs use.
- `createGridFocus` and `createCellRange` losing `@experimental`, with browser a11y and screen-reader specs.
- A prototype route showing ui-sdk under our CSP (with the meta tag) and alongside Base UI overlays, with no violations
  and no focus regressions.
