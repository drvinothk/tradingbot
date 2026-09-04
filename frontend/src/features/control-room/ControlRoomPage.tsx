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
  ManualReconcilePositionOut,
  RunningStrategyOut,
  SessionOut,
  SquareOffPositionOut,
} from '../../shared/api/types'

// Rows that actually reached a live position today -- excludes
// 'pending_approval' (nothing fired yet), 'rejected' (broker refused it),
// and 'cancelled' (withdrawn before it could fill -- never became a trade
// either way). Shared by the Live Trades Today tile, the per-strategy
// table, and the MTM per-lot calc so all three numbers agree with each
// other.
const REAL_TRADE_STATUSES = new Set<TradeRowStatus>(['position_open', 'closing', 'closed'])

const STATUS_LABELS: Record<TradeRowStatus, string> = {
  pending_approval: 'Pending Approval',
  order_sent: 'Order Sent',
  position_open: 'Position Open',
  // A resting SL/TSL/target exit order that's already live at the broker,
  // just not yet triggered/filled -- "Closing (exit sent)" read as if the
  // position were already in the process of closing, when really nothing
  // has happened yet at the broker beyond placing the order. The row's own
  // label already says what was sent (e.g. "SL exit · sell · ...", see
  // buildTradeRows.ts) so this status text doesn't need to repeat it.
  closing: 'Awaiting trigger',
  rejected: 'Rejected',
  // Distinct from 'rejected': the broker never refused this order -- it was
  // withdrawn (most commonly a resting protective SL/TSL cancelled because
  // the position ended up exiting a different way, e.g. a structure-break
  // exit). Previously collapsed into 'Rejected', which misread a routine,
  // expected cancellation as a broker error.
  cancelled: 'Cancelled',
  closed: 'Closed',
}

const STATUS_BADGE_CLASS: Record<TradeRowStatus, string> = {
  pending_approval: 'badge-warning',
  order_sent: 'badge',
  position_open: 'badge-success',
  closing: 'badge-warning',
  rejected: 'badge-live',
  // Neutral, not alarming -- a cancellation isn't a failure state the way a
  // broker rejection is.
  cancelled: 'badge',
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

// Every currency/amount figure on this page is now shown rounded to the
// nearest whole rupee -- decimals add no decision-relevant precision at a
// glance and cost horizontal space this page is otherwise tight on. Only
// applies to display -- the underlying numbers (from the API, used for
// sorting/summing/comparisons) are untouched.
function fmtAmt(value: number): string {
  return Math.round(value).toString()
}

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
  // 2026-09-04: renamed from `totalPnl` to make the cost-netting explicit
  // at the call site -- (realizedPnl + unrealizedPnl) - report.total_cost.
  // The one metric on this page that has cost baked in; every other P&L
  // figure here is gross. Cost only ever reflects *closed* trades
  // (report.total_cost is TradeOutcome-scoped -- see reporting/service.py)
  // -- an open position's eventual cost isn't estimated, by explicit
  // decision, so this slightly understates cost while positions are open.
  // Defaults the cost term to 0 before the report loads / with no session,
  // same "don't block on the report" immediacy realizedPnl/unrealizedPnl
  // already have (unlike the Total Cost/Win Rate boxes, which show '…').
  actualPnl: number
  // 2026-09-04: now divides actualPnl (net of cost), not the old gross
  // totalPnl -- the more decision-useful "what did each lot actually make"
  // figure, since this sits right next to Actual P&L in the UI.
  perLotPnl: number | null
  totalLots: number
  realTradeCount: number
  // Rows that have actually closed -- excludes 'position_open'/'closing',
  // which are still live trades in progress, not "trades" for a Total
  // Trades count. See `report?.trade_count` (server-side, TradeOutcome-
  // backed, same closed-only definition) which is preferred once loaded;
  // this is only the client-side fallback used before that query resolves
  // or when there's no session at all.
  closedTradeCount: number
  openTrades: number
  openRisk: number | null
  potentialProfit: number | null
  // P&L from trades that have already closed today (TradeRow.pnl on a
  // 'closed' row is Position.realized_pnl) -- distinct from the
  // still-moving unrealized figure below, and from "Potential Profit"
  // (an estimate of what a still-open position would net if its target
  // were hit, not what it's worth right now).
  realizedPnl: number
  // The running/live P&L of positions that are open right now (Position
  // .unrealized_pnl as of the last tick) -- marks-to-market, not a
  // hypothetical target-hit estimate.
  unrealizedPnl: number
}

