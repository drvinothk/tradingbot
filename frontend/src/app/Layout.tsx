import { NavLink, Outlet } from 'react-router-dom'
import { useAuth } from '../shared/auth/AuthContext'
import { ModeBanner } from './ModeBanner'

export function Layout() {
  const { user, logout } = useAuth()

  return (
    <div className="layout">
      <nav className="topnav">
        <div className="topnav-links">
          <NavLink to="/running">Running Strategies</NavLink>
          <NavLink to="/sessions">Sessions</NavLink>
          <NavLink to="/strategies">Strategies</NavLink>
          <NavLink to="/reports">Reports</NavLink>
          <NavLink to="/recovery">Recovery</NavLink>
        </div>
        <div className="topnav-user">
          <span>{user?.email}</span>
          <button onClick={() => void logout()}>Log out</button>
        </div>
      </nav>
      <ModeBanner />
      <main className="content">
        <Outlet />
      </main>
    </div>
  )
}
