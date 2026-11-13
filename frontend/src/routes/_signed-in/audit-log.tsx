import { zodResolver } from "@hookform/resolvers/zod"
import { keepPreviousData } from "@tanstack/react-query"
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router"
import { useEffect, useRef } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { useGetAuditFilterOptions, useListAuditEntries } from "@/api/generated/endpoints"
import { Role, type AuditEntrySchema } from "@/api/generated/model"
import { FormField, SelectField } from "@/components/form-field"
import { Button, buttonVariants } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { NativeSelectOption } from "@/components/ui/native-select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { cn, hospitalTime, scrollableRegion } from "@/lib/utils"
import { onlyFor } from "@/session"

/** The router reads `?user=123` as a number, and a hospital ID is a perfectly good username. */
const text = z.union([z.string(), z.number().transform(String)]).optional().catch(undefined)
/** A hand-edited date is dropped rather than sent: the API would refuse it and the page could only
 *  say "reload", with the bad value still in the address. */
const day = z.iso.date().optional().catch(undefined)
const searchSchema = z.object({
  user: text,
  action: text,
  date_from: day,
  date_to: day,
  older_than: z.number().int().optional().catch(undefined),
})

export const Route = createFileRoute("/_signed-in/audit-log")({
  beforeLoad: onlyFor(Role.system_admin),
  validateSearch: (search) => searchSchema.parse(search),
  staticData: { title: "Audit log" },
  component: AuditLogPage,
})

const filterSchema = z
  .object({
    user: z.string().trim(),
    action: z.string(),
    date_from: z.string(),
    date_to: z.string(),
  })
  .refine((filters) => !filters.date_from || !filters.date_to || filters.date_from <= filters.date_to, {
    path: ["date_to"],
    message: "This is before the start date.",
  })

/** Actions taken by someone not signed in, who is shown as "Unknown" rather than as the system. */
const ANONYMOUS_ACTIONS = new Set(["auth.login_failed", "auth.login_throttled"])

const ACTION_NAMES: Record<string, string> = {
  "auth.login": "Logged in",
  "auth.login_failed": "Failed login",
  "auth.login_throttled": "Logins throttled",
  "auth.logout": "Logged out",
  "auth.password_changed": "Changed own password",
  "backup.downloaded": "Downloaded a backup",
  "hospital_rules.changed": "Changed hospital rules",
  "leave.changed": "Changed a nurse's leave",
  "leave.recorded": "Recorded a nurse's leave",
  "leave.removed": "Removed a nurse's leave",
  "leave_type.changed": "Changed a leave type",
  "leave_type.created": "Added a leave type",
  "nurse.added": "Added a nurse",
  "nurse.attributes_changed": "Changed a nurse's rostering attributes",
  "nurse.details_corrected": "Corrected a nurse's details",
  "nurse.end_date_changed": "Changed a nurse's end date",
  "nurse.probation_ended": "Ended a nurse's probation",
  "nurse.probation_extended": "Extended a nurse's probation",
  "nurse.probation_length_set": "Set the length of a nurse's probation",
  "nurse.probation_ticked": "Put a nurse on probation",
  "nurse.probation_unticked": "Took a nurse off probation",
  "nurse.skills_changed": "Changed a nurse's skills",
  "nurse.skills_imported": "Imported skills",
  "nurse.transferred": "Transferred a nurse",
  "nurses.exported": "Exported the staff list",
  "nurses.imported": "Imported nurses",
  "public_holiday.changed": "Changed a public holiday",
  "public_holiday.created": "Added a public holiday",
  "public_holiday.removed": "Removed a public holiday",
  "ramadan_dates.changed": "Changed Ramadan dates",
  "roster.approved": "Approved a roster",
  "roster.changed": "Changed an approved roster",
  "roster.draft_replaced": "A solve replaced a draft",
  "roster.draft_restored": "Restored a result as a draft",
  "roster.exported": "Exported a roster version",
  "request_window.opened": "Opened a request window",
  "request_window.closed": "Closed a request window early",
  "request.made": "Made a request",
  "request.withdrawn": "Withdrew a request",
  "request.accepted": "Accepted a nurse's request",
  "request.rejected": "Rejected a nurse's request",
  "request.edited": "Changed a nurse's accepted request",
  "request.cancelled": "Cancelled a nurse's accepted request",
  "roster.rejected": "Rejected a roster",
  "roster.submitted": "Submitted a roster",
  "shift.changed": "Changed a shift",
  "shift.created": "Added a shift",
  "skill.changed": "Changed a skill",
  "skill.created": "Added a skill",
  "solve.started": "Started a solve",
  "solve.stopped": "Stopped a solve",
  "technical_settings.changed": "Changed technical settings",
  "unit.archived": "Archived a unit",
  "unit.changed": "Changed a unit",
  "unit.created": "Created a unit",
  "unit_rules.changed": "Changed unit rules",
  "user.created": "Created user",
  "user.deactivated": "Deactivated user",
  "user.name_corrected": "Corrected a user's name",
  "user.password_reset": "Reset a user's password",
  "user.reactivated": "Reactivated user",
}

/** An action not named here yet is shown by its code, which is still readable. */
const actionName = (action: string) => ACTION_NAMES[action] ?? action

