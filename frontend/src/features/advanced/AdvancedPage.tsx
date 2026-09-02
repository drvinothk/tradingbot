import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { api, ApiError } from '../../shared/api/client'
import { useSessions } from '../../shared/hooks/useSessions'
import { useSessionBuckets } from '../../shared/hooks/useSessionBuckets'
import { useStrategies } from '../../shared/hooks/useStrategies'
import { useInstruments } from '../../shared/hooks/useInstruments'
import { useRunningStrategies } from '../../shared/hooks/useRunningStrategies'
import { useReconciliationRuns, useSystemAlerts } from '../../shared/hooks/useRecovery'
import { useDailyLimits, useSetDailyLimits } from '../../shared/hooks/useDailyLimits'
import { type AlertIncident, groupAlertsIntoIncidents } from '../../shared/alerts/groupAlerts'
import { strategyTypeLabel } from '../../shared/format/friendlyLabel'
import type {
  DailyLimitsOut,
  ExecutionMode,
  InstrumentFirewallOut,
  InstrumentOut,
  MarketDataTelemetryOut,
  UnderlyingFeedTelemetryOut,
  VolumeProxySymbolTelemetryOut,
  MaxTradesPerDayOut,
  ProviderPreferenceOut,
  RuntimeMode,
  SessionOut,
  SetStrategyPowerOut,
  StrategyConfigOut,
  StrategyRunOut,
  StrategyType,
  UnderlyingSymbol,
} from '../../shared/api/types'

const UNDERLYING_SYMBOLS: UnderlyingSymbol[] = ['NIFTY', 'BANKNIFTY']
// "Main data provider" selector. Values must match backend
// RECOGNIZED_OVERRIDE_PROVIDERS (app/api/v1/market_data.py) -- "" clears the
// override (automatic health-based failover), "shoonya"/"alice_blue" pin the
// live feed to that leg and freeze auto-switching in BOTH directions.
// "angel_one" archived 2026-08-21 -- see CLAUDE.md's Angel One section.
const MAIN_PROVIDER_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: 'Automatic — Shoonya, fails over to Alice Blue' },
  { value: 'shoonya', label: 'Shoonya only (no failover)' },
  { value: 'alice_blue', label: 'Alice Blue only (temporary)' },
]

// The 10 real strategy types (6 original + 4 conviction-gated, added
// 2026-08-31 after Sweep #4's entry/exit-tuning results). `synthetic` (the
// Phase-2 pipeline-proof stub) is archived out of this list — its existing
// DB row is left untouched (already disabled), just no longer shown here.
// Order matters: fixed row order, not alphabetical/DB order.
const PRIMARY_STRATEGY_TYPES: StrategyType[] = [
  'orb',
  'oi_volume_confirmed',
  'ema_micro_pullback',
  'vwap_pullback',
  'liquidity_sweep_reversal',
  'orb_conviction',
  'oi_volume_confirmed_conviction',
  'ema_micro_pullback_conviction',
  'vwap_pullback_conviction',
  'liquidity_sweep_reversal_conviction',
]

export function AdvancedPage() {
  return (
    <div>
      <div className="page-header">
        <h2>Advanced</h2>
      </div>

      {/* Global, workspace-wide controls -- Daily settings, Live instrument
          firewall (NIFTY/BANKNIFTY toggles), Main data provider -- grouped
          in one ribbon at the top instead of scattered through the page,
          since none of these are tied to any one session/strategy. Grid
          wraps to 1-2 columns depending on width (see
          .global-controls-strip). */}
      <div className="global-controls-strip">
        <GlobalDailyLimitsCard />
        <GlobalSettingsCard />
      </div>

      <StrategyControlCard />
      <SystemErrorsCard />

      {/* Backend restart is the emergency-adjacent sibling of Reconciliation
          & Recovery -- kept beside it rather than at the very bottom, but
          narrower since Reconciliation & Recovery's own tables need the
          width (see .recovery-restart-row). */}
      <div className="recovery-restart-row">
        <ReconciliationAndRecoveryCard />
        <BackendRestartCard />
      </div>

      <MarketDataTelemetryCard />
      <GlobalExecutionTimingsPlaceholder />
    </div>
  )
}

// ---------- Daily settings (session Daily Plan defaults) ----------

