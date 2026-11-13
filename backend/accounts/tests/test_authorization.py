"""Walks every endpoint the API has, so an endpoint added later is held to this too."""
import re
from uuid import uuid4

import pytest
from django.test import Client

from accounts.authz import SignedIn
from accounts.models import Role
from config.api import api
from conftest import PASSWORD

pytestmark = pytest.mark.django_db

PUBLIC = {"GET /api/health", "GET /api/auth/session", "POST /api/auth/login"}
# The catalogue: every role reads it, and it is neither a roster nor a rostering attribute.
ANY_ROLE = {"POST /api/auth/logout", "POST /api/auth/change-password",
            "GET /api/hospital/shifts", "GET /api/hospital/leave-types", "GET /api/hospital/skills",
            "GET /api/hospital/public-holidays",
            "GET /api/hospital/ramadan",
            # Each user's own, and nobody else's: nothing here takes a user.
            "GET /api/notifications", "GET /api/notifications/unread", "POST /api/notifications/{notification_id}/read",
            "POST /api/notifications/read-all", "POST /api/notifications/push/state", "POST /api/notifications/push/on",
            "POST /api/notifications/push/off", "POST /api/notifications/push/off-everywhere"}


def endpoints():
    """(method, path, the SignedIn guarding it or None), one per method of every operation."""
    found = []
    for router in api._get_bound_routers():  # private, but only these have the inherited auth applied
        prefix = router.prefix
        for path, view in router.path_operations.items():
            for operation in view.operations:
                guards = [auth for auth in operation.auth_callbacks if isinstance(auth, SignedIn)]
                full = "/api" + (f"/{prefix.strip('/')}" if prefix.strip("/") else "") + path
                found += [(method, full, guards[0] if guards else None) for method in operation.methods]
    return found


ENDPOINTS = endpoints()
RESTRICTED = [(method, path, guard) for method, path, guard in ENDPOINTS if guard and guard.roles]
REFUSED_ROLES = [(method, path, role) for method, path, guard in RESTRICTED for role in Role if role not in guard.roles]


def call(client, method, path):
    url = re.sub(r"\{[^}]*\}", str(uuid4()), path)
    return getattr(client, method.lower())(url, {}, content_type="application/json")


def label(method, path):
    return f"{method} {path}"


def test_the_walk_found_the_endpoints():
    assert {"GET /api/users", "PUT /api/users/{user_id}/full-name", "GET /api/audit/entries", "PUT /api/ops/technical-settings",
            "PUT /api/hospital/rules", "GET /api/units", "POST /api/units/{unit_id}/archive"} <= {
        label(method, path) for method, path, _ in ENDPOINTS}


def test_only_the_named_endpoints_are_public():
    assert {label(method, path) for method, path, guard in ENDPOINTS if guard is None} == PUBLIC


def test_only_the_named_endpoints_leave_the_role_open():
    assert {label(method, path) for method, path, guard in ENDPOINTS if guard and not guard.roles} == ANY_ROLE


def test_every_role_is_refused_somewhere():
    assert {role for _, _, role in REFUSED_ROLES} == set(Role)


@pytest.mark.parametrize("method, path, role", REFUSED_ROLES)
def test_a_role_gets_403_where_it_has_no_business(signed_in, method, path, role):
    assert call(signed_in(role), method, path).status_code == 403


@pytest.mark.parametrize("method, path, guard", RESTRICTED, ids=lambda value: value if isinstance(value, str) else "")
def test_signed_out_gets_401(method, path, guard):
    assert call(Client(), method, path).status_code == 401


@pytest.mark.parametrize("method, path, guard", RESTRICTED, ids=lambda value: value if isinstance(value, str) else "")
def test_a_pending_forced_change_gets_403_even_with_the_right_role(signed_in, method, path, guard):
    client = signed_in(guard.roles[0], must_change_password=True)

    assert call(client, method, path).status_code == 403


@pytest.mark.parametrize("method, path, guard", [e for e in RESTRICTED if e[0] != "GET"],
                         ids=lambda value: value if isinstance(value, str) else "")
def test_a_write_without_the_csrf_token_gets_403_even_with_the_right_role(make_user, method, path, guard):
    make_user(role=guard.roles[0])
    client = Client(enforce_csrf_checks=True)
    client.get("/api/auth/session")
    token = client.cookies["csrftoken"].value
    assert client.post("/api/auth/login", {"username": "someone", "password": PASSWORD},
                       content_type="application/json", headers={"X-CSRFToken": token}).status_code == 200

    assert call(client, method, path).status_code == 403


