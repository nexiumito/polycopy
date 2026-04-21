# M1 — Watcher + Storage

Spec d'implémentation du Module 1 de polycopy : détecter et persister les trades des wallets cibles via la Data API Polymarket. Aucune stratégie, aucune exécution d'ordre, aucune alerte.

Source de vérité fonctionnelle : `docs/architecture.md` sections "Watcher" et "Storage". Conventions : `CLAUDE.md`. Schéma API : skill `/polymarket:polymarket` + https://docs.polymarket.com/api-reference/core/get-user-activity.

---

## 0. Pré-requis environnement

### 0.1 Bootstrap automatique (chemin canonique)

Sous WSL Ubuntu (chemin natif type `/home/<user>/code/polycopy`) :

```bash
bash scripts/setup.sh
```

Le script (idempotent) fait : check Python ≥ 3.11, supprime le dossier fantôme `{src/`, crée et active `.venv/`, installe `pip install -e ".[dev]"`, vérifie `ruff`/`mypy`/`pytest`, copie `.env.example` → `.env` si absent, applique le patch §0.5 ci-dessous, lance le smoke test `python -m polycopy --dry-run`. Pas-à-pas utilisateur : `docs/setup.md`.

### 0.2 Skill Polymarket (déjà installé via plugin)

Source de vérité pour tous les schémas Polymarket pendant l'implémentation. Invocation : `/polymarket:polymarket`. Si une réinstallation est nécessaire :

```
/plugin marketplace add atompilot/polymarket-skill
/plugin install polymarket@atompilot-polymarket-skill
```

Règle stricte : ne jamais inventer la forme d'une réponse API — interroger le skill ou la doc, et capturer une réponse réelle en fixture avant d'écrire le DTO.

### 0.3 Fichier `.env`

Pour M1 uniquement, **une seule variable est obligatoire** :

| Variable | À faire | Remarque |
|---|---|---|
| `TARGET_WALLETS` | **Obligatoire**. 1 ou plusieurs adresses Polygon (CSV). Doit être l'adresse **proxy** Polymarket (visible sur le profil public d'un trader), pas l'EOA. | Sans signature requise. |
| `POLL_INTERVAL_SECONDS` | Défaut `5`. Monter à `15`-`30` en dev. | — |
| `DRY_RUN` | Laisser `true`. | Garde-fou global. |
| `LOG_LEVEL` | Défaut `INFO`, `DEBUG` pour tracer les requêtes. | — |
| `DATABASE_URL` | Défaut `sqlite+aiosqlite:///polycopy.db`. | Fichier auto-créé. |

**Laisser vide à M1** : `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`, `POLYMARKET_SIGNATURE_TYPE`, `TELEGRAM_*`. Voir patch §0.5.

### 0.4 Format `TARGET_WALLETS` (déjà géré côté config)

`config.py` accepte CSV (`0xabc,0xdef`) ou JSON (`["0xabc","0xdef"]`) grâce à `Annotated[list[str], NoDecode]` + `@field_validator(mode="before")`. Le default JSON-decode de pydantic-settings v2 est désactivé pour ce champ — sinon CSV crashait sur `JSONDecodeError`. **Déjà appliqué** par `scripts/setup.sh` (idempotent).

### 0.5 Patch config Polymarket wallet (déjà appliqué)

Dans `src/polycopy/config.py` :

```python
polymarket_private_key: str | None = Field(None, description="Clé privée du wallet de signature (requis à M3)")
polymarket_funder: str | None = Field(None, description="Adresse du proxy wallet (requis à M3)")
```

**Justification** : ces deux champs ne sont lus que par l'Executor (M3). Les laisser obligatoires force à coller une fausse clé privée dans `.env` pour faire tourner M1 — mauvaise hygiène sécurité. Le garde-fou est déplacé à l'Executor : si `DRY_RUN=false` et que l'une des deux est absente au démarrage, l'Executor `raise RuntimeError`. À implémenter à M3.

**Déjà appliqué** par `scripts/setup.sh`. Le `# type: ignore[call-arg]` sur `settings = Settings()` est aussi supprimé (devenu inutile).

### 0.6 Critère de validation "environnement"

```bash
python -m polycopy --dry-run
```

Doit log `polycopy_starting` et sortir code 0 après ~1s (le stub actuel). Si ce critère casse, stopper et corriger l'env avant tout code M1.

### 0.7 Nettoyage

Le dossier fantôme `{src/` (artéfact brace-expansion shell) doit être absent. `scripts/setup.sh` le retire automatiquement.

