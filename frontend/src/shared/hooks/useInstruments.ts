import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import type { InstrumentOut } from '../api/types'

export function useInstruments() {
  return useQuery({
    queryKey: ['instruments'],
    queryFn: () => api.get<InstrumentOut[]>('/instruments'),
  })
}
