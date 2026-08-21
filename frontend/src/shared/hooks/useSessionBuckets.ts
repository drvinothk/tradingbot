import { useSessions } from './useSessions'
import { useBrokerAccounts } from './useBrokerAccounts'
import type { SessionOut } from '../api/types'

export interface SessionBuckets {
  isLoading: boolean
  /** The active session backed by a real (non-mock) broker account, if any. */
  liveSession: SessionOut | null
  /** The active session backed by the mock broker account, if any. */
  paperSession: SessionOut | null
  allSessions: SessionOut[]
}

// The finalized UI plan's "only 2 fixed sessions ever exist per login --
// Live and Paper" simplification isn't a literal backend concept (each
// TradingSession row is still created explicitly against one specific
// BrokerAccount, see api.v1.sessions.create_session) -- so this bucket-izes
// the real session list by which broker account backs each one:
// broker_type == 'mock' is the Paper bucket, anything else (shoonya today)
// is the Live bucket. Picks the most-recently-started ACTIVE session in
// each bucket, matching useActiveSessionMode's own "first active session"
// precedent.
export function useSessionBuckets(): SessionBuckets {
  const sessionsQuery = useSessions()
  const brokerAccountsQuery = useBrokerAccounts()

  const sessions = sessionsQuery.data ?? []
  const brokerAccounts = brokerAccountsQuery.data ?? []

  const brokerTypeById = new Map(brokerAccounts.map((b) => [b.id, b.broker_type]))
  const active = sessions.filter((s) => s.status === 'active')

  const liveSession =
    active.find((s) => (brokerTypeById.get(s.broker_account_id) ?? 'mock') !== 'mock') ?? null
  const paperSession =
    active.find((s) => (brokerTypeById.get(s.broker_account_id) ?? 'mock') === 'mock') ?? null

  return {
    isLoading: sessionsQuery.isLoading || brokerAccountsQuery.isLoading,
    liveSession,
    paperSession,
    allSessions: sessions,
  }
}
