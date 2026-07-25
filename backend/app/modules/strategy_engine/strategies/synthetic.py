"""Phase 2's only strategy: proves Signal -> TradeIntent -> RiskDecision ->
audit end-to-end before any real strategy exists (Phase 4). Each cycle picks
the top-ranked ATM+/-N contract via the strike-ranking engine, proposes a
trivial fixed-percent stop/target around the current premium, and — only
when Risk actually dispatches it (auto mode, all limits clear) — immediately
"closes" the position with a small synthetic P&L via
`strategy_engine.service.close_dispatched_trade_intent_synthetically` (the
same helper `POST /trade-approvals/{id}/approve` uses for the
approval-required path), so the daily-loss-cap / daily-target-profit /
consecutive-loss checks have real data to evaluate against. See that
helper's docstring for why this stand-in exists.

`SyntheticStrategyRunner` is the "on a timer" part (per the Phase 2 build
plan bullet) — a background-thread loop mirroring
`MockBrokerAdapter`'s own threading pattern, each tick opening its own
short-lived session since it runs off the request thread.
"""

from __future__ import annotations

import logging
import random
import threading
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date

from sqlalchemy.orm import Session

from app.core.db.session import session_scope
from app.domain.risk.models import RiskDecision
from app.domain.session.models import TradingSession
from app.domain.strategy.models import (
    SignalSide,
    StrategyConfig,
    StrategyRun,
    StrategyRunStatus,
    TradeIntent,
)
from app.modules.strategy_engine.interface import Strategy, TradeProposal
from app.modules.strategy_engine.service import (
    close_dispatched_trade_intent_synthetically,
    submit_signal,
)
from app.modules.strategy_engine.strike_ranking.engine import (
    StrikeRankingConfig,
    rank_from_latest_snapshot,
)

logger = logging.getLogger("app.strategy_engine.synthetic")

STOP_PCT = 0.10
TARGET_PCT = 0.15
QTY_LOTS = 1

SessionFactory = Callable[[], AbstractContextManager[Session]]


class SyntheticStrategy(Strategy):
    def __init__(
        self,
        instrument_id: uuid.UUID,
        expiry_date: date,
        ranking_config: StrikeRankingConfig = StrikeRankingConfig(),
        rng: random.Random | None = None,
    ) -> None:
        self.instrument_id = instrument_id
        self.expiry_date = expiry_date
        self.ranking_config = ranking_config
        self.rng = rng or random.Random()

    def evaluate(self, db: Session, strategy_run: StrategyRun) -> TradeProposal | None:
        ranked = rank_from_latest_snapshot(
            db, self.instrument_id, self.expiry_date, self.ranking_config
        )
        if not ranked:
            return None

        top = ranked[0]
        entry_price = top.ltp
        stop_price = round(entry_price * (1 - STOP_PCT), 2)
        target_price = round(entry_price * (1 + TARGET_PCT), 2)

        return TradeProposal(
            option_contract_id=top.option_contract_id,
            side=SignalSide.BUY,
            qty_lots=QTY_LOTS,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            payload={"strike_score": top.score, "breakdown": top.breakdown},
        )

    def run_cycle(
        self,
        db: Session,
        strategy_run: StrategyRun,
        trading_session: TradingSession,
        strategy_config: StrategyConfig,
    ) -> RiskDecision | None:
        proposal = self.evaluate(db, strategy_run)
        if proposal is None:
            return None

        decision = submit_signal(db, strategy_run, trading_session, strategy_config, proposal)

        trade_intent = db.get(TradeIntent, decision.trade_intent_id)
        if trade_intent is not None:
            # Deliberately not a real fill simulation (Phase 3's Execution
            # Service against mock prices does that) — just enough win/loss
            # variance, as a fraction of the capital Risk already computed,
            # to exercise the consecutive-loss and daily-loss/target checks.
            # No-ops if the intent didn't actually dispatch this cycle (e.g.
            # approval-required — that path closes separately once a human
            # approves it, via the same shared helper).
            close_dispatched_trade_intent_synthetically(
                db, trading_session, trade_intent, rng=self.rng
            )

        return decision


class SyntheticStrategyRunner:
    def __init__(
        self,
        strategy: SyntheticStrategy,
        strategy_run_id: uuid.UUID,
        interval_seconds: float = 30.0,
        session_factory: SessionFactory = session_scope,
    ) -> None:
        self._strategy = strategy
        self._strategy_run_id = strategy_run_id
        self._interval_seconds = interval_seconds
        self._session_factory = session_factory
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval_seconds + 5)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                with self._session_factory() as db:
                    strategy_run = db.get(StrategyRun, self._strategy_run_id)
                    if strategy_run is None or strategy_run.status == StrategyRunStatus.STOPPED:
                        return
                    trading_session = db.get(TradingSession, strategy_run.trading_session_id)
                    strategy_config = db.get(StrategyConfig, strategy_run.strategy_config_id)
                    if trading_session is None or strategy_config is None:
                        logger.warning(
                            "strategy_run %s references a missing session/config — stopping",
                            self._strategy_run_id,
                        )
                        return
                    self._strategy.run_cycle(db, strategy_run, trading_session, strategy_config)
            except Exception:  # noqa: BLE001 - a background loop must never die silently-crashed
                logger.exception(
                    "synthetic strategy cycle failed for run %s", self._strategy_run_id
                )
            self._stop_event.wait(self._interval_seconds)
