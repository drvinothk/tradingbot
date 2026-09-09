// Extracted from ControlRoomPage.tsx so both the global ModeBanner ribbon
// and Control Room itself can show the same feed-age badge without
// duplicating the formatting/color logic.

import { isWithinMarketHoursIST } from '../time/ist'

const FEED_STATE_BADGE_CLASS: Record<'live' | 'degraded' | 'stale' | 'dead', string> = {
  live: 'badge-success',
  degraded: 'badge-warning',
  // Stale and dead both mean "don't trust this" -- same treatment as the
  // rejected-trade badge elsewhere in the app (STATUS_BADGE_CLASS.rejected
  // reuses badge-live the same way, for the same "something's wrong" red).
  stale: 'badge-live',
  dead: 'badge-live',
}

// Outside market hours a stale/dead underlying feed is the expected resting
// state (NSE is closed), not a fault -- so drop the alarming red and render
// it as a neutral badge. During market hours the red stays: a dead feed then
// is a real problem. Mirrors the backend's own pre-09:15 alert-triage rule.
function feedBadgeClass(feedState: 'live' | 'degraded' | 'stale' | 'dead'): string {
  if ((feedState === 'stale' || feedState === 'dead') && !isWithinMarketHoursIST()) {
    return 'badge'
  }
  return FEED_STATE_BADGE_CLASS[feedState]
}

function formatFeedAge(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s ago`
  return `${Math.round(seconds / 60)}m ago`
}

export function FeedLatencyBadge({
  feedAgeSeconds,
  feedState,
}: {
  feedAgeSeconds: number | null
  feedState: 'live' | 'degraded' | 'stale' | 'dead' | null
}) {
  return (
    <span className="muted">
      WS Feed:{' '}
      {feedAgeSeconds !== null && feedState !== null ? (
        <span className={`badge ${feedBadgeClass(feedState)}`}>{formatFeedAge(feedAgeSeconds)}</span>
      ) : (
        <span className="badge">no data</span>
      )}
    </span>
  )
}
