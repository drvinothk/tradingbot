import { CandlestickSeries, createChart, type IChartApi, type ISeriesApi, type UTCTimestamp } from 'lightweight-charts'
import { useEffect, useMemo, useRef, useState } from 'react'
import type { CandleOut, UnderlyingSymbol } from '../../shared/api/types'
import { useCandles } from '../../shared/hooks/useCandles'
import { useInstruments } from '../../shared/hooks/useInstruments'

const INTERVALS: { label: string; minutes: number }[] = [
  { label: '1m', minutes: 1 },
  { label: '5m', minutes: 5 },
  { label: '15m', minutes: 15 },
]

const CHART_HEIGHT = 460

// Only "60s" bars are ever persisted (backend BAR_TIMEFRAME) -- anything
// coarser is a plain client-side groupby of the raw 1-min series, aligned
// to wall-clock interval boundaries (not just "every N rows"). Assumes
// `bars` is already chronological, which /market-data/candles guarantees.
function resample(bars: CandleOut[], intervalMinutes: number): CandleOut[] {
  if (intervalMinutes <= 1 || bars.length === 0) return bars
  const bucketSeconds = intervalMinutes * 60
  const order: number[] = []
  const buckets = new Map<number, CandleOut>()
  for (const bar of bars) {
    const epochSeconds = Math.floor(new Date(bar.bucket_start).getTime() / 1000)
    const bucketStart = Math.floor(epochSeconds / bucketSeconds) * bucketSeconds
    const existing = buckets.get(bucketStart)
    if (!existing) {
      buckets.set(bucketStart, { ...bar, bucket_start: new Date(bucketStart * 1000).toISOString() })
      order.push(bucketStart)
    } else {
      existing.high = Math.max(existing.high, bar.high)
      existing.low = Math.min(existing.low, bar.low)
      existing.close = bar.close
      existing.volume += bar.volume
    }
  }
  return order.map((key) => buckets.get(key)!)
}

// TradingView's own open-source lightweight-charts (MIT), fed by this
// system's real `price_bars` via polling -- not the third-party embed this
// replaced. See MarketTerminalPage's plan notes: `price_bars` only
// accumulates while a strategy is actively scanning this underlying, so an
// empty result here is an expected state, not an error.
export function PriceChart({ underlying }: { underlying: UnderlyingSymbol }) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const [intervalMinutes, setIntervalMinutes] = useState(1)

  const instrumentsQuery = useInstruments()
  const instrumentId = instrumentsQuery.data?.find((i) => i.symbol === underlying)?.id ?? null
  const candlesQuery = useCandles(instrumentId)

  const bars = useMemo(
    () => resample(candlesQuery.data ?? [], intervalMinutes),
    [candlesQuery.data, intervalMinutes],
  )

  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const chart = createChart(container, {
      layout: { background: { color: 'transparent' }, textColor: '#94a3b8' },
      grid: {
        vertLines: { color: 'rgba(139, 92, 246, 0.12)' },
        horzLines: { color: 'rgba(139, 92, 246, 0.12)' },
      },
      rightPriceScale: { borderColor: 'rgba(139, 92, 246, 0.4)' },
      timeScale: {
        borderColor: 'rgba(139, 92, 246, 0.4)',
        timeVisible: true,
        secondsVisible: false,
      },
      width: container.clientWidth,
      height: CHART_HEIGHT,
    })
    const series = chart.addSeries(CandlestickSeries, {
      upColor: '#10b981',
      downColor: '#e06c75',
      borderVisible: false,
      wickUpColor: '#10b981',
      wickDownColor: '#e06c75',
    })
    chartRef.current = chart
    seriesRef.current = series

    const resizeObserver = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width
      if (width) chart.applyOptions({ width })
    })
    resizeObserver.observe(container)

    return () => {
      resizeObserver.disconnect()
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
    }
    // Chart instance is created once per mount -- data updates flow through
    // the separate effect below via the series ref, not by recreating this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    const series = seriesRef.current
    if (!series) return
    series.setData(
      bars.map((bar) => ({
        time: Math.floor(new Date(bar.bucket_start).getTime() / 1000) as UTCTimestamp,
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
      })),
    )
    chartRef.current?.timeScale().fitContent()
  }, [bars])

  const showEmptyState = !candlesQuery.isLoading && bars.length === 0

  return (
    <div>
      <div className="row-actions" style={{ marginBottom: '0.5rem' }}>
        {INTERVALS.map((opt) => (
          <button
            key={opt.label}
            className="btn-ghost"
            style={
              opt.minutes === intervalMinutes
                ? { borderColor: 'var(--accent)', color: 'var(--text-main)' }
                : undefined
            }
            onClick={() => setIntervalMinutes(opt.minutes)}
          >
            {opt.label}
          </button>
        ))}
      </div>
      <div style={{ position: 'relative' }}>
        <div ref={containerRef} style={{ width: '100%', height: `${CHART_HEIGHT}px` }} />
        {showEmptyState && (
          <div
            style={{
              position: 'absolute',
              inset: 0,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              textAlign: 'center',
              padding: '1rem',
            }}
          >
            <p className="muted">
              {underlying} isn't streaming yet — start a strategy on this underlying to begin
              ingestion, or wait for the next candle.
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
