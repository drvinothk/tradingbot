import type {
  InstrumentOut,
  OrderOut,
  PositionLegOut,
  PositionOut,
  RunningStrategyOut,
} from '../api/types'
import { exitReasonLabel, friendlyTradeLabel } from '../format/friendlyLabel'

export type TradeRowStatus =
  | 'pending_approval'
  | 'order_sent'
  | 'position_open'
  | 'closing'
  | 'rejected'
  | 'cancelled'
  | 'closed'

export interface TradeRow {
  key: string
  status: TradeRowStatus
  label: string
  strategyType: string | null
  strike: number | null
  expiryDate: string | null
  optionType: string | null
  // Real lot count (qty_lots), not raw contract quantity -- see
  // CLAUDE.md's "qty_lots, not raw quantity" rule. Derived client-side from
  // `qty / lot_size` via `resolveLotSize` below since OrderOut/PositionOut
  // only ever carry the absolute contract qty, never a lots field.
  lots: number | null
  // The contract's lot size (e.g. 65 for NIFTY) -- `null` only when no
  // matching Instrument could be resolved (approval-stage rows, which have
  // no contract_symbol yet) so the UI can fall back to a plain "lot(s)"
  // label instead of fabricating an "x0" suffix.
  lotSize: number | null
  entryPrice: number | null
  ltp: number | null
  targetPrice: number | null
  stopPrice: number | null
  trailStopPrice: number | null
  pnl: number | null
  isPnlRealized: boolean
  // Position.entry_slippage / net TradeOutcome.slippage -- positive means a
  // favorable fill relative to what was intended. Both null for any row
  // that isn't a position (pending approval, order-sent, in-flight exit --
  // none of those have a fill/close to measure yet); exitSlippage stays
  // null while the position is still open too.
  entrySlippage: number | null
  exitSlippage: number | null
  // How the position actually closed (target/stop/trail/manual/eod/...) --
  // TradeOutcome.exit_reason once genuinely closed. A 'closing' row (an
  // in-flight, not-yet-filled exit order) instead carries that same order's
  // *intended* reason (Order.intended_exit_reason) -- what's driving this
  // pending exit, not yet a confirmed outcome. `null` for row types that
  // never have either (pending approvals, order-sent/rejected/cancelled).
  exitReason: string | null
  openedAt: string | null
  closedAt: string | null
  timestamp: string
  // present only for rows a UI action can act on
  approvalId?: string
  positionId?: string
  // True on a 'position_open' row when an in-flight (submitted, not yet
  // filled/rejected/cancelled) exit order already exists for this position
  // -- the position's own 'closing' row is the visible representation of
  // that order; this flag is just what the UI uses to disable a second
  // Square Off click on the position row itself.
  hasPendingExit?: boolean
  // Which broker a trade actually fired to (or, for a not-yet-dispatched
  // pending approval, the best guess available pre-dispatch) -- 'live'/
  // 'paper'/null. This is what buckets a row into Today's Trades vs
  // Today's Paper Trades; see buildTradeRows' own docstring for why this
  // is the order's own recorded mode, not the session/strategy's current
  // config.
  mode: 'live' | 'paper' | null
  // The session this row's underlying run/order/position actually belongs
  // to -- only used as a last-resort bucketing fallback when `mode` itself
  // is null (a data-integrity gap, e.g. a position whose opening order
  // can't be resolved), so such a row still appears somewhere rather than
  // silently vanishing from both buckets.
  sessionId: string | null
  // Per-leg detail for a staged (multi-leg) exit -- always `[]` for
  // approval/order rows (they have no legs) and for a legacy single-exit
  // position. `legs.length > 1` is what the UI treats as "this was a
  // staged exit" (see ControlRoomPage's Exit Via cell / expand toggle).
  legs: PositionLegOut[]
}

const STATUS_ORDER: Record<TradeRowStatus, number> = {
  pending_approval: 0,
  order_sent: 1,
  position_open: 2,
  closing: 3,
  rejected: 4,
  cancelled: 5,
  closed: 6,
}

// Both statuses mean "this order never became/stayed a fill" -- used for
// control flow (should this order still be treated as in-flight?). The
// *displayed* status is NOT collapsed from this any more (see below) -- a
// broker-rejected order and a system-cancelled order (e.g. a resting
// protective stop cancelled because the position exited a different way)
// are materially different and must render as different statuses/labels.
const TERMINAL_UNFILLED_ORDER_STATUSES = new Set(['rejected', 'cancelled'])

