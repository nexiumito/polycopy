"""Client write CLOB : signature L1+L2 et POST `/order` via `py-clob-client`.

`py-clob-client` est sync (utilise `requests`). On wrap chaque appel SDK via
`asyncio.to_thread` pour ne pas bloquer l'event loop.

Garde-fou : ce client **refuse de s'instancier** en `dry_run=true` ou sans
clés Polymarket. Cf. spec M3 §2.
"""

import asyncio
from typing import TYPE_CHECKING, Any

import structlog
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from polycopy.executor.dtos import BuiltOrder, OrderResult

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)

_HOST = "https://clob.polymarket.com"
_CHAIN_ID = 137


class ClobWriteClient:
    """Wrapper async sur `py-clob-client` pour POST `/order` signés."""

    def __init__(self, settings: "Settings") -> None:
        if settings.dry_run:
            raise RuntimeError(
                "ClobWriteClient must not be instantiated in dry-run mode "
                "(lazy init expected from ExecutorOrchestrator).",
            )
        if settings.polymarket_private_key is None or settings.polymarket_funder is None:
            raise RuntimeError(
                "ClobWriteClient requires POLYMARKET_PRIVATE_KEY and "
                "POLYMARKET_FUNDER (set them in .env).",
            )
        self._settings = settings
        self._client = self._derive_client(settings)
        # Aucun log des creds (api_key, secret, passphrase) — règle de sécurité.
        log.info(
            "executor_creds_ready",
            signature_type=settings.polymarket_signature_type,
        )

    @staticmethod
    def _derive_client(settings: "Settings") -> ClobClient:
        # Étape 1 : L1 — déterministe pour la même clé+nonce.
        temp_client = ClobClient(
            _HOST,
            key=settings.polymarket_private_key,
            chain_id=_CHAIN_ID,
        )
        api_creds = temp_client.create_or_derive_api_creds()
        # Étape 2 : L2 — client signataire complet.
        return ClobClient(
            _HOST,
            key=settings.polymarket_private_key,
            chain_id=_CHAIN_ID,
            creds=api_creds,
            signature_type=settings.polymarket_signature_type,
            funder=settings.polymarket_funder,
        )

    async def post_order(self, built: BuiltOrder) -> OrderResult:
        """Signe et POST l'ordre via le SDK (sync) en off-loadant sur un thread."""
        if self._settings.dry_run:  # garde-fou défense en profondeur §2.3
            raise RuntimeError("post_order called while dry_run=True (bug)")
        args = self._build_order_args(built)
        options = {"tick_size": str(built.tick_size), "neg_risk": built.neg_risk}
        order_type_enum = OrderType[built.order_type]
        response: dict[str, Any] = await asyncio.to_thread(
            self._client.create_and_post_order,
            args,
            options,
            order_type_enum,
        )
        return OrderResult.model_validate(response)

    @staticmethod
    def _build_order_args(built: BuiltOrder) -> OrderArgs:
        side_const = BUY if built.side == "BUY" else SELL
        # FIXME: pour FOK BUY, `size` est en USD à dépenser (selon doc skill).
        # À confirmer empiriquement au 1er run réel à $1.
        return OrderArgs(
            token_id=built.token_id,
            price=built.price,
            size=built.size,
            side=side_const,
        )
