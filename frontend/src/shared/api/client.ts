const BASE = '/api/v1'

export class ApiError extends Error {
  status: number
  body: unknown

  constructor(status: number, body: unknown) {
    const detail =
      typeof body === 'object' && body !== null && 'detail' in body
        ? String((body as { detail: unknown }).detail)
        : `Request failed with status ${status}`
    super(detail)
    this.status = status
    this.body = body
  }
}

async function request<T>(base: string, path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    ...init,
    credentials: 'include',
    headers: {
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  })

  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new ApiError(response.status, body)
  }

  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}

export const api = {
  get: <T>(path: string) => request<T>(BASE, path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(BASE, path, {
      method: 'POST',
      body: body !== undefined ? JSON.stringify(body) : undefined,
    }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(BASE, path, {
      method: 'PATCH',
      body: body !== undefined ? JSON.stringify(body) : undefined,
    }),
}

// File downloads (trade-log Excel, WS-quality CSV) need the raw response
// blob + Content-Disposition filename, not `request`'s JSON parsing --
// separate helper rather than overloading `api.get`.
export async function downloadFile(path: string, fallbackFilename: string): Promise<void> {
  const response = await fetch(`${BASE}${path}`, { credentials: 'include' })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new ApiError(response.status, body)
  }
  const blob = await response.blob()
  const disposition = response.headers.get('content-disposition') ?? ''
  const match = /filename="?([^";]+)"?/.exec(disposition)
  const filename = match?.[1] ?? fallbackFilename

  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

// Shoonya's (and Alice Blue's) OAuth routes live outside /api/v1 on purpose
// (the registered redirect URL on each broker's own portal is a fixed URL —
// prefixing it would break that registration), so they need their own
// unprefixed client rather than going through `api` above. Shared between
// both brokers since the shape is identical, not duplicated per-broker.
export const shoonyaApi = {
  get: <T>(path: string) => request<T>('', path),
  post: <T>(path: string, body?: unknown) =>
    request<T>('', path, {
      method: 'POST',
      body: body !== undefined ? JSON.stringify(body) : undefined,
    }),
}
