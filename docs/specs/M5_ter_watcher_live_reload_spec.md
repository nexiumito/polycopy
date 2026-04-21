# M5_ter — Watcher live-reload

**Status** : Draft — 2026-04-21
**Depends on** : M1 (Watcher), M5 (Discovery + Scoring), M5_bis (Competitive eviction + `sell_only`/`blacklisted`)
**Ne bloque pas** : M13+ (fees), M14+ (parallel strategy), M15+ (Goldsky streaming)

---

## 0. TL;DR

Aujourd'hui, `WatcherOrchestrator` lit `target_repo.list_active()` **une seule fois au boot** et fige la liste des pollers pour toute la session. Toute mutation du lifecycle wallet survenant après le boot (promote M5, eviction M5_bis, demote, blacklist reconciliation, etc.) est **invisible au watcher** jusqu'au prochain redémarrage. Symptôme observé en prod le 2026-04-21 : 7 wallets promus `shadow → active` dans la matinée → 0 trade détecté sur la journée.

M5_ter ajoute un **cycle de reload périodique** (défaut 5 min, configurable via `WATCHER_RELOAD_INTERVAL_SECONDS`). À chaque tick : fetch `list_wallets_to_poll()`, diff set-based contre les pollers en cours, `tg.create_task(...)` pour les nouveaux, `task.cancel() + await` pour les retirés. Logs silencieux si no-op, info uniquement sur changement.

Diff strictement additif : zéro ligne modifiée dans `WalletPoller`, `DataApiClient`, `DetectedTradeRepository`, `DiscoveryOrchestrator`, `EvictionScheduler`. Le seul point de contact = `WatcherOrchestrator.run_forever` + 1 nouvelle méthode repository.

---

## 1. Motivation & use case concret

### 1.1 Le problème — blind spot boot-time

[watcher/orchestrator.py:50](../../src/polycopy/watcher/orchestrator.py#L50) appelle `target_repo.list_active()` **une fois** avant d'entrer dans le `asyncio.TaskGroup`. La liste des pollers est figée pour toute la durée du process.

### 1.2 Scénario observé — 2026-04-21, machine uni-debian

> *Timeline matinée* :
> - 09:42 : bot démarré avec `DISCOVERY_ENABLED=true`, `TARGET_WALLETS=""`. `target_repo.list_active()` retourne `[]`. Log `watcher_no_active_targets`, `WatcherOrchestrator.run_forever` sort immédiatement.
> - 11:15 : `DiscoveryOrchestrator` termine son 1er cycle, promeut 7 wallets `shadow → active` via `transition_status`. `trader_promoted` × 7 dans `trader_events`. 7 alertes Telegram `trader_promoted` envoyées correctement.
> - 18:47 : résumé quotidien M7 — `Trades détectés : 0 · Top wallets actifs : aucun · Promotions Discovery : 7`.

L'utilisateur avait 7 wallets activement scorés en DB, 7 entrées `target_traders.status='active'`, mais **le watcher pollait encore sa liste vide du boot**. Chaque mutation Discovery/M5_bis passe inaperçue tant que le user ne relance pas manuellement le bot.

### 1.3 Surface étendue par M5_bis

M5_bis a ajouté 2 nouveaux status + 6 types de transitions qui doivent tous refléter en quasi-temps-réel côté watcher :

| Transition M5/M5_bis | Impact watcher |
|---|---|
| `shadow → active` (T2 M5) | **+1 poller** |
| `shadow → active*` (T3 cascade) | **+1 poller** |
| `active → sell_only` (T5 cascade) | **0 delta** (watcher continue à poller pour copier SELL) |
| `sell_only → active` (T6 abort) | **0 delta** |
| `sell_only → active*` (T7 rebond) | **0 delta** |
| `sell_only → shadow` (T8 complete) | **-1 poller** |
| `active → shadow` (T4 M5 demote) | **-1 poller** |
| `any → blacklisted` (T10) | **-1 poller** |
| `blacklisted → shadow/pinned` (T11/T12) | **+1 poller si pinned** |

Sans M5_ter, toutes les mutations ci-dessus sont invisibles au watcher — M5_bis ne peut pas livrer sa valeur métier sans M5_ter.

---

## 2. Scope / non-goals

### 2.1 Dans le scope

- Cycle de reload périodique dans `WatcherOrchestrator.run_forever`.
- Nouvelle méthode repository `list_wallets_to_poll()` (filtre status ∈ {active, sell_only}, exclut blacklist via env var).
- Diff set-based wallets à poller vs pollers en cours.
- `tg.create_task(...)` pour les nouveaux, `task.cancel() + await` pour les retirés.
- Nouvelle env var `WATCHER_RELOAD_INTERVAL_SECONDS` (défaut 300s).
- Logs structlog sobres (debug no-op, info sur changement).
- Test plan complet (14 cas §9).

### 2.2 Hors scope explicites

- ❌ **Reload dynamique des env vars** — `BLACKLISTED_WALLETS` et `TARGET_WALLETS` restent statiques (lues 1 fois par Pydantic au boot). Un changement à chaud nécessite un restart (ou un `/restart <machine>` via remote control M12_bis). M5_ter **n'écoute pas** les modifications de `.env`.
- ❌ **Pub/sub DB** (SQLite/Postgres NOTIFY) pour notifications push. Le cycle polling TTL 5 min est volontairement simple — l'alternative ajouterait un listener + un canal fragile (SQLite ne supporte pas LISTEN nativement).
- ❌ **Reload dans `__main__`** — M5_ter ne touche pas au TaskGroup du runner, uniquement au TaskGroup interne du watcher.
- ❌ **Reload des autres orchestrators** — `StrategyOrchestrator`, `ExecutorOrchestrator`, etc. n'ont pas le même blind spot (ils n'ont pas de liste statique par wallet ; ils consomment les trades via queue asyncio). Hors scope.
- ❌ **Hot-swap `POLL_INTERVAL_SECONDS`** — un changement de cadence poller individuel à chaud n'est pas supporté. Un restart est requis.

