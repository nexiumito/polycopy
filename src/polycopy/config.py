"""Configuration centralisée via Pydantic Settings.

Toutes les variables sont chargées depuis l'environnement (ou .env en dev).
Aucune valeur sensible en dur dans le code.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings du bot, validées au démarrage."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # --- Polymarket wallet ---
    polymarket_private_key: str = Field(..., description="Clé privée du wallet de signature")
    polymarket_funder: str = Field(..., description="Adresse du proxy wallet")
    polymarket_signature_type: int = Field(1, ge=0, le=2)

    # --- Cibles ---
    target_wallets: list[str] = Field(default_factory=list)

    # --- Sizing & risk ---
    copy_ratio: float = Field(0.01, gt=0, le=1)
    max_position_usd: float = Field(100, gt=0)
    min_market_liquidity_usd: float = Field(5000, ge=0)
    min_hours_to_expiry: float = Field(24, ge=0)
    max_slippage_pct: float = Field(2.0, ge=0)
    kill_switch_drawdown_pct: float = Field(20, ge=0, le=100)

    # --- Polling ---
    poll_interval_seconds: int = Field(5, ge=1)

    # --- Storage ---
    database_url: str = "sqlite+aiosqlite:///polycopy.db"

    # --- Mode ---
    dry_run: bool = True

    # --- Monitoring ---
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # --- Logs ---
    log_level: str = "INFO"


settings = Settings()  # type: ignore[call-arg]
