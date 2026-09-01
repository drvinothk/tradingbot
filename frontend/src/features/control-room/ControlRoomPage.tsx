import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError } from '../../shared/api/client'
import { useSessionBuckets } from '../../shared/hooks/useSessionBuckets'
import { useRunningStrategies } from '../../shared/hooks/useRunningStrategies'
import { useOrders, usePositions } from '../../shared/hooks/useOrdersAndPositions'
import { useInstruments } from '../../shared/hooks/useInstruments'
import { useSystemAlerts } from '../../shared/hooks/useRecovery'
import { groupAlertsIntoIncidents } from '../../shared/alerts/groupAlerts'
import { buildTradeRows, type TradeRow, type TradeRowStatus } from '../../shared/trades/buildTradeRows'
import { exitReasonLabel, stagedExitSummary, strategyTypeLabel } from '../../shared/format/friendlyLabel'
import type {
  DailyReportOut,
  RunningStrategyOut,
  SessionOut,
  SquareOffPositionOut,
} from '../../shared/api/types'

// Rows that actually reached a live position today -- excludes
// 'pending_approval' (nothing fired yet) and 'rejected' (never became a
// trade). Shared by the Live Trades Today tile, the per-strategy table, and
// the MTM per-lot calc so all three numbers agree with each other.
const REAL_TRADE_STATUSES = new Set<TradeRowStatus>(['position_open', 'closing', 'closed'])

const STATUS_LABELS: Record<TradeRowStatus, string> = {
  pending_approval: 'Pending Approval',
  order_sent: 'Order Sent',
  position_open: 'Position Open',
  closing: 'Closing (exit sent)',
  rejected: 'Rejected',
  closed: 'Closed',
}

const STATUS_BADGE_CLASS: Record<TradeRowStatus, string> = {
  pending_approval: 'badge-warning',
  order_sent: 'badge',
  position_open: 'badge-success',
  closing: 'badge-warning',
  rejected: 'badge-live',
  closed: 'badge',
}

// Only `live_enabled` places real orders -- the three emergency modes
// (kill_switch/degraded_mode/reconciliation_lock) are NOT "live" for the
// purpose of the Go Live/Go Paper toggle, even though they're also not
// paper_only. Treating them as "live" made "Go Paper" look normally
// enabled while the session was actually stuck in an emergency state that
// Go Paper can't fix -- see Advanced's Reconciliation & Recovery card.
const LIVE_MODES = new Set(['live_enabled'])
const EMERGENCY_MODES = new Set(['kill_switch', 'degraded_mode', 'reconciliation_lock'])

// Stable reference so `?? EMPTY_RUNS` doesn't defeat downstream useMemo
// dependency checks the way `?? []` (a fresh array literal every render)
// would.
const EMPTY_RUNS: RunningStrategyOut[] = []

export function ControlRoomPage() {
  const queryClient = useQueryClient()
  const { liveSession, paperSession, isLoading: bucketsLoading } = useSessionBuckets()
  const [actionError, setActionError] = useState<string | null>(null)
  const [hiddenRowKeys, setHiddenRowKeys] = useState<Set<string>>(new Set())

  const runningQuery = useRunningStrategies()
  const runs = runningQuery.data ?? EMPTY_RUNS

  // Fetch both sessions' orders/positions here (not per-bucket) so a
  // trade can be bucketed by what it actually fired to the broker
  // (`TradeRow.mode`) rather than by which session's query it happened to
  // come from -- a strategy's force_paper override can put a paper-mode
  // order under the Live session's trading_session_id, and the old
  // per-session-scoped fetch had no way to catch that. See
  // buildTradeRows' own docstring.
  const liveOrdersQuery = useOrders(liveSession?.id ?? null)
  const livePositionsQuery = usePositions(liveSession?.id ?? null)
  const paperOrdersQuery = useOrders(paperSession?.id ?? null)
  const paperPositionsQuery = usePositions(paperSession?.id ?? null)
  const instrumentsQuery = useInstruments()

  const sessionModeById = useMemo(() => {
    const m = new Map<string, 'live' | 'paper'>()
    if (liveSession) m.set(liveSession.id, 'live')
    if (paperSession) m.set(paperSession.id, 'paper')
    return m
  }, [liveSession, paperSession])

  const allRows = useMemo(
    () =>
      buildTradeRows(
        runs,
        [...(liveOrdersQuery.data ?? []), ...(paperOrdersQuery.data ?? [])],
        [...(livePositionsQuery.data ?? []), ...(paperPositionsQuery.data ?? [])],
        instrumentsQuery.data ?? [],
        sessionModeById,
      ),
    [
      runs,
      liveOrdersQuery.data,
      paperOrdersQuery.data,
      livePositionsQuery.data,
      paperPositionsQuery.data,
      instrumentsQuery.data,
      sessionModeById,
    ],
  )

  // mode is the ground truth; sessionId is only consulted for the rare
  // data-integrity edge case where mode itself is null (see TradeRow's own
  // docstring) so such a row still shows up somewhere instead of vanishing.
  const liveRows = allRows.filter(
    (r) => r.mode === 'live' || (r.mode === null && r.sessionId === liveSession?.id),
  )
  const paperRows = allRows.filter(
    (r) => r.mode === 'paper' || (r.mode === null && r.sessionId === paperSession?.id),
  )

  const invalidateTrades = () => {
    queryClient.invalidateQueries({ queryKey: ['strategies', 'running'] })
    queryClient.invalidateQueries({ queryKey: ['orders'] })
    queryClient.invalidateQueries({ queryKey: ['positions'] })
  }

  function hideRow(key: string) {
    setHiddenRowKeys((prev) => new Set(prev).add(key))
  }

  return (
    <div>
      <ControlRoomHeader
        liveSession={liveSession}
        onError={setActionError}
        onChanged={invalidateTrades}
      />

      {actionError && <p className="error">{actionError}</p>}

      <TodaysActivityCard
        runs={runs}
        liveRows={liveRows}
        paperRows={paperRows}
        liveSessionId={liveSession?.id ?? null}
        paperSessionId={paperSession?.id ?? null}
      />

      <div className="grid-2">
        <StrategyStatusCard runs={runs} />
        <AttentionCard runs={runs} />
      </div>

      <LiveTradesByStrategy liveRows={liveRows} />

      <TradeBucketCard
        title="Today's Trades (Live)"
        session={liveSession}
        rows={liveRows}
        hiddenRowKeys={hiddenRowKeys}
        onHideRow={hideRow}
        onChanged={invalidateTrades}
        onError={setActionError}
        emptyHint={
          bucketsLoading
            ? 'Loading...'
            : liveSession
              ? 'No trades yet today.'
              : 'No trading session is active today.'
        }
      />

      <TradeBucketCard
        title="Today's Paper Trades"
        session={paperSession}
        rows={paperRows}
        hiddenRowKeys={hiddenRowKeys}
        onHideRow={hideRow}
        onChanged={invalidateTrades}
        onError={setActionError}
        emptyHint={bucketsLoading ? 'Loading...' : 'No paper-mode trades yet today.'}
      />

      <AuditTickerPlaceholder />
    </div>
  )
}

