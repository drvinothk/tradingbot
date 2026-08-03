import { useActiveSessionMode } from '../shared/hooks/useActiveSessionMode'

const MODE_LABELS: Record<string, string> = {
  paper_only: 'Paper only',
  paper_plus_guarded_live: 'Paper + guarded live',
  live_enabled: 'Live enabled',
  degraded_mode: 'Degraded mode',
  reconciliation_lock: 'Reconciliation lock',
  kill_switch: 'Kill switch',
}

export function ModeBanner() {
  const { isLoading, activeSession, shoonyaConnected } = useActiveSessionMode()

  if (isLoading) {
    return null
  }

  const modeLabel = activeSession ? (MODE_LABELS[activeSession.mode] ?? activeSession.mode) : null
  const isAlarming =
    activeSession != null &&
    ['degraded_mode', 'reconciliation_lock', 'kill_switch'].includes(activeSession.mode)

  return (
    <div className={`mode-banner${isAlarming ? ' mode-banner-alarm' : ''}`}>
      <span>{activeSession ? `Active session: ${modeLabel}` : 'No active session'}</span>
      <span className={`badge${shoonyaConnected ? ' badge-live' : ''}`}>
        Broker: {shoonyaConnected ? 'Shoonya (REAL)' : 'Mock'}
      </span>
    </div>
  )
}
