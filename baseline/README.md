# Baseline screenshots, before the redesign

Every page in every role's navigation, as it looked at commit `b0e1214` on `main`, captured on 14 November 2026 from a
seeded throwaway stack (`e2e/stack.sh up`, the same seed the browser checks use). This branch is a reference for later
tickets to compare against; it is never merged.

**How they were taken.** Headless Chromium (Playwright 1.64) signed in through the login page as each role and opened each
link in that role's header, plus the sub-pages a role reaches from them: a roster's print view, one solve-history result,
the nurse import pages, a unit's roster from Rosters, and the Day view. Each page was waited on (heading drawn, no
"Loading…", network quiet), then shot full page:

- **desktop**: 1440 × 900 window, device scale 1;
- **375**: 375 × 812, mobile emulation (Pixel 7 user agent, touch), device scale 2.

The header with the phone menu open was shot once per role at 375. Service workers were blocked. Nothing was submitted.

**Months chosen.** On the capture date (November 2026) no unit has a roster for the current month, so every month page was
opened on **December 2026**, which is approved for both units. Request pages use **January 2027**, where ER has an open
request window and ICU has a draft. The Day view shows 15 December 2026.

**Accounts.** System Admin `admin`. CNO `carla`. Deputy CNO `hala`. Unit Manager `omar`, who manages ICU (64 nurses) and
has a nurse profile there, so he also gets My roster and My month. Staff Nurse `Amal` on ER (4 nurses). `mona`, ER's
manager, was captured for one page only: Requests with an open window. Each role saw only the pages that `onlyFor` allows
it. No final address differs from the one asked for, and no page logged a console error.

**Counts.** 77 pages × 2 widths, plus 5 phone menus: 159 images. The header links per role are System Admin 5, CNO 14,
Deputy CNO 13, Unit Manager with a nurse profile 16, Staff Nurse 9 (Change password left out). Notifications, reached
from the bell, was captured for every role.

## Problems seen

These are what showed up while capturing. They are observations, not decisions. File names are relative to this folder.

### Header and navigation

1. **The desktop header wraps at 1440 px.** With 14 to 17 links in one row, the name, bell and Log out drop to a second
   line for the CNO and the Deputy CNO (`cno/home-desktop.png`, `deputy-cno/home-desktop.png`). For a Unit Manager with a
   nurse profile the header takes three rows: brand, links, then user (`unit-manager/home-desktop.png`). Only the System
   Admin's and the Staff Nurse's headers fit on one row. The links are not grouped, and "Change password" sits among the
   page links.
2. **The phone menu is a flat list that pushes the page down.** It holds up to 17 links, each about 44 px tall, with no
   sections, and the page continues below it (`unit-manager/phone-menu-375.png`, `cno/phone-menu-375.png`).
3. **Print views open inside the full app header.** They are also the only pages aligned to the left edge (x = 16)
   (`unit-manager/roster-print-desktop.png`, `staff-nurse/my-roster-print-desktop.png`).

### Layout and width

4. **Content width and left edge change from page to page at 1440 px.** Page headings start at x ≈ 144 (Day view, Roster,
   Rostering attributes), 208 (Rosters, Nurses, Audit log, Solve history, Expiries, Shifts), 272 (Leave, Skills) or 336
   (Home, Health, Technical settings, Units, Unit rules, Hospital rules, Leave types), and at 16 for print views. Compare
   `cno/day-desktop.png`, `cno/rosters-desktop.png` and `cno/units-desktop.png`. Some pages wrap their content in bordered
   cards (`cno/expiries-desktop.png`, `cno/units-desktop.png`), others don't (`cno/rosters-desktop.png`,
   `cno/leave-desktop.png`).
5. **Tables are wider than their column even on desktop.** On Rostering attributes the last column is cut off at 1440 px
   ("Overtime this ye…") in `deputy-cno/rostering-attributes-desktop.png` and `unit-manager/rostering-attributes-desktop.png`.
