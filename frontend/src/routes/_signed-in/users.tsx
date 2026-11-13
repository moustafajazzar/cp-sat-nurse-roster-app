import { zodResolver } from "@hookform/resolvers/zod"
import { useQueryClient } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import { useState } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import { ApiError } from "@/api/fetcher"
import {
  getListUsersQueryKey,
  useCorrectUserName,
  useCreateUser,
  useDeactivateUser,
  useListUsers,
  useReactivateUser,
  useResetUserPassword,
} from "@/api/generated/endpoints"
import {
  NewUserBodyRole,
  Role,
  type ManagedUser,
  type NewUserRefusal,
  type ReactivateRefusal,
} from "@/api/generated/model"
import {
  createUserBodyFullNameMax as FULL_NAME_MAX,
  createUserBodyUsernameMax as USERNAME_MAX,
  createUserBodyUsernameRegExp as USERNAME,
} from "@/api/generated/zod"
import { FormField, SelectField } from "@/components/form-field"
import { LeavePageGuard, PasswordDialog, type Shown } from "@/components/password-dialog"
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { FieldGroup } from "@/components/ui/field"
import { NativeSelectOption } from "@/components/ui/native-select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useAwaitingPassword } from "@/lib/use-awaiting-password"
import { serverErrors, showServerErrors } from "@/lib/server-errors"
import { useEditing } from "@/lib/use-editing"
import { hospitalTime, scrollableRegion } from "@/lib/utils"
import { useFocusOnOpen } from "@/lib/use-focus-on-open"
import { ROLE_NAMES } from "@/roles"
import { onlyFor } from "@/session"

export const Route = createFileRoute("/_signed-in/users")({
  beforeLoad: onlyFor(Role.system_admin),
  staticData: { title: "Users" },
  component: UsersPage,
})

const fullName = z.string().trim().min(1, "Enter their full name.").max(FULL_NAME_MAX, `At most ${FULL_NAME_MAX} characters.`)

const schema = z.object({
  role: z.enum(NewUserBodyRole, "Choose a role."),
  full_name: fullName,
  username: z
    .string()
    .trim()
    .min(1, "Enter a username.")
    .max(USERNAME_MAX, `At most ${USERNAME_MAX} characters.`)
    .regex(USERNAME, "Use only letters, digits, dots, dashes and underscores."),
})
type NewUser = z.input<typeof schema>

function NewUserForm({ users, onCreated }: { users: ManagedUser[]; onCreated: (shown: Shown) => void }) {
  const queryClient = useQueryClient()
  const create = useCreateUser({ mutation: { gcTime: 0 } })
  const form = useForm<NewUser>({
    resolver: zodResolver(schema),
    defaultValues: { role: "" as NewUser["role"], full_name: "", username: "" },
  })
  const { errors } = form.formState

  const submit = form.handleSubmit((data) =>
    create.mutate(
      { data: data as z.output<typeof schema> },
      {
        onSuccess: ({ user, generated_password }) => {
          void queryClient.invalidateQueries({ queryKey: getListUsersQueryKey() })
          form.reset()
          onCreated({ username: user.username, full_name: user.full_name, password: generated_password })
        },
        onError: (error) => {
          if (!(error instanceof ApiError) || error.status !== 409) return
          const refusal = error.body as NewUserRefusal
          if (refusal.role?.[0]) form.setError("role", { message: refusal.role[0] }, { shouldFocus: true })
          if (refusal.username?.[0]) form.setError("username", { message: refusal.username[0] }, { shouldFocus: true })
        },
      },
    ),
  )
  const unexpected = create.isError && !(create.error instanceof ApiError && create.error.status === 409)

  function holder(role: Role) {
    return role === Role.unit_manager ? undefined : users.find((user) => user.role === role && user.is_active)
  }

  return (
    <form onSubmit={submit} noValidate>
      <FieldGroup>
        <SelectField label="Role" registration={form.register("role")} error={errors.role}>
          <NativeSelectOption value="">Choose a role</NativeSelectOption>
          {Object.values(NewUserBodyRole).map((role) => {
            const taken = holder(role)
            return (
              <NativeSelectOption key={role} value={role} disabled={!!taken}>
                {ROLE_NAMES[role]}{taken ? ` (${taken.username} holds this role)` : ""}
              </NativeSelectOption>
            )
          })}
        </SelectField>
        <FormField label="Full name" autoComplete="off" registration={form.register("full_name")} error={errors.full_name} />
        <FormField label="Username" autoComplete="off" spellCheck={false} autoCapitalize="none"
          registration={form.register("username")} error={errors.username} />
        {unexpected && <p role="alert" className="text-sm text-destructive">The user could not be created. Check your connection and try again.</p>}
        <Button type="submit" disabled={create.isPending} className="self-start">Create user</Button>
      </FieldGroup>
    </form>
  )
}