MANAGER, APPROVERS = {Role.UNIT_MANAGER}, {Role.CNO, Role.DEPUTY_CNO}
# Who may call each transition and read of a roster, and take its files or its print sheet: a
# nurse takes no file, only their own unit's print sheet of the roster in force.
# A unit's own scope is `units.scope`, held by the rosters' tests.
ROSTER_ROLES = {
    "GET /api/units/{unit_id}/roster": MANAGER | APPROVERS,
    "POST /api/units/{unit_id}/roster/submit": MANAGER,
    "POST /api/units/{unit_id}/roster/approve": APPROVERS,
    "POST /api/units/{unit_id}/roster/reject": APPROVERS,
    "POST /api/units/{unit_id}/roster/pins": MANAGER,
    "POST /api/units/{unit_id}/roster/check": MANAGER,
    "POST /api/units/{unit_id}/roster/versions": MANAGER,
    "GET /api/units/{unit_id}/roster/versions": MANAGER | APPROVERS,
    "GET /api/units/{unit_id}/roster/versions/compare": MANAGER | APPROVERS,
    "GET /api/units/{unit_id}/roster/export": MANAGER | APPROVERS,
    "GET /api/units/{unit_id}/roster/sheet": MANAGER | APPROVERS,
    "POST /api/units/{unit_id}/solves": MANAGER,
    "GET /api/units/{unit_id}/solves/preview": MANAGER,
    "POST /api/units/{unit_id}/replans": MANAGER,
    "GET /api/units/{unit_id}/replans/preview": MANAGER,
    "GET /api/units/{unit_id}/solve-history": MANAGER | APPROVERS,
    "GET /api/units/{unit_id}/solve-history/{solve_id}": MANAGER | APPROVERS,
    "POST /api/units/{unit_id}/solve-history/{solve_id}/restore": MANAGER,
    "GET /api/rosters/overview": APPROVERS,
    "GET /api/rosters/day": APPROVERS,
    "GET /api/rosters/submitted": APPROVERS,
    "GET /api/rosters/mine": {Role.STAFF_NURSE, Role.UNIT_MANAGER},
    "GET /api/rosters/mine/sheet": {Role.STAFF_NURSE, Role.UNIT_MANAGER},
}


def test_each_roster_transition_and_read_is_for_these_roles_only():
    guarded = {label(method, path): set(guard.roles) for method, path, guard in RESTRICTED}
    assert {name: guarded.get(name) for name in ROSTER_ROLES} == ROSTER_ROLES


def test_the_system_admin_reaches_no_roster_content():
    """Their solve history across units is a closed list of fields (`solves.history_api.hospital_router`)."""
    roster_roles = {label(method, path): guard.roles for method, path, guard in RESTRICTED
                    if re.search(r"/roster|/solves|/replans|/units/\{unit_id\}/solve-history", path)}
    assert set(ROSTER_ROLES) <= set(roster_roles)
    assert [name for name, roles in roster_roles.items() if Role.SYSTEM_ADMIN in roles] == []


# Swaps: a staff nurse proposes, answers and withdraws, and reads only their own; the unit's manager reads and decides
# the unit's. Nobody else reads any: not the System Admin, and not the CNO or the Deputy CNO.
NURSE = {Role.STAFF_NURSE}
SWAP_ROLES = {
    "GET /api/swaps/mine": NURSE,
    "GET /api/swaps/compose": NURSE,
    "POST /api/swaps/mine": NURSE,
    "POST /api/swaps/mine/{swap_id}/withdraw": NURSE,
    "POST /api/swaps/mine/{swap_id}/accept": NURSE,
    "POST /api/swaps/mine/{swap_id}/reject": NURSE,
    "GET /api/units/{unit_id}/swaps": MANAGER,
    "GET /api/units/{unit_id}/swaps/{swap_id}": MANAGER,
    "POST /api/units/{unit_id}/swaps/{swap_id}/approve": MANAGER,
    "POST /api/units/{unit_id}/swaps/{swap_id}/reject": MANAGER,
}


def test_each_swap_endpoint_is_for_these_roles_only():
    guarded = {label(method, path): set(guard.roles) for method, path, guard in RESTRICTED}
    assert {name: roles for name, roles in guarded.items() if "swaps" in name} == SWAP_ROLES


