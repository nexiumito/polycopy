# M9 — CLI silencieux + onglet /logs + README overhaul

Spec d'implémentation du **vernis de lancement et d'onboarding** de polycopy. M1..M8 ont bâti un bot fonctionnel et instrumenté, mais l'expérience de **premier contact** reste brute : `python -m polycopy --dry-run` déverse des dizaines de lignes JSON à la seconde dans le terminal ; un nouvel utilisateur ne sait pas si "ça marche" ou "ça crash en silence". Le README est un document technique de 300 lignes, pas un tutorial.

M9 transforme cette couche d'accueil : écran CLI minimaliste (TUI statique via `rich` déjà transitive), rotation des logs JSON vers un fichier, **nouvel onglet `/logs` dans le dashboard M6** (lecture fichier, pas DB), et **refonte README** style "landing page open-source moderne" avec tutorial illustré pas-à-pas, FAQ juridique / pratique, comparaison avec autres bots Polymarket, Hall of Fame wallets publics.

Source de vérité fonctionnelle : `docs/architecture.md` (étendu §Module dashboard + nouveau §CLI). Conventions : `CLAUDE.md`. Code existant : `src/polycopy/__main__.py` (entrypoint actuel), `src/polycopy/dashboard/` (M4.5 / M6). Spec de référence : `specs/M4.5-dashboard.md` + `specs/M6-dashboard-2026.md`.

---

## 0. Pré-requis environnement

### 0.1 Bootstrap (déjà fait)

