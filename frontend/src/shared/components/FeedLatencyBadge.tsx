// Extracted from ControlRoomPage.tsx so both the global ModeBanner ribbon
// and Control Room itself can show the same feed-age badge without
// duplicating the formatting/color logic.

const FEED_STATE_BADGE_CLASS: Record<'live' | 'degraded' | 'stale' | 'dead', string> = {
  live: 'badge-success',
  degraded: 'badge-warning',
  // Stale and dead both mean "don't trust this" -- same treatment as the
  // rejected-trade badge elsewhere in the app (STATUS_BADGE_CLASS.rejected
  // reuses badge-live the same way, for the same "something's wrong" red).
  stale: 'badge-live',
  dead: 'badge-live',
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
      Feed:{' '}
      {feedAgeSeconds !== null && feedState !== null ? (
        <span className={`badge ${FEED_STATE_BADGE_CLASS[feedState]}`}>{formatFeedAge(feedAgeSeconds)}</span>
      ) : (
        <span className="badge">no data</span>
      )}
    </span>
  )
}
