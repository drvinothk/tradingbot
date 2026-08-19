"""VIX/PCR environment metrics — the real data pipeline, 2026-08-19.

**Scope, deliberately narrow**: this only feeds already-wired call sites
(every strategy's own signal `payload={"env": ...}`, recorded purely for
context/audit) real data instead of the prior hardcoded `None`. No
strategy gates an entry/exit decision on any of this yet — that's real,
separate future work, not touched here.

**VIX**: reuses the existing `Instrument`/`QuoteTick` ingestion pipeline
exactly as every underlying already does — see `market_data.market_hours
.ENV_METRIC_SYMBOLS` for where the `"INDIA VIX"` subscription itself gets
triggered, and both providers' own symbol-translation maps
(`truedata_provider._TO_TRUEDATA_SYMBOL`, `shoonya.adapter
._UNDERLYING_INDEX_TSYM`/`_UNDERLYING_INDEX_SEARCH_TEXT`) for how that
symbol resolves to each broker's real one. **Deliberately no staleness
filtering** — India VIX is a computed index, not continuously traded
(live-observed as low as ~2 ticks/60s vs. NIFTY's 350+/min, see
`market_data.ingestion._WS_HEALTH_GRACE_SECONDS_BY_SYMBOL`'s own
docstring), so "the latest tick, however old" is the correct semantics
here, not a bug to guard against — this is informational context, not a
trade-safety gate the rest of `market_data.freshness`'s staleness
machinery exists to protect.

**PCR**: computed on read, not persisted separately — `OptionChainSnapshot
.chain_data` already carries per-strike `oi`/`volume` for every snapshot,
timestamped, so `compute_pcr` works identically whether called for "right
now" (the latest snapshot) or reconstructed later for a specific historical
trade (`reporting.exporter`'s own use, against whichever snapshot was
current as of that trade's entry time).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.domain.market.models import Instrument, OptionChainSnapshot, QuoteTick
from app.modules.market_data.market_hours import ENV_METRIC_SYMBOLS
from app.modules.strategy_engine.interface import EnvPayload

VIX_SYMBOL = ENV_METRIC_SYMBOLS[0]


def compute_pcr(chain_data: list[dict]) -> tuple[float | None, float | None]:
    """Returns `(pcr_oi, pcr_vol)` -- put/call ratio by open interest and by
    volume, summed across every strike in `chain_data` (one
    `OptionChainSnapshot.chain_data` entry list, see
    `market_data.ingestion.record_option_chain_snapshot` for the exact
    per-entry shape this reads: `option_type` ("CE"/"PE"), `oi`, `volume`).

    `None` for either ratio when the denominator (total call OI/volume) is
    zero or the chain is empty -- a real chain always has both sides
    populated once trading has genuinely started, so a zero denominator
    means "no usable data yet," not "an exotic real ratio," and returning
    `None` (rather than 0.0 or a divide-by-zero) keeps that distinction
    visible to any caller/report reading this later.
    """
    call_oi = put_oi = call_vol = put_vol = 0
    for entry in chain_data:
        option_type = entry.get("option_type")
        oi = entry.get("oi") or 0
        volume = entry.get("volume") or 0
        if option_type == "CE":
            call_oi += oi
            call_vol += volume
        elif option_type == "PE":
            put_oi += oi
            put_vol += volume

    pcr_oi = (put_oi / call_oi) if call_oi > 0 else None
    pcr_vol = (put_vol / call_vol) if call_vol > 0 else None
    return pcr_oi, pcr_vol


def get_vix_as_of(db: Session, as_of_utc: datetime | None = None) -> float | None:
    """Public building block, also used directly by `reporting.exporter`
    (which needs VIX alone, without paying for a chain-snapshot query it
    doesn't need for that column) -- `get_env_metrics` below composes from
    this rather than duplicating it."""
    vix_instrument = db.query(Instrument).filter(Instrument.symbol == VIX_SYMBOL).one_or_none()
    if vix_instrument is None:
        return None
    query = db.query(QuoteTick).filter(QuoteTick.instrument_id == vix_instrument.id)
    if as_of_utc is not None:
        query = query.filter(QuoteTick.ts <= as_of_utc)
    tick = query.order_by(QuoteTick.ts.desc()).first()
    return float(tick.ltp) if tick is not None else None


def get_chain_data_as_of(
    db: Session, instrument_id: uuid.UUID, expiry_date: date, as_of_utc: datetime | None = None
) -> list[dict] | None:
    """Public building block -- `get_env_metrics` composes from this for
    the PCR side; `reporting.exporter` also calls it directly to derive a
    specific traded contract's own raw OI (`get_contract_oi`) from the
    exact same snapshot, without a second, duplicate query."""
    query = db.query(OptionChainSnapshot).filter(
        OptionChainSnapshot.instrument_id == instrument_id,
        OptionChainSnapshot.expiry_date == expiry_date,
    )
    if as_of_utc is not None:
        query = query.filter(OptionChainSnapshot.ts <= as_of_utc)
    snapshot = query.order_by(OptionChainSnapshot.ts.desc()).first()
    if snapshot is None:
        return None
    # OptionChainSnapshot.chain_data is typed Mapped[dict] on the model
    # (a pre-existing inaccuracy, not touched here) but
    # record_option_chain_snapshot always actually writes a list of
    # per-strike dicts -- confirmed by reading that function directly.
    return snapshot.chain_data  # type: ignore[return-value]


def get_contract_oi(chain_data: list[dict], contract_symbol: str) -> int | None:
    """Raw open interest for one specific contract within a chain-snapshot's
    entry list -- distinct from `compute_pcr`'s chain-wide aggregate.
    `reporting.exporter`'s own per-trade "OI (at entry)" column reads this,
    not the PCR ratio. `None` if the contract isn't in that snapshot (a
    strike outside the snapshot's own ranked/captured range) or its `oi`
    field itself was `None`.
    """
    for entry in chain_data:
        if entry.get("contract_symbol") == contract_symbol:
            oi = entry.get("oi")
            return int(oi) if oi is not None else None
    return None


def get_env_metrics(
    db: Session,
    instrument_id: uuid.UUID,
    expiry_date: date,
    *,
    as_of_utc: datetime | None = None,
) -> EnvPayload | None:
    """`as_of_utc=None` (the default, and the only mode `get_latest_env_
    metrics` below exposes) means "the latest available, right now" -- no
    staleness filtering on the VIX side regardless (see module docstring
    for why). Passing a real `as_of_utc` instead reconstructs what was
    known *at that moment* (both the VIX tick and the option-chain
    snapshot are filtered to `ts <= as_of_utc`) -- `reporting.exporter`'s
    own use, to report a historical trade's env metrics as of its entry
    time rather than whatever is current when the report is generated.

    `None` only when genuinely nothing was available yet as of the
    requested moment (no VIX tick received, no option-chain snapshot taken
    for this (instrument, expiry)) -- otherwise a partially-populated
    `EnvPayload` (`total=False`, every key independently optional), so a
    VIX tick landing before this strategy's first chain snapshot (or vice
    versa) still reports whatever is actually known rather than waiting
    for every field to be ready at once.
    """
    vix = get_vix_as_of(db, as_of_utc)
    chain_data = get_chain_data_as_of(db, instrument_id, expiry_date, as_of_utc)
    pcr_oi, pcr_vol = compute_pcr(chain_data) if chain_data is not None else (None, None)

    if vix is None and pcr_oi is None and pcr_vol is None:
        return None
    return EnvPayload(vix=vix, pcr_oi=pcr_oi, pcr_vol=pcr_vol)


def get_latest_env_metrics(
    db: Session, instrument_id: uuid.UUID, expiry_date: date
) -> EnvPayload | None:
    """Every strategy's own call site — see `get_env_metrics`'s own
    docstring for the full behavior; this is just its `as_of_utc=None`
    ("right now") case, kept as a separate, simpler-signature function so
    six call sites across the strategy files don't need to know the
    historical-lookback parameter exists at all.
    """
    return get_env_metrics(db, instrument_id, expiry_date)
