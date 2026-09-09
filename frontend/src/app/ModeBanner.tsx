import { useQuery } from '@tanstack/react-query'
import { api } from '../shared/api/client'
import { useActiveSessionMode } from '../shared/hooks/useActiveSessionMode'
import { FeedLatencyBadge } from '../shared/components/FeedLatencyBadge'
import { isWithinMarketHoursIST } from '../shared/time/ist'
import type { ProviderPreferenceOut } from '../shared/api/types'

// 2026-09-08 (paper/live inversion): live_enabled is now the normal resting
// state (the daily session is born live), so it gets the plain label.
// paper_only means the "Go Paper" master switch is engaged -- a deliberate,
// usually-temporary global clamp -- so it's called out, but it's not an
// alarm state (see isAlarming below, which still only covers the three
// emergency modes).
const MODE_LABELS: Record<string, string> = {
  paper_only: 'Paper (global clamp on)',
  live_enabled: 'Live',
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
  // Not an alarm, but worth flagging: "Go Paper" is engaged, so no strategy
  // is placing real orders regardless of its own Live mark.
  const isPaperClamp = activeSession?.mode === 'paper_only'

  const activeLeg = providerQuery.data?.live_active_leg ?? null
  const providerSuffix = activeLeg ? ` (${PROVIDER_LABELS[activeLeg] ?? activeLeg})` : ''

  // Broker/REST -- the order-execution path (this is what actually places
  // orders), kept separate from WS feed health above: REST failing blocks
  // live order placement even if market data is still fine via a WS
  // failback. States mapped from the two booleans /shoonya/status actually
  // gives us: no valid session at all -> red "Not Connected"; session valid
  // and data flowing -> green "Connected"; session valid but no fresh data.
  // That last case is only worth an amber "Connecting..." *during* market
  // hours -- outside them a valid-but-quiet feed is the normal resting state
  // every evening/weekend, so it reads as a neutral "Idle - market closed"
  // instead of nagging indefinitely. badge-live is the standing
  // "something's wrong" red (rejected trade, dead feed during hours, etc.).
  const marketOpen = isWithinMarketHoursIST()
  const brokerClass = !shoonyaSessionValid
    ? 'badge-live'
    : shoonyaConnected
      ? 'badge-success'
      : marketOpen
        ? 'badge-warning'
        : 'badge'
  const brokerText = !shoonyaSessionValid
    ? 'Broker: Shoonya (Not Connected)'
    : shoonyaConnected
      ? 'Broker: Shoonya (Connected)'
      : marketOpen
        ? 'Broker: Shoonya (Connecting...)'
        : 'Broker: Shoonya (Idle — market closed)'

  return (
    <div className={`mode-banner${isAlarming ? ' mode-banner-alarm' : ''}`}>
      <span>
        {activeSession ? `Active session: ${modeLabel}` : 'No active session'}
        {isPaperClamp && (
          <span className="badge badge-warning" style={{ marginLeft: '0.5rem' }}>
            Go Paper engaged — no real orders
          </span>
        )}
      </span>
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