function useScopeMetrics(
  sessionId: string | null,
  mode: 'live' | 'paper',
  rows: TradeRow[],
  runs: RunningStrategyOut[],
): ScopeMetrics {
  // mode keeps this scope's Closed Trades/Win Rate/Max Drawdown/Largest Loss
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
  const grossPnl = realTradeRows.reduce((sum, r) => sum + (r.pnl ?? 0), 0)
  // Cost defaults to 0 before the report loads / with no session -- same
  // "don't block the headline number on the report" immediacy every other
  // figure in this box already has (unlike Total Cost/Win Rate, which show
  // '…' while loading). See ScopeMetrics.actualPnl's own comment.
  const actualPnl = grossPnl - (dailyReportQuery.data?.total_cost ?? 0)
  // Lots only, not realTradeRows -- a 'closing' row is a still-resting exit
  // order (most commonly the LIVE protective SL-LMT placed at entry, see
  // buildTradeRows' own STATUS_LABELS comment: "just not yet triggered/
  // filled") for a position that is, by construction, still status
  // 'position_open' at the same time (the position only flips to 'closed'
  // once that very order fills). Summing both double-counted every open
  // LIVE position's lots -- once from its 'position_open' row, again from
  // its own resting-stop 'closing' row carrying the same qty.
  const lotsRows = rows.filter((r) => r.status === 'position_open' || r.status === 'closed')
  const totalLots = lotsRows.reduce((sum, r) => sum + (r.lots ?? 0), 0)
  const perLotPnl = totalLots > 0 ? actualPnl / totalLots : null
  const openTrades = rows.filter((r) => r.status === 'position_open').length
  const closedRows = rows.filter((r) => r.status === 'closed')
  const openRows = rows.filter((r) => r.status === 'position_open')
  const closedTradeCount = closedRows.length
  const realizedPnl = closedRows.reduce((sum, r) => sum + (r.pnl ?? 0), 0)
  const unrealizedPnl = openRows.reduce((sum, r) => sum + (r.pnl ?? 0), 0)

  // Open risk/potential profit are scoped the same way -- only positions
  // belonging to strategies in this scope, so Live never mixes a paper
  // position's numbers in or vice versa. Scoped off the *position's own*
  // recorded mode (open_position.mode), not run.is_live -- is_live answers
  // "would a *new* dispatch go live right now" (current config), which can
  // disagree with an already-open position opened under a different
  // config/session state (e.g. a paper position still open from before the
  // session flipped to live_enabled). Using is_live here previously leaked
  // a still-open paper position's potential_profit/open_risk into the Live
  // box with zero real live positions open. Falls back to is_live when
  // there's no open position, or its mode couldn't be resolved server-side
  // (a data-integrity gap -- see RunningPositionOut.mode's own docstring).
  const isLiveScope = mode === 'live'
  const scopedRuns = runs.filter((r) =>
    r.open_position?.mode != null ? r.open_position.mode === mode : r.is_live === isLiveScope,
  )
  const openRisks = scopedRuns
    .map((r) => r.open_position?.open_risk)
    .filter((v): v is number => v != null)
  const potentialProfits = scopedRuns
    .map((r) => r.open_position?.potential_profit)
    .filter((v): v is number => v != null)

  return {
    sessionId,
    report: dailyReportQuery.data,
    actualPnl,
    perLotPnl,
    totalLots,
    realTradeCount: realTradeRows.length,
    closedTradeCount,
    openTrades,
    openRisk: openRisks.length > 0 ? openRisks.reduce((sum, v) => sum + v, 0) : null,
    potentialProfit:
      potentialProfits.length > 0 ? potentialProfits.reduce((sum, v) => sum + v, 0) : null,
    realizedPnl,
    unrealizedPnl,
  }
}

// User-controlled, not activity-controlled -- Live is always shown, Paper is
// only ever shown when this box is ticked. Persisted so a preference set
// once (e.g. "always show Paper so I can check it after market close")
// survives a reload instead of resetting to whatever happened to be trading
// at the time, which is exactly the gap this replaces (the old
// `showPaperSubRibbon` only ever appeared while a live-routed strategy was
// active, so Paper was unreachable outside market hours).
const SHOW_PAPER_STORAGE_KEY = 'controlRoom.showPaperPanel'

