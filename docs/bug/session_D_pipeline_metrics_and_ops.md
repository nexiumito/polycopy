# Session D — Pipeline metrics + ops hygiene

**Priorité** : 🟡 #4 (observabilité + confort ops, pas bloquant)
**Charge estimée** : M (1-2 jours)
**Branche suggérée** : `fix/pipeline-metrics-ops`

---

## Objectif business

Observer correctement le pipeline + avoir un shutdown propre + éviter les
mini-frictions setup qui bouffent du temps à chaque pull. Pas business-critical,
mais sans ça on prend de mauvaises décisions sur les perfs et on perd 3 min à
chaque redémarrage.

## Items

### D1 — Split `watcher_detected_ms` en `realtime` + `backfill`

Déjà documenté dans [docs/backlog.md:85-134](../backlog.md). Aujourd'hui le stage
`watcher_detected_ms` sur /latence affiche **p99 = 44 min** à cause de la
conflation entre vraie latence temps-réel et rattrapage du backlog historique
d'un wallet fraîchement promu.

**Fix retenu (option 1 du backlog)** :
- `watcher_realtime_detected_ms` = `now() − trade.timestamp` **uniquement pour les
  trades < 5 min** (zone temps-réel). Filtre en amont de `latency_repo.insert()`.
- `watcher_backfill_duration_ms` = durée totale du cycle `get_trades()` lors
  d'une promotion (mesurée autour du call `_poll_once`). Audit pur, pas comparé
  aux autres stages.

