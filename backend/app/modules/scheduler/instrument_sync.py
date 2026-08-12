"""Daily instrument/strike sync job — and, not coincidentally, the same
function that seeds the instrument master the first time. Idempotent upsert
against `broker.get_instrument_master(exchange)`, so "seed once" and "run
daily" are the same operation: this is what closes the gap the source
blueprint missed (strikes/expiries roll and need refreshing), and Phase 5
swaps the mock adapter for the real Shoonya one behind broker_port with zero
changes here.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.domain.market.models import Instrument, InstrumentMasterSyncLog, OptionContract, SyncStatus
from app.domain.market.models import OptionType as MarketOptionType
from app.modules.broker_adapter.base.broker_port import BrokerPort

logger = logging.getLogger("app.scheduler.instrument_sync")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def sync_instrument_master(
    db: Session, broker: BrokerPort, exchanges: list[str]
) -> InstrumentMasterSyncLog:
    """Pulls the full instrument list per exchange from `broker`, upserts
    Instrument (underlyings) and OptionContract rows, deactivates contracts
    whose expiry has passed, and records the outcome — success or failure —
    in `instrument_master_sync_log`. Never raises past this function: a
    failed sync is recorded, not thrown, since a scheduled job dying
    silently is worse than a logged failure the Scheduler's health check
    can act on.

    2026-08-11/12: a diff-based deactivation pass (any previously-active,
    not-yet-expired contract an underlying's own sync queried but didn't
    see again gets deactivated too, not just calendar-expired ones) was
    added, then **reverted the next day after live evidence showed it's
    unsafe against this real broker's actual behavior**: two real
    `SearchScrip` calls for the same underlying (`BANKNIFTY`), ~2.5 minutes
    apart, returned **completely different, non-overlapping expiry sets**
    (`2026-08-13` in one call, `2026-08-25` in the next) — confirmed via a
    live spot-price cross-check against Angel One's own feed
    (BANKNIFTY ~57,446 at the time) that `2026-08-13`'s ~51,000 strikes were
    the real, current data and `2026-08-25`'s ~31,000 strikes were the
    stale ones. A diff pass scoped to "what this one call returned" flips
    valid data inactive whenever a call happens to return a partial/
    different subset of the true chain, which this broker's `SearchScrip`
    demonstrably does — not a one-off fluke, reproduced twice in the same
    session. Time-based expiry (below) doesn't have this failure mode since
    it never depends on what any single call did or didn't return, so it's
    what's left. The specific stale-row problem the diff pass was built
    for (a wrong expiry surviving indefinitely because it's still
    calendar-future) is real and still open — see the build plan's "Known
    open items" for the current plan (manual, targeted cleanup of
    confirmed-bad rows, not a general per-sync heuristic).
    """
    run_at = _utcnow()
    instruments_updated = 0
    contracts_added = 0
    contracts_expired = 0

    try:
        for exchange in exchanges:
            infos = broker.get_instrument_master(exchange)
            underlying_infos = [i for i in infos if not i.is_option]
            option_infos = [i for i in infos if i.is_option]

            # 2026-08-12 temporary diagnostic: the diff-deactivation block
            # below deactivated a previously-correct 84-contract batch
            # (NIFTY+BANKNIFTY's real Aug-13 series) the first time it ran
            # for real, with zero new contracts added in the same run --
            # this logs exactly what SearchScrip actually returned, per
            # underlying, so that can be root-caused instead of guessed at.
            # `.warning` since this app has no logging config (see
            # ws_client.py's own identical reasoning) -- remove once the
            # 2026-08-12 NIFTY-instrument-missing investigation is closed.
            expiries_by_underlying: dict[str, set[date]] = defaultdict(set)
            for info in option_infos:
                if info.underlying and info.expiry is not None:
                    expiries_by_underlying[info.underlying].add(info.expiry)
            logger.warning(
                "sync_instrument_master(%s): %d option rows from broker; "
                "expiries seen per underlying: %s",
                exchange,
                len(option_infos),
                {u: sorted(exps) for u, exps in expiries_by_underlying.items()},
            )

            symbol_to_instrument_id: dict[str, uuid.UUID] = {}
            for info in underlying_infos:
                instrument = (
                    db.query(Instrument)
                    .filter(Instrument.symbol == info.symbol, Instrument.exchange == exchange)
                    .one_or_none()
                )
                if instrument is None:
                    instrument = Instrument(
                        id=uuid.uuid4(),
                        symbol=info.symbol,
                        exchange=exchange,
                        lot_size=info.lot_size,
                        tick_size=info.tick_size,
                        freeze_qty=info.freeze_qty,
                        is_active=True,
                    )
                    db.add(instrument)
                    db.flush()
                    instruments_updated += 1
                elif (
                    instrument.lot_size != info.lot_size
                    or instrument.tick_size != Decimal(str(info.tick_size))
                    or (info.freeze_qty is not None and instrument.freeze_qty != info.freeze_qty)
                ):
                    # Comparing the DB's Decimal to a raw float directly is unreliable
                    # (binary float imprecision makes 0.05 != Decimal('0.0500') even
                    # when they represent the same value) — route the float through
                    # its string repr to get a matching Decimal first.
                    instrument.lot_size = info.lot_size
                    instrument.tick_size = info.tick_size
                    # Only overwrite when the broker actually supplies a value —
                    # never blank out an operator-set freeze_qty just because
                    # this sync source (e.g. Shoonya, today) doesn't carry one.
                    if info.freeze_qty is not None:
                        instrument.freeze_qty = info.freeze_qty
                    instruments_updated += 1
                symbol_to_instrument_id[info.symbol] = instrument.id

            for info in option_infos:
                underlying_id = symbol_to_instrument_id.get(info.underlying or "")
                if underlying_id is None:
                    underlying_row = (
                        db.query(Instrument)
                        .filter(
                            Instrument.symbol == info.underlying,
                            Instrument.exchange == exchange,
                        )
                        .one_or_none()
                    )
                    if underlying_row is None:
                        # Can't attach an option contract with no known underlying —
                        # skip rather than guess; the next sync run will pick it up
                        # once the underlying itself has been synced.
                        continue
                    underlying_id = underlying_row.id

                option_type = MarketOptionType(info.option_type.value if info.option_type else "CE")
                contract = (
                    db.query(OptionContract)
                    .filter(
                        OptionContract.instrument_id == underlying_id,
                        OptionContract.expiry_date == info.expiry,
                        OptionContract.strike == info.strike,
                        OptionContract.option_type == option_type,
                    )
                    .one_or_none()
                )
                if contract is None:
                    db.add(
                        OptionContract(
                            id=uuid.uuid4(),
                            instrument_id=underlying_id,
                            expiry_date=info.expiry,
                            strike=info.strike or 0.0,
                            option_type=option_type,
                            symbol=info.symbol,
                            broker_token=info.broker_token,
                            is_active=True,
                        )
                    )
                    contracts_added += 1
                elif (
                    not contract.is_active
                    and info.expiry is not None
                    and info.expiry >= date.today()
                ):
                    # Reactivate only if it's genuinely still tradable — never
                    # resurrect a contract past its own expiry just because a
                    # stale broker pull happened to list it again.
                    contract.is_active = True

        # SessionLocal is created with autoflush=False (see core/db/session.py),
        # so contracts just added above via db.add() are invisible to the
        # query below until explicitly flushed — without this, a contract
        # inserted with an already-past expiry wouldn't be caught until the
        # *next* sync call, silently leaving a stale-active contract for one
        # extra cycle.
        db.flush()

        today = date.today()
        expired = (
            db.query(OptionContract)
            .join(Instrument, OptionContract.instrument_id == Instrument.id)
            .filter(
                OptionContract.expiry_date < today,
                OptionContract.is_active.is_(True),
                Instrument.exchange.in_(exchanges),
            )
        )
        for contract in expired:
            contract.is_active = False
            contracts_expired += 1

        log = InstrumentMasterSyncLog(
            id=uuid.uuid4(),
            run_at=run_at,
            instruments_updated=instruments_updated,
            contracts_added=contracts_added,
            contracts_expired=contracts_expired,
            status=SyncStatus.SUCCESS,
        )
    except Exception as exc:  # noqa: BLE001 - deliberately caught: see docstring
        log = InstrumentMasterSyncLog(
            id=uuid.uuid4(),
            run_at=run_at,
            instruments_updated=instruments_updated,
            contracts_added=contracts_added,
            contracts_expired=contracts_expired,
            status=SyncStatus.FAILED,
            detail=str(exc)[:1000],
        )

    db.add(log)
    db.flush()
    return log
