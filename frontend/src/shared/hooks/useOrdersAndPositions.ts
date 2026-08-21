import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import type { OrderOut, PositionOut } from '../api/types'

const POLL_INTERVAL_MS = 4000

// GET /orders and GET /positions (backend/app/api/v1/execution.py) are both
// scoped to a single trading_session_id query param, not workspace-wide --
// so Control Room's Today's Trades tables poll them per-session (Live
// bucket / Paper bucket), same shape as useRunningStrategies' own poll.
export function useOrders(tradingSessionId: string | null) {
  return useQuery({
    queryKey: ['orders', tradingSessionId],
    queryFn: () => api.get<OrderOut[]>(`/orders?trading_session_id=${tradingSessionId}`),
    enabled: tradingSessionId !== null,
    refetchInterval: POLL_INTERVAL_MS,
  })
}

export function usePositions(tradingSessionId: string | null) {
  return useQuery({
    queryKey: ['positions', tradingSessionId],
    queryFn: () => api.get<PositionOut[]>(`/positions?trading_session_id=${tradingSessionId}`),
    enabled: tradingSessionId !== null,
    refetchInterval: POLL_INTERVAL_MS,
  })
}