function ControlRoomHeader({
  liveSession,
  onError,
  onChanged,
}: {
  liveSession: SessionOut | null
  onError: (message: string | null) => void
  onChanged: () => void
}) {
  const queryClient = useQueryClient()
  const invalidateSessions = () => queryClient.invalidateQueries({ queryKey: ['sessions'] })

  const [killArmed, setKillArmed] = useState(false)

  const goLiveMutation = useMutation({
    mutationFn: (sessionId: string) => api.post(`/sessions/${sessionId}/go-live`),
    onSuccess: () => {
      invalidateSessions()
      onError(null)
    },
    onError: (err) => onError(err instanceof ApiError ? err.message : 'Go live failed'),
  })

  const goPaperMutation = useMutation({
    mutationFn: (sessionId: string) => api.post(`/sessions/${sessionId}/go-paper`),
    onSuccess: () => {
      invalidateSessions()
      onError(null)
    },
    onError: (err) => onError(err instanceof ApiError ? err.message : 'Go paper failed'),
  })

  // Combines kill-switch + square-off into one Live-session-only action, per
  // the plan ("Kill Switch ... combines kill-switch + square-off"). Requires
  // an explicit "arm" click first (the .armed pulsing-border state) before
  // the actual destructive call fires, rather than a plain window.confirm --
  // matches the kill-switch's own dedicated visual states in the theme spec.
  //
  // Each step is caught independently: if kill-switch itself never reaches
  // the broker, nothing happened and the user is told exactly that. If
  // kill-switch succeeds but the follow-up square-off call throws, the
  // session IS now killed (real state change) -- refetching session/
  // position data and reporting the partial failure accurately matters
  // more here than in the old all-or-nothing version, which surfaced a
  // generic "Kill switch failed" and never refreshed anything, leaving the
  // user unable to tell a real kill from a no-op.
  const killSwitchMutation = useMutation({
    mutationFn: async (sessionId: string) => {
      await api.post(`/sessions/${sessionId}/kill-switch`, { reason: 'Control Room kill switch' })
      try {
        await api.post(`/sessions/${sessionId}/square-off`)
        return { squareOffFailed: false, squareOffMessage: null as string | null }
      } catch (err) {
        return {
          squareOffFailed: true,
          squareOffMessage: err instanceof ApiError ? err.message : 'Square-off failed',
        }
      }
    },
    onSuccess: (result) => {
      invalidateSessions()
      onChanged()
      setKillArmed(false)
      onError(
        result.squareOffFailed
          ? `Kill switch engaged, but the follow-up square-off failed: ${result.squareOffMessage}. ` +
              'Positions may still be open -- check Today\'s Trades and use Square Off there, or ' +
              'Advanced -> Reconciliation & Recovery, to finish flattening them.'
          : null,
      )
    },
    onError: (err) => {
      // The kill-switch call itself never went through -- nothing changed
      // on the session, but still refresh so the UI reflects reality
      // rather than an assumed no-op.
      invalidateSessions()
      onChanged()
      setKillArmed(false)
      onError(
        (err instanceof ApiError ? err.message : 'Kill switch request failed') +
          ' -- nothing was changed; the session is still in its previous state.',
      )
    },
  })

  function handleGoLive() {
    if (!liveSession) {
      onError('No Live-bucket session found — connect a real broker account and start a session first.')
      return
    }
    const typed = window.prompt(
      'This switches the Live session to LIVE -- strategies whose own Mode is "Live" ' +
        '(not "Paper") can then place real orders. Type LIVE to confirm.',
    )
    if (typed === 'LIVE') goLiveMutation.mutate(liveSession.id)
  }

  function handleGoPaper() {
    if (!liveSession) return
    if (window.confirm("Switch the Live session's master mode back to Paper?")) {
      goPaperMutation.mutate(liveSession.id)
    }
  }

  function handleKillSwitchClick() {
    if (!liveSession) {
      onError('No Live session to kill.')
      return
    }
    if (!killArmed) {
      setKillArmed(true)
      return
    }
    killSwitchMutation.mutate(liveSession.id)
  }

  const isLive = liveSession != null && LIVE_MODES.has(liveSession.mode)
  const isEmergency = liveSession != null && EMERGENCY_MODES.has(liveSession.mode)
  const killClassName = [
    'kill-switch',
    // 'danger' gives the default (non-locked, non-armed) state its red
    // text/border -- previously never actually applied despite a comment
    // in index.css claiming the default state used it, so the button
    // rendered as plain white text the whole time. .locked/.armed below
    // still correctly override it when active: both are more specific
    // compound selectors (button.kill-switch.locked/.armed) than the
    // single-class button.danger rule, regardless of danger also being
    // present in the class list.
    'danger',
    killSwitchMutation.isPending ? 'locked' : '',
    killArmed && !killSwitchMutation.isPending ? 'armed' : '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <div className="page-header">
      <h2>Control Room</h2>
      <div className="row-actions">
        <button
          className="btn-start"
          disabled={goLiveMutation.isPending || isLive || isEmergency}
          onClick={handleGoLive}
          title={liveSession ? '' : 'No Live-bucket session found'}
        >
          Go Live
        </button>
        <button
          className="btn-ghost"
          disabled={goPaperMutation.isPending || !isLive || isEmergency}
          onClick={handleGoPaper}
        >
          Go Paper
        </button>
        <button
          className={killClassName}
          disabled={!liveSession}
          onClick={handleKillSwitchClick}
          onBlur={() => setKillArmed(false)}
        >
          {killArmed ? 'Confirm Kill Switch?' : 'Kill Switch'}
        </button>
      </div>
      {isEmergency && liveSession && (
        <p className="muted" style={{ marginTop: '0.4rem' }}>
          Live session is in an emergency state ({liveSession.mode.replace(/_/g, ' ')}) — Go Live/Go
          Paper are disabled. Recover it first from Advanced → Reconciliation &amp; Recovery.
        </p>
      )}
    </div>
  )
}