/** Resolves the lot size for a tradable-contract symbol (e.g.
 * "NIFTY25AUG26C24250") by matching it against `GET /instruments`'
 * underlying `symbol` (e.g. "NIFTY") as a prefix -- the only linkage
 * available without a backend change (`OptionContract` itself has no
 * lot_size column; it lives on `Instrument`, joined server-side only for
 * `underlying_symbol` validation, not returned on Order/Position rows).
 * Longest-symbol-first so a hypothetical future instrument whose symbol is
 * itself a prefix of another (unlike today's NIFTY/BANKNIFTY pair) can't
 * match the wrong one. */
function resolveLotSize(contractSymbol: string | null, instruments: InstrumentOut[]): number | null {
  if (!contractSymbol) return null
  const match = [...instruments]
    .sort((a, b) => b.symbol.length - a.symbol.length)
    .find((i) => contractSymbol.startsWith(i.symbol))
  return match?.lot_size ?? null
}

/** Converts an absolute contract quantity into a real lot count, per
 * CLAUDE.md's "qty_lots, not raw quantity" rule -- `qty` on OrderOut/
 * PositionOut is always `qty_lots * lot_size`, never a lot count itself. */
function qtyToLots(qty: number, lotSize: number | null): number | null {
  return lotSize ? Math.round(qty / lotSize) : null
}

/** Builds the unified "Today's Trades" row list — see the UI dashboard
 * plan's Control Room spec. Merges three sources that each only cover part
 * of a trade's lifecycle: `RunningStrategyOut.pending_approvals` (Approve/
 * Reject stage), `GET /orders` (order-sent / rejected stage), and
 * `GET /positions` (open / closed stage).
 *
 * `GET /orders`/`GET /positions` now join `OptionContract` (symbol/strike/
 * expiry/option_type — all plain stored columns, populated verbatim from
 * the broker's instrument master at sync time, not computed at read time)
 * plus strategy_type/target/stop/trail/LTP/P&L/mode server-side, so a row
 * for an actual order or position always has the real tradable-contract
 * identity AND the real mode it fired to the broker with. `pending_
 * approvals` (from `GET /strategies/running`) has no such join — that
 * endpoint only ever carries `option_contract_id`, so an approval-stage
 * row still falls back to the strategy+time label until it either fills
 * (becomes an order/position row) or is rejected.
 *
 * This function does NOT scope its inputs to one session — pass every
 * run/order/position from every session, and use each row's own `mode`
 * to bucket it into Live vs Paper afterward (see ControlRoomPage). Mode is
 * the entry order's *actual recorded* live/paper tag, not the session's or
 * strategy's *current* config — a strategy's force_paper override can be
 * flipped after a position already opened, and the config at render time
 * would misrepresent what actually happened. Approval rows have no order
 * yet (nothing's been fired to a broker), so `sessionModeById` (session id
 * -> 'live'/'paper', from `useSessionBuckets`) is the best available guess
 * pre-dispatch — approvals are gated on execution_mode, not paper/live, so
 * in practice this is nearly always 'live', but nothing enforces that
 * server-side today.
 */