function Filters() {
  const { user, action, date_from, date_to } = Route.useSearch()
  const navigate = useNavigate({ from: Route.fullPath })
  const options = useGetAuditFilterOptions()
  const form = useForm({
    resolver: zodResolver(filterSchema),
    // Follows the address, so going back in the browser also brings back what the form said.
    values: { user: user ?? "", action: action ?? "", date_from: date_from ?? "", date_to: date_to ?? "" },
  })
  const { errors } = form.formState
  const filtered = !!(user || action || date_from || date_to)
  const actions = [...new Set([...(options.data?.actions ?? []), ...(action ? [action] : [])])]

  const apply = form.handleSubmit((filters) =>
    navigate({
      search: {
        user: filters.user || undefined,
        action: filters.action || undefined,
        date_from: filters.date_from || undefined,
        date_to: filters.date_to || undefined,
      },
    }),
  )

  return (
    <form onSubmit={apply} noValidate aria-label="Filters">
      <div className="grid items-start gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <FormField label="User" list="audit-users" autoComplete="off" spellCheck={false} autoCapitalize="none"
          description="What they did and what was done to them."
          registration={form.register("user")} error={errors.user} />
        <datalist id="audit-users">
          {options.data?.users.map((username) => <option key={username} value={username} />)}
        </datalist>
        <SelectField label="Action" registration={form.register("action")} error={errors.action}>
          <NativeSelectOption value="">All actions</NativeSelectOption>
          {actions.map((name) => <NativeSelectOption key={name} value={name}>{actionName(name)}</NativeSelectOption>)}
        </SelectField>
        <FormField label="From" type="date" registration={form.register("date_from")} error={errors.date_from} />
        <FormField label="To" type="date" registration={form.register("date_to")} error={errors.date_to} />
      </div>
      <div className="mt-4 flex items-center gap-4">
        <Button type="submit">Apply filters</Button>
        {filtered && <Link to={Route.fullPath} search={{}} className="text-sm underline underline-offset-4">Clear filters</Link>}
      </div>
    </form>
  )
}

function who(entry: AuditEntrySchema) {
  return entry.actor ?? (ANONYMOUS_ACTIONS.has(entry.action) ? "Unknown" : "System")
}

function Details({ entry }: { entry: AuditEntrySchema }) {
  const before = entry.before ?? {}
  const after = entry.after ?? {}
  const fields = [...new Set([...Object.keys(before), ...Object.keys(after)])]
  if (!fields.length) return null
  const show = (value: unknown) => JSON.stringify(value)
  return (
    <ul className="flex flex-col gap-0.5">
      {fields.map((field) => (
        <li key={field} className="wrap-anywhere">
          {field}: {field in before && <>{show(before[field])} → </>}
          {field in after ? show(after[field]) : "removed"}
        </li>
      ))}
    </ul>
  )
}

function Entries({ entries }: { entries: AuditEntrySchema[] }) {
  return (
    <Table container={scrollableRegion("Audit entries")}>
      <TableHeader>
        <TableRow>
          <TableHead>Time</TableHead>
          <TableHead>Who</TableHead>
          <TableHead>Action</TableHead>
          <TableHead>On</TableHead>
          <TableHead>Details</TableHead>
          <TableHead>IP address</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {entries.map((entry) => (
          <TableRow key={entry.id} className="align-top">
            <TableCell className="tabular-nums">{hospitalTime(entry.at)}</TableCell>
            <TableCell translate="no">{who(entry)}</TableCell>
            <TableCell>{actionName(entry.action)}</TableCell>
            <TableCell translate="no">{entry.target}</TableCell>
            <TableCell className="min-w-56 whitespace-normal"><Details entry={entry} /></TableCell>
            <TableCell className="tabular-nums">{entry.ip}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function AuditLogPage() {
  const { older_than, ...filters } = Route.useSearch()
  const entries = useListAuditEntries({ ...filters, older_than }, { query: { placeholderData: keepPreviousData } })
  const page = entries.data
  const heading = useRef<HTMLHeadingElement>(null)

  // The link that was clicked may be gone from the new page, which would drop focus on the floor.
  const previous = useRef(older_than)
  useEffect(() => {
    if (previous.current !== older_than) heading.current?.focus()
    previous.current = older_than
  }, [older_than])

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4">
      <h1 className="text-xl font-medium">Audit log</h1>
      <Card>
        <CardContent><Filters /></CardContent>
      </Card>
      <h2 ref={heading} tabIndex={-1} className="font-medium outline-none">Entries</h2>
      <p role="status" className="text-sm text-muted-foreground">
        {entries.isPending || entries.isPlaceholderData ? "Loading…" : page ? `${page.entries.length} shown, newest first. Times are hospital time.` : ""}
      </p>
      {entries.isError && <p role="alert">The audit log could not be read. Reload the page to try again.</p>}
      {page && page.entries.length === 0 && <p>No entries match. Try wider dates or fewer filters.</p>}
      {page && page.entries.length > 0 && (
        <Card>
          <CardContent><Entries entries={page.entries} /></CardContent>
        </Card>
      )}
      {page && (older_than !== undefined || page.older_than != null) && (
        <nav aria-label="Pages" className="flex gap-2">
          {older_than !== undefined && (
            <Link to={Route.fullPath} search={filters} className={cn(buttonVariants({ variant: "outline", size: "sm" }))}>Newest entries</Link>
          )}
          {page.older_than != null && (
            <Link to={Route.fullPath} search={{ ...filters, older_than: page.older_than }}
              className={cn(buttonVariants({ variant: "outline", size: "sm" }))}>Older entries</Link>
          )}
        </nav>
      )}
    </div>
  )
}
