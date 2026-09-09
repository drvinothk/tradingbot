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

  const waitForRestart = useCallback(async (waitingMessage = 'Waiting for the backend to come back up…') => {
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
    const deadline = Date.now() + POLL_TIMEOUT_MS

    const finish = (text: string) => {
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
        finish("The backend hasn't confirmed coming back up after 60s — check the server logs.")
        return
      }
      window.setTimeout(tick, POLL_INTERVAL_MS)
    }

    window.setTimeout(tick, POLL_INTERVAL_MS)
  }, [])

  return { isWaiting, message, waitForRestart }
}
