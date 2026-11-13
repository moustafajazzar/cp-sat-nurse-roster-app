import { useQueryClient, type QueryClient } from "@tanstack/react-query"
import { redirect, useNavigate } from "@tanstack/react-router"
import { useState } from "react"

import { getGetSessionQueryKey, getGetSessionQueryOptions, useLogout } from "@/api/generated/endpoints"
import type { Me, Role, SessionState } from "@/api/generated/model"
import { turnThisDeviceOffForLogOut } from "@/lib/push"

/** Who is signed in. The answer is cached until login, logout, a password change or a 401 changes
 *  it, so a route guard costs no request. Nothing here polls: see SESSION_SAVE_EVERY_REQUEST. */
export async function currentUser(queryClient: QueryClient): Promise<Me | null> {
  const session = await queryClient.ensureQueryData(getGetSessionQueryOptions<SessionState>())
  return session.user ?? null
}

export function setCurrentUser(queryClient: QueryClient, user: Me | null) {
  queryClient.setQueryData<SessionState>(getGetSessionQueryKey(), { user })
}

export function cachedUser(queryClient: QueryClient): Me | null {
  return queryClient.getQueryData<SessionState>(getGetSessionQueryKey())?.user ?? null
}

/** A route's `beforeLoad` for a page only these roles may see. It mirrors what the API enforces, so
 *  someone who follows an old link lands on their home instead of on a page of refusals. */
export function onlyFor(...roles: Role[]) {
  return ({ context }: { context: { user: Me } }) => {
    if (!roles.includes(context.user.role)) throw redirect({ to: "/" })
  }
}

export function useSignOut() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const logout = useLogout()
  const [leaving, setLeaving] = useState(false)
  return {
    pending: leaving || logout.isPending,
    // This device's pushes end first, and signing out goes on whatever that answered. A session that expires by
    // itself ends none, since reaching someone who is not using the app is the point.
    run: async () => {
      setLeaving(true)
      await turnThisDeviceOffForLogOut()
      setLeaving(false)
      logout.mutate(undefined, {
        onSuccess: () => {
          queryClient.clear() // what the last user saw must not outlive their session
          setCurrentUser(queryClient, null)
          void navigate({ to: "/login" })
        },
      })
    },
  }
}
