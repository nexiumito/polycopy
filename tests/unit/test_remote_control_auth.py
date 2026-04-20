"""Tests ``TOTPGuard`` (M12_bis §4.4.3 Phase C)."""

from __future__ import annotations

import time

import pyotp
import pytest

from polycopy.remote_control.auth import TOTPGuard

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