function GlobalDailyLimitsCard() {
  const queryClient = useQueryClient()
  const { data, isLoading } = useDailyLimits()
  const setLimits = useSetDailyLimits()
  const maxTradesQuery = useQuery({
    queryKey: ['system-settings', 'max-trades-per-day'],
    queryFn: () => api.get<MaxTradesPerDayOut>('/system-settings/max-trades-per-day'),
  })
  const setMaxTrades = useMutation({
    mutationFn: (max_trades_per_day: number) =>
      api.patch<MaxTradesPerDayOut>('/system-settings/max-trades-per-day', { max_trades_per_day }),
    onSuccess: (data) => queryClient.setQueryData(['system-settings', 'max-trades-per-day'], data),
  })
  // `null` means "not yet touched by the user" -- falls back to the
  // fetched value. Once the user types anything, including clearing the
  // field to '', the state itself becomes the source of truth (`??`, not
  // `||`, so an emptied string doesn't snap back to the fetched value the
  // way it used to, which made the field impossible to fully clear before
  // typing a new number).
  const [budget, setBudget] = useState<string | null>(null)
  const [target, setTarget] = useState<string | null>(null)
  const [lossCap, setLossCap] = useState<string | null>(null)
  const [maxTrades, setMaxTradesInput] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const effectiveBudget = budget ?? (data ? String(data.daily_budget_amount) : '')
  const effectiveTarget = target ?? (data ? String(data.daily_target_profit) : '')
  const effectiveLossCap = lossCap ?? (data ? String(data.daily_loss_cap) : '')
  const effectiveMaxTrades =
    maxTrades ?? (maxTradesQuery.data ? String(maxTradesQuery.data.max_trades_per_day) : '')

  async function handleSave(event: FormEvent) {
    event.preventDefault()
    setError(null)
    const body: DailyLimitsOut = {
      daily_budget_amount: Number(effectiveBudget),
      daily_target_profit: Number(effectiveTarget),
      daily_loss_cap: Number(effectiveLossCap),
      // Not exposed here -- funding mode is a broker-level concern, not
      // something this app should let a user toggle per day.
      funding_mode: 'cash',
    }
    const maxTradesValue = Number(effectiveMaxTrades)
    if (!body.daily_budget_amount || !body.daily_target_profit || !body.daily_loss_cap) {
      setError('Budget, target profit, and loss cap are all required and must be greater than zero.')
      return
    }
    if (!maxTradesValue || !Number.isInteger(maxTradesValue)) {
      setError('Max trades / strategy / day must be a whole number greater than zero.')
      return
    }
    try {
      await Promise.all([
        setLimits.mutateAsync(body),
        setMaxTrades.mutateAsync(maxTradesValue),
      ])
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Save failed')
    }
  }

  const loading = isLoading || maxTradesQuery.isLoading
  const saving = setLimits.isPending || setMaxTrades.isPending

  return (
    <div className="card">
      <h3>Daily settings</h3>
      <p className="muted">
        Default budget / target profit / loss cap for a newly-created session's{' '}
        <strong>Daily plan</strong> (Reconciliation &amp; Recovery, below) — that Daily Plan is what
        Risk Service actually enforces live. Editing this changes what the <em>next</em> new session
        starts with; today's already-active session stays independently editable and untouched. Max
        trades / strategy / day applies immediately, across every strategy.
      </p>
      <form onSubmit={handleSave} className="row-actions" style={{ alignItems: 'flex-end', flexWrap: 'wrap' }}>
        <div className="form-row" style={{ marginBottom: 0 }}>
          <label htmlFor="daily-budget">Budget (₹)</label>
          <input
            id="daily-budget"
            type="number"
            disabled={loading}
            value={effectiveBudget}
            onChange={(e) => setBudget(e.target.value)}
            style={{ width: '110px' }}
          />
        </div>
        <div className="form-row" style={{ marginBottom: 0 }}>
          <label htmlFor="daily-target">Target profit (₹)</label>
          <input
            id="daily-target"
            type="number"
            disabled={loading}
            value={effectiveTarget}
            onChange={(e) => setTarget(e.target.value)}
            style={{ width: '110px' }}
          />
        </div>
        <div className="form-row" style={{ marginBottom: 0 }}>
          <label htmlFor="daily-loss-cap">Loss cap (₹)</label>
          <input
            id="daily-loss-cap"
            type="number"
            disabled={loading}
            value={effectiveLossCap}
            onChange={(e) => setLossCap(e.target.value)}
            style={{ width: '110px' }}
          />
        </div>
        <div className="form-row" style={{ marginBottom: 0 }}>
          <label htmlFor="daily-max-trades">Max trades / strategy / day</label>
          <input
            id="daily-max-trades"
            type="number"
            min={1}
            disabled={loading}
            value={effectiveMaxTrades}
            onChange={(e) => setMaxTradesInput(e.target.value)}
            style={{ width: '110px' }}
          />
        </div>
        <button type="submit" disabled={saving || loading}>
          Save
        </button>
      </form>
      {error && <p className="error">{error}</p>}
    </div>
  )
}

// ---------- Strategy Control ----------

function StrategyControlCard() {
  const queryClient = useQueryClient()
  const strategiesQuery = useStrategies()
  const runningQuery = useRunningStrategies()
  const instrumentsQuery = useInstruments()
  const { liveSession, paperSession } = useSessionBuckets()

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['strategies'] })
    queryClient.invalidateQueries({ queryKey: ['strategies', 'running'] })
  }

  const strategies = strategiesQuery.data ?? []
  const runs = runningQuery.data ?? []
  const instruments = instrumentsQuery.data ?? []

  // Exactly one StrategyConfig per type is the common case this app runs
  // today (per the plan: "these are the user's real current StrategyConfigs
  // ... name to be dropped from UI"). If more than one config shares a
  // type, every one of them renders as its own sub-row under that type's
  // header instead of only the first — "sub-rows appear only once a 2nd
  // concurrent instrument run starts" from the plan, generalized to "more
  // than one config of this type" so nothing is silently hidden.
  const byType = new Map<string, StrategyConfigOut[]>()
  for (const s of strategies) {
    const list = byType.get(s.strategy_type) ?? []
    list.push(s)
    byType.set(s.strategy_type, list)
  }

  return (
    <div className="card">
      <h3>Strategy Control</h3>
      <p className="muted">
        Power-on always starts in Auto mode (server-side <code>spawn_one_now</code>) — the
        "Execution mode" select on each row only applies to the explicit Start button below it, not
        to Power.
      </p>
      {/* Only ~2 strategy-type groups visible at once -- the rest scroll
          inside this frame instead of growing the whole page. Per-row
          Mode/Instrument/Execution-mode selects are unchanged. */}
      <div className="strategy-scroll">
        {PRIMARY_STRATEGY_TYPES.map((type) => (
          <StrategyTypeGroup
            key={type}
            type={type}
            configs={byType.get(type) ?? []}
            runs={runs}
            instruments={instruments}
            liveSession={liveSession}
            paperSession={paperSession}
            onChanged={invalidate}
          />
        ))}
      </div>

      <CreateStrategyDefinitionRow onCreated={invalidate} />
    </div>
  )
}

function StrategyTypeGroup({
  type,
  configs,
  runs,
  instruments,
  liveSession,
  paperSession,
  onChanged,
}: {
  type: StrategyType
  configs: StrategyConfigOut[]
  runs: import('../../shared/api/types').RunningStrategyOut[]
  instruments: InstrumentOut[]
  liveSession: SessionOut | null
  paperSession: SessionOut | null
  onChanged: () => void
}) {
  // Real data untouched either way -- this only changes what's rendered.
  // Disabled configs (almost always old test/leftover ones once a type has
  // more than one) fold behind "show more" instead of always cluttering
  // the type's row list; an enabled config always shows regardless of this
  // toggle, since that's the one actually running or ready to run.
  const [showDisabled, setShowDisabled] = useState(false)
  const enabledConfigs = configs.filter((c) => c.is_enabled)
  const disabledConfigs = configs.filter((c) => !c.is_enabled)
  const visibleConfigs = showDisabled ? configs : enabledConfigs

  return (
    <div style={{ marginBottom: '1rem' }}>
      <div className="section-title section-title-bright">{strategyTypeLabel(type)}</div>
      {configs.length === 0 ? (
        <p className="muted">No strategy config of this type exists yet.</p>
      ) : (
        <>
          {visibleConfigs.length === 0 ? (
            <p className="muted">No enabled config of this type — {disabledConfigs.length} disabled.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Power</th>
                  <th>Mode</th>
                  <th>Instrument</th>
                  <th>Lots</th>
                  <th>Running</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {visibleConfigs.map((config) => (
                  <StrategyConfigRow
                    key={config.id}
                    config={config}
                    run={runs.find((r) => r.strategy_config_id === config.id) ?? null}
                    instruments={instruments}
                    liveSession={liveSession}
                    paperSession={paperSession}
                    onChanged={onChanged}
                  />
                ))}
              </tbody>
            </table>
          )}
          {disabledConfigs.length > 0 && (
            <button className="btn-ghost" onClick={() => setShowDisabled((v) => !v)}>
              {showDisabled ? 'Hide' : `Show ${disabledConfigs.length} more (disabled)`}
            </button>
          )}
        </>
      )}
    </div>
  )
}

