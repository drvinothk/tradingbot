import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import type { RunningStrategyOut } from '../api/types'

const POLL_INTERVAL_MS = 4000

export function useRunningStrategies() {
  return useQuery({
    queryKey: ['strategies', 'running'],
    queryFn: () => api.get<RunningStrategyOut[]>('/strategies/running'),
    refetchInterval: POLL_INTERVAL_MS,
  })
}
