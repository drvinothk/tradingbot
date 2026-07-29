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
                )
                .distinct()
            }
        )
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
