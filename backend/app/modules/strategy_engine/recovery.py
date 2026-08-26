"""Strategy-runner startup recovery — moved out of `app.main` 2026-08-26 to
break the one real backward import edge in the codebase:
`session.bootstrapper` needed this function and imported it straight from
`app.main` at module level, meaning a domain module reached up into the
FastAPI entrypoint. Living here instead, both `app.main` and
`session.bootstrapper` import it downward, matching every other dependency
direction in the app.
"""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger("app.strategy_engine.recovery")


def resume_strategy_runners() -> None:
    """For every `StrategyRun` left non-`STOPPED` on a `trading_session`
    still `ACTIVE` (the signature of a crash/restart mid-scan, not a clean
    `stop_strategy` call): rebuild its `Strategy` object and resume its
    `StrategyRunner` thread. Without this, `strategy_runs.status` stays
    `scanning` forever after any restart — an in-process
    `threading.Thread` (`api.v1.strategies._RUNNERS`) with nothing durable
    behind it — while nothing is actually happening: no market-data
    ingestion, no evaluate() cycles, no signals. `GET /strategies/running`
    keeps reporting it as live regardless, since it reads `strategy_runs`
    rows, not runner liveness. Found live: three real restarts in one
    session (deploying the Shoonya WS diagnostic patch) each silently
    zombied every running strategy this same way.

    Only possible because `StrategyRun.instrument_id`/`expiry_date` are now
    persisted (see that column's own docstring) — before, that information
    only ever lived in the in-memory `Strategy` object inside the runner
    thread itself, so a resume was impossible even in principle. Runs where
    those are still `NULL` predate the column and are skipped, not
    resumed — they need a manual stop + restart via the API, same as before
    this fix existed.

    A `trading_session` that isn't `ACTIVE` (kill_switch/degraded_mode/
    reconciliation_lock/ended) is deliberately not resumed — same
    "don't silently reanimate a session no longer in a tradeable state"
    reasoning as the `PositionManager` resume above. One run's failure
    (a stale `strategy_type`, a deleted `Instrument`) is caught and skipped
    rather than aborting every other run's resume or startup itself.

    2026-08-14: `ensure_ingestion_running` is skipped entirely (not just
    deferred a cycle) when `market_data.provider_composition.
    is_market_data_ready()` is `False` — this function runs before
    any human has had a chance to reconnect Shoonya, so calling it
    unconditionally used to permanently cache the market-data provider
    singleton wrapping the mock broker, silently writing fabricated prices
    into `price_bars`/`quote_ticks` under a real-looking "shoonya"
    configuration until a human happened to reconnect. The `StrategyRunner`
    thread still starts either way — it just sits idle (no bars, no
    signal) until `market_data.registry.reset_for_reconnect` starts
    ingestion for real once Shoonya connects.
    """
    from app.api.v1.strategies import _RUNNERS, _build_strategy
    from app.core.db.session import session_scope
    from app.core.sleep_inhibitor import get_sleep_inhibitor
    from app.domain.market.models import Instrument
    from app.domain.session.models import TradingSession, TradingSessionStatus
    from app.domain.strategy.models import StrategyConfig, StrategyRun, StrategyRunStatus
    from app.modules.execution_engine.paper.registry import ensure_position_manager_running
    from app.modules.market_data.provider_composition import is_market_data_ready
    from app.modules.market_data.registry import ensure_ingestion_running
    from app.modules.strategy_engine.runner import StrategyRunner

    with session_scope() as db:
        runs = (
            db.query(StrategyRun)
            .join(TradingSession, StrategyRun.trading_session_id == TradingSession.id)
            .filter(
                StrategyRun.status != StrategyRunStatus.STOPPED,
                TradingSession.status == TradingSessionStatus.ACTIVE,
            )
            .all()
        )
        if not runs:
            logger.info("Strategy-runner recovery check: no stale active runs found.")
            return

        ingestion_ready = is_market_data_ready()
        if not ingestion_ready:
            logger.warning(
                "Shoonya not connected yet — deferring market-data ingestion for all "
                "resumed runs until reconnect (see market_data.registry.reset_for_reconnect)."
            )

        resumed: list[uuid.UUID] = []
        skipped_no_instrument: list[uuid.UUID] = []
        for run in runs:
            if run.instrument_id is None or run.expiry_date is None:
                skipped_no_instrument.append(run.id)
                continue

            try:
                strategy_config = db.get(StrategyConfig, run.strategy_config_id)
                instrument = db.get(Instrument, run.instrument_id)
                if strategy_config is None or instrument is None:
                    logger.warning(
                        "strategy_run %s references a missing config/instrument — skipping resume",
                        run.id,
                    )
                    continue

                strategy = _build_strategy(strategy_config, run.instrument_id, run.expiry_date)
                interval = run.interval_seconds if run.interval_seconds is not None else 30.0

                def _forget_runner(run_id: uuid.UUID = run.id) -> None:
                    # Default-arg binds run_id at definition time, not call
                    # time -- avoids the classic late-binding closure-in-a-
                    # loop bug (`run` is reassigned every iteration).
                    _RUNNERS.pop(run_id, None)

                runner = StrategyRunner(
                    strategy,
                    run.id,
                    interval_seconds=interval,
                    on_self_stop=_forget_runner,
                )
                runner.start()
                _RUNNERS[run.id] = runner

                get_sleep_inhibitor().acquire(f"strategy_run:{run.id}")
                if ingestion_ready:
                    ensure_ingestion_running(instrument.symbol)
                ensure_position_manager_running(run.trading_session_id)

                resumed.append(run.id)
            except Exception:
                logger.exception(
                    "Failed to resume strategy_run %s — leaving it non-stopped but idle; "
                    "stop and restart it manually via the API",
                    run.id,
                )

        if resumed:
            logger.warning(
                "Resumed %d strategy runner(s) found active at startup: %s",
                len(resumed),
                [str(r) for r in resumed],
            )
        if skipped_no_instrument:
            logger.warning(
                "%d strategy_run(s) left non-stopped but predate instrument_id/expiry_date "
                "and cannot be resumed — stop and restart them via the API: %s",
                len(skipped_no_instrument),
                [str(r) for r in skipped_no_instrument],
            )
