// Hand-written TS types mirroring backend/app/api/v1's Pydantic *Out models.
// No OpenAPI-codegen tooling — the API surface is small enough that a
// generator adds a dependency for little benefit right now.

export type ExecutionMode = 'auto' | 'approval_required'
export type FundingMode = 'cash' | 'mtf'
export type StrategyRunStatus = 'scanning' | 'in_position' | 'paused' | 'stopped'
export type StrategyType =
  | 'synthetic'
  | 'orb'
  | 'vwap_pullback'
  | 'ema_micro_pullback'
  | 'oi_volume_confirmed'
  | 'liquidity_sweep_reversal'
export type RuntimeMode = 'force_paper'
export type UnderlyingSymbol = 'NIFTY' | 'BANKNIFTY'

export interface UserOut {
  id: string
  email: string
  display_name: string
}

export interface ShoonyaStatusOut {
  connected: boolean
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
  status: string
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
  // The entry order's mode ('live'/'paper') -- what actually fired to the
  // broker when this position opened, not the session's/strategy's current
  // config (which can drift after the fact). Ground truth for Live vs
  // Paper bucketing.
  mode: string | null
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
  daily_max_lots: number
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
