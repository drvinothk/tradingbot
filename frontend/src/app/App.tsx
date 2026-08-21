import { Navigate, Route, Routes } from 'react-router-dom'
import { Layout } from './Layout'
import { RequireAuth } from './RequireAuth'
import { LoginPage } from '../features/auth/LoginPage'
import { ControlRoomPage } from '../features/control-room/ControlRoomPage'
import { MarketTerminalPage } from '../features/market-terminal/MarketTerminalPage'
import { ReportsPage } from '../features/reports/ReportsPage'
import { AdvancedPage } from '../features/advanced/AdvancedPage'

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<RequireAuth />}>
        <Route element={<Layout />}>
          <Route path="/control-room" element={<ControlRoomPage />} />
          <Route path="/market-terminal" element={<MarketTerminalPage />} />
          <Route path="/reports" element={<ReportsPage />} />
          <Route path="/advanced" element={<AdvancedPage />} />
          <Route path="/" element={<Navigate to="/control-room" replace />} />
        </Route>
      </Route>
    </Routes>
  )
}
