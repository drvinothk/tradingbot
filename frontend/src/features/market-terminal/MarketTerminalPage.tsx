import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api, ApiError } from '../../shared/api/client'
import { BrokerConnectionRow } from '../../shared/components/BrokerConnectionRow'
import type { DiagnosticRole, DiagnosticStatusOut, UnderlyingSymbol } from '../../shared/api/types'

type DiagnosticMode = 'default' | 'failback' | 'both'

const DIAGNOSTIC_MODE_OPTIONS: { value: DiagnosticMode; label: string }[] = [
  { value: 'default', label: 'Test Default' },
  { value: 'failback', label: 'Test Failback' },
  { value: 'both', label: 'Both' },
]

function rolesForMode(mode: DiagnosticMode): DiagnosticRole[] {
  return mode === 'both' ? ['default', 'failback'] : [mode]
}

// "Default"/"Failback" never name a broker on purpose -- see
// diagnostic_session.py's own module docstring. Whichever provider each
// slot resolves to today (shown next to the dropdown once a run starts) is
// resolved fresh by the backend, not hardcoded here.
function WsQualityTestControl() {
  const queryClient = useQueryClient()
  const [mode, setMode] = useState<DiagnosticMode>('default')
  const [error, setError] = useState<string | null>(null)

  const statusQuery = useQuery({
    queryKey: ['market-data', 'diagnostic-status'],
    queryFn: () => api.get<DiagnosticStatusOut>('/market-data/diagnostic/status'),
    refetchInterval: 15_000,
  })

  const invalidateStatus = () =>
    queryClient.invalidateQueries({ queryKey: ['market-data', 'diagnostic-status'] })

  const startMutation = useMutation({
    mutationFn: () => api.post('/market-data/diagnostic/start', { mode }),
    onSuccess: () => {
      setError(null)
      invalidateStatus()
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : 'Could not start test'),
  })

  const stopMutation = useMutation({
    mutationFn: () => api.post('/market-data/diagnostic/stop', { mode }),
    onSuccess: invalidateStatus,
    onError: (err) => setError(err instanceof ApiError ? err.message : 'Could not stop test'),
  })

  const roles = rolesForMode(mode)
  const running = roles.map((role) => statusQuery.data?.[role])
  const anyRunning = running.some((r) => r?.running)
  const providers = running
    .filter((r) => r?.running && r.provider)
    .map((r) => r?.provider)
    .join(' + ')

  return (
    <div className="row-actions">
      {/* Deliberately .muted, not .broker-status -- this is a rarely-used
          diagnostic tool, not a status worth the same visual weight as the
          actual broker connection state next to it. */}
      <span className="muted">WS Quality Test</span>
      <select
        value={mode}
        disabled={anyRunning}
        onChange={(e) => setMode(e.target.value as DiagnosticMode)}
      >
        {DIAGNOSTIC_MODE_OPTIONS.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      <button
        className="btn-ghost"
        disabled={startMutation.isPending || stopMutation.isPending}
        onClick={() => (anyRunning ? stopMutation.mutate() : startMutation.mutate())}
      >
        {anyRunning ? 'Stop' : 'Run'}
      </button>
      {anyRunning && providers && <span className="muted">({providers})</span>}
      {error && <span className="error">{error}</span>}
    </div>
  )
}

const TRADINGVIEW_SYMBOL: Record<UnderlyingSymbol, string> = {
  NIFTY: 'NSE:NIFTY',
  BANKNIFTY: 'NSE:BANKNIFTY',
}

// Demo-only rows so the depth-bar/table styling described in the plan is
// visible even though the real option-chain endpoint doesn't exist yet —
// clearly labeled WIP, never presented as live data.
const DEMO_STRIKES = [
  { strike: 24200, callRatio: '2.1x Buyers', callSide: 'buy' as const, putRatio: '1.4x Sellers', putSide: 'sell' as const },
  { strike: 24250, callRatio: '3x Buyers', callSide: 'buy' as const, putRatio: '1.1x Buyers', putSide: 'buy' as const },
  { strike: 24300, callRatio: '1.2x Sellers', callSide: 'sell' as const, putRatio: '2.4x Sellers', putSide: 'sell' as const },
]

export function MarketTerminalPage() {
  const [underlying, setUnderlying] = useState<UnderlyingSymbol>('NIFTY')
  const [chartLabel, setChartLabel] = useState<string | null>(null)

  return (
    <div>
      <div className="broker-ribbon">
        <div className="broker-ribbon-left">
          <BrokerConnectionRow
            brokerLabel="Shoonya"
            statusPath="/shoonya/status"
            loginUrlPath="/shoonya/login-url"
            queryKeyPrefix="shoonya"
          />
          <BrokerConnectionRow
            brokerLabel="Alice Blue"
            statusPath="/aliceblue/status"
            loginUrlPath="/aliceblue/login-url"
            queryKeyPrefix="alice_blue"
          />
        </div>
        <WsQualityTestControl />
      </div>

      <div className="page-header">
        <h2>Market Terminal</h2>
        <div className="row-actions">
          <label htmlFor="underlying-select" className="muted">
            Underlying
          </label>
          <select
            id="underlying-select"
            value={underlying}
            onChange={(e) => {
              setUnderlying(e.target.value as UnderlyingSymbol)
              setChartLabel(null)
            }}
          >
            <option value="NIFTY">Nifty</option>
            <option value="BANKNIFTY">Bank Nifty</option>
          </select>
        </div>
      </div>

      <div className="grid-2">
        <div className="card">
          <div className="card-header">
            <h3>
              {underlying === 'NIFTY' ? 'Nifty' : 'Bank Nifty'} Option Chain{' '}
              <span className="badge badge-wip">WIP</span>
            </h3>
          </div>
          <div className="wip-panel">
            <h4>Live option chain not wired up yet</h4>
            <p>
              No backend endpoint returns live option-chain/depth data today — this needs a real
              candle-history + option-chain read path before this table can show anything but a
              static demo. The rows below illustrate the intended depth-bar layout only.
            </p>
          </div>
          <table style={{ opacity: 0.55, marginTop: '0.75rem' }}>
            <thead>
              <tr>
                <th>Call Depth</th>
                <th>Call LTP</th>
                <th style={{ textAlign: 'center' }}>Strike</th>
                <th>Put LTP</th>
                <th>Put Depth</th>
              </tr>
            </thead>
            <tbody>
              {DEMO_STRIKES.map((row) => (
                <tr
                  key={row.strike}
                  style={{ cursor: 'pointer' }}
                  onClick={() => setChartLabel(`${row.strike} demo`)}
                >
                  <td>
                    <div className={`depth-bar depth-bar-${row.callSide}`}>{row.callRatio}</div>
                  </td>
                  <td>—</td>
                  <td style={{ textAlign: 'center', fontWeight: 700 }}>{row.strike}</td>
                  <td>—</td>
                  <td>
                    <div className={`depth-bar depth-bar-${row.putSide}`}>{row.putRatio}</div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="card">
          <div className="card-header">
            <h3>Chart{chartLabel ? `: ${chartLabel}` : ''}</h3>
          </div>
          {/* TradingView's own iframe embed needs no backend work at all —
              live and fully functional, per the plan's explicit note that
              this is the one Market Terminal piece buildable without the
              option-chain endpoint. */}
          <iframe
            title="TradingView chart"
            src={`https://s.tradingview.com/widgetembed/?frameElementId=tv-chart&symbol=${encodeURIComponent(
              TRADINGVIEW_SYMBOL[underlying],
            )}&interval=5&hidesidetoolbar=1&hidetoptoolbar=0&symboledit=1&saveimage=0&toolbarbg=110e1b&studies=%5B%5D&theme=dark&style=1&timezone=Asia%2FKolkata`}
            style={{ width: '100%', height: '520px', border: 'none', borderRadius: '6px' }}
            allowFullScreen
          />
        </div>
      </div>
    </div>
  )
}
