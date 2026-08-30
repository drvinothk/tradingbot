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

  // Broker/REST -- the order-execution path (this is what actually places
  // orders), kept separate from WS feed health above: REST failing blocks
  // live order placement even if market data is still fine via a WS
  // failback. Three states mapped from the two booleans /shoonya/status
  // actually gives us: no valid session at all -> red "Not Connected";
  // session valid and data flowing -> green "Connected"; session valid but
  // no fresh data yet (a reconnect/retry in progress) -> amber
  // "Connecting...". badge-live is this app's standing "something's wrong"
  // red (reused for a stale/dead feed, a rejected trade, etc.).
  const brokerClass = !shoonyaSessionValid ? 'badge-live' : shoonyaConnected ? 'badge-success' : 'badge-warning'
  const brokerText = !shoonyaSessionValid
    ? 'Broker: Shoonya (Not Connected)'
    : shoonyaConnected
      ? 'Broker: Shoonya (Connected)'
      : 'Broker: Shoonya (Connecting...)'

  return (
    <div className={`mode-banner${isAlarming ? ' mode-banner-alarm' : ''}`}>
      <span>{activeSession ? `Active session: ${modeLabel}` : 'No active session'}</span>
      <span className="row-actions">
        <span className="muted">
          <FeedLatencyBadge feedAgeSeconds={feedAgeSeconds} feedState={feedState} />
          {providerSuffix}
        </span>
        <span className={`badge ${brokerClass}`}>{brokerText}</span>
      </span>
    </div>
  )
}
