import type { SystemAlertOut } from '../api/types'

// Groups repeated occurrences of the same underlying issue (e.g. a
// recurring "market data stale for NIFTY" warning firing every few
// minutes) into one incident row instead of one row per raw alert --
// `/system-alerts` already caps at 100 rows (see its own `limit` default),
// and a single noisy category can easily fill that cap on its own,
// crowding out genuinely distinct issues. Grouping key is
// category + message with numbers stripped, so "...(42s)" and "...(61s)"
// collapse together but two different messages/categories don't. Purely
// client-side -- no backend change, the underlying SystemAlert rows are
// untouched.
//
// Extracted 2026-08-31 from AdvancedPage.tsx's "System errors" card (its
// original home) so ControlRoomPage.tsx's "Attention Required" panel can
// reuse the same, already-proven grouping key instead of a second, weaker
// one -- keep both consumers in sync with this file, not each other.
//
// 2026-09-03: the backend now collapses a recurring alert's raw rows into
// one row with its own occurrence_count/last_seen_at (see
// alerting.manager.send_alert) -- so a single SystemAlertOut here can
// already represent many real occurrences. `count` sums occurrence_count
// (not row count -- summing `+= 1` per raw row would *undercount* once
// collapsing is live, the opposite of the old overcounting problem this
// file was built to fix), and `lastSeen` tracks last_seen_at (not
// created_at, which freezes at a collapsed row's first occurrence --
// ControlRoomPage.tsx's 30-minute staleness filter reads `lastSeen`, and a
// still-actively-recurring issue must not read as stale just because its
// row is old). Both fall back to `?? 1`/`?? created_at` for any row from
// before the backend change shipped.
export function normalizeMessageForGrouping(message: string): string {
  return message
    .replace(/\d+(\.\d+)?/g, '#')
    .replace(/\s+/g, ' ')
    .trim()
}

export function severityRank(severity: string): number {
  return severity === 'critical' ? 2 : severity === 'warning' ? 1 : 0
}

export interface AlertIncident {
  key: string
  severity: string
  category: string
  message: string
  count: number
  lastSeen: string
  firstSeen: string
  occurrences: SystemAlertOut[]
}

// 2026-09-03: `firstSeen` tracks the earliest `created_at` across every raw
// occurrence merged into this incident -- mirrors `lastSeen`'s own tracking
// exactly, just min instead of max. Added for ControlRoomPage.tsx's
// self-healing grace window (an incident must have been open for a few
// seconds before it's worth surfacing as "needs your attention" -- see that
// file's own SELF_HEALING_GRACE_CATEGORIES) -- purely additive, every other
// consumer of AlertIncident is unaffected.
export function groupAlertsIntoIncidents(alerts: SystemAlertOut[]): AlertIncident[] {
  const byKey = new Map<string, AlertIncident>()
  for (const alert of alerts) {
    const key = `${alert.category}::${normalizeMessageForGrouping(alert.message)}`
    const rowCount = alert.occurrence_count ?? 1
    const rowLastSeen = alert.last_seen_at ?? alert.created_at
    const existing = byKey.get(key)
    if (!existing) {
      byKey.set(key, {
        key,
        severity: alert.severity,
        category: alert.category,
        message: alert.message,
        count: rowCount,
        lastSeen: rowLastSeen,
        firstSeen: alert.created_at,
        occurrences: [alert],
      })
      continue
    }
    existing.count += rowCount
    existing.occurrences.push(alert)
    if (new Date(rowLastSeen) > new Date(existing.lastSeen)) {
      existing.lastSeen = rowLastSeen
      existing.message = alert.message
    }
    if (new Date(alert.created_at) < new Date(existing.firstSeen)) {
      existing.firstSeen = alert.created_at
    }
    if (severityRank(alert.severity) > severityRank(existing.severity)) {
      existing.severity = alert.severity
    }
  }
  return Array.from(byKey.values()).sort(
    (a, b) => new Date(b.lastSeen).getTime() - new Date(a.lastSeen).getTime(),
  )
}
