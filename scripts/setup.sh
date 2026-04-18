#!/usr/bin/env bash
# scripts/setup.sh — Bootstrap polycopy dev env on a fresh WSL Ubuntu.
# Idempotent. Run `bash scripts/setup.sh` from the repo root (or anywhere —
# the script resolves its own location).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

info() { printf '[setup] %s\n' "$*"; }
ok()   { printf '[setup] OK   %s\n' "$*"; }
skip() { printf '[setup] SKIP %s\n' "$*"; }
fail() { printf '[setup] FAIL %s\n' "$*" >&2; exit 1; }

info "repo root: ${REPO_ROOT}"

# --- 1. Python 3.11+ --------------------------------------------------------
info "looking for Python 3.11+..."
PYTHON_BIN=""
for candidate in python3.12 python3.13 python3.11 python3 python; do
    if command -v "${candidate}" >/dev/null 2>&1; then
        ver=$("${candidate}" -c 'import sys; print(sys.version_info.major*100+sys.version_info.minor)' 2>/dev/null || echo 0)
        if [ "${ver:-0}" -ge 311 ]; then
            PYTHON_BIN="${candidate}"
            break
        fi
    fi
done
if [ -z "${PYTHON_BIN}" ]; then
    fail $'Python 3.11+ not found.\n        On WSL Ubuntu, install it with:\n          sudo apt update && sudo apt install -y python3.11 python3.11-venv python3-pip\n        If Ubuntu 22.04 lacks python3.11 in apt, enable the deadsnakes PPA first:\n          sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt update\n        Then re-run: bash scripts/setup.sh'
fi
ok "python: $("${PYTHON_BIN}" --version) at $(command -v "${PYTHON_BIN}")"

