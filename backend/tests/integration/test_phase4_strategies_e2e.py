"""End-to-end proof of Phase 4's "done when": ORB, VWAP Pullback, and EMA
Micro-pullback each run on the real `StrategyRunner` (not just `check_setup`
in isolation, which test_orb_strategy.py/test_vwap_pullback_strategy.py/
test_ema_micro_pullback_strategy.py already cover) through the unchanged
Signal -> TradeIntent -> RiskDecision -> dispatch pipeline, concurrently,
across more than one trading_session, in a mix of auto and
approval-required execution mode.

Uses its own fully real-committed fixture data via `real_commit_factory`
(background threads need real commits visible across connections — the
rolled-back `db` fixture is invisible to them) and cleans up explicitly, in
FK-safe order, in a try/finally — same pattern
test_synthetic_strategy.py's `test_runner_executes_on_a_timer_and_stops_cleanly`
established.
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.security.passwords import hash_password
from app.domain.execution.models import (
    Order,
    OrderEvent,
    Position,
    PositionStatus,
    StopPlan,
    TradeOutcome,
    TrailPlan,
)
from app.domain.identity.models import (
    BrokerAccount,
    BrokerAccountStatus,
    BrokerType,
    User,
    Workspace,
)
from app.domain.market.models import (
    IndicatorSnapshot,
    Instrument,
    OptionChainSnapshot,
    OptionContract,
    OptionType,
    PriceBar,
    QuoteTick,
)
from app.domain.risk.models import RiskDecision, RiskLimitConfig
from app.domain.session.models import FundingMode, SafeMode, TradingSession
from app.domain.strategy.models import (
    ApprovalStatus,
    ExecutionMode,
    PendingTradeApproval,
    Signal,
    StrategyConfig,
    StrategyRun,
    StrategyRunStatus,
    TradeIntent,
    TradeIntentStatus,
)
from app.modules.strategy_engine.common_rules import BAR_TIMEFRAME
from app.modules.strategy_engine.runner import StrategyRunner
from app.modules.strategy_engine.strategies.ema_micro_pullback import EMAMicroPullbackStrategy
from app.modules.strategy_engine.strategies.orb import ORBStrategy
from app.modules.strategy_engine.strategies.vwap_pullback import VWAPPullbackStrategy

EXPIRY = date(2026, 7, 30)


@pytest.fixture
def real_commit_factory(engine):
    session_factory = sessionmaker(bind=engine, future=True)

    @contextmanager
    def _scope():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return _scope


def _seed_chain(
    db, instrument: Instrument, ce: OptionContract, pe: OptionContract, *, spot: float
) -> None:
    now = datetime.now(UTC)
    db.add(
        QuoteTick(
            id=uuid.uuid4(), instrument_id=instrument.id, ltp=spot, bid=spot - 1, ask=spot + 1,
            volume=10000, oi=None, ts=now,
        )
    )
    db.add(
        OptionChainSnapshot(
            id=uuid.uuid4(), instrument_id=instrument.id, expiry_date=EXPIRY, ts=now,
            chain_data=[
                {
                    "contract_symbol": ce.symbol, "strike": float(ce.strike),
                    "option_type": OptionType.CE.value, "ltp": 80.0, "bid": 79.5, "ask": 80.5,
                    "volume": 5000, "oi": 20000,
                },
                {
                    "contract_symbol": pe.symbol, "strike": float(pe.strike),
                    "option_type": OptionType.PE.value, "ltp": 75.0, "bid": 74.5, "ask": 75.5,
                    "volume": 5000, "oi": 20000,
                },
            ],
        )
    )


def _seed_bar(db, instrument: Instrument, bucket_start: datetime, *, o, h, l, c) -> None:  # noqa: E741
    db.add(
        PriceBar(
            id=uuid.uuid4(), instrument_id=instrument.id, timeframe=BAR_TIMEFRAME,
            bucket_start=bucket_start, open=o, high=h, low=l, close=c, volume=1000,
        )
    )


def _seed_indicator(db, instrument: Instrument, name: str, value: float) -> None:
    db.add(
        IndicatorSnapshot(
            id=uuid.uuid4(), instrument_id=instrument.id, indicator_name=name,
            timeframe=BAR_TIMEFRAME, value=value, ts=datetime.now(UTC),
        )
    )


def _seed_orb_breakout(db, instrument: Instrument, strategy_run_started_at: datetime) -> None:
    """OR window flat at [21950, 22050], then a bar closing above it."""
    or_start = strategy_run_started_at
    mid = 22000.0
    db.add(PriceBar(
        id=uuid.uuid4(), instrument_id=instrument.id, timeframe=BAR_TIMEFRAME,
        bucket_start=or_start, open=mid, high=22050.0, low=21950.0, close=mid, volume=1000,
    ))
    for i in range(1, 15):
        db.add(PriceBar(
            id=uuid.uuid4(), instrument_id=instrument.id, timeframe=BAR_TIMEFRAME,
            bucket_start=or_start + timedelta(minutes=i),
            open=mid, high=mid + 5, low=mid - 5, close=mid, volume=1000,
        ))
    _seed_bar(
        db, instrument, or_start + timedelta(minutes=15),
        o=22050, h=22080, l=22045, c=22070,
    )


def _seed_vwap_pullback(db, instrument: Instrument) -> None:
    vwap = 22000.0
    _seed_indicator(db, instrument, "VWAP", vwap)
    base = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
    _seed_bar(db, instrument, base, o=22015, h=22020, l=vwap, c=22010)
    _seed_bar(db, instrument, base + timedelta(minutes=1), o=22015, h=22035, l=22012, c=22030)


def _seed_ema_pullback(db, instrument: Instrument) -> None:
    ema9 = 22000.0
    _seed_indicator(db, instrument, "EMA9", ema9)
    _seed_indicator(db, instrument, "EMA20", 21950.0)
    base = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
    _seed_bar(db, instrument, base, o=22010, h=22015, l=ema9, c=22005)
    _seed_bar(db, instrument, base + timedelta(minutes=1), o=22010, h=22030, l=22008, c=22025)


def test_three_strategies_run_concurrently_across_two_sessions_mixed_modes(
    real_commit_factory,
):
    ids: dict[str, list[uuid.UUID] | uuid.UUID] = {}
    runners: list[StrategyRunner] = []
    try:
        with real_commit_factory() as db:
            workspace = Workspace(id=uuid.uuid4(), name=f"phase4-e2e-{uuid.uuid4().hex[:8]}")
            db.add(workspace)
            db.flush()
            ids["workspace_id"] = workspace.id

            user = User(
                id=uuid.uuid4(), workspace_id=workspace.id,
                email=f"phase4-{uuid.uuid4().hex[:8]}@example.com",
                password_hash=hash_password("correct horse battery staple"),
                display_name="Phase 4 E2E User", is_active=True,
            )
            db.add(user)
            db.flush()
            ids["user_id"] = user.id

            broker_account = BrokerAccount(
                id=uuid.uuid4(), workspace_id=workspace.id, broker_type=BrokerType.SHOONYA,
                label="phase4-e2e-account", credentials_ref="config/credentials/shoonya.env",
                status=BrokerAccountStatus.ACTIVE,
            )
            db.add(broker_account)
            db.flush()
            ids["broker_account_id"] = broker_account.id

            session_a = TradingSession(
                id=uuid.uuid4(), workspace_id=workspace.id, broker_account_id=broker_account.id,
                started_by_user_id=user.id, mode=SafeMode.PAPER_ONLY, started_at=datetime.now(UTC),
                budget_amount=1_000_000, daily_target_profit=1_000_000, daily_loss_cap=1_000_000,
                funding_mode=FundingMode.CASH,
            )
            session_b = TradingSession(
                id=uuid.uuid4(), workspace_id=workspace.id, broker_account_id=broker_account.id,
                started_by_user_id=user.id, mode=SafeMode.PAPER_ONLY, started_at=datetime.now(UTC),
                budget_amount=1_000_000, daily_target_profit=1_000_000, daily_loss_cap=1_000_000,
                funding_mode=FundingMode.CASH,
            )
            db.add_all([session_a, session_b])
            db.flush()
            ids["trading_session_ids"] = [session_a.id, session_b.id]

            # A separate underlying per strategy keeps each one's bar/
            # indicator history independent — real usage would typically
            # share one underlying (e.g. NIFTY) across strategies, but nothing
            # about the pipeline requires it, and independent instruments
            # avoid one strategy's bar shape accidentally satisfying (or
            # breaking) another's pattern-matching in this test.
            instruments = {}
            option_contracts = {}
            for tag in ("orb", "vwap", "ema"):
                inst = Instrument(
                    id=uuid.uuid4(), symbol=f"NIFTY-{tag.upper()}-E2E", exchange="NFO",
                    lot_size=25, tick_size=0.05,
                )
                db.add(inst)
                db.flush()
                ce = OptionContract(
                    id=uuid.uuid4(), instrument_id=inst.id, expiry_date=EXPIRY, strike=22000,
                    option_type=OptionType.CE, symbol=f"NIFTY-{tag.upper()}-E2E-CE",
                )
                pe = OptionContract(
                    id=uuid.uuid4(), instrument_id=inst.id, expiry_date=EXPIRY, strike=22000,
                    option_type=OptionType.PE, symbol=f"NIFTY-{tag.upper()}-E2E-PE",
                )
                db.add_all([ce, pe])
                db.flush()
                instruments[tag] = inst
                option_contracts[tag] = (ce, pe)
                _seed_chain(db, inst, ce, pe, spot=22000.0)
            ids["instrument_ids"] = [i.id for i in instruments.values()]

            strategy_configs = {
                "orb": StrategyConfig(
                    id=uuid.uuid4(), workspace_id=workspace.id, name="orb-e2e", strategy_type="orb",
                ),
                "vwap": StrategyConfig(
                    id=uuid.uuid4(), workspace_id=workspace.id, name="vwap-e2e",
                    strategy_type="vwap_pullback",
                ),
                "ema": StrategyConfig(
                    id=uuid.uuid4(), workspace_id=workspace.id, name="ema-e2e",
                    strategy_type="ema_micro_pullback",
                ),
            }
            db.add_all(strategy_configs.values())
            db.flush()
            ids["strategy_config_ids"] = [c.id for c in strategy_configs.values()]

            # orb: auto mode, session_a. vwap: approval-required, session_a.
            # ema: auto mode, session_b — two strategies in one session, one
            # in another, satisfying "across multiple sessions".
            strategy_runs = {
                "orb": StrategyRun(
                    id=uuid.uuid4(), strategy_config_id=strategy_configs["orb"].id,
                    trading_session_id=session_a.id, execution_mode=ExecutionMode.AUTO,
                    status=StrategyRunStatus.SCANNING, started_at=datetime.now(UTC),
                    started_by_user_id=user.id,
                ),
                "vwap": StrategyRun(
                    id=uuid.uuid4(), strategy_config_id=strategy_configs["vwap"].id,
                    trading_session_id=session_a.id, execution_mode=ExecutionMode.APPROVAL_REQUIRED,
                    status=StrategyRunStatus.SCANNING, started_at=datetime.now(UTC),
                    started_by_user_id=user.id,
                ),
                "ema": StrategyRun(
                    id=uuid.uuid4(), strategy_config_id=strategy_configs["ema"].id,
                    trading_session_id=session_b.id, execution_mode=ExecutionMode.AUTO,
                    status=StrategyRunStatus.SCANNING, started_at=datetime.now(UTC),
                    started_by_user_id=user.id,
                ),
            }
            db.add_all(strategy_runs.values())
            db.flush()
            ids["strategy_run_ids"] = [r.id for r in strategy_runs.values()]

            _seed_orb_breakout(db, instruments["orb"], strategy_runs["orb"].started_at)
            _seed_vwap_pullback(db, instruments["vwap"])
            _seed_ema_pullback(db, instruments["ema"])

            # Captured as plain UUIDs before this session commits/closes —
            # reading ORM attributes off these objects afterward would raise
            # DetachedInstanceError (expire_on_commit=True by default).
            instrument_ids_by_tag = {tag: inst.id for tag, inst in instruments.items()}
            strategy_run_ids_by_tag = {tag: run.id for tag, run in strategy_runs.items()}

        strategies = {
            "orb": ORBStrategy(instrument_ids_by_tag["orb"], EXPIRY),
            "vwap": VWAPPullbackStrategy(instrument_ids_by_tag["vwap"], EXPIRY),
            "ema": EMAMicroPullbackStrategy(instrument_ids_by_tag["ema"], EXPIRY),
        }
        for tag, strategy in strategies.items():
            runner = StrategyRunner(
                strategy, strategy_run_ids_by_tag[tag], interval_seconds=0.05,
                session_factory=real_commit_factory,
            )
            runner.start()
            runners.append(runner)

        time.sleep(0.4)
        for runner in runners:
            runner.stop()

        with real_commit_factory() as verify_db:
            orb_intent = (
                verify_db.query(TradeIntent)
                .filter(TradeIntent.strategy_run_id == strategy_run_ids_by_tag["orb"])
                .one()
            )
            assert orb_intent.status == TradeIntentStatus.DISPATCHED
            orb_position = (
                verify_db.query(Position)
                .filter(Position.trade_intent_id == orb_intent.id)
                .one()
            )
            assert orb_position.status == PositionStatus.OPEN

            vwap_intent = (
                verify_db.query(TradeIntent)
                .filter(TradeIntent.strategy_run_id == strategy_run_ids_by_tag["vwap"])
                .one()
            )
            assert vwap_intent.status == TradeIntentStatus.PENDING_APPROVAL
            approval = (
                verify_db.query(PendingTradeApproval)
                .filter(PendingTradeApproval.trade_intent_id == vwap_intent.id)
                .one()
            )
            assert approval.status == ApprovalStatus.PENDING

            ema_intent = (
                verify_db.query(TradeIntent)
                .filter(TradeIntent.strategy_run_id == strategy_run_ids_by_tag["ema"])
                .one()
            )
            assert ema_intent.status == TradeIntentStatus.DISPATCHED

            orb_run = verify_db.get(StrategyRun, strategy_run_ids_by_tag["orb"])
            vwap_run = verify_db.get(StrategyRun, strategy_run_ids_by_tag["vwap"])
            ema_run = verify_db.get(StrategyRun, strategy_run_ids_by_tag["ema"])
            assert orb_run.status == StrategyRunStatus.IN_POSITION
            # vwap never dispatched (still pending approval) -> no Position yet.
            assert vwap_run.status == StrategyRunStatus.SCANNING
            assert ema_run.status == StrategyRunStatus.IN_POSITION
    finally:
        for runner in runners:
            runner.stop()
        with real_commit_factory() as cleanup_db:
            strategy_run_ids = ids.get("strategy_run_ids", [])
            trade_intent_ids = [
                row[0]
                for row in cleanup_db.query(TradeIntent.id).filter(
                    TradeIntent.strategy_run_id.in_(strategy_run_ids)
                )
            ]
            position_ids = [
                row[0]
                for row in cleanup_db.query(Position.id).filter(
                    Position.trade_intent_id.in_(trade_intent_ids)
                )
            ]
            from sqlalchemy import or_ as sa_or

            order_ids = [
                row[0]
                for row in cleanup_db.query(Order.id).filter(
                    sa_or(
                        Order.trade_intent_id.in_(trade_intent_ids),
                        Order.position_id.in_(position_ids),
                    )
                )
            ]
            cleanup_db.query(OrderEvent).filter(OrderEvent.order_id.in_(order_ids)).delete(
                synchronize_session=False
            )
            cleanup_db.query(TradeOutcome).filter(
                TradeOutcome.position_id.in_(position_ids)
            ).delete(synchronize_session=False)
            cleanup_db.query(StopPlan).filter(StopPlan.position_id.in_(position_ids)).delete(
                synchronize_session=False
            )
            cleanup_db.query(TrailPlan).filter(TrailPlan.position_id.in_(position_ids)).delete(
                synchronize_session=False
            )
            cleanup_db.query(Position).filter(Position.id.in_(position_ids)).update(
                {"closing_order_id": None}, synchronize_session=False
            )
            cleanup_db.query(Order).filter(Order.position_id.in_(position_ids)).delete(
                synchronize_session=False
            )
            cleanup_db.query(Position).filter(
                Position.trade_intent_id.in_(trade_intent_ids)
            ).delete(synchronize_session=False)
            cleanup_db.query(Order).filter(Order.trade_intent_id.in_(trade_intent_ids)).delete(
                synchronize_session=False
            )
            cleanup_db.query(PendingTradeApproval).filter(
                PendingTradeApproval.trade_intent_id.in_(trade_intent_ids)
            ).delete(synchronize_session=False)
            cleanup_db.query(RiskDecision).filter(
                RiskDecision.trade_intent_id.in_(trade_intent_ids)
            ).delete(synchronize_session=False)
            cleanup_db.query(TradeIntent).filter(
                TradeIntent.strategy_run_id.in_(strategy_run_ids)
            ).delete(synchronize_session=False)
            cleanup_db.query(Signal).filter(
                Signal.strategy_run_id.in_(strategy_run_ids)
            ).delete(synchronize_session=False)
            cleanup_db.query(StrategyRun).filter(
                StrategyRun.id.in_(strategy_run_ids)
            ).delete(synchronize_session=False)
            cleanup_db.query(StrategyConfig).filter(
                StrategyConfig.id.in_(ids.get("strategy_config_ids", []))
            ).delete(synchronize_session=False)

            instrument_ids = ids.get("instrument_ids", [])
            from app.domain.broker.models import BrokerSyncState, ReconciliationRun

            cleanup_db.query(BrokerSyncState).filter(
                BrokerSyncState.option_contract_id.in_(
                    cleanup_db.query(OptionContract.id).filter(
                        OptionContract.instrument_id.in_(instrument_ids)
                    )
                )
            ).delete(synchronize_session=False)
            cleanup_db.query(OptionContract).filter(
                OptionContract.instrument_id.in_(instrument_ids)
            ).delete(synchronize_session=False)
            cleanup_db.query(QuoteTick).filter(
                QuoteTick.instrument_id.in_(instrument_ids)
            ).delete(synchronize_session=False)
            cleanup_db.query(OptionChainSnapshot).filter(
                OptionChainSnapshot.instrument_id.in_(instrument_ids)
            ).delete(synchronize_session=False)
            cleanup_db.query(PriceBar).filter(
                PriceBar.instrument_id.in_(instrument_ids)
            ).delete(synchronize_session=False)
            cleanup_db.query(IndicatorSnapshot).filter(
                IndicatorSnapshot.instrument_id.in_(instrument_ids)
            ).delete(synchronize_session=False)

            trading_session_ids = ids.get("trading_session_ids", [])
            from app.domain.audit.models import AuditEvent
            from app.domain.ops.models import SystemAlert

            cleanup_db.query(ReconciliationRun).filter(
                ReconciliationRun.trading_session_id.in_(trading_session_ids)
            ).delete(synchronize_session=False)
            cleanup_db.query(SystemAlert).filter(
                SystemAlert.trading_session_id.in_(trading_session_ids)
            ).delete(synchronize_session=False)
            cleanup_db.query(AuditEvent).filter(
                AuditEvent.trading_session_id.in_(trading_session_ids)
            ).delete(synchronize_session=False)
            cleanup_db.query(TradingSession).filter(
                TradingSession.id.in_(trading_session_ids)
            ).delete(synchronize_session=False)

            cleanup_db.query(Instrument).filter(Instrument.id.in_(instrument_ids)).delete(
                synchronize_session=False
            )
            if "broker_account_id" in ids:
                cleanup_db.query(BrokerAccount).filter(
                    BrokerAccount.id == ids["broker_account_id"]
                ).delete(synchronize_session=False)
            if "user_id" in ids:
                cleanup_db.query(User).filter(User.id == ids["user_id"]).delete(
                    synchronize_session=False
                )
            if "workspace_id" in ids:
                cleanup_db.query(RiskLimitConfig).filter(
                    RiskLimitConfig.workspace_id == ids["workspace_id"]
                ).delete(synchronize_session=False)
                cleanup_db.query(Workspace).filter(
                    Workspace.id == ids["workspace_id"]
                ).delete(synchronize_session=False)
            cleanup_db.commit()
