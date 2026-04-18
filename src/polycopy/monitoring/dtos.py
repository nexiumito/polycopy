"""DTOs du module Monitoring (M4)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

AlertLevel = Literal["INFO", "WARNING", "ERROR", "CRITICAL"]


class Alert(BaseModel):
    """Événement critique à pousser sur la queue alertes (M4).

    ``cooldown_key`` permet au ``AlertDispatcher`` de dédupliquer les alertes
    répétées dans une fenêtre (default 60 s). ``None`` = jamais de throttle.
    """

    model_config = ConfigDict(frozen=True)

    level: AlertLevel
    event: str
    body: str
    cooldown_key: str | None = None
