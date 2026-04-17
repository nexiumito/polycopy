"""Configuration centralisée via Pydantic Settings.

Toutes les variables sont chargées depuis l'environnement (ou .env en dev).
Aucune valeur sensible en dur dans le code.
"""

import json
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Settings du bot, validées au démarrage."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # --- Polymarket wallet ---
    polymarket_private_key: str | None = Field(
        None,
        description="Clé privée du wallet de signature (requis à M3)",
    )
    polymarket_funder: str | None = Field(
        None,
        description="Adresse du proxy wallet (requis à M3)",
    )
    polymarket_signature_type: int = Field(1, ge=0, le=2)

    # --- Cibles ---
    # `NoDecode` désactive le JSON-decode auto de pydantic-settings pour ce champ ;
    # le validator ci-dessous reçoit la string brute et gère CSV + JSON.
    target_wallets: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("target_wallets", mode="before")
    @classmethod
    def _parse_target_wallets(cls, v: object) -> object:
        """Accepte `TARGET_WALLETS` en CSV (`0xabc,0xdef`) ou en JSON (`["0xabc","0xdef"]`)."""
        if isinstance(v, str):
            stripped = v.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return v

    # --- Sizing & risk ---
    copy_ratio: float = Field(0.01, gt=0, le=1)
    max_position_usd: float = Field(100, gt=0)
    min_market_liquidity_usd: float = Field(5000, ge=0)
    min_hours_to_expiry: float = Field(24, ge=0)
    max_slippage_pct: float = Field(2.0, ge=0)
    kill_switch_drawdown_pct: float = Field(20, ge=0, le=100)
    risk_available_capital_usd_stub: float = Field(
        1000.0,
        gt=0,
        description=(
            "Stub M2 du capital dispo pour le RiskManager. "
            "Remplacé par lecture wallet on-chain à M3."
        ),
    )

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


settings = Settings()