function readShowPaperPreference(): boolean {
  try {
    return window.localStorage.getItem(SHOW_PAPER_STORAGE_KEY) === 'true'
  } catch {
    return false
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
  // A live_enabled session can hold both live-routed and FORCE_PAPER
  // strategies together (no separate mock-broker session exists in that
  // case -- see useSessionBuckets' own docstring) -- so when there's no
  // distinct paper session, fall back to the live session's own id and let
  // the server-side `mode=paper` filter do the real scoping (exactly what
  // build_daily_report's `mode` param exists for). Without this fallback
  // the report query never fires at all (disabled on a null session id),
  // so Total Cost/Win Rate/Drawdown/Largest Loss/Largest Profit silently
  // render as '--' even though the trade rows themselves (session-
  // independent, keyed off each row's own mode) show real data.
  const paperReportSessionId = paperSessionId ?? liveSessionId

  const liveMetrics = useScopeMetrics(liveSessionId, 'live', liveRows, runs)
  const paperMetrics = useScopeMetrics(paperReportSessionId, 'paper', paperRows, runs)

  const [showPaper, setShowPaper] = useState(readShowPaperPreference)

  const handleShowPaperChange = (checked: boolean) => {
    setShowPaper(checked)
    try {
      window.localStorage.setItem(SHOW_PAPER_STORAGE_KEY, String(checked))
    } catch {
      // localStorage unavailable (private browsing etc) -- preference just
      // won't survive a reload, not worth surfacing an error for.
    }
  }

  return (
    <div className="card">
      <div className="card-header">
        <h3>Today&apos;s Activity</h3>
        <span className="badge badge-success">Live</span>
      </div>
      <div className="metrics-strip">
        <ActivityMetricsBoxes metrics={liveMetrics} />
      </div>
      {showPaper && (
        <>
          <div className="muted metrics-substrip-label">Paper</div>
          <div className="metrics-strip metrics-substrip">
            <ActivityMetricsBoxes metrics={paperMetrics} />
          </div>
        </>
      )}
      <div className="card-footer-row">
        <label className="paper-toggle">
          <input
            type="checkbox"
            checked={showPaper}
            onChange={(e) => handleShowPaperChange(e.target.checked)}
          />
          Paper
        </label>
      </div>
    </div>
  )
}

// 2026-09-04: renamed from TotalPnlRow -- was the muted summary line under
// the two prominent boxes, showing Total P&L (now Actual P&L, promoted to
// the prominent box below) + Per Lot. Swapped per explicit user request:
// this row now shows Realized P&L (renamed from "Realized Profit", which
// used to be one of the two prominent boxes) instead. Per Lot now divides
// actualPnl (net of cost), not this row's own realizedPnl -- see
// ScopeMetrics.perLotPnl's own comment for why.
function RealizedPnlRow({ metrics }: { metrics: ScopeMetrics }) {
  if (metrics.sessionId === null) return null
  return (
    <div className="total-pnl-row muted">
      Realized P&amp;L{' '}
      <span className={metrics.realizedPnl >= 0 ? 'pnl-positive' : 'pnl-negative'}>
        {metrics.realizedPnl >= 0 ? '+' : ''}
        {fmtAmt(metrics.realizedPnl)}
      </span>
      {metrics.perLotPnl !== null && (
        <>
          {' · '}Per Lot{' '}
          <span className={metrics.perLotPnl >= 0 ? 'pnl-positive' : 'pnl-negative'}>
            {metrics.perLotPnl >= 0 ? '+' : ''}
            {fmtAmt(metrics.perLotPnl)}
          </span>
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
  // Closed-trade count only -- an open position isn't a "trade" for this
  // headline number yet (it has its own Open Trades box next to it).
  // `report.trade_count` (server-side, once loaded) is already scoped this
  // way -- see build_daily_report's docstring -- so it's preferred; the
  // client-side fallback (before the report loads, or with no session at
  // all) must match that same closed-only definition rather than falling
  // back to `realTradeCount`, which used to include open/closing rows.
  const totalTradesDisplay =
    sessionId === null ? metrics.closedTradeCount : (report?.trade_count ?? metrics.closedTradeCount)
  const maxDrawdownDisplay = sessionId === null ? '—' : report ? fmtAmt(report.max_drawdown) : '…'
  const largestLossDisplay =
    sessionId === null ? '—' : report ? fmtAmt(report.largest_single_loss) : '…'
  const largestWinDisplay =
    sessionId === null ? '—' : report ? fmtAmt(report.largest_single_win) : '…'
  const totalCostDisplay = sessionId === null ? '—' : report ? fmtAmt(report.total_cost) : '…'

  return (
    <>
      {/* 2026-09-04: Actual P&L (left) / Unrealized P&L (middle) / Cost+Win
          Rate (right, stacked -- same overall box height as the two main
          columns, smaller font to fit two label+value pairs in that
          space). Actual P&L (swapped in from the muted row, renamed from
          "Total P&L") is the one figure on this page with cost netted in --
          see ScopeMetrics.actualPnl's own comment. Realized P&L (renamed
          from "Realized Profit") moved down to the muted row below,
          alongside Per Lot -- see RealizedPnlRow. */}
      <div className="metric-box metric-box-split metric-box-wide">
        <div className="metric-box-pnl-group">
          <div className="metric-box-pnl-values">
            <div className="metric-box-main">
              <div className="metric-label">Actual P&amp;L</div>
              <div className="metric-value">
                <span className={metrics.actualPnl >= 0 ? 'pnl-positive' : 'pnl-negative'}>
                  {metrics.actualPnl >= 0 ? '+' : ''}
                  {fmtAmt(metrics.actualPnl)}
                </span>
              </div>
            </div>
            <div className="metric-box-main">
              <div className="metric-label">Unrealized P&amp;L</div>
              <div className="metric-value">
                <span className={metrics.unrealizedPnl >= 0 ? 'pnl-positive' : 'pnl-negative'}>
                  {metrics.unrealizedPnl >= 0 ? '+' : ''}
                  {fmtAmt(metrics.unrealizedPnl)}
                </span>
              </div>
            </div>
          </div>
          <RealizedPnlRow metrics={metrics} />
        </div>
        <div className="metric-box-stacked">
          <div className="metric-box-stacked-item">
            <div className="metric-label">Total Cost</div>
            <div className="metric-value">{totalCostDisplay}</div>
          </div>
          <div className="metric-box-stacked-item">
            <div className="metric-label">Win Rate</div>
            <div className="metric-value">{winRateDisplay}</div>
          </div>
        </div>
      </div>

      <div className="metric-box metric-box-split">
        <div className="metric-box-main">
          <div className="metric-label">Closed Trades</div>
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

      <div className="metric-box metric-box-split metric-box-narrow">
        <div className="metric-box-main">
          <div className="metric-label">Open Risk</div>
          <div className="metric-value">
            {metrics.openRisk !== null ? fmtAmt(metrics.openRisk) : '—'}
          </div>
        </div>
        <div className="metric-box-secondary">
          <div className="metric-label">Potential Profit</div>
          <div className="metric-value">
            {metrics.potentialProfit !== null ? fmtAmt(metrics.potentialProfit) : '—'}
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
                <td>
                  {run.strategy_name} <span className="muted">({strategyTypeLabel(run.strategy_type)})</span>
                </td>
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

// 2026-09-03: self-healing grace window -- mirrors backend
// alerting/manager.py's _SELF_HEALING_GRACE_CATEGORIES/
// _SELF_HEALING_GRACE_SECONDS exactly (same categories, same 10s value; see
// that module's own docstring, "2026-09-03: self-healing grace window", for
// the full reasoning). These three categories are raised on an ambiguous
// intermediate broker/reconciliation state that PositionManager's own 3s
// retry cycle resolves within ~1s the overwhelming majority of the time --
// surfacing them here the instant they're detected, before the system has
// even had one retry cycle to resolve itself, trained a "just FYI, ignore
// it" reflex rather than a "this needs you" one. Held back from this card
// only -- the SystemAlert row is written immediately regardless and stays
// visible without any delay on the Advanced page's "System errors" card.
// Keep this set/value in sync with the backend's if either changes.
const SELF_HEALING_GRACE_CATEGORIES = new Set([
  'protective_stop_cancel_unresolved',
  'exit_order_unfilled',
  'reconciliation_mismatch',
])
const SELF_HEALING_GRACE_MS = 10_000

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
      // 2026-09-04: mirrors Telegram's own mode!=PAPER suppression gate
      // (alerting.manager._should_push_to_telegram) -- a paper-mode alert
      // needs no urgent real-attention here either. mode === null (health
      // checks, market-data staleness -- not tied to a specific position)
      // is never suppressed, same as the backend's own rule.
      inc.mode !== 'paper' &&
      Date.now() - new Date(inc.lastSeen).getTime() <= ATTENTION_STALE_AFTER_MS &&
      (!SELF_HEALING_GRACE_CATEGORIES.has(inc.category) ||
        Date.now() - new Date(inc.firstSeen).getTime() >= SELF_HEALING_GRACE_MS),
  )

  const approvalItems: AttentionItem[] = runs.flatMap((run) =>
    run.pending_approvals.map((approval) => ({
      key: `approval:${approval.approval_id}`,
      kind: 'approval' as const,
      badgeLabel: 'Approval',
      message: `${run.strategy_name} (${strategyTypeLabel(run.strategy_type)}) ${approval.side} ${approval.qty_lots} lot${approval.qty_lots === 1 ? '' : 's'} @ ${fmtAmt(approval.entry_price)}`,
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
    // 'position_open'/'closed' only, not REAL_TRADE_STATUSES -- a 'closing'
    // row is a still-resting exit order (most commonly the LIVE protective
    // SL-LMT) for a position that is, by construction, still counted via
    // its own 'position_open' row at the same time (see useScopeMetrics'
    // identical `lotsRows` fix above). Including 'closing' here inflated
    // this table's per-strategy Trades column the same way it inflated
    // Total Lots.
    if (
      row.strategyType === null ||
      (row.status !== 'position_open' && row.status !== 'closed')
    )
      continue
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
                      {fmtAmt(stats.pnl)}
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
  const [optionTypeFilter, setOptionTypeFilter] = useState<'all' | 'CE' | 'PE'>('all')

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
      (strategyFilter === 'all' || r.strategyType === strategyFilter) &&
      (optionTypeFilter === 'all' || r.optionType === optionTypeFilter),
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
  // Fallback for a position Square Off itself can't clear -- e.g. it's
  // already exhausted its own automatic exit-order retries
  // (exit_order_attempts_exhausted) or reconciliation's own broker-history
  // auto-repair found no matching fill. A human who has independently
  // confirmed the real exit price (the broker's own app, a contract note)
  // can close the local record with it directly. Always closes the
  // position's full remaining qty -- see the endpoint's own docstring.
  const manualReconcileMutation = useMutation({
    mutationFn: ({ positionId, exitPrice }: { positionId: string; exitPrice: number }) =>
      api.post<ManualReconcilePositionOut>(`/positions/${positionId}/manual-reconcile`, {
        exit_price: exitPrice,
      }),
    onSuccess: (result) => {
      onChanged()
      onError(`Manually reconciled — closed at ${fmtAmt(result.exit_price)}, P&L ${fmtAmt(result.realized_pnl)}.`)
    },
    onError: (err) => onError(err instanceof ApiError ? err.message : 'Manual reconcile failed'),
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
          <select
            value={optionTypeFilter}
            onChange={(e) => setOptionTypeFilter(e.target.value as 'all' | 'CE' | 'PE')}
          >
            <option value="all">CE &amp; PE</option>
            <option value="CE">CE only</option>
            <option value="PE">PE only</option>
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
                    onManualReconcile={() => {
                      if (!row.positionId) return
                      const typed = window.prompt(
                        'Manually close this position with a known real exit price ' +
                          '(from the broker\'s own app or a contract note). This bypasses the ' +
                          'normal exit path entirely -- only use it when Square Off has already ' +
                          'failed and you have independently confirmed the real fill. Enter the ' +
                          'exit price:',
                      )
                      if (typed === null) return
                      const exitPrice = Number(typed)
                      if (!Number.isFinite(exitPrice) || exitPrice <= 0) {
                        onError(`"${typed}" is not a valid positive price.`)
                        return
                      }
                      setActingRowKey(row.key)
                      manualReconcileMutation.mutate({ positionId: row.positionId, exitPrice })
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
                      {fmtAmt(totalsPnl)}
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
  onManualReconcile,
  onHide,
  isPending,
}: {
  row: TradeRow
  onApprove: () => void
  onReject: () => void
  onSquareOff: () => void
  onManualReconcile: () => void
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
      <td>
        {row.entryPrice !== null ? fmtAmt(row.entryPrice) : '—'}
        {row.entrySlippage !== null && (
          <div
            className={row.entrySlippage >= 0 ? 'pnl-positive' : 'pnl-negative'}
            style={{ fontSize: '0.7rem' }}
          >
            slip {row.entrySlippage >= 0 ? '+' : ''}
            {fmtAmt(row.entrySlippage)}
          </div>
        )}
      </td>
      <td>
        {row.status === 'position_open' ? (
          row.ltp !== null ? (
            fmtAmt(row.ltp)
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
            {row.targetPrice !== null ? fmtAmt(row.targetPrice) : '—'} /{' '}
            {slTsl !== null ? `${fmtAmt(slTsl)} (${slTslLabel})` : '—'}
          </>
        ) : (
          '—'
        )}
      </td>
      <td>
        {row.pnl !== null ? (
          <span className={row.pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}>
            {row.pnl >= 0 ? '+' : ''}
            {fmtAmt(row.pnl)}
            {row.isPnlRealized ? '' : ' (unrl.)'}
          </span>
        ) : (
          '—'
        )}
        {row.exitSlippage !== null && (
          <div
            className={row.exitSlippage >= 0 ? 'pnl-positive' : 'pnl-negative'}
            style={{ fontSize: '0.7rem' }}
          >
            slip {row.exitSlippage >= 0 ? '+' : ''}
            {fmtAmt(row.exitSlippage)}
          </div>
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
        {(row.status === 'closed' || row.status === 'rejected' || row.status === 'cancelled') && !showMore && (
          <button className="btn-ghost" onClick={() => setShowMore(true)}>
            View more
          </button>
        )}
        {(row.status === 'closed' || row.status === 'rejected' || row.status === 'cancelled') && showMore && (
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
        {row.status === 'position_open' && showMore && (
          <button
            className="btn-ghost"
            disabled={isPending}
            title="Fallback for a position Square Off can't close -- enter a known real exit price to close it locally."
            onClick={onManualReconcile}
          >
            Manual reconcile
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
                <th>Stop / TSL</th>
                <th>Target</th>
                <th>Exit Via</th>
                <th>P&amp;L</th>
                <th>Slippage</th>
                <th>Closed</th>
              </tr>
            </thead>
            <tbody>
              {row.legs.map((leg) => {
                // Same convention as the summary row's own Target/SL-TSL
                // column (slTsl/slTslLabel above) -- PositionLegOut.
                // trail_stop_price is only non-null once *this leg's own*
                // TrailPlan has activated (see api/v1/execution.py's leg
                // construction), so per-leg TSL state is visible even when
                // some legs of a staged position are trailing and others
                // are still at their original static stop.
                const legSlTsl = leg.trail_stop_price ?? leg.stop_price
                const legSlTslLabel = leg.trail_stop_price !== null ? 'TSL' : 'SL'
                return (
                  <tr key={leg.leg_index}>
                    <td>
                      {leg.leg_index + 1}/{row.legs.length}
                    </td>
                    <td>{leg.kind}</td>
                    <td>{leg.qty}</td>
                    <td>{legSlTsl !== null ? `${fmtAmt(legSlTsl)} (${legSlTslLabel})` : '—'}</td>
                    <td>{leg.target_price !== null ? fmtAmt(leg.target_price) : '—'}</td>
                    <td>{exitReasonLabel(leg.exit_reason)}</td>
                    <td>
                      {leg.realized_pnl !== null ? (
                        <span className={leg.realized_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}>
                          {leg.realized_pnl >= 0 ? '+' : ''}
                          {fmtAmt(leg.realized_pnl)}
                        </span>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td>
                      {leg.slippage !== null ? (
                        <span className={leg.slippage >= 0 ? 'pnl-positive' : 'pnl-negative'}>
                          {leg.slippage >= 0 ? '+' : ''}
                          {fmtAmt(leg.slippage)}
                        </span>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td>{leg.closed_at ? new Date(leg.closed_at).toLocaleTimeString() : '—'}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </td>
      </tr>
    )}
    </>
  )
}
