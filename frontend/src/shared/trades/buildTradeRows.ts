import type { InstrumentOut, OrderOut, PositionOut, RunningStrategyOut } from '../api/types'
import { friendlyTradeLabel } from '../format/friendlyLabel'

export type TradeRowStatus =
  | 'pending_approval'
  | 'order_sent'
  | 'position_open'
  | 'closing'
  | 'rejected'
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
}

const STATUS_ORDER: Record<TradeRowStatus, number> = {
  pending_approval: 0,
  order_sent: 1,
  position_open: 2,
  closing: 3,
  rejected: 4,
  closed: 5,
}

const REJECTED_ORDER_STATUSES = new Set(['rejected', 'cancelled'])

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

/** Builds the unified "Today's Trades" row list for one session bucket
 * (Live or Paper) — see the UI dashboard plan's Control Room spec. Merges
 * three sources that each only cover part of a trade's lifecycle:
 * `RunningStrategyOut.pending_approvals` (Approve/Reject stage),
 * `GET /orders` (order-sent / rejected stage), and `GET /positions`
 * (open / closed stage).
 *
 * `GET /orders`/`GET /positions` now join `OptionContract` (symbol/strike/
 * expiry/option_type — all plain stored columns, populated verbatim from
 * the broker's instrument master at sync time, not computed at read time)
 * plus strategy_type/target/stop/trail/LTP/P&L server-side, so a row for an
 * actual order or position always has the real tradable-contract identity.
 * `pending_approvals` (from `GET /strategies/running`) has no such join —
 * that endpoint only ever carries `option_contract_id`, so an
 * approval-stage row still falls back to the strategy+time label until it
 * either fills (becomes an order/position row) or is rejected.
 */
export function buildTradeRows(
  runs: RunningStrategyOut[],
  orders: OrderOut[],
  positions: PositionOut[],
  instruments: InstrumentOut[] = [],
): TradeRow[] {
  const rows: TradeRow[] = []

  const runByPositionId = new Map<string, RunningStrategyOut>()
  for (const run of runs) {
    if (run.open_position) runByPositionId.set(run.open_position.position_id, run)
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
        openedAt: null,
        closedAt: null,
        timestamp: approval.expires_at,
        approvalId: approval.approval_id,
      })
    }
  }

  // Populated while walking `orders` below -- an exit Order always carries
  // `position_id` (see execution.models.Order's own docstring on the
  // entry/exit split), so any exit order still in flight (submitted, not
  // yet filled/rejected/cancelled) flags the position it's closing so the
  // position row itself can disable Square Off rather than inviting a
  // second, duplicate close attempt.
  const positionIdsWithPendingExit = new Set<string>()

  for (const order of orders) {
    const isRejectedOrCancelled = REJECTED_ORDER_STATUSES.has(order.status)
    const isExitOrder = Boolean(order.position_id)

    if (order.status === 'filled') {
      continue // filled orders are represented by their resulting Position row instead
    }

    if (isExitOrder && !isRejectedOrCancelled) {
      // In-flight exit order -- previously dropped silently (its
      // position_id made it look like "already represented by the
      // Position row"), which left a submitted-but-unfilled exit
      // invisible while the position still showed as fully open with
      // Square Off clickable. Represent it explicitly instead.
      positionIdsWithPendingExit.add(order.position_id as string)
      const exitLotSize = resolveLotSize(order.contract_symbol, instruments)
      const label = order.strategy_type
        ? friendlyTradeLabel(order.strategy_type, order.contract_symbol, order.submitted_at)
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
        openedAt: null,
        closedAt: null,
        timestamp: order.submitted_at,
      })
      continue
    }

    // Entry orders (order-sent / rejected), and rejected/cancelled exit
    // orders (which fall back to the same 'rejected' treatment as before).
    const label = order.strategy_type
      ? friendlyTradeLabel(order.strategy_type, order.contract_symbol, order.submitted_at)
      : `Order · ${order.side} · ${new Date(order.submitted_at).toLocaleTimeString()}`
    const orderLotSize = resolveLotSize(order.contract_symbol, instruments)
    rows.push({
      key: `order-${order.id}`,
      status: isRejectedOrCancelled ? 'rejected' : 'order_sent',
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
      openedAt: null,
      closedAt: null,
      timestamp: order.submitted_at,
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
    rows.push({
      key: `position-${position.id}`,
      status: isOpen ? 'position_open' : 'closed',
      label,
      strategyType,
      strike: position.strike,
      expiryDate: position.expiry_date,
      optionType: position.option_type,
      lots: qtyToLots(position.qty, positionLotSize),
      lotSize: positionLotSize,
      entryPrice: position.entry_price,
      ltp: isOpen ? position.ltp : position.exit_price,
      targetPrice: position.target_price,
      stopPrice: position.stop_price,
      trailStopPrice: position.trail_stop_price,
      pnl: isOpen ? position.unrealized_pnl : position.realized_pnl,
      isPnlRealized: !isOpen,
      openedAt: position.opened_at,
      closedAt: position.closed_at,
      timestamp: position.closed_at ?? position.opened_at,
      positionId: position.id,
      hasPendingExit: isOpen && positionIdsWithPendingExit.has(position.id),
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
