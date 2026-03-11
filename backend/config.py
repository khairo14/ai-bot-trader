from pydantic_settings import BaseSettings
from pydantic import model_validator
from typing import List
import os
from loguru import logger

# Resolve .env relative to this file so it works regardless of CWD
_ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")

class Settings(BaseSettings):
    # Application
    secret_key: str = "change_this"
    debug: bool = False
    log_level: str = "INFO"

    # Database
    database_url: str = "postgresql://trader:password@db:5432/ai_trader"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # Binance
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = True
    # Binance testnet (paper) keys — only needed when binance_testnet=True
    binance_api_key_testnet: str = ""
    binance_api_secret_testnet: str = ""

    # HTTP proxy (e.g. for local VPN: http://127.0.0.1:port — leave blank on VPS)
    http_proxy: str = ""

    # Alpaca
    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_data_feed: str = "iex"
    # Alpaca live credentials (only needed when switching to live mode)
    alpaca_api_key_live: str = ""
    alpaca_api_secret_live: str = ""
    alpaca_base_url_live: str = "https://api.alpaca.markets"

    # IBKR
    ibkr_host: str = "host.docker.internal"
    ibkr_port: int = 7497
    ibkr_port_live: int = 7496
    ibkr_client_id: int = 1          # FastAPI (web process)
    ibkr_client_id_celery: int = 2   # Celery worker — must differ from ibkr_client_id
    ibkr_paper: bool = True

    # Paper trading
    paper_initial_balance: float = 10000.0

    # Risk management
    risk_per_trade_pct: float = 2.0
    max_open_positions: int = 5
    daily_circuit_breaker_pct: float = 5.0
    max_daily_loss_per_strategy_pct: float = 5.0
    max_consecutive_losses: int = 3
    max_exposure_per_asset_pct: float = 15.0
    max_exposure_per_class_pct: float = 40.0
    default_rr_ratio: float = 2.0
    atr_stop_multiplier: float = 2.0

    # Forward test auto-scheduler toggle
    # Set to 0 to disable (manual "Run Now" only); any non-zero value enables it.
    # The actual run interval per strategy is derived from its own timeframe setting
    # (1m strat → runs every 60 s, 1h strat → every 3600 s, 1d → every 86400 s).
    forward_test_interval_minutes: int = 1  # non-zero = enabled

    # BUG-3 FIX: Daily P&L counter reset hour (UTC). Default 0 = midnight UTC.
    # US traders may prefer 22 (10 PM UTC = 5 PM ET) so the counter resets at
    # end-of-NYSE-session instead of 7 PM ET (=midnight UTC).
    daily_reset_hour_utc: int = 0  # 0–23

    # Internal API — used by Celery workers to call back into the FastAPI server
    # (e.g. flush MLScorer cache after weekly retrain).
    # In Docker Compose this must point at the service name, not 127.0.0.1.
    api_internal_url: str = "http://backend:8000"

    # CORS
    cors_origins: List[str] = ["http://localhost:3000", "http://localhost:5173"]

    # Internal security
    # Random secret shared between Celery workers and FastAPI for /internal/* endpoints.
    # Generate with: python -c "import secrets; print(secrets.token_hex(32))"
    internal_api_secret: str = ""

    # Cookie security — set to True only when serving over HTTPS (e.g. a VPS with TLS).
    # Must be False for local Docker/HTTP setups; Secure cookies are silently dropped
    # by browsers on plain HTTP connections, which breaks the login flow.
    cookie_secure: bool = False

    # OpenAI (optional — not used by core trading engine; reserved for future LLM features)
    openai_api_key: str = ""

    # Gmail notifications
    gmail_user: str = ""                  # your Gmail address
    gmail_app_password: str = ""          # 16-char App Password from Google
    notify_email_to: str = ""             # recipient (defaults to gmail_user if blank)
    notify_email_enabled: bool = False    # set to true to actually send emails

    class Config:
        env_file = _ENV_FILE
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"

    @model_validator(mode="after")
    def _warn_missing_secrets(self) -> "Settings":
        """Abort startup if SECRET_KEY is the default; warn on missing broker keys."""
        _INSECURE_DEFAULTS = {"change_this", "change_this_to_a_random_64_char_string", ""}
        if self.secret_key.lower() in _INSECURE_DEFAULTS:
            raise ValueError(
                "SECRET_KEY is set to an insecure default. "
                "Generate a real key: python -c \"import secrets; print(secrets.token_hex(32))\" "
                "and set it in your .env file."
            )

        # ── Live-mode broker key enforcement ────────────────────────────────
        # Raise (not just warn) if a broker is in LIVE mode without real credentials.
        if not self.binance_testnet:
            if not self.binance_api_key or not self.binance_api_secret:
                raise ValueError(
                    "BINANCE_TESTNET=false but BINANCE_API_KEY / BINANCE_API_SECRET are empty. "
                    "Set live credentials or switch back to testnet mode."
                )
        _alpaca_is_live = "paper-api" not in self.alpaca_base_url
        if _alpaca_is_live:
            if not self.alpaca_api_key_live or not self.alpaca_api_secret_live:
                raise ValueError(
                    "Alpaca live URL configured but ALPACA_API_KEY_LIVE / ALPACA_API_SECRET_LIVE "
                    "are empty. Set live credentials or switch back to paper mode."
                )

        warnings = []
        if not self.binance_api_key:
            warnings.append("BINANCE_API_KEY")
        if not self.alpaca_api_key:
            warnings.append("ALPACA_API_KEY")
        if not self.alpaca_api_secret:
            warnings.append("ALPACA_API_SECRET")
        if self.database_url in ("postgresql://trader:password@db:5432/ai_trader", ""):
            warnings.append("DATABASE_URL (still at default)")

        # ── F-058: Reject CORS wildcard in production ───────────────────
        if "*" in self.cors_origins and not self.debug:
            raise ValueError(
                "CORS_ORIGINS contains '*' but DEBUG=false. "
                "Set CORS_ORIGINS to an explicit list of allowed origins for production."
            )

        if warnings:
            logger.warning(
                f"[Config] Missing or default secrets detected: {', '.join(warnings)}. "
                "Update your .env file."
            )

        # GAP-6 FIX: warn if internal_api_secret is empty — /internal/* endpoints
        # are callable by anyone on the network without authentication.
        if not self.internal_api_secret:
            logger.warning(
                "[Config] INTERNAL_API_SECRET is empty — /internal/* endpoints are "
                "unprotected. Generate a secret: "
                "python -c \"import secrets; print(secrets.token_hex(32))\" and set it in .env."
            )

        # GAP-7 FIX: warn if cookie_secure is False in a context that looks like
        # production (DEBUG=False). Secure cookies are silently dropped by browsers
        # over plain HTTP, but on HTTPS/VPS this setting must be True.
        if not self.cookie_secure and not self.debug:
            logger.warning(
                "[Config] COOKIE_SECURE=False but DEBUG=False — if this instance is "
                "served over HTTPS (e.g. a VPS with TLS), set COOKIE_SECURE=True in "
                ".env or login sessions will be silently dropped by browsers."
            )

        # ── IBKR client ID collision (Error 326) check ─────────────────
        if self.ibkr_client_id == self.ibkr_client_id_celery:
            raise ValueError(
                f"IBKR_CLIENT_ID and IBKR_CLIENT_ID_CELERY must be different "
                f"(both are {self.ibkr_client_id}). IB Gateway rejects duplicate "
                f"client IDs with Error 326."
            )

        return self


settings = Settings()
