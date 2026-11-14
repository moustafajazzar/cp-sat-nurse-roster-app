import { Menu } from "@base-ui/react/menu"

import type { EditBody, EditorShift } from "@/api/generated/model"
import { readableOn } from "@/lib/utils"

export type EditableCellState = {
  hospitalId: string
  date: string
  /** Who and when, as a heading says it. */
  title: string
  /** The nurse's shifts this unit works, or every one when the list is empty. */
  shifts: EditorShift[]
  /** A day of leave, outside their dates, or a nurse no longer in the unit's month, as it stands now: nothing can be pinned. */
  credited: "leave" | "outside" | "gone" | null
  holdsShift: boolean
  pin: { shift: string; origin: string } | null
  pending: EditBody | null
}

const ITEM = "flex cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm outline-none select-none data-highlighted:bg-accent data-highlighted:text-accent-foreground data-disabled:opacity-60"

/** The shift-days of a cell's date on the roster in force, each of which a cover can ask one more nurse for. */
export type CoverChoices = {
  /** The unit's shifts. */
  shifts: EditorShift[]
  /** The day as a heading says it. */
  day: (date: string) => string
  onOpen: (date: string, shift: string) => void
}

/** One cell's choices, opened against the cell. Escape or a choice closes it, and focus goes back to the cell. On the roster in
 *  force it also offers a cover for each shift of the cell's date. */
export function CellEditor({ cell, anchor, onChoose, onUndo, onClose, covers }: {
  cell: EditableCellState | null
  anchor: HTMLElement | null
  onChoose: (edit: EditBody) => void
  onUndo: () => void
  onClose: () => void
  covers?: CoverChoices
}) {
  const edit = (action: EditBody["action"], shift?: string): EditBody => ({ hospital_id: cell!.hospitalId, date: cell!.date, action, ...(shift ? { shift } : {}) })
  // A request's pin is a promise to one nurse, changed on its own page where she is told; a swap's or a cover's has no such
  // page, so the manager's edit replaces it like their own.
  const fromElsewhere = cell?.pin?.origin === "request"
  return (
    <Menu.Root open={cell !== null} onOpenChange={(open) => { if (!open) onClose() }} modal={false}>
      <Menu.Portal>
        <Menu.Positioner anchor={anchor} side="bottom" align="start" sideOffset={4} className="z-50 outline-none">
          <Menu.Popup finalFocus={anchor ? { current: anchor } : true}
            className="min-w-56 rounded-md border bg-popover p-1 text-popover-foreground shadow-md outline-none">
            {cell && (
              <Menu.Group>
                <Menu.GroupLabel className="px-2 py-1.5 text-xs font-medium text-muted-foreground">{cell.title}</Menu.GroupLabel>
                {fromElsewhere && <p className="max-w-64 px-2 pb-1.5 text-xs">Pinned by an accepted {cell.pin!.origin}. Change it there.</p>}
                {cell.credited && (
                  <p className="max-w-64 px-2 pb-1.5 text-xs">
                    {cell.credited === "leave" ? "On leave this day, and leave wins over a pin."
                      : cell.credited === "outside" ? "Outside their start and end dates."
                      : "No longer in this unit's month, so they work nothing here."}
                  </p>
                )}
                {!fromElsewhere && !cell.credited && cell.shifts.map((shift) => (
                  <Menu.Item key={shift.code} className={ITEM} onClick={() => onChoose(edit("shift", shift.code))}>
                    <span aria-hidden="true" className="inline-flex size-5 items-center justify-center rounded-sm text-[10px]"
                      style={{ backgroundColor: shift.colour, color: readableOn(shift.colour) }}>{shift.code}</span>
                    Pin {shift.display_name}
                  </Menu.Item>
                ))}
                {!fromElsewhere && !cell.credited && <Menu.Item className={ITEM} onClick={() => onChoose(edit("off"))}>Pin a day off</Menu.Item>}
                {!fromElsewhere && (cell.pin || (cell.credited && cell.holdsShift)) && (
                  <Menu.Item className={ITEM} onClick={() => onChoose(edit("clear"))}>
                    {cell.credited && cell.holdsShift ? "Take the shift off this day" : "Clear the pin"}
                  </Menu.Item>
                )}
                {cell.pending && <Menu.Item className={ITEM} onClick={onUndo}>Undo this change</Menu.Item>}
                {cell.credited && !cell.holdsShift && !cell.pending && !cell.pin && (
                  <Menu.Item className={ITEM} disabled>Nothing to change here</Menu.Item>
                )}
              </Menu.Group>
            )}
            {cell && covers && covers.shifts.length > 0 && (
              <>
                <Menu.Separator className="-mx-1 my-1 h-px bg-border" />
                <Menu.Group>
                  <Menu.GroupLabel className="px-2 py-1.5 text-xs font-medium text-muted-foreground">One more nurse on {covers.day(cell.date)}</Menu.GroupLabel>
                  {covers.shifts.map((shift) => (
                    <Menu.Item key={shift.code} className={ITEM} onClick={() => covers.onOpen(cell.date, shift.code)}>
                      Open a cover for {shift.display_name}
                    </Menu.Item>
                  ))}
                </Menu.Group>
              </>
            )}
          </Menu.Popup>
        </Menu.Positioner>
      </Menu.Portal>
    </Menu.Root>
  )
}
