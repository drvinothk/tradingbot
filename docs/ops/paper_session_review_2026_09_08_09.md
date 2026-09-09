# Paper-session review — 2026-09-08 & 2026-09-09

All times IST. Both days: live deployment on A1 (`144.24.137.112`), session
`live_enabled`, **all strategy trades executed paper** (the `_Live` configs were
never re-armed after the 2026-09-08 paper/live inversion, so
`EMA_Convic_Live` / `OI_Convic_Live` ran paper and produced rows identical to
their `_Paper` twins — the paper book double-booked every conviction trade).

---

## Part A — Infra (2026-09-08 deep dive)

| # | Finding | Severity | Status |
|---|---|---|---|
| P1 | **63-min `reconciliation_lock`** (09:35–10:38) from manual equity trades (`JWL-EQ`, `THERMAX-EQ`, `SCHNEIDER-EQ`) in the same Shoonya account. Blocked 41 live entry intents; 304 CRITICAL `reconciliation_mismatch` alerts. Root cause: the app-placed-symbol scoping fix (`b4e33a2`) was deployed mid-session (file mtime 10:23, loaded on the 10:36 restart) — the lock hit *before* the fix was live. Recovered in 2 min post-restart. | 🔴 | Fix deployed; **held clean on 09-09** (zero locks despite `live_enabled`) |
| P2 | **6 backend restarts**, ~4 in market hours (05:47 unknown, 08:55 autologin, 09:55 manual, 10:36 manual-deploy, 11:57 unknown, 15:16 manual). None were crashes. Each drops the WS feed, resets failover state, re-runs `resume_strategy_runners`, clears the order-update cache. | 🔴 | Behavioral — recurred 09-09 (6 restarts, 4 in market hours) |
| P3 | Shoonya WS: **two** failure modes — `ak/NOT_OK` (auth rejected, ×54, session-invalidation from autologin + manual re-logins) and HTTP 502 (gateway, broker infra). Autologin's `/shoonya/callback` + `/aliceblue/callback` both returned 401 — token hand-off failed, fell back to a restart. | 🟠 | Partly addressed by `4f21f5e` auto-login resilience; callback 401 still open |
| P4 | 5 strategy runs held open positions during the 13:43–13:49 feed blackout (`strategy_run_stalled`). Priced from `broker.get_quote` REST fallback — no confirmed bad exit, but real exposure. | 🟡 | Feed fix (below) largely closes this |
| P5 | 9 `price_drift_exceeded` + `reentry_cooldown` rejections 13:51–13:52 — signals on stale bars during the gap. Risk behaved correctly. | 🟡 | Feed fix closes this |
| — | No OOM, no crashes, no `kill_switch`/`degraded_mode`, DB/NTP/disk clean. Post-market load spike = the `backtest.slice` p17 sweep (cgroup-capped, off-hours). | 🟢 | — |

**Verdict:** no strong code fix outstanding from Part A — P1 fix is deployed and
held; P2 is behavioral; P3's remaining piece (callback 401) is a small,
bounded fix; P4/P5 are downstream of the feed and closed by `4f21f5e`.

---

## Part B — What the deployed fixes achieved (09-08 → 09-09)

