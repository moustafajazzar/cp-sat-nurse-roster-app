import { createFileRoute, redirect } from "@tanstack/react-router"
import { z } from "zod"

import { monthInAddress } from "@/components/month-nav"

/** My leave is part of My month now. The address stays, for anyone who kept it. */
export const Route = createFileRoute("/_signed-in/my-leave")({
  validateSearch: (search) => z.object({ month: monthInAddress }).parse(search),
  beforeLoad: ({ search }) => { throw redirect({ to: "/my-month", search, replace: true }) },
})
