"""Standalone, out-of-process Alice Blue WS data-flow diagnostic — 2026-08-28.

Same purpose and shape as `shoonya_ws_quality_diagnostic.py` /
`angel_ws_quality_diagnostic.py` (those docstrings explain the general
design; this one only documents what's different for Alice Blue).

**Completely isolated from the trading-bot app**: no DB session, no
`app.main`, no strategy/execution/ingestion code touched. Alice Blue is not
the active `MARKET_DATA_PROVIDER` anywhere today (Shoonya is on OCI, mock
locally), so there is no live Alice Blue WS connection for this one to
conflict with. It reuses the already-cached OAuth session on disk
(`config/credentials/.alice_blue_session_cache.json`, written by the app's
own `/aliceblue/callback`) via `get_alice_blue_session()` — it never logs
in, never writes the cache, never mutates any account state. The
`createWsSess` pre-connect call (required for the WS handshake — see
`alice_blue_auth.create_ws_session`) is a pure server-side WS-session
registration, the exact same call the real provider makes; it does not
disturb anything.

Subscribes to NIFTY 50 (NSE token 26000) and NIFTY BANK (NSE token 26009)
over a fresh, dedicated `AliceBlueWSClient` connection and watches tick
arrival timing to characterise real-world WS quality: how often it drops,
how long each drop lasts, and the steady-state tick rate in between.
Observation-only — infers interruptions purely from tick-arrival gaps, plus
reports the client's own `reconnect_count` at each heartbeat.

**Connectivity probe first** (like the Angel One script): connect + subscribe
+ watch briefly, abort with a clear message if a clean connect/subscribe
cycle can't complete — a dead cached session or a rejected WS auth should
fail fast, not spin for 45 minutes.

**Warm-up gate**: "official" recording only starts once every symbol has
gone `WARMUP_SECONDS` with zero gaps longer than `GAP_THRESHOLD_SECONDS`, so
the connect/subscribe settling period isn't counted as a spurious
interruption against Alice Blue's own WS quality.

Usage (from backend/):
    .venv/Scripts/python.exe scripts/alice_blue_ws_quality_diagnostic.py --until 15:15

`--until HH:MM` is an IST wall-clock stop time (default 15:15). The script
also stops on Ctrl-C / SIGTERM. Writes two files next to this script:
    - alice_blue_ws_quality_events.jsonl  (one JSON object per line, live-appended)
    - alice_blue_ws_quality_summary.txt   (rewritten on every heartbeat + at exit)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.settings import get_settings  # noqa: E402
from app.modules.broker_adapter.base.contracts import Tick  # noqa: E402
from app.modules.market_data.providers.alice_blue_auth import create_ws_session  # noqa: E402
from app.modules.market_data.providers.alice_blue_session import (  # noqa: E402
    get_alice_blue_session,
)
from app.modules.market_data.providers.alice_blue_ws_client import (  # noqa: E402
    AliceBlueWSClient,
)

IST = ZoneInfo("Asia/Kolkata")

EVENTS_FILE = Path(__file__).resolve().parent / "alice_blue_ws_quality_events.jsonl"
SUMMARY_FILE = Path(__file__).resolve().parent / "alice_blue_ws_quality_summary.txt"

# NSE-assigned index tokens — confirmed identical to Shoonya's in
# alice_blue_scrip_master.py's own docstring (Alice Blue's INDICES V2 file).
# (contract_symbol, exchange, token)
SUBSCRIPTIONS = [
    ("NIFTY", "NSE", "26000"),
    ("BANKNIFTY", "NSE", "26009"),
]

# Same reasoning as the Shoonya/Angel scripts: a liquid index feed ticks
# multiple times a second in real market hours, so a 5s silence is already
# suspicious rather than "a quiet moment."
GAP_THRESHOLD_SECONDS = {"NIFTY": 5.0, "BANKNIFTY": 5.0}
WARMUP_SECONDS = 120.0
HEARTBEAT_SECONDS = 60.0
PROBE_SECONDS = 10.0


class _QualityMonitor:
    """Kept as its own copy rather than a shared module — same precedent the
    Angel One script's own `_QualityMonitor` docstring records.
    """

    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols
        self.last_tick_at: dict[str, float] = {}
        self.in_gap: dict[str, bool] = dict.fromkeys(symbols, True)
        self.gap_started_at: dict[str, float] = dict.fromkeys(symbols, time.monotonic())
        self.tick_count: dict[str, int] = dict.fromkeys(symbols, 0)
        self.tick_count_since_heartbeat: dict[str, int] = dict.fromkeys(symbols, 0)
        self.last_ltp: dict[str, float | None] = dict.fromkeys(symbols, None)
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
        print(json.dumps(event), flush=True)

    def on_tick(self, tick: Tick) -> None:
        symbol = tick.contract_symbol
        now = time.monotonic()
        was_in_gap = self.in_gap.get(symbol, True)
        self.last_tick_at[symbol] = now
        self.last_ltp[symbol] = tick.ltp
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

    def write_heartbeat(self, reconnect_count: int, note: str = "") -> None:
        if self.recording_active and self.recording_started_at is not None:
            recording_note = f" (started {self.recording_started_at.isoformat()})"
        else:
            recording_note = " (still warming up)"
        lines = [
            f"Alice Blue WS quality diagnostic -- {datetime.now(UTC).isoformat()} "
            f"({datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')})",
            note or "",
            f"Client reconnect_count (re-connects, not the first): {reconnect_count}",
            f"Recording active: {self.recording_active}{recording_note}",
            f"Total interruptions since recording started: {self.total_interruptions}",
            f"Total downtime since recording started: {self.total_downtime_seconds:.1f}s",
            "",
            "Per-symbol tick counts (last 60s / lifetime) | last LTP | state:",
        ]
        for symbol in self.symbols:
            lines.append(
                f"  {symbol}: {self.tick_count_since_heartbeat.get(symbol, 0)} / "
                f"{self.tick_count.get(symbol, 0)} | "
                f"ltp={self.last_ltp.get(symbol)} | "
                f"{'IN GAP' if self.in_gap.get(symbol) else 'ok'}"
            )
            self.tick_count_since_heartbeat[symbol] = 0
        SUMMARY_FILE.write_text("\n".join(x for x in lines if x is not None) + "\n")


def _parse_until(value: str) -> datetime:
    try:
        hh, mm = (int(x) for x in value.split(":", 1))
    except ValueError as exc:  # noqa: TRY003
        raise SystemExit(f"--until must be HH:MM (IST), got {value!r}") from exc
    now_ist = datetime.now(IST)
    stop = now_ist.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if stop <= now_ist:
        raise SystemExit(
            f"--until {value} IST is already in the past (now {now_ist.strftime('%H:%M')} IST)."
        )
    return stop


def _run_connectivity_probe(client: AliceBlueWSClient) -> None:
    client.start()
    if not client._connected.is_set():  # noqa: SLF001
        client.stop()
        raise SystemExit(
            "Connectivity probe FAILED: WS did not connect+authenticate within 5s. "
            "Most likely the cached Alice Blue session is dead — re-connect via "
            "/aliceblue/login-url. Check the logged auth-attempt / ConnectionError lines above."
        )
    client.subscribe(SUBSCRIPTIONS)
    print(
        f"Connectivity probe: connected + subscribed, watching {PROBE_SECONDS:.0f}s "
        f"(0 ticks is normal outside market hours) ...",
        flush=True,
    )
    time.sleep(PROBE_SECONDS)
    print("Connectivity probe: OK — clean connect/subscribe cycle completed.", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--until", default="15:15", help="IST wall-clock stop time HH:MM (default 15:15)")
    args = parser.parse_args()
    stop_at = _parse_until(args.until)

    settings = get_settings().alice_blue
    session = get_alice_blue_session()
    if session is None:
        raise SystemExit(
            "No cached Alice Blue session on disk — a human must complete the browser "
            "login via /aliceblue/login-url first."
        )
    print(
        f"Using cached Alice Blue session: client_id={session.client_id!r}. "
        f"Will run until {stop_at.strftime('%Y-%m-%d %H:%M:%S IST')} "
        f"(~{(stop_at - datetime.now(IST)).total_seconds() / 60:.0f} min).",
        flush=True,
    )

    monitor = _QualityMonitor([s for s, _, _ in SUBSCRIPTIONS])

    client = AliceBlueWSClient(
        settings.ws_host,
        uid=f"{session.client_id}_API",
        actid=f"{session.client_id}_API",
        user_session=session.user_session,
        on_tick=monitor.on_tick,
        ensure_ws_session=lambda: create_ws_session(settings, session),
    )

    _run_connectivity_probe(client)
    print(
        f"Warming up -- need {WARMUP_SECONDS:.0f}s with zero gaps exceeding "
        f"{GAP_THRESHOLD_SECONDS} before official recording starts.",
        flush=True,
    )

    last_heartbeat = time.monotonic()
    stopped_reason = "reached --until stop time"
    try:
        while datetime.now(IST) < stop_at:
            time.sleep(1.0)
            monitor.check_gaps()
            monitor.check_warmup()
            if time.monotonic() - last_heartbeat >= HEARTBEAT_SECONDS:
                monitor.write_heartbeat(client.reconnect_count)
                last_heartbeat = time.monotonic()
    except KeyboardInterrupt:
        stopped_reason = "KeyboardInterrupt"
    finally:
        monitor.write_heartbeat(client.reconnect_count, note=f"FINAL — stopped: {stopped_reason}")
        client.stop()
        monitor._emit({"event": "stopped", "reason": stopped_reason})  # noqa: SLF001
        monitor.events_fh.close()
        print(f"Stopped ({stopped_reason}). Summary: {SUMMARY_FILE}", flush=True)


if __name__ == "__main__":
    main()