6. **The roster grid doesn't use the width it has.** At 1440 px the grid is cut off around day 28 inside a column about
   1150 px wide, with white space on both sides, even for ER's 4 nurses (`staff-nurse/my-roster-desktop.png`,
   `cno/unit-roster-er-desktop.png`, `unit-manager/roster-desktop.png`). The print view fits all 31 days plus the totals
   into the same width (`staff-nurse/my-roster-print-desktop.png`).

### At 375 px

7. **No page scrolls sideways**: for every shot `scrollWidth` equals the viewport. Every table, though, is cut off inside
   its own scroll area with no visible cue, often hiding the actions:
   - Users: Status and the Correct name, Deactivate and Reset password buttons (`system-admin/users-375.png`).
   - Audit log: On, Details and IP address (`system-admin/audit-log-375.png`).
   - Nurses: everything after Gender, including five action buttons per row (`cno/nurses-375.png`). Rows are about 80 px
     tall because of the hidden stacked buttons.
   - Rostering attributes: only Name, the Set attributes button and Hospital ID show
     (`unit-manager/rostering-attributes-375.png`).
   - Day view: the overtime column (`cno/day-375.png`).
   - Rosters: from February on (`cno/rosters-375.png`).
   - The Versions table: Print, CSV and Excel are off screen (`cno/unit-roster-er-375.png`).
   - Hours and cost per nurse: from Required on (`unit-manager/solve-history-detail-375.png`).
8. **The roster grid shows about 6 of 31 days at a time** (`unit-manager/roster-375.png`, `staff-nurse/my-roster-375.png`,
   `cno/unit-roster-icu-375.png`), and the nurse-name column takes about a third of the width.
9. **Print views are not adapted**: 8 days, then cut off (`unit-manager/roster-print-375.png`,
   `staff-nurse/my-roster-print-375.png`).
10. **Rosters' month controls wrap around the range heading.** "Earlier month" sits above "December 2026 to March 2027"
    and "Later month" below it (`cno/rosters-375.png`).

### Length and density

11. **The Nurses page is very long.** For the CNO, each of 68 nurses has five buttons stacked in three lines, giving 8,324
    px on desktop and 11,752 px at 375 (`cno/nurses-desktop.png`, `cno/nurses-375.png`). There is no search or paging,
    only a unit filter.
12. **Rule and catalogue forms are long single columns with help text under every field.** Unit rules runs to 6,893 px at
    375 (`unit-manager/unit-rules-375.png`) and Hospital rules to 5,282 px (`cno/hospital-rules-375.png`). Holidays and
    Ramadan repeats a First day / Days / Add form for each proposed holiday (`cno/holidays-desktop.png`).
13. **Long grey explanations come before the content on most pages.** At 375 the manager's Roster has about 600 px of
    headings and explanation (an intro paragraph, the approval line, a second paragraph on editing) before the grid begins
    (`unit-manager/roster-375.png`).
14. **Hours and cost per nurse lists all 64 ICU nurses below the grid,** making Roster 8,648 px tall on desktop
    (`unit-manager/roster-desktop.png`).

### Headings and wording

15. **Weak type hierarchy.** The h1 is only a little larger than section headings and body text. On Home the h1 is the
    user's name, not a page title (`cno/home-desktop.png`, `staff-nurse/home-desktop.png`).
16. **Headings follow different patterns.** The manager's pages say "Roster of ICU", "Nurses of ICU" and "Rostering
    attributes of ICU" but "Requests in ICU", "Swaps in ICU" and "Leave in ICU". The CNO's pages carry no unit ("Rostering
    attributes", "Leave") even with a unit chosen. Print views say "ICU roster, December 2026", and a solve result says
    "Result for December 2026". Omar's Roster and My roster are both headed "Roster of ICU"
    (`unit-manager/roster-desktop.png`, `unit-manager/my-roster-desktop.png`).
