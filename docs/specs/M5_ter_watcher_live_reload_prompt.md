# Prompt pour générer la spec M5_ter — Live-reload du watcher

> Copie le bloc ci-dessous tel quel dans une nouvelle session Claude Code.
> Il contient tout le contexte nécessaire (codebase, décisions, contraintes)
> pour produire `docs/specs/M5_ter_watcher_live_reload_spec.md`.
>
> **Prérequis** : la feature M5_bis (competitive eviction) doit être
> implémentée avant M5_ter. Le prompt ci-dessous suppose que `sell_only` et
> `blacklisted` existent déjà dans l'enum `TraderStatus`.

---

```
Tu vas écrire la spec d'implémentation `docs/specs/M5_ter_watcher_live_reload_spec.md`
pour le projet polycopy (bot de copy-trading Polymarket en Python 3.11 / asyncio).

## Prérequis de lecture (à faire avant d'écrire)

- `CLAUDE.md` (conventions code + sécurité, notamment sections Discovery M5
  et M12_bis)
- `docs/architecture.md` (vue d'ensemble + status par module)
- `docs/specs/M1-watcher-storage.md` (M1 watcher, la couche que M5_ter étend)
- `docs/specs/M5-trader-scoring.md` (M5 discovery — origine du blind spot)
- `docs/specs/M5_bis_competitive_eviction_spec.md` ← **CRITIQUE** : M5_ter
  doit gérer les nouveaux statuts `sell_only` et `blacklisted` introduits
  par M5_bis, ainsi que les transitions `active ↔ sell_only` réversibles.
- `docs/specs/M12_bis_multi_machine_remote_control_spec.md` — **format de
  référence de style** (sections numérotées, tableaux, invariants,
  phases d'implémentation, test plan). Nouvelle spec doit suivre
  rigoureusement ce format.
- `src/polycopy/watcher/orchestrator.py` (le module à étendre)
- `src/polycopy/watcher/wallet_poller.py`
- `src/polycopy/storage/repositories.py` (TargetTraderRepository)
- `src/polycopy/storage/models.py` (enum TraderStatus étendu par M5_bis)
- `src/polycopy/discovery/orchestrator.py` (qui mute le status en DB)

## Contexte du problème (à formaliser dans la spec §1)

Aujourd'hui (et jusqu'à M5_bis inclus), `WatcherOrchestrator.run_forever`
appelle `target_repo.list_active()` **une seule fois** au boot, crée un
`WalletPoller` par wallet trouvé, lance les pollers dans un
`asyncio.TaskGroup` figé, et boucle jusqu'à `stop_event.set()`.

Conséquence : toute mutation du statut d'un wallet en DB survenant après
le boot est ignorée par le watcher. Observé en conditions réelles
(2026-04-21, PC uni-debian) :

- `DiscoveryOrchestrator` promote 7 wallets `shadow → active` dans la
  matinée → alertes Telegram `trader_promoted` correctement envoyées,
  dashboard affiche bien les 7 actifs.
- Mais **0 trade détecté** sur la journée : le watcher polle toujours sa
  liste vide du boot. Résumé quotidien M7 reflète fidèlement l'état :
  "Trades détectés : 0, Top wallets actifs : aucun, Discovery : 7 promus".

Avec M5_bis (eviction compétitive) la surface de mutations s'élargit :
- `shadow → active*` (cascade eviction)
- `active → sell_only` (évincé)
- `sell_only → active` (abort ou rebond)
- `sell_only → shadow` (positions closes)
- `active/sell_only/shadow → blacklisted` (user edit env var)
- `blacklisted → shadow` (user retrait)

**Toute ces transitions doivent refléter en quasi-temps-réel sur
l'ensemble des pollers actifs du watcher.**

## Objectif M5_ter (livrable)

Étendre `WatcherOrchestrator` pour qu'il :

1. **Re-fetch périodiquement** la liste des wallets à poller depuis la DB
   toutes les `WATCHER_RELOAD_INTERVAL_SECONDS` (défaut 300, soit 5 min).

2. **Définir la requête "wallets à poller"** :
   Status ∈ {`active`, `sell_only`} ET NOT in BLACKLISTED_WALLETS.

   Décision de design à figer dans la spec :
   - Exposer une nouvelle méthode explicite `list_wallets_to_poll()` sur
     `TargetTraderRepository` (recommandé — clarté sémantique), OU
   - Étendre la signature de `list_active()` avec un param
     `include_sell_only: bool = True` (plus léger mais sémantique floue).

   La spec doit trancher en faveur de la 1ère option par défaut. `list_active()`
   reste disponible pour les usages discovery/dashboard qui veulent vraiment
   "seulement les actifs au sens stricte" (UI score display, etc.).

3. **Diff set-based vs les pollers en cours** :
   - current_polled = clés du dict `self._pollers_by_wallet`
   - desired_polled = wallets retournés par `list_wallets_to_poll()`
   - to_add = desired_polled − current_polled → instancier `WalletPoller` +
     `tg.create_task(...)`
   - to_remove = current_polled − desired_polled → `task.cancel()` +
     `await task` pour absorber proprement `CancelledError`

4. **Cas particulier : transition `active ↔ sell_only`** n'est PAS un
   event watcher. Le wallet continue d'être polled identiquement. La
   logique de filtrage BUY/SELL côté Strategy (M5_bis) traite la
   différence. M5_ter ignore cette transition — set diff ne change pas.

5. **Logger structlog** :
   - `watcher_reload_cycle` (debug si no-op, info si changement)
   - `watcher_pollers_added=<count>` + `added_wallets=[...]`
   - `watcher_pollers_removed=<count>` + `removed_wallets=[...]`
   - `watcher_pollers_total=<count>`
   - `watcher_reload_failed` (warning si DB lock / erreur ponctuelle —
     le cycle suivant retentera)

6. **Préserver tous les invariants M1/M5/M5_bis** :
   - `pinned=true` wallets (TARGET_WALLETS env) jamais retirés même si
     leur status devient bizarre (M5 invariant, `transition_status` raise
     déjà sur pinned).
   - `BLACKLISTED_WALLETS` absolu : un wallet dans cette liste n'apparaît
     jamais dans `list_wallets_to_poll()` peu importe son status DB.
   - Aucune dédup `tx_hash` cassée (chaque poller a son own in-memory
     state, pas de race avec les inserts `DetectedTrade` via la DB).
   - Cancel d'un poller n'impacte pas les trades déjà détectés et
     persistés en DB — le flush est synchrone côté poller.

## Contraintes non négociables

- **Diff strictement additif sur M1/M5/M5_bis** : zéro ligne modifiée dans
  `WalletPoller`, `DataApiClient`, `DetectedTradeRepository`,
  `DiscoveryOrchestrator`, `EvictionScheduler` (M5_bis). M5_ter modifie
  uniquement `WatcherOrchestrator.run_forever` + 1 nouvelle méthode
  repository.
- **TaskGroup vivant** : utiliser `asyncio.TaskGroup` avec ajouts
  dynamiques (Python 3.11+ le supporte via `tg.create_task` pendant que
  le groupe tourne). Alternative si problématique : gérer les pollers à
  plat avec un dict `wallet → asyncio.Task`, cancel manuels, et un
  `asyncio.gather(*tasks)` final. La spec doit trancher explicitement.
- **Cancel propre des pollers retirés** : `task.cancel()` puis
  `await task` dans un `except asyncio.CancelledError: pass`. Le poller
  doit accepter une annulation externe sans corrompre son état (httpx
  client partagé par l'orchestrator, pas de leak si le poller meurt).
- **Cadence configurable** : `WATCHER_RELOAD_INTERVAL_SECONDS` env,
  défaut 300, bornes `[30, 3600]` via `Field(ge=30, le=3600)` Pydantic.
  La limite min 30s est choisie pour éviter l'over-polling DB si l'user
  settle trop bas par erreur.
- **Pas de race avec stop_event** : si `stop_event.set()` pendant un
  cycle reload, sortie propre (cancel tous les pollers en attente,
  log `watcher_stopped_during_reload`).
- **Logs sobres** : pas de log info à chaque cycle si pas de changement
  (debug uniquement si no-op, info seulement si
  `added > 0 or removed > 0`).
- **Pas de dépendance vers M14+** : M5_ter marche tout seul après M5_bis,
  n'attend aucun composant futur.
- **Conventions CLAUDE.md** : async, type hints stricts, structlog, pas
  de print, docstrings FR, code/identifiants EN.
- **Qualité** : `mypy --strict` propre, `ruff check . && ruff format .`
  propre, coverage `pytest` ≥ 85% sur `src/polycopy/watcher/`.
- **Tests via respx + mock TargetTraderRepository** : pas d'appel réseau
  réel.

## Cas de test obligatoires (à lister dans la spec §9)

1. **Boot froid** : 3 wallets actifs + 0 sell_only en DB → 3 pollers
   démarrés. Pas de cycle reload tant que TTL pas atteint.

2. **Cycle après promote M5** : 1 wallet `shadow → active` en DB
   (via Discovery scheduler) → reload trouve 4, ajoute 1 poller.
   `watcher_pollers_added=1`, `added_wallets=["0xabc..."]`.

3. **Cycle après eviction M5_bis active → sell_only** : wallet continue
   d'être polled. `watcher_pollers_added=0, removed=0`. Pas de log info
   (c'est un no-op côté watcher).

4. **Cycle après rebond M5_bis sell_only → active** : même, no-op watcher.

5. **Cycle après sell_only → shadow (positions closes)** : reload trouve
   N-1, cancel le poller du wallet qui n'est plus en {active, sell_only}.
   `watcher_pollers_removed=1`. Le poller cancelled absorbe
   `CancelledError` sans crash, ses httpx connexions sont proprement
   fermées.

6. **Cycle après demote active → shadow** : idem, cancel le poller.

7. **Cycle après blacklist ajouté** : user édite BLACKLISTED_WALLETS +
   restart bot → au 1er cycle reload le wallet blacklist n'est plus
   dans `list_wallets_to_poll()` → cancel. Note : le restart est
   nécessaire car BLACKLISTED_WALLETS est une env var statique. M5_ter
   ne se charge pas du reload dynamique d'env vars (hors scope).

8. **Cycle no-op** : aucune diff → log debug only, aucune action. Après
   10 cycles no-op, toujours aucun log info (évite spam).

9. **Stop_event mid-cycle** : reload commencé (appelle
   `list_wallets_to_poll()` → retourne 5), au milieu de la phase d'ajout
   de pollers → stop_event set → sortie propre sans lancer les nouveaux,
   cancel des existants.

10. **Repository raise** : DB lock momentané sur `list_wallets_to_poll()`
    → cycle skipped + log warning `watcher_reload_failed`, prochain
    cycle retry sans dégât.

11. **Pinned not removed** : un pinned wallet dans TARGET_WALLETS env
    reste toujours dans `list_wallets_to_poll()` (M5 garantit pinned +
    active, `transition_status` raise sur pinned). Jamais cancel.

12. **Blacklist coexistence** : un wallet listé dans
    `BLACKLISTED_WALLETS` + `DISCOVERY_ENABLED=true` n'apparaît jamais
    dans `list_wallets_to_poll()` même si score théorique eleve
    (vérifié 2× côté M5, 1× supplémentaire côté M5_ter — défense en
    profondeur).

13. **Non-régression M1..M5_bis** : si `WATCHER_RELOAD_INTERVAL_SECONDS`
    est très élevé (ex. 3600s = 1h), le comportement M1..M5_bis est
    observé : le watcher se comporte quasi comme avant (1 reload/h).
    Les tests M1 existants doivent rester green.

14. **Edge case cancel simultané** : 2 wallets à retirer au même cycle
    → `await asyncio.gather(*cancels, return_exceptions=True)` pour
    absorber leurs `CancelledError` sans fuite.

## Mises à jour de doc (à inclure dans le commit)

- `README.md` :
  - Section "Variables d'environnement" → bloc `<details>` Discovery ou
    Watcher → ajout `WATCHER_RELOAD_INTERVAL_SECONDS` avec default 300.
  - Section "Roadmap" → ajout `[x] M5_ter : Watcher live-reload`.
  - Section "Architecture & stack" → le diagramme watcher gagne une
    note "+ reload cycle (M5_ter)" ou équivalent.
- `docs/architecture.md` : section Watcher gagne un paragraphe
  "Status M5_ter" expliquant le reload cycle.
- `CLAUDE.md` : section sécurité gagne 1 bullet sur l'invariant
  pinned + blacklisted préservé par le live-reload, et sur le fait
  que M5_ter ne touche jamais aux pollers actifs pour la transition
  active ↔ sell_only (M5_bis).
- `.env.example` : la nouvelle variable avec commentaire explicatif.
- `docs/specs/M5_bis_competitive_eviction_spec.md` : section "Open
  questions" → marquer "watcher live-reload" comme résolu par M5_ter.

## Format de la spec

Suis le format rigoureux de `docs/specs/M12_bis_multi_machine_remote_control_spec.md` :

- §1 Motivation + use case (observé 2026-04-21 sur uni-debian avec 7 promus mais 0 détecté)
- §2 Scope / non-goals (ce que M5_ter NE fait PAS — ex: reload dynamique env vars, notifications temps réel via pub-sub DB, etc.)
- §3 User stories (3 scénarios : promote, eviction→sell_only, demote total)
- §4 Architecture (position dans la stack, interaction avec Discovery et
  EvictionScheduler)
- §5 Algorithme du reload cycle (pseudocode du diff set + cancel propre)
- §6 DTOs / signatures (la nouvelle méthode `list_wallets_to_poll()`,
  typing du dict pollers)
- §7 Settings (nouvelle env var `WATCHER_RELOAD_INTERVAL_SECONDS`)
- §8 Invariants sécurité (pinned preservation, blacklist absolute,
  no-regression on `active ↔ sell_only`)
- §9 Test plan (14 cas ci-dessus, parametrizés pytest quand pertinent)
- §10 Impact sur l'existant (liste fichiers + LOC estimé ; cible
  ~150-250 LOC implémentation + ~300 LOC tests)
- §11 Migration / Backwards compat (aucune migration DB, env var
  optionnelle avec défaut sûr)
- §12 Commandes de vérification finale (pytest, ruff, mypy, smoke test
  manuel reload en observant les logs)
- §13 Hors scope M5_ter (NE PAS implémenter)
- §14 Notes d'implémentation + zones d'incertitude (TaskGroup vs plat,
  gestion du cancel race avec stop_event)
- §15 Prompt à m'envoyer pour lancer l'implémentation

Cible 400-700 lignes (M5_ter est plus simple que M12_bis). Préfère la
précision chirurgicale aux développements philosophiques.

## À la fin, propose un commit message

`feat(watcher): M5_ter live-reload pollers on target_traders changes`

Ne commit PAS — l'utilisateur validera la spec d'abord.
```
