"""The only place a raw Alice Blue (Noren-family) WebSocket payload is ever
touched. Alice Blue's touchline tick shape is confirmed live 2026-08-21 from
its own official WebSocket doc page: `{"t":"tk","e":"NFO","tk":"54957",
"ts":"NIFTY28JUL22C16600","ls":"50","ti":"0.05","c":"42.20","lp":"84.00",
"pc":"99.05","ft":"1658911102"}` — `lp` (last price) is the only price
field; there is no confirmed bid/ask/volume/OI field on a touchline push
(those would need a separate depth subscription, whose message shape isn't
documented anywhere Alice Blue's own docs showed — depth parsing is
deliberately not implemented yet, see `alice_blue_ws_client.py`'s own
docstring). `bid`/`ask` default to `0.0`, `volume`/`oi` default to `0`/`None`
if genuinely absent — never fabricated.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.modules.broker_adapter.base.contracts import Tick


class NormalizationError(Exception):
    pass


def _float(raw: dict, key: str, default: float = 0.0) -> float:
    value = raw.get(key)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(raw: dict, key: str, default: int = 0) -> int:
    value = raw.get(key)
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _utcnow() -> datetime:
    return datetime.now(UTC)


def parse_tick(raw: dict, contract_symbol: str) -> Tick:
    if "lp" not in raw:
        raise NormalizationError(f"Alice Blue tick missing 'lp': {raw!r}")
    return Tick(
        contract_symbol=contract_symbol,
        ltp=_float(raw, "lp"),
        bid=_float(raw, "bp1", default=0.0),
        ask=_float(raw, "sp1", default=0.0),
        volume=_int(raw, "v", default=0),
        oi=_int(raw, "oi", default=0) if "oi" in raw else None,
        ts=_utcnow(),
    )
