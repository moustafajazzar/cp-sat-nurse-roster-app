---
status: accepted
---

# Pins stay with their origins, outside the roster-in-force module

Every change to the roster in force goes through one module that owns the locks, the version, the audit entry and telling
the unit's nurses. Pins are deliberately not part of it: each origin makes, replaces or ends its own pins before it asks
for the change, and passes only a count for the audit entry.

A pin does not follow a version. A request pins a cell when the month has no roster yet or only a draft, leave ends pins
across units whether or not any roster exists, a manager's edit can clear a pin without altering a cell, and a re-plan
makes no pin at all. Owning pins inside the module would have meant one option per origin on its interface, for behaviour
that is not about the roster in force.

## Considered options

- **Pins inside the module, chosen by origin.** Rejected for the reasons above: the interface would widen with every
  origin's exception, and the pins made without a change would still need a second path.
- **Pins with their origins (chosen).** Which pin yields to which change remains a question of whether a cell can hold a
  change, which is judged before the roster in force is changed.