| Fix (commit) | 09-08 | 09-09 | Verdict |
|---|---|---|---|
| **Anti-flap failover + REST fallback** (`4f21f5e`) | 124 WS drops → ~9-min feed gaps (failover thrashed shoonya↔alice_blue every 60–90 s; Alice Blue never streamed) | **415 WS drops → 2 missing minutes** all day; steady ~5,250 ticks / 15-min | ✅ Big win — 3× the WS churn, near-zero feed gap |
| **Option-chain plausibility rails** (`d8b1ca0`) | 13 `price_drift` rejects; a corrupted **40.55** premium (real ~36) entered a signal for the ATM strike and passed the no-arb bound | **1 `price_drift` reject**; herd entry prices (78.40 / 107.75 / 124.50 / 183.75) all clean, no corruption | ✅ Held (one no-arb-bound gap on 09-08 still worth chasing) |
| **Reconciliation app-symbol scoping** (`b4e33a2`) | 63-min lock from equity trades | **Zero** reconciliation_lock / mismatch | ✅ Held |
| **`same_direction_trouble_locked` guard** (new) | not present | 27 VWAP re-entries blocked in the afternoon chop (12:57–14:06) | ✅ Helped — but per-config + reactive (see gap #3) |

---

## Part C — Paper-trade P&L

### 2026-09-08 (tight range, NIFTY 23,620–23,650 all afternoon)

| Config | Entries | Net | W/L legs |
|---|---|---|---|
| VWAP_RSI_Paper_Convic | 4 | **+₹7,137** | 4/0 |
| VWAP_Base_Test 4 | 3 | +₹5,018 | 3/0 |
| VWAP_Base | 3 | +₹4,036 | 5/1 |
| OI/Vol_Base | 1 | +₹3,396 | 2/0 |
| Test | 1 | +₹3,009 | 1/0 |
| EMA_Convic_Paper | 5 | −₹24 | 7/8 |
| EMA_Base | 8 | **−₹4,202** | 5/9 |

VWAP carried the book; EMA micro-pullback churned the range and bled.

### 2026-09-09 (one big AM move ~12:33, then reversal chop)

| Config | Entries | Net | Read |
|---|---|---|---|
| OI/Vol_Base | 1 | **+₹18,613** | caught the 12:33 move (target + trail) |
| OI_Convic (×2 twins) | 2 | +₹13,146 | same move |
| EMA_Base | 5 | −₹1,547 | late trail wins salvaged it |
| EMA_Convic (×2 twins) | 2 | −₹7,793 | one herd entry, 0/6 legs |
| VWAP_Base | 5 | −₹7,196 | 2/8 |
| VWAP_RSI_Paper_Convic | 5 | −₹8,847 | 1/4 |
| VWAP_Base_Test 4 | 5 | **−₹12,942** | 1/4 |
| **Net** | | **≈ −₹6,566** | |

Exit-reason split: `trail` +₹56,234 (13) · `structure_break` −₹50,388 (20) ·
`stop` −₹21,236 (8) · `target` +₹8,824 (1). The day was 13 trailing wins
(mostly the one AM move) vs 28 structure/stop losses (afternoon chop).

---

## Part D — The consistent gaps (both days)

1. **Herd entry / zero diversification — the #1 structural problem, unaddressed.**
   On 09-09, 5–6 configs (across ORB / VWAP×3 / EMA×2 / OI) independently fired
   `buy` on the **same ATM strike, same direction, same minute**:

   | Contract | # configs that entered |
   |---|---|
   | NIFTY15SEP26C23500 | 6 |
   | NIFTY15SEP26C23700 | 6 |
   | NIFTY15SEP26P23500 | 5 |

   At **14:07** on 09-09, 6 configs entered C23700 @ **78.40 within 30 s**, all
   stopped / structure-broke within minutes (−₹916 to −₹5,102 each). 09-08's
   40.55 pile-on was the same shape. `max_concurrent_positions = 2` is enforced
   **per strategy_run (per config)**, not across the session — so 6 configs =
   1 bet sized 6×, not 6 uncorrelated bets.

2. **`structure_break` is the herd-exit twin of the herd-entry** — −₹50k of
   09-09's losses. On choppy days every config is structure-broken at the same
   minute by the same underlying move.

3. **`same_direction_trouble_locked` is per-config and reactive.** It blocked 27
   VWAP re-entries on 09-09 (good) but only arms *after* a config takes losses,
   and it **released right before the 14:07 massacre**, letting all 6 configs in.

4. **Regime fragility.** VWAP won 09-08 / lost −₹29k on 09-09; EMA lost 09-08 /
   recovered 09-09. No regime filter — P&L is "did trailing catch the one real
   move before the chop gave it back."

5. **Manual restarts + mode-toggling during market hours** (behavioral,
   recurring both days). 09-09: 6 restarts (4 in market hours), plus
   kill-switch 09:42 → recover 10:49 → live/paper/live toggle 11:10–11:47.

6. **`_Live` twins running paper** → the paper book double-books every
   conviction trade, inflating both wins and losses ~2× and distorting the
   P&L read. Re-arm the live configs or disable them.

---

## Part E — Plan: address herd entry with minimal effect on wins

**Goal:** kill the "6 configs stop out together in chop" pattern without
clipping the "6 configs trail a real move" pattern. The discriminator is
**outcome after a short delay** — on a real move the first entry is green in
~60 s; in chop it's red.

**Approach: staggered confirmation entry (leader / follower), not a hard cap.**

1. **New opt-in risk check `herd_entry_guard`** in `risk_service.evaluate_trade_intent`,
   gated by a per-`strategy_type` allowlist (same isolation pattern as the
   one-shot entry guards — see `feedback_one_shot_guard_isolation`).

2. **Key = (underlying, strike, side)** bucketed by the actual
   `option_contract_id` + side. On a new intent, look up open positions **and**
   `DISPATCHED` intents from *other configs in the same session* on that key
   within the last `herd_window_seconds`.

3. **Leader slots.** If fewer than `herd_max_leaders` (default **2**) same-key
   positions/intents exist → allow immediately (this intent is a leader). This
   preserves the first 2 configs' near-optimal entry — the bulk of the trail
   upside on a real move comes from the earliest fills.

4. **Follower confirmation.** If leader slots are full → look up the *earliest*
   leader position's current unrealised P&L from the live feed:
   - `pnl_pct >= herd_confirm_pnl_pct` (default **0**, i.e. not underwater) → allow.
   - else → reject with reason `herd_follower_unconfirmed`.
   This is the whole trick: on a real move the leader is green by the time
   followers evaluate (~30–75 s later) so they still get in ~1–3 points worse;
   in chop the leader is red so followers are blocked.

5. **Fail-open on a stale feed.** If `market_data.freshness` says the leader's
   tick is stale, allow the follower (the guard is a P&L optimisation, not a
   safety gate — don't let a feed blip block a real move).

6. **Make `same_direction_trouble_locked` cross-config** as the cheap
   complement: arm it on the **first** losing same-key exit in the session
   (not the Nth), scoped to the session not the config, with a
   `herd_trouble_cooldown_seconds` (default 300) release. This catches the
   *re-entry* wave that step 4 doesn't (a config with no open position yet).

7. **Tune + validate before deploy.** Replay 09-08, 09-09, and ≥2 known
   trend days through the backtest engine with the guard on; sweep
   `herd_max_leaders` ∈ {1,2,3}, `herd_window_seconds` ∈ {60,90,120},
   `herd_confirm_pnl_pct` ∈ {0, 2, 5}. Accept the setting only if trend-day
   net P&L drops < ~10% while 09-09 structure_break losses drop > ~40%.

8. **Ship paper-only, observe a week.** Then, separately, re-arm or disable the
   `_Live` twins so the book stops double-counting.

**Expected effect (from 09-09 replay estimate):** the 14:07 C23700 herd would
have admitted 2 leaders (EMA_Base + one VWAP), blocked the other 4 →
~−₹4k instead of ~−₹13k on that cluster; the 12:33 winning move is unaffected
(leader green within a minute, followers admitted). Net: most of the
structure_break bleed removed, trail wins intact.

---

## Part F — Decision & backlog (added 2026-09-09, after the replay + config-reduction analysis)

### F.0 Herd-entry guard (Part E) — NOT being built now

Retrospective replay of Part E over 12 sessions (2026-08-25 → 09-09, 459
filled paper positions) showed the guard **as written** moves net P&L by
**−₹2,857 (−0.7%)**, statistically indistinguishable from zero (bootstrap ΔP&L
95% CI [−₹36k, +₹30k]; Wilcoxon daily p≈0.55; Mann-Whitney on per-lot P&L of
blocked vs admitted p≈0.14) and it slightly *worsens* loss days (−₹3k). Reason
the follower-price gate is inert: a real pile-on fills every config at one
identical price in the same 1–30 s (14:07 C23700: all @ 78.40), so
`unreal_pct ≈ 0` and `0 ≥ 0` admits everyone; raising `confirm_pct` to 2–5 %
over-blocks and destroys win days (−₹130k+).

**The pile-ons are ~90 % a config-multiplicity artifact.** 120 s same-contract
bursts of ≥3 configs over the fortnight: **30 bursts as-run → 8 with one
config per strategy_type → 2 with one config per family** (ORB/VWAP/EMA/OI).
Loss-burst bleed −₹141k → −₹9k on the same reduction. 20 of 30 bursts are
`vwap_pullback`-only (the `VWAP_Base` / `VWAP_Base_Test 4` / `VWAP_RSI_Convic`
near-clones firing together).

**Decision:** no guard build. Collapse to one config per strategy (operator
will do this once the paper-vs-live / minor-tweak comparison configs have
served their purpose), run the final set ~1 week, then re-measure the two
residuals below. The `_Live` twins should be re-armed or disabled at the same
time (Part D #6).

### F.1 Cross-family convergence — DEFERRED backlog item

Config reduction does **not** remove this. On both 09-08 and 09-09, after
trimming to one config per strategy, `ema_micro_pullback` + `vwap_pullback`
still fired the **same strike, same direction, same second** on a shared
breakout candle:

| Day | Contract | Configs | Combined net |
|---|---|---|---|
| 2026-09-09 14:07 | NIFTY15SEP26C23700 @ 78.40 | EMA + VWAP | ≈ −₹8,500 |
| 2026-09-08 | (smaller, same shape) | EMA + VWAP | — |

Only ~1 such burst/week spans 3+ families; the EMA+VWAP 2-family case is the
common one. **Proposed cheap guard (not built):** a hard
`(underlying, strike, side)` cap = max 1 open position across all configs in
the session — a plain cross-config dedup, no P&L logic, no leader/follower.
Simulated effect (1-per-family scenario): fortnight burst count 30 → 2.
Tradeoff: also caps the winning cross-family entries on real moves — accept
only after confirming on the final config set that the loss bursts still
outweigh.

### F.2 Single-config serial re-entry (esp. VWAP) — guards EXIST, with a coverage gap

Checked `risk_engine/service.py`. Two relevant guards already run:

| Guard | Scope | Window | Condition | Applies to |
|---|---|---|---|---|
| `_reentry_cooldown_locked` | **per strategy_config** | 5 min | re-entry on the **exact same `option_contract_id`** at a price **≥ the prior exit** (a strictly-cheaper re-entry is allowed) | all strategy types |
| `_same_direction_trouble_locked` ("Guard 1") | **cross-strategy, cross-strike** | 10 min | ≥ 2 *other* positions on the same underlying + CE/PE, same mode, each ≤ −₹300/lot (open unrealised or closed <10 min) | opt-in: `ema_micro_pullback(_conviction)`, `vwap_pullback(_conviction)` — ORB/OI/liquidity excluded (`_CROSS_STRATEGY_GUARD_APPLIES_TO`) |

So VWAP **is** covered by both. The 2026-09-09 afternoon VWAP bleed
(4 losing calls, −₹16.2k) slipped through because:

- `_reentry_cooldown_locked` is **exact-contract only** — VWAP rolled across
  C23650 → C23600 → C23500 → C23700, a different `option_contract_id` each
  time, so the cooldown never triggered.
- Guard 1's **10-min window + "2 others in trouble" count** was too tight —
  the losers were 15–40 min apart, so ≤1 was "in trouble" in any 10-min
  window at the moment of each new entry.

**Options considered, both simulated (2026-09-09) over recorded fills:**

1. **Widen `_reentry_cooldown_locked`** from exact-contract to same underlying +
   same CE/PE (any strike), keeping the "not strictly cheaper than prior exit"
   price test. **REJECTED — sim over 09-01→09-09 (7 sessions): −₹101k to −₹127k
   net, PF 1.81 → 1.55.** It blocks *winners*, not losers (blocked cohort
   +₹260–290/lot; Mann-Whitney p ≈ 1.0 the wrong way; −₹75k on 09-03 alone).
   The price-vs-prior-exit test does not survive crossing strikes — re-entering
   a *running* move often costs ≥ the prior exit, so the guard can't tell a
   trend roll from a reversal chase. The existing exact-contract scoping exists
   for this reason. Do not pursue.

2. **Loosen Guard 1's window** (`_CROSS_STRATEGY_TROUBLE_WINDOW`, currently
   10 min; count stays 2 — the "count → 1 on same strike" relaxation was also
   simmed and is net-negative, −₹11–16k, because on a real move you *do*
   re-enter the same strike). Fine sweep 10/12/15/17/20/25 min, then 10 vs 15
   over the **full 4.5-week history (2026-08-10 → 09-09, 615 positions, 21
   sessions — but the guard only fires on 6 days; pre-late-Aug config density is
   too low to herd, so full-history ≈ 3-week ≈ 2-week, all within ₹100 of each
   other)**:

   | window | net Δ | PF | blocked | WIN-day Δ | LOSS-day Δ | bootstrap P(Δ>0) | days +/− |
   |---|---|---|---|---|---|---|---|
   | **10 (current)** | +₹36,952 | 1.80 | 22 | **+₹12,571** | +₹24,382 | 0.97 | +5/−2 |
   | 15 | +₹37,008 | 1.87 | 42 | **−₹17,293** | +₹54,301 | 0.91 | +5/−3 |
   | 20 | +₹37,287 | 1.87 | 45 | −₹17,521 | +₹54,808 | 0.91 | +5/−3 |

   **10 → 15 is P&L-neutral (+₹56 over 4.5 weeks).** It does not add net edge —
   it only **reshapes the distribution**: the 20 extra positions it blocks net
   ~₹0/lot (a coin-flip mix). w = 15 recovers loss days much harder (09-09
   −₹9,242 → +₹27,577) but gives the same amount back clipping winning
   afternoon re-entries on trend days (09-03 −₹15k, 09-07 −₹23k); w = 10 leaves
   win days *up* +₹12.6k and only worsens 2 days total. 12–20 min is a flat
   plateau (no interior optimum); ≥25 starts clipping a win day.

**Decision: leave Guard 1 at 10 min — no change.** Over 4.5 weeks a wider
window buys nothing on net and the window is the wrong lever for the
09-09-type afternoon herd (it collaterally clips trend-day winners).

**Moderate-risk note:** w = 15 (or 20) is the setting to pick if the operator
values a **smoother equity curve over max total P&L** — smaller drawdowns on
chop days (it flips 09-09 from −₹9k to +₹28k), smaller gains on the big trend
days, roughly the same total, PF 1.80 → 1.87. It's a lower-variance /
less-loss-vs-less-profit profile, not a P&L improvement. Same-net, lower-spread
— a legitimate risk-preference choice, not a tuning win. (Caveat: 6 active days,
bootstrap CI still wide; the "in-trouble" mark is an interpolation of Guard 1's
real live-unrealised check — treat the absolute numbers as soft, the
10-vs-15 *relative* comparison as the finding.)

**Re-check trigger:** if the 10-vs-15 (or wider) window question is raised
again, re-run the sweep against a much larger sample — target **~3 months of
sessions** (≈ 60 trading days, vs the 6 active days this rests on). Only at that
scale is "10 → 15 is P&L-neutral" vs "15 genuinely reduces net" separable, and
the moderate-risk / variance-reduction claim testable with a bootstrap CI that
doesn't straddle zero. Until then, treat this as provisional.

**Every admission-guard variant across this thread lands the same way:** herd
damage ≈ herd profit, so blunt guards net ≈ 0. The real levers are config
reduction (F.0) and regime awareness, not re-entry-window tuning.

### F.3 Carry-overs (unchanged, tracked here for one list)

- **OI conviction staged exit locks too early on a trend** — 2026-09-09
  C23500: `OI_Convic` trail-only +₹6,573 vs `OI/Vol_Base` single-leg
  target+trail +₹18,613 on the same entry. ~₹12k left on the table. Check
  `arm` / leg-split (`[4,3,3]`, arm .30) against the base single-leg config.
- Autologin `/shoonya/callback` + `/aliceblue/callback` 401 token hand-off
  (Part A P3).
- One 2026-09-08 no-arb-bound gap: the 40.55 corrupted premium passed the
  plausibility rails.
