import { zodResolver } from "@hookform/resolvers/zod"
import { useQueryClient } from "@tanstack/react-query"
import { createFileRoute, redirect, useNavigate, useRouter } from "@tanstack/react-router"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { ApiError } from "@/api/fetcher"
import { useLogin } from "@/api/generated/endpoints"
import type { LoginRefusal, ThrottleRefusal } from "@/api/generated/model"
import { FormField } from "@/components/form-field"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { FieldGroup } from "@/components/ui/field"
import { throttledWords } from "@/lib/throttle-words"
import { hospitalClock } from "@/lib/utils"
import { currentUser, setCurrentUser } from "@/session"

/** Only an address in this app is gone back to, so a crafted link cannot send someone elsewhere once they sign in. */
const inThisApp = (address: string) => /^\/(?![/\\])/.test(address)

const searchSchema = z.object({
  /** The session ended behind the user's back (`sessionCheck` in `router.tsx`), not by Log out. */
  ended: z.boolean().optional().catch(undefined),
  /** The page they were on then, with its search: where signing in again goes. */
  back: z.string().refine(inThisApp).optional().catch(undefined),
})

export const Route = createFileRoute("/login")({
  validateSearch: (search) => searchSchema.parse(search),
  beforeLoad: async ({ context }) => {
    if (await currentUser(context.queryClient)) throw redirect({ to: "/" })
  },
  staticData: { title: "Log in" },
  component: LoginPage,
})

const schema = z.object({
  username: z.string().min(1, "Enter your username."),
  password: z.string().min(1, "Enter your password."),
})

function refusalMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 429) return throttledWords(error.body as ThrottleRefusal | undefined)
  if (error instanceof ApiError && error.status === 423) {
    // The lockout is the name's, whether or not an account has it, so the words name no account.
    const until = (error.body as Partial<LoginRefusal> | undefined)?.locked_until
    return until
      ? `Too many failed logins. Try again after ${hospitalClock(until)}.`
      : "Too many failed logins. Try again later."
  }
  if (error instanceof ApiError && error.status === 401) return "Wrong username or password."
  return "The login could not be completed. Try again."
}

const SIGNED_OUT = "You were signed out. Changes you had not saved are lost. Sign in to carry on."

function LoginPage() {
  const { ended, back } = Route.useSearch()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const router = useRouter()
  const login = useLogin()
  const form = useForm({ resolver: zodResolver(schema), defaultValues: { username: "", password: "" } })
  const signedOut = ended && !login.isError

  const submit = form.handleSubmit((data) =>
    login.mutate(
      { data },
      {
        onSuccess: (me) => {
          setCurrentUser(queryClient, me)
          // The signed-in layout sends a forced password change where it belongs, and a page's own guard sends a role that
          // may not open it home.
          if (back) router.history.push(back)
          else void navigate({ to: "/" })
        },
      },
    ),
  )

  return (
    <main className="flex min-h-svh items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle><h1>Nurse roster</h1></CardTitle>
        </CardHeader>
        <CardContent>
          {/* Read out on arriving, and again with the field that has focus. A failed login's own alert replaces it. */}
          {signedOut && <p id="signed-out" role="alert" className="mb-4 text-sm">{SIGNED_OUT}</p>}
          <form onSubmit={submit} noValidate>
            <FieldGroup>
              <FormField label="Username" autoComplete="username" autoFocus describedBy={signedOut ? "signed-out" : undefined}
                registration={form.register("username")} error={form.formState.errors.username} />
              <FormField label="Password" type="password" autoComplete="current-password"
                registration={form.register("password")} error={form.formState.errors.password} />
              {login.isError && <p role="alert" className="text-sm text-destructive">{refusalMessage(login.error)}</p>}
              <Button type="submit" disabled={login.isPending}>Log in</Button>
            </FieldGroup>
          </form>
        </CardContent>
      </Card>
    </main>
  )
}
