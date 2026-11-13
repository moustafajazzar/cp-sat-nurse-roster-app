import { zodResolver } from "@hookform/resolvers/zod"
import { useQueryClient } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import { useForm } from "react-hook-form"
import { z } from "zod"

import {
  getGetTechnicalSettingsQueryKey,
  useGetTechnicalSettings,
  useUpdateTechnicalSettings,
} from "@/api/generated/endpoints"
import { Role, type TechnicalSettingsSchema } from "@/api/generated/model"
import {
  updateTechnicalSettingsBodyBackupRetentionDaysMax as RETENTION_MAX,
  updateTechnicalSettingsBodyTimeLimitCapSecondsMax as CAP_MAX,
  updateTechnicalSettingsBodyBackupTimeRegExp as CLOCK_TIME,
} from "@/api/generated/zod"
import { FormField } from "@/components/form-field"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { FieldGroup } from "@/components/ui/field"
import { onlyFor } from "@/session"

export const Route = createFileRoute("/_signed-in/settings")({
  beforeLoad: onlyFor(Role.system_admin),
  staticData: { title: "Technical settings" },
  component: SettingsPage,
})

const clockTime = z.string().regex(CLOCK_TIME, "Enter a time, such as 02:00.")
const schema = z.object({
  time_limit_cap_seconds: z
    .int("Enter a whole number of seconds.")
    .min(1, "At least 1 second.")
    .max(CAP_MAX, `At most ${CAP_MAX} seconds.`),
  backup_time: clockTime,
  backup_retention_days: z
    .int("Enter a whole number of days.")
    .min(1, "At least 1 day.")
    .max(RETENTION_MAX, `At most ${RETENTION_MAX} days.`),
  close_time: clockTime,
})

function SettingsForm({ current }: { current: TechnicalSettingsSchema }) {
  const queryClient = useQueryClient()
  const update = useUpdateTechnicalSettings()
  const form = useForm({ resolver: zodResolver(schema), defaultValues: current })
  const { errors, isDirty } = form.formState

  const submit = form.handleSubmit((data) =>
    update.mutate(
      { data },
      {
        onSuccess: (saved) => {
          queryClient.setQueryData(getGetTechnicalSettingsQueryKey(), saved)
          form.reset(saved)
        },
      },
    ),
  )

  return (
    <form onSubmit={submit} noValidate>
      <FieldGroup>
        <FormField label="Time limit cap (seconds)" type="number"
          description="The longest time limit a unit manager may give a solve."
          registration={form.register("time_limit_cap_seconds", { valueAsNumber: true })}
          error={errors.time_limit_cap_seconds} />
        <FormField label="Backup time" type="time"
          description="Hospital time, every night."
          registration={form.register("backup_time")} error={errors.backup_time} />
        <FormField label="Backup retention (days)" type="number"
          registration={form.register("backup_retention_days", { valueAsNumber: true })}
          error={errors.backup_retention_days} />
        <FormField label="Month close time" type="time"
          description="Hospital time, on the first day after a month."
          registration={form.register("close_time")} error={errors.close_time} />
        {update.isError && <p role="alert" className="text-sm text-destructive">The settings could not be saved. Check your connection and save again.</p>}
        {/* Always rendered, because a live region mounted with its text may never be read out. */}
        <p role="status" className="text-sm text-muted-foreground empty:sr-only">
          {update.isSuccess && !isDirty ? "Saved." : ""}
        </p>
        <Button type="submit" disabled={update.isPending || !isDirty} className="self-start">Save</Button>
      </FieldGroup>
    </form>
  )
}

function SettingsPage() {
  const settings = useGetTechnicalSettings()

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4">
      <h1 className="text-xl font-medium">Technical settings</h1>
      {settings.isPending && <p>Loading…</p>}
      {settings.isError && <p role="alert">The settings could not be read. Reload the page to try again.</p>}
      {settings.data && (
        <Card>
          <CardContent>
            <SettingsForm current={settings.data} />
          </CardContent>
        </Card>
      )}
    </div>
  )
}
