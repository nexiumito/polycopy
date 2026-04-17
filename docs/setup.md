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

**Pour M1** (le milestone courant), **tu n'as besoin de renseigner qu'une seule variable** :

| Variable | À faire à M1 |
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

## 10. Migration de schéma DB (M3+)

Tant que le projet n'a pas Alembic (prévu à M4), toute modification de
`src/polycopy/storage/models.py` après un `git pull` impose de recréer la DB
locale :

```bash
rm polycopy.db
python -m polycopy --dry-run   # init_db.create_all recrée tout
```

Les données dev (detected_trades, strategy_decisions, my_orders) sont perdues —
acceptable jusqu'à l'introduction d'Alembic.
