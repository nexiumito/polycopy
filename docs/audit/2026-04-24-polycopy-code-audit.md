# Audit code polycopy — 2026-04-24

## 0. Executive summary

**~70 findings** dédupliqués sur un codebase de 19 k LOC Python (src) + 24 k LOC tests, 7 migrations Alembic, 34 endpoints HTTP. Audit diagnostic read-only de 6 agents en parallèle sur toutes les couches + 1 passe horizontale transverse.

**Verdict général** : codebase **solide et bien structuré** (isolation des couches, triple garde-fou M3 intact, zéro secret leaké dans les templates), mais **dette accumulée sur la cohérence cross-couches**. Les 4 sessions de bugs (A/B/C/D) identifiées en amont sont **confirmées** par l'audit (chaque item a été retrouvé dans le code). L'audit révèle **surtout** que le scoring v2 et le couplage dry-run/live souffrent de défauts structurels **au-delà** des items déjà listés.

**Top-3 findings CRITIQUES** :
1. **[C-001] PositionSizer + RiskManager ne filtrent pas `simulated`** → un flip `dry_run → live` bloque silencieusement tous les BUY live tant que des positions virtuelles traînent ouvertes (cross-couche strategy/storage).
2. **[C-002] Kill switch digéré / retardé par `AlertDigestWindow`** → l'alerte CRITICAL `kill_switch_triggered` passe dans la même pile de digest que les alertes routine ; si d'autres CRITICAL arrivent dans la fenêtre 5 min, le `stop_event.set()` est retardé. Le bot continue à trader pendant la fenêtre.
3. **[C-003] Drawdown baseline mélange modes** → `get_max_total_usdc(only_real=execution_mode=="live")` mélange `SIMULATION + DRY_RUN` dans la même colonne `is_dry_run`. Un flip de mode (ou un simple run antérieur avec capital différent) peut déclencher un **faux-positif kill switch immédiat**, ou masquer un vrai drawdown.

**Tendance générale** : code de qualité, mais le projet manque d'un **garant horizontal** — 3 fonctions calculent le PnL total avec des capitals initiaux dry-run différents et des filtres `simulated` différents. Le scoring v2 empile 5 bugs indépendants (winsorisation dynamique, `risk_adjusted` Sortino qui favorise les zombies, `timing_alpha` placeholder qui colle +0.10 à tout le monde, compute Brier qui utilise le prix du side acheté au lieu du prix YES, `zombie_ratio` filtre temporel non implémenté).

**Top-3 recommandations d'action immédiate** (avant deep-search) :
- Fixer les 3 findings CRITICAL ci-dessus (patch ciblé, <1 j chacun).
- Unifier `dry_run_virtual_capital_usd` et `dry_run_initial_capital_usd` en **un seul** setting (cf. [H-004]).
- Nettoyer le scoring v2 en **désactivant la pondération `timing_alpha=0.20`** tant que le placeholder est en place, redistribuer (cf. [H-008]).

Après patch des CRITICAL + HIGH, le deep-search multi-LLM devrait se concentrer sur **strategic concerns** (formules scoring, pondérations) et **logique métier** plutôt que sur l'implémentation elle-même.

---

## 1. Target summary

