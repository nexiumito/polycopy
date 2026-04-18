# Night test runbook — polycopy

Procédure pour lancer le bot sur une nuit entière sans surveillance. Couvre :
setup Mac premier run, 3 presets `.env` (du plus safe au plus ambitieux), lancement,
vérifications matinales via `scripts/night_test_status.py`.

> **Recommandation première utilisation** : preset **A** (shadow passive v1+v2).
> Zéro risque capital, observation maximale.

---

## 1. Setup Mac (premier run uniquement)

```bash
cd ~/code/polycopy                  # adapte au chemin où tu clones
git pull --ff-only                  # récupère les dernières modifs M12

# Python 3.11+ requis
python3 --version

# Bootstrap (crée .venv, installe deps, copie .env.example → .env si absent)
bash scripts/setup.sh

# Si setup.sh râle sur Mac (scripté pour WSL), fallback manuel :
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
[ ! -f .env ] && cp .env.example .env

# Applique les migrations DB (crée trader_daily_pnl M12 entre autres)
source .venv/bin/activate
alembic upgrade head
```

---

## 2. Presets `.env` — choisis UN seul

### Preset A — Shadow passive (recommandé 1ère nuit)

v1 pilote `DecisionEngine`, v2 calcule en parallèle pour observation. **Zéro risque
capital**. Dashboard `/traders/scoring` rend la comparaison.

```bash
# --- Wallets à observer ---
TARGET_WALLETS=0xAAA...,0xBBB...,0xCCC...    # tes 3 wallets en lowercase

# --- Exécution SAFE ---
EXECUTION_MODE=dry_run
DRY_RUN_REALISTIC_FILL=true                   # simulation FOK orderbook réelle (M8)
DRY_RUN_VIRTUAL_CAPITAL_USD=1000.0

# --- Risque ---
KILL_SWITCH_DRAWDOWN_PCT=20
MAX_POSITION_USD=50
COPY_RATIO=0.01
MAX_SLIPPAGE_PCT=2.0

# --- Dashboard (à consulter le matin) ---
DASHBOARD_ENABLED=true
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8787
DASHBOARD_LOGS_ENABLED=true

# --- Discovery + Scoring v2 shadow (M12) ---
DISCOVERY_ENABLED=true
DISCOVERY_INTERVAL_SECONDS=3600               # 1h → ~8 cycles sur 8h
DISCOVERY_CANDIDATE_POOL_SIZE=100
MAX_ACTIVE_TRADERS=10
TRADER_SHADOW_DAYS=7
DISCOVERY_BACKEND=data_api

SCORING_VERSION=v1                            # ← v1 PILOTE
SCORING_V2_SHADOW_DAYS=14                     # ← v2 calculé EN PARALLÈLE
SCORING_V2_WINDOW_DAYS=90
SCORING_V2_COLD_START_MODE=false
SCORING_V2_CUTOVER_READY=false

# --- Equity curve (source Sortino/Calmar v2) ---
TRADER_DAILY_PNL_ENABLED=true
TRADER_DAILY_PNL_INTERVAL_SECONDS=3600        # 1h → ~8 points naturels / nuit

# --- Pipeline temps réel M11 ---
STRATEGY_CLOB_WS_ENABLED=true
STRATEGY_GAMMA_ADAPTIVE_CACHE_ENABLED=true
LATENCY_INSTRUMENTATION_ENABLED=true

# --- Monitoring ---
PNL_SNAPSHOT_INTERVAL_SECONDS=300

# --- Credentials CLOB (non utilisés en dry_run — laisser vides) ---
POLYMARKET_PRIVATE_KEY=
POLYMARKET_FUNDER=
POLYMARKET_SIGNATURE_TYPE=2
```

### Preset B — v2 pilote dry-run (avancé, après validation preset A)

⚠️ v2 devient autoritaire pour `DecisionEngine`. Toujours en dry-run donc zéro
capital réel, mais les décisions promote/demote suivent la formule v2. Utile
après quelques nuits de preset A où tu as vu que v2 donne des scores cohérents.

```bash
# Identique preset A SAUF :
SCORING_VERSION=v2                            # ← v2 PILOTE
SCORING_V2_SHADOW_DAYS=0                      # ← v1 plus calculé
SCORING_V2_CUTOVER_READY=true                 # ← active bouton dashboard
```

