import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import type { ReconciliationHistoryOut, SystemAlertOut } from '../api/types'

// Same 4s poll RunningStrategiesPage already uses, not the unpolled
// useSessions/useStrategies precedent — this needs to feel live.
const POLL_INTERVAL_MS = 4000

export function useSystemAlerts() {
  return useQuery({
    queryKey: ['system-alerts'],
    queryFn: () => api.get<SystemAlertOut[]>('/system-alerts?is_resolved=false'),
    refetchInterval: POLL_INTERVAL_MS,
  })
}

export function useReconciliationRuns(sessionId: string | null) {
  return useQuery({
    queryKey: ['reconciliation-runs', sessionId],
    queryFn: () => api.get<ReconciliationHistoryOut>(`/sessions/${sessionId}/reconciliation-runs`),
    enabled: sessionId !== null,
    refetchInterval: POLL_INTERVAL_MS,
  })
}