# Pre-flight: ensure the -venv / ensurepip support package is installed,
# otherwise `python -m venv` leaves a broken .venv/ behind.
if ! "${PYTHON_BIN}" -c 'import ensurepip' >/dev/null 2>&1; then
    pyver=$("${PYTHON_BIN}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    fail $'ensurepip is missing for '"${PYTHON_BIN}"$' — the python'"${pyver}"$'-venv package is not installed.\n        Fix it with:\n          sudo apt install -y python'"${pyver}"$'-venv\n        Then re-run: bash scripts/setup.sh'
fi

# --- 2. Ghost folder cleanup ------------------------------------------------
if [ -d '{src' ]; then
    rm -rf '{src'
    ok "removed ghost folder '{src/' (shell brace-expansion artefact)"
else
    skip "ghost folder '{src/' absent"
fi

# --- 3. venv ----------------------------------------------------------------
if [ ! -d .venv ]; then
    info "creating .venv/ with ${PYTHON_BIN}..."
    if ! "${PYTHON_BIN}" -m venv .venv; then
        # Clean up the half-created venv so the next run starts fresh.
        rm -rf .venv
        fail "'${PYTHON_BIN} -m venv .venv' failed — half-created .venv/ removed so you can retry cleanly"
    fi
    ok ".venv/ created"
elif [ ! -x .venv/bin/python ]; then
    info ".venv/ exists but looks broken (no .venv/bin/python) — recreating..."
    rm -rf .venv
    "${PYTHON_BIN}" -m venv .venv
    ok ".venv/ recreated"
else
    skip ".venv/ already exists"
fi

# venv activate references unset vars — relax nounset briefly.
set +u
# shellcheck disable=SC1091
source .venv/bin/activate
set -u
ok "venv activated ($(command -v python))"

# --- 4. pip + deps ----------------------------------------------------------
info "upgrading pip..."
python -m pip install --quiet --upgrade pip
ok "pip: $(pip --version | awk '{print $1, $2}')"

info "installing project + dev deps (pip install -e '.[dev]') — may take a minute on first run..."
pip install --quiet -e ".[dev]"
ok "deps installed"

# --- 5. Verify dev tools ----------------------------------------------------
for tool in ruff mypy pytest; do
    if command -v "${tool}" >/dev/null 2>&1; then
        ok "${tool}: $("${tool}" --version 2>&1 | head -n1)"
    else
        fail "${tool} missing after install — check pip output above"
    fi
done

# --- 6. .env ----------------------------------------------------------------
if [ ! -f .env ]; then
    cp .env.example .env
    ok ".env created from .env.example (remember to edit TARGET_WALLETS before a real run)"
else
    skip ".env already exists — not overwritten"
fi

# --- 7. Config patches (M1 spec §0.5 + NoDecode CSV parsing for TARGET_WALLETS)
info "checking config.py patches (§0.5 + TARGET_WALLETS CSV)..."
python - <<'PYEOF'
from pathlib import Path

p = Path("src/polycopy/config.py")
src = p.read_text(encoding="utf-8")
original = src

# --- §0.5: make Polymarket wallet fields optional (used only from M3). -------
src = src.replace(
    'polymarket_private_key: str = Field(..., description="Clé privée du wallet de signature")',
    'polymarket_private_key: str | None = Field(None, description="Clé privée du wallet de signature (requis à M3)")',
)
src = src.replace(
    'polymarket_funder: str = Field(..., description="Adresse du proxy wallet")',
    'polymarket_funder: str | None = Field(None, description="Adresse du proxy wallet (requis à M3)")',
)
src = src.replace(
    "settings = Settings()  # type: ignore[call-arg]",
    "settings = Settings()",
)

# --- TARGET_WALLETS CSV parsing ---------------------------------------------
# Pydantic Settings v2 JSON-decodes list[str] from env BEFORE any field_validator
# fires, so CSV input (documented in .env.example) raises JSONDecodeError.
# `NoDecode` disables that auto-decode for the annotated field, letting our
# before-validator see the raw string and split it.
if "NoDecode" not in src:
    if "import json\n" not in src:
        src = src.replace(
            '"""\n\nfrom pydantic import Field',
            '"""\n\nimport json\nfrom typing import Annotated\n\nfrom pydantic import Field',
        )
    src = src.replace(
        "from pydantic import Field\n",
        "from pydantic import Field, field_validator\n",
    )
    src = src.replace(
        "from pydantic_settings import BaseSettings, SettingsConfigDict",
        "from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict",
    )
    old_block = (
        "    # --- Cibles ---\n"
        "    target_wallets: list[str] = Field(default_factory=list)\n"
        "\n"
        "    # --- Sizing & risk ---\n"
    )
    new_block = (
        "    # --- Cibles ---\n"
        "    # `NoDecode` désactive le JSON-decode auto de pydantic-settings pour ce champ ;\n"
        "    # le validator ci-dessous reçoit la string brute et gère CSV + JSON.\n"
        "    target_wallets: Annotated[list[str], NoDecode] = Field(default_factory=list)\n"
        "\n"
        "    @field_validator(\"target_wallets\", mode=\"before\")\n"
        "    @classmethod\n"
        "    def _parse_target_wallets(cls, v: object) -> object:\n"
        "        \"\"\"Accepte `TARGET_WALLETS` en CSV (`0xabc,0xdef`) ou en JSON (`[\\\"0xabc\\\",\\\"0xdef\\\"]`).\"\"\"\n"
        "        if isinstance(v, str):\n"
        "            stripped = v.strip()\n"
        "            if not stripped:\n"
        "                return []\n"
        "            if stripped.startswith(\"[\"):\n"
        "                return json.loads(stripped)\n"
        "            return [item.strip() for item in stripped.split(\",\") if item.strip()]\n"
        "        return v\n"
        "\n"
        "    # --- Sizing & risk ---\n"
    )
    src = src.replace(old_block, new_block)

if src != original:
    p.write_text(src, encoding="utf-8")
    print("[setup] OK   config.py patched")
else:
    print("[setup] SKIP config.py already patched")
PYEOF

# --- 8. Smoke test ----------------------------------------------------------
info "smoke test: python -m polycopy --dry-run"
if python -m polycopy --dry-run; then
    ok "smoke test passed (exit 0)"
else
    fail "smoke test failed — read the structlog lines above"
fi

# --- Done -------------------------------------------------------------------
cat <<'EOM'

[setup] =====================================================
[setup] setup complete
[setup]   1. activate venv:  source .venv/bin/activate
[setup]   2. edit .env       (minimum: set TARGET_WALLETS to a real Polygon address)
[setup]   3. dry-run:        python -m polycopy --dry-run
[setup]   4. tests:          pytest
[setup] =====================================================
EOM
