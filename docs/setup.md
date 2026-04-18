# Setup — polycopy

Guide pas-à-pas pour un utilisateur Windows qui démarre avec **WSL Ubuntu** et peu ou pas d'expérience Python. Objectif : cloner le repo et lancer `python -m polycopy --dry-run` en **moins de 5 minutes**.

Environnement de référence : **WSL Ubuntu (bash)**. Le bot peut probablement tourner sous Windows ou macOS directs, mais ce n'est ni testé ni supporté.

---

## 1. Installer WSL Ubuntu (si pas déjà fait)

Dans un PowerShell **Administrateur** sous Windows :

```powershell
wsl --install -d Ubuntu
```

Redémarre si demandé, puis lance "Ubuntu" depuis le menu Démarrer pour créer l'utilisateur Unix (login + mot de passe). Une fois dans le shell Ubuntu, tu tapes des commandes Linux — c'est là qu'on va travailler.

Tuto officiel : https://learn.microsoft.com/windows/wsl/install

## 2. Installer les dépendances système

Dans **WSL bash** (pas PowerShell) :

```bash
sudo apt update
sudo apt install -y git python3.11 python3.11-venv python3-pip
```

Explication :
- `git` : pour cloner le repo.
- `python3.11` + `python3.11-venv` : Python 3.11 et le module `venv` (Ubuntu le sépare en deux paquets).
- `python3-pip` : l'installeur de paquets Python.

> **Ubuntu 24.04** arrive avec Python 3.12 par défaut mais sans son paquet venv. Si `setup.sh` se plaint de `ensurepip is missing` :
> ```bash
> sudo apt install -y python3.12-venv
> rm -rf .venv
> ```
> puis relance le script.

> Si `python3.11` est introuvable dans apt (cas Ubuntu 22.04), active d'abord le PPA deadsnakes :
> ```bash
> sudo add-apt-repository -y ppa:deadsnakes/ppa
> sudo apt update
> sudo apt install -y python3.11 python3.11-venv python3-pip
> ```

Vérification :

```bash
python3.11 --version   # doit répondre "Python 3.11.x"
```

## 3. Cloner le repo

On recommande de bosser en **Linux natif** (`/home/<toi>/code/polycopy`), pas depuis `/mnt/c/...` — l'I/O côté `/mnt/c` est lent pour les venvs et les tests.

```bash
mkdir -p ~/code
cd ~/code
git clone https://github.com/nexiumito/polycopy.git
cd polycopy
```

## 4. Lancer le bootstrap automatique

Un seul script fait tout le boulot :

```bash
bash scripts/setup.sh
```

Ce que ça fait, ligne par ligne :

1. Vérifie que Python 3.11+ est installé.
2. Supprime un dossier fantôme `{src/` s'il traîne (artéfact shell).
3. Crée `.venv/` (environnement Python isolé) à la racine.
4. Active le venv et met à jour `pip`.
5. Installe le projet en mode éditable + les outils de dev (`pytest`, `ruff`, `mypy`, etc.).
6. Copie `.env.example` → `.env` si `.env` n'existe pas (**jamais d'écrasement**).
7. Applique un patch de config (§0.5 de la spec M1) qui rend la clé privée Polymarket optionnelle pour M1.
8. Lance un smoke test : `python -m polycopy --dry-run` doit s'exécuter et sortir avec code 0.

Chaque étape log `[setup] OK …`, `[setup] SKIP …` ou `[setup] FAIL …`. En cas d'erreur, le script s'arrête immédiatement (`set -euo pipefail`) — lis la dernière ligne FAIL pour savoir quoi corriger.

Le script est **idempotent** : tu peux le relancer autant de fois que tu veux sans casser quoi que ce soit.

## 5. Éditer `.env`

Ouvre le fichier dans ton éditeur :

```bash
code .env        # VS Code (avec l'extension Remote-WSL)
# ou
nano .env        # si tu préfères le terminal
```

**Pour démarrer en dry-run** (milestone courant : M4), **tu n'as besoin de renseigner qu'une seule variable** :

| Variable | À faire en dry-run |
|---|---|
| `TARGET_WALLETS` | **Obligatoire**. Mets 1 adresse Polygon connue active sur Polymarket (CSV pour plusieurs). |
| `POLL_INTERVAL_SECONDS` | Laisse `5`, ou monte à `15` en dev pour économiser le rate limit. |
| `DRY_RUN` | Laisse `true`. |
| `LOG_LEVEL` | Laisse `INFO`. |
| `DATABASE_URL` | Laisse la valeur SQLite par défaut. |

**Tu peux laisser vide pour M1** : `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`, `POLYMARKET_SIGNATURE_TYPE`, `TELEGRAM_*`. Ces champs ne servent qu'à partir de M3 (Executor).

### Où trouver une adresse de wallet à observer

Ouvre https://polymarket.com, clique sur un trader dans un leaderboard de marché, copie son adresse Polygon (commence par `0x...`, 42 caractères). C'est une donnée publique, aucun risque à la coller dans `.env`.

Exemple : un wallet public connu (à remplacer par ton pick) :
```
TARGET_WALLETS=0x1234567890abcdef1234567890abcdef12345678
```

