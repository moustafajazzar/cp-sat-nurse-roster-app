"""Each step a cover takes once open: offered or added to by the unit's manager, accepted or declined by a nurse offered it,
withdrawn by the manager. Every write about a cover stores an expiry its date makes first, in a transaction of its own so a
refusal keeps it (`covers.expiring.expire_passed`, called by the endpoints). Each refusal, then the write, the audit entry
and whoever is told; call each inside a transaction.

An acceptance changes the roster in force, so it holds the unit's month as every such change does (`rosters.in_force.hold`)
and then locks the cover's row, and no leave write or version passes between its check and its write; its version goes in force
through `rosters.in_force.change`, which tells the unit's nurses and writes the roster-change entry. An offer, a decline and a
withdrawal change nothing on the roster and take the cover's row lock alone, which nothing holds while waiting on another lock,
so none of them waits in a circle. The rows they write refer to nurses and users, whose locks are all `FOR NO KEY UPDATE`, so the
checks of those foreign keys at commit wait on nothing.

The audit log says a cover moved and by whom: `cover.offered` and `cover.withdrawn` (the manager, the unit as target),
`cover.accepted` and `cover.declined` (the nurse, her hospital ID as target), never its date, shift or note."""
from __future__ import annotations

from uuid import UUID

from django.http import Http404, HttpRequest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import User
from audit.log import record
from hospital.api import refuse
from notifications import sending
from notifications.sending import CoverStep
from nurses.models import NurseProfile
from rosters import in_force
from rosters.approved import NewCell
from rosters.models import PinOrigin
from rosters.pins import long_date, pin_cell
from rosters.review import name_of
from units.models import Unit

from covers.expiring import state_now
from covers.judging import CoverMonth, judge
from covers.models import WAITING, Answer, Cover, CoverOffer, CoverState

NOT_OFFERED = "You have been offered no such cover."
NOT_IN_UNIT = "There is no such cover in your unit."
NO_CELL_CHANGED = "Your cell already holds this cover's shift, so there is nothing to take."

# How a cover no longer waiting reads to the manager, and to a nurse offered it: never why it was short or who took it.
MANAGERS_WORDS = {
    CoverState.TAKEN: "This cover has been taken",
    CoverState.WITHDRAWN: "This cover has been withdrawn",
    CoverState.FILLED: "A new version of the roster has filled this cover",
    CoverState.EXPIRED: "This cover's date has passed",
}
WHY_NOT_WITHDRAW = {
    CoverState.TAKEN: "This cover has been taken, so it cannot be withdrawn. Edit the roster to change who works the shift.",
    CoverState.WITHDRAWN: "This cover has already been withdrawn.",
    CoverState.FILLED: "A new version of the roster has filled this cover, so there is nothing to withdraw.",
    CoverState.EXPIRED: "This cover's date has passed, so there is nothing to withdraw.",
}
NURSES_WORDS = {
    CoverState.TAKEN: "Another nurse has taken this cover",
    CoverState.WITHDRAWN: "Your manager has withdrawn this cover",
    CoverState.FILLED: "This cover is no longer needed",
    CoverState.EXPIRED: "This cover's date has passed",
}


def which(cover: Cover) -> str:
    """"Day (D) on 25 June 2026": the shift by its name now."""
    return f"{cover.shift.display_name} ({cover.shift.code}) on {long_date(cover.date)}"


def _locked(condition: dict, missing: str) -> Cover:
    cover = (Cover.objects.select_for_update(of=("self",)).select_related("unit__manager", "roster__unit", "shift")
             .filter(**condition).first())
    if cover is None:
        raise Http404(missing)
    return cover


def offer(by: User, unit: Unit, cover_id: UUID, hospital_ids: list[str], note: str, request: HttpRequest) -> Cover:
    """Each nurse named must be in the list as judged now, or the offer is refused naming her. On an open cover it becomes
    offered with the note; on an offered one, those not yet in it are added (a nurse who declined is in it, so not asked again)
    and the note stays as first written. Only those added are told."""
    cover = _locked({"pk": cover_id, "unit": unit}, NOT_IN_UNIT)
    if not cover.waiting:
        raise HttpError(409, f"{MANAGERS_WORDS[cover.state]}, so it cannot be offered.")
    if not hospital_ids:
        refuse((("nurses",), "Tick at least one nurse to offer it to."))
    already = set(cover.offers.values_list("nurse__user__username", flat=True))
    added = [hospital_id for hospital_id in dict.fromkeys(hospital_ids) if hospital_id not in already]
    if not added:
        refuse((("nurses",), "Everyone ticked has been offered it already."))
    judged = judge(cover.roster, cover.date, cover.shift)
    if judged.reason or judged.full:
        raise HttpError(409, judged.reason or judged.full)
    could = {nurse.hospital_id for nurse in judged.nurses}
    refused = [hospital_id for hospital_id in added if hospital_id not in could]
    if refused:
        # Only someone on this cover's roster, whose people the manager reads, is named. Any other ID is said back as it was
        # sent, the same for a nurse of another unit as for no such nurse, as a swap's check and a pin edit say it.
        on_roster = {nurse.user.username: nurse.user for nurse in NurseProfile.objects.select_related("user").filter(
            user__username__in=[hospital_id for hospital_id in refused if hospital_id in cover.roster.grid])}
        refuse(*[(("nurses",), f"{name_of(on_roster[hospital_id]) if hospital_id in on_roster else hospital_id} cannot take "
                               "this cover now, so it cannot be offered to them.") for hospital_id in refused])
    nurses = {nurse.user.username: nurse
              for nurse in NurseProfile.objects.select_related("user").filter(user__username__in=added)}
    CoverOffer.objects.bulk_create([CoverOffer(cover=cover, nurse=nurses[hospital_id]) for hospital_id in added])
    if cover.state == CoverState.OPEN:
        cover.state, cover.note = CoverState.OFFERED, note
        cover.save(update_fields=["state", "note"])
    record("cover.offered", actor=by, target=unit.name, request=request)
    sending.cover_step(cover, CoverStep.OFFERED, by, nurses=[nurses[hospital_id].user for hospital_id in added])
    return cover


