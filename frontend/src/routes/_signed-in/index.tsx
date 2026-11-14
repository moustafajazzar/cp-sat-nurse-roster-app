import { createFileRoute, Link, redirect, useRouteContext } from "@tanstack/react-router"
import type { ReactNode } from "react"
import { z } from "zod"

import { useGetMyRoster, useGetMyUnit, useListSubmittedRosters, useListUnits } from "@/api/generated/endpoints"
import { Role } from "@/api/generated/model"
import { ExpiriesCount } from "@/components/expiries"
import { ROLE_NAMES } from "@/roles"
import { CnoPrototype } from "@/components/cno-prototype"
import { hospitalTime, monthName } from "@/lib/utils"

export const Route = createFileRoute("/_signed-in/")({
  beforeLoad: ({ context }) => {
    if (context.user.role === Role.system_admin) throw redirect({ to: "/health" })
  },
  // PROTOTYPE (CNO cross-unit views): ?variant=a|b|c shows a throwaway hospital-wide home.
  validateSearch: (search) => z.object({ variant: z.enum(["a", "b", "c"]).optional() }).parse(search),
  staticData: { title: "Home" },
  component: HomePage,
})

/** The CNO and the Deputy CNO both decide, so both see what waits, oldest first. */
function Waiting() {
  const waiting = useListSubmittedRosters()
  if (waiting.isPending) return <p>Loading the rosters waiting for a decision…</p>
  if (waiting.isError) return <p role="alert">The rosters waiting for a decision could not be read. Reload the page to try again.</p>
  return (
    <section aria-labelledby="waiting" className="flex flex-col gap-2">
      <h2 id="waiting" className="text-base font-medium">Waiting for a decision</h2>
      {waiting.data.length === 0 ? <p>No roster is waiting for a decision.</p> : (
        <ul className="flex flex-col gap-1">
          {waiting.data.map((roster) => (
            <li key={roster.unit_id + roster.month}>
              <Link to="/unit-roster" search={{ unit: roster.unit_id, month: roster.month }} className="underline underline-offset-3">{roster.unit}, {monthName(roster.month)}</Link>
              {", "}submitted {hospitalTime(roster.submitted_at)} by {roster.submitted_by}.
              {roster.changed_since && " Something has changed since it was submitted."}
            </li>
          ))}
        </ul>
      )}
      <p><Link to="/rosters" className="underline underline-offset-3">Every unit's rosters, month by month</Link></p>
    </section>
  )
}

/** A deactivated manager's unit waits here for the CNO, who is told nowhere else, so a failed read must say so. */
function CnoHome() {
  const units = useListUnits()
  const waiting = (units.data ?? []).filter((unit) => !unit.archived && !unit.manager).map((unit) => unit.name)
  return (
    <>
      <Waiting />
      <p>Set the rules every unit rosters by under Hospital rules, and keep the shifts, leave types, holidays and Ramadan dates they use.</p>
      <p>Add the hospital's nurses under <Link to="/nurses" className="underline underline-offset-3">Nurses</Link>.</p>
      {units.isError && <p role="alert">The units could not be read, so any with no manager are not listed. Reload the page to try again.</p>}
      {waiting.length > 0 && (
        <p>
          {waiting.join(", ")} {waiting.length === 1 ? "has" : "have"} no manager.{" "}
          <Link to="/units" className="underline underline-offset-3">Assign one under Units</Link>
        </p>
      )}
      <ExpiriesCount />
    </>
  )
}

function ManagerHome() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  return (
    <>
      <ManagedUnit />
      {/* On their unit's rosters themselves, so their own roster and month are theirs to read, whatever unit they hold. */}
      {user.has_nurse_profile && <>
        <p><Link to="/my-roster" className="underline underline-offset-3">My roster</Link> has your own shifts for each coming month once it is approved.</p>
        <p>Your hours, shifts and leave are under <Link to="/my-month" className="underline underline-offset-3">My month</Link>.</p>
      </>}
    </>
  )
}

function ManagedUnit() {
  const units = useListUnits()
  if (units.isPending) return <p>Loading…</p>
  if (units.isError) return <p role="alert">Your unit could not be read. Reload the page to try again.</p>
  const unit = units.data[0]
  if (!unit) return <p>No unit is assigned to you yet. The CNO assigns each unit its manager.</p>
  return (
    <>
      <p>Your unit is {unit.name}.</p>
      {unit.rules_need_review && (
        <p>
          Its unit rules are still the seeded defaults.{" "}
          <Link to="/unit-rules" className="underline underline-offset-3">Review them before its first solve</Link>
        </p>
      )}
    </>
  )
}

function NurseHome() {
  const unit = useGetMyUnit()
  const roster = useGetMyRoster()
  if (unit.isPending) return <p>Loading…</p>
  if (unit.isError) return <p role="alert">Your unit could not be read. Reload the page to try again.</p>
  return (
    <>
      <p>Your unit is {unit.data.name}.</p>
      {roster.data && (roster.data.roster
        ? <p>The roster of {monthName(roster.data.month)} is approved. <Link to="/my-roster" className="underline underline-offset-3">See it under My roster</Link></p>
        : <>
          <p>No roster yet for {monthName(roster.data.month)}.</p>
          <p><Link to="/my-roster" className="underline underline-offset-3">My roster</Link> has each coming month once it is approved.</p>
        </>)}
      {roster.isError && <p role="alert">Whether this month's roster is approved could not be read. My roster tries again.</p>}
      <p>Your hours, shifts and leave are under <Link to="/my-month" className="underline underline-offset-3">My month</Link>.</p>
    </>
  )
}

/** Every role but the System Admin's, so a new role cannot go without a home. */
const HOME_OF: Record<Exclude<Role, "system_admin">, () => ReactNode> = {
  cno: CnoHome,
  deputy_cno: Waiting,
  unit_manager: ManagerHome,
  staff_nurse: NurseHome,
}

function HomePage() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  const Home = HOME_OF[user.role as keyof typeof HOME_OF]
  const { variant } = Route.useSearch() as { variant?: "a" | "b" | "c" }
  if (variant && (user.role === Role.cno || user.role === Role.deputy_cno)) return <CnoPrototype variant={variant} user={user} />
  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-2">
      <h1 className="text-xl font-medium">{user.full_name || user.username}</h1>
      <p className="text-muted-foreground">{ROLE_NAMES[user.role]}</p>
      <Home />
    </div>
  )
}
