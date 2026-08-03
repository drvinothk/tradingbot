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
}

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
}

export interface StrategyRunOut {
  strategy_run_id: string
  status: string
  execution_mode: string
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
