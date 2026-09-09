import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
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
  const { isWaiting: isRestarting, message: restartMessage, waitForRestart } = useWaitForRestart()
  // Set when the user clicks "Manual reconnect": once the status query flips
  // to connected we know the browser OAuth completed, and the backend has a
  // restart queued (it always does after a manual login) -- start polling.
  const awaitingManualConnect = useRef(false)

  const statusQuery = useQuery({
    queryKey: [queryKeyPrefix, 'status'],
    queryFn: () => shoonyaApi.get<ShoonyaStatusOut>(statusPath),
    refetchOnWindowFocus: true,
  })

  const connected = statusQuery.data?.connected ?? false

  useEffect(() => {
    if (connected && awaitingManualConnect.current) {
      awaitingManualConnect.current = false
      void waitForRestart(`${brokerLabel} reconnected — finalising with a backend restart…`)
    }
  }, [connected, brokerLabel, waitForRestart])

  const manualReconnectMutation = useMutation({
    mutationFn: () => shoonyaApi.get<ShoonyaLoginUrlOut>(loginUrlPath),
    onSuccess: (data) => {
      awaitingManualConnect.current = true
      window.open(data.authorize_url, '_blank', 'noopener,noreferrer')
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
      // The engine restarts the backend only if it did a fresh login; poll
      // either way -- boot-status just won't change if it didn't.
      void waitForRestart('Auto-reconnect running (both brokers) — checking for a restart…')
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
        title="Re-run the headless auto-login for both brokers (keeps the current tokens if still valid)"
        onClick={() => autoReconnectMutation.mutate()}
      >
        Reconnect
      </button>
      <button
        className="btn-ghost"
        disabled={busy}
        title="Browser login, then restart the backend for a guaranteed-clean state"
        onClick={() => manualReconnectMutation.mutate()}
      >
        {connected ? 'Manual reconnect' : 'Connect'}
      </button>
      {restartMessage && <span className="muted">{restartMessage}</span>}
      {error && <span className="error">{error}</span>}
    </div>
  )
}
