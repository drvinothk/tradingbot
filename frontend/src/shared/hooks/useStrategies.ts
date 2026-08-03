import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import type { StrategyConfigOut } from '../api/types'

// Same queryFn/queryKey copy-pasted across StrategiesPage/ReportsPage,
// factored out here. Preserves the literal ['strategies'] key — no page
// currently invalidates it from another page, so this doesn't change any
// existing cache-invalidation behavior.
export function useStrategies() {
  return useQuery({
    queryKey: ['strategies'],
    queryFn: () => api.get<StrategyConfigOut[]>('/strategies'),
  })
}