const nameSchema = z.object({ full_name: fullName })
type NameFields = z.infer<typeof nameSchema>

function NameForm({ row, onDone, onCancel }: { row: ManagedUser; onDone: (message: string) => void; onCancel: () => void }) {
  const queryClient = useQueryClient()
  const correct = useCorrectUserName()
  const form = useForm<NameFields>({ resolver: zodResolver(nameSchema), defaultValues: { full_name: row.full_name } })
  const { errors } = form.formState

  useFocusOnOpen(() => { form.setFocus("full_name") }, [form])

  const submit = form.handleSubmit((data) =>
    correct.mutate({ userId: row.id, data }, {
      onSuccess: (next) => {
        void queryClient.invalidateQueries({ queryKey: getListUsersQueryKey() })
        onDone(next.full_name === row.full_name ? `Nothing changed for ${next.username}.` : `The name of ${next.username} is now ${next.full_name}.`)
      },
      onError: (error) => showServerErrors(form, error),
    }),
  )
  const unexpected = correct.isError && serverErrors(correct.error).length === 0

  return (
    <form onSubmit={submit} noValidate>
      <FieldGroup>
        <FormField label="Full name" autoComplete="off" registration={form.register("full_name")} error={errors.full_name} />
        <p className="text-sm wrap-anywhere text-muted-foreground">
          The username <span translate="no">{row.username}</span> cannot be changed.
        </p>
        {unexpected && <p role="alert" className="text-sm text-destructive">The name could not be saved. Check your connection and try again.</p>}
        <div className="flex flex-wrap gap-2">
          <Button type="submit" disabled={correct.isPending}>Save name</Button>
          <Button type="button" variant="outline" disabled={correct.isPending} onClick={onCancel}>Cancel</Button>
        </div>
      </FieldGroup>
    </form>
  )
}

type Action = "deactivate" | "reactivate" | "reset"
type Asked = { user: ManagedUser; action: Action }

const QUESTIONS: Record<Action, { title: (name: string) => string; body: string; confirm: string; destructive: boolean }> = {
  deactivate: {
    title: (name) => `Deactivate ${name}?`,
    body: "They are logged out at once and cannot log in until they are reactivated.",
    confirm: "Deactivate",
    destructive: true,
  },
  reactivate: {
    title: (name) => `Reactivate ${name}?`,
    body: "They can log in again with the password they had.",
    confirm: "Reactivate",
    destructive: false,
  },
  reset: {
    title: (name) => `Reset the password of ${name}?`,
    body: "Their password stops working and they are logged out. You get a new one to give them, which they must change at their next login.",
    confirm: "Reset password",
    destructive: true,
  },
}