---

## 1. Objectif M1 (scope exact)

Détecter en quasi-temps-réel les trades des `TARGET_WALLETS` via la Data API publique Polymarket et les persister dédupliqués en SQLite.

**Livrable fonctionnel** : `python -m polycopy --dry-run` ouvre N tasks asyncio (1 par wallet), poll `GET https://data-api.polymarket.com/activity?user=<addr>&type=TRADE&start=<last_ts>&sortDirection=ASC` toutes les `POLL_INTERVAL_SECONDS`, insère les trades nouveaux en base, ignore les doublons par `transactionHash`, log chaque détection, reprend proprement au boot suivant à partir du dernier `timestamp` connu par wallet.

**Hors livrable M1** : filtres marché, sizing, risk, ordres CLOB, Telegram, dashboard PnL, scoring de traders.

---

## 2. Modèles de données — `src/polycopy/storage/models.py`

Base SQLAlchemy 2.0 async :

```python
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass
```

### 2.1 `TargetTrader` (peuplé à M1)

| Colonne | Type | Contrainte |
|---|---|---|
| `id` | `int` | PK autoincrement |
| `wallet_address` | `str(42)` | **UNIQUE**, indexed, normalisé en lowercase avant insert |
| `label` | `str(64) \| None` | nullable |
| `score` | `float \| None` | nullable (peuplé à M5) |
| `active` | `bool` | default `True` |
| `added_at` | `datetime` | default `now(UTC)`, timezone-aware |

### 2.2 `DetectedTrade` (peuplé à M1)

| Colonne | Type | Contrainte |
|---|---|---|
| `id` | `int` | PK autoincrement |
| `tx_hash` | `str(66)` | **UNIQUE**, **indexed**, clé de dédup |
| `target_wallet` | `str(42)` | **indexed** (lowercase) |
| `condition_id` | `str(66)` | indexed |
| `asset_id` | `str` | token_id ERC1155 CTF (champ `asset` côté API) |
| `side` | `str(4)` | `"BUY"` ou `"SELL"` |
| `size` | `float` | taille en outcome tokens |
| `usdc_size` | `float` | taille en USDC |
| `price` | `float` | 0.0 < price < 1.0 |
| `timestamp` | `datetime` | **indexed**, timezone-aware UTC |
| `outcome` | `str(32) \| None` | label de l'outcome (ex: "Yes", "Trump"), depuis `outcome` |
| `slug` | `str \| None` | slug du marché |
| `raw_json` | `JSON` | réponse brute de l'API, audit + re-parsing futur |

### 2.3 Déclarés vides pour M3/M4 (structures, aucune logique)

- `MyOrder` (id, source_trade_id FK→detected_trades.id, clob_order_id, side, size, price, status, sent_at, filled_at)
- `MyPosition` (id, condition_id, asset_id, size, avg_price, opened_at, closed_at)
- `PnlSnapshot` (id, timestamp, total_usdc, realized_pnl, unrealized_pnl, drawdown_pct)

Commentaire `# Populated from M3 onwards` au-dessus de chacun. Créés par `create_all`, jamais lus/écrits à M1.

---

## 3. Repositories — `src/polycopy/storage/repositories.py`

Style SQLAlchemy 2.0 : `select()`, pas de `Query`. Sessions async via `async_sessionmaker[AsyncSession]` injecté dans le constructeur.

### 3.1 `TargetTraderRepository`

```python
class TargetTraderRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None: ...

    async def list_active(self) -> list[TargetTrader]: ...
    async def upsert(self, wallet_address: str, label: str | None = None) -> TargetTrader: ...
```

- `upsert` normalise l'adresse en lowercase, met `active=True` si le trader existait désactivé.

### 3.2 `DetectedTradeRepository`

```python
class DetectedTradeRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None: ...

    async def insert_if_new(self, trade: DetectedTradeDTO) -> bool: ...
    async def get_latest_timestamp(self, wallet: str) -> datetime | None: ...
    async def count_for_wallet(self, wallet: str) -> int: ...  # debug
```

- `insert_if_new` retourne `True` si inséré, `False` si `tx_hash` déjà présent. Implémentation : `INSERT OR IGNORE` SQLite, ou try/except sur `IntegrityError`.
- `get_latest_timestamp` : `max(timestamp)` pour le wallet (lowercase), `None` si vide.

**DTO Pydantic** `DetectedTradeDTO` vit dans `storage/dtos.py`.

---

