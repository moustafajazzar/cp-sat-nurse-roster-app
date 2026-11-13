import type { ComponentProps, ReactNode } from "react"
import type { FieldError as HookFormError, UseFormRegisterReturn } from "react-hook-form"

import { Field, FieldContent, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { NativeSelect } from "@/components/ui/native-select"
import { cn } from "@/lib/utils"

type FormFieldProps = {
  label: string
  description?: ReactNode
  registration: UseFormRegisterReturn
  /** Client-side validation's error for this field. */
  error?: HookFormError
  /** Something else on the page that the field is read with, by its id. */
  describedBy?: string
} & Omit<ComponentProps<typeof Input>, "name" | "onChange" | "onBlur" | "ref" | "aria-describedby">

/** A labelled input wired to React Hook Form, with its description and its error. Give it an `id` when another form on
 *  the page has a field of the same name. */
export function FormField({ label, description, registration, error, describedBy, id: given, ...input }: FormFieldProps) {
  const id = given ?? `field-${registration.name}`
  const descriptionId = description ? `${id}-description` : undefined
  const errorId = error ? `${id}-error` : undefined
  return (
    <Field data-invalid={!!error || undefined}>
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      <Input id={id} aria-invalid={!!error || undefined}
        aria-describedby={[errorId, descriptionId, describedBy].filter(Boolean).join(" ") || undefined}
        {...input} {...registration} />
      {description && <FieldDescription id={descriptionId}>{description}</FieldDescription>}
      {/* Tied to its input and read when the input takes focus, so not an alert of its own. */}
      <FieldError id={errorId} role={undefined} errors={error ? [error] : undefined} />
    </Field>
  )
}

type SelectFieldProps = {
  label: string
  description?: ReactNode
  registration: UseFormRegisterReturn
  error?: HookFormError
  children: ReactNode
}

/** The same for a native select, which is what a phone handles best. */
export function SelectField({ label, description, registration, error, children }: SelectFieldProps) {
  const id = `field-${registration.name}`
  const descriptionId = description ? `${id}-description` : undefined
  const errorId = error ? `${id}-error` : undefined
  return (
    <Field data-invalid={!!error || undefined}>
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      <NativeSelect id={id} aria-invalid={!!error || undefined}
        aria-describedby={[errorId, descriptionId].filter(Boolean).join(" ") || undefined} {...registration}>
        {children}
      </NativeSelect>
      {description && <FieldDescription id={descriptionId}>{description}</FieldDescription>}
      <FieldError id={errorId} role={undefined} errors={error ? [error] : undefined} />
    </Field>
  )
}

export const CHECKBOX = "size-4 shrink-0 accent-primary outline-none focus-visible:ring-3 focus-visible:ring-ring"

type CheckboxFieldProps = {
  label: string
  description?: ReactNode
  registration: UseFormRegisterReturn
  error?: HookFormError
  /** Shown but fixed; the form keeps the value it holds. */
  disabled?: boolean
}

/** A switch for a rule. Native, so a phone and a screen reader get what they already know. */
export function CheckboxField({ label, description, registration, error, disabled }: CheckboxFieldProps) {
  const id = `field-${registration.name}`
  const descriptionId = description ? `${id}-description` : undefined
  const errorId = error ? `${id}-error` : undefined
  return (
    <Field orientation="horizontal" data-invalid={!!error || undefined}>
      <input id={id} type="checkbox" aria-invalid={!!error || undefined}
        aria-describedby={[errorId, descriptionId].filter(Boolean).join(" ") || undefined}
        className={cn(CHECKBOX, "mt-0.5 disabled:cursor-not-allowed disabled:opacity-50")} {...registration} disabled={disabled} />
      <FieldContent>
        <FieldLabel htmlFor={id}>{label}</FieldLabel>
        {description && <FieldDescription id={descriptionId}>{description}</FieldDescription>}
        <FieldError id={errorId} role={undefined} errors={error ? [error] : undefined} />
      </FieldContent>
    </Field>
  )
}
