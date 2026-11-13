import { zodResolver } from "@hookform/resolvers/zod"
import { useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useRouteContext } from "@tanstack/react-router"
import { useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { getListLeaveTypesQueryKey, useCreateLeaveType, useListLeaveTypes, useUpdateLeaveType } from "@/api/generated/endpoints"
import { Role, type LeaveTypeBody, type LeaveTypeSchema } from "@/api/generated/model"
import { createLeaveTypeBodyNameMax as NAME_MAX } from "@/api/generated/zod"
import { CheckboxField, FormField } from "@/components/form-field"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { FieldGroup } from "@/components/ui/field"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { serverErrors, showServerErrors } from "@/lib/server-errors"
import { useEditing } from "@/lib/use-editing"
import { scrollableRegion } from "@/lib/utils"
import { useFocusOnOpen } from "@/lib/use-focus-on-open"

/** Open to every role, which may all read the catalogue. Only the CNO gets the form. */
export const Route = createFileRoute("/_signed-in/leave-types")({
  staticData: { title: "Leave types" },
  component: LeaveTypesPage,
})

const schema = z.object({
  name: z.string().trim().min(1, "Enter a name.").max(NAME_MAX, `At most ${NAME_MAX} characters.`),
  active: z.boolean(),
})

const NEW_TYPE: LeaveTypeBody = { name: "", active: true }

function LeaveTypeForm({ row, onDone, onCancel }: { row: LeaveTypeSchema | null; onDone: (message: string) => void; onCancel: () => void }) {
  const queryClient = useQueryClient()
  const create = useCreateLeaveType()
  const update = useUpdateLeaveType()
  const mutation = row ? update : create
  const form = useForm<LeaveTypeBody>({ resolver: zodResolver(schema), defaultValues: row ?? NEW_TYPE })
  const { errors } = form.formState

  useFocusOnOpen(() => {
    if (row) form.setFocus("name")
  }, [row, form])

  const saved = (message: string) => {
    void queryClient.invalidateQueries({ queryKey: getListLeaveTypesQueryKey() })
    onDone(message)
  }
  const refused = (error: unknown) => showServerErrors(form, error)

  const submit = form.handleSubmit((data) => {
    if (row) {
      update.mutate({ leaveTypeId: row.id, data }, { onSuccess: (next) => saved(`${next.name} is saved.`), onError: refused })
    } else {
      create.mutate({ data }, {
        onSuccess: (next) => {
          form.reset(NEW_TYPE)
          saved(`${next.name} is added.`)
        },
        onError: refused,
      })
    }
  })
  const unexpected = mutation.isError && serverErrors(mutation.error).length === 0

  return (
    <form onSubmit={submit} noValidate>
      <FieldGroup>
        <FormField label="Name" autoComplete="off" registration={form.register("name")} error={errors.name} />
        <CheckboxField label="Active" description="Turn this off to retire the type. It stays on this list, and it can be made active again."
          registration={form.register("active")} error={errors.active} />
        {unexpected && <p role="alert" className="text-sm text-destructive">The leave type could not be saved. Check your connection and try again.</p>}
        <div className="flex gap-2">
          <Button type="submit" disabled={mutation.isPending}>{row ? "Save leave type" : "Add leave type"}</Button>
          {row && <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>}
        </div>
      </FieldGroup>
    </form>
  )
}

function LeaveTypesPage() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  const types = useListLeaveTypes()
  const { editing, start, finish } = useEditing<LeaveTypeSchema>()
  const [done, setDone] = useState("")
  const mayChange = user.role === Role.cno

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4">
      <h1 className="text-xl font-medium">Leave types</h1>
      <p className="text-muted-foreground">
        The reasons a unit manager can record for a leave. Every type counts the same for a nurse's hours.
        {!mayChange && " Only the CNO can change these."}
      </p>
      {types.isPending && <p>Loading…</p>}
      {types.isError && <p role="alert">The leave types could not be read. Reload the page to try again.</p>}
      {/* Always rendered, because a live region mounted with its text may never be read out. */}
      <p role="status" className="text-sm text-muted-foreground empty:sr-only">{done}</p>
      {types.data && (
        <Card>
          <CardContent>
            <Table container={mayChange ? undefined : scrollableRegion("Leave types")}>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Status</TableHead>
                  {mayChange && <TableHead className="sticky end-0 bg-card"><span className="sr-only">Actions</span></TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
                {types.data.map((row) => (
                  <TableRow key={row.id}>
                    <TableCell>{row.name}</TableCell>
                    <TableCell><Badge variant={row.active ? "secondary" : "outline"}>{row.active ? "Active" : "Retired"}</Badge></TableCell>
                    {mayChange && (
                      <TableCell className="sticky end-0 bg-card text-end">
                        <Button variant="outline" size="sm" aria-label={`Edit ${row.name}`}
                          onClick={(event) => { setDone(""); start(row, event.currentTarget) }}>
                          Edit
                        </Button>
                      </TableCell>
                    )}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
      {mayChange && types.data && (
        <Card className="max-w-xl">
          <CardHeader><CardTitle><h2>{editing ? `Edit ${editing.name}` : "Add a leave type"}</h2></CardTitle></CardHeader>
          <CardContent>
            <LeaveTypeForm key={editing?.id ?? "new"} row={editing} onCancel={finish}
              onDone={(message) => { setDone(message); finish() }} />
          </CardContent>
        </Card>
      )}
    </div>
  )
}
