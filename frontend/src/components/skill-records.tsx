import { zodResolver } from "@hookform/resolvers/zod"
import { Link } from "@tanstack/react-router"
import { useRef, useState } from "react"
import { type FieldErrors, type UseFormRegister, useForm } from "react-hook-form"
import { z } from "zod"

import { useListSkills, useRecordNurseSkill, useRemoveNurseSkill, useRenewNurseSkill } from "@/api/generated/endpoints"
import type { NurseSkillRow } from "@/api/generated/model"
import { FormField, SelectField } from "@/components/form-field"
import { RecordsTable } from "@/components/nurse-skills"
import { Button } from "@/components/ui/button"
import { FieldGroup } from "@/components/ui/field"
import { NativeSelectOptGroup, NativeSelectOption } from "@/components/ui/native-select"
import { plainRefusal, serverErrors, showServerErrors } from "@/lib/server-errors"
import { byKind, Kind } from "@/lib/skill-kinds"
import { useFocusOnOpen } from "@/lib/use-focus-on-open"
import { shortDay } from "@/lib/utils"

/** Recording, renewing and taking off what a nurse holds: the CNO's on the Nurses page and the Deputy CNO's on Rostering
 *  attributes, with the same forms, fields and refusals, since the API's rules are the same for both. */

/** Whose records: a row of either page's list, as it refreshes. */
type NurseWithRecords = { id: string; full_name: string; skills: NurseSkillRow[] }

const COULD_NOT_SAVE = "That did not work. Check your connection and try again."
const required = (message: string) => z.string().min(1, message)

/** What an answer says when it is not about a field (a 409 explains itself), or that it did not work. */
function Unexpected({ error }: { error: unknown }) {
  return error && serverErrors(error).length === 0
    ? <p role="alert" className="text-sm text-destructive">{plainRefusal(error) ?? COULD_NOT_SAVE}</p>
    : null
}

const EXPIRY_HINT = "The last day it counts."
const IDENTIFIER_HINT = "The number or code the issuing body gave it."
const LICENCE_HINT = "A licence needs its identifier, the number or code the issuing body gave it."
const ISSUE_DATE_HINT = "When it was issued. Nothing reads it: the expiry is what a roster reads."

const needsExpiry = (name: string) => `${name} expires. Enter the last day it counts.`
const needsIdentifier = (name: string) => `${name} is a licence, so it needs its identifier.`

/** The identifier and the issue date, as the record and the renew forms both ask for them. `of` names the entry, for a form
 *  that sits among others; `licence` marks the identifier required. */
function RecordDetailFields({ register, errors, licence, of, id }: {
  register: UseFormRegister<RecordDetails>; errors: FieldErrors<RecordDetails>; licence: boolean; of?: string; id?: string
}) {
  const suffix = of ? ` of ${of}` : ""
  return (
    <>
      <FormField id={id && `identifier-${id}`} label={`Identifier${suffix}${licence ? "" : " (optional)"}`} autoComplete="off"
        aria-required={licence || undefined} translate="no" description={licence ? LICENCE_HINT : IDENTIFIER_HINT}
        registration={register("identifier")} error={errors.identifier} />
      <FormField id={id && `issue-date-${id}`} label={`Issue date${suffix} (optional)`} type="date" description={ISSUE_DATE_HINT}
        registration={register("issue_date")} error={errors.issue_date} />
    </>
  )
}

const recordDetailsSchema = z.object({ expiry: z.string(), identifier: z.string().max(100, "At most 100 characters."), issue_date: z.string() })
type RecordDetails = z.infer<typeof recordDetailsSchema>

/** What a form's fields say, as the API takes them: an empty field is none. */
const asSent = ({ identifier, issue_date }: RecordDetails) => ({ identifier: identifier.trim() || null, issue_date: issue_date || null })

/** The form that renews one record, under the table: a new expiry, identifier or issue date, or all three, as the body gave
 *  them. What it says already is sent as it is, and the API changes nothing then. */