export function buildTradeRows(
  runs: RunningStrategyOut[],
  orders: OrderOut[],
  positions: PositionOut[],
  instruments: InstrumentOut[] = [],
  sessionModeById: Map<string, 'live' | 'paper'> = new Map(),
): TradeRow[] {
  const rows: TradeRow[] = []

  const runByPositionId = new Map<string, RunningStrategyOut>()
  for (const run of runs) {
    if (run.open_position) runByPositionId.set(run.open_position.position_id, run)
  }

  // Position.trail_stop_price, looked up by position id -- needed below
  // while walking `orders` (which runs before the `positions` loop further
  // down) so the resting protective stop's own row can say "TSL" instead of
  // "SL" once this position has actually started trailing, matching the
  // position's own summary row (slTsl/slTslLabel in ControlRoomPage.tsx)
  // instead of contradicting it.
  const trailStopPriceByPositionId = new Map<string, number | null>()
  for (const position of positions) {
    trailStopPriceByPositionId.set(position.id, position.trail_stop_price)
  }

  for (const run of runs) {
    for (const approval of run.pending_approvals) {
      rows.push({
        key: `approval-${approval.approval_id}`,
        status: 'pending_approval',
        label: friendlyTradeLabel(run.strategy_type, null, approval.expires_at),
        strategyType: run.strategy_type,
        strike: null,
        expiryDate: null,
        optionType: null,
        lots: approval.qty_lots,
        lotSize: null,
        entryPrice: approval.entry_price,
        ltp: null,
        targetPrice: null,
        stopPrice: null,
        trailStopPrice: null,
        pnl: null,
        isPnlRealized: false,
        entrySlippage: null,
        exitSlippage: null,
        exitReason: null,
        openedAt: null,
        closedAt: null,
        timestamp: approval.expires_at,
        approvalId: approval.approval_id,
        mode: sessionModeById.get(run.trading_session_id) ?? null,
        sessionId: run.trading_session_id,
        legs: [],
      })
    }
  }

  // Populated while walking `orders` below -- a *genuine* in-flight close
  // attempt (submitted, not yet filled/rejected/cancelled) flags the
  // position it's closing so the position row itself can disable Square Off
  // rather than inviting a second, duplicate close attempt. Deliberately
  // excludes the LIVE resting protective SL-LMT (`order_type === 'sl_limit'`,
  // placed once at entry and left resting for the position's entire open
  // lifetime -- see execution_engine.paper.protective_stop's own docstring)
  // -- that order isn't "closing" anything, it's the normal state of every
  // open LIVE position, and `close_position` already cancels it safely
  // before placing its own exit order (see that function's own comment on
  // the "never have both a resting SL-LMT and a fresh exit order active at
  // once" invariant, including the race where the stop fills a moment
  // before the cancel lands). Treating it as a pending exit here previously
  // left Square Off permanently disabled for the whole life of any live
  // position, forcing the manual-reconcile fallback (which force-closes the
  // *local* record without confirming the broker side, unlike Square Off)
  // as the only available action.
  const positionIdsWithPendingExit = new Set<string>()

  for (const order of orders) {
    const isTerminalUnfilled = TERMINAL_UNFILLED_ORDER_STATUSES.has(order.status)
    const isExitOrder = Boolean(order.position_id)
    const isRestingProtectiveStop = order.order_type === 'sl_limit'

    if (order.status === 'filled') {
      continue // filled orders are represented by their resulting Position row instead
    }

    if (isExitOrder && !isTerminalUnfilled) {
      // In-flight exit order -- previously dropped silently (its
      // position_id made it look like "already represented by the
      // Position row"), which left a submitted-but-unfilled exit
      // invisible while the position still showed as fully open with
      // Square Off clickable. Represent it explicitly instead.
      if (!isRestingProtectiveStop) positionIdsWithPendingExit.add(order.position_id as string)
      const exitLotSize = resolveLotSize(order.contract_symbol, instruments)
      // What's actually driving this pending exit -- the resting stop is
      // always a stop-loss *order* by construction (order_type is exclusive
      // to it, nothing else in this codebase ever sets 'sl_limit'), but
      // sync_resting_protective_stop re-prices that same resting order in
      // place once the position's trail activates rather than placing a new
      // one -- so both the label and the Exit Via column (exitReason below)
      // say trail once this position has actually started trailing,
      // matching the position's own summary row instead of contradicting
      // it. Anything else carries its own real intended_exit_reason,
      // recorded by close_position at the moment it placed this exact
      // order. `null` only for a row from before that field existed.
      const isRestingStopTrailing =
        isRestingProtectiveStop &&
        (trailStopPriceByPositionId.get(order.position_id as string) ?? null) !== null
      const orderExitReason = isRestingProtectiveStop
        ? isRestingStopTrailing
          ? 'trail'
          : 'stop'
        : order.intended_exit_reason
      const exitKind = orderExitReason ? exitReasonLabel(orderExitReason) : null
      const label = order.strategy_type
        ? friendlyTradeLabel(order.strategy_type, order.contract_symbol, order.submitted_at)
        : exitKind
          ? `${exitKind} exit · ${order.side} · ${new Date(order.submitted_at).toLocaleTimeString()}`
          : `Exit order · ${order.side} · ${new Date(order.submitted_at).toLocaleTimeString()}`
      rows.push({
        key: `order-${order.id}`,
        status: 'closing',
        label,
        strategyType: order.strategy_type,
        strike: order.strike,
        expiryDate: order.expiry_date,
        optionType: order.option_type,
        lots: qtyToLots(order.qty, exitLotSize),
        lotSize: exitLotSize,
        entryPrice: order.avg_fill_price,
        ltp: null,
        targetPrice: null,
        stopPrice: null,
        trailStopPrice: null,
        pnl: null,
        isPnlRealized: false,
        entrySlippage: null,
        exitSlippage: null,
        exitReason: orderExitReason,
        openedAt: null,
        closedAt: null,
        timestamp: order.submitted_at,
        mode: order.mode === 'live' ? 'live' : 'paper',
        sessionId: order.trading_session_id,
        legs: [],
      })
      continue
    }

    // Entry orders (order-sent / rejected), and terminal-unfilled exit
    // orders -- a cancelled exit order (e.g. a resting protective stop
    // cancelled because the position exited a different way, like a
    // structure-break) is a distinct, non-alarming outcome from a broker
    // rejection and must not share its status or label.
    const label = order.strategy_type
      ? friendlyTradeLabel(order.strategy_type, order.contract_symbol, order.submitted_at)
      : `${isExitOrder ? 'Exit order' : 'Order'} · ${order.side} · ${new Date(order.submitted_at).toLocaleTimeString()}`
    const orderLotSize = resolveLotSize(order.contract_symbol, instruments)
    rows.push({
      key: `order-${order.id}`,
      status: order.status === 'cancelled' ? 'cancelled' : order.status === 'rejected' ? 'rejected' : 'order_sent',
      label,
      strategyType: order.strategy_type,
      strike: order.strike,
      expiryDate: order.expiry_date,
      optionType: order.option_type,
      lots: qtyToLots(order.qty, orderLotSize),
      lotSize: orderLotSize,
      entryPrice: order.avg_fill_price,
      ltp: null,
      targetPrice: null,
      stopPrice: null,
      trailStopPrice: null,
      pnl: null,
      isPnlRealized: false,
      entrySlippage: null,
      exitSlippage: null,
      exitReason: null,
      openedAt: null,
      closedAt: null,
      timestamp: order.submitted_at,
      mode: order.mode === 'live' ? 'live' : 'paper',
      sessionId: order.trading_session_id,
      legs: [],
    })
  }

  for (const position of positions) {
    const run = runByPositionId.get(position.id)
    const strategyType = run?.strategy_type ?? position.strategy_type
    const label = strategyType
      ? friendlyTradeLabel(strategyType, position.contract_symbol, position.opened_at)
      : `Position · ${position.side} · ${new Date(position.opened_at).toLocaleTimeString()}`
    const isOpen = position.status === 'open'
    const positionLotSize = resolveLotSize(position.contract_symbol, instruments)
    // `position.qty` is the *remaining open* qty -- decremented toward 0 as
    // each staged-exit leg closes (see exit_legs.py on the backend), so a
    // fully-closed staged position has `qty === 0`. Sum each leg's own qty
    // instead when legs exist, so a closed multi-leg trade's Lots column
    // shows its real original size, not 0.
    const totalQty =
      position.legs.length > 0
        ? position.legs.reduce((sum, leg) => sum + leg.qty, 0)
        : position.qty
    rows.push({
      key: `position-${position.id}`,
      status: isOpen ? 'position_open' : 'closed',
      label,
      strategyType,
      strike: position.strike,
      expiryDate: position.expiry_date,
      optionType: position.option_type,
      lots: qtyToLots(totalQty, positionLotSize),
      lotSize: positionLotSize,
      entryPrice: position.entry_price,
      ltp: isOpen ? position.ltp : position.exit_price,
      targetPrice: position.target_price,
      stopPrice: position.stop_price,
      trailStopPrice: position.trail_stop_price,
      pnl: isOpen ? position.unrealized_pnl : position.realized_pnl,
      isPnlRealized: !isOpen,
      entrySlippage: position.entry_slippage,
      exitSlippage: isOpen ? null : position.exit_slippage,
      exitReason: isOpen ? null : position.exit_reason,
      openedAt: position.opened_at,
      closedAt: position.closed_at,
      timestamp: position.closed_at ?? position.opened_at,
      positionId: position.id,
      hasPendingExit: isOpen && positionIdsWithPendingExit.has(position.id),
      mode: position.mode === 'live' ? 'live' : position.mode === 'paper' ? 'paper' : null,
      sessionId: position.trading_session_id,
      legs: position.legs,
    })
  }

  rows.sort((a, b) => {
    const statusDiff = STATUS_ORDER[a.status] - STATUS_ORDER[b.status]
    if (statusDiff !== 0) return statusDiff
    // Closed bucket: most-recently-closed first. Every other bucket: most
    // recent first too, matching the plan's "pending on top" intent overall.
    return new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime()
  })

  return rows
}
