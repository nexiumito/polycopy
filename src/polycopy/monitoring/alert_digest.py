"""Compteur glissant d'alertes par ``event_type`` pour le digest mode (M7 §2.5).

Algorithme : on garde pour chaque ``event_type`` une ``deque`` de timestamps.
À chaque ``register(alert, now)`` :

1. On purge les entrées antérieures à ``now - window``.
2. On append le nouveau timestamp.
3. Si la taille atteint ``threshold`` → on retourne ``emit_digest`` avec
   ``count`` + on vide la queue (reset du compteur pour cet event_type).
4. Sinon → ``emit_single``.

Aucun état n'est persisté en DB. Reset au boot, comportement acceptable à M7
(spec §1 hors scope : persistance reportable M7.1).
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta

from polycopy.monitoring.dtos import Alert, DigestDecision


class AlertDigestWindow:
    """Fenêtre glissante in-memory de compteurs d'alertes par ``event_type``."""

    def __init__(self, window_seconds: int, threshold: int) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        if threshold < 2:
            raise ValueError("threshold must be >= 2")
        self._window = timedelta(seconds=window_seconds)
        self._threshold = threshold
        self._buckets: dict[str, deque[datetime]] = defaultdict(deque)

    @property
    def threshold(self) -> int:
        return self._threshold

    @property
    def window_seconds(self) -> int:
        return int(self._window.total_seconds())

    def register(self, alert: Alert, now: datetime) -> DigestDecision:
        """Enregistre ``alert`` au temps ``now`` et retourne la décision."""
        bucket = self._buckets[alert.event]
        cutoff = now - self._window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        bucket.append(now)
        count = len(bucket)
        if count >= self._threshold:
            bucket.clear()
            return DigestDecision(
                action="emit_digest",
                count=count,
                event_type=alert.event,
            )
        return DigestDecision(
            action="emit_single",
            count=count,
            event_type=alert.event,
        )

    def reset(self) -> None:
        """Purge tous les buckets. Utile en tests."""
        self._buckets.clear()
