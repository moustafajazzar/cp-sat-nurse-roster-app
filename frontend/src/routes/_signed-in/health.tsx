import { createFileRoute, Link } from "@tanstack/react-router"
import type { ReactNode } from "react"

import { getDownloadBackupUrl, useGetInstallationHealth } from "@/api/generated/endpoints"
import { Role, type InstallationHealth } from "@/api/generated/model"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { hospitalTime, scrollableRegion } from "@/lib/utils"
import { onlyFor } from "@/session"

export const Route = createFileRoute("/_signed-in/health")({
  beforeLoad: onlyFor(Role.system_admin),
  staticData: { title: "Health" },
  component: HealthPage,
})

export function formatBytes(bytes: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"]
  const power = Math.min(Math.floor(Math.log(Math.max(bytes, 1)) / Math.log(1024)), units.length - 1)
  const value = bytes / 1024 ** power
  return `${power === 0 || value >= 100 ? value.toFixed(0) : value.toFixed(1)} ${units[power]}`
}

function Row({ name, children }: { name: string; children: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <dt>{name}</dt>
      <dd className="text-end text-muted-foreground tabular-nums">{children}</dd>
    </div>
  )
}

function Reachability({ reachable }: { reachable: boolean }) {
  return <Badge variant={reachable ? "secondary" : "destructive"}>{reachable ? "Reachable" : "Unreachable"}</Badge>
}

const LAST_FAILURE = {
  unreachable: {
    word: "Unreachable",
    variant: "destructive",
    why: "The last one failed because the push service could not be reached, or answered that it was overloaded. If these keep coming with nothing sent, the server's firewall may be blocking the push services; the installation instructions name the hosts to allow.",
  },
  refused: {
    word: "Refused",
    variant: "destructive",
    why: "The last one failed because the push service refused the push. Expect this for a while after the push key pair is replaced, until each device opens the app again.",
  },
  gone: {
    word: "Gone",
    variant: "secondary",
    why: "The last one failed because the device's subscription no longer exists, as when notifications are turned off in its browser. It was removed, and nothing needs doing.",
  },
} as const

/** Pushes the worker sent in the last day, by attempt: two counts and a fixed word, nothing of anyone. */
function Pushes({ push }: { push: InstallationHealth["push"] }) {
  const last = push.last_failure ? LAST_FAILURE[push.last_failure] : null
  return (
    <Card>
      <CardHeader><CardTitle><h2>Pushes</h2></CardTitle></CardHeader>
      <CardContent className="flex flex-col gap-3">
        <dl className="flex flex-col gap-2">
          <Row name="Sent, last 24 hours">{push.sent}</Row>
          <Row name="Failed, last 24 hours">{push.failed}</Row>
          <Row name="Last failure">
            {last ? <Badge variant={last.variant}>{last.word}</Badge> : "None"}
          </Row>
        </dl>
        {last && <p className="text-sm">{last.why}</p>}
        <p className="text-sm text-muted-foreground">
          Every send to one device is counted, and so is each try again, so one push can fail up to four times. A push that
          fails is not sent later; its notification stays on the bell.
        </p>
      </CardContent>
    </Card>
  )
}

/** The backups the worker keeps, which only the System Admin may take: each holds the whole database. */
function Backups({ health }: { health: InstallationHealth }) {
  const { backups, backup_schedule: schedule } = health
  return (
    <Card className="sm:col-span-2">
      <CardHeader><CardTitle><h2>Backups</h2></CardTitle></CardHeader>
      <CardContent className="flex flex-col gap-3">
        <dl className="flex flex-col gap-2">
          <Row name="Last backup">{health.last_backup ? hospitalTime(health.last_backup) : "None yet"}</Row>
        </dl>
        <p className="text-sm text-muted-foreground">
          Taken every day at {schedule.time} hospital time and kept {schedule.retention_days} days, on this server's disk; change either
          in <Link to="/settings" className="underline underline-offset-3">Technical settings</Link>. A backup holds every account and every
          nurse's leave, so keep a downloaded one as safe as the server itself. Copies kept elsewhere are the hospital's to make.
        </p>
        {backups.length > 0 && (
          <Table container={scrollableRegion("Backups kept")}>
            <TableHeader>
              <TableRow>
                <TableHead>Taken (hospital time)</TableHead>
                <TableHead className="text-end">Size</TableHead>
                <TableHead><span className="sr-only">Download</span></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {backups.map((backup) => (
                <TableRow key={backup.name}>
                  <TableHead scope="row" className="font-normal tabular-nums">{hospitalTime(backup.taken_at)}</TableHead>
                  <TableCell className="text-end tabular-nums">{formatBytes(backup.size_bytes)}</TableCell>
                  <TableCell>
                    <a href={getDownloadBackupUrl(backup.name)} download className="underline underline-offset-3"
                      aria-label={`Download the backup of ${hospitalTime(backup.taken_at)}`}>Download</a>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  )
}

function HealthPage() {
  // A manual refresh and no timer: polling would keep an idle session alive for ever.
  const health = useGetInstallationHealth()

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-medium">Health</h1>
        <Button variant="outline" size="sm" onClick={() => health.refetch()} disabled={health.isFetching}>Refresh</Button>
      </div>
      {/* Always rendered, so a screen reader hears each Refresh; only the first load shows it. */}
      <p role="status" className={health.isPending ? undefined : "sr-only"}>
        {health.isFetching ? "Checking the installation…" : health.isSuccess ? "Checked." : ""}
      </p>
      {health.isError && <p role="alert">The health of the installation could not be read. Refresh to try again.</p>}
      {health.data && (
        <div className="grid gap-4 sm:grid-cols-2">
          <Card>
            <CardHeader><CardTitle><h2>Services</h2></CardTitle></CardHeader>
            <CardContent>
              <dl className="flex flex-col gap-2">
                <Row name="Solver"><Reachability reachable={health.data.solver.reachable} /></Row>
                {health.data.solver.version && <Row name="Solver version">{health.data.solver.version}</Row>}
                {health.data.solver.workers != null && <Row name="Solver threads">{health.data.solver.workers}</Row>}
                <Row name="Database"><Reachability reachable={health.data.database} /></Row>
              </dl>
            </CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle><h2>Queue</h2></CardTitle></CardHeader>
            <CardContent>
              <dl className="flex flex-col gap-2">
                <Row name="Workers alive">{health.data.queue.workers}</Row>
                <Row name="Jobs waiting">{health.data.queue.waiting}</Row>
                <Row name="Jobs running">{health.data.queue.running}</Row>
                <Row name="Jobs failed, last 30 days">{health.data.queue.failed}</Row>
              </dl>
            </CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle><h2>Storage</h2></CardTitle></CardHeader>
            <CardContent>
              <dl className="flex flex-col gap-2">
                <Row name="Database size">{formatBytes(health.data.database_size_bytes)}</Row>
                {health.data.disk ? (
                  <>
                    <Row name="Disk free">{formatBytes(health.data.disk.free_bytes)}</Row>
                    <Row name="Disk size">{formatBytes(health.data.disk.total_bytes)}</Row>
                  </>
                ) : (
                  <Row name="Disk">Unknown</Row>
                )}
              </dl>
            </CardContent>
          </Card>
          <Pushes push={health.data.push} />
          <Backups health={health.data} />
        </div>
      )}
    </div>
  )
}
