import { createFileRoute, Link } from "@tanstack/react-router"
import { useEffect, useRef, useState } from "react"

import { ApiError } from "@/api/fetcher"
import {
  getDownloadNurseImportTemplateUrl,
  useConfirmNurseImport,
  usePreviewNurseImport,
} from "@/api/generated/endpoints"
import { Role, type ImportPreview, type ImportRow, type SheetRow } from "@/api/generated/model"
import { CELL, type CheckedFile, CheckFileForm, correctTheFile, linesWithProblems, MAX_ROWS, ProblemList } from "@/components/csv-import"
import { LeavePageDialog, LeavePageGuard } from "@/components/password-dialog"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { serverErrors } from "@/lib/server-errors"
import { cn, scrollableRegion } from "@/lib/utils"
import { onlyFor } from "@/session"

export const Route = createFileRoute("/_signed-in/nurses_/import")({
  beforeLoad: onlyFor(Role.cno),
  staticData: { title: "Import nurses" },
  component: ImportPage,
})

const NO_PASSWORD_AGAIN = "Nobody can see these passwords again. A nurse without theirs needs a password reset, one at a time, on the Nurses page."
const COLUMN_NAMES: Record<string, string> = {
  hospital_id: "Hospital ID",
  full_name: "Full name",
  gender: "Gender",
  unit: "Unit",
  start_date: "Start date",
  on_probation: "On probation",
}

const nurseCount = (count: number) => (count === 1 ? "1 nurse" : `${count} nurses`)

type CheckedNurseFile = CheckedFile<ImportPreview>

