import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api, ApiError } from '../../shared/api/client'
import { BrokerConnectionRow } from '../../shared/components/BrokerConnectionRow'
import type {
  DiagnosticRole,
  DiagnosticStatusOut,
  RunningStrategyOut,
  UnderlyingSymbol,
} from '../../shared/api/types'
import { signalReasonLabel, strategyTypeLabel } from '../../shared/format/friendlyLabel'
import { useRunningStrategies } from '../../shared/hooks/useRunningStrategies'
import { useStreamingSymbols } from '../../shared/hooks/useStreamingSymbols'
import { PriceChart } from './PriceChart'

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
  const streamingSymbolsQuery = useStreamingSymbols()
  const streamingSymbols = streamingSymbolsQuery.data?.symbols ?? []

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
            <option value="NIFTY" disabled={!streamingSymbols.includes('NIFTY')}>
              Nifty{streamingSymbols.includes('NIFTY') ? '' : ' (not streaming)'}
            </option>
            <option value="BANKNIFTY" disabled={!streamingSymbols.includes('BANKNIFTY')}>
              Bank Nifty{streamingSymbols.includes('BANKNIFTY') ? '' : ' (not streaming)'}
            </option>
          </select>
        </div>
      </div>

      <div className="grid-2">
        <SignalPanel />

        <div className="card">
          <div className="card-header">
            <h3>Chart{chartLabel ? `: ${chartLabel}` : ''}</h3>
          </div>
          {/* Own-data chart (2026-08-30) -- lightweight-charts (TradingView's
              open-source lib) fed by this system's real price_bars via
              polling, replacing the third-party TradingView embed. See
              PriceChart's own docstring for why an empty result is an
              expected state, not an error. */}
          <PriceChart underlying={underlying} />
        </div>
      </div>

      {/* Moved to the bottom (2026-08-30) -- still fake demo data, not
          wired to a real option-chain endpoint yet. Revisit once the real
          ATM +/- N strikes table (LTP/OI/OI-delta/volume/spread) is built;
          see the Market Terminal design discussion for why this was
          deliberately deferred rather than built alongside the chart/
          signal panel. */}
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
    </div>
  )
}

// Market Terminal signal panel (2026-08-30) -- live visibility into why a
// scanning strategy hasn't fired: reason code, plus (when already resolved
// at that point) the exact candidate strike/CE-PE/expiry/LTP/planned
// entry/stop/target. Reuses the existing useRunningStrategies() poll (same
// /strategies/running the Advanced/Control Room pages already share) --
// no new network traffic beyond what's already firing wherever those pages
// happen to be open.
function SignalPanel() {
  const runningQuery = useRunningStrategies()
  const runs = runningQuery.data ?? []

  return (
    <div className="card">
      <div className="card-header">
        <h3>Signal Panel</h3>
      </div>
      {runs.length === 0 ? (
        <p className="muted">No strategies running right now.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Strategy</th>
              <th>Status</th>
              <th>Reason</th>
              <th>Candidate</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <SignalPanelRow key={run.strategy_run_id} run={run} />
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function SignalPanelRow({ run }: { run: RunningStrategyOut }) {
  const signal = run.last_signal

  return (
    <tr>
      <td>
        {run.strategy_name}
        <div className="muted" style={{ fontSize: '0.85em' }}>
          {strategyTypeLabel(run.strategy_type)}
        </div>
      </td>
      <td>
        <span className={`badge ${run.status === 'in_position' ? 'badge-success' : ''}`}>
          {run.status === 'in_position' ? 'In Position' : 'Scanning'}
        </span>
      </td>
      <td>{signal ? signalReasonLabel(signal.reason_code) : <span className="muted">—</span>}</td>
      <td>
        {signal && signal.side && signal.strike !== null ? (
          <div style={{ fontSize: '0.9em' }}>
            <div>
              {signal.strike} {signal.side}
              {signal.expiry_date ? ` · ${signal.expiry_date}` : ''}
            </div>
            <div className="muted">
              LTP {signal.ltp ?? '—'} · Entry {signal.planned_entry ?? '—'} · SL{' '}
              {signal.stop_price ?? '—'} · Tgt {signal.target_price ?? '—'}
            </div>
          </div>
        ) : (
          <span className="muted">—</span>
        )}
      </td>
    </tr>
  )
}
