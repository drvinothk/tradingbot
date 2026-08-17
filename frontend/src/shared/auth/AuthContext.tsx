import { createContext, useContext, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../api/client'
import type { UserOut } from '../api/types'

interface AuthContextValue {
  user: UserOut | null
  isLoading: boolean
  login: (email: string, password: string) => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()

  const { data: user, isLoading } = useQuery({
    queryKey: ['auth', 'me'],
    queryFn: async () => {
      try {
        return await api.get<UserOut>('/auth/me')
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) return null
        throw err
      }
    },
  })

  async function login(email: string, password: string) {
    const loggedInUser = await api.post<UserOut>('/auth/login', { email, password })
    queryClient.setQueryData(['auth', 'me'], loggedInUser)

    // Dual-Trigger Model: the login-triggered half of the daily bootstrap,
    // alongside the existing 09:00 IST scheduler -- fire-and-forget, since
    // this is an ambient sync the user isn't watching for and must never
    // block or fail the actual login on.
    void api.post('/sessions/bootstrap-now').catch(() => {})
  }

  async function logout() {
    await api.post('/auth/logout')
    queryClient.setQueryData(['auth', 'me'], null)
    queryClient.clear()
  }

  return (
    <AuthContext.Provider value={{ user: user ?? null, isLoading, login, logout }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
