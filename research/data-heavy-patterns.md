# Navigation, density and command patterns in data-heavy tools

Research for the ticket "Which navigation, density and command patterns do data-heavy tools use?" on the map
"Wayfinder: upgrade the frontend UI/UX". Researched 2026-11-14.

**Question.** How do data-heavy tools (Linear, Airtable, Retool, Grafana) handle navigation across many pages and roles,
information density, keyboard-first use, empty and loading states, and the move from desktop to phone? Which of these
patterns fit an app with five roles and up to 15 pages per role, held to 320 px wide?

**Method and caveats.** Primary sources only: vendor docs, vendor changelogs and design blogs, design-system docs
(GitHub Primer, Atlassian, Android/Material) and W3C (WCAG 2.2, the ARIA Authoring Practices). Pages were read through a
fetch tool that summarises, so wording below is paraphrased; check the linked page before quoting it verbatim in the spec.
Where a vendor says nothing on a topic, this file says so rather than filling the gap. Apple's HIG, Material's own site
(m3.material.io) and Atlassian's navigation-system pages render client-side and could not be read; nothing here rests on
them.

## Short answer

The tools converge on one shape, and most of it fits this app:

1. **Desktop: a persistent left sidebar, grouped, with personal pages first; a slim header for search, notifications and
   the account.** Linear (sidebar of Inbox, My Issues, Workspace, Teams, Favorites), Retool ("vertical navigation works
   better when there are many top-level menu items"; header kept for search and account) and Grafana (grouped menu,
   breadcrumbs, search in the header) all do this. Today's app puts 10 to 17 links in one ungrouped header row.
2. **A command palette on Ctrl/Cmd+K plus a visible search control.** Linear, Grafana and GitHub all open one with Ctrl/Cmd+K,
   mix "go to a page" with "do an action", and rank by context and recent use. For this app it should list the user's own
   pages from `navigation(user)` first; searching nurses, units and months needs a new API.
3. **Keyboard-first, but safely.** Vendors add single-key and two-key sequences (Linear `G` then `I`, Grafana `d` `s`) and a
   `?` sheet. WCAG 2.1.4 requires that printable-key shortcuts can be turned off, remapped, or work only on focus, so this
   app should ship modifier shortcuts (palette, `?` sheet via the palette) and keep any letter sequences behind a setting.
4. **Density is a dial, not a single value.** Airtable offers four row heights (Short is the default), Primer's DataTable
   three paddings, Atlassian an 8 px scale with a compact band for table cells. Compact suits the Unit Manager's desktop
   grid; the Staff Nurse's phone views want comfortable spacing and 44 px targets.
5. **Phone: a bottom bar of 3 to 5 destinations, a rail or sidebar when wider, and a reduced task set.** Android's adaptive
   scaffold switches bar to rail by window size; Material's bar is for three to five equal destinations; Linear and
   Retool Mobile both use a bottom tab bar, and Linear's mobile app targets "away from keyboard" work. The Staff Nurse has
   exactly five primary pages, so a bottom bar fits that role; the other roles have 13 to 16 pages and need grouped
   menus on a phone.
6. **Loading and empty states** come from the design systems, not the four vendors: Primer's timing rules (nothing under
   1 s, spinner to 3 s, determinate beyond, background task past 10 s) and Atlassian's split between an empty state and a
   blank slate.

## The app today

Read from `frontend/src/roles.ts` and `frontend/src/routes/_signed-in.tsx` at the commit this branch was cut from.

| Role | Linked pages | With Change password |
| --- | ---: | ---: |
| System Admin | 5 | 6 |
| CNO | 14 | 15 |
| Deputy CNO | 13 | 14 |
| Unit Manager | 14 | 15 |
| Unit Manager with a nurse profile | 16 | 17 |
| Staff Nurse | 9 | 10 |

- **Shape.** One header. At 640 px and wider (`sm`) every page is a link in a single wrapping row, ungrouped and without
  icons. Narrower, a Menu button discloses a full-width list of the same links (44 px rows, `min-h-11`), plus who is signed
  in and Log out. Escape closes it and returns focus. The notification bell stays in the header in both shapes.
