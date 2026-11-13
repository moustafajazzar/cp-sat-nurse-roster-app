import { linkOptions } from "@tanstack/react-router"

import { Role, type Me } from "@/api/generated/model"

export const ROLE_NAMES: Record<Role, string> = {
  system_admin: "System Admin",
  cno: "CNO",
  deputy_cno: "Deputy CNO",
  unit_manager: "Unit Manager",
  staff_nurse: "Staff Nurse",
}

const ADMIN_PAGES = linkOptions([
  { to: "/health", label: "Health" },
  { to: "/settings", label: "Technical settings" },
  { to: "/users", label: "Users" },
  { to: "/audit-log", label: "Audit log" },
  { to: "/solve-history", label: "Solve history" },
])
const HOME = { to: "/", label: "Home", activeOptions: { exact: true } } as const
const RULES = { to: "/hospital-rules", label: "Hospital rules" } as const
const CATALOGUE = [
  { to: "/shifts", label: "Shifts" },
  { to: "/leave-types", label: "Leave types" },
  { to: "/skills", label: "Skills" },
  { to: "/holidays", label: "Holidays and Ramadan" },
] as const
const UNITS = { to: "/units", label: "Units" } as const
const NURSES = { to: "/nurses", label: "Nurses" } as const
const EXPIRIES = { to: "/expiries", label: "Expiries" } as const
const UNIT_RULES = { to: "/unit-rules", label: "Unit rules" } as const
const ATTRIBUTES = { to: "/rostering-attributes", label: "Rostering attributes" } as const
const IMPORT_SKILLS = { to: "/nurses/import-skills", label: "Import skills" } as const
const ROSTER = { to: "/roster", label: "Roster" } as const
const SOLVE_HISTORY = { to: "/solve-history", label: "Solve history" } as const
const LEAVE = { to: "/leave", label: "Leave" } as const
const REQUESTS = { to: "/requests", label: "Requests" } as const
const MY_MONTH = { to: "/my-month", label: "My month" } as const
const MY_ROSTER = { to: "/my-roster", label: "My roster" } as const
const MY_SWAPS = { to: "/swaps", label: "Swaps" } as const
const MY_COVERS = { to: "/covers", label: "Covers" } as const
const UNIT_SWAPS = { to: "/unit-swaps", label: "Swaps" } as const
const UNIT_COVERS = { to: "/unit-covers", label: "Covers" } as const
const ROSTERS = { to: "/rosters", label: "Rosters" } as const
const DAY_VIEW = { to: "/day", label: "Day view" } as const
const NURSE_PAGES = linkOptions([HOME, MY_MONTH, MY_ROSTER, MY_SWAPS, MY_COVERS, ...CATALOGUE])
const MANAGER_PAGES = linkOptions([HOME, ROSTER, SOLVE_HISTORY, REQUESTS, UNIT_SWAPS, UNIT_COVERS, UNIT_RULES, ATTRIBUTES, NURSES, LEAVE, ...CATALOGUE])
/** A manager on their unit's rosters reads their own roster and month too: first, as a Staff Nurse's are, and apart from the
 *  unit's pages, since My roster's heading names the unit as Roster's does. A Staff Nurse's requests, swaps and covers are
 *  not theirs: only staff nurses' cells are swapped or offered. */
const MANAGER_NURSE_PAGES = linkOptions([HOME, MY_ROSTER, MY_MONTH, ...MANAGER_PAGES.filter((page) => page !== HOME)])
/** The Deputy CNO records nurses' skills on Rostering attributes and imports them beside it; the CNO reaches the
 *  import from the Nurses page. */
const DEPUTY_PAGES = linkOptions([HOME, ROSTERS, DAY_VIEW, UNIT_RULES, ATTRIBUTES, IMPORT_SKILLS, EXPIRIES, LEAVE, RULES, ...CATALOGUE])
const CNO_PAGES = linkOptions([HOME, ROSTERS, DAY_VIEW, UNITS, UNIT_RULES, ATTRIBUTES, NURSES, EXPIRIES, LEAVE, RULES, ...CATALOGUE])

const NAVIGATION = {
  system_admin: ADMIN_PAGES,
  cno: CNO_PAGES,
  deputy_cno: DEPUTY_PAGES,
  unit_manager: MANAGER_PAGES,
  staff_nurse: NURSE_PAGES,
} satisfies Record<Role, unknown>

/** What the signed-in user reaches from the header: their role's pages, and a Unit Manager's own with a nurse profile. A page left
 *  out of a role's list is only not linked; the API refuses the rest whatever this says. The catalogue's pages open for the
 *  System Admin too, since every role may read them, but nothing in their work calls for a link. */
export function navigation(user: Me) {
  return user.role === Role.unit_manager && user.has_nurse_profile ? MANAGER_NURSE_PAGES : NAVIGATION[user.role]
}
