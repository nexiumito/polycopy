# Session E — Cross-layer integrity + security hardening

**Priorité** : 🔥 #1 ex æquo avec A (CRITICALs non couverts par A/B/C/D)
**Charge estimée** : M (1-2 jours, 5 items ciblés)
**Branche suggérée** : `fix/cross-layer-integrity`

---

## Origine

Session identifiée par l'**audit code 2026-04-24** ([docs/audit/2026-04-24-polycopy-code-audit.md](../audit/2026-04-24-polycopy-code-audit.md)).
Les 5 items ci-dessous sont des **CRITICALs cross-couche** qui n'entrent dans
aucune des 4 sessions existantes (A/B/C/D) parce qu'ils touchent plusieurs
modules en même temps (strategy ↔ storage, monitoring ↔ alerts, executor ↔
dashboard, config).

## Objectif business

Corriger les défauts structurels révélés par l'audit qui peuvent :
- **Bloquer silencieusement** tous les BUY live après un flip de mode (E1)
- **Retarder le kill switch** de 5 min lors de multiples CRITICAL simultanés (E2)
- Déclencher un **faux-positif kill switch** au flip SIMULATION/DRY_RUN/LIVE (E3)
- **Corrompre le calcul de drawdown** sur panne midpoint transitoire (E4)
- **Désaligner** les 2 settings de capital initial dry-run (E5)

Ces bugs **s'activent pendant le test 14 j ou au passage live** — blocker
direct pour le cutover business.

## Items

### E1 — Filtre `simulated` manquant dans strategy/risk (CRITICAL [C-001])

