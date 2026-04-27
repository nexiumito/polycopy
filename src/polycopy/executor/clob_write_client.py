"""Client write CLOB : signature L1+L2 et POST `/order` via `py-clob-client-v2`.

`py-clob-client-v2` est sync (utilise `requests`). On wrap chaque appel SDK via
`asyncio.to_thread` pour ne pas bloquer l'event loop.

Garde-fou : ce client **refuse de s'instancier** en `dry_run=true` ou sans
clés Polymarket. Cf. spec M3 §2.

M18 : SDK V1 → V2 (`py_clob_client` → `py_clob_client_v2`). Le SDK V2 garde
le constructor V1-style positionnel (cf. spec M18 §4.1 D1) — diff minimal.
Le SDK V2 est **dual-version capable** : signe V1 ou V2 selon le résultat de
`/version` endpoint backend (cf. spec M18 §4.11 D11). Ship pré-cutover safe.
"""

import asyncio
from typing import TYPE_CHECKING, Any

import structlog
from py_clob_client_v2 import (
    BuilderConfig,
    ClobClient,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
)

from polycopy.executor.dtos import BuiltOrder, OrderResult

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)

_CHAIN_ID = 137


class ClobWriteClient:
    """Wrapper async sur `py-clob-client-v2` pour POST `/order` signés."""

    def __init__(self, settings: "Settings") -> None:
        if settings.execution_mode != "live":
            raise RuntimeError(
                "ClobWriteClient must be instantiated only when "
                f"execution_mode='live' (got {settings.execution_mode!r} — "
                "lazy init expected from ExecutorOrchestrator).",
            )
        if settings.polymarket_private_key is None or settings.polymarket_funder is None:
            raise RuntimeError(
                "ClobWriteClient requires POLYMARKET_PRIVATE_KEY and "
                "POLYMARKET_FUNDER (set them in .env).",
            )
        self._settings = settings
        self._client = self._derive_client(settings)
        # Aucun log des creds (api_key, secret, passphrase) — règle de sécurité.
        # Builder code public — flag bool pour réduire le spam log (la valeur
        # peut être consultée dans `.env` directement).
        log.info(
            "executor_creds_ready",
            signature_type=settings.polymarket_signature_type,
            use_server_time=settings.polymarket_use_server_time,
            builder_code_set=settings.polymarket_builder_code is not None,
        )

    @staticmethod
    def _derive_client(settings: "Settings") -> ClobClient:
        host = settings.polymarket_clob_host
        # Étape 1 : L1 — déterministe pour la même clé+nonce.
        temp_client = ClobClient(
            host,
            chain_id=_CHAIN_ID,
            key=settings.polymarket_private_key,
        )
        api_creds = temp_client.create_or_derive_api_key()
        # M18 ME.5 D9 : BuilderConfig instancié uniquement si builder_code set,
        # sinon None laissé au SDK qui skip naturellement le plombage.
        builder_config: BuilderConfig | None = None
        if settings.polymarket_builder_code is not None:
            builder_address = (
                settings.polymarket_builder_address or settings.polymarket_funder or ""
            )
            builder_config = BuilderConfig(
                builder_address=builder_address,
                builder_code=settings.polymarket_builder_code,
            )
        # Étape 2 : L2 — client signataire complet.
        return ClobClient(
            host,
            chain_id=_CHAIN_ID,
            key=settings.polymarket_private_key,
            creds=api_creds,
            signature_type=settings.polymarket_signature_type,
            funder=settings.polymarket_funder,
            builder_config=builder_config,
            use_server_time=settings.polymarket_use_server_time,
        )

    async def post_order(self, built: BuiltOrder) -> OrderResult:
        """Signe et POST l'ordre via le SDK (sync) en off-loadant sur un thread."""
        if self._settings.execution_mode != "live":  # garde-fou défense en profondeur §2.3
            raise RuntimeError(
                f"post_order called with execution_mode="
                f"{self._settings.execution_mode!r} — MUST be 'live' (bug)",
            )
        args = self._build_order_args(built)
        # M18 : SDK V2 attend `PartialCreateOrderOptions` (dataclass), pas dict.
        # `tick_size` est un Literal["0.1","0.01","0.001","0.0001"] côté SDK.
        options = PartialCreateOrderOptions(
            tick_size=str(built.tick_size),  # type: ignore[arg-type]
            neg_risk=built.neg_risk,
        )
        # M18 : SDK V2 expose `OrderType` comme classe à attributs (pas Enum) —
        # `OrderType.FOK` est `"FOK"` (string), accès via `getattr`.
        order_type_enum = getattr(OrderType, built.order_type)
        response: dict[str, Any] = await asyncio.to_thread(
            self._client.create_and_post_order,
            args,
            options,
            order_type_enum,
        )
        return OrderResult.model_validate(response)

    @staticmethod
    def _build_order_args(built: BuiltOrder) -> OrderArgs:
        # M18 D4 : le SDK V2 accepte la string `"BUY"`/`"SELL"` directement
        # via OrderArgs(side=...) — plus de conversion via constants.
        # FIXME: pour FOK BUY, `size` est en USD à dépenser (selon doc skill).
        # À confirmer empiriquement au 1er run réel à $1.
        return OrderArgs(
            token_id=built.token_id,
            price=built.price,
            size=built.size,
            side=built.side,
        )
