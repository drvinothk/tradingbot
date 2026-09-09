import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/client'

interface BootStatusResponse {
  boot_id: string
}

const POLL_INTERVAL_MS = 2000
const POLL_TIMEOUT_MS = 60_000

/**
 * Detects a backend restart completing: snapshots the current
 * `/system-settings/boot-status` `boot_id`, then polls until it changes (a
 * new process is up) or the timeout is hit. Shared by every "…then the
 * backend restarts" flow (Advanced's Restart card, the Control Room Kill
 * Switch, the Market Terminal "Reconnect" / "Manual reconnect" buttons).
 *
 * Connection-refused / proxy errors mid-restart are expected and keep the
 * poll alive; only the overall timeout gives up. A token bumped on every
 * fresh call (and on unmount) stops a stale poll loop from racing a newer
 * one or updating state after the component is gone.
 *
 * `waitForRestart(msg, { expectRestart, timeoutMs })`: pass
 * `expectRestart: false` when a restart is only *possible* (the two Market
 * Terminal reconnect flows) so a timeout clears quietly rather than showing
 * an error; `timeoutMs` widens the window for a flow that may take a while
 * (a manual OAuth completed in another tab).
 */
export function useWaitForRestart() {
  const [isWaiting, setIsWaiting] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const tokenRef = useRef(0)

  useEffect(() => {
    return () => {
      tokenRef.current += 1
    }
  }, [])

  const waitForRestart = useCallback(
    async (
      waitingMessage = 'Waiting for the backend to come back up…',
      opts: { expectRestart?: boolean; timeoutMs?: number } = {},
    ) => {
      // `expectRestart: false` — the caller isn't certain a restart will
      // actually happen (the auto "Reconnect" engine only restarts if it did a
      // fresh login; a "Manual reconnect" restart depends on the user finishing
      // OAuth in another tab). On timeout we then clear silently instead of
      // emitting the "check the server logs" error, since "no restart" is a
      // legitimate outcome here. A real `boot_id` change still resolves normally.
      const { expectRestart = true, timeoutMs = POLL_TIMEOUT_MS } = opts

      let previousBootId: string | null = null
      try {
        previousBootId = (await api.get<BootStatusResponse>('/system-settings/boot-status')).boot_id
      } catch {
        previousBootId = null
      }

      tokenRef.current += 1
      const token = tokenRef.current
      setIsWaiting(true)
      setMessage(waitingMessage)
      const deadline = Date.now() + timeoutMs

      const finish = (text: string | null) => {
        if (tokenRef.current !== token) return
        setIsWaiting(false)
        setMessage(text)
      }

      const tick = () => {
        if (tokenRef.current !== token) return
        api
          .get<BootStatusResponse>('/system-settings/boot-status')
          .then((result) => {
            if (tokenRef.current !== token) return
            if (previousBootId === null || result.boot_id !== previousBootId) {
              finish('The backend is back up.')
              return
            }
            scheduleNext()
          })
          .catch(() => scheduleNext())
      }

      const scheduleNext = () => {
        if (tokenRef.current !== token) return
        if (Date.now() >= deadline) {
          finish(
            expectRestart
              ? `The backend hasn't confirmed coming back up after ${Math.round(
                  timeoutMs / 1000,
                )}s — check the server logs.`
              : null,
          )
          return
        }
        window.setTimeout(tick, POLL_INTERVAL_MS)
      }

      window.setTimeout(tick, POLL_INTERVAL_MS)
    },
    [],
  )

  return { isWaiting, message, waitForRestart }
}