17. **Raw values are shown as stored.** Timestamps print as `2026-11-14 07:40:39`, with seconds, on rosters, versions,
    solve history and the audit log. Solve cost prints as a bare number ("Cost 234516"). Audit log details print as raw
    key/value pairs (`role: "system_admin"`, `must_change_password: true`) (`system-admin/audit-log-desktop.png`).
18. **Status words contradict each other.** Every seeded solve shows "Finished" with Duration "Did not start" and Attempts 0
    (`system-admin/solve-history-desktop.png`, `unit-manager/solve-history-detail-desktop.png`). This may come from how
    the seed records its solves, but the page shows both words side by side.
19. **"Defaults, review before the first solve" appears on units that already have approved months.** It shows on Hospital
    rules and Unit rules (`cno/hospital-rules-desktop.png`, `unit-manager/unit-rules-375.png`). Omar's Home says "Its unit
    rules are still the seeded defaults. Review them before its first solve", which also lacks its full stop
    (`unit-manager/home-desktop.png`). This is seed state, but the wording sits beside approved rosters.
20. **The request window's Month field shows Chrome's empty month placeholder "--------- ----"**
    (`unit-manager/requests-desktop.png`, `unit-manager/requests-375.png`).

### Controls and states

21. **Links that don't look like links.** In the Versions table, Print, CSV and Excel are plain text with no underline or
    button style (`unit-manager/roster-desktop.png`).
22. **Month and day controls are named differently.** Most month pages say "Previous month" and "Next month", Rosters says
    "Earlier month" and "Later month", and Day view says "Earlier day" and "Later day".
23. **Row actions wrap.** On Users, the three action buttons take two lines per row (`system-admin/users-desktop.png`).
    On Nurses (CNO) they take three (`cno/nurses-desktop.png`).
24. **Empty states are bare sentences**, with no visual treatment and usually no next step. Examples: "No leave is recorded
    in December 2026." (`cno/leave-desktop.png`), "No swap has been proposed for December 2026."
    (`unit-manager/unit-swaps-desktop.png`), "No skills yet." (`cno/skills-desktop.png`), "No public holidays are entered
    for 2026." (`cno/holidays-desktop.png`), and "You have no notifications."
25. **A Staff Nurse's Home leads with "No roster yet for November 2026."** December is approved, but Home names it only
    through the general My roster link (`staff-nurse/home-desktop.png`).
26. **The Day view's totals row puts two facts in one cell**: "All units / None short" (`cno/day-desktop.png`).

### Not assessed, or artefacts of the capture

- **Loading states**: every shot waited for data, so these images say nothing about them.
- **Notifications**: "Notifications are blocked for this site in your browser's settings" comes from the headless browser,
  not from a user's real setting.
- **Seed data**: "No skills", "no leave", "no holidays", "Last login: Never", and the empty swaps and covers reflect what the
  seed creates, so the empty states are real but expected.

## Index

### Signed out

| Page | Address | Desktop 1440 | Phone 375 |
| --- | --- | --- | --- |
| Login | `/login` | [desktop](login/login-desktop.png) | [375](login/login-375.png) |

### System Admin (`admin`)

| Page | Address | Desktop 1440 | Phone 375 |
| --- | --- | --- | --- |
| Health | `/health` | [desktop](system-admin/health-desktop.png) | [375](system-admin/health-375.png) |
| Technical settings | `/settings` | [desktop](system-admin/settings-desktop.png) | [375](system-admin/settings-375.png) |
| Users | `/users` | [desktop](system-admin/users-desktop.png) | [375](system-admin/users-375.png) |
| Audit log | `/audit-log` | [desktop](system-admin/audit-log-desktop.png) | [375](system-admin/audit-log-375.png) |
| Solve history | `/solve-history?month=2026-12` | [desktop](system-admin/solve-history-desktop.png) | [375](system-admin/solve-history-375.png) |
| Notifications (bell) | `/notifications` | [desktop](system-admin/notifications-desktop.png) | [375](system-admin/notifications-375.png) |
| Header with the phone menu open | `/health (Menu open)` | - | [375](system-admin/phone-menu-375.png) |