**Location** : [src/polycopy/strategy/pipeline.py:177-210](../../src/polycopy/strategy/pipeline.py#L177-L210) + [src/polycopy/strategy/pipeline.py:263-274](../../src/polycopy/strategy/pipeline.py#L263-L274)

**Bug** : `_check_buy`, `_check_sell`, `RiskManager.check` requêtent
`MyPosition WHERE closed_at IS NULL` **sans filtrer `simulated`**. Un flip
`dry_run → live` avec des positions virtuelles traînantes bloque silencieusement
tous les BUY réels (reason `position_already_open`) et pollue l'exposition.

**Fix** : ajouter `MyPosition.simulated == (settings.execution_mode != "live")`
aux 3 queries. Pattern déjà présent dans `repositories.py::upsert_on_fill` vs
`upsert_virtual` — à propager.

**Tests** : `test_position_sizer_ignores_virtual_positions_in_live_mode`,
`test_risk_manager_ignores_virtual_positions_in_live_mode`,
`test_dry_run_still_sees_only_virtual_positions`.

### E2 — Kill switch CRITICAL bypass digest + cooldown=0 ([C-002] + [M-009])

**Location** : [src/polycopy/monitoring/alert_dispatcher.py:117-145](../../src/polycopy/monitoring/alert_dispatcher.py#L117-L145) + [src/polycopy/monitoring/alert_digest.py:44-63](../../src/polycopy/monitoring/alert_digest.py#L44-L63)

**Bug** : `AlertDispatcher` fait passer **toutes** les alertes par
`self._digest.register(alert, now)`, y compris les `level=CRITICAL`. Un
kill_switch peut se retrouver batché dans la fenêtre digest 5 min → le
`stop_event.set()` est retardé d'autant. En parallèle, le cooldown 60 s
s'applique aussi aux CRITICAL → un 2e `executor_auth_fatal` à 30 s est drop.

**Fix** :
- Bypass `self._digest.register()` et `cooldown` si `alert.level == "CRITICAL"`
  → émission immédiate.
- **Ordre strict dans `PnlSnapshotWriter._maybe_trigger_alerts`** : `touch
  halt.flag` AVANT `stop_event.set()` AVANT push alert (sinon respawn peut
  redémarrer avant que le sentinel n'existe).

**Tests** : `test_critical_alert_bypasses_digest_window`,
`test_critical_alert_bypasses_cooldown`,
`test_kill_switch_touches_sentinel_before_stop_event`.

### E3 — Colonne `execution_mode` sur `PnlSnapshot` (migration 0008) ([C-003] + [M-020])

**Location** : [src/polycopy/monitoring/pnl_writer.py:86-98](../../src/polycopy/monitoring/pnl_writer.py#L86-L98) + [src/polycopy/storage/repositories.py:809-817](../../src/polycopy/storage/repositories.py#L809-L817)

**Bug** : aujourd'hui `PnlSnapshot.is_dry_run: bool` capture uniquement
`live ↔ non-live`. Un run SIMULATION avec `virtual_capital=$50k` puis un run
DRY_RUN avec `$10k` écrivent dans le **même bucket** `is_dry_run=True`. Le
`get_max_total_usdc(only_real=False)` renvoie le max des deux → drawdown faux
au flip.

**Fix** :
- Migration Alembic `0008_pnl_snapshot_execution_mode` : ajout colonne
  `execution_mode: Literal["simulation", "dry_run", "live"]` avec backfill
  `is_dry_run=False → "live"`, `is_dry_run=True → "dry_run"`.
- `get_max_total_usdc` prend `execution_mode: str` en param → filtre strict.
- **Migration data atomique** (transaction SQLite).

**Tests** : `test_pnl_snapshot_execution_mode_column_backfill`,
`test_get_max_total_usdc_segregates_by_mode`, `test_kill_switch_does_not_false_positive_on_flip_mode`.

### E4 — `VirtualWalletStateReader` fallback `last_known_mid` ([C-004])

**Location** : [src/polycopy/executor/virtual_wallet_reader.py:48-71](../../src/polycopy/executor/virtual_wallet_reader.py#L48-L71)

**Bug** : si `ClobMidpointClient.get()` renvoie `None` (panne WS+HTTP
transitoire), la position est `continue`-skipped → `total_usdc` artificiellement
bas → le max historique baisse → le tick suivant avec mid OK remonte →
**drawdown factice**.

**Fix** : maintenir un dict `last_known_mid: dict[asset_id, (mid, timestamp)]`
à l'intérieur du reader. Si mid actuel est `None` ET last_known existe et
`age < 5 min` → utiliser last_known. Si panne > 5 min → **lever exception
`MidpointUnavailableError`** qui fait skip le snapshot entier (plutôt qu'un
`total_usdc` corrompu). Le `PnlSnapshotWriter` log WARNING et retry au tick
suivant.

**Tests** : `test_virtual_wallet_uses_last_known_mid_on_transient_none`,
`test_virtual_wallet_raises_on_mid_outage_exceeding_5min`,
`test_pnl_writer_skips_snapshot_on_midpoint_unavailable`.

### E5 — Fusionner `dry_run_virtual_capital_usd` + `dry_run_initial_capital_usd` ([H-004])

**Location** : [src/polycopy/config.py:315](../../src/polycopy/config.py#L315) + [src/polycopy/config.py:359](../../src/polycopy/config.py#L359) + [src/polycopy/executor/virtual_wallet_reader.py:62](../../src/polycopy/executor/virtual_wallet_reader.py#L62) + [src/polycopy/dashboard/queries.py:965](../../src/polycopy/dashboard/queries.py#L965)

**Bug** : deux settings distincts représentent la même chose. `VirtualWalletStateReader`
utilise `dry_run_virtual_capital_usd` pour `total_usdc`. Dashboard utilise
`dry_run_initial_capital_usd` pour le `open_latent_pnl_usd`. Divergence →
latent PnL faux.

**Fix** :
- Conserver `DRY_RUN_INITIAL_CAPITAL_USD` (plus explicite) comme source unique.
- Déprécier `DRY_RUN_VIRTUAL_CAPITAL_USD` (lu 1 version avec warning
  `config_deprecation_dry_run_virtual_capital`, fallback sur
  `DRY_RUN_INITIAL_CAPITAL_USD`).
- Propager dans `VirtualWalletStateReader`, `pnl_writer`, `queries.py`.

**Tests** : `test_dry_run_initial_capital_is_single_source_of_truth`,
`test_deprecation_warning_logged_on_legacy_virtual_capital_var`.

## Hypothèses à valider

- **H-E1** : est-ce qu'on veut **jamais** voir une position virtuelle en
  live (alternative : tolérer, afficher tagged "dry-run remainder") ? Décision
  retenue par défaut : **jamais** (positions live et virtuelles strictement
  disjointes, conforme M8 invariants).
- **H-E2** : le `halt.flag` sentinel doit-il être écrit avant ou après
  `stop_event.set()` ? CLAUDE.md §M12_bis dit **avant** (sinon respawn
  unsafe). À confirmer dans l'implémentation.
- **H-E4** : 5 min de tolérance sur `last_known_mid` est-ce trop long ?
  À comparer vs la fréquence des snapshots (défaut `PNL_SNAPSHOT_INTERVAL=300s
  = 5min`). Probablement mettre 2× l'interval = 10 min.

## Livrables

- Migration Alembic 0008
- Patches ciblés sur `pipeline.py`, `alert_dispatcher.py`, `alert_digest.py`,
  `pnl_writer.py`, `virtual_wallet_reader.py`, `config.py`, `queries.py`
- ~15 tests unit (3-4 par fix)
- 2 tests integration (E2 kill switch bypass + E3 flip mode no false positive)
- Mise à jour CLAUDE.md §Executor (garde-fou 5 : simulated filter), §Monitoring
  (CRITICAL bypass), §M8 (last_known_mid fallback)
- Mise à jour `.env.example` deprecation `DRY_RUN_VIRTUAL_CAPITAL_USD`

## Out of scope

- Pas de refacto complet de `AlertDispatcher` — juste le bypass CRITICAL ciblé.
- Pas d'ajout d'alerte "virtual position detected in live mode" (nice to have,
  peut venir session F).
- Pas de fix des autres bugs `monitoring/pnl_writer.py` ([H-002], [H-005]) qui
  vont dans un plan global séparé si besoin.

## Success criteria

1. Démo flip `EXECUTION_MODE=dry_run → live` avec 3 positions virtuelles
   traînantes → les BUY live passent (E1 validé).
2. 3 kill_switch_triggered en 1 s → tous 3 émis immédiatement, `stop_event.set()`
   < 100 ms après le 1er (E2 validé).
3. Séquence SIMULATION $50k → DRY_RUN $10k → premier tick DRY_RUN calcule
   drawdown vs $10k uniquement, pas vs $50k (E3 validé).
4. Panne mid 2 min sur une position → `total_usdc` reste cohérent avec
   last_known_mid, pas de faux drawdown (E4 validé).
5. `DRY_RUN_VIRTUAL_CAPITAL_USD=800` + `DRY_RUN_INITIAL_CAPITAL_USD=1000`
   simultanés → warning deprecation + value=1000 utilisée partout (E5 validé).
