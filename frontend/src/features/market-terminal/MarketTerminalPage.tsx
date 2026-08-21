import { useState } from 'react'
import type { UnderlyingSymbol } from '../../shared/api/types'

const TRADINGVIEW_SYMBOL: Record<UnderlyingSymbol, string> = {
  NIFTY: 'NSE:NIFTY',
  BANKNIFTY: 'NSE:BANKNIFTY',
}

// Demo-only rows so the depth-bar/table styling described in the plan is
// visible even though the real option-chain endpoint doesn't exist yet —
// clearly labeled WIP, never presented as live data.
const DEMO_STRIKES = [
  { strike: 24200, callRatio: '2.1x Buyers', callSide: 'buy' as const, putRatio: '1.4x Sellers', putSide: 'sell' as const },
  { strike: 24250, callRatio: '3x Buyers', callSide: 'buy' as const, putRatio: '1.1x Buyers', putSide: 'buy' as const },
  { strike: 24300, callRatio: '1.2x Sellers', callSide: 'sell' as const, putRatio: '2.4x Sellers', putSide: 'sell' as const },
]

export function MarketTerminalPage() {
  const [underlying, setUnderlying] = useState<UnderlyingSymbol>('NIFTY')
  const [chartLabel, setChartLabel] = useState<string | null>(null)

  return (
    <div>
      <div className="page-header">
        <h2>Market Terminal</h2>
        <div className="row-actions">
          <label htmlFor="underlying-select" className="muted">
            Underlying
          </label>
          <select
            id="underlying-select"
            value={underlying}
            onChange={(e) => {
              setUnderlying(e.target.value as UnderlyingSymbol)
              setChartLabel(null)
            }}
          >
            <option value="NIFTY">Nifty</option>
            <option value="BANKNIFTY">Bank Nifty</option>
          </select>
        </div>
      </div>

      <div className="grid-2">
        <div className="card">
          <div className="card-header">
            <h3>
              {underlying === 'NIFTY' ? 'Nifty' : 'Bank Nifty'} Option Chain{' '}
              <span className="badge badge-wip">WIP</span>
            </h3>
          </div>
          <div className="wip-panel">
            <h4>Live option chain not wired up yet</h4>
            <p>
              No backend endpoint returns live option-chain/depth data today — this needs a real
              candle-history + option-chain read path before this table can show anything but a
              static demo. The rows below illustrate the intended depth-bar layout only.
            </p>
          </div>
          <table style={{ opacity: 0.55, marginTop: '0.75rem' }}>
            <thead>
              <tr>
                <th>Call Depth</th>
                <th>Call LTP</th>
                <th style={{ textAlign: 'center' }}>Strike</th>
                <th>Put LTP</th>
                <th>Put Depth</th>
              </tr>
            </thead>
            <tbody>
              {DEMO_STRIKES.map((row) => (
                <tr
                  key={row.strike}
                  style={{ cursor: 'pointer' }}
                  onClick={() => setChartLabel(`${row.strike} demo`)}
                >
                  <td>
                    <div className={`depth-bar depth-bar-${row.callSide}`}>{row.callRatio}</div>
                  </td>
                  <td>—</td>
                  <td style={{ textAlign: 'center', fontWeight: 700 }}>{row.strike}</td>
                  <td>—</td>
                  <td>
                    <div className={`depth-bar depth-bar-${row.putSide}`}>{row.putRatio}</div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="card">
          <div className="card-header">
            <h3>Chart{chartLabel ? `: ${chartLabel}` : ''}</h3>
          </div>
          {/* TradingView's own iframe embed needs no backend work at all —
              live and fully functional, per the plan's explicit note that
              this is the one Market Terminal piece buildable without the
              option-chain endpoint. */}
          <iframe
            title="TradingView chart"
            src={`https://s.tradingview.com/widgetembed/?frameElementId=tv-chart&symbol=${encodeURIComponent(
              TRADINGVIEW_SYMBOL[underlying],
            )}&interval=5&hidesidetoolbar=1&hidetoptoolbar=0&symboledit=1&saveimage=0&toolbarbg=110e1b&studies=%5B%5D&theme=dark&style=1&timezone=Asia%2FKolkata`}
            style={{ width: '100%', height: '520px', border: 'none', borderRadius: '6px' }}
            allowFullScreen
          />
        </div>
      </div>
    </div>
  )
}
