import { Navigate, Route, Routes } from 'react-router-dom'
import { Layout } from './Layout'
import { RequireAuth } from './RequireAuth'
import { LoginPage } from '../features/auth/LoginPage'
import { RunningStrategiesPage } from '../features/strategies/RunningStrategiesPage'
import { SessionsPage } from '../features/sessions/SessionsPage'
import { StrategiesPage } from '../features/strategies/StrategiesPage'
import { ReportsPage } from '../features/reports/ReportsPage'
import { RecoveryPage } from '../features/recovery/RecoveryPage'

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<RequireAuth />}>
        <Route element={<Layout />}>
          <Route path="/running" element={<RunningStrategiesPage />} />
          <Route path="/sessions" element={<SessionsPage />} />
          <Route path="/strategies" element={<StrategiesPage />} />
          <Route path="/reports" element={<ReportsPage />} />
          <Route path="/recovery" element={<RecoveryPage />} />
          <Route path="/" element={<Navigate to="/running" replace />} />
        </Route>
      </Route>
    </Routes>
  )
}
