import { zodResolver } from "@hookform/resolvers/zod"
import { useQueryClient } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import { useRef, useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import {
  getListUnitManagersQueryKey,
  getListUnitsQueryKey,
  useArchiveUnit,
  useCreateUnit,
  useListUnitManagers,
  useListUnits,
  useUpdateUnit,
} from "@/api/generated/endpoints"
import { Role, type ManagerChoice, type UnitSchema } from "@/api/generated/model"
import { createUnitBodyNameMax as NAME_MAX } from "@/api/generated/zod"
import { FormField, SelectField } from "@/components/form-field"
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { FieldGroup } from "@/components/ui/field"
import { NativeSelectOption } from "@/components/ui/native-select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { serverErrors, showServerErrors } from "@/lib/server-errors"
import { useEditing } from "@/lib/use-editing"
import { useFocusOnOpen } from "@/lib/use-focus-on-open"
import { onlyFor } from "@/session"

export const Route = createFileRoute("/_signed-in/units")({
  beforeLoad: onlyFor(Role.cno),
  staticData: { title: "Units" },
  component: UnitsPage,
})

const schema = z.object({
  name: z.string().trim().min(1, "Enter a name.").max(NAME_MAX, `At most ${NAME_MAX} characters.`),
  manager_id: z.string(), // Empty for none, since a select's value is text.
})
type Fields = z.infer<typeof schema>

const NEW_UNIT: Fields = { name: "", manager_id: "" }

/** A change to a unit also changes what each manager holds. */
function useRefreshUnits() {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: getListUnitsQueryKey() })
    void queryClient.invalidateQueries({ queryKey: getListUnitManagersQueryKey() })
  }
}