## 4. Bootstrap DB — `src/polycopy/storage/engine.py` + `init_db.py`

### 4.1 `engine.py`

```python
def create_engine_and_session(
    database_url: str,
    echo: bool = False,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]: ...
```

- `echo=True` si `LOG_LEVEL=DEBUG`.
- `pool_pre_ping=True`.

### 4.2 `init_db.py`

```python
async def init_db(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    target_wallets: list[str],
) -> None: ...
```

- `Base.metadata.create_all` via `async with engine.begin()`.
- Upsert chaque wallet de `target_wallets` dans `target_traders` (idempotent).
- Log `db_initialized` avec `tables_created` + `targets_count`.

### 4.3 Migration Alembic

**Hors scope M1.** Note `# TODO M2+: introduire Alembic` en tête de `init_db.py`.

---

## 5. Client Data API — `src/polycopy/watcher/data_api_client.py`

### 5.1 Endpoint (verrouillé via /polymarket + docs officielle)

- **Base URL** : `https://data-api.polymarket.com`
- **Route** : `GET /activity`
- **Auth** : aucune (`security: []`)
- **Rate limit** : non documenté côté API, ~100 req/min retenu côté `CLAUDE.md` par prudence

### 5.2 Query params (noms et casse exacts)

| Param | Type | Défaut | À utiliser à M1 |
|---|---|---|---|
| `user` | string (Address 0x...) | — | **obligatoire**, lowercase |
| `type` | string (CSV array) | — | **`TRADE`** (majuscules, valeur exacte) |
| `start` | int (unix seconds) | — | dernier `timestamp` connu en DB pour ce wallet |
| `end` | int (unix seconds) | — | non utilisé à M1 |
| `limit` | int 0–500 | 100 | 100 |
| `offset` | int 0–10000 | 0 | 0 + paginer si réponse pleine |
| `sortBy` | enum (TIMESTAMP \| TOKENS \| CASH) | TIMESTAMP | TIMESTAMP |
| `sortDirection` | enum (ASC \| DESC) | DESC | **`ASC`** (chronologique pour reprise propre) |
| `market` | array (Hash64) | — | non utilisé |
| `eventId` | array (int) | — | non utilisé (exclusif avec `market`) |
| `side` | enum (BUY \| SELL) | — | non utilisé (on capture les deux) |

### 5.3 Schéma de réponse (Activity object)

Le endpoint retourne un `array<Activity>`. Champs par item (verbatim, casse exacte) :

```yaml
proxyWallet: string (Address)         # adresse du wallet pollé (= param user)
timestamp: integer (int64, unix s)
conditionId: string (Hash64)
type: string enum                     # TRADE | SPLIT | MERGE | REDEEM | REWARD | CONVERSION | MAKER_REBATE | REFERRAL_REWARD
size: number
usdcSize: number
transactionHash: string
price: number
asset: string                         # token_id ERC1155
side: string enum                     # BUY | SELL
outcomeIndex: integer
title: string
slug: string
icon: string
eventSlug: string
outcome: string
name: string
pseudonym: string
bio: string
profileImage: string
profileImageOptimized: string
```

Status codes : 200 (array), 400, 401, 500.

### 5.4 DTOs Pydantic — `src/polycopy/watcher/dtos.py`

`TradeActivity` mappe le sous-ensemble utile :

```python
class TradeActivity(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow", frozen=True)

    proxy_wallet: str = Field(alias="proxyWallet")
    timestamp: int                        # unix seconds, parsé en datetime UTC dans le poller
    condition_id: str = Field(alias="conditionId")
    asset: str
    side: Literal["BUY", "SELL"]
    size: float
    usdc_size: float = Field(alias="usdcSize")
    price: float
    transaction_hash: str = Field(alias="transactionHash")
    outcome: str | None = None
    slug: str | None = None
    type: Literal["TRADE", "SPLIT", "MERGE", "REDEEM", "REWARD", "CONVERSION", "MAKER_REBATE", "REFERRAL_REWARD"]
```

`extra="allow"` pour encaisser les futurs champs sans casser. Le payload brut reste persisté en `raw_json`.

### 5.5 Client

```python
class DataApiClient:
    BASE_URL = "https://data-api.polymarket.com"

    def __init__(self, http_client: httpx.AsyncClient) -> None: ...

    async def get_trades(
        self,
        wallet: str,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[TradeActivity]: ...
```

