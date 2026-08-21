import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { api, ApiError } from '../../shared/api/client'
import { useSessionBuckets } from '../../shared/hooks/useSessionBuckets'
import { useRunningStrategies } from '../../shared/hooks/useRunningStrategies'
import { useOrders, usePositions } from '../../shared/hooks/useOrdersAndPositions'
import { useInstruments } from '../../shared/hooks/useInstruments'
import { useActiveSessionMode } from '../../shared/hooks/useActiveSessionMode'
import { buildTradeRows, type TradeRow, type TradeRowStatus } from '../../shared/trades/buildTradeRows'
import type { RunningStrategyOut, SessionOut, SquareOffPositionOut } from '../../shared/api/types'

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

// Only these two modes place real orders -- the three emergency modes
// (kill_switch/degraded_mode/reconciliation_lock) are NOT "live" for the
// purpose of the Go Live/Go Paper toggle, even though they're also not
// paper_only. Treating them as "live" made "Go Paper" look normally
// enabled while the session was actually stuck in an emergency state that
// Go Paper can't fix -- see Advanced's Reconciliation & Recovery card.
const LIVE_MODES = new Set(['paper_plus_guarded_live', 'live_enabled'])
const EMERGENCY_MODES = new Set(['kill_switch', 'degraded_mode', 'reconciliation_lock'])

// Stable reference so `?? EMPTY_RUNS` doesn't defeat downstream useMemo
// dependency checks the way `?? []` (a fresh array literal every render)
// would.
const EMPTY_RUNS: RunningStrategyOut[] = []

export function ControlRoomPage() {
  const queryClient = useQueryClient()
  const { liveSession, paperSession, isLoading: bucketsLoading } = useSessionBuckets()
  const { shoonyaConnected } = useActiveSessionMode()
  const [actionError, setActionError] = useState<string | null>(null)
  const [hiddenRowKeys, setHiddenRowKeys] = useState<Set<string>>(new Set())
  const [paperExpanded, setPaperExpanded] = useState(false)

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
        shoonyaConnected={shoonyaConnected}
        onError={setActionError}
        onChanged={invalidateTrades}
      />

      {actionError && <p className="error">{actionError}</p>}

      <MetricsStripPlaceholder />

      <TradeBucketCard
        title="Today's Trades (Live)"
        session={liveSession}
        rows={liveRows}
        defaultExpanded
        hiddenRowKeys={hiddenRowKeys}
        onHideRow={hideRow}
        onChanged={invalidateTrades}
        onError={setActionError}
        emptyHint={bucketsLoading ? 'Loading...' : 'No Live trading session is active today.'}
      />

      <TradeBucketCard
        title="Today's Paper Trades"
        session={paperSession}
        rows={paperRows}
        defaultExpanded={false}
        expanded={paperExpanded}
        onToggleExpanded={() => setPaperExpanded((v) => !v)}
        hiddenRowKeys={hiddenRowKeys}
        onHideRow={hideRow}
        onChanged={invalidateTrades}
        onError={setActionError}
        emptyHint={bucketsLoading ? 'Loading...' : 'No Paper trading session is active today.'}
      />

      <AuditTickerPlaceholder />
    </div>
  )
}

function ControlRoomHeader({
  liveSession,
  shoonyaConnected,
  onError,
  onChanged,
}: {
  liveSession: SessionOut | null
  shoonyaConnected: boolean
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
    killSwitchMutation.isPending ? 'locked' : '',
    killArmed && !killSwitchMutation.isPending ? 'armed' : '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <div className="page-header">
      <h2>Control Room</h2>
      <div className="row-actions">
        <span className="muted">
          <span className={`status-dot ${shoonyaConnected ? 'on' : 'off'}`} />{' '}
          Broker: {shoonyaConnected ? 'Shoonya (REAL)' : 'Mock'}
        </span>
        <span className="muted">Backend/Feed latency: <span className="badge badge-wip">WIP</span></span>
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

function MetricsStripPlaceholder() {
  const metrics = ['Net P&L (MTM)', 'Margin Utilized', 'Trades vs. Limit', 'Max Drawdown']
  return (
    <div className="metrics-strip">
      {metrics.map((label) => (
        <div className="metric-box" key={label}>
          <div className="metric-label">{label}</div>
          <div className="metric-value muted">
            — <span className="badge badge-wip">WIP</span>
          </div>
        </div>
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
  defaultExpanded,
  expanded: expandedProp,
  onToggleExpanded,
  hiddenRowKeys,
  onHideRow,
  onChanged,
  onError,
  emptyHint,
}: {
  title: string
  session: SessionOut | null
  rows: TradeRow[]
  defaultExpanded: boolean
  expanded?: boolean
  onToggleExpanded?: () => void
  hiddenRowKeys: Set<string>
  onHideRow: (key: string) => void
  onChanged: () => void
  onError: (message: string | null) => void
  emptyHint: string
}) {
  const [localExpanded, setLocalExpanded] = useState(defaultExpanded)
  const isExpanded = expandedProp ?? localExpanded
  const toggle = onToggleExpanded ?? (() => setLocalExpanded((v) => !v))

  const visibleRows = rows.filter((r) => !hiddenRowKeys.has(r.key))

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
        <h3>
          {title} {session && <span className="muted">({session.mode})</span>}
        </h3>
        <span className={`chevron ${isExpanded ? 'open' : ''}`}>▶</span>
      </div>
      {isExpanded &&
        (!session ? (
          <p className="muted">{emptyHint}</p>
        ) : visibleRows.length === 0 ? (
          <p className="muted">No trades yet today.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Trade</th>
                <th>Lots</th>
                <th>Entry Price</th>
                <th>LTP</th>
                <th>Target / SL-TSL</th>
                <th>P&amp;L</th>
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
          </table>
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

  const slTsl = row.trailStopPrice ?? row.stopPrice
  const slTslLabel = row.trailStopPrice !== null ? 'TSL' : 'SL'

  return (
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
          <span style={{ whiteSpace: 'nowrap' }}>
            {row.targetPrice !== null ? row.targetPrice.toFixed(2) : '—'} /{' '}
            {slTsl !== null ? `${slTsl.toFixed(2)} (${slTslLabel})` : '—'}
          </span>
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
      <td style={{ fontSize: '0.8rem' }}>
        <div>{row.openedAt ? new Date(row.openedAt).toLocaleTimeString() : '—'}</div>
        <div className="muted">{row.closedAt ? new Date(row.closedAt).toLocaleTimeString() : '—'}</div>
      </td>
      <td>
        <span className={`badge ${STATUS_BADGE_CLASS[row.status]}`}>{STATUS_LABELS[row.status]}</span>
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
      </td>
    </tr>
  )
}