# Covers: the unit's manager reads, opens, offers and withdraws the unit's; a staff nurse reads those offered
# to her and answers them. Nobody else reads any: not the System Admin, and not the CNO or the Deputy CNO.
COVER_ROLES = {
    "GET /api/units/{unit_id}/covers": MANAGER,
    "GET /api/units/{unit_id}/covers/{cover_id}": MANAGER,
    "POST /api/units/{unit_id}/covers": MANAGER,
    "POST /api/units/{unit_id}/covers/{cover_id}/offer": MANAGER,
    "POST /api/units/{unit_id}/covers/{cover_id}/withdraw": MANAGER,
    "GET /api/covers/mine": NURSE,
    "POST /api/covers/mine/{cover_id}/accept": NURSE,
    "POST /api/covers/mine/{cover_id}/decline": NURSE,
}


def test_each_cover_endpoint_is_for_these_roles_only():
    guarded = {label(method, path): set(guard.roles) for method, path, guard in RESTRICTED}
    assert {name: roles for name, roles in guarded.items() if "covers" in name} == COVER_ROLES


# Preferences: a staff nurse makes, withdraws and asks for her own as requests, and reads only her own; the unit's
# manager reads the unit's, read-only. Nobody else reads any: not the System Admin, and not the CNO or the Deputy CNO.
PREFERENCE_ROLES = {
    "GET /api/preferences/mine": NURSE,
    "POST /api/preferences/mine": NURSE,
    "POST /api/preferences/mine/{preference_id}/withdraw": NURSE,
    "POST /api/preferences/mine/{preference_id}/ask-as-request": NURSE,
    "GET /api/units/{unit_id}/request-windows/{window_id}/preferences": MANAGER,
}


def test_each_preference_endpoint_is_for_these_roles_only():
    guarded = {label(method, path): set(guard.roles) for method, path, guard in RESTRICTED}
    assert {name: roles for name, roles in guarded.items() if "preferences" in name} == PREFERENCE_ROLES


# A nurse's records: the CNO and the Deputy CNO record, renew and take off a nurse's skills, certificates and
# licences, by hand or from the skills import; every other write about a nurse keeps its roles, and the catalogue stays the
# CNO's. A closed list: a write about a nurse added later is named here with its roles.
CNO, RECORDERS = {Role.CNO}, {Role.CNO, Role.DEPUTY_CNO}
NURSE_WRITE_ROLES = {
    "POST /api/hospital/skills": CNO,
    "PUT /api/hospital/skills/{skill_id}": CNO,
    "POST /api/nurses": CNO,
    "POST /api/nurses/managers": CNO,
    "POST /api/nurses/{nurse_id}/transfer": CNO,
    "PUT /api/nurses/{nurse_id}/end-date": CNO,
    "PUT /api/nurses/{nurse_id}/details": CNO,
    "POST /api/nurses/{nurse_id}/reset-password": CNO | MANAGER,
    "GET /api/nurses/import/template": CNO,
    "POST /api/nurses/import/preview": CNO,
    "POST /api/nurses/import/confirm": CNO,
    "GET /api/nurses/export": CNO,
    "GET /api/nurses/skills-import/template": RECORDERS,
    "POST /api/nurses/skills-import/preview": RECORDERS,
    "POST /api/nurses/skills-import/confirm": RECORDERS,
    "POST /api/nurses/{nurse_id}/skills": RECORDERS,
    "PUT /api/nurses/{nurse_id}/skills/{skill_id}": RECORDERS,
    "POST /api/nurses/{nurse_id}/skills/{skill_id}/remove": RECORDERS,
    "PUT /api/units/{unit_id}/nurses/{nurse_id}/attributes": MANAGER,
    "PUT /api/units/{unit_id}/nurses/{nurse_id}/allowed-shifts": MANAGER,
    "PUT /api/units/{unit_id}/nurses/{nurse_id}/probation/length": MANAGER,
    "POST /api/units/{unit_id}/nurses/{nurse_id}/probation/extend": MANAGER,
    "POST /api/units/{unit_id}/nurses/{nurse_id}/probation/end": MANAGER,
    "PUT /api/units/{unit_id}/nurses/{nurse_id}/preceptor": MANAGER,
}


def test_the_cno_and_the_deputy_cno_record_a_nurses_skills_and_every_other_nurse_write_keeps_its_roles():
    guarded = {label(method, path): set(guard.roles) for method, path, guard in RESTRICTED}
    writes = {name: roles for name, roles in guarded.items()
              if re.match(r"(POST|PUT|PATCH|DELETE) /api/(nurses|units/\{unit_id\}/nurses)", name)
              or re.match(r"GET /api/nurses/(import/template|skills-import/template|export)$", name)
              or re.match(r"(POST|PUT) /api/hospital/skills", name)}
    assert writes == NURSE_WRITE_ROLES