- `wallet` normalisé en lowercase avant requête.
- `since` → unix seconds via `int(since.timestamp())`, passé en param `start`.
- Pagination via `offset` : tant que la page renvoie `limit` items, refetch avec `offset += limit`.
- Filtre côté client : `type == "TRADE"` (sécurité, même si on demande `type=TRADE`).
- Timeout httpx 10s.
- Tenacity sur la méthode interne `_fetch_page` :
  ```python
  @retry(
      retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
      wait=wait_exponential(multiplier=1, min=1, max=30),
      stop=stop_after_attempt(5),
      before_sleep=before_sleep_log(log, logging.WARNING),
      reraise=True,
  )
  ```
- Sur 429 : respecter le header `Retry-After` si présent, sinon backoff exponentiel.

### 5.6 Garde-fou rate limit

Avec `POLL_INTERVAL_SECONDS=5` × N wallets, total = `N × 12 req/min`. Si `N × 12 > 100`, log `rate_limit_risk` au boot. Pour 1-3 wallets de test à M1 : aucun risque.

---

## 6. Poller — `src/polycopy/watcher/wallet_poller.py`

```python
class WalletPoller:
    def __init__(
        self,
        wallet_address: str,
        client: DataApiClient,
        repo: DetectedTradeRepository,
        interval_seconds: int,
    ) -> None: ...

    async def run(self, stop_event: asyncio.Event) -> None: ...
```

Logique de `run` :

1. `last_ts = await repo.get_latest_timestamp(wallet) or (now_utc() - timedelta(hours=1))`
2. Boucle :
   - `trades = await client.get_trades(wallet, since=last_ts)`
   - Pour chaque trade : convertir `timestamp` int → `datetime(tz=UTC)`, construire `DetectedTradeDTO`, `inserted = await repo.insert_if_new(dto)` ; log `trade_detected` si `inserted`, sinon `trade_dedup_skipped` (debug).
   - Update `last_ts = max(last_ts, max(t.timestamp for t in trades))` après inserts.
   - `await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)` (sleep interruptible).
   - Sur `TimeoutError` : c'est le tick normal, continue.
   - Sur `asyncio.CancelledError` : re-raise.
   - Sur toute autre exception : log `poller_error` (error), backoff court (5s), retente, **ne crash pas la boucle**.

Log binding : `log = structlog.get_logger().bind(wallet=wallet_address)`.

---

## 7. Orchestrateur — `src/polycopy/watcher/orchestrator.py`

```python
class WatcherOrchestrator:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None: ...

    async def run_forever(self) -> None: ...
```

- 1 `httpx.AsyncClient` partagé (pool réutilisé par tous les pollers).
- 1 `DataApiClient`, 1 `DetectedTradeRepository`, 1 `TargetTraderRepository`.
- 1 `WalletPoller` par wallet renvoyé par `TargetTraderRepository.list_active()`.
- Toutes les tasks via `asyncio.TaskGroup` (Python 3.11+).
- Handlers `SIGINT` / `SIGTERM` set le `stop_event` partagé → pollers sortent → TaskGroup termine.
- Sur sortie : `await http_client.aclose()`, puis `await engine.dispose()`.

---

## 8. Intégration `__main__` — `src/polycopy/__main__.py`

Remplacer le stub :

1. `argparse` : `--dry-run` (force `settings.dry_run=True`), `--log-level` (override).
2. Init `structlog` JSON renderer (processors : timestamp ISO UTC, level, logger_name, JSONRenderer).
3. `engine, session_factory = create_engine_and_session(settings.database_url)`
4. `await init_db(engine, session_factory, settings.target_wallets)`
5. `await WatcherOrchestrator(session_factory, settings).run_forever()`
6. `KeyboardInterrupt` → log `polycopy_stopped_by_user`, `sys.exit(0)`.
7. `Exception` non gérée → log `polycopy_crashed` avec traceback, `sys.exit(1)`.

---

## 9. Tests

### 9.1 Arborescence

```
tests/
├── __init__.py
├── conftest.py
├── fixtures/
│   └── activity_sample.json    # réponse réelle Data API capturée
├── unit/
│   ├── __init__.py
│   ├── test_config.py
│   ├── test_data_api_client.py
│   ├── test_repositories.py
│   └── test_wallet_poller.py
└── integration/
    ├── __init__.py
    └── test_data_api_live.py   # @pytest.mark.integration
```

### 9.2 `conftest.py`

- Fixture `async_engine` : `sqlite+aiosqlite:///:memory:`, `create_all` avant test, dispose après.
- Fixture `session_factory` dérivée.
- Fixtures `target_trader_repo`, `detected_trade_repo`.
- Fixture `sample_activity_payload` : lit `fixtures/activity_sample.json`.

