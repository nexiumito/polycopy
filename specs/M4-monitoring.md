# M4 — Monitoring

Spec d'implémentation du Module 4 de polycopy : visibilité opérationnelle via alertes Telegram sur événements critiques, snapshots PnL périodiques, script de rapport PnL et migrations Alembic. **Pas de UI web**, **pas de Prometheus** (hors scope §14).

Source de vérité fonctionnelle : `docs/architecture.md` section "Monitoring". Conventions : `CLAUDE.md`. Schémas API : skill `/polymarket:polymarket` (rappel — `WalletStateReader` réutilisé), Telegram Bot API officielle (https://core.telegram.org/bots/api). Templates : `specs/M1-watcher-storage.md`, `specs/M2-strategy-engine.md`, `specs/M3-executor.md`.

---

## 0. Pré-requis environnement

### 0.1 Bootstrap (déjà fait)

`bash scripts/setup.sh` (idempotent). Aucun nouveau patch config requis.

### 0.2 Skill Polymarket (déjà installé)

Rappel uniquement : `WalletStateReader` (M3) est réutilisé par le `PnlSnapshotWriter` pour calculer le `total_position_value_usd`. Pas de nouvel endpoint Polymarket à découvrir à M4.

### 0.3 `.env` — variables Telegram (OPTIONNELLES)

Telegram est **entièrement optionnel** à M4. Si absent, AlertDispatcher log les alertes localement et ne POST rien (no-op silencieux). Aucun crash, aucune exception.

| Variable env | Champ Settings | Default | Requis quand |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | `telegram_bot_token` | `None` | uniquement pour activer les alertes |
| `TELEGRAM_CHAT_ID` | `telegram_chat_id` | `None` | uniquement pour activer les alertes |

**Pas-à-pas pour activer les alertes Telegram (5 min)** :

1. Sur Telegram, cherche `@BotFather` (compte officiel vérifié) → envoie `/newbot`.
2. Choisis un nom (ex: `polycopy local bot`) puis un username terminé par `bot` (ex: `polycopy_local_bot`).
3. BotFather te répond avec un token format `123456789:ABCdef...` → copie-le dans `.env` comme `TELEGRAM_BOT_TOKEN=...`.
4. Cherche **ton bot** sur Telegram, ouvre la conversation, envoie-lui n'importe quel message (ex: `/start`).
5. Dans un navigateur, ouvre `https://api.telegram.org/bot<TON_TOKEN>/getUpdates`.
6. Repère dans le JSON le champ `"chat": {"id": 12345678, ...}` → copie cet `id` dans `.env` comme `TELEGRAM_CHAT_ID=12345678`.
7. Redémarre `python -m polycopy --dry-run` et observe le log `telegram_enabled` au lieu de `telegram_disabled`.

Doc officielle : https://core.telegram.org/bots/features#botfather

### 0.4 Nouvelles env vars (toutes OPTIONNELLES)

| Variable env | Champ Settings | Default | Description |
|---|---|---|---|
| `PNL_SNAPSHOT_INTERVAL_SECONDS` | `pnl_snapshot_interval_seconds` | `300` | Période entre 2 snapshots PnL (5 min default). |
| `ALERT_LARGE_ORDER_USD_THRESHOLD` | `alert_large_order_usd_threshold` | `50.0` | Seuil USD au-dessus duquel un fill déclenche `order_filled_large`. |
| `ALERT_COOLDOWN_SECONDS` | `alert_cooldown_seconds` | `60` | Anti-spam par event_type ; pas de DB, in-memory. |

À ajouter à `config.py` ET `.env.example`. Aucune n'est requise — tous les defaults marchent.

### 0.5 Critère de validation "environnement"

```bash
PNL_SNAPSHOT_INTERVAL_SECONDS=5 python -m polycopy --dry-run
```

Doit logger en plus des M1/M2/M3 events :

- `telegram_disabled` (warning) si pas de token, ou `telegram_enabled` sinon.
- `monitoring_started`.
- `pnl_snapshot_writer_started`.
- `alert_dispatcher_started`.
- `pnl_snapshot_written` (au moins 1 fois après 5s).
- `monitoring_stopped` au shutdown.
- Exit 0 sur SIGINT.

---

## 1. Objectif M4 (scope exact)

Donner au bot une **boucle de feedback opérationnelle** :

- **Snapshots PnL périodiques** persistés en DB (`pnl_snapshots` peuplé enfin) → permet le calcul du drawdown all-time-high pour le kill switch.
- **Kill switch fonctionnel** : déclenché par le `PnlSnapshotWriter` à chaque snapshot dont le drawdown dépasse `KILL_SWITCH_DRAWDOWN_PCT`. Stop le bot via le `stop_event` partagé.
- **Alertes Telegram** sur 5 événements critiques (kill switch, exec auth fatal, exec error, gros fill, drawdown warning).
- **Rapport PnL** générable à la main : `python scripts/pnl_report.py --since 7 --output html`.
- **Migrations DB** via Alembic — fini le `rm polycopy.db` après chaque modif schéma.

**Hors livrable M4** : UI web, dashboard temps réel, Prometheus `/metrics`, multi-process, auto-cancel positions au kill switch, scoring de traders.

---

## 2. PnlSnapshot — table + repository + writer

### 2.1 Schéma `PnlSnapshot` (extension)

État actuel (M1, vide) : `id, timestamp, total_usdc, realized_pnl, unrealized_pnl, drawdown_pct` (toutes nullable).

**Ajouts + tightening pour M4** :

| Colonne | Type | Contrainte |
|---|---|---|
| `id` | `int` | PK autoincrement |
| `timestamp` | `datetime` | **indexed**, default `now(UTC)`, nullable=False |
| `total_usdc` | `float` | nullable=False, default 0.0 (= `total_position_value_usd` + `available_capital_usd_stub`) |
| `realized_pnl` | `float` | nullable=False, default 0.0 |
| `unrealized_pnl` | `float` | nullable=False, default 0.0 (= sum `cashPnl` Data API positions) |
| `drawdown_pct` | `float` | nullable=False, default 0.0 |
| `open_positions_count` | `int` | nullable=False, default 0 (**nouveau**) |
| `cash_pnl_total` | `float \| None` | nullable=True (= sum `cashPnl` Data API ; en dry-run reste `None`) (**nouveau**) |
| `is_dry_run` | `bool` | nullable=False, default False (**nouveau** — distingue snapshots "vrais" des stubs) |

`is_dry_run` permet à `pnl_report.py` de filtrer les snapshots utiles (vrais wallet) vs stubs (dev local). Sans cette colonne on mélange les deux.

### 2.2 `PnlSnapshotRepository` — `src/polycopy/storage/repositories.py`

```python
class PnlSnapshotRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None: ...

    async def insert(self, dto: PnlSnapshotDTO) -> PnlSnapshot: ...
    async def get_max_total_usdc(self, *, only_real: bool = True) -> float | None: ...
    async def get_latest(self, *, only_real: bool = True) -> PnlSnapshot | None: ...
    async def list_since(
        self,
        since: datetime,
        *,
        only_real: bool = True,
    ) -> list[PnlSnapshot]: ...
```

- `get_max_total_usdc(only_real=True)` filtre `is_dry_run=False` — utilisé par le calcul de drawdown pour ne pas mélanger.
- Append-only (jamais d'update).

`PnlSnapshotDTO` Pydantic frozen vit dans `storage/dtos.py` (cohérence avec les autres DTOs de repos) :

```python
class PnlSnapshotDTO(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_usdc: float
    realized_pnl: float
    unrealized_pnl: float
    drawdown_pct: float
    open_positions_count: int
    cash_pnl_total: float | None
    is_dry_run: bool
```

### 2.3 `PnlSnapshotWriter` — `src/polycopy/monitoring/pnl_writer.py`

```python
class PnlSnapshotWriter:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        wallet_state_reader: WalletStateReader,
        alerts_queue: asyncio.Queue[Alert],
    ) -> None: ...

    async def run(self, stop_event: asyncio.Event) -> None: ...
```

`run` :

1. Log `pnl_snapshot_writer_started` (interval=...).
2. Boucle `while not stop_event.is_set()` :
   - `state = await wallet_state_reader.get_state()` (réutilise M3 — gère dry-run automatiquement).
   - `total = state.total_position_value_usd + state.available_capital_usd`.
   - `max_ever = await repo.get_max_total_usdc(only_real=not settings.dry_run)` (compare apples to apples).
   - `drawdown_pct = ((max_ever - total) / max_ever * 100) if (max_ever and max_ever > 0) else 0.0`.
   - Insert `PnlSnapshotDTO(total_usdc=total, ..., is_dry_run=settings.dry_run)`.
   - Log `pnl_snapshot_written` (binding : total_usdc, drawdown_pct, open_positions_count).
   - **Kill switch** : si `drawdown_pct >= settings.kill_switch_drawdown_pct` ET `not settings.dry_run` → push `Alert(level="CRITICAL", event="kill_switch_triggered", body=..., cooldown_key="kill_switch")` + `stop_event.set()`. Log `kill_switch_triggered` error. **Important** : kill switch ne se déclenche **JAMAIS en dry-run** (sinon le bot se tue tout seul à chaque test sur des stubs).
   - **Drawdown warning** : si `drawdown_pct >= 0.75 * settings.kill_switch_drawdown_pct` mais < seuil → push `Alert(level="WARNING", event="pnl_snapshot_drawdown", body=..., cooldown_key="drawdown_warning")`. Pas de stop.
   - `await asyncio.wait_for(stop_event.wait(), timeout=settings.pnl_snapshot_interval_seconds)` (sleep interruptible).
3. Sur `asyncio.CancelledError` : raise.
4. Sur autre exception : log `pnl_writer_error`, backoff 30s (interruptible), continue.
5. Sur sortie : log `pnl_snapshot_writer_stopped`.

**Decision : kill switch logic vit ici, pas dans `RiskManager` (M2).**

Justification : à M2 le `RiskManager` lisait drawdown depuis `pnl_snapshots` mais la table était vide → drawdown toujours 0 → kill switch jamais déclenché. À M4 on aurait pu refactorer `RiskManager` pour brancher `PnlSnapshotRepository` mais ça touche M2 sans nécessité — le `PnlSnapshotWriter` est un meilleur emplacement (single source of truth, déclenche périodiquement, sait set le `stop_event`). M2 reste inchangé. Le kill switch est désormais **temporel** (vérifié toutes les `PNL_SNAPSHOT_INTERVAL_SECONDS`), pas **par-trade** — acceptable car drawdown bouge lentement.

---

## 3. TelegramClient — `src/polycopy/monitoring/telegram_client.py`

### 3.1 Endpoint (verrouillé via doc officielle)

- **URL** : `https://api.telegram.org/bot<BOT_TOKEN>/sendMessage`
- **Méthode** : POST JSON
- **Auth** : token dans l'URL (pas dans header) — **ne JAMAIS log l'URL en clair**.
- **Rate limit** : 30 messages/seconde global, 1 message/seconde par chat. Pas critique pour notre usage (alertes éparses).

### 3.2 Request body

```json
{
  "chat_id": "<TELEGRAM_CHAT_ID>",
  "text": "<message Markdown>",
  "parse_mode": "MarkdownV2"
}
```

### 3.3 Response schema

Succès :
```json
{"ok": true, "result": {"message_id": 123, ...}}
```

Erreur :
```json
{"ok": false, "error_code": 400, "description": "Bad Request: ..."}
```

### 3.4 Choix `httpx` direct vs `python-telegram-bot`

**Recommandation : `httpx` direct.**

Justification :
- `python-telegram-bot` est un framework complet (bot polling, webhook, dispatcher, handlers) pour 1 seul endpoint REST → over-engineering.
- Cohérence avec `DataApiClient`, `GammaApiClient`, `ClobMetadataClient`, `WalletStateReader` (tous httpx async).
- Plus simple à mocker via respx pour les tests.
- `python-telegram-bot` peut être retiré des deps en follow-up si confirmé non utilisé ailleurs (note `# TODO M5: retirer si encore inutilisé`).

### 3.5 Client

```python
class TelegramClient:
    BASE_URL = "https://api.telegram.org"
    DEFAULT_TIMEOUT = 5.0

    def __init__(self, http_client: httpx.AsyncClient, settings: Settings) -> None:
        self._http = http_client
        self._token = settings.telegram_bot_token
        self._chat_id = settings.telegram_chat_id
        self._enabled = self._token is not None and self._chat_id is not None

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send(self, text: str) -> bool:
        """Retourne True si POST réussi, False sinon (incl. mode disabled)."""
        if not self._enabled:
            log.debug("telegram_send_skipped_disabled")
            return False
        # POST + retry tenacity sur 429/5xx
        ...
```

- **Sécurité** : `self._token` jamais loggé. Au boot, log `telegram_enabled` (ou `telegram_disabled`) sans aucune valeur. Si erreur HTTP, log `telegram_error` avec status_code et description **mais pas l'URL** (qui contient le token).
- Tenacity : `wait_exponential(min=1, max=5), stop_after_attempt(3)`, retry sur `httpx.TransportError` + `httpx.HTTPStatusError` 429/5xx uniquement (pas 400 — bad request).
- Sur 400 (mauvais chat_id ou markdown malformé) : log `telegram_error` + return False, ne pas retry.

### 3.6 Markdown escaping

Telegram MarkdownV2 a beaucoup de caractères spéciaux à échapper (`_*[]()~\`>#+-=|{}.!`). Pour M4 simplifier : utiliser `parse_mode="Markdown"` (V1, plus permissif) et limiter les payloads `body` à du texte simple sans caractères pénibles. Si futur besoin de formatting riche, helper `_escape_markdown_v2(text)` à ajouter.

---

## 4. AlertDispatcher + `Alert` DTO — `src/polycopy/monitoring/{dtos.py, alert_dispatcher.py}`

### 4.1 `Alert` DTO

```python
class Alert(BaseModel):
    """Événement critique à pousser sur la queue alertes."""

    model_config = ConfigDict(frozen=True)

    level: Literal["INFO", "WARNING", "ERROR", "CRITICAL"]
    event: str
    body: str  # message Telegram (Markdown V1 simple)
    cooldown_key: str | None = None  # None = jamais throttle
```

### 4.2 `AlertDispatcher`

```python
class AlertDispatcher:
    def __init__(
        self,
        queue: asyncio.Queue[Alert],
        telegram_client: TelegramClient,
        settings: Settings,
    ) -> None:
        self._queue = queue
        self._telegram = telegram_client
        self._cooldown_seconds = settings.alert_cooldown_seconds
        self._last_sent: dict[str, datetime] = {}

    async def run(self, stop_event: asyncio.Event) -> None: ...
```

`run` :

1. Log `alert_dispatcher_started` (binding : telegram_enabled=...).
2. Boucle `while not stop_event.is_set()` :
   - `alert = await asyncio.wait_for(queue.get(), timeout=1.0)` (poll interruptible).
   - Sur `TimeoutError` : continue.
   - Cooldown check : si `alert.cooldown_key` présent ET `now - last_sent[key] < cooldown_seconds` → log `alert_throttled` (debug) + `continue`. Sinon update `last_sent[key]=now`.
   - Préfixer `body` avec emoji selon level (`🟢` INFO, `🟡` WARNING, `🔴` ERROR, `🚨` CRITICAL) + ligne `*[{event}]*`.
   - `sent = await telegram.send(formatted_body)`.
   - Log `alert_sent` (binding : event, level) ou `alert_send_failed` (warning).
3. Sur sortie : log `alert_dispatcher_stopped`.

**Pas d'écriture DB** des alertes à M4 (uniquement logs structlog + envoi Telegram).

---

## 5. Producteurs d'alertes (modifications M2/M3)

### 5.1 Liste des events

| Event | Source | Condition | Cooldown key | Level |
|---|---|---|---|---|
| `kill_switch_triggered` | `PnlSnapshotWriter` | `drawdown_pct ≥ KILL_SWITCH_DRAWDOWN_PCT` (only_real, NOT in dry-run) | `"kill_switch"` | CRITICAL |
| `pnl_snapshot_drawdown` | `PnlSnapshotWriter` | `drawdown_pct ≥ 75% × KILL_SWITCH_DRAWDOWN_PCT` mais < 100% | `"drawdown_warning"` | WARNING |
| `executor_auth_fatal` | `ExecutorOrchestrator` (catch `ExecutorAuthError`) | toujours | `"auth"` | CRITICAL |
| `executor_error` | `execute_order` (exception non-auth) | toujours | `"executor_error"` | ERROR |
| `order_filled_large` | `_persist_result` (M3 pipeline) | `taking_amount/10⁶ ≥ ALERT_LARGE_ORDER_USD_THRESHOLD` | `"order_filled_large"` | INFO |

### 5.2 Modifications minimales aux orchestrators

Pattern identique à l'injection de queues M2→M3 :

- `WatcherOrchestrator.__init__` : ajouter `alerts_queue: asyncio.Queue[Alert] | None = None` (optionnel pour rétrocompat tests M1). À M4 le watcher n'émet pas d'alerte mais reçoit la queue par cohérence.
- `StrategyOrchestrator.__init__` : ajouter `alerts_queue: asyncio.Queue[Alert] | None = None`. Pas d'émission à M4 — M2 reste inchangé.
- `ExecutorOrchestrator.__init__` : ajouter `alerts_queue: asyncio.Queue[Alert] | None = None` ; injecte dans `execute_order` via param.
- `ExecutorOrchestrator.run_forever` : sur catch `ExecutorAuthError`, push `Alert(level="CRITICAL", event="executor_auth_fatal", ..., cooldown_key="auth")` AVANT de `stop_event.set()` + raise.

### 5.3 Modification minime de `execute_order` (M3 pipeline)

Ajouter param kwarg `alerts_queue: asyncio.Queue[Alert] | None = None` (default None pour rétrocompat tests M3). Push d'alerte aux 2 endroits :

1. Sur `Exception` capturée autour de `write_client.post_order(...)` (status FAILED) : push `Alert(level="ERROR", event="executor_error", body=..., cooldown_key="executor_error")`.
2. Dans `_persist_result`, branche `result.status == "matched"` : si `taking_amount` parsable ET `Decimal(taking_amount) / 10⁶ ≥ settings.alert_large_order_usd_threshold` → push `Alert(level="INFO", event="order_filled_large", body=..., cooldown_key="order_filled_large")`.

Tous les push utilisent `try: queue.put_nowait(alert) except asyncio.QueueFull: log warning` — jamais bloquant.

### 5.4 Pas de modif à M2 RiskManager

Comme noté §2.3, le kill switch logic vit dans `PnlSnapshotWriter`. `RiskManager` reste 100% inchangé, ses tests M2 inchangés.

---

## 6. Dashboard PnL — `scripts/pnl_report.py`

### 6.1 CLI

```bash
python scripts/pnl_report.py --since 7 --output html --db sqlite+aiosqlite:///polycopy.db
```

Args (argparse) :
- `--since` : nb jours d'historique (default 7).
- `--output` : `html` (default) | `csv` | `stdout`.
- `--db` : URL DB (default lit `.env` via Settings).
- `--include-dry-run` : flag pour inclure les snapshots dry-run (default False — pertinent uniquement en mode réel).

### 6.2 Lecture DB

Script **synchrone** (sqlite3 ou SQLAlchemy sync) — c'est un outil ponctuel, pas une boucle event loop. Pas de `asyncio.run()`. Réutilise les modèles SQLAlchemy mais via `create_engine` sync sur le même `polycopy.db`.

Données extraites :
- `pnl_snapshots` (filtré par `timestamp >= now - since_days` et `is_dry_run=False` sauf `--include-dry-run`).
- `my_orders` (counts par status sur la période).
- `my_positions` (positions ouvertes — count + total `size * avg_price`).

### 6.3 Stats calculées

- Latest `total_usdc`, latest `drawdown_pct`.
- Max drawdown sur la fenêtre.
- Variation `total_usdc` (Δ entre 1er et dernier snapshot de la période).
- Compteurs orders : SIMULATED, SENT, FILLED, REJECTED, FAILED.
- Nb positions ouvertes.

### 6.4 Output

- `stdout` : table tabulée plain text (zéro dep).
- `csv` : 1 ligne par snapshot, csv stdlib.
- `html` : f-string template inline (pas de Jinja). Sections : metadata, stats globales, table snapshots, **mini-graphique SVG inline** du `total_usdc` over time.

### 6.5 Choix : SVG natif vs matplotlib

**Recommandation : SVG natif Python (zéro dep).**

Justification :
- `matplotlib` ajoute ~50 MB de deps (numpy, pillow, etc.) pour 1 graphique de 200 px.
- Un sparkline SVG basique (polyline x/y normalisés sur la fenêtre) suffit pour visualiser une tendance.
- Code SVG natif = ~30 lignes Python, totalement testable.
- Si un jour besoin d'interactif → switch à plotly/bokeh, hors scope M4.

Helper `_render_sparkline_svg(timestamps, values, width=400, height=80) -> str` interne au script.

### 6.6 Tests

- 1 test smoke : génère un rapport `stdout` sur une DB vide → ne crash pas, output "no snapshots found".
- 1 test : insert 3 snapshots fixtures + génère `csv` → vérifie que les 3 lignes sont là.
- Pas de test du HTML (visual, à valider manuellement).

---

## 7. Migration Alembic

### 7.1 Ajout dep

Dans `pyproject.toml` `[project.optional-dependencies] dev` : ajouter `alembic>=1.13.0`.

### 7.2 Init

```bash
alembic init alembic
```

Crée `alembic/`, `alembic.ini`, `alembic/env.py`, `alembic/versions/`.

### 7.3 Configuration `alembic/env.py`

Modifier pour :
- Lire `DATABASE_URL` depuis `Settings` (pas depuis `alembic.ini`).
- Pointer `target_metadata = polycopy.storage.models.Base.metadata`.
- Mode async : utiliser `async_engine_from_config` + `run_async_migrations`. Le template Alembic standard est sync ; il faut adapter pour aiosqlite (cf. https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic).

### 7.4 Première migration "baseline"

```bash
alembic revision --autogenerate -m "baseline_m3_schema"
```

Génère `alembic/versions/<hash>_baseline_m3_schema.py` qui crée toutes les tables actuelles (`target_traders`, `detected_trades`, `strategy_decisions`, `my_orders`, `my_positions`, `pnl_snapshots`). **Auditer manuellement** la migration générée (autogenerate peut rater des indexes ou des contraintes — vérifier `UniqueConstraint("condition_id", "asset_id")` sur `my_positions`, les `index=True`, les `nullable=False`, etc.).

Ajouter une 2e migration **dans le même PR** pour les ajouts M4 sur `PnlSnapshot` (`open_positions_count`, `cash_pnl_total`, `is_dry_run`, tightening nullables) :

```bash
alembic revision --autogenerate -m "m4_pnl_snapshot_columns"
```

### 7.5 Modification de `init_db.py`

Remplacer `Base.metadata.create_all` par `alembic upgrade head` programmatique :

```python
from alembic import command
from alembic.config import Config

async def init_db(...):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", database_url)
    # alembic.command.upgrade est sync — wrap dans to_thread
    await asyncio.to_thread(command.upgrade, cfg, "head")
    # Puis upsert wallets via TargetTraderRepository (inchangé)
```

### 7.6 Documentation `docs/setup.md`

**Remplacer la section "Migration de schéma DB (M3+)"** (`rm polycopy.db`) par :

```markdown
## 10. Migration de schéma DB (M4+)

Alembic gère désormais les migrations. Workflow :

### Première installation (DB neuve)
`init_db` exécute automatiquement `alembic upgrade head` au boot — rien à faire.

### Après git pull qui modifie src/polycopy/storage/models.py
`init_db` détecte les nouvelles migrations et les applique au boot. **Tes données sont préservées.**

### Si tu as une DB préexistante de M3 (sans Alembic) à migrer
Une seule fois, marquer la DB comme "à jour avec la baseline M3" :
\`\`\`bash
source .venv/bin/activate
alembic stamp head
\`\`\`
Sans `stamp head`, `alembic upgrade head` plantera avec `table already exists`.

Cas extrême ("nuclear") : `rm polycopy.db && python -m polycopy --dry-run` repart de zéro (perte des données dev).

### Créer une nouvelle migration (dev)
\`\`\`bash
# Après modif src/polycopy/storage/models.py :
alembic revision --autogenerate -m "ma_migration"
# Audite le fichier généré dans alembic/versions/, puis :
alembic upgrade head
\`\`\`

⚠️ SQLite a des limites d'`ALTER TABLE` (pas de DROP COLUMN avant 3.35, pas de RENAME COLUMN avant 3.25). Pour drop/rename une colonne, Alembic auto-génère une stratégie "create new table + copy data + drop old". Auditer manuellement.
```

---

## 8. MonitoringOrchestrator + intégration `__main__`

### 8.1 `MonitoringOrchestrator` — `src/polycopy/monitoring/orchestrator.py`

```python
class MonitoringOrchestrator:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        alerts_queue: asyncio.Queue[Alert],
    ) -> None: ...

    async def run_forever(self, stop_event: asyncio.Event) -> None: ...
```

`run_forever` :

1. Construit `httpx.AsyncClient` partagé (réutilisé par `WalletStateReader` + `TelegramClient`).
2. Construit `WalletStateReader(http_client, settings)`, `TelegramClient(http_client, settings)`.
3. Log `monitoring_started` (`telegram_enabled=...`, `pnl_interval=...`).
4. Si `not telegram_client.enabled` : log `telegram_disabled` (warning).
5. Lance 2 sous-tâches dans un `asyncio.TaskGroup` interne :
   - `PnlSnapshotWriter(session_factory, settings, wallet_state_reader, alerts_queue).run(stop_event)`
   - `AlertDispatcher(alerts_queue, telegram_client, settings).run(stop_event)`
6. Sur sortie : `await http_client.aclose()`, log `monitoring_stopped`.

### 8.2 `__main__._run()` modifications

Ajouts :

```python
alerts_queue: asyncio.Queue[Alert] = asyncio.Queue(maxsize=100)
# ... existants : detected_trades_queue, approved_orders_queue, stop_event ...

watcher = WatcherOrchestrator(..., alerts_queue=alerts_queue)
strategy = StrategyOrchestrator(..., alerts_queue=alerts_queue)
executor = ExecutorOrchestrator(..., alerts_queue=alerts_queue)
monitoring = MonitoringOrchestrator(session_factory, settings, alerts_queue)

async with asyncio.TaskGroup() as tg:
    tg.create_task(watcher.run_forever(stop_event))
    tg.create_task(strategy.run_forever(stop_event))
    tg.create_task(executor.run_forever(stop_event))
    tg.create_task(monitoring.run_forever(stop_event))
```

Pas de nouvelle queue créée par M4 (alerts_queue est partagée). Pas de nouveau garde-fou démarrage (Telegram tolérant).

---

## 9. Tests

### 9.1 Arborescence

```
tests/
├── fixtures/
│   ├── (fixtures M1+M2+M3 existants)
│   └── telegram_send_message_response.json   # response Telegram fictive
├── unit/
│   ├── (tests M1+M2+M3 existants)
│   ├── test_pnl_snapshot_repository.py
│   ├── test_pnl_snapshot_writer.py
│   ├── test_telegram_client.py
│   ├── test_alert_dispatcher.py
│   ├── test_alert_producers.py             # vérif que M2/M3 push bien
│   ├── test_monitoring_orchestrator.py
│   ├── test_alembic_baseline.py
│   └── test_pnl_report_script.py
└── integration/
    ├── (tests M1+M2+M3 existants)
    └── test_telegram_send_live.py          # @pytest.mark.integration, opt-in
```

### 9.2 `conftest.py` (extension)

- `pnl_snapshot_repo` (dérivé de `session_factory`).
- `_telegram_disabled_settings()` / `_telegram_enabled_settings(token, chat_id)` helpers.
- `sample_telegram_response` : `{"ok": true, "result": {"message_id": 1, "date": ..., "chat": {...}, "text": "..."}}`.

### 9.3 `test_pnl_snapshot_repository.py`

- `insert` persiste, retourne avec id.
- `get_max_total_usdc(only_real=True)` ignore les `is_dry_run=True`.
- `get_latest(only_real=True)` retourne le dernier ; `None` si vide.
- `list_since` filtre par timestamp.

### 9.4 `test_pnl_snapshot_writer.py`

- 1 cycle dry-run : insert 1 snapshot avec `is_dry_run=True`, kill switch jamais déclenché même si drawdown > seuil.
- 1 cycle real : mock `WalletStateReader.get_state` → snapshot inséré, `is_dry_run=False`.
- Drawdown calculation : insert 2 snapshots préalables (max=1000), nouveau snapshot avec total=600 → `drawdown_pct=40`.
- Kill switch trigger : drawdown=25 (avec seuil=20, real mode) → `Alert(event="kill_switch_triggered")` push + `stop_event.set()`.
- Drawdown warning : drawdown=18 (75% de 20=15, donc 18 ≥ 15) → `Alert(event="pnl_snapshot_drawdown")` push, **stop_event NON set**.
- Exception interne : log error, continue.

### 9.5 `test_telegram_client.py` (respx)

- Disabled mode (no token) : `enabled is False`, `send()` → False, **aucune requête HTTP** émise (vérifié via respx).
- Enabled mode happy path : POST sur `https://api.telegram.org/bot<TOKEN>/sendMessage` → True. Vérifier que le `chat_id` et `text` sont bien dans le body JSON.
- 429 → retry → succès.
- 400 → False, pas de retry.
- 500 → retry → False après 3 tentatives.
- Vérifier qu'aucun `assert_called_with` n'expose le token dans le call args (respx route capture l'URL — vérifier que les **logs** ne contiennent pas le token via caplog).

### 9.6 `test_alert_dispatcher.py`

- Push 1 alert → send appelé 1 fois.
- 2 alertes même `cooldown_key` < 60s → 2e throttled (send appelé 1 fois total).
- 2 alertes même `cooldown_key` après cooldown expiré (monkeypatch `_now()`) → 2 sends.
- Cooldown_key None → jamais throttle.
- Stop event set → loop sort proprement.

### 9.7 `test_alert_producers.py`

- Mock `execute_order` flow : trigger SDK exception → vérifier `Alert(event="executor_error")` push sur queue.
- Trigger result success matched avec taking_amount=100000000 (100 USD) et threshold=50 → `Alert(event="order_filled_large")` push.
- Trigger ExecutorAuthError → vérifier orchestrator push `Alert(event="executor_auth_fatal")` AVANT raise.

### 9.8 `test_monitoring_orchestrator.py`

- Init OK avec/sans token.
- `run_forever` lance les 2 sous-tâches, log monitoring_started.
- Stop event → 2 sous-tâches stoppent, log monitoring_stopped.

### 9.9 `test_alembic_baseline.py`

```python
def test_alembic_upgrade_head_creates_all_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")  # sync URL pour Alembic
    command.upgrade(cfg, "head")
    # Vérifier que les 6 tables existent via inspect
    engine = create_engine(f"sqlite:///{db_path}")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {"target_traders", "detected_trades", "strategy_decisions",
            "my_orders", "my_positions", "pnl_snapshots"} <= tables
```

### 9.10 `test_pnl_report_script.py`

- Run script avec `--output stdout` sur DB vide → ne crash pas, message clair.
- Insert 3 snapshots fixtures via repo → run avec `--output csv` → vérifier 3 lignes CSV générées.
- Pas de test HTML (visual).

### 9.11 Test live opt-in `test_telegram_send_live.py`

```python
@pytest.mark.integration
async def test_send_real_message() -> None:
    settings = Settings(_env_file=".env")
    if settings.telegram_bot_token is None or settings.telegram_chat_id is None:
        pytest.skip("Telegram not configured in .env")
    async with httpx.AsyncClient() as http:
        client = TelegramClient(http, settings)
        sent = await client.send("polycopy integration test message")
    assert sent is True
```

### 9.12 Couverture

```bash
pytest --cov=src/polycopy/monitoring --cov-report=term-missing
```

Seuil : **≥ 80% sur `src/polycopy/monitoring/`**. M1+M2+M3 doivent rester ≥ 80% (pas de régression sur les modifs minimes des orchestrators).

---

## 10. Mises à jour de documentation (même PR)

### 10.1 `README.md`

- Cocher M4 dans "État d'avancement".
- Ajouter à la table env vars : `PNL_SNAPSHOT_INTERVAL_SECONDS`, `ALERT_LARGE_ORDER_USD_THRESHOLD`, `ALERT_COOLDOWN_SECONDS`. Mettre `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` colonne "Requis: non (pour alertes)".
- Ajouter section **"Alertes Telegram (optionnel)"** avec les 7 étapes pas-à-pas (cf. §0.3 spec).
- Ajouter section **"Rapport PnL"** :
  ```bash
  source .venv/bin/activate
  python scripts/pnl_report.py --since 7 --output html
  # → génère pnl_report.html, ouvrir dans un navigateur
  ```
- Mettre à jour Quickstart pour mentionner les snapshots toutes les 5 min (`PNL_SNAPSHOT_INTERVAL_SECONDS=300`).

### 10.2 `docs/architecture.md`

Ajouter en tête de la section "Monitoring" :

```markdown
> **Status M4** ✅ — implémenté. Alertes Telegram (httpx direct) avec cooldown 60s par event_type. PnL snapshots persistés toutes les 5 min via `WalletStateReader` (M3 réutilisé). Kill switch déclenché par le writer si drawdown ≥ `KILL_SWITCH_DRAWDOWN_PCT`. Alembic gère les migrations. Voir `specs/M4-monitoring.md` et `src/polycopy/monitoring/`.
```

### 10.3 `CLAUDE.md`

Section "Sécurité — RÈGLES STRICTES" — ajouter :

```markdown
- `TELEGRAM_BOT_TOKEN` ne doit JAMAIS être commit ni loggé en clair. Le token est visible dans l'URL de tous les appels `sendMessage` — utiliser HTTPS exclusif (httpx default), pas de log de l'URL en clair, rotation immédiate si token compromis.
```

Section "APIs Polymarket utilisées" — pas de changement (Telegram n'est pas Polymarket).

### 10.4 `docs/setup.md`

Remplacer la section "Migration de schéma DB (M3+)" par le workflow Alembic (cf. §7.6 spec). Ajouter une note sur `alembic stamp head` pour les utilisateurs qui ont une DB préexistante de M3.

---

## 11. Commandes de vérification finale

```bash
ruff check .
ruff format --check .
mypy src
pytest --cov=src/polycopy/monitoring --cov=src/polycopy/executor --cov=src/polycopy/strategy --cov=src/polycopy/watcher --cov=src/polycopy/storage --cov-report=term-missing
pytest -m integration                                # opt-in (Telegram, CLOB auth, Gamma, Data API)
alembic upgrade head                                 # applique migrations sur la DB courante
alembic current                                      # vérifie la version
PNL_SNAPSHOT_INTERVAL_SECONDS=5 python -m polycopy --dry-run  # ≥ 60s, log pnl_snapshot_written, exit 0 sur SIGINT
python scripts/pnl_report.py --since 7 --output html # génère pnl_report.html
```

---

## 12. Critères d'acceptation

- [ ] `python -m polycopy --dry-run` (sans Telegram) tourne **60 s** sans crash. Log : `polycopy_starting`, `db_initialized`, `watcher_started`, `strategy_started`, `executor_started`, `monitoring_started`, `telegram_disabled`, `pnl_snapshot_writer_started`, `alert_dispatcher_started`. Au moins 1 `pnl_snapshot_written` (avec `PNL_SNAPSHOT_INTERVAL_SECONDS=5`). Exit 0 sur SIGINT.
- [ ] Avec `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` valides : 1 message reçu sur Telegram via `pytest -m integration test_send_real_message` (vérifié manuellement).
- [ ] Aucun token Telegram dans les logs (vérification automatisée par grep dans `test_telegram_client.py` via `caplog`).
- [ ] `alembic upgrade head` applique sans erreur sur une DB vide ; `alembic current` retourne le head.
- [ ] Drawdown > `KILL_SWITCH_DRAWDOWN_PCT` en mode réel → `kill_switch_triggered` alert + `stop_event.set()` (testé en unit).
- [ ] **Drawdown jamais déclenché en dry-run** (testé en unit, sécurité critique).
- [ ] `ruff check .` + `ruff format --check .` + `mypy src` (--strict) : 0 erreur.
- [ ] `pytest` : 0 échec. Coverage ≥ 80% sur `monitoring/` ET pas de régression sur les autres couches.
- [ ] `python scripts/pnl_report.py --since 7 --output html` génère un fichier HTML lisible avec sparkline SVG.
- [ ] Tests M1+M2+M3 passent toujours (modifs minimales des orchestrators rétrocompatibles via `alerts_queue=None` default).
- [ ] Commit unique : `feat(monitoring): implement M4 telegram alerts, PnL snapshots and Alembic migrations`.

---

## 13. Hors scope M4 (NE PAS implémenter)

- Endpoint `/metrics` Prometheus — utilité limitée pour un bot solo, reporter à M5+ si besoin.
- Backtest framework.
- Scoring de traders, sélection automatique → **M5**.
- Multi-process / multi-VPS.
- Migration des données dev existantes via Alembic (acceptable de partir d'une DB vide post-Alembic init OU `alembic stamp head` sur DB existante).
- WebSocket user channel pour fills temps réel (FOK synchrone, pas besoin).
- Dashboard interactif (web app, plotly dash, etc.) — `scripts/pnl_report.py` HTML statique suffit.
- Auto-cancel des positions en cas de kill switch (M4 set juste le `stop_event` ; cancel = M5).
- Refactor de M2 RiskManager pour brancher PnlSnapshotRepository (kill switch logic vit dans le writer M4, M2 inchangé).
- Markdown V2 escaping rigoureux pour Telegram (M4 = Markdown V1 simple).
- `python-telegram-bot` removal des deps (cf. §3.4 — suggéré en follow-up).

---

## 14. Notes d'implémentation

**Ordre de travail suggéré** :

1. Préalable : skill query rapide `/polymarket:polymarket` (rappel `WalletStateReader` réutilisé).
2. Étendre `PnlSnapshot` (3 nouvelles colonnes) + `PnlSnapshotDTO` + `PnlSnapshotRepository` + tests repo.
3. Init Alembic (`alembic init alembic`, configurer `env.py` async, baseline migration M3 + migration M4 colonnes PnlSnapshot). Modifier `init_db.py` pour `alembic upgrade head`.
4. Test smoke `test_alembic_baseline.py` passe.
5. Créer `src/polycopy/monitoring/{__init__.py, dtos.py}` (`Alert`).
6. `telegram_client.py` + `test_telegram_client.py` (respx, vérifier no-leak token via caplog).
7. `alert_dispatcher.py` + `test_alert_dispatcher.py` (cooldown, monkeypatch `_now()`).
8. `pnl_writer.py` + `test_pnl_snapshot_writer.py` (kill switch trigger logic — éliminé en dry-run).
9. `monitoring_orchestrator.py` + `test_monitoring_orchestrator.py`.
10. Refactor producteurs alertes (Strategy + Executor) : injection `alerts_queue` (param optionnel), push sur events critiques. Tests M2/M3 toujours verts.
11. Refactor `__main__` : alerts_queue + MonitoringOrchestrator dans TaskGroup.
12. Ajouter env vars à `config.py` + `.env.example`.
13. `scripts/pnl_report.py` + tests basiques + smoke manuel HTML.
14. Doc updates §10 (README + architecture + CLAUDE + setup avec workflow Alembic complet).
15. Smoke test `--dry-run` 60s avec `PNL_SNAPSHOT_INTERVAL_SECONDS=5`.
16. Smoke test manuel Telegram (si user a fourni token, sinon skip).
17. Commit unique.

**Principes** :

- **Pas d'abstraction prématurée** : 1 dispatcher concret, 1 writer concret, 1 telegram client concret. Pas de `AbstractAlertSink`.
- **Logs structurés partout** : `monitoring_started`, `monitoring_stopped`, `pnl_snapshot_writer_started`, `pnl_snapshot_written`, `pnl_snapshot_writer_stopped`, `pnl_writer_error`, `alert_dispatcher_started`, `alert_dispatcher_stopped`, `alert_sent`, `alert_send_failed`, `alert_throttled`, `telegram_enabled`, `telegram_disabled`, `telegram_send_skipped_disabled`, `telegram_error`, `kill_switch_triggered`. Bindings : `event`, `level`, `cooldown_key`, `drawdown_pct`, `total_usdc`.
- **Pas de `print` jamais.**
- **Telegram bypass silencieux** : si pas de token, `TelegramClient.send` retourne False sans raise. AlertDispatcher continue à drainer la queue (logs `telegram_send_skipped_disabled`).
- **Cooldown alertes** : map en mémoire, pas de DB. Reset au boot OK (rate limit best effort).
- **Kill switch déterministe** : seul le writer le déclenche, et **jamais en dry-run**. Single source of truth.
- **Alembic + DB existante** : documenté `alembic stamp head` pour ne pas perdre les données.

**Pièges anticipés à documenter** :

1. `python-telegram-bot` est dans deps mais **pas utilisé** — note `# TODO M5: retirer si encore inutilisé`.
2. Telegram chat_id : peut être négatif (groupe/supergroup), positif (DM). Toujours `str` côté config.
3. Alembic + SQLite : `ALTER TABLE` limité ; toute migration future qui drop/rename colonne nécessite la stratégie "create new table + copy + drop old" (Alembic auto-génère mais auditer manuellement).
4. `TELEGRAM_BOT_TOKEN` au format `<bot_id>:<secret>` — visible dans l'URL des appels. HTTPS exclusif (httpx default), rotation immédiate si compromis. **JAMAIS de log d'URL contenant le token.**
5. PnlSnapshotWriter en dry-run écrit des snapshots avec `is_dry_run=True` et `total_usdc=stub` — utile pour smoke test mais filtrés par défaut dans `pnl_report.py` (sans `--include-dry-run`).
6. Kill switch trigger = `stop_event.set()` = TaskGroup termine = bot s'arrête. **Pas de pause + resume** à M4. Pour redémarrer, intervention manuelle (relancer `python -m polycopy`).
7. `alembic upgrade head` sur la DB existante du user (héritée de M3) plante avec `table already exists`. Documenter `alembic stamp head` comme one-shot de transition.
8. Markdown V1 dans `body` des alertes — éviter les caractères `_*[]()~` non échappés (ex: condition_id en hex est safe).
9. `WalletStateReader` cache TTL 30s (M3) — combiné avec `PNL_SNAPSHOT_INTERVAL_SECONDS=300` (default), il refetchera 1x sur 10 ; OK, le pnl bouge lentement.
10. Le writer fait grossir indéfiniment `pnl_snapshots` (1 ligne / 5 min = 288/jour = 105k/an). Acceptable. Cleanup périodique = M5+ si besoin.

---

## 15. Prompt à m'envoyer pour lancer l'implémentation

Copie-colle tel quel quand tu es prêt :

```
/implement-module M4

Suis specs/M4-monitoring.md à la lettre. Avant tout code, action obligatoire : invoque /polymarket:polymarket pour rappel (WalletStateReader M3 réutilisé). Pas de nouvelle capture de fixture Polymarket — Telegram n'est pas Polymarket. Compose manuellement tests/fixtures/telegram_send_message_response.json basé sur le schéma Telegram Bot API officiel (cf. spec §3.3).

Contraintes non négociables :
- Patches M1/M2/M3 (NoDecode, optional polymarket fields, validator CSV, queue refactor, 4 garde-fous Executor) déjà appliqués — ne rien redéfaire.
- Telegram bypass silencieux : si TELEGRAM_BOT_TOKEN absent → no-op total, aucun crash, aucune exception. Test unit dédié.
- AUCUN log de TELEGRAM_BOT_TOKEN même partiel (vérifié par grep automatisé dans les critères §12).
- Kill switch déclenché EXCLUSIVEMENT par PnlSnapshotWriter, JAMAIS en dry-run (sécurité critique — testé en unit).
- 3 nouvelles env vars optionnelles : PNL_SNAPSHOT_INTERVAL_SECONDS (300), ALERT_LARGE_ORDER_USD_THRESHOLD (50.0), ALERT_COOLDOWN_SECONDS (60). Documenter config.py + .env.example.
- Alembic init + 2 migrations (baseline M3 + ajouts M4 PnlSnapshot). Modifier init_db.py pour `alembic upgrade head` au boot. Documenter `alembic stamp head` dans docs/setup.md pour les users avec DB préexistante.
- Refactor minimal des orchestrators M1/M2/M3 : ajouter `alerts_queue: asyncio.Queue[Alert] | None = None` partout, default None pour rétrocompat.
- httpx direct pour Telegram (pas python-telegram-bot — note retrait suggéré en M5).
- SVG natif pour le sparkline pnl_report.py (pas de matplotlib).
- Conventions CLAUDE.md (async, Pydantic v2, SQLAlchemy 2.0, structlog, docstrings FR / code EN, pas de print).
- mypy --strict propre, ruff propre, pytest ≥ 80% coverage sur monitoring/.
- README polish : ajouter sections "Alertes Telegram (optionnel)" + "Rapport PnL" + cocher M4 + 3 nouvelles env vars dans la table.
- Doc updates §10 dans le même commit (README + architecture + CLAUDE + setup avec workflow Alembic).
- Commit final unique : feat(monitoring): implement M4 telegram alerts, PnL snapshots and Alembic migrations

Demande-moi confirmation avant tout patch sensible (config.py, .env, modif schéma DB autre que PnlSnapshot §2.1, suppression de fichier autre que polycopy.db local).

Si une question reste ambiguë (ex: configuration Alembic env.py async — la doc Alembic a 2 patterns différents pour async; behavior exact d'`alembic upgrade head` sur DB SQLite préexistante avec mêmes tables), tranche avec une recommandation et signale-le moi avant d'implémenter, ne bloque pas silencieusement.
```
