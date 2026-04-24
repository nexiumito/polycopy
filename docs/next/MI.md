# MI — Ops hygiene + Goldsky free tier integration

**Priorité** : 🟡 P3 (slack time, ops quality-of-life)
**Charge estimée** : M (2-3 jours)
**Branche suggérée** : `chore/ops-hygiene-goldsky`
**Prérequis** : aucun
**Bloque** : — (parallélisable)

---

## 1. Objectif business

Améliorer la qualité ops du projet sur 4 axes :
1. **Shutdown graceful** — `systemctl stop polycopy.service` prend 1-3 min puis SIGKILL (observé session audit). Gain : <15s reboot propre.
2. **Setup script + scripts Python cross-platform** — `python3` vs `python`, shebang manquants, `night_test_status.py` non-exécutable direct sur Debian fresh install.
3. **DB schema documentation** — commandes SQL ad-hoc actuellement cassantes (colonnes `status_updated_at`, `created_at` qu'on a inventées). Document canonique.
4. **Goldsky free Starter integration** — convergence 3/3 deep-search pour enrichir discovery pipeline avec data temps réel subgraph, zero coût.

Pas bloquant mais améliore la productivité debug/investigation quotidienne + débloque MF (Sirolly port peut bénéficier de data Goldsky cleaner).

## 2. Contexte & problème observé

### Findings référencés

- **Backlog `docs/backlog.md` §"Shutdown lent"** : "`systemctl --user stop polycopy.service` ou `restart` prend régulièrement 1-3 min avant de reprendre la main, et finit souvent sur un TimeoutStopSec → SIGKILL systemd forcé. Observé plusieurs fois lors des maintenance windows M13". Hypothèses : WalletPoller cycle bloquant, ClobMarketWSClient reconnect loop sans check stop_event, DryRunResolutionWatcher `wait_for`, MonitoringOrchestrator schedulers `asyncio.sleep` long.

- **Session audit user feedback 2026-04-24** : `bash scripts/night_test_status.py` crashed avec "from : commande introuvable" (parseur shell qui prend le Python pour du bash). `python3 scripts/night_test_status.py` crashed avec "[FATAL] polycopy not installed" alors que `bash scripts/setup.sh` vient de tourner.

- **Session audit user feedback 2026-04-24** : commandes SQL envoyées contenaient `status_updated_at` et `created_at` qui n'existent pas dans le schéma — j'ai inventé ces noms. Schema réel : probablement `status_changed_at`, `event_at` / `cycle_at`. **Pas documenté** — le user doit chercher dans `src/polycopy/storage/models.py` ou faire `sqlite3 .schema`.

- **[F23] 🟢 3/3** (synthèse §3.2) : Goldsky free Starter tier adopté. **Perplexity B5** : "Starter plan free (no card), includes 3 always-on subgraphs and 100,000 entities free, Scale charging ~$0.05 per worker-hour beyond 2,250 hours". **Gemini §"Bootstrapping"** : "excellent for historical backfilling of a newly discovered wallet". **Claude §7.1 (b) + §8.3** : "Goldsky Turbo Pipeline on `polymarket.order_filled` Starter-free-tier as an incremental fan-out". Prix estimé $36-50/mois pour Turbo Pipeline avec worker + entity storage. **Starter free** = 3 subgraphs + 100k entities, suffisant pour notre usage.

- **Audit M-024** : `filter_noisy_endpoints` re-compile regex à chaque `configure_logging()`. Pattern list compilée fresh à chaque call. Inefficace sur tests en boucle. Fix : cache module-level.

- **Audit M-014** : validator cross-field manquant `TARGET_WALLETS ∩ BLACKLISTED_WALLETS`. CLAUDE.md annonce crash boot clair mais partiellement en place.

- **Audit M-027** : `TargetTraderRepository.list_wallets_to_poll()` blacklist appliquée en Python pas SQL. Défense-en-profondeur saute si `blacklist=None`. Fix : SQL WHERE + Python double-check.

- **Audit H-010** : `RateLimiter` + `AutoLockdown` per-process → multi-worker uvicorn bypass. Fix : documenter crash boot si `workers > 1`.

- **Audit H-011** : TOCTOU entre `mkdir`/`chmod`/`write_text` dans `SentinelFile.touch()`. Fix : `os.open(path, O_WRONLY|O_CREAT|O_EXCL, 0o600)` atomique.

- **Audit H-012** : Migration 0007 data migration non-transactionnelle. Fix : wrapper dans `connection.begin():`.

### Sessions originales mappées

**Session D brouillon** items D3 (shutdown), D4 (setup script), D5 (DB queries), D6 (schema docs), D7 (night_test_status robustesse) → intégrés ici. D1+D2 sont dans **ME** (latence pipeline).

## 3. Scope (items détaillés)

### MI.1 — Shutdown graceful timeout 10s

- **Location** : [src/polycopy/cli/runner.py](../../src/polycopy/cli/runner.py) + audit boucles `while not stop_event` dans tous les orchestrators
- **Ce qu'il faut faire** :
  - **Fix v1 (rapide)** : wrapper `TaskGroup.__aexit__` dans `asyncio.wait_for(timeout=SHUTDOWN_TIMEOUT_SECONDS=10)`. Si timeout, log ERROR + laisser systemd SIGKILL prendre la main.
  - **Fix v2 (investigation)** : audit de chaque boucle pour identifier les await longs qui ne check pas `stop_event`. Candidats :
    - `WalletPoller._poll_cycle` : HTTP `httpx.get` timeout 30s × `_MAX_CURSOR_RESETS` × N pollers → potentiellement plusieurs minutes. Fix : wrap chaque HTTP call en `asyncio.wait_for(..., timeout=min(X, remaining_shutdown_budget))`.
    - `ClobMarketWSClient` reconnect tenacity : max 10 retries × backoff exponentiel = plusieurs minutes. Fix : check `stop_event` entre retries.
    - `DryRunResolutionWatcher._run_once` : `asyncio.wait_for(stop_event.wait(), timeout=interval_s=1800)` — devrait wakeup sur set() mais vérifier pas blocage dans `_run_once` en cours.
    - `MonitoringOrchestrator` heartbeat/daily scheduler : `asyncio.sleep` long typique. Remplacer par `asyncio.wait_for(stop_event.wait(), timeout=heartbeat_s)`.
  - **Décision D1** : commencer par Fix v1 (safe, 1h). Fix v2 = travail d'audit plus long, faire dans MI.2 séparé.
  - Settings : nouveau `SHUTDOWN_TIMEOUT_SECONDS: int = 10` (range [5, 60]).
  - Log explicite au shutdown : `shutdown_initiated` INFO, `shutdown_timeout_hit` WARNING si Fix v1 trigger.
- **Tests requis** :
  - `test_runner_shutdown_within_timeout_normal_case` (stop_event → tasks finish → exit < 5s)
  - `test_runner_shutdown_timeout_triggers_on_blocked_task` (simuler task blocked, timeout=3s, verify ERROR log)
- **Sources** : Backlog §"Shutdown lent" + Session D D3.
- **Charge item** : 0.5 jour

### MI.2 — Audit + fix boucles `while not stop_event`

- **Location** : multi-file audit (WalletPoller, ClobMarketWSClient, DryRunResolutionWatcher, MonitoringOrchestrator)
- **Ce qu'il faut faire** :
  - Grep `while not stop_event` (ou équivalent) dans `src/polycopy/`.
  - Pour chaque occurrence, vérifier que tous les `await` longs dans la boucle sont wrappés en `asyncio.wait_for` avec timeout raisonnable OU check `stop_event` avant/après.
  - Pattern recommandé :
    ```python
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(self._do_work(), timeout=shutdown_budget_s)
        except asyncio.TimeoutError:
            if stop_event.is_set():
                break
            # Retry ou log
    ```
  - **Attention** : certains awaits ne peuvent pas être timeoutés brutalement (ex : commit DB pending). Documenter les edge cases.
  - **Décision D2** : si une boucle identifiée est complexe à fix, **documenter le pattern** dans le code (commentaire `# KNOWN: this loop may delay shutdown up to X seconds due to Y`). Honnête > fake fix.
- **Tests requis** :
  - `test_wallet_poller_cycles_check_stop_event_between_calls`
  - `test_clob_ws_client_reconnect_respects_stop_event`
  - `test_dry_run_resolution_watcher_wakes_on_stop_event`
  - `test_monitoring_orchestrator_schedulers_wake_on_stop_event`
- **Sources** : Backlog §"Shutdown lent" hypothèses détaillées.
- **Charge item** : 1 jour

### MI.3 — Fix scripts Python cross-platform

- **Location** : `scripts/*.py` + `scripts/setup.sh` + `docs/setup.md`
- **Ce qu'il faut faire** :
  - Tous les scripts `scripts/*.py` : shebang `#!/usr/bin/env python3` (pas `python`). Vérifier chmod +x.
  - `scripts/setup.sh` : après venv creation, create alias `python → python3` dans venv bin OR document clearly dans README.md que toutes commandes doivent utiliser `python3` ou `python -m` après activate venv.
  - Scripts qui import `polycopy` : ajouter `sys.path` fix au top si besoin, ou check explicit `pip install -e ".[dev]"` done.
  - **`night_test_status.py` spécifique** : ajouter check `try: import polycopy; except ImportError: print("Activate venv first: source .venv/bin/activate"); sys.exit(1)`. Friendly error au lieu de `[FATAL] polycopy not installed`.
  - **Décision D3** : maintenir setup.sh comme bootstrap idempotent (déjà en place) — juste vérifier qu'il couvre le cas "shebang python3" et Debian-friendly.
  - Mise à jour `README.md` (Quickstart) et `docs/setup.md` : explicit commands `python3` pour macOS/Debian/Ubuntu.
- **Tests requis** :
  - `test_all_scripts_have_python3_shebang` (grep automatisé)
  - `test_night_test_status_friendly_error_without_venv`
- **Sources** : User feedback 2026-04-24 + Session D D4.
- **Charge item** : 0.5 jour

### MI.4 — DB schema documentation

- **Location** : nouveau fichier `docs/db_schema.md` + `scripts/db_queries.md` OR `scripts/db_helpers.py`
- **Ce qu'il faut faire** :
  - Générer automatiquement via `sqlite3 polycopy.db ".schema"` + annoter manuellement.
  - Structure `docs/db_schema.md` :
    - Table par table : colonnes + types + indexes + contraintes
    - Comments sur colonnes non-évidentes (ex: `trader_events.event_metadata JSON schema documenté`)
    - Section "Common queries" : 10-15 queries SQL prêtes à coller pour investigations courantes :
      - Adresses complètes des ACTIVE + score
      - Gate rejections par wallet pattern
      - Evolution `trader_scores` pour un wallet v1 vs v2
      - Latency samples distribution
      - Positions virtuelles orphelines
      - `auto_blacklist_candidates` (wallets cumulative_pnl < seuil)
      - Wash cluster scores (post-MF.1)
  - Alternative Python : `scripts/db_helpers.py` avec fonctions prêtes à call interactivement :
    ```python
    def active_wallets_full_addresses() -> list[str]: ...
    def wallet_score_history(wallet: str, version: str = "v2.1") -> pd.DataFrame: ...
    def gate_rejections_last_24h() -> dict[str, int]: ...
    ```
  - **Décision D4** : DOC markdown + helpers Python (les deux se complètent).
  - Mise à jour CLAUDE.md §Tests ou §Commandes courantes pour pointer vers `docs/db_schema.md`.
- **Tests requis** :
  - `test_db_schema_doc_matches_actual_schema` (CI check — dump .schema et compare)
  - `test_db_helpers_active_wallets_returns_list`
  - `test_db_helpers_functions_are_read_only` (pas de DELETE/UPDATE/INSERT)
- **Sources** : User feedback 2026-04-24 + Session D D5 + D6.
- **Charge item** : 0.5 jour

### MI.5 — Goldsky free Starter tier integration

- **Location** : nouveau fichier `src/polycopy/discovery/goldsky_enrichment.py` (ou extend `goldsky_client.py` existant) + setup doc
- **Ce qu'il faut faire** :
  - **Scope minimal** : utiliser Goldsky public subgraphs (pas Turbo Pipeline payant) pour enrichir le pool de candidats discovery.
  - Créer/renommer subgraph query :
    ```graphql
    query TopWalletsByRealizedPnl {
      userPositions(
        first: 100,
        orderBy: realizedPnl,
        orderDirection: desc,
        where: { realizedPnl_gt: 0 }
      ) {
        userId
        realizedPnl
        volume
        ...
      }
    }
    ```
  - Documentation free tier Goldsky :
    - Starter plan free, no credit card.
    - 3 always-on subgraphs.
    - 100k entities free.
    - 20 req/10s per IP (cohérent Perplexity B1 Goldsky rate limits).
  - Integration dans `CandidatePool.build()` comme **source additionnelle** au `/holders` + `/trades` Data API (pas remplacement).
  - Config setting `DISCOVERY_GOLDSKY_FREE_ENABLED: bool = True` (défaut actif après setup).
  - Setup guide `docs/setup_goldsky.md` : steps pour créer compte Goldsky free + obtenir API key (si nécessaire) + configurer `.env`.
  - **Décision D5** : pas de Turbo Pipeline webhook. Free tier + periodic query (cache TTL 6h cohérent avec cycle discovery) suffit.
  - **Attention** : `goldsky_client.py` existe déjà (cf. `src/polycopy/discovery/goldsky_client.py`) — enrichir plutôt que dupliquer.
- **Tests requis** :
  - `test_goldsky_enrichment_queries_top_wallets`
  - `test_goldsky_enrichment_respects_rate_limit`
  - `test_goldsky_fallback_on_api_error`
  - `test_candidate_pool_merges_goldsky_with_data_api`
- **Sources deep-search** : F23, Perplexity B5, Gemini §Bootstrapping, Claude §8.3.
- **Charge item** : 1 jour

### MI.6 — (Bonus) Petits fixes audit M-level

- **Location** : divers
- **Ce qu'il faut faire** :
  - **Audit M-024** : `filter_noisy_endpoints` regex cache module-level. 5 lignes fix.
  - **Audit M-014** : validator cross-field `TARGET_WALLETS ∩ BLACKLISTED_WALLETS` avec `EVICTION_ENABLED=true` = crash boot clair. Vérifier implémentation existante, fix si partial.
  - **Audit M-027** : `list_wallets_to_poll()` blacklist SQL WHERE + Python double-check. Actuel : Python seul.
  - **Audit H-011** : TOCTOU `SentinelFile.touch()` atomique via `os.open(O_CREAT|O_EXCL)`. 10 lignes fix.
  - **Audit H-012** : Migration 0007 data migration wrapped dans transaction. Petit fix sur migration déjà shippée (future regression prevention).
  - **Audit H-010** : documenter crash boot si `workers > 1` dans `RemoteControlOrchestrator.__init__`. Commentaire + assertion.
- **Tests requis** :
  - `test_filter_noisy_endpoints_uses_cached_regex`
  - `test_target_blacklist_cross_field_validator_crashes_boot`
  - `test_list_wallets_to_poll_sql_filter_blacklist`
  - `test_sentinel_touch_atomic`
  - `test_remote_control_refuses_multi_worker`
- **Sources** : Audit M/H level divers.
- **Charge item** : 0.5 jour

## 4. Architecture / décisions clefs

- **D1** : Fix v1 shutdown timeout wrapper avant fix v2 audit boucles. Justification : safe first, investigate ensuite.
- **D2** : documenter `# KNOWN: delay shutdown up to X` si boucle complexe à fix. Justification : honnête > fake fix.
- **D3** : scripts Python utilisent `python3` shebang explicite. Justification : Debian/Ubuntu default, macOS compatible, friendly WSL.
- **D4** : DB schema doc markdown + helpers Python. Justification : markdown readable humain, helpers scriptable programmatique.
- **D5** : Goldsky free tier only, pas Turbo Pipeline payant. Justification : contraintes <$50/mo infra, Turbo Pipeline (~$50/mo) marginalement meilleur que free + query périodique.

## 5. Invariants sécurité

- **Triple garde-fou M3 + 4ᵉ M8** : intact. MI touche uniquement ops + discovery enrichment.
- **Blacklist double-check** : renforcé par MI.6 M-027 (SQL + Python).
- **Sentinel halt.flag permissions 0o600/0o700** : renforcé par MI.6 H-011 (atomic creation).
- **Zéro secret leak** : Goldsky free tier n'utilise pas d'API key (public subgraphs). Si key required future tier, discipline identique `TELEGRAM_BOT_TOKEN`.
- **Read-only Data API + Gamma + Goldsky** : cohérent M5 invariant. Pas de nouvelle surface écriture.

## 6. Hypothèses empiriques à valider AVANT ship

Aucune hypothèse critique — MI est ops + enrichment déterministe. Validation post-ship :
- **Shutdown time** : `time systemctl stop polycopy.service` < 15s sur 10 essais consécutifs (MI.1 + MI.2 validé).
- **Setup fresh** : clone repo sur Debian vierge → `bash scripts/setup.sh` → `python3 scripts/night_test_status.py --full` fonctionne sans friction (MI.3 validé).
- **Goldsky enrichment** : post-ship, pool candidats discovery s'enrichit de ≥10 wallets non couverts par `/holders` seul sur 24h.

## 7. Out of scope

- **Goldsky Turbo Pipeline payant** : Claude §7.1 (b) documente mais coûteux. Hors scope MI, future spec si besoin latence ou coverage (après évaluation ROI).
- **Docker / systemd unit refactor** : M12_bis artefacts supervisor systemd suffisants. Hors scope.
- **Multi-worker uvicorn RateLimiter refactor** : Audit H-010 documente que per-process rate limit saute en multi-worker. MI.6 documente juste, pas fix (spec sécurité future si passage multi-worker).
- **Migration Python `/` path `Decimal` complet** : audit M-012 signale Float → Numeric migration. Hors scope MI (trop invasif).
- **DryRunResolutionWatcher retry cap sur neg_risk scalar** : audit M-019. Spec M13 extension, hors scope MI.
- **DST transition edge case DailySummaryScheduler** : audit M-015. Hors scope, future spec.
- **Heartbeat skipped reason critical détail** : audit M-016. Hors scope, spec future.
- **Dashboard URL troncation digest** : audit M-018. Hors scope (cosmétique).

## 8. Success criteria

1. **Tests ciblés verts** : ~15 nouveaux tests unit + 2 integration.
2. **Shutdown < 15s** : 10 essais consécutifs `systemctl stop polycopy.service` mesurés.
3. **Fresh setup sans friction** : sur une Debian/Ubuntu fresh, `git clone + setup.sh + python3 scripts/night_test_status.py --boot` fonctionne en <5 min sans erreur.
4. **DB schema doc complète** : `docs/db_schema.md` couvre les 11 tables + 10 common queries documentées.
5. **Goldsky enrichment** : post-ship, logs `goldsky_enrichment_fetched` montrent ≥10 wallets récupérés par cycle discovery. Pool candidate augmente de ≥5%.
6. **Audit M-level fixes** : 6 items résolus (M-024, M-014, M-027, H-010, H-011, H-012).

## 9. Mapping origines (traçabilité)

| Item | Audit | Session | Deep-search | Synthèse roadmap |
|---|---|---|---|---|
| MI.1 | — | D (D3) | Backlog §"Shutdown lent" | #39 |
| MI.2 | — | D (D3 extension) | Backlog hypothèses détaillées | #39 |
| MI.3 | — | D (D4) | User feedback 2026-04-24 | — |
| MI.4 | — | D (D5 + D6) | User feedback 2026-04-24 | — |
| MI.5 | — | — (new) | F23 (3/3), Perplexity B5 + Gemini + Claude §8.3 | #20 |
| MI.6 | [M-024, M-014, M-027, H-010, H-011, H-012] | — (audit) | — | audit mapping |

## 10. Prompt de génération de spec

````markdown
# Contexte

Lis `docs/next/MI.md` en entier. C'est le brief actionnable du module MI
(Ops hygiene + Goldsky free tier integration). Améliore qualité ops sur 4 axes :
shutdown graceful, scripts cross-platform, DB schema docs, Goldsky enrichment.
Inclut 6 petits fixes audit M/H level bonus.

# Tâche

Produire `docs/specs/M22-ops-hygiene-goldsky.md` suivant strictement le format
des specs M1..M21 existantes.

Numéro : M22 (après séquentiel, ajuster selon ordre ship réel).

# Prérequis (à lire avant de rédiger)

- `CLAUDE.md` §Conventions + §Sécurité (invariants blacklist, sentinel, creds)
- `docs/setup.md` + `docs/backlog.md` §"Shutdown lent"
- `docs/specs/M13_dry_run_observability_spec.md` comme template de forme
- `docs/specs/M5-trader-scoring.md` (goldsky_client.py existant)
- Audit M-014, M-024, M-027, H-010, H-011, H-012
- Synthèse §3.2 F23 Goldsky adoption
- Goldsky [docs.goldsky.com/chains/polymarket](https://docs.goldsky.com/chains/polymarket) + [pricing](https://goldsky.com/pricing)

# Contraintes

- Lecture seule src/ + docs
- Écriture uniquement `docs/specs/M22-ops-hygiene-goldsky.md`
- Longueur cible : 800-1100 lignes
- Migration Alembic : aucune (MI est code ops + enrichment, pas de nouveau schéma)
- Inclure §Commandes vérif : benchmark shutdown time + fresh setup test

# Livrable

- Le fichier `docs/specs/M22-ops-hygiene-goldsky.md` complet
- Un ping final ≤ 10 lignes : tests estimés, charge cumulée, ordre commits
  (recommandé : MI.3 shebangs → MI.4 DB docs → MI.6 audit fixes quick → MI.1
  shutdown v1 → MI.2 shutdown v2 audit → MI.5 Goldsky)
````

## 11. Notes d'implémentation

### Piège : shutdown timeout + TaskGroup

Python 3.11+ `TaskGroup.__aexit__` cancel all tasks on exit. Si wrappé dans `asyncio.wait_for(timeout=10)`, les tasks reçoivent `CancelledError`. Les tasks qui ne handle pas `CancelledError` gracefully peuvent **double-cancel** ou hang. Attention pattern propre :
```python
try:
    await asyncio.wait_for(task_group.__aexit__(None, None, None), timeout=10)
except asyncio.TimeoutError:
    log.error("shutdown_timeout_hit")
    # Let systemd SIGKILL
```

### Piège : Goldsky subgraph versions drift

Perplexity B5 documente que subgraph versions peuvent drift. Pattern déjà en place dans `goldsky_client.py` M5 : hardcoded fallback URL + override env. Vérifier cohérence MI.5.

### Piège : DB schema docs maintenance

`docs/db_schema.md` risque de drift vs schéma actuel. **Mitigation** : test CI `test_db_schema_doc_matches_actual_schema` qui compare dump `.schema` sqlite vs contenu markdown. Si drift, CI fail. User doit update doc.

### Piège : `night_test_status.py` import chain

Script fait `from polycopy.config import Settings` → nécessite `pip install -e`. User se tâche venv inactivé → friendly error message. Pattern :
```python
try:
    from polycopy.config import Settings
except ImportError:
    print("[ERROR] polycopy not installed in current venv.")
    print("[HINT] Activate venv: source .venv/bin/activate")
    print("[HINT] Or run bootstrap: bash scripts/setup.sh")
    sys.exit(1)
```

### Références externes

- **Goldsky docs Polymarket** : [docs.goldsky.com/chains/polymarket](https://docs.goldsky.com/chains/polymarket).
- **Goldsky pricing** : [goldsky.com/pricing](https://goldsky.com/pricing) + [docs.goldsky.com/pricing/summary](https://docs.goldsky.com/pricing/summary).
- **Chainstack comparison** : [chainstack.com/top-5-hosted-subgraph-indexing-platforms-2026](https://chainstack.com/top-5-hosted-subgraph-indexing-platforms-2026/).
- **Backlog §"Shutdown lent"** : [docs/backlog.md:200-242](../../docs/backlog.md#L200).

### Questions ouvertes pertinentes à MI

Aucune question directe. MI est déterministe ops.
