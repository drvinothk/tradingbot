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
  const time = new Date(timestamp)
  const timeText = Number.isNaN(time.getTime()) ? '' : time.toLocaleTimeString()
  const parts = [strategyTypeLabel(strategyType), instrumentSymbol ?? null, timeText].filter(
    (part): part is string => Boolean(part),
  )
  return parts.join(' · ')
}

export function shortId(id: string): string {
  return id.slice(0, 8)
}