**Fichiers impactés** :
- [src/polycopy/watcher/wallet_poller.py](../../src/polycopy/watcher/wallet_poller.py#L94)
- [src/polycopy/storage/repositories.py](../../src/polycopy/storage/repositories.py) (LatencyRepository)
- [src/polycopy/dashboard/queries.py](../../src/polycopy/dashboard/queries.py) (latence route)
- [src/polycopy/dashboard/templates/latency.html](../../src/polycopy/dashboard/templates/)
- Migration Alembic `0008_split_watcher_latency` (ajout enum stage name).

### D2 — Fix `filtered > enriched` counter bug

Sur /latence on observe `strategy_filtered_ms count = 27493` > `strategy_enriched_ms
count = 27418` (théoriquement impossible — chaque trade passe enriched → filtered).

Soit :
- Bug de comptage (un stage posté plusieurs fois ?)
- Ordering race condition (enriched perd un sample)
- Stage `enriched` skippé sur certains chemins (ex : trades qui entrent direct
  dans filtered après un recovery)

**Action** : audit [src/polycopy/strategy/pipeline.py](../../src/polycopy/strategy/pipeline.py)
+ confirmer que chaque stage est appelé exactement une fois par trade, et
ajouter un invariant test `test_pipeline_stage_counts_monotonic_decreasing`.

### D3 — Shutdown graceful (1-3 min → < 10 s)

Déjà backlog ([docs/backlog.md:200-242](../backlog.md#L200)). `systemctl stop
polycopy` prend 1-3 min et finit souvent sur SIGKILL. Cause probable : un await
long dans WalletPoller / ClobMarketWSClient / DryRunResolutionWatcher ne check
pas `stop_event` entre les retries.

**Fix v1 proposé** : ajouter `shutdown_timeout_seconds = 10` dans
[src/polycopy/cli/runner.py](../../src/polycopy/cli/runner.py), via
`asyncio.wait_for` sur le TaskGroup `__aexit__`. Si timeout, on laisse systemd
SIGKILL proprement.

**Fix v2 plus propre** : audit chaque `while not stop_event` et wrapper les
`await` longs en `asyncio.wait_for(..., timeout=min(X, remaining))`. Plus
chirurgical, pas bloquant pour v1.

### D4 — Setup script rejouable après pull

Constat 2026-04-24 : sur Debian, `python scripts/night_test_status.py` échoue
avec `python: command not found` (Debian a `python3` pas `python`). Et aussi
`[FATAL] polycopy not installed` même après `bash scripts/setup.sh` —
probablement parce que le shebang du script fait `#!/usr/bin/env python`
(devrait être `python3`).

**Fixes** :
- Shebang `#!/usr/bin/env python3` sur tous les scripts `scripts/*.py`.
- `scripts/setup.sh` : créer un alias `python → python3` dans le venv OU
  documenter explicitement que toutes les commandes doivent utiliser `python3`.
- Mise à jour `docs/setup.md` et `README.md` pour cohérence.

### D5 — Scripts DB helpers documentés

Ajouter un fichier `scripts/db_queries.md` ou `scripts/db_helpers.py` avec les
queries SQL courantes (récupérer adresse complète, inspecter trader_events,
export trader_scores v2 evolution). User a dû les demander ponctuellement —
un fichier de référence évite la réinvention.

Exemples à inclure :
- Lister les wallets ACTIVE + status_changed_at
- Filter `trader_events` par `event_type` et window
- Export `trader_scores` evolution pour un wallet donné
- Compter les `gate_rejected` par raison
- Lister les positions virtuelles orphelines (closed_at IS NULL AND created_at < X)

### D6 — Noms de colonnes DB documentés

Noté pendant l'audit 2026-04-24 : `status_updated_at` et `created_at` **n'existent
pas** tels quels dans les tables `target_traders` / `trader_events`. Les noms
réels sont `status_changed_at` (à vérifier) et `event_at` / `cycle_at`. Un
mini-doc `docs/db_schema.md` ou un commentaire dans
[src/polycopy/storage/models.py](../../src/polycopy/storage/models.py) facilite
les queries ad-hoc.

Alternativement : générer automatiquement via `alembic` ou via
`sqlite3 polycopy.db ".schema"` + export périodique.

### D7 — Improve `night_test_status.py` robustesse

Le script crash sur Debian (`python: not found`, puis `[FATAL] polycopy not
installed`). Faire en sorte qu'il :
- Utilise explicitement `python3` (shebang correct).
- Détecte sa propre installation et suggère `pip install -e '.[dev]'` si absent.
- Affiche un résumé structuré qui ne dépend pas de `polycopy` package pour les
  queries pure-SQL (au cas où).

## Hypothèses à valider

- **H1** : `asyncio.wait_for(TaskGroup.__aexit__, timeout=10)` fonctionne — vérifier
  que TaskGroup propage correctement la cancellation sur timeout.
- **H2** : le bug `filtered > enriched` n'est pas dû à une race condition mais à
  un bug de path (enriched skippé sur SELL post-fix Bug 5 ?).

## Livrables

- Migration Alembic 0008 pour split latency stages
- Mise à jour `latency.html` template
- Fix shutdown timeout dans `cli/runner.py`
- Fix shebangs tous les scripts
- Nouveau `docs/db_schema.md` ou `scripts/db_queries.md`
- ~6-10 tests unit (split latency, pipeline monotonic, shutdown timeout)
- Mise à jour CLAUDE.md §Latency (nouveaux stages) + §Ops (shutdown)

## Out of scope

- Pas d'optimisation de la latence réseau (Goldsky subgraph, Polygon logs) —
  backlog, dépend de résultats test M13.
- Pas de refacto `ClobMarketWSClient` reconnect logic (stable M11).
- Pas de Docker/k8s / supervisor refactor — systemd unit M12_bis suffit.

## Success criteria

1. `systemctl stop polycopy` complète en < 15 s dans 95 % des cas (mesurable avec
   un script de benchmark restart).
2. /latence affiche 2 stages distincts `watcher_realtime_detected_ms` (attendu
   p50 < 5 s) et `watcher_backfill_duration_ms` (attendu variable selon wallet).
3. `strategy_filtered_ms.count ≤ strategy_enriched_ms.count` toujours.
4. `python3 scripts/night_test_status.py --full` fonctionne out-of-the-box sur
   un checkout fresh post-setup.sh.