---

## 3. User stories

### 3.1 Story A — Promote Discovery invisible sans M5_ter

**Avant M5_ter** : user démarre le bot à 09:00 avec 0 wallet en DB. `DISCOVERY_ENABLED=true` promeut 7 wallets à 11:15. Le watcher reste vide toute la journée. Résumé quotidien `trades_detected=0`.

**Avec M5_ter** : à 11:15 `Discovery` écrit `status='active'` × 7. À 11:20 (au plus tard, selon `WATCHER_RELOAD_INTERVAL_SECONDS=300`), `WatcherOrchestrator` lance son cycle de reload. Log :
```
watcher_reload_cycle added=7 removed=0 total=7
added_wallets=["0xabc…", "0xdef…", "0xghi…", ...]
```
7 pollers démarrent, premiers trades détectés dans les minutes qui suivent.

### 3.2 Story B — Eviction M5_bis transparente côté watcher

Contexte : 5 actives poller. À 14:32, `EvictionScheduler` déclenche T3+T5 (cascade : `0xcand → active`, `0xworst → sell_only`).

Au cycle de reload suivant (≤ 5 min plus tard) :
- Set diff : `desired = {0xok1, 0xok2, 0xok3, 0xok4, 0xcand, 0xworst}` (6 wallets, tous avec status ∈ {active, sell_only}).
- Set diff : `current = {0xok1, 0xok2, 0xok3, 0xok4, 0xworst}` (5 wallets).
- **+1 poller pour `0xcand`**. `0xworst` reste polled (T5 ne le retire pas — il est toujours dans `list_wallets_to_poll()` via `status='sell_only'`).

Log :
```
watcher_reload_cycle added=1 removed=0 total=6
added_wallets=["0xcand…"]
```

Le poller de `0xworst` continue à détecter ses SELL. Le `TraderLifecycleFilter` côté strategy rejette les BUY. Wind-down fonctionne.

### 3.3 Story C — Blacklist live via restart

L'user ajoute `0xbad` à `BLACKLISTED_WALLETS` dans `.env` et relance le bot (ou `POST /v1/restart/<machine>` M12_bis). Au boot :
- `EvictionScheduler.reconcile_blacklist` (M5_bis) écrit `status='blacklisted'` pour `0xbad`.
- Le 1er `WatcherOrchestrator.run_forever` appelle `list_wallets_to_poll()` → exclut `0xbad` (blacklist env OU status blacklisted).
- Aucun poller `0xbad` démarré.

Note : sans restart, M5_ter **ne propage pas** le changement d'env var — c'est un choix de scope (§2.2).

---

## 4. Architecture

### 4.1 Position dans la stack

```
__main__.py  (runner asyncio, TaskGroup top-level)
    │
    └─► WatcherOrchestrator.run_forever(stop_event)      ◄── M5_ter extend ici
            │
            ├─► list_wallets_to_poll()  (new method, §6)  ◄── M5_ter ajoute
            │       └─► TargetTraderRepository
            │                │
            │                └─► DB (target_traders.status) ◄── muté par
            │                                                    DiscoveryOrchestrator M5
            │                                                    EvictionScheduler M5_bis
            │
            └─► TaskGroup interne
                    ├─► WalletPoller(0xabc).run(stop_event)     ◄── M1 inchangé
                    ├─► WalletPoller(0xdef).run(stop_event)
                    └─► ... (ajoutés/retirés dynamiquement par M5_ter)
```

### 4.2 Interactions — qui mute le status ?

