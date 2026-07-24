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
    pool_size: int = 5

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
    vendor_code: str = ""
    user_id: str = ""
    redirect_url: str = "http://127.0.0.1:5000/shoonya/callback"
    primary_ip: str = ""
    backup_ip: str = ""
    totp_secret: SecretStr = SecretStr("")
    api_host: str = "https://api.shoonya.com/NorenWClientAPI"
    ws_host: str = "wss://api.shoonya.com/NorenWSAPI/"
    oauth_authorize_url: str = "https://api.shoonya.com/OAuthlogin/authorize/oauth"


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


class Settings:
    """Aggregate accessor — import `get_settings()`, not the sub-classes directly."""

    def __init__(self) -> None:
        self.app = AppSettings()
        self.db = DBSettings()
        self.redis = RedisSettings()
        self.shoonya = ShoonyaSettings()
        self.risk_defaults = RiskDefaults()


@lru_cache
def get_settings() -> Settings:
    return Settings()
