// Hand-written TS types mirroring backend/app/api/v1's Pydantic *Out models.
// No OpenAPI-codegen tooling — the API surface is small enough that a
// generator adds a dependency for little benefit right now.

export type ExecutionMode = 'auto' | 'approval_required'
export type StrategyType =
  | 'synthetic'
  | 'orb'
  | 'orb_conviction'
  | 'vwap_pullback'
  | 'vwap_pullback_conviction'
  | 'ema_micro_pullback'
  | 'ema_micro_pullback_conviction'
  | 'oi_volume_confirmed'
  | 'oi_volume_confirmed_conviction'
  | 'liquidity_sweep_reversal'
  | 'liquidity_sweep_reversal_conviction'
export type RuntimeMode = 'force_paper'
export type UnderlyingSymbol = 'NIFTY' | 'BANKNIFTY'

export interface UserOut {
  id: string
  email: string
  display_name: string
}

export interface ShoonyaStatusOut {
  // `connected` = data is actually flowing right now. `session_valid` = a real
  // broker adapter is installed and hasn't hit an auth failure (Shoonya only;
  // Alice Blue reuses this type and omits it).
  connected: boolean
  session_valid?: boolean
  // Age (seconds) and classification of whichever signal (streamed tick or
  // REST-fallback bar) is freshest right now, across the tradable
  // underlyings -- Shoonya only, Alice Blue omits both, same as session_valid.
  feed_age_seconds?: number | null
  feed_state?: 'live' | 'degraded' | 'stale' | 'dead' | null
}

export interface ShoonyaLoginUrlOut {
  authorize_url: string
}

export interface BrokerAccountOut {
  id: string
  broker_type: string
  label: string
  status: string
}

export interface SessionOut {
  id: string
  mode: string
  status: string
  broker_account_id: string
}

export interface StrategyConfigOut {
  id: string
  name: string
  strategy_type: string
  params: Record<string, unknown>
  is_enabled: boolean
  runtime_mode: string | null
  underlying_symbol: string | null
}

export interface SetStrategyPowerOut {
  is_enabled: boolean
  run_started: boolean
  run_stopped: boolean
  run_id: string | null
  detail: string
}

export interface ProviderPreferenceOut {
  active_provider: string | null
  live_active_leg: string | null
}

export interface InstrumentFirewallOut {
  active_live_instruments: string[]
  recognized_instruments: string[]
}

export type DiagnosticRole = 'default' | 'failback'

export interface DiagnosticRoleStatus {
  running: boolean
  provider: string | null
  run_id: string | null
}

export type DiagnosticStatusOut = Record<DiagnosticRole, DiagnosticRoleStatus>

export interface InstrumentOut {
  id: string
  symbol: string
  exchange: string
  lot_size: number
  expiry_dates: string[]
}

export interface RunningPositionOut {
  position_id: string
  option_contract_id: string
  side: string
  qty: number
  entry_price: number
  // Rupees at risk if the current stop (or every open multi-leg stop)
  // hits right now -- null when there's no stop data to compute from at
  // all, distinct from a genuine 0.
  open_risk: number | null
}

export interface PendingApprovalOut {
  approval_id: string
  trade_intent_id: string
  option_contract_id: string
  side: string
  qty_lots: number
  entry_price: number
  expires_at: string
}

export interface LastSignalOut {
  reason_code: string
  evaluated_at: string
  option_contract_id: string | null
  side: string | null // "CE" / "PE"
  strike: number | null
  expiry_date: string | null
  symbol: string | null
  planned_entry: number | null
  ltp: number | null // fresh reference premium, not the possibly-stale planned_entry
  stop_price: number | null
  target_price: number | null
}

export interface RunningStrategyOut {
  strategy_run_id: string
  strategy_config_id: string
  strategy_name: string
  strategy_type: string
  trading_session_id: string
  execution_mode: string
  status: string
  started_at: string
  open_position: RunningPositionOut | null
  pending_approvals: PendingApprovalOut[]
  data_freshness: string | null
  // Whether a new dispatch for this run would resolve to the real broker
  // right now -- not just "session is live_enabled" (a FORCE_PAPER
  // strategy inside a live session is not). See
  // broker_adapter.composition.is_strategy_routed_live.
  is_live: boolean
  // Market Terminal signal panel (2026-08-30) -- why a SCANNING run
  // hasn't fired, plus (when already resolved at that point) the exact
  // candidate that was rejected. `null` = nothing to report yet, or this
  // run isn't SCANNING right now.
  last_signal: LastSignalOut | null
}