function RenewForm({ nurse, skill, refresh, onSaid, onDone }: {
  nurse: NurseWithRecords; skill: NurseSkillRow; refresh: () => void; onSaid: (message: string) => void; onDone: () => void
}) {
  const renew = useRenewNurseSkill()
  const form = useForm<RecordDetails>({ resolver: zodResolver(recordDetailsSchema), defaultValues: {
    expiry: skill.expiry ?? "", identifier: skill.details?.identifier ?? "", issue_date: skill.details?.issue_date ?? "",
  } })
  const { errors } = form.formState
  const licence = skill.kind === Kind.licence
  useFocusOnOpen(() => form.setFocus(skill.expires ? "expiry" : "identifier"), [])

  const submit = form.handleSubmit((values) => {
    if (skill.expires && !values.expiry) {
      form.setError("expiry", { message: needsExpiry(skill.name) }, { shouldFocus: true })
      return
    }
    const sent = asSent(values)
    if (licence && !sent.identifier) {
      form.setError("identifier", { message: needsIdentifier(skill.name) }, { shouldFocus: true })
      return
    }
    const expiry = skill.expires ? values.expiry : null
    const same = expiry === skill.expiry && sent.identifier === (skill.details?.identifier ?? null)
      && sent.issue_date === (skill.details?.issue_date ?? null)
    renew.mutate({ nurseId: nurse.id, skillId: skill.skill_id, data: { expiry, ...sent } }, {
      onSuccess: () => {
        refresh()
        onSaid(same ? `Nothing changed for ${skill.name}.`
          : expiry && expiry !== skill.expiry ? `${skill.name} of ${nurse.full_name} now counts until ${shortDay(expiry)}.`
          : `${skill.name} of ${nurse.full_name} is renewed.`)
        onDone()
      },
      onError: (error) => { refresh(); showServerErrors(form, error) },
    })
  })

  return (
    <form onSubmit={submit} noValidate aria-label={`Renew ${skill.name}`} className="flex max-w-md flex-col gap-3">
      <FieldGroup>
        {skill.expires && (
          <FormField id={`expiry-${skill.skill_id}`} label={`Expiry of ${skill.name}`} type="date" description={EXPIRY_HINT}
            registration={form.register("expiry")} error={errors.expiry} />
        )}
        <RecordDetailFields register={form.register} errors={errors} licence={licence} of={skill.name} id={skill.skill_id} />
        <Unexpected error={renew.error} />
        <div className="flex flex-wrap gap-2">
          <Button type="submit" size="sm" disabled={renew.isPending} aria-label={`Save ${skill.name}`}>Save</Button>
          <Button type="button" size="sm" variant="outline" disabled={renew.isPending} aria-label={`Cancel renewing ${skill.name}`}
            onClick={onDone}>Cancel</Button>
        </div>
      </FieldGroup>
    </form>
  )
}

/** Her records, each to renew or take off. A retired skill only comes off. */
function HeldSkills({ nurse, refresh, onSaid, onGone }: {
  nurse: NurseWithRecords; refresh: () => void; onSaid: (message: string) => void; onGone: () => void
}) {
  const remove = useRemoveNurseSkill()
  const [renewing, setRenewing] = useState<string | null>(null)
  const chosen = nurse.skills.find((skill) => skill.skill_id === renewing)
  const renewButton = (skillId: string) => `renew-${nurse.id}-${skillId}`

  function takeOff(skill: NurseSkillRow) {
    remove.mutate({ nurseId: nurse.id, skillId: skill.skill_id }, {
      onSuccess: () => { refresh(); onSaid(`${skill.name} is taken off ${nurse.full_name}.`); onGone() },
      onError: () => refresh(),
    })
  }
  function closeRenewal(skillId: string) {
    setRenewing(null)
    // Its row's own button, which the form was opened with.
    requestAnimationFrame(() => document.getElementById(renewButton(skillId))?.focus())
  }

  return (
    <div className="flex flex-col gap-2">
      <RecordsTable skills={nurse.skills} label={`Skills of ${nurse.full_name}`} details
        actions={(skill) => (
          <>
            {skill.active && (
              <Button id={renewButton(skill.skill_id)} type="button" size="sm" variant="outline" disabled={remove.isPending}
                aria-expanded={renewing === skill.skill_id} aria-label={`Renew ${skill.name}`}
                onClick={() => { remove.reset(); setRenewing(renewing === skill.skill_id ? null : skill.skill_id) }}>
                Renew
              </Button>
            )}
            <Button type="button" size="sm" variant="outline" disabled={remove.isPending} aria-label={`Take off ${skill.name}`}
              onClick={() => takeOff(skill)}>Take off</Button>
          </>
        )}
      />
      {/* Under the table rather than in a row of it: a row is as wide as the table, which scrolls on a phone. */}
      {chosen?.active && (
        <section aria-labelledby={`renewing-${chosen.skill_id}`} className="flex flex-col gap-2">
          <h4 id={`renewing-${chosen.skill_id}`} className="font-medium">Renew {chosen.name}</h4>
          <RenewForm key={chosen.skill_id} nurse={nurse} skill={chosen} refresh={refresh} onSaid={onSaid} onDone={() => closeRenewal(chosen.skill_id)} />
        </section>
      )}
      <Unexpected error={remove.error} />
      {nurse.skills.some((skill) => !skill.active) && (
        <p className="text-sm text-muted-foreground">A retired skill can no longer be renewed. The record stays until you take it off.</p>
      )}
    </div>
  )
}

const newSkillSchema = recordDetailsSchema.extend({ skill_id: required("Choose a skill.") })
type NewSkill = z.infer<typeof newSkillSchema>

