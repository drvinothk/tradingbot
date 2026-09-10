import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { ApiError, shoonyaApi } from '../api/client'
import { api } from '../api/client'
import type { ShoonyaLoginUrlOut, ShoonyaStatusOut } from '../api/types'
import { useWaitForRestart } from '../hooks/useWaitForRestart'

interface BrokerConnectionRowProps {
  brokerLabel: string
  statusPath: string
  loginUrlPath: string
  // Shoonya's must stay exactly ['shoonya', 'status'] -- useActiveSessionMode
  // (header ModeBanner, Control Room) reads this same key so a Connect click
  // here refreshes those too, not just this row's own cache.
  queryKeyPrefix: string
}

export function BrokerConnectionRow({
  brokerLabel,
  statusPath,
  loginUrlPath,
  queryKeyPrefix,
}: BrokerConnectionRowProps) {
  const queryClient = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  // Set once an auto-reconnect is fired from this row, so the engine-log
  // viewer only appears where it was triggered (the log itself is global).
  const [autoTriggered, setAutoTriggered] = useState(false)
  const [logOpen, setLogOpen] = useState(false)
  const { isWaiting: isRestarting, message: restartMessage, waitForRestart } = useWaitForRestart()

  const statusQuery = useQuery({
    queryKey: [queryKeyPrefix, 'status'],
    queryFn: () => shoonyaApi.get<ShoonyaStatusOut>(statusPath),
    refetchOnWindowFocus: true,
  })

  const engineLogQuery = useQuery({
    queryKey: ['auto-reconnect-log'],
    queryFn: () =>
      api.get<{ exists: boolean; lines: string[]; note: string | null }>(
        '/system-settings/last-auto-reconnect-log',
      ),
    enabled: autoTriggered && logOpen,
    refetchInterval: autoTriggered && logOpen ? 5000 : false,
  })

  const connected = statusQuery.data?.connected ?? false

  // Only the Shoonya OAuth callback restarts the backend now (2026-09-10:
  // the Alice Blue one no longer does -- AB is market-data-only and re-wires
  // its live provider in place). So the message must not promise a restart
  // for AB, and neither message should claim the broker is "reconnected" --
  // this fires when the login *tab opens*, not when OAuth completes.
  const manualRestartsBackend = queryKeyPrefix === 'shoonya'

  const manualReconnectMutation = useMutation({
    mutationFn: () => shoonyaApi.get<ShoonyaLoginUrlOut>(loginUrlPath),
    onSuccess: (data) => {
      window.open(data.authorize_url, '_blank', 'noopener,noreferrer')
      // Poll from now with a wide window; clears quietly if the user never
      // completes login in the other tab (any later restart self-heals).
      void waitForRestart(
        manualRestartsBackend
          ? `${brokerLabel} login opened — the backend restarts once you finish it in the other tab…`
          : `${brokerLabel} login opened in a new tab — complete it there; this row updates on return.`,
        { expectRestart: false, timeoutMs: 180_000 },
      )
      queryClient.invalidateQueries({ queryKey: [queryKeyPrefix, 'status'] })
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : 'Could not start login'),
  })

  const autoReconnectMutation = useMutation({
    mutationFn: () =>
      api.post<{ triggered: boolean; message: string }>(
        '/system-settings/reconnect-brokers-auto',
      ),
    onSuccess: (data) => {
      setError(null)
      setAutoTriggered(true)
      // The engine restarts the backend only if it did a fresh login; poll
      // either way. `expectRestart: false` so "no boot_id change" clears
      // quietly instead of a spurious "check the server logs" after 2 min.
      void waitForRestart('Auto-reconnect running (both brokers) — checking for a restart…', {
        expectRestart: false,
        timeoutMs: 120_000,
      })
      if (!data.triggered) setError(data.message)
      queryClient.invalidateQueries({ queryKey: [queryKeyPrefix, 'status'] })
      queryClient.invalidateQueries({ queryKey: ['alice_blue', 'status'] })
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : 'Could not start auto-reconnect'),
  })

  const busy =
    manualReconnectMutation.isPending || autoReconnectMutation.isPending || isRestarting

  return (
    <div className="row-actions">
      <span className={`broker-status ${connected ? 'on' : 'off'}`}>
        <span className={`status-dot ${connected ? 'on' : 'off'}`} /> {brokerLabel}:{' '}
        {statusQuery.isLoading ? 'checking...' : connected ? 'connected' : 'not connected'}
      </span>
      <button
        className="btn-ghost"
        disabled={busy}
        title="Re-checks BOTH brokers' login tokens and headless-logs-in only a dead one. Does not fix a stalled market-data feed — for that, check Advanced → Market Data."
        onClick={() => autoReconnectMutation.mutate()}
      >
        Reconnect
      </button>
      <button
        className="btn-ghost"
        disabled={busy}
        title="Browser login for this broker, then (Shoonya only) restart the backend for a guaranteed-clean state"
        onClick={() => manualReconnectMutation.mutate()}
      >
        {connected ? 'Manual reconnect' : 'Connect'}
      </button>
      {restartMessage && <span className="muted">{restartMessage}</span>}
      {error && <span className="error">{error}</span>}
      {autoTriggered && (
        <details
          style={{ flexBasis: '100%' }}
          onToggle={(e) => setLogOpen((e.currentTarget as HTMLDetailsElement).open)}
        >
          <summary className="muted" style={{ cursor: 'pointer', fontSize: '0.8rem' }}>
            Auto-reconnect engine log
          </summary>
          <pre
            style={{
              fontSize: '0.72rem',
              maxHeight: '12rem',
              overflow: 'auto',
              margin: '0.3rem 0 0',
              whiteSpace: 'pre-wrap',
            }}
          >
            {engineLogQuery.data?.note ||
              (engineLogQuery.data?.lines ?? []).join('\n') ||
              'loading…'}
          </pre>
        </details>
      )}
    </div>
  )
}
