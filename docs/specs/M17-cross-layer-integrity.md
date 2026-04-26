# M17 — Cross-layer integrity patches (audit CRITICALs)

**Status** : Draft — 2026-04-26
**Depends on** : M2 (Strategy pipeline + `PositionSizer` + `RiskManager`),
M3 (Executor + triple garde-fou intact), M4 (Monitoring + `PnlSnapshotWriter`
+ kill switch), M7 (Telegram + `AlertDigestWindow` + `AlertDispatcher`),
M8 (Dry-run realistic + `VirtualWalletStateReader`), M10 (parity 3 modes
SIMULATION/DRY_RUN/LIVE — `execution_mode` enum), M12_bis Phase D
(`SentinelFile` + ordre touch sentinel **avant** `stop_event.set()`),
M13 (`MyPosition.simulated` filter pattern + dry-run resolution + `/home`
queries), M14 (scoring v2.1 — strict no-touch), M15 (anti-toxic lifecycle —
`MyPosition.source_wallet_address` + `MyPositionRepository.sum_realized_pnl_by_source_wallet`
**API préservée**), M16 (fees-aware `PositionSizer._check_buy` — diff
strictement additif côté M17)
**Bloque** : passage `EXECUTION_MODE=live` (les CRITICALs s'activent au flip
dry_run→live OU au cutover de mode SIMULATION → DRY_RUN)
**Workflow git** : commits directement sur `main` (pas de branche, pas de
PR — règle projet)
**Charge estimée** : M (3-4 jours dev, 0 jour shadow — comportement
déterministe, fix audit pur)
**Numéro** : M17 (après M14 = MA `scoring v2.1`, M15 = MB `anti-toxic
lifecycle`, M16 = MC `dynamic fees + EV`)

---

## 0. TL;DR

M17 corrige les **5 CRITICALs cross-couche** + **3 HIGH connexes** révélés
par l'audit code 2026-04-24 ([docs/audit/2026-04-24-polycopy-code-audit.md](../audit/2026-04-24-polycopy-code-audit.md)
§3 🔴 CRITICAL + 🟠 HIGH). Tous **silencieux en dry-run continu**, mais
**bloquants au flip live** ou **corrupteurs du calcul de kill switch**.

**8 items couplés** mappés MD.1 → MD.8 du brief
[docs/next/MD.md](../next/MD.md) :

- **MD.1** — `PositionSizer._check_buy` + `_check_sell` + `RiskManager.check`
  filtrent désormais `MyPosition.simulated == (execution_mode != "live")` aux
  3 queries `WHERE closed_at IS NULL` ; un flip `dry_run → live` n'hérite
  plus des positions virtuelles traînantes (audit C-001).
- **MD.2** — `AlertDispatcher._handle` bypass de la **fenêtre digest** pour
  les alertes `level="CRITICAL"` (envoi immédiat, jamais batché). Le
  cooldown 60s par `cooldown_key` est **préservé** (idempotence anti-flood
  cohérente avec §11 piège). M-009 (auth fatal throttled) résolu via la
  même short-circuit (cooldown_key distinct par event ⇒ pas de collision).
- **MD.3** — Migration Alembic **0010** ajoute `pnl_snapshots.execution_mode
  VARCHAR(16) NOT NULL DEFAULT 'live'` + backfill depuis `is_dry_run`. La
  même migration relâche `trader_events.wallet_address` en `nullable=True`
  pour MD.7 (events système). `PnlSnapshotRepository.get_max_total_usdc`
  prend un nouveau paramètre `execution_mode: Literal[...]` strict —
  SIMULATION et DRY_RUN ne se polluent plus mutuellement (audit C-003 +
  M-020).
- **MD.4** — `VirtualWalletStateReader` maintient un dict `_last_known_mid`
  TTL 10 min. Mid manquant + last_known frais → fallback ; mid manquant +
  last_known stale → `MidpointUnavailableError` levée et catchée par
  `PnlSnapshotWriter` (skip snapshot, pas de drawdown factice — audit
  C-004).
- **MD.5** — `dry_run_virtual_capital_usd` **deprecated** au profit de
  `dry_run_initial_capital_usd` (source unique). Validator Pydantic lit
  l'ancien comme fallback avec WARNING `config_deprecation_dry_run_virtual_capital_env`,
  pattern strict copié de M10 `DRY_RUN=true` → `EXECUTION_MODE` (audit
  H-004).
- **MD.6** — `PnlSnapshotDTO.realized_pnl` et `unrealized_pnl` peuplés avec
  les vraies valeurs (au lieu de `0.0` hardcodé). Nouveau helper
  `WalletStateReader.get_realized_pnl_cumulative(mode=...)` agrège
  `MyPosition.realized_pnl` filtré par `simulated == (mode != "live")` ;
  `unrealized = total_usdc − initial_capital − realized_cumulative`. Les
  milestones `/pnl` et le PnL latent `/home` consomment désormais la même
  source de vérité (audit H-002 + C-005 conséquence).
- **MD.7** — Kill switch écrit un `TraderEvent(wallet_address=NULL,
  event_type="kill_switch")` avant `stop_event.set()`. Le pipeline
  `_maybe_trigger_alerts` reste **strictement** ordonné `insert_event →
  push_alert → touch_sentinel → stop_event.set()`. Le `Literal
  TraderEventType` étendu avec `"kill_switch"` (audit H-005).
- **MD.8** — *(optionnel — peut migrer en MI)* Pattern single-flight
  `_inflight: dict[str, asyncio.Future]` propagé aux 3 caches async
  (`GammaApiClient.get_market`, `ClobOrderbookReader.get_book`,
  `ClobMarketWSClient._maybe_subscribe`). Économie 5-10× requêtes sur
  burst de coroutines concurrentes même clé (audit M-007).

Diff strictement additif sur les invariants critiques :

- **Triple garde-fou M3 + 4ᵉ M8** : intacts. M17 n'ajoute aucune surface de
  signature CLOB, uniquement des fix de cohérence data + ordre alerte.
- **Kill switch parité 3 modes M10** : M17 **renforce** l'invariant — MD.2
  garantit immédiateté, MD.3 garantit ségrégation des baselines, MD.7
  garantit traçabilité.
- **API M15 préservée** : `MyPositionRepository.sum_realized_pnl_by_source_wallet`
  + `count_wins_losses_by_source_wallet` signatures **inchangées** — MB.1
  (`_compute_internal_pnl_score`) et MB.8 (auto-blacklist) continuent à
  filtrer `simulated == (execution_mode != "live")` selon le pattern
  pré-existant. MD.1 propage juste ce **même** pattern aux 3 queries
  `MyPosition` du pipeline strategy/risk qui l'avaient oublié.
- **Append-only scoring versions M14/M15** : aucun touch des fonctions
  `compute_score_v2_1` / `compute_score_v2_1_1` ni du registry. MD.6
  alimente le DTO PnL au niveau monitoring, hors couche scoring.
- **Sentinel `halt.flag` 0o600** : intact. MD.2 + MD.7 préservent l'ordre
  exact `touch sentinel → stop_event.set()` documenté CLAUDE.md §Sécurité
  M12_bis Phase D.

