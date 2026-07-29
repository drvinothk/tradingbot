import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../../shared/api/client'
import type { RunningStrategyOut } from '../../shared/api/types'
import { useState } from 'react'

const POLL_INTERVAL_MS = 4000

export function RunningStrategiesPage() {
  const queryClient = useQueryClient()
  const [actionError, setActionError] = useState<string | null>(null)

  const { data, isLoading, error } = useQuery({
    queryKey: ['strategies', 'running'],
    queryFn: () => api.get<RunningStrategyOut[]>('/strategies/running'),
    refetchInterval: POLL_INTERVAL_MS,
  })

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['strategies', 'running'] })

  const stopMutation = useMutation({
    mutationFn: (strategyConfigId: string) => api.post(`/strategies/${strategyConfigId}/stop`),
    onSuccess: invalidate,
    onError: (err) => setActionError(err instanceof ApiError ? err.message : 'Stop failed'),
  })

  const approveMutation = useMutation({
    mutationFn: (approvalId: string) => api.post(`/trade-approvals/${approvalId}/approve`),
    onSuccess: invalidate,
    onError: (err) => setActionError(err instanceof ApiError ? err.message : 'Approve failed'),
  })

  const rejectMutation = useMutation({
    mutationFn: (approvalId: string) => api.post(`/trade-approvals/${approvalId}/reject`),
    onSuccess: invalidate,
    onError: (err) => setActionError(err instanceof ApiError ? err.message : 'Reject failed'),
  })

  if (isLoading) return <p>Loading...</p>
  if (error) return <p className="error">{(error as Error).message}</p>

  const runs = data ?? []

  return (
    <div>
      <div className="page-header">
        <h2>Running Strategies</h2>
      </div>
      {actionError && <p className="error">{actionError}</p>}
      {runs.length === 0 ? (
        <p>No strategies currently running.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Strategy</th>
              <th>Type</th>
              <th>Mode</th>
              <th>Status</th>
              <th>Open Position</th>
              <th>Pending Approvals</th>
              <th>Started</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.strategy_run_id}>
                <td>{run.strategy_name}</td>
                <td>{run.strategy_type}</td>
                <td>
                  <span className="badge">{run.execution_mode}</span>
                </td>
                <td>{run.status}</td>
                <td>
                  {run.open_position
                    ? `${run.open_position.side} x${run.open_position.qty} @ ${run.open_position.entry_price}`
                    : '-'}
                </td>
                <td>
                  {run.pending_approvals.length === 0 ? (
                    0
                  ) : (
                    <div className="row-actions" style={{ flexDirection: 'column', alignItems: 'flex-start' }}>
                      {run.pending_approvals.map((approval) => (
                        <div key={approval.approval_id} className="row-actions">
                          <span>
                            {approval.side} x{approval.qty_lots} @ {approval.entry_price}
                          </span>
                          <button
                            disabled={approveMutation.isPending}
                            onClick={() => approveMutation.mutate(approval.approval_id)}
                          >
                            Approve
                          </button>
                          <button
                            disabled={rejectMutation.isPending}
                            className="danger"
                            onClick={() => rejectMutation.mutate(approval.approval_id)}
                          >
                            Reject
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                </td>
                <td>{new Date(run.started_at).toLocaleTimeString()}</td>
                <td>
                  <button
                    className="danger"
                    disabled={stopMutation.isPending}
                    onClick={() => stopMutation.mutate(run.strategy_config_id)}
                  >
                    Stop
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