function ActionDialog({ asked, onClose, onDone, onPassword }: {
  asked: Asked | null
  onClose: () => void
  onDone: (message: string) => void
  onPassword: (shown: Shown) => void
}) {
  const queryClient = useQueryClient()
  const deactivate = useDeactivateUser()
  const reactivate = useReactivateUser()
  const reset = useResetUserPassword({ mutation: { gcTime: 0 } })
  const [refusal, setRefusal] = useState("")
  const pending = deactivate.isPending || reactivate.isPending || reset.isPending

  function close() {
    setRefusal("")
    onClose()
  }

  function failed(error: unknown) {
    setRefusal(
      error instanceof ApiError && error.status === 409
        ? (error.body as ReactivateRefusal).detail
        : "That did not work. Check your connection and try again.",
    )
  }

  function confirm() {
    if (!asked) return
    const { user, action } = asked
    const finished = (message: string) => {
      void queryClient.invalidateQueries({ queryKey: getListUsersQueryKey() })
      close()
      onDone(message)
    }
    const variables = { userId: user.id }
    if (action === "deactivate") {
      deactivate.mutate(variables, { onSuccess: () => finished(`${user.username} is deactivated.`), onError: failed })
    } else if (action === "reactivate") {
      reactivate.mutate(variables, { onSuccess: () => finished(`${user.username} is active again.`), onError: failed })
    } else {
      reset.mutate(variables, {
        onSuccess: ({ generated_password }) => {
          finished(`The password of ${user.username} is reset.`)
          onPassword({ username: user.username, full_name: user.full_name, password: generated_password })
        },
        onError: failed,
      })
    }
  }

  const question = asked && QUESTIONS[asked.action]
  return (
    <AlertDialog open={asked !== null} onOpenChange={(open) => { if (!open && !pending) close() }}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{asked && question?.title(asked.user.username)}</AlertDialogTitle>
          <AlertDialogDescription>
            {question?.body}
            {asked?.action === "deactivate" && asked.user.unit && (
              ` ${asked.user.unit} is left with no manager until the CNO assigns one. Reactivating them does not give it back.`
            )}
          </AlertDialogDescription>
        </AlertDialogHeader>
        {refusal && <p role="alert" className="text-sm text-destructive">{refusal}</p>}
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <Button variant={question?.destructive ? "destructive" : "default"} disabled={pending} onClick={confirm}>
            {question?.confirm}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

function UsersTable({ users, onAsk, onCorrect }: {
  users: ManagedUser[]
  onAsk: (asked: Asked) => void
  onCorrect: (user: ManagedUser, button: HTMLElement) => void
}) {
  const awaitingPassword = useAwaitingPassword()
  return (
    <Table container={scrollableRegion("Users")}>
      <TableHeader>
        <TableRow>
          <TableHead>Name</TableHead>
          <TableHead>Username</TableHead>
          <TableHead>Role</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Last login</TableHead>
          <TableHead className="sm:sticky sm:end-0 sm:bg-card"><span className="sr-only">Actions</span></TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {users.map((user) => (
          <TableRow key={user.id}>
            <TableCell>{user.full_name}</TableCell>
            <TableCell translate="no">{user.username}</TableCell>
            <TableCell>
              {ROLE_NAMES[user.role]}
              {user.unit && <span className="block text-muted-foreground">{user.unit}</span>}
            </TableCell>
            <TableCell>
              <div className="flex flex-wrap gap-1">
                <Badge variant={user.is_active ? "secondary" : "outline"}>{user.is_active ? "Active" : "Deactivated"}</Badge>
                {user.must_change_password && <Badge variant="outline">Password change due</Badge>}
              </div>
            </TableCell>
            <TableCell className="tabular-nums">{user.last_login ? hospitalTime(user.last_login) : "Never"}</TableCell>
            {/* Stays in view when the table scrolls sideways, from 640 px up. On a phone it would cover the names, so
                there it scrolls with the row and comes last. */}
            <TableCell className="sm:sticky sm:end-0 sm:bg-card">
              {/* Two by two, because three in a line would cover the whole row on a phone. */}
              <div className="ml-auto grid w-fit grid-cols-[auto_auto] gap-2">
                {/* Correcting a name replaces the form that is waiting for its answer. */}
                <Button variant="outline" size="sm" disabled={awaitingPassword} aria-label={`Correct name of ${user.username}`}
                  onClick={(event) => onCorrect(user, event.currentTarget)}>
                  Correct name
                </Button>
                {/* One button that changes its words, so focus survives the change. */}
                <Button variant="outline" size="sm" aria-label={`${user.is_active ? "Deactivate" : "Reactivate"} ${user.username}`}
                  onClick={() => onAsk({ user, action: user.is_active ? "deactivate" : "reactivate" })}>
                  {user.is_active ? "Deactivate" : "Reactivate"}
                </Button>
                {/* The dialog shows one password, so a second answer would replace the first. */}
                <Button variant="outline" size="sm" disabled={awaitingPassword} aria-label={`Reset password of ${user.username}`}
                  onClick={() => onAsk({ user, action: "reset" })}>
                  Reset password
                </Button>
              </div>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function UsersPage() {
  const users = useListUsers()
  const [shown, setShown] = useState<Shown | null>(null)
  const [asked, setAsked] = useState<Asked | null>(null)
  const [done, setDone] = useState("")
  const { editing, start, finish } = useEditing<ManagedUser>()
  const awaitingPassword = useAwaitingPassword()

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4">
      <h1 className="text-xl font-medium">Users</h1>
      <p className="text-muted-foreground">The CNO, the Deputy CNO and the unit managers. The CNO adds nurses. Times are hospital time.</p>
      {users.isPending && <p>Loading…</p>}
      {users.isError && <p role="alert">The users could not be read. Reload the page to try again.</p>}
      {/* Always rendered, because a live region mounted with its text may never be read out. */}
      <p role="status" className="text-sm wrap-anywhere text-muted-foreground empty:sr-only">{done}</p>
      {users.data && (
        <>
          <Card>
            <CardContent>
              {users.data.length > 0
                ? <UsersTable users={users.data} onAsk={(next) => { setDone(""); setAsked(next) }}
                    onCorrect={(user, button) => { setDone(""); start(user, button) }} />
                : <p>There are no users yet. Create the CNO first: the CNO creates the units and adds the nurses.</p>}
            </CardContent>
          </Card>
          <Card className="max-w-xl">
            <CardHeader><CardTitle><h2 className="wrap-anywhere">{editing ? `Correct the name of ${editing.username}` : "Create a user"}</h2></CardTitle></CardHeader>
            <CardContent>
              {editing
                ? <NameForm key={editing.id} row={editing} onCancel={finish} onDone={(message) => { setDone(message); finish() }} />
                : <NewUserForm users={users.data} onCreated={setShown} />}
            </CardContent>
          </Card>
        </>
      )}
      <ActionDialog asked={asked} onClose={() => setAsked(null)} onDone={setDone} onPassword={setShown} />
      <PasswordDialog shown={shown} onDone={() => setShown(null)} />
      <LeavePageGuard holding={awaitingPassword} />
    </div>
  )
}
