"""Tests ``TOTPGuard`` + ``RateLimiter`` (M12_bis §4.4.3-4.4.4 Phase C)."""

from __future__ import annotations

import time

import pyotp
import pytest

from polycopy.remote_control.auth import RateLimiter, TOTPGuard

_SECRET = "JBSWY3DPEHPK3PXP"  # base32 16-chars


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_totp_guard_requires_non_empty_secret() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        TOTPGuard("")


# ---------------------------------------------------------------------------
# Happy path : code courant valide
# ---------------------------------------------------------------------------


def test_verify_current_code_returns_true() -> None:
    guard = TOTPGuard(_SECRET)
    code = pyotp.TOTP(_SECRET).now()
    assert guard.verify(code) is True


# ---------------------------------------------------------------------------
# Clock skew toléré (±30s = valid_window=1)
# ---------------------------------------------------------------------------


def test_verify_previous_step_code_accepted() -> None:
    """Un code généré 30s dans le passé reste valide (valid_window=1)."""
    guard = TOTPGuard(_SECRET)
    prev_code = pyotp.TOTP(_SECRET).at(int(time.time()) - 30)
    assert guard.verify(prev_code) is True


def test_verify_next_step_code_accepted() -> None:
    """Un code généré 30s dans le futur est aussi accepté (tolérance ±30s)."""
    guard = TOTPGuard(_SECRET)
    next_code = pyotp.TOTP(_SECRET).at(int(time.time()) + 30)
    assert guard.verify(next_code) is True


def test_verify_far_past_code_rejected() -> None:
    """Code 2 minutes dans le passé rejeté (hors fenêtre ±30s)."""
    guard = TOTPGuard(_SECRET)
    far_code = pyotp.TOTP(_SECRET).at(int(time.time()) - 120)
    assert guard.verify(far_code) is False


# ---------------------------------------------------------------------------
# Codes malformés / types invalides
# ---------------------------------------------------------------------------


def test_verify_wrong_code_rejected() -> None:
    guard = TOTPGuard(_SECRET)
    assert guard.verify("000000") is False


def test_verify_5_digits_rejected() -> None:
    """Exactement 6 chiffres requis (règle §4.4.3 : regex ^\\d{6}$)."""
    guard = TOTPGuard(_SECRET)
    assert guard.verify("12345") is False


def test_verify_7_digits_rejected() -> None:
    guard = TOTPGuard(_SECRET)
    assert guard.verify("1234567") is False


def test_verify_letters_rejected() -> None:
    guard = TOTPGuard(_SECRET)
    assert guard.verify("abcdef") is False


def test_verify_mixed_alphanum_rejected() -> None:
    guard = TOTPGuard(_SECRET)
    assert guard.verify("12a456") is False


def test_verify_empty_string_rejected() -> None:
    guard = TOTPGuard(_SECRET)
    assert guard.verify("") is False