// "Today's Activity" -- consolidates the old Net P&L / Margin Utilized /
// Live Trades Today / Max Drawdown boxes into one card with 4 split-metric
// boxes (P&L+WinRate, TotalTrades+OpenTrades, MaxDrawdown+LargestLoss,
// OpenRisk+PotentialProfit), scoped to whichever of Live/Paper is actually
// where the action is: if any running strategy is genuinely routed live
// right now (RunningStrategyOut.is_live -- NOT just "session is
// live_enabled", see that field's own comment in shared/api/types.ts), the
// main strip shows Live data. Margin Utilized is dropped outright -- it was
// a non-functional WIP stub, and open risk is the meaningful real-money
// replacement.
//
// 2026-09-01: Paper no longer disappears once Live starts. A live-routed
// strategy going live never stops a force_paper strategy from trading
// alongside it in the same session -- previously the whole card just
// swapped to Live and every paper number vanished from view. Now both
// scopes are always computed (useScopeMetrics below is called once per
// scope, unconditionally -- required anyway since hooks can't be
// conditional), and Paper renders as a smaller sub-ribbon under the main
// Live strip whenever there's genuine paper-side activity to show.
interface ScopeMetrics {
  sessionId: string | null
  report: DailyReportOut | undefined
  totalPnl: number
  perLotPnl: number | null
  totalLots: number
  realTradeCount: number
  openTrades: number
  openRisk: number | null
  potentialProfit: number | null
}

function useScopeMetrics(
  sessionId: string | null,
  mode: 'live' | 'paper',
  rows: TradeRow[],
  runs: RunningStrategyOut[],
): ScopeMetrics {
  // mode keeps this scope's Total Trades/Win Rate/Max Drawdown/Largest Loss
  // scoped to the same population as its already-per-trade-scoped P&L below
  // -- without it, a live_enabled session holding both live-routed and
  // force_paper strategies together (normal since 2026-08-28) blends the
  // force_paper strategy's own paper trades into stats labeled "Live". See
  // build_daily_report's own docstring.
  const dailyReportQuery = useQuery({
    queryKey: ['reports', 'daily', sessionId, mode],
    queryFn: () => api.get<DailyReportOut>(`/reports/sessions/${sessionId}/daily?mode=${mode}`),
    enabled: sessionId != null,
    refetchInterval: 15_000,
  })

  const realTradeRows = rows.filter((r) => REAL_TRADE_STATUSES.has(r.status))
  const totalPnl = realTradeRows.reduce((sum, r) => sum + (r.pnl ?? 0), 0)
  const totalLots = realTradeRows.reduce((sum, r) => sum + (r.lots ?? 0), 0)
  const perLotPnl = totalLots > 0 ? totalPnl / totalLots : null
  const openTrades = rows.filter((r) => r.status === 'position_open').length

  // Open risk/potential profit are scoped the same way -- only positions
  // belonging to strategies in this scope, so Live never mixes a paper
  // position's numbers in or vice versa.
  const isLiveScope = mode === 'live'
  const scopedRuns = runs.filter((r) => r.is_live === isLiveScope)
  const openRisks = scopedRuns
    .map((r) => r.open_position?.open_risk)
    .filter((v): v is number => v != null)
  const potentialProfits = scopedRuns
    .map((r) => r.open_position?.potential_profit)
    .filter((v): v is number => v != null)

  return {
    sessionId,
    report: dailyReportQuery.data,
    totalPnl,
    perLotPnl,
    totalLots,
    realTradeCount: realTradeRows.length,
    openTrades,
    openRisk: openRisks.length > 0 ? openRisks.reduce((sum, v) => sum + v, 0) : null,
    potentialProfit:
      potentialProfits.length > 0 ? potentialProfits.reduce((sum, v) => sum + v, 0) : null,
  }
}

