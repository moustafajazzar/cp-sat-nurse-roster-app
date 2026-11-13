import { zodResolver } from "@hookform/resolvers/zod"
import { useQueryClient } from "@tanstack/react-query"
import { createFileRoute, useRouteContext } from "@tanstack/react-router"
import { useEffect, useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { getListSkillsQueryKey, useCreateSkill, useListSkills, useUpdateSkill } from "@/api/generated/endpoints"
import { Role, type SkillBody, type SkillSchema } from "@/api/generated/model"
import { createSkillBodyIssuingBodyMax as ISSUING_BODY_MAX, createSkillBodyNameMax as NAME_MAX } from "@/api/generated/zod"
import { CheckboxField, FormField, SelectField } from "@/components/form-field"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { FieldGroup } from "@/components/ui/field"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { serverErrors, showServerErrors } from "@/lib/server-errors"
import { byKind, Kind, KINDS } from "@/lib/skill-kinds"
import { useEditing } from "@/lib/use-editing"
import { scrollableRegion } from "@/lib/utils"
import { useFocusOnOpen } from "@/lib/use-focus-on-open"

/** Open to every role, which may all read the catalogue: it names no one. Only the CNO gets the form. */
export const Route = createFileRoute("/_signed-in/skills")({
  staticData: { title: "Skills" },
  component: SkillsPage,
})

const schema = z.object({
  name: z.string().trim().min(1, "Enter a name.").max(NAME_MAX, `At most ${NAME_MAX} characters.`),
  kind: z.enum(Kind),
  issuing_body: z.string().trim().max(ISSUING_BODY_MAX, `At most ${ISSUING_BODY_MAX} characters.`),
  expires: z.boolean(),
  required_to_work: z.boolean(),
  lets_a_nurse_precept: z.boolean(),
  active: z.boolean(),
})

const NEW_SKILL: SkillBody = { name: "", kind: Kind.skill, issuing_body: "", expires: false, required_to_work: false, lets_a_nurse_precept: false, active: true }

function SkillForm({ skill, onDone, onCancel }: { skill: SkillSchema | null; onDone: (message: string) => void; onCancel: () => void }) {
  const queryClient = useQueryClient()
  const create = useCreateSkill()
  const update = useUpdateSkill()
  const mutation = skill ? update : create
  const form = useForm<SkillBody>({
    resolver: zodResolver(schema),
    defaultValues: skill
      ? { name: skill.name, kind: skill.kind, issuing_body: skill.issuing_body, expires: skill.expires, required_to_work: skill.required_to_work, lets_a_nurse_precept: skill.lets_a_nurse_precept, active: skill.active }
      : NEW_SKILL,
  })
  const { errors } = form.formState
  // A licence always expires: the flag is held on and fixed while the kind is licence, and freed as it was left after.
  const licence = form.watch("kind") === Kind.licence
  useEffect(() => {
    if (licence) form.setValue("expires", true)
  }, [licence, form])

  useFocusOnOpen(() => {
    if (skill) form.setFocus("name")
  }, [skill, form])

  const saved = (message: string) => {
    void queryClient.invalidateQueries({ queryKey: getListSkillsQueryKey() })
    onDone(message)
  }
  const refused = (error: unknown) => showServerErrors(form, error)

  const submit = form.handleSubmit((data) => {
    if (skill) {
      update.mutate({ skillId: skill.id, data }, { onSuccess: (row) => saved(`${row.name} is saved.`), onError: refused })
    } else {
      create.mutate({ data }, {
        onSuccess: (row) => {
          form.reset(NEW_SKILL)
          saved(`${row.name} is added.`)
        },
        onError: refused,
      })
    }
  })
  const unexpected = mutation.isError && serverErrors(mutation.error).length === 0

  return (
    <form onSubmit={submit} noValidate>
      <FieldGroup>
        <FormField label="Name" autoComplete="off" spellCheck={false}
          description={skill ? "The old name is recorded, so what was saved under it still finds this skill." : undefined}
          registration={form.register("name")} error={errors.name} />
        <SelectField label="Kind"
          description="A skill is an ability, such as triage. A certificate attests something a nurse has earned, such as a degree or a course. A licence permits a nurse to practise and always expires."
          registration={form.register("kind")} error={errors.kind}>
          {KINDS.map(({ kind, one }) => <option key={kind} value={kind}>{one}</option>)}
        </SelectField>
        <FormField label="Issuing body" autoComplete="off"
          description="Who issues it, such as the Saudi Commission for Health Specialties or a university. Leave it empty when no one does."
          registration={form.register("issuing_body")} error={errors.issuing_body} />
        <CheckboxField label="Expires" disabled={licence}
          description={`${licence ? "A licence always expires. " : ""}Every nurse who holds this skill has an expiry for it, the last day it counts.`}
          registration={form.register("expires")} error={errors.expires} />
        <CheckboxField label="Required to work" description="From the day after its expiry, a nurse holding this skill works no shift."
          registration={form.register("required_to_work")} error={errors.required_to_work} />
        {/* The mark is checked when a preceptor is assigned, and only then. */}
        <CheckboxField label="Lets a nurse precept"
          description="A nurse holding this skill can be assigned as the preceptor of a nurse on probation, unless it has expired."
          registration={form.register("lets_a_nurse_precept")} error={errors.lets_a_nurse_precept} />
        <CheckboxField label="Active" description="Turn this off to retire the skill. It stays on this list, the nurses who hold it keep it, and it can be made active again."
          registration={form.register("active")} error={errors.active} />
        {unexpected && <p role="alert" className="text-sm text-destructive">The skill could not be saved. Check your connection and try again.</p>}
        <div className="flex gap-2">
          <Button type="submit" disabled={mutation.isPending}>{skill ? "Save skill" : "Add skill"}</Button>
          {skill && <Button type="button" variant="outline" onClick={onCancel}>Cancel</Button>}
        </div>
      </FieldGroup>
    </form>
  )
}

function FormerNames({ skill }: { skill: SkillSchema }) {
  if (skill.renames.length === 0) return null
  return (
    <ul className="text-xs text-muted-foreground">
      {skill.renames.map((rename) => <li key={rename.at}>Was {rename.old_name} until {rename.at.slice(0, 10)}</li>)}
    </ul>
  )
}

const yesNo = (value: boolean) => (value ? "Yes" : "No")

const COLUMNS = ["Name", "Issuing body", "Expires", "Required to work", "Lets a nurse precept", "Status"]

function SkillsTable({ skills, onEdit }: { skills: SkillSchema[]; onEdit?: (skill: SkillSchema, button: HTMLElement) => void }) {
  return (
    // Nothing else in a reader's table can take focus, and a keyboard still has to scroll it.
    <Table container={onEdit ? undefined : scrollableRegion("Skills")}>
      <TableHeader>
        <TableRow>
          {COLUMNS.map((column) => <TableHead key={column}>{column}</TableHead>)}
          {onEdit && <TableHead className="sticky end-0 bg-card"><span className="sr-only">Actions</span></TableHead>}
        </TableRow>
      </TableHeader>
      {byKind(skills, (skill) => skill.kind).map((group) => (
        <TableBody key={group.kind} aria-labelledby={`skills-${group.kind}`}>
          <TableRow className="hover:bg-transparent">
            <TableHead id={`skills-${group.kind}`} scope="rowgroup" colSpan={COLUMNS.length + (onEdit ? 1 : 0)}
              className="bg-muted/50 font-medium text-foreground">{group.heading}</TableHead>
          </TableRow>
          {group.rows.map((skill) => (
            <TableRow key={skill.id}>
              <TableCell>
                <span className="font-medium">{skill.name}</span>
                <FormerNames skill={skill} />
              </TableCell>
              {/* A body's name may be long: it wraps rather than pushing the marks out of sight. */}
              <TableCell className="min-w-40 whitespace-normal">{skill.issuing_body}</TableCell>
              <TableCell>{yesNo(skill.expires)}</TableCell>
              <TableCell>{yesNo(skill.required_to_work)}</TableCell>
              <TableCell>{yesNo(skill.lets_a_nurse_precept)}</TableCell>
              <TableCell>
                <Badge variant={skill.active ? "secondary" : "outline"}>{skill.active ? "Active" : "Retired"}</Badge>
              </TableCell>
              {onEdit && (
                <TableCell className="sticky end-0 bg-card text-end">
                  <Button variant="outline" size="sm" aria-label={`Edit ${skill.name}`} onClick={(event) => onEdit(skill, event.currentTarget)}>Edit</Button>
                </TableCell>
              )}
            </TableRow>
          ))}
        </TableBody>
      ))}
    </Table>
  )
}

function SkillsPage() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  const skills = useListSkills()
  const { editing, start, finish } = useEditing<SkillSchema>()
  const [done, setDone] = useState("")
  const mayChange = user.role === Role.cno

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4">
      <h1 className="text-xl font-medium">Skills</h1>
      <p className="text-muted-foreground">
        The skills a nurse may hold and a unit's skill needs may count. A skill that lets a nurse precept is checked only when a preceptor is assigned, so turning that off later changes no preceptor already assigned.
        {!mayChange && " Only the CNO can change these."}
      </p>
      {skills.isPending && <p>Loading…</p>}
      {skills.isError && <p role="alert">The skills could not be read. Reload the page to try again.</p>}
      {/* Always rendered, because a live region mounted with its text may never be read out. */}
      <p role="status" className="text-sm text-muted-foreground empty:sr-only">{done}</p>
      {skills.data && skills.data.length === 0 && <p>No skills yet.</p>}
      {skills.data && skills.data.length > 0 && (
        <Card>
          <CardContent>
            <SkillsTable skills={skills.data} onEdit={mayChange ? (skill, button) => { setDone(""); start(skill, button) } : undefined} />
          </CardContent>
        </Card>
      )}
      {mayChange && skills.data && (
        <Card className="max-w-xl">
          <CardHeader><CardTitle><h2>{editing ? `Edit ${editing.name}` : "Add a skill"}</h2></CardTitle></CardHeader>
          <CardContent>
            <SkillForm key={editing?.id ?? "new"} skill={editing} onCancel={finish}
              onDone={(message) => { setDone(message); finish() }} />
          </CardContent>
        </Card>
      )}
    </div>
  )
}
