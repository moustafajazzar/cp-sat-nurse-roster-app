import type { ReactNode } from "react"
import { z } from "zod"

import { buttonVariants } from "@/components/ui/button"
import { cn, monthAfter, monthName } from "@/lib/utils"

/** A month in the address. A hand-edited one is dropped rather than sent: the API would refuse it. */
export const monthInAddress = z.string().regex(/^\d{4}-(0[1-9]|1[0-2])$/).optional().catch(undefined)
/** A day in a page's address, "2026-06-04": one that is not a real date is left out, as a missing one is. */
export const dateInAddress = z.string().regex(/^\d{4}-\d{2}-\d{2}$/)
  .refine((iso) => {
    const day = new Date(`${iso}T00:00:00Z`)
    return !Number.isNaN(day.getTime()) && day.toISOString().startsWith(iso)
  })
  .optional().catch(undefined)

/** How a link looks here, for a page's `link`. */
export const MONTH_LINK = cn(buttonVariants({ variant: "outline", size: "sm" }))

/** The month a page shows, with links either side, so the month stays in the address and focus stays on the link. The
 *  page draws its own links, because only it knows its route and what else its address holds. */
export function MonthNav({ month, link }: { month: string; link: (month: string, label: string) => ReactNode }) {
  // On a phone the month is above and the two links share the row beneath, each half of it; wider, one row. Only the heading
  // moves, and it takes no focus, so the order of the links is the same either way.
  return (
    <nav aria-label="Month" className="grid grid-cols-2 items-center gap-2 sm:flex sm:flex-wrap sm:gap-x-3">
      <div className="max-sm:*:w-full">{link(monthAfter(month, -1), "Previous month")}</div>
      <h2 className="col-span-2 text-base font-medium tabular-nums max-sm:order-first">{monthName(month)}</h2>
      <div className="col-start-2 max-sm:*:w-full">{link(monthAfter(month, 1), "Next month")}</div>
    </nav>
  )
}