### Preset C — M5 strict (rollback)

Force le bot à ignorer complètement M12, comportement M5 pur. Utile si tu
soupçonnes un bug M12 et veux isoler.

```bash
# Identique preset A SAUF :
SCORING_VERSION=v1
SCORING_V2_SHADOW_DAYS=0                      # ← zéro calcul v2
TRADER_DAILY_PNL_ENABLED=false                # ← scheduler off
```

---

## 3. Préparation + lancement

```bash
cd ~/code/polycopy
source .venv/bin/activate

# 3.1 — Vérif config + imports
python -c "from polycopy.config import Settings; s = Settings(); \
  print(f'Mode: {s.execution_mode} | Wallets: {len(s.target_wallets)} | \
v2 shadow: {s.scoring_v2_shadow_days} | Scoring: {s.scoring_version}')"

# 3.2 — Seed equity curves (preset A ou B — pas preset C)
# Injecte 30j de curve synthétique pour que v2 calcule dès le 1er cycle.
# Idempotent : si déjà seedé, no-op.
python scripts/seed_m12_dev_curves.py --pattern mixed --days 30

# 3.3 — Lancement avec protection Mac sleep (caffeinate)
# Option recommandée : daemon + caffeinate empêche tout type de sleep
caffeinate -dis python -m polycopy --no-cli > /tmp/polycopy_night.log 2>&1 &
echo $! > /tmp/polycopy_night.pid
echo "Bot démarré — PID=$(cat /tmp/polycopy_night.pid)"
```

### Vérif avant d'aller dormir (attends 60s puis) :

```bash
sleep 60
python scripts/night_test_status.py --boot
# Check boot OK + dashboard accessible + 1er cycle démarre.
```

---

## 4. Au matin — diagnostic en 1 commande

```bash
cd ~/code/polycopy
source .venv/bin/activate
python scripts/night_test_status.py --full
```

Le script retourne un résumé structuré :
- Process UP/DOWN
- Cycles discovery tournés
- Erreurs/crashes détectés
- Trades détectés / orders simulés / positions virtuelles
- Scores v1/v2 écrits (dual-compute healthy ?)
- Gate rejections (diagnostic v2)
- Equity curves accumulées
- PnL virtuel + drawdown observés

Exit code :
- `0` : tout OK
- `1` : warnings (non-bloquant, ex: gates rejectés > 50%)
- `2` : erreurs (process DOWN, kill switch déclenché, traceback dans logs)

---

## 5. Arrêt propre

```bash
# Shutdown graceful (SIGINT)
kill -INT $(cat /tmp/polycopy_night.pid) 2>/dev/null
wait $(cat /tmp/polycopy_night.pid) 2>/dev/null
rm /tmp/polycopy_night.pid /tmp/polycopy_night.log
```

---

## Red flags à surveiller

| Pattern logs | Gravité | Interprétation |
|---|---|---|
| `kill_switch_triggered` | 🔴 | Bot arrêté — drawdown virtuel dépassé, regarde `pnl_snapshots` |
| `Traceback` | 🔴 | Crash non géré — contexte nécessaire |
| `discovery_cycle_failed` x3+ | 🟡 | Data API down ou rate-limit persistant |
| `429 Too Many Requests` isolé | 🟢 | Backoff tenacity OK, rien à faire |
| `gate_rejected` massif | 🟢 | Wallets aléatoires du feed `/trades`, normal |
| `watcher_detected_ms` énorme | 🟢 | Rattrapage trades historiques au boot |
| `ws_connection_status=reconnecting` | 🟡 | WS CLOB instable — fallback HTTP automatique |

---

## Préférences observées pour presets

Après 2-3 nuits preset A validées (v1 & v2 produisent des scores cohérents sur
tes target wallets), tu peux :

1. Passer preset B (v2 pilote dry-run) pour observer si le lifecycle
   promote/demote change avec v2.
2. Si preset B tourne 1 semaine clean + backtest OK (via
   `scripts/backtest_scoring_v2.py`), envisager `EXECUTION_MODE=live` avec
   `MAX_POSITION_USD=10` ultra-conservateur.

⚠️ Jamais sauter directement de preset A à `EXECUTION_MODE=live`.