function TodaysActivityCard({
  runs,
  liveRows,
  paperRows,
  liveSessionId,
  paperSessionId,
}: {
  runs: RunningStrategyOut[]
  liveRows: TradeRow[]
  paperRows: TradeRow[]
  liveSessionId: string | null
  paperSessionId: string | null
}) {
  const anyLive = runs.some((r) => r.is_live)

  const liveMetrics = useScopeMetrics(liveSessionId, 'live', liveRows, runs)
  const paperMetrics = useScopeMetrics(paperSessionId, 'paper', paperRows, runs)

  const primaryMetrics = anyLive ? liveMetrics : paperMetrics
  const primaryLabel = anyLive ? 'Live' : 'Paper'

  // The sub-ribbon only ever appears once Live is primary, and only when
  // there's genuine paper-side activity to show underneath it -- a
  // workspace with no paper strategies at all never gets an empty ribbon.
  const showPaperSubRibbon = anyLive && (paperRows.length > 0 || runs.some((r) => !r.is_live))

  return (
    <div className="card">
      <div className="card-header">
        <h3>Today&apos;s Activity</h3>
        <span className="muted">({primaryLabel})</span>
      </div>
      <div className="metrics-strip">
        <ActivityMetricsBoxes metrics={primaryMetrics} />
      </div>
      {showPaperSubRibbon && (
        <>
          <div className="muted metrics-substrip-label">Paper</div>
          <div className="metrics-strip metrics-substrip">
            <ActivityMetricsBoxes metrics={paperMetrics} />
          </div>
        </>
      )}
    </div>
  )
}

function ActivityMetricsBoxes({ metrics }: { metrics: ScopeMetrics }) {
  const { sessionId, report } = metrics
  const winRateDisplay =
    sessionId === null
      ? '—'
      : !report
        ? '…'
        : report.trade_count > 1
          ? `${Math.round(report.win_rate * 100)}%`
          : '—'
  const totalTradesDisplay =
    sessionId === null ? metrics.realTradeCount : (report?.trade_count ?? metrics.realTradeCount)
  const maxDrawdownDisplay =
    sessionId === null ? '—' : report ? report.max_drawdown.toFixed(2) : '…'
  const largestLossDisplay =
    sessionId === null ? '—' : report ? report.largest_single_loss.toFixed(2) : '…'
  const largestWinDisplay =
    sessionId === null ? '—' : report ? report.largest_single_win.toFixed(2) : '…'
  const totalCostDisplay =
    sessionId === null ? '—' : report ? report.total_cost.toFixed(2) : '…'

  return (
    <>
      <div className="metric-box metric-box-split metric-box-wide">
        <div className="metric-box-main">
          <div className="metric-label">P&amp;L</div>
          <div className="metric-value">
            <span className={metrics.totalPnl >= 0 ? 'pnl-positive' : 'pnl-negative'}>
              {metrics.totalPnl >= 0 ? '+' : ''}
              {metrics.totalPnl.toFixed(2)}
            </span>
          </div>
          <div className="metric-subvalue muted">
            {metrics.perLotPnl !== null
              ? `${metrics.perLotPnl >= 0 ? '+' : ''}${metrics.perLotPnl.toFixed(2)} / lot`
              : '— / lot'}
          </div>
        </div>
        <div className="metric-box-secondary">
          <div className="metric-label">Total Cost</div>
          <div className="metric-value">{totalCostDisplay}</div>
        </div>
        <div className="metric-box-secondary">
          <div className="metric-label">Win Rate</div>
          <div className="metric-value">{winRateDisplay}</div>
        </div>
      </div>

      <div className="metric-box metric-box-split">
        <div className="metric-box-main">
          <div className="metric-label">Total Trades</div>
          <div className="metric-value">{totalTradesDisplay}</div>
          <div className="metric-subvalue muted">
            {metrics.totalLots} lot{metrics.totalLots === 1 ? '' : 's'}
          </div>
        </div>
        <div className="metric-box-secondary">
          <div className="metric-label">Open Trades</div>
          <div className="metric-value">{metrics.openTrades}</div>
        </div>
      </div>

      <div className="metric-box metric-box-split metric-box-wide">
        <div className="metric-box-main">
          <div className="metric-label">Total Drawdown</div>
          <div className="metric-value">{maxDrawdownDisplay}</div>
        </div>
        <div className="metric-box-secondary">
          <div className="metric-label">Largest Single Loss</div>
          <div className="metric-value">{largestLossDisplay}</div>
        </div>
        <div className="metric-box-secondary">
          <div className="metric-label">Largest Single Profit</div>
          <div className="metric-value">{largestWinDisplay}</div>
        </div>
      </div>

      <div className="metric-box metric-box-split">
        <div className="metric-box-main">
          <div className="metric-label">Open Risk</div>
          <div className="metric-value">
            {metrics.openRisk !== null ? metrics.openRisk.toFixed(2) : '—'}
          </div>
        </div>
        <div className="metric-box-secondary">
          <div className="metric-label">Potential Profit</div>
          <div className="metric-value">
            {metrics.potentialProfit !== null ? metrics.potentialProfit.toFixed(2) : '—'}
          </div>
        </div>
      </div>
    </>
  )
}

const RUN_STATUS_LABELS: Record<string, string> = {
  scanning: 'Scanning',
  in_position: 'In Position',
  paused: 'Paused',
  stopped: 'Stopped',
}

const FRESHNESS_BADGE_CLASS: Record<string, string> = {
  live: 'badge-success',
  degraded: 'badge-warning',
  stale: 'badge-live',
  dead: 'badge-live',
}

// A strategy is healthy if it's actively working (scanning/in_position) and
// its data isn't stale/dead -- `data_freshness === null` (no live runner
// registered, e.g. briefly right after a restart) is treated as healthy
// too, since it's not evidence of a problem on its own.
function isStrategyHealthy(run: RunningStrategyOut): boolean {
  const statusOk = run.status === 'scanning' || run.status === 'in_position'
  const freshnessOk = run.data_freshness === null || run.data_freshness === 'live' || run.data_freshness === 'degraded'
  return statusOk && freshnessOk
}

