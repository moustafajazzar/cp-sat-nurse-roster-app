import type { ReactNode, Ref } from "react"
import type { FieldError } from "react-hook-form"

import { CHECKBOX } from "@/components/form-field"
import { FieldDescription, FieldError as FieldErrorText, FieldLegend, FieldSet } from "@/components/ui/field"

type CodeCheckboxesProps = {
  /** Also the base of its ids, and what the form focuses to show a refusal. */
  id: string
  legend: string
  description?: ReactNode
  options: { code: string; label: string }[]
  value: string[]
  onChange: (value: string[]) => void
  error?: FieldError
  fieldRef?: Ref<HTMLFieldSetElement>
}

/** Several shifts out of a short list. The value keeps the options' order, whatever order they were ticked in. */
export function CodeCheckboxes({ id, legend, description, options, value, onChange, error, fieldRef }: CodeCheckboxesProps) {
  const toggle = (code: string, on: boolean) =>
    onChange(options.map((option) => option.code).filter((other) => (other === code ? on : value.includes(other))))
  const descriptionId = description ? `${id}-description` : undefined
  const errorId = error ? `${id}-error` : undefined
  return (
    <FieldSet ref={fieldRef} tabIndex={-1} aria-describedby={[errorId, descriptionId].filter(Boolean).join(" ") || undefined}
      className="gap-2 outline-none">
      <FieldLegend variant="label" className="mb-0">{legend}</FieldLegend>
      <div className="flex flex-wrap gap-x-4 gap-y-2">
        {options.map(({ code, label }) => (
          <label key={code} className="flex items-center gap-2 text-sm">
            <input type="checkbox" className={CHECKBOX} checked={value.includes(code)}
              onChange={(event) => toggle(code, event.target.checked)} />
            {label}
          </label>
        ))}
      </div>
      {description && <FieldDescription id={descriptionId}>{description}</FieldDescription>}
      <FieldErrorText id={errorId} errors={error ? [error] : undefined} role={undefined} />
    </FieldSet>
  )
}
