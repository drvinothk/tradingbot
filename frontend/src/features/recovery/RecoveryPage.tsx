import { useState } from 'react'
import { useSessions } from '../../shared/hooks/useSessions'
import { useReconciliationRuns, useSystemAlerts } from '../../shared/hooks/useRecovery'

export function RecoveryPage() {
  const alertsQuery = useSystemAlerts()
  const sessionsQuery = useSessions()
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null)
  const reconciliationQuery = useReconciliationRuns(selectedSessionId)

  const alerts = alertsQuery.data ?? []
  const sessions = sessionsQuery.data ?? []

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
    </div>
  )
}
