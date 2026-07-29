import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../../shared/api/client'
import type {
  ExecutionMode,
  InstrumentOut,
  SessionOut,
  StrategyConfigOut,
  StrategyRunOut,
  StrategyType,
} from '../../shared/api/types'

const STRATEGY_TYPES: StrategyType[] = ['synthetic', 'orb', 'vwap_pullback', 'ema_micro_pullback']

export function StrategiesPage() {
  const queryClient = useQueryClient()
  const [createError, setCreateError] = useState<string | null>(null)
  const [startError, setStartError] = useState<string | null>(null)
  const [startedRuns, setStartedRuns] = useState<Record<string, StrategyRunOut>>({})

  const strategiesQuery = useQuery({
    queryKey: ['strategies'],
    queryFn: () => api.get<StrategyConfigOut[]>('/strategies'),
  })
  const sessionsQuery = useQuery({
    queryKey: ['sessions'],
    queryFn: () => api.get<SessionOut[]>('/sessions'),
  })
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
        {sessions.map((s) => (
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
