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
  orb_conviction: 'ORB Conviction',
  vwap_pullback: 'VWAP Pullback',
  vwap_pullback_conviction: 'VWAP Pullback (Conviction)',
  ema_micro_pullback: 'EMA Micro-Pullback',
  ema_micro_pullback_conviction: 'EMA Micro-Pullback (Conviction)',
  oi_volume_confirmed: 'OI/Volume Confirmed',
  oi_volume_confirmed_conviction: 'OI/Volume Confirmed (Conviction)',
  liquidity_sweep_reversal: 'Liquidity Sweep/Reversal',
  liquidity_sweep_reversal_conviction: 'Liquidity Sweep/Reversal (Conviction)',
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

// Market Terminal signal panel (2026-08-30) -- reason codes from
// ConvictionGateMixin._conviction_reject_reason (backend
// conviction_gates.py) plus each *_conviction strategy's own extra native
// gates. A `conviction_` prefix wraps most of the shared codes at their
// call site (e.g. "conviction_vix_below_band") -- stripped here before
// lookup so one map entry covers both the bare and prefixed form. Anything
// unmapped (a future gate, or an early base-strategy gate that only ever
// logs via _log_once with its own ad hoc key) falls back to a title-cased
// version of the raw code rather than showing nothing.
const SIGNAL_REASON_LABELS: Record<string, string> = {
  vix_below_band: 'VIX below band',
  vix_above_band: 'VIX above band',
  pcr_below_band: 'PCR below band',
  pcr_above_band: 'PCR above band',
  prior_day_trend_disagrees: 'Prior-day trend disagrees',
  prior_day_not_ready: 'Prior-day data not ready',
  htf_ema_trend_disagrees: 'EMA trend disagrees',
  htf_ema_not_ready: 'EMA trend not ready',
  atr_not_expanding: 'ATR not expanding',
  atr_not_ready: 'ATR not ready',
  volume_not_surging: 'Volume not surging',
  volume_not_ready: 'Volume data not ready',
  skip_weekday: 'Skipped weekday',
  ce_only: 'CE-only filter',
  min_bars_since_open: 'Waiting for more bars since open',
  min_ema_spread_atr_ratio: 'EMA9/EMA20 spread too tight',
  oi_price_misaligned: 'OI/price not aligned',
  breakout_too_weak: 'Breakout too weak',
  breakout_strength_not_ready: 'Breakout strength not ready',
  drift_disagrees: 'Underlying drift disagrees',
  drift_not_ready: 'Underlying drift not ready',
  min_displacement_atr: 'Confirmation candle too small',
  time_window: 'Outside trade window',
  range_filter: 'Range width out of band',
  body_ratio: 'Candle body ratio too low',
  vwap_stale: 'VWAP data stale',
}

function titleCaseReasonCode(reasonCode: string): string {
  return reasonCode
    .replace(/^conviction_/, '')
    .split('_')
    .filter(Boolean)
    .map((word) => word[0].toUpperCase() + word.slice(1))
    .join(' ')
}

export function signalReasonLabel(reasonCode: string | null): string {
  if (!reasonCode) return '—'
  const bare = reasonCode.replace(/^conviction_/, '')
  return SIGNAL_REASON_LABELS[bare] ?? titleCaseReasonCode(reasonCode)
}

// A staged (multi-leg) position's PositionOut.exit_reason is the literal
// sentinel "staged" once more than one leg has closed -- not something a
// human should ever see verbatim. This builds a real, friendly summary of
// what actually happened instead, e.g. "Target ×1, Trail ×1". Legs still
// OPEN (exit_reason === null) are excluded -- the position as a whole is
// still 'position_open' in that case, and the "N/M legs closed" badge
// (ControlRoomPage) covers that detail separately.
export function stagedExitSummary(legs: { exit_reason: string | null }[]): string {
  const closedLegs = legs.filter((leg) => leg.exit_reason !== null)
  if (closedLegs.length === 0) return '—'
  const counts = new Map<string, number>()
  for (const leg of closedLegs) {
    const label = exitReasonLabel(leg.exit_reason)
    counts.set(label, (counts.get(label) ?? 0) + 1)
  }
  return [...counts.entries()].map(([label, count]) => `${label} ×${count}`).join(', ')
}
