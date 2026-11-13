import type { FieldPath, FieldValues, UseFormReturn } from "react-hook-form"

import { ApiError } from "@/api/fetcher"

type Refusal = { detail?: { loc: (string | number)[]; msg: string }[] }

/** A 422's refusals, each with the form path it belongs to. By default the whole location: pass `toPath`
 *  when a page shows a list's errors on the list rather than on each entry. */
export function serverErrors(
  error: unknown,
  toPath: (loc: (string | number)[]) => string = (loc) => loc.join("."),
): { path: string; message: string }[] {
  if (!(error instanceof ApiError) || error.status !== 422) return []
  const detail = (error.body as Refusal | undefined)?.detail
  if (!Array.isArray(detail)) return []
  return detail.map(({ loc, msg }) => ({
    // ["body", "body", "weights", "change"] starts with where the value came from.
    path: toPath(loc.slice(2)),
    // Pydantic opens a validator's own message with this.
    message: msg.replace(/^Value error, /, ""),
  }))
}

/** Shows a 422's refusals beside their fields, with focus on the first. `setFocus` because `setError`'s own
 *  focus cannot reach a checkbox. */
export function showServerErrors<Fields extends FieldValues>(
  form: UseFormReturn<Fields>,
  error: unknown,
  toPath?: (loc: (string | number)[]) => string,
) {
  const errors = serverErrors(error, toPath)
  errors.forEach(({ path, message }) => form.setError(path as FieldPath<Fields>, { message }))
  if (errors[0]) form.setFocus(errors[0].path as FieldPath<Fields>)
}

/** What a refusal that is not about a field says, when the answer says anything: 403 and 409 explain themselves. */
export function plainRefusal(error: unknown): string | undefined {
  if (!(error instanceof ApiError) || error.status === 422) return undefined
  const detail = (error.body as { detail?: unknown } | undefined)?.detail
  return typeof detail === "string" ? detail : undefined
}
