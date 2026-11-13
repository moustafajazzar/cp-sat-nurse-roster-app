/** Every generated API call comes through here. A non-2xx answer throws, so TanStack Query sees it
 *  as an error and generated types describe success only. */

export class ApiError extends Error {
  readonly status: number
  readonly body: unknown

  constructor(status: number, body: unknown) {
    super(`The API answered ${status}`)
    this.status = status
    this.body = body
  }
}

function csrfToken(): string {
  return document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/)?.[1] ?? ""
}

export async function apiFetch<T>(url: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  const method = (init.method ?? "GET").toUpperCase()
  if (method !== "GET" && method !== "HEAD") headers.set("X-CSRFToken", csrfToken())

  const response = await fetch(url, { ...init, headers, credentials: "same-origin" })
  const text = await response.text()
  let body: unknown = text
  try {
    body = text ? JSON.parse(text) : undefined
  } catch {
    // Not JSON (a proxy's error page, say): the text is all there is.
  }
  if (!response.ok) throw new ApiError(response.status, body)
  return body as T
}
