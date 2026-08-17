import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../../shared/api/client'
import { useSessions } from '../../shared/hooks/useSessions'
import { useStrategies } from '../../shared/hooks/useStrategies'
import type {
  ExecutionMode,
  InstrumentOut,
  RuntimeMode,
  SessionOut,
  SetStrategyPowerOut,
  StrategyConfigOut,
  StrategyRunOut,
  StrategyType,
  UnderlyingSymbol,
} from '../../shared/api/types'

const STRATEGY_TYPES: StrategyType[] = [
  'synthetic',
  'orb',
  'vwap_pullback',
  'ema_micro_pullback',
  'oi_volume_confirmed',
  'liquidity_sweep_reversal',
]

// Matches api.v1.system_settings.RECOGNIZED_FIREWALL_INSTRUMENTS -- the only
// two underlyings this system trades, same hardcoded-list convention as
// STRATEGY_TYPES above (no OpenAPI-codegen, see shared/api/types.ts).
const UNDERLYING_SYMBOLS: UnderlyingSymbol[] = ['NIFTY', 'BANKNIFTY']

export function StrategiesPage() {
  const queryClient = useQueryClient()
  const [createError, setCreateError] = useState<string | null>(null)
  const [startError, setStartError] = useState<string | null>(null)
  const [startedRuns, setStartedRuns] = useState<Record<string, StrategyRunOut>>({})

  const strategiesQuery = useStrategies()
  const sessionsQuery = useSessions()
  const instrumentsQuery = useQuery({
    queryKey: ['instruments'],
    queryFn: () => api.get<InstrumentOut[]>('/instruments'),
  })

  const invalidateStrategies = () => queryClient.invalidateQueries({ queryKey: ['strategies'] })

  const [name, setName] = useState('')
  const [strategyType, setStrategyType] = useState<StrategyType>('synthetic')
  const [paramsText, setParamsText] = useState('{}')

  const createMutation = useMutation({
    mutationFn: () => {
      let params: Record<string, unknown>
      try {
        params = JSON.parse(paramsText || '{}')
      } catch {
        throw new Error('Params must be valid JSON')
      }
      return api.post<StrategyConfigOut>('/strategies', { name, strategy_type: strategyType, params })
    },
    onSuccess: () => {
      invalidateStrategies()
      setName('')
      setParamsText('{}')
    },
    onError: (err) =>
      setCreateError(err instanceof ApiError ? err.message : (err as Error).message ?? 'Create failed'),
  })

  function handleCreate(event: FormEvent) {
    event.preventDefault()
    setCreateError(null)
    createMutation.mutate()
  }

  const stopMutation = useMutation({
    mutationFn: (strategyId: string) => api.post(`/strategies/${strategyId}/stop`),
    onSuccess: invalidateStrategies,
  })

  const strategies = strategiesQuery.data ?? []
  const sessions = sessionsQuery.data ?? []
  const instruments = instrumentsQuery.data ?? []

  return (
    <div>
      <div className="page-header">
        <h2>Strategies</h2>
      </div>

      <div className="card">
        <h3>Create a strategy</h3>
        <form onSubmit={handleCreate}>
          <div className="form-row">
            <label htmlFor="name">Name</label>
            <input id="name" value={name} onChange={(e) => setName(e.target.value)} required />
          </div>
          <div className="form-row">
            <label htmlFor="strategy-type">Type</label>
            <select
              id="strategy-type"
              value={strategyType}
              onChange={(e) => setStrategyType(e.target.value as StrategyType)}
            >
              {STRATEGY_TYPES.map((type) => (
                <option key={type} value={type}>
                  {type}
                </option>
              ))}
            </select>
          </div>
          <div className="form-row">
            <label htmlFor="params">Params (JSON)</label>
            <textarea
              id="params"
              rows={3}
              value={paramsText}
              onChange={(e) => setParamsText(e.target.value)}
            />
          </div>
          {createError && <p className="error">{createError}</p>}
          <div className="form-actions">
            <button type="submit" disabled={createMutation.isPending}>
              Create strategy
            </button>
          </div>
        </form>
      </div>

      {startError && <p className="error">{startError}</p>}

      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Type</th>
            <th>Status</th>
            <th>Power</th>
            <th>Mode</th>
            <th>Instrument</th>
            <th>Start</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {strategies.map((strategy) => (
            <tr key={strategy.id}>
              <td>{strategy.name}</td>
              <td>{strategy.strategy_type}</td>
              <td>{strategy.status}</td>
              <StrategyPatchControls strategy={strategy} onSuccess={invalidateStrategies} />
              <td>
                <StartStrategyForm
                  strategyId={strategy.id}
                  sessions={sessions}
                  instruments={instruments}
                  onStarted={(run) =>
                    setStartedRuns((prev) => ({ ...prev, [strategy.id]: run }))
                  }
                  onError={(message) => setStartError(message)}
                  onSuccess={invalidateStrategies}
                />
                {startedRuns[strategy.id] && (
                  <p className="badge">run started: {startedRuns[strategy.id].status}</p>
                )}
              </td>
              <td>
                <button className="danger" onClick={() => stopMutation.mutate(strategy.id)}>
                  Stop
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// Three `<td>`s (Power/Safety/Instrument) rendered as siblings of the
// caller's own `<td>`s within the same `<tr>` -- each control PATCHes
// independently on change, matching stopMutation's own
// mutate-then-invalidate pattern elsewhere on this page rather than a
// single combined form with its own submit button.
function StrategyPatchControls({
  strategy,
  onSuccess,
}: {
  strategy: StrategyConfigOut
  onSuccess: () => void
}) {
  const patchMutation = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api.patch<StrategyConfigOut>(`/strategies/${strategy.id}`, body),
    onSuccess,
  })

  // Dual-Trigger Model: the Power checkbox goes through its own dedicated
  // route (not the generic PATCH above) since it has a real side effect --
  // it starts/stops the actual run, not just the is_enabled flag -- and
  // must never look like a silent no-op when it can't (trade window
  // closed, no active session today, a broker error). `detail` holds the
  // last response's explanation, shown inline right under the checkbox.
  const [powerDetail, setPowerDetail] = useState<string | null>(null)
  const powerMutation = useMutation({
    mutationFn: (is_enabled: boolean) =>
      api.post<SetStrategyPowerOut>(`/strategies/${strategy.id}/power`, { is_enabled }),
    onSuccess: (result) => {
      setPowerDetail(result.run_started || result.run_stopped ? null : result.detail)
      onSuccess()
    },
    onError: (err) =>
      setPowerDetail(err instanceof ApiError ? err.message : 'Power toggle failed'),
  })

  return (
    <>
      <td>
        <label className="row-actions" style={{ gap: '0.4rem' }}>
          <input
            type="checkbox"
            checked={strategy.is_enabled}
            disabled={powerMutation.isPending}
            onChange={(e) => powerMutation.mutate(e.target.checked)}
          />
          {strategy.is_enabled ? 'On' : 'Off'}
        </label>
        {powerDetail && (
          <p style={{ fontSize: '0.8rem', opacity: 0.75, margin: '0.2rem 0 0' }}>{powerDetail}</p>
        )}
      </td>
      <td>
        {/* Mode master-switch feature: "Live" = clear the runtime_mode
            override (null, i.e. "not force_paper") -- whether a strategy
            in that state actually trades live still depends on the
            session-level master switch (Sessions page) and every other
            gate in get_execution_broker (instrument firewall,
            ALLOW_REAL_MONEY_DISPATCH). This dropdown only ever controls
            this one strategy's own opt-in/opt-out. */}
        <select
          value={strategy.runtime_mode ?? ''}
          disabled={patchMutation.isPending}
          onChange={(e) =>
            patchMutation.mutate({
              runtime_mode: (e.target.value || null) as RuntimeMode | null,
            })
          }
        >
          <option value="">Live</option>
          <option value="force_paper">Paper</option>
        </select>
      </td>
      <td>
        <select
          value={strategy.underlying_symbol ?? ''}
          disabled={patchMutation.isPending}
          onChange={(e) =>
            patchMutation.mutate({ underlying_symbol: e.target.value || null })
          }
        >
          <option value="">Unset</option>
          {UNDERLYING_SYMBOLS.map((symbol) => (
            <option key={symbol} value={symbol}>
              {symbol}
            </option>
          ))}
        </select>
      </td>
    </>
  )
}

function StartStrategyForm({
  strategyId,
  sessions,
  instruments,
  onStarted,
  onError,
  onSuccess,
}: {
  strategyId: string
  sessions: SessionOut[]
  instruments: InstrumentOut[]
  onStarted: (run: StrategyRunOut) => void
  onError: (message: string) => void
  onSuccess: () => void
}) {
  const [sessionId, setSessionId] = useState('')
  const [instrumentId, setInstrumentId] = useState('')
  const [expiryDate, setExpiryDate] = useState('')
  const [executionMode, setExecutionMode] = useState<ExecutionMode>('auto')

  const startMutation = useMutation({
    mutationFn: () =>
      api.post<StrategyRunOut>(`/strategies/${strategyId}/start`, {
        trading_session_id: sessionId,
        instrument_id: instrumentId,
        expiry_date: expiryDate,
        execution_mode: executionMode,
      }),
    onSuccess: (run) => {
      onStarted(run)
      onSuccess()
    },
    onError: (err) => onError(err instanceof ApiError ? err.message : 'Start failed'),
  })

  const selectedInstrument = instruments.find((i) => i.id === instrumentId)

  function handleStart() {
    if (!sessionId || !instrumentId || !expiryDate) {
      onError('Session, instrument, and expiry are all required to start')
      return
    }
    startMutation.mutate()
  }

  return (
    <div className="row-actions" style={{ flexWrap: 'wrap' }}>
      <select value={sessionId} onChange={(e) => setSessionId(e.target.value)}>
        <option value="">Session...</option>
        {/* Ended sessions have no business being startable against — same
            reasoning end_session's own 409 guards enforce server-side; this
            is what actually keeps stray old sessions from cluttering this
            picker once they're ended via the Sessions page. */}
        {sessions
          .filter((s) => s.status === 'active')
          .map((s) => (
            <option key={s.id} value={s.id}>
              {s.id.slice(0, 8)} ({s.mode})
            </option>
          ))}
      </select>
      <select
        value={instrumentId}
        onChange={(e) => {
          setInstrumentId(e.target.value)
          setExpiryDate('')
        }}
      >
        <option value="">Instrument...</option>
        {instruments.map((i) => (
          <option key={i.id} value={i.id}>
            {i.symbol}
          </option>
        ))}
      </select>
      <select value={expiryDate} onChange={(e) => setExpiryDate(e.target.value)}>
        <option value="">Expiry...</option>
        {(selectedInstrument?.expiry_dates ?? []).map((date) => (
          <option key={date} value={date}>
            {date}
          </option>
        ))}
      </select>
      <select
        value={executionMode}
        onChange={(e) => setExecutionMode(e.target.value as ExecutionMode)}
      >
        <option value="auto">Auto</option>
        <option value="approval_required">Approval required</option>
      </select>
      <button disabled={startMutation.isPending} onClick={handleStart}>
        Start
      </button>
    </div>
  )
}
