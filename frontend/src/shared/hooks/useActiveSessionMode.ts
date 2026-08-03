import { useQuery } from '@tanstack/react-query'
import { api, shoonyaApi } from '../api/client'
import type { SessionOut, ShoonyaStatusOut } from '../api/types'

// Surfaces the current safe-mode + connected-broker state so it's visible
// everywhere in the app, not just per-row on the Sessions page. Added after
// an audit found that connecting Shoonya (for real market data) could
// silently cause paper trades to route through it for order placement too —
// this banner is the fastest way an operator would notice that happening,
// on top of the backend fix that actually closes the gap
// (composition.get_execution_broker).
export interface ActiveSessionMode {
  isLoading: boolean
  activeSession: SessionOut | null
  shoonyaConnected: boolean
}

export function useActiveSessionMode(): ActiveSessionMode {
  const sessionsQuery = useQuery({
    queryKey: ['sessions'],
    queryFn: () => api.get<SessionOut[]>('/sessions'),
  })

  const shoonyaStatusQuery = useQuery({
    queryKey: ['shoonya-status'],
    queryFn: () => shoonyaApi.get<ShoonyaStatusOut>('/shoonya/status'),
    refetchInterval: 15_000,
  })

  const activeSession = sessionsQuery.data?.find((s) => s.status === 'active') ?? null

  return {
    isLoading: sessionsQuery.isLoading || shoonyaStatusQuery.isLoading,
    activeSession,
    shoonyaConnected: shoonyaStatusQuery.data?.connected ?? false,
  }
}