## 6. Relancer le dry-run

À chaque nouvelle session WSL, réactive le venv avant de lancer le bot :

```bash
cd ~/code/polycopy
source .venv/bin/activate
python -m polycopy --dry-run
```

Tu dois voir au moins :
- Une ligne structlog `polycopy_starting` avec `dry_run=True` et `targets=[...]`.
- Sortie code 0 après ~1s (le stub M1 n'itère pas encore).

## 7. Lancer les tests

Tant que le venv est actif :

```bash
pytest
```

## 8. Troubleshooting

| Symptôme | Cause probable | Fix |
|---|---|---|
| `python3.11: command not found` | Paquet pas installé | Retour étape 2. |
| `ensurepip is not available` | Le paquet `pythonX.Y-venv` n'est pas installé pour la version Python choisie | `sudo apt install -y python3.12-venv` (adapte la version), puis `rm -rf .venv` et relance le script. |
| `bash: scripts/setup.sh: No such file or directory` | Pas à la racine du repo | `cd ~/code/polycopy` puis relance. |
| `ModuleNotFoundError: polycopy` au dry-run | venv pas activé | `source .venv/bin/activate`. |
| `pydantic_core._pydantic_core.ValidationError: POLYMARKET_PRIVATE_KEY field required` | Le patch §0.5 n'a pas été appliqué | Relance `bash scripts/setup.sh` (idempotent). |
| `.env` absent | Première exécution interrompue | Relance le script ou `cp .env.example .env`. |
| Installation pip très lente | Tu bosses depuis `/mnt/c/...` | Déplace le repo dans `~/code/` (Linux natif). |

## 9. Mise à jour des dépendances

Après un `git pull` qui modifie `pyproject.toml` :

```bash
bash scripts/setup.sh
```

Les deps seront réinstallées, sans toucher à `.venv/` ni `.env`.

## 10. Migration de schéma DB (M4+)

Depuis M4, Alembic gère les migrations. `init_db` exécute automatiquement
`alembic upgrade head` au boot — rien à faire pour la plupart des users.

### Première installation (DB neuve)
Rien à faire — `init_db` applique la baseline + tous les deltas automatiquement.

### Après git pull qui modifie `src/polycopy/storage/models.py`
Si le PR contient une nouvelle migration Alembic, `init_db` l'applique au boot.
**Tes données sont préservées.**

### DB préexistante de M3 (sans Alembic)
`init_db` détecte l'état "tables M3 présentes mais pas de `alembic_version`"
et appelle automatiquement `alembic stamp 0001_baseline_m3` puis
`alembic upgrade head` — transparent pour l'utilisateur.

Si tu veux forcer cet état manuellement (ex: DB corrompue ou script externe) :

```bash
source .venv/bin/activate
alembic stamp head      # marque la DB comme "à jour avec head", sans rejouer
```

Option nucléaire : `rm polycopy.db && python -m polycopy --dry-run` repart de
zéro (perte des données dev, acceptable pour un env de dev).

### Créer une nouvelle migration (dev)

```bash
# Après modif src/polycopy/storage/models.py :
alembic revision --autogenerate -m "ma_migration"
# Audite le fichier généré dans alembic/versions/, puis :
alembic upgrade head
```

⚠️ SQLite a des limites d'`ALTER TABLE` (pas de DROP COLUMN avant 3.35,
pas de RENAME COLUMN avant 3.25). Pour drop/rename une colonne, Alembic
auto-génère une stratégie "create new table + copy data + drop old". Auditer
manuellement les migrations générées.

## 11. Activer les alertes Telegram (optionnel, M4)

Voir la section "Alertes Telegram (optionnel)" du [README](../README.md).
Telegram est entièrement optionnel : sans token, le bot log les événements
localement et ne POST rien (bypass silencieux).

## 12. Générer un rapport PnL

Le writer persiste un snapshot toutes les `PNL_SNAPSHOT_INTERVAL_SECONDS`
(5 min default). Pour un rapport HTML :

```bash
source .venv/bin/activate
python scripts/pnl_report.py --since 7 --output html
# → écrit pnl_report.html, ouvre dans un navigateur
```

Formats alternatifs : `--output stdout` (table plain text) ou `--output csv`.

## 13. Activer le dashboard local (optionnel, M4.5)

Dashboard web **read-only** pour superviser les détections, décisions, ordres, positions et PnL en quasi-temps-réel.

Dans `.env` :

```
DASHBOARD_ENABLED=true
```

Relance le bot puis ouvre `http://127.0.0.1:8787/` dans ton navigateur.

Pages : Home (KPIs) · Détection · Stratégie · Exécution · Positions · PnL (graph Chart.js).

Le dashboard est **read-only** (aucun `POST`/`DELETE` exposé) et n'est joignable que depuis la machine hôte (bind localhost, aucune auth applicative nécessaire).

Pour changer le port : `DASHBOARD_PORT=9000`.

Pour l'exposer sur le LAN (⚠️ **à tes risques** — tu exposes les wallets observés et tes trades à tout le réseau local, aucune auth) :

