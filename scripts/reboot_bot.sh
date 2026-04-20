#!/usr/bin/env bash
# scripts/reboot_bot.sh — Redémarre polycopy proprement en mode test détaché.
#
# 1. SIGTERM sur le wrapper systemd-inhibit (laisse 8s pour flush DB/WS).
# 2. SIGKILL si toujours vivant.
# 3. Relance détachée via setsid + systemd-inhibit (survit au lock écran).
# 4. Capture le PID et lance le boot check.
#
# Usage : bash scripts/reboot_bot.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

WRAPPER_PATTERN='^systemd-inhibit --what=sleep:idle --who=polycopy'
BOOT_LOG=/tmp/polycopy_boot.log
PID_FILE=/tmp/polycopy_night.pid

info() { printf '[reboot] %s\n' "$*"; }
ok()   { printf '[reboot] \xe2\x9c\x93  %s\n' "$*"; }
warn() { printf '[reboot] \xe2\x9a\xa0  %s\n' "$*" >&2; }
fail() { printf '[reboot] \xe2\x9c\x97  %s\n' "$*" >&2; exit 1; }

# ── 1. Stop ──────────────────────────────────────────────────────────────────
EXISTING_PID=$(pgrep -f "${WRAPPER_PATTERN}" | head -1 || true)
if [ -n "${EXISTING_PID}" ]; then
  info "Instance active PID=${EXISTING_PID} — SIGTERM"
  kill -TERM "${EXISTING_PID}" 2>/dev/null || true
  for i in $(seq 1 8); do
    sleep 1
    if ! kill -0 "${EXISTING_PID}" 2>/dev/null; then
      ok "Arrêt propre après ${i}s"
      break
    fi
  done
  if kill -0 "${EXISTING_PID}" 2>/dev/null; then
    warn "Toujours vivant après 8s — SIGKILL"
    kill -KILL "${EXISTING_PID}" 2>/dev/null || true
    sleep 1
  fi
else
  info "Aucune instance active"
fi

# Nettoie les orphelins éventuels (python -m polycopy sans wrapper)
ORPHANS=$(pgrep -f 'python -m polycopy' || true)
if [ -n "${ORPHANS}" ]; then
  warn "Process orphelins détectés : ${ORPHANS} — SIGKILL"
  # shellcheck disable=SC2086
  kill -KILL ${ORPHANS} 2>/dev/null || true
  sleep 1
fi

# ── 2. Launch détaché ────────────────────────────────────────────────────────
info "Lancement détaché (setsid + systemd-inhibit)"
setsid systemd-inhibit --what=sleep:idle --who=polycopy --why="14-day test" \
  python -m polycopy --no-cli > "${BOOT_LOG}" 2>&1 < /dev/null &
disown

sleep 4
WRAPPER_PID=$(pgrep -f "${WRAPPER_PATTERN}" | head -1 || true)
if [ -z "${WRAPPER_PID}" ]; then
  fail "Launch FAILED — tail ${BOOT_LOG} :"
  tail -30 "${BOOT_LOG}" >&2 || true
  exit 1
fi
ok "PID=${WRAPPER_PID}"
ps -p "${WRAPPER_PID}" -o pid,ppid,tt,stat,cmd --no-headers || true

echo "${WRAPPER_PID}" > "${PID_FILE}"
ok "PID file écrit : ${PID_FILE}"

# ── 3. Boot check (après 60 s pour laisser le 1er cycle démarrer) ────────────
if [ -f "${REPO_ROOT}/scripts/night_test_status.py" ]; then
  info "Attente 60 s avant boot check (laisse tourner le 1er cycle discovery)…"
  for i in $(seq 1 60); do
    if ! kill -0 "${WRAPPER_PID}" 2>/dev/null; then
      fail "Process mort pendant l'attente (t+${i}s) — tail ${BOOT_LOG} :"
      tail -30 "${BOOT_LOG}" >&2 || true
      exit 1
    fi
    sleep 1
  done
  info "Boot check"
  python "${REPO_ROOT}/scripts/night_test_status.py" --boot || true
else
  warn "scripts/night_test_status.py introuvable — skip boot check"
fi