| Acteur | Transition | Appelé par |
|---|---|---|
| `DiscoveryOrchestrator._run_one_cycle` | `shadow → active`, `active → shadow` (demote), `shadow → shadow` (discovered) | TaskGroup top-level, tous les `DISCOVERY_INTERVAL_SECONDS` (6h défaut) |
| `EvictionScheduler.run_cycle` | T3/T5/T6/T7/T8 cascades | Appelé par `DiscoveryOrchestrator` après `decision_engine.decide` (Phase C M5_bis) |
| `EvictionScheduler.reconcile_blacklist` | T10/T11/T12 | Au boot + à chaque cycle Discovery |

**M5_ter ne dépend d'aucun signal venant de ces acteurs.** Il lit simplement `target_traders.status` via le repository à intervalle fixe. Pattern strict "eventual consistency sur TTL `WATCHER_RELOAD_INTERVAL_SECONDS`".

### 4.3 Dépendance au `stop_event`

Inchangé : `stop_event` reste la source unique de shutdown global, partagée entre watcher/strategy/executor/discovery/remote_control. Le reload cycle respecte `stop_event.is_set()` à chaque étape (avant fetch DB, avant `tg.create_task`, avant sleep).

---

## 5. Algorithme du reload cycle

### 5.1 Pseudocode

```python
async def run_forever(self, stop_event: asyncio.Event) -> None:
    target_repo = TargetTraderRepository(self._session_factory)
    trade_repo = DetectedTradeRepository(self._session_factory)
    latency_repo = TradeLatencyRepository(...) if enabled else None

    # Dict wallet → asyncio.Task. Source de vérité des pollers en cours.
    pollers_by_wallet: dict[str, asyncio.Task[None]] = {}

    async with httpx.AsyncClient() as http_client:
        api_client = DataApiClient(http_client)

        async with asyncio.TaskGroup() as tg:
            # Boucle reload : première itération immédiate (boot),
            # puis toutes les WATCHER_RELOAD_INTERVAL_SECONDS.
            while not stop_event.is_set():
                try:
                    desired_wallets = await target_repo.list_wallets_to_poll(
                        blacklist=self._settings.blacklisted_wallets,
                    )
                except Exception:
                    log.warning("watcher_reload_failed")
                    # Skip cycle, retry au prochain TTL.
                    if await _sleep_or_stop(stop_event, interval):
                        break
                    continue

                desired = {w.lower() for w in desired_wallets}
                current = set(pollers_by_wallet.keys())
                to_add = desired - current
                to_remove = current - desired

                # Cancel retirés — await pour propagation propre.
                if to_remove:
                    await _cancel_pollers(
                        pollers_by_wallet, to_remove,
                    )

                # Lancer nouveaux.
                for wallet in to_add:
                    if stop_event.is_set():
                        break
                    poller = WalletPoller(
                        wallet_address=wallet,
                        client=api_client,
                        repo=trade_repo,
                        interval_seconds=self._settings.poll_interval_seconds,
                        out_queue=self._out_queue,
                        latency_repo=latency_repo,
                        instrumentation_enabled=self._settings.latency_instrumentation_enabled,
                    )
                    pollers_by_wallet[wallet] = tg.create_task(
                        poller.run(stop_event),
                        name=f"wallet_poller:{wallet}",
                    )

                if to_add or to_remove:
                    log.info(
                        "watcher_reload_cycle",
                        added=len(to_add),
                        removed=len(to_remove),
                        total=len(pollers_by_wallet),
                        added_wallets=sorted(to_add),
                        removed_wallets=sorted(to_remove),
                    )
                else:
                    log.debug(
                        "watcher_reload_cycle_noop",
                        total=len(pollers_by_wallet),
                    )

                if await _sleep_or_stop(stop_event, interval):
                    break

        log.info("watcher_stopped", final_pollers=len(pollers_by_wallet))


async def _cancel_pollers(
    pollers_by_wallet: dict[str, asyncio.Task[None]],
    wallets_to_remove: set[str],
) -> None:
    """Cancel + await les pollers retirés. Absorbe CancelledError."""
    tasks_to_cancel = []
    for wallet in wallets_to_remove:
        task = pollers_by_wallet.pop(wallet, None)
        if task is not None and not task.done():
            task.cancel()
            tasks_to_cancel.append(task)
    if tasks_to_cancel:
        # return_exceptions=True absorbe les CancelledError individuelles.
        await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
```

### 5.2 Choix architectural — TaskGroup vivant

**Décision** : on garde `asyncio.TaskGroup` mais on utilise sa capacité à accepter `create_task()` pendant que le groupe tourne (supporté Python 3.11+).

Rationale :
- Pattern cohérent avec le reste du codebase (M1/M4/M5/M10 utilisent tous TaskGroup).
- Exception propagation propre via `except*`.
- Si un poller crash avec une exception non-catchée, le TaskGroup remonte l'exception et cancel les frères — comportement souhaité (non-régression M1).

**Alternative rejetée** : dict `wallet → asyncio.Task` à plat sans TaskGroup, avec `asyncio.gather` final. Rejetée parce que :
- Pas de propagation automatique d'exception (il faut boucler manuellement).
- Pas cohérent avec le reste du projet.
- Plus de code de gestion d'erreurs pour aucun gain fonctionnel.

