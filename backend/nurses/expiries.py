"""The expiries page: each skill of a serving nurse that has expired or expires within 90 days of the hospital's
today, and each skill required to work she has no record of, so that renewals are chased before the roster is touched. It is
worked out when read: no job keeps it and nothing is sent about it, since the page is the reminder.

"Expired" and "required" mean what a solve input means by them: a retired skill is no record, so it is no row of any
kind (`nurses.skills.tracked`); a kind whose `expires` is off has no expiry, whatever date is kept
(`nurses.skills.counted_expiry`); the required skills are the catalogue's live ones (`hospital.rules.required_skills`); and an
expiry is the last day a skill counts, so it has expired from the next day, as the solver bars it (`solves.input.barred_from`,
which reads it per month, so it cannot be called here)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from hospital.models import Skill, SkillKind
from hospital.rules import required_skills
from nurses import membership
from nurses.models import NurseProfile
from nurses.skills import counted_expiry, current_skills, tracked
from units.models import Unit

HORIZON = dt.timedelta(days=90)


class ExpiryState(StrEnum):
    EXPIRED = "expired"
    EXPIRING = "expiring"
    NO_RECORD = "no_record"


@dataclass(frozen=True)
class Expiry:
    nurse: NurseProfile
    unit: Unit
    skill: str
    kind: SkillKind
    state: ExpiryState
    expiry: dt.date | None


def expiries(nurses, unit_of) -> list[Expiry]:
    """`nurses`: the profiles the reader may see; `unit_of(nurse)`: the unit the row names for her. Those not serving are left
    out. The oldest date first, then each skill she has no record of, each by her name and the skill's."""
    today = membership.today()
    required = required_skills()
    kinds = dict(Skill.objects.filter(name__in=required).values_list("name", "kind"))
    found: list[Expiry] = []
    for nurse in membership.serving(nurses).select_related("user").prefetch_related("placements__unit", current_skills()):
        held = tracked(nurse.current_skills)
        for row in held:
            expiry = counted_expiry(row)
            if expiry is not None and expiry <= today + HORIZON:
                state = ExpiryState.EXPIRED if expiry < today else ExpiryState.EXPIRING
                found.append(Expiry(nurse, unit_of(nurse), row.skill.name, row.skill.kind, state, expiry))
        names = {row.skill.name for row in held}
        found += [Expiry(nurse, unit_of(nurse), name, kinds[name], ExpiryState.NO_RECORD, None)
                  for name in required if name not in names]
    return sorted(found, key=lambda row: (row.expiry is None, row.expiry or today, row.nurse.user.full_name.lower(),
                                          row.skill.lower(), row.nurse.pk))
