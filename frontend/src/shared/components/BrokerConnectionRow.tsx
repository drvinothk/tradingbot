import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { ApiError, shoonyaApi } from '../api/client'
import type { ShoonyaLoginUrlOut, ShoonyaStatusOut } from '../api/types'

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

  const statusQuery = useQuery({
    queryKey: [queryKeyPrefix, 'status'],
    queryFn: () => shoonyaApi.get<ShoonyaStatusOut>(statusPath),
    refetchOnWindowFocus: true,
  })

  const connectMutation = useMutation({
    mutationFn: () => shoonyaApi.get<ShoonyaLoginUrlOut>(loginUrlPath),
    onSuccess: (data) => {
      window.open(data.authorize_url, '_blank', 'noopener,noreferrer')
      // The actual "connected" flip happens on the broker's own OAuth
      // callback (a separate tab/redirect), not this response -- refetch
      // on window focus (already set above) picks it up once the user
      // comes back, but invalidate now too so this row and the header
      // banner reconcile together rather than drifting.
      queryClient.invalidateQueries({ queryKey: [queryKeyPrefix, 'status'] })
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : 'Could not start login'),
  })

  const connected = statusQuery.data?.connected ?? false

  return (
    <div className="row-actions">
      <span className={`broker-status ${connected ? 'on' : 'off'}`}>
        <span className={`status-dot ${connected ? 'on' : 'off'}`} /> {brokerLabel}:{' '}
        {statusQuery.isLoading ? 'checking...' : connected ? 'connected' : 'not connected'}
      </span>
      <button
        className="btn-ghost"
        disabled={connectMutation.isPending}
        onClick={() => connectMutation.mutate()}
      >
        {connected ? 'Reconnect' : 'Connect'}
      </button>
      {error && <span className="error">{error}</span>}
    </div>
  )
}
