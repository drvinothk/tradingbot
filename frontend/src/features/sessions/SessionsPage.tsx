import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { api, ApiError, shoonyaApi } from '../../shared/api/client'
import { useSessions } from '../../shared/hooks/useSessions'
import type {
  BrokerAccountOut,
  FundingMode,
  SessionOut,
  ShoonyaLoginUrlOut,
  ShoonyaStatusOut,
} from '../../shared/api/types'

export function SessionsPage() {
  const queryClient = useQueryClient()
  const [formError, setFormError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const sessionsQuery = useSessions()

  const brokerAccountsQuery = useQuery({
    queryKey: ['broker-accounts'],
    queryFn: () => api.get<BrokerAccountOut[]>('/broker-accounts'),
  })

  const invalidateSessions = () => queryClient.invalidateQueries({ queryKey: ['sessions'] })

  const [brokerAccountId, setBrokerAccountId] = useState('')
  const [budgetAmount, setBudgetAmount] = useState('')
  const [dailyTargetProfit, setDailyTargetProfit] = useState('')
  const [dailyLossCap, setDailyLossCap] = useState('')
  const [fundingMode, setFundingMode] = useState<FundingMode>('cash')

  const createMutation = useMutation({
    mutationFn: () =>
      api.post<SessionOut>('/sessions', {
        broker_account_id: brokerAccountId,
        budget_amount: budgetAmount ? Number(budgetAmount) : undefined,
        daily_target_profit: dailyTargetProfit ? Number(dailyTargetProfit) : undefined,
        daily_loss_cap: dailyLossCap ? Number(dailyLossCap) : undefined,
        funding_mode: fundingMode,
      }),
    onSuccess: () => {
      invalidateSessions()
      setBudgetAmount('')
      setDailyTargetProfit('')
      setDailyLossCap('')
    },
    onError: (err) => setFormError(err instanceof ApiError ? err.message : 'Create failed'),
  })

  const killSwitchMutation = useMutation({
    mutationFn: (sessionId: string) =>
      api.post(`/sessions/${sessionId}/kill-switch`, { reason: 'manual kill switch from UI' }),
    onSuccess: invalidateSessions,
    onError: (err) => setActionError(err instanceof ApiError ? err.message : 'Kill switch failed'),
  })

  const squareOffMutation = useMutation({
    mutationFn: (sessionId: string) => api.post(`/sessions/${sessionId}/square-off`),
    onSuccess: invalidateSessions,
    onError: (err) => setActionError(err instanceof ApiError ? err.message : 'Square-off failed'),
  })

  const reconcileMutation = useMutation({
    mutationFn: (sessionId: string) => api.post(`/sessions/${sessionId}/reconcile`),
    onSuccess: invalidateSessions,
    onError: (err) => setActionError(err instanceof ApiError ? err.message : 'Reconcile failed'),
  })

  function handleCreate(event: FormEvent) {
    event.preventDefault()
    setFormError(null)
    if (!brokerAccountId) {
      setFormError('Pick a broker account')
      return
    }
    createMutation.mutate()
  }

  const brokerAccounts = brokerAccountsQuery.data ?? []
  const sessions = sessionsQuery.data ?? []

  return (
    <div>
      <div className="page-header">
        <h2>Sessions</h2>
      </div>

      <ShoonyaConnectionCard />

      <div className="card">
        <h3>Start a new session</h3>
        <form onSubmit={handleCreate}>
          <div className="form-row">
            <label htmlFor="broker-account">Broker account</label>
            <select
              id="broker-account"
              value={brokerAccountId}
              onChange={(e) => setBrokerAccountId(e.target.value)}
            >
              <option value="">Select...</option>
              {brokerAccounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.label} ({account.broker_type})
                </option>
              ))}
            </select>
          </div>
          <div className="form-row">
            <label htmlFor="budget">Budget amount (optional, uses default)</label>
            <input
              id="budget"
              type="number"
              value={budgetAmount}
              onChange={(e) => setBudgetAmount(e.target.value)}
            />
          </div>
          <div className="form-row">
            <label htmlFor="target">Daily target profit (optional)</label>
            <input
              id="target"
              type="number"
              value={dailyTargetProfit}
              onChange={(e) => setDailyTargetProfit(e.target.value)}
            />
          </div>
          <div className="form-row">
            <label htmlFor="loss-cap">Daily loss cap (optional)</label>
            <input
              id="loss-cap"
              type="number"
              value={dailyLossCap}
              onChange={(e) => setDailyLossCap(e.target.value)}
            />
          </div>
          <div className="form-row">
            <label htmlFor="funding-mode">Funding mode</label>
            <select
              id="funding-mode"
              value={fundingMode}
              onChange={(e) => setFundingMode(e.target.value as FundingMode)}
            >
              <option value="cash">Cash</option>
              <option value="mtf">MTF</option>
            </select>
          </div>
          {formError && <p className="error">{formError}</p>}
          <div className="form-actions">
            <button type="submit" disabled={createMutation.isPending}>
              Create session
            </button>
          </div>
        </form>
      </div>

      {actionError && <p className="error">{actionError}</p>}

      <table>
        <thead>
          <tr>
            <th>Mode</th>
            <th>Status</th>
            <th>Broker account</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {sessions.map((session) => (
            <tr key={session.id}>
              <td>
                <span className="badge">{session.mode}</span>
              </td>
              <td>{session.status}</td>
              <td>{session.broker_account_id}</td>
              <td>
                <div className="row-actions">
                  <button onClick={() => squareOffMutation.mutate(session.id)}>Square off</button>
                  <button onClick={() => reconcileMutation.mutate(session.id)}>Reconcile</button>
                  <button
                    className="danger"
                    onClick={() => killSwitchMutation.mutate(session.id)}
                  >
                    Kill switch
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function statusLabel(isLoading: boolean, connected: boolean): string {
  if (isLoading) return 'checking...'
  return connected ? 'connected' : 'not connected'
}

function ShoonyaConnectionCard() {
  const [error, setError] = useState<string | null>(null)

  const statusQuery = useQuery({
    queryKey: ['shoonya', 'status'],
    queryFn: () => shoonyaApi.get<ShoonyaStatusOut>('/shoonya/status'),
    // The OAuth login happens in a separate tab (Shoonya's own login page,
    // then their redirect back to /shoonya/callback) — refetching on focus
    // is what notices "connected" flipping true once the user returns here,
    // without needing a websocket for something this infrequent.
    refetchOnWindowFocus: true,
  })

  const connectMutation = useMutation({
    mutationFn: () => shoonyaApi.get<ShoonyaLoginUrlOut>('/shoonya/login-url'),
    onSuccess: (data) => {
      window.open(data.authorize_url, '_blank', 'noopener,noreferrer')
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : 'Could not start login'),
  })

  const connected = statusQuery.data?.connected ?? false

  return (
    <div className="card">
      <h3>Shoonya connection</h3>
      <p>
        Status: <span className="badge">{statusLabel(statusQuery.isLoading, connected)}</span>
      </p>
      {error && <p className="error">{error}</p>}
      <div className="form-actions">
        <button disabled={connectMutation.isPending} onClick={() => connectMutation.mutate()}>
          {connected ? 'Reconnect Shoonya' : 'Connect Shoonya'}
        </button>
      </div>
      <p style={{ fontSize: '0.85rem', opacity: 0.75 }}>
        Opens Shoonya's own login page in a new tab. After you log in there, come back to this
        tab — the status above updates automatically.
      </p>
    </div>
  )
}
