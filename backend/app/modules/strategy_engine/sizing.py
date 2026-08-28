"""Where a strategy run's `qty_lots` comes from.

2026-08-24: `qty_lots` used to be a hardcoded `QTY_LOTS = 1` constant in every
strategy file. It became a real per-strategy `params["qty_lots"]` tunable with a
mode-aware default -- explicit user request: "default will be 1 lot for live
trading, and 10 lots for paper trading... if I dont edit, 1 lot stays as default,
hence the risk is also managed there."

2026-08-28: the first cut keyed the default off `StrategyConfig.status == LIVE`
-- a field with no API setter, stuck at `research` forever -- so a strategy
graduated to live via the session master switch (`SafeMode.LIVE_ENABLED`) kept
the 10-lot *paper* default while Risk Service (correctly) gated it as live,
and every signal was rejected for `per_trade_lot_cap_exceeded`. Fixed by keying
the default off the exact same predicate that actually routes the order --
`broker_adapter.composition.is_strategy_routed_live` -- so sizing and
risk/broker routing can never disagree again.

An explicit `params["qty_lots"]` always wins over the default, in either mode
(1 lot is only a conservative testing-phase floor; real multi-lot sizing is set
per strategy in the UI). `resolve_qty_lots` is called both at strategy
construction (`api.v1.strategies._build_strategy`) and once per cycle
(`strategy_engine.runner.run_cycle`) so a mid-session Paper<->Live flip
re-sizes a *running* strategy on its next cycle without a restart.
"""

from __future__ import annotations

from app.domain.session.models import TradingSession
from app.domain.strategy.models import StrategyConfig, StrategyRun
from app.modules.broker_adapter.composition import is_strategy_routed_live

# 1 lot is a deliberate live-trading floor for the current testing phase; raise
# it per strategy via `params["qty_lots"]` in the UI. Paper stays larger (and is
# risk-service-exempt for the per-trade lot cap -- see risk_engine.service's own
# mode-aware rule) so paper runs can prove entry logic at a realistic size.
DEFAULT_QTY_LOTS_LIVE = 1
DEFAULT_QTY_LOTS_PAPER = 10


def resolve_qty_lots(
    strategy_config: StrategyConfig,
    trading_session: TradingSession | None,
    strategy_run: StrategyRun | None,
) -> int:
    """`params["qty_lots"]` if the operator set one; otherwise the mode-aware
    default, keyed on whether this strategy would actually route live right now
    (`is_strategy_routed_live`). `trading_session is None` (only reachable from
    `_build_strategy`'s param-mapping unit tests, never production) falls back to
    the paper default -- the conservative direction: an oversized value on a
    live path is caught by Risk Service's `per_trade_lot_cap`, never dispatched.
    """
    explicit = (strategy_config.params or {}).get("qty_lots")
    if explicit is not None:
        return int(explicit)

    if trading_session is None:
        return DEFAULT_QTY_LOTS_PAPER

    routed_live = is_strategy_routed_live(trading_session, strategy_run)
    return DEFAULT_QTY_LOTS_LIVE if routed_live else DEFAULT_QTY_LOTS_PAPER