### 5.3 Choix architectural — Interval TTL fixe vs adaptatif

Interval fixe `WATCHER_RELOAD_INTERVAL_SECONDS` (défaut 300). Pas d'adaptatif (ex. "reload plus fréquent les 30 premières min après un cycle Discovery"). Rationale : simplicité. L'user peut baisser à 60s s'il trouve 5 min trop lent ; pas de heuristique qui complique sans gain mesuré.

### 5.4 Edge case — `stop_event` mid-cycle

Si `stop_event.set()` survient pendant un cycle de reload :
- Avant `list_wallets_to_poll()` : sortie immédiate via le `while not stop_event.is_set()` loop guard.
- Pendant la boucle `for wallet in to_add` : check `if stop_event.is_set(): break` avant chaque `create_task`.
- Pendant `_cancel_pollers` : les cancel tournent quand même, on attend leur completion propre.
- Pendant `_sleep_or_stop` : retourne `True` immédiatement, break la boucle while.

Aucune fuite de task possible — le TaskGroup parent absorbera les pollers déjà lancés au shutdown via `stop_event`.

---

## 6. DTOs / signatures

### 6.1 Nouvelle méthode repository

```python
# src/polycopy/storage/repositories.py
class TargetTraderRepository:
    ...
    async def list_wallets_to_poll(
        self,
        *,
        blacklist: list[str] | None = None,
    ) -> list[TargetTrader]:
        """Retourne les wallets que le Watcher doit poller (M5_ter).

        Filtre ``active=True`` ET ``status IN ('active', 'pinned', 'sell_only')``,
        puis exclut les wallets présents dans ``blacklist`` (double check
        défense-en-profondeur : normalement ``reconcile_blacklist`` les a déjà
        mis en ``status='blacklisted'`` donc filtrés par la clause IN, mais on
        préserve l'invariant "BLACKLISTED_WALLETS absolu").

        Différent de ``list_active()`` qui a une sémantique "wallets *trading*"
        (shadow exclu, sell_only inclus mais pas de filtrage blacklist env
        — responsabilité du caller). M5_ter adopte une sémantique "wallets
        *à poller réseau*".
        """
        blacklist_lc = {w.lower() for w in (blacklist or [])}
        async with self._session_factory() as session:
            stmt = select(TargetTrader).where(
                TargetTrader.active.is_(True),
                TargetTrader.status.in_(("active", "pinned", "sell_only")),
            )
            result = await session.execute(stmt)
            traders = list(result.scalars().all())
        return [
            t for t in traders if t.wallet_address.lower() not in blacklist_lc
        ]
```

### 6.2 Décision sémantique — nouvelle méthode vs param ajouté

**Décision figée** : **nouvelle méthode `list_wallets_to_poll()`**, pas un param `include_sell_only` sur `list_active()`.

Rationale :
- Sémantique explicite à l'appelant (le nom dit ce qu'il retourne).
- `list_active()` conserve sa sémantique M5 "wallets *trading* au sens strict" (utilisé par le dashboard, les queries de discovery, les tests M5 existants). M5_bis a déjà étendu `list_active()` pour inclure sell_only car watcher + dashboard `/traders` consomment tous les deux. Mais le dashboard veut "active + pinned + sell_only" pour afficher les badges, tandis que le watcher M5_ter veut "active + pinned + sell_only - blacklist".
- La double-check blacklist en Python-side est trivial (4 lignes) et clarifie l'intention — contre un SQL `NOT IN` qui nécessiterait passer `blacklist` à chaque appel `list_active`.

**Note** : `list_active()` (M5_bis Phase C.3) reste inchangée et continue à inclure `sell_only` sans filtrage blacklist. Elle n'est pas appelée par `WatcherOrchestrator.run_forever` après M5_ter — remplacée par `list_wallets_to_poll()`.

### 6.3 Typing du dict pollers

```python
pollers_by_wallet: dict[str, asyncio.Task[None]] = {}
```

Clé = `wallet_address.lower()` (normalisation stricte, cohérent partout dans le codebase). Valeur = `asyncio.Task[None]` (le poller retourne `None` via `await poller.run(stop_event)`).

---

## 7. Settings

### 7.1 Nouvelle env var

| Nom | Type | Default | Validation | Notes |
|---|---|---|---|---|
| `WATCHER_RELOAD_INTERVAL_SECONDS` | `int` | `300` | `Field(ge=30, le=3600)` | TTL du cycle de reload. Min 30s évite l'over-polling DB. Max 1h — au-delà, autant désactiver en remontant encore. |

Ajout `.env.example` après `POLL_INTERVAL_SECONDS` :

```ini
# M5_ter — Cadence du cycle de reload du watcher (re-fetch liste wallets à poller).
# Default 300 (5 min). Range [30, 3600]. Réactif aux changements M5/M5_bis
# (promotions, eviction, demote, blacklist) en quasi-temps-réel sans restart.
WATCHER_RELOAD_INTERVAL_SECONDS=300
```

