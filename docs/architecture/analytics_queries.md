# Analytics queries (future)

Not implemented — a list of the key questions the schema should already be
able to answer once there's enough trade history, given every `Signal`'s
`payload` carries `strategy` and (once wired) `env` (VIX/PCR, stubbed `None`
until that pipeline exists) alongside each strategy's own context fields
(`or_high`/`or_low`, `vwap`, `ema9`/`ema20`, `window_high`/`window_low`).

- **Win rate by strategy** — join `signals` → `trade_intents` →
  `trade_outcomes`, group by `payload->>'strategy'`.
- **VIX/PCR regime** — once `env` is real (not `None`), bucket outcomes by
  `payload->'env'->>'vix'` / `pcr_oi` / `pcr_vol` ranges to see which
  strategies hold up in high-VIX or skewed-PCR conditions.
- **Time-of-day** — bucket by `signals.generated_at` (converted to IST) to
  see whether entries cluster, and perform, differently across the
  09:31-15:09 trading window.
- **Expiry vs normal day** — once `core.market_utils.is_expiry_day` is
  wired into signal payloads or joined by date, compare outcomes on expiry
  days against the rest of the week.
