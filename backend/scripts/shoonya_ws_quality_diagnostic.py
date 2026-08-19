"""Standalone, out-of-process Shoonya WS quality monitor — 2026-08-19.

Runs entirely in isolation from the trading-bot app itself: no DB session, no
`app.main`, no strategy/execution code touched. Reuses the already-live
Shoonya session's credentials (exported by the app's own temporary
`GET /shoonya/export-ws-session-for-diagnostic` endpoint into
`.ws_diagnostic_session.json`) rather than logging in separately, since a
second independent login risked silently invalidating the app's own live
session if Shoonya's session model only allows one active token per account
(never confirmed either way — not worth risking a live paper-trading session
to find out).

Subscribes to NIFTY, BANKNIFTY, and India VIX over a fresh `ShoonyaWSClient`
connection and watches tick arrival timing to characterize real-world WS
quality: how often it drops, how long each drop lasts, and the steady-state
tick rate in between. Deliberately observation-only — infers interruptions
purely from tick-arrival gaps rather than hooking into `ShoonyaWSClient`'s
own internal reconnect state, since that state isn't exposed as a public
callback and gap-based inference is what actually matters for "was real
market data flowing or not" regardless of the underlying cause (a dropped
socket, a stalled subscription, or anything else).

**Warm-up gate**: per explicit request, "documentation" (the official
interruption log this run is *for*) only starts once every symbol has gone
`WARMUP_SECONDS` with zero gaps longer than `GAP_THRESHOLD_SECONDS` — so the
initial connect/subscribe settling period never gets counted as a spurious
"interruption" against Shoonya's own WS quality.

Usage:
    .venv/bin/python scripts/shoonya_ws_quality_diagnostic.py

Writes two files next to this script's own working directory:
    - shoonya_ws_quality_events.jsonl  (one JSON object per line, live-appended)
    - shoonya_ws_quality_summary.txt   (rewritten on every heartbeat)

Runs until killed (Ctrl-C, or `kill` if backgrounded with nohup) — intended
to run for the rest of a trading session and be reviewed after market close.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modules.broker_adapter.base.contracts import Tick  # noqa: E402
from app.modules.broker_adapter.shoonya.rest_client import ShoonyaRestClient  # noqa: E402
from app.modules.broker_adapter.shoonya.ws_client import ShoonyaWSClient  # noqa: E402

SESSION_FILE = Path(__file__).resolve().parent.parent / ".ws_diagnostic_session.json"
EVENTS_FILE = Path(__file__).resolve().parent / "shoonya_ws_quality_events.jsonl"
SUMMARY_FILE = Path(__file__).resolve().parent / "shoonya_ws_quality_summary.txt"

# Liquid index feeds tick multiple times a second during real market hours --
# a gap this long is already suspicious, not just "a quiet moment." VIX is
# a computed index, not a continuously-traded instrument, and updates far
# less often -- live-observed ~7 ticks/min (~8.5s average spacing) during
# this run's own warm-up, so a flat 5s threshold would keep VIX perpetually
# "in gap" and warm-up would never clear. Per-symbol thresholds instead.
GAP_THRESHOLD_SECONDS = {
    "NIFTY": 5.0,
    "BANKNIFTY": 5.0,
    "VIX": 60.0,
}
# "Fully started and flawless for 1-2 min" per the explicit request -- the
# upper end, so a borderline-clean 1-minute window doesn't start the official
# log on a fluke.
WARMUP_SECONDS = 120.0
HEARTBEAT_SECONDS = 60.0

# Shoonya's own display-style tsym convention for index scrips (see
# ShoonyaBrokerAdapter._resolve_underlying_token's own docstring for the
# live-confirmed history behind these exact strings) -- searched on NSE.
_INDEX_SEARCH = {
    "NIFTY": ("NIFTY", "NIFTY 50"),
    "BANKNIFTY": ("NIFTY", "NIFTY BANK"),
    "VIX": ("VIX", "INDIAVIX"),
}


def _load_session() -> dict:
    if not SESSION_FILE.exists():
        raise SystemExit(
            f"{SESSION_FILE} not found -- hit GET /shoonya/export-ws-session-for-diagnostic "
            "(in a browser tab already logged into the app) first."
        )
    return json.loads(SESSION_FILE.read_text())


def _resolve_index_tokens(rest: ShoonyaRestClient, uid: str) -> dict[str, tuple[str, str]]:
    """Returns {display_symbol: (exchange, token)}. Searches once per unique
    search text, matches by exact uppercased tsym -- same approach
    `ShoonyaBrokerAdapter._resolve_underlying_token` already uses live.
    """
    resolved: dict[str, tuple[str, str]] = {}
    search_cache: dict[str, list[dict]] = {}
    for symbol, (search_text, exact_tsym) in _INDEX_SEARCH.items():
        rows = search_cache.get(search_text)
        if rows is None:
            rows = rest.search_scrip(uid, "NSE", search_text)
            search_cache[search_text] = rows
        match = next(
            (r for r in rows if str(r.get("tsym", "")).upper() == exact_tsym.upper()), None
        )
        if match is None:
            candidates = [r.get("tsym") for r in rows][:20]
            raise SystemExit(
                f"No exact NSE tsym match for {symbol!r} (expected {exact_tsym!r}) "
                f"among {len(rows)} search_scrip results: {candidates}"
            )
        resolved[symbol] = ("NSE", str(match["token"]))
    return resolved


class _QualityMonitor:
    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols
        self.last_tick_at: dict[str, float] = {}
        self.in_gap: dict[str, bool] = dict.fromkeys(symbols, True)  # "no data yet" = a gap
        self.gap_started_at: dict[str, float] = dict.fromkeys(symbols, time.monotonic())
        self.tick_count: dict[str, int] = dict.fromkeys(symbols, 0)
        self.tick_count_since_heartbeat: dict[str, int] = dict.fromkeys(symbols, 0)
        self.recording_active = False
        self.recording_started_at: datetime | None = None
        self.all_clear_since: float | None = None
        self.total_interruptions = 0
        self.total_downtime_seconds = 0.0
        self.events_fh = EVENTS_FILE.open("a")

    def _emit(self, event: dict) -> None:
        event["ts"] = datetime.now(UTC).isoformat()
        self.events_fh.write(json.dumps(event) + "\n")
        self.events_fh.flush()
        print(json.dumps(event))

    def on_tick(self, tick: Tick) -> None:
        symbol = tick.contract_symbol
        now = time.monotonic()
        was_in_gap = self.in_gap.get(symbol, True)
        self.last_tick_at[symbol] = now
        self.tick_count[symbol] = self.tick_count.get(symbol, 0) + 1
        self.tick_count_since_heartbeat[symbol] = (
            self.tick_count_since_heartbeat.get(symbol, 0) + 1
        )
        if was_in_gap:
            self.in_gap[symbol] = False
            gap_duration = now - self.gap_started_at.get(symbol, now)
            if self.recording_active:
                self._emit(
                    {
                        "event": "gap_ended",
                        "symbol": symbol,
                        "gap_duration_seconds": round(gap_duration, 2),
                    }
                )
                self.total_interruptions += 1
                self.total_downtime_seconds += gap_duration

    def check_gaps(self) -> None:
        now = time.monotonic()
        for symbol in self.symbols:
            last = self.last_tick_at.get(symbol)
            silent_for = now - last if last is not None else now
            threshold = GAP_THRESHOLD_SECONDS.get(symbol, 5.0)
            if silent_for >= threshold and not self.in_gap.get(symbol, False):
                self.in_gap[symbol] = True
                self.gap_started_at[symbol] = last if last is not None else now
                if self.recording_active:
                    self._emit({"event": "gap_started", "symbol": symbol})

    def check_warmup(self) -> None:
        if self.recording_active:
            return
        now = time.monotonic()
        offenders = [s for s in self.symbols if self.in_gap.get(s, True)]
        if offenders:
            if self.all_clear_since is not None:
                print(f"[warmup reset] gap on: {offenders}", flush=True)
            self.all_clear_since = None
            return
        if self.all_clear_since is None:
            self.all_clear_since = now
        elif now - self.all_clear_since >= WARMUP_SECONDS:
            self.recording_active = True
            self.recording_started_at = datetime.now(UTC)
            self._emit(
                {
                    "event": "recording_started",
                    "note": f"all symbols clean for {WARMUP_SECONDS:.0f}s warm-up window",
                }
            )

    def write_heartbeat(self) -> None:
        if self.recording_active and self.recording_started_at is not None:
            recording_note = f" (started {self.recording_started_at.isoformat()})"
        else:
            recording_note = " (still warming up)"
        lines = [
            f"Shoonya WS quality diagnostic -- {datetime.now(UTC).isoformat()}",
            f"Recording active: {self.recording_active}{recording_note}",
            f"Total interruptions since recording started: {self.total_interruptions}",
            f"Total downtime since recording started: {self.total_downtime_seconds:.1f}s",
            "",
            "Per-symbol tick counts (last 60s / lifetime):",
        ]
        for symbol in self.symbols:
            lines.append(
                f"  {symbol}: {self.tick_count_since_heartbeat.get(symbol, 0)} / "
                f"{self.tick_count.get(symbol, 0)} "
                f"(currently {'IN GAP' if self.in_gap.get(symbol) else 'ok'})"
            )
            self.tick_count_since_heartbeat[symbol] = 0
        SUMMARY_FILE.write_text("\n".join(lines) + "\n")


def main() -> None:
    session = _load_session()
    rest = ShoonyaRestClient(session["api_host"], session["access_token"])
    tokens = _resolve_index_tokens(rest, session["uid"])
    print("Resolved tokens:", tokens)

    monitor = _QualityMonitor(list(tokens))

    client = ShoonyaWSClient(
        session["ws_host"],
        uid=session["uid"],
        actid=session["actid"],
        access_token=session["access_token"],
        on_tick=monitor.on_tick,
    )
    client.start()
    entries = [
        (symbol, exchange, token) for symbol, (exchange, token) in tokens.items()
    ]
    client.subscribe(entries)
    print(f"Subscribed: {entries}")
    print(
        f"Warming up -- need {WARMUP_SECONDS:.0f}s with zero gaps exceeding each "
        f"symbol's own threshold ({GAP_THRESHOLD_SECONDS}) before official recording starts."
    )

    last_heartbeat = time.monotonic()
    try:
        while True:
            time.sleep(1.0)
            monitor.check_gaps()
            monitor.check_warmup()
            if time.monotonic() - last_heartbeat >= HEARTBEAT_SECONDS:
                monitor.write_heartbeat()
                last_heartbeat = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        client.stop()
        monitor.write_heartbeat()
        monitor.events_fh.close()
        print("Stopped.")


if __name__ == "__main__":
    main()
