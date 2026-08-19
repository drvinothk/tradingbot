"""Standalone, out-of-process Angel One WS quality monitor — 2026-08-19.

Same purpose and shape as `shoonya_ws_quality_diagnostic.py` (that script's
own docstring explains the general design; this one only documents what's
different for Angel One), built to test tonight's two fixes
(`angel_ws_client.py`'s `_on_close` arity patch and the literal text-"ping"
heartbeat) under real market conditions without touching the live app —
`MARKET_DATA_PROVIDER` stays whatever it's set to (TrueData, per project
memory), this script never imports or drives any of that.

**Completely isolated, zero DB dependency** — unlike the Shoonya script
(which reuses the app's own already-live session specifically to avoid a
second, possibly session-invalidating login), Angel One's login
(`loginByPassword` + TOTP) is fully programmatic and, since Angel is not
today's active `MARKET_DATA_PROVIDER`, there is no live Angel session for a
second login to conflict with — a genuinely fresh, independent login here
is safe. Index tokens (NIFTY/BANKNIFTY) are resolved directly from Angel's
own public scrip-master file (`ScripMasterService.fetch_and_parse` — a
plain HTTP download, no DB) rather than the DB-backed `broker_symbol_map`
`get_angel_token` normally falls back to — this script never opens a DB
session at all, not even a read.

**Connectivity probe before the real run** — user-requested: rule out
"we're currently stuck at Angel's 3-connections-per-client-code cap" (a
theory de-prioritized after research into the cap's own real failure mode
— a clean, immediate HTTP 429 at handshake, not a hang — but worth
settling empirically rather than assumed) and make sure this script's own
connection lifecycle is clean (exercising tonight's `_on_close` fix) before
committing to a long unattended run. Connects, waits `PROBE_SECONDS`,
disconnects cleanly, and aborts with a clear message if that doesn't
succeed — a long run that can't even complete one clean connect/disconnect
cycle first would just spin uselessly.

Usage:
    .venv/bin/python scripts/angel_ws_quality_diagnostic.py

Writes two files next to this script's own directory:
    - angel_ws_quality_events.jsonl  (one JSON object per line, live-appended)
    - angel_ws_quality_summary.txt   (rewritten every heartbeat)

Runs until killed (Ctrl-C, or `kill` if backgrounded with nohup) — same
"start it, dry-run briefly, then leave it running for the rest of the
session" workflow already used for the Shoonya diagnostic.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyotp  # noqa: E402

from app.config.settings import AngelOneSettings  # noqa: E402
from app.modules.broker_adapter.base.contracts import Tick  # noqa: E402
from app.modules.market_data.providers.angel_rest_client import AngelOneRestClient  # noqa: E402
from app.modules.market_data.providers.angel_ws_client import (  # noqa: E402
    EXCHANGE_TYPE_NSE_CM,
    AngelWSClient,
    RawAngelTick,
)
from app.modules.market_data.scrip_master import ScripMasterService  # noqa: E402

EVENTS_FILE = Path(__file__).resolve().parent / "angel_ws_quality_events.jsonl"
SUMMARY_FILE = Path(__file__).resolve().parent / "angel_ws_quality_summary.txt"

# Same reasoning as shoonya_ws_quality_diagnostic.py's own GAP_THRESHOLD_
# SECONDS: liquid index feeds tick multiple times a second during real
# market hours -- a gap this long is already suspicious. Angel doesn't
# stream India VIX today (scrip_master.py's own _TRACKED_UNDERLYINGS has no
# VIX entry -- out of scope here, not a bug), so only the two tradable
# underlyings are watched.
GAP_THRESHOLD_SECONDS = {"NIFTY": 5.0, "BANKNIFTY": 5.0}
WARMUP_SECONDS = 120.0
HEARTBEAT_SECONDS = 60.0

# 2026-08-19, user-requested: a short clean connect/disconnect first,
# before the real run -- see module docstring.
PROBE_SECONDS = 10.0


def _login(settings: AngelOneSettings) -> tuple[AngelOneRestClient, str, str]:
    rest = AngelOneRestClient(
        settings.rest_host,
        api_key=settings.api_key,
        mac_address=settings.resolved_mac_address(),
        auth_proxy=settings.auth_proxy,
    )
    totp = pyotp.TOTP(settings.totp_secret.get_secret_value()).now()
    data = rest.login_by_password(settings.client_code, settings.password.get_secret_value(), totp)
    return rest, data["jwtToken"], data["feedToken"]


def _resolve_index_tokens(symbols: list[str]) -> dict[str, str]:
    """Direct from Angel's own scrip-master file (`fetch_and_parse`, no DB)
    -- index rows (`kind="index"`) are present under exchange segment NSE
    for each tracked underlying. Raises with a clear message (not a silent
    empty dict) if a requested symbol isn't found -- same "fail loud on a
    resolution gap" discipline as the Shoonya diagnostic's own
    `_resolve_index_tokens`.
    """
    scrip_master = ScripMasterService()
    try:
        row_count = scrip_master.fetch_and_parse()
        print(f"Scrip master: {row_count} tracked rows parsed")
        resolved: dict[str, str] = {}
        for symbol in symbols:
            row = scrip_master._rows_by_key.get((symbol, "index", None, None, None))  # noqa: SLF001
            if row is None:
                raise SystemExit(
                    f"No NSE index row found for {symbol!r} in the Angel scrip master "
                    f"-- see scrip_master.py's own module docstring, 'index row' caveat"
                )
            resolved[symbol] = row.token
        return resolved
    finally:
        scrip_master.close()


class _QualityMonitor:
    """Verbatim copy of shoonya_ws_quality_diagnostic.py's own
    `_QualityMonitor` -- kept as two separate copies rather than a shared
    module, matching this project's own "don't force a shared helper
    across files that might reasonably diverge" precedent (e.g.
    structure_level/touch_and_confirm in strategy_engine) -- these two
    scripts already differ in how they resolve tokens and log in, and are
    each meant to be read and understood standalone.
    """

    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols
        self.last_tick_at: dict[str, float] = {}
        self.in_gap: dict[str, bool] = dict.fromkeys(symbols, True)
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
        self.tick_count_since_heartbeat[symbol] = self.tick_count_since_heartbeat.get(symbol, 0) + 1
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
            f"Angel One WS quality diagnostic -- {datetime.now(UTC).isoformat()}",
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


def _run_connectivity_probe(settings: AngelOneSettings, token_by_symbol: dict[str, str]) -> None:
    """See module docstring. Raises SystemExit with a clear message on
    failure rather than proceeding into the real run."""
    print(f"Connectivity probe: connecting via proxy={bool(settings.auth_proxy)} ...")
    rest, jwt_token, feed_token = _login(settings)
    probe_ticks: list[RawAngelTick] = []
    client = AngelWSClient(
        auth_token=jwt_token,
        api_key=settings.api_key,
        client_code=settings.client_code,
        feed_token=feed_token,
        on_tick=probe_ticks.append,
        proxy_url=settings.auth_proxy,
    )
    try:
        client.start()
        if not client._connected.is_set():  # noqa: SLF001
            raise SystemExit(
                "Connectivity probe FAILED: connection did not open within the client's own "
                "10s timeout -- check logs above for the real error (a 429 here would mean "
                "the 3-connections-per-client-code cap is genuinely the blocker)."
            )
        entries = [(token, EXCHANGE_TYPE_NSE_CM) for token in token_by_symbol.values()]
        client.subscribe(entries)
        print(
            f"Connectivity probe: connected and subscribed, watching for {PROBE_SECONDS:.0f}s ..."
        )
        time.sleep(PROBE_SECONDS)
    finally:
        client.stop()
        rest.close()
    print(
        f"Connectivity probe: OK -- clean connect/subscribe/disconnect cycle completed "
        f"({len(probe_ticks)} tick(s) received during the {PROBE_SECONDS:.0f}s probe window; "
        f"0 is expected outside market hours)."
    )


def main() -> None:
    settings = AngelOneSettings()  # type: ignore[call-arg]
    token_by_symbol = _resolve_index_tokens(["NIFTY", "BANKNIFTY"])
    print("Resolved tokens:", token_by_symbol)
    symbol_by_token = {token: symbol for symbol, token in token_by_symbol.items()}

    _run_connectivity_probe(settings, token_by_symbol)

    monitor = _QualityMonitor(list(token_by_symbol))

    def on_raw_tick(raw: RawAngelTick) -> None:
        symbol = symbol_by_token.get(raw.token)
        if symbol is None:
            return
        monitor.on_tick(
            Tick(
                contract_symbol=symbol,
                ltp=raw.ltp,
                bid=raw.bid,
                ask=raw.ask,
                volume=raw.volume,
                oi=raw.oi,
                ts=raw.ts,
            )
        )

    rest, jwt_token, feed_token = _login(settings)
    client = AngelWSClient(
        auth_token=jwt_token,
        api_key=settings.api_key,
        client_code=settings.client_code,
        feed_token=feed_token,
        on_tick=on_raw_tick,
        proxy_url=settings.auth_proxy,
    )
    client.start()
    entries = [(token, EXCHANGE_TYPE_NSE_CM) for token in token_by_symbol.values()]
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
        rest.close()
        monitor.write_heartbeat()
        monitor.events_fh.close()
        print("Stopped.")


if __name__ == "__main__":
    main()