function RowsTable({ rows, label }: { rows: ImportRow[]; label: string }) {
  const hasProblems = rows.some((row) => row.problems.length > 0)
  return (
    <Table container={scrollableRegion(label)}>
      <TableHeader>
        <TableRow>
          <TableHead>Line</TableHead>
          {hasProblems && <TableHead>Problems</TableHead>}
          <TableHead>Hospital ID</TableHead>
          <TableHead>Full name</TableHead>
          <TableHead>Gender</TableHead>
          <TableHead>Unit</TableHead>
          <TableHead>Start date</TableHead>
          <TableHead>On probation</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => (
          <TableRow key={row.line}>
            <TableCell className="tabular-nums">{row.line}</TableCell>
            {hasProblems && (
              <TableCell className="min-w-64 whitespace-normal text-destructive">
                <ProblemList problems={row.problems} columns={COLUMN_NAMES} />
              </TableCell>
            )}
            <TableCell translate="no" className={CELL}>{row.hospital_id}</TableCell>
            <TableCell className={cn(CELL, "min-w-32")}>{row.full_name}</TableCell>
            <TableCell className={CELL}>{row.gender}</TableCell>
            <TableCell className={CELL}>{row.unit}</TableCell>
            <TableCell className={cn(CELL, "min-w-24 tabular-nums")}>{row.start_date}</TableCell>
            <TableCell className={CELL}>{row.on_probation}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function Preview({ file, changed, adding, checking, refusal, onAdd }: {
  file: CheckedNurseFile
  changed: boolean
  adding: boolean
  checking: boolean
  refusal: string | null
  onAdd: (token: string) => void
}) {
  const heading = useRef<HTMLHeadingElement>(null)
  const { rows, token } = file.preview
  const withProblems = rows.filter((row) => row.problems.length > 0)
  const fine = rows.length - withProblems.length

  useEffect(() => { heading.current?.focus() }, [file])

  return (
    <Card>
      <CardHeader>
        <CardTitle><h2 ref={heading} tabIndex={-1} className="outline-none">
          {token ? `${nurseCount(rows.length)} ready to add` : linesWithProblems(withProblems.length, rows.length)}
        </h2></CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {changed && <p role="alert" className="text-sm text-destructive">Something changed since the last check, so nothing was saved. This is how the file stands now.</p>}
        {token ? (
          <p>Nothing is saved until you add them.</p>
        ) : (
          <p>{correctTheFile(fine)}</p>
        )}
        {token && (
          <div className="flex flex-col items-start gap-2">
            <Button onClick={() => onAdd(token)} disabled={adding || checking}>{adding ? "Adding…" : `Add ${nurseCount(rows.length)}`}</Button>
            <p role="status" className="text-sm text-muted-foreground empty:sr-only">
              {adding && "Making each password takes a moment on purpose, so a large file can take a few minutes. Keep this page open."}
            </p>
            {refusal && <p role="alert" className="text-sm text-destructive">{refusal}</p>}
          </div>
        )}
        <RowsTable rows={token ? rows : withProblems} label="Lines of the file" />
      </CardContent>
    </Card>
  )
}

/** A spreadsheet would run a pasted cell that starts like a formula, as `safe_cell` in backend/nurses/spreadsheets.py
 *  says, and a tab or a line break in a name would start a new cell. */
function pasteSafe(cell: string) {
  const oneCell = cell.replace(/[\t\r\n]+/g, " ")
  return /^ *[=+\-@＝＋－＠]/.test(oneCell) ? `'${oneCell}` : oneCell
}

function PasswordSheetCard({ rows, onDone }: { rows: SheetRow[]; onDone: () => void }) {
  const [copied, setCopied] = useState("")
  const heading = useRef<HTMLHeadingElement>(null)
  const region = scrollableRegion("Passwords")

  useEffect(() => { heading.current?.focus() }, [])

  async function copy() {
    const lines = rows.map((row) => [row.hospital_id, row.full_name, row.unit, row.generated_password].map(pasteSafe).join("\t"))
    try {
      await navigator.clipboard.writeText(["Hospital ID\tFull name\tUnit\tPassword", ...lines].join("\n"))
      setCopied("Copied.")
    } catch {
      setCopied("They could not be copied. Print the sheet, or select the table by hand.")
    }
  }

  return (
    <Card className="print:border-0 print:shadow-none">
      <CardHeader>
        <CardTitle><h2 ref={heading} tabIndex={-1} className="outline-none">Passwords for {nurseCount(rows.length)}</h2></CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <p className="print:hidden">
          The nurses are added. This is the only time these passwords are shown, so print or copy them before you close this.
          Each nurse must choose a new one at their next login.
        </p>
        <div className="flex flex-wrap gap-2 print:hidden">
          <Button onClick={() => window.print()}>Print</Button>
          <Button variant="outline" onClick={copy}>Copy all</Button>
          <Button variant="outline" onClick={onDone}>Done</Button>
        </div>
        <p role="status" className="text-sm text-muted-foreground empty:sr-only print:hidden">{copied}</p>
        <Table container={{ ...region, className: cn(region.className, "print:overflow-visible") }}>
          <TableHeader>
            <TableRow>
              <TableHead>Hospital ID</TableHead>
              <TableHead>Full name</TableHead>
              <TableHead>Unit</TableHead>
              <TableHead>Password</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row) => (
              <TableRow key={row.hospital_id} className="break-inside-avoid">
                <TableCell translate="no">{row.hospital_id}</TableCell>
                <TableCell className={cn(CELL, "min-w-32")}>{row.full_name}</TableCell>
                <TableCell className={CELL}>{row.unit}</TableCell>
                <TableCell translate="no" className="font-mono">{row.generated_password}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}

function ImportPage() {
  const [file, setFile] = useState<CheckedNurseFile | null>(null)
  const [changed, setChanged] = useState(false)
  const [checking, setChecking] = useState(false)
  const check = usePreviewNurseImport({ mutation: { gcTime: 0 } })
  const confirm = useConfirmNurseImport({ mutation: { gcTime: 0 } })
  const [sheet, setSheet] = useState<SheetRow[] | null>(null) // The passwords live in this state and nowhere else.
  const [finishing, setFinishing] = useState(false)
  const [done, setDone] = useState("")
  const heading = useRef<HTMLHeadingElement>(null)

  function add(token: string) {
    if (!file || checking || confirm.isPending) return
    confirm.mutate({ data: { file: file.bytes, token } }, {
      onSuccess: (answer) => {
        confirm.reset() // The mutation holds its answer until told otherwise, and that answer holds passwords.
        setFile(null)
        setSheet(answer.rows)
      },
      onError: (error) => {
        if (error instanceof ApiError && error.status === 409) {
          confirm.reset()
          setChanged(true)
          setFile({ ...file, preview: error.body as ImportPreview })
        }
      },
    })
  }

  const refusal = confirm.isError ? (serverErrors(confirm.error)[0]?.message
    ?? "It is not known whether these nurses were added. Check the file again: a nurse who was added shows as already in use, and the Nurses page can reset their password.") : null

  function finish() {
    setDone(`${nurseCount(sheet?.length ?? 0)} added.`)
    setSheet(null)
    setFinishing(false)
    heading.current?.focus()
  }

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-4">
      <h1 ref={heading} tabIndex={-1} className="text-xl font-medium outline-none">Import nurses</h1>
      <p role="status" className="text-sm text-muted-foreground empty:sr-only print:hidden">{done}</p>
      {sheet ? (
        <PasswordSheetCard rows={sheet} onDone={() => setFinishing(true)} />
      ) : (
        <>
          <Card className="max-w-xl">
            <CardHeader><CardTitle><h2>Choose a file</h2></CardTitle></CardHeader>
            <CardContent className="flex flex-col gap-4">
              <p>
                <a href={getDownloadNurseImportTemplateUrl()} download className="underline underline-offset-4">Download the template</a>,
                {" "}fill in one nurse per line, up to {MAX_ROWS} in a file, and save it as CSV UTF-8.
                Or go back to <Link to="/nurses" className="underline underline-offset-4">Nurses</Link>.
              </p>
              <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[auto_1fr]">
                <dt className="font-medium">Hospital ID</dt>
                <dd className="mb-2 text-muted-foreground sm:mb-0">It becomes their username and cannot be corrected later. In Excel, format the column as Text so it keeps any leading zeros.</dd>
                <dt className="font-medium">Gender</dt>
                <dd className="mb-2 text-muted-foreground sm:mb-0">F or M.</dd>
                <dt className="font-medium">Unit</dt>
                <dd className="mb-2 text-muted-foreground sm:mb-0">Its name on the Units page.</dd>
                <dt className="font-medium">Start date</dt>
                <dd className="mb-2 text-muted-foreground sm:mb-0">YYYY-MM-DD, such as 2027-01-31. In Excel, format the column as yyyy-mm-dd, or saving changes the dates.</dd>
                <dt className="font-medium">On probation</dt>
                <dd className="text-muted-foreground">Yes for a newly hired nurse, or No. Empty means No, and a file without this column imports as before.</dd>
              </dl>
              {/* The reset only clears what the last import said: no check runs during one, nor one during a check. */}
              <CheckFileForm check={check} lines="nurses" confirming={confirm.isPending} checking={checking} onChecking={setChecking}
                onChecked={(next) => { setDone(""); setChanged(false); confirm.reset(); setFile(next) }} />
            </CardContent>
          </Card>
          {file && <Preview file={file} changed={changed} adding={confirm.isPending} checking={checking} refusal={refusal} onAdd={add} />}
        </>
      )}
      <LeavePageGuard holding={sheet !== null || confirm.isPending} title="Leave without the passwords?" description={NO_PASSWORD_AGAIN} />
      <LeavePageDialog open={finishing} title="Close these passwords?" description={NO_PASSWORD_AGAIN} action="Close passwords"
        onStay={() => setFinishing(false)} onLeave={finish} />
    </div>
  )
}
