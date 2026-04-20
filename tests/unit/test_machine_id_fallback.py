"""Tests ``MACHINE_ID`` normalization + hostname fallback.

Cf. spec M12_bis §3.1 + §3.4. Couvre :
- lecture env var explicite → normalisation upper.
- env var absente/vide/whitespace → fallback ``socket.gethostname()``.
- caractères hors ``[A-Z0-9_-]`` → remplacés par ``-``.
- entrée 100 % invalide → ``"UNKNOWN"``.
- cap 32 chars strict.
- ``MACHINE_EMOJI`` default + override + max_length.
- helper ``machine_id_source()`` reflète la source réelle.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from polycopy.config import Settings, machine_id_source


def _make(**env_kwargs: object) -> Settings:
    """Instancie ``Settings`` sans lire ``.env`` (isolation test)."""
    return Settings(_env_file=None, **env_kwargs)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# MACHINE_ID — lecture env var
# ---------------------------------------------------------------------------


def test_machine_id_from_env_uppercased() -> None:
    s = _make(machine_id="pc-fixe")
    assert s.machine_id == "PC-FIXE"
    assert machine_id_source() == "env"


def test_machine_id_from_env_preserves_already_upper() -> None:
    s = _make(machine_id="MACBOOK")
    assert s.machine_id == "MACBOOK"
    assert machine_id_source() == "env"


def test_machine_id_from_env_underscore_preserved() -> None:
    s = _make(machine_id="UNI_DEBIAN")
    assert s.machine_id == "UNI_DEBIAN"


# ---------------------------------------------------------------------------
# MACHINE_ID — fallback hostname
# ---------------------------------------------------------------------------


def test_machine_id_none_fallback_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("socket.gethostname", lambda: "macbook")
    s = _make()
    assert s.machine_id == "MACBOOK"
    assert machine_id_source() == "hostname"


def test_machine_id_empty_fallback_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("socket.gethostname", lambda: "my-laptop")
    s = _make(machine_id="")
    assert s.machine_id == "MY-LAPTOP"
    assert machine_id_source() == "hostname"


def test_machine_id_whitespace_fallback_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("socket.gethostname", lambda: "debian-uni")
    s = _make(machine_id="   ")
    assert s.machine_id == "DEBIAN-UNI"
    assert machine_id_source() == "hostname"


# ---------------------------------------------------------------------------
# MACHINE_ID — normalisation caractères invalides
# ---------------------------------------------------------------------------


def test_machine_id_dot_normalized_to_dash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hostname FQDN (``pc.local``) → point hors regex → remplacé par ``-``."""
    monkeypatch.setattr("socket.gethostname", lambda: "pc.local")
    s = _make()
    assert s.machine_id == "PC-LOCAL"


def test_machine_id_special_chars_normalized() -> None:
    s = _make(machine_id="PC@Home#1")
    assert s.machine_id == "PC-HOME-1"


def test_machine_id_accents_normalized() -> None:
    s = _make(machine_id="pc-élie")
    assert s.machine_id == "PC--LIE"


def test_machine_id_all_invalid_becomes_unknown() -> None:
    s = _make(machine_id="@@@")
    assert s.machine_id == "UNKNOWN"


def test_machine_id_single_space_becomes_unknown() -> None:
    """Edge case : entrée whitespace-only tombe en fallback hostname."""
    # (Test doublon sémantique de whitespace_fallback mais ici on vérifie
    # qu'on ne retombe PAS sur "UNKNOWN" : la chaîne d'echappe au fallback
    # hostname qui produit une valeur non-vide.)
    s = _make(machine_id="   ")
    assert s.machine_id != "UNKNOWN"


# ---------------------------------------------------------------------------
# MACHINE_ID — cap 32 chars
# ---------------------------------------------------------------------------


def test_machine_id_caps_at_32_chars() -> None:
    long_name = "A" * 50
    s = _make(machine_id=long_name)
    assert s.machine_id is not None
    assert len(s.machine_id) == 32
    assert s.machine_id == "A" * 32


def test_machine_id_caps_after_normalization() -> None:
    """Le cap 32 s'applique APRÈS substitution des chars invalides."""
    raw = "pc@" + "X" * 40  # 43 chars, '@' → '-'
    s = _make(machine_id=raw)
    assert s.machine_id is not None
    assert len(s.machine_id) == 32


# ---------------------------------------------------------------------------
# MACHINE_EMOJI
# ---------------------------------------------------------------------------


def test_machine_emoji_default() -> None:
    s = _make()
    assert s.machine_emoji == "🖥️"


def test_machine_emoji_override() -> None:
    s = _make(machine_emoji="💻")
    assert s.machine_emoji == "💻"


def test_machine_emoji_school() -> None:
    s = _make(machine_emoji="🏫")
    assert s.machine_emoji == "🏫"


def test_machine_emoji_max_length_rejected() -> None:
    """``MACHINE_EMOJI`` cap 8 chars (accommode ZWJ + VS mais refuse du blabla)."""
    with pytest.raises(ValidationError):
        _make(machine_emoji="x" * 9)


# ---------------------------------------------------------------------------
# machine_id_source() helper
# ---------------------------------------------------------------------------


def test_machine_id_source_tracks_env_then_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le flag global reflète la DERNIÈRE instanciation ``Settings``."""
    _make(machine_id="PC-FIXE")
    assert machine_id_source() == "env"

    monkeypatch.setattr("socket.gethostname", lambda: "fallback")
    _make()
    assert machine_id_source() == "hostname"