### 7.2 Env vars existantes impactées

Aucune. M5_ter lit `BLACKLISTED_WALLETS` (existant M5) mais ne modifie ni sa sémantique, ni sa cadence de lecture (toujours statique, lue 1 fois par Pydantic au boot).

---

## 8. Invariants sécurité

- **Pinned preservation** : un wallet `pinned=True` est dans `list_wallets_to_poll()` tant qu'il a `status ∈ {active, pinned, sell_only}` ET n'est pas dans `BLACKLISTED_WALLETS`. Le cas "user blacklist un wallet déjà dans `TARGET_WALLETS`" déclenche un crash boot clair (validator Pydantic M5_bis `_validate_m5_bis_eviction`), donc le watcher ne voit jamais cet état incohérent.
- **Blacklist absolu** : double-check défense-en-profondeur. (1) `EvictionScheduler.reconcile_blacklist` met `status='blacklisted'` → clause SQL `IN ('active', 'pinned', 'sell_only')` le filtre. (2) Même si le reconcile n'a pas encore tourné (race boot), le param `blacklist` passé à `list_wallets_to_poll()` filtre en Python-side. Aucune fenêtre où un blacklist wallet serait polled.
- **Non-régression `active ↔ sell_only`** : la cascade T3+T5 (M5_bis) écrit `active → sell_only` et `shadow → active` en même transaction. Au prochain cycle reload, les deux wallets sont dans `list_wallets_to_poll()` → aucun cancel, aucun nouveau → no-op côté watcher (set diff vide). Le poller de `worst` continue sans interruption. Les tests vérifient explicitement ce no-op (§9 cas 3, 4).
- **Dédup `tx_hash` préservé** : chaque `WalletPoller` gère son propre in-memory `last_ts`. Un cancel + relance (ex. wallet passe shadow → active → shadow → active à travers plusieurs cycles) crée un nouveau poller avec un nouveau `last_ts = repo.get_latest_timestamp()` — le dernier `timestamp` persisté en DB. Aucun trade perdu ni dupliqué.
- **Pas de race avec stop_event** : tous les `create_task()` dans `to_add` et les `cancel()` dans `to_remove` vérifient `stop_event.is_set()` avant action. Le TaskGroup parent absorbe les pollers orphelins à l'exit.
- **Isolation sur failure** : un `list_wallets_to_poll()` qui raise (DB lock momentané, FS error, corruption) est capturé, loggué `watcher_reload_failed`, et le cycle est skippé. Le next tick retente. Les pollers existants continuent à tourner normalement — pas de collapse en cascade.
- **Aucun secret exposé** : `list_wallets_to_poll()` retourne uniquement des `wallet_address` (publics on-chain) + leur status. Aucun appel réseau dans le reload cycle lui-même (seulement DB locale). Aucun log ne contient de secret même partiellement.

---

## 9. Test plan

