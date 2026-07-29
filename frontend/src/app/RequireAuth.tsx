import { Navigate, Outlet } from 'react-router-dom'
import { useAuth } from '../shared/auth/AuthContext'

export function RequireAuth() {
  const { user, isLoading } = useAuth()

  if (isLoading) return <p>Loading...</p>
  if (!user) return <Navigate to="/login" replace />
  return <Outlet />
}
