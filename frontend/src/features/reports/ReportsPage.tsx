import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { api, ApiError, downloadFile } from '../../shared/api/client'
import { useSessions } from '../../shared/hooks/useSessions'
import { useStrategies } from '../../shared/hooks/useStrategies'
import { strategyTypeLabel } from '../../shared/format/friendlyLabel'
import type { DailyReportOut, PerformanceStatsOut, ScorecardOut } from '../../shared/api/types'

const DOWNLOAD_OPTIONS = [
  { value: 'eod-excel', label: 'EOD trade-log Excel' },
  { value: 'ws-quality', label: 'WS/feed quality report' },
  { value: 'scorecard', label: 'Strategy Scorecard export' },
] as const

export function ReportsPage() {
  const sessionsQuery = useSessions()
  const strategiesQuery = useStrategies()

  const [sessionId, setSessionId] = useState('')
  const [strategyId, setStrategyId] = useState('')
  const [downloadChoice, setDownloadChoice] = useState('')
  const [downloadNote, setDownloadNote] = useState<string | null>(null)
  const [downloading, setDownloading] = useState(false)

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

  async function handleDownload() {
    if (!downloadChoice) return
    setDownloadNote(null)

    if (downloadChoice === 'scorecard') {
      setDownloadNote(
        'Strategy Scorecard export is not available yet — the download endpoint isn’t built yet.',
      )
      return
    }

    setDownloading(true)
    try {
      if (downloadChoice === 'eod-excel') {
        await downloadFile('/reports/trade-log-export', 'trade_log.xlsx')
      } else if (downloadChoice === 'ws-quality') {
        await downloadFile('/reports/ws-quality-export', 'ws_quality.csv')
      }
    } catch (err) {
      setDownloadNote(err instanceof ApiError ? err.message : 'Download failed')
    } finally {
      setDownloading(false)
    }
  }

  return (
    <div>
      <div className="page-header">
        <h2>Reports</h2>
        <div className="row-actions">
          <select value={downloadChoice} onChange={(e) => setDownloadChoice(e.target.value)}>
            <option value="">Download...</option>
            {DOWNLOAD_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <button
            className="btn-ghost"
            disabled={!downloadChoice || downloading}
            onClick={() => void handleDownload()}
          >
            Download
          </button>
        </div>
      </div>

      {downloadNote && <p className="muted">{downloadNote}</p>}

      <div className="card">
        <h3>Daily report</h3>
        <select value={sessionId} onChange={(e) => setSessionId(e.target.value)}>
          <option value="">Select a session...</option>
          {sessions.map((s) => (
            <option key={s.id} value={s.id}>
              {s.mode} — {s.status}
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
              {strategyTypeLabel(s.strategy_type)}
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