Couverture cible ≥ 85 % sur `src/polycopy/watcher/`. Tests unitaires via `respx` + mock `TargetTraderRepository` (pas d'appel réseau réel). Tests d'intégration opt-in `-m integration`.

| # | Cas | Résultat attendu |
|---|---|---|
| 1 | Boot froid — 3 actives, 0 sell_only, 0 blacklist | 3 pollers démarrés. Log `watcher_reload_cycle added=3 removed=0 total=3`. |
| 2 | Après boot — 1 `shadow → active` en DB | Au cycle suivant : `added=1, removed=0, added_wallets=[0xnew]`. Le poller tourne. |
| 3 | Après boot — 1 `active → sell_only` (cascade M5_bis) | No-op. `added=0, removed=0`. Log debug only (cas §3.2 Story B). |
| 4 | Après boot — 1 `sell_only → active` (abort T6) | No-op. Même état. |
| 5 | Après boot — 1 `sell_only → shadow` (T8 complete) | `added=0, removed=1`. Poller cancel propre absorbe `CancelledError`. |
| 6 | Après boot — 1 `active → shadow` (T4 demote) | Idem cas 5. |
| 7 | Après boot — user ajoute wallet à `BLACKLISTED_WALLETS` + restart | Au boot, `reconcile_blacklist` set `status='blacklisted'`. Watcher n'instancie aucun poller pour lui. |
| 8 | 10 cycles consécutifs no-op | Aucun log info, 10 logs debug. Pas de spam. |
| 9 | `stop_event.set()` pendant `to_add` | Sortie propre, pas de task orpheline, log `watcher_stopped`. |
| 10 | `list_wallets_to_poll()` raise (DB lock) | Log `watcher_reload_failed`. Retry au cycle suivant. Pas de crash. Pollers existants continuent. |
| 11 | Pinned non retirable | Un wallet `pinned=True` reste dans `list_wallets_to_poll()` peu importe les cycles (M5 `transition_status` raise sur pinned, donc aucune mutation possible). |
| 12 | Coexistence `BLACKLISTED_WALLETS` + `DISCOVERY_ENABLED=true` | Un wallet dans blacklist n'apparaît jamais dans `list_wallets_to_poll()`. Double check vérifié (côté M5 + côté M5_ter). |
| 13 | Non-régression M1..M5_bis avec `WATCHER_RELOAD_INTERVAL_SECONDS=3600` | Comportement proche M1..M5_bis (1 reload/h). Tests M1 existants passent identiques. |
| 14 | 2 wallets à retirer au même cycle | `asyncio.gather(*cancels, return_exceptions=True)` absorbe les `CancelledError` individuelles sans fuite. |

### 9.1 Fichier de tests proposé

`tests/unit/test_watcher_live_reload.py` — 14 tests parametrizés quand pertinent.

Fixtures :
- `session_factory` : SQLite in-memory + `Base.metadata.create_all`.
- `target_repo` : `TargetTraderRepository(session_factory)`.
- `fake_api_client` : mock avec 0 trade (pour que le poller ne bloque pas sur I/O).
- `short_interval_settings` : `WATCHER_RELOAD_INTERVAL_SECONDS=0.1` pour tests rapides (override via monkeypatch si nécessaire).

Pattern :
```python
async def test_reload_adds_new_poller_after_promote(target_repo, session_factory):
    # Setup : 2 actives.
    await _seed_actives(target_repo, ["0xa", "0xb"])
    orchestrator = WatcherOrchestrator(session_factory, settings, ...)
    stop_event = asyncio.Event()

    # Lance orchestrator dans une task, laisse un cycle, promote 1 wallet, laisse 2 cycles.
    task = asyncio.create_task(orchestrator.run_forever(stop_event))
    await asyncio.sleep(0.15)  # cycle 1
    await _seed_actives(target_repo, ["0xc"])  # promote during run
    await asyncio.sleep(0.15)  # cycle 2 doit voir 0xc
    stop_event.set()
    await task

    # Vérifie que le poller de 0xc a été créé (via caplog sur watcher_reload_cycle).
```

---

## 10. Impact sur l'existant

| Fichier | LOC estimés | Nature du changement |
|---|---|---|
| [src/polycopy/watcher/orchestrator.py](../../src/polycopy/watcher/orchestrator.py) | +120 / -20 | Réécriture `run_forever` avec reload loop + helpers `_cancel_pollers`, `_sleep_or_stop` |
| [src/polycopy/storage/repositories.py](../../src/polycopy/storage/repositories.py) | +25 | Nouvelle méthode `list_wallets_to_poll` |
| [src/polycopy/config.py](../../src/polycopy/config.py) | +10 | Nouvelle env var `WATCHER_RELOAD_INTERVAL_SECONDS` |
| [.env.example](../../.env.example) | +5 | Bloc commenté pour la nouvelle env var |
| [CLAUDE.md](../../CLAUDE.md) | +5 | Bullet sécurité sur pinned + blacklist preservation + no-regression active↔sell_only |
| [docs/architecture.md](../../docs/architecture.md) | +10 | Paragraphe "Status M5_ter" dans la section Watcher |
| [README.md](../../README.md) | +5 | Env var documentée + roadmap coché |
| [docs/specs/ROADMAP.md](./ROADMAP.md) | +1 | Ligne "M5_ter ✅ shipped" |
| [docs/specs/M5_bis_competitive_eviction_spec.md](./M5_bis_competitive_eviction_spec.md) | ±2 | Open question "watcher live-reload" marquée résolue |
| tests/unit/test_watcher_live_reload.py | +300 | Nouveau fichier, 14 cas de test |

**Total LOC** : ~165 LOC prod + ~300 LOC tests. Pas de migration DB, pas de changement de schéma.

---

## 11. Migration / Backwards compat

- **Aucune migration DB** : M5_ter ne touche aucune table, aucun index, aucune colonne. Zéro interaction Alembic.
- **Env var opt-in avec default safe** : `WATCHER_RELOAD_INTERVAL_SECONDS=300` par défaut. Un user qui upgrade de M5_bis à M5_ter sans toucher `.env` obtient automatiquement le comportement live-reload avec une cadence 5 min.
- **Downgrade** : revenir à M5_bis revient à recompiler l'ancien `WatcherOrchestrator` sans boucle reload. Pas de data à migrer, pas d'état incompatible.
- **Behavior parity avec M5_bis si interval élevé** : `WATCHER_RELOAD_INTERVAL_SECONDS=3600` → le watcher reload 1 fois par heure, très proche du comportement M1..M5_bis (reload au boot uniquement) tant que l'interval n'est pas franchi. Utile pour observer/debugger si on suspecte un bug introduit par M5_ter.
- **Non-régression M1/M5** : tous les tests existants sur `WatcherOrchestrator` (test_watcher_orchestrator.py) doivent passer identiques. Les fixtures qui seedent des wallets pré-boot continuent de fonctionner — le 1er cycle reload voit immédiatement les wallets seedés.

---

## 12. Commandes de vérification finale

```bash
# Lint + format
ruff check . && ruff format .

# Types stricts
mypy --strict src

# Tests unitaires M5_ter + non-régression watcher
pytest tests/unit/test_watcher_live_reload.py tests/unit/test_watcher*.py -v

# Couverture watcher
pytest tests/unit/test_watcher_live_reload.py --cov=polycopy.watcher --cov-report=term-missing

# Full suite pour non-régression globale
pytest

# Smoke test manuel (en observation logs) :
# 1. Démarrer le bot : DISCOVERY_ENABLED=true python -m polycopy
# 2. Observer log watcher_reload_cycle au boot (added=N, si N=0 c'est OK)
# 3. Dans un autre terminal, seed un wallet manuellement :
#    sqlite3 polycopy.db "INSERT INTO target_traders (wallet_address, status, active, added_at, pinned) VALUES ('0xdeadbeef', 'active', 1, datetime('now'), 0);"
# 4. Attendre <WATCHER_RELOAD_INTERVAL_SECONDS> secondes
# 5. Log attendu : watcher_reload_cycle added=1 added_wallets=["0xdeadbeef"]
# 6. Retirer le wallet :
#    sqlite3 polycopy.db "UPDATE target_traders SET status='shadow', active=0 WHERE wallet_address='0xdeadbeef';"
# 7. Attendre <WATCHER_RELOAD_INTERVAL_SECONDS> secondes
# 8. Log attendu : watcher_reload_cycle removed=1 removed_wallets=["0xdeadbeef"]
# 9. Ctrl+C → watcher_stopped clean.
```

---

## 13. Hors scope M5_ter (à ne pas implémenter)

- Reload dynamique de `BLACKLISTED_WALLETS` / `TARGET_WALLETS` sans restart (reste hors scope — nécessite un file watcher sur `.env`, scope bien plus large).
- Pub/sub DB (SQLite TRIGGER + NOTIFY, Postgres LISTEN/NOTIFY). Le TTL polling est volontairement simple.
- Intégration à un `/admin` endpoint pour forcer un reload immédiat (ex. `POST /v1/watcher/reload`). Hors scope M5_ter — serait un ajout pertinent à M12_bis remote control si demandé plus tard.
- Reload des `POLL_INTERVAL_SECONDS` individuels (hot-swap cadence par poller).
- Métrique temps-réel "dernier reload succesful at" exposée au dashboard. Prévu M14+ si besoin d'observability supplémentaire.

---

## 14. Notes d'implémentation & zones d'incertitude

### 14.1 `asyncio.TaskGroup` + `create_task` dynamique

Python 3.11+ supporte `tg.create_task(...)` pendant que le TaskGroup est actif, tant que le contexte `async with` n'est pas sorti. Attention : si une task créée **avant** le reload loop raise une exception qui fait sortir le TaskGroup, **toutes** les tasks (y compris celles créées dynamiquement) sont cancellées. Comportement souhaité (non-régression M1).

Edge case : si le reload loop lui-même raise une exception non-catchée, le TaskGroup remonte la cascade. Le code actuel `except Exception: log.warning(watcher_reload_failed)` à l'intérieur de la boucle protège contre ça — seul un `asyncio.CancelledError` ou un bug structurel remonterait.

### 14.2 Interaction avec `poller.run(stop_event)` cancel

Un `task.cancel()` injecte un `CancelledError` dans la coroutine `poller.run`. La boucle `while not stop_event.is_set()` du poller absorbe naturellement — le poller sort proprement. Aucun changement requis dans `WalletPoller` (diff strictement additif).

**Attention latence M11** : le poller écrit des samples `trade_latency_samples` via `latency_repo.insert`. Si cancel pendant un insert DB, la session SQLAlchemy async est rollback par le context manager. Aucun orphelin.

### 14.3 Race condition — reload mid-scoring

Scénario théorique : `DiscoveryOrchestrator` écrit `status='active'` pour `0xnew` à T+0. `WatcherOrchestrator` fait son reload cycle à T+0.01 (très simultané). `list_wallets_to_poll()` peut ou non voir `0xnew` selon la visibilité de transaction SQLAlchemy.

**Décision** : on accepte la latence `WATCHER_RELOAD_INTERVAL_SECONDS` max entre écriture et prise en compte. Si le write de Discovery passe juste après le read du watcher, le wallet sera vu au cycle suivant (donc dans max `WATCHER_RELOAD_INTERVAL_SECONDS` secondes). Aucune criticité temps réel (le wallet ne fera de toute façon rien tant qu'il n'est pas polled).

### 14.4 Test timings

Les tests M5_ter nécessitent des intervalles courts (0.1s) pour finir en < 5s. Utiliser `monkeypatch.setattr(settings, "watcher_reload_interval_seconds", 0.1)` ou construire `Settings(watcher_reload_interval_seconds=0.1)` directement.

**Attention** : Pydantic `Field(ge=30)` bloquerait 0.1. Solutions :
1. Relâcher la validation Pydantic en test (override Field via `model_validate`).
2. Ajouter un mode de test isolé `Settings.for_testing(reload_interval=0.1)` qui bypass la validation.
3. Mocker la fonction `_sleep_or_stop` pour retourner instantanément.

**Décision préférée** : option 3 — mocker `_sleep_or_stop`. Evite de relâcher la validation prod. Alternative : utiliser `pytest.MonkeyPatch.setattr` sur l'attribut de l'instance après construction.

---

## 15. Prompt d'implémentation

Ready-to-paste pour lancer l'implémentation :

```
Tu es l'implémenteur de la milestone M5_ter (polycopy). La spec fait autorité :

- Lecture obligatoire :
  - docs/specs/M5_ter_watcher_live_reload_spec.md (cette spec)
  - docs/specs/M5_bis_competitive_eviction_spec.md (M5_bis dont M5_ter dépend)
  - docs/specs/M1-watcher-storage.md (pattern M1 inchangé)
  - CLAUDE.md (conventions + sécurité Discovery M5 + M5_bis)

- Workflow strict :
  1. Une seule phase (M5_ter est petite, ~165 LOC prod + 300 LOC tests). Une
     branche `feat/m5ter` + une PR squashée vers main.
  2. Initialise un TodoWrite avec les étapes :
     (a) feat(storage): list_wallets_to_poll method on TargetTraderRepository
     (b) feat(config): WATCHER_RELOAD_INTERVAL_SECONDS env var
     (c) feat(watcher): reload cycle in WatcherOrchestrator.run_forever
     (d) test(watcher): 14 tests live-reload
     (e) docs: README + docs/architecture + CLAUDE.md + M5_bis spec update
  3. Après chaque commit, lance ciblé : `pytest tests/unit/test_watcher*.py`
     `ruff check src/polycopy/watcher/ src/polycopy/storage/` `mypy --strict src`.
  4. Full pytest avant la PR.

- Règles code :
  - Python 3.11+ strict type hints, Pydantic v2, async partout, mypy --strict.
  - Pas de commentaires superflus (CLAUDE.md).
  - Cite `path:line` dans les messages de commit quand tu touches du code existant.
  - Conventional commits (feat/test/docs).
  - Tests en même temps que le code.

- Sécurité (non-négociable) :
  - Diff additif strict sur WalletPoller (zéro ligne modifiée).
  - Pinned preservation + blacklist absolute double-check.
  - active↔sell_only = no-op watcher (poller continue sans interruption).
  - Pas de secret loggé — vérifié automatique par grep dans le test_security.

- Décisions déjà tranchées (ne pas re-débattre) :
  - Nouvelle méthode list_wallets_to_poll (pas param sur list_active).
  - TaskGroup vivant avec create_task dynamique (pas dict à plat).
  - Interval fixe TTL (pas adaptatif).
  - Mock _sleep_or_stop pour les tests rapides.

- Démarrage :
  Lis les 4 fichiers obligatoires, résume en 3 lignes ce que tu vas faire en
  Phase unique M5_ter, crée la branche feat/m5ter, initialise le TodoWrite
  avec les 5 étapes, attaque l'étape (a). Rends-moi la main après la PR
  prête pour review.
```

---

## 16. Commit message proposé

```
feat(watcher): M5_ter live-reload pollers on target_traders changes

Ajoute un cycle de reload périodique dans WatcherOrchestrator.run_forever
qui re-fetch list_wallets_to_poll() toutes les
WATCHER_RELOAD_INTERVAL_SECONDS (défaut 300s) et diff set-based contre les
pollers en cours : tg.create_task pour les nouveaux, task.cancel + await
pour les retirés. Réactif aux mutations M5 (promote/demote) et M5_bis
(eviction cascade, sell_only wind-down, blacklist reconcile) sans restart.

Nouvelle méthode TargetTraderRepository.list_wallets_to_poll filtre
status IN ('active', 'pinned', 'sell_only') + exclusion blacklist env
(défense-en-profondeur). Transition active↔sell_only = no-op côté
watcher (le poller continue pour copier les SELL, BUY bloqués par
TraderLifecycleFilter strategy).

Diff strictement additif : zéro ligne modifiée dans WalletPoller,
DataApiClient, DetectedTradeRepository, DiscoveryOrchestrator,
EvictionScheduler. 14 tests unitaires (promote, eviction, demote,
blacklist, cancel, stop_event mid-cycle, DB lock retry, pinned,
non-régression M1).

Cf. spec docs/specs/M5_ter_watcher_live_reload_spec.md.
```

---

_Fin de la spec M5_ter. Prochaine étape : me lancer via le prompt §15._
