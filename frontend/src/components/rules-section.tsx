import type { ReactNode } from "react"

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { FieldDescription } from "@/components/ui/field"
import { LayoutSection, useFormLayout } from "@/components/form-layout-prototype"

/** A section of a rules page: a region named by its heading, whichever role is reading. */
export function Section({ title, description, children }: { title: string; description?: ReactNode; children: ReactNode }) {
  // PROTOTYPE (forms pattern): inside a prototype frame the variant lays the section out.
  if (useFormLayout()) return <LayoutSection title={title} description={description}>{children}</LayoutSection>
  const id = `section-${title.toLowerCase().replaceAll(" ", "-")}`
  return (
    <Card role="region" aria-labelledby={id}>
      <CardHeader>
        <CardTitle><h2 id={id}>{title}</h2></CardTitle>
        {description && <CardDescription>{description}</CardDescription>}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

/** What a role that cannot change a rule reads instead of a disabled input, which would dim what they came to read. */
export function ReadOnlyList({ items }: { items: { label: string; value: ReactNode; description?: string }[] }) {
  return (
    <dl className="flex flex-col gap-4">
      {items.map(({ label, value, description }) => (
        <div key={label} className="flex flex-col gap-0.5">
          <dt className="text-sm font-medium">{label}</dt>
          <dd className="text-sm tabular-nums">{value}</dd>
          {description && <dd><FieldDescription>{description}</FieldDescription></dd>}
        </div>
      ))}
    </dl>
  )
}

export const onOff = (on: boolean) => (on ? "On" : "Off")