### CNO (`carla`)

| Page | Address | Desktop 1440 | Phone 375 |
| --- | --- | --- | --- |
| Home | `/` | [desktop](cno/home-desktop.png) | [375](cno/home-375.png) |
| Rosters | `/rosters?month=2026-12` | [desktop](cno/rosters-desktop.png) | [375](cno/rosters-375.png) |
| Day view | `/day?date=2026-12-15` | [desktop](cno/day-desktop.png) | [375](cno/day-375.png) |
| Units | `/units` | [desktop](cno/units-desktop.png) | [375](cno/units-375.png) |
| Unit rules | `/unit-rules` | [desktop](cno/unit-rules-desktop.png) | [375](cno/unit-rules-375.png) |
| Rostering attributes | `/rostering-attributes` | [desktop](cno/rostering-attributes-desktop.png) | [375](cno/rostering-attributes-375.png) |
| Nurses | `/nurses` | [desktop](cno/nurses-desktop.png) | [375](cno/nurses-375.png) |
| Expiries | `/expiries` | [desktop](cno/expiries-desktop.png) | [375](cno/expiries-375.png) |
| Leave | `/leave?month=2026-12` | [desktop](cno/leave-desktop.png) | [375](cno/leave-375.png) |
| Hospital rules | `/hospital-rules` | [desktop](cno/hospital-rules-desktop.png) | [375](cno/hospital-rules-375.png) |
| Shifts | `/shifts` | [desktop](cno/shifts-desktop.png) | [375](cno/shifts-375.png) |
| Leave types | `/leave-types` | [desktop](cno/leave-types-desktop.png) | [375](cno/leave-types-375.png) |
| Skills | `/skills` | [desktop](cno/skills-desktop.png) | [375](cno/skills-375.png) |
| Holidays and Ramadan | `/holidays` | [desktop](cno/holidays-desktop.png) | [375](cno/holidays-375.png) |
| Notifications (bell) | `/notifications` | [desktop](cno/notifications-desktop.png) | [375](cno/notifications-375.png) |
| Import nurses | `/nurses/import` | [desktop](cno/nurses-import-desktop.png) | [375](cno/nurses-import-375.png) |
| Import skills | `/nurses/import-skills` | [desktop](cno/nurses-import-skills-desktop.png) | [375](cno/nurses-import-skills-375.png) |
| Header with the phone menu open | `/ (Menu open)` | - | [375](cno/phone-menu-375.png) |
| Unit roster, ER (from Rosters) | `/unit-roster?unit=<id>&month=2026-12` | [desktop](cno/unit-roster-er-desktop.png) | [375](cno/unit-roster-er-375.png) |
| Unit roster, ICU (from Rosters) | `/unit-roster?unit=<id>&month=2026-12` | [desktop](cno/unit-roster-icu-desktop.png) | [375](cno/unit-roster-icu-375.png) |
| Roster print view, ER | `/roster/print?unit=<id>&month=2026-12&version=1` | [desktop](cno/roster-print-er-desktop.png) | [375](cno/roster-print-er-375.png) |

### Deputy CNO (`hala`)

