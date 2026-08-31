"""Environment-driven settings. One class per concern, aggregated into `Settings`.

Shoonya credentials are loaded from `config/credentials/shoonya.env` (gitignored,
never the tracked `.env`) so a broker secret can never end up committed by accident
even if someone edits the wrong file.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_DIR = Path(__file__).resolve().parent
CREDENTIALS_DIR = CONFIG_DIR / "credentials"
ENVIRONMENTS_DIR = CONFIG_DIR / "environments"
BACKEND_ROOT_DIR = CONFIG_DIR.parent.parent
DOTENV_PATH = BACKEND_ROOT_DIR / ".env"


class DBSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DB_", env_file=DOTENV_PATH, extra="ignore")

    host: str = "localhost"
    port: int = 5432
    name: str = "trading_bot"
    user: str = "trading_bot"
    password: SecretStr = SecretStr("")
    # 2026-08-31: raised 5 -> 20 (+ new max_overflow, was an unset implicit
    # 10) after a live incident traced a whole-app hang during a volatile
    # market spike to this pool, not to CPU/RAM (both were idle). Root
    # cause: dispatch_trade_intent/close_position hold LOCK_EXECUTION_
    # SINGLETON (core/locking.py) -- a transaction-scoped Postgres advisory
    # lock, by design, to prevent duplicate live orders -- for the full
    # duration of the blocking broker place_order() HTTP call. Every other
    # thread waiting on that same lock (up to LOCK_ACQUIRE_TIMEOUT=10s) is
    # *already holding a pooled connection* while it waits. A burst of
    # several strategies firing/exiting near-simultaneously (5 concurrent
    # conviction-gated strategies today) could tie up most/all of the old
    # 15-connection ceiling (5 + implicit max_overflow=10), starving the
    # FastAPI dashboard's own get_db()-backed requests on the same pool --
    # that's what made the *whole app* look hung, not just order dispatch.
    # Postgres itself allows max_connections=100 (checked live on the OCI
    # box), so 20+20=40 has ample headroom. This does not change the
    # serialization itself -- orders still dispatch strictly one-at-a-time,
    # exactly as LOCK_EXECUTION_SINGLETON requires -- it only stops that
    # expected queuing from starving unrelated read-only requests.
    pool_size: int = 20
    max_overflow: int = 20

    @property
    def sqlalchemy_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.user}:{self.password.get_secret_value()}"
            f"@{self.host}:{self.port}/{self.name}"
        )


class RedisSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REDIS_", env_file=DOTENV_PATH, extra="ignore")

    host: str = "localhost"
    port: int = 6379
    db: int = 0

    @property
    def url(self) -> str:
        return f"redis://{self.host}:{self.port}/{self.db}"


class ShoonyaSettings(BaseSettings):
    """Loaded from config/credentials/shoonya.env — corrected after live-checking
    shoonya.com/api-documentation directly (Phase 0's earlier conclusion, based
    only on the older ShoonyaApi-py GitHub README, was wrong): Shoonya's current
    API uses a genuine OAuth-style browser redirect, not a direct TOTP-only REST
    call. Flow: build an authorize URL from `oauth_authorize_url` + client_id +
    redirect_url -> open it in a browser -> user logs in on Shoonya's own site
    (User ID + password + OTP/TOTP) -> Shoonya redirects the browser back to
    `redirect_url` with a `code` query param -> POST {code, checksum=SHA256(
    client_id+secret_code+code)} to `{api_host}/GenAcsTok` for the access token.
    `redirect_url` therefore genuinely needs to be reachable — but only by the
    user's own browser on this machine, since the redirect happens client-side,
    so http://127.0.0.1:... is fine and does not need to be internet-facing.
    """

    model_config = SettingsConfigDict(
        env_prefix="SHOONYA_",
        env_file=CREDENTIALS_DIR / "shoonya.env",
        extra="ignore",
    )

    client_id: str = ""
    secret_code: SecretStr = SecretStr("")
    user_id: str = ""
    redirect_url: str = "http://127.0.0.1:5000/shoonya/callback"
    # **CONFIRMED, 2026-08-20** — the `NorenWClientTP`/`NorenWSTP` discrepancy
    # this comment used to flag (a Phase 5 research spike found the official
    # Shoonya-Dev GitHub org's own wrapper hardcoding those paths instead)
    # is resolved: real, successful `GenAcsTok`/`PlaceOrder`/`PositionBook`
    # calls against this actual account, live-logged all day 2026-08-20
    # including the first genuine live order to reach the broker, all went
    # through `NorenWClientAPI` with clean HTTP 200 responses. This is now a
    # primary-source-confirmed value, not a researched guess.
    api_host: str = "https://api.shoonya.com/NorenWClientAPI"
    ws_host: str = "wss://api.shoonya.com/NorenWSAPI/"
    oauth_authorize_url: str = "https://api.shoonya.com/OAuthlogin/authorize/oauth"
    # 2026-08-11: WS auth handshake root-caused and fixed (see ws_client.py's
    # own docstring) — the real bug was the connect-message shape, not this
    # value; "API" (the classic-QuickAuth convention) was confirmed correct.
    # Left configurable rather than hardcoded, so a future auth regression
    # can still be worked around via env var alone, no redeploy needed.
    ws_auth_source: str = "API"

    def missing_required_fields(self) -> list[str]:
        """Fields the OAuth login (`build_authorize_url`) + token exchange
        (`exchange_code_for_token`) + adapter construction actually consume —
        checked here instead of at `Settings()` construction time, since
        every test and every paper-only local run constructs `ShoonyaSettings()`
        regardless of whether Shoonya is ever used, and must not start failing
        just because no `shoonya.env` exists yet.
        """
        missing = []
        if not self.client_id:
            missing.append("SHOONYA_CLIENT_ID")
        if not self.secret_code.get_secret_value():
            missing.append("SHOONYA_SECRET_CODE")
        if not self.user_id:
            missing.append("SHOONYA_USER_ID")
        if not self.redirect_url:
            missing.append("SHOONYA_REDIRECT_URL")
        return missing


class MarketDataSettings(BaseSettings):
    """Which live-tick provider `market_data.provider_composition.get_market_data_provider()`
    resolves to — independent of `ShoonyaSettings`/execution entirely (see that
    module's own docstring for why market data and execution are two separate
    ports). `"mock"` is the safe default so every test and local dev run
    behaves exactly as before this existed, unless explicitly opted in.
    """

    model_config = SettingsConfigDict(
        env_prefix="MARKET_DATA_", env_file=DOTENV_PATH, extra="ignore"
    )

    provider: str = "mock"  # "angel_one" | "shoonya" | "truedata" | "alice_blue" | "mock"
    # Off by default: the 08:30-16:00 IST market-hours gate
    # (market_data.market_hours / MarketHoursGatedProvider) applies to
    # whichever real provider is selected. Set MARKET_DATA_ALLOW_OFFHOURS_
    # TESTING=true for local/dev sessions that need to exercise a real
    # provider outside those hours (e.g. testing a fix at night) without
    # disabling the gate for everyone. Never applies to "mock" — that
    # provider is never wrapped by the gate at all, see
    # provider_composition.get_market_data_provider's own docstring.
    allow_offhours_testing: bool = False
    # A deliberately *bounded* alternative to allow_offhours_testing, not a
    # duplicate of it -- that flag removes the cutoff entirely (any time of
    # day or night); this one keeps a real hard stop, just a later one
    # (23:30 IST instead of 16:00), for exactly one scoped use case:
    # TrueData's aftermarket "Full Market Feed Replay" server, which streams
    # a prior real trading day back as though it were live in the evening.
    # Off by default for the identical reason allow_offhours_testing's own
    # docstring already gives -- never set this in the tracked .env, only
    # via a scoped systemctl set-environment on a live box or an inline env
    # var locally, for the one evening session that actually needs it.
    is_replay_mode: bool = False
    # Wraps the primary provider (whatever `provider` above resolves to) in
    # FailoverMarketDataProvider, backed by `failover_backup_provider` --
    # see that class's own docstring for the 5s-trip/90s-anti-flap-recovery
    # state machine. Off by default: zero behavior change for every existing
    # test/local/live run unless explicitly opted in, same discipline as
    # every other flag in this class. Ignored entirely when provider ==
    # "mock" (matches every other gate's mock exclusion).
    failover_enabled: bool = False
    # "alice_blue" is the standing default as of 2026-08-25 -- promoted as
    # Shoonya's failover backup after a real multi-hour live comparison (see
    # provider_composition._RECOGNIZED_FAILOVER_BACKUPS's own comment).
    # "angel_one" also remains a recognized value (archived, not removed --
    # see CLAUDE.md's Angel One section) for whoever reactivates it later.
    # TrueData is a deliberately deferred scope call, not yet live-tested as
    # a failover backup. get_market_data_provider validates this is
    # recognized, not "mock", and not equal to `provider` itself; a bad
    # value fails loud rather than silently resolving to a single-provider
    # setup with failover quietly inert.
    failover_backup_provider: str = "alice_blue"
    # How long the primary may go without a tick before failing over. Raised
    # 5.0 -> 10.0 on 2026-08-25, before ever enabling failover live, per
    # explicit user judgment: both Shoonya and Alice Blue are brokers first,
    # not dedicated market-data vendors, and the real comparison data
    # (CLAUDE.md's 2026-08-25 entry -- Shoonya alone saw 86 short drops in a
    # single day) shows brief broker-side reconnect blips are routine, not
    # exceptional. 10s rides those out without flapping while staying far
    # below the seconds-to-minutes cadence any strategy actually acts on.
    failover_threshold_seconds: float = 10.0
    # How long the primary must stream continuously-healthy ticks again
    # before failover switches back -- anti-flap, same reasoning as above.
    failover_recovery_stabilization_seconds: float = 90.0
    # Backoff between retrying a *failed* backup subscribe attempt (e.g. an
    # Angel One login failure) -- deliberately much longer than the 1s
    # watchdog poll interval so a real backup outage doesn't hammer a
    # failing login endpoint every cycle.
    failover_backup_retry_seconds: float = 30.0


class AngelOneSettings(BaseSettings):
    """Loaded from config/credentials/angel_one.env (gitignored, never the
    tracked .env — same secrets discipline as ShoonyaSettings). Endpoint/
    payload details are from the user-supplied Angel One SmartAPI doc
    extraction (2026-08), not independently re-verified against a live
    account yet — see AngelOneMarketDataProvider's own docstring for exactly
    what's confirmed vs. still an assumption.

    Unlike Shoonya (whose TOTP entry happens on Shoonya's own login page in
    the user's browser, never read by this backend at all), `totp_secret`
    here is genuinely read and used: Angel's `loginByPassword` is a direct
    server-to-server REST call requiring a live TOTP code in the request
    body, so this backend generates it itself via `pyotp.TOTP(...).now()`.
    """

    model_config = SettingsConfigDict(
        env_prefix="ANGELONE_",
        env_file=CREDENTIALS_DIR / "angel_one.env",
        extra="ignore",
    )

    api_key: str = ""
    client_code: str = ""
    password: SecretStr = SecretStr("")
    totp_secret: SecretStr = SecretStr("")
    # uuid.getnode() default is a real MAC, but not necessarily the one
    # Angel's account/session expects registered — overridable via env with
    # no redeploy, same "configurable, not hardcoded" pattern as
    # ShoonyaSettings.ws_auth_source.
    mac_address: str = ""
    rest_host: str = "https://apiconnect.angelone.in"
    ws_host: str = "wss://smartapisocket.angelone.in/smart-stream"
    # Two known URLs for the scrip master file (a domain rebrand,
    # angelbroking.com -> angelone.in) — see scrip_master.py's own module
    # docstring for the "flagged, not silently picked" treatment.
    scrip_master_url: str = (
        "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
    )
    scrip_master_url_fallback: str = (
        "https://margincalculator.angelbroking.com/OpenAPI_MasterData/OpenAPIScripMaster.json"
    )
    # Empty by default: the login REST call goes direct, same as every other
    # outbound call in this codebase. Live-confirmed 2026-08-05 (an A/B test —
    # apiconnect.angelone.in times out from the OCI VM's IP but responds
    # instantly to the identical request from an unrelated residential IP,
    # even though Angel's own *public* scrip-master endpoint answers fine
    # from the OCI IP) that Angel's authenticated gateway specifically
    # rejects/drops that IP — set this to an HTTP(S) proxy URL
    # (`http://host:port` or `https://user:pass@host:port`) you control to
    # route just the login call through a different egress IP. Never point
    # this at a public/third-party proxy: whoever runs it can see the
    # plaintext login payload (API key, client code, password, TOTP code).
    # The WebSocket connection (angel_ws_client.py) deliberately never reads
    # this — it must stay a direct connection for latency, and SmartStream
    # wasn't the endpoint that failed anyway.
    auth_proxy: str = ""

    def missing_required_fields(self) -> list[str]:
        missing = []
        if not self.api_key:
            missing.append("ANGELONE_API_KEY")
        if not self.client_code:
            missing.append("ANGELONE_CLIENT_CODE")
        if not self.password.get_secret_value():
            missing.append("ANGELONE_PASSWORD")
        if not self.totp_secret.get_secret_value():
            missing.append("ANGELONE_TOTP_SECRET")
        return missing

    def resolved_mac_address(self) -> str:
        """`uuid.getnode()`, formatted as a colon-separated MAC string, when
        `mac_address` isn't explicitly configured. Local import: this is the
        only place in `AngelOneSettings` that needs it, and importing `uuid`
        at module scope for one rarely-called method isn't worth it.
        """
        if self.mac_address:
            return self.mac_address
        import uuid as _uuid

        node = _uuid.getnode()
        return ":".join(f"{(node >> shift) & 0xFF:02X}" for shift in range(40, -8, -8))


class AliceBlueSettings(BaseSettings):
    """Loaded from config/credentials/alice_blue.env (gitignored, same secret
    discipline as ShoonyaSettings/AngelOneSettings/TrueDataSettings).

    Market-data-only, per explicit user decision 2026-08-21 -- Shoonya stays
    the execution broker, untouched. Auth is a browser-redirect OAuth-style
    flow (App Code + a human login on Alice Blue's own site), **not** Angel
    One's direct server-to-server password+TOTP login -- confirmed live
    2026-08-21 against Alice Blue's own official ANT V3 docs
    (ant.aliceblueonline.com/productdocumentation), not inferred from an
    older `pya3`-style wrapper (the docs explicitly warn those may target an
    earlier API generation). Confirmed flow:

    1. Browser -> `{authorize_base_url}?appcode={app_code}` (no redirect_uri
       param -- Alice's docs show only `appcode`, presumably because the
       redirect URL is already tied to the App Code at registration time,
       same as this account's own registered
       `https://68-233-110-76.sslip.io/aliceblue/callback`).
    2. User logs in with their own Alice Blue credentials on Alice's site
       (this backend never sees that password -- same reasoning
       ShoonyaSettings' own docstring gives for its OAuth flow).
    3. Browser redirected back to `redirect_url` with `authCode` + `userId`
       query params.
    4. checksum = SHA256(userId + authCode + api_secret) (hex digest,
       plain concatenation -- same "concatenate then hex-digest" convention
       `ShoonyaSettings`' own `GenAcsTok` checksum uses, confirmed
       independently for Alice Blue by its own docs).
    5. POST `{"checkSum": "<checksum>"}` (confirmed: no userId/authCode in
       the body itself) to `{api_host}/open-api/od/v1/vendor/getUserDetails`
       -> `{"stat": "Ok", "clientId": ..., "userSession": <JWT>}`.

    **Unconfirmed, flagged rather than silently assumed** (Alice's own docs
    didn't show a worked REST example): the exact `Authorization` header
    shape for every *later* REST call using `userSession` -- see
    `alice_blue_rest_client.py`'s own docstring for the hedge applied and
    what to check first if a call 401s. The WebSocket connect frame *is*
    confirmed (`Websocket/` doc page, matches Shoonya's own Noren-family
    protocol almost exactly): `susertoken` is
    `sha256(sha256(userSession))`, not the raw session -- see
    `alice_blue_ws_client.py`.

    Contract-master download (`contract_master_*_url`) is confirmed live
    2026-08-21 by downloading and inspecting the real file directly (same
    "public, no-auth, daily-updated" discipline as Shoonya's own
    `NFO_symbols.txt.zip` pipeline) -- real fields: `symbol`, `option_type`,
    `expiry_date` (epoch *milliseconds*), `token`, `trading_symbol`,
    `exch`/`exchange_segment`, `lot_size`, `strike_price`, `tick_size`. The
    legacy (pre-V2) contract-master download was discontinued 2025-11-30
    per Alice's own docs -- only the V2 JSON endpoints below are current.
    """

    model_config = SettingsConfigDict(
        env_prefix="ALICEBLUE_",
        env_file=CREDENTIALS_DIR / "alice_blue.env",
        extra="ignore",
    )

    app_code: str = ""
    api_secret: SecretStr = SecretStr("")
    redirect_url: str = ""
    authorize_base_url: str = "https://ant.aliceblueonline.com/"
    api_host: str = "https://a3.aliceblueonline.com"
    # 2026-08-21, live-confirmed: the bare (no trailing slash) URL 301s at
    # the plain-HTTP level (nginx: "Location: .../NorenWS/") -- harmless
    # for a browser's own WS client (which follows this automatically) but
    # `websockets.sync.client.connect` doesn't follow redirects during the
    # handshake at all, so every connection attempt failed with
    # `InvalidStatus: 301` and silent reconnect-loop retries, never once
    # reaching authentication. Trailing slash added directly rather than
    # inferred from Alice Blue's own docs (which show it without one) --
    # confirmed by hitting the bare host directly from the OCI box.
    ws_host: str = "wss://ws1.aliceblueonline.com/NorenWS/"
    contract_master_nfo_url: str = (
        "https://v2api.aliceblueonline.com/restpy/static/contract_master/V2/NFO"
    )
    contract_master_nse_url: str = (
        "https://v2api.aliceblueonline.com/restpy/static/contract_master/V2/NSE"
    )
    # Same reasoning as AngelOneSettings.auth_proxy -- unset by default (this
    # account hasn't hit an IP block on Alice Blue's side yet, unlike Angel
    # One), but present so a future block doesn't need a code change, just an
    # env var.
    auth_proxy: str = ""

    def missing_required_fields(self) -> list[str]:
        missing = []
        if not self.app_code:
            missing.append("ALICEBLUE_APP_CODE")
        if not self.api_secret.get_secret_value():
            missing.append("ALICEBLUE_API_SECRET")
        if not self.redirect_url:
            missing.append("ALICEBLUE_REDIRECT_URL")
        return missing


class TrueDataSettings(BaseSettings):
    """Loaded from config/credentials/truedata.env (gitignored, never the
    tracked .env — same secrets discipline as ShoonyaSettings/
    AngelOneSettings). Was live/subscribed and exercised against a real
    account before the trial expired and it was archived 2026-08-25 (see
    CLAUDE.md) — credentials removed from the deployment, this class and
    `TrueDataMarketDataProvider` left fully intact, so reactivating is an
    env-var change plus restoring the credentials file, no code changes
    needed. The `TD_live(...)` constructor shape, `live_port`, and the
    `replay.truedata.in` aftermarket-replay switch are all sourced from
    directly reading the current official `truedata` PyPI package's own
    installed source (not a paraphrase, and not the now-superseded
    `truedata-ws` package this used to be built against) plus TrueData's
    own official WebSocket API spec — see `TrueDataProvider`'s own
    docstring for the full discrepancy writeup and exactly what's still
    confirmed vs. inferred vs. an open question.
    """

    model_config = SettingsConfigDict(
        env_prefix="TRUEDATA_",
        env_file=CREDENTIALS_DIR / "truedata.env",
        extra="ignore",
    )

    username: str = ""
    password: SecretStr = SecretStr("")
    # push.truedata.in is the real production feed. replay.truedata.in
    # streams a prior real trading day back as though it were live —
    # confirmed real via truedata-ws's own official README (not guessed),
    # explicitly intended for exactly this system's "test the paper-trading
    # loop after hours" use case. Never leave this pointed at replay in a
    # tracked .env — same "off by default, scoped override only" reasoning
    # MarketDataSettings.allow_offhours_testing's own docstring gives for a
    # different off-hours testing knob.
    url: str = "push.truedata.in"
    # 8084 = TrueData's own official spec's Production real-time WebSocket
    # port (Sandbox is 8086) -- corrected 2026-08-11 from the prior 8082
    # default, which came from truedata-ws's own README, not TrueData's own
    # documentation. See TrueDataProvider's own docstring for the full
    # discrepancy writeup before assuming this is the final word untested.
    live_port: int = 8084

    def missing_required_fields(self) -> list[str]:
        missing = []
        if not self.username:
            missing.append("TRUEDATA_USERNAME")
        if not self.password.get_secret_value():
            missing.append("TRUEDATA_PASSWORD")
        return missing


class RiskDefaults(BaseSettings):
    """System-default risk governance values — these seed `risk_limit_configs`
    on first run; day-to-day overrides live on `trading_sessions`, not here.
    """

    model_config = SettingsConfigDict(env_prefix="RISK_", env_file=DOTENV_PATH, extra="ignore")

    max_concurrent_positions: int = 2
    max_trades_per_day: int = 5
    consecutive_loss_pause_threshold: int = 2
    daily_loss_cap: float = 5000.0
    daily_target_profit: float = 5000.0
    per_trade_lot_cap: int = 1
    default_budget: float = 50000.0


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_", env_file=DOTENV_PATH, extra="ignore")

    env: str = Field(default="local", description="local | cloud")
    secret_key: SecretStr = SecretStr("dev-only-change-me")
    session_ttl_minutes: int = 60 * 12
    # Ops-Hardening Phase 5. Off by default -- never set in the tracked
    # .env, same "dangerous behavior needs explicit opt-in" discipline as
    # MARKET_DATA_ALLOW_OFFHOURS_TESTING. get_execution_broker refuses
    # (ConfigurationError) rather than silently falling back to paper if
    # session mode calls for live execution but this is False -- the whole
    # point is a missing/false flag must never be interpreted as "use
    # paper instead."
    allow_real_money_dispatch: bool = False
    # 2026-08-19: a genuinely live dispatch now sends order_type=LIMIT (not
    # MARKET -- Shoonya rejects MKT via API, live-confirmed:
    # "ALGO_CHK: MKT Order type not allowed for API order"), so it needs a
    # real, non-zero price buffer to tolerate LTP movement between decision
    # and placement and still fill. Deliberately separate from
    # PaperTradingSettings.fill_slippage_pct (which defaults to 0.0 and
    # exists for a different purpose -- realistic paper *fill simulation*,
    # not "will a real limit order actually execute"). 0.5% is a starting
    # point, not a researched figure -- tune via APP_LIVE_LIMIT_ORDER_
    # BUFFER_PCT once live fills are observed.
    live_limit_order_buffer_pct: float = 0.005
    # Weekend rest mode (see app.modules.ops.weekend_rest). On by default;
    # inert Mon-Fri IST regardless. APP_WEEKEND_REST_ENABLED=false is an
    # instant kill switch (settable via `systemctl set-environment` on the
    # OCI box with no redeploy, same escape-hatch shape as
    # MARKET_DATA_ALLOW_OFFHOURS_TESTING). On a Sat/Sun the system stays
    # awake only while a signed-in user is active, and for
    # weekend_rest_idle_minutes after their last authenticated request.
    weekend_rest_enabled: bool = True
    weekend_rest_idle_minutes: int = 10


class PaperTradingSettings(BaseSettings):
    """Paper-trade fill mechanics -- deliberately separate from RiskDefaults
    (governance limits, not fill simulation) and from MarketDataSettings
    (source of ticks, not what price mock fills execute at).
    """

    model_config = SettingsConfigDict(env_prefix="PAPER_", env_file=DOTENV_PATH, extra="ignore")

    # Applied unfavorably (worse than the reference price) on every mock
    # fill -- entries pay slightly more, exits receive slightly less -- so
    # paper P&L doesn't overstate real performance by assuming perfect
    # fills. 0.0 by default: zero behavior change (beyond the price-source
    # fix itself, which is not optional) for every existing test/deployment
    # unless explicitly configured. A small nonzero value (e.g. 0.001-0.005)
    # is recommended for realistic paper performance once this is
    # live-verified.
    fill_slippage_pct: float = 0.0


class TelegramSettings(BaseSettings):
    """Ops-Hardening Phase 2. Loaded from config/credentials/telegram.env
    (gitignored, same secret-isolation discipline as shoonya.env/angel_one.env)
    -- a bot token is a real credential, not app config. Both fields default
    to empty, which `app.modules.alerting.manager` treats as "Telegram not
    configured" and falls back to SystemAlert-only, not an error -- this
    system must work (paper trading, alerts written to the DB) with zero
    Telegram setup at all.
    """

    model_config = SettingsConfigDict(
        env_prefix="TELEGRAM_",
        env_file=CREDENTIALS_DIR / "telegram.env",
        extra="ignore",
    )

    bot_token: SecretStr = SecretStr("")
    chat_id: str = ""


class Settings:
    """Aggregate accessor — import `get_settings()`, not the sub-classes directly."""

    def __init__(self) -> None:
        self.app = AppSettings()
        self.db = DBSettings()
        self.redis = RedisSettings()
        self.shoonya = ShoonyaSettings()
        self.market_data = MarketDataSettings()
        self.angel_one = AngelOneSettings()
        self.alice_blue = AliceBlueSettings()
        self.truedata = TrueDataSettings()
        self.risk_defaults = RiskDefaults()
        self.paper_trading = PaperTradingSettings()
        self.telegram = TelegramSettings()


@lru_cache
def get_settings() -> Settings:
    return Settings()
