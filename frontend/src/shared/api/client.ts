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
}

// Shoonya's OAuth routes live outside /api/v1 on purpose (the backend's
// SHOONYA_REDIRECT_URL is a fixed URL registered on Shoonya's own API key
// form — prefixing it would break that registration), so they need their
// own unprefixed client rather than going through `api` above.
export const shoonyaApi = {
  get: <T>(path: string) => request<T>('', path),
}
