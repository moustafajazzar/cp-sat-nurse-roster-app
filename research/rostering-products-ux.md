# How rostering products design the roster workspace and the staff phone experience

Research for the Wayfinder map "Wayfinder: upgrade the frontend UI/UX", answering the ticket "How do rostering products
design the roster workspace and the staff phone experience?". Researched 2026-11-14.

Products: RLDatix HealthRoster (now sold as Allocate Optima, with the Loop staff app), Deputy, When I Work, Rotageek and
Quinyx.

## How much to trust each claim

Every claim below carries a tag for its evidence:

- **[help]**: the vendor's own help-centre article, opened and read.
- **[product]**: the vendor's product page or blog (marketing, which says what the product claims, not how it looks).
- **[customer]**: a hospital trust's own guide to the product, with screenshots of the real screens. It is first-hand but
  configured locally, so the screens may differ elsewhere.
- **[snippet]**: a search-result summary of a vendor page that could not be opened (Deputy's help centre refuses automated
  reads). Treat these as weaker.

HealthRoster's manager screens are not documented publicly. RLDatix's help sits behind a customer login, so its
workspace comes mainly from one NHS trust's Roster Approval guide, which has annotated screenshots. Deputy's manager side
rests mostly on snippets.

## Answer in brief

**Recurring patterns (four or five of the five products):**

1. **A people-by-days grid is the workspace.** Rows are staff, columns are days. Above or below the grid sit **per-day
   totals and a demand or coverage line**: how many are on against how many are needed.
2. **Unfilled work has its own row at the top of the grid.** Vendors call these open shifts, gap shifts, unassigned shifts
   or unfilled duties. A manager fills them by hand from a list of eligible staff, with ineligible staff greyed out, or
   hands them to the staff app for staff to claim.
3. **Rule breaches show in three places at once:** a mark on the shift tile, a count beside the person's name, and a
   **panel or summary that lists them all**. Selecting a breach in the panel jumps to the cells it is about. Most
   products split breaches into an overridable warning and a hard stop.
4. **Auto-scheduling produces a draft for review, never a decision.** The manager reviews the result, can undo or revert
   it, edits by hand in the same grid, then publishes.
5. **Publishing is a distinct, visible step** that tells staff, and the screen shows what is published and what is not.
6. **The staff phone app is built around "my shifts"**, a week or a list with a month calendar on demand. Next to it sit
   an available-shifts list, a requests inbox and a team view. A swap or drop request shows a pending state, and staff
   stay responsible for the shift until it is approved.
7. **Swaps run through stages**: a colleague accepts, then (optionally) the manager approves. Eligibility (skills,
   rest, hours) is checked before a colleague is offered the swap.

**Specific to healthcare (HealthRoster above all, plus the healthcare framing of Deputy):**

- **Long horizons with a timetable.** A roster period is about four weeks and is built weeks ahead. It has fixed deadlines
  for when staff requests close, when the draft is due, partial approval, full approval, and when unfilled duties go to
  the bank.
- **Two-level approval** (partial, then full) with a pre-approval **analyser**. The analyser scores the roster on safety
  (unfilled duties, missing charge cover, missing mandatory skills, skill mix), effectiveness (hours against contract),
  fairness (requests granted, duties with warnings) and leave. It also blocks approval while any leave request is still
  undecided.
- **Skill mix and charge (team-leader) cover** are first-class safety measures, alongside plain headcount.
- **Requested duties** (staff asking for particular shifts before the roster is built), and **bank shifts**, which only
  eligible staff can see.
- **Credential and fatigue rules**: rest between shifts, expired qualifications. Deputy's healthcare material makes these
  its main selling point.

**For this app, in short.** The Unit Manager's roster page already holds every piece: the grid, solve, re-plan, pins,
edits, violations, versions and covers. But they sit in one long column of cards, and violations come as a list below
the grid. The products' common answer is one workspace: a toolbar for solving and submitting, per-shift-day totals pinned
to the grid, violations marked on the cells plus a panel that jumps to them, and an analyser-style summary before
**Submit**. The Staff Nurse's phone experience today is spread across Home, My roster (a full unit grid), My month,
Swaps and Covers. The products point to a phone home built on "my next shifts", with a short list of what needs the
nurse (offered covers, swap answers, open request windows) and the unit grid one step away.

---