function RecordSkillForm({ nurse, refresh, keepsCatalogue, onSaid }: {
  nurse: NurseWithRecords; refresh: () => void; keepsCatalogue: boolean; onSaid: (message: string) => void
}) {
  const catalogue = useListSkills()
  const record = useRecordNurseSkill()
  const form = useForm<NewSkill>({ resolver: zodResolver(newSkillSchema),
    defaultValues: { skill_id: "", expiry: "", identifier: "", issue_date: "" } })
  const { errors } = form.formState
  const held = new Set(nurse.skills.map((skill) => skill.skill_id))
  const choices = (catalogue.data ?? []).filter((skill) => skill.active && !held.has(skill.id))
  const chosen = choices.find((skill) => skill.id === form.watch("skill_id"))
  const licence = chosen?.kind === Kind.licence

  const submit = form.handleSubmit((values) => {
    if (chosen?.expires && !values.expiry) {
      form.setError("expiry", { message: needsExpiry(chosen.name) }, { shouldFocus: true })
      return
    }
    const sent = asSent(values)
    if (chosen && licence && !sent.identifier) {
      form.setError("identifier", { message: needsIdentifier(chosen.name) }, { shouldFocus: true })
      return
    }
    record.mutate({ nurseId: nurse.id, data: { skill_id: values.skill_id, expiry: chosen?.expires ? values.expiry : null, ...sent } }, {
      onSuccess: () => {
        refresh()
        form.reset()
        onSaid(`${chosen?.name ?? "The skill"} is recorded for ${nurse.full_name}.`)
      },
      onError: (error) => { refresh(); showServerErrors(form, error) },
    })
  })

  if (catalogue.isPending) return <p>Loading…</p>
  if (catalogue.isError) return <p role="alert">The skill catalogue could not be read. Reload the page to try again.</p>
  if (catalogue.data.length === 0) {
    return keepsCatalogue
      ? <p>The skill catalogue is empty. <Link to="/skills" className="underline underline-offset-3">Add skills under Skills</Link></p>
      : <p>The skill catalogue is empty. The CNO adds skills, certificates and licences to it under Skills.</p>
  }
  if (choices.length === 0) return <p>{nurse.full_name} holds every skill in the catalogue.</p>
  return (
    <form onSubmit={submit} noValidate>
      <FieldGroup>
        <SelectField label="Skill" registration={form.register("skill_id")} error={errors.skill_id}>
          <NativeSelectOption value="">Choose a skill</NativeSelectOption>
          {byKind(choices, (skill) => skill.kind).map((group) => (
            <NativeSelectOptGroup key={group.kind} label={group.heading}>
              {group.rows.map((skill) => <NativeSelectOption key={skill.id} value={skill.id}>{skill.name}</NativeSelectOption>)}
            </NativeSelectOptGroup>
          ))}
        </SelectField>
        {chosen?.expires && (
          <FormField label="Expiry" type="date" description={EXPIRY_HINT} registration={form.register("expiry")} error={errors.expiry} />
        )}
        {chosen && <RecordDetailFields register={form.register} errors={errors} licence={licence} />}
        <Unexpected error={record.error} />
        <div><Button type="submit" disabled={record.isPending}>Record skill</Button></div>
      </FieldGroup>
    </form>
  )
}

/** Stays open through each change, which says what it did in the page's status line. Reads her skills from the list as it
 *  refreshes, so a renewal elsewhere on the page shows here. */
export function SkillsPanel({ nurse, refresh, keepsCatalogue, onSaid, onClose }: {
  nurse: NurseWithRecords
  /** Reads the list the page shows again, after every answer, a refusal's too, since it says the page may be out of date. */
  refresh: () => void
  /** The CNO's, who adds to an empty catalogue; the Deputy CNO only reads it. */
  keepsCatalogue: boolean
  onSaid: (message: string) => void
  onClose: () => void
}) {
  // Taking a skill off removes the buttons it was taken off with, so focus goes on to what is left to do.
  const recordHeading = useRef<HTMLHeadingElement>(null)
  return (
    <div className="flex flex-col gap-6">
      {nurse.skills.length === 0
        ? <p>No skills are recorded for {nurse.full_name}.</p>
        : <HeldSkills nurse={nurse} refresh={refresh} onSaid={onSaid} onGone={() => recordHeading.current?.focus()} />}
      <section className="flex flex-col gap-3" aria-labelledby="record-skill">
        <h3 id="record-skill" ref={recordHeading} tabIndex={-1} className="font-medium outline-none">Record a skill</h3>
        <RecordSkillForm nurse={nurse} refresh={refresh} keepsCatalogue={keepsCatalogue} onSaid={onSaid} />
      </section>
      <div><Button type="button" variant="outline" onClick={onClose}>Done</Button></div>
    </div>
  )
}