def withdraw(by: User, unit: Unit, cover_id: UUID, request: HttpRequest) -> Cover:
    """Open or offered. The nurses offered it are told, never why."""
    cover = _locked({"pk": cover_id, "unit": unit}, NOT_IN_UNIT)
    if not cover.waiting:
        raise HttpError(409, WHY_NOT_WITHDRAW[cover.state])
    cover.state, cover.ended_by, cover.ended_at = CoverState.WITHDRAWN, by, timezone.now()
    cover.save(update_fields=["state", "ended_by", "ended_at"])
    record("cover.withdrawn", actor=by, target=unit.name, request=request)
    sending.cover_step(cover, CoverStep.WITHDRAWN, by)
    return cover


def _why_not_answer(cover: Cover, mine: CoverOffer, verb: str) -> str | None:
    if mine.answer == Answer.ACCEPTED:
        return "You have taken this cover already."
    state = state_now(cover)
    if state not in WAITING:
        return f"{NURSES_WORDS[state]}, so {verb}."
    if mine.answer == Answer.DECLINED:
        return "You have declined this cover already."
    return None


def decline(user: User, cover_id: UUID, request: HttpRequest) -> Cover:
    """Under the cover's lock only, which every answer to it takes first. The manager is told; a cover everyone declined stays
    offered with nobody left."""
    cover = _locked({"pk": cover_id, "offers__nurse__user": user}, NOT_OFFERED)
    mine = cover.offers.get(nurse__user=user)
    refusal = _why_not_answer(cover, mine, "there is nothing to decline")
    if refusal:
        raise HttpError(409, refusal)
    mine.answer, mine.answered_at = Answer.DECLINED, timezone.now()
    mine.save(update_fields=["answer", "answered_at"])
    record("cover.declined", actor=user, target=user.username, request=request)
    sending.cover_step(cover, CoverStep.DECLINED, user)
    return cover


def accept(user: User, cover_id: UUID, request: HttpRequest) -> Cover:
    """The first acceptance takes it, with the cover's month held for her (`rosters.in_force.hold`): the date read first, then
    the state, then judged again for her alone on the month as it stands and refused in her own words
    (`CoverMonth.refusal_for`), the cover staying offered. Then her cell a pin of origin `cover`, replacing any pin she held that
    day, in a change of origin `cover` to the roster in force, which tells the unit's nurses; the others offered it are told it
    is taken, and the manager that she accepted. Its version fills no other cover (`covers.expiring`). No scope is checked: the
    offer to her is her scope. A cover's unit and month never change, so they are read before the hold."""
    found = (CoverOffer.objects.filter(cover_id=cover_id, nurse__user=user)
             .values_list("cover__unit_id", "cover__roster__month").first())
    if found is None:
        raise Http404(NOT_OFFERED)
    unit_id, month = found
    held = in_force.hold(unit_id, month, nurses={user.username})
    unit, roster = held.unit, held.roster
    nurse = NurseProfile.objects.select_related("user").get(user=user)
    cover = _locked({"pk": cover_id, "offers__nurse__user": user}, NOT_OFFERED)
    mine = cover.offers.get(nurse__user=user)
    refusal = _why_not_answer(cover, mine, "you can no longer take it")
    if refusal:
        raise HttpError(409, refusal)
    said = CoverMonth(roster, cover.date, cover.shift).refusal_for(user.username)
    if said:
        raise HttpError(409, " ".join(said))
    now = timezone.now()
    mine.answer, mine.answered_at = Answer.ACCEPTED, now
    mine.save(update_fields=["answer", "answered_at"])
    pin_cell(unit, nurse, cover.date, cover.shift, PinOrigin.COVER, user)
    # The cover's own entry first, then the roster-change entry the change writes.
    record("cover.accepted", actor=user, target=user.username, request=request)
    try:
        version = in_force.change(held, [NewCell(user.username, cover.date, cover.shift.code)], origin=in_force.Origin.COVER,
                                  by=user, reason=f"Cover taken by {name_of(user)}: {which(cover)}.", pins=1, request=request)
    except in_force.Refused as refused:
        raise HttpError(409, str(refused)) from None
    if version is in_force.UNCHANGED:
        # Raised inside the transaction, so her answer, her pin and the entry go with it.
        raise HttpError(409, NO_CELL_CHANGED)
    cover.state, cover.taker, cover.made_version, cover.ended_at = CoverState.TAKEN, nurse, version, now
    cover.save(update_fields=["state", "taker", "made_version", "ended_at"])
    sending.cover_step(cover, CoverStep.TAKEN, user)
    sending.cover_step(cover, CoverStep.ACCEPTED, user)
    return cover
