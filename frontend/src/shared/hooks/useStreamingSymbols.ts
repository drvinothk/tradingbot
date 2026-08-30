import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import type { StreamingSymbolsOut } from '../api/types'

const POLL_INTERVAL_MS = 5000

// Underlyings market_data.registry is actually ingesting right now -- backs
// the chart's symbol picker so it never offers a symbol with nothing behind
// it (see registry.subscribed_symbols's own docstring).
export function useStreamingSymbols() {
  return useQuery({
    queryKey: ['market-data', 'streaming-symbols'],
    queryFn: () => api.get<StreamingSymbolsOut>('/market-data/streaming-symbols'),
    refetchInterval: POLL_INTERVAL_MS,
  })
}
