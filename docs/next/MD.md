# MD — Cross-layer integrity patches (audit CRITICALs)

**Priorité** : 🟠 P2 (blockers avant passage live, différable tant qu'on reste dry-run)
**Charge estimée** : M (3-4 jours)
**Branche suggérée** : `fix/cross-layer-integrity`
**Prérequis** : aucun
**Bloque** : passage live (les CRITICALs s'activent au flip dry_run→live ou au cutover de mode)

---

## 1. Objectif business

Corriger les **5 CRITICALs cross-couche** révélés par l'audit code 2026-04-24, plus 3 HIGH connexes. Ces bugs sont **silencieux en dry-run continu** mais **bloquent le passage live** et/ou **corrompent les calculs de kill switch**. Timing : ship **avant** le flip `EXECUTION_MODE=live` mais **pas urgent** tant que polycopy tourne en shadow period dry-run (MA/MB/MC shippés d'abord). Naturellement parallélisable à toute autre session P1/P2.

## 2. Contexte & problème observé

Tous les findings de ce module viennent de l'**audit code** `docs/audit/2026-04-24-polycopy-code-audit.md`, section §3 🔴 CRITICAL + 🟠 HIGH.

### Findings audit référencés

- **[C-001]** (audit §3 CRITICAL) : `PositionSizer` et `RiskManager` ne filtrent pas `MyPosition.simulated`.
  - Location : [src/polycopy/strategy/pipeline.py:177-210](../../src/polycopy/strategy/pipeline.py#L177-L210) + [src/polycopy/strategy/pipeline.py:263-274](../../src/polycopy/strategy/pipeline.py#L263-L274)
  - Description : `_check_buy` / `_check_sell` et `RiskManager.check` requêtent `MyPosition WHERE closed_at IS NULL` **sans filtrer `simulated`**. Conséquence : en LIVE, des positions virtuelles héritées du dry-run bloquent les BUY réels (`position_already_open`) et polluent le calcul d'exposition.
  - Impact : un flip dry_run → live avec positions virtuelles traînantes **bloque silencieusement tous les BUY live**. Inversement, un run live historique pollue les décisions dry-run.

- **[C-002]** (audit §3 CRITICAL) : Kill switch CRITICAL digéré/retardé par `AlertDigestWindow`.
  - Location : [src/polycopy/monitoring/alert_dispatcher.py:117-145](../../src/polycopy/monitoring/alert_dispatcher.py#L117-L145) + [src/polycopy/monitoring/alert_digest.py:44-63](../../src/polycopy/monitoring/alert_digest.py#L44-L63)
  - Description : Le dispatcher passe **toutes** les alertes par `self._digest.register(alert, now)`, **sans exception pour CRITICAL**. Si la fenêtre digest (5 min) contient déjà ≥ `threshold-1` kill_switch, le message est batché — `stop_event.set()` ne déclenche pas immédiatement dans le flow alerte.
  - Impact : pendant **5 min** (fenêtre digest), le bot continue à poster des ordres alors que le drawdown kill-switch est atteint. **Perte capital potentielle**.

- **[C-003]** (audit §3 CRITICAL) : Drawdown baseline mélange SIMULATION + DRY_RUN dans le même bucket `is_dry_run`.
  - Location : [src/polycopy/monitoring/pnl_writer.py:86-98](../../src/polycopy/monitoring/pnl_writer.py#L86-L98) + [src/polycopy/storage/repositories.py:809-817](../../src/polycopy/storage/repositories.py#L809-L817)
  - Description : `only_real = execution_mode == "live"` → en DRY_RUN ou SIMULATION, `only_real=False` et `get_max_total_usdc(only_real=False)` renvoie **TOUS** les snapshots non-live. Si l'utilisateur a tourné SIMULATION avec virtual_capital=$50k, puis DRY_RUN avec $10k, le premier tick DRY_RUN calcule `drawdown = (10000-50000)/50000 = 80%` → **kill switch immédiat faux-positif**.

- **[C-004]** (audit §3 CRITICAL) : `VirtualWalletStateReader` skip silencieusement les positions dont le mid est None → sous-évaluation du capital.
  - Location : [src/polycopy/executor/virtual_wallet_reader.py:48-71](../../src/polycopy/executor/virtual_wallet_reader.py#L48-L71)
  - Description : si WS+HTTP midpoint retournent None (panne transitoire), la position est `continue`-skipped, pas comptée ni dans `exposure` ni dans `unrealized`. Le `total_usdc` retourné est artificiellement bas → le max historique baisse → le prochain tick avec midpoint OK remonte → **drawdown factice** vs ce min creux.

- **[C-005]** (audit §3 CRITICAL) : Divergence realized_pnl entre `/home` et `/performance` — deux agrégations sur mêmes données.
  - Location : [src/polycopy/dashboard/queries.py:757-824](../../src/polycopy/dashboard/queries.py#L757-L824) vs [queries.py:1690-1870](../../src/polycopy/dashboard/queries.py#L1690-L1870)
  - Description : `get_home_alltime_stats` calcule `live_pnl` via `Σ(SELL×price) - Σ(BUY×price)` inline par position. `list_trader_performance` calcule `stats["sell_recovery"] - stats["buy_cost"]` après agrégation par trader. Ordre d'évaluation ≠ → divergences possibles. Pire : `PnlSnapshotWriter` hardcode `realized_pnl = 0.0` dans le DTO persisté (cf. [H-002]).

- **[H-002]** (audit §3 HIGH) : `PnlSnapshotDTO` hardcode `realized_pnl=0.0, unrealized_pnl=0.0` — ne sert à rien, source de bug cross-source.
  - Location : [src/polycopy/monitoring/pnl_writer.py:90-98](../../src/polycopy/monitoring/pnl_writer.py#L90-L98)
  - Description : les colonnes DB `realized_pnl` et `unrealized_pnl` de `pnl_snapshots` sont peuplées avec 0.0 hardcodé. Seul `total_usdc` reflète la vérité.

- **[H-004]** (audit §3 HIGH) : Deux capitaux initiaux dry-run distincts : `dry_run_virtual_capital_usd` vs `dry_run_initial_capital_usd`.
  - Location : [src/polycopy/config.py:315](../../src/polycopy/config.py#L315) + [config.py:359](../../src/polycopy/config.py#L359) + [virtual_wallet_reader.py:62](../../src/polycopy/executor/virtual_wallet_reader.py#L62) + [queries.py:965](../../src/polycopy/dashboard/queries.py#L965)
  - Description : deux settings distincts représentent la même chose. Divergence → latent PnL faux.

- **[H-005]** (audit §3 HIGH) : Kill switch jamais écrit dans `trader_events` → milestone `/pnl` vide.
  - Location : [src/polycopy/monitoring/pnl_writer.py:127-155](../../src/polycopy/monitoring/pnl_writer.py#L127-L155) + [queries.py:1119-1133](../../src/polycopy/dashboard/queries.py#L1119-L1133)
  - Description : `_maybe_trigger_alerts` push Telegram + `stop_event.set()` + sentinel, mais n'écrit **jamais** dans `trader_events`. Or `get_pnl_milestones` query `TraderEvent WHERE event_type = "kill_switch"` → milestone toujours vide.

- **[M-009]** (audit §3 MEDIUM connexe à C-002) : Cooldown 60s appliqué aussi aux CRITICAL → `auth fatal` throttled.
  - Location : [src/polycopy/monitoring/alert_dispatcher.py:117-145](../../src/polycopy/monitoring/alert_dispatcher.py#L117-L145)
  - Description : `executor_auth_fatal` répété à 30s intervals → 2e alerte drop silencieusement.

### Session originale mappée

**Session E brouillon** (`docs/bug/session_E_cross_layer_integrity_and_hardening.md`) items E1-E5 intégrés ici + ajouts M-009, M-007 TOCTOU, H-002, H-005.

### Pourquoi c'est P2 (différable tant qu'on est en dry-run)

- **C-001** ne mord **qu'au flip live**. Tant que `EXECUTION_MODE=dry_run`, les SELL copiés fonctionnent (MA/MB ont fixé le bug côté PositionSizer side-awareness). Le filter `simulated` manquant n'est visible qu'au basculement mode.
- **C-002** ne mord **que si ≥N kill_switch alertes en 5 min**. Probabilité faible mais impact élevé.
- **C-003** ne mord **qu'au changement de `dry_run_virtual_capital_usd`** ou au flip mode. Notre workflow actuel : capital virtuel $1000 stable → pas de faux positif observé.
- **C-004** ne mord **qu'en cas de panne mid transitoire**. WSS + fallback HTTP M11 + cache TTL rendent rare.
- **C-005** visible sur dashboard : utilisateur a remarqué `total_usdc $1005 vs calcul $1006.50` (UX) mais pas bloquant.

Ship MD **après** MA+MB+MC, idéalement avant MF pour éviter interférences data corrompue (bad realized_pnl influence les futurs calculs internal_pnl_score de MB).

## 3. Scope (items détaillés)

### MD.1 — Filtrer `simulated` dans `PositionSizer` + `RiskManager` (audit C-001)

- **Location** : [src/polycopy/strategy/pipeline.py:177-210](../../src/polycopy/strategy/pipeline.py#L177-L210) + [pipeline.py:263-274](../../src/polycopy/strategy/pipeline.py#L263-L274)
- **Ce qu'il faut faire** :
  - Aux 3 queries `MyPosition WHERE closed_at IS NULL`, ajouter le filtre :
    ```python
    simulated_value = settings.execution_mode != "live"
    stmt = select(MyPosition).where(
        MyPosition.condition_id == ctx.trade.condition_id,
        MyPosition.closed_at.is_(None),
        MyPosition.simulated == simulated_value,  # NOUVEAU
    )
    ```
  - Pattern déjà utilisé par `repositories.py::upsert_on_fill` vs `upsert_virtual` — à propager aux 3 sites.
  - **Décision D1** : binaire `mode != "live" → simulated=True`. Ne distingue pas SIMULATION vs DRY_RUN (même flag). La distinction propre passe par `execution_mode` column (MD.3).
  - Vérifier qu'aucun autre query `MyPosition WHERE closed_at IS NULL` ailleurs dans le code n'oublie ce filtre (grep).
- **Tests requis** :
  - `test_position_sizer_ignores_virtual_positions_in_live_mode`
  - `test_risk_manager_ignores_virtual_positions_in_live_mode`
  - `test_dry_run_still_sees_only_virtual_positions`
  - `test_no_cross_mode_pollution_after_flip`
- **Sources** : Audit C-001 + Session E E1.
- **Charge item** : 0.5 jour

### MD.2 — Bypass digest + cooldown pour alertes CRITICAL (audit C-002 + M-009)

- **Location** : [src/polycopy/monitoring/alert_dispatcher.py:117-145](../../src/polycopy/monitoring/alert_dispatcher.py#L117-L145) + [src/polycopy/monitoring/alert_digest.py:44-63](../../src/polycopy/monitoring/alert_digest.py#L44-L63)
- **Ce qu'il faut faire** :
  - Dans `AlertDispatcher.dispatch(alert)`, **early-return bypass** si `alert.level == "CRITICAL"` :
    ```python
    if alert.level == "CRITICAL":
        # Bypass digest window ET cooldown
        await self._telegram_client.send(alert)
        # Toujours écrire en event trace pour audit
        return
    # Sinon logique existante (digest + cooldown)
    ```
  - **Ordre strict dans `PnlSnapshotWriter._maybe_trigger_alerts`** : écrire sentinel `halt.flag` **AVANT** `stop_event.set()` **AVANT** push alert. Confirmer que c'est déjà le cas (CLAUDE.md §Sécurité dit "touch sentinel avant stop_event.set()"). Si pas respecté → fix.
- **Tests requis** :
  - `test_critical_alert_bypasses_digest_window`
  - `test_critical_alert_bypasses_cooldown`
  - `test_kill_switch_touches_sentinel_before_stop_event`
  - `test_non_critical_alerts_still_digested_and_cooldowned`
- **Sources** : Audit C-002 + M-009 + Session E E2.
- **Charge item** : 0.5 jour

### MD.3 — Migration Alembic 0008 : `PnlSnapshot.execution_mode` column (audit C-003 + M-020)

- **Location** : nouveau fichier `alembic/versions/0008_pnl_snapshot_execution_mode.py` + [src/polycopy/storage/models.py](../../src/polycopy/storage/models.py) + [src/polycopy/monitoring/pnl_writer.py:86-98](../../src/polycopy/monitoring/pnl_writer.py#L86-L98) + [src/polycopy/storage/repositories.py:809-817](../../src/polycopy/storage/repositories.py#L809-L817)
- **Ce qu'il faut faire** :
  - Migration 0008 :
    - ADD COLUMN `execution_mode VARCHAR(16) NOT NULL DEFAULT 'live'` à `pnl_snapshots`.
    - Backfill : `UPDATE pnl_snapshots SET execution_mode = 'dry_run' WHERE is_dry_run = 1`; `UPDATE pnl_snapshots SET execution_mode = 'live' WHERE is_dry_run = 0`.
    - Préserver `is_dry_run` pour rétro-compat lecture temporaire (on ne drop pas, mais on stoppe l'écriture).
  - `PnlSnapshotWriter._persist` écrit désormais `execution_mode=settings.execution_mode` (string literal "simulation" | "dry_run" | "live").
  - `get_max_total_usdc()` prend nouveau param `execution_mode: Literal[...]` et filtre strict. Callers à mettre à jour.
  - **Décision D2** : binary migration atomique via `op.batch_alter_table` (SQLite-friendly), backfill in-place.
  - **Décision D3** : conserver `is_dry_run` column pour 1 version puis deprecate (pattern cohérent avec `DRY_RUN` → `EXECUTION_MODE` M10).
- **Tests requis** :
  - `test_migration_0008_upgrade_downgrade_idempotent`
  - `test_migration_0008_backfill_execution_mode_correct`
  - `test_get_max_total_usdc_segregates_by_mode`
  - `test_kill_switch_does_not_false_positive_on_flip_mode`
  - `test_pnl_snapshot_writes_execution_mode`
- **Sources** : Audit C-003 + M-020 + Session E E3.
- **Charge item** : 1 jour

### MD.4 — `VirtualWalletStateReader` fallback `last_known_mid` (audit C-004)

- **Location** : [src/polycopy/executor/virtual_wallet_reader.py:48-71](../../src/polycopy/executor/virtual_wallet_reader.py#L48-L71)
- **Ce qu'il faut faire** :
  - Maintenir un dict `_last_known_mid: dict[asset_id, tuple[Decimal, datetime]]` interne.
  - Dans la boucle positions : si `ClobMidpointClient.get()` renvoie `None` ET `last_known` existe ET `age < 10 min` → utiliser `last_known`. Sinon si `age >= 10 min` → lever `MidpointUnavailableError`.
  - `PnlSnapshotWriter` catch `MidpointUnavailableError` → log WARNING `pnl_snapshot_skipped_midpoint_unavailable` + skip ce snapshot (retry au prochain tick, pas de corruption).
  - **Décision D4** : 10 min de tolérance sur last_known (= 2× `PNL_SNAPSHOT_INTERVAL=300s` default). Compromis entre résilience panne transitoire et fraîcheur.
  - Expose nouvelle exception dans `executor/__init__.py` pour que le writer puisse la catch.
- **Tests requis** :
  - `test_virtual_wallet_uses_last_known_mid_on_transient_none`
  - `test_virtual_wallet_raises_on_mid_outage_exceeding_10min`
  - `test_pnl_writer_skips_snapshot_on_midpoint_unavailable`
  - `test_last_known_mid_evicted_after_ttl`
- **Sources** : Audit C-004 + Session E E4.
- **Charge item** : 0.5 jour

### MD.5 — Fusionner `dry_run_virtual_capital_usd` et `dry_run_initial_capital_usd` (audit H-004)

- **Location** : [src/polycopy/config.py:315](../../src/polycopy/config.py#L315) + [config.py:359](../../src/polycopy/config.py#L359) + [src/polycopy/executor/virtual_wallet_reader.py:62](../../src/polycopy/executor/virtual_wallet_reader.py#L62) + [src/polycopy/dashboard/queries.py:965](../../src/polycopy/dashboard/queries.py#L965)
- **Ce qu'il faut faire** :
  - **Décision D5** : conserver `DRY_RUN_INITIAL_CAPITAL_USD` (plus explicite) comme source unique. Déprécier `DRY_RUN_VIRTUAL_CAPITAL_USD`.
  - Pydantic validator : lecture de `DRY_RUN_VIRTUAL_CAPITAL_USD` legacy avec warning `config_deprecation_dry_run_virtual_capital_env`, fallback `DRY_RUN_INITIAL_CAPITAL_USD`. Pattern cohérent avec M10 dépréciation `DRY_RUN=true/false`.
  - Propager dans `VirtualWalletStateReader`, `pnl_writer`, `queries.py` pour n'utiliser que `dry_run_initial_capital_usd`.
  - `.env.example` commenté : "DEPRECATED : utilise DRY_RUN_INITIAL_CAPITAL_USD".
- **Tests requis** :
  - `test_dry_run_initial_capital_is_single_source_of_truth`
  - `test_deprecation_warning_logged_on_legacy_virtual_capital_var`
  - `test_legacy_fallback_if_new_unset`
- **Sources** : Audit H-004 + Session E E5.
- **Charge item** : 0.5 jour

### MD.6 — Peupler `PnlSnapshotDTO.realized_pnl + unrealized_pnl` au lieu de 0.0 (audit H-002)

- **Location** : [src/polycopy/monitoring/pnl_writer.py:90-98](../../src/polycopy/monitoring/pnl_writer.py#L90-L98)
- **Ce qu'il faut faire** :
  - `PnlSnapshotWriter._persist` calcule :
    ```python
    realized_pnl_cumulative = await self._wallet_state_reader.get_realized_pnl_cumulative(
        mode=settings.execution_mode
    )
    unrealized_pnl_current = total_usdc - initial_capital - realized_pnl_cumulative
    ```
    Et persiste les deux valeurs au lieu de `0.0`.
  - `WalletStateReader.get_realized_pnl_cumulative()` : nouvelle méthode helper qui agrège `my_positions.realized_pnl` filtrée par mode.
  - **Impact downstream** : les milestones `/pnl` et les sparklines `/home` qui consomment ces colonnes voient désormais les vraies valeurs (au lieu de 0 partout).
  - **Décision D6** : rétro-remplir les snapshots historiques est **hors scope** (optionnel script `scripts/backfill_pnl_snapshot_realized.py` déférable si user demande).
- **Tests requis** :
  - `test_pnl_snapshot_populates_realized_pnl_nonzero_when_positions_closed`
  - `test_pnl_snapshot_unrealized_matches_formula`
  - `test_dashboard_consumes_pnl_snapshot_values_not_inline_recompute` (régression test → /home et /performance doivent converger)
- **Sources** : Audit H-002 + Session E (nouveau).
- **Charge item** : 0.5 jour

### MD.7 — Écrire `TraderEvent(event_type="kill_switch")` (audit H-005)

- **Location** : [src/polycopy/monitoring/pnl_writer.py:127-155](../../src/polycopy/monitoring/pnl_writer.py#L127-L155) + [src/polycopy/storage/models.py TraderEvent](../../src/polycopy/storage/models.py) + [src/polycopy/dashboard/queries.py:1119-1133](../../src/polycopy/dashboard/queries.py#L1119-L1133)
- **Ce qu'il faut faire** :
  - Avant `stop_event.set()` dans `_maybe_trigger_alerts`, écrire :
    ```python
    await self._events_repo.insert_event(
        wallet_address=None,  # system-level event, pas wallet-specific
        event_type="kill_switch",
        event_metadata={
            "drawdown_pct": drawdown_pct,
            "total_usdc": total_usdc,
            "max_total_usdc": max_total_usdc,
            "execution_mode": settings.execution_mode,
            "threshold": settings.kill_switch_drawdown_pct,
        },
    )
    ```
  - **Attention** : `trader_events.wallet_address` doit être **nullable** (event système). Vérifier le schéma (migration possible si pas nullable). Si migration, faire dans 0008 (MD.3) pour regrouper.
  - `get_pnl_milestones` dashboard query consomme désormais les events `kill_switch` et les affiche avec icône + timestamp.
- **Tests requis** :
  - `test_kill_switch_writes_trader_event_system_level`
  - `test_pnl_milestones_includes_kill_switch_events`
  - `test_trader_events_wallet_nullable_backward_compat` (si migration)
- **Sources** : Audit H-005 + Session E (nouveau).
- **Charge item** : 0.5 jour

### MD.8 — (Bonus) Single-flight TOCTOU sur caches async (audit M-007)

- **Location** : [src/polycopy/strategy/gamma_client.py:88-108](../../src/polycopy/strategy/gamma_client.py#L88-L108) + [src/polycopy/executor/clob_orderbook_reader.py:58-76](../../src/polycopy/executor/clob_orderbook_reader.py#L58-L76) + [src/polycopy/strategy/clob_ws_client.py:237-268](../../src/polycopy/strategy/clob_ws_client.py#L237-L268)
- **Ce qu'il faut faire** :
  - Pattern TOCTOU commun : `read cache → await fetch → write cache` sans lock → N coroutines simultanées sur même clé font N fetches.
  - Fix pattern `_inflight: dict[str, asyncio.Future]` :
    ```python
    async def fetch_key(key):
        if key in self._cache and not self._cache[key].expired():
            return self._cache[key].value
        if key in self._inflight:
            return await self._inflight[key]  # join existing fetch
        fut = asyncio.create_task(self._do_fetch(key))
        self._inflight[key] = fut
        try:
            val = await fut
            self._cache[key] = CacheEntry(val, now())
            return val
        finally:
            del self._inflight[key]
    ```
  - Appliquer aux 3 clients listés. **Optionnel** si charge MD serrée — peut migrer en MI ou MJ.
- **Tests requis** :
  - `test_gamma_client_single_flight_prevents_redundant_fetches` (simuler 10 coroutines concurrentes sur même condition_id)
  - `test_clob_orderbook_reader_single_flight`
  - `test_clob_ws_client_subscription_single_flight`
- **Sources** : Audit M-007.
- **Charge item** : 1 jour (optionnel)

## 4. Architecture / décisions clefs

- **D1** : filtre `simulated = (mode != "live")` binaire, pas tri-state. Justification : simpler, SIMULATION et DRY_RUN sont tous deux `simulated=True` côté MyPosition. Ségrégation tri-state passe par `execution_mode` column (MD.3).
- **D2** : migration 0008 atomique batch_alter_table. Justification : SQLite-friendly, cohérent avec migration 0004 M8 pattern.
- **D3** : `is_dry_run` column conservée 1 version, puis deprecated. Justification : pattern cohérent M10 DRY_RUN env var.
- **D4** : tolérance `last_known_mid` = 10 min (2× snapshot interval). Justification : résilience sans staleness excessive.
- **D5** : `DRY_RUN_INITIAL_CAPITAL_USD` gagne comme source unique. Justification : naming plus explicite, pattern M13 preset A/B/C.
- **D6** : backfill historique PnlSnapshots hors scope. Justification : trop intrusif, valeurs historiques lues via inline recompute tant que nouvelles valeurs écrites sont correctes.
- **D7** : MD.8 single-flight TOCTOU est **optionnel**. Justification : charge MD sinon explosive, peut migrer en MI ops hygiene.

## 5. Invariants sécurité

- **Triple garde-fou M3 + 4ᵉ M8** : intact. MD n'ajoute aucune surface de signature CLOB, uniquement des fix de cohérence data.
- **Kill switch parité 3 modes** (M10) : MD.2 + MD.3 **renforcent** l'invariant (bypass CRITICAL + segregation par mode).
- **Sentinel `halt.flag` permissions 0o600 + parent 0o700** : intact (MD.2 n'ajoute que l'ordre d'écriture).
- **Zéro secret loggé** : les nouveaux events structlog (`pnl_snapshot_skipped_midpoint_unavailable`, `kill_switch_written_to_events`) n'incluent que numeric + mode string. Test grep.
- **Append-only scoring versions** : MD ne touche pas au scoring (zéro impact MA/MB).
- **Migration 0008 data integrity** : backfill idempotent, rollback propre (`is_dry_run` column preserved 1 version).

## 6. Hypothèses empiriques à valider AVANT ship

- **Aucune hypothèse critique** — MD est du patching audit, les comportements corrects sont déterministes.
- **Validation post-ship uniquement** : 
  - Test E2E : flip `EXECUTION_MODE=dry_run → live` avec 3 positions virtuelles traînantes → BUY live passent (MD.1 valide).
  - 3 kill_switch_triggered en 1 s → tous 3 émis immédiatement, `stop_event.set()` < 100 ms après le 1er (MD.2 valide).
  - Séquence SIMULATION $50k → DRY_RUN $10k → premier tick DRY_RUN calcule drawdown vs $10k (MD.3 valide).

## 7. Out of scope

- **TOCTOU audit M-007 single-flight** : item MD.8, **optionnel** — peut migrer en **MI** si charge MD serrée.
- **M-008 N+1 queries dans `get_home_alltime_stats`** : migre en **MH** (UX + perf dashboard).
- **M-012 Float → Numeric/Decimal migration** : trop invasif pour MD, spec future si arrondis cumulés deviennent mesurables.
- **M-019 `DryRunResolutionWatcher` retry cap neg_risk scalar** : spec M13 extension, hors scope MD.
- **M-014 validator cross-field TARGET_WALLETS ∩ BLACKLISTED_WALLETS** : migre en **MI** ops hygiene.
- **H-010 + H-011 RateLimiter + Sentinel TOCTOU (remote_control)** : hors scope M12_bis, migre en MI ou spec future sécurité.
- **H-012 Migration 0007 non-transactionnelle** : pattern audit historique, migre MI si scope.
- **Rétro-backfill PnlSnapshots historiques** : hors scope (script optionnel si user demande).

## 8. Success criteria

1. **Tests ciblés verts** : ~15 nouveaux tests unit + 3 integration.
2. **Migration 0008 propre** : `alembic upgrade head` + `alembic downgrade -1` sans perte data, idempotent.
3. **Flip mode safe** : E2E test flip `dry_run → live` avec positions traînantes → aucun BUY bloqué à tort.
4. **Kill switch immédiat** : E2E test 3 CRITICAL en 1s → les 3 alertes envoyées + stop_event set() < 100ms.
5. **Dashboard cohérent** : `/home` et `/performance` affichent le **même** total realized_pnl (écart < 1 cent).
6. **Milestones `/pnl` peuplés** : post-ship, un kill_switch trigger apparaît dans la timeline `/pnl` (au moins sur test injection).

## 9. Mapping origines (traçabilité)

| Item | Audit | Session | Deep-search | Synthèse roadmap |
|---|---|---|---|---|
| MD.1 | [C-001] | E (E1) | — | #21 |
| MD.2 | [C-002] + [M-009] | E (E2) | — | #22 |
| MD.3 | [C-003] + [M-020] | E (E3) | — | #23 |
| MD.4 | [C-004] | E (E4) | — | #24 |
| MD.5 | [H-004] | E (E5) | — | #25 |
| MD.6 | [H-002] | E (new) | — | — |
| MD.7 | [H-005] | E (new) | — | — |
| MD.8 | [M-007] | E (new, optional) | — | #36 |

## 10. Prompt de génération de spec

````markdown
# Contexte

Lis `docs/next/MD.md` en entier. C'est le brief actionnable du module MD
(Cross-layer integrity patches). Il regroupe 5 CRITICALs + 3 HIGH de l'audit
code 2026-04-24, all fix de cohérence inter-couches silencieux tant qu'on
reste en dry-run stable.

# Tâche

Produire `docs/specs/M17-cross-layer-integrity.md` suivant strictement le
format des specs M1..M16 existantes (§ numérotées habituelles).

Numéro : M17 (après MA=M14, MB=M15, MC=M16).

# Prérequis (à lire avant de rédiger)

- `CLAUDE.md` §Conventions + §Sécurité (invariants M3+M8 triple/4ᵉ garde-fou,
  kill switch parité 3 modes M10, sentinel halt.flag order-strict)
- `docs/specs/M13_dry_run_observability_spec.md` comme template de forme
- `docs/specs/M4-monitoring.md` + `docs/specs/M10-parity-and-log-hygiene.md`
  comme référence contenu monitoring/kill switch
- `docs/audit/2026-04-24-polycopy-code-audit.md` sections C-001, C-002, C-003,
  C-004, C-005, H-002, H-004, H-005, M-007, M-009

# Contraintes

- Lecture seule src/ + docs
- Écriture uniquement `docs/specs/M17-cross-layer-integrity.md`
- Longueur cible : 1000-1400 lignes
- **Migration Alembic 0008** : documenter complète, avec SQL upgrade + downgrade
- Ordre commits recommandé : MD.1 → MD.2 → MD.5 → MD.4 → MD.6 → MD.3 (migration)
  → MD.7 → MD.8 (optional)

# Livrable

- Le fichier `docs/specs/M17-cross-layer-integrity.md` complet
- Un ping final ≤ 10 lignes : tests estimés, charge cumulée, ordre commits
````

## 11. Notes d'implémentation

### Piège : ordering MD.3 migration vs MD.6/MD.7

**MD.3 inclut la migration 0008 `execution_mode` column sur `pnl_snapshots`**. MD.6 et MD.7 **n'ont pas besoin** de migration structurelle (peuplement de colonnes existantes + écriture `trader_events`).

**MAIS** : MD.7 peut nécessiter `trader_events.wallet_address` nullable. Vérifier le schéma actuel. Si pas nullable, inclure dans migration 0008 pour éviter 2 migrations.

### Piège : backward compat lecture `is_dry_run`

Post-migration 0008, les snapshots historiques ont `execution_mode` backfillé. Mais si un bug regression écrase `execution_mode='live'` par défaut à tort sur un DRY_RUN, on lit la mauvaise valeur. **Mitigation** : CHECK constraint `execution_mode IN ('simulation', 'dry_run', 'live')` + default `'live'` explicite à l'insert (pas NULL).

### Piège : auto-lockdown CRITICAL alertes

MD.2 bypass digest + cooldown pour CRITICAL. **Attention** : cela peut créer un **flood** d'alertes Telegram si le kill switch se déclenche en cascade (ex: drawdown franchit seuil à chaque snapshot 5 min → 1 alerte / 5 min). Considérer une **protection au-delà** : même CRITICAL, un event_type donné ne doit pas re-fire plus d'1× par minute (cooldown_key strict). Claude §E2 dit "bypass cooldown" mais il faut préserver idempotence.

**Solution** : bypass uniquement le **digest**, conserver cooldown 60s strict par `cooldown_key`. Re-fire du même kill_switch après 60s = message spam utile (escalation signal), pas flood.

### Piège : `MidpointUnavailableError` vs `stop_event`

MD.4 lève `MidpointUnavailableError` qui bubble up au `PnlSnapshotWriter`. Le writer catch et skip le snapshot. **Attention** : si panne mid prolongée (>10 min), le writer skippe plusieurs snapshots → le `max_total_usdc` ne progresse plus → au retour mid, le `current total_usdc` peut se comparer à un max obsolète. **Acceptable** : drawdown vs un max ancien reste une approximation, pas pire que le bug C-004 initial.

### Références audit

- **C-001** [audit §3 CRITICAL](../../docs/audit/2026-04-24-polycopy-code-audit.md#L60)
- **C-002** [audit §3 CRITICAL](../../docs/audit/2026-04-24-polycopy-code-audit.md#L68)
- **C-003** [audit §3 CRITICAL](../../docs/audit/2026-04-24-polycopy-code-audit.md#L76)
- **C-004** [audit §3 CRITICAL](../../docs/audit/2026-04-24-polycopy-code-audit.md#L85)
- **C-005** [audit §3 CRITICAL](../../docs/audit/2026-04-24-polycopy-code-audit.md#L94)
- **H-002** [audit §3 HIGH](../../docs/audit/2026-04-24-polycopy-code-audit.md#L112)
- **H-004** [audit §3 HIGH](../../docs/audit/2026-04-24-polycopy-code-audit.md#L126)
- **H-005** [audit §3 HIGH](../../docs/audit/2026-04-24-polycopy-code-audit.md#L134)
- **M-007** [audit §3 MEDIUM](../../docs/audit/2026-04-24-polycopy-code-audit.md#L250)
- **M-009** [audit §3 MEDIUM](../../docs/audit/2026-04-24-polycopy-code-audit.md#L260)