| Page | Address | Desktop 1440 | Phone 375 |
| --- | --- | --- | --- |
| Home | `/` | [desktop](deputy-cno/home-desktop.png) | [375](deputy-cno/home-375.png) |
| Rosters | `/rosters?month=2026-12` | [desktop](deputy-cno/rosters-desktop.png) | [375](deputy-cno/rosters-375.png) |
| Day view | `/day?date=2026-12-15` | [desktop](deputy-cno/day-desktop.png) | [375](deputy-cno/day-375.png) |
| Unit rules | `/unit-rules` | [desktop](deputy-cno/unit-rules-desktop.png) | [375](deputy-cno/unit-rules-375.png) |
| Rostering attributes | `/rostering-attributes` | [desktop](deputy-cno/rostering-attributes-desktop.png) | [375](deputy-cno/rostering-attributes-375.png) |
| Import skills | `/nurses/import-skills` | [desktop](deputy-cno/nurses-import-skills-desktop.png) | [375](deputy-cno/nurses-import-skills-375.png) |
| Expiries | `/expiries` | [desktop](deputy-cno/expiries-desktop.png) | [375](deputy-cno/expiries-375.png) |
| Leave | `/leave?month=2026-12` | [desktop](deputy-cno/leave-desktop.png) | [375](deputy-cno/leave-375.png) |
| Hospital rules | `/hospital-rules` | [desktop](deputy-cno/hospital-rules-desktop.png) | [375](deputy-cno/hospital-rules-375.png) |
| Shifts | `/shifts` | [desktop](deputy-cno/shifts-desktop.png) | [375](deputy-cno/shifts-375.png) |
| Leave types | `/leave-types` | [desktop](deputy-cno/leave-types-desktop.png) | [375](deputy-cno/leave-types-375.png) |
| Skills | `/skills` | [desktop](deputy-cno/skills-desktop.png) | [375](deputy-cno/skills-375.png) |
| Holidays and Ramadan | `/holidays` | [desktop](deputy-cno/holidays-desktop.png) | [375](deputy-cno/holidays-375.png) |
| Notifications (bell) | `/notifications` | [desktop](deputy-cno/notifications-desktop.png) | [375](deputy-cno/notifications-375.png) |
| Header with the phone menu open | `/ (Menu open)` | - | [375](deputy-cno/phone-menu-375.png) |
| Unit roster, ICU (from Rosters) | `/unit-roster?unit=<id>&month=2026-12` | [desktop](deputy-cno/unit-roster-icu-desktop.png) | [375](deputy-cno/unit-roster-icu-375.png) |
| Roster print view, ICU | `/roster/print?unit=<id>&month=2026-12&version=1` | [desktop](deputy-cno/roster-print-icu-desktop.png) | [375](deputy-cno/roster-print-icu-375.png) |

### Unit Manager with a nurse profile (ICU) (`omar`)

| Page | Address | Desktop 1440 | Phone 375 |
| --- | --- | --- | --- |
| Home | `/` | [desktop](unit-manager/home-desktop.png) | [375](unit-manager/home-375.png) |
| My roster | `/my-roster?month=2026-12` | [desktop](unit-manager/my-roster-desktop.png) | [375](unit-manager/my-roster-375.png) |
| My month | `/my-month?month=2026-12` | [desktop](unit-manager/my-month-desktop.png) | [375](unit-manager/my-month-375.png) |
| Roster | `/roster?month=2026-12` | [desktop](unit-manager/roster-desktop.png) | [375](unit-manager/roster-375.png) |
| Solve history | `/solve-history?month=2026-12` | [desktop](unit-manager/solve-history-desktop.png) | [375](unit-manager/solve-history-375.png) |
| Requests | `/requests?month=2027-01` | [desktop](unit-manager/requests-desktop.png) | [375](unit-manager/requests-375.png) |
| Swaps | `/unit-swaps?month=2026-12` | [desktop](unit-manager/unit-swaps-desktop.png) | [375](unit-manager/unit-swaps-375.png) |
| Covers | `/unit-covers?month=2026-12` | [desktop](unit-manager/unit-covers-desktop.png) | [375](unit-manager/unit-covers-375.png) |
| Unit rules | `/unit-rules` | [desktop](unit-manager/unit-rules-desktop.png) | [375](unit-manager/unit-rules-375.png) |
| Rostering attributes | `/rostering-attributes` | [desktop](unit-manager/rostering-attributes-desktop.png) | [375](unit-manager/rostering-attributes-375.png) |
| Nurses | `/nurses` | [desktop](unit-manager/nurses-desktop.png) | [375](unit-manager/nurses-375.png) |
| Leave | `/leave?month=2026-12` | [desktop](unit-manager/leave-desktop.png) | [375](unit-manager/leave-375.png) |
| Shifts | `/shifts` | [desktop](unit-manager/shifts-desktop.png) | [375](unit-manager/shifts-375.png) |
| Leave types | `/leave-types` | [desktop](unit-manager/leave-types-desktop.png) | [375](unit-manager/leave-types-375.png) |
| Skills | `/skills` | [desktop](unit-manager/skills-desktop.png) | [375](unit-manager/skills-375.png) |
| Holidays and Ramadan | `/holidays` | [desktop](unit-manager/holidays-desktop.png) | [375](unit-manager/holidays-375.png) |
| Notifications (bell) | `/notifications` | [desktop](unit-manager/notifications-desktop.png) | [375](unit-manager/notifications-375.png) |
| Roster, next month's draft | `/roster?month=2027-01` | [desktop](unit-manager/roster-draft-next-month-desktop.png) | [375](unit-manager/roster-draft-next-month-375.png) |
| Roster print view | `/roster/print?unit=<id>&month=2026-12&version=1` | [desktop](unit-manager/roster-print-desktop.png) | [375](unit-manager/roster-print-375.png) |
| Solve history: one result | `/solve-history/<id>` | [desktop](unit-manager/solve-history-detail-desktop.png) | [375](unit-manager/solve-history-detail-375.png) |
| My roster print view | `/my-roster/print?month=2026-12` | [desktop](unit-manager/my-roster-print-desktop.png) | [375](unit-manager/my-roster-print-375.png) |
| Header with the phone menu open | `/ (Menu open)` | - | [375](unit-manager/phone-menu-375.png) |

