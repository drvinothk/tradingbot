"""Phase 2's only strategy: proves Signal -> TradeIntent -> RiskDecision ->
audit end-to-end before any real strategy exists (Phase 4). Each cycle picks
the top-ranked ATM+/-N contract via the strike-ranking engine and proposes a
trivial fixed-percent stop/target around the current premium.
`strategy_engine.service.submit_signal` handles everything from there,
including — as of Phase 3 — real dispatch-to-Execution-Service when Risk
approves it in auto mode; this module no longer needs to do anything once
`submit_signal` returns.

Runs on the shared `strategy_engine.runner.StrategyRunner` (Phase 4) like
every other Strategy — this file used to carry its own bespoke runner
(`SyntheticStrategyRunner`), removed once that loop was generalized.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.orm import Session

from app.domain.market.models import Instrument
from app.domain.strategy.models import SignalSide, StrategyRun
from app.modules.strategy_engine.common_rules import compute_stop_target
from app.modules.strategy_engine.interface import Strategy, TradeProposal
from app.modules.strategy_engine.strike_ranking.engine import (
    StrikeRankingConfig,
    rank_from_latest_snapshot,
)

STOP_PCT = 0.10
TARGET_PCT = 0.15
QTY_LOTS = 1


class SyntheticStrategy(Strategy):
    def __init__(
        self,
        instrument_id: uuid.UUID,
        expiry_date: date,
        ranking_config: StrikeRankingConfig = StrikeRankingConfig(),
    ) -> None:
        self.instrument_id = instrument_id
        self.expiry_date = expiry_date
        self.ranking_config = ranking_config

    def evaluate(self, db: Session, strategy_run: StrategyRun) -> TradeProposal | None:
        ranked = rank_from_latest_snapshot(
            db, self.instrument_id, self.expiry_date, self.ranking_config
        )
        if not ranked:
            return None

        top = ranked[0]
        entry_price = top.ltp
        instrument = db.get(Instrument, self.instrument_id)
        tick_size = float(instrument.tick_size) if instrument is not None else 0.0
        stop_price, target_price = compute_stop_target(entry_price, STOP_PCT, TARGET_PCT, tick_size)

        return TradeProposal(
            option_contract_id=top.option_contract_id,
            side=SignalSide.BUY,
            qty_lots=QTY_LOTS,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            payload={
                "strategy": "synthetic",
                "strike_score": top.score,
                "breakdown": top.breakdown,
            },
        )
