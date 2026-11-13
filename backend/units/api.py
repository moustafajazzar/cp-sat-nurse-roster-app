"""The CNO's units and the assigning of their managers. Who reaches which unit is `units.scope`."""
from __future__ import annotations

from uuid import UUID

from django.db import transaction
from ninja import Router, Schema, Status
from pydantic import ConfigDict

from accounts.authz import SignedIn
from accounts.models import Role, User
from audit.log import record
from hospital.api import differing, refuse, save_unless_taken
from hospital.catalogue_api import Name
from hospital.models import HospitalSettings
from nurses.membership import placed_outside
from units.models import Unit
from units.rules import seeded_rules
from units.scope import unit_in_scope, units_in_scope

router = Router(tags=["units"])
cno = SignedIn(Role.CNO)


class UnitBody(Schema):
    # Not StrictSchema, whose strict mode would refuse every id, because JSON has only strings.
    model_config = ConfigDict(extra="forbid")
    name: Name
    manager_id: UUID | None


class UnitManagerSchema(Schema):
    id: UUID
    username: str
    full_name: str


class UnitSchema(Schema):
    id: UUID
    name: str
    archived: bool
    manager: UnitManagerSchema | None
    rules_need_review: bool


class ManagerChoice(UnitManagerSchema):
    unit: str | None

    @staticmethod
    def resolve_unit(user: User) -> str | None:
        return user.managed_unit_name


def _manager_to_assign(manager_id: UUID | None, unit_id: UUID | None) -> User | None:
    """Locks the account first, as deactivating it does, so it cannot be deactivated or given another unit
    between this check and the save. The one-to-one field is the database's word on the same thing."""
    if manager_id is None:
        return None
    manager = User.objects.select_for_update(no_key=True).filter(pk=manager_id, role=Role.UNIT_MANAGER, is_active=True).first()
    if manager is None:
        refuse((("manager_id",), "This is not an active unit manager."))
    held = Unit.objects.filter(manager=manager).exclude(pk=unit_id).first()
    if held is not None:
        refuse((("manager_id",), f"{manager.username} already manages {held.name}. A manager holds one unit: "
                                 f"give {held.name} another manager, or none, first."))
    elsewhere = placed_outside(manager, unit_id)
    if elsewhere is not None:
        how = ("Transfer it here from this month first." if unit_id else
               "Add the unit with no manager, transfer the profile there from this month, then assign them.")
        refuse((("manager_id",), f"{manager.username}'s nurse profile is in {elsewhere.name}, and a manager's must be "
                                 f"in the unit they hold. {how}"))
    return manager


def _username(user: User | None) -> str | None:
    return user.username if user else None


def _audited(name: str, manager: User | None) -> dict:
    return {"name": name, "manager": _username(manager)}


def _name_taken_message(name: str) -> str:
    return f"{name} is already a unit."


@router.get("", response=list[UnitSchema], auth=SignedIn(Role.CNO, Role.DEPUTY_CNO, Role.UNIT_MANAGER),
            operation_id="listUnits")
def list_units(request) -> list[Unit]:
    """A manager's list is the unit he holds, or empty. Archived units too, for those who see them all."""
    return list(units_in_scope(request.user).select_related("manager"))


@router.post("", response={201: UnitSchema}, auth=cno, operation_id="createUnit")
def create_unit(request, body: UnitBody) -> Status:
    with transaction.atomic():
        HospitalSettings.load(for_update=True)  # The catalogue's lock: see `seeded_rules`.
        manager = _manager_to_assign(body.manager_id, None)
        unit = Unit(name=body.name, manager=manager, rules=seeded_rules())
        save_unless_taken(unit, "name", _name_taken_message(body.name))
        record("unit.created", actor=request.user, target=unit.name, request=request, after=_audited(unit.name, manager))
    return Status(201, unit)


@router.get("/managers", response=list[ManagerChoice], auth=cno, operation_id="listUnitManagers")
def list_unit_managers(request) -> list[User]:
    """Who the CNO can assign: the active unit managers, with the unit each one holds."""
    return list(User.objects.filter(role=Role.UNIT_MANAGER, is_active=True)
                .select_related("managed_unit").order_by("username"))


@router.get("/{unit_id}", response=UnitSchema, auth=SignedIn(Role.CNO, Role.DEPUTY_CNO, Role.UNIT_MANAGER),
            operation_id="getUnit")
def get_unit(request, unit_id: UUID) -> Unit:
    return unit_in_scope(request.user, unit_id)


@router.put("/{unit_id}", response=UnitSchema, auth=cno, operation_id="updateUnit")
def update_unit(request, unit_id: UUID, body: UnitBody) -> Unit:
    """Renames the unit, assigns its manager, reassigns it or takes the manager off it. Taking off is how two
    managers change places, since neither can be given a unit while holding one."""
    with transaction.atomic():
        manager = _manager_to_assign(body.manager_id, unit_id)
        unit = unit_in_scope(request.user, unit_id, for_update=True)
        old, new = differing(_audited(unit.name, unit.manager), _audited(body.name, manager))
        if not new:
            return unit
        if unit.archived:
            refuse((("name",), f"{unit.name} is archived and cannot be changed."))
        unit.name, unit.manager = body.name, manager
        save_unless_taken(unit, "name", _name_taken_message(body.name))
        record("unit.changed", actor=request.user, target=unit.name, request=request, before=old, after=new)
    return unit


@router.post("/{unit_id}/archive", response=UnitSchema, auth=cno, operation_id="archiveUnit")
def archive_unit(request, unit_id: UUID) -> Unit:
    """The unit lets its manager go, who can then hold another. There is no way back: a hospital that reopens a
    unit creates one under the name, which is free again, and the old one stays on record."""
    with transaction.atomic():
        unit = unit_in_scope(request.user, unit_id, for_update=True)
        if unit.archived:
            return unit
        nurses = unit.nurse_count()
        if nurses:
            refuse((("archived",), f"{nurses} nurse{' belongs' if nurses == 1 else 's belong'} to {unit.name}. "
                                   "Transfer or deactivate them first."))
        manager = _username(unit.manager)
        unit.archived, unit.manager = True, None
        unit.save(update_fields=["archived", "manager"])
        record("unit.archived", actor=request.user, target=unit.name, request=request,
               before={"archived": False, "manager": manager}, after={"archived": True, "manager": None})
    return unit
