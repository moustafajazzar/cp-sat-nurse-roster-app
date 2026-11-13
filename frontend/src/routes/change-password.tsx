import { zodResolver } from "@hookform/resolvers/zod"
import { useQueryClient } from "@tanstack/react-query"
import { createFileRoute, Link, redirect, useNavigate, useRouteContext } from "@tanstack/react-router"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { ApiError } from "@/api/fetcher"
import { useChangePassword } from "@/api/generated/endpoints"
import type { ChangePasswordRefusal, ThrottleRefusal } from "@/api/generated/model"
import { FormField } from "@/components/form-field"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { FieldGroup } from "@/components/ui/field"
import { throttledWords } from "@/lib/throttle-words"
import { currentUser, setCurrentUser, useSignOut } from "@/session"

export const Route = createFileRoute("/change-password")({
  beforeLoad: async ({ context }) => {
    const user = await currentUser(context.queryClient)
    if (!user) throw redirect({ to: "/login" })
    return { forced: user.must_change_password }
  },
  staticData: { title: "Change password" },
  component: ChangePasswordPage,
})

const schema = z
  .object({
    current_password: z.string().min(1, "Enter your current password."),
    new_password: z.string().min(10, "Use at least 10 characters."),
    repeat: z.string(),
  })
  .refine((data) => data.new_password === data.repeat, {
    path: ["repeat"],
    message: "The two passwords differ.",
  })

function ChangePasswordPage() {
  const { forced } = useRouteContext({ from: "/change-password" })
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const change = useChangePassword()
  const signOut = useSignOut()
  const form = useForm({
    resolver: zodResolver(schema),
    defaultValues: { current_password: "", new_password: "", repeat: "" },
  })
  const { errors } = form.formState

  const submit = form.handleSubmit(({ current_password, new_password }) =>
    change.mutate(
      { data: { current_password, new_password } },
      {
        onSuccess: (me) => {
          setCurrentUser(queryClient, me)
          void navigate({ to: "/" })
        },
        onError: (error) => {
          if (error instanceof ApiError && error.status === 400) {
            const refusal = error.body as ChangePasswordRefusal
            for (const field of ["current_password", "new_password"] as const) {
              const messages = refusal[field]
              if (messages?.length) form.setError(field, { message: messages.join(" ") }, { shouldFocus: true })
            }
          } else if (error instanceof ApiError && error.status === 429) {
            form.setError("root", { message: throttledWords(error.body as ThrottleRefusal | undefined) })
          } else {
            form.setError("root", { message: "The password could not be changed. Try again." })
          }
        },
      },
    ),
  )

  return (
    <main className="flex min-h-svh items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle><h1>{forced ? "Choose a new password" : "Change your password"}</h1></CardTitle>
          {forced && <CardDescription>You must replace the password you were given before going on.</CardDescription>}
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} noValidate>
            <FieldGroup>
              <FormField label="Current password" type="password" autoComplete="current-password" autoFocus
                registration={form.register("current_password")} error={errors.current_password} />
              <FormField label="New password" type="password" autoComplete="new-password"
                description="At least 10 characters, not a common password and not like your name."
                registration={form.register("new_password")} error={errors.new_password} />
              <FormField label="Repeat the new password" type="password" autoComplete="new-password"
                registration={form.register("repeat")} error={errors.repeat} />
              {errors.root && <p role="alert" className="text-sm text-destructive">{errors.root.message}</p>}
              <Button type="submit" disabled={change.isPending}>Change password</Button>
              {forced ? (
                <Button type="button" variant="ghost" onClick={signOut.run} disabled={signOut.pending}>Log out</Button>
              ) : (
                <Link to="/" className="flex items-center justify-center text-sm text-muted-foreground hover:text-foreground pointer-coarse:min-h-11">Back to home</Link>
              )}
            </FieldGroup>
          </form>
        </CardContent>
      </Card>
    </main>
  )
}