```
DASHBOARD_HOST=0.0.0.0
```

### Troubleshooting

| Symptôme | Cause probable | Fix |
|---|---|---|
| Pas de log `dashboard_starting` | `DASHBOARD_ENABLED` absent ou `false` | Édite `.env` → `DASHBOARD_ENABLED=true`, relance. |
| Connexion refusée | Bot pas lancé, ou port déjà pris | `ss -tlnp \| grep 8787` pour vérifier. |
| Page blanche | CDN (HTMX / Chart.js / Pico.css) bloqué | Premier chargement nécessite internet, ensuite cache navigateur. |
| Le navigateur Windows ne voit rien | Sous WSL2, `http://localhost:8787/` est normalement forwardé auto. Sinon `http://$(hostname -I):8787/`. | — |

## 14. Activer la découverte automatique de traders (optionnel, M5)

M5 permet au bot de **découvrir et scorer automatiquement** des wallets Polymarket, puis de promouvoir les meilleurs en cibles actives (copiées par le watcher). **Opt-in strict** : par défaut, le bot n'utilise que les `TARGET_WALLETS` listés dans `.env`.

⚠️ **Pré-requis bloquant** : lance le backtest avant d'activer en prod.

```bash
source .venv/bin/activate
python scripts/score_backtest.py \
  --wallets-file specs/m5_backtest_seed.txt \
  --as-of 2026-01-15 \
  --observe-days 30 \
  --output backtest_v1_report.html
```

Ouvre `backtest_v1_report.html`. Si la corrélation Spearman score ↔ ROI observé est **≥ 0.30**, tu peux activer M5. Sinon, n'active pas en prod (la formule v1 ne prédit pas suffisamment — remontée à l'équipe pour itération en `SCORING_VERSION=v2`).

### Activation

Dans `.env` :

```env
DISCOVERY_ENABLED=true
DISCOVERY_INTERVAL_SECONDS=21600   # 6 h, default
MAX_ACTIVE_TRADERS=10              # plafond dur — jamais retrait arbitraire
TRADER_SHADOW_DAYS=7               # observation avant promotion
SCORING_VERSION=v1
SCORING_PROMOTION_THRESHOLD=0.65
SCORING_DEMOTION_THRESHOLD=0.40
```

Relance le bot. Ouvre le dashboard M4.5 → onglet **Traders** (`http://127.0.0.1:8787/traders`) :

- Tes wallets `pinned` (seed `TARGET_WALLETS`) apparaissent en premier.
- Au 1er cycle (~6 h), des wallets `shadow` apparaissent.
- Après `TRADER_SHADOW_DAYS` jours, si leur score reste ≥ 0.65, ils sont promus `active` (suivis par le watcher).

### Désactivation à chaud

```bash
# .env
DISCOVERY_ENABLED=false
```

Redémarre. Aucun wallet n'est retiré — l'état persiste en DB (les `active`/`shadow` déjà promus restent suivis par le watcher).

### Blacklist

Pour empêcher définitivement un wallet d'entrer dans `target_traders` :

```bash
# .env
BLACKLISTED_WALLETS=0xabc,0xdef,0x123
```

Vérifié 2 fois par cycle (pre-bootstrap et pre-promotion). Même un wallet qui scorerait 1.0 sera refusé.

### Retirer manuellement un trader actif

M5 ne dépasse **jamais** `MAX_ACTIVE_TRADERS` : si le cap est atteint, il refuse d'ajouter + alerte `discovery_cap_reached`. Pour libérer une place, soit tu augmentes le cap, soit tu retires manuellement un wallet :

```sql
-- Via sqlite3 ou ton client DB préféré :
UPDATE target_traders
   SET status='paused', active=0
 WHERE wallet_address='0xabc';
```

(Éditer `TARGET_WALLETS` dans `.env` et redémarrer ne retire **pas** un wallet déjà en DB — c'est voulu, pour éviter les toggles accidentels.)

### Troubleshooting

| Symptôme | Cause probable | Fix |
|---|---|---|
| Pas de log `discovery_starting` | `DISCOVERY_ENABLED` absent ou `false` | Édite `.env` → `DISCOVERY_ENABLED=true`. |
| `discovery_cycle_failed` répété | Data API down ou rate-limited | `curl https://data-api.polymarket.com/trades?limit=5` pour tester. |
| `discovery_cap_reached` répété | `MAX_ACTIVE_TRADERS` atteint | Augmente le cap ou retire manuellement un wallet (SQL ci-dessus). |
| Score bloqué à 0.0 sur un wallet | Cold start (< `SCORING_MIN_CLOSED_MARKETS`) | Attendre — le wallet doit accumuler 10 positions résolues. |
| `goldsky_cycle_failed` | URL subgraph obsolète | Mettre à jour `GOLDSKY_POSITIONS_SUBGRAPH_URL` (voir https://thegraph.com/hosted-service/subgraphs/polymarket). |
| Toutes les promotions refusées | `DISCOVERY_SHADOW_BYPASS=false` + `TRADER_SHADOW_DAYS>0` | Normal, attendre N jours ou baisser `TRADER_SHADOW_DAYS`. |

