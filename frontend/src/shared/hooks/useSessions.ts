import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import type { SessionOut } from '../api/types'

// Same queryFn/queryKey copy-pasted across SessionsPage/StrategiesPage/
// ReportsPage, factored out here. Preserves the literal ['sessions'] key —
// no page currently invalidates it from another page, so this doesn't
// change any existing cache-invalidation behavior.
export function useSessions() {
  return useQuery({
    queryKey: ['sessions'],
    queryFn: () => api.get<SessionOut[]>('/sessions'),
  })
}
