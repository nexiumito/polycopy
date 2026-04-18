# Prompt pour générer la spec M11 — Backtest scheduler + persistence

> Copie le bloc ci-dessous tel quel dans une nouvelle session Claude Code.
> Génère `specs/M11-backtest-scheduler.md` au format détaillé de M3-M8.

---

```
Tu vas écrire la spec d'implémentation `specs/M11-backtest-scheduler.md` pour
le projet polycopy (bot de copy-trading Polymarket en Python 3.11 / asyncio).

Avant tout, lis :
- `CLAUDE.md` (conventions code + sécurité)
- `docs/architecture.md`
- `specs/M5-trader-scoring.md` (M5 discovery + scoring v1, dont le backtest
  valide la formule)
- `specs/M7-telegram-enhanced.md` (pattern scheduler TZ-aware
  `DailySummaryScheduler` à reproduire)
- `specs/M8-dry-run-realistic.md` (format de référence + pattern Alembic
  batch_alter_table)
- `scripts/score_backtest.py` (le code existant à refactor en module)
- `src/polycopy/monitoring/orchestrator.py` (TaskGroup où co-lancer le
  scheduler)
- `src/polycopy/monitoring/daily_summary_scheduler.py` (pattern TZ-aware)
- `src/polycopy/monitoring/daily_summary_queries.py` (à étendre pour inclure
  le dernier backtest dans le résumé quotidien)
- `src/polycopy/storage/models.py` + `repositories.py` (pour la nouvelle
  table)
- `specs/m5_backtest_seed.txt` (seed statique des ~50 wallets)

## Contexte du problème (à formaliser dans la spec)

Aujourd'hui, le backtest M5 vit dans `scripts/score_backtest.py` — un script
sync standalone que l'utilisateur lance manuellement et lit le rapport HTML
généré (`backtest_v1_report.html`). Cible documentée : Spearman ≥ 0.30 sur
~50 wallets seed avant d'activer `DISCOVERY_ENABLED=true` en prod. **Aucune
automatisation, aucune persistance des résultats successifs, aucune
intégration au monitoring**. Quand l'utilisateur a ses 4 wallets auto-promus
par discovery dans le dashboard, il n'a aucun signal automatique sur la
qualité courante de la formule v1.

M11 répond : un scheduler interne qui lance le backtest périodiquement,
persiste les résultats en DB (audit + tendance), alerte Telegram à chaque
run, et inclut le dernier résultat dans le résumé quotidien M7.

**M11 est strictement de l'OBSERVATION, aucune décision opérationnelle**.
La prise de décision (lockout discovery sur fail) est le scope explicite
de M12 — ne PAS l'inclure ici.

## Pourquoi cette cadence et ce périmètre

- **Spearman = corr(score_t, ROI_observé_30j_après_t)** — le numérateur
  (score) est statique tant que la formule v1 ne change pas, le dénominateur
  (ROI 30j) évolue lentement à mesure que des marchés se résolvent. Donc
  un backtest 1×/h serait quasi-identique au précédent → gaspillage CPU
  et appels Goldsky/Data API.
- **Cadence retenue : 1×/jour** (`BACKTEST_INTERVAL_HOURS=24`, default 24,
  bornes `[6, 168]` soit 6 h à 7 jours). À heure fixe TZ-aware (réutilise
  le pattern `tg_daily_summary_*` de M7) — par défaut 03:00 Europe/Paris
  pour ne pas concurrencer le résumé quotidien à 09:00.
- **Seed STATIQUE** : `specs/m5_backtest_seed.txt` reste immuable hors PR
  humain. Aucune auto-extension à partir des wallets découverts (principe
  ML train/test split — la validation doit être indépendante du
  promotionnel). Documenter explicitement.
- **v1 → v2** : le bot ne réécrit jamais sa formule. v2 = nouveau code
  Python commité par un humain, bump `SCORING_VERSION` dans `.env`, M11
  re-scorera tout le seed à la prochaine itération (la formule active est
  celle pointée par `SCORING_VERSION`). M11 doit logger
  `scoring_version` dans chaque run pour audit.

## Objectif M11 (livrable)

1. **Refactor** `scripts/score_backtest.py` :
   - Extraire la logique métier dans
     `src/polycopy/discovery/backtest_runner.py` (callable async réutilisable).
   - Le script `pnl_report.py` sync historique reste opérationnel (CLI
     manuel) en délégant au runner via un wrapper sync (similaire au
     pattern `pnl_report.py`).
2. **Nouveau scheduler** `src/polycopy/discovery/backtest_scheduler.py` :
   - `BacktestScheduler.run(stop_event)` boucle TZ-aware :
     calcule le prochain `next_run_at` à partir de `BACKTEST_HOUR_LOCAL`
     (default 3) + `BACKTEST_TIMEZONE` (default `tg_daily_summary_timezone`
     pour rester cohérent), `asyncio.wait_for(stop_event.wait, delta_s)`,
     puis exécute `_run_once()`.
   - Intervalle minimum : `BACKTEST_INTERVAL_HOURS=24` (skip un run si
     dernier run < BACKTEST_INTERVAL_HOURS — protection contre restart
     boucle).
3. **Persistance** :
   - Nouvelle table `backtest_runs(id, scoring_version, spearman,
     sample_size, low_confidence_count, p_value, ran_at, passed_threshold,
     seed_path, error_msg)`.
   - Migration Alembic 0005 (batch_alter_table SQLite-friendly si besoin,
     mais ici nouvelle table donc create_table simple).
   - Repository `BacktestRunRepository` (insert, latest_for_version,
     list_recent).
4. **Alertes Telegram** :
   - Après chaque run, push une alerte :
     - `backtest_completed` INFO si `passed_threshold=True`
     - `backtest_below_threshold` WARNING si `passed_threshold=False`
     (cooldown 24h pour ne pas spammer si plusieurs runs failed)
   - Body inclut Spearman, sample_size, scoring_version.
5. **Daily summary M7 enrichi** :
   - Nouveau champ `latest_backtest: BacktestSummary | None` dans
     `DailySummaryContext`.
   - Template `daily_summary.md.j2` ajoute une section "Backtest M5" si
     présent (Spearman, version, statut, date du dernier run).
   - Variable optionnelle (None si table vide → no-op section).
6. **Co-lancement** :
   - `BacktestScheduler` lancé conditionnellement dans le TaskGroup de
     `MonitoringOrchestrator` si `DISCOVERY_ENABLED=true`. Indépendant de
     `TELEGRAM_BOT_TOKEN` (les alertes seront no-op silencieuses sans
     token).
   - **Pourquoi MonitoringOrchestrator** : cohérence avec
     `DailySummaryScheduler` (pattern TZ-aware déjà éprouvé).
7. **Aucune décision opérationnelle** :
   - Discovery continue de tourner normalement.
   - Le résultat du backtest est OBSERVÉ, jamais LU par
     `DiscoveryOrchestrator` à M11.
   - M12 fera ce branchement — préserver strictement la séparation.

## Contraintes non négociables

- **Diff additif sur M5** : zéro ligne modifiée dans `scoring.py`,
  `decision_engine.py`, `DiscoveryOrchestrator`. M11 ajoute un module
  parallèle.
- **Aucune dépendance vers M10/M12**. M11 peut shipper avant ou après M10.
- **Seed strictement statique** : `specs/m5_backtest_seed.txt` n'est jamais
  écrit par M11. Le path est lu en config (`BACKTEST_SEED_PATH`, default
  `specs/m5_backtest_seed.txt`).
- **Aucun secret loggé** (POLYMARKET_PRIVATE_KEY, FUNDER, TELEGRAM_BOT_TOKEN,
  GOLDSKY_API_KEY) — vérifié par grep automatisé dans tests.
- **Pas de log d'erreur Goldsky qui leak l'URL avec une éventuelle clé** —
  redact les credentials avant logging.
- **Backtest run est idempotent** : si crash mid-run, prochain cycle retry
  proprement. Pas d'état corrompu.
- **Threshold configurable** : `BACKTEST_SPEARMAN_THRESHOLD=0.30` env
  (default 0.30, bornes `[0.0, 1.0]` via `Field(ge=0.0, le=1.0)`).
  La spec M5 documente 0.30 — défaut cohérent.
- **Cron interne pas concurrent** : si dernier run < `BACKTEST_INTERVAL_HOURS`,
  skip + log debug `backtest_skipped_too_recent`. Évite les doubles runs au
  redémarrage si l'horloge passe l'heure cible 2× dans la fenêtre TZ.
- **TZ-aware, pas naïf UTC** : si on schedule à 03:00 Europe/Paris, le
  premier run après boot doit pointer le prochain 03:00 local correct
  (DST-safe via `zoneinfo`).
- **Conventions CLAUDE.md** : async, type hints stricts, structlog,
  docstrings FR, code/identifiants EN.
- **mypy --strict propre, ruff propre, pytest ≥ 80% coverage** sur
  `src/polycopy/discovery/` (M5 + M11 cumulé).
- **Tests respx** pour mocker Goldsky/Data API si le runner les utilise.
- **Tests scheduler** : faux clock via `monkeypatch` du `_now()`, vérifier
  delta calculation, skip-if-recent, multiple cycles.

## Cas de test obligatoires (à lister §9)

1. Refactor `score_backtest.py` : appel via le runner async produit le
   même Spearman que l'ancien script sync (test de non-régression sur
   fixture).
2. Scheduler : boot à 02:00 → prochain run prévu à 03:00 (Europe/Paris).
3. Scheduler : boot à 03:30 → prochain run prévu à 03:00 le lendemain.
4. Scheduler : skip si dernier run < `BACKTEST_INTERVAL_HOURS`.
5. Run happy path : Spearman ≥ 0.30 → DB row inséré avec
   `passed_threshold=True`, alerte INFO `backtest_completed` poussée.
6. Run fail : Spearman < 0.30 → DB row inséré, alerte WARNING
   `backtest_below_threshold` poussée.
7. Run exception : Goldsky 500 → DB row avec `error_msg` non-NULL et
   `passed_threshold=False`, alerte ERROR.
8. Cooldown alerte : 5 runs failed dans la fenêtre cooldown → 1 seule
   alerte WARNING (déduplication M4 préservée).
9. Daily summary inclut backtest si présent : `latest_backtest` non-None
   → template rend la section. Si table vide → section absente.
10. Migration 0005 : applique sur DB M8 → table `backtest_runs` créée
    avec colonnes attendues + index sur `(scoring_version, ran_at)`.
11. Repository : `latest_for_version('v1')` retourne le plus récent ;
    `list_recent(3)` retourne les 3 derniers ordre desc.
12. Aucun secret dans les logs structlog (grep automatisé).

## Mises à jour de doc

- `README.md` : section M11 "Backtest auto-scheduled" + 4 env vars
  (`BACKTEST_INTERVAL_HOURS`, `BACKTEST_HOUR_LOCAL`,
  `BACKTEST_SPEARMAN_THRESHOLD`, `BACKTEST_SEED_PATH`).
- `docs/architecture.md` : section "Module : Discovery" gagne un §
  "Status M11" décrivant le scheduler + pattern TZ-aware.
- `CLAUDE.md` : section sécurité gagne 1 bullet sur l'absence de fuite de
  creds dans `backtest_runs.error_msg` (redact obligatoire).
- `docs/setup.md` : §19 "Backtest auto-scheduled (M11)" avec exemple
  d'override d'heure + lecture des résultats DB.
- `.env.example` : 4 nouvelles variables avec commentaires.

## Format de la spec

Suis le format exhaustif de `specs/M8-dry-run-realistic.md` :
- §0 Pré-requis
- §1 Objectif scope exact (insister sur "OBSERVATION ONLY, M12 fera la
  décision")
- §2 Arbitrages techniques :
  - 2.1 Cadence 24h vs corrélation avec discovery cycle (rejeté, justifié)
  - 2.2 Seed statique vs auto-extension (rejeté, principe ML train/test)
  - 2.3 Refactor module vs rester script standalone
  - 2.4 Co-lancement Monitoring vs Discovery TaskGroup
  - 2.5 Threshold configurable vs hard-coded 0.30
  - 2.6 Persistance DB vs simple log structuré
  - 2.7 Alerte cooldown 24h vs immédiat
- §3 Arborescence
- §4 APIs (aucune nouvelle, refactor de l'existant)
- §5 Storage (migration Alembic 0005 + table `backtest_runs`)
- §6 DTOs + repositories
- §7 BacktestRunner (refactor de l'existant en async module)
- §8 BacktestScheduler (TZ-aware, pattern `DailySummaryScheduler`)
- §9 Tests détaillés
- §10 Doc updates
- §11 Commandes de vérification finale
- §12 Critères d'acceptation
- §13 Hors scope M11 (notamment : auto-disable discovery = M12,
  auto-rotation des formules, auto-extension du seed, exposition d'une
  page dashboard `/backtest-history` = reportable M11.1)
- §14 Notes d'implémentation + zones d'incertitude :
  - 14.5 doit lister explicitement : "scoring_version v1 → v2 = nouveau
    code humain, M11 ne génère jamais de formule" + "seed = read-only
    pour le bot, croissance via PR"
- §15 Prompt à m'envoyer pour lancer l'implémentation

Cible 1000-1300 lignes. Inclure pseudocode du scheduler et du runner.

À la fin, propose un commit message :
`feat(discovery,monitoring): M11 backtest scheduler + persistence`
```