function StrategyStatusCard({ runs }: { runs: RunningStrategyOut[] }) {
  const unhealthy = runs.filter((r) => !isStrategyHealthy(r))
  const [expanded, setExpanded] = useState(unhealthy.length > 0)

  useEffect(() => {
    if (unhealthy.length > 0) setExpanded(true)
  }, [unhealthy.length])

  return (
    <div className="card">
      <div className="collapsible-header" onClick={() => setExpanded((v) => !v)}>
        <h3>Strategy Status</h3>
        <span className={`chevron ${expanded ? 'open' : ''}`}>▶</span>
      </div>
      {runs.length === 0 ? (
        <p className="muted">No strategies running.</p>
      ) : unhealthy.length === 0 ? (
        <p className="muted">
          <span className="badge badge-success">OK</span> All {runs.length} strateg
          {runs.length === 1 ? 'y' : 'ies'} scanning normally.
        </p>
      ) : (
        <p className="muted">
          <span className="badge badge-live">Attention</span> {unhealthy.length} of {runs.length}{' '}
          strateg{runs.length === 1 ? 'y' : 'ies'} need{unhealthy.length === 1 ? 's' : ''} a look.
        </p>
      )}
      {expanded && runs.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Strategy</th>
              <th>Status</th>
              <th>Feed</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.strategy_run_id}>
                <td>{strategyTypeLabel(run.strategy_type)}</td>
                <td>
                  <span className={isStrategyHealthy(run) ? 'badge' : 'badge badge-live'}>
                    {RUN_STATUS_LABELS[run.status] ?? run.status}
                  </span>
                </td>
                <td>
                  {run.data_freshness !== null ? (
                    <span className={`badge ${FRESHNESS_BADGE_CLASS[run.data_freshness] ?? 'badge'}`}>
                      {run.data_freshness}
                    </span>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

// Mirrors backend alerting/manager.py's TELEGRAM_ALLOWED_CATEGORIES --
// deliberately excludes 'trade_approval_pending' even though that category
// IS telegram-eligible server-side: a pending approval is already
// represented here directly from the live `pending_approvals` data (below),
// which is ground truth and always current, so showing the alert row too
// would just duplicate/stale-shadow the same event. Keep this list in sync
// with the backend's if that allowlist changes.
const ATTENTION_ALERT_CATEGORIES = new Set([
  'strategy_run_stalled',
  'stale_session_not_closed',
  'protective_stop_placement_failed',
  'protective_stop_cancel_failed',
  'protective_stop_cancel_unresolved',
  'exit_order_unfilled',
  'margin_breach_square_off',
  'daily_loss_cap_breached',
  'reconciliation_mismatch',
  'health_check_failed',
  'order_rejected',
  'broker_disconnected',
  'market_data_stale',
  'market_data_failover_switch',
])

interface AttentionItem {
  key: string
  kind: 'approval' | 'alert'
  badgeLabel: string
  message: string
  whenLabel: string
  sortValue: number
}

// A category re-firing every scheduler cycle (market_data_stale,
// health_check_failed, strategy_run_stalled, reconciliation_mismatch, ...)
// never sets is_resolved -- nothing in the backend ever does (see
// groupAlerts.ts's own docstring) -- so a still-genuine issue keeps
// re-alerting well inside this window and stays visible; one that's gone
// quiet for 30 minutes is treated as resolved and drops off.
const ATTENTION_STALE_AFTER_MS = 30 * 60 * 1000

function AttentionCard({ runs }: { runs: RunningStrategyOut[] }) {
  const alertsQuery = useSystemAlerts()
  const [expanded, setExpanded] = useState(false)

  // Same category+message grouping AdvancedPage.tsx's System Errors card
  // uses (see shared/alerts/groupAlerts.ts) -- keeps a repeating alert to
  // one row instead of one per raw occurrence, and (unlike a plain
  // category-only grouping) still keeps two genuinely different messages
  // in the same category as separate rows.
  const incidents = groupAlertsIntoIncidents(alertsQuery.data ?? [])
  const relevantAlerts = incidents.filter(
    (inc) =>
      inc.severity === 'critical' &&
      ATTENTION_ALERT_CATEGORIES.has(inc.category) &&
      Date.now() - new Date(inc.lastSeen).getTime() <= ATTENTION_STALE_AFTER_MS,
  )

  const approvalItems: AttentionItem[] = runs.flatMap((run) =>
    run.pending_approvals.map((approval) => ({
      key: `approval:${approval.approval_id}`,
      kind: 'approval' as const,
      badgeLabel: 'Approval',
      message: `${strategyTypeLabel(run.strategy_type)} ${approval.side} ${approval.qty_lots} lot${approval.qty_lots === 1 ? '' : 's'} @ ${approval.entry_price.toFixed(2)}`,
      whenLabel: `expires ${new Date(approval.expires_at).toLocaleTimeString()}`,
      sortValue: new Date(approval.expires_at).getTime(),
    })),
  )
  const alertItems: AttentionItem[] = relevantAlerts.map((incident) => ({
    key: `alert:${incident.key}`,
    kind: 'alert' as const,
    badgeLabel: incident.category,
    message: incident.count > 1 ? `${incident.message} (×${incident.count})` : incident.message,
    whenLabel: new Date(incident.lastSeen).toLocaleTimeString(),
    sortValue: new Date(incident.lastSeen).getTime(),
  }))

  // Pending approvals first (soonest-expiring first -- most urgent to
  // decide on), then alerts most-recent-first.
  approvalItems.sort((a, b) => a.sortValue - b.sortValue)
  alertItems.sort((a, b) => b.sortValue - a.sortValue)
  const items = [...approvalItems, ...alertItems]

  const nothingToShow = items.length === 0

  return (
    <div className="card">
      <div className="collapsible-header" onClick={() => setExpanded((v) => !v)}>
        <h3>Attention Required</h3>
        <span className={`chevron ${expanded ? 'open' : ''}`}>▶</span>
      </div>
      {alertsQuery.isLoading ? (
        <p className="muted">Loading...</p>
      ) : nothingToShow ? (
        <p className="muted">Nothing needs attention.</p>
      ) : !expanded ? (
        <div className="attention-preview">
          {items.slice(0, 2).map((item) => (
            <AttentionPreviewRow key={item.key} item={item} />
          ))}
        </div>
      ) : (
        <div className="attention-scroll">
          {items.map((item) => (
            <AttentionPreviewRow key={item.key} item={item} />
          ))}
        </div>
      )}
    </div>
  )
}

function AttentionPreviewRow({ item }: { item: AttentionItem }) {
  return (
    <div className="attention-row">
      <span className={`attention-dot ${item.kind === 'approval' ? 'warning' : 'danger'}`} />
      <span className={`badge ${item.kind === 'approval' ? 'badge-warning' : 'badge-live'}`}>
        {item.badgeLabel}
      </span>
      <span className="attention-message">{item.message}</span>
      <span className="muted attention-when">{item.whenLabel}</span>
    </div>
  )
}

function LiveTradesByStrategy({ liveRows }: { liveRows: TradeRow[] }) {
  const [expanded, setExpanded] = useState(false)

  const byStrategy = new Map<
    string,
    { trades: number; pnl: number; closedWins: number; closedTotal: number }
  >()
  for (const row of liveRows) {
    if (row.strategyType === null || !REAL_TRADE_STATUSES.has(row.status)) continue
    const entry = byStrategy.get(row.strategyType) ?? {
      trades: 0,
      pnl: 0,
      closedWins: 0,
      closedTotal: 0,
    }
    entry.trades += 1
    entry.pnl += row.pnl ?? 0
    if (row.status === 'closed') {
      entry.closedTotal += 1
      if ((row.pnl ?? 0) > 0) entry.closedWins += 1
    }
    byStrategy.set(row.strategyType, entry)
  }
  const strategyRows = [...byStrategy.entries()]

  return (
    <div className="card">
      <div className="collapsible-header" onClick={() => setExpanded((v) => !v)}>
        <h3>Live Trades by Strategy</h3>
        <span className={`chevron ${expanded ? 'open' : ''}`}>▶</span>
      </div>
      {expanded &&
        (strategyRows.length === 0 ? (
          <p className="muted">No live trades yet today.</p>
        ) : (
          <table className="trade-table">
            <thead>
              <tr>
                <th>Strategy</th>
                <th>Trades</th>
                <th>P&amp;L</th>
                <th>Win %</th>
              </tr>
            </thead>
            <tbody>
              {strategyRows.map(([strategyType, stats]) => (
                <tr key={strategyType}>
                  <td>{strategyTypeLabel(strategyType)}</td>
                  <td>{stats.trades}</td>
                  <td>
                    <span className={stats.pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}>
                      {stats.pnl >= 0 ? '+' : ''}
                      {stats.pnl.toFixed(2)}
                    </span>
                  </td>
                  <td>
                    {stats.closedTotal > 0
                      ? `${Math.round((stats.closedWins / stats.closedTotal) * 100)}%`
                      : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}
    </div>
  )
}

function AuditTickerPlaceholder() {
  return (
    <div className="audit-ticker">
      <div className="audit-ticker-track">
        <span>
          [WIP] Audit ticker needs a live event stream — GET /audit exists but is poll-only today.
        </span>
        <span>System and reconciliation events will scroll here once wired up.</span>
      </div>
    </div>
  )
}

function TradeBucketCard({
  title,
  session,
  rows,
  hiddenRowKeys,
  onHideRow,
  onChanged,
  onError,
  emptyHint,
}: {
  title: string
  session: SessionOut | null
  rows: TradeRow[]
  hiddenRowKeys: Set<string>
  onHideRow: (key: string) => void
  onChanged: () => void
  onError: (message: string | null) => void
  emptyHint: string
}) {
  // Both Live and Paper start collapsed. The first time this card actually
  // has trades to show (including at initial load, if any already exist),
  // it auto-opens once -- `hasAutoExpanded` + `userToggledRef` make sure
  // that only ever happens once, and never overrides a deliberate manual
  // collapse/expand afterward (a ~4s poll refetch must not snap a
  // user-collapsed card back open).
  const [localExpanded, setLocalExpanded] = useState(false)
  const [hasAutoExpanded, setHasAutoExpanded] = useState(false)
  const userToggledRef = useRef(false)

  // Independent per card instance -- Live and Paper each get their own
  // filter state, so filtering one never affects the other.
  const [statusFilter, setStatusFilter] = useState<TradeRowStatus | 'all'>('all')
  const [strategyFilter, setStrategyFilter] = useState<string>('all')

  const unhiddenRows = rows.filter((r) => !hiddenRowKeys.has(r.key))

  useEffect(() => {
    if (!userToggledRef.current && !hasAutoExpanded && unhiddenRows.length > 0) {
      setLocalExpanded(true)
      setHasAutoExpanded(true)
    }
  }, [unhiddenRows.length, hasAutoExpanded])

  const isExpanded = localExpanded
  const toggle = () => {
    userToggledRef.current = true
    setLocalExpanded((v) => !v)
  }

  const strategyOptions = [
    ...new Set(unhiddenRows.map((r) => r.strategyType).filter((s): s is string => s !== null)),
  ]
  const visibleRows = unhiddenRows.filter(
    (r) =>
      (statusFilter === 'all' || r.status === statusFilter) &&
      (strategyFilter === 'all' || r.strategyType === strategyFilter),
  )

  // Totals row -- recomputed from visibleRows, so it updates live as the
  // status/strategy filters above change. Only Lots and P&L are summed;
  // every other column has no meaningful total (entry/exit prices, exit
  // reason, status, etc. don't aggregate).
  const totalsLots = visibleRows.reduce((sum, r) => sum + (r.lots ?? 0), 0)
  const totalsPnl = visibleRows.reduce((sum, r) => sum + (r.pnl ?? 0), 0)
  const totalsHasOpenPosition = visibleRows.some((r) => r.status === 'position_open')

  // Which single row is currently being acted on -- Approve/Reject/
  // Square-off all share these three mutation objects, but their own
  // `isPending` used to disable every row's buttons at once (acting on one
  // pending approval greyed out Approve/Reject on every other unrelated
  // row in the same bucket too). Scoping to the specific row key the user
  // clicked keeps unrelated rows interactive.
  const [actingRowKey, setActingRowKey] = useState<string | null>(null)

  const approveMutation = useMutation({
    mutationFn: (approvalId: string) => api.post(`/trade-approvals/${approvalId}/approve`),
    onSuccess: onChanged,
    onError: (err) => onError(err instanceof ApiError ? err.message : 'Approve failed'),
    onSettled: () => setActingRowKey(null),
  })
  const rejectMutation = useMutation({
    mutationFn: (approvalId: string) => api.post(`/trade-approvals/${approvalId}/reject`),
    onSuccess: onChanged,
    onError: (err) => onError(err instanceof ApiError ? err.message : 'Reject failed'),
    onSettled: () => setActingRowKey(null),
  })
  const squareOffMutation = useMutation({
    mutationFn: (positionId: string) => api.post<SquareOffPositionOut>(`/positions/${positionId}/square-off`),
    onSuccess: (result) => {
      onChanged()
      if (!result.success) {
        onError(`Square-off requested for position — ${result.detail ?? 'left open for reconciliation'}.`)
      } else {
        onError(null)
      }
    },
    onError: (err) => onError(err instanceof ApiError ? err.message : 'Square-off failed'),
    onSettled: () => setActingRowKey(null),
  })

  return (
    <div className="card">
      <div className="collapsible-header" onClick={toggle}>
        <h3>{title}</h3>
        <span className={`chevron ${isExpanded ? 'open' : ''}`}>▶</span>
      </div>
      {isExpanded && unhiddenRows.length > 0 && (
        <div className="row-actions trade-filters">
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as TradeRowStatus | 'all')}
          >
            <option value="all">All statuses</option>
            {(Object.keys(STATUS_LABELS) as TradeRowStatus[]).map((s) => (
              <option key={s} value={s}>
                {STATUS_LABELS[s]}
              </option>
            ))}
          </select>
          <select value={strategyFilter} onChange={(e) => setStrategyFilter(e.target.value)}>
            <option value="all">All strategies</option>
            {strategyOptions.map((s) => (
              <option key={s} value={s}>
                {strategyTypeLabel(s)}
              </option>
            ))}
          </select>
        </div>
      )}
      {isExpanded &&
        // Rows are bucketed by each trade's own recorded mode now, not by
        // this session existing (see buildTradeRows' own docstring) -- a
        // workspace can have real paper-mode trades with no separate
        // mock-backed session at all (e.g. force_paper strategies routed
        // through the one real broker's session). Gating the whole table
        // on `!session` here would hide genuinely real trades behind a
        // stale "no session" message -- check rows first, always.
        (visibleRows.length === 0 ? (
          <p className="muted">
            {unhiddenRows.length === 0
              ? session
                ? 'No trades yet today.'
                : emptyHint
              : 'No trades match the selected filters.'}
          </p>
        ) : (
          <div className="trade-table-scroll">
            <table className="trade-table">
              <colgroup>
                <col style={{ width: '18%' }} />
                <col style={{ width: '6%' }} />
                <col style={{ width: '8%' }} />
                <col style={{ width: '8%' }} />
                <col style={{ width: '13%' }} />
                <col style={{ width: '9%' }} />
                <col style={{ width: '8%' }} />
                <col style={{ width: '9%' }} />
                <col style={{ width: '10%' }} />
                <col style={{ width: '11%' }} />
              </colgroup>
              <thead>
                <tr>
                  <th>Trade</th>
                  <th>Lots</th>
                  <th>Entry Price</th>
                  <th>LTP</th>
                  <th>Target / SL-TSL</th>
                  <th>P&amp;L</th>
                  <th>Exit Via</th>
                  <th>Entry / Exit</th>
                  <th>Status</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {visibleRows.map((row) => (
                  <TradeRowView
                    key={row.key}
                    row={row}
                    onApprove={() => {
                      if (!row.approvalId) return
                      setActingRowKey(row.key)
                      approveMutation.mutate(row.approvalId)
                    }}
                    onReject={() => {
                      if (!row.approvalId) return
                      setActingRowKey(row.key)
                      rejectMutation.mutate(row.approvalId)
                    }}
                    onSquareOff={() => {
                      if (!row.positionId) return
                      setActingRowKey(row.key)
                      squareOffMutation.mutate(row.positionId)
                    }}
                    onHide={() => onHideRow(row.key)}
                    isPending={actingRowKey === row.key}
                  />
                ))}
              </tbody>
              <tfoot>
                <tr className="trade-table-totals">
                  <td>Total</td>
                  <td>{totalsLots > 0 ? totalsLots : '—'}</td>
                  <td></td>
                  <td></td>
                  <td></td>
                  <td>
                    <span className={totalsPnl >= 0 ? 'pnl-positive' : 'pnl-negative'}>
                      {totalsPnl >= 0 ? '+' : ''}
                      {totalsPnl.toFixed(2)}
                      {totalsHasOpenPosition ? ' (incl. open)' : ''}
                    </span>
                  </td>
                  <td></td>
                  <td></td>
                  <td></td>
                  <td></td>
                </tr>
              </tfoot>
            </table>
          </div>
        ))}
    </div>
  )
}

function TradeRowView({
  row,
  onApprove,
  onReject,
  onSquareOff,
  onHide,
  isPending,
}: {
  row: TradeRow
  onApprove: () => void
  onReject: () => void
  onSquareOff: () => void
  onHide: () => void
  isPending: boolean
}) {
  const [showMore, setShowMore] = useState(false)
  const [legsExpanded, setLegsExpanded] = useState(false)

  const slTsl = row.trailStopPrice ?? row.stopPrice
  const slTslLabel = row.trailStopPrice !== null ? 'TSL' : 'SL'

  const isStaged = row.legs.length > 1
  const closedLegCount = row.legs.filter((leg) => leg.status === 'closed').length

  return (
    <>
    <tr>
      <td>{row.label}</td>
      <td>
        {row.lots !== null ? (
          <span className="lots-label">
            <b>{row.lots}</b>{' '}
            <span className="lots-suffix">
              {row.lotSize !== null ? `x${row.lotSize}` : `lot${row.lots === 1 ? '' : 's'}`}
            </span>
          </span>
        ) : (
          '—'
        )}
      </td>
      <td>{row.entryPrice !== null ? row.entryPrice.toFixed(2) : '—'}</td>
      <td>
        {row.status === 'position_open' ? (
          row.ltp !== null ? (
            row.ltp.toFixed(2)
          ) : (
            <span className="muted">
              — <span className="badge badge-wip">no tick yet</span>
            </span>
          )
        ) : (
          '—'
        )}
      </td>
      <td>
        {row.targetPrice !== null || slTsl !== null ? (
          <>
            {row.targetPrice !== null ? row.targetPrice.toFixed(2) : '—'} /{' '}
            {slTsl !== null ? `${slTsl.toFixed(2)} (${slTslLabel})` : '—'}
          </>
        ) : (
          '—'
        )}
      </td>
      <td>
        {row.pnl !== null ? (
          <span className={row.pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}>
            {row.pnl >= 0 ? '+' : ''}
            {row.pnl.toFixed(2)}
            {row.isPnlRealized ? '' : ' (unrl.)'}
          </span>
        ) : (
          '—'
        )}
      </td>
      <td>{isStaged ? stagedExitSummary(row.legs) : exitReasonLabel(row.exitReason)}</td>
      <td style={{ fontSize: '0.8rem' }}>
        <div>{row.openedAt ? new Date(row.openedAt).toLocaleTimeString() : '—'}</div>
        <div className="muted">{row.closedAt ? new Date(row.closedAt).toLocaleTimeString() : '—'}</div>
      </td>
      <td>
        <span className={`badge ${STATUS_BADGE_CLASS[row.status]}`}>{STATUS_LABELS[row.status]}</span>
        {row.status === 'position_open' && isStaged && closedLegCount > 0 && (
          <span className="badge badge-wip" style={{ marginLeft: '0.35rem' }}>
            {closedLegCount}/{row.legs.length} legs closed
          </span>
        )}
      </td>
      <td>
        {row.status === 'pending_approval' && (
          <div className="row-actions">
            <button className="btn-approve" disabled={isPending} onClick={onApprove}>
              Approve
            </button>
            <button className="btn-reject" disabled={isPending} onClick={onReject}>
              Reject
            </button>
          </div>
        )}
        {(row.status === 'closed' || row.status === 'rejected') && !showMore && (
          <button className="btn-ghost" onClick={() => setShowMore(true)}>
            View more
          </button>
        )}
        {(row.status === 'closed' || row.status === 'rejected') && showMore && (
          <button className="btn-ghost" onClick={onHide}>
            Delete
          </button>
        )}
        {row.status === 'position_open' && !showMore && (
          <button className="btn-ghost" onClick={() => setShowMore(true)}>
            View more
          </button>
        )}
        {row.status === 'position_open' && showMore && (
          <button
            className="btn-stop"
            disabled={isPending || row.hasPendingExit}
            title={row.hasPendingExit ? 'An exit order for this position is already in flight.' : undefined}
            onClick={onSquareOff}
          >
            {row.hasPendingExit ? 'Exit sent...' : 'Square off'}
          </button>
        )}
        {isStaged && (
          <button className="btn-ghost" onClick={() => setLegsExpanded((v) => !v)}>
            {legsExpanded ? 'Hide legs' : `${row.legs.length} legs`}
          </button>
        )}
      </td>
    </tr>
    {isStaged && legsExpanded && (
      // colSpan matches the parent <table>'s own 10-column <colgroup>
      // above -- keep the two in sync if that column count ever changes.
      <tr className="trade-row-legs">
        <td colSpan={10}>
          <table className="leg-detail-table">
            <thead>
              <tr>
                <th>Leg</th>
                <th>Kind</th>
                <th>Qty</th>
                <th>Stop</th>
                <th>Target</th>
                <th>Exit Via</th>
                <th>P&amp;L</th>
                <th>Closed</th>
              </tr>
            </thead>
            <tbody>
              {row.legs.map((leg) => (
                <tr key={leg.leg_index}>
                  <td>
                    {leg.leg_index + 1}/{row.legs.length}
                  </td>
                  <td>{leg.kind}</td>
                  <td>{leg.qty}</td>
                  <td>{leg.stop_price !== null ? leg.stop_price.toFixed(2) : '—'}</td>
                  <td>{leg.target_price !== null ? leg.target_price.toFixed(2) : '—'}</td>
                  <td>{exitReasonLabel(leg.exit_reason)}</td>
                  <td>
                    {leg.realized_pnl !== null ? (
                      <span className={leg.realized_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}>
                        {leg.realized_pnl >= 0 ? '+' : ''}
                        {leg.realized_pnl.toFixed(2)}
                      </span>
                    ) : (
                      '—'
                    )}
                  </td>
                  <td>{leg.closed_at ? new Date(leg.closed_at).toLocaleTimeString() : '—'}</td>
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
