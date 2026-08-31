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
  occurrences: SystemAlertOut[]
}

export function groupAlertsIntoIncidents(alerts: SystemAlertOut[]): AlertIncident[] {
  const byKey = new Map<string, AlertIncident>()
  for (const alert of alerts) {
    const key = `${alert.category}::${normalizeMessageForGrouping(alert.message)}`
    const existing = byKey.get(key)
    if (!existing) {
      byKey.set(key, {
        key,
        severity: alert.severity,
        category: alert.category,
        message: alert.message,
        count: 1,
        lastSeen: alert.created_at,
        occurrences: [alert],
      })
      continue
    }
    existing.count += 1
    existing.occurrences.push(alert)
    if (new Date(alert.created_at) > new Date(existing.lastSeen)) {
      existing.lastSeen = alert.created_at
      existing.message = alert.message
    }
    if (severityRank(alert.severity) > severityRank(existing.severity)) {
      existing.severity = alert.severity
    }
  }
  return Array.from(byKey.values()).sort(
    (a, b) => new Date(b.lastSeen).getTime() - new Date(a.lastSeen).getTime(),
  )
}
