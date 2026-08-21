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
const EMERGENCY_MODES = new Set(['degraded_mode', 'reconciliation_lock', 'kill_switch'])

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

  // Same queryKey AdvancedPage's ShoonyaConnectionRow uses for the
  // identical endpoint -- previously two independent keys
  // (['shoonya-status'] here, ['shoonya', 'status'] there) meant this
  // header banner could keep showing stale status for up to 15s after a
  // real Connect succeeded on Advanced, since invalidating one cache never
  // touched the other.
  const shoonyaStatusQuery = useQuery({
    queryKey: ['shoonya', 'status'],
    queryFn: () => shoonyaApi.get<ShoonyaStatusOut>('/shoonya/status'),
    refetchInterval: 15_000,
  })

  const activeSessions = sessionsQuery.data?.filter((s) => s.status === 'active') ?? []
  // Prefer surfacing a session that's actually in an emergency state --
  // with two simultaneously-active sessions (Live + Paper), picking
  // whichever happened to come first in array order could pick the boring
  // paper_only one and hide the fact that the other is in kill_switch/
  // degraded_mode/reconciliation_lock, which is exactly the case this
  // banner exists to surface.
  const activeSession =
    activeSessions.find((s) => EMERGENCY_MODES.has(s.mode)) ?? activeSessions[0] ?? null

  return {
    isLoading: sessionsQuery.isLoading || shoonyaStatusQuery.isLoading,
    activeSession,
    shoonyaConnected: shoonyaStatusQuery.data?.connected ?? false,
  }
}
