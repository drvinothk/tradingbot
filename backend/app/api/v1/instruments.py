"""Read-only instrument listing for frontend dropdowns (instrument/expiry
pickers on the start-strategy form). Not workspace-scoped — Instrument/
OptionContract are shared exchange-wide data, same reasoning those models
already have no workspace_id column.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.clock import now_ist
from app.core.db.session import get_db
from app.core.security.rbac import require_permission
from app.domain.identity.models import User
from app.domain.market.models import Instrument, OptionContract

router = APIRouter(prefix="/instruments", tags=["instruments"])


class InstrumentOut(BaseModel):
    id: uuid.UUID
    symbol: str
    exchange: str
    lot_size: int
    expiry_dates: list[date]

    model_config = {"from_attributes": True}


@router.get("", response_model=list[InstrumentOut])
def list_instruments(
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("strategy.view")),
) -> list[InstrumentOut]:
    instruments = (
        db.query(Instrument).filter(Instrument.is_active.is_(True)).order_by(Instrument.symbol).all()
    )

    result: list[InstrumentOut] = []
    for instrument in instruments:
        expiry_dates = sorted(
            {
                row[0]
                for row in db.query(OptionContract.expiry_date)
                .filter(
                    OptionContract.instrument_id == instrument.id,
                    OptionContract.is_active.is_(True),
                    # Belt-and-suspenders alongside `is_active`, same
                    # "filter at the read side, not just at sync time"
                    # reasoning as this loop's own FUT*/decoy-row comment
                    # below: a calendar-past expiry has no business in the
                    # picker regardless of whether some sync ever flips its
                    # `is_active` flag correctly. `now_ist().date()`, not
                    # `date.today()` — this deployment's server clock runs
                    # UTC, and `date.today()` would read yesterday's date
                    # for the ~5.5 real hours each night IST has already
                    # crossed midnight but UTC hasn't (2026-08-12 QC pass).
                    OptionContract.expiry_date >= now_ist().date(),
                )
                .distinct()
            }
        )
        # An instrument with no active option contracts can never be used to
        # start a strategy — the start form requires an expiry, and every
        # strategy ranks option strikes from a chain this instrument has
        # none of. Live-found: a real Shoonya `SearchScrip` for
        # "NIFTY"/"BANKNIFTY" also matches futures contracts
        # (`NIFTY25AUG26F`) and unrelated substring decoys
        # (`NIFTYNXT5025AUG26F`), which got synced in as underlying
        # `Instrument` rows. `ShoonyaBrokerAdapter.get_instrument_master`
        # now skips `FUT*` rows at the source, but rows synced *before* that
        # fix still sit in the DB, showing up in the frontend's instrument
        # picker with an empty expiry dropdown — selectable, then failing
        # validation with a confusing "expiry is required". Filtering here
        # (rather than only at sync time) means the picker is correct
        # regardless of what historical rows exist, and stays correct for
        # any future broker whose search is similarly fuzzy.
        if not expiry_dates:
            continue
        result.append(
            InstrumentOut(
                id=instrument.id,
                symbol=instrument.symbol,
                exchange=instrument.exchange,
                lot_size=instrument.lot_size,
                expiry_dates=expiry_dates,
            )
        )
    return result
