import { useListUnits } from "@/api/generated/endpoints"
import { Role, type Me } from "@/api/generated/model"
import { Field, FieldLabel } from "@/components/ui/field"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"

/** Which unit a page is about: the one a manager holds, or for a hospital-wide role the one named in the address, else
 *  the first live one. An old link may name a unit that is not there, which then falls back the same way. */
export function useChosenUnit(user: Me, inAddress: string | undefined) {
  const units = useListUnits()
  const list = units.data ?? []
  const chosen = user.role === Role.unit_manager
    ? list[0]
    : list.find((unit) => unit.id === inAddress) ?? list.find((unit) => !unit.archived) ?? list[0]
  return { units, chosen }
}

export function UnitPicker({ units, value, onChange }: { units: { id: string; name: string; archived: boolean }[]; value: string; onChange: (unit: string) => void }) {
  return (
    <Field className="max-w-xs">
      <FieldLabel htmlFor="chosen-unit">Unit</FieldLabel>
      <NativeSelect id="chosen-unit" value={value} onChange={(event) => onChange(event.target.value)}>
        {units.map((unit) => (
          <NativeSelectOption key={unit.id} value={unit.id}>{unit.archived ? `${unit.name} (archived)` : unit.name}</NativeSelectOption>
        ))}
      </NativeSelect>
    </Field>
  )
}
