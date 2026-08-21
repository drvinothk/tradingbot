import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import type { BrokerAccountOut } from '../api/types'

export function useBrokerAccounts() {
  return useQuery({
    queryKey: ['broker-accounts'],
    queryFn: () => api.get<BrokerAccountOut[]>('/broker-accounts'),
  })
}