export interface CandleOut {
  bucket_start: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface StreamingSymbolsOut {
  symbols: string[]
}

export interface StrategyRunOut {
  strategy_run_id: string
  status: string
  execution_mode: string
}

export interface OrderOut {
  id: string
  trading_session_id: string
  option_contract_id: string
  trade_intent_id: string | null
  position_id: string | null
  mode: string
  side: string
  order_type: string
  qty: number
  status: string
  filled_qty: number
  avg_fill_price: number | null
  broker_order_id: string
  submitted_at: string
  // Additive lookups joined server-side (GET /orders) -- null when the
  // underlying join has nothing (e.g. an exit order has no trade_intent_id,
  // so no strategy_type).
  contract_symbol: string | null
  strike: number | null
  expiry_date: string | null
  option_type: string | null
  strategy_type: string | null
}

export interface PositionLegOut {
  leg_index: number
  kind: string
  qty: number
  status: string
  stop_price: number | null
  target_price: number | null
  trail_stop_price: number | null
  exit_reason: string | null
  realized_pnl: number | null
  closed_at: string | null
}

export interface PositionOut {
  id: string
  trading_session_id: string
  option_contract_id: string
  trade_intent_id: string
  side: string
  qty: number
  entry_price: number
  status: string
  opened_at: string
  closed_at: string | null
  // Additive lookups joined server-side (GET /positions).
  contract_symbol: string | null
  strike: number | null
  expiry_date: string | null
  option_type: string | null
  strategy_type: string | null
  target_price: number | null
  stop_price: number | null
  trail_stop_price: number | null
  ltp: number | null
  unrealized_pnl: number | null
  exit_price: number | null
  realized_pnl: number | null
  // How the position actually closed (target/stop/trail/manual/eod/...) --
  // `null` for an open position or one with no recorded outcome yet.
  exit_reason: string | null
  // The entry order's mode ('live'/'paper') -- what actually fired to the
  // broker when this position opened, not the session's/strategy's current
  // config (which can drift after the fact). Ground truth for Live vs
  // Paper bucketing.
  mode: string | null
  // One row per staged exit leg (empty for a legacy single-exit position).
  // `legs.length > 1` is the UI's own signal that this trade was a staged
  // (multi-leg) exit -- see PositionExitLeg's own docstring on the backend.
  legs: PositionLegOut[]
}

export interface SquareOffPositionOut {
  success: boolean
  position_id: string
  detail?: string
  exit_price?: number
  realized_pnl?: number
  slippage?: number
  exit_reason?: string
  closed_at?: string
}

export interface DailyLimitsOut {
  daily_budget_amount: number
  daily_target_profit: number
  daily_loss_cap: number
  funding_mode: string
}

export interface MaxTradesPerDayOut {
  max_trades_per_day: number
}

export interface UnderlyingFeedTelemetryOut {
  symbol: string
  feed_age_seconds: number | null
  feed_state: string
}

export interface MarketDataTelemetryOut {
  underlyings: UnderlyingFeedTelemetryOut[]
}

export interface PerformanceStatsOut {
  trade_count: number
  win_count: number
  loss_count: number
  win_rate: number
  avg_win: number
  avg_loss: number
  profit_factor: number | null
  max_drawdown: number
  total_realized_pnl: number
  total_slippage: number
  signal_count: number
  dispatched_count: number
  filled_count: number
}

export interface DailyReportOut extends PerformanceStatsOut {
  trading_session_id: string
}

export interface ScorecardOut extends PerformanceStatsOut {
  strategy_config_id: string
}

export interface SystemAlertOut {
  id: string
  trading_session_id: string | null
  severity: string
  category: string
  message: string
  payload: Record<string, unknown>
  created_at: string
  resolved_at: string | null
  is_resolved: boolean
}

export interface ReconciliationRunOut {
  id: string
  trigger_type: string
  mismatches_found: number
  action_taken: string
  started_at: string
  finished_at: string
}

export interface BrokerSyncStateOut {
  option_contract_id: string
  local_qty: number
  broker_qty: number
  is_mismatched: boolean
  checked_at: string
}

export interface ReconciliationHistoryOut {
  runs: ReconciliationRunOut[]
  current_mismatches: BrokerSyncStateOut[]
}
