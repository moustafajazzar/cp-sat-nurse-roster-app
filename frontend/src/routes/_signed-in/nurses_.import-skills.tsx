import { createFileRoute, Link, useRouteContext } from "@tanstack/react-router"
import { useEffect, useId, useRef, useState } from "react"

import { ApiError } from "@/api/fetcher"
import {
  getDownloadSkillsImportTemplateUrl,
  useConfirmSkillsImport,
  usePreviewSkillsImport,
} from "@/api/generated/endpoints"
import { Role, type SkillImportDone, type SkillImportPreview, type SkillImportRow } from "@/api/generated/model"
import { CELL, type CheckedFile, CheckFileForm, correctTheFile, linesWithProblems, MAX_ROWS, ProblemList } from "@/components/csv-import"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { serverErrors } from "@/lib/server-errors"
import { byKind, type Kind } from "@/lib/skill-kinds"
import { cn, scrollableRegion, shortDay } from "@/lib/utils"
import { onlyFor } from "@/session"

export const Route = createFileRoute("/_signed-in/nurses_/import-skills")({
  // The CNO's, and the Deputy CNO's: both record what a nurse holds.
  beforeLoad: onlyFor(Role.cno, Role.deputy_cno),
  staticData: { title: "Import skills" },
  component: ImportSkillsPage,
})

const COLUMN_NAMES: Record<string, string> = {
  hospital_id: "Hospital ID",
  skill: "Skill",
  expiry: "Expiry",
  identifier: "Identifier",
  issue_date: "Issue date",
}

/** The columns after the first two (Line, then Problems or Change), each a field of the row as the file has it. */
const FILE_COLUMNS = ["Hospital ID", "Nurse", "Skill", "Expiry", "Identifier", "Issue date"]

