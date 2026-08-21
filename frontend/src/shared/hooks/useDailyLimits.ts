import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { DailyLimitsOut } from '../api/types'

export function useDailyLimits() {
  return useQuery({
    queryKey: ['system-settings', 'daily-limits'],
    queryFn: () => api.get<DailyLimitsOut>('/system-settings/daily-limits'),
  })
}

export function useSetDailyLimits() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: DailyLimitsOut) => api.patch<DailyLimitsOut>('/system-settings/daily-limits', body),
    onSuccess: (data) => queryClient.setQueryData(['system-settings', 'daily-limits'], data),
  })
}