function StrategyConfigRow({
  config,
  run,
  instruments,
  liveSession,
  paperSession,
  onChanged,
}: {
  config: StrategyConfigOut
  run: import('../../shared/api/types').RunningStrategyOut | null
  instruments: InstrumentOut[]
  liveSession: SessionOut | null
  paperSession: SessionOut | null
  onChanged: () => void
}) {
  const queryClient = useQueryClient()
  const [powerDetail, setPowerDetail] = useState<string | null>(null)
  const [executionMode, setExecutionMode] = useState<ExecutionMode>('auto')
  const [startError, setStartError] = useState<string | null>(null)
  // `null` = not yet touched, falls back to the server value -- same
  // pattern as GlobalDailyLimitsCard's budget/target/loss-cap fields.
  const [lotsInput, setLotsInput] = useState<string | null>(null)

  // Both mutations below write the real server response straight into the
  // `['strategies']` list cache via setQueryData, same pattern
  // firewallMutation/useDailyLimits already use on this page -- an
  // invalidate-only onSuccess (the previous behavior here) leaves the
  // control showing its pre-mutation value until the background refetch
  // resolves, which visibly snaps the checkbox/select back before
  // snapping forward again a moment later.
  const patchMutation = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api.patch<StrategyConfigOut>(`/strategies/${config.id}`, body),
    onSuccess: (updated) => {
      queryClient.setQueryData<StrategyConfigOut[]>(['strategies'], (prev) =>
        prev ? prev.map((s) => (s.id === updated.id ? updated : s)) : prev,
      )
      onChanged()
    },
  })

  const powerMutation = useMutation({
    mutationFn: (is_enabled: boolean) =>
      api.post<SetStrategyPowerOut>(`/strategies/${config.id}/power`, { is_enabled }),
    onSuccess: (result, is_enabled) => {
      queryClient.setQueryData<StrategyConfigOut[]>(['strategies'], (prev) =>
        prev ? prev.map((s) => (s.id === config.id ? { ...s, is_enabled } : s)) : prev,
      )
      setPowerDetail(result.run_started || result.run_stopped ? null : result.detail)
      onChanged()
    },
    onError: (err) => setPowerDetail(err instanceof ApiError ? err.message : 'Power toggle failed'),
  })

  const stopMutation = useMutation({
    mutationFn: () => api.post(`/strategies/${config.id}/stop`),
    onSuccess: onChanged,
  })

  // Single Start control per row -- both `trading_session_id` and
  // `expiry_date` (which POST /strategies/{id}/start genuinely requires
  // explicitly) are resolved silently from data already on screen rather
  // than asked for again in a second form:
  //   - session: this row's own "Mode" select (config.runtime_mode) already
  //     says Live vs Paper: force_paper -> the Paper session bucket,
  //     otherwise the Live bucket (useSessionBuckets, same bucketing
  //     Control Room's header uses).
  //   - instrument: this row's own "Instrument" select (config
  //     .underlying_symbol) already names the underlying; resolved here to
  //     the matching Instrument row.
  //   - expiry: the nearest (soonest) date in that instrument's own
  //     expiry_dates (GET /instruments, already sorted ascending and
  //     filtered to is_active + >= today) -- the exact same query
  //     `strategy_engine.auto_spawner.resolve_nearest_expiry` runs
  //     server-side for every cron/login auto-spawn and for the Power
  //     toggle's own spawn_one_now path, so this isn't a new policy, just
  //     the codebase's existing "nearest expiry" convention applied
  //     client-side. Execution mode is the one field genuinely left to the
  //     user, since spawn_one_now always hardcodes AUTO with no
  //     approval_required option.
  const startMutation = useMutation({
    mutationFn: (body: {
      trading_session_id: string
      instrument_id: string
      expiry_date: string
      execution_mode: ExecutionMode
    }) => api.post<StrategyRunOut>(`/strategies/${config.id}/start`, { ...body, interval_seconds: 30 }),
    onSuccess: () => {
      setStartError(null)
      onChanged()
    },
    onError: (err) => setStartError(err instanceof ApiError ? err.message : 'Start failed'),
  })

  const instrument = instruments.find((i) => i.symbol === config.underlying_symbol)
  const isPaperMode = config.runtime_mode === 'force_paper'
  const targetSession = isPaperMode ? paperSession : liveSession
  const expiryDate = instrument?.expiry_dates[0] ?? null

  function handleStart() {
    if (!config.underlying_symbol || !instrument) {
      setStartError('Set Instrument for this strategy (above) before starting.')
      return
    }
    if (!targetSession) {
      setStartError(
        `No active ${isPaperMode ? 'Paper' : 'Live'} session -- start one in Control Room first.`,
      )
      return
    }
    if (!expiryDate) {
      setStartError(`No active listed expiry for ${instrument.symbol}.`)
      return
    }
    setStartError(null)
    startMutation.mutate({
      trading_session_id: targetSession.id,
      instrument_id: instrument.id,
      expiry_date: expiryDate,
      execution_mode: executionMode,
    })
  }

  return (
    <>
      <tr>
        <td>
          <label className="row-actions" style={{ gap: '0.4rem' }}>
            <input
              type="checkbox"
              checked={config.is_enabled}
              disabled={powerMutation.isPending}
              onChange={(e) => powerMutation.mutate(e.target.checked)}
            />
            {config.is_enabled ? 'On' : 'Off'}
          </label>
        </td>
        <td>
          <select
            value={config.runtime_mode ?? ''}
            disabled={patchMutation.isPending}
            onChange={(e) =>
              patchMutation.mutate({ runtime_mode: (e.target.value || null) as RuntimeMode | null })
            }
          >
            <option value="">Live</option>
            <option value="force_paper">Paper</option>
          </select>
        </td>
        <td>
          <select
            value={config.underlying_symbol ?? ''}
            disabled={patchMutation.isPending}
            onChange={(e) => patchMutation.mutate({ underlying_symbol: e.target.value || null })}
          >
            <option value="">Unset</option>
            {UNDERLYING_SYMBOLS.map((symbol) => (
              <option key={symbol} value={symbol}>
                {symbol}
              </option>
            ))}
          </select>
        </td>
        <td>
          <input
            type="number"
            min={1}
            className="lots-input"
            placeholder={isPaperMode ? '10' : '1'}
            value={lotsInput ?? (typeof config.params.qty_lots === 'number' ? String(config.params.qty_lots) : '')}
            disabled={patchMutation.isPending}
            onChange={(e) => setLotsInput(e.target.value)}
            onBlur={() => {
              if (lotsInput === null) return
              const trimmed = lotsInput.trim()
              if (trimmed === '') {
                patchMutation.mutate({ qty_lots: null })
              } else {
                const parsed = Number(trimmed)
                // Not a valid positive integer -- revert silently rather than
                // sending NaN (which JSON.stringify would silently turn into
                // null, clearing the override instead of rejecting the input).
                if (Number.isInteger(parsed) && parsed > 0) {
                  patchMutation.mutate({ qty_lots: parsed })
                }
              }
              setLotsInput(null)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
            }}
          />
        </td>
        <td>
          {run ? (
            <span className="badge badge-success">{run.status}</span>
          ) : (
            <span className="muted">idle</span>
          )}
        </td>
        <td>
          <div className="row-actions">
            {run ? (
              <button className="btn-stop" disabled={stopMutation.isPending} onClick={() => stopMutation.mutate()}>
                Stop
              </button>
            ) : (
              <>
                <select
                  value={executionMode}
                  disabled={startMutation.isPending}
                  onChange={(e) => setExecutionMode(e.target.value as ExecutionMode)}
                >
                  <option value="auto">Auto</option>
                  <option value="approval_required">Approval required</option>
                </select>
                <button className="btn-start" disabled={startMutation.isPending} onClick={handleStart}>
                  Start
                </button>
              </>
            )}
          </div>
        </td>
      </tr>
      {(powerDetail || startError) && (
        <tr>
          <td colSpan={7} className="muted" style={{ fontSize: '0.8rem' }}>
            {startError ?? powerDetail}
          </td>
        </tr>
      )}
    </>
  )
}