### Unit Manager (ER, extra page) (`mona`)

| Page | Address | Desktop 1440 | Phone 375 |
| --- | --- | --- | --- |
| Requests with an open window | `/requests?month=2027-01` | [desktop](unit-manager-er/requests-open-window-desktop.png) | [375](unit-manager-er/requests-open-window-375.png) |

### Staff Nurse (ER) (`Amal`)

| Page | Address | Desktop 1440 | Phone 375 |
| --- | --- | --- | --- |
| Home | `/` | [desktop](staff-nurse/home-desktop.png) | [375](staff-nurse/home-375.png) |
| My month | `/my-month?month=2026-12` | [desktop](staff-nurse/my-month-desktop.png) | [375](staff-nurse/my-month-375.png) |
| My roster | `/my-roster?month=2026-12` | [desktop](staff-nurse/my-roster-desktop.png) | [375](staff-nurse/my-roster-375.png) |
| Swaps | `/swaps?month=2026-12` | [desktop](staff-nurse/swaps-desktop.png) | [375](staff-nurse/swaps-375.png) |
| Covers | `/covers?month=2026-12` | [desktop](staff-nurse/covers-desktop.png) | [375](staff-nurse/covers-375.png) |
| Shifts | `/shifts` | [desktop](staff-nurse/shifts-desktop.png) | [375](staff-nurse/shifts-375.png) |
| Leave types | `/leave-types` | [desktop](staff-nurse/leave-types-desktop.png) | [375](staff-nurse/leave-types-375.png) |
| Skills | `/skills` | [desktop](staff-nurse/skills-desktop.png) | [375](staff-nurse/skills-375.png) |
| Holidays and Ramadan | `/holidays` | [desktop](staff-nurse/holidays-desktop.png) | [375](staff-nurse/holidays-375.png) |
| Notifications (bell) | `/notifications` | [desktop](staff-nurse/notifications-desktop.png) | [375](staff-nurse/notifications-375.png) |
| My month with an open request window | `/my-month?month=2027-01` | [desktop](staff-nurse/my-month-window-desktop.png) | [375](staff-nurse/my-month-window-375.png) |
| My roster print view | `/my-roster/print?month=2026-12` | [desktop](staff-nurse/my-roster-print-desktop.png) | [375](staff-nurse/my-roster-print-375.png) |
| Header with the phone menu open | `/ (Menu open)` | - | [375](staff-nurse/phone-menu-375.png) |