### 9.3 `test_config.py`

- `polymarket_private_key` peut être absent sans crash.
- `target_wallets` parse un CSV (`"0xabc,0xdef"` → 2 items).
- `target_wallets` parse un JSON (`'["0xabc","0xdef"]'` → 2 items).
- `target_wallets` vide (`""`) → `[]`.
- `dry_run` default `True`.

### 9.4 `test_data_api_client.py` (respx)

- 200 happy path : mock 1 réponse depuis `fixtures/activity_sample.json`, parse en `list[TradeActivity]`, vérifie les alias (`proxyWallet`, `transactionHash`, `usdcSize`).
- 429 : 2×429 puis 200 → tenacity retry, succès final, vérifier le backoff ≥ 1s.
- Pagination : 2 pages (`limit` plein puis vide), vérifier que `offset` est incrémenté de `limit` et que les batches sont concaténés.
- `httpx.TransportError` : retry puis propagation après 5 tentatives.
- Filtrage `since` : vérifier `start=<unix_ts>` dans la query string ; absence si `since=None`.
- Param `type=TRADE` toujours présent.

### 9.5 `test_repositories.py`

- `TargetTraderRepository.upsert` : insert nouveau, ré-upsert → 1 ligne, `active=True`. Insert majuscule → stockée en lowercase.
- `DetectedTradeRepository.insert_if_new` : True la 1ère fois, False la 2ème (même `tx_hash`).
- `get_latest_timestamp` : correct sur 3 trades, `None` si wallet inconnu.

### 9.6 `test_wallet_poller.py`

- Mock `DataApiClient.get_trades` pour retourner 3 trades puis 0.
- Stop event après 2 ticks → 3 insertions.
- Relance le poller → 0 nouvelle insertion (dedup via DB in-memory).
- Simule exception API : log `poller_error`, ne crash pas, continue après backoff.

### 9.7 `test_data_api_live.py` (opt-in)

```python
@pytest.mark.integration
async def test_fetch_real_wallet_activity():
    wallet = "0x..."  # wallet public actif (note la source dans le fichier)
    async with httpx.AsyncClient() as http:
        client = DataApiClient(http)
        trades = await client.get_trades(wallet, limit=10)
    assert isinstance(trades, list)
```

Run : `pytest -m integration`. Pas en CI par défaut.

### 9.8 Couverture

```bash
pytest --cov=src/polycopy/watcher --cov=src/polycopy/storage --cov-report=term-missing
```

Seuil : **≥ 80 %** sur `watcher/` et `storage/`.

---

## 10. Mises à jour de documentation (même PR)

### 10.1 `README.md`

Ajouter après "Roadmap" :

```markdown
## État d'avancement

- [x] M1 : Watcher + Storage
- [ ] M2 : Strategy Engine
- [ ] M3 : Executor
- [ ] M4 : Monitoring
- [ ] M5 : Scoring
```

### 10.2 `docs/architecture.md`

Préambule "Status M1 ✅ implémenté au commit XYZ" en tête des sections Watcher et Storage. Pas de réécriture — la spec reste la source de vérité fonctionnelle.

### 10.3 `CLAUDE.md`

1. Section "Quand tu hésites", remplacer la puce Polymarket par :
   > - **Sur la sémantique d'un endpoint Polymarket** : invoquer d'abord le skill `/polymarket:polymarket`. En dernier recours, https://docs.polymarket.com. Jamais deviner.
2. Section "Sécurité", ajouter :
   > - `polymarket_private_key` et `polymarket_funder` sont **optionnels** au niveau config. Ils ne sont consommés que par l'Executor (M3), qui refuse de démarrer si `DRY_RUN=false` et que l'une des deux est absente.
3. Section "APIs Polymarket utilisées", ajouter en tête :
   > Source de vérité pour tous les schémas : skill `/polymarket:polymarket`. Capturer toute réponse réelle en fixture avant d'écrire un DTO.

### 10.4 `docs/setup.md`

Déjà créé par le bootstrap. À M1, ajouter une section "Lancer M1" qui pointe sur la commande `python -m polycopy --dry-run` post-implémentation et la lecture des logs `trade_detected`.

---

## 11. Commandes de vérification finale

```bash
ruff check .
ruff format --check .
mypy src
pytest --cov=src/polycopy --cov-report=term-missing
pytest -m integration                   # opt-in, taps la vraie API
python -m polycopy --dry-run            # ≥ 60s sans erreur avec ≥ 1 wallet
```

