"""What a manager reads for each reason a cell cannot hold a change (`rosters.cells`), written once so that an edit, a
request's decision and a swap never give two accounts of one fact. The manager reads every reason in full, as they read the
leave and the attributes it rests on. What a nurse reads is each origin's to say, in her own words."""
from __future__ import annotations

import datetime as dt

from rosters.cells import Reason
from rosters.pins import long_date


def bar_said(reason: Reason, whose: str) -> str:
    """"Amal's BLS, required to work, counted until 20 June 2026", with `whose` "Amal's" or "Your": how every sentence that
    says a bar in full begins, a nurse's own too."""
    return f"{whose} {reason.skill}, required to work, counted until {long_date(reason.last)}"


def _held_said(reason: Reason, name: str, day: dt.date, asking: str) -> str:
    """A pin that does not yield, said from where the manager stands: on the editor a pin is what they look at, on a request
    the cell is its own, and on a swap it is one nurse's among several. A cover's refusal is read by its nurse alone."""
    match asking:
        case "edit":
            return f"This pin comes from an accepted {reason.origin}, on {long_date(day)}, so change it there."
        case "request":
            return f"An accepted {reason.origin} holds this cell on {long_date(day)}, so change it there."
        case "swap":
            return f"An accepted {reason.origin} holds {name}'s cell on {long_date(day)}, so change it there."
    raise ValueError(f"No manager reads why a {asking} cannot change a pinned cell.")


def manager_said(reason: Reason, name: str, day: dt.date, month: dt.date, asking: str) -> str:
    """Why `name`'s cell on `day` of the unit's `month` cannot hold what the origin `asking` asked, in full."""
    match reason.why:
        case "not_in_month":
            return f"{name} is not on this unit's roster for {month:%B %Y}."
        case "outside_dates":
            return f"{long_date(day)} is outside {name}'s dates, so there is nothing to pin."
        case "leave":
            return f"{name} is on leave on {long_date(day)}, and leave wins over a pin."
        case "shift_not_worked":
            return f"{reason.shift} is not one of this unit's shifts."
        case "barred":
            return f"{bar_said(reason, name + "'s")}, so {name} can work no shift on {long_date(day)}."
        case "shift_not_allowed":
            return f"{reason.shift} is not one of {name}'s allowed shifts."
        case "night_on_probation":
            return (f"{name} is on probation in {month:%B %Y}, and a nurse on probation works no {reason.shift}, the night "
                    "shift.")
        case "held":
            return _held_said(reason, name, day, asking)
    raise ValueError(f"A reason with no words: {reason.why}.")
