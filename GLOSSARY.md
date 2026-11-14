# Nurse rostering

The language of a hospital's monthly nurse rosters: how a unit's month is planned, approved, and changed afterwards.

## Language

### A unit's month

**Draft**:
A unit's roster for a month before it is approved. It can be replaced by a solve or edited freely, and no nurse is told of it.
_Avoid_: Proposal, working copy

**Roster in force**:
A unit's approved roster for a month as it stands now: what each nurse is expected to work. It is always the latest version.
_Avoid_: Published roster, live roster, current roster, final roster

**Month over**:
A month earlier than the hospital's current month. Its roster in force is fact and can no longer change.
_Avoid_: Closed month (a month close is a separate event), past month

### Changing the roster in force

**Change**:
Anything that alters at least one cell of the roster in force, whatever its origin. Each change makes exactly one version; something that alters no cell is not a change.
_Avoid_: Edit (one origin only), update, modification

**Version**:
One numbered state of the roster in force. Approval makes the first; every later change that alters at least one cell makes the next, in force at once.
_Avoid_: Revision, snapshot

**Origin**:
What caused a version: the approved solve, a manager's edit, leave, a re-plan, a swap, a request or a cover. Leave is an origin in its own right, though the version list shows its versions as edits.
_Avoid_: Source, kind, type

**Edit**:
The manager's direct change to cells, with a reason. One origin among several, never a word for changes in general.
_Avoid_: Manual change, override

**Pin**:
A cell fixed to a shift or to off, which a solve or re-plan must keep. A pin belongs to whoever caused it (the manager, a request, a swap or a cover) and exists whether or not the month has a roster yet.
_Avoid_: Lock, fixed cell

**Set aside**:
Said of a pin that counts for nothing because its cell could not hold it now: its nurse has left the month or the date is outside her dates, or the pin is to a shift she may no longer work or is barred from that day. The pin is not ended, and counts again if the facts come back.
_Avoid_: Ignored, invalid, suspended

**Bar**:
What keeps a nurse off every shift from a date on: a skill required to work whose last counted day was the day before. A day off is still hers on a barred day.
_Avoid_: Block, restriction, expiry (the last day a skill counts, which a bar follows)

### What a roster breaks

**Violation**:
A hard rule a roster breaks, at a nurse, a date or a shift-day. It is the same thing whoever reads it; a swap's problem and a cover's lack are that origin's wording of one.
_Avoid_: Finding, lack, problem, error

**Shift-day**:
One shift on one date in a unit: what a rule about a shift's staffing on a day is about, such as its headcount or its team leader.
_Avoid_: Slot, shift instance
