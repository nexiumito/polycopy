"""Gestion du :class:`PoolContext` via ``contextvars`` (M12 §5.2).

Le ``SCORING_VERSIONS_REGISTRY`` exige une signature uniforme ``(metrics) ->
float``, mais la formule v2 nécessite en plus un :class:`PoolContext`
(valeurs pool-wide pour winsorisation + Brier baseline). On passe le contexte
via un ``ContextVar`` posé par :class:`DiscoveryOrchestrator` au début de
chaque cycle.

Invariant : le contextvar est systématiquement **reset à None** à la fin du
cycle (via :func:`bind_pool_context` qui retourne un token de reset). Les
tests unitaires qui appellent :func:`compute_score_v2` directement fournissent
leur propre :class:`PoolContext` et peuvent ignorer ce helper.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from polycopy.discovery.scoring.v2.dtos import PoolContext


_CURRENT_POOL_CONTEXT: ContextVar[PoolContext | None] = ContextVar(
    "scoring_v2_pool_context",
    default=None,
)


@contextmanager
def bind_pool_context(ctx: PoolContext | None) -> Iterator[None]:
    """Pose ``ctx`` comme :data:`_CURRENT_POOL_CONTEXT` pendant le bloc.

    Reset automatique à la sortie (exception ou non). Utilisé par
    :class:`DiscoveryOrchestrator._run_one_cycle` pour encadrer la boucle
    scoring.
    """
    token: Token[PoolContext | None] = _CURRENT_POOL_CONTEXT.set(ctx)
    try:
        yield
    finally:
        _CURRENT_POOL_CONTEXT.reset(token)