- **Commit audité** : `cfef37d` (HEAD, branche `main`)
- **LOC src** : 19103 lignes (Python)
- **LOC tests** : 23769 lignes sur 143 fichiers
- **Migrations Alembic** : 7 (0001 baseline M3 → 0007 M5_bis eviction)
- **Endpoints dashboard** : 29 routes GET-only + `/healthz`
- **Endpoints remote-control** : 2 GET + 3 POST (machine-scoped, TOTP-protected)
- **Modules couverts** : watcher, strategy, executor, monitoring, dashboard, discovery (+ eviction + scoring v1/v2), storage, cli, remote_control, config, tests, docs/specs
- **Modules hors scope** : tests runtime (pas d'exécution pytest), Tailscale runtime (hors machine Tailscale), libs externes (py-clob-client, httpx — trust+pin)
- **Dépenses parallèles** : 6 agents exploration (~15 min chacun), ~98 findings bruts, dédupliqués à ~70

---

## 2. Tech stack inventory

- **Python** 3.11+, asyncio, Pydantic v2 (Settings + DTOs), SQLAlchemy 2.0 async, structlog, FastAPI
- **DB** : SQLite + Alembic
- **Libs** : py-clob-client (trading), httpx (HTTP), websockets + tenacity (CLOB WS), Jinja2 (templates), pyotp (TOTP), rich (CLI)
- **Frontend** : Tailwind CDN + HTMX + Chart.js + Lucide + Inter (zéro build step)
- **Telegram** : bot emitter-only, `MarkdownV2`, templates Jinja surchargeables
- **Infra** : supervisor systemd + Tailscale (M12_bis remote control)
- **Architecture** : 9 orchestrators asyncio pilotés par `cli/runner.py` en TaskGroup — Watcher, Strategy, Executor, Monitoring, Dashboard, Discovery, LatencyPurge, DryRunResolution, RemoteControl. Communication via DB + asyncio.Queue.
- **Tables DB (11)** : `target_traders`, `detected_trades`, `my_orders`, `my_positions`, `pnl_snapshots`, `trader_scores`, `trader_events`, `strategy_decisions`, `trade_latency_samples`, `trader_daily_pnl`, + `alembic_version`

---

## 3. Findings par sévérité

Format : `[SEV-NNN] Titre — Location (liens cliquables) — Impact — Fix 1-2 phrases — Session ref`.

Les findings marqués `cf. session X` **complètent** un item déjà identifié (nuance ou cas edge nouveau). Les findings marqués `NEW` **n'étaient pas** dans les 4 sessions.

---

### 🔴 CRITICAL — action immédiate

#### [C-001] PositionSizer et RiskManager ne filtrent pas `MyPosition.simulated` — mode flip dry_run→live bloqué
- **Location** : [src/polycopy/strategy/pipeline.py:177-210](src/polycopy/strategy/pipeline.py#L177-L210) + [src/polycopy/strategy/pipeline.py:263-274](src/polycopy/strategy/pipeline.py#L263-L274)
- **Description** : `_check_buy` / `_check_sell` et `RiskManager.check` requêtent `MyPosition WHERE closed_at IS NULL` **sans filtrer `simulated`**. Conséquence : en LIVE, des positions virtuelles héritées du dry-run bloquent les BUY réels (`position_already_open`) et polluent le calcul d'exposition.
- **Impact** : un utilisateur qui teste en dry-run puis flip LIVE voit **zéro BUY passer** jusqu'à nettoyer la DB manuellement. Inversement, un run live historique pollue les décisions dry-run.
- **Reproduction** : `EXECUTION_MODE=dry_run` → 3 fills virtuels → stop → `EXECUTION_MODE=live` → aucun BUY ne passe `PositionSizer`.
- **Fix suggéré** : ajouter `MyPosition.simulated == (settings.execution_mode != "live")` aux 3 queries. Pattern déjà utilisé par `repositories.py::upsert_on_fill` vs `upsert_virtual` — à propager.
- **Session ref** : NEW

#### [C-002] Kill switch CRITICAL digéré/retardé par `AlertDigestWindow`
- **Location** : [src/polycopy/monitoring/alert_dispatcher.py:117-145](src/polycopy/monitoring/alert_dispatcher.py#L117-L145) + [src/polycopy/monitoring/alert_digest.py:44-63](src/polycopy/monitoring/alert_digest.py#L44-L63)
- **Description** : `PnlSnapshotWriter` pousse `Alert(event="kill_switch_triggered", level="CRITICAL", cooldown_key="kill_switch")`. Le dispatcher passe **toutes** les alertes par `self._digest.register(alert, now)`, **sans exception pour CRITICAL**. Si la fenêtre digest (5 min) contient déjà ≥ `threshold-1` kill_switch, le message est batché — `stop_event.set()` ne déclenche pas immédiatement dans le flow alerte.
- **Impact** : pendant 5 min (fenêtre digest), le bot continue à poster des ordres alors que le drawdown kill-switch est atteint. Perte capital potentielle.
- **Reproduction** : mocker `AlertDigestWindow.register` pour retourner `action="buffer"` sur un kill_switch → vérifier que `stop_event.set()` n'a pas été invoqué dans la même tick.
- **Fix suggéré** : bypass `self._digest.register()` si `alert.level == "CRITICAL"` → émission immédiate + écriture sentinel `halt.flag` AVANT toute alerte (ordre strict).
- **Session ref** : NEW

#### [C-003] Drawdown baseline mélange SIMULATION + DRY_RUN dans le même bucket `is_dry_run`
- **Location** : [src/polycopy/monitoring/pnl_writer.py:86-98](src/polycopy/monitoring/pnl_writer.py#L86-L98) + [src/polycopy/storage/repositories.py:809-817](src/polycopy/storage/repositories.py#L809-L817)
- **Description** : `only_real = execution_mode == "live"` → en DRY_RUN ou SIMULATION, `only_real=False` et `get_max_total_usdc(only_real=False)` renvoie **TOUS** les snapshots non-live. Si l'utilisateur a tourné SIMULATION avec virtual_capital=$50k, puis DRY_RUN avec $10k, le premier tick DRY_RUN calcule `drawdown = (10000-50000)/50000 = 80%` → kill switch immédiat faux-positif.
- **Impact** : faux-positif KILL sur flip de mode / changement de capital virtuel. Inversement, faux-négatif si le max historique inclut un SIMULATION avec capital inférieur.
- **Reproduction** : séquence live→dry_run avec capitals différents dans la même DB.
- **Fix suggéré** : ajouter colonne `execution_mode` à `PnlSnapshot` (migration 0008), ou bucketiser `get_max_total_usdc` par `(is_dry_run, execution_mode)`.
- **Session ref** : NEW (contredit l'intent M10 parité dry-run/live)

#### [C-004] `VirtualWalletStateReader` skip silencieusement les positions dont le mid est None → sous-évaluation du capital
- **Location** : [src/polycopy/executor/virtual_wallet_reader.py:48-71](src/polycopy/executor/virtual_wallet_reader.py#L48-L71)
- **Description** : si WS+HTTP midpoint retournent None (panne transitoire), la position est `continue`-skipped, pas comptée ni dans `exposure` ni dans `unrealized`. Le `total_usdc` retourné est artificiellement bas → le max historique baisse → le prochain tick avec midpoint OK remonte → **drawdown factice** vs ce min creux.
- **Impact** : inverse de [C-003] : kill switch ne déclenche **pas** alors qu'il devrait, OU déclenche à tort après une panne midpoint.
- **Reproduction** : simuler `ClobMidpointClient.get` retournant None pendant 2 snapshots → total_usdc oscille artificiellement.
- **Fix suggéré** : conserver le `last_known_mid` par asset_id, OU lever une exception si ≥1 position a un mid None (retry snapshot) plutôt que de produire un `total_usdc` corrompu.
- **Session ref** : NEW

#### [C-005] Divergence realized_pnl entre `/home` et `/performance` — deux agrégations sur mêmes données
- **Location** : [src/polycopy/dashboard/queries.py:757-824](src/polycopy/dashboard/queries.py#L757-L824) vs [src/polycopy/dashboard/queries.py:1690-1870](src/polycopy/dashboard/queries.py#L1690-L1870)
- **Description** : `get_home_alltime_stats` calcule `live_pnl` via `Σ(SELL×price) - Σ(BUY×price)` inline par position (lignes 815-817). `list_trader_performance` calcule `stats["sell_recovery"] - stats["buy_cost"]` après agrégation par trader. Ordre d'évaluation ≠ → si un fill arrive entre les deux appels, les totaux divergent. Pire : les deux sources de vérité ne s'accordent pas avec `PnlSnapshotWriter` qui hardcode `realized_pnl = 0.0` dans le DTO persisté (cf. [H-002]).
- **Impact** : home affiche X $, performance affiche Y $, kill switch raisonne sur Z $. Triple vérité incohérente.
- **Reproduction** : afficher /home puis /performance en rapide succession pendant qu'un fill est processé.
- **Fix suggéré** : extraire un helper central `aggregate_realized_pnl(mode, wallets=None)` utilisé par `/home`, `/performance`, `/activity`. Peupler `realized_pnl` et `unrealized_pnl` dans `PnlSnapshotDTO` au lieu de les hardcoder à 0.
- **Session ref** : NEW (aggrave session C item C4)

---

### 🟠 HIGH — blocquant avant passage live

#### [H-001] `strategy_filtered_ms` toujours émis même sur early-reject → `count(filtered) > count(enriched)` légal
- **Location** : [src/polycopy/strategy/orchestrator.py:128-153](src/polycopy/strategy/orchestrator.py#L128-L153) + [src/polycopy/strategy/pipeline.py:280-335](src/polycopy/strategy/pipeline.py#L280-L335)
- **Description** : `strategy_filtered_ms` est wrappé **autour** de `run_pipeline` en `finally` → émis même si reject précoce par `TraderLifecycleFilter` (sell_only/blacklisted) ou `EntryPriceFilter`. En revanche, `strategy_enriched_ms` (stage `MarketFilter`) n'est émis que si la boucle atteint le 2e filtre. Avec `EVICTION_ENABLED=true`, **toutes** les détections d'un sell_only incrementent filtered sans enriched → inversion observée sur `/latency` par l'utilisateur.
- **Impact** : dashboard latence affiche des ratios faux. Les p50/p95 de `filtered` sont gonflés par les early-rejects.
- **Fix suggéré** : ne pas émettre `filtered` si la boucle s'arrête avant `MarketFilter`. OU renommer `filtered` en `pipeline_total` et documenter que ce n'est pas un stage ordonné.
- **Session ref** : D (item D2 — cause racine confirmée)

#### [H-002] `PnlSnapshotDTO` hardcode `realized_pnl=0.0, unrealized_pnl=0.0` — ne sert à rien, source de bug cross-source
- **Location** : [src/polycopy/monitoring/pnl_writer.py:90-98](src/polycopy/monitoring/pnl_writer.py#L90-L98)
- **Description** : les colonnes DB `realized_pnl` et `unrealized_pnl` de `pnl_snapshots` sont peuplées avec 0.0 hardcodé. Seul `total_usdc` reflète la vérité. Toute query dashboard ou monitoring qui consomme ces colonnes voit des 0.
- **Impact** : surface de bug implicite — milestones PnL, sparklines historiques, debug via SQL raw → données factices.
- **Fix suggéré** : peupler les deux colonnes avec les valeurs calculées par `WalletStateReader.get_state()`. Au passage, consommer ces colonnes depuis `get_home_alltime_stats` (au lieu de recalculer).
- **Session ref** : NEW

#### [H-003] Trade body `alert.body` injecté sans `telegram_md_escape` dans `fallback.md.j2`
- **Location** : [src/polycopy/monitoring/alert_renderer.py:112-131](src/polycopy/monitoring/alert_renderer.py) + `templates/fallback.md.j2`
- **Description** : les templates dédiés échappent correctement, mais `fallback.md.j2` (utilisé pour tout event_type non documenté) injecte `{{ body }}` sans filtre `telegram_md_escape`. Si un `body` contient un `*`, `_`, `[`, `]`, `` ` ``, Telegram renvoie 400 Bad Request → alerte perdue.
- **Impact** : alertes routines passent (templates dédiés), mais alertes exotiques (nouveaux event_type en développement) peuvent être silencieusement drop.
- **Fix suggéré** : `{{ body | telegram_md_escape }}` dans `fallback.md.j2` + test de non-régression grep sur templates.
- **Session ref** : NEW

#### [H-004] Deux capitaux initiaux dry-run distincts : `dry_run_virtual_capital_usd` vs `dry_run_initial_capital_usd`
- **Location** : [src/polycopy/config.py:315](src/polycopy/config.py#L315) + [src/polycopy/config.py:359](src/polycopy/config.py#L359) + [src/polycopy/executor/virtual_wallet_reader.py:62](src/polycopy/executor/virtual_wallet_reader.py#L62) + [src/polycopy/dashboard/queries.py:965](src/polycopy/dashboard/queries.py#L965)
- **Description** : `VirtualWalletStateReader` lit `dry_run_virtual_capital_usd` pour calculer `total_usdc`. Dashboard `get_home_alltime_stats` lit `dry_run_initial_capital_usd` pour calculer le `open_latent_pnl_usd`. Pas de cross-validation. Deux settings peuvent diverger (et historiquement c'est ce qui arrive après refactor).
- **Impact** : home affiche un latent PnL faux si les deux settings divergent.
- **Fix suggéré** : fusionner en un seul setting, dépréquer l'autre avec warning 1 version.
- **Session ref** : NEW

#### [H-005] Kill switch jamais écrit dans `trader_events` → milestone `/pnl` vide
- **Location** : [src/polycopy/monitoring/pnl_writer.py:127-155](src/polycopy/monitoring/pnl_writer.py#L127-L155) + [src/polycopy/dashboard/queries.py:1119-1133](src/polycopy/dashboard/queries.py#L1119-L1133)
- **Description** : `_maybe_trigger_alerts` push Telegram + `stop_event.set()` + sentinel, mais n'écrit **jamais** dans `trader_events`. Or `get_pnl_milestones` query `TraderEvent WHERE event_type = "kill_switch"` pour l'afficher sur `/pnl` → milestone toujours vide.
- **Impact** : post-mortem impossible depuis le dashboard. Faux contrat UI.
- **Fix suggéré** : écrire un `TraderEvent(event_type="kill_switch", wallet_address=None, event_metadata={...})` en même temps que l'alerte. Migrer `trader_events.wallet_address` en nullable ou créer une table `system_events` dédiée.
- **Session ref** : NEW

#### [H-006] `pilot_score` fallback silencieux v1 quand v2 indisponible au boot
- **Location** : [src/polycopy/discovery/orchestrator.py:335-403](src/polycopy/discovery/orchestrator.py#L335-L403)
- **Description** : si `SCORING_VERSION=v2` et `SCORING_V2_SHADOW_DAYS=0` au boot (aucun `trader_scores` v2 encore), `compute_v2=False` → `score_v2_value=None` → `pilot_score = score_v1_value`. Le DecisionEngine pilote en v1 sans log WARNING ni alerte.
- **Impact** : l'utilisateur croit avoir activé v2 mais v1 pilote. Peut durer tant que la shadow period n'a pas matérialisé la couverture.
- **Fix suggéré** : log WARNING `pilot_score_fallback_v1` au premier fallback + alerte INFO Telegram `scoring_pilot_fallback`. Idéalement, crash boot si `SCORING_VERSION=v2` ET couverture nulle (garde-fou).
- **Session ref** : B (étend B1, nouvelle facette)

#### [H-007] State machine eviction utilise les scores **stale** (DB) au lieu des scores refreshed
- **Location** : [src/polycopy/discovery/eviction/state_machine.py:79-166](src/polycopy/discovery/eviction/state_machine.py#L79-L166) vs [src/polycopy/discovery/eviction/scheduler.py:206-216](src/polycopy/discovery/eviction/scheduler.py#L206-L216)
- **Description** : `classify_sell_only_transitions` évalue T6 abort sur `self_score = scores.get(wallet, sell_only.score or 0.0)` (fresh) mais `_delta_vs_worst` consomme `active_non_pinned` avec `t.score` de la DB (stale — cycle précédent). Les décisions T6 mélangent fresh+stale.
- **Impact** : l'hystérésis abort peut se déclencher ou s'abstenir à tort → rotation eviction erratique.
- **Fix suggéré** : refresher les scores des snapshots (miroir `_classify_cascade`) avant `classify_sell_only_transitions`.
- **Session ref** : NEW

#### [H-008] `timing_alpha=0.5` placeholder uniforme → pool normalization renvoie 0.5 pour tous → +0.10 gratuit sur chaque score v2
- **Location** : [src/polycopy/discovery/scoring/v2/factors/timing_alpha.py:25-31](src/polycopy/discovery/scoring/v2/factors/timing_alpha.py#L25-L31) + [src/polycopy/discovery/metrics_collector_v2.py:49](src/polycopy/discovery/metrics_collector_v2.py#L49)
- **Description** : tous les wallets ont `timing_alpha_weighted=0.5` (constante documentée décision D3 M12). Donc `p5==p95==0.5` → pool normalisation sentinel 0.5. Le facteur `timing_alpha` (poids 0.20) contribue `0.20 × 0.5 = 0.10` **uniforme** à TOUS les scores. Ne discrimine rien mais colle +0.10 au score final.
- **Impact** : le seuil `SCORING_PROMOTION_THRESHOLD=0.65` est effectivement `~0.55` sur les 5 autres facteurs utiles. Tous les gates de décision sont décalés de +0.10.
- **Fix suggéré** : dropper temporairement `timing_alpha` de la pondération (0.20 → 0) et redistribuer sur les 5 autres facteurs, OU retourner 0.0 dans le pool pour qu'il s'annule au lieu de s'additionner.
- **Session ref** : B (nouveau — effet secondaire de la décision D3 pas anticipé)

#### [H-009] `risk_adjusted` Sortino sentinel 3.0 sur curve quasi-plate → wallets zombies/inactifs dominent
- **Location** : [src/polycopy/discovery/scoring/v2/factors/risk_adjusted.py:79-88](src/polycopy/discovery/scoring/v2/factors/risk_adjusted.py#L79-L88)
- **Description** : wallet avec equity curve stable (14 points identiques) → returns tous ≈0 → `downside=[]` → Sortino retourne `_RATIO_CAP_SENTINEL=3.0`. Calmar idem (max_dd<1e-4). Résultat : `risk_adjusted = 0.6×3 + 0.4×3 = 3.0` → top pool.
- **Impact** : wallets qui ne tradent plus ("holders") dominent le facteur `risk_adjusted` (poids 0.25) alors qu'ils ne génèrent aucun alpha. Gate `zombie_ratio<0.40` concerne les positions, pas l'equity curve globale → protection incomplète.
- **Fix suggéré** : exiger variance minimale `pstdev(returns) > 1e-3` avant sentinel, sinon renvoyer 0.0.
- **Session ref** : B (nouveau — cause structurelle de la variance v2 décrite dans B1)

#### [H-010] `RateLimiter` + `AutoLockdown` per-process → multi-worker uvicorn bypass possible
- **Location** : [src/polycopy/remote_control/auth.py:75-171](src/polycopy/remote_control/auth.py#L75-L171)
- **Description** : `RateLimiter._history` et `AutoLockdown._failures` sont des dicts in-memory par process. Si uvicorn tourne `--workers N`, un attaquant a `5 × N` tentatives au lieu de 5.
- **Impact** : brute-force TOTP réaliste avec plusieurs workers. Invariant "5 strikes → lockdown" non préservé.
- **Fix suggéré** : documenter crash boot si `workers > 1` OU migrer vers Redis-backed (hors scope M12_bis). Minimum : commentaire explicite + assertion dans `RemoteControlOrchestrator.__init__`.
- **Session ref** : NEW

#### [H-011] TOCTOU entre `mkdir`/`chmod`/`write_text` dans `SentinelFile.touch()`
- **Location** : [src/polycopy/remote_control/sentinel.py:54-66](src/polycopy/remote_control/sentinel.py#L54-L66)
- **Description** : séquence non-atomique mkdir(0o700)→chmod→write_text→chmod(0o600). Sur FS partagé (NFS), race possible entre mkdir et chmod. Symlink swap peut rediriger `halt.flag`.
- **Impact** : exploit théorique (requiert accès local partagé). Kill switch contournable.
- **Fix suggéré** : `os.open(path, O_WRONLY|O_CREAT|O_EXCL, 0o600)` atomique.
- **Session ref** : NEW

#### [H-012] Migration 0007 data migration non-transactionnelle
- **Location** : [alembic/versions/0007_m5_bis_eviction.py:90-98](alembic/versions/0007_m5_bis_eviction.py#L90-L98)
- **Description** : `UPDATE target_traders SET status='shadow' WHERE status='paused'` via `op.execute()` sans `BEGIN/COMMIT` explicite. Si le process crashe mid-migration, re-run doit être idempotent — l'est sur SQLite single-writer, mais la spec ne garantit pas les fault-tolerance cases (FS partagé, stall IO).
- **Impact** : edge case rare. Restart pendant migration peut laisser des wallets en `paused` (status legacy) cohabitant avec des `shadow`.
- **Fix suggéré** : wrapper dans `connection.begin():` ou utiliser un context Alembic transactionnel.
- **Session ref** : NEW

#### [H-013] `EntryPriceFilter` et `PositionSizer` non couverts par tests SELL
- **Location** : [tests/unit/test_strategy_pipeline.py](tests/unit/test_strategy_pipeline.py) — sections BUY/SELL
- **Description** : post-fix M13 Bug 5 (side-awareness), `PositionSizer._check_sell` et `EntryPriceFilter` (SELL passthrough) ne sont couverts que par des tests BUY + 1 test SELL par filtre. Aucun test explicite "SELL avec price=1.5 passe" ou "SELL sans position retourne `sell_without_position` reason" à distinguer visuellement de `size_zero`.
- **Impact** : refactor futur peut réintroduire le bug original sans échec de test.
- **Fix suggéré** : ajouter `test_entry_price_filter_sell_always_passes_regardless_of_price` et `test_position_sizer_sell_without_position_returns_specific_reason_code`.
- **Session ref** : NEW (complète M13)

#### [H-014] `_compute_zombie_ratio` — filtre temporel <30j **documenté mais pas implémenté**
- **Location** : [src/polycopy/discovery/metrics_collector_v2.py:173-197](src/polycopy/discovery/metrics_collector_v2.py#L173-L197)
- **Description** : docstring "Excluded du dénominateur : positions ouvertes depuis < 30 j". Code : aucun filtre temporel (commentaire admet l'impossibilité — pas de `opened_at` sur `RawPosition`).
- **Impact** : wallets récents injustement pénalisés sur `zombie_ratio`. Gate `zombie_ratio<0.40` rejette des wallets jeunes qui ne sont pas zombies.
- **Fix suggéré** : enrichir `RawPosition` avec `opened_at` (via Data API `/positions`), OU aligner la docstring avec le comportement réel (admettre l'approximation).
- **Session ref** : B (nouveau — cause racine de "couverture v2 faible")

#### [H-015] LRU eviction race dans `ClobMarketWSClient`
- **Location** : [src/polycopy/strategy/clob_ws_client.py:457-464](src/polycopy/strategy/clob_ws_client.py#L457-L464)
- **Description** : `_evict_lru_if_needed` fait `pop(oldest)` + `await _send_unsubscribe(...)`. Entre le pop et l'await, `_listen_loop` peut re-insert le même token (WS message arrive). Duplicates silencieux.
- **Impact** : memory leak lent. Duplicates de souscriptions. Watch_WS status peut basculer en "down" à tort.
- **Fix suggéré** : lock autour de l'eviction, OU marker "evicting" sentinel dans `_subscribed`.
- **Session ref** : NEW

#### [H-016] Divergence M5_bis events ignorés par `daily_summary`, `heartbeat`, Home KPI
- **Location** : [src/polycopy/monitoring/daily_summary_queries.py:255-262](src/polycopy/monitoring/daily_summary_queries.py#L255-L262) + [src/polycopy/dashboard/queries.py:318-325](src/polycopy/dashboard/queries.py#L318-L325) + [src/polycopy/monitoring/heartbeat_scheduler.py:114-119](src/polycopy/monitoring/heartbeat_scheduler.py#L114-L119)
- **Description** : `daily_summary` compte `promoted_active + promoted` et `demoted_paused + demoted`. Rate les 6 nouveaux event_types M5_bis : `promoted_active_via_eviction`, `promoted_active_via_rebound`, `demoted_to_sell_only`, `eviction_completed_to_shadow`, `blacklisted`, `blacklist_removed`. Idem Home KPIs et heartbeat (ne compte pas `sell_only` dans `_count_active_wallets`).
- **Impact** : Telegram daily recap sous-compte les rotations ; heartbeat affiche un watcher_count menteur.
- **Fix suggéré** : centraliser `PROMOTION_EVENT_TYPES` et `DEMOTION_EVENT_TYPES` dans `storage/dtos.py`, consommer partout. Ajouter `sell_only` dans `_count_active_wallets`.
- **Session ref** : NEW

---

### 🟡 MEDIUM — observabilité, qualité, planning

#### [M-001] `_compute_brier` mélange prob(YES) et prob(side-acheté)
- **Location** : [src/polycopy/discovery/metrics_collector_v2.py:165-170](src/polycopy/discovery/metrics_collector_v2.py#L165-L170)
- **Description** : code : `outcome = 1.0 if cash_pnl > 0 else 0.0` et `pred = avg_price`. Sémantique incohérente : `pred` est le prix du side acheté (pas prob(YES)). Si on interprète le Brier comme "calibration du side pris", c'est valide. Docstring module dit "outcome ∈ {0, 1} (YES won / NO won)" — contradiction.
- **Fix** : documenter explicitement "Brier of taken side" OU fetcher `resolvedOutcome` pour recalculer prob(YES). (Session ref : NEW)

#### [M-002] `winsorize_p5_p95` méthode "lower" inefficace pour pools <20
- **Location** : [src/polycopy/discovery/scoring/v2/normalization.py:16-33](src/polycopy/discovery/scoring/v2/normalization.py#L16-L33)
- **Description** : `idx_p5 = int(0.05 * n)` → pour n=10, idx=0 = min. Pour n<20, aucune winsorisation effective — les outliers squattent p95. Cohérent avec la variance cycle-to-cycle de session B.
- **Fix** : interpolation linéaire (numpy-style `np.quantile`) ou p10/p90. (Session ref : B B3)

#### [M-003] Raw brier calculé avec baseline=0.25 au pool, baseline vraie utilisée au scoring final
- **Location** : [src/polycopy/discovery/orchestrator.py:651-653](src/polycopy/discovery/orchestrator.py#L651-L653)
- **Description** : le pool `calibration_pool` est construit avec `brier_baseline_pool=0.25` fixe. Le scoring (ligne 317) utilise la moyenne pool réelle. Les p5/p95 du pool sont donc "décalés" par rapport à l'échelle du wallet_value. Biais systématique sur calibration. (Session ref : B connexe)

#### [M-004] `MyPositionRepository` upsert non-documenté sur conflit unique `(condition_id, asset_id, simulated)`
- **Location** : [src/polycopy/storage/models.py:247-255](src/polycopy/storage/models.py#L247-L255) + [src/polycopy/storage/repositories.py](src/polycopy/storage/repositories.py)
- **Description** : contrainte unique composite, mais aucun `upsert()` explicite dans les repos. Si deux coroutines créent concurrement sur même clé, `IntegrityError` raw. (Session ref : NEW)

#### [M-005] Contextvar `trade_id` non re-bindé dans pipeline → logs stage_complete sans trade_id
- **Location** : [src/polycopy/watcher/wallet_poller.py:99](src/polycopy/watcher/wallet_poller.py#L99)
- **Description** : le bind contextvar est fait dans `_poll_once` log `trade_detected` mais le contexte est perdu quand le DTO atterrit dans `StrategyOrchestrator` (autre task). Les logs `stage_complete` n'ont plus le `trade_id` (toujours persisté en DB par ailleurs). Observabilité dégradée pour debug.
- **Fix** : re-bind dans `StrategyOrchestrator._handle_trade`. (Session ref : NEW)

#### [M-006] Cache Gamma TTL 1 an sur `closed/archived` — pas d'invalidation si re-listing
- **Location** : [src/polycopy/strategy/_cache_policy.py:27-43](src/polycopy/strategy/_cache_policy.py#L27-L43)
- **Description** : `_TTL_RESOLVED_SENTINEL = 31_536_000`. Un marché qui repasse actif (rare mais possible) reste stale 1 an.
- **Fix** : cap à 30 jours OU tracker resolution state séparément. (Session ref : NEW)

#### [M-007] TOCTOU sur caches async (Gamma / CLOB WS / CLOB orderbook)
- **Location** : [gamma_client.py:88-108](src/polycopy/strategy/gamma_client.py#L88-L108), [clob_orderbook_reader.py:58-76](src/polycopy/executor/clob_orderbook_reader.py#L58-L76), [clob_ws_client.py:237-268](src/polycopy/strategy/clob_ws_client.py#L237-L268)
- **Description** : pattern "read cache → await fetch → write cache" sans lock → N coroutines simultanées sur même clé font N fetches redondants.
- **Fix** : single-flight pattern `_inflight: dict[str, asyncio.Future]`. (Session ref : NEW)

#### [M-008] N+1 queries dans `get_home_alltime_stats` — slow sur /home à partir de ~50 positions fermées
- **Location** : [src/polycopy/dashboard/queries.py:803-817](src/polycopy/dashboard/queries.py#L803-L817)
- **Description** : boucle `await session.execute(...)` par position fermée non-simulée.
- **Fix** : `WHERE (condition_id, asset_id) IN (...)` + group en mémoire. (Session ref : NEW)

#### [M-009] Cooldown 60s appliqué aussi aux CRITICAL → `auth fatal` throttled
- **Location** : [src/polycopy/monitoring/alert_dispatcher.py:117-145](src/polycopy/monitoring/alert_dispatcher.py#L117-L145)
- **Description** : `executor_auth_fatal` répété à 30s intervals → 2e alerte drop silencieusement.
- **Fix** : `if alert.level == "CRITICAL": cooldown_seconds = 0`. (Session ref : NEW, voisine de [C-002])

#### [M-010] Win rate : `realized_pnl == 0` ni gagnant ni perdant
- **Location** : [src/polycopy/dashboard/queries.py:949-951](src/polycopy/dashboard/queries.py#L949-L951)
- **Description** : `wins=count(>0)`, `losses=count(<0)`, `decided=wins+losses`. Break-even exclu du dénominateur. 100% WR affiché avec 5 break-even.
- **Fix** : décider convention (gagnant ou neutre) + appliquer uniformément /home et /performance. (Session ref : NEW)

#### [M-011] `Gain max latent` assume YES pour toutes positions
- **Location** : [src/polycopy/dashboard/queries.py:920-924](src/polycopy/dashboard/queries.py#L920-L924)
- **Description** : formule `(1 − avg_price) × size`. Pour NO, upside est `avg_price × size`. Formule invalide pour NO.
- **Fix** : joindre `DetectedTrade.outcome` pour distinguer YES/NO. (Session ref : session C item C5 — confirme bug)

#### [M-012] Float au lieu de Numeric/Decimal pour USDC sur plusieurs tables
- **Location** : [src/polycopy/storage/models.py:138-139,187-188,270-273](src/polycopy/storage/models.py#L138)
- **Description** : `usdc_size`, `equity_usdc`, `realized_pnl`, `unrealized_pnl`, `drawdown_pct` typés `Float`. Erreurs d'arrondi cumulatives sur 1000s trades.
- **Impact** : drift DB vs on-chain truth de qqs cents/dollars. Faux kill switch possible sur gros historique.
- **Fix** : migration 0008 → `Numeric(18, 8)`. (Session ref : NEW)

#### [M-013] `BLACKLISTED_WALLETS` JSON non-lowercased (bug historique préservé backward-compat)
- **Location** : [src/polycopy/config.py:878-891](src/polycopy/config.py#L878-L891)
- **Description** : CSV lowercased, JSON non. Si user migre de CSV à JSON en gardant casse, double-check `wallet.lower() not in blacklisted` échoue.
- **Fix** : normaliser les 2 chemins + annoncer migration 1 version. (Session ref : NEW)

#### [M-014] `validator _validate_m5_bis_eviction` cross-field manquant (conflit TARGET ∩ BLACKLIST)
- **Location** : [src/polycopy/config.py:1025-1046](src/polycopy/config.py#L1025-L1046) + [src/polycopy/discovery/eviction/state_machine.py:205-214](src/polycopy/discovery/eviction/state_machine.py#L205-L214)
- **Description** : CLAUDE.md annonce "conflit TARGET_WALLETS ∩ BLACKLISTED_WALLETS avec EVICTION_ENABLED=true = crash boot clair". Le validator existe **partiellement** (vérifie overlap) mais n'exige pas `eviction_enabled` actif ni ne force un `transition_status_unsafe` défensif côté state_machine. Si un pinned est ajouté à blacklist en runtime, `transition_status_unsafe` bypasse le safeguard pinned.
- **Fix** : renforcer le validator + ajouter check explicite dans `reconcile_blacklist_decisions`. (Session ref : NEW)

#### [M-015] `DailySummaryScheduler` DST transition edge case
- **Location** : [src/polycopy/monitoring/daily_summary_scheduler.py:29-41](src/polycopy/monitoring/daily_summary_scheduler.py#L29-L41)
- **Description** : `replace(hour=...)` ambigü en DST forward/backward. Le timing du recap peut se décaler ±1 h 2× par an.
- **Fix** : disambiguation explicite via `fold=0/1` ou accepter le décalage. (Session ref : BACKLOG)

#### [M-016] `HeartbeatScheduler.skipped reason="recent_critical"` sans détail du critical
- **Location** : [src/polycopy/monitoring/heartbeat_scheduler.py:99-112](src/polycopy/monitoring/heartbeat_scheduler.py#L99-L112)
- **Description** : impossible de savoir a posteriori QUEL critical a bloqué le heartbeat.
- **Fix** : `AlertDispatcher.last_critical_event()` + log. (Session ref : BACKLOG)

#### [M-017] WS health watchdog double-trigger sur frontière idle
- **Location** : [src/polycopy/strategy/clob_ws_client.py:506](src/polycopy/strategy/clob_ws_client.py#L506)
- **Description** : seuil `idle > health_check_seconds * 2` avec `>` strict + boucle à intervalle `health_check_seconds` → peut reconnecter à tort si message arrive dans la gap.
- **Fix** : `>=` + hystérésis 5s avant re-reconnect. (Session ref : NEW)

#### [M-018] Dashboard URL lien tronqué dans digest volumineux (> 4096 chars)
- **Location** : [src/polycopy/monitoring/alert_renderer.py:195-202](src/polycopy/monitoring/alert_renderer.py#L195-L202)
- **Description** : troncation silencieuse → lien dashboard en fin de message coupé.
- **Fix** : pusher lien dashboard en tête OU réduire `DIGEST_SAMPLE_MAX`. (Session ref : BACKLOG)

#### [M-019] `DryRunResolutionWatcher` n'a pas de retry cap → positions NEG_RISK non-binaires restent open forever
- **Location** : [src/polycopy/executor/dry_run_resolution_watcher.py:174-190](src/polycopy/executor/dry_run_resolution_watcher.py#L174-L190)
- **Description** : marché NEG_RISK scalar (>2 outcomes) → `_winning_outcome_index` renvoie None → position reste open indéfiniment, log retry bruyant.
- **Fix** : cap `max_resolution_retries` → force-close REJECTED après N cycles. (Session ref : NEW, étend M13)

#### [M-020] Execution mode "simulation" fusionné dans `is_dry_run=True` → distinction perdue en DB
- **Location** : [src/polycopy/monitoring/pnl_writer.py:90-98](src/polycopy/monitoring/pnl_writer.py#L90-L98)
- **Description** : `is_dry_run: bool` ne capture pas le triplé (simulation, dry_run, live). Impossibilité de filtrer SIMULATION vs DRY_RUN historiquement.
- **Fix** : ajouter colonne `execution_mode: Literal`. Cf. [C-003] même migration. (Session ref : NEW)

#### [M-021] `_compute_cash_pnl_90d` ignore cash_pnl des positions ouvertes
- **Location** : [src/polycopy/discovery/metrics_collector_v2.py:225-227](src/polycopy/discovery/metrics_collector_v2.py#L225-L227)
- **Description** : gate `cash_pnl_90d > 0` calculé uniquement sur positions résolues. Un HODLer patient avec plein de positions ouvertes profitable est rejeté.
- **Fix** : inclure `cash_pnl` des positions ouvertes. (Session ref : NEW)

#### [M-022] `TraderDailyPnlWriter._compute_snapshot_dto` — calcul `realized_pnl_day` algébriquement faux
- **Location** : [src/polycopy/discovery/trader_daily_pnl_writer.py:163-171](src/polycopy/discovery/trader_daily_pnl_writer.py#L163-L171)
- **Description** : `realized_pnl_day = realized_cum - (prev.equity - prev.unrealized_day)` — l'identité soustraite ≠ `prev.realized_cum`.
- **Fix** : persister `realized_cum` directement + soustraire proprement. (Session ref : NEW)

#### [M-023] `transition_status` flag `active` incohérent entre 3 sites
- **Location** : [src/polycopy/storage/repositories.py:261-262,298-300](src/polycopy/storage/repositories.py#L261-L300) + [src/polycopy/storage/models.py:60-66](src/polycopy/storage/models.py#L60-L66)
- **Description** : invariant documenté `active=True ⟺ status ∈ {'active','pinned','sell_only'}` implémenté partiellement dans 3 endroits distincts.
- **Fix** : extraire helper `_resolve_active_flag(status)`. (Session ref : NEW)

#### [M-024] `filter_noisy_endpoints` re-compile regex à chaque `configure_logging()`
- **Location** : [src/polycopy/cli/logging_config.py:39-75](src/polycopy/cli/logging_config.py#L39-L75)
- **Description** : pattern list compilée fresh à chaque call. Inefficace sur tests en boucle.
- **Fix** : cache module-level. (Session ref : NEW)

#### [M-025] TOTP `valid_window=1` effectif `±60s` documenté `±30s`
- **Location** : [src/polycopy/remote_control/auth.py:36,70-72](src/polycopy/remote_control/auth.py#L36-L72)
- **Description** : `pyotp.TOTP.verify(valid_window=1)` check T-1, T, T+1 = 3 windows = ±60s. Docstring dit ±30s.
- **Fix** : aligner docstring sur comportement OU réduire à `valid_window=0`. (Session ref : NEW)

#### [M-026] Permissions log file race entre `mkdir` et chmod tardif
- **Location** : [src/polycopy/cli/logging_config.py:107-127](src/polycopy/cli/logging_config.py#L107-L127)
- **Description** : `touch(mode=0o600)` après `RotatingFileHandler.__init__` qui ouvre le fichier en umask système.
- **Fix** : `os.open(path, O_CREAT|O_EXCL, 0o600)` atomique AVANT handler init. (Session ref : connexe D3)

#### [M-027] `TargetTraderRepository.list_wallets_to_poll()` blacklist appliquée en Python, pas SQL
- **Location** : [src/polycopy/storage/repositories.py:84-114](src/polycopy/storage/repositories.py#L84-L114)
- **Description** : filtre status SQL + blacklist Python. Si `blacklist=None` (caller oublie env var), la défense-en-profondeur saute.
- **Fix** : lire `settings.blacklisted_wallets` dans la méthode + WHERE SQL + Python. (Session ref : NEW, M5_ter connexe)

#### [M-028] Cache entry timestamp boundary — `DataApiClient` cursor reset `start` inclusif
- **Location** : [src/polycopy/watcher/data_api_client.py:89](src/polycopy/watcher/data_api_client.py#L89)
- **Description** : `since = timestamp(last trade)` inclusif → duplicates absorbés par dedup `tx_hash` mais latency samples comptent la même trade 2×.
- **Fix** : `since = last_ts + 1`. (Session ref : NEW)

---

### 🟢 LOW — polish, cohérence, micro-perf

- **[L-001]** `_CacheEntry` dataclass non frozen — future in-place mutation risk. [clob_ws_client.py:152-156](src/polycopy/strategy/clob_ws_client.py#L152-L156)
- **[L-002]** `_extract_volume_24h` tolère 2 field names (volume24hr + volume_24h_usd) — schema brittleness. [_cache_policy.py:76-91](src/polycopy/strategy/_cache_policy.py#L76-L91)
- **[L-002]** `max(0.0, detected_ms)` masque les clock skew silencieusement. [wallet_poller.py:95-97](src/polycopy/watcher/wallet_poller.py#L95)
- **[L-003]** Pas de test explicite que `TraderLifecycleFilter` est le filtre [0] du pipeline → refactor futur peut casser l'ordre sans échec. [pipeline.py:306-313](src/polycopy/strategy/pipeline.py#L306-L313)
- **[L-004]** `get_home_kpi_cards` — sparkline filtre `is_dry_run=False` mais `latest_snapshot` pas filtré → incohérence 24h vs dernier point. [queries.py:657-676](src/polycopy/dashboard/queries.py#L657-L676)
- **[L-005]** `_format_card_usd` (entiers) vs `format_usd` (2 décimales) — rupture visuelle /home vs /activity. [queries.py:632-637](src/polycopy/dashboard/queries.py#L632) + [jinja_filters.py:22-42](src/polycopy/dashboard/jinja_filters.py#L22). Cf. session C C4.
- **[L-006]** `/performance?status=invalid` : validation côté Python après chargement DB → inefficace. [routes.py:547](src/polycopy/dashboard/routes.py#L547) + [queries.py:1848](src/polycopy/dashboard/queries.py#L1848)
- **[L-007]** `paused` legacy encore dans `_VALID_TRADER_STATUSES` dashboard. [queries.py:536](src/polycopy/dashboard/queries.py#L536)
- **[L-008]** `_LATENCY_STAGES_ORDER` hardcodé → nouveau stage ajouté apparaît en queue du graph. [queries.py:1190-1197](src/polycopy/dashboard/queries.py#L1190)
- **[L-009]** Health check externe cache TTL 30s — pas d'invalidation event-driven. [health_check.py:26,80-90](src/polycopy/dashboard/health_check.py#L26)
- **[L-010]** `/logs/download` filename hardcodé `polycopy.log` — confus si plusieurs bots. [routes.py:302-314](src/polycopy/dashboard/routes.py#L302)
- **[L-011]** Migration 0006 crée 3 indexes dont le composite `(wallet, date)` rend redondants les 2 singles. [0006_m12_trader_daily_pnl.py:58-68](alembic/versions/0006_m12_trader_daily_pnl.py)
- **[L-012]** `MACHINE_ID` normalisation silencieuse remplace chars → collision possible `PC@HOME` et `PC-HOME`. [config.py:970-995](src/polycopy/config.py#L970)
- **[L-013]** `MACHINE_ID` fallback `socket.gethostname()` non validé avant normalisation. [config.py:988](src/polycopy/config.py#L988)
- **[L-014]** `tailnet_name` regex `[a-z0-9-]+.ts.net` accepte `---` (RFC 1123 violation). [config.py:162-174](src/polycopy/config.py#L162)
- **[L-015]** `TradeLatencySample.purge_older_than()` ne vérifie pas `rowcount > 0` — purge silencieuse si DB locked. [repositories.py:1019-1028](src/polycopy/storage/repositories.py#L1019)
- **[L-016]** `MyPosition.realized_pnl` pas de CHECK constraint ni validation sign — bug executor inversion sign silencieux. [models.py:245](src/polycopy/storage/models.py#L245)
- **[L-017]** Weighted pondération v2 `0.25/0.20/0.20/0.15/0.10/0.10` — source non documentée (ni ADR, ni backtest). [aggregator.py:43-48](src/polycopy/discovery/scoring/v2/aggregator.py#L43)
- **[L-018]** `_compute_days_active` UTC strict ignore fuseau local — biais +X jours pour traders US. [metrics_collector_v2.py:230-240](src/polycopy/discovery/metrics_collector_v2.py#L230)
- **[L-019]** `exclude_wallets` set dans `CandidatePool.build` pas lowercased avant comparaison. [candidate_pool.py:72-73](src/polycopy/discovery/candidate_pool.py#L72)
- **[L-020]** `HysteresisTracker` in-memory reset silencieux au restart — audit trail incomplet pour eviction avortée par reboot. [hysteresis_tracker.py:44-46](src/polycopy/discovery/eviction/hysteresis_tracker.py#L44)
- **[L-021]** Repr d'exception CLOB loggée via `str(exc)` — risque low-probability d'échappement secret. [executor/pipeline.py:214-216](src/polycopy/executor/pipeline.py#L214)
- **[L-022]** `reconcile_blacklist` appelé 2× au boot puis à chaque cycle — bruyant mais idempotent. [discovery/orchestrator.py:151,451](src/polycopy/discovery/orchestrator.py#L151)
- **[L-023]** `EvictionStateMachine` — absence de reset du compteur `rebound` lors de pivot abort/rebound. [state_machine.py:79-166](src/polycopy/discovery/eviction/state_machine.py#L79)
- **[L-024]** `MAX_SELL_ONLY_WALLETS` default=10 hardcodé mais doc dit "aligné sur MAX_ACTIVE_TRADERS" — validator cross-field absent. [config.py:819-828](src/polycopy/config.py#L819)
- **[L-025]** `_decide_shadow` — `discovered_at=None` → `days_observed=0` éternellement, wallet bloqué shadow. [decision_engine.py:240-245](src/polycopy/discovery/decision_engine.py#L240)
- **[L-026]** Ré-injection UTC inconsistante entre call-sites (6 variantes repérées) — pattern `ensure_utc()` helper à extraire. Cf. horizontal audit.
- **[L-027]** `Size 0.00` sur `/activity` légal (SELL orphelin) mais confus sans tooltip. Cf. session C C2.

---

### 📋 INFO — observations, pas d'action immédiate

- **[I-001]** `SlippageChecker` n'a pas de `stage_name` dédié → son coût (potentiellement dominant) est fusionné dans `strategy_filtered_ms`. Enrichissement observabilité cf. session D.
- **[I-002]** Secrets : **aucune fuite détectée** dans les templates Jinja (monitoring + dashboard), les `repr`/exception messages scannés, les `executor_creds_ready` event, ni dans `TOTPGuard.verify`. Les `RuntimeError` CLOB citent les **noms** d'env vars, pas les valeurs.
- **[I-003]** `Decimal` vs `float` discipline : `clob_orderbook_reader.py`, `realistic_fill.py` respectent `Decimal(str(...))` puis `float()` à la persistance. Seul `virtual_wallet_reader.py:58-62` utilise `float` direct (`pos.size * mid`), reconnu comme acceptable pour simple multiplication.
- **[I-004]** Triple/quadruple garde-fou M3+M8 : **intact** (lazy init, RuntimeError boot, double-check pre-POST, assert dry_run avant `_persist_realistic_simulated`). Pas de régression.
- **[I-005]** Blacklist double-check : confirmé dans `DecisionEngine.decide` (pre-bootstrap) et `list_wallets_to_poll` (pre-polling). `reconcile_blacklist` idempotent boot + cycle.
- **[I-006]** Routes dashboard : **29 GET-only + 1 /healthz**. Aucune route POST/PUT/DELETE détectée. Contrat M4.5 read-only préservé.
- **[I-007]** Remote control : 3 POST (restart/stop/resume) sur `/v1/<action>/{machine}`, pipeline auth complet.
- **[I-008]** Spearman rank `/scoring` sur intersection v1∩v2 : correctement implémenté post-commit 1ba8ae3, mais ranks locaux ≠ ranks pool-wide affichés → peut dérouter un lecteur. Cf. session B B5.
- **[I-009]** `Discovery.paused` fusion `paused + sell_only` dans Home KPI — sémantiquement discutable (sell_only ≠ paused).

---

## 4. Security positives (à préserver)

- ✅ **Triple garde-fou M3** (lazy init ClobClient, RuntimeError boot si live sans creds, assert `dry_run is False` pré-POST) + **4ᵉ garde-fou M8** (`assert execution_mode == "dry_run"` avant `_persist_realistic_simulated`) — intact dans le code source.
- ✅ **Isolation package `discovery/eviction/`** — pure functions `CascadePlanner` + `EvictionStateMachine` testables indépendamment.
- ✅ **Grep automatisé anti-leak secrets** — 7 fichiers de tests couvrent les surfaces critiques (templates Telegram, log files, remote control).
- ✅ **Templates Jinja** : `autoescape=False` + `StrictUndefined` + filter `telegram_md_escape` sur wallet_address, condition_id, machine_id. Variable manquante crashe explicitement (pas de silent success).
- ✅ **`MACHINE_ID` normalisation stricte** Pydantic regex `^[A-Z0-9_-]+$` (sauf edge case silencieux [L-012]).
- ✅ **Dashboard read-only strict** : zéro POST/PUT/DELETE, `SELECT`-only via session factory.
- ✅ **Remote control Tailscale bind CGNAT** `100.64.0.0/10` double-checked (Pydantic + runtime).
- ✅ **`RateLimiter` deque sliding 60s** + **`AutoLockdown` 3 strikes** + sentinel file 0o600 — pipeline défensif bien structuré (sauf limites multi-worker [H-010] et TOCTOU [H-011]).
- ✅ **Signature CLOB L1+L2** confinée à `ClobWriteClient`, jamais instancié en dry-run.
- ✅ **Blacklist double-check** (pre-bootstrap + pre-promotion + reconcile au boot et cycle).

---

## 5. Strategic concerns (logique métier)

Section dédiée pour les "is this a good idea?".

### 5.1 Magic numbers mal justifiés

| # | Paramètre | Valeur | Localisation | Commentaire |
|---|---|---|---|---|
| S1 | `EVICTION_SCORE_MARGIN` | 0.15 | config.py:800 | Sur scores normalisés concentrés en [0.3, 0.7], 0.15 = 50% de la plage réelle. Distribution empirique suggère 0.05-0.08. Cf. session A A3. |
| S2 | `EVICTION_HYSTERESIS_CYCLES × DISCOVERY_INTERVAL` | 3 × 6h = 18h | config.py + hysteresis_tracker | Combiné variance v2 cycle-to-cycle, l'éviction ne déclenche quasi-jamais. |
| S3 | Pondérations v1 | 0.30/0.30/0.20/0.20 | scoring/v1.py | Source non documentée (aucun ADR, backtest, littérature). |
| S4 | Pondérations v2 | 0.25/0.20/0.20/0.15/0.10/0.10 | aggregator.py:43 | Source non documentée. Cf. [L-017]. |
| S5 | `_RATIO_CAP_SENTINEL` | 3.0 | risk_adjusted.py:34 | Arbitraire. Cause [H-009] (zombies dominent). |
| S6 | `_ZOMBIE_CURRENT_VALUE_PCT` | 0.02 | metrics_collector_v2.py:54 | 2% : drawdown 98% sur 1j ≠ zombie. |
| S7 | `_BRIER_MIN_RESOLVED` | 5 | metrics_collector_v2.py:58 | Statistiquement ≥ 20 nécessaires pour Brier significatif (IC 95% ≈ ±0.1). |
| S8 | `_TIMING_ALPHA_NEUTRAL` | 0.5 | metrics_collector_v2.py:49 | Placeholder. Effet secondaire +0.10 gratuit au score final (cf. [H-008]). |
| S9 | `_MIN_CURVE_POINTS` | 14 | risk_adjusted.py:37 | Incompatible gate `days_active≥30` normal mais compatible cold_start ≥7. |
| S10 | `DISCOVERY_INTERVAL_SECONDS` | 21600 (6h) | config.py | Trop lent pour marchés prédictifs avec TTL < 48h. |
| S11 | `KILL_SWITCH_DRAWDOWN_PCT` | defaults à | config.py | À vérifier — pas audité ici. |
| S12 | `STRATEGY_MAX_ENTRY_PRICE` | 0.97 | config.py M13 | Arbitraire (ni 0.95 ni 0.99). Bon ordre de grandeur mais pas backtesté. |
| S13 | Gate `trade_count_90d ≥ 50` | 50 | gates.py | Smart money patient → 30 trades massifs rejetés. |
| S14 | Gate `zombie_ratio < 0.40` | 0.40 | gates.py | 40% accepté — permissif si `_compute_zombie_ratio` cassé [H-14]. |
| S15 | `TRADER_SHADOW_DAYS` | 7 | config.py | Arbitraire. |
| S16 | `SCORING_V2_SHADOW_DAYS` | 14 | config.py | Vs. analyse variance [H-009] — peut être insuffisant. |

### 5.2 Dettes de conception vs dettes techniques

| Type | Item | Description |
|---|---|---|
| Conception | Scoring v2 | 4 défauts structurels indépendants (winsorisation instable, Sortino zombies, timing_alpha placeholder, Brier ambigu) s'empilent. |
| Conception | Calcul PnL fragmenté | 4 sources de vérité différentes (PnlSnapshotWriter, VirtualWalletStateReader, get_home_alltime_stats, list_trader_performance) → divergences garanties. |
| Technique | Float au lieu de Decimal | [M-012] sur colonnes USDC. |
| Technique | Cache TOCTOU | [M-007] sur 3 caches async. |
| Conception | Eviction margin irréaliste | [H-009] + session A A3 — les deux défauts (scoring instable + margin trop stricte) se cumulent. |
| Technique | Latency metrics fragmentées | [H-001] + session D D2. |

### 5.3 Mécaniques qui s'annulent

1. **`timing_alpha=0.5` + normalisation pool** → facteur inopérant mais consomme 20% de la pondération → effet global +0.10 sur score final (cf. [H-008]).
2. **Variance v2 cycle-to-cycle + eviction margin 0.15 + hysteresis 3 cycles** → le compteur d'éviction ne se stabilise jamais 3 cycles d'affilée sur la même direction. L'eviction théorique est en pratique quasi-impossible à déclencher.
3. **Gates durs v2 stricts (days_active≥30) + shadow_days=0 fallback v1** → l'utilisateur croit flipper v2 mais v1 pilote silencieusement [H-006].

---

## 6. Coverage summary

| Module | % lines audited | Profondeur | Notes |
|---|---|---|---|
| watcher/ | 100% | Haute | Spec M1 + M5_ter confrontée à l'impl |
| strategy/ | 95% | Haute | WS reconnect path non testable runtime |
| executor/ | 100% | Haute | Incl. dry-run M8 + DryRunResolutionWatcher M13 |
| monitoring/ | 100% | Haute | Incl. tous templates Jinja + AlertDigestWindow |
| dashboard/ | 95% | Haute | 1870 LOC queries.py lu intégralement |
| discovery/ | 100% | **Très haute** | 6 facteurs v2 + eviction package complet |
| storage/ | 100% | Haute | 7 migrations + models + repos |
| cli/ | 90% | Moyenne | runner.py bifurcation mode examinée |
| remote_control/ | 70% | Moyenne | Tailscale bind non testé runtime (hors portée) |
| config.py | 100% | Haute | Tous validators + cross-field checks |
| tests/ | ~20% | Basse | Audit structurel (mocks/asserts) seulement sur samples |
| alembic/ | 100% | Haute | 7 migrations + data migrations |
| docs/specs/ | 100% | Moyenne | Confrontation spec↔impl pour M1/M2/M3/M4/M5/M5_bis/M5_ter/M6/M7/M8/M9/M10/M11/M12/M12_bis/M13 |

**Audit cumulé estimé** : ~5 h agent-time (parallélisé sur 6 agents = ~1 h wall-time).

---

## 7. Mapping findings → sessions

### Session A (anti-toxic trader lifecycle)
- Confirmed : [H-007], [H-013], [L-023], [L-025], [S1], [S2]
- Ajoute : [M-023] (transition_status 3 sites), [L-024] (MAX_SELL_ONLY_WALLETS validator)
- Total : **A1-A6 validés** + 2 items complémentaires

### Session B (scoring v2 reliability)
- Confirmed : [H-006] (fallback silencieux), [H-009] (Sortino zombies), [M-002] (winsorisation), [M-003] (Brier baseline)
- Ajoute fort : [H-008] **timing_alpha +0.10 gratuit** (nouveau root cause), [H-014] **zombie_ratio filtre temporel non-implémenté**, [M-001] **Brier prob YES vs side-acheté**
- Total : **B1-B7 validés** + 3 nouvelles causes racines

### Session C (dashboard UX & consistency)
- Confirmed : [L-004], [L-005], [L-006], [L-007], [L-008], [L-010], [L-027]
- Ajoute : [C-005] divergence realized_pnl home/performance (upgrade CRITIQUE), [M-008] N+1 perf, [M-010] win rate edge case, [M-011] Gain max YES-only

### Session D (pipeline metrics + ops hygiene)
- Confirmed : [H-001] **cause racine filtered>enriched** identifiée (TraderLifecycleFilter early-reject + finally block)
- Ajoute : [M-026] log file permissions race, [M-015] DST edge case

### Non couvert par A/B/C/D → **Session E suggérée : "Integrity cross-layer + security hardening"**

Scope suggéré (3-5 items, 1-2 jours) :
- **E1** : fix [C-001] `simulated` filter manquant strategy → propager pattern dans 3 queries.
- **E2** : fix [C-002] kill switch bypass digest + cooldown CRITICAL (cf. [M-009]).
- **E3** : fix [C-003] + [M-020] ajout colonne `execution_mode` à `PnlSnapshot` (migration 0008) + refactor `get_max_total_usdc`.
- **E4** : fix [C-004] `VirtualWalletStateReader` fallback `last_known_mid` au lieu de skip.
- **E5** : unifier `dry_run_*_capital_usd` settings [H-004].

**Session F optionnelle** : "Scoring v2 structural cleanup" (si deep-search confirme les hypothèses de B).

---

## 8. Risk summary

### Hotspots (concentration de findings CRITICAL/HIGH)

| Module | CRITICAL | HIGH | MEDIUM | Total sev≥M |
|---|---|---|---|---|
| `monitoring/pnl_writer.py` | 2 (C-003, C-005) | 2 (H-002, H-005) | 1 (M-020) | 5 |
| `dashboard/queries.py` | 1 (C-005) | 0 | 4 | 5 |
| `strategy/pipeline.py` | 1 (C-001) | 1 (H-001) | 0 | 2 |
| `discovery/metrics_collector_v2.py` | 0 | 2 (H-014) | 3 (M-001, M-002, M-003, M-021, M-022) | 5+ |
| `discovery/scoring/v2/factors/` | 0 | 2 (H-008, H-009) | 0 | 2 |
| `config.py` | 0 | 0 | 4 (M-013, M-014) | 2 |

**Priorité** : `monitoring/pnl_writer.py` et `discovery/metrics_collector_v2.py` concentrent les bugs structurels. Ces deux fichiers devraient subir un refactor ciblé en parallèle d'une spec dédiée.

### Scénarios top-3 "what if"

1. **Flip dry_run → live avec positions virtuelles traînantes** ([C-001]) — probabilité **haute** (workflow standard), impact **haut** (zéro BUY live). Délai détection : aucun log clair.
2. **Kill switch retardé par digest window** ([C-002]) — probabilité **faible** (besoin de plusieurs kill_switch en 5 min, rare), impact **très haut** (perte capital). Délai : 5 min.
3. **Faux-positif kill switch au flip SIMULATION → DRY_RUN avec capital différent** ([C-003]) — probabilité **moyenne** (user Elie test 14j, change de mode), impact **moyen** (arrêt bot, pas de perte mais alarme fausse).

---

## 9. Recommandations prioritaires

### Top-5 actions AVANT deep-search (fix trivial, règle déjà claire)

1. **[C-001]** Ajouter filtre `simulated` à `PositionSizer`/`RiskManager` — 20 min de code + 5 tests.
2. **[C-002]** Bypass `AlertDigestWindow` pour CRITICAL + [M-009] cooldown=0 CRITICAL — 15 min.
3. **[H-002]** Peupler `PnlSnapshotDTO.realized_pnl`/`unrealized_pnl` au lieu de 0.0 hardcodé — 30 min.
4. **[H-005]** Écrire `TraderEvent(event_type="kill_switch")` en même temps que l'alerte — 20 min + migration nullable `wallet_address`.
5. **[H-004]** Fusionner les 2 capitals initiaux dry-run en 1 setting + deprecation warning — 45 min.

### Top-5 axes deep-search multi-LLM (ce que l'audit n'a pas tranché)

1. **Scoring formule alternative** : la littérature académique / bot Polymarket quelle formule utilise pour évaluer un "smart money" ? Le combo Sortino+Calmar+Brier+HHI+consistency+discipline est-il défendable ? → papers à citer.
2. **Winsorisation petit pool** : sur N<50 wallets, quelle technique de normalisation éviter la variance dynamique ([H-009] + M-002) ? Options : winsorisation sur pool de référence fixe (top-200 Polymarket), moving average p5/p95 EMA, quantile régularisé.
3. **Signal internal_performance** (session A A1) : la pondération 0.20 proposée est-elle justifiée par un backtest ? Quel poids donner au signal interne vs externe ?
4. **Timing alpha implémentation réelle** : un timing alpha "pair→wallet" rigoureux nécessite quoi ? (Stratégie D3 M12 reportée — est-ce implémentable sans Polymarket internals ?)
5. **Thresholds eviction margin** : comment calibrer `EVICTION_SCORE_MARGIN` sur la distribution empirique observée ? Y a-t-il un papier sur les "margins of statistical significance" pour ranking systems ?

### Top-3 hypothèses à valider empiriquement

1. **H-EMP-1** : mesurer la variance cycle-to-cycle v2 sur les 14j shadow period pour confirmer [H-008]+[H-009] (timing_alpha + Sortino zombies). Script `scripts/debug_scoring_v2_variance.py` déjà planifié session B B1.
2. **H-EMP-2** : capturer la distribution réelle des scores v2 sur 5-10 cycles → calculer si `EVICTION_SCORE_MARGIN=0.15` est atteignable.
3. **H-EMP-3** : backtest `SCORING_INTERNAL_FACTOR_ENABLED` (session A A1) sur pnl historique de 30j — le signal interne prédit-il mieux que external ?

---

## 10. Méta

- **Durée réelle d'audit** : ~1 h (wall-time, 6 agents parallèles) + ~20 min consolidation.
- **Fichiers lus** : ~120 Python + ~30 templates + ~20 specs.
- **Coverage exhaustif** atteint sur : discovery (scoring v2 + eviction), monitoring (pnl + alerts), executor (dry-run + live), dashboard (queries.py complet).
- **Coverage limité** : tests/ (audit statique seulement), remote_control/ (Tailscale runtime hors portée).

**Prochaine étape** : validation humaine des findings CRITICAL + HIGH, puis brief deep-search multi-LLM orienté Strategic concerns (§5) + Hypothèses (§9.3).
