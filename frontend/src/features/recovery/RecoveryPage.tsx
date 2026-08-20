import { useMutation } from '@tanstack/react-query'
import { useState } from 'react'
import { api, ApiError } from '../../shared/api/client'
import { useSessions } from '../../shared/hooks/useSessions'
import { useReconciliationRuns, useSystemAlerts } from '../../shared/hooks/useRecovery'

interface OpenLivePosition {
  trading_session_id: string
  contract_symbol: string
  qty: number
}

interface RestartBackendResponse {
  ok: boolean
  message: string
}

interface RestartBlockedDetail {
  message: string
  open_live_positions: OpenLivePosition[]
}

export function RecoveryPage() {
  const alertsQuery = useSystemAlerts()
  const sessionsQuery = useSessions()
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null)
  const reconciliationQuery = useReconciliationRuns(selectedSessionId)

  const alerts = alertsQuery.data ?? []
  const sessions = sessionsQuery.data ?? []

  const [restartReason, setRestartReason] = useState('')
  const [restartStatus, setRestartStatus] = useState<string | null>(null)
  const [blockedPositions, setBlockedPositions] = useState<OpenLivePosition[] | null>(null)

  // force=false first, always -- the backend's own open-live-position guard
  // is the real safety net; a second, explicitly-labeled click is required
  // to override it once positions are actually shown, see below.
  const restartMutation = useMutation({
    mutationFn: (force: boolean) =>
      api.post<RestartBackendResponse>('/system-settings/restart-backend', {
        reason: restartReason,
        force,
      }),
    onSuccess: (result) => {
      setBlockedPositions(null)
      setRestartStatus(result.message)
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        const detail = (err.body as { detail?: RestartBlockedDetail } | null)?.detail
        setRestartStatus(detail?.message ?? 'Blocked by open live positions.')
        setBlockedPositions(detail?.open_live_positions ?? [])
      } else {
        setBlockedPositions(null)
        setRestartStatus(err instanceof ApiError ? err.message : 'Restart failed.')
      }
    },
  })

  function handleRestartClick() {
    if (!restartReason.trim()) {
      setRestartStatus('A reason is required.')
      return
    }
    if (
      !window.confirm(
        'This restarts the entire backend process. Every open position’s stop/target/' +
          'trail monitoring pauses for a few seconds while it comes back up. Continue?',
      )
    ) {
      return
    }
    setBlockedPositions(null)
    restartMutation.mutate(false)
  }

  return (
    <div>
      <div className="page-header">
        <h2>Recovery</h2>
      </div>

      <div className="card">
        <h3>Unresolved system alerts</h3>
        {alertsQuery.isLoading ? (
          <p>Loading...</p>
        ) : alerts.length === 0 ? (
          <p>None — nothing currently needs attention.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Severity</th>
                <th>Category</th>
                <th>Message</th>
                <th>When</th>
              </tr>
            </thead>
            <tbody>
              {alerts.map((alert) => (
                <tr key={alert.id}>
                  <td>
                    <span className={alert.severity === 'critical' ? 'badge badge-live' : 'badge'}>
                      {alert.severity}
                    </span>
                  </td>
                  <td>{alert.category}</td>
                  <td>{alert.message}</td>
                  <td>{new Date(alert.created_at).toLocaleTimeString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="card">
        <h3>Reconciliation history</h3>
        <div className="form-row">
          <label htmlFor="recovery-session">Session</label>
          <select
            id="recovery-session"
            value={selectedSessionId ?? ''}
            onChange={(e) => setSelectedSessionId(e.target.value || null)}
          >
            <option value="">Select a session...</option>
            {sessions.map((session) => (
              <option key={session.id} value={session.id}>
                {session.id} ({session.mode})
              </option>
            ))}
          </select>
        </div>

        {selectedSessionId === null ? (
          <p>Pick a session to see its reconciliation history.</p>
        ) : reconciliationQuery.isLoading ? (
          <p>Loading...</p>
        ) : (
          <>
            <h4>Current mismatches</h4>
            {(reconciliationQuery.data?.current_mismatches.length ?? 0) === 0 ? (
              <p>None — local and broker positions agree.</p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>Option contract</th>
                    <th>Local qty</th>
                    <th>Broker qty</th>
                    <th>Checked</th>
                  </tr>
                </thead>
                <tbody>
                  {reconciliationQuery.data?.current_mismatches.map((mismatch) => (
                    <tr key={mismatch.option_contract_id}>
                      <td>{mismatch.option_contract_id}</td>
                      <td>{mismatch.local_qty}</td>
                      <td>{mismatch.broker_qty}</td>
                      <td>{new Date(mismatch.checked_at).toLocaleTimeString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            <h4>Recent runs</h4>
            {(reconciliationQuery.data?.runs.length ?? 0) === 0 ? (
              <p>No reconciliation runs recorded yet for this session.</p>
            ) : (
              <table>
                <thead>
                  <tr>
                    <th>Trigger</th>
                    <th>Mismatches found</th>
                    <th>Action taken</th>
                    <th>Finished</th>
                  </tr>
                </thead>
                <tbody>
                  {reconciliationQuery.data?.runs.map((run) => (
                    <tr key={run.id}>
                      <td>{run.trigger_type}</td>
                      <td>{run.mismatches_found}</td>
                      <td>{run.action_taken}</td>
                      <td>{new Date(run.finished_at).toLocaleTimeString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </div>

      <div className="card">
        <h3>Restart backend</h3>
        <p>
          Last resort if a recovery action above doesn't fix a stuck session — restarts the
          entire backend process. Every open position's stop/target/trail monitoring pauses
          for a few seconds while it comes back up.
        </p>
        <div className="form-row">
          <label htmlFor="restart-reason">Reason</label>
          <textarea
            id="restart-reason"
            value={restartReason}
            onChange={(e) => setRestartReason(e.target.value)}
            rows={2}
          />
        </div>
        <button
          className="danger"
          disabled={restartMutation.isPending}
          onClick={handleRestartClick}
        >
          Restart backend
        </button>
        {restartStatus && <p>{restartStatus}</p>}
        {blockedPositions && blockedPositions.length > 0 && (
          <>
            <table>
              <thead>
                <tr>
                  <th>Session</th>
                  <th>Contract</th>
                  <th>Qty</th>
                </tr>
              </thead>
              <tbody>
                {blockedPositions.map((position, i) => (
                  <tr key={i}>
                    <td>{position.trading_session_id}</td>
                    <td>{position.contract_symbol}</td>
                    <td>{position.qty}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <button
              className="danger"
              disabled={restartMutation.isPending}
              onClick={() => restartMutation.mutate(true)}
            >
              Restart anyway
            </button>
          </>
        )}
      </div>
    </div>
  )
}
