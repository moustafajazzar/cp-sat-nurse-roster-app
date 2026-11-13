import { zodResolver } from "@hookform/resolvers/zod"
import type { UseMutationResult } from "@tanstack/react-query"
import { type FormEvent, useMemo } from "react"
import { useForm } from "react-hook-form"
import { z } from "zod"

import type { ImportProblem } from "@/api/generated/model"
import { FormField } from "@/components/form-field"
import { Button } from "@/components/ui/button"
import { FieldGroup } from "@/components/ui/field"
import { serverErrors, showServerErrors } from "@/lib/server-errors"

// As in backend/nurses/importing.py, which has the last word. Here so a large file is not sent for nothing.
export const MAX_ROWS = 500
const MAX_MEGABYTES = 1
const COULD_NOT_CHECK = "That did not work. Check your connection and try again."

/** The classes of a cell holding what the file said, which may be long and unbroken. */
export const CELL = "max-w-64 whitespace-normal wrap-anywhere"

/** The file as it was when checked, and what the check said. Held as bytes, so that what is confirmed is what was
 *  read, whatever happens to the file on disk. Nothing of it is kept anywhere else. */
export type CheckedFile<Preview> = { bytes: Blob; preview: Preview }

type Check<Preview> = UseMutationResult<Preview, unknown, { data: { file: Blob } }, unknown>

/** Choosing a CSV file and sending it to be checked, for every import. `lines` names what the file holds, as the
 *  server's refusal of a file over the size limit does: "nurses", "skills". */
export function CheckFileForm<Preview>({ check, lines, confirming, checking, onChecking, onChecked }: {
  check: Check<Preview>
  lines: string
  confirming: boolean
  checking: boolean
  onChecking: (checking: boolean) => void
  onChecked: (checked: CheckedFile<Preview>) => void
}) {
  const fileSchema = useMemo(() => z.object({
    file: z.custom<FileList | null>()
      .refine((files) => !!files?.length, "Choose a CSV file.")
      .refine((files) => !files?.[0] || files[0].size <= MAX_MEGABYTES * 1024 * 1024,
        `This file is larger than ${MAX_MEGABYTES} MB, which is more than ${MAX_ROWS} ${lines} need.`),
  }), [lines])
  const form = useForm<{ file: FileList | null }>({ resolver: zodResolver(fileSchema), defaultValues: { file: null } })
  const { errors } = form.formState

  const readAndSend = form.handleSubmit(async ({ file: files }) => {
    let bytes: Blob
    try {
      bytes = new Blob([await (files as FileList)[0].arrayBuffer()])
    } catch {
      // A browser refuses to read a file that was saved again after it was chosen.
      form.setError("file", { message: "This file changed after you chose it. Choose it again." }, { shouldFocus: true })
      return
    }
    let preview: Preview
    try {
      preview = await check.mutateAsync({ data: { file: bytes } })
    } catch (error) {
      showServerErrors(form, error)
      return
    }
    check.reset()
    onChecked({ bytes, preview })
  })

  /** A check and an import never overlap, because a check's answer replaces the file an import is working from. So
   *  this is one stretch for the page, from the press to the answer, reading the file and all. */
  async function submit(event: FormEvent) {
    event.preventDefault()
    if (confirming || checking) return
    onChecking(true)
    try {
      await readAndSend(event)
    } finally {
      onChecking(false)
    }
  }

  return (
    <form onSubmit={submit} noValidate>
      <FieldGroup>
        <FormField label="CSV file" type="file" accept=".csv,text/csv" registration={form.register("file")} error={errors.file} />
        {check.isError && serverErrors(check.error).length === 0 && <p role="alert" className="text-sm text-destructive">{COULD_NOT_CHECK}</p>}
        <div>
          <Button type="submit" disabled={confirming || checking}>{checking ? "Checking…" : "Check file"}</Button>
        </div>
      </FieldGroup>
    </form>
  )
}

/** A row's problems, each after the name of its column, or alone when it is about the whole line. */
export function ProblemList({ problems, columns }: { problems: ImportProblem[]; columns: Record<string, string> }) {
  return (
    <ul className="flex flex-col gap-1">
      {problems.map((problem) => (
        <li key={`${problem.column}-${problem.message}`}>
          {problem.column ? `${columns[problem.column]}: ` : ""}{problem.message}
        </li>
      ))}
    </ul>
  )
}

/** The heading of a check's answer when some lines have a problem. */
export function linesWithProblems(withProblems: number, all: number) {
  return `${withProblems} of ${all} ${all === 1 ? "line" : "lines"} ${withProblems === 1 ? "has" : "have"} a problem`
}

/** What to say under that heading. */
export function correctTheFile(fine: number) {
  return "Nothing can be saved until every line is right. Correct the file, choose it above and check it again."
    + (fine > 0 ? ` The other ${fine === 1 ? "line has" : `${fine} lines have`} no problem.` : "")
}
