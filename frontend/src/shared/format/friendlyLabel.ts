// Cross-cutting rule from the UI dashboard plan: UUIDs stay as the real
// system identifier everywhere internally (FKs, audit chain, API `id`
// fields) but are never shown in the UI. This computes a friendly,
// human-readable label client-side from data already present in API
// responses (strategy type + instrument + timestamp) rather than showing a
// raw id anywhere.
//
// `GET /orders`/`GET /positions` now join `OptionContract` server-side and
// return `contract_symbol` (the real tradable symbol, e.g.
// "NIFTY25AUG26C24250", stored verbatim on `OptionContract.symbol` at sync
// time — see that model's own docstring) directly on `OrderOut`/
// `PositionOut`. Callers pass it as `instrumentSymbol` here when available;
// `RunningPositionOut`/`PendingApprovalOut` (from `GET /strategies/running`)
// still only carry `option_contract_id`, so an approval-stage row (no order/
// position yet) still falls back to the strategy+time label below.

const STRATEGY_TYPE_LABELS: Record<string, string> = {
  synthetic: 'Synthetic',
  orb: 'ORB',
  vwap_pullback: 'VWAP Pullback',
  ema_micro_pullback: 'EMA Micro-Pullback',
  oi_volume_confirmed: 'OI/Volume Confirmed',
  liquidity_sweep_reversal: 'Liquidity Sweep/Reversal',
}

export function strategyTypeLabel(strategyType: string): string {
  return STRATEGY_TYPE_LABELS[strategyType] ?? strategyType
}

export function friendlyTradeLabel(
  strategyType: string,
  instrumentSymbol: string | null | undefined,
  timestamp: string | Date,
): string {
  const parts: (string | null)[] = [strategyTypeLabel(strategyType), instrumentSymbol ?? null]
  // The trade table has its own dedicated Entry/Exit time column, so once a
  // real contract is known (order/position rows) a trailing timestamp here
  // is just a duplicate of that column. Only the pending-approval fallback
  // (no contract yet, nothing else in the row carries a time) still needs
  // one so the row isn't reduced to a bare, indistinguishable strategy name.
  if (!instrumentSymbol) {
    const time = new Date(timestamp)
    const timeText = Number.isNaN(time.getTime()) ? '' : time.toLocaleTimeString()
    if (timeText) parts.push(timeText)
  }
  return parts.filter((part): part is string => Boolean(part)).join(' · ')
}

// `ExitReason` (backend `app.domain.execution.models.ExitReason`) values,
// mapped to a short label for the "Exit Via" column -- target/stop/trail are
// the common cases and get the plain labels the user actually asked for;
// everything else still gets a readable label rather than the raw enum
// string leaking into the UI.
const EXIT_REASON_LABELS: Record<string, string> = {
  target: 'Target',
  stop: 'SL',
  trail: 'TSL',
  eod_square_off: 'EOD',
  manual: 'Manual',
  structure_break: 'Structure break',
  spread_blowout: 'Spread blowout',
  margin_breach: 'Margin breach',
  reconciled: 'Reconciled',
}

export function exitReasonLabel(exitReason: string | null): string {
  if (!exitReason) return '—'
  return EXIT_REASON_LABELS[exitReason] ?? exitReason
}
