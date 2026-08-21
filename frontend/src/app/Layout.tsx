import { NavLink, Outlet } from 'react-router-dom'
import { useAuth } from '../shared/auth/AuthContext'
import { ModeBanner } from './ModeBanner'

export function Layout() {
  const { user, logout } = useAuth()

  return (
    <div className="layout">
      <nav className="topnav">
        <span className="topnav-brand">Trading Bot</span>
        <div className="topnav-links">
          <NavLink to="/control-room">Control Room</NavLink>
          <NavLink to="/market-terminal">Market Terminal</NavLink>
          <NavLink to="/reports">Reports</NavLink>
          <NavLink to="/advanced">Advanced</NavLink>
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