function CreateStrategyDefinitionRow({ onCreated }: { onCreated: () => void }) {
  const [expanded, setExpanded] = useState(false)
  const [strategyType, setStrategyType] = useState<StrategyType>('orb')
  const [paramsText, setParamsText] = useState('{}')
  const [error, setError] = useState<string | null>(null)

  const createMutation = useMutation({
    mutationFn: () => {
      let params: Record<string, unknown>
      try {
        params = JSON.parse(paramsText || '{}')
      } catch {
        throw new Error('Params must be valid JSON')
      }
      // name intentionally omitted -- server auto-generates one, per the
      // plan's decision #5 ("consistent with dropping name everywhere else
      // ... just Type + Params").
      return api.post<StrategyConfigOut>('/strategies', { strategy_type: strategyType, params })
    },
    onSuccess: () => {
      onCreated()
      setParamsText('{}')
      setExpanded(false)
      setError(null)
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : (err as Error).message ?? 'Create failed'),
  })

  return (
    <div className="section-title" style={{ marginTop: '1.5rem' }}>
      <div className="collapsible-header" onClick={() => setExpanded((v) => !v)}>
        <span>Create Strategy Definition</span>
        <span className={`chevron ${expanded ? 'open' : ''}`}>▶</span>
      </div>
      {expanded && (
        <div className="row-actions" style={{ marginTop: '0.5rem', flexWrap: 'wrap' }}>
          <select value={strategyType} onChange={(e) => setStrategyType(e.target.value as StrategyType)}>
            {[
              'orb',
              'orb_conviction',
              'vwap_pullback',
              'vwap_pullback_conviction',
              'ema_micro_pullback',
              'ema_micro_pullback_conviction',
              'oi_volume_confirmed',
              'oi_volume_confirmed_conviction',
              'liquidity_sweep_reversal',
              'liquidity_sweep_reversal_conviction',
            ].map((type) => (
              <option key={type} value={type}>
                {strategyTypeLabel(type)}
              </option>
            ))}
          </select>
          <textarea
            rows={2}
            style={{ minWidth: '260px' }}
            value={paramsText}
            onChange={(e) => setParamsText(e.target.value)}
          />
          <button disabled={createMutation.isPending} onClick={() => createMutation.mutate()}>
            Create
          </button>
          {error && <span className="error">{error}</span>}
        </div>
      )}
    </div>
  )
}

// ---------- System errors ----------

function SystemErrorsCard() {
  const alertsQuery = useSystemAlerts()
  const [expanded, setExpanded] = useState(false)
  const [openIncidentKey, setOpenIncidentKey] = useState<string | null>(null)
  const alerts = alertsQuery.data ?? []
  const incidents = groupAlertsIntoIncidents(alerts)
  const visible = expanded ? incidents : incidents.slice(0, 5)

  return (
    <div className="card">
      <h3>System errors</h3>
      {incidents.length > 0 && (
        <p className="muted">
          Repeated occurrences of the same issue are grouped into one incident — use "Details" to
          see every individual occurrence.
        </p>
      )}
      {alertsQuery.isLoading ? (
        <p className="muted">Loading...</p>
      ) : incidents.length === 0 ? (
        <p className="muted">None — nothing currently needs attention.</p>
      ) : (
        <>
          <table>
            <thead>
              <tr>
                <th>Severity</th>
                <th>Category</th>
                <th>Message</th>
                <th>Count</th>
                <th>Last seen</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {visible.map((incident) => (
                <IncidentRows
                  key={incident.key}
                  incident={incident}
                  isOpen={openIncidentKey === incident.key}
                  onToggle={() =>
                    setOpenIncidentKey((k) => (k === incident.key ? null : incident.key))
                  }
                />
              ))}
            </tbody>
          </table>
          {incidents.length > 5 && (
            <button className="btn-ghost" onClick={() => setExpanded((v) => !v)}>
              {expanded
                ? 'Show fewer'
                : `Show all ${incidents.length} incidents (${alerts.length} events)`}
            </button>
          )}
        </>
      )}
    </div>
  )
}

