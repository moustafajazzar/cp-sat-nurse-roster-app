"""What a manager reads for each reason a cell cannot hold a change (`rosters.cells`), written once so that an edit, a
request's decision and a swap never give two accounts of one fact. The manager reads every reason in full, as they read the
leave and the attributes it rests on. What a nurse reads is each origin's to say, in her own words."""
from __future__ import annotations

import datetime as dt

from rosters.cells import EDIT, REQUEST, SWAP, Asking, Reason
from rosters.pins import long_date


def bar_said(reason: Reason, whose: str) -> str:
    """"Amal's BLS, required to work, counted until 20 June 2026", with `whose` "Amal's" or "Your": how every sentence that
    says a bar in full begins, a nurse's own too."""
    return f"{whose} {reason.skill}, required to work, counted until {long_date(reason.last)}"


def holding(reason: Reason, name: str) -> str:
    """"Amal's approved swap" or "The cover Amal took": the swap's or the cover's pin that holds `name`'s cell, as a sentence
    about a request's decision opens."""
    return {"swap": f"{name}'s approved swap", "cover": f"The cover {name} took"}[reason.origin]


def _held_said(reason: Reason, name: str, day: dt.date, asking: Asking) -> str:
    """A pin that does not yield, said from where the manager stands: on the editor a pin is what they look at, on a request
    the cell is its own, and on a swap it is one nurse's among several. A cover's refusal is read by its nurse alone. A request's
    pin is changed on its own page, where its nurse is told; a swap's or a cover's has no such page, and only the manager's edit
    replaces it, so that is the way through a request's decision."""
    if asking == EDIT:
        return f"This pin comes from an accepted {reason.origin}, on {long_date(day)}, so change it there."
    if asking == REQUEST:
        return (f"{holding(reason, name)} holds this cell on {long_date(day)}. To grant the request, edit the cell first, "
                f"which replaces the {reason.origin}'s pin, then decide the request.")
    if asking == SWAP:
        return f"An accepted {reason.origin} holds {name}'s cell on {long_date(day)}, so change it there."
    raise ValueError(f"No manager reads why a {asking} cannot change a pinned cell.")


def manager_said(reason: Reason, name: str, day: dt.date, month: dt.date, asking: Asking) -> str:
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