`bash scripts/setup.sh` (idempotent). M9 ajoute **1 dépendance Python minimale** : `rich>=13.0,<15.0` (déjà transitive via `pydantic` dans certaines versions — vérifier `.venv/lib/python*/site-packages/rich/` avant d'ajouter explicitement). Si non présente, ajout explicite dans `pyproject.toml` `[project] dependencies`.

Dépendances optionnelles pour capture screenshots (reportable / hors critical path) : `playwright>=1.40` en `[project.optional-dependencies] docs`. User installe via `pip install -e ".[docs]"` puis `playwright install chromium` une seule fois.

### 0.2 Pas d'invocation skill Polymarket

M9 est 100 % couche présentation CLI + doc + dashboard. Aucun endpoint Polymarket nouveau consommé.

### 0.3 `.env` — nouvelles variables (toutes OPTIONNELLES)

| Variable env | Champ Settings | Default | Description |
|---|---|---|---|
| `CLI_SILENT` | `cli_silent` | `true` | Si `true`, le terminal affiche l'écran statique rich. Si `false` (ou flag `--verbose`), ancien comportement M1..M8 = tous les logs JSON stdout. |
| `LOG_FILE` | `log_file` | `~/.polycopy/logs/polycopy.log` | Chemin du fichier log rotatif. Expanded via `Path.expanduser()`. |
| `LOG_FILE_MAX_BYTES` | `log_file_max_bytes` | `10_485_760` | Taille max avant rotation (10 MB). `Field(ge=1_048_576)`. |
| `LOG_FILE_BACKUP_COUNT` | `log_file_backup_count` | `10` | Nombre de fichiers rotatifs à conserver. `Field(ge=1, le=100)`. |
| `DASHBOARD_LOGS_ENABLED` | `dashboard_logs_enabled` | `true` | Expose l'onglet `/logs` dans le dashboard. Si `false` → l'onglet reste stub M6 (ou est caché). |
| `DASHBOARD_LOGS_TAIL_LINES` | `dashboard_logs_tail_lines` | `500` | Lignes max affichées dans l'onglet logs (anti-RAM). `Field(ge=50, le=5000)`. |

Flags CLI nouveaux :

| Flag | Effet |
|---|---|
| `--verbose` | Bypasse `CLI_SILENT=true`, dump les logs JSON stdout (comportement M1..M8). |
| `--log-level LEVEL` | Override `settings.log_level` (existant M1). Conservé. |
| `--no-cli` | Désactive complètement l'écran rich (stdout = rien, tous les logs vers fichier seulement). Utile pour systemd / daemon. |

**Variables inchangées** : `LOG_LEVEL` (M1), `DASHBOARD_ENABLED` (M4.5). Backwards compat : `.env` M8 qui n'ajoute rien → comportement M9 = écran silencieux (CLI_SILENT=true par défaut) + logs JSON toujours accessibles dans le fichier.

### 0.4 Interdépendance avec les autres specs post-M5

- **M6 (dashboard 2026)** : M6 ajoute un stub `/logs` "arrive en M9". M9 remplace ce template par le vrai onglet fonctionnel. Interdépendance forte — M9 assume l'existence du layout M6. **Recommandation d'ordre : M6 avant M9**. Mais le brief user liste "M9 → M6 → M8 → M7" en argument que le README M9 inclura les captures M6. **Contradiction** : M9 doit shipper **après** M6 pour avoir le design M6 à capturer ET pour remplacer le stub `/logs`. Reformulé dans §14.5.
- **M7 (Telegram enrichi)** : la FAQ README M9 documente les commandes BotFather + les 4 types de messages M7. Si M7 ne ship pas avant M9 → FAQ stub "M7 pending". Recommandation : M7 avant ou simultané avec M9.
- **M8 (dry-run réaliste)** : la FAQ README M9 répond "comment observer le PnL que j'aurais eu ?" en pointant vers M8. Si M8 pas shipped → FAQ minimaliste. Recommandation : M8 avant M9.

**Ordre effectif** (relecture du brief) : "M9 → M6 → M8 → M7" n'est pas viable strictement — M9 dépend de M6 pour les captures et le stub. **Décision spec M9** : lancer M9 en **dernier**, après M6 + M8 + M7. Le brief user est une idée ; la spec tranche.

### 0.5 Critère de validation "environnement"

```bash
python -m polycopy --dry-run
```

Doit :

- Afficher 1 écran statique `rich` avec les 6 modules + lien dashboard + chemin log file + "Ctrl+C pour arrêter".
- Ne **pas** émettre de JSON stdout (bypass via `RichHandler` sur `logging.StreamHandler` avec niveau `CRITICAL` sur stdout ; tout passe par `RotatingFileHandler` → `LOG_FILE`).
- Créer `~/.polycopy/logs/polycopy.log` automatiquement (et ses parents).
- Ctrl+C → message rich "🛑 arrêt propre" + sortie code 0.

```bash
python -m polycopy --dry-run --verbose
```

Doit afficher les JSON logs stdout + le fichier log (double stream), comme M1..M8.

```bash
tail -f ~/.polycopy/logs/polycopy.log | head
```

Doit montrer les events JSON structlog (inchangés depuis M1).

Dashboard `http://127.0.0.1:8787/logs` doit afficher les 500 derniers events avec filtre level + recherche texte + "live tail" toggle.

### 0.6 Sécurité — rappels stricts pour M9

**Invariants préservés** :

- **Aucun secret loggé** : depuis M1 la consigne CLAUDE.md tient. M9 **écrit** les logs dans un fichier — le user doit savoir que ce fichier peut contenir des infos sensibles (wallets publics, condition_ids, timestamps). Documenter dans `docs/setup.md` §18 : `~/.polycopy/logs/` n'est **pas** destiné à être partagé sans anonymisation.
- **`LOG_FILE` écrit avec permissions 0600** (user-only read/write). Si le parent directory est créé par M9 → `mode=0o700`. Protège des users co-présents sur un host multi-tenant.
- **Onglet `/logs` dashboard** : read-only strict (GET uniquement). Même invariant M4.5/M6. Le endpoint `GET /logs/download` (download complet) est restreint à la même invariante bind localhost. **Aucune lecture via user-controlled path** : `LOG_FILE` vient de `settings` (env), jamais d'un query param.
- **Live tail via WebSocket** ? **Non**. M9 utilise HTMX polling 2 s sur `/partials/logs-tail` (cohérent avec M4.5 §2.3 "pas de WebSocket"). Si un user veut vraiment du streaming → `tail -f` dans un terminal.
- **Search query param échapé** : `/logs?q=<user_input>` — la query est utilisée comme `needle` dans un `str.contains()` côté serveur. Aucun passage SQL/shell. Jinja `autoescape=True` sur le render du fragment (dashboard utilise `autoescape=True` en HTML — pas Markdown v2 Telegram).
- **Download endpoint** `/logs/download` : sert `LOG_FILE` en streaming avec `Content-Type: text/plain`. Pas d'`Content-Disposition: attachment` avec filename user-controlled — nom hardcodé `polycopy.log`. Limite : file size actuel (peut être > 10 MB si rotation disabled ? Non, cap par `LOG_FILE_MAX_BYTES`).
- **README** : pas de token, pas de clé privée, pas de chat_id publié dans les screenshots. Les valeurs exemple utilisent `0xabc…def` troncatures et `<ton_token>` placeholders.
- **Screenshots Playwright** : générés via `scripts/capture_screenshots.py` qui démarre le bot local avec des valeurs factices (token Telegram fake, wallets test). Ne jamais committer des screenshots pris sur un bot prod user.

---

## 1. Objectif M9 (scope exact)

Faire passer l'expérience utilisateur de "je lance le bot et je vois passer 300 lignes JSON en 30 secondes" à "je lance le bot et en 10 secondes je sais qu'il tourne, où sont mes logs, où est le dashboard".

Livrable fonctionnel :

- **CLI silencieux par défaut** : écran statique `rich` avec logo ASCII, 6 lignes modules, lien dashboard, chemin log file, version du bot.
- **Log file rotation** : `~/.polycopy/logs/polycopy.log` avec rotation 10 MB × 10 fichiers. Tous les JSON structlog y vont, même en mode `--verbose` (double stream).
- **Onglet `/logs` dashboard** : lecture du fichier log (pas DB), 500 dernières lignes, filtres level + event_type + recherche, toggle live tail polling 2 s, download du fichier complet.
- **README overhaul** : header visuel + badges shields.io + hook pitch + quickstart 5 min + tutorial pas-à-pas illustré (7 étapes avec screenshots) + FAQ + comparaison autres bots + Hall of Fame wallets publics + disclaimer.
- **Assets** : `assets/screenshots/` avec PNG 1280×720 des 7 écrans clés (logo, setup.sh run, .env édition VSCode, CLI silencieux, dashboard home, BotFather, dashboard traders).
- **Script de capture reproductible** : `scripts/capture_screenshots.py` via Playwright headless — idempotent, rejouable quand le design M6 change.

**Hors livrable M9** :

- **Pas de TUI interactif** (écran rich statique, rafraîchissement conditionnel sur module status change via polling `asyncio.sleep(5)`). Un TUI interactif full-screen (ala htop) est reportable M9.1 via `rich.live.Live`.
- **Pas de tail -f natif dans le TUI** (juste le fichier + le dashboard `/logs`).
- **Pas de daemon / systemd unit** livré par M9. Documenté dans setup.md.
- **Pas de télémétrie** (analytics, crash reporter). Vie privée user.
- **Pas de notifications desktop** (libnotify, Toast Windows).
- **Pas d'auto-update check**.
- **Pas de refactor structlog** M1..M8. Les processors existants (`add_log_level`, `TimeStamper`, `JSONRenderer`) sont conservés. M9 ajoute uniquement un `RotatingFileHandler` et gère le stdout selon `--verbose`.
- **Pas de traduction README** (FR uniquement, cohérent avec CLAUDE.md).
- **Pas de screencast vidéo** (MP4/GIF). Screenshots PNG statiques suffisent à M9.

---

## 2. Arbitrages techniques (7 points à trancher explicitement)

### 2.1 CLI silent — `rich` statique vs TUI interactif

**Recommandation : écran `rich` statique au boot + rafraîchissement léger sur status change.**

Implementation :

```python
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

def render_status_screen(settings, modules_status, version):
    console = Console()
    body = Table.grid(padding=(0, 2))
    body.add_column(justify="left", style="bold")
    body.add_column(justify="left")
    for module in modules_status:
        emoji = "✅" if module.enabled else "⏸️"
        body.add_row(emoji, f"{module.name}  {module.detail}")
    panel = Panel.fit(
        body,
        title=f"🤖 polycopy v{version}  [{'dry-run' if settings.dry_run else 'LIVE'}]",
        subtitle=f"Ouvre ton navigateur : http://127.0.0.1:{settings.dashboard_port}" if settings.dashboard_enabled else "",
        border_style="cyan" if settings.dry_run else "red",
    )
    console.print(panel)
    console.print(f"\nLogs JSON : [cyan]{settings.log_file}[/cyan]")
    console.print("Ctrl+C pour arrêter\n")
```

Pros :

- `rich` est probablement déjà transitive via `pydantic`. Si absent → 1 dep ajoutée, ~600 KB, zero risk.
- Rendu ANSI clean sur terminaux modernes (WSL bash, iTerm, Terminal.app). Fallback ASCII via `force_terminal=False` en CI.
- Statique = re-render uniquement sur changement de status (éviter le flicker d'un TUI live).

Cons :

- Pas de real-time visual feedback (trades défilent). Le user doit ouvrir le dashboard pour ça. **Intention** : pousser le user vers le dashboard, pas l'accrocher au terminal.

**Alternatives écartées** :

- **TUI interactif `rich.live.Live`** : belle idée, mais complique le shutdown (refresh thread vs asyncio), et le dashboard fait le job. Reportable M9.1.
- **Textual** (app TUI full-screen) : new dep ~5 MB, big abstraction overhead. Over-engineering pour un écran 10 lignes.
- **ASCII art pur sans rich** : moins beau, perd les couleurs sémantiques (dry-run cyan vs live red = signal visuel immédiat).

### 2.2 Log routing — double stream stdout + file

**Recommandation : `RotatingFileHandler` **toujours actif** + `StreamHandler(stdout)` conditionnel `--verbose`.**

Config structlog / stdlib :

```python
def _configure_logging(level: str, log_file: Path, max_bytes: int, backup_count: int, silent: bool) -> None:
    level_int = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level_int)
    root.handlers.clear()

    log_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    file_handler = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    file_handler.setLevel(level_int)
    root.addHandler(file_handler)

    if not silent:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(logging.Formatter("%(message)s"))
        stream_handler.setLevel(level_int)
        root.addHandler(stream_handler)

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_int),
        cache_logger_on_first_use=True,
    )
```

Pros :

- Le fichier contient **toujours** tout — debug post-mortem possible même en mode silent.
- `tail -f` natif marche.
- Pas de fichier avec permissions user-insecure : `0o700` sur le parent + `0o600` sur le fichier (Python `RotatingFileHandler` hérite des umask → mettre `os.chmod(log_file, 0o600)` après création).

Cons :

- I/O disk : ~100 events/min → ~1 KB/min en JSON compact → rotation ~7 jours avant 1er fichier 10 MB. Cap 10 fichiers → ~70 jours de logs. Acceptable.

**Alternatives écartées** :

- **Seulement fichier, jamais stdout** : perd la possibilité `--verbose` pour debug interactif.
- **Seulement stdout** (M1..M8 actuel) : impossible de reconstituer les events post-crash.
- **journalctl / syslog** : dépend de l'OS (WSL, systemd pas toujours dispo). Fichier simple = universel.

### 2.3 Écran TUI refresh strategy

**Recommandation : render 1 fois au boot + re-render uniquement quand un module change de statut.**

Mécanisme : `MonitoringOrchestrator` (M4) expose déjà un système d'events structurés. M9 ajoute un **observer** simple : si un event `*_stopped` ou `*_started` ou `*_degraded` est loggé → re-render. Implémentation via un processor structlog custom qui emit un `status_changed` event local :

```python
def status_change_observer(status_store: StatusStore):
    def processor(logger, method_name, event_dict):
        event = event_dict.get("event")
        if event in _RELEVANT_STATUS_EVENTS:
            status_store.update_from_event(event_dict)
        return event_dict
    return processor
```

`StatusStore` est un dict in-memory partagé avec le CLI screen. Re-render sur change : `console.clear()` + `render_status_screen(...)`.

**Alternatives écartées** :

- **Polling la DB toutes les 5 s pour status** : lent, décolle des events réels.
- **Live refresh complet ala htop** : flicker, pas utile pour un signal "est-ce que ça tourne".
- **Pas de re-render après boot** : perd l'info "module a crashé", user voit toujours ✅.

### 2.4 Onglet `/logs` dashboard — lecture fichier, filtres, live tail

**Recommandation : read `LOG_FILE` + `os.SEEK_END - N_BYTES` pour lire les `DASHBOARD_LOGS_TAIL_LINES` dernières lignes. Filtres côté serveur (level, event_type, q search). HTMX polling 2 s pour live tail.**

```python
async def read_log_tail(log_file: Path, max_lines: int) -> list[LogEntry]:
    """Lit les dernières N lignes d'un fichier JSONL sans charger tout en RAM."""
    if not log_file.exists():
        return []
    lines = []
    # Read from end, chunk-by-chunk
    with log_file.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        chunk_size = 64 * 1024
        buf = b""
        while size > 0 and len(lines) <= max_lines:
            read_size = min(chunk_size, size)
            size -= read_size
            f.seek(size)
            chunk = f.read(read_size)
            buf = chunk + buf
            lines = buf.split(b"\n")
        lines = lines[-max_lines:]
    entries: list[LogEntry] = []
    for line in lines:
        if not line:
            continue
        try:
            data = json.loads(line)
            entries.append(LogEntry.model_validate(data))
        except (json.JSONDecodeError, ValidationError):
            continue
    return entries
```

**Filtres** : après tail read, filtrer en Python (pas SQL) :

```python
def filter_entries(entries, *, levels: set[str] | None, event_types: set[str] | None, q: str | None):
    result = entries
    if levels:
        result = [e for e in result if e.level.upper() in levels]
    if event_types:
        result = [e for e in result if e.event in event_types]
    if q:
        q_lower = q.lower()
        result = [e for e in result if q_lower in json.dumps(e.model_dump()).lower()]
    return result
```

**Live tail** : `hx-trigger="every 2s[document.visibilityState==='visible']"` sur `/partials/logs-tail`. Polling agressif 2 s acceptable (lecture fichier locale, ~5 ms).

**Download** : `GET /logs/download` → `StreamingResponse(file.open("rb"), media_type="text/plain")`. Cap size = taille current du fichier (capé à `LOG_FILE_MAX_BYTES`).

**Alternatives écartées** :

- **Lecture DB** : les logs ne sont pas persistés en DB (cf. M4/M7 design).
- **WebSocket** : cohérence M4.5 §2.3 "pas de SSE/WS".
- **Charger tout le fichier à chaque poll** : O(taille). Mauvais à 10 MB.
- **Index full-text** : over-engineering pour 500 lignes.

### 2.5 README — structure narrative vs référence

**Recommandation : hybride. En-tête "landing page" + quickstart + tutorial linéaire puis référence (env vars, architecture) en section détachée.**

Ordre :

1. **Hero** : logo, pitch 1 phrase, badges (Python 3.11+, License MIT, CI status optional, Code Coverage).
2. **Hook** : "Copie le trading de vos traders Polymarket préférés, sans lever le petit doigt." + screenshot dashboard home.
3. **Quickstart 5 min** : 3 commandes, screenshot terminal attendu, screenshot dashboard home.
4. **Tutorial illustré pas-à-pas** : 7 étapes :
   - Step 1 : installer WSL (si Windows) — screenshot.
   - Step 2 : clone + `bash scripts/setup.sh` — screenshot terminal output attendu.
   - Step 3 : éditer `.env` avec 1 wallet — screenshot VSCode.
   - Step 4 : `python -m polycopy --dry-run` — screenshot CLI silencieux.
   - Step 5 : ouvrir dashboard — screenshot Home M6.
   - Step 6 : activer Telegram via BotFather — screenshot BotFather conversation.
   - Step 7 : passer en prod (avec checklist warnings) — schéma décisionnel.
5. **FAQ** : 8-10 questions fréquentes (légalité, capital initial, "ça marche bien", PnL plonge, etc.).
6. **Comparaison** : table 3 colonnes (polycopy / bot-concurrent-1 / bot-concurrent-2) sur 8 critères (open-source, langage, dashboard, scoring auto, dry-run semi-réel, Telegram, licence, dernière maj).
7. **Hall of Fame** : 5-10 wallets publics connus (avec disclaimer "aucune endorsement").
8. **Architecture** (section repliée ou lien vers `docs/architecture.md`).
9. **Variables d'environnement** (table complète conservée, mais en bas / dans un détail HTML).
10. **Going live** (section existante, conservée).
11. **Roadmap**.
12. **Avertissement** (renforcé).

Pros :

- Un nouveau visiteur lit la hero + hook + quickstart (~2 min) et sait s'il est intéressé.
- Un user avancé saute directement à la table env vars.
- Tutorial linéaire pour qui veut "tout faire proprement" (le cas typique premier run).

Cons :

- README devient long (~500-700 lignes). Mitigé : sections `<details>` HTML pour les parties référence (GitHub rend `<details>` natif).

**Alternatives écartées** :

- **README minimaliste + doc sur site externe** : introduit un autre repo / GH Pages / MkDocs. Pas de valeur ajoutée pour un bot perso.
- **Tutoriel uniquement, référence séparée dans `docs/`** : fragmenté, GitHub prioritaire sur `README.md` scroll.
- **Video tutorial embedded** : GIF > 500 KB tue le loading README. Screenshots PNG compressés.

### 2.6 Screenshots — Playwright vs captures manuelles

**Recommandation : Playwright scripté via `scripts/capture_screenshots.py` (opt-in via `pip install -e ".[docs]"`).**

Script :

```python
# scripts/capture_screenshots.py
async def capture_all(output_dir: Path) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await ctx.new_page()

        # Screenshot 1 : dashboard home
        await page.goto("http://127.0.0.1:8787/home")
        await page.wait_for_selector('[data-testid="kpi-cards"]', timeout=5000)
        await page.screenshot(path=output_dir / "dashboard-home.png")

        # Screenshot 2 : dashboard /traders
        await page.goto("http://127.0.0.1:8787/traders")
        await page.screenshot(path=output_dir / "dashboard-traders.png")

        # Screenshot 3 : dashboard /pnl
        await page.goto("http://127.0.0.1:8787/pnl")
        await page.wait_for_selector('#pnl-chart', timeout=5000)
        await page.screenshot(path=output_dir / "dashboard-pnl.png")

        await browser.close()
```

**Prérequis** : bot lancé localement sur le port 8787 avec des données de démo pré-seedées (fixtures DB). Script associé `scripts/seed_demo_db.py` qui populate une DB temporaire avec 10 trades, 3 ordres, 2 positions, 5 snapshots PnL.

**Screenshots non-dashboard** :

- `terminal-silent-cli.png` : capture asciinema → png via `asciinema-automation` ou simplement manuelle + Shotwell. **v1 : capture manuelle** (une seule fois, peu susceptible de changer souvent). Documenter le process dans `scripts/capture_screenshots.py` docstring.
- `botfather-conversation.png` : capture manuelle (Telegram Desktop).
- `vscode-env-edit.png` : capture manuelle.

**Alternatives écartées** :

- **imgbot / GitHub Actions captures** : CI complexité.
- **Tout Playwright** : le CLI et VSCode ne sont pas web. Manuel inévitable pour ces 3.

### 2.7 FAQ content — juridique / capital / fiabilité

**Recommandation : 8 questions, réponses courtes, responsabilité partagée.**

Exemples :

> **Est-ce légal dans mon pays ?**
> Polymarket est inaccessible depuis certaines juridictions (notamment USA). Le code polycopy lui-même est neutre — ce que tu en fais relève du droit local. Si tu es résident US, Canada, Royaume-Uni, France (hors DOM-TOM) ou certaines autres juridictions, **vérifie avec un juriste** avant d'utiliser Polymarket. L'auteur de polycopy ne donne aucun conseil juridique.

> **Combien je dois mettre au départ ?**
> **Minimum pour un test** : $5 en USDC sur ton proxy wallet Polymarket. Lance `MAX_POSITION_USD=1` pendant 1-2 semaines. Si tu es satisfait, monte par paliers (`$5`, `$20`, `$100`). Ne dépasse jamais ce que tu peux perdre.

> **Comment je sais que le bot tourne bien ?**
> 3 signaux : (1) le dashboard `/healthz` répond 200, (2) Telegram heartbeat (M7) arrive toutes les 12 h, (3) les snapshots PnL sont datés de < 10 min (`sqlite3 polycopy.db "SELECT MAX(timestamp) FROM pnl_snapshots"`).

> **Que faire si mon PnL plonge ?**
> Si drawdown ≥ `KILL_SWITCH_DRAWDOWN_PCT=20%`, le kill switch coupe automatiquement le bot (M4, uniquement en mode live). Sinon, tu peux : (1) mettre `DRY_RUN=true` et redémarrer (observer sans risque), (2) baisser `MAX_POSITION_USD`, (3) retirer les pires wallets via `UPDATE target_traders SET active=0 WHERE wallet_address='0x...'`.

Etc. 4 autres : stack utilisé, hidden costs (gas, fees), comparaison manuel vs bot, issues GitHub.

**Alternatives écartées** :

- **FAQ longue (20+ questions)** : noie le signal. 8 suffit.
- **FAQ non mise à jour** : inclure une `Last reviewed: <date>` en bas et mettre à jour à chaque milestone.
- **Pas de FAQ légale** : sujet sensible mais le disclaimer actuel README est vague ; le rendre explicite protège l'auteur.

---

## 3. Arborescence du module — `src/polycopy/cli/` (nouveau)

Changements minimaux, 1 nouveau sous-package `cli/` :

```
src/polycopy/
├── __main__.py                       RÉÉCRIT (boot parse args + delegate à cli/runner.py)
├── cli/
│   ├── __init__.py                   NOUVEAU
│   ├── runner.py                     NOUVEAU (orchestre le boot : logging, DB, TaskGroup)
│   ├── status_screen.py              NOUVEAU (rendu rich + StatusStore)
│   ├── logging_config.py             NOUVEAU (RotatingFileHandler + stdout conditionnel)
│   └── version.py                    NOUVEAU (lit git SHA + pyproject version, cached)
└── dashboard/
    ├── log_reader.py                 NOUVEAU (read_log_tail + filter_entries)
    ├── queries.py                    (étendu : get_log_tail délègue à log_reader)
    ├── routes.py                     (+ /logs + /logs/download + /partials/logs-tail)
    └── templates/
        ├── logs.html                 RÉÉCRIT (remplace M6 stub logs_stub.html)
        └── partials/
            └── logs_tail.html        NOUVEAU

scripts/
├── capture_screenshots.py            NOUVEAU (Playwright, opt-in)
└── seed_demo_db.py                   NOUVEAU (populate demo data pour screenshots)

assets/
└── screenshots/                       NOUVEAU
    ├── dashboard-home.png
    ├── dashboard-traders.png
    ├── dashboard-pnl.png
    ├── terminal-silent-cli.png
    ├── vscode-env-edit.png
    ├── botfather-conversation.png
    └── logo.svg                       NOUVEAU (remplace Polymarket logo hero README)
```

**Pas de nouveau module top-level fonctionnel** : `cli/` est une couche de présentation, pas un module métier. Respecte la règle CLAUDE.md "aucun module ne dépend d'un autre module fonctionnel" — `cli/` dépend de `config/` et coordonne les autres orchestrators via `runner.py` (le nouveau `__main__` ultra-minimal).

---

## 4. API externes / structures — `LogEntry` DTO

### 4.1 `LogEntry` — parsing d'une ligne JSON structlog

```python
class LogEntry(BaseModel):
    """Une ligne du fichier log JSON structlog (parsée best-effort)."""
    model_config = ConfigDict(extra="allow", frozen=True)

    timestamp: datetime | None = Field(None, alias="timestamp")
    level: str = Field("INFO", alias="level")
    event: str = "(no_event)"
    logger: str | None = None
    # Le reste des fields structlog sont preserved via extra="allow"
```

`extra="allow"` car structlog peut écrire n'importe quel binding (`wallet`, `tx_hash`, `asset_id`...). Le frontend les affiche tous dans un `<details>` dépliable.

### 4.2 Filtres query params

```python
@router.get("/logs", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    levels: Annotated[list[str] | None, Query()] = None,
    events: Annotated[list[str] | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> HTMLResponse: ...
```

Validation stricte :

- `levels` : liste limitée aux 5 levels standard (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`). Autres → 400.
- `q` : max 200 chars. Pas de regex exposée (performance + ReDoS).
- `events` : liste cap 20 (évite requête géante avec des centaines d'events).

---

## 5. Storage (inchangé à M9)

M9 ne touche pas aux modèles, repositories, migrations. Les logs vivent dans le fichier système, pas en DB.

**Rappel** : la FAQ M9 mentionne `SELECT MAX(timestamp) FROM pnl_snapshots` en troubleshooting. Lecture read-only via sqlite CLI, pas via M9.

---

## 6. Queries / routes / templates dashboard (extensions M9)

### 6.1 `log_reader.py` — `read_log_tail` + `filter_entries`

Cf. §2.4 pseudocode. Pure async I/O, testable en unit.

### 6.2 Extensions `routes.py`

```python
@router.get("/logs", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    levels: ..., events: ..., q: ...,
) -> HTMLResponse:
    settings = get_settings(request)
    if not settings.dashboard_logs_enabled:
        return templates.TemplateResponse("logs_stub.html", {"request": request, "reason": "DASHBOARD_LOGS_ENABLED=false"})
    entries = await read_log_tail(settings.log_file, settings.dashboard_logs_tail_lines)
    filtered = filter_entries(entries, levels=set(levels or []), event_types=set(events or []), q=q)
    context = {
        "request": request,
        "entries": filtered,
        "filter_levels": levels or [],
        "filter_events": events or [],
        "filter_q": q or "",
    }
    return templates.TemplateResponse("logs.html", context)


@router.get("/partials/logs-tail", response_class=HTMLResponse)
async def logs_tail_partial(request: Request, levels, events, q) -> HTMLResponse:
    # Same logic, renvoie fragment
    ...


@router.get("/logs/download")
async def logs_download(request: Request) -> StreamingResponse:
    settings = get_settings(request)
    if not settings.dashboard_logs_enabled:
        raise HTTPException(403, "Logs download disabled")
    log_file = settings.log_file
    if not log_file.exists():
        raise HTTPException(404, "Log file not found")
    return StreamingResponse(
        log_file.open("rb"),
        media_type="text/plain",
        headers={"Content-Disposition": "attachment; filename=polycopy.log"},
    )
```

Toujours `GET`. Invariant M4.5/M6 préservé.

### 6.3 Template `logs.html`

```html
{% extends "base.html" %}
{% block title %}Logs{% endblock %}
{% block content %}
<h2>Logs</h2>

<form hx-get="/logs" hx-push-url="true" class="flex gap-2 mb-4" hx-trigger="change, keyup delay:500ms from:input[name='q']">
  <label>
    <select name="levels" multiple>
      {% for lvl in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] %}
        <option value="{{ lvl }}" {% if lvl in filter_levels %}selected{% endif %}>{{ lvl }}</option>
      {% endfor %}
    </select>
  </label>
  <input type="text" name="q" placeholder="Recherche texte" value="{{ filter_q }}">
  <a href="/logs/download" class="btn">Télécharger .log</a>
  <label>
    <input type="checkbox" id="live-tail-toggle"> Live tail (2 s)
  </label>
</form>

<div id="logs-container"
     hx-get="/partials/logs-tail?..."
     hx-trigger="{% if live_tail %}every 2s[document.visibilityState==='visible']{% else %}load{% endif %}"
     hx-swap="innerHTML">
  {% include "partials/logs_tail.html" %}
</div>

<script>
  document.getElementById('live-tail-toggle').addEventListener('change', (e) => {
    const container = document.getElementById('logs-container');
    if (e.target.checked) {
      container.setAttribute('hx-trigger', "every 2s[document.visibilityState==='visible']");
    } else {
      container.setAttribute('hx-trigger', 'load');
    }
    htmx.process(container);
  });
</script>
{% endblock %}
```

`partials/logs_tail.html` rend une liste :

```html
{% for entry in entries %}
  <details class="log-entry log-{{ entry.level|lower }}">
    <summary>
      <time>{{ entry.timestamp | humanize_dt }}</time>
      <span class="badge badge-{{ entry.level|lower }}">{{ entry.level }}</span>
      <code>{{ entry.event }}</code>
    </summary>
    <pre><code>{{ entry.model_dump_json(indent=2) }}</code></pre>
  </details>
{% endfor %}
{% if not entries %}
  <p class="text-muted">Aucun log trouvé.</p>
{% endif %}
```

### 6.4 Suppression de `logs_stub.html` M6

Le template `logs_stub.html` créé par M6 (placeholder "arrive en M9") est **conservé** mais utilisé uniquement si `DASHBOARD_LOGS_ENABLED=false` (avec un contenu adapté : "Logs désactivés via config"). Si `=true` → vraie page `logs.html`.

---

## 7. Orchestration — nouveau `__main__` + `cli/runner.py`

### 7.1 `__main__.py` minimaliste

```python
"""Entrypoint CLI du bot polycopy."""
import sys
from polycopy.cli.runner import main

if __name__ == "__main__":
    sys.exit(main())
```

### 7.2 `cli/runner.py`

```python
def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = _load_settings()
    _apply_cli_overrides(settings, args)

    _configure_logging(
        level=settings.log_level,
        log_file=settings.log_file,
        max_bytes=settings.log_file_max_bytes,
        backup_count=settings.log_file_backup_count,
        silent=settings.cli_silent and not args.verbose,
    )

    modules_status = _build_initial_module_status(settings)
    if settings.cli_silent and not args.verbose and not args.no_cli:
        render_status_screen(settings, modules_status, version=get_version())

    try:
        asyncio.run(_async_main(settings, modules_status))
    except KeyboardInterrupt:
        _render_shutdown_message(settings)
        return 0
    except Exception as e:
        log.exception("polycopy_crashed", error=str(e))
        _render_crash_message(settings, e)
        return 1
    return 0


async def _async_main(settings, modules_status) -> None:
    # Logique équivalente à __main__ M8 actuel :
    # - create_engine_and_session
    # - init_db
    # - asyncio.TaskGroup avec watcher/strategy/executor/monitoring/dashboard/discovery
    ...
```

### 7.3 `_build_initial_module_status`

Inspecte `settings.*_enabled` pour chaque module et construit la liste :

```python
def _build_initial_module_status(settings) -> list[ModuleStatus]:
    return [
        ModuleStatus(name="Watcher", enabled=True, detail=f"{len(settings.target_wallets)} wallets pinned"),
        ModuleStatus(name="Strategy", enabled=True, detail="4 filtres actifs"),
        ModuleStatus(name="Executor", enabled=True, detail="simulé" if settings.dry_run else "LIVE"),
        ModuleStatus(name="Monitoring", enabled=True, detail=f"Telegram {'ON' if settings.telegram_bot_token else 'OFF'}, PnL {settings.pnl_snapshot_interval_seconds // 60} min"),
        ModuleStatus(name="Dashboard", enabled=settings.dashboard_enabled, detail=f"http://{settings.dashboard_host}:{settings.dashboard_port}" if settings.dashboard_enabled else "désactivé"),
        ModuleStatus(name="Discovery", enabled=settings.discovery_enabled, detail=f"{settings.discovery_interval_seconds // 3600}h cycle, {settings.scoring_version}" if settings.discovery_enabled else "désactivé"),
    ]
```

---

## 8. Tests

### 8.1 Arborescence

```
tests/
├── fixtures/
│   └── log_sample.jsonl                   NOUVEAU (~50 lignes JSON structlog variées)
├── unit/
│   ├── test_cli_logging_config.py         NOUVEAU
│   ├── test_cli_status_screen.py          NOUVEAU (render rich → stdout capture)
│   ├── test_cli_runner.py                 NOUVEAU
│   ├── test_cli_version.py                NOUVEAU
│   ├── test_dashboard_log_reader.py       NOUVEAU (read_tail + filters)
│   ├── test_dashboard_logs_route.py       NOUVEAU (ASGITransport + fixture log file)
│   ├── test_dashboard_logs_download.py    NOUVEAU
│   └── test_readme_links.py               NOUVEAU (no broken internal links)
└── integration/
    └── test_cli_subprocess_smoke.py       NOUVEAU @pytest.mark.integration
```

### 8.2 `test_cli_logging_config.py`

- `silent=True` → file handler ajouté, pas de stream handler.
- `silent=False` → 2 handlers actifs.
- Log file parent créé avec mode `0o700`.
- Log file créé avec mode `0o600` après premier write.
- Rotation : écrire > `max_bytes` → fichier `.1` créé, original tronqué.

### 8.3 `test_cli_status_screen.py`

- Render avec 6 modules, tous enabled → output contient 6× `✅`.
- Render avec Discovery disabled → `⏸️` sur cette ligne.
- Render dry-run → border cyan (via `rich.get_console().export_text(styles=True)`).
- Render LIVE → border red.
- Re-render après `StatusStore.update_from_event` → nouveau rendu cohérent.
- Dry-run badge + chemin log file affichés.

### 8.4 `test_cli_runner.py`

- `main(["--dry-run"])` avec settings mocked → exit 0 rapide (stop_event immediate set).
- `main(["--verbose", "--dry-run"])` → logging config `silent=False`.
- `main(["--no-cli", "--dry-run"])` → pas d'écran rich, fichier log OK.
- KeyboardInterrupt simulé → exit 0, shutdown message rich rendu.
- Exception inattendue → exit 1, crash message rich.

### 8.5 `test_dashboard_log_reader.py`

- `read_log_tail` sur fixture 50 lignes avec `max_lines=10` → 10 dernières.
- Fichier inexistant → `[]`.
- JSON malformé → ignoré silencieusement.
- `filter_entries` levels → seulement WARNING+ERROR gardés.
- `filter_entries` q="wallet" → entries avec "wallet" dans JSON dump.
- `filter_entries` events={"trade_detected"} → seulement cet event.
- Combinaison levels + events + q : intersection correcte.

### 8.6 `test_dashboard_logs_route.py`

- `GET /logs` avec `DASHBOARD_LOGS_ENABLED=true` + fixture log → 200 HTML avec lignes.
- `GET /logs?levels=ERROR&q=executor` → seulement les matches filtrés.
- `GET /logs` avec `DASHBOARD_LOGS_ENABLED=false` → 200 HTML "logs désactivés" (stub).
- `GET /logs?levels=INVALID` → 400 validation error.
- `GET /logs?q=<200 chars>` → 400 max_length.
- `GET /partials/logs-tail` → 200 HTML fragment only.

### 8.7 `test_dashboard_logs_download.py`

- `GET /logs/download` avec `LOG_FILE` existant → 200 `text/plain`, content = file content.
- `GET /logs/download` avec fichier inexistant → 404.
- `GET /logs/download` avec `DASHBOARD_LOGS_ENABLED=false` → 403.
- `Content-Disposition: attachment; filename=polycopy.log` exact (pas de user-controlled filename).

### 8.8 `test_readme_links.py`

- Parse `README.md` via simple regex `\[.*?\]\((.*?)\)`.
- Pour chaque lien interne (commence par `./` ou `docs/` ou `assets/` ou `specs/`) → vérifier que le fichier existe.
- Pour chaque lien asset `assets/screenshots/*.png` → vérifier présence.
- Links externes (https://) : skip (pas de check réseau en test).

### 8.9 `test_cli_subprocess_smoke.py` (integration)

```python
@pytest.mark.integration
def test_silent_cli_smoke(tmp_path):
    env = os.environ.copy()
    env["TARGET_WALLETS"] = "0x0000000000000000000000000000000000000001"
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    env["LOG_FILE"] = str(tmp_path / "test.log")
    env["CLI_SILENT"] = "true"
    env["DRY_RUN"] = "true"

    proc = subprocess.Popen(
        [sys.executable, "-m", "polycopy", "--dry-run"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    time.sleep(5)
    proc.send_signal(signal.SIGINT)
    stdout, _ = proc.communicate(timeout=10)
    text = stdout.decode()
    # L'écran rich a été rendu
    assert "polycopy" in text
    assert "Watcher" in text
    assert "Dashboard" in text
    # Pas de JSON stdout
    assert '"event": "watcher_started"' not in text
    # Mais le fichier log contient des JSON
    log_text = (tmp_path / "test.log").read_text()
    assert '"event"' in log_text
```

### 8.10 Couverture

```bash
pytest --cov=src/polycopy/cli --cov=src/polycopy/dashboard --cov-report=term-missing
```

Seuils : ≥ 80 % sur `src/polycopy/cli/` + maintenu sur `src/polycopy/dashboard/`. Non-régression M1..M8 ≥ 80 %.

---

## 9. README — structure détaillée

### 9.1 Header

```markdown
<p align="center">
  <img src="assets/screenshots/logo.svg" alt="polycopy" width="180">
</p>

<h1 align="center">polycopy</h1>

<p align="center">
  <em>Copie le trading de vos traders Polymarket préférés, sans lever le petit doigt.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11+-blue.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License MIT">
  <img src="https://img.shields.io/badge/status-personal%20prototype-orange.svg" alt="Personal prototype">
</p>

<p align="center">
  <img src="assets/screenshots/dashboard-home.png" alt="Dashboard home" width="720">
</p>

---

⚠️ **Statut : prototype personnel, pas un produit.** Pas de garantie. Trade à tes risques. Lis l'[Avertissement](#avertissement) avant tout usage réel.
```

### 9.2 Section Quickstart 5 min

```markdown
## Quickstart (5 minutes)

\`\`\`bash
# 1. Clone
git clone https://github.com/<user>/polycopy ~/code/polycopy && cd ~/code/polycopy

# 2. Setup (idempotent, ~2 min)
bash scripts/setup.sh

# 3. Configure 1 wallet à copier dans .env
# (édite TARGET_WALLETS avec une adresse publique Polymarket)

# 4. Lance
source .venv/bin/activate
python -m polycopy --dry-run
\`\`\`

Après 3-5 secondes, tu devrais voir :

![Terminal silent CLI](assets/screenshots/terminal-silent-cli.png)

Ouvre `http://127.0.0.1:8787/` (si `DASHBOARD_ENABLED=true`) :

![Dashboard home](assets/screenshots/dashboard-home.png)

**C'est tout.** Le bot est en dry-run : il détecte les trades de ton wallet cible et log ce qu'il ferait, **sans jamais envoyer d'ordre**.
```

### 9.3 Section Tutorial pas-à-pas

7 sous-sections `<details>` pliables, chacune avec screenshot + explication. Cf. §2.5 structure.

### 9.4 Section FAQ

8 questions-réponses courtes. Cf. §2.7.

### 9.5 Section Comparaison

```markdown
## Comparaison avec d'autres bots Polymarket

| Critère | polycopy | polytracker-bot | pm-copy-trader |
|---|---|---|---|
| Open-source | ✅ MIT | ✅ GPL | ❌ closed |
| Langage | Python 3.11 | TypeScript | Rust |
| Dashboard local | ✅ M4.5+M6 | ❌ | ❌ |
| Scoring auto | ✅ M5 | ❌ | ✅ ML |
| Dry-run semi-réel | ✅ M8 | ❌ | ❌ |
| Alertes Telegram | ✅ M7 enrichies | ✅ basiques | ❌ |
| Installation WSL-friendly | ✅ `scripts/setup.sh` | ⚠️ manuel | ❌ x86 only |
| Dernière maj | 2026-04 | 2024-08 | 2025-11 |

_Liste non exhaustive. Pas d'endorsement — l'auteur de polycopy ne connaît pas personnellement ces projets, infos basées sur README publics à T0._
```

### 9.6 Section Hall of Fame

```markdown
## Hall of Fame — wallets publics notables

Des wallets Polymarket dont le track record a été documenté publiquement. **Aucune endorsement** — ces infos viennent de posts publics / `/holders` / leaderboards tiers. Utilise `/traders` dans le dashboard pour leur score live.

| Pseudonyme / label | Adresse proxy | Réputation | Source |
|---|---|---|---|
| Fredi9999 | `0xabc…def` | Gros volumes macro | [Tweet public 2026-02](...) |
| CoinBaseUser7 | `0xdef…abc` | ROI > 40 % sur élections | [Post Substack](...) |
| ... | ... | ... | ... |

**Ne copie jamais aveuglément** — vérifie via `python scripts/score_backtest.py --wallets-file custom.txt --as-of <date> --observe-days 30`.
```

### 9.7 Sections conservées

Architecture, Stack, Going live, Telegram (étendu M7), Découverte (M5), Dashboard (M4.5 + M6), Roadmap, Avertissement. Cf. README actuel. Mise à jour minimale à M9 :

- Roadmap : cocher `[x] **M6** ...`, `[x] **M7** ...`, `[x] **M8** ...`, `[x] **M9** : CLI silencieux + onglet /logs + README overhaul`.
- Ajouter les 6 env vars M9.
- Lien vers `docs/setup.md` §18 pour `LOG_FILE` / `CLI_SILENT`.

---

## 10. Mises à jour de documentation

### 10.1 `README.md` — refonte complète §9

### 10.2 `docs/architecture.md`

Nouvelle section après Module Dashboard :

```markdown
## CLI / Logging (M9)

> **Status M9** ✅ — CLI silencieux par défaut (écran `rich` statique), logs JSON rotatifs vers `~/.polycopy/logs/polycopy.log` (10 MB × 10 fichiers), onglet `/logs` dashboard (lecture fichier, filtres + live tail HTMX 2 s). Flag `--verbose` restaure le stream stdout. `__main__.py` devient minimaliste ; la logique boot vit dans `src/polycopy/cli/runner.py`. Voir `specs/M9-silent-cli-and-readme.md`.
```

### 10.3 `CLAUDE.md`

Section "Conventions de code" — ajouter :

```markdown
- **CLI entrypoint M9** : `__main__.py` est minimaliste. Toute logique boot dans `src/polycopy/cli/runner.py`. Rendu terminal via `rich` (dépendance explicite). Par défaut silencieux (`CLI_SILENT=true`). Flag `--verbose` restaure le stream JSON stdout.
- **Logs M9** : destination par défaut = `~/.polycopy/logs/polycopy.log` via `RotatingFileHandler` (10 MB × 10). Permissions 0o700/0o600. Fichier **toujours** écrit, même en `--verbose` (double stream). Pas de logs en DB.
```

Section "Sécurité", ajouter :

```markdown
- **Logs file M9** : `~/.polycopy/logs/polycopy.log` peut contenir wallets publics, condition_ids, timestamps — **non sensible en soi** mais **à ne pas partager tel quel** (identifie ta stratégie). Permissions 0o600. Endpoint `/logs/download` bind localhost + `DASHBOARD_LOGS_ENABLED=true` only.
```

### 10.4 `docs/setup.md`

Nouvelle **section 18** :

```markdown
## 18. Logs et CLI silencieux (M9)

Par défaut, `python -m polycopy` affiche un écran statique avec les modules actifs et le lien du dashboard. Les logs JSON détaillés vont dans `~/.polycopy/logs/polycopy.log`.

### Changer le chemin log file

\`\`\`env
LOG_FILE=/tmp/polycopy-run1.log
LOG_FILE_MAX_BYTES=5242880       # 5 MB
LOG_FILE_BACKUP_COUNT=5
\`\`\`

### Restaurer l'ancien mode verbose (M1..M8)

\`\`\`bash
python -m polycopy --dry-run --verbose
# ou
CLI_SILENT=false python -m polycopy --dry-run
\`\`\`

### Mode daemon (systemd, nohup)

\`\`\`bash
python -m polycopy --no-cli > /dev/null 2>&1 &
# Tous les logs vont dans LOG_FILE, aucun stdout/stderr.
\`\`\`

### Onglet /logs dashboard

Dans `.env` : `DASHBOARD_LOGS_ENABLED=true` (default). Ouvre `http://127.0.0.1:8787/logs`. Filtres level + recherche texte + download. Live tail toggle (polling 2 s).

### Troubleshooting

- **Fichier log vide** → vérifier permissions `~/.polycopy/logs/` (mode 0o700 attendu).
- **Écran rich manquant** → terminal non-TTY ? Vérifier `python -c "import sys; print(sys.stdout.isatty())"`. Sur Jenkins / CI : utiliser `--no-cli`.
- **Rotation jamais déclenchée** → `LOG_FILE_MAX_BYTES` trop haut vs volume de logs. Baisse ou laisse le fichier grossir.
- **Endpoint /logs 404** → `DASHBOARD_LOGS_ENABLED=false`, ou `DASHBOARD_ENABLED=false` tout court.
```

### 10.5 `docs/setup.md` — section 19 "Capture des screenshots" (opt-in)

```markdown
## 19. Regénérer les screenshots README (opt-in)

Les PNG du README vivent dans `assets/screenshots/` et sont générés via Playwright.

\`\`\`bash
pip install -e ".[docs]"
playwright install chromium
# Dans un terminal : lance le bot avec des données de démo
python scripts/seed_demo_db.py
DASHBOARD_ENABLED=true python -m polycopy --dry-run &
# Dans un autre terminal : capture
python scripts/capture_screenshots.py --output assets/screenshots/
# Stop le bot
kill %1
\`\`\`

Captures manuelles nécessaires (CLI + BotFather + VSCode) : documentées dans `scripts/capture_screenshots.py` en docstring.
```

---

## 11. Commandes de vérification finale

```bash
ruff check .
ruff format --check .
mypy src
pytest --cov=src/polycopy/cli --cov=src/polycopy/dashboard --cov-report=term-missing    # ≥ 80 %
pytest                                                                                    # non-régression M1..M8

# Smoke test CLI silent
python -m polycopy --dry-run &
sleep 3
ls ~/.polycopy/logs/polycopy.log                                                          # fichier existe
test "$(stat -c '%a' ~/.polycopy/logs/polycopy.log)" = "600"                              # permissions
kill %1 && wait                                                                           # exit 0 propre

# Smoke test --verbose
python -m polycopy --dry-run --verbose 2>&1 | head -5 | grep -q '"event"'                 # JSON stdout présent

# Smoke test --no-cli
python -m polycopy --dry-run --no-cli 2>&1 | head -1                                      # vide ou quasi-vide

# Dashboard /logs
DASHBOARD_ENABLED=true python -m polycopy --dry-run &
sleep 3
curl -sSf http://127.0.0.1:8787/logs | grep -q 'polycopy'                                 # HTML rendu
curl -sSf http://127.0.0.1:8787/logs/download -o /tmp/dl.log                              # download OK
kill %1 && wait

# Vérif README links
pytest tests/unit/test_readme_links.py -v

# Capture screenshots (optional)
pip install -e ".[docs]" && playwright install chromium
python scripts/seed_demo_db.py
DASHBOARD_ENABLED=true python -m polycopy --dry-run &
sleep 5
python scripts/capture_screenshots.py --output /tmp/screenshots/
ls /tmp/screenshots/*.png                                                                 # 3 PNG (home, traders, pnl)
kill %1 && wait
```

---

## 12. Critères d'acceptation

- [ ] `python -m polycopy --dry-run` affiche un écran `rich` statique avec logo + 6 modules + dry-run badge + lien dashboard (si enabled) + chemin log file + "Ctrl+C pour arrêter". Aucun JSON stdout.
- [ ] `python -m polycopy --dry-run --verbose` affiche les JSON stdout + écrit le fichier log (double stream). Équivalent comportement M8.
- [ ] `python -m polycopy --dry-run --no-cli` n'affiche rien sur stdout (headless mode). Fichier log OK.
- [ ] Fichier log créé à `~/.polycopy/logs/polycopy.log` (ou `LOG_FILE` override). Permissions 0o600. Parent dir 0o700.
- [ ] Rotation : fichier > `LOG_FILE_MAX_BYTES` → rotation `.1` créée, cap à `LOG_FILE_BACKUP_COUNT` fichiers.
- [ ] Ctrl+C → shutdown propre avec message rich "🛑 arrêt propre". Exit 0.
- [ ] Crash inattendu → crash message rich avec stack trace. Exit 1. Fichier log preserve le traceback complet.
- [ ] `GET /logs` dashboard rend la page avec 500 dernières entries. Filtres levels + events + q fonctionnent côté serveur.
- [ ] `GET /logs?levels=ERROR&q=executor` renvoie seulement les entries matchant (level ERROR + texte "executor").
- [ ] Live tail HTMX polling 2 s fonctionne (toggle checkbox). Pas d'implémentation WebSocket (cohérence M4.5).
- [ ] `GET /logs/download` sert le fichier log complet en `text/plain` avec filename hardcodé `polycopy.log`.
- [ ] `DASHBOARD_LOGS_ENABLED=false` → `/logs` rend le stub (ou `logs_stub.html` avec raison config).
- [ ] Toutes les routes FastAPI M9 restent `GET`. Test `test_dashboard_security_m6.py` inchangé (aucune régression).
- [ ] Aucun secret leaké dans les logs écrits fichier (`POLYMARKET_PRIVATE_KEY`, `TELEGRAM_BOT_TOKEN`, L2 creds) — grep du fichier log après un run smoke.
- [ ] README rendering GitHub : hero visible, badges OK, screenshots chargent, liens internes fonctionnels (`tests/unit/test_readme_links.py` passe).
- [ ] 7 screenshots dans `assets/screenshots/` : `dashboard-home.png`, `dashboard-traders.png`, `dashboard-pnl.png`, `terminal-silent-cli.png`, `vscode-env-edit.png`, `botfather-conversation.png`, `logo.svg`.
- [ ] FAQ contient 8 questions avec responses sourcées. Légalité + capital initial + monitoring + drawdown traités.
- [ ] Table de comparaison avec 2-3 bots concurrents + 8 critères.
- [ ] Hall of Fame avec 5-10 wallets publics + disclaimer "no endorsement".
- [ ] `scripts/capture_screenshots.py` fonctionne avec `playwright` installé (opt-in via `[project.optional-dependencies] docs`). Reproducible : un second run écrase les PNG existants à l'identique (± bruit pixel négligeable).
- [ ] `scripts/seed_demo_db.py` populate une DB avec 10 trades + 3 ordres + 2 positions + 5 snapshots PnL. Idempotent (nettoie la DB avant).
- [ ] `ruff check .` + `ruff format --check .` + `mypy src` (`--strict`) : 0 erreur.
- [ ] `pytest` : 0 échec. Coverage ≥ 80 % sur `src/polycopy/cli/`. Non-régression M1..M8 ≥ 80 %.
- [ ] Docs §10 à jour (`README.md` refondu, `docs/architecture.md` §CLI, `CLAUDE.md` +2 puces, `docs/setup.md` §18-19) dans le **même** commit.
- [ ] Commit final unique : `feat(cli,dashboard,docs): M9 silent CLI + /logs tab + README overhaul`.

---

## 13. Hors scope M9 (NE PAS implémenter)

- **TUI interactif full-screen** (Textual, `rich.live.Live`). Écran statique + re-render conditionnel suffit.
- **WebSocket / SSE pour `/logs` live tail**. HTMX polling 2 s suffit, cohérence M4.5.
- **Tail `-f` natif dans le dashboard** (spawner subprocess). Risque de zombies, complexité injustifiée.
- **Logs en DB** (`logs_persisted` table). Fichier rotatif suffit.
- **Télémétrie / crash reporter / analytics**. Vie privée user.
- **Auto-update check** (`check_for_updates()` au boot). Pas de registry central.
- **Notifications desktop** (libnotify, Toast Windows). Telegram M7 couvre.
- **Screencast MP4 / GIF** dans le README. PNG statiques.
- **i18n** (README anglais en plus du FR). Scope M10+.
- **Daemon / systemd unit** fourni. Documenté en setup.md §18.
- **Export Grafana / Datadog** des logs. Fichier JSON parseable par les outils tiers.
- **Filtres avancés** `/logs` (regex, dates from-to). Q simple + levels suffit.
- **Paging offset/limit** sur `/logs`. Cap 500 lignes + live tail suffit.
- **Clear log file** depuis le dashboard. Actions write interdites (M4.5 invariant).
- **Vendored `rich` / fallback pure-Python**. Rich est stable, dep acceptée.
- **Playwright in-package** (install auto). Opt-in `[docs]` extra.
- **Screenshots vidéo animés** pour montrer une action multi-step. PNG statiques.
- **Branding / theme customization** de l'écran rich CLI. Couleur dry-run vs LIVE suffit.
- **Readme dans une langue alternative** (EN / ES / DE). FR uniquement.

---

## 14. Notes d'implémentation + zones d'incertitude

### 14.1 Ordre de travail suggéré

1. **Ajouter 6 env vars** dans `config.py` + `.env.example` + validators (`log_file` via `Path.expanduser()`).
2. **Vérifier `rich` dep** : `python -c "import rich"` dans le venv. Si absent, ajouter `rich>=13.0,<15.0` aux deps + `pip install -e ".[dev]"`.
3. **Créer `src/polycopy/cli/`** avec `logging_config.py`, `status_screen.py`, `version.py`, `runner.py`.
4. **Tests unit** `test_cli_logging_config.py`, `test_cli_status_screen.py`, `test_cli_version.py`, `test_cli_runner.py`.
5. **Réécrire `__main__.py`** minimaliste.
6. **Créer `src/polycopy/dashboard/log_reader.py`** + tests.
7. **Étendre `dashboard/routes.py`** avec `/logs` + `/logs/download` + `/partials/logs-tail` + tests.
8. **Réécrire `templates/logs.html`** + `partials/logs_tail.html` (remplace stub M6).
9. **Smoke test manuel** : bot run, écran rich, dashboard `/logs`, download.
10. **Créer `scripts/seed_demo_db.py`** — populate data plausible.
11. **Créer `scripts/capture_screenshots.py`** Playwright.
12. **Run capture** : lancer le bot avec seed, capturer 3 PNG dashboard.
13. **Captures manuelles** (CLI, BotFather, VSCode) → `assets/screenshots/`.
14. **Créer `logo.svg`** — simple "P" stylisé ou reutiliser l'existant + vectoriser.
15. **Refondre `README.md`** section par section (garder une version branchée `README.old.md` temporaire pendant l'écriture).
16. **Créer `test_readme_links.py`** — check assets exist + internal links valid.
17. **Docs §10** dans le même commit.
18. **Commit unique** : `feat(cli,dashboard,docs): M9 silent CLI + /logs tab + README overhaul`.

### 14.2 Principes

- **Backwards compat** : flag `--verbose` restaure M1..M8 exactement. Tests existants utilisant `subprocess` + parse JSON stdout doivent passer avec `--verbose`.
- **Pas d'abstraction prématurée** : `status_screen.py` a 2 fonctions concrètes (`render_status_screen`, `render_shutdown_message`). Pas de classe `ScreenRenderer`.
- **File I/O simple** : `read_log_tail` utilise `os.SEEK_END` + chunk. Pas de `aiofiles` (sync suffisant pour 64 KB chunks en request handler — FastAPI supporte sync handlers).
- **Templates réutilisent les filtres M6** (`humanize_dt`, etc.) — pas de nouveau filter.
- **Sessions DB non impliquées** : `/logs` lit uniquement le fichier. Aucune query DB sur cette page.
- **README = produit marketing** : priorité UX lecteur > exhaustivité. Détails techniques → `docs/` ou sections pliables.

### 14.3 Décisions auto-arbitrées

1. **`CLI_SILENT=true` par défaut** : breaking change M8→M9. Les users qui tail stdout pour monitoring cassent. **Mitigation** : flag `--verbose` restaure. Documenter breaking change dans CHANGELOG / README roadmap.
2. **Écran rich statique (pas live)** : simple, robuste, testable.
3. **`~/.polycopy/logs/polycopy.log` par défaut** : cohérent avec la convention XDG-like. `/tmp` candidat rejeté (non persistant cross-boot).
4. **Permissions 0o600 file + 0o700 parent** : protège des co-users sur un host multi-tenant. WSL single-user : redundant mais safe.
5. **Onglet `/logs` dans sidebar M6** : remplacement direct du stub M6. Pas de redesign.
6. **HTMX polling 2 s pour live tail** : cohérence M4.5/M6 (même pattern que partials).
7. **500 lignes cap** : RAM navigateur + render time raisonnable. Plus = bouton "Télécharger .log".
8. **Pas de filtres avancés** (regex, dates from-to) : levels + q text + events suffisent à 80 % des cas.
9. **Playwright en `[docs]` extra** : pas au critical path (5 % des users veulent regenerate screenshots).
10. **`scripts/seed_demo_db.py` idempotent** : nettoie avant d'insérer. Evite data multiplication sur runs répétés.
11. **Hall of Fame** : 5-10 wallets max pour éviter impression d'endorsement. Disclaimer fort.
12. **Comparaison 2-3 bots concurrents** : ne pas aller chercher tout le marché. Montrer qu'on n'est pas seuls, qu'on a des features distinctes.
13. **FAQ 8 questions** : cap raisonnable pour la lecture linéaire.
14. **Screenshots 1280×720** : compromis qualité / poids PNG (~100-200 KB chacun).
15. **`--no-cli` mode daemon** : pour systemd + nohup + cron. Documenté.

### 14.4 Pièges anticipés

1. **`rich.Console().print()` dans un sub-process stdout non-TTY** : rich détecte automatiquement et fallback ASCII. Vérifier en test `test_cli_subprocess_smoke.py`.
2. **`RotatingFileHandler` + concurrency** : plusieurs process écrivant le même fichier → race. M9 est mono-process, pas d'issue.
3. **Permissions 0o600 sur Windows** : `os.chmod` sur NTFS ≠ POSIX. Best-effort, pas d'assertion stricte en test Windows.
4. **`Path.expanduser()` sur Windows** : utiliser `Path.home()` en fallback si `~` pose problème.
5. **Rotation pendant que le dashboard lit** : `read_log_tail` peut rencontrer un fichier re-créé entre ouverture et read. `try/except (OSError, FileNotFoundError)` + retourner `[]`.
6. **JSON multiline via `StackInfoRenderer`** : un traceback écrit plusieurs lignes non-parsables. `LogEntry` parse best-effort, skip lignes mal formées.
7. **`StreamingResponse` avec file handle ouvert** : fermer après stream. `yield from` pattern FastAPI ou `with open(...) as f: yield from f`.
8. **Download endpoint fait crasher le dashboard sur gros fichier** : cap `LOG_FILE_MAX_BYTES` protège (10 MB max). Download = 10 MB streamé = quelques secondes.
9. **Query param `q` avec caractères spéciaux** (`<script>`) : Jinja `autoescape=True` sur dashboard (HTML templates) → safe. Double-check via test.
10. **Screenshots Playwright flaky** : wait explicit sur `data-testid` selectors, pas sur timing arbitraire.
11. **Live tail checkbox state** : perdu au swap HTMX. JS localStorage preference ? **Décision** : état non-persisté, re-click à chaque pageload. Acceptable.
12. **README GitHub rendering** : `<p align="center">` + `<img src>` supportés ; `<details><summary>` aussi. Tester le rendu sur GitHub preview avant merge.
13. **Badges shields.io** : URLs externes pouvant 404 si le repo n'est pas public/nommé. Utiliser `static` badges inline si besoin.
14. **Hall of Fame wallets devenus inactifs** : l'adresse reste valide mais le wallet peut ne plus trader. Ajouter `Last active: <date>` par entrée.
15. **Asciinema vs simple screenshot** pour terminal CLI : simple PNG suffit à M9. Asciinema = reportable M9.1.

### 14.5 Zones d'incertitude à lever AVANT implémentation

1. **Brief user disait "M9 → M6 → M8 → M7"** : impossible strictement — M9 a besoin des layouts M6 (pour stub /logs replacement) et des features M7/M8 (pour FAQ complète + screenshots). **Décision spec** : M9 ship **après** M6 + M7 + M8. Signaler à l'utilisateur. Le brief est une suggestion, pas contractuel.

2. **`rich` dep transitive** : à vérifier en `pip show rich` dans le venv actuel. Si transitive via `pydantic[all]` ou autre → pas d'ajout explicite nécessaire. Si pure transitive d'une lib optionnelle risquant de disparaître → ajout explicite.

3. **Playwright install size** : `playwright install chromium` = ~150 MB. **Trop** pour un install user standard — d'où l'extra `[docs]` opt-in. Documenter explicitement.

4. **Screenshot reproducibility** : même command run 2 fois peut donner des pixels différents (dates, sparklines aléatoires). **Mitigation** : `scripts/seed_demo_db.py` utilise des timestamps fixes + random seed fixé. Screenshots deviennent pixel-identiques run-to-run.

5. **README hero image** : un bon dashboard screenshot nécessite que M6 soit déjà shipped (cf. 14.5 §1). Si M6 pas encore mergé → M9 utilise un placeholder. Recommandation : ne pas merger M9 sans M6 mergé avant.

6. **Logo SVG** : à designer. Options : (a) "P" stylisé dans un hexagone, (b) réutiliser `assets/Company_Logo_Polymarket.png` existant (risque de confusion marque), (c) commissionner un logo ($50-100). **v1** : SVG simple généré via code (texte "p" + cercle) jusqu'à ce qu'un designer livre. Placeholder acceptable.

7. **Hall of Fame ethics** : lister un wallet public sans consentement de l'owner = grey area. **Décision** : restreindre aux wallets déjà cités publiquement (source link obligatoire : tweet / article / leaderboard tiers). Disclaimer "no endorsement" visible.

8. **FAQ légalité** : l'auteur polycopy n'est pas juriste. **Décision** : phrasing "vérifie avec un juriste" explicit + liste non-exhaustive des juridictions à risque. Protège l'auteur d'un claim de "conseil juridique".

9. **Comparaison avec bots concurrents** : info peut être périmée / inexacte. **Décision** : footnote "infos basées sur README publics à T0, non exhaustif, PR welcome pour corriger".

10. **`--no-cli` + terminal TTY** : est-ce que redirection `> /dev/null` suffit ? Oui, mais `--no-cli` est plus explicite et désactive aussi le setup rich.

11. **`LOG_FILE` default path Windows** : `~/.polycopy/logs/` → `C:\Users\<user>\.polycopy\logs\`. Fonctionne sous Windows/WSL. Mais `Path.expanduser()` sur Windows sans WSL peut être ambigu. **Décision** : doc setup.md mention "Windows natif non officiellement supporté, WSL recommandé" (cohérent README).

12. **Tests subprocess en CI** : `test_cli_subprocess_smoke.py` demande un vrai Python subprocess. Marque `@pytest.mark.integration` + skip en CI si besoin.

13. **Vieille stdout logs qui attendent sur un écran rich rendu** : un hook structlog qui écrit sur stdout override le rich screen. **Solution** : ne pas ajouter `StreamHandler(stdout)` si `silent=True`, cohérent §2.2.

---

## 15. Prompt à m'envoyer pour lancer l'implémentation

Copie-colle tel quel quand tu es prêt :

```
/implement-module M9

Suis specs/M9-silent-cli-and-readme.md à la lettre. Pas d'invocation skill Polymarket requise — M9 est 100 % CLI + dashboard + docs.

Avant tout code, actions obligatoires :

1. Vérifier que `rich` est disponible dans le venv :
   pip show rich
   Si absent, ajouter `rich>=13.0,<15.0` à pyproject.toml deps principales puis pip install -e ".[dev]".

2. Vérifier que M6, M7, M8 sont mergés AVANT M9. Sinon STOP et signale — M9 assume leur présence (stub /logs M6 à remplacer, FAQ Telegram M7, FAQ dry-run M8). Cf. zone §14.5 point 1.

3. Capturer 1 fichier log sample via smoke test du bot actuel :
   python -m polycopy --dry-run > /dev/null 2>&1 &
   sleep 10
   cp polycopy.log tests/fixtures/log_sample.jsonl   # ou depuis le path stdout capture M1..M8
   kill %1

Ensuite suis l'ordre §14.1.

Contraintes non négociables :

- CLI_SILENT=true par défaut (BREAKING change M8→M9). Flag --verbose restaure le comportement M1..M8 stdout JSON. Documenté en breaking change.
- Fichier log TOUJOURS écrit (même en --verbose), permissions 0o600 + parent 0o700. `~/.polycopy/logs/polycopy.log` par défaut.
- RotatingFileHandler 10 MB × 10 fichiers par défaut.
- Ne PAS toucher aux processors structlog M1..M8. M9 ajoute uniquement 1 file handler + conditionnel stream handler.
- `__main__.py` devient minimaliste (3 lignes). Logique boot entièrement dans src/polycopy/cli/runner.py.
- Écran rich statique au boot. Re-render sur status change uniquement (observer simple). Pas de rich.live.Live.
- Aucun secret loggé dans le fichier (grep automatisé : POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER, TELEGRAM_BOT_TOKEN, L2 creds, GOLDSKY_API_KEY).
- Onglet /logs dashboard : read fichier only (pas DB). HTMX polling 2s pour live tail. Pas de WebSocket/SSE.
- Filtres serveur : levels (enum strict), events (liste cap 20), q (max_length 200). Validation Pydantic Query.
- /logs/download : GET only, filename hardcoded polycopy.log, Content-Type text/plain, 404 si fichier absent.
- Toutes les routes M9 sont GET. Test test_dashboard_security_m6.py doit passer sans diff.
- DASHBOARD_LOGS_ENABLED=true par défaut. False → /logs rend stub avec raison config.
- README refonte : hero + quickstart + tutorial 7 étapes illustrées + FAQ 8 questions + comparaison 2-3 bots + Hall of Fame + sections référence pliables.
- Screenshots : 3 dashboard via Playwright (scripts/capture_screenshots.py opt-in [docs]) + 3 manuels (CLI, BotFather, VSCode) + logo.svg.
- scripts/seed_demo_db.py idempotent : nettoie avant insert. Pour screenshots reproductibles, timestamps fixes + random seed fixe.
- Comparaison bots : infos sourcées, footnote "non exhaustif, PR welcome".
- Hall of Fame : 5-10 wallets, source link par entrée, disclaimer no endorsement.
- FAQ légalité : "vérifie avec un juriste" explicit. Liste juridictions à risque non exhaustive.
- Rich fallback ASCII si non-TTY (auto via rich). Testé en subprocess.
- --no-cli mode daemon : zéro stdout, tout vers fichier.
- Conventions CLAUDE.md (async, Pydantic v2, SQLAlchemy 2.0, structlog, docstrings FR / code EN, pas de print — sauf rich console output dans cli/).
- mypy --strict propre, ruff propre, pytest ≥ 80% coverage sur src/polycopy/cli/ et src/polycopy/dashboard/. Non-régression M1..M8 ≥ 80%.
- Tests via subprocess (integration opt-in), ASGITransport (routes), fixture file log pour log_reader.
- Playwright en optional-dependencies [docs]. pip install -e ".[docs]" + playwright install chromium.
- Doc updates §10 dans le même commit (README refondu + architecture §CLI + CLAUDE +2 puces + setup §18 + §19).
- Commit final unique : feat(cli,dashboard,docs): M9 silent CLI + /logs tab + README overhaul

Demande-moi confirmation avant tout patch sensible :
- pyproject.toml (ajout rich explicite si non transitive ; optional-dependencies [docs]).
- __main__.py (réécriture complète — garder version M8 en backup local si doute).
- config.py (6 env vars + validators, notamment Path.expanduser pour log_file).
- Suppression ou renommage d'un screenshot existant (ne pas écraser assets/ sans confirmation).
- README.md : si la version actuelle sera remplacée → garder un README.M8.md backup temporaire.

Si une zone §14.5 se confirme problématique pendant l'implémentation (ex: Playwright trop lourd, rich cassé en terminal WSL, README trop long GitHub tronque, Hall of Fame éthique floue, M6/M7/M8 pas encore mergés), STOP et signale.

Smoke test final obligatoire avant merge :
- python -m polycopy --dry-run → écran rich rendu, pas de JSON stdout, fichier log écrit.
- python -m polycopy --dry-run --verbose → JSON stdout + fichier.
- python -m polycopy --dry-run --no-cli → rien sur stdout, fichier.
- DASHBOARD_ENABLED=true → /logs charge 500 entries, filtres levels+q fonctionnent, /logs/download streame le fichier.
- README rendered GitHub : hero + quickstart + tutorial lisibles, screenshots chargent, liens internes OK.
- Capture des 7 screenshots dans assets/screenshots/ (3 via Playwright + 4 manuels). Attacher la liste à la PR.
```
