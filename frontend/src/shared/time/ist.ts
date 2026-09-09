// IST (Asia/Kolkata) time helpers for the UI.
//
// India observes no DST, so Asia/Kolkata is a fixed UTC+05:30 -- but weekday
// and clock extraction still go through Intl so they're correct no matter
// what timezone the viewer's own machine is set to.

// Seconds to add to a real UTC epoch so a library that only renders in UTC
// (lightweight-charts -- no timezone option) displays IST wall-clock instead.
export const IST_OFFSET_SECONDS = 5.5 * 3600

interface ISTParts {
  // 1 = Mon ... 7 = Sun, matching the backend's now_ist().isoweekday().
  weekday: number
  hour: number
  minute: number
}

const WEEKDAY_INDEX: Record<string, number> = {
  Mon: 1,
  Tue: 2,
  Wed: 3,
  Thu: 4,
  Fri: 5,
  Sat: 6,
  Sun: 7,
}

function istParts(d: Date): ISTParts {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-US', {
      timeZone: 'Asia/Kolkata',
      weekday: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
      .formatToParts(d)
      .map((p) => [p.type, p.value]),
  )
  return {
    weekday: WEEKDAY_INDEX[parts.weekday as string] ?? 0,
    // Some engines emit '24' for midnight -- fold back to 0.
    hour: Number(parts.hour) % 24,
    minute: Number(parts.minute),
  }
}

// Mon-Fri, ~09:10-15:35 IST -- a few minutes wider than the real 09:15-15:30
// session on each side so the pre-open feed warm-up and the post-close flush
// aren't flagged as "something's wrong". Mirrors the backend's own
// "pre-09:15 data-missing = expected" alert-triage rule (market_data.market_hours).
export function isWithinMarketHoursIST(d: Date = new Date()): boolean {
  const { weekday, hour, minute } = istParts(d)
  if (weekday < 1 || weekday > 5) return false
  const minutesSinceMidnight = hour * 60 + minute
  return minutesSinceMidnight >= 9 * 60 + 10 && minutesSinceMidnight <= 15 * 60 + 35
}
