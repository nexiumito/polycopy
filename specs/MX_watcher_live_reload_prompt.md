# Prompt pour générer la spec M10 — Live-reload du watcher

> Copie le bloc ci-dessous tel quel dans une nouvelle session Claude Code.
> Il contient tout le contexte nécessaire (codebase, décisions, contraintes)
> pour produire `specs/M10-watcher-live-reload.md` au même format détaillé que
> `specs/M3-executor.md`, `specs/M5-trader-scoring.md`, `specs/M8-dry-run-realistic.md`.

---

```
Tu vas écrire la spec d'implémentation `specs/M10-watcher-live-reload.md` pour
le projet polycopy (bot de copy-trading Polymarket en Python 3.11 / asyncio).

Avant tout, lis :
- `CLAUDE.md` (conventions code + sécurité)
- `docs/architecture.md` (vue d'ensemble + status par module)
- `specs/M1-watcher-storage.md` (M1 watcher dont M10 étend)
- `specs/M5-trader-scoring.md` (M5 discovery dont M10 résout un blind spot)
- `specs/M8-dry-run-realistic.md` (format de référence pour la spec)
- `src/polycopy/watcher/orchestrator.py` (le module à étendre)
- `src/polycopy/watcher/wallet_poller.py`
- `src/polycopy/storage/repositories.py` (TargetTraderRepository.list_active)
- `src/polycopy/discovery/orchestrator.py` (qui promeut les wallets en DB)

## Contexte du problème (à formaliser dans la spec)

Aujourd'hui (M1→M9), `WatcherOrchestrator.run_forever` appelle
`target_repo.list_active()` UNE seule fois au boot, crée un `WalletPoller` par
wallet trouvé, lance les pollers dans un `asyncio.TaskGroup` figé, et boucle
jusqu'à `stop_event.set()`. Quand M5 `DiscoveryOrchestrator` promeut un wallet
(`shadow → active` en DB + alerte Telegram `trader_promoted`), le statut DB
est mis à jour mais le watcher ne re-fetch jamais cette liste — donc le
nouveau wallet n'est PAS polled jusqu'au prochain restart.

Symptôme observé : un user qui laisse polycopy tourner avec
`DISCOVERY_ENABLED=true` accumule des "blind spots" — chaque cycle discovery
peut ajouter des wallets en DB que le bot copie sur le dashboard mais ne
poll pas réellement. Workaround actuel : restart manuel ou cron. Inacceptable
pour un mode autonome.

## Objectif M10 (livrable)

Étendre `WatcherOrchestrator` pour qu'il :
1. Re-fetch `target_repo.list_active()` toutes les
   `WATCHER_RELOAD_INTERVAL_SECONDS` (default 300, soit 5 min).
2. Diff vs les pollers en cours :
   - Wallets nouveaux → `WalletPoller(...)` instancié + `tg.create_task(...)`.
   - Wallets retirés (demote → paused, ou suppression manuelle) → cancel
     proprement la tâche du poller correspondant.
3. Logger structlog : `watcher_reload_cycle`, `watcher_pollers_added=N`,
   `watcher_pollers_removed=N`, `watcher_pollers_total=N`.
4. Préserver tous les invariants M1/M5 :
   - Pinned (TARGET_WALLETS env) jamais retirés (M5 garantit `pinned=True`,
     `transition_status` raise sur pinned).
   - Blacklist : `BLACKLISTED_WALLETS` exclus côté DB déjà.
   - Aucune dédup tx_hash cassée (chaque poller a son own state, pas de
     race avec les inserts `DetectedTrade`).

## Contraintes non négociables

- **Diff strictement additif sur M1** : zéro ligne modifiée dans
  `WalletPoller`, `DataApiClient`, `DetectedTradeRepository`. M10 modifie
  uniquement `WatcherOrchestrator.run_forever`.
- **TaskGroup vivant** : utiliser `asyncio.TaskGroup` avec ajouts dynamiques
  (Python 3.11+ supporte `tg.create_task` pendant qu'il tourne).
  Alternative si problématique : gérer les pollers à plat avec un dict
  `wallet → asyncio.Task`, cancel manuels, et un `asyncio.gather` final.
- **Cancel propre des pollers retirés** : `task.cancel()` + `await task`
  pour absorber `CancelledError` ; le poller doit accepter une annulation
  externe sans corrompre son état (httpx client partagé, pas de leak).
- **Cadence configurable** : `WATCHER_RELOAD_INTERVAL_SECONDS` env, default
  300, bornes `[30, 3600]` via `Field(ge=30, le=3600)` Pydantic.
- **Pas de race avec stop_event** : si `stop_event.set()` pendant un cycle
  de reload, sortie propre (cancel tous les pollers, log
  `watcher_stopped`).
- **Logs sobres** : pas de log à chaque cycle si pas de changement
  (info seulement si `added > 0 or removed > 0`, sinon debug).
- **Pas de dépendance vers M11/M12** : M10 marche tout seul, n'attend
  aucun nouveau composant. Indépendance complète.
- **Conventions CLAUDE.md** : async, type hints stricts, structlog, pas
  de print, docstrings FR, code/identifiants EN.
- **mypy --strict propre, ruff propre, pytest ≥ 80 % coverage sur
  `src/polycopy/watcher/`**.
- **Tests via respx + mock TargetTraderRepository** : pas d'appel réseau
  réel.

## Cas de test obligatoires (à lister dans la spec §9)

1. Boot : 3 wallets en DB → 3 pollers démarrés. Pas de cycle reload tant
   que TTL pas atteint.
2. Cycle après promote : 1 wallet ajouté en DB → reload trouve 4 wallets,
   ajoute 1 poller. `watcher_pollers_added=1`.
3. Cycle après demote : 1 wallet `active=False` → reload trouve 3 wallets,
   cancel le poller. `watcher_pollers_removed=1`. Le poller cancelled
   absorbe CancelledError sans crash.
4. Cycle no-op : aucune diff → log debug only, aucune action.
5. Stop_event mid-cycle : reload commencé puis stop_event set → sortie
   propre, tous pollers cancel.
6. Repository raise : DB lock momentané → cycle skipped + log warning,
   prochain cycle retry.
7. Pinned not removed : un pinned `transition_status` raise (M5 invariant)
   → ne disparaît jamais de la liste, jamais cancel.
8. Blacklist coexistence : un wallet en blacklist n'apparaît jamais dans
   `list_active()` → jamais polled (pre-M10 + post-M10).

## Mises à jour de doc (à inclure dans le commit)

- `README.md` : 1 ligne dans la table env vars (`WATCHER_RELOAD_INTERVAL_SECONDS`).
- `docs/architecture.md` : section Watcher gagne un paragraphe "Status M10".
- `CLAUDE.md` : section sécurité gagne 1 bullet sur l'invariant pinned
  préservé par le live-reload.
- `docs/setup.md` : un §18 court "Live-reload du watcher (M10)".
- `.env.example` : la nouvelle variable avec commentaire.

## Format de la spec

Suis le format exhaustif de `specs/M8-dry-run-realistic.md` :
- §0 Pré-requis (bootstrap, env vars, sécurité, critère validation env)
- §1 Objectif scope exact (livrable + hors livrable)
- §2 Arbitrages techniques (tranchés explicitement, alternatives écartées)
- §3 Arborescence du module (fichiers touchés)
- §4 APIs (aucune nouvelle ici, juste réutilisation TargetTraderRepository)
- §5 Storage (aucune migration nécessaire)
- §6 DTOs / signatures (typing du nouveau dict pollers etc)
- §7 Algorithme du reload cycle (diff + cancel)
- §8 (skip — pas pertinent ici)
- §9 Tests détaillés
- §10 Doc updates
- §11 Commandes de vérification finale
- §12 Critères d'acceptation
- §13 Hors scope M10 (NE PAS implémenter)
- §14 Notes d'implémentation + zones d'incertitude
- §15 Prompt à m'envoyer pour lancer l'implémentation

Cible 600-900 lignes (M10 est plus simple que M8). Préfère la précision
chirurgicale aux développements philosophiques.

À la fin, propose un commit message :
`feat(watcher): M10 live-reload pollers on target_traders changes`
```
