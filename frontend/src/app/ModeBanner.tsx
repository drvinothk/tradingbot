import { useActiveSessionMode } from '../shared/hooks/useActiveSessionMode'

const MODE_LABELS: Record<string, string> = {
  paper_only: 'Paper only',
  live_enabled: 'Live enabled',
  degraded_mode: 'Degraded mode',
  reconciliation_lock: 'Reconciliation lock',
  kill_switch: 'Kill switch',
}

export function ModeBanner() {
  const { isLoading, activeSession, shoonyaConnected, shoonyaSessionValid } = useActiveSessionMode()

  if (isLoading) {
    return null
  }

  const modeLabel = activeSession ? (MODE_LABELS[activeSession.mode] ?? activeSession.mode) : null
  const isAlarming =
    activeSession != null &&
    ['degraded_mode', 'reconciliation_lock', 'kill_switch'].includes(activeSession.mode)

  // Identity ("Shoonya (REAL)" vs "Mock") tracks session_valid so a brief feed
  // stall doesn't read as "switched to Mock"; a stalled-but-real feed gets an
  // amber "no data" marker instead.
  let brokerText: string
  let brokerClass: string
  if (!shoonyaSessionValid) {
    brokerText = 'Broker: Mock'
    brokerClass = 'badge'
  } else if (shoonyaConnected) {
    brokerText = 'Broker: Shoonya (REAL)'
    brokerClass = 'badge badge-live'
  } else {
    brokerText = 'Broker: Shoonya (REAL) — no data'
    brokerClass = 'badge badge-warning'
  }

  return (
    <div className={`mode-banner${isAlarming ? ' mode-banner-alarm' : ''}`}>
      <span>{activeSession ? `Active session: ${modeLabel}` : 'No active session'}</span>
      <span className={brokerClass}>{brokerText}</span>
    </div>
  )
}
