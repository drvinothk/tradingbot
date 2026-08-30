import { useQuery } from '@tanstack/react-query'
import { api } from '../shared/api/client'
import { useActiveSessionMode } from '../shared/hooks/useActiveSessionMode'
import { FeedLatencyBadge } from '../shared/components/FeedLatencyBadge'
import type { ProviderPreferenceOut } from '../shared/api/types'

const MODE_LABELS: Record<string, string> = {
  paper_only: 'Paper only',
  live_enabled: 'Live enabled',
  degraded_mode: 'Degraded mode',
  reconciliation_lock: 'Reconciliation lock',
  kill_switch: 'Kill switch',
}

// Which provider is actually serving ticks right now (Shoonya or its Alice
// Blue failback) -- same query key AdvancedPage's failover-override control
// already uses for GET /market-data/provider-preference, so both share one
// cache.
const PROVIDER_LABELS: Record<string, string> = {
  shoonya: 'Shoonya',
  alice_blue: 'Alice Blue',
}

export function ModeBanner() {
  const { isLoading, activeSession, shoonyaConnected, shoonyaSessionValid, feedAgeSeconds, feedState } =
    useActiveSessionMode()
  const providerQuery = useQuery({
    queryKey: ['market-data', 'provider-preference'],
    queryFn: () => api.get<ProviderPreferenceOut>('/market-data/provider-preference'),
    refetchInterval: 15_000,
  })

  if (isLoading) {
    return null
  }

  const modeLabel = activeSession ? (MODE_LABELS[activeSession.mode] ?? activeSession.mode) : null
  const isAlarming =
    activeSession != null &&
    ['degraded_mode', 'reconciliation_lock', 'kill_switch'].includes(activeSession.mode)

  const activeLeg = providerQuery.data?.live_active_leg ?? null
  const providerSuffix = activeLeg ? ` (${PROVIDER_LABELS[activeLeg] ?? activeLeg})` : ''

  // The order-execution-path signal, kept separate from feed health: REST
  // failing blocks live order placement even if market data is still fine
  // via a WS failback, so collapsing the two into one dot would hide that.
  // badge-success (not badge-live) for the good case -- badge-live means
  // "something's wrong" everywhere else in this app (rejected trades, a
  // stale/dead feed, both also shown in this same ribbon now), so reusing
  // it here for a routine "connected" state would collide with that.
  const restClass = !shoonyaSessionValid ? 'badge' : shoonyaConnected ? 'badge-success' : 'badge-warning'
  const restText = !shoonyaSessionValid ? 'Shoonya: Mock' : 'Shoonya: Connected'

  return (
    <div className={`mode-banner${isAlarming ? ' mode-banner-alarm' : ''}`}>
      <span>{activeSession ? `Active session: ${modeLabel}` : 'No active session'}</span>
      <span className="row-actions">
        <span className="muted">
          <FeedLatencyBadge feedAgeSeconds={feedAgeSeconds} feedState={feedState} />
          {providerSuffix}
        </span>
        <span className={`badge ${restClass}`}>{restText}</span>
      </span>
    </div>
  )
}
