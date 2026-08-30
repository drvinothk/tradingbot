import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import type { CandleOut } from '../api/types'

const POLL_INTERVAL_MS = 5000

// Only "60s" bars are ever persisted (see backend BAR_TIMEFRAME) -- callers
// wanting a coarser interval resample this raw 1-min series client-side.
export function useCandles(instrumentId: string | null, limit = 200) {
  return useQuery({
    queryKey: ['market-data', 'candles', instrumentId, limit],
    queryFn: () =>
      api.get<CandleOut[]>(
        `/market-data/candles?instrument_id=${instrumentId}&timeframe=60s&limit=${limit}`,
      ),
    enabled: instrumentId !== null,
    refetchInterval: POLL_INTERVAL_MS,
  })
}