function UnitForm({ row, managers, onDone, onCancel }: {
  row: UnitSchema | null
  managers: ManagerChoice[]
  onDone: (message: string) => void
  onCancel: () => void
}) {
  const refresh = useRefreshUnits()
  const create = useCreateUnit()
  const update = useUpdateUnit()
  const mutation = row ? update : create
  const form = useForm<Fields>({
    resolver: zodResolver(schema),
    defaultValues: row ? { name: row.name, manager_id: row.manager?.id ?? "" } : NEW_UNIT,
  })
  const { errors } = form.formState

  useFocusOnOpen(() => {
    if (row) form.setFocus("name")
  }, [row, form])

  const saved = (message: string) => {
    refresh()
    onDone(message)
  }
  const refused = (error: unknown) => showServerErrors(form, error)

  const submit = form.handleSubmit(({ name, manager_id }) => {
    const data = { name, manager_id: manager_id || null }
    if (row) {
      update.mutate({ unitId: row.id, data }, { onSuccess: (next) => saved(`${next.name} is saved.`), onError: refused })
    } else {
      create.mutate({ data }, {
        onSuccess: (next) => {
          form.reset(NEW_UNIT)
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
        <SelectField label="Manager" registration={form.register("manager_id")} error={errors.manager_id}
          description={managers.length === 0 ? "The System Admin creates unit manager accounts. You can add the unit now and assign its manager later." : undefined}>
          <NativeSelectOption value="">No manager yet</NativeSelectOption>
          {managers.map((manager) => {
            const holdsAnother = manager.unit !== null && manager.id !== row?.manager?.id
            return (
              <NativeSelectOption key={manager.id} value={manager.id} disabled={holdsAnother}>
                {manager.full_name} ({manager.username}{holdsAnother ? `, manages ${manager.unit}` : ""})
              </NativeSelectOption>
            )
          })}
        </SelectField>
        {unexpected && <p role="alert" className="text-sm text-destructive">The unit could not be saved. Check your connection and try again.</p>}
        <div className="flex gap-2">
          <Button type="submit" disabled={mutation.isPending}>{row ? "Save unit" : "Add unit"}</Button>
          {row && <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>}
        </div>
      </FieldGroup>
    </form>
  )
}

/** Cancelling returns focus to the row's button. Archiving takes that button away, so focus goes to where
 *  the next unit is entered instead. */
function ArchiveDialog({ unit, onCancel, onArchived }: { unit: UnitSchema | null; onCancel: () => void; onArchived: (unit: UnitSchema) => void }) {
  const refresh = useRefreshUnits()
  const archive = useArchiveUnit()
  const archived = useRef(false)
  const refusal = serverErrors(archive.error)[0]?.message

  function confirm() {
    if (!unit) return
    archive.mutate({ unitId: unit.id }, {
      onSuccess: () => {
        refresh()
        archived.current = true
        onArchived(unit)
      },
    })
  }

  function cancel() {
    archive.reset()
    onCancel()
  }

  return (
    <AlertDialog open={unit !== null} onOpenChange={(open) => { if (!open && !archive.isPending) cancel() }}>
      <AlertDialogContent finalFocus={() => {
        if (!archived.current) return true
        archived.current = false
        return document.getElementById("field-name")
      }}>
        <AlertDialogHeader>
          <AlertDialogTitle>Archive {unit?.name}?</AlertDialogTitle>
          <AlertDialogDescription>
            A unit with no nurses can be archived. It stays on this list and cannot be brought back, but its name can be used again.
            {unit?.manager && ` ${unit.manager.full_name} can then hold another unit.`}
          </AlertDialogDescription>
        </AlertDialogHeader>
        {archive.isError && <p role="alert" className="text-sm text-destructive">{refusal ?? "That did not work. Check your connection and try again."}</p>}
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <Button variant="destructive" disabled={archive.isPending} onClick={confirm}>Archive</Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

function UnitsPage() {
  const units = useListUnits()
  const managers = useListUnitManagers()
  const { editing, start, finish, drop } = useEditing<UnitSchema>()
  const [done, setDone] = useState("")
  const [asked, setAsked] = useState<UnitSchema | null>(null)

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4">
      <h1 className="text-xl font-medium">Units</h1>
      <p className="text-muted-foreground">Each unit has one manager, and a manager holds one unit. A unit with no manager waits here until you assign one.</p>
      {(units.isPending || managers.isPending) && <p>Loading…</p>}
      {(units.isError || managers.isError) && <p role="alert">The units could not be read. Reload the page to try again.</p>}
      {/* Always rendered, because a live region mounted with its text may never be read out. */}
      <p role="status" className="text-sm text-muted-foreground empty:sr-only">{done}</p>
      {units.data && managers.data && (
        <>
          <Card>
            <CardContent>
              {units.data.length === 0 ? <p>There are no units yet. Add the first one below.</p> : (
                // Unlike the users' table, a row's actions do not stay in view on a narrow screen: two buttons
                // would cover the name and the manager, which are what the row is for.
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Name</TableHead>
                      <TableHead>Manager</TableHead>
                      <TableHead><span className="sr-only">Actions</span></TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {units.data.map((row) => (
                      <TableRow key={row.id}>
                        <TableCell>{row.name}</TableCell>
                        <TableCell>
                          {row.archived && <Badge variant="outline">Archived</Badge>}
                          {!row.archived && !row.manager && <Badge variant="outline">No manager</Badge>}
                          {row.manager && <>{row.manager.full_name} <span className="text-muted-foreground" translate="no">{row.manager.username}</span></>}
                        </TableCell>
                        <TableCell>
                          {!row.archived && (
                            <div className="flex justify-end gap-2">
                              <Button variant="outline" size="sm" aria-label={`Edit ${row.name}`}
                                onClick={(event) => { setDone(""); start(row, event.currentTarget) }}>
                                Edit
                              </Button>
                              <Button variant="outline" size="sm" aria-label={`Archive ${row.name}`}
                                onClick={() => { setDone(""); setAsked(row) }}>
                                Archive
                              </Button>
                            </div>
                          )}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
          <Card className="max-w-xl">
            <CardHeader><CardTitle><h2>{editing ? `Edit ${editing.name}` : "Add a unit"}</h2></CardTitle></CardHeader>
            <CardContent>
              <UnitForm key={editing?.id ?? "new"} row={editing} managers={managers.data} onCancel={finish}
                onDone={(message) => { setDone(message); finish() }} />
            </CardContent>
          </Card>
        </>
      )}
      <ArchiveDialog unit={asked} onCancel={() => setAsked(null)} onArchived={(unit) => {
        setDone(`${unit.name} is archived.`)
        if (editing?.id === unit.id) drop()
        setAsked(null)
      }} />
    </div>
  )
}
