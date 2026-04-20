"""Tests ``TOTPGuard`` + ``RateLimiter`` + ``AutoLockdown`` (M12_bis §4.4 Phase C)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pyotp
import pytest

from polycopy.monitoring.dtos import Alert
from polycopy.remote_control import SentinelFile
from polycopy.remote_control.auth import AutoLockdown, RateLimiter, TOTPGuard

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


# ===========================================================================
# AutoLockdown (§4.4.5)
# ===========================================================================


def _lockdown(
    tmp_path: Path,
    *,
    alerts_queue: asyncio.Queue[Alert] | None = None,
    max_failures: int = 3,
    clock_start: float = 0.0,
) -> tuple[AutoLockdown, SentinelFile, _FakeClock]:
    sentinel = SentinelFile(tmp_path / "halt.flag")
    clock = _FakeClock(start=clock_start)
    lockdown = AutoLockdown(
        sentinel=sentinel,
        alerts_queue=alerts_queue,
        max_failures=max_failures,
        window_seconds=60.0,
        clock=clock.now,
    )
    return lockdown, sentinel, clock


# ---------------------------------------------------------------------------
# Construction invalide
# ---------------------------------------------------------------------------


def test_autolockdown_rejects_invalid_max_failures(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_failures"):
        AutoLockdown(sentinel=SentinelFile(tmp_path / "h.flag"), max_failures=0)


def test_autolockdown_rejects_invalid_window(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="window_seconds"):
        AutoLockdown(sentinel=SentinelFile(tmp_path / "h.flag"), window_seconds=0.0)


# ---------------------------------------------------------------------------
# is_locked
# ---------------------------------------------------------------------------


def test_is_locked_false_initially(tmp_path: Path) -> None:
    lockdown, _, _ = _lockdown(tmp_path)
    assert lockdown.is_locked is False


def test_is_locked_true_if_preexisting_sentinel(tmp_path: Path) -> None:
    """Si le sentinel existe déjà au boot → ``is_locked`` lu depuis le FS."""
    sentinel = SentinelFile(tmp_path / "halt.flag")
    sentinel.touch(reason="previous_run_kill_switch")
    lockdown = AutoLockdown(sentinel=sentinel)
    assert lockdown.is_locked is True


# ---------------------------------------------------------------------------
# 3-strikes triggering
# ---------------------------------------------------------------------------


def test_record_failure_below_threshold_returns_false(tmp_path: Path) -> None:
    lockdown, sentinel, _ = _lockdown(tmp_path, max_failures=3)
    assert lockdown.record_failure("100.64.0.10") is False
    assert lockdown.record_failure("100.64.0.10") is False
    assert sentinel.exists() is False
    assert lockdown.is_locked is False


def test_third_failure_triggers_lockdown(tmp_path: Path) -> None:
    lockdown, sentinel, _ = _lockdown(tmp_path, max_failures=3)
    lockdown.record_failure("100.64.0.10")
    lockdown.record_failure("100.64.0.10")
    assert lockdown.record_failure("100.64.0.10") is True  # 3e = lockdown
    assert sentinel.exists() is True
    assert sentinel.reason() == "auto_lockdown_brute_force"
    assert lockdown.is_locked is True


def test_failures_outside_window_dont_trigger(tmp_path: Path) -> None:
    """Échecs >60s avant n'accumulent pas — fenêtre glissante correcte."""
    lockdown, sentinel, clock = _lockdown(tmp_path, max_failures=3)
    lockdown.record_failure("100.64.0.10")
    lockdown.record_failure("100.64.0.10")
    clock.tick(61.0)  # les 2 entries précédentes sortent de la fenêtre
    assert lockdown.record_failure("100.64.0.10") is False  # compteur effectif = 1
    assert sentinel.exists() is False


def test_failures_from_different_ips_both_count(tmp_path: Path) -> None:
    """Lockdown est par-IP, mais 3 IPs différentes N'ACCUMULENT PAS
    un failure count global — chaque IP a son propre compteur.
    """
    lockdown, sentinel, _ = _lockdown(tmp_path, max_failures=3)
    lockdown.record_failure("100.64.0.10")
    lockdown.record_failure("100.64.0.20")
    lockdown.record_failure("100.64.0.30")
    # Chaque IP a seulement 1 failure, aucune atteint le seuil.
    assert sentinel.exists() is False


# ---------------------------------------------------------------------------
# record_success → reset du compteur
# ---------------------------------------------------------------------------


def test_success_resets_failure_count(tmp_path: Path) -> None:
    lockdown, sentinel, _ = _lockdown(tmp_path, max_failures=3)
    lockdown.record_failure("100.64.0.10")
    lockdown.record_failure("100.64.0.10")
    lockdown.record_success("100.64.0.10")
    # Le compteur est reset → 3 nouveaux échecs nécessaires.
    lockdown.record_failure("100.64.0.10")
    lockdown.record_failure("100.64.0.10")
    assert sentinel.exists() is False
    assert lockdown.record_failure("100.64.0.10") is True  # 3e après reset


# ---------------------------------------------------------------------------
# Alert Telegram émise sur lockdown
# ---------------------------------------------------------------------------


def test_lockdown_emits_telegram_alert(tmp_path: Path) -> None:
    queue: asyncio.Queue[Alert] = asyncio.Queue(maxsize=10)
    lockdown, _, _ = _lockdown(tmp_path, alerts_queue=queue, max_failures=2)
    lockdown.record_failure("100.64.0.10")
    lockdown.record_failure("100.64.0.10")
    assert queue.qsize() == 1
    alert = queue.get_nowait()
    assert alert.event == "remote_control_brute_force_detected"
    assert alert.level == "CRITICAL"
    assert "100.64.0.10" in alert.body
    assert alert.cooldown_key == "remote_control_brute_force"


def test_lockdown_works_without_alerts_queue(tmp_path: Path) -> None:
    """``alerts_queue=None`` : lockdown se déclenche mais sans alerte Telegram."""
    lockdown, sentinel, _ = _lockdown(tmp_path, alerts_queue=None, max_failures=1)
    lockdown.record_failure("100.64.0.10")
    assert sentinel.exists() is True


def test_lockdown_alert_queue_full_does_not_crash(tmp_path: Path) -> None:
    """Queue pleine → `put_nowait` raise `QueueFull` → loggé mais le
    lockdown reste effectif (sentinel posé)."""
    queue: asyncio.Queue[Alert] = asyncio.Queue(maxsize=1)
    queue.put_nowait(Alert(level="INFO", event="filler", body="x"))
    lockdown, sentinel, _ = _lockdown(tmp_path, alerts_queue=queue, max_failures=1)
    # Ne doit PAS raise.
    lockdown.record_failure("100.64.0.10")
    assert sentinel.exists() is True


# ---------------------------------------------------------------------------
# Secret TOTP ne fuite PAS dans les alertes / logs
# ---------------------------------------------------------------------------


def test_lockdown_alert_body_contains_no_secret(tmp_path: Path) -> None:
    """Règle CLAUDE.md : le secret TOTP ne doit JAMAIS apparaître dans
    les alertes (même pas le hash, même pas tronqué)."""
    queue: asyncio.Queue[Alert] = asyncio.Queue(maxsize=10)
    lockdown, _, _ = _lockdown(tmp_path, alerts_queue=queue, max_failures=1)
    lockdown.record_failure("100.64.0.10")
    alert = queue.get_nowait()
    # Aucune des 3 séquences suivantes ne doit apparaître dans l'alerte :
    assert "JBSWY3DPEHPK3PXP" not in alert.body
    assert "secret" not in alert.body.lower()
    assert "base32" not in alert.body.lower()