---

## 12. Critères d'acceptation

- [ ] `python -m polycopy --dry-run` tourne **60 s** sans crash, log ≥ 1 `polycopy_starting`, ≥ 1 `db_initialized`, persiste ≥ 0 trade en DB (0 valide si le wallet n'a rien tradé sur la fenêtre).
- [ ] Ctrl-C → shutdown propre : tasks terminées, `http_client.aclose()` appelé, `engine.dispose()` appelé, aucune task orpheline.
- [ ] `ruff check .` : 0 erreur. `ruff format --check .` : 0 diff.
- [ ] `mypy src` en mode `--strict` : 0 erreur.
- [ ] `pytest` : 0 échec. Couverture ≥ 80 % sur `src/polycopy/watcher/` et `src/polycopy/storage/`.
- [ ] Dossier fantôme `{src/` absent (déjà géré par `setup.sh`).
- [ ] `docs/setup.md` permet à un nouvel utilisateur de lancer le bot en dry-run en **< 5 min**.
- [ ] `README.md`, `docs/architecture.md` et `CLAUDE.md` mis à jour comme décrit §10.
- [ ] Commit final Conventional Commits : `feat(watcher,storage): implement M1 wallet polling and trade persistence`.

---

## 13. Hors scope M1 (NE PAS implémenter)

- Filtres marché, sizing, risk manager → **M2**
- Client CLOB, signature et envoi d'ordres → **M3**
- Alertes Telegram, dashboard PnL, endpoint `/metrics` Prometheus → **M4**
- Scoring de traders, sélection automatique → **M5**
- Migrations Alembic (M1 = `create_all` only)
- WebSocket CLOB pour la détection (architecture.md §Watcher : `/activity` polling REST suffit)
- Multi-process / multi-VPS
- Backtesting framework

---

## 14. Notes d'implémentation

**Ordre de travail suggéré** :

1. Capturer une réponse réelle `/activity` dans `tests/fixtures/activity_sample.json` (wallet de test depuis `.env`).
2. DTOs Pydantic + models SQLAlchemy.
3. Repositories + tests unitaires (SQLite in-memory).
4. Data API client + tests respx (utilise la fixture capturée).
5. Poller + tests.
6. Orchestrator.
7. `__main__` : argparse, structlog init, init_db, run_forever.
8. Doc updates (README, architecture, CLAUDE).
9. Smoke test `--dry-run` sur 60s.
10. Commit unique : `feat(watcher,storage): implement M1 wallet polling and trade persistence`.

**Principes** :

- Pas d'abstraction prématurée : un client HTTP concret, un poller concret, un orchestrateur concret. Pas d'interface `AbstractTradeSource` tant qu'il n'y a pas 2 sources.
- Logs structurés partout : événements clés (`trade_detected`, `trade_dedup_skipped`, `poller_error`, `db_initialized`, `watcher_started`, `watcher_stopped`) avec bindings (`wallet`, `tx_hash`, `condition_id`).
- Pas de `print` jamais.
- `target_wallet` toujours stocké lowercase (DB + DTO + index).
- `timestamp` Polymarket = unix seconds → `datetime(tz=UTC)`.

---

## 15. Prompt à m'envoyer pour lancer l'implémentation

Copie-colle tel quel quand tu es prêt :

```
/implement-module M1

Suis specs/M1-watcher-storage.md à la lettre. Avant tout code, action obligatoire : invoque /polymarket:polymarket pour reconfirmer le schéma /activity et capture une réponse réelle dans tests/fixtures/activity_sample.json en pollant la première adresse de TARGET_WALLETS dans .env (un seul appel httpx, sauve le payload tel quel). Ensuite suis l'ordre §14.

Contraintes non négociables :
- Patch §0.5 et fix CSV TARGET_WALLETS sont déjà appliqués (config.py contient NoDecode + _parse_target_wallets) — ne pas les redéfaire.
- Conventions CLAUDE.md (async, Pydantic v2, SQLAlchemy 2.0, structlog, docstrings FR / code EN, pas de print).
- mypy --strict doit passer, ruff check propre, pytest vert avec coverage ≥ 80% sur watcher/ et storage/.
- Mises à jour doc §10 dans le même commit.
- Commit final unique : feat(watcher,storage): implement M1 wallet polling and trade persistence

Demande-moi confirmation avant tout patch sensible (config.py, .env, suppression de fichier).
```
