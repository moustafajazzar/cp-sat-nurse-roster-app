"""The JSON API. The OpenAPI document is not served: `manage.py export_openapi_schema` writes
it for the frontend's generated client (see frontend/orval.config.ts).

Every endpoint is signed-in unless it says `auth=None`, and names its roles with
`accounts.authz.SignedIn`. accounts/tests/test_authorization.py walks every endpoint to hold this."""
from ninja import NinjaAPI

from accounts.api import router as accounts_router
from accounts.api import users_router
from accounts.authz import SignedIn
from audit.api import router as audit_router
from closes.api import router as closes_router
from hospital.api import router as hospital_router
from hospital.catalogue_api import router as catalogue_router
from notifications.api import router as notifications_router
from notifications.push_api import router as push_router
from nurses.api import router as nurses_router
from nurses.attributes_api import router as nurse_attributes_router
from nurses.files_api import router as nurse_files_router
from nurses.leave_api import my_leave_router, unit_leave_router
from nurses.skills_api import router as nurse_skills_router
from ops.api import router as ops_router
from requesting.api import router as requesting_router
from requesting.mine_api import router as my_requests_router
from requesting.preferences_api import router as my_preferences_router
from rosters.api import router as rosters_router
from rosters.exports_api import router as roster_exports_router
from rosters.reading_api import router as rosters_reading_router
from solves.api import router as solves_router
from solves.history_api import hospital_router as hospital_solves_router
from solves.history_api import router as solve_history_router
from swaps.api import router as swaps_router
from swaps.mine_api import router as my_swaps_router
from covers.api import router as covers_router
from covers.mine_api import router as my_covers_router
from units.api import router as units_router
from units.rules_api import router as unit_rules_router

api = NinjaAPI(title="Nurse roster", version="1", openapi_url=None, docs_url=None, auth=SignedIn())
api.add_router("", ops_router)
api.add_router("/auth", accounts_router)
api.add_router("/users", users_router)
api.add_router("/audit", audit_router)
api.add_router("/hospital", hospital_router)
api.add_router("/hospital", catalogue_router)
api.add_router("/units", units_router)
api.add_router("/units", unit_rules_router)
api.add_router("/units", nurse_attributes_router)
api.add_router("/units", unit_leave_router)
api.add_router("/units", solves_router)
api.add_router("/units", rosters_router)
api.add_router("/units", roster_exports_router)
api.add_router("/units", solve_history_router)
api.add_router("/units", requesting_router)
api.add_router("/units", swaps_router)
api.add_router("/units", covers_router)
api.add_router("/units", closes_router)
api.add_router("/requests", my_requests_router)
api.add_router("/preferences", my_preferences_router)
api.add_router("/swaps", my_swaps_router)
api.add_router("/covers", my_covers_router)
api.add_router("/rosters", rosters_reading_router)
api.add_router("/solve-history", hospital_solves_router)
api.add_router("/nurses", nurses_router)
api.add_router("/nurses", nurse_files_router)
api.add_router("/nurses", my_leave_router)
api.add_router("/nurses", nurse_skills_router)
api.add_router("/notifications/push", push_router)
api.add_router("/notifications", notifications_router)