def test_verify_non_string_type_rejected() -> None:
    """Protection contre un caller qui passerait un int par erreur."""
    guard = TOTPGuard(_SECRET)
    assert guard.verify(123456) is False  # type: ignore[arg-type]
    assert guard.verify(None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Isolement secret : deux secrets différents → codes distincts
# ---------------------------------------------------------------------------


def test_two_guards_with_different_secrets_are_independent() -> None:
    guard_a = TOTPGuard("JBSWY3DPEHPK3PXP")
    guard_b = TOTPGuard("MFRGGZDFMZTWQ2LK")
    code_a = pyotp.TOTP("JBSWY3DPEHPK3PXP").now()
    # Code A NE doit PAS valider avec guard B.
    assert guard_b.verify(code_a) is False
    # Et doit valider avec guard A.
    assert guard_a.verify(code_a) is True


# ===========================================================================
# RateLimiter (§4.4.4)
# ===========================================================================


class _FakeClock:
    """Horloge injectable pour tests — ``tick()`` avance le temps."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def tick(self, seconds: float) -> None:
        self._now += seconds


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_rate_limiter_rejects_invalid_max_attempts() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        RateLimiter(max_attempts=0)


def test_rate_limiter_rejects_invalid_window() -> None:
    with pytest.raises(ValueError, match="window_seconds"):
        RateLimiter(window_seconds=0.0)


# ---------------------------------------------------------------------------
# Happy path : 5 tentatives autorisées, 6e refusée
# ---------------------------------------------------------------------------


def test_rate_limiter_allows_first_five_attempts() -> None:
    clock = _FakeClock()
    limiter = RateLimiter(max_attempts=5, window_seconds=60.0, clock=clock.now)
    for _ in range(5):
        assert limiter.allow("100.64.0.10") is True


def test_rate_limiter_rejects_sixth_attempt() -> None:
    clock = _FakeClock()
    limiter = RateLimiter(max_attempts=5, window_seconds=60.0, clock=clock.now)
    for _ in range(5):
        limiter.allow("100.64.0.10")
    assert limiter.allow("100.64.0.10") is False


# ---------------------------------------------------------------------------
# Sliding window — expiration des entries
# ---------------------------------------------------------------------------


def test_rate_limiter_recovers_after_window_expires() -> None:
    """Après 60s, les tentatives historiques sont purgées → quota restauré."""
    clock = _FakeClock()
    limiter = RateLimiter(max_attempts=3, window_seconds=60.0, clock=clock.now)
    for _ in range(3):
        limiter.allow("100.64.0.10")
    # 6e au dessus du seuil = refusée.
    assert limiter.allow("100.64.0.10") is False
    # Avance au-delà de la fenêtre + 1s.
    clock.tick(61.0)
    assert limiter.allow("100.64.0.10") is True


def test_rate_limiter_partial_window_cleanup() -> None:
    """Seules les entries hors fenêtre sont purgées, les récentes restent."""
    clock = _FakeClock()
    limiter = RateLimiter(max_attempts=3, window_seconds=60.0, clock=clock.now)
    limiter.allow("100.64.0.10")  # t=0
    clock.tick(40.0)
    limiter.allow("100.64.0.10")  # t=40
    clock.tick(30.0)  # t=70 : la 1ère entry (t=0) est hors fenêtre, la 2e reste
    # À t=70, la fenêtre couvre [10, 70]. Entry 1 (t=0) hors, entry 2 (t=40) in.
    # On a donc 1 tentative dans l'historique → 2 tentatives encore dispo.
    assert limiter.allow("100.64.0.10") is True  # -> 2 in history
    assert limiter.allow("100.64.0.10") is True  # -> 3 in history, quota atteint
    assert limiter.allow("100.64.0.10") is False  # 4e refusée


# ---------------------------------------------------------------------------
# Isolation par IP
# ---------------------------------------------------------------------------


def test_rate_limiter_tracks_ips_independently() -> None:
    clock = _FakeClock()
    limiter = RateLimiter(max_attempts=2, window_seconds=60.0, clock=clock.now)
    # IP A épuise son quota.
    assert limiter.allow("100.64.0.10") is True
    assert limiter.allow("100.64.0.10") is True
    assert limiter.allow("100.64.0.10") is False
    # IP B doit toujours être OK.
    assert limiter.allow("100.64.0.20") is True


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def test_rate_limiter_reset_per_ip() -> None:
    clock = _FakeClock()
    limiter = RateLimiter(max_attempts=1, window_seconds=60.0, clock=clock.now)
    assert limiter.allow("100.64.0.10") is True
    assert limiter.allow("100.64.0.10") is False
    limiter.reset("100.64.0.10")
    assert limiter.allow("100.64.0.10") is True


def test_rate_limiter_reset_all() -> None:
    clock = _FakeClock()
    limiter = RateLimiter(max_attempts=1, window_seconds=60.0, clock=clock.now)
    limiter.allow("100.64.0.10")
    limiter.allow("100.64.0.20")
    limiter.reset()
    assert limiter.allow("100.64.0.10") is True
    assert limiter.allow("100.64.0.20") is True


def test_rate_limiter_refused_attempt_not_recorded() -> None:
    """Une tentative refusée ne doit PAS être ajoutée à l'historique —
    sinon la récupération après la fenêtre serait plus lente que nécessaire.
    """
    clock = _FakeClock()
    limiter = RateLimiter(max_attempts=2, window_seconds=10.0, clock=clock.now)
    limiter.allow("100.64.0.10")  # t=0, accept
    limiter.allow("100.64.0.10")  # t=0, accept
    assert limiter.allow("100.64.0.10") is False  # t=0, reject, not recorded
    clock.tick(5.0)
    # Les 2 entries t=0 sont encore dans la fenêtre [-5, 5] → toujours refusé.
    assert limiter.allow("100.64.0.10") is False
    clock.tick(6.0)  # t=11 : fenêtre [1, 11] → entries t=0 hors
    assert limiter.allow("100.64.0.10") is True


# ---------------------------------------------------------------------------
# Usage réel : ``time.monotonic`` par défaut
# ---------------------------------------------------------------------------


def test_rate_limiter_uses_monotonic_by_default() -> None:
    """Smoke test : le limiter default fonctionne sans injection d'horloge."""
    limiter = RateLimiter(max_attempts=2)
    ip = "100.64.0.10"
    assert limiter.allow(ip) is True
    assert limiter.allow(ip) is True
    assert limiter.allow(ip) is False
    # Pas de sleep → le 3e reste refusé (la fenêtre 60s est toujours ouverte).
    _ = time  # silence unused import — time est utilisé indirectement via monotonic