Tests cumulés estimés : **~28 tests unit** + **3 tests intégration**
(flip mode E2E + kill switch immédiat + dashboard convergence
home/performance). Charge cumulée : **3-4 jours dev**, 0 jour shadow.
Prérequis : aucun (M17 indépendant de toute hypothèse empirique). Bloque
le passage live (les bugs s'activent au flip).

Aucun rétro-backfill : les snapshots historiques `pnl_snapshots` se
backfill `execution_mode` via la migration 0010 ; les rows
`my_positions` antérieures à M15 conservent `source_wallet_address=NULL`
(cohérent §M15.2.2). Versioning sacré préservé.

---

## 1. Motivation & use case concret

### 1.1 Le symptôme observé — 2026-04-24

L'audit code 2026-04-24 documente 5 CRITICALs cross-couche
([audit §3](../audit/2026-04-24-polycopy-code-audit.md#L60)) :

> **C-001** [pipeline.py:188-191](../../src/polycopy/strategy/pipeline.py#L188-L191) :
> les 3 queries `select(MyPosition).where(MyPosition.closed_at.is_(None))`
> dans `_check_buy`, `_check_sell` et `RiskManager.check` **n'incluent pas**
> de clause `MyPosition.simulated == ...`. En dry-run continu, c'est
> invisible (toutes les positions sont `simulated=True`). Au flip
> `EXECUTION_MODE=live`, les ~512 positions virtuelles M13 traînant
> ouvertes (cf. observation 2026-04-23 [M13 §1.1](M13_dry_run_observability_spec.md))
> bloquent silencieusement TOUS les BUY live avec `position_already_open`.
>
> **C-002** [alert_dispatcher.py:135](../../src/polycopy/monitoring/alert_dispatcher.py#L135) :
> `self._digest.register(alert, self._now())` est appelé pour **toutes**
> les alertes, sans court-circuit `level=="CRITICAL"`. Si `threshold-1`
> alertes même event_type sont déjà dans la fenêtre 5 min, le kill switch
> est **batché** et le `stop_event.set()` qui vit dans
> `PnlSnapshotWriter._maybe_trigger_alerts` se déclenche bien (autre
> chemin), MAIS le message Telegram critique se trouve dans le digest →
> l'utilisateur ne reçoit pas l'alerte immédiate. Pire : si une rafale
> d'alertes routine (`pnl_snapshot_drawdown` warning) bouche la fenêtre,
> le digest peut absorber un kill_switch (le compteur compte par
> event_type, mais l'effet UX = silence côté Telegram).
>
> **C-003** [pnl_writer.py:86-87](../../src/polycopy/monitoring/pnl_writer.py#L86-L87) +
> [repositories.py:961-969](../../src/polycopy/storage/repositories.py#L961-L969) :
> `only_real = execution_mode == "live"` — quand `execution_mode in
> {"simulation", "dry_run"}`, `only_real=False` ⇒ `get_max_total_usdc`
> renvoie le max sur **tous** les snapshots `is_dry_run=True` (SIM+DRY).
> Si l'utilisateur a tourné un backtest SIMULATION avec
> `dry_run_virtual_capital_usd=$50000`, puis bascule
> `EXECUTION_MODE=dry_run` à `$1000`, le premier tick écrit `total_usdc=$1000`
> et calcule `drawdown = (1000 - 50000) / 50000 = 98%` → kill switch
> immédiat **faux-positif**.
>
> **C-004** [virtual_wallet_reader.py:54-57](../../src/polycopy/executor/virtual_wallet_reader.py#L54-L57) :
> `if mid is None: continue` skippe la position au lieu d'utiliser un
> last_known. En cas de panne CLOB midpoint transitoire (5xx, 429,
> network blip), la position vaut **0** dans le calcul `total_usdc` du
> tick courant → `total_usdc` plonge → max historique baisse aussi
> (premier tick avec mid manquant peut écrire un nouveau min). Au tick
> suivant avec mid OK, `total_usdc` remonte → drawdown **artificiel**
> calculé contre le min creux. Risque kill switch faux-positif sur
> outage transient.
>
> **C-005** [queries.py:802-817](../../src/polycopy/dashboard/queries.py#L802-L817) vs
> [queries.py:1770-1828](../../src/polycopy/dashboard/queries.py#L1770-L1828) :
> `get_home_alltime_stats` calcule `live_pnl` inline `Σ(SELL×price) -
> Σ(BUY×price)` sur les fills des positions closed.
> `list_trader_performance` calcule `stats["sell_recovery"] -
> stats["buy_cost"]` après agrégation par trader. Mêmes données,
> ordres d'évaluation et timezone-handling différents → divergences
> visibles UX. L'utilisateur a déjà signalé l'écart sur dashboard
> 2026-04-24 (cf. memory `project_dashboard_audit_20260424.md`).

3 HIGH connexes :

> **H-002** [pnl_writer.py:90-98](../../src/polycopy/monitoring/pnl_writer.py#L90-L98) :
> `realized_pnl=0.0, unrealized_pnl=0.0` hardcodés dans le DTO. Les
> colonnes DB sont peuplées de zéros. Les milestones `/pnl` qui les
> consomment ont des graphes plats sur ces axes.
>
> **H-004** [config.py:316](../../src/polycopy/config.py#L316) +
> [config.py:360](../../src/polycopy/config.py#L360) +
> [virtual_wallet_reader.py:62](../../src/polycopy/executor/virtual_wallet_reader.py#L62) +
> [queries.py:965](../../src/polycopy/dashboard/queries.py#L965) : deux
> settings `dry_run_virtual_capital_usd` (default 1000) et
> `dry_run_initial_capital_usd` (default None) cohabitent. Le premier
> alimente le `VirtualWalletStateReader` (`total_usdc =
> virtual_capital + realized + unrealized`). Le second alimente la
> `/home` PnL latent card. Si l'utilisateur ne sait pas qu'il y en a
> deux, il modifie l'un et constate des divergences entre `total_usdc`
> et la card "PnL latent".
>
> **H-005** [pnl_writer.py:127-155](../../src/polycopy/monitoring/pnl_writer.py#L127-L155) :
> kill switch écrit Telegram + `stop_event.set()` + sentinel mais
> **n'écrit jamais** dans `trader_events`. La query
> `get_pnl_milestones` à
> [queries.py:1119-1133](../../src/polycopy/dashboard/queries.py#L1119-L1133)
> sélectionne `TraderEvent.event_type == "kill_switch"` → résultat
> toujours vide → milestone `/pnl` "Kill switch" jamais affiché.
> Audit trail muet sur l'événement le plus important du bot.

Et 1 MEDIUM connexe à C-002 :

> **M-009** [alert_dispatcher.py:123-133](../../src/polycopy/monitoring/alert_dispatcher.py#L123-L133) :
> le cooldown 60s s'applique aussi aux CRITICAL. Si `executor_auth_fatal`
> fire à T+0 et T+30s (ex: rotation de clé pendant un poll), la 2ᵉ alerte
> est **drop silencieusement**. L'utilisateur peut manquer la 2ᵉ
> notification d'un problème encore actif.

### 1.2 Pourquoi P2 (différable tant qu'on est en dry-run)

| Bug | Mord en dry-run continu ? | Mord au flip live ? | Impact si mord |
|---|---|---|---|
| C-001 | Non (filter manquant N/A) | **Oui** (workflow standard) | **Zéro BUY live** silencieux. Délai détection : aucun log clair, l'utilisateur voit "0 trade en 24h". |
| C-002 | Si rafale ≥ N CRITICAL en 5 min | Idem | Stop differé jusqu'à 5 min — perte capital potentielle pendant la fenêtre. |
| C-003 | Au changement de capital virtuel OU au flip mode | Oui | Faux-positif kill switch immédiat → stop bot, perte de la run. |
| C-004 | Sur outage CLOB midpoint transitoire | Idem | Drawdown factice → kill switch faux-positif sur outage 5xx. |
| C-005 | Visible UX (utilisateur signalé 2026-04-24) | Idem | Confusion UX. Pas bloquant. |
| H-002 | Permanent (DB peuplée 0.0) | Idem | Milestones plats, graphes mort. Pas bloquant. |
| H-004 | Latent (config divergence) | Idem | Confusion UX si l'utilisateur édite l'un et pas l'autre. |
| H-005 | Permanent (events kill_switch jamais écrits) | Idem | Milestone `/pnl` "Kill switch" jamais peuplé. |
| M-009 | Sur rafale auth fatal | Idem | 2ᵉ alerte critique drop. Rare mais grave. |

**Aucun bug ne mord en dry-run stable** sur le test 14j en cours
2026-04-24. M17 doit ship **avant** le flip `EXECUTION_MODE=live` mais
**après** MA + MB + MC (déjà mergés sur `main` cf. git log
80bb3c6..872cadf). Idéalement avant toute spec MF/MH qui consomme
`internal_pnl_score` (cf. M15 §1.5) — un realized_pnl corrompu en amont
polluerait toute la chaîne anti-toxic.

### 1.3 Ce qui ne change PAS dans M17

Diff strictement additif sur les invariants critiques :

- **Triple garde-fou M3** : `lazy_init ClobClient`, `RuntimeError` boot
  si `live` + creds absentes, `assert execution_mode == "live"` avant
  POST, `WalletStateReader` re-fetch — **intacts**.
- **4ᵉ garde-fou M8** : `assert settings.execution_mode == "dry_run"`
  avant `_persist_realistic_simulated` — **intact**.
- **Pipeline order strategy M2 + M11 + M13 + M16** : `TraderLifecycle →
  Market → EntryPrice → PositionSizer → SlippageChecker → RiskManager`
  — **intact**. MD.1 ajoute juste un `WHERE simulated = ?` aux 3 queries
  internes, pas un nouveau filtre.
- **Side-aware MD.13 Bug 5** : `_check_buy` filtre coarse sur
  `condition_id`, `_check_sell` filtre fin sur `(condition_id,
  asset_id)` — **intact**. MD.1 propage `simulated` aux deux sites.
- **M14 scoring v2.1 + M15 v2.1.1** : aucune fonction scoring touchée.
  MD.6 alimente `PnlSnapshotDTO.realized_pnl` au niveau monitoring,
  hors couche scoring (registry M14
  [`SCORING_VERSIONS_REGISTRY`](../../src/polycopy/discovery/scoring/__init__.py)
  intact).
- **M15 MB.1 + MB.8 API publiques** :
  `MyPositionRepository.sum_realized_pnl_by_source_wallet(*,
  wallet_address, since, simulated)` et
  `count_wins_losses_by_source_wallet` signatures **strictement
  préservées**. M17 n'altère ni la signature, ni le filtre `simulated`
  qu'elles appliquent. MD.1 propage juste le même pattern de filtre
  aux queries non-source-wallet du pipeline.
- **M15 MB.6 probation** : `PositionSizer._apply_probation_multiplier`
  reste appliqué après le check fee/EV M16 (cf. pipeline.py:262-264). MD.1
  ajoute juste le filtre `simulated` à la query `existing` du début de
  `_check_buy`, AVANT toute logique probation/EV.
- **M16 fee adjustment** : `FeeRateClient` + EV after-fee dans
  `_check_buy` — **intact**. MD.1 ajoute le filtre `simulated` à la
  query `existing` qui vit AVANT le bloc fee, n'altère ni la math fee
  ni le rejet `ev_negative_after_fees`.
- **Sentinel halt.flag M12_bis Phase D** : permissions 0o600 (fichier)
  + 0o700 (parent), ordre `touch sentinel → stop_event.set()` —
  **intact**. MD.2 + MD.7 préservent strictement cet ordre (MD.7
  ajoute juste `insert_event` AVANT le touch sentinel).

### 1.4 Ce que change explicitement M17 (vue de haut)

| Module | Diff | Référence MD |
|---|---|---|
| [src/polycopy/strategy/pipeline.py:188-191](../../src/polycopy/strategy/pipeline.py#L188-L191) | `_check_buy` query `existing` : ajout `MyPosition.simulated == settings.execution_mode != "live"` | MD.1 |
| [src/polycopy/strategy/pipeline.py:333-338](../../src/polycopy/strategy/pipeline.py#L333-L338) | `_check_sell` query `existing` : même ajout | MD.1 |
| [src/polycopy/strategy/pipeline.py:402-404](../../src/polycopy/strategy/pipeline.py#L402-L404) | `RiskManager.check` query `open_positions` : même ajout | MD.1 |
| [src/polycopy/monitoring/alert_dispatcher.py:117-145](../../src/polycopy/monitoring/alert_dispatcher.py#L117-L145) | `_handle` : early-return bypass digest si `level="CRITICAL"`. Cooldown préservé. | MD.2 |
| [alembic/versions/0010_pnl_snapshot_execution_mode.py](../../alembic/versions/0010_pnl_snapshot_execution_mode.py) | **Nouveau fichier**. Migration 0010 : ADD `pnl_snapshots.execution_mode` + backfill + `trader_events.wallet_address` nullable | MD.3 + MD.7 |
| [src/polycopy/storage/models.py:285-303](../../src/polycopy/storage/models.py#L285-L303) | `PnlSnapshot.execution_mode` Mapped + CHECK constraint Literal triple | MD.3 |
| [src/polycopy/storage/models.py:374-392](../../src/polycopy/storage/models.py#L374-L392) | `TraderEvent.wallet_address` → `Mapped[str \| None]`, nullable=True | MD.7 |
| [src/polycopy/storage/dtos.py:57-68](../../src/polycopy/storage/dtos.py#L57-L68) | `PnlSnapshotDTO.execution_mode: Literal[...]` ; conserve `is_dry_run` 1 version | MD.3 |
| [src/polycopy/storage/dtos.py:124-152](../../src/polycopy/storage/dtos.py#L124-L152) | `TraderEventType` += `"kill_switch"` | MD.7 |
| [src/polycopy/storage/dtos.py:168-180](../../src/polycopy/storage/dtos.py#L168-L180) | `TraderEventDTO.wallet_address: str \| None = None` (system events) | MD.7 |
| [src/polycopy/storage/repositories.py:961-993](../../src/polycopy/storage/repositories.py#L961-L993) | `get_max_total_usdc(*, execution_mode: Literal[...] \| None = None)` + `get_latest` + `list_since` | MD.3 |
| [src/polycopy/storage/repositories.py:1083-1099](../../src/polycopy/storage/repositories.py#L1083-L1099) | `TraderEventRepository.insert` autorise `wallet_address=None` (lower-cased en `None`-safe) | MD.7 |
| [src/polycopy/executor/virtual_wallet_reader.py:30-83](../../src/polycopy/executor/virtual_wallet_reader.py) | `_last_known_mid` dict + TTL 10 min + `MidpointUnavailableError` | MD.4 |
| [src/polycopy/executor/exceptions.py](../../src/polycopy/executor/exceptions.py) | **Nouveau** : `MidpointUnavailableError` | MD.4 |
| [src/polycopy/executor/__init__.py](../../src/polycopy/executor/__init__.py) | Réexport `MidpointUnavailableError` | MD.4 |
| [src/polycopy/config.py:316-369](../../src/polycopy/config.py#L316-L369) | Validator deprecation `dry_run_virtual_capital_usd → dry_run_initial_capital_usd` | MD.5 |
| [src/polycopy/executor/orchestrator.py:110](../../src/polycopy/executor/orchestrator.py#L110) | `virtual_capital=settings.dry_run_initial_capital_usd or settings.risk_available_capital_usd_stub` | MD.5 |
| [src/polycopy/executor/virtual_wallet_reader.py:62](../../src/polycopy/executor/virtual_wallet_reader.py#L62) | Idem (source unique) | MD.5 |
| [src/polycopy/executor/wallet_state_reader.py](../../src/polycopy/executor/wallet_state_reader.py) | Nouveau helper `get_realized_pnl_cumulative(*, mode)` (live + virtual) | MD.6 |
| [src/polycopy/monitoring/pnl_writer.py:78-109](../../src/polycopy/monitoring/pnl_writer.py#L78-L109) | `_tick` peuple `realized_pnl` + `unrealized_pnl` + `execution_mode` dans le DTO | MD.3 + MD.6 |
| [src/polycopy/monitoring/pnl_writer.py:127-155](../../src/polycopy/monitoring/pnl_writer.py#L127-L155) | `_maybe_trigger_alerts` insert `TraderEvent("kill_switch")` AVANT `stop_event.set()` | MD.7 |
| [src/polycopy/dashboard/queries.py:1119-1133](../../src/polycopy/dashboard/queries.py#L1119-L1133) | `get_pnl_milestones` consomme désormais des events `kill_switch` non-vides | MD.7 |
| [src/polycopy/strategy/gamma_client.py:88-108](../../src/polycopy/strategy/gamma_client.py) | (MD.8 optionnel) `_inflight: dict[str, Future]` single-flight | MD.8 |
| [src/polycopy/executor/clob_orderbook_reader.py:58-76](../../src/polycopy/executor/clob_orderbook_reader.py) | (MD.8 optionnel) idem | MD.8 |
| [src/polycopy/strategy/clob_ws_client.py:237-268](../../src/polycopy/strategy/clob_ws_client.py) | (MD.8 optionnel) idem `_maybe_subscribe` | MD.8 |
| [.env.example](../../.env.example) | DEPRECATED comment sur `DRY_RUN_VIRTUAL_CAPITAL_USD` | MD.5 |
| [CLAUDE.md](../../CLAUDE.md) | §Conventions + §Sécurité — bloc M17 cross-layer | tous |
| Tests | +28 unit + 3 intégration | tous |

---

## 2. Scope / non-goals

### 2.1 Dans le scope

**MD.1 — Filtre `simulated` dans `PositionSizer` + `RiskManager`** :

- 3 queries SQL `select(MyPosition).where(MyPosition.closed_at.is_(None))`
  étendues avec `MyPosition.simulated == (settings.execution_mode != "live")`.
  Locations exactes :
  - [pipeline.py:188-191](../../src/polycopy/strategy/pipeline.py#L188-L191)
    (`_check_buy` query `existing`).
  - [pipeline.py:333-338](../../src/polycopy/strategy/pipeline.py#L333-L338)
    (`_check_sell` query `existing`).
  - [pipeline.py:402-404](../../src/polycopy/strategy/pipeline.py#L402-L404)
    (`RiskManager.check` query `open_positions`).
- Décision **D1** : binaire `simulated = (execution_mode != "live")`. Ne
  distingue pas SIMULATION vs DRY_RUN (même flag DB depuis M8). La
  ségrégation tri-state passe par MD.3 (`pnl_snapshots.execution_mode`),
  pas via une nouvelle colonne sur `my_positions` (cf. §11 piège — M3..M14
  n'ont pas un tri-state sur les positions, on ne le crée pas en M17).
- **Pattern référent déjà à l'œuvre** :
  [`MyPositionRepository.list_open_virtual`](../../src/polycopy/storage/repositories.py#L804)
  utilise `simulated.is_(True)` ;
  [`upsert_on_fill` vs `upsert_virtual`](../../src/polycopy/storage/repositories.py)
  séparent les deux paths à l'écriture. MD.1 propage le pattern aux
  3 queries strategy/risk qui l'avaient oublié. **Aucune nouvelle
  abstraction**.
- **Attention regression** : un grep `MyPosition.closed_at.is_(None)` doit
  retourner exactement les 3 queries listées + les queries
  `MyPositionRepository.list_open_virtual` (déjà filtrée) + queries
  exécution `_persist_*` (qui ne filtrent pas car écrivent l'unique row).
  Aucune autre query à corriger. Vérifié §6.1 inventaire.

**MD.2 — Bypass digest pour CRITICAL ; cooldown préservé** :

- Modification stricte de
  [`AlertDispatcher._handle`](../../src/polycopy/monitoring/alert_dispatcher.py#L117) :

  ```python
  async def _handle(self, alert: Alert) -> None:
      """Applique cooldown → digest → rendu → envoi Telegram.

      M17 MD.2 : si ``alert.level == "CRITICAL"`` → bypass DIGEST (envoi
      immédiat, jamais batché). Cooldown préservé : un même cooldown_key
      reste throttlé 60s pour éviter le flood en cascade. Cf. spec §11.4.
      """
      self._counts_since_boot[alert.event] += 1
      if alert.level in _CRITICAL_LEVELS:
          self._last_critical_at = self._now()

      # M17 MD.2 : cooldown reste appliqué AUSSI aux CRITICAL pour
      # garantir l'idempotence (cf. piège §11.4). Le bypass concerne
      # UNIQUEMENT la fenêtre digest qui peut retarder de 5 min.
      if alert.cooldown_key is not None:
          now = self._now()
          last = self._last_sent.get(alert.cooldown_key)
          if last is not None and (now - last).total_seconds() < self._cooldown_seconds:
              log.debug(
                  "alert_throttled",
                  cooldown_key=alert.cooldown_key,
                  alert_event=alert.event,
                  level=alert.level,
              )
              return
          self._last_sent[alert.cooldown_key] = now

      # M17 MD.2 : early-return bypass digest pour CRITICAL.
      if alert.level == "CRITICAL":
          formatted = self._renderer.render_alert(alert)
          sent = await self._telegram.send(formatted)
          log.info(
              "alert_sent_critical_bypass_digest",
              alert_event=alert.event,
              level=alert.level,
              sent=sent,
          )
          return

      decision = self._digest.register(alert, self._now())
      self._digest_buffer[alert.event].append(alert)
      # ... (logique existante M7 préservée pour non-CRITICAL)
  ```
- **Décision D2** : bypass strictement la **fenêtre digest** (le
  `register` qui retarde) ; le **cooldown** reste appliqué. Justification
  §11.4 piège : sans cooldown, un kill switch qui re-fire à chaque
  snapshot (cycles de 300s par défaut, ou 5s en cas de bug) provoque un
  flood Telegram → on perd la lisibilité. Avec cooldown 60s :
  - kill_switch fire à T+0 → envoi immédiat.
  - re-fire à T+5s → drop (même `cooldown_key="kill_switch"`).
  - re-fire à T+65s → envoi (escalation utile : "le bot est encore en
    drawdown 1 min plus tard").
- **M-009 résolu** par le même mécanisme : `executor_auth_fatal` a un
  `cooldown_key="auth_fatal"` distinct. Si l'utilisateur veut zéro
  throttle même 60s, il peut set `ALERT_COOLDOWN_SECONDS=0` (déjà
  exposé). Pas de changement settings v1.
- **Ordre strict `_maybe_trigger_alerts`** : MD.7 ajoute
  `insert_event` AVANT `_push_alert` (cf. §5.7). L'ordre **strict**
  reste :
  1. `await self._events_repo.insert(TraderEventDTO(event_type="kill_switch", ...))`.
  2. `self._push_alert(Alert(level="CRITICAL", ...))`.
  3. `if self._sentinel: self._sentinel.touch(reason="kill_switch")`.
  4. `stop_event.set()`.

  Étape 2 (`_push_alert`) n'attend rien — c'est un `put_nowait` dans la
  queue. Le dispatcher draine la queue dans une coroutine séparée ;
  MD.2 garantit qu'il n'introduit pas de latence digest sur la sortie.
  Étapes 3+4 ne dépendent pas de Telegram (le sentinel + stop_event
  sont locaux). Le sentinel reste **strictement avant** `stop_event.set()`
  (CLAUDE.md §Sécurité M12_bis Phase D).

**MD.3 — Migration Alembic 0010 + `pnl_snapshots.execution_mode`** :

- **Numéro 0010** (et **non 0008** comme le brief MD.md le mentionne).
  Justification : la chain Alembic actuelle est `0001 → ... → 0007 →
  0009` (le 0008 a été sauté intentionnellement par M15, cf.
  [docstring 0009_m15_anti_toxic_lifecycle.py](../../alembic/versions/0009_m15_anti_toxic_lifecycle.py)).
  M17 prend **0010** avec `down_revision="0009_m15_anti_toxic_lifecycle"`,
  chain linéaire. Pas de re-numérotation rétroactive (versioning sacré).
- Walkthrough complet en §11.
- Schéma résultant pour `pnl_snapshots` :
  ```
  id INTEGER PRIMARY KEY AUTOINCREMENT
  timestamp DATETIME NOT NULL
  total_usdc FLOAT NOT NULL DEFAULT 0.0
  realized_pnl FLOAT NOT NULL DEFAULT 0.0
  unrealized_pnl FLOAT NOT NULL DEFAULT 0.0
  drawdown_pct FLOAT NOT NULL DEFAULT 0.0
  open_positions_count INTEGER NOT NULL DEFAULT 0
  cash_pnl_total FLOAT NULL
  is_dry_run BOOLEAN NOT NULL DEFAULT 0     -- DEPRECATED (1 version)
  execution_mode VARCHAR(16) NOT NULL DEFAULT 'live'  -- NEW
  CHECK (execution_mode IN ('simulation', 'dry_run', 'live'))  -- NEW
  ```
- Backfill in-place :
  - `UPDATE pnl_snapshots SET execution_mode='dry_run' WHERE is_dry_run=1`
  - `UPDATE pnl_snapshots SET execution_mode='live' WHERE is_dry_run=0`
- **Décision D3** : conserver `is_dry_run` 1 version puis deprecate via
  warning de lecture (pattern strict copié M10 `DRY_RUN=true/false` →
  `EXECUTION_MODE`). Le writer M17 écrit dans **les deux** colonnes
  (`is_dry_run = (execution_mode != "live")` + `execution_mode = ...`)
  pour rétrocompat lecture des outils externes. Drop programmé en
  M18+ via une migration 0011 qui drop la colonne.
- `PnlSnapshotRepository.get_max_total_usdc(*, execution_mode: Literal[...] | None = None)` :
  - `None` → comportement legacy (no filter, agrège tous les modes —
    pour outils dashboard cross-mode si besoin).
  - `"live"` → `WHERE execution_mode = 'live'`.
  - `"dry_run"` → `WHERE execution_mode = 'dry_run'`.
  - `"simulation"` → `WHERE execution_mode = 'simulation'`.
- Idem `get_latest` et `list_since`. Le paramètre **legacy `only_real`
  reste accepté** pendant 1 version pour les tests M4..M16 (avec
  warning de deprecation). Cf. §11.3.
- `PnlSnapshotWriter._tick` :
  ```python
  state = await self._reader.get_state()
  total = state.total_position_value_usd + state.available_capital_usd
  mode = self._settings.execution_mode  # "simulation" | "dry_run" | "live"

  # M17 MD.3 : segregation stricte par mode (plus de bucket SIM+DRY).
  max_ever = await self._repo.get_max_total_usdc(execution_mode=mode)
  drawdown_pct = self._compute_drawdown_pct(max_ever, total)

  # M17 MD.6 : peuple realized_pnl + unrealized_pnl avec les vraies valeurs.
  realized_cumulative = await self._reader.get_realized_pnl_cumulative(mode=mode)
  initial_capital = float(
      self._settings.dry_run_initial_capital_usd
      or self._settings.risk_available_capital_usd_stub
  )
  unrealized_pnl = total - initial_capital - realized_cumulative

  is_simulated = mode != "live"
  dto = PnlSnapshotDTO(
      total_usdc=total,
      realized_pnl=realized_cumulative,        # ◀── MD.6
      unrealized_pnl=unrealized_pnl,           # ◀── MD.6
      drawdown_pct=drawdown_pct,
      open_positions_count=state.open_positions_count,
      cash_pnl_total=None,
      is_dry_run=is_simulated,                 # legacy compat 1 version
      execution_mode=mode,                     # ◀── MD.3
  )
  await self._repo.insert(dto)
  ```

**MD.4 — `VirtualWalletStateReader` last_known_mid + exception** :

- Nouvelle exception
  [`src/polycopy/executor/exceptions.py::MidpointUnavailableError`](../../src/polycopy/executor/exceptions.py) :

  ```python
  class MidpointUnavailableError(RuntimeError):
      """M17 MD.4 — levée par VirtualWalletStateReader si le mid d'au moins
      une position est manquant ET aucun last_known frais (≤10 min) n'est
      disponible. Catchée par PnlSnapshotWriter → skip snapshot
      (pas de drawdown factice).
      """

      def __init__(self, asset_id: str, last_known_age_seconds: float | None) -> None:
          super().__init__(
              f"Midpoint unavailable for asset_id={asset_id}, "
              f"last_known_age={last_known_age_seconds}s"
          )
          self.asset_id = asset_id
          self.last_known_age_seconds = last_known_age_seconds
  ```
- Réexport public dans
  [`src/polycopy/executor/__init__.py`](../../src/polycopy/executor/__init__.py)
  pour que le writer puisse `catch` proprement.
- `VirtualWalletStateReader` étendu :

  ```python
  class VirtualWalletStateReader:
      _LAST_KNOWN_TTL_SECONDS: ClassVar[float] = 600.0  # 10 min (= 2× snapshot interval)

      def __init__(self, ...):
          ...
          self._last_known_mid: dict[str, tuple[Decimal, datetime]] = {}

      async def get_state(self) -> WalletState:
          positions = await self._positions_repo.list_open_virtual()
          unrealized = 0.0
          exposure = 0.0
          for pos in positions:
              mid = await self._safe_get_midpoint(pos.asset_id)
              if mid is None:
                  # MD.4 : essayer le last_known frais
                  mid = self._fetch_last_known(pos.asset_id)
                  if mid is None:
                      raise MidpointUnavailableError(
                          asset_id=pos.asset_id,
                          last_known_age_seconds=None,
                      )
              # MD.4 : write-through cache last_known
              self._record_last_known(pos.asset_id, mid)
              current_value = pos.size * float(mid)
              unrealized += current_value - pos.size * pos.avg_price
              exposure += current_value
          ...
  ```

  Helpers `_fetch_last_known(asset_id)` retourne `Decimal | None` selon
  `now() - timestamp <= TTL`. `_record_last_known` overwrite. **TTL 10
  min** = 2× `pnl_snapshot_interval_seconds=300` default.
- `PnlSnapshotWriter._tick` catch :

  ```python
  try:
      state = await self._reader.get_state()
  except MidpointUnavailableError as exc:
      log.warning(
          "pnl_snapshot_skipped_midpoint_unavailable",
          asset_id=exc.asset_id,
          last_known_age_seconds=exc.last_known_age_seconds,
          mode=self._settings.execution_mode,
      )
      return  # skip snapshot, retry au prochain tick
  ```

  Skip propre du tick, le `max_total_usdc` ne progresse pas, le
  drawdown ne se calcule pas contre une valeur creuse. Au retour mid OK,
  comportement nominal reprend.
- **Décision D4** : 10 min TTL = compromis résilience / staleness.
  Justification : `pnl_snapshot_interval_seconds=300` default → 1 skip
  acceptable (5 min de retard sur la mise à jour `max_total_usdc`),
  2 skips = 10 min — au-delà, on préfère raise et signaler explicitement
  que l'outage est prolongé (alerte WARNING `pnl_snapshot_skipped_midpoint_unavailable`
  visible dans `/logs`).

**MD.5 — Fusion `dry_run_virtual_capital_usd` + `dry_run_initial_capital_usd`** :

- **Décision D5** : conserver `DRY_RUN_INITIAL_CAPITAL_USD` (plus
  explicite, pattern M13 preset A/B/C). `DRY_RUN_VIRTUAL_CAPITAL_USD`
  deprecated.
- Validator Pydantic au niveau `Settings` (cf. M10 `DRY_RUN`
  deprecation pattern) :

  ```python
  @model_validator(mode="before")
  @classmethod
  def _migrate_legacy_virtual_capital(cls, data: Any) -> Any:
      """M17 MD.5 — `DRY_RUN_VIRTUAL_CAPITAL_USD` legacy → fallback sur
      `DRY_RUN_INITIAL_CAPITAL_USD` si le nouveau n'est pas set.

      Pattern strict copié de M10 `DRY_RUN=true/false` migration.
      Warning unique au boot, pas de crash.
      """
      if not isinstance(data, dict):
          return data
      legacy = data.get("dry_run_virtual_capital_usd") or os.getenv(
          "DRY_RUN_VIRTUAL_CAPITAL_USD"
      )
      explicit = data.get("dry_run_initial_capital_usd") or os.getenv(
          "DRY_RUN_INITIAL_CAPITAL_USD"
      )
      if legacy is not None and explicit is None:
          warnings.warn(
              "DRY_RUN_VIRTUAL_CAPITAL_USD is deprecated. "
              "Use DRY_RUN_INITIAL_CAPITAL_USD instead.",
              DeprecationWarning,
              stacklevel=2,
          )
          # log structlog visible au boot
          data["dry_run_initial_capital_usd"] = float(legacy)
      return data
  ```
- Au niveau code consommateur :
  - [`virtual_wallet_reader.py:62`](../../src/polycopy/executor/virtual_wallet_reader.py#L62) :
    `total_usdc = (settings.dry_run_initial_capital_usd or
    settings.risk_available_capital_usd_stub) + realized + unrealized`.
  - [`executor/orchestrator.py:110`](../../src/polycopy/executor/orchestrator.py#L110) :
    `virtual_capital=settings.dry_run_initial_capital_usd or
    settings.risk_available_capital_usd_stub`.
  - [`dashboard/queries.py:965-966`](../../src/polycopy/dashboard/queries.py#L965) :
    déjà sur `dry_run_initial_capital_usd` — aucun changement.
- `dry_run_virtual_capital_usd` Pydantic Field gardé comme
  `Field(default=None, deprecated=True)` pour 1 version. Drop programmé
  en M18+.
- `.env.example` :

  ```bash
  # M17 MD.5 — DEPRECATED. Utilise DRY_RUN_INITIAL_CAPITAL_USD à la place.
  # Sera retiré en M18.
  # DRY_RUN_VIRTUAL_CAPITAL_USD=1000

  # Capital initial virtuel pour le PnL dry-run (M8 + M13 + M17).
  # Source unique post-M17. Si non set, fallback sur RISK_AVAILABLE_CAPITAL_USD_STUB.
  # DRY_RUN_INITIAL_CAPITAL_USD=1000
  ```

**MD.6 — `PnlSnapshotDTO.realized_pnl + unrealized_pnl` peuplés** :

- Nouveau helper sur `WalletStateReader` (live) **et**
  `VirtualWalletStateReader` (dry-run/sim) — interface uniforme :

  ```python
  async def get_realized_pnl_cumulative(self, *, mode: Literal["simulation", "dry_run", "live"]) -> float:
      """M17 MD.6 — somme des `MyPosition.realized_pnl` filtrée par mode.

      Live (`mode="live"`) : `simulated.is_(False)`.
      Dry-run + sim : `simulated.is_(True)`.
      """
      filter_simulated = mode != "live"
      ...  # SELECT COALESCE(SUM(realized_pnl), 0.0) WHERE simulated = ? AND closed_at IS NOT NULL
  ```
- `PnlSnapshotWriter._tick` consomme le helper (cf. snippet MD.3 ci-dessus).
  La math `unrealized = total − initial_capital − realized_cumulative`
  est cohérente avec la card `/home` PnL latent M13
  ([queries.py:980-982](../../src/polycopy/dashboard/queries.py#L980-L982))
  → **convergence garantie** entre snapshot et inline-recompute. C-005
  s'évanouit côté UX (les deux source écrivent et lisent le même
  réservoir).
- **Décision D6** : pas de backfill rétroactif des snapshots historiques
  (qui ont `realized_pnl=0.0` hardcodé). Les graphes `/pnl` afficheront
  une discontinuité à la date de migration M17 (jump du plat à la
  vraie courbe). Acceptable — l'utilisateur sait que le bot n'a pas
  réellement été à $0 PnL. Optionnel script
  `scripts/backfill_pnl_snapshot_realized.py` déférable si demande.
- **Note** : H-002 + C-005 partagent la même source de bug (PnL
  hardcodé 0). MD.6 résout les deux en parallèle.

**MD.7 — Kill switch `TraderEvent` audit trail** :

- Migration 0010 (cf. MD.3) inclut le passage `trader_events.wallet_address`
  → `nullable=True`. Justification regroupement : c'est la 2ᵉ migration
  M17, autant la consolider dans le même commit Alembic.
- `TraderEventType` Literal étendu avec `"kill_switch"` à
  [storage/dtos.py:124-152](../../src/polycopy/storage/dtos.py#L124-L152).
- `TraderEventDTO.wallet_address: str | None = None` (default `None` =
  system-level event).
- `TraderEventRepository.insert` adapte le `lower()` :
  ```python
  record = TraderEvent(
      wallet_address=dto.wallet_address.lower() if dto.wallet_address else None,
      ...
  )
  ```
- `PnlSnapshotWriter._maybe_trigger_alerts` étendu (cf. §5.7) :

  ```python
  if drawdown_pct >= threshold:
      log.error("kill_switch_triggered", ...)
      # MD.7 : audit trail AVANT alerte + sentinel + stop.
      await self._events_repo.insert(
          TraderEventDTO(
              wallet_address=None,  # system-level
              event_type="kill_switch",
              event_metadata={
                  "drawdown_pct": drawdown_pct,
                  "total_usdc": total,
                  "max_total_usdc": max_ever,
                  "execution_mode": mode,
                  "threshold": threshold,
              },
          ),
      )
      self._push_alert(Alert(level="CRITICAL", event="kill_switch_triggered", ...))
      if self._sentinel is not None:
          self._sentinel.touch(reason="kill_switch")
      stop_event.set()
      return
  ```
- `_maybe_trigger_alerts` reçoit `events_repo: TraderEventRepository |
  None = None` au constructeur (default `None` pour rétrocompat tests).
  Si `None`, l'`insert_event` est skipped silencieusement (logging
  WARNING `kill_switch_event_repo_missing`). Le caller
  (`MonitoringOrchestrator`) injecte toujours le repo en production.
- `get_pnl_milestones` consomme désormais des events `"kill_switch"`
  non vides → milestone "Kill switch" peuplée dans `/pnl` UX. Aucun
  changement de query side, juste les events qui apparaissent.
- **Attention** : `TraderEventRepository.insert` lance déjà un
  `commit()` court → l'`await` introduit ~5-15 ms de latence sur le
  chemin kill switch. Acceptable (le `stop_event.set()` arrive après ;
  le chemin Telegram est en queue async). Si latence critique observée,
  passer en fire-and-forget avec `asyncio.create_task` (cf. risque §13.4).

**MD.8 — Single-flight TOCTOU sur 3 caches async (optionnel)** :

- Pattern commun :

  ```python
  class _SomeCache:
      def __init__(self, ...) -> None:
          self._cache: dict[str, _Entry] = {}
          self._inflight: dict[str, asyncio.Future[_Value]] = {}
          self._lock = asyncio.Lock()

      async def get(self, key: str) -> _Value:
          # Fast path : cache hit
          entry = self._cache.get(key)
          if entry is not None and not entry.expired():
              return entry.value
          # Slow path : possible single-flight join
          async with self._lock:
              # Re-check under lock (another coroutine may have populated)
              entry = self._cache.get(key)
              if entry is not None and not entry.expired():
                  return entry.value
              fut = self._inflight.get(key)
              if fut is None:
                  fut = asyncio.create_task(self._do_fetch(key))
                  self._inflight[key] = fut
          # Await OUTSIDE the lock to allow concurrent fetches on different keys
          try:
              value = await fut
              self._cache[key] = _Entry(value, datetime.now(tz=UTC))
              return value
          finally:
              # Cleanup AFTER cache write
              async with self._lock:
                  self._inflight.pop(key, None)
  ```
- Appliqué aux 3 sites :
  - [`GammaApiClient.get_market`](../../src/polycopy/strategy/gamma_client.py#L88-L108) :
    `_inflight` keyed par `condition_id`.
  - [`ClobOrderbookReader.get_book`](../../src/polycopy/executor/clob_orderbook_reader.py#L58-L76) :
    `_inflight` keyed par `token_id`.
  - [`ClobMarketWSClient._maybe_subscribe`](../../src/polycopy/strategy/clob_ws_client.py#L237-L268) :
    `_inflight` keyed par `token_id` (pour la 1ʳᵉ subscribe — la
    sub elle-même est idempotente WS-side mais le check du dict
    `_subscribed` est TOCTOU avant le 1ᵉʳ envoi).
- **Optionnel** : MD.8 est explicitement marquée OPTIONNELLE par le
  brief. Si la charge MD est serrée, migrer en MI ou MJ. La gain est
  modeste (5-10× requêtes économisées sur burst de coroutines
  concurrentes) mais réel sur des journées de pic découverte. Le
  bug n'est **pas** observable via crash ; il est observable via
  surcharge réseau / quota Data API.

### 2.2 Hors scope explicites (liste exhaustive)

- ❌ **M-008** N+1 queries dans `get_home_alltime_stats` — migre en
  **MH** (UX + perf dashboard).
- ❌ **M-012** Float → Numeric/Decimal migration sur PnL — trop
  invasif pour M17, spec future si arrondis cumulés deviennent
  mesurables (`MyPosition.realized_pnl`, `PnlSnapshot.total_usdc`).
- ❌ **M-014** validator cross-field `TARGET_WALLETS ∩
  BLACKLISTED_WALLETS` — migre en **MI** ops hygiene.
- ❌ **M-019** `DryRunResolutionWatcher` retry cap neg_risk scalar —
  spec M13 extension, hors scope MD.
- ❌ **H-010 + H-011** RateLimiter + Sentinel TOCTOU
  (`remote_control`) — hors scope M12_bis, migre en MI ou spec future.
- ❌ **H-012** Migration 0007 non-transactionnelle — pattern audit
  historique, migre MI si scope.
- ❌ **Rétro-backfill `pnl_snapshots.realized_pnl`** — script optionnel
  déférable. Acceptable que les graphes `/pnl` aient une discontinuité
  à la date M17 ship.
- ❌ **Rétro-backfill `my_positions.source_wallet_address`** — déjà
  hors scope M15 (cf. M15 §2.2). Les positions M3..M14 ouvertes avant
  migration 0009 conservent `source_wallet_address=NULL`.
- ❌ **Drop `pnl_snapshots.is_dry_run`** — migre en M18+ (1 version
  buffer cohérent M10 `DRY_RUN` deprecation). M17 écrit dans les deux
  colonnes en parallèle.
- ❌ **Drop `dry_run_virtual_capital_usd` setting** — migre en M18+
  (1 version buffer cohérent).
- ❌ **Refonte du calcul `live_pnl` inline** dans
  `get_home_alltime_stats` — MD.6 assure la convergence avec
  `pnl_snapshots.realized_pnl` au niveau snapshot, mais le
  `get_home_alltime_stats` continue de calculer son `live_pnl` inline
  (lecture des fills). C-005 ne disparaît pas par fusion query, il
  disparaît par **convergence des sources** : les deux paths
  consomment le même `MyPosition.realized_pnl` (M13 Bug 5 + MD.6).
- ❌ **Migration `MyPosition` vers tri-state `execution_mode`** —
  M17 garde le boolean `simulated` (compat M3..M16). La distinction
  SIM/DRY se fait au niveau `pnl_snapshots.execution_mode`. Si un
  besoin futur émerge (ex: SIM persistente partagée DB avec
  DRY_RUN), spec dédiée.
- ❌ **Single-flight sur `FeeRateClient` M16** — déjà fait par M16
  (cf. spec M16 §2.1). MD.8 ajoute aux 3 caches restants.
- ❌ **Téléport `ClobReadClient.get_midpoint` cache** — pas un cache,
  appel direct. Pas TOCTOU.
- ❌ **Auto-recovery du sentinel `halt.flag`** : déjà M12_bis Phase D.
  Pas touché par M17.
- ❌ **Réordonner les filtres pipeline strategy** : ordre fixé par
  M2 + M11 + M13 + M16. M17 ne touche pas l'ordre, juste les filtres
  internes des 3 queries.
- ❌ **Dashboard panel "drawdown breakdown par mode"** : la donnée
  existe post-MD.3, l'UX migre en **MH**.

---

## 3. User stories

### 3.1 Story A — MD.1 débloque le flip dry_run → live

**Avant M17** (workflow standard 2026-04-25 → 2026-05-01) :

- Test 14j sur `uni-debian` se termine. Utilisateur set
  `EXECUTION_MODE=live` dans `.env`, ajoute `POLYMARKET_PRIVATE_KEY` +
  `POLYMARKET_FUNDER`, restart bot.
- `WalletPoller` détecte 2 trades source en T+0+30s. Pipeline strategy
  s'exécute :
  - Trade 1 : `0xtrader1 BUY YES sur condX`. `_check_buy` queries
    `MyPosition WHERE condition_id=condX AND closed_at IS NULL`.
    **Sans MD.1** : retourne 1 row (la position virtuelle M13/M16 sur
    le même `condition_id` créée la veille, jamais fermée). →
    `position_already_open` REJECT.
  - Trade 2 : `0xtrader2 BUY YES sur condY`. Idem → REJECT.
- Sur 24h, **0 BUY live envoyés**. L'utilisateur ouvre `/strategie` et
  voit `Approuvées: 0`, `Rejetées: 188 (position_already_open: 188)`.
  Diagnostic difficile : c'est silencieux côté logs.

**Après M17 MD.1** :

- T+0 : flip `EXECUTION_MODE=live`. `simulated_value =
  settings.execution_mode != "live" = False`.
- Trade 1 : `_check_buy` query `MyPosition WHERE condition_id=condX
  AND closed_at IS NULL AND simulated = False`. Retourne 0 row (les
  ~512 positions virtuelles M13 ont `simulated=True`, exclues par le
  filtre). → check passé, sizing fait, RiskManager OK, order envoyé.
- Sur 24h, BUY live nominaux. Aucune intervention manuelle requise.

### 3.2 Story B — MD.2 garantit l'immédiateté du kill switch CRITICAL

**Avant M17** (cas pathologique 5 min) :

- T+0 : `executor_auth_fatal` fire (rotation accidentelle de la clé
  L2). Alerte CRITICAL pousse digest registry pour event `auth_fatal`
  → 1 alerte enregistrée.
- T+10s : `pnl_snapshot_drawdown` fire (warning). Idem digest registry
  pour event `pnl_snapshot_drawdown` → 1 alerte enregistrée.
- T+30s : `kill_switch_triggered` fire (drawdown breach). `level=CRITICAL`,
  `cooldown_key="kill_switch"`, event distinct. Digest registry pour
  event `kill_switch_triggered` → ce serait la 1ʳᵉ alerte de cet event,
  donc digest decision = `emit_immediate` (cf.
  [alert_digest.py:44-63](../../src/polycopy/monitoring/alert_digest.py#L44-L63)).
- ⚠️ MAIS : si `digest_threshold=2` et 2 kill switches consécutifs (re-fire
  à T+35s par bug ou drawdown qui re-breach), le 2ᵉ va être batché
  jusqu'à la fin de fenêtre 5 min. L'utilisateur reçoit l'1ʳᵉ alerte
  immédiate, perd la 2ᵉ pendant 5 min.

**Après M17 MD.2** :

- T+30s : `kill_switch_triggered` fire. `_handle` voit `level="CRITICAL"`
  → bypass digest, envoi immédiat. `cooldown_key="kill_switch"`
  enregistré.
- T+35s : re-fire. `cooldown_key="kill_switch"` last_sent à T+30s, delta
  5s < 60s → throttle (cf. piège §11.4 — anti-flood préservé).
- T+95s : re-fire (drawdown encore breach). `cooldown_key` last_sent à
  T+30s, delta 65s > 60s → envoi immédiat.

L'utilisateur reçoit 1ʳᵉ alerte à T+30s, 2ᵉ alerte à T+95s. Pas de
silence 5 min, pas de flood par seconde.

### 3.3 Story C — MD.3 + MD.4 + MD.6 + MD.7 segregation propre

**Avant M17** (workflow réaliste utilisateur début 2026-05) :

- Lundi : utilisateur run SIMULATION 14j avec
  `dry_run_virtual_capital_usd=$50000` → 250 snapshots écrits avec
  `is_dry_run=True`, `total_usdc` oscille $48k-$52k.
- Mardi : utilisateur set `EXECUTION_MODE=dry_run` à `$1000`, restart.
- T+0+5min : 1ᵉʳ snapshot DRY_RUN. `total_usdc=$998`. `get_max_total_usdc(only_real=False)` →
  `52000` (pollué par les snapshots SIMULATION). `drawdown = (1000 -
  52000) / 52000 = 98%` → kill switch immédiat. Bot stop. Utilisateur
  surpris.

**Après M17 MD.3** :

- Mardi T+0+5min : `get_max_total_usdc(execution_mode="dry_run")` →
  ne renvoie que les snapshots `execution_mode="dry_run"` (vide en
  T+0) → `None` → drawdown = 0%. Snapshot écrit. Pas de kill switch.
- T+1h : `total_usdc=$1010`, max = $1010, drawdown = 0%. Comportement
  nominal.

**Avant M17 MD.4** :

- T+1h30 : panne CLOB `/midpoint` 5xx pendant 8 min. 1 position
  ouverte vaut 0 dans le calcul → `total_usdc = 1000 - 50 = 950`,
  écrit comme nouveau min. Au retour mid OK, `total_usdc = 1010`,
  drawdown = (950 - 1010) / 950 → mais on rentre négatif (pas de
  drawdown). MAIS si l'utilisateur set `KILL_SWITCH_DRAWDOWN_PCT=5%`,
  un futur tick à $1000 → drawdown vs max $1010 = ~1%, ok.
  **Néanmoins** : le min creux à $950 a corrompu la baseline.

**Après M17 MD.4** :

- T+1h30 : panne mid détectée. `_safe_get_midpoint` retourne `None`.
  `_fetch_last_known(asset_id)` retourne le mid d'il y a 5 min →
  utilisé. Tick écrit `total_usdc = 1010 - $0.20 = 1009.80` (drift
  réel uniquement). Pas de creux artificiel.
- À T+11min sans recovery, le `last_known` expire → raise
  `MidpointUnavailableError`. Writer log WARNING, skip ce tick.
  Au retour mid OK, comportement nominal.

**Avant M17 MD.6** :

- Card `/home` "PnL latent" : calcule
  `total_usdc - initial_capital - realized_pnl_total` inline. Cohérent.
- Milestones `/pnl` : graphe `realized_pnl` plat à 0. Utilisateur voit
  une courbe morte.

**Après M17 MD.6** :

- Card `/home` PnL latent : inchangée (déjà inline-correct).
- Milestones `/pnl` : graphe `realized_pnl` cumulatif, monte à mesure
  que les positions se cristallisent (M13 Bug 5 + neg_risk M8 v2).
  **Convergence** : la card `/home` "PnL latent" et le graphe `/pnl`
  "PnL réalisé cumulatif" + "PnL latent" séparés affichent les mêmes
  valeurs avec écart < 1 cent.

**Avant M17 MD.7** :

- T+0+30min : kill switch fire. Telegram OK. Sentinel posé. `stop_event`
  set. **Mais** : milestone `/pnl` "Kill switch" reste vide.
- Lundi suivant, utilisateur ouvre `/pnl`, voit la courbe drawdown qui
  plonge à T+0+30min. Pas d'icône kill switch sur la timeline. Doit
  croiser avec `/logs` pour comprendre.

**Après M17 MD.7** :

- T+0+30min : kill switch fire. `TraderEventDTO(wallet_address=None,
  event_type="kill_switch", event_metadata={...})` insert. Telegram OK.
  Sentinel. stop_event.
- Lundi : `/pnl` affiche un marqueur `🚨 Kill switch` à T+0+30min sur la
  timeline avec tooltip `drawdown_pct=23.4%, total_usdc=$767,
  threshold=20%`. Diagnostic immédiat.

### 3.4 Story D — MD.5 simplification config

**Avant M17** :

- Utilisateur édite `.env`, set `DRY_RUN_VIRTUAL_CAPITAL_USD=2000`.
  Boote le bot. Card `/home` PnL latent affiche `latent = 998 - 1000 -
  0 = -2`. Mais `total_usdc=998` (calculé contre virtual_capital=2000
  côté reader). UX divergente : "j'ai mis 2000 et je vois -2 ?".
- Utilisateur édite aussi `DRY_RUN_INITIAL_CAPITAL_USD=2000`. Restart.
  Maintenant cohérent. Mais l'utilisateur n'aurait pas dû avoir 2 vars
  à set.

**Après M17 MD.5** :

- Utilisateur set `DRY_RUN_INITIAL_CAPITAL_USD=2000` (seul setting).
- Si l'utilisateur set par erreur l'ancien `DRY_RUN_VIRTUAL_CAPITAL_USD=2000`
  uniquement, le validator Pydantic logge un WARNING au boot
  (`config_deprecation_dry_run_virtual_capital_env`) ET le reroute
  vers `dry_run_initial_capital_usd`. Comportement strict.
- `/home` card PnL latent et reader cohérents.

### 3.5 Story E — MD.8 single-flight évite la cascade redundancy

**Avant M17** (cas Discovery boot) :

- Boot : `MetricsCollectorV2.collect()` lance 50 coroutines parallèles
  pour 50 wallets candidats. Chacune appelle
  `gamma_client.get_market(condX)` pour résoudre le `category` de
  chaque condition. Si 30 wallets partagent les mêmes 5 conditions
  trending, on émet **30 requêtes Gamma identiques**, chacune avec un
  TTL cache vide en T+0 boot.
- Quotas Gamma 100 req/min → on consomme 30 req en 5s.

**Après M17 MD.8** :

- Boot : 30 coroutines simultanées sur `condX`. La 1ʳᵉ pose un Future
  dans `_inflight["condX"]`. Les 29 suivantes await le même Future.
  Une seule request HTTP émise.
- Économie ~5-10×. Pas observable côté UX, mais visible dans les
  budgets quotas et latence boot.

---

## 4. Architecture

### 4.1 Flux global M17 (8 sujets)

```
                   ┌──────────────────────────────────────────────────┐
                   │  Pipeline strategy (M2 + M11 + M13 + M16)         │
                   │                                                   │
                   │  TraderLifecycle → Market → EntryPrice →          │
                   │  PositionSizer (M16 fee + M15 probation)          │
                   │      ├─ _check_buy   query MyPosition.simulated◀──MD.1
                   │      ├─ _check_sell  query MyPosition.simulated◀──MD.1
                   │  → SlippageChecker → RiskManager                  │
                   │      └─ check       query MyPosition.simulated◀──MD.1
                   │                                                   │
                   └──────────────────────────────────────────────────┘

                   ┌──────────────────────────────────────────────────┐
                   │  Monitoring (M4 + M7 + M10 + M12_bis Phase D)     │
                   │                                                   │
                   │  PnlSnapshotWriter (M4 + M10)                     │
                   │      _tick                                        │
                   │       ├─ get_state                                │
                   │       │    └─ VirtualWalletStateReader (M8)       │
                   │       │         _last_known_mid TTL 10 min ◀──── MD.4
                   │       │         raise MidpointUnavailableError    │
                   │       │  catch → log WARNING + skip tick          │
                   │       │                                            │
                   │       ├─ get_max_total_usdc(execution_mode=...)◀──MD.3
                   │       ├─ get_realized_pnl_cumulative(mode=...)◀──MD.6
                   │       ├─ build PnlSnapshotDTO                     │
                   │       │    realized_pnl = cumulative ◀────────── MD.6
                   │       │    unrealized_pnl = total - init - real  │
                   │       │    execution_mode = settings.execution_mode ◀ MD.3
                   │       │    is_dry_run = mode != "live" (legacy)   │
                   │       └─ _maybe_trigger_alerts                    │
                   │            if drawdown >= threshold:              │
                   │              insert_event(kill_switch) ◀──────── MD.7
                   │              push Alert CRITICAL                  │
                   │              touch sentinel                       │
                   │              stop_event.set()                     │
                   │                                                   │
                   │  AlertDispatcher (M7)                             │
                   │      _handle                                      │
                   │       ├─ cooldown 60s par cooldown_key (préservé) │
                   │       ├─ if level == "CRITICAL":                  │
                   │       │     bypass digest, send immediate ◀───── MD.2
                   │       └─ else: digest window logic (M7)           │
                   │                                                   │
                   └──────────────────────────────────────────────────┘

                   ┌──────────────────────────────────────────────────┐
                   │  Dashboard queries (M6 + M13)                     │
                   │                                                   │
                   │  /home  HomeAllTimeStats                          │
                   │     PnL latent inline-recompute ◀─────────────── unchanged
                   │     converge avec PnlSnapshot.realized_pnl ──────  MD.6 effet
                   │                                                   │
                   │  /pnl   get_pnl_milestones                        │
                   │     TraderEvent("kill_switch") events visibles ◀ MD.7
                   │                                                   │
                   └──────────────────────────────────────────────────┘

                   ┌──────────────────────────────────────────────────┐
                   │  Storage (M1 + M3 + M4 + M5 + M5_bis + M11 + M15) │
                   │                                                   │
                   │  Migration 0010_pnl_snapshot_execution_mode ◀──── MD.3
                   │     ALTER pnl_snapshots ADD execution_mode (NOT NULL)
                   │     CHECK (execution_mode IN ('simulation', 'dry_run', 'live'))
                   │     UPDATE pnl_snapshots SET execution_mode = ...     (backfill)
                   │     ALTER trader_events ALTER wallet_address NULL ◀── MD.7
                   │                                                   │
                   │  PnlSnapshotRepository                            │
                   │     get_max_total_usdc(execution_mode=...) ◀───── MD.3
                   │     insert(dto.execution_mode, dto.realized_pnl)  │
                   │                                                   │
                   │  TraderEventRepository                            │
                   │     insert(wallet_address=None, ...) ◀────────── MD.7
                   │                                                   │
                   └──────────────────────────────────────────────────┘

                   ┌──────────────────────────────────────────────────┐
                   │  Caches async (optionnel MD.8)                    │
                   │                                                   │
                   │  GammaApiClient.get_market(condition_id)          │
                   │  ClobOrderbookReader.get_book(token_id)           │
                   │  ClobMarketWSClient._maybe_subscribe(token_id)    │
                   │     _inflight: dict[str, asyncio.Future] ◀────── MD.8
                   │                                                   │
                   └──────────────────────────────────────────────────┘
```

### 4.2 Fichiers touchés

Tous les changements sont **additifs** ou **in-place** dans des fichiers
existants. **Deux nouveaux fichiers** : `executor/exceptions.py` +
migration `0010_*.py`.

| Module | Type de changement | Lignes estimées |
|---|---|---|
| [src/polycopy/strategy/pipeline.py](../../src/polycopy/strategy/pipeline.py) | 3 sites : `_check_buy` + `_check_sell` + `RiskManager.check` query `simulated` filter | +6 / -0 |
| [src/polycopy/monitoring/alert_dispatcher.py](../../src/polycopy/monitoring/alert_dispatcher.py) | `_handle` : early-return CRITICAL bypass digest | +14 / -2 |
| [src/polycopy/monitoring/pnl_writer.py](../../src/polycopy/monitoring/pnl_writer.py) | `_tick` peuple realized + unrealized + execution_mode ; `_maybe_trigger_alerts` insert TraderEvent kill_switch | +35 / -8 |
| [src/polycopy/storage/models.py](../../src/polycopy/storage/models.py) | `PnlSnapshot.execution_mode` Mapped + CHECK ; `TraderEvent.wallet_address` nullable | +6 / -2 |
| [src/polycopy/storage/dtos.py](../../src/polycopy/storage/dtos.py) | `PnlSnapshotDTO.execution_mode` ; `TraderEventDTO.wallet_address: str \| None` ; `TraderEventType += "kill_switch"` | +6 / -3 |
| [src/polycopy/storage/repositories.py](../../src/polycopy/storage/repositories.py) | `PnlSnapshotRepository.get_*` accepte `execution_mode: Literal[...] \| None` ; `TraderEventRepository.insert` accepte `wallet_address=None` | +30 / -12 |
| [src/polycopy/executor/virtual_wallet_reader.py](../../src/polycopy/executor/virtual_wallet_reader.py) | `_last_known_mid` + `_record_last_known` + `_fetch_last_known` ; raise `MidpointUnavailableError` | +40 / -6 |
| [src/polycopy/executor/exceptions.py](../../src/polycopy/executor/exceptions.py) | **Nouveau fichier** : `MidpointUnavailableError` | +20 / -0 |
| [src/polycopy/executor/__init__.py](../../src/polycopy/executor/__init__.py) | Réexport `MidpointUnavailableError` | +2 / -0 |
| [src/polycopy/executor/wallet_state_reader.py](../../src/polycopy/executor/wallet_state_reader.py) | Nouveau helper `get_realized_pnl_cumulative(*, mode)` | +20 / -0 |
| [src/polycopy/config.py](../../src/polycopy/config.py) | Validator `_migrate_legacy_virtual_capital` ; deprecation Field | +20 / -0 |
| [src/polycopy/executor/orchestrator.py](../../src/polycopy/executor/orchestrator.py) | virtual_capital source unique | +1 / -1 |
| [.env.example](../../.env.example) | Bloc DEPRECATED comment + bloc M17 | +8 / -0 |
| [alembic/versions/0010_pnl_snapshot_execution_mode.py](../../alembic/versions/0010_pnl_snapshot_execution_mode.py) | **Nouveau fichier** : migration | +90 / -0 |
| [src/polycopy/strategy/gamma_client.py](../../src/polycopy/strategy/gamma_client.py) | (MD.8) `_inflight` single-flight | +25 / -0 |
| [src/polycopy/executor/clob_orderbook_reader.py](../../src/polycopy/executor/clob_orderbook_reader.py) | (MD.8) idem | +25 / -0 |
| [src/polycopy/strategy/clob_ws_client.py](../../src/polycopy/strategy/clob_ws_client.py) | (MD.8) idem | +25 / -0 |
| [CLAUDE.md](../../CLAUDE.md) | §Conventions + §Sécurité — bloc M17 cross-layer | +25 / -0 |
| tests/unit/ | +28 tests (cf. §9) | +700 / -0 |
| tests/integration/ | +3 tests (cf. §9) | +200 / -0 |

### 4.3 Dépendances avec autres milestones

- **M2 + M3** : intacts. Triple garde-fou préservé. `RiskManager` reste
  injecté dans le pipeline post-PositionSizer.
- **M4** : `PnlSnapshotWriter` étendu (3 modifs internes). Pas de
  refactor structurel.
- **M5 + M5_bis + M5_ter** : aucun touch.
- **M7** : `AlertDispatcher` modifié (early-return CRITICAL). Templates
  Telegram inchangés. Le `digest.md.j2` reste utilisé pour les non-CRITICAL.
- **M8** : `VirtualWalletStateReader` étendu (last_known cache).
  4ᵉ garde-fou intact.
- **M10** : `execution_mode` enum déjà en place ; M17 le **propage**
  dans `pnl_snapshots` schéma + filtres queries.
- **M11** : aucun touch.
- **M12_bis Phase D** : sentinel + ordre touch + stop_event intacts.
- **M13** : Bug 5 side-aware préservé. PnL latent card inchangée. M8 v2
  resolution intacte.
- **M14** : aucun touch (scoring v2.1 isolé).
- **M15** : MB.1 + MB.6 + MB.8 paths intacts. La query
  `sum_realized_pnl_by_source_wallet` filtre déjà `simulated`,
  cohérent avec MD.1. M15 audit trail (`auto_blacklisted`,
  `probation_released`) inchangé. `wallet_address` nullable dans
  `trader_events` n'invalide pas les events existants (NOT NULL → NULL
  est élargi, pas un breaking constraint).
- **M16** : `FeeRateClient` + EV after-fee dans `_check_buy` intact.
  MD.1 ajoute le `WHERE simulated = ?` à la query `existing` qui vit
  AVANT le bloc fee. La math fee est sur `raw_my_size` post-cap, pas
  affectée.

---

## 5. Algorithme par item

### 5.1 MD.1 — Filtre `simulated` 3 sites

**Pattern référent** :

```python
simulated_value = self._settings.execution_mode != "live"
stmt = select(MyPosition).where(
    MyPosition.condition_id == ctx.trade.condition_id,  # ou autres conditions
    MyPosition.closed_at.is_(None),
    MyPosition.simulated == simulated_value,  # ◀── M17 MD.1
)
```

Site 1 — [`_check_buy`](../../src/polycopy/strategy/pipeline.py#L186-L192) :

```python
async def _check_buy(self, ctx: PipelineContext) -> FilterResult:
    simulated_value = self._settings.execution_mode != "live"
    async with self._session_factory() as session:
        stmt = select(MyPosition).where(
            MyPosition.condition_id == ctx.trade.condition_id,
            MyPosition.closed_at.is_(None),
            MyPosition.simulated == simulated_value,  # ◀── M17 MD.1
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return FilterResult(passed=False, reason="position_already_open")
    # ... (suite inchangée : raw_size, cap_size, fee/EV M16, probation M15.MB.6)
```

Site 2 — [`_check_sell`](../../src/polycopy/strategy/pipeline.py#L329-L346) :

```python
async def _check_sell(self, ctx: PipelineContext) -> FilterResult:
    simulated_value = self._settings.execution_mode != "live"
    async with self._session_factory() as session:
        stmt = select(MyPosition).where(
            MyPosition.condition_id == ctx.trade.condition_id,
            MyPosition.asset_id == ctx.trade.asset_id,
            MyPosition.closed_at.is_(None),
            MyPosition.simulated == simulated_value,  # ◀── M17 MD.1
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is None:
        return FilterResult(passed=False, reason="sell_without_position")
    # ... (suite inchangée)
```

Site 3 — [`RiskManager.check`](../../src/polycopy/strategy/pipeline.py#L399-L410) :

```python
async def check(self, ctx: PipelineContext) -> FilterResult:
    if ctx.my_size is None or ctx.midpoint is None:
        return FilterResult(passed=False, reason="risk_inputs_missing")
    simulated_value = self._settings.execution_mode != "live"
    async with self._session_factory() as session:
        stmt = select(MyPosition).where(
            MyPosition.closed_at.is_(None),
            MyPosition.simulated == simulated_value,  # ◀── M17 MD.1
        )
        open_positions = list((await session.execute(stmt)).scalars().all())
    # ... (suite inchangée : current_exposure + prospective_cost)
```

**Note de cohérence avec MB.1 / MB.8** :
[`MyPositionRepository.sum_realized_pnl_by_source_wallet`](../../src/polycopy/storage/repositories.py#L864)
filtre déjà `MyPosition.simulated.is_(simulated)` selon le mode passé en
argument. M17 MD.1 propage le **même** pattern aux 3 queries
strategy/risk qui ne le faisaient pas. Aucune divergence d'API : MB.1
attend toujours `simulated: bool` explicite côté caller (le caller
M15 calcule `simulated = (settings.execution_mode != "live")` au site
d'appel), M17 fait pareil.

**Vérification non-régression** : un grep `MyPosition.closed_at.is_(None)`
dans `src/polycopy/` doit retourner exactement les 3 queries listées +
`MyPositionRepository.list_open_virtual` (déjà filtré `simulated.is_(True)`)
+ `MyPositionRepository.sum_realized_pnl_virtual` (idem) + les paths
d'écriture (`_persist_*`) qui n'ont pas de `simulated` filter
(comportement attendu — ils écrivent une row ciblée). 4 sites total à
auditer manuellement.

### 5.2 MD.2 — Bypass digest CRITICAL

**Logique stricte** : court-circuit avant `digest.register`. Cooldown
préservé.

```python
async def _handle(self, alert: Alert) -> None:
    self._counts_since_boot[alert.event] += 1
    if alert.level in _CRITICAL_LEVELS:
        self._last_critical_at = self._now()

    # Cooldown : appliqué dans tous les cas (idempotence anti-flood)
    if alert.cooldown_key is not None:
        now = self._now()
        last = self._last_sent.get(alert.cooldown_key)
        if last is not None and (now - last).total_seconds() < self._cooldown_seconds:
            log.debug("alert_throttled", cooldown_key=alert.cooldown_key, alert_event=alert.event, level=alert.level)
            return
        self._last_sent[alert.cooldown_key] = now

    # MD.2 : bypass digest pour CRITICAL
    if alert.level == "CRITICAL":
        formatted = self._renderer.render_alert(alert)
        sent = await self._telegram.send(formatted)
        log.info(
            "alert_sent_critical_bypass_digest",
            alert_event=alert.event,
            level=alert.level,
            sent=sent,
        )
        return

    decision = self._digest.register(alert, self._now())
    self._digest_buffer[alert.event].append(alert)
    # ... (logique M7 inchangée pour non-CRITICAL)
```

**Décisions** :

- **Pourquoi cooldown préservé** : cf. piège §11.4. Sans cooldown,
  un kill_switch qui re-fire à chaque tick (5s en cas de bug
  `pnl_snapshot_interval_seconds=5`) provoque flood. Avec cooldown 60s
  : la 1ʳᵉ alerte passe immédiate, les suivantes 60s espacées.
- **Pourquoi cette signature `level == "CRITICAL"`** : le set
  `_CRITICAL_LEVELS = {"CRITICAL", "ERROR"}` existe déjà côté
  dispatcher pour `_last_critical_at`. M17 MD.2 short-circuit
  uniquement `level == "CRITICAL"` (pas "ERROR") : un ERROR reste
  digestible (cas "executor_pipeline_error" non bloquant) tandis
  qu'un CRITICAL est urgent.
- **Pourquoi pas de cooldown bypass** : couvert par §11.4. Bypass = flood
  potentiel, pire qu'un retard digest 5 min.

### 5.3 MD.3 — Migration 0010 + `execution_mode` segregation

Walkthrough complet en §11. Snippet `_tick` (cf. §2.1 MD.3) :

```python
mode = self._settings.execution_mode
max_ever = await self._repo.get_max_total_usdc(execution_mode=mode)
drawdown_pct = self._compute_drawdown_pct(max_ever, total)
```

`PnlSnapshotRepository.get_max_total_usdc` :

```python
async def get_max_total_usdc(
    self,
    *,
    execution_mode: Literal["simulation", "dry_run", "live"] | None = None,
    only_real: bool | None = None,  # legacy 1 version
) -> float | None:
    """Retourne le max historique de ``total_usdc``.

    M17 MD.3 : nouveau paramètre `execution_mode` (strict). Si fourni,
    filtre `WHERE execution_mode = ?`. Si None, comportement legacy
    (no filter ou `is_dry_run` selon `only_real`).

    `only_real` reste accepté 1 version pour rétrocompat tests M4..M16
    avec warning de deprecation.
    """
    if only_real is not None and execution_mode is None:
        warnings.warn(
            "PnlSnapshotRepository.get_max_total_usdc(only_real=...) is "
            "deprecated. Use execution_mode='live' or execution_mode='dry_run'.",
            DeprecationWarning,
            stacklevel=2,
        )
        execution_mode = "live" if only_real else None  # legacy SIM+DRY bucket

    async with self._session_factory() as session:
        stmt = select(func.max(PnlSnapshot.total_usdc))
        if execution_mode is not None:
            stmt = stmt.where(PnlSnapshot.execution_mode == execution_mode)
        result = await session.execute(stmt)
        value = result.scalar_one_or_none()
        return float(value) if value is not None else None
```

Idem `get_latest` et `list_since`.

### 5.4 MD.4 — `VirtualWalletStateReader` last_known + exception

**Implémentation complète** :

```python
from datetime import datetime, timedelta, UTC
from decimal import Decimal
from typing import ClassVar

from polycopy.executor.exceptions import MidpointUnavailableError


class VirtualWalletStateReader:
    _LAST_KNOWN_TTL_SECONDS: ClassVar[float] = 600.0  # 10 min = 2× snapshot interval default

    def __init__(self, ...) -> None:
        ...
        self._last_known_mid: dict[str, tuple[float, datetime]] = {}

    async def get_state(self) -> WalletState:
        positions = await self._positions_repo.list_open_virtual()
        unrealized = 0.0
        exposure = 0.0
        for pos in positions:
            mid = await self._safe_get_midpoint(pos.asset_id)
            if mid is None:
                # MD.4 : essaye le last_known frais
                last_known = self._fetch_last_known(pos.asset_id)
                if last_known is None:
                    # Last-known absent ou stale → raise pour signaler le skip
                    age = self._last_known_age_seconds(pos.asset_id)
                    log.warning(
                        "virtual_wallet_midpoint_stale_last_known",
                        asset_id=pos.asset_id,
                        last_known_age_seconds=age,
                    )
                    raise MidpointUnavailableError(
                        asset_id=pos.asset_id,
                        last_known_age_seconds=age,
                    )
                mid = last_known
                log.info(
                    "virtual_wallet_using_last_known_mid",
                    asset_id=pos.asset_id,
                    mid=mid,
                )
            else:
                self._record_last_known(pos.asset_id, mid)
            current_value = pos.size * mid
            unrealized += current_value - pos.size * pos.avg_price
            exposure += current_value
        realized = await self._positions_repo.sum_realized_pnl_virtual()
        initial = float(
            self._settings.dry_run_initial_capital_usd
            or self._settings.risk_available_capital_usd_stub
        )
        total_usdc = initial + realized + unrealized
        return WalletState(
            total_position_value_usd=exposure,
            available_capital_usd=total_usdc - exposure,
            open_positions_count=len(positions),
        )

    def _record_last_known(self, asset_id: str, mid: float) -> None:
        self._last_known_mid[asset_id] = (mid, datetime.now(tz=UTC))

    def _fetch_last_known(self, asset_id: str) -> float | None:
        entry = self._last_known_mid.get(asset_id)
        if entry is None:
            return None
        mid, recorded_at = entry
        age = (datetime.now(tz=UTC) - recorded_at).total_seconds()
        if age > self._LAST_KNOWN_TTL_SECONDS:
            # TTL expiré : on ne sert pas la valeur (mais on ne purge pas
            # non plus, le prochain `_record_last_known` overwrite proprement)
            return None
        return mid

    def _last_known_age_seconds(self, asset_id: str) -> float | None:
        entry = self._last_known_mid.get(asset_id)
        if entry is None:
            return None
        return (datetime.now(tz=UTC) - entry[1]).total_seconds()
```

`PnlSnapshotWriter._tick` :

```python
async def _tick(self, stop_event: asyncio.Event) -> None:
    try:
        state = await self._reader.get_state()
    except MidpointUnavailableError as exc:
        log.warning(
            "pnl_snapshot_skipped_midpoint_unavailable",
            asset_id=exc.asset_id,
            last_known_age_seconds=exc.last_known_age_seconds,
            mode=self._settings.execution_mode,
        )
        return  # skip ce tick
    # ... (suite normal)
```

### 5.5 MD.5 — Validator config deprecation

```python
@model_validator(mode="before")
@classmethod
def _migrate_legacy_virtual_capital(cls, data: Any) -> Any:
    """M17 MD.5 — `DRY_RUN_VIRTUAL_CAPITAL_USD` deprecated → reroute vers
    `DRY_RUN_INITIAL_CAPITAL_USD` si le nouveau n'est pas set.

    Pattern strict copié de M10 `_migrate_legacy_dry_run_flag`.
    """
    if not isinstance(data, dict):
        return data
    legacy_value = data.get("dry_run_virtual_capital_usd")
    if legacy_value is None:
        legacy_value = os.getenv("DRY_RUN_VIRTUAL_CAPITAL_USD")
    explicit_value = data.get("dry_run_initial_capital_usd")
    if explicit_value is None:
        explicit_value = os.getenv("DRY_RUN_INITIAL_CAPITAL_USD")
    if legacy_value is not None and explicit_value is None:
        warnings.warn(
            "DRY_RUN_VIRTUAL_CAPITAL_USD is deprecated. "
            "Use DRY_RUN_INITIAL_CAPITAL_USD instead. Will be removed in M18.",
            DeprecationWarning,
            stacklevel=2,
        )
        # Visible dans le log boot via structlog
        data["dry_run_initial_capital_usd"] = float(legacy_value)
    return data
```

Le Field `dry_run_virtual_capital_usd` est marqué deprecated mais
conservé pour 1 version (lecture compat tests M4..M16).

### 5.6 MD.6 — `realized_pnl + unrealized_pnl` peuplés

`WalletStateReader` (live) helper :

```python
async def get_realized_pnl_cumulative(
    self,
    *,
    mode: Literal["simulation", "dry_run", "live"],
) -> float:
    """M17 MD.6 — somme des realized_pnl filtré par mode.

    Live (`mode="live"`) : `simulated.is_(False)`.
    Dry-run / sim : `simulated.is_(True)`.
    """
    filter_simulated = mode != "live"
    async with self._session_factory() as session:
        stmt = select(func.coalesce(func.sum(MyPosition.realized_pnl), 0.0)).where(
            MyPosition.simulated.is_(filter_simulated),
            MyPosition.closed_at.is_not(None),
        )
        result = await session.execute(stmt)
        value = result.scalar_one()
        return float(value) if value is not None else 0.0
```

`VirtualWalletStateReader` partage la même signature (helper symétrique
sur le même repo). Le `PnlSnapshotWriter` injecté avec
`reader: WalletStateReader | VirtualWalletStateReader` peut appeler
`get_realized_pnl_cumulative(mode=mode)` sans branchement.

`_tick` :

```python
realized_cumulative = await self._reader.get_realized_pnl_cumulative(mode=mode)
initial_capital = float(
    self._settings.dry_run_initial_capital_usd
    or self._settings.risk_available_capital_usd_stub
)
unrealized_pnl = total - initial_capital - realized_cumulative
```

`PnlSnapshotDTO` + insert : cf. §2.1 MD.3 snippet.

**Convergence `/home`** : `get_home_alltime_stats` continue de calculer
`live_pnl` inline (lecture des fills FILLED). Avec MD.6 cohérent dans
`PnlSnapshot`, les deux sources convergent à < 1 cent près. La
divergence C-005 historique vient du fait que `pnl_snapshots.realized_pnl=0`
hardcodé ne convergait avec rien — MD.6 rétablit la cohérence.

### 5.7 MD.7 — Kill switch `TraderEvent` audit

`PnlSnapshotWriter` constructeur :

```python
def __init__(
    self,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    wallet_state_reader: WalletStateReader | VirtualWalletStateReader,
    alerts_queue: asyncio.Queue[Alert],
    *,
    sentinel: SentinelFile | None = None,
    events_repo: TraderEventRepository | None = None,  # ◀── M17 MD.7
) -> None:
    ...
    self._events_repo = events_repo
```

`_maybe_trigger_alerts` :

```python
async def _maybe_trigger_alerts(
    self,
    total: float,
    drawdown_pct: float,
    max_ever: float | None,
    stop_event: asyncio.Event,
) -> None:
    threshold = self._settings.kill_switch_drawdown_pct
    mode = self._settings.execution_mode
    if drawdown_pct >= threshold:
        log.error(
            "kill_switch_triggered",
            mode=mode,
            drawdown_pct=drawdown_pct,
            threshold=threshold,
            total_usdc=total,
        )
        # M17 MD.7 : audit trail AVANT alerte/sentinel/stop.
        if self._events_repo is not None:
            try:
                await self._events_repo.insert(
                    TraderEventDTO(
                        wallet_address=None,  # system-level event
                        event_type="kill_switch",
                        event_metadata={
                            "drawdown_pct": drawdown_pct,
                            "total_usdc": total,
                            "max_total_usdc": max_ever,
                            "execution_mode": mode,
                            "threshold": threshold,
                        },
                    ),
                )
                log.info("kill_switch_event_recorded")
            except Exception:
                log.exception("kill_switch_event_insert_failed")
                # On continue : Telegram + sentinel + stop_event restent.
        else:
            log.warning("kill_switch_event_repo_missing")

        self._push_alert(
            Alert(
                level="CRITICAL",
                event="kill_switch_triggered",
                body=(
                    f"Kill switch — drawdown {drawdown_pct:.2f}% "
                    f">= seuil {threshold:.2f}%. "
                    f"total_usdc={total:.2f}. Stop du bot."
                ),
                cooldown_key="kill_switch",
            ),
        )
        # M12_bis Phase D §4.6 : touch sentinel AVANT stop_event.set()
        # — ordre critique préservé.
        if self._sentinel is not None:
            self._sentinel.touch(reason="kill_switch")
        stop_event.set()
        return
    # ... (warning logic inchangée)
```

L'`insert_event` est protégé par try/except large : si l'écriture DB
échoue (rare, DB lock), le kill switch fire quand même via Telegram +
sentinel + stop_event. L'audit trail manquant est logué mais
non-bloquant.

`MonitoringOrchestrator` (caller) injecte le repo au boot :

```python
events_repo = TraderEventRepository(session_factory)
pnl_writer = PnlSnapshotWriter(
    session_factory=session_factory,
    settings=settings,
    wallet_state_reader=wallet_state_reader,
    alerts_queue=alerts_queue,
    sentinel=sentinel,
    events_repo=events_repo,  # ◀── M17 MD.7
)
```

### 5.8 MD.8 — Single-flight pattern (optionnel)

Cf. §2.1 MD.8 snippet. Application stricte aux 3 sites listés. Tests :
mock 10 coroutines concurrentes sur même clé, vérifier 1 seul fetch
HTTP. Cf. §9.

---

## 6. DTOs et schéma DB

### 6.1 `PnlSnapshotDTO` (post-MD.3 + MD.6)

```python
class PnlSnapshotDTO(BaseModel):
    """Snapshot PnL prêt pour insertion en base. Écrit par PnlSnapshotWriter (M4)."""

    model_config = ConfigDict(frozen=True)

    total_usdc: float
    realized_pnl: float                           # ◀── M17 MD.6 (était 0.0 hardcodé)
    unrealized_pnl: float                         # ◀── M17 MD.6 (était 0.0 hardcodé)
    drawdown_pct: float
    open_positions_count: int
    cash_pnl_total: float | None
    is_dry_run: bool                              # legacy, conservé 1 version
    execution_mode: Literal["simulation", "dry_run", "live"]  # ◀── M17 MD.3
```

### 6.2 `TraderEventDTO` + `TraderEventType` (post-MD.7)

```python
TraderEventType = Literal[
    "discovered",
    "scored",
    "promoted_active",
    "demoted_paused",
    "kept",
    "skipped_blacklist",
    "skipped_cap",
    "manual_override",
    "revived_shadow",
    "gate_rejected",
    "promoted_active_via_eviction",
    "demoted_to_sell_only",
    "eviction_aborted",
    "promoted_active_via_rebound",
    "eviction_completed_to_shadow",
    "eviction_deferred_one_per_cycle",
    "eviction_deferred_sell_only_cap",
    "blacklisted",
    "blacklist_removed",
    "demoted_to_shadow",
    "probation_released",
    "auto_blacklisted",
    "kill_switch",  # ◀── M17 MD.7 — system-level (wallet_address=None)
]


class TraderEventDTO(BaseModel):
    model_config = ConfigDict(frozen=True)

    wallet_address: str | None = None  # ◀── M17 MD.7 (était str obligatoire)
    event_type: TraderEventType
    from_status: str | None = None
    to_status: str | None = None
    score_at_event: float | None = None
    scoring_version: str | None = None
    reason: str | None = None
    event_metadata: dict[str, Any] | None = None
```

### 6.3 `PnlSnapshot` ORM (post-migration 0010)

```python
class PnlSnapshot(Base):
    __tablename__ = "pnl_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, index=True, nullable=False)
    total_usdc: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    drawdown_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    open_positions_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cash_pnl_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # legacy 1 version
    execution_mode: Mapped[str] = mapped_column(  # ◀── M17 MD.3
        String(16),
        nullable=False,
        server_default=sa.text("'live'"),
    )

    __table_args__ = (
        CheckConstraint(
            "execution_mode IN ('simulation', 'dry_run', 'live')",
            name="ck_pnl_snapshots_execution_mode",
        ),
    )
```

### 6.4 `TraderEvent` ORM (post-migration 0010 MD.7)

```python
class TraderEvent(Base):
    __tablename__ = "trader_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet_address: Mapped[str | None] = mapped_column(  # ◀── M17 MD.7 (NULL → system events)
        String(42),
        index=True,
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc, index=True, nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    score_at_event: Mapped[float | None] = mapped_column(Float, nullable=True)
    scoring_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    event_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (Index("ix_trader_events_wallet_at", "wallet_address", "at"),)
```

L'index composite reste utilisable même avec NULL — SQLite accepte NULL
dans un index, et les queries `WHERE wallet_address = '0x...'` continuent
de profiter de l'index. Les events système (`wallet_address=NULL`)
restent rares (~0-1 par jour en pratique).

---

## 7. Settings (config Pydantic + env)

Aucun **nouveau setting** introduit par M17. Diff :

- `dry_run_virtual_capital_usd` : marqué deprecated (cf. §5.5 validator).
  Default reste `1000.0` pour 1 version compat. Sera retiré en M18+.
- `dry_run_initial_capital_usd` : default reste `None` (cohérent M13).
  Validator MD.5 le populate depuis `DRY_RUN_VIRTUAL_CAPITAL_USD` legacy
  si non set explicitement.

Aucun bump des défauts existants. M17 est un fix audit, pas une feature.

`.env.example` :

```bash
# ── M17 MD.5 — Capital initial dry-run (source unique post-M17) ─────
# Source unique pour le PnL latent dry-run (M8 + M13 + M17). Si non
# set, fallback sur RISK_AVAILABLE_CAPITAL_USD_STUB.
# DRY_RUN_INITIAL_CAPITAL_USD=1000

# DEPRECATED M17 — utilise DRY_RUN_INITIAL_CAPITAL_USD à la place.
# Sera retiré en M18. Si set sans le nouveau, validator Pydantic
# logge un warning et reroute la valeur.
# DRY_RUN_VIRTUAL_CAPITAL_USD=1000
```

---

## 8. Logs structurés

Nouveaux events structlog M17 :

| Event | Niveau | Contexte | Module |
|---|---|---|---|
| `alert_sent_critical_bypass_digest` | INFO | `alert_event`, `level`, `sent` | `monitoring/alert_dispatcher.py` |
| `alert_throttled` | DEBUG | `cooldown_key`, `alert_event`, `level` | `monitoring/alert_dispatcher.py` (étendu : ajout `level`) |
| `pnl_snapshot_skipped_midpoint_unavailable` | WARNING | `asset_id`, `last_known_age_seconds`, `mode` | `monitoring/pnl_writer.py` |
| `virtual_wallet_using_last_known_mid` | INFO | `asset_id`, `mid` | `executor/virtual_wallet_reader.py` |
| `virtual_wallet_midpoint_stale_last_known` | WARNING | `asset_id`, `last_known_age_seconds` | `executor/virtual_wallet_reader.py` |
| `kill_switch_event_recorded` | INFO | (vide — meta dans event_metadata DB) | `monitoring/pnl_writer.py` |
| `kill_switch_event_insert_failed` | EXCEPTION | (auto trace) | `monitoring/pnl_writer.py` |
| `kill_switch_event_repo_missing` | WARNING | (vide) | `monitoring/pnl_writer.py` |
| `config_deprecation_dry_run_virtual_capital_env` | WARNING (boot one-shot) | `value`, `redirected_to` | `config.py` validator |

**Aucun secret loggé** : tous les events ne contiennent que des
floats / strings publiques (asset_id, mode, cooldown_key — pas de
private key, pas de Telegram token, pas de creds CLOB L2). Vérifié
par `test_m17_no_secret_leak.py` (grep automatisé sur les nouveaux
events).

**M10 hygiene preservation** : aucun nouvel event ne génère de
`dashboard_request` (les nouveaux events vivent en monitoring/executor,
pas dashboard). Le filter `filter_noisy_endpoints` M10 reste opérant.

---

## 9. Tests

Tests **unitaires ciblés** (~28) + **3 tests d'intégration**.

### 9.1 MD.1 — Filtre `simulated` (4 tests)

- `test_position_sizer_check_buy_filters_simulated_in_live_mode` :
  positions virtuelles préexistantes → `_check_buy` n'est pas bloqué
  en `EXECUTION_MODE=live`.
- `test_position_sizer_check_sell_filters_simulated_in_live_mode` :
  même assertion sur le path SELL.
- `test_risk_manager_check_filters_simulated_in_live_mode` : exposition
  calculée seulement sur les positions live.
- `test_dry_run_still_sees_only_virtual_positions` :
  `EXECUTION_MODE=dry_run`, positions live préexistantes (cas
  hypothétique) → invisibles aux 3 queries.

### 9.2 MD.2 — Bypass digest CRITICAL (4 tests)

- `test_critical_alert_bypasses_digest_window` : N alertes CRITICAL en
  rafale de 5s → toutes envoyées immédiatement (cooldown 0 dans le
  test) ; pas de `digest.register` enregistré pour ces alertes.
- `test_critical_alert_respects_cooldown` : 2 alertes CRITICAL même
  `cooldown_key` à T+0 et T+30s avec cooldown_seconds=60 → la 2ᵉ est
  drop. À T+65s → la 3ᵉ passe.
- `test_non_critical_alert_still_digested` : niveau WARNING → digest
  logic préservée.
- `test_critical_alert_telegram_disabled_no_crash` :
  `telegram_client.enabled=False` → log + return sans raise.

### 9.3 MD.3 — Migration 0010 + segregation (5 tests)

- `test_migration_0010_upgrade_creates_execution_mode_column` :
  `alembic upgrade head` → schema attendu présent.
- `test_migration_0010_backfill_correct` : populate 5 rows
  `is_dry_run=True` + 5 rows `is_dry_run=False` avant migration ;
  après upgrade, 5 ont `execution_mode='dry_run'`, 5 ont
  `execution_mode='live'`.
- `test_migration_0010_downgrade_idempotent` : `alembic upgrade head`
  → `alembic downgrade -1` → `alembic upgrade head` → schéma identique
  après les 3 ops.
- `test_get_max_total_usdc_segregates_by_mode` : insert 3 modes,
  query par mode → seul le mode demandé.
- `test_pnl_snapshot_writes_execution_mode` : `_tick` écrit le mode
  correct selon `settings.execution_mode`.

### 9.4 MD.4 — `last_known_mid` (4 tests)

- `test_virtual_wallet_records_last_known_on_success` : 1 fetch OK →
  dict peuplé.
- `test_virtual_wallet_uses_last_known_mid_on_transient_none` :
  fetch_1 OK, fetch_2 None, last_known frais → fetch_2 utilise
  last_known.
- `test_virtual_wallet_raises_on_mid_outage_exceeding_10min` :
  last_known TTL > 600s → `MidpointUnavailableError`.
- `test_pnl_writer_skips_snapshot_on_midpoint_unavailable` : raise →
  log WARNING, pas d'insert, retourne propre.

### 9.5 MD.5 — Config deprecation (3 tests)

- `test_dry_run_initial_capital_is_single_source_of_truth` : seul le
  nouveau set → fonctionne.
- `test_deprecation_warning_logged_on_legacy_virtual_capital_var` :
  legacy set, nouveau absent → warning + reroute.
- `test_legacy_fallback_disabled_if_new_explicit` : les deux set →
  nouveau gagne, pas de warning.

### 9.6 MD.6 — Realized + unrealized peuplés (3 tests)

- `test_pnl_snapshot_populates_realized_pnl_nonzero_when_positions_closed` :
  positions closed avec realized non-zero → DTO réel.
- `test_pnl_snapshot_unrealized_matches_formula` : `unrealized = total
  - initial - realized`.
- `test_dashboard_consumes_pnl_snapshot_values_not_inline_recompute` :
  régression /home et /performance convergent sur la même valeur
  realized_pnl_total à < 1 cent.

### 9.7 MD.7 — Kill switch event (3 tests)

- `test_kill_switch_writes_trader_event_system_level` : kill switch →
  `TraderEvent(wallet_address=None, event_type="kill_switch",
  event_metadata={...})` insert.
- `test_pnl_milestones_includes_kill_switch_events` :
  `get_pnl_milestones` retourne le marker.
- `test_kill_switch_order_strict` : insert_event AVANT push_alert AVANT
  touch_sentinel AVANT stop_event.set() (vérifié via mock séquence).

### 9.8 MD.8 — Single-flight (3 tests, optionnel)

- `test_gamma_client_single_flight_prevents_redundant_fetches` : 10
  coroutines concurrentes sur `condX` → 1 seul HTTP request emitted.
- `test_clob_orderbook_reader_single_flight` : idem 10 coroutines
  concurrentes sur `tokenY` → 1 seul fetch.
- `test_clob_ws_client_subscription_single_flight` : 10 coroutines
  concurrentes appelant `_maybe_subscribe(tokenZ)` → 1 seul
  envoi WS sub.

### 9.9 Tests d'intégration (3 tests)

- `test_flip_dry_run_to_live_no_buy_blocked_by_virtual_positions` :
  E2E. Setup : DB avec 50 positions virtuelles M13 ouvertes
  (`simulated=True`). Flip `EXECUTION_MODE=live`. Inject 10 trades
  source. Pipeline → 10 BUY APPROVED.
- `test_kill_switch_immediate_under_alert_pressure` : E2E. Setup :
  digest fenêtre avec 10 alertes routine non-CRITICAL en buffer.
  Inject kill_switch CRITICAL → reçu Telegram en < 100 ms.
  `stop_event.is_set()` < 100 ms après le push.
- `test_dashboard_home_performance_realized_pnl_convergence` : E2E.
  Setup : 20 positions closed avec realized varié. Tick PnL writer
  → `/home` PnL latent et `/performance` total realized convergent à
  < 1 cent.

### 9.10 Total tests

- **Unit** : ~28 (MD.1=4, MD.2=4, MD.3=5, MD.4=4, MD.5=3, MD.6=3,
  MD.7=3, MD.8=3 optionnel donc 25 si MD.8 reporté).
- **Intégration** : 3.
- **Couverture** : strategy `pipeline.py` reste >= 85%, monitoring
  `pnl_writer.py` + `alert_dispatcher.py` >= 90%, executor
  `virtual_wallet_reader.py` >= 85%.

---

## 10. Sécurité — revue invariants

### 10.1 Triple garde-fou M3 + 4ᵉ M8 (intacts)

M17 ne touche aucun chemin de signature CLOB ni `_persist_realistic_simulated`.
Vérification :

- `lazy_init ClobClient` (M3 §1) : path inchangé. MD.1 ne touche pas
  l'executor.
- `RuntimeError` boot si `live` + creds absentes (M3 §2) : intact.
- `assert execution_mode == "live"` avant POST CLOB (M3 §3) : intact.
- `WalletStateReader.refetch_before_post` (M3 §4) : intact. MD.6 ajoute
  `get_realized_pnl_cumulative` qui ne traverse pas le path POST.
- `assert execution_mode == "dry_run"` avant `_persist_realistic_simulated`
  (M8 §1.4 garde-fou 4) : intact.

### 10.2 Kill switch parité 3 modes M10 (renforcé)

M17 **renforce** l'invariant :

- MD.2 : CRITICAL bypass digest → l'alerte ne peut plus être retardée
  par la fenêtre 5 min. Parité dry-run/live améliorée (en live aussi
  on bénéficie de l'immédiateté).
- MD.3 : segregation par mode → un kill switch en dry-run ne lit pas
  les baselines SIM, un kill switch en live ne lit pas les baselines
  dry-run. Ségrégation propre, plus de pollution cross-mode.
- MD.7 : audit trail propre → diagnostic post-mortem immédiat sur
  `/pnl` et `/logs`, dans les 3 modes.

### 10.3 Sentinel halt.flag M12_bis Phase D (intact)

Permissions 0o600 fichier + 0o700 parent : intactes. Ordre touch
`sentinel → stop_event.set()` strictement préservé. MD.7 ajoute
`insert_event` AVANT le touch sentinel — l'ordre devient :

```
1. insert_event(kill_switch)        ◀── M17 MD.7 (nouveau)
2. push_alert(kill_switch_triggered)
3. touch sentinel                    ◀── M12_bis Phase D
4. stop_event.set()                  ◀── M12_bis Phase D (toujours dernier)
```

`stop_event.set()` reste **strictement la dernière étape**.

### 10.4 Aucune nouvelle creds consommée

M17 reste 100% read-only côté API publique :

- MD.1 : pure SQL local.
- MD.2 : pure logique Python in-process.
- MD.3 : migration Alembic SQL local.
- MD.4 : `_safe_get_midpoint` consume déjà `ClobReadClient.get_midpoint`
  read-only public M2 (no auth). Aucune nouvelle creds.
- MD.5 : pure config Pydantic local.
- MD.6 : pure SQL local (lecture `MyPosition.realized_pnl`).
- MD.7 : pure SQL local (insert `trader_events`).
- MD.8 : pure refactoring cache async.

### 10.5 Append-only scoring versions

M17 ne touche pas les fonctions `compute_score_v2_1` / `compute_score_v2_1_1`
ni le `SCORING_VERSIONS_REGISTRY`. Aucune row `trader_scores` réécrite.
MD.6 alimente `pnl_snapshots.realized_pnl` au niveau monitoring, **hors
couche scoring**.

### 10.6 Migration 0010 data integrity

- Backfill idempotent : la migration upgrade vérifie qu'`execution_mode`
  n'est pas déjà set avant de backfiller (rollback puis re-upgrade =
  no-op).
- Rollback propre : `downgrade()` drop `execution_mode` sans toucher
  `is_dry_run` (préservé) → restaure schéma 0009 strict.
- `trader_events.wallet_address` NULL → la migration relâche
  `nullable=False` à `nullable=True` ; downgrade restaure NOT NULL
  uniquement si **aucune row n'a `wallet_address=NULL`** (sinon la
  downgrade fail avec message clair). Cf. §11.

### 10.7 Aucun secret loggé

Vérifié par `test_m17_no_secret_leak.py` (grep automatisé) :

- Les 9 nouveaux events structlog n'incluent que numeric / string
  publique.
- Les 2 nouveaux fichiers source (`exceptions.py`, migration `0010_*.py`)
  ne contiennent aucun pattern secret.
- Le validator MD.5 logge la valeur `legacy_value` (capital, montant
  USD entier) — non secret.

---

## 11. Migration Alembic 0010 — walkthrough complet

### 11.1 Fichier `alembic/versions/0010_pnl_snapshot_execution_mode.py`

```python
"""M17 cross-layer integrity — pnl_snapshots.execution_mode + trader_events.wallet_address nullable.

Bundle 2 changements structurels MD.3 + MD.7 :

- ``pnl_snapshots.execution_mode`` (NOT NULL, default 'live') : segregation
  des baselines drawdown par mode (audit C-003). Backfill in-place depuis
  ``is_dry_run`` (1=dry_run, 0=live).
- ``trader_events.wallet_address`` : passe NOT NULL → NULL pour autoriser
  les events système (audit H-005 — kill switch). Le NULL signale un event
  non-attaché à un wallet spécifique.

Migration **strictement additive + relaxation contrainte**. Defaults safe :
- Rows existantes ``pnl_snapshots`` backfillées via ``UPDATE`` SQL pendant
  l'upgrade, en cohérence avec le flag ``is_dry_run`` historique.
- Rows existantes ``trader_events`` conservent leur ``wallet_address``
  non-NULL (la relaxation n'invalide pas les rows existantes).

SQLite-friendly via ``batch_alter_table`` (cohérent migrations 0007 / 0009).

Cf. spec :
:doc:`docs/specs/M17-cross-layer-integrity.md` §11.

Note : on saute le numéro 0008 (sauté par M15 — cf. docstring
0009_m15_anti_toxic_lifecycle.py). M17 est ancrée sur 0009 directement,
chain linéaire conservée.

Revision ID: 0010_pnl_snapshot_execution_mode
Revises: 0009_m15_anti_toxic_lifecycle
Create Date: 2026-04-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_pnl_snapshot_execution_mode"
down_revision: str | Sequence[str] | None = "0009_m15_anti_toxic_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- MD.3 : pnl_snapshots.execution_mode + CHECK constraint ---------------
    with op.batch_alter_table("pnl_snapshots") as batch:
        batch.add_column(
            sa.Column(
                "execution_mode",
                sa.String(16),
                nullable=False,
                server_default=sa.text("'live'"),
            ),
        )
        batch.create_check_constraint(
            "ck_pnl_snapshots_execution_mode",
            "execution_mode IN ('simulation', 'dry_run', 'live')",
        )

    # Backfill in-place depuis is_dry_run.
    op.execute(
        sa.text(
            "UPDATE pnl_snapshots SET execution_mode = 'dry_run' WHERE is_dry_run = 1"
        ),
    )
    op.execute(
        sa.text(
            "UPDATE pnl_snapshots SET execution_mode = 'live' WHERE is_dry_run = 0"
        ),
    )

    # --- MD.7 : trader_events.wallet_address NOT NULL → NULL ------------------
    with op.batch_alter_table("trader_events") as batch:
        batch.alter_column(
            "wallet_address",
            existing_type=sa.String(42),
            nullable=True,
        )


def downgrade() -> None:
    # --- MD.7 : trader_events.wallet_address NULL → NOT NULL (safe) -----------
    # Refuse de downgrade s'il existe des rows avec wallet_address=NULL,
    # sinon downgrade casse la contrainte sur des données système.
    bind = op.get_bind()
    null_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM trader_events WHERE wallet_address IS NULL")
    ).scalar_one()
    if null_count > 0:
        raise RuntimeError(
            f"Cannot downgrade migration 0010 : {null_count} system-level "
            "trader_events rows have wallet_address=NULL. Delete them or "
            "restore them to a wallet first."
        )
    with op.batch_alter_table("trader_events") as batch:
        batch.alter_column(
            "wallet_address",
            existing_type=sa.String(42),
            nullable=False,
        )

    # --- MD.3 : drop execution_mode + CHECK -----------------------------------
    with op.batch_alter_table("pnl_snapshots") as batch:
        batch.drop_constraint(
            "ck_pnl_snapshots_execution_mode",
            type_="check",
        )
        batch.drop_column("execution_mode")
```

### 11.2 Test upgrade → downgrade → re-upgrade

Test d'intégration `test_migration_0010_idempotent` :

```python
async def test_migration_0010_upgrade_downgrade_idempotent(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    # Setup : appliquer toutes les migrations jusqu'à 0009
    await run_alembic_upgrade(db_url, target="0009_m15_anti_toxic_lifecycle")

    # Insert quelques rows test
    await insert_test_rows(db_url, [
        ("pnl_snapshots", {"is_dry_run": True, "total_usdc": 1000}),
        ("pnl_snapshots", {"is_dry_run": False, "total_usdc": 2000}),
        ("trader_events", {"wallet_address": "0xabc", "event_type": "promoted_active"}),
    ])

    # Upgrade vers 0010
    await run_alembic_upgrade(db_url, target="0010_pnl_snapshot_execution_mode")

    # Vérifie le backfill
    rows = await fetch_all(db_url, "SELECT is_dry_run, execution_mode FROM pnl_snapshots")
    assert rows == [(True, "dry_run"), (False, "live")]

    # Downgrade
    await run_alembic_downgrade(db_url, steps=1)

    # Vérifie que execution_mode disparaît mais is_dry_run reste
    cols = await get_columns(db_url, "pnl_snapshots")
    assert "execution_mode" not in cols
    assert "is_dry_run" in cols

    # Re-upgrade : idempotent
    await run_alembic_upgrade(db_url, target="0010_pnl_snapshot_execution_mode")
    rows = await fetch_all(db_url, "SELECT is_dry_run, execution_mode FROM pnl_snapshots")
    assert rows == [(True, "dry_run"), (False, "live")]
```

### 11.3 Rétrocompat lecture `is_dry_run`

`PnlSnapshotRepository.get_max_total_usdc(only_real=...)` est conservé
1 version. Le validator de paramètre :

```python
if only_real is not None and execution_mode is None:
    warnings.warn(
        "PnlSnapshotRepository.get_max_total_usdc(only_real=...) is "
        "deprecated. Use execution_mode='live' or execution_mode='dry_run'.",
        DeprecationWarning,
    )
    execution_mode = "live" if only_real else None
```

`only_real=False` legacy = lit `is_dry_run=True OR False` (pas de
filtre) → équivalent `execution_mode=None` (aussi pas de filtre).
Comportement identique à pré-M17 strict — aucun appelant ne casse.

`only_real=True` legacy = lit `is_dry_run=False` → équivalent
`execution_mode='live'`. Identique.

### 11.4 Sentinel touch order préservé

Vérifier que MD.7 (`insert_event`) précède toujours
`touch sentinel → stop_event.set()`. Test :

```python
async def test_kill_switch_order_strict(monkeypatch):
    sequence = []
    monkeypatch.setattr(events_repo, "insert", AsyncMock(side_effect=lambda *a, **kw: sequence.append("event")))
    monkeypatch.setattr(alerts_queue, "put_nowait", lambda alert: sequence.append("alert"))
    monkeypatch.setattr(sentinel, "touch", lambda *a, **kw: sequence.append("sentinel"))
    monkeypatch.setattr(stop_event, "set", lambda: sequence.append("stop"))

    await writer._maybe_trigger_alerts(total=500, drawdown_pct=50, max_ever=1000, stop_event=stop_event)

    assert sequence == ["event", "alert", "sentinel", "stop"]
```

---

## 12. Plan d'implémentation (commits)

Ordre recommandé (8 commits atomiques sur `main`, pattern M14/M15/M16) :

1. **MD.1 — `simulated` filter 3 sites** (`commit fix(strategy): MD.1 propagate simulated filter`)
   - Aucune dépendance amont. Pure SQL.
   - Tests : 4 unit (§9.1).
   - Risque : aucun (additif strict).

2. **MD.2 — Bypass digest CRITICAL** (`commit fix(monitoring): MD.2 critical alerts bypass digest`)
   - Aucune dépendance.
   - Tests : 4 unit (§9.2).
   - Risque : si l'utilisateur a configuré `ALERT_COOLDOWN_SECONDS=0`,
     les CRITICAL flood. Mitigation : default reste 60s.

3. **MD.5 — Config deprecation `dry_run_virtual_capital_usd`**
   (`commit fix(config): MD.5 deprecate dry_run_virtual_capital_usd`)
   - Aucune dépendance.
   - Tests : 3 unit (§9.5).
   - Risque : warning au boot si l'utilisateur a la legacy var.

4. **MD.4 — `last_known_mid` + `MidpointUnavailableError`**
   (`commit fix(executor): MD.4 virtual wallet last_known_mid fallback`)
   - Dépend de MD.5 (`dry_run_initial_capital_usd` source unique).
   - Tests : 4 unit (§9.4).
   - Risque : skip de snapshot pendant outage prolongé. Acceptable.

5. **MD.6 — `realized_pnl + unrealized_pnl` peuplés**
   (`commit fix(monitoring): MD.6 populate pnl snapshot realized + unrealized`)
   - Dépend de MD.5 (initial_capital source unique pour la math
     unrealized).
   - Tests : 3 unit (§9.6).
   - Risque : aucun (additif strict, les colonnes étaient peuplées 0).

6. **MD.3 — Migration 0010 + segregation**
   (`commit feat(storage): MD.3 alembic 0010 pnl_snapshots execution_mode`)
   - Dépend de MD.6 (le DTO doit être prêt à inclure `execution_mode`
     avant que la migration soit mergée).
   - Tests : 5 unit + 1 intégration (§9.3 + §9.9).
   - Risque : migration sur DB prod. Mitigation : test
     upgrade/downgrade/re-upgrade vert.

7. **MD.7 — Kill switch `TraderEvent`**
   (`commit feat(monitoring): MD.7 kill_switch trader event audit trail`)
   - Dépend de MD.3 (la migration 0010 inclut `wallet_address` nullable).
   - Tests : 3 unit + 1 intégration (§9.7 + §9.9).
   - Risque : insert DB ajoute ~5-15 ms au chemin kill switch.
     Acceptable.

8. **MD.8 (optionnel) — Single-flight 3 caches**
   (`commit perf(strategy): MD.8 single-flight pattern on async caches`)
   - Aucune dépendance fonctionnelle.
   - Tests : 3 unit (§9.8).
   - Risque : aucun (perf pur).

**Si charge serrée** : MD.8 migre en MI ou MJ. MD.1..MD.7 sont
suffisants pour débloquer le passage live (les 5 CRITICALs + 2 HIGH
ciblés).

**Charge cumulée** : ~3-4 jours dev. **Sans MD.8** : ~3 jours.

---

## 13. Risques + non-régressions

### 13.1 Risque : migration 0010 sur DB prod existante

**Symptôme potentiel** : la migration upgrade alters `pnl_snapshots`
sur une DB SQLite avec ~10k snapshots historiques. Le `batch_alter_table`
SQLite recopie la table → coût ~100-500 ms sur 10k rows. Acceptable.

**Mitigation** : migration testée upgrade → downgrade → re-upgrade
en CI sur fixture représentative.

**Plan B** : si downtime perceptible, l'utilisateur peut stopper le
bot, run la migration manuellement (`alembic upgrade head`), restart.

### 13.2 Risque : MD.7 audit trail latence kill switch

**Symptôme potentiel** : `await events_repo.insert(...)` ajoute
~5-15 ms au chemin kill switch avant `stop_event.set()`. Sur un kill
switch déclenché à drawdown 50%, 15 ms en plus n'est pas critique.

**Mitigation** : try/except large autour de l'insert — si la DB est
lockée, on log l'exception et on continue vers Telegram + sentinel +
stop_event. Le kill switch fire toujours.

**Plan B** : si latence observée critique, refactoriser en
fire-and-forget `asyncio.create_task(events_repo.insert(...))` (pas
attendu). Trade-off : perte d'audit en cas de crash entre `task` et
DB commit.

### 13.3 Risque : MD.4 skip prolongé masquant drawdown réel

**Symptôme potentiel** : panne CLOB midpoint > 10 min (rare). Le writer
skip plusieurs ticks → `max_total_usdc` ne progresse pas → au retour
mid OK, le `current total_usdc` peut se comparer à un max obsolète.

**Mitigation** : le drawdown vs un max ancien reste une
**approximation** sécurisante (pire-case = max plus bas que la réalité
→ drawdown sous-estimé → kill switch trigger plus tard). C'est plus
sûr que C-004 originel (drawdown sur-estimé → kill switch faux-positif).

**Plan B** : alerte WARNING `pnl_snapshot_skipped_midpoint_unavailable`
visible dans `/logs` → l'utilisateur peut intervenir manuellement
pendant l'outage.

### 13.4 Risque : MD.2 flood Telegram CRITICAL

**Symptôme potentiel** : un CRITICAL re-fire à la cadence du
`pnl_snapshot_interval_seconds` (default 300s = 5 min). À chaque tick
si drawdown > threshold, re-fire → 12 alertes/heure → flood Telegram.

**Mitigation** : cooldown 60s strict par `cooldown_key`. Re-fire à
T+5min = T+300s > 60s = passe. 1 alerte par tick maximum. 12/h
acceptable (escalation utile).

**Plan B** : si l'utilisateur veut moins, il peut set
`ALERT_COOLDOWN_SECONDS=600` (10 min). Trade-off : perd des
re-fire utiles.

### 13.5 Non-régressions vérifiables

- [ ] `pytest tests/unit/strategy/` vert (M2 + M11 + M13 + M16
      preserves).
- [ ] `pytest tests/unit/monitoring/` vert (M4 + M7 + M10 preserves).
- [ ] `pytest tests/unit/executor/` vert (M3 + M8 + M13 preserves).
- [ ] `pytest tests/unit/storage/` vert + 1 intégration migration
      0010.
- [ ] `pytest tests/unit/discovery/` vert (M5 + M5_bis + M5_ter +
      M14 + M15 inchangés — MB.1 `_compute_internal_pnl_score` filtre
      `simulated == (mode != "live")` cohérent avec MD.1).
- [ ] `mypy --strict src/` vert.
- [ ] `ruff check src/ tests/` vert.

---

## 14. Pièges concrets / FAQ

### 14.1 Piège : ordering MD.3 migration vs MD.6/MD.7

MD.3 inclut migration 0010 (`pnl_snapshots.execution_mode` +
`trader_events.wallet_address` nullable). MD.6 + MD.7 sont des fixes
code qui consomment ces colonnes :

- MD.6 écrit `execution_mode` dans `PnlSnapshotDTO` → ne peut shipper
  qu'**après** MD.3.
- MD.7 écrit `TraderEvent(wallet_address=NULL)` → ne peut shipper
  qu'**après** la relaxation NULL via MD.3.

Ordre commits (cf. §12) : MD.6 ship d'abord la **logique** (DTO accepte
`execution_mode` mais le repo l'ignore tant que la migration n'est
pas mergée), puis MD.3 ship la migration + active la persistence,
puis MD.7 ship en dernier (il dépend de la migration).

**Alternatif simple** : MD.3 ship d'abord (migration + DTO + repo),
puis MD.6 + MD.7. C'est ce que recommande l'ordre §12 strict.

### 14.2 Piège : backward compat lecture `is_dry_run`

Post-migration 0010, les snapshots historiques ont `execution_mode`
backfillé. Mais si un bug regression écrase `execution_mode='live'`
par défaut à tort sur un DRY_RUN, on lit la mauvaise valeur.
**Mitigation** :

- CHECK constraint `execution_mode IN (...)` empêche d'écrire une
  valeur invalide.
- `server_default 'live'` explicite — pas NULL → fail-fast si la
  contrainte CHECK est violée à l'insert.
- Test `test_pnl_snapshot_writes_execution_mode` vérifie que le DTO
  écrit la valeur attendue selon `settings.execution_mode`.

### 14.3 Piège : auto-lockdown CRITICAL alertes en cascade

MD.2 bypass digest pour CRITICAL. **Attention** : cela peut créer un
flood d'alertes Telegram si le kill switch se déclenche en cascade
(ex: drawdown franchit seuil à chaque snapshot 5 min → 1 alerte / 5
min). Considérer une protection au-delà : même CRITICAL, un
event_type donné ne doit pas re-fire plus d'1× par minute (cooldown
60s strict par `cooldown_key`).

**Solution adoptée** : bypass uniquement le **digest**, conserver
cooldown 60s strict par `cooldown_key`. Re-fire du même kill_switch
après 60s = message spam utile (escalation signal), pas flood.

### 14.4 Piège : `MidpointUnavailableError` vs `stop_event`

MD.4 lève `MidpointUnavailableError` qui bubble up au
`PnlSnapshotWriter`. Le writer catch et skip le snapshot.
**Attention** : si panne mid prolongée (>10 min), le writer skip
plusieurs snapshots → le `max_total_usdc` ne progresse plus → au
retour mid, le `current total_usdc` peut se comparer à un max
obsolète.

**Acceptable** : drawdown vs un max ancien reste une approximation,
**pas pire que le bug C-004 initial** (qui calculait drawdown vs
un min creux artificiel). C-004 fix = direction sécurisante.

### 14.5 Piège : MD.7 + crash entre insert et stop_event

Si le bot crash (kill -9) entre `events_repo.insert(kill_switch)` et
`stop_event.set()`, le respawn supervisor (M12_bis Phase D) trouve :

- `trader_events` row `kill_switch` peuplée → audit trail OK.
- Sentinel `halt.flag` posée (étape 3) → respawn en mode paused.
- `stop_event` était local → perdu.

Comportement nominal : respawn en pause sans re-poster d'orders.
L'utilisateur voit l'event dans `/pnl`, prend la décision manuelle
(résolution drawdown / `--force-resume`).

Si crash entre étape 1 (insert_event) et étape 3 (touch sentinel),
le respawn trouve l'event mais pas le sentinel → respawn en mode
normal, qui re-déclenche le kill switch au prochain tick → re-insert
event + sentinel + stop. Idempotent (cooldown 60s côté Telegram, pas
de spam). Acceptable.

### 14.6 FAQ : Pourquoi pas de tri-state `MyPosition.execution_mode` ?

Q : Pourquoi MD.1 utilise `simulated == (mode != "live")` (binaire)
plutôt que de migrer `MyPosition` vers `execution_mode` (tri-state) ?

R : Décision **D1** §2.1. La distinction SIM vs DRY_RUN au niveau
`MyPosition` n'a aucun consommateur :

- M3 dry_run path : crée des positions `simulated=True` que ce soit
  SIM ou DRY_RUN.
- M3 live path : crée `simulated=False`.
- M4..M16 readers ne distinguent jamais SIM de DRY_RUN au niveau
  position.

La distinction tri-state est utile uniquement pour les baselines
PnL drawdown (MD.3 — `pnl_snapshots.execution_mode`). Migrer
`MyPosition` serait une refacto invasive non justifiée. Si un futur
besoin émerge (ex: SIM partagé en DB avec DRY_RUN), spec dédiée.

### 14.7 FAQ : Pourquoi ne pas drop `is_dry_run` immédiatement ?

Q : Pourquoi conserver `pnl_snapshots.is_dry_run` 1 version au lieu de
dropper dans 0010 ?

R : Décision **D3** §2.1. Pattern strict copié de M10 `DRY_RUN=true/false`
deprecation. Le buffer 1 version permet :

- Outils externes (export CSV, scripts ad-hoc) lisent encore
  `is_dry_run` sans break.
- Rollback rapide possible via downgrade 0010 si bug critique post-merge.
- Tests M4..M16 existants restent verts sans rewrite massif.

Drop programmé en migration 0011 (M18+).

### 14.8 FAQ : Pourquoi pas de migration `MyPosition.simulated` retroactive ?

Q : Si on flip `EXECUTION_MODE=live` après dry-run prolongé, les ~512
positions virtuelles M13 traînent. MD.1 les filtre out. Mais en DB,
elles restent ouvertes indéfiniment ?

R : **Oui, et c'est volontaire**. Les positions virtuelles ont une
realité dry-run : elles vont se résoudre via `DryRunResolutionWatcher`
M8 v2 quand le marché Polymarket résout. À ce moment :

- `MyPosition.closed_at` est posée.
- `realized_pnl` cristallisé.
- La position disparaît des queries `closed_at IS NULL`.

L'utilisateur n'a rien à faire manuellement. Si l'utilisateur veut
purger, c'est un script ad-hoc DB hors scope M17.

---

## 15. Mapping origines (traçabilité)

| Item | Audit | Session | Synthèse roadmap | Spec section |
|---|---|---|---|---|
| MD.1 | [C-001](../audit/2026-04-24-polycopy-code-audit.md#L60) | E (E1) | #21 | §2.1 + §5.1 |
| MD.2 | [C-002](../audit/2026-04-24-polycopy-code-audit.md#L68) + [M-009](../audit/2026-04-24-polycopy-code-audit.md#L260) | E (E2) | #22 | §2.1 + §5.2 |
| MD.3 | [C-003](../audit/2026-04-24-polycopy-code-audit.md#L76) + [M-020](../audit/2026-04-24-polycopy-code-audit.md#L319) | E (E3) | #23 | §2.1 + §5.3 + §11 |
| MD.4 | [C-004](../audit/2026-04-24-polycopy-code-audit.md#L85) | E (E4) | #24 | §2.1 + §5.4 |
| MD.5 | [H-004](../audit/2026-04-24-polycopy-code-audit.md#L126) | E (E5) | #25 | §2.1 + §5.5 |
| MD.6 | [H-002](../audit/2026-04-24-polycopy-code-audit.md#L112) (+ [C-005](../audit/2026-04-24-polycopy-code-audit.md#L94) effet) | E (new) | — | §2.1 + §5.6 |
| MD.7 | [H-005](../audit/2026-04-24-polycopy-code-audit.md#L134) | E (new) | — | §2.1 + §5.7 + §11 |
| MD.8 | [M-007](../audit/2026-04-24-polycopy-code-audit.md#L250) | E (new, optional) | #36 | §2.1 + §5.8 |

**Sources** :

- Audit code 2026-04-24 :
  [docs/audit/2026-04-24-polycopy-code-audit.md](../audit/2026-04-24-polycopy-code-audit.md).
- Brief actionnable : [docs/next/MD.md](../next/MD.md).
- Memory `project_dashboard_audit_20260424` (UX divergence /home vs
  /performance signalée par l'utilisateur 2026-04-24).
- CLAUDE.md §Conventions + §Sécurité (invariants triple/4ᵉ garde-fou,
  parité 3 modes, sentinel order).

**Specs antérieures non touchées (lecture confirmée)** :

- [M2 Strategy engine](M2-strategy-engine.md) §pipeline order.
- [M3 Executor](M3-executor.md) §triple garde-fou.
- [M4 Monitoring](M4-monitoring.md) §kill switch + alert dispatcher.
- [M7 Telegram enhanced](M7-telegram-enhanced.md) §AlertDigestWindow.
- [M8 Dry-run realistic](M8-dry-run-realistic.md) §4ᵉ garde-fou.
- [M10 Parity & log hygiene](M10-parity-and-log-hygiene.md) §execution_mode + kill switch parity.
- [M12_bis Remote control](M12_bis_multi_machine_remote_control_spec.md) §Phase D sentinel order.
- [M13 Dry-run observability](M13_dry_run_observability_spec.md) §Bug 5 + neg_risk resolution + initial_capital fallback.
- [M14 Scoring v2.1-ROBUST](M14-scoring-v2.1-robust.md) §registry + audit trail sacré.
- [M15 Anti-toxic lifecycle](M15-anti-toxic-lifecycle.md) §MB.1 + MB.6 + MB.8 + migration 0009.
- [M16 Dynamic fees + EV](M16-dynamic-fees-ev.md) §FeeRateClient + EV after-fee.

---

## 16. Prompt d'implémentation

Bloc à coller tel quel dans une nouvelle conversation Claude Code à
l'implémentation M17.

````markdown
# Contexte

polycopy a 5 CRITICALs cross-couche + 3 HIGH connexes révélés par
l'audit code 2026-04-24. Tous silencieux en dry-run continu, mais
**bloquants au flip live** (C-001 — `position_already_open` sur toutes
les positions virtuelles M13 traînantes) ou **corrupteurs du calcul de
kill switch** (C-002 digest delay 5 min, C-003 baselines mélangées
SIM/DRY, C-004 mid-outage drawdown factice). Plus 3 HIGH (H-002 PnL
hardcodé 0.0, H-004 deux capitaux divergents, H-005 audit trail
kill_switch vide).

Diagnostic complet dans
[docs/specs/M17-cross-layer-integrity.md](docs/specs/M17-cross-layer-integrity.md). 8
items couplés MD.1 → MD.8 (filter `simulated` 3 sites + bypass digest
CRITICAL + migration Alembic 0010 `pnl_snapshots.execution_mode` +
last_known_mid VirtualWallet + dépréciation config + populate
realized_pnl + audit trail kill_switch + single-flight optionnel).

# Prérequis

- Lire `docs/specs/M17-cross-layer-integrity.md` **en entier**
  (spécifiquement §5 algorithmes par item + §9 test plan + §11
  walkthrough migration 0010).
- Lire [CLAUDE.md](CLAUDE.md) sections "Conventions de code" et
  "Sécurité" (triple garde-fou M3 + 4ᵉ M8 préservés, kill switch parité
  3 modes M10, sentinel halt.flag order-strict M12_bis Phase D).
- Lire
  [docs/audit/2026-04-24-polycopy-code-audit.md](docs/audit/2026-04-24-polycopy-code-audit.md)
  sections C-001 à C-005, H-002, H-004, H-005, M-007, M-009 pour le
  contexte des bugs.
- Lire [docs/specs/M4-monitoring.md](docs/specs/M4-monitoring.md) §kill
  switch + alert dispatcher pour pattern référent.
- Lire
  [docs/specs/M10-parity-and-log-hygiene.md](docs/specs/M10-parity-and-log-hygiene.md)
  §execution_mode enum + parity 3 modes pour invariants.
- Lire
  [docs/specs/M15-anti-toxic-lifecycle.md](docs/specs/M15-anti-toxic-lifecycle.md)
  §11.1 pour la chain Alembic actuelle (0001 → 0007 → 0009, le 0008
  sauté). M17 prend le numéro **0010**.

# Ordre de commits recommandé (8 commits atomiques)

1. `fix(strategy): MD.1 propagate simulated filter to PositionSizer + RiskManager` (MD.1, §5.1, 4 tests §9.1)
2. `fix(monitoring): MD.2 critical alerts bypass digest window` (MD.2, §5.2, 4 tests §9.2)
3. `fix(config): MD.5 deprecate dry_run_virtual_capital_usd in favor of dry_run_initial_capital_usd` (MD.5, §5.5, 3 tests §9.5)
4. `fix(executor): MD.4 virtual wallet last_known_mid fallback + MidpointUnavailableError` (MD.4, §5.4, 4 tests §9.4)
5. `fix(monitoring): MD.6 populate pnl_snapshot realized + unrealized` (MD.6, §5.6, 3 tests §9.6)
6. `feat(storage): MD.3 alembic 0010 pnl_snapshots.execution_mode + trader_events.wallet_address nullable` (MD.3+MD.7 schema, §5.3 + §11, 5 tests §9.3 + 1 intégration §9.9)
7. `feat(monitoring): MD.7 kill_switch trader event audit trail` (MD.7, §5.7, 3 tests §9.7 + 1 intégration §9.9)
8. `perf(strategy): MD.8 single-flight pattern on async caches (gamma + orderbook + ws)` (MD.8 **optionnel**, §5.8, 3 tests §9.8)

**Si charge serrée** : MD.8 migre en MI ou MJ. MD.1..MD.7 suffisent
pour débloquer le passage live.

**Push sur main après chaque commit.** Pas de branche, pas de PR
(règle projet, workflow trunk-based).

# Validation entre commits

- Tests ciblés (cf. memory `feedback_test_scope`) : ~30 sec / commit.
- `ruff check .` + `ruff format --check .` + `mypy src` après chaque commit.
- Avant commit 6 (migration 0010) : montrer le diff
  `alembic/versions/0010_*.py` pour valider le backfill SQL et le
  safeguard downgrade `wallet_address NULL → NOT NULL` (cf. §11.1).
- Avant commit 7 (MD.7) : valider l'ordre strict
  `insert_event → push_alert → touch_sentinel → stop_event.set()` via
  test `test_kill_switch_order_strict` (§11.4).

# Tests + quality gates

- Tests ciblés entre commits.
- Full `pytest` + `ruff check .` + `ruff format .` + `mypy src` à la
  fin du bundle (8 ou 7 commits).
- Tests d'intégration § 9.9 obligatoires : flip mode E2E,
  kill switch immédiat, convergence /home ↔ /performance.

# Git workflow

- **Tout commit directement sur `main`** — pas de branche, pas de PR
  (règle projet, workflow trunk-based).
- 7-8 commits atomiques poussés en série sur `main` après validation
  tests verts entre chaque push.
- Update CLAUDE.md §Conventions + §Sécurité avec mention M17
  cross-layer (cf. §10 spec M14/M15/M16 pour pattern).

# Contraintes non négociables

- **Triple garde-fou M3 + 4ᵉ M8 préservés** : aucun touch des chemins
  signature CLOB ni `_persist_realistic_simulated`.
- **Kill switch parité 3 modes M10** : MD.2 + MD.3 + MD.7 renforcent
  l'invariant (immédiateté + ségrégation + traçabilité).
- **Sentinel halt.flag order strict** (CLAUDE.md §Sécurité M12_bis Phase D) :
  `insert_event → push_alert → touch_sentinel → stop_event.set()`.
  `stop_event.set()` reste **strictement la dernière étape**.
- **API M15 préservée** :
  `MyPositionRepository.sum_realized_pnl_by_source_wallet` +
  `count_wins_losses_by_source_wallet` signatures **inchangées**.
- **Versioning sacré scoring** : aucune fonction `compute_score_v2_1`
  / `compute_score_v2_1_1` touchée. MD.6 alimente le DTO PnL au niveau
  monitoring, hors couche scoring.
- **Migration 0010 SQLite-friendly** : `batch_alter_table` + CHECK
  constraint + backfill SQL. Cohérent migrations 0007 / 0009.
- **Rétrocompat 1 version** : `pnl_snapshots.is_dry_run` conservé,
  `dry_run_virtual_capital_usd` setting conservé. Drop programmé M18+.
- **Aucun secret loggé** : les 9 nouveaux events structlog (cf. §8) ne
  contiennent que numeric / string publique. Test grep automatisé.
- **Conventions CLAUDE.md** : async, Pydantic v2, SQLAlchemy 2.0,
  structlog, docstrings FR / code EN, pas de print.
- **mypy strict propre, ruff propre, coverage ≥ 80 %** sur nouveaux
  fichiers.

# Demande-moi confirmation AVANT

- Si la migration 0010 nécessite un down_revision différent (chain
  Alembic actuelle = 0001 → 0007 → 0009, le 0008 réservé). Ne pas
  prendre 0008 sans valider.
- Si tu observes un test M5 / M5_bis / M5_ter / M14 / M15 / M16 qui
  casse (signal de scope creep — M17 est strictement additif sur ces
  modules).
- Si le grep `MyPosition.closed_at.is_(None)` retourne plus de 4 sites
  (pipeline 3 + repo `list_open_virtual` 1) — il faut auditer chaque
  site supplémentaire.
- Si l'utilisateur a `ALERT_COOLDOWN_SECONDS=0` configuré (le bypass
  digest CRITICAL sans cooldown peut flood — §13.4).

# STOP et signale si

- Schema `pnl_snapshots` ou `trader_events` divergent du modèle ORM
  attendu en §6 (potentielle migration manquante en amont).
- Backfill SQL `UPDATE pnl_snapshots SET execution_mode = ...` retourne
  0 rows alors que la table contient des snapshots historiques (bug
  WHERE clause).
- Test `test_kill_switch_order_strict` échoue (l'ordre est critique
  pour l'invariant sentinel M12_bis).
- `MidpointUnavailableError` se déclenche en boucle au boot (le
  last_known est vide à T+0 — vérifier que le 1ᵉʳ tick OK populate
  bien le dict avant tout skip).

# Plan à confirmer

Commence par me confirmer ton plan en 1 message bref (1 phrase par
commit), puis enchaîne les 7-8 commits dans l'ordre ci-dessus. Tests
verts avant chaque push.
````

---

## 17. Commit message proposé (bundle wrap)

```
feat(integrity): M17 cross-layer integrity patches (audit CRITICALs)

Bundle 7-8 items (MD.1 → MD.8) qui ferme les 5 CRITICALs cross-couche
+ 3 HIGH connexes révélés par l'audit code 2026-04-24, blockers
silencieux du passage live :

- MD.1 PositionSizer._check_buy + _check_sell + RiskManager.check
  filtrent désormais MyPosition.simulated == (execution_mode != "live")
  aux 3 queries WHERE closed_at IS NULL (audit C-001 — flip dry_run
  → live ne bloque plus tous les BUY live à cause des positions
  virtuelles M13 traînantes).
- MD.2 AlertDispatcher._handle bypass de la fenêtre digest pour les
  alertes level=CRITICAL (envoi immédiat, jamais batché). Cooldown 60s
  par cooldown_key préservé (idempotence anti-flood §11.4). Audit
  C-002 + M-009 résolus.
- MD.3 migration Alembic 0010 ajoute pnl_snapshots.execution_mode
  VARCHAR(16) NOT NULL DEFAULT 'live' + CHECK constraint +
  backfill in-place depuis is_dry_run. PnlSnapshotRepository.get_*
  prennent execution_mode: Literal[...] strict. Plus de pollution
  cross-mode SIM/DRY/LIVE (audit C-003 + M-020). is_dry_run conservé
  1 version (drop M18+). down_revision="0009_m15_anti_toxic_lifecycle"
  (chain linéaire, le 0008 reste sauté par M15).
- MD.4 VirtualWalletStateReader._last_known_mid dict TTL 10 min +
  MidpointUnavailableError (nouvelle exception
  executor/exceptions.py). Mid manquant + last_known frais → fallback ;
  mid manquant + stale → raise + skip snapshot côté
  PnlSnapshotWriter (pas de drawdown factice — audit C-004).
- MD.5 dry_run_virtual_capital_usd deprecated au profit de
  dry_run_initial_capital_usd (source unique). Validator Pydantic
  reroute legacy avec warning config_deprecation_dry_run_virtual_capital_env
  (pattern strict copié M10 DRY_RUN deprecation — audit H-004).
- MD.6 PnlSnapshotDTO.realized_pnl + unrealized_pnl peuplés avec les
  vraies valeurs (au lieu de 0.0 hardcodé). Helper
  WalletStateReader.get_realized_pnl_cumulative(*, mode) agrège
  MyPosition.realized_pnl filtré par mode. unrealized = total -
  initial - realized_cumulative. Convergence /home ↔ /performance
  garantie (audit H-002 + C-005 effet).
- MD.7 kill switch écrit TraderEvent(wallet_address=NULL,
  event_type="kill_switch") AVANT push_alert AVANT touch_sentinel
  AVANT stop_event.set() (ordre strict CLAUDE.md §Sécurité M12_bis
  Phase D). Migration 0010 relâche trader_events.wallet_address en
  NULL pour autoriser les events système. Audit trail kill_switch
  visible /pnl milestones (audit H-005).
- MD.8 (optionnel) single-flight pattern _inflight: dict[str,
  asyncio.Future] propagé aux 3 caches async restants
  (GammaApiClient.get_market, ClobOrderbookReader.get_book,
  ClobMarketWSClient._maybe_subscribe) — audit M-007.

Diff strictement additif sur les invariants critiques :
- Triple garde-fou M3 + 4ᵉ M8 : intacts (M17 ne touche aucun chemin
  signature CLOB ni _persist_realistic_simulated).
- Kill switch parité 3 modes M10 : RENFORCÉ par MD.2 + MD.3 + MD.7.
- API M15 (sum_realized_pnl_by_source_wallet, count_wins_losses_*) :
  signatures INCHANGÉES.
- Versioning sacré scoring (compute_score_v2_1, _v2_1_1, registry) :
  aucun touch.
- Sentinel halt.flag order strict M12_bis Phase D : préservé
  (insert_event → alert → sentinel → stop_event).

~28 tests unit + 3 intégration (flip mode E2E, kill switch immédiat
sous pression digest, convergence /home ↔ /performance). Migration
Alembic 0010 testée upgrade → downgrade → re-upgrade idempotent.
Aucune nouvelle creds, aucun secret loggé, M17 reste 100% read-only
côté API publique (lecture SQL + insert audit trail local).

Cf. spec [docs/specs/M17-cross-layer-integrity.md](docs/specs/M17-cross-layer-integrity.md).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

---

## 18. Critères d'acceptation

- [ ] 7-8 items MD.1 → MD.8 (selon scope MD.8 optionnel) implémentés
      selon §5.
- [ ] **MD.1** : 3 queries `MyPosition WHERE closed_at IS NULL` dans
      `pipeline.py` (`_check_buy`, `_check_sell`, `RiskManager.check`)
      portent désormais `MyPosition.simulated == (execution_mode != "live")`.
- [ ] **MD.1** : grep `MyPosition.closed_at.is_(None)` dans `src/`
      retourne exactement 5 sites (3 pipeline + `list_open_virtual` +
      `sum_realized_pnl_virtual` — déjà filtrés simulated).
- [ ] **MD.2** : `AlertDispatcher._handle` early-return pour
      `level == "CRITICAL"` AVANT `digest.register`. Cooldown 60s par
      `cooldown_key` reste appliqué (test `test_critical_alert_respects_cooldown`).
- [ ] **MD.2** : event structlog `alert_sent_critical_bypass_digest`
      INFO émis sur chaque CRITICAL.
- [ ] **MD.3** : migration `alembic/versions/0010_pnl_snapshot_execution_mode.py`
      crée avec `down_revision="0009_m15_anti_toxic_lifecycle"`.
- [ ] **MD.3** : `alembic upgrade head` → `alembic downgrade -1` →
      `alembic upgrade head` idempotent (test `test_migration_0010_idempotent`).
- [ ] **MD.3** : `PnlSnapshot.execution_mode` Mapped + CHECK constraint
      `IN ('simulation', 'dry_run', 'live')`.
- [ ] **MD.3** : `PnlSnapshotRepository.get_max_total_usdc(*, execution_mode=...)`
      filtre strict ; `only_real` legacy reste accepté avec
      DeprecationWarning.
- [ ] **MD.3** : backfill SQL `UPDATE pnl_snapshots SET
      execution_mode='dry_run' WHERE is_dry_run=1` + symétrique
      `'live'`. Test `test_migration_0010_backfill_correct`.
- [ ] **MD.4** : `MidpointUnavailableError` exception dans
      `src/polycopy/executor/exceptions.py` + réexport `__init__.py`.
- [ ] **MD.4** : `VirtualWalletStateReader._last_known_mid:
      dict[str, tuple[float, datetime]]` + helpers `_record_last_known`
      / `_fetch_last_known` + TTL 600s.
- [ ] **MD.4** : `PnlSnapshotWriter._tick` catch `MidpointUnavailableError`
      → log WARNING `pnl_snapshot_skipped_midpoint_unavailable` + skip
      tick.
- [ ] **MD.5** : validator Pydantic `_migrate_legacy_virtual_capital`
      reroute `DRY_RUN_VIRTUAL_CAPITAL_USD` → `DRY_RUN_INITIAL_CAPITAL_USD`
      avec DeprecationWarning + log structlog
      `config_deprecation_dry_run_virtual_capital_env`.
- [ ] **MD.5** : `executor/orchestrator.py` + `virtual_wallet_reader.py`
      lisent `dry_run_initial_capital_usd` (source unique).
- [ ] **MD.6** : `WalletStateReader.get_realized_pnl_cumulative(*, mode)`
      + `VirtualWalletStateReader.get_realized_pnl_cumulative(*, mode)`
      symétriques (interface uniforme).
- [ ] **MD.6** : `PnlSnapshotDTO.realized_pnl + unrealized_pnl` peuplés
      avec les vraies valeurs ; `unrealized = total - initial - realized`.
- [ ] **MD.6** : test régression `test_dashboard_consumes_pnl_snapshot_values_not_inline_recompute`
      vert (convergence /home ↔ /performance < 1 cent).
- [ ] **MD.7** : `TraderEventType` Literal += `"kill_switch"`.
- [ ] **MD.7** : `TraderEventDTO.wallet_address: str | None = None`.
- [ ] **MD.7** : migration 0010 relâche `trader_events.wallet_address`
      en `nullable=True`.
- [ ] **MD.7** : `PnlSnapshotWriter._maybe_trigger_alerts` insert
      `TraderEventDTO(event_type="kill_switch", wallet_address=None,
      event_metadata={drawdown_pct, total_usdc, max_total_usdc,
      execution_mode, threshold})` AVANT `push_alert` AVANT `touch_sentinel`
      AVANT `stop_event.set()`. Test `test_kill_switch_order_strict`
      vert.
- [ ] **MD.7** : `get_pnl_milestones` retourne désormais des markers
      `kill_switch` non-vides après injection d'event test.
- [ ] **MD.8** (si shippé) : pattern `_inflight: dict[str,
      asyncio.Future]` appliqué à `GammaApiClient.get_market`,
      `ClobOrderbookReader.get_book`, `ClobMarketWSClient._maybe_subscribe`.
      3 tests `test_*_single_flight_prevents_redundant_fetches` verts.
- [ ] **Triple garde-fou M3 + 4ᵉ M8 préservés** : aucun fichier
      `executor/wallet_state_reader.py` (chemin POST), `executor/clob_write_client.py`,
      `executor/_persist_realistic_simulated` modifié au-delà des helpers
      MD.6.
- [ ] **Kill switch parité 3 modes M10** : tests M10 existants verts
      (parité dry_run / live `kill_switch_triggered` CRITICAL).
- [ ] **Sentinel order strict** : test `test_kill_switch_order_strict`
      vérifie séquence `event → alert → sentinel → stop_event`.
- [ ] **API M15 préservée** :
      `MyPositionRepository.sum_realized_pnl_by_source_wallet` +
      `count_wins_losses_by_source_wallet` signatures **inchangées**.
      Tests M15 MB.1 + MB.8 verts.
- [ ] **Versioning sacré scoring** : aucun touch dans
      `src/polycopy/discovery/scoring/`. Tests M14 + M15 verts.
- [ ] **Aucune nouvelle creds CLOB consommée** — M17 reste 100%
      read-only côté API publique.
- [ ] **Aucun secret loggé** : test `test_m17_no_secret_leak.py` grep
      automatisé sur les 9 nouveaux events structlog.
- [ ] **CLAUDE.md** §Conventions + §Sécurité mises à jour avec mention
      M17 cross-layer.
- [ ] **.env.example** documenté : DEPRECATED comment sur
      `DRY_RUN_VIRTUAL_CAPITAL_USD` + bloc `DRY_RUN_INITIAL_CAPITAL_USD`.
- [ ] Tests M2..M16 existants passent inchangés (rétrocompat stricte).
- [ ] **Invariants M5 / M5_bis / M5_ter / M11 / M12 / M13 / M14 / M15 /
      M16 préservés** : lifecycle, eviction, watcher, latency, scoring,
      dry-run executor, anti-toxic, fees-aware — tous intacts.
- [ ] `ruff check .` + `ruff format --check .` + `mypy src` : 0 erreur.
- [ ] `pytest` : 0 échec. Coverage ≥ 80 % sur fichiers nouveaux/modifiés.
- [ ] Smoke test runtime 60s : pas d'erreur cascade, pas de
      `pnl_snapshot_skipped_midpoint_unavailable` au boot (le 1ᵉʳ tick
      doit populate last_known).
- [ ] 7-8 commits atomiques MD.1 → MD.8 (selon scope) poussés sur
      `main` (pas de branche, pas de PR — règle projet).

---

**FIN spec M17 — Cross-layer integrity patches.**

Charge totale estimée : **3-4 jours dev** (sans MD.8) à **4-5 jours**
(avec MD.8 optionnel). Tests : **~28 unit** + **3 intégration**. Migration :
**0010** linéaire (down_revision = 0009). Bloque le passage live ; ship
avant tout flip `EXECUTION_MODE=live` en prod.