- **Shared pages.** Four catalogue pages (Shifts, Leave types, Skills, Holidays and Ramadan) appear for every role but the
  System Admin, so they are a natural "reference" group.
- **Access.** A page missing from a role's list is only unlinked; the API refuses what the role may not do (the comment on
  `navigation()`). Airtable makes the same distinction (below).
- **Keyboard.** The roster grid (`roster-grid.tsx`) already has one tab stop and moves with the arrows, Home, End, Page Up
  and Page Down, mirrored in right-to-left. Cells are buttons; no typed character does anything in the grid, and there are
  no app-wide shortcuts.
- **Loading and empty.** Text such as "Loading the expiries…", sometimes in `role="status"`, no skeletons.
- **Components.** Only a handful of shadcn components are installed, but `@base-ui/react` 1.9 already ships `dialog`,
  `drawer`, `combobox`, `autocomplete`, `navigation-menu`, `menu`, `scroll-area`, `toast` and `tooltip`, so none of the
  patterns below needs a new dependency.

## Findings

### 1. Navigation across many pages and roles

**Linear.**
- The app's frame is an "inverted L": a sidebar plus a header that together form the "global chrome" around the main
  view. The 2024 redesign worked on that chrome rather than on screens. It cut visual noise, aligned labels, icons and
  buttons in the sidebar and tabs, and raised the hierarchy and density of navigation items.
  ([How we redesigned the Linear UI](https://linear.app/now/how-we-redesigned-the-linear-ui);
  [changelog 2024-03-20](https://linear.app/changelog/2024-03-20-new-linear-ui))
- The sidebar is ordered personal first, then shared: Inbox, My Issues, Workspace, Your teams, and Favorites for pages a
  user visits often. ([Navigating Linear](https://linear.app/enablement/guide/navigating-linear))

**Retool.**
- The Sidebar frame was introduced for "complex, information-dense" apps. Its post names three failures: no navigation
  or an overloaded header, long unstructured pages, and secondary information outweighing primary.
- Retool's answer is to move page links into the sidebar and keep the header for universal actions such as search and
  the account. It states that vertical navigation works better when there are many top-level items.
  ([Introducing the Sidebar Frame](https://retool.com/blog/introducing-the-sidebar-frame-create-intuitive-interfaces-for-complex-apps))
- The frame can configure an optimized mobile layout on small screens. ([Sidebar frame reference](https://docs.retool.com/apps/reference/sidebar))

**Grafana.**
- The 9.x navigation (default from 9.5) groups related tools in the menu, adds breadcrumbs and a sidebar for moving
  between pages, and puts search in a header on every page.
- The stated reason is that a product that has grown must make its added functionality discoverable.
  ([What's new in 9.5](https://archive.grafana.com/docs/grafana/v9.5/whatsnew/whats-new-in-v9-5/))
- A later update shows all levels of the menu in a more compact design, with a better dock and better scrolling.
  ([Navigation updates, 2023-12-18](https://grafana.com/whats-new/2023-12-18-navigation-updates/))

**Airtable (Interface Designer).**
- A published interface has a navigation menu on the left. It is not shown at all when the interface has one page, or when
  the user can reach only one page. ([Getting started with Interface Designer](https://support.airtable.com/articles/8078126534-getting-started-with-airtable-interface-designer))
- Hiding a page removes it from the menu, not from access: anyone with access can still open its link. Airtable recommends
  hiding the pages end users don't need so their menu is shorter.
  ([Managing and sharing interfaces](https://support.airtable.com/articles/4255096925-managing-and-sharing-airtable-interfaces))
- Bookmarks to pages a user can't reach are not shown, and a group with none left is hidden.
  ([Interface layout: Overview](https://support.airtable.com/articles/8961492433-airtable-interface-layout-overview))

**GitHub Primer (design system).**
- `NavList` is a vertical list of links, for links only (mixed links and actions belong in `ActionList`). Group items with
  dividers, never separate single items with them.
- Use leading icons on all items or on none. `NavList.Heading` gives the `<nav>` landmark its accessible name and can be
  visually hidden. ([NavList guidelines](https://primer.style/product/components/nav-list/guidlines/))
- Narrow screens: a sidebar that indexes detail pages is hidden on the detail page, with a back link one level deep and
  breadcrumbs deeper. A sidebar of filters becomes a menu. Minimise navigation elements while keeping the essential ones
  reachable. ([Navigation patterns](https://primer.style/product/ui-patterns/navigation/))

**Pattern.** Persistent grouped sidebar; personal items first; header reserved for search, notifications and account;
the menu shaped by what the user may reach, not by what exists; back links or breadcrumbs for depth.

### 2. Command palette and global search

- **Linear.** Cmd/Ctrl+K opens the command menu for pages and actions. `/` searches issues, projects and documents, and an
  issue opens directly from its identifier. ([Navigating Linear](https://linear.app/enablement/guide/navigating-linear))
  Commands are grouped by function with icons, and groups are re-ranked by the current view.
  ([changelog 2019-12-18](https://linear.app/changelog/2019-12-18-new-command-menu))
- **Grafana.** Cmd/Ctrl+K, or a click on the input in the navigation bar, opens the palette. It finds and opens dashboards,
  folders and pages, creates dashboards and alert rules, and switches between light and dark themes. Backspace in an empty
  input steps back. ([Search](https://grafana.com/docs/grafana/next/search/))
  On launch it listed the five most recently viewed dashboards; its stated aim was keyboard-only movement around the product.
  ([Grafana blog, March 2023](https://grafana.com/blog/2023/03/09/new-in-grafana-cloud-key-improvements-to-the-command-palette-and-navigation-experience))
- **GitHub.** Ctrl/Cmd+K opens a palette scoped to where the user is, with the scope shown and adjustable. Modes have
  prefixes: `#` issues and PRs, `@` users and repos, `/` files, `>` commands, `?` help. Enter opens a result and
  Ctrl/Cmd+Enter opens it in a new tab. Suggestions draw on context and recent use.
  Because the defaults can clash with OS or browser shortcuts, users can change them in accessibility settings.
  ([GitHub Command Palette](https://docs.github.com/en/get-started/accessibility/github-command-palette))
- **Accessibility pattern.** The ARIA combobox keeps DOM focus in the input. `aria-activedescendant` points at the
  highlighted option and `aria-expanded` tracks the popup. Down moves into the list, Enter accepts, and Escape closes and
  returns to the input. ([APG combobox](https://www.w3.org/WAI/ARIA/apg/patterns/combobox/))
- **Airtable and Retool.** Airtable mobile supports Search and Quick Filters
  ([desktop and mobile differences](https://support.airtable.com/articles/9609457174-airtable-desktop-and-mobile-feature-differences));
  no palette guidance was found for either vendor.

**Pattern.** One entry point (Ctrl/Cmd+K and a visible control) that mixes navigation and actions, scoped to context, ranked
by recent use, with a help mode, built on the ARIA combobox.

### 3. Keyboard-first use

- **Linear.** Two-key go-to sequences (`G` then `I` for Inbox, `G` then `M` for My Issues), with `?` for the full list
  ([Navigating Linear](https://linear.app/enablement/guide/navigating-linear)). The `?` sheet is searchable and also
  reachable from Help in the sidebar ([changelog 2021-03-25](https://linear.app/changelog/2021-03-25-keyboard-shortcuts-help)).
- **Grafana.** `?` lists all shortcuts. Dashboard shortcuts include `d` `s` (settings), `d` `k` (kiosk), `Ctrl+S`
  (save) and `Esc`, and panel shortcuts act on the panel under the pointer (`e` edit, `v` view)
  ([Use dashboards](https://grafana.com/docs/grafana/latest/dashboards/use-dashboards/)). The page names no way to turn
  them off.
- **GitHub.** Lets users rebind the palette shortcut (above).
- **WCAG 2.1.4 Character Key Shortcuts (Level A).** A shortcut made only of printable characters must have one of: a way
  to turn it off, a way to remap it to include Ctrl, Alt or similar, or be active only while its component has focus. The
  reason is accidental triggering, which hits speech-input users hardest.
  ([Understanding 2.1.4](https://www.w3.org/WAI/WCAG22/Understanding/character-key-shortcuts.html))
- **WAI-ARIA grid.** One tab stop for the whole grid. Arrows move one cell and don't wrap in a data grid; Home and End go
  to the row's ends, Ctrl+Home and Ctrl+End to the grid's corners, and Page Up and Page Down by an author-chosen number of
  rows. For cells with several widgets or editable content, Enter or F2 suspends grid navigation and Escape restores it.
  ([APG grid](https://www.w3.org/WAI/ARIA/apg/patterns/grid/))

**Pattern.** Palette on a modifier; `?` sheet; letter sequences for experts. Only the first is safe by default under
WCAG 2.1.4.

### 4. Information density

- **Airtable.** Grid views offer Short, Medium, Tall and Extra Tall rows. Short is the default for new views. Shorter rows
  show more records; taller rows show more of each.
  ([Grid view](https://support.airtable.com/articles/7905594155-airtable-grid-view))
- **Primer DataTable.** `cellPadding` is condensed (most data in least space), normal (default) or spacious.
  ([DataTable](https://primer.style/product/components/data-table/))
- **Atlassian.** The spacing scale has an 8 px base. 0 to 8 px serves compact UI such as badges, icon buttons and table
  cells; 12 to 24 px larger, less dense components; 32 px and up layout. The scale is meant to support customisable density
  later. ([Spacing](https://atlassian.design/foundations/spacing))
- **Linear.** The redesign raised navigation density while cutting visual noise. It moved theme generation to LCH, so
  colours at the same lightness look equally light, built the light and dark themes from three inputs (base, accent,
  contrast), and uses the contrast input for high-contrast themes. Accent colour was limited for a neutral, timeless look.
  ([How we redesigned the Linear UI](https://linear.app/now/how-we-redesigned-the-linear-ui))
- **Grafana and Retool.** No first-party density guidance found.

**Pattern.** A small set of named density steps (three or four) on dense surfaces, dense by default where scanning many
rows is the job, and a calm neutral palette so density doesn't read as clutter.

### 5. Empty and loading states

No first-party guidance found from Linear, Airtable, Retool or Grafana; the design systems cover it.

- **Primer loading.** Timing by expected wait:
  - Under 1 s: show nothing, because a flashing indicator feels slower.
  - 1 to 3 s: spinner.
  - 3 to 10 s: determinate indicator if possible.
  - Over 10 s: determinate, and make it a background task so the rest of the UI stays usable.

  Skeletons suit large regions. Put the indicator where the content will appear, avoid layout shift, and show items as
  they arrive. Make one announcement per group of skeletons, set `aria-busy` on a live region while it fills, and keep
  `role="status"` elements always rendered.
  ([Loading](https://primer.style/product/ui-patterns/loading/))
- **Primer degraded experiences.** Don't show a generic empty state for content the user created that failed to load; say
  it is unavailable or drop the section. Keep loading and error states inside the region, not the whole view.
  ([Degraded experiences](https://primer.style/ui-patterns/degraded-experiences))
- **Atlassian.** An *empty state* follows a finished or cleared task (an emptied inbox): give the reason and a next step,
  in short sentence-case wording. A *blank slate* invites first use of a feature. Calls to action are one- or two-word
  verbs. Frequent messages should be shorter and less playful.
  ([Empty state](https://atlassian.design/foundations/content/designing-messages/empty-state))

### 6. From desktop to phone

- **Android and Material.** The adaptive navigation scaffold shows a bottom navigation bar when the window is compact and a
  rail otherwise, and an app can switch to a permanent drawer at expanded widths
  ([Build adaptive navigation](https://developer.android.com/develop/ui/compose/layouts/adaptive/build-adaptive-navigation)).
  A navigation bar is for three to five destinations of equal importance that stay the same across screens
  ([Navigation bar](https://developer.android.com/develop/ui/compose/components/navigation-bar)).
- **Linear Mobile.** Built for "away from keyboard" work: keep up with updates, file issues, unblock the team
  ([changelog 2024-09-19](https://linear.app/changelog/2024-09-19-introducing-linear-mobile)).
  - Its custom tab bar can grow past five items because its users need more. On iPad it becomes a sidebar.
  - The redesign was driven by managers and executives needing team overviews, and it aims at navigation that adapts to
    the user's role ([Linear Liquid Glass](https://linear.app/now/linear-liquid-glass)).
  - Users can reorder the main items and add projects or documents to the bar
    ([changelog 2026-01-22](https://linear.app/changelog/2026-01-22-customize-your-navigation-in-linear-mobile)).
- **Retool Mobile.** A bottom tab bar appears once at least two screens are marked for it. The first tab is the landing
  screen. Other screens are reached by navigate and back actions, modals and bottom sheets.
  ([Assemble mobile screens](https://docs.retool.com/mobile/guides/interaction-navigation/screens))
- **Airtable.** The mobile apps carry grid, list, calendar and kanban views, search, quick filters, forms and interface
  navigation. Automations, timeline and Gantt, CSV import and export, print, and most interface-building controls are
  desktop only. Reading that list, mobile is for viewing, finding and light entry, and building and administration stay on
  desktop; that summary is this file's inference, not Airtable's wording.
  ([Desktop and mobile differences](https://support.airtable.com/articles/9609457174-airtable-desktop-and-mobile-feature-differences))
- **Grafana.** No first-party phone navigation guidance found.
- **WCAG.** Reflow (1.4.10, AA) requires no two-dimensional scrolling at 320 CSS px. Data tables and other content that
  needs two dimensions are excepted, but the heading, search and pagination around them are not, and a table can sit in
  its own scroll container
  ([Understanding 1.4.10](https://www.w3.org/WAI/WCAG22/Understanding/reflow.html)).
  Target Size (2.5.8, AA) is 24×24 CSS px minimum, with a spacing exception; 2.5.5 (AAA) is 44×44
  ([Understanding 2.5.8](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html)).

## Fit for this app

The constraints are five roles, 5 to 17 links per role, 320 px, PWA, a CSP without `unsafe-eval`, today's accessibility,
print layouts, and RTL later.

### Adopt

| Pattern | Why it fits here | Conditions |
| --- | --- | --- |
| **Grouped left sidebar on desktop, header for search, bell and account** (Linear, Retool, Grafana, Primer) | 13 to 17 links is past what a header row can hold without wrapping, the problem Retool names. Grouping also lets the four catalogue pages share one "reference" group across roles. | Group headings via a named `<nav>` (Primer). Use logical properties (`inset-inline-start`, `border-inline-end`) so the sidebar moves for RTL. `print:hidden` as today. |
| **Collapsible sidebar to an icon rail** (Grafana dock, Android rail) | The Unit Manager's roster grid is the widest surface; a rail gives the width back without losing wayfinding. | Icons on all items or none (Primer); every rail item keeps an accessible name and a tooltip. Collapsed state is a per-viewer preference, so `localStorage` behind try/catch is enough. |
| **Personal pages first** (Linear's sidebar order) | A Unit Manager with a nurse profile already gets My roster and My month first (`MANAGER_NURSE_PAGES`). The same rule gives every role a "my work" group. | Group names go in a `*-words.ts` module. |
| **Menu shaped by role, hiding ≠ refusing** (Airtable) | This is already the rule in `roles.ts`; keep `navigation(user)` the single source for sidebar, phone menu and palette alike. | With one reachable page, show no sidebar (Airtable). That doesn't happen with today's roles, but it is the right edge case. |
| **Bottom tab bar for the Staff Nurse on a phone** (Material three to five, Linear and Retool Mobile) | The Staff Nurse has exactly five primary pages (Home, My month, My roster, Swaps, Covers). The four catalogue pages, Change password and Log out go behind an account or "more" entry. | At 320 px five tabs get 64 px each: within the 24 px minimum and the 44 px enhanced target, but whether labels like "My roster" fit unclipped is a prototype question. The bar must sit above the safe-area inset in the installed PWA. |
| **Command palette on Ctrl/Cmd+K plus a visible search button** (Linear, Grafana, GitHub) | It turns 13 to 17 pages into one type-ahead and gives keyboard users one door. It works the same for every role because it reads `navigation(user)`. | Build on Base UI `Dialog` + `Combobox`/`Autocomplete` (already installed, no `eval`), following the APG combobox. The phone entry point is a visible button, since there is no keyboard. Pages first; actions (start a solve, open today's day view) second. |
| **`?` shortcuts sheet, reachable from the palette and a menu item** (Linear, Grafana) | Makes the grid's existing keys (arrows, Home, End, Page Up and Down) discoverable. | Opening it through the palette or a button keeps it free of a bare printable shortcut. |
| **Density steps** (Airtable, Primer, Atlassian) | The desktop roster grid and long tables (Nurses, Expiries, Audit log) are scanning surfaces; compact by default there. The Staff Nurse's phone views stay comfortable with 44 px rows, as the phone menu already is. | Two or three named steps as design tokens, not ad hoc paddings. A user-facing toggle is optional and per viewer. |
| **Primer loading timings and Atlassian's empty/blank-slate split** | Most fetches finish under a second (show nothing). A solve runs for minutes, the "background task" case, which `solves/` already handles on the queue. Skeletons suit the roster grid; text suits small regions. | Keep wording in `*-words.ts`. Keep `role="status"` regions always rendered (Primer), which today's conditional "Loading…" paragraphs are not. |

### Adapt or defer

- **Global search across records** (Linear's `/`, GitHub's `#` and `@`). Searching nurses, units or a month's roster from the
  palette needs a backend endpoint that respects roles. The map already lists "global search" under API changes not yet
  specified. Phase 1 of the palette can ship with pages and actions only.
- **Phone navigation for the CNO, Deputy CNO and Unit Manager.** 13 to 16 pages exceed any bottom bar. Options: a bar of four
  plus "More" leading to the grouped list, or keeping today's disclosure menu but grouped. The phone is a lower-priority
  surface for these roles (per the map), so this is a prototype comparison, not a decision here.
- **Breadcrumbs or back links** (Primer, Grafana). Useful where pages already go deeper (Solve history → a solve, Nurses →
  Import skills); not needed as global chrome.
- **User-customised navigation** (Linear favourites and mobile bar reordering). Low value at 5 to 17 fixed pages, and it
  adds stored state. If it is ever added, don't call it pinning: "pin" is a glossary term for a fixed cell.

### Avoid

- **Bare-letter shortcuts on by default** (Linear's `G` `I`, Grafana's `d` `s`). WCAG 2.1.4 requires an off switch, a remap
  or focus scoping. If go-to sequences are wanted, ship them off by default behind a setting. Never fire them while focus
  is in an input, a combobox or a grid cell, so the grid's own keys and any future typed entry are unaffected.
- **Multi-level flyout menus** (Grafana's all-levels menu). The app's hierarchy is one level of pages plus a few detail
  pages; flyouts add pointer-hover dependence for no gain.
- **A hamburger-only menu as the Staff Nurse's main navigation**, as today. It hides all five primary destinations behind
  a tap; the bottom bar keeps them visible (Material: consistent destinations across screens).

## Questions this leaves for later tickets

1. Which groups, and which group names, does each role's sidebar have? This belongs to the IA/nav decision and needs a
   grilling session with `GLOSSARY.md`.
2. At what width does the sidebar appear, and when does it collapse to a rail? It has to coexist with the roster grid at
   1280 px and with 400 % zoom (320 CSS px).
3. Five tabs at 320 px: icons with labels, a label-only bar, or four tabs and More? This is a prototype question.
4. Which actions belong in the palette in phase 1, and which need the search API?

## Sources

Vendors
- Linear: [How we redesigned the Linear UI](https://linear.app/now/how-we-redesigned-the-linear-ui) ·
  [New Linear UI changelog](https://linear.app/changelog/2024-03-20-new-linear-ui) ·
  [Navigating Linear](https://linear.app/enablement/guide/navigating-linear) ·
  [New command menu](https://linear.app/changelog/2019-12-18-new-command-menu) ·
  [Keyboard shortcuts help](https://linear.app/changelog/2021-03-25-keyboard-shortcuts-help) ·
  [Introducing Linear Mobile](https://linear.app/changelog/2024-09-19-introducing-linear-mobile) ·
  [Linear Liquid Glass](https://linear.app/now/linear-liquid-glass) ·
  [Customize your navigation in Linear Mobile](https://linear.app/changelog/2026-01-22-customize-your-navigation-in-linear-mobile)
- Airtable: [Getting started with Interface Designer](https://support.airtable.com/articles/8078126534-getting-started-with-airtable-interface-designer) ·
  [Managing and sharing interfaces](https://support.airtable.com/articles/4255096925-managing-and-sharing-airtable-interfaces) ·
  [Interface layout: Overview](https://support.airtable.com/articles/8961492433-airtable-interface-layout-overview) ·
  [Grid view](https://support.airtable.com/articles/7905594155-airtable-grid-view) ·
  [Desktop and mobile feature differences](https://support.airtable.com/articles/9609457174-airtable-desktop-and-mobile-feature-differences)
- Retool: [Introducing the Sidebar Frame](https://retool.com/blog/introducing-the-sidebar-frame-create-intuitive-interfaces-for-complex-apps) ·
  [Sidebar frame reference](https://docs.retool.com/apps/reference/sidebar) ·
  [Assemble mobile screens](https://docs.retool.com/mobile/guides/interaction-navigation/screens)
- Grafana: [What's new in 9.5](https://archive.grafana.com/docs/grafana/v9.5/whatsnew/whats-new-in-v9-5/) ·
  [Navigation updates (2023-12-18)](https://grafana.com/whats-new/2023-12-18-navigation-updates/) ·
  [Command palette and navigation (blog)](https://grafana.com/blog/2023/03/09/new-in-grafana-cloud-key-improvements-to-the-command-palette-and-navigation-experience) ·
  [Search](https://grafana.com/docs/grafana/next/search/) ·
  [Use dashboards (keyboard shortcuts)](https://grafana.com/docs/grafana/latest/dashboards/use-dashboards/)
- GitHub: [Command Palette](https://docs.github.com/en/get-started/accessibility/github-command-palette)

Design systems
- Primer: [NavList guidelines](https://primer.style/product/components/nav-list/guidlines/) ·
  [Navigation patterns](https://primer.style/product/ui-patterns/navigation/) ·
  [DataTable](https://primer.style/product/components/data-table/) ·
  [Loading](https://primer.style/product/ui-patterns/loading/) ·
  [Degraded experiences](https://primer.style/ui-patterns/degraded-experiences)
- Atlassian: [Spacing](https://atlassian.design/foundations/spacing) ·
  [Empty state](https://atlassian.design/foundations/content/designing-messages/empty-state)
- Android / Material 3: [Build adaptive navigation](https://developer.android.com/develop/ui/compose/layouts/adaptive/build-adaptive-navigation) ·
  [Navigation bar](https://developer.android.com/develop/ui/compose/components/navigation-bar)

Standards
- W3C: [Understanding 2.1.4 Character Key Shortcuts](https://www.w3.org/WAI/WCAG22/Understanding/character-key-shortcuts.html) ·
  [Understanding 1.4.10 Reflow](https://www.w3.org/WAI/WCAG22/Understanding/reflow.html) ·
  [Understanding 2.5.8 Target Size (Minimum)](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html) ·
  [APG combobox pattern](https://www.w3.org/WAI/ARIA/apg/patterns/combobox/) ·
  [APG grid pattern](https://www.w3.org/WAI/ARIA/apg/patterns/grid/)

App files read: `frontend/src/roles.ts`, `frontend/src/routes/_signed-in.tsx`, `frontend/src/components/roster-grid.tsx`,
`GLOSSARY.md`, and the installed `@base-ui/react` package directory.