## 1. The manager's roster workspace

### RLDatix HealthRoster / Allocate Optima

- **Name.** RLDatix has renamed HealthRoster to Allocate Optima, with the same features. [product]
  ([RLDatix: Allocate Optima](https://www.rldatix.com/en-uki/solution/optima/))
- **Layout.** The "View Rosters" page has three parts:
  - a left list of **requirements** (grades and skills, such as registered nurses and healthcare assistants), each with a
    count of unfilled duties;
  - a pink **unfilled-duties band** across the top of the grid, holding the duties not yet given to anyone, set against
    the budgeted establishment;
  - the **main grid**, with people grouped by role and dates across, about 4 weeks at a time, and coloured shift tiles
    labelled with short codes.

  Two right-hand columns show live **net hours and hours left** per person, which the manager is meant to bring to zero.
  An **Assignment Count** button opens a per-shift, per-day table of required against assigned staff. The guide says not
  to trust the colours of the grid alone. [customer]
  ([ELFT Roster Approval SOP](https://www.elft.nhs.uk/sites/default/files/2026-06/Roster%20Approval%20SOP.pdf))
- **Warnings and approval in the grid.** Leave requests not yet approved carry a question-mark badge on the cell. Right-
  clicking a cell offers Edit, **Retest Rules**, Approve and Reject. Approval is refused while any leave request is
  undecided. A purple bar across the roster carries its state, such as "Partially Approved". Right-clicking that bar
  offers Edit Roster, **Auto Roster**, **Roster Analyser** and **Retest Rules on Roster**. [customer] (same SOP)
- **Roster Analyser.** It opens as a full panel with six headline areas, each marked as fine or failing: Budget,
  Unavailability, Safety, Effectiveness, Annual Leave and Fairness. Each area expands into figures. Safety covers roster
  unfilled %, missing charge cover, missing skills and skill mix. Effectiveness covers over- and under-contracted hours,
  additional duty hours and wrong grade type. Fairness covers requested duties %, duties with warnings % and duties below
  minimum length. The panel's backdrop changes with the score, from clear sky to storm. The panel carries the **Partially
  Approve** and **Fully Approve** actions. [customer] (same SOP)
- **Deadlines.** The trust publishes a dated schedule for each period: roster built from the template about 13 weeks
  ahead, staff requests closed about 9 weeks ahead, draft and partial approval about 8 weeks ahead, full approval and
  publish 6 weeks ahead, unfilled shifts sent to the bank 5 weeks ahead. The system notifies approvers and shows these
  dates on the unit summary. [customer] (same SOP)
- **Reports.** A Roster Stats report lists, for each roster: % changed since approval, % manually rostered, % auto
  rostered, % requested, % duties with warnings and % unfilled. [customer] (same SOP)
- **Vendor claims.** Auto-rostering, built-in "trigger warnings", skill-matched compliant staffing, rest between shifts,
  and self-rostering within limits. [product]
  ([RLDatix rostering, APAC](https://www.rldatix.com/en-apac/solution/workforce/rostering/))
- **SafeCare.** This separate module compares the staff on duty against patient acuity, using recognised nursing acuity
  tools, and lets clinicians raise **red flags** that are tracked to closure. It is a day-of-care staffing tool that sits
  beside the roster. [snippet]
  ([RLDatix SafeCare](https://www.rldatix.com/en-uki/solution/safecare/))
- **Snippet-only cue.** An NSW Health page reportedly says a predefined warning turns the shift orange, with the detail on
  hover. The page refused automated reads, so this is unconfirmed. [snippet]
  ([NSW Health: create and check a roster](https://www.health.nsw.gov.au/Rostering/Pages/create-a-roster.aspx))

### Deputy

- **Scope of actions.** Actions on the schedule (publish, bulk changes) apply only to the shifts in the current view.
  [snippet]
  ([Deputy: Schedule overview](https://help.deputy.com/hc/en-au/articles/4688713423759))
- **Auto-Schedule** builds the empty shift structure and fills it with staff in one action. [snippet] (same article)
- **Publish.** A Publish button in the top right shows how many shifts in the view are still unpublished, and changes
  colour once everything is published. A manager can publish with or without notifying staff. Only filled or open shifts
  can be published. [snippet] (same article)
- **Stress profiles.** These are per-person limits: hours per shift, day and week, days per week, and minimum rest
  overnight. Breaking one shows a warning that the manager can override ("Schedule anyway"). An optional extension
  notifies managers when a profile is triggered. [snippet]
  ([Deputy: stress-profile notification](https://help.deputy.com/hc/en-au/articles/4692800411023-Can-managers-be-notified-when-a-schedule-has-triggered-a-stress-profile-for-a-team-member))
- **Insights and stats.** An Insights menu and a stats panel give scheduled hours, required staff and wage cost per
  period. Open and empty shifts are left out of the totals by default. [snippet]
  ([Deputy help 4764591177871](https://help.deputy.com/hc/en-au/articles/4764591177871),
  [Deputy help 4798884080271](https://help.deputy.com/hc/en-au/articles/4798884080271))
- **Healthcare pitch.** A monthly roster view that shows gaps weeks ahead; reusable patterns; credential expiry rules that
  stop unqualified rostering; flags for too little rest and approaching overtime; AI fill suggestions offered as drafts
  for the manager to approve; publishing to staff phones. [product]
  ([Deputy: healthcare team scheduling](https://www.deputy.com/au/blog/mastering-healthcare-team-scheduling-with-deputy))

### When I Work

- **OpenShifts row.** In the users view, the top row of the scheduler holds OpenShifts. It can be collapsed on busy days.
  Shifts that need pickup approval carry an icon. [help]
  ([When I Work: How OpenShifts work](https://help.wheniwork.com/articles/how-openshifts-work/))
- **Conflicts.** Creating or editing a shift shows a "Scheduling Concerns" banner on it. A concern can be an overlapping
  shift, a broken scheduling rule, approved time off, or an unavailability preference. The assign list can be narrowed to
  qualified users. [help]
  ([When I Work: Identifying scheduling conflicts](https://help.wheniwork.com/articles/identifying-scheduling-conflicts))
- **Auto-Assign** fills unpublished OpenShifts, in week view only. Its options respect max hours, unavailability,
  preferred times and weekends-first. The results appear in the grid unsaved, with **Save Shifts** or **Revert**; the
  manager then edits and **Publishes & Notifies**. [help]
  ([When I Work: Auto-assign shifts](https://help.wheniwork.com/articles/auto-assign-shifts))
- Not found: any on-grid marker for conflicts beyond the edit banner.

### Rotageek

- **Layout.** The calendar of shifts and leave sits in the centre. The left panel lists employees with their contracted
  and maximum hours, coloured green (on contract), amber (above contract, under max) or red (under contract or over max).
  The top bar shows per-day people scheduled, people on leave or sick, hours against target, and cost. A Tools menu
  offers copy week, print, **undo an Autoscheduler run**, send gap shifts and bulk delete. A bottom panel compares hours
  and cost against budget. [help]
  ([Rotageek: Scheduler overview](https://support.rotageek.com/en/articles/9657242-scheduler-overview-filters-and-tools))
- **Demand graph.** This pinnable graph above the grid stacks the staff scheduled per task, in 15-minute steps, under a
  purple target line, with optional minimum and maximum lines. [help]
  ([Rotageek: The demand graph](https://support.rotageek.com/en/articles/9663263-the-demand-graph))
- **Gap shifts.** These sit in a gaps row at the top. A manager fills one directly (staff with a clash, leave or missing
  skill are greyed out), uses "preview" to highlight who could take it, or sends it to the app for staff to request. A
  filled gap is a draft until the manager approves it. [help]
  ([Rotageek: Gap shifts](https://support.rotageek.com/en/articles/9662682-gap-shifts))
- **Warnings** appear in four places: a count badge by the employee's name, an icon on the shift tile, the shift's edit
  dialog, and a Warnings button with a total count. The button opens a dialog grouped by employee, with search and a
  filter by type of breach. Each warning shows the scheduled value, the threshold, the difference and the dates.
  Selecting a warning **highlights and scrolls to** its shifts, and the dialog refreshes as the manager edits. [help]
  ([Rotageek: Scheduler warnings](https://support.rotageek.com/en/articles/14499274-scheduler-warnings))
- **Schedule Analysis.** A sidebar lists each configured rule with the current schedule's result for it, and shows
  whether a change made it better or worse. The sources conflict on whether it re-runs by itself:
  [article 9849736](https://support.rotageek.com/en/articles/9849736-automatic-schedule-analysis) says to re-trigger it
  by hand, while a search summary of
  [article 4131301](https://support.rotageek.com/en/articles/4131301-automatic-schedule-analysis) says it re-runs on every
  change. [help]/[snippet]
- **Autoscheduler.** It builds and staffs the shifts in one pass and gives a fit-to-demand score. It flags unfilled cover
  and rule breaks before publishing. Hard rules come before preferences and fairness, and fairness covers things like a
  fair share of weekends. [product]
  ([Rotageek: Autoscheduling](https://www.rotageek.com/autoscheduling))

### Quinyx

- **Schedule view.** Day, week, month and custom periods, switched in the top left. The top right shows whether the period
  is published. Each employee row shows a metric such as scheduled against nominal hours, as a percentage. Sorting
  includes "metric difference". **Unassigned shifts** sit in a collapsible row at the top; selecting one opens a side
  panel for assigning it, with unavailable staff greyed out. Bank holidays are marked in red. A period can be locked.
  [help]
  ([Quinyx: Schedule view](https://quinyx.helpdocs.io/l/en/article/w5u1t14jgu-schedule-view))
- **Warnings and hard stops.** A warning is an overridable dialog. A hard stop blocks the action outright. The dialog
  lists one entry per rule, and an admin can make a rule silent. [help]
  ([Quinyx: Schedule warnings](https://quinyx.helpdocs.io/l/en/article/6jiwwx92en-schedule-warnings))
- **Schedule violations panel.** It sits beside the schedule and lists breaches of law, agreements and policy, such as
  overtime and rest, to resolve inline. The same release added saved, shareable screen setups (filters, display options,
  statistics). [help]
  ([Quinyx Unlocked, April 2026](https://quinyx.helpdocs.io/l/en/article/ngbgmvn6cx-quinyx-unlocked-april-2026))
- **Auto Schedule / Auto Assign runs** are listed in a run table. Selecting a run opens a side panel of its warnings, each
  with a suggested fix. [help]
  ([Quinyx: Auto Schedule and Auto Assign warnings](https://quinyx.helpdocs.io/l/en/article/bqc22wbgct-auto-schedule-and-auto-assign-warnings))
- Not retrieved: how warnings mark the shift tiles, and the statistics rows.

## 2. The staff phone experience

### RLDatix Loop (HealthRoster's staff app; it replaced Employee Online)

- **Features.** The app shows the personal roster as a calendar and who is working with you. Staff can make requests,
  including duties requested ahead of the roster; check their leave entitlement, with an optional view of how many
  colleagues are off that week; swap shifts; show their availability; and book bank shifts, which only eligible staff
  see. The personal roster exports as an iCalendar link. [product]
  ([RLDatix: Loop FAQ](https://uk.rldatix.com/allocate-loop-frequently-asked-questions/))
- **One board's setup.** Staff view personal and team rosters, request annual and study leave (but cannot cancel it in
  the app), manage bank shifts (request, cancel, express interest), and use messaging and push notifications. A web
  version serves staff without a phone. [customer]
  ([NHS Lanarkshire: Loop guidance](https://www.lanarkshirehr.nhs.scot/wp-content/uploads/Loop-Guidance.pdf))

### Deputy

- **Swaps and offers.** Staff can swap a shift or offer it to qualified colleagues. Swaps are turned on per location, and
  manager approval is optional. Until a swap is claimed and approved, the shift is still the staff member's to work.
- **Open shifts** are found under Home, then Available Shifts. On the web they go to whoever claims first, or with
  approval on, the manager picks from those who asked.
- The app's schedule can show more than the website's Schedule page. [snippet]
  ([Deputy help 4614775254671](https://help.deputy.com/hc/en-au/articles/4614775254671),
  [Deputy: Employee guide to the mobile app](https://help.deputy.com/hc/en-au/articles/4753097048719))
- Healthcare pitch: availability and leave requests from the phone, open shifts accepted with a tap, and shifts synced to
  a personal calendar. [product] (Deputy healthcare blog above)

### When I Work

- **Schedule tab.** It opens on **My Shifts**: one week, with a strip of days to tap and swipe and a full month calendar
  pulled down from it, plus daily and total hours and a "confirm this week" action. **All Shifts** is the whole team's
  schedule; the employer can hide it or limit it to the staff member's positions. **OpenShifts** are the shifts the staff
  member qualifies for. [help]
  ([When I Work: Schedule views for employees](https://help.wheniwork.com/articles/schedule-views-for-employees-iphone-ipad))
- **Requests.** Swaps and drops start from the shift itself. The swap list shows only shifts both people are qualified
  and available for. A Requests view tracks the staff member's own requests, and a "Shift Requests" card on the
  dashboard counts the colleagues' requests waiting for an answer. [snippet]
  ([When I Work: Get your shifts covered](https://help.wheniwork.com/articles/get-your-shifts-covered-ios),
  [Accepting swap and drop requests](https://help.wheniwork.com/articles/accept-shift-swap-and-drop-requests-ios))
- **Picking up OpenShifts.** The first eligible person to accept gets the shift, unless the manager has turned on
  approval. [help] (How OpenShifts work, above)

### Rotageek

- **Swaps and drops.** Both start from the shift. Eligibility for a swap: both shifts in the future, in the same week and
  location, and each person able to work the other's with the right skills. The shift's status reads "swap requested",
  then "awaiting approval" or "not approved". Incoming swaps appear under **Available Shifts** with a two-arrow mark. A
  dropped shift goes to colleagues with the right skills. [help]
  ([Rotageek: Mobile app swaps and drops](https://support.rotageek.com/en/articles/9790791-mobile-app-swaps-and-drops))
- **Gap shifts** sent from the scheduler appear in the app for staff to request; the manager assigns them. [help]
  (Gap shifts, above)
- The app shows leave balances, requests and their outcomes, and pending requests can be edited or cancelled. [snippet]

### Quinyx

- **Colour-coded schedule.** Shifts on the schedule are coloured by state: the staff member's own shifts; leave applied
  for or approved; shifts colleagues want to give away; unmanned shifts available to book; and shifts the staff member
  has shown interest in. [help]
  ([Quinyx: Mobile quick start](https://quinyx.helpdocs.io/l/en/article/59hs7s933j-mobile-quick-start))
- **Swaps.** Staff pick their shift, then the date and the colleague, and submit. The app marks a "good match" when
  start and end times, shift type, hours or skills line up. Both sides are notified of the decision, and a request can be
  withdrawn. [help]
  ([Quinyx: Mobile swapping shifts](https://quinyx.helpdocs.io/l/en/article/t02l97c06v-mobile-swapping-shifts))
- **Mobile only.** Some actions, such as swapping a shift, are in the phone app and not the web staff portal. [snippet]
  ([Quinyx: Mobile app and Staff portal functionality](https://quinyx.helpdocs.io/l/en/article/4mo98d9o5y-mobile-app-and-staff-portal-functionality))

## 3. What it means for this app

The app today (read from `frontend/src/routes/_signed-in/roster.tsx`, `my-roster.tsx`, `my-month.tsx`, `index.tsx` and
`frontend/src/roles.ts`):

- **Unit Manager.** The Roster page is one column of cards: month navigation, solve progress or the last solve, roster
  status, then the Draft card. The Draft card holds the grid, the legend, the cell editor, pending edits,
  `Violations` (a list under the grid), submit, versions and the solve details. The re-plan, preview/solve and month
  close cards follow. The grid marks pins and unsaved edits on cells, but not violations. No per-shift-day totals sit
  with the grid.
- **Staff Nurse.** Home is a few sentences of text with links. My roster is the whole unit's grid for the month, with the
  nurse's own row first. My month gathers hours, requests, preferences, skills and leave as cards. Swaps and Covers are
  separate pages.

The implications below are options for the spec, not features to build. They use the app's own words (Draft, Roster in
force, Version, Pin, Violation, Shift-day, Cover). Words like "publish" and "open shift" belong to the vendors.

### Unit Manager (desktop roster workspace)

1. **One workspace instead of a column of cards.** Every product keeps the grid in the middle, the controls in a toolbar
   (solve or re-plan, save, submit; the products' "publish") and the details in side panels. Here that means a toolbar
   with Solve / Re-plan / Save edits / Submit and the month's state, and a panel for the solve's progress and details,
   so the grid stays on screen while a solve runs. Versions and month close can move to secondary panels or tabs.
2. **Violations on the cells and in a panel that jumps to them.** Rotageek's pattern is the most complete: a mark on the
   cell, a count per nurse row, and a list grouped by nurse that refreshes and scrolls to the cells. The app's
   Violations already name a nurse, a date or a shift-day, so each could address a cell, a row or a column. The mark
   needs a text alternative and a cue that is not colour alone, and the jump must move focus inside the roving-focus
   grid.
3. **Per-shift-day totals pinned to the grid.** HealthRoster's unfilled-duties band and Assignment Count, Rotageek's
   demand graph, and Deputy's and Quinyx's per-day stats all put "on duty against needed" beside the days. For a nurse
   roster that means, per shift and date, headcount against the rule, plus a mark for a missing team leader or a
   missing skill. This is HealthRoster's healthcare-specific move: skill mix and charge cover, not headcount alone. The
   API may need a per-shift-day summary if the draft does not already carry one.
4. **An analyser before Submit.** HealthRoster gates approval on a scored summary: safety, hours against contract,
   requests granted, fairness, leave. The app has the parts (violations, cost, pin notes, preferences, hours) spread
   over the page. A "ready to submit" summary that shows them together would match how the CNO and Deputy CNO approve,
   which is the nearest analogue to HealthRoster's partial and full approval.
5. **Solve as a reviewable draft.** Every product treats auto-scheduling as a draft with undo or revert. The app already
   does this (a solve replaces the Draft, and Pins hold). The design should keep that clear on screen: what the solve
   changed, and that Pins survive.
6. **Uncovered work as a row.** Vendors put open or gap shifts in a top row. The app has no such thing, since the solver
   fills the grid and shortfalls show as Violations. The nearest are a Shift-day short of nurses and the Cover the manager
   offers. A top band of short Shift-days, each able to start a Cover, would carry the pattern over without inventing
   open shifts.
7. **Constraints.** A desktop-first workspace still has to hold at 320 px. The products' phone apps drop the full grid for
   managers, so on a narrow screen the workspace could fall back to a day or nurse view rather than a squeezed grid. The
   print layout stays a separate route.

### Staff Nurse (phone)

1. **"My next shifts" as the landing screen.** Every app opens on the staff member's own shifts: a week strip with a month
   on demand (When I Work), or a calendar (Loop). The app's Home is text, and My roster opens on the whole unit's grid,
   which at 320 px is a wide table. A phone-first My roster could lead with the nurse's own shifts as a list or week
   strip, with the unit grid one tap away (Loop's "who you work with", When I Work's All Shifts).
2. **One place for what needs the nurse.** Every product gathers requests and answers in an inbox or dashboard card: a
   Cover offered (first to accept takes it, as in the "open shift without approval" pattern), a swap waiting for this
   nurse's answer, a swap's decision, and a request window that is open. The app has the notification bell plus separate
   Swaps, Covers and My month pages. The spec could make Home that inbox.
3. **Visible states for swaps.** Rotageek and Quinyx show each stage: requested, awaiting the colleague, awaiting the
   manager, approved or not. The app's swap already runs these stages: colleagues accept, then the manager decides. The
   phone view should show the stage on the nurse's own cell and in the inbox. It should also say, as every vendor does,
   that the nurse works the shift until the swap is approved.
4. **Start actions from the shift.** Every app starts swap, drop and offer from the shift itself. The app starts swaps
   from a form on the Swaps page. Tapping one of the nurse's own cells could start a swap there.
5. **Healthcare request windows.** HealthRoster runs a published timetable (requests close weeks before the roster), and
   Loop takes duty requests before the roster exists. The app's requests and preferences already have windows (My month
   says when they open and close). On the phone, the window's state and its close date belong on Home, not deep in My
   month.
6. **Calendar export** (Loop's iCalendar link, Deputy's calendar sync) recurs. It would be a new API surface, so it is
   only an option.

## Gaps and caveats

- No product's help centre showed the phone apps' tab bars in full, so what the phone app opens on comes only from When
  I Work's article.
- HealthRoster's workspace comes from one trust's guide. Grid colours, the right-click menus and the analyser's backdrop
  are that trust's version of Optima.
- Deputy's help centre refused automated reads, so its claims rest on search summaries and product pages.
- Quinyx's warning marks on shift tiles, and its statistics rows, were not retrieved.
- Rotageek's two Schedule Analysis articles disagree on whether the analysis re-runs by itself.