const count = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`
const lineCount = (n: number) => count(n, "line", "lines")

/** What a renewal changes, each field the line differs in with what was recorded: "was until 31 Dec 2026, identifier
 *  was 21-RN-0412". An empty cell in the file is none, so a line may take an identifier or an issue date off. */
function renewalWords(row: SkillImportRow) {
  /** What was recorded, when the line differs from it: its words, or `none` when nothing was. */
  const was = (inFile: string, held: string | null, words: (held: string) => string, none: string) =>
    inFile === (held ?? "") ? null : held ? words(held) : none
  const changes = [
    was(row.expiry, row.held_expiry, (held) => `was until ${shortDay(held)}`, ""),
    was(row.identifier, row.held_identifier, (held) => `identifier was ${held}`, "no identifier before"),
    was(row.issue_date, row.held_issue_date, (held) => `issue date was ${shortDay(held)}`, "no issue date before"),
  ].filter(Boolean)
  return ["Renewal", ...changes].join(", ")
}

/** What saving does with a row, in the page's words. */
function changeWords(row: SkillImportRow) {
  switch (row.change) {
    case "record": return "New"
    case "renew": return renewalWords(row)
    case "none": return "Already recorded"
    default: return ""
  }
}

/** "records 2 skills and renews 1 skill", leaving out what is none. */
function savingWords({ recorded, renewed }: { recorded: number; renewed: number }) {
  return [recorded > 0 && `records ${count(recorded, "skill", "skills")}`, renewed > 0 && `renews ${count(renewed, "skill", "skills")}`]
    .filter(Boolean).join(" and ")
}

/** "2 skills recorded and 1 skill renewed." */
function savedWords({ recorded, renewed }: { recorded: number; renewed: number }) {
  const said = [recorded > 0 && `${count(recorded, "skill", "skills")} recorded`, renewed > 0 && `${count(renewed, "skill", "skills")} renewed`]
    .filter(Boolean).join(" and ")
  return said ? `${said}.` : "Nothing needed saving."
}

/** The lines under Skills, Certificates and Licences, as every table of skills is, and last the lines whose skill
 *  names no catalogue entry or cannot be read, which have no kind. Within a group, in the file's order. */
function groupsOf(rows: SkillImportRow[]) {
  const unknown = rows.filter((row) => !row.kind)
  return [
    ...byKind(rows.filter((row) => row.kind), (row) => row.kind as Kind),
    ...(unknown.length > 0 ? [{ kind: "unknown", heading: "Other lines", rows: unknown }] : []),
  ]
}

function RowsTable({ rows, label }: { rows: SkillImportRow[]; label: string }) {
  const id = useId()
  const hasProblems = rows.some((row) => row.problems.length > 0)
  return (
    <Table container={scrollableRegion(label)}>
      <TableHeader>
        <TableRow>
          <TableHead>Line</TableHead>
          {hasProblems ? <TableHead>Problems</TableHead> : <TableHead>Change</TableHead>}
          {FILE_COLUMNS.map((column) => <TableHead key={column}>{column}</TableHead>)}
        </TableRow>
      </TableHeader>
      {groupsOf(rows).map((group) => (
        <TableBody key={group.kind} aria-labelledby={`${id}-${group.kind}`}>
          <TableRow className="hover:bg-transparent">
            <TableHead id={`${id}-${group.kind}`} scope="rowgroup" colSpan={FILE_COLUMNS.length + 2}
              className="bg-muted/50 font-medium text-foreground">{group.heading}</TableHead>
          </TableRow>
          {group.rows.map((row) => (
            <TableRow key={row.line}>
              <TableCell className="tabular-nums">{row.line}</TableCell>
              {hasProblems ? (
                <TableCell className="min-w-64 whitespace-normal text-destructive">
                  <ProblemList problems={row.problems} columns={COLUMN_NAMES} />
                </TableCell>
              ) : (
                <TableCell className="min-w-32 whitespace-normal">{changeWords(row)}</TableCell>
              )}
              <TableCell translate="no" className={CELL}>{row.hospital_id}</TableCell>
              <TableCell className={cn(CELL, "min-w-32")}>{row.full_name}</TableCell>
              <TableCell className={cn(CELL, "min-w-24")}>{row.skill}</TableCell>
              <TableCell className={cn(CELL, "min-w-24 tabular-nums")}>{row.expiry}</TableCell>
              <TableCell translate="no" className={cn(CELL, "min-w-28")}>{row.identifier}</TableCell>
              <TableCell className={cn(CELL, "min-w-24 tabular-nums")}>{row.issue_date}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      ))}
    </Table>
  )
}

function Preview({ file, changed, saving, checking, refusal, onSave }: {
  file: CheckedFile<SkillImportPreview>
  changed: boolean
  saving: boolean
  checking: boolean
  refusal: string | null
  onSave: (token: string) => void
}) {
  const heading = useRef<HTMLHeadingElement>(null)
  const { rows, token } = file.preview
  const withProblems = rows.filter((row) => row.problems.length > 0)
  const recorded = rows.filter((row) => row.change === "record").length
  const renewed = rows.filter((row) => row.change === "renew").length
  const unchanged = rows.filter((row) => row.change === "none").length
  const saves = recorded + renewed

  useEffect(() => { heading.current?.focus() }, [file])

  return (
    <Card>
      <CardHeader>
        <CardTitle><h2 ref={heading} tabIndex={-1} className="outline-none">
          {token ? `${lineCount(rows.length)} ready to save` : linesWithProblems(withProblems.length, rows.length)}
        </h2></CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {changed && <p role="alert" className="text-sm text-destructive">Something changed since the last check, so nothing was saved. This is how the file stands now.</p>}
        {token ? (
          <p>
            {saves > 0 ? `Saving ${savingWords({ recorded, renewed })}. Nothing is saved until you do.` : "Every line says what is already recorded, so there is nothing to save."}
            {saves > 0 && unchanged > 0 && ` ${count(unchanged, "line says", "lines say")} what is already recorded.`}
          </p>
        ) : (
          <p>{correctTheFile(rows.length - withProblems.length)}</p>
        )}
        {token && saves > 0 && (
          <div className="flex flex-col items-start gap-2">
            <Button onClick={() => onSave(token)} disabled={saving || checking}>{saving ? "Saving…" : `Save ${count(saves, "skill", "skills")}`}</Button>
            {refusal && <p role="alert" className="text-sm text-destructive">{refusal}</p>}
          </div>
        )}
        <RowsTable rows={token ? rows : withProblems} label="Lines of the file" />
      </CardContent>
    </Card>
  )
}

function ImportSkillsPage() {
  const { user } = useRouteContext({ from: "/_signed-in" })
  // Where each reads hospital IDs and records by hand: the Deputy CNO has no Nurses page.
  const isCno = user.role === Role.cno
  const [file, setFile] = useState<CheckedFile<SkillImportPreview> | null>(null)
  const [changed, setChanged] = useState(false)
  const [checking, setChecking] = useState(false)
  const check = usePreviewSkillsImport({ mutation: { gcTime: 0 } })
  const confirm = useConfirmSkillsImport({ mutation: { gcTime: 0 } })
  const [done, setDone] = useState("")
  const heading = useRef<HTMLHeadingElement>(null)

  function save(token: string) {
    if (!file || checking || confirm.isPending) return
    confirm.mutate({ data: { file: file.bytes, token } }, {
      onSuccess: (answer: SkillImportDone) => {
        confirm.reset()
        setFile(null)
        setDone(savedWords(answer))
        heading.current?.focus()
      },
      onError: (error) => {
        if (error instanceof ApiError && error.status === 409) {
          confirm.reset()
          setChanged(true)
          setFile({ ...file, preview: error.body as SkillImportPreview })
        }
      },
    })
  }

  const refusal = confirm.isError ? (serverErrors(confirm.error)[0]?.message
    ?? "It is not known whether these skills were saved. Check the file again: a line that was saved shows as already recorded.") : null

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4">
      <h1 ref={heading} tabIndex={-1} className="text-xl font-medium outline-none">Import skills</h1>
      <p role="status" className="text-sm text-muted-foreground empty:sr-only">{done}</p>
      <Card className="max-w-xl">
        <CardHeader><CardTitle><h2>Choose a file</h2></CardTitle></CardHeader>
        <CardContent className="flex flex-col gap-4">
          <p>
            <a href={getDownloadSkillsImportTemplateUrl()} download className="underline underline-offset-4">Download the template</a>,
            {" "}fill in one skill of one nurse per line, up to {MAX_ROWS} in a file, and save it as CSV UTF-8.
            Or go back to {isCno
              ? <Link to="/nurses" className="underline underline-offset-4">Nurses</Link>
              : <Link to="/rostering-attributes" className="underline underline-offset-4">Rostering attributes</Link>}.
          </p>
          <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[auto_1fr]">
            <dt className="font-medium">Hospital ID</dt>
            <dd className="mb-2 text-muted-foreground sm:mb-0">{isCno ? "As on the Nurses page." : "As on Rostering attributes."}</dd>
            <dt className="font-medium">Skill</dt>
            <dd className="mb-2 text-muted-foreground sm:mb-0">Its name on the Skills page, a certificate or a licence among them. For one the nurse already holds, the line is her record as it should now read, and saving renews it.</dd>
            <dt className="font-medium">Expiry</dt>
            <dd className="mb-2 text-muted-foreground sm:mb-0">The last day it counts, as YYYY-MM-DD, such as 2027-01-31. Empty for a skill that does not expire. In Excel, format the date columns as yyyy-mm-dd, or saving changes the dates.</dd>
            <dt className="font-medium">Identifier</dt>
            <dd className="mb-2 text-muted-foreground sm:mb-0">The number its issuing body gave it. Required on a licence; empty when there is none, which takes off one recorded.</dd>
            <dt className="font-medium">Issue date</dt>
            <dd className="text-muted-foreground">When it was issued, as YYYY-MM-DD: not after the expiry or today. Empty when not known, which takes off one recorded.</dd>
          </dl>
          {/* The reset only clears what the last save said: no check runs during one, nor one during a check. */}
          <CheckFileForm check={check} lines="skills" confirming={confirm.isPending} checking={checking} onChecking={setChecking}
            onChecked={(next) => { setDone(""); setChanged(false); confirm.reset(); setFile(next) }} />
        </CardContent>
      </Card>
      {file && <Preview file={file} changed={changed} saving={confirm.isPending} checking={checking} refusal={refusal} onSave={save} />}
    </div>
  )
}
