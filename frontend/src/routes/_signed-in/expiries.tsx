import { createFileRoute, useRouteContext } from "@tanstack/react-router"

import { Role } from "@/api/generated/model"
import { ExpiriesList } from "@/components/expiries"
import { Card, CardContent } from "@/components/ui/card"
import { onlyFor } from "@/session"

/** Every unit's, for the CNO and the Deputy CNO. A unit manager reads his own unit's on his Nurses page. Nothing is
 *  sent about an expiry: this page is the reminder. */
export const Route = createFileRoute("/_signed-in/expiries")({
  beforeLoad: onlyFor(Role.cno, Role.deputy_cno),
  staticData: { title: "Expiries" },
  component: ExpiriesPage,
})

function ExpiriesPage() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  const who = user.role === Role.cno
    ? "You record and renew skills on the Nurses page, and the Deputy CNO on Rostering attributes."
    : "You record and renew skills on Rostering attributes, and the CNO on the Nurses page."
  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4">
      <h1 className="text-xl font-medium">Expiries</h1>
      <Card>
        <CardContent className="flex flex-col gap-4">
          <ExpiriesList label="Expiries" showUnit who={who} />
        </CardContent>
      </Card>
    </div>
  )
}
