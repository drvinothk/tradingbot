import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { api, ApiError } from '../../shared/api/client'
import type {
  DailyReportOut,
  PerformanceStatsOut,
  ScorecardOut,
  SessionOut,
  StrategyConfigOut,
} from '../../shared/api/types'

export function ReportsPage() {
  const sessionsQuery = useQuery({
    queryKey: ['sessions'],
    queryFn: () => api.get<SessionOut[]>('/sessions'),
  })
  const strategiesQuery = useQuery({
    queryKey: ['strategies'],
    queryFn: () => api.get<StrategyConfigOut[]>('/strategies'),
  })

  const [sessionId, setSessionId] = useState('')
  const [strategyId, setStrategyId] = useState('')

  const dailyReportQuery = useQuery({
    queryKey: ['reports', 'daily', sessionId],
    queryFn: () => api.get<DailyReportOut>(`/reports/sessions/${sessionId}/daily`),
    enabled: Boolean(sessionId),
  })

  const scorecardQuery = useQuery({
    queryKey: ['reports', 'scorecard', strategyId],
    queryFn: () => api.get<ScorecardOut>(`/reports/strategies/${strategyId}/scorecard`),
    enabled: Boolean(strategyId),
  })

  const sessions = sessionsQuery.data ?? []
  const strategies = strategiesQuery.data ?? []

  return (
    <div>
      <div className="page-header">
        <h2>Reports</h2>
      </div>

      <div className="card">
        <h3>Daily report</h3>
        <select value={sessionId} onChange={(e) => setSessionId(e.target.value)}>
          <option value="">Select a session...</option>
          {sessions.map((s) => (
            <option key={s.id} value={s.id}>
              {s.id.slice(0, 8)} ({s.mode})
            </option>
          ))}
        </select>
        {dailyReportQuery.error && (
          <p className="error">
            {dailyReportQuery.error instanceof ApiError
              ? dailyReportQuery.error.message
              : 'Failed to load report'}
          </p>
        )}
        {dailyReportQuery.data && <StatsTable stats={dailyReportQuery.data} />}
      </div>

      <div className="card">
        <h3>Strategy scorecard</h3>
        <select value={strategyId} onChange={(e) => setStrategyId(e.target.value)}>
          <option value="">Select a strategy...</option>
          {strategies.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </select>
        {scorecardQuery.error && (
          <p className="error">
            {scorecardQuery.error instanceof ApiError
              ? scorecardQuery.error.message
              : 'Failed to load scorecard'}
          </p>
        )}
        {scorecardQuery.data && <StatsTable stats={scorecardQuery.data} />}
      </div>
    </div>
  )
}

function StatsTable({ stats }: { stats: PerformanceStatsOut }) {
  const rows: [string, string | number][] = [
    ['Trades', stats.trade_count],
    ['Wins', stats.win_count],
    ['Losses', stats.loss_count],
    ['Win rate', `${(stats.win_rate * 100).toFixed(1)}%`],
    ['Avg win', stats.avg_win.toFixed(2)],
    ['Avg loss', stats.avg_loss.toFixed(2)],
    ['Profit factor', stats.profit_factor === null ? '-' : stats.profit_factor.toFixed(2)],
    ['Max drawdown', stats.max_drawdown.toFixed(2)],
    ['Total realized P&L', stats.total_realized_pnl.toFixed(2)],
    ['Total slippage', stats.total_slippage.toFixed(2)],
    ['Signals', stats.signal_count],
    ['Dispatched', stats.dispatched_count],
    ['Filled', stats.filled_count],
  ]

  return (
    <table>
      <tbody>
        {rows.map(([label, value]) => (
          <tr key={label}>
            <th>{label}</th>
            <td>{value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