function IncidentRows({
  incident,
  isOpen,
  onToggle,
}: {
  incident: AlertIncident
  isOpen: boolean
  onToggle: () => void
}) {
  return (
    <>
      <tr>
        <td>
          <span className={incident.severity === 'critical' ? 'badge badge-live' : 'badge'}>
            {incident.severity}
          </span>
        </td>
        <td>{incident.category}</td>
        <td>{incident.message}</td>
        <td>{incident.count > 1 ? `×${incident.count}` : '—'}</td>
        <td>{new Date(incident.lastSeen).toLocaleTimeString()}</td>
        <td>
          {incident.count > 1 && (
            <button className="btn-ghost" onClick={onToggle}>
              {isOpen ? 'Hide' : 'Details'}
            </button>
          )}
        </td>
      </tr>
      {isOpen && (
        <tr className="trade-row-legs">
          <td colSpan={6}>
            <table className="leg-detail-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Message</th>
                </tr>
              </thead>
              <tbody>
                {incident.occurrences
                  .slice()
                  .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
                  .map((occurrence) => (
                    <tr key={occurrence.id}>
                      <td>{new Date(occurrence.created_at).toLocaleTimeString()}</td>
                      <td>{occurrence.message}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </td>
        </tr>
      )}
    </>
  )
}

// ---------- Global Execution Timings (WIP) ----------

function GlobalExecutionTimingsPlaceholder() {
  return (
    <div className="card">
      <h3>
        Global Execution Timings <span className="badge badge-wip">WIP</span>
      </h3>
      <div className="wip-panel">
        <h4>Needs its own safety design pass before this becomes editable</h4>
        <p>
          Today's trade-window/cutoff timings are hardcoded/env-var driven, not runtime-editable.
          Per the plan, exposing a live editor here needs a dedicated safety review first (e.g. a
          type-to-confirm gate like Go Live's) — not built yet.
        </p>
      </div>
    </div>
  )
}

// ---------- Reconciliation & Recovery ----------

function ReconciliationAndRecoveryCard() {
  const queryClient = useQueryClient()
  const sessionsQuery = useSessions()
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null)
  const reconciliationQuery = useReconciliationRuns(selectedSessionId)
  const [error, setError] = useState<string | null>(null)
  // Ended sessions accumulate one per trading day, most with nothing
  // actionable (only 'active' ones get buttons) -- fold the rest behind
  // "show more" instead of always listing every one. Nothing here deletes
  // or filters the underlying data (SessionOut has no timestamp field to
  // sort by), it's purely how many of the already-returned rows render.
  const [showAllSessions, setShowAllSessions] = useState(false)
  // Sessions are already returned most-recent-first (list_sessions orders by
  // started_at desc) -- showing just the last one by default is "today's
  // session" at a glance, with older ones folded into "show more".
  const SESSIONS_VISIBLE_DEFAULT = 1

  const sessions = sessionsQuery.data ?? []
  const visibleSessions = showAllSessions ? sessions : sessions.slice(0, SESSIONS_VISIBLE_DEFAULT)
  const hiddenSessionCount = sessions.length - visibleSessions.length
  const invalidateSessions = () => queryClient.invalidateQueries({ queryKey: ['sessions'] })

  const squareOffMutation = useMutation({
    mutationFn: (sessionId: string) => api.post(`/sessions/${sessionId}/square-off`),
    onSuccess: invalidateSessions,
    onError: (err) => setError(err instanceof ApiError ? err.message : 'Square-off failed'),
  })
  const reconcileMutation = useMutation({
    mutationFn: (sessionId: string) => api.post(`/sessions/${sessionId}/reconcile`),
    onSuccess: invalidateSessions,
    onError: (err) => setError(err instanceof ApiError ? err.message : 'Reconcile failed'),
  })
  const endMutation = useMutation({
    mutationFn: (sessionId: string) => api.post(`/sessions/${sessionId}/end`),
    onSuccess: invalidateSessions,
    onError: (err) => setError(err instanceof ApiError ? err.message : 'End session failed'),
  })
  const recoverMutation = useMutation({
    mutationFn: (sessionId: string) => api.post(`/sessions/${sessionId}/recover-from-kill-switch`),
    onSuccess: invalidateSessions,
    onError: (err) => setError(err instanceof ApiError ? err.message : 'Recover failed'),
  })
  const recoverFromDegradedMutation = useMutation({
    mutationFn: (sessionId: string) => api.post(`/sessions/${sessionId}/recover-from-degraded`),
    onSuccess: invalidateSessions,
    onError: (err) => setError(err instanceof ApiError ? err.message : 'Recover from degraded failed'),
  })
  const recoverFromReconciliationLockMutation = useMutation({
    mutationFn: (sessionId: string) =>
      api.post<{ recovered: boolean; mismatches_found?: number }>(
        `/sessions/${sessionId}/recover-from-reconciliation-lock`,
      ),
    onSuccess: (result) => {
      invalidateSessions()
      if (!result.recovered) {
        setError(`Still ${result.mismatches_found ?? 'some'} mismatch(es) — not recovered.`)
      } else {
        setError(null)
      }
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : 'Recover from reconciliation_lock failed'),
  })

  const dailyPlanMutation = useMutation({
    mutationFn: ({ sessionId, body }: { sessionId: string; body: Record<string, unknown> }) =>
      api.post<SessionOut>(`/sessions/${sessionId}/daily-plan`, body),
    onSuccess: invalidateSessions,
    onError: (err) => setError(err instanceof ApiError ? err.message : 'Daily plan update failed'),
  })

  return (
    <div className="card">
      <h3>Reconciliation &amp; Recovery</h3>
      <p className="muted">
        Each active session's <strong>Daily plan</strong> below (budget / target / loss cap) is
        enforced live by Risk Service. The <strong>Daily settings</strong> card at the top of the
        page only sets the defaults a <em>new</em> session starts with.
      </p>

      {error && <p className="error">{error}</p>}

      <table>
        <thead>
          <tr>
            <th>Session</th>
            <th>Status</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {visibleSessions.map((session) => (
            <tr key={session.id}>
              <td>
                <span className="badge">{session.mode}</span>
              </td>
              <td>{session.status}</td>
              <td>
                <div className="row-actions">
                  {session.status === 'active' && (
                    <>
                      <button className="danger" onClick={() => squareOffMutation.mutate(session.id)}>
                        Square off
                      </button>
                      <button onClick={() => reconcileMutation.mutate(session.id)}>Reconcile</button>
                      {session.mode === 'kill_switch' && (
                        <button
                          className="danger"
                          disabled={recoverMutation.isPending}
                          onClick={() => recoverMutation.mutate(session.id)}
                        >
                          Recover
                        </button>
                      )}
                      {session.mode === 'degraded_mode' && (
                        <button
                          className="danger"
                          disabled={recoverFromDegradedMutation.isPending}
                          onClick={() => recoverFromDegradedMutation.mutate(session.id)}
                        >
                          Recover
                        </button>
                      )}
                      {session.mode === 'reconciliation_lock' && (
                        <button
                          className="danger"
                          disabled={recoverFromReconciliationLockMutation.isPending}
                          onClick={() => recoverFromReconciliationLockMutation.mutate(session.id)}
                        >
                          Recover
                        </button>
                      )}
                      <button
                        className="danger"
                        disabled={endMutation.isPending}
                        onClick={() => endMutation.mutate(session.id)}
                      >
                        End session
                      </button>
                      <DailyPlanEditor
                        session={session}
                        onSave={(body) => dailyPlanMutation.mutate({ sessionId: session.id, body })}
                        isPending={dailyPlanMutation.isPending}
                      />
                    </>
                  )}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {hiddenSessionCount > 0 && (
        <button className="btn-ghost" onClick={() => setShowAllSessions(true)}>
          Show {hiddenSessionCount} more
        </button>
      )}
      {showAllSessions && sessions.length > SESSIONS_VISIBLE_DEFAULT && (
        <button className="btn-ghost" onClick={() => setShowAllSessions(false)}>
          Hide
        </button>
      )}

      <div className="section-title">Reconciliation history</div>
      <div className="form-row">
        <label htmlFor="recovery-session">Session</label>
        <select
          id="recovery-session"
          value={selectedSessionId ?? ''}
          onChange={(e) => setSelectedSessionId(e.target.value || null)}
        >
          <option value="">Select a session...</option>
          {sessions.map((session) => (
            <option key={session.id} value={session.id}>
              {session.mode} — {session.status}
            </option>
          ))}
        </select>
      </div>
      {selectedSessionId === null ? (
        <p className="muted">Pick a session to see its reconciliation history.</p>
      ) : reconciliationQuery.isLoading ? (
        <p className="muted">Loading...</p>
      ) : (
        <>
          <h4>Current mismatches</h4>
          {(reconciliationQuery.data?.current_mismatches.length ?? 0) === 0 ? (
            <p className="muted">None — local and broker positions agree.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Local qty</th>
                  <th>Broker qty</th>
                  <th>Checked</th>
                </tr>
              </thead>
              <tbody>
                {reconciliationQuery.data?.current_mismatches.map((mismatch) => (
                  <tr key={mismatch.option_contract_id}>
                    <td>{mismatch.local_qty}</td>
                    <td>{mismatch.broker_qty}</td>
                    <td>{new Date(mismatch.checked_at).toLocaleTimeString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <h4>Recent runs</h4>
          {(reconciliationQuery.data?.runs.length ?? 0) === 0 ? (
            <p className="muted">No reconciliation runs recorded yet for this session.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Trigger</th>
                  <th>Mismatches found</th>
                  <th>Action taken</th>
                  <th>Finished</th>
                </tr>
              </thead>
              <tbody>
                {reconciliationQuery.data?.runs.map((run) => (
                  <tr key={run.id}>
                    <td>{run.trigger_type}</td>
                    <td>{run.mismatches_found}</td>
                    <td>{run.action_taken}</td>
                    <td>{new Date(run.finished_at).toLocaleTimeString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  )
}

function DailyPlanEditor({
  session,
  onSave,
  isPending,
}: {
  session: SessionOut
  onSave: (body: Record<string, unknown>) => void
  isPending: boolean
}) {
  const [expanded, setExpanded] = useState(false)
  const [target, setTarget] = useState('')
  const [lossCap, setLossCap] = useState('')
  const [budget, setBudget] = useState('')

  if (!expanded) {
    return (
      <button className="btn-ghost" onClick={() => setExpanded(true)}>
        Daily plan...
      </button>
    )
  }

  return (
    <div className="row-actions">
      <input
        type="number"
        placeholder="Budget"
        value={budget}
        onChange={(e) => setBudget(e.target.value)}
        style={{ width: '100px' }}
      />
      <input
        type="number"
        placeholder="Target profit"
        value={target}
        onChange={(e) => setTarget(e.target.value)}
        style={{ width: '110px' }}
      />
      <input
        type="number"
        placeholder="Loss cap"
        value={lossCap}
        onChange={(e) => setLossCap(e.target.value)}
        style={{ width: '100px' }}
      />
      <button
        disabled={isPending || !budget || !target || !lossCap}
        onClick={() =>
          onSave({
            budget_amount: Number(budget),
            daily_target_profit: Number(target),
            daily_loss_cap: Number(lossCap),
            funding_mode: 'cash',
          })
        }
      >
        Save
      </button>
      <button className="btn-ghost" onClick={() => setExpanded(false)}>
        Cancel
      </button>
      <span className="muted" style={{ fontSize: '0.75rem' }}>
        session {session.mode}
      </span>
    </div>
  )
}

// ---------- Global settings (firewall + failover) ----------

function GlobalSettingsCard() {
  const queryClient = useQueryClient()
  const [error, setError] = useState<string | null>(null)

  const firewallQuery = useQuery({
    queryKey: ['system-settings', 'instrument-firewall'],
    queryFn: () => api.get<InstrumentFirewallOut>('/system-settings/instrument-firewall'),
  })
  const providerPrefQuery = useQuery({
    queryKey: ['market-data', 'provider-preference'],
    queryFn: () => api.get<ProviderPreferenceOut>('/market-data/provider-preference'),
  })

  const firewallMutation = useMutation({
    mutationFn: (active_live_instruments: string[]) =>
      api.patch<InstrumentFirewallOut>('/system-settings/instrument-firewall', {
        active_live_instruments,
      }),
    onSuccess: (data) => queryClient.setQueryData(['system-settings', 'instrument-firewall'], data),
    onError: (err) => setError(err instanceof ApiError ? err.message : 'Firewall update failed'),
  })

  const providerPrefMutation = useMutation({
    mutationFn: (active_provider: string | null) =>
      api.patch<ProviderPreferenceOut>('/market-data/provider-preference', { active_provider }),
    onSuccess: (data) => queryClient.setQueryData(['market-data', 'provider-preference'], data),
    onError: (err) => {
      const msg = err instanceof ApiError ? err.message : 'Provider preference update failed'
      setError(
        /subscribe backup provider 'alice_blue'/.test(msg)
          ? "Alice Blue isn't connected — connect it on the Market Terminal, then retry."
          : msg,
      )
    },
  })

  const activeLiveInstruments = firewallQuery.data?.active_live_instruments ?? []

  function toggleInstrument(symbol: UnderlyingSymbol, checked: boolean) {
    const next = checked
      ? [...activeLiveInstruments, symbol]
      : activeLiveInstruments.filter((s) => s !== symbol)
    firewallMutation.mutate(next)
  }

  return (
    <div className="card">
      <h3>Global settings</h3>
      {error && <p className="error">{error}</p>}

      {/* Two halves divided by a vertical line rather than stacked rows --
          keeps this card's height in line with Daily settings next to it
          in the ribbon. */}
      <div className="global-settings-split">
        <div className="row-actions">
          {UNDERLYING_SYMBOLS.map((symbol) => (
            <label key={symbol} style={{ display: 'flex', gap: '0.3rem', alignItems: 'center' }}>
              <input
                type="checkbox"
                checked={activeLiveInstruments.includes(symbol)}
                disabled={firewallMutation.isPending || firewallQuery.isLoading}
                onChange={(e) => toggleInstrument(symbol, e.target.checked)}
              />
              {symbol}
            </label>
          ))}
        </div>

        <div className="form-row" style={{ marginBottom: 0 }}>
          <label htmlFor="failover-override">Main data provider</label>
          <select
            id="failover-override"
            value={providerPrefQuery.data?.active_provider ?? ''}
            disabled={providerPrefMutation.isPending || providerPrefQuery.isLoading}
            onChange={(e) => providerPrefMutation.mutate(e.target.value || null)}
          >
            {MAIN_PROVIDER_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
          {providerPrefQuery.data?.live_active_leg && (
            <span className="badge" style={{ marginLeft: '0.5rem' }}>
              live: {providerPrefQuery.data.live_active_leg}
            </span>
          )}
        </div>
      </div>
      <p className="muted" style={{ fontSize: '0.75rem', margin: '0.5rem 0 0' }}>
        Automatic is the only self-healing mode. "Shoonya only" / "Alice Blue only" pin the feed and
        disable switching both ways. "Alice Blue only" needs a live Alice Blue session — connect it
        on the Market Terminal first.
      </p>
    </div>
  )
}

// ---------- Market data telemetry ----------

const FEED_STATE_LABEL: Record<string, string> = {
  live: 'Live',
  degraded: 'Degraded',
  stale: 'Stale',
  dead: 'Dead',
}

function feedStateBadgeClass(state: string): string {
  if (state === 'live') return 'badge badge-success'
  if (state === 'degraded') return 'badge badge-warning'
  return 'badge badge-live' // stale/dead -- same "needs attention" red as elsewhere on this page
}

function formatFeedAge(seconds: number | null): string {
  if (seconds === null) return 'no data'
  if (seconds < 60) return `${Math.round(seconds)}s ago`
  return `${Math.round(seconds / 60)}m ago`
}

function formatIndicatorValue(value: number | null): string {
  return value !== null ? value.toFixed(2) : '—'
}

function formatPcr(u: UnderlyingFeedTelemetryOut): string {
  // No option-chain snapshot yet for this (underlying, nearest expiry) --
  // e.g. nothing has scanned it today -- or this symbol has no option chain
  // at all (INDIA VIX). See get_market_data_telemetry's own docstring.
  if (u.pcr_oi === null && u.pcr_vol === null) return '—'
  const oi = u.pcr_oi !== null ? u.pcr_oi.toFixed(2) : '—'
  const vol = u.pcr_vol !== null ? u.pcr_vol.toFixed(2) : '—'
  const age = u.pcr_age_seconds !== null ? ` (${formatFeedAge(u.pcr_age_seconds)})` : ''
  return `${oi} oi / ${vol} vol${age}`
}

function MarketDataTelemetryCard() {
  const telemetryQuery = useQuery({
    queryKey: ['market-data', 'telemetry'],
    queryFn: () => api.get<MarketDataTelemetryOut>('/market-data/telemetry'),
    refetchInterval: 15_000,
  })
  // Same queryKey GlobalSettingsCard's providerPrefQuery uses -- shares its
  // cache/network request rather than fetching this twice per page load.
  const providerPrefQuery = useQuery({
    queryKey: ['market-data', 'provider-preference'],
    queryFn: () => api.get<ProviderPreferenceOut>('/market-data/provider-preference'),
  })

  const underlyings = telemetryQuery.data?.underlyings ?? []
  const calculatedSymbols = telemetryQuery.data?.calculated_symbols ?? []

  return (
    <div className="card">
      <h3>Market Data Adapter telemetry</h3>
      <p className="muted">
        Live per-underlying feed freshness, reusing the same staleness classification the trading
        gates themselves use — not a separate counter system. Also lists every tradable/metric
        symbol (including INDIA VIX) plus the computed indicator/PCR values already persisted for
        each, even where no strategy gates on them yet, plus any calculated symbol streamed purely
        to feed another symbol's indicators (e.g. the front-month future used as NIFTY/BANKNIFTY's
        volume source — see the table below).
      </p>
      <div className="form-row">
        <label>Active provider</label>
        <span>
          {providerPrefQuery.data?.active_provider ?? 'Automatic'}
          {providerPrefQuery.data?.live_active_leg && (
            <span className="badge" style={{ marginLeft: '0.5rem' }}>
              live: {providerPrefQuery.data.live_active_leg}
            </span>
          )}
        </span>
      </div>
      {telemetryQuery.isLoading ? (
        <p className="muted">Loading...</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Feed state</th>
              <th>Last tick/bar</th>
              <th>RSI14</th>
              <th>EMA9</th>
              <th>EMA20</th>
              <th>VWAP</th>
              <th>PCR (OI / Vol)</th>
            </tr>
          </thead>
          <tbody>
            {underlyings.map((u) => (
              <tr key={u.symbol}>
                <td>{u.symbol}</td>
                <td>
                  <span className={feedStateBadgeClass(u.feed_state)}>
                    {FEED_STATE_LABEL[u.feed_state] ?? u.feed_state}
                  </span>
                </td>
                <td>{formatFeedAge(u.feed_age_seconds)}</td>
                <td>{formatIndicatorValue(u.rsi14)}</td>
                <td>{formatIndicatorValue(u.ema9)}</td>
                <td>{formatIndicatorValue(u.ema20)}</td>
                <td>{formatIndicatorValue(u.vwap)}</td>
                <td>{formatPcr(u)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="muted" style={{ fontSize: '0.75rem', margin: '0.5rem 0 0' }}>
        INDIA VIX ticks far less often than NIFTY/BANKNIFTY by design, so it uses a wider
        staleness threshold — its "Stale"/"Dead" badge is informational only, not a trading gate
        (nothing here ever blocks a strategy on VIX freshness). PCR only applies to the two
        tradable underlyings — it needs an option-chain snapshot, which only exists once
        something has scanned that underlying's nearest expiry today.
      </p>
      {calculatedSymbols.length > 0 && (
        <>
          <h4 style={{ margin: '1rem 0 0.25rem' }}>Calculated symbols</h4>
          <p className="muted" style={{ fontSize: '0.75rem', margin: '0 0 0.5rem' }}>
            Genuinely subscribed on the wire but never persisted to price_bars/indicator_snapshots
            themselves — streamed purely to feed another symbol's indicator above.
          </p>
          <table>
            <thead>
              <tr>
                <th>Source symbol</th>
                <th>Feeds</th>
                <th>Subscribed</th>
                <th>Last price</th>
                <th>Last cumulative volume</th>
              </tr>
            </thead>
            <tbody>
              {calculatedSymbols.map((c: VolumeProxySymbolTelemetryOut, idx: number) => (
                <tr key={`${c.source_symbol ?? 'unknown'}-${idx}`}>
                  <td>{c.source_symbol ?? '—'}</td>
                  <td>{c.target_symbol ?? '—'}</td>
                  <td>{c.subscribed ? 'Yes' : 'No'}</td>
                  <td>{formatIndicatorValue(c.last_price)}</td>
                  <td>{formatIndicatorValue(c.last_cum_volume)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  )
}

// ---------- Backend restart ----------

interface OpenLivePosition {
  trading_session_id: string
  contract_symbol: string
  qty: number
}

interface RestartBackendResponse {
  ok: boolean
  message: string
  boot_id: string
}

interface BootStatusResponse {
  boot_id: string
}

interface RestartBlockedDetail {
  message: string
  open_live_positions: OpenLivePosition[]
}

// How long to keep polling /boot-status for a new boot_id before giving up
// and telling the user to go check server logs themselves.
const RESTART_POLL_INTERVAL_MS = 2000
const RESTART_POLL_TIMEOUT_MS = 60_000

function BackendRestartCard() {
  const [restartReason, setRestartReason] = useState('')
  const [restartStatus, setRestartStatus] = useState<string | null>(null)
  const [blockedPositions, setBlockedPositions] = useState<OpenLivePosition[] | null>(null)
  const [isWaitingForRestart, setIsWaitingForRestart] = useState(false)
  // Bumped on every new restart attempt (and on unmount) so a poll loop
  // started by an earlier attempt recognizes it's stale and stops instead
  // of racing a newer one or updating state after the component is gone.
  const pollTokenRef = useRef(0)

  useEffect(() => {
    return () => {
      pollTokenRef.current += 1
    }
  }, [])

  function pollForNewBoot(previousBootId: string, token: number) {
    const deadline = Date.now() + RESTART_POLL_TIMEOUT_MS

    const tick = () => {
      if (pollTokenRef.current !== token) return
      api
        .get<BootStatusResponse>('/system-settings/boot-status')
        .then((result) => {
          if (pollTokenRef.current !== token) return
          if (result.boot_id !== previousBootId) {
            setIsWaitingForRestart(false)
            setRestartStatus('Restart successful — the new backend process is up.')
            return
          }
          scheduleNext()
        })
        .catch(() => {
          // Expected while the process is actually down (connection refused,
          // proxy error, etc.) -- not a failure on its own, keep polling
          // until the overall timeout below gives up.
          if (pollTokenRef.current !== token) return
          scheduleNext()
        })
    }

    const scheduleNext = () => {
      if (pollTokenRef.current !== token) return
      if (Date.now() >= deadline) {
        setIsWaitingForRestart(false)
        setRestartStatus(
          "Restart was scheduled, but the backend hasn't confirmed coming back up after " +
            '60s. Check the server logs.',
        )
        return
      }
      setTimeout(tick, RESTART_POLL_INTERVAL_MS)
    }

    setTimeout(tick, RESTART_POLL_INTERVAL_MS)
  }

  const restartMutation = useMutation({
    mutationFn: (force: boolean) =>
      api.post<RestartBackendResponse>('/system-settings/restart-backend', {
        reason: restartReason,
        force,
      }),
    onSuccess: (result) => {
      setBlockedPositions(null)
      setRestartStatus(`${result.message} Waiting for the new process to come up…`)
      setIsWaitingForRestart(true)
      pollTokenRef.current += 1
      pollForNewBoot(result.boot_id, pollTokenRef.current)
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        const detail = (err.body as { detail?: RestartBlockedDetail } | null)?.detail
        setRestartStatus(detail?.message ?? 'Blocked by open live positions.')
        setBlockedPositions(detail?.open_live_positions ?? [])
      } else {
        setBlockedPositions(null)
        setRestartStatus(err instanceof ApiError ? err.message : 'Restart failed.')
      }
    },
  })

  function handleRestartClick() {
    if (!restartReason.trim()) {
      setRestartStatus('A reason is required.')
      return
    }
    if (
      !window.confirm(
        'This restarts the entire backend process. Every open position’s stop/target/' +
          'trail monitoring pauses for a few seconds while it comes back up. Continue?',
      )
    ) {
      return
    }
    setBlockedPositions(null)
    restartMutation.mutate(false)
  }

  return (
    <div className="card">
      <h3>Backend restart</h3>
      <p className="muted">
        Last resort if a recovery action above doesn't fix a stuck session — restarts the entire
        backend process. Every open position's stop/target/trail monitoring pauses for a few
        seconds while it comes back up.
      </p>
      <div className="form-row">
        <label htmlFor="restart-reason">Reason</label>
        <textarea
          id="restart-reason"
          value={restartReason}
          onChange={(e) => setRestartReason(e.target.value)}
          rows={2}
        />
      </div>
      <button
        className="danger"
        disabled={restartMutation.isPending || isWaitingForRestart}
        onClick={handleRestartClick}
      >
        Restart backend
      </button>
      {restartStatus && <p>{restartStatus}</p>}
      {blockedPositions && blockedPositions.length > 0 && (
        <>
          <table>
            <thead>
              <tr>
                <th>Session</th>
                <th>Contract</th>
                <th>Qty</th>
              </tr>
            </thead>
            <tbody>
              {blockedPositions.map((position, i) => (
                <tr key={i}>
                  <td>{position.trading_session_id}</td>
                  <td>{position.contract_symbol}</td>
                  <td>{position.qty}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <button
            className="danger"
            disabled={restartMutation.isPending || isWaitingForRestart}
            onClick={() => restartMutation.mutate(true)}
          >
            Restart anyway
          </button>
        </>
      )}
    </div>
  )
}
