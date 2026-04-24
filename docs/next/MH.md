# MH — Dashboard UX polish + consistency

**Priorité** : 🟡 P3 (slack time, UX polish)
**Charge estimée** : M (2-3 jours)
**Branche suggérée** : `feat/dashboard-ux-polish`
**Prérequis** : aucun (indépendant)
**Bloque** : — (parallélisable à toute autre session)

---

## 1. Objectif business

Lever les **frictions quotidiennes** de l'utilisation du dashboard observées pendant la session d'audit 2026-04-24. L'utilisateur a spécifiquement signalé : impossibilité de copier une adresse wallet non-tronquée, colonnes illisibles (`Size 0.00`), métriques biaisées (`APPROVE STRATÉGIE 5.0%`), confusion PnL latent vs réalisé vs gain max, divergences d'arrondi TOTAL USDC. Pas bloquant pour le test business mais améliore **significativement** l'efficacité quotidienne d'audit + debug. Ajoute les visualisations produites par MA/MB/MF (stability metric scoring, badge wash-risk, delta top-10 v2.2).

## 2. Contexte & problème observé

### Frictions utilisateur 2026-04-24

- **Adresses tronquées partout** : signalé explicitement par user session audit : "Je n'arrive pas à blacklist car je n'ai pas de moyen de récupérer l'adresse complète". Dû passer par SQL SSH sur debian.
- **`Size 0.00` sur `/activité`** : toutes les lignes affichent `Size 0.00` alors que les sizes réelles sont `0.001-0.05` shares (copy_ratio 0.01 × source size). Colonne inutile visuellement.
- **`APPROVE STRATÉGIE: 5.0%`** sur `/home` : 95% trades rejetés. Mais biaisé car `trade_detected` non-reset au reset positions/capital post-M13 Bug 5 fix. Donné par user comme source de confusion.
- **Confusion PnL** : user demande "1,006 USDC, PnL réalisé -0.54, PnL latent +7.04, je ne comprend pas trop...". Décomposition correcte (`total = initial + realized + latent`) mais pas évidente sans tooltip.
- **Arrondi `TOTAL USDC`** : affiche $1006 mais calcul exact = $1006.50. Tronqué à l'unité alors que les autres champs sont en cents.
- **Spearman rank `/scoring`** : affichage ranks pool-entier (33, 45, 52) malgré commit `1ba8ae3` qui devait corriger. Fix incomplet.

### Findings référencés

- **Audit `/activité` Size 0.00** : [L-027 docs/audit §3](../../docs/audit/2026-04-24-polycopy-code-audit.md).
- **Audit M-010 Win rate break-even exclusion** : "wins=count(>0), losses=count(<0), decided=wins+losses. Break-even exclu du dénominateur. 100% WR affiché avec 5 break-even".
- **Audit M-011 Gain max latent assume YES pour toutes positions** : "formule `(1 − avg_price) × size`. Pour NO, upside est `avg_price × size`. Formule invalide pour NO".
- **Audit L-004 Sparkline filtre `is_dry_run=False` mais `latest_snapshot` pas filtré** : incohérence 24h vs dernier point.
- **Audit L-005 `_format_card_usd` entiers vs `format_usd` 2 décimales** : rupture visuelle /home vs /activity.
- **Audit M-008 N+1 queries dans `get_home_alltime_stats`** : slow sur /home à partir de ~50 positions fermées.
- **Synthèse §6.3 Session C extensions** : dashboard `/scoring` stability metric (std sur N cycles), badge wash-risk post-MF, afficher v2.1 vs v2.1.1 vs v2.2 side-by-side avec delta rank, fee_drag column /performance.
- **Synthèse §8 H-EMP-2, H-EMP-6** : dashboard affichage post-MA (rank stability) + post-ME (latence split).
- **Audit I-008 Spearman implémentation** : "correctement implémenté post-commit 1ba8ae3, mais ranks locaux ≠ ranks pool-wide affichés → peut dérouter un lecteur. Cf. session B B5". Le fix code est là, le **display** ne reflète pas.

### Sessions originales mappées

**Session C** (`docs/bug/session_C_dashboard_ux_and_consistency.md`) items C1-C8 intégrés ici + extensions deep-search + findings audit L/M level.

## 3. Scope (items détaillés)

### MH.1 — Bouton copier adresse + tooltip fullhash sur toutes les vues

- **Location** : [src/polycopy/dashboard/templates/](../../src/polycopy/dashboard/templates/) + [src/polycopy/dashboard/jinja_filters.py](../../src/polycopy/dashboard/jinja_filters.py)
- **Ce qu'il faut faire** :
  - Nouvelle macro Jinja `render_wallet_address(address)` produisant :
    ```html
    <span class="wallet-addr" title="{{ address }}" data-addr="{{ address }}">
      {{ address[:6] }}…{{ address[-4:] }}
    </span>
    <button class="copy-btn" onclick="copyToClipboard('{{ address }}', this)">
      <i data-lucide="copy" class="w-3 h-3"></i>
    </button>
    ```
  - JS vanilla ~15 lignes ajouté dans `base.html` ou nouveau `static/js/copy.js` :
    ```javascript
    async function copyToClipboard(text, btn) {
      try {
        await navigator.clipboard.writeText(text);
        btn.classList.add('copied');
        setTimeout(() => btn.classList.remove('copied'), 1500);
      } catch {
        // Fallback : select + execCommand for older browsers
      }
    }
    ```
  - Appliquer la macro sur **toutes** les pages listant des wallets : `/home` (top trader + discovery), `/détection`, `/stratégie`, `/exécution` (source + cond_id), `/positions` (cond_id), `/pnl` (milestones), `/activité`, `/traders`, `/performance`, `/scoring v1/v2`, `/logs` (si wallet mentionné).
  - Appliquer aussi sur **condition_ids** (même pattern, utile pour SQL query ad-hoc).
  - **Décision D1** : pas de dépendance clipboard.js externe, vanilla JS suffit (~200 bytes). Cohérent M6 zéro build step.
  - Style Tailwind : `.copy-btn { opacity: 0.4; } .copy-btn:hover { opacity: 1; } .copy-btn.copied::after { content: "✓"; }`.
- **Tests requis** :
  - `test_render_wallet_address_macro_truncates_correctly`
  - `test_render_wallet_address_includes_full_address_in_title`
  - `test_copy_btn_rendered_on_every_wallet_display_page` (grep templates)
  - `test_no_secret_leak_via_copy_button` (le full address est public on-chain, OK)
- **Sources** : Session C C1 + user feedback direct 2026-04-24.
- **Charge item** : 0.5 jour

### MH.2 — Fix `Size 0.00` display sur `/activité`

- **Location** : [src/polycopy/dashboard/jinja_filters.py:22-42](../../src/polycopy/dashboard/jinja_filters.py#L22) + [src/polycopy/dashboard/templates/activity.html](../../src/polycopy/dashboard/templates/)
- **Ce qu'il faut faire** :
  - Nouveau filter Jinja `format_size_precise(size: float) -> str` :
    ```python
    def format_size_precise(size: float) -> str:
        if size == 0:
            return "0"
        if abs(size) >= 1:
            return f"{size:.2f}"
        if abs(size) >= 0.01:
            return f"{size:.3f}"
        if abs(size) >= 0.0001:
            return f"{size:.4f}"
        return f"{size:.2e}"  # Scientific notation for very small
    ```
  - Appliquer sur `/activité` column `Size`, `/exécution` column `Size`, `/positions` si size affiché.
  - Tooltip avec valeur exacte full precision : `<span title="{{ size }}">{{ size | format_size_precise }}</span>`.
  - **Décision D2** : 4 tiers de formatage (unité, 3 décimales, 4 décimales, scientifique). Balance lisibilité + précision.
- **Tests requis** :
  - `test_format_size_precise_scales_correctly`
  - `test_format_size_precise_scientific_under_0_0001`
  - `test_activity_template_uses_format_size_precise`
- **Sources** : Session C C2 + audit L-027.
- **Charge item** : 0.25 jour

### MH.3 — Métrique `APPROVE STRATÉGIE` en fenêtre glissante 24h

- **Location** : [src/polycopy/dashboard/queries.py](../../src/polycopy/dashboard/queries.py) + [src/polycopy/dashboard/templates/home.html](../../src/polycopy/dashboard/templates/)
- **Ce qu'il faut faire** :
  - Actuel : `strategy_approve_rate_pct` = `count(decision="APPROVE") / count(detected_trades) all-time`.
  - Problème : après reset positions/capital, `count(detected_trades)` continue à grossir sans reset → biais persistant.
  - **Fix** : base fenêtre glissante **24h** : `count(decision="APPROVE" in last 24h) / count(detected_trades in last 24h)`.
  - Cohérent avec les autres stats /home `trades_detected_24h`, `volume_24h`, etc.
  - Label UI : "APPROVE STRATÉGIE (24h)" au lieu de "APPROVE STRATÉGIE" (explicite).
  - **Alternative** (hors scope, pour user decision future) : ajouter commande CLI `polycopy reset-metrics` qui reset **aussi** `detected_trades`. Pas dans MH.
- **Tests requis** :
  - `test_strategy_approve_rate_uses_24h_window`
  - `test_strategy_approve_rate_label_indicates_24h`
- **Sources** : Session C C3 + user feedback 2026-04-24.
- **Charge item** : 0.25 jour

### MH.4 — Tooltips explicatifs PnL (latent/réalisé/gain max/exposition)

- **Location** : [src/polycopy/dashboard/templates/home.html](../../src/polycopy/dashboard/templates/)
- **Ce qu'il faut faire** :
  - Ajouter tooltip `<span title="...">` sur les 4 cartes KPI principales :
    - **PnL réalisé** : "Gains/pertes cristallisés sur positions fermées (SELL + résolutions marché). Définitif, ne bouge plus."
    - **PnL latent** : "Mark-to-market des positions ouvertes : Σ (mid_price - avg_buy) × size. Change avec les prix marché. Formule : total_usdc = initial_capital + realized_pnl + latent_pnl."
    - **Gain max latent** : "Payout théorique si toutes les positions YES gagnent ET toutes les NO perdent : Σ (1 - avg_price) × size sur YES + Σ avg_price × size sur NO. **Note** : aujourd'hui assume YES pour tout, fix F MH.5."
    - **Exposition** : "Capital engagé dans les positions ouvertes : Σ avg_price × size. Ce qu'on perdrait si tout tombe à 0."
    - **Drawdown** : "Chute depuis le plus haut historique du total_usdc. Fermeture quand ≥ KILL_SWITCH_DRAWDOWN_PCT."
    - **Win rate** : "Positions fermées avec realized_pnl > 0 / (wins + losses). Exclut break-even (fix F MH.6)."
  - Décision D3 : `<span title>` vanilla (natif HTML) + petite icône Lucide `info` à côté pour signaler qu'il y a une explication. Pas de modal complexe.
- **Tests requis** :
  - `test_home_kpi_cards_have_explanatory_tooltips`
- **Sources** : Session C C5 + user feedback 2026-04-24 "je ne comprend pas trop".
- **Charge item** : 0.25 jour

### MH.5 — Fix `Gain max latent` formule side-aware (audit M-011)

- **Location** : [src/polycopy/dashboard/queries.py:920-924](../../src/polycopy/dashboard/queries.py#L920) + [src/polycopy/dashboard/templates/home.html](../../src/polycopy/dashboard/templates/)
- **Ce qu'il faut faire** :
  - Actuel : `open_max_profit_usd = Σ (1 - avg_price) × size` pour **toutes** les positions.
  - Bug (audit M-011) : pour BUY NO, upside = `avg_price × size` (pas `1 - avg_price × size`).
  - Fix : joindre `DetectedTrade.outcome` (YES/NO) ou `MyPosition.asset_id` vs Gamma `clobTokenIds[0]` pour distinguer :
    ```python
    max_profit_per_position = (
        (1 - avg_price) × size if side == "YES"
        else avg_price × size  # BUY NO
    )
    ```
  - **Décision D4** : stocker `outcome_side` directement dans `MyPosition` (migration 0011 si nécessaire) pour éviter N+1 lookup. Alternative : compute à la volée via Gamma lookup cache.
  - Documenter dans tooltip MH.4 la correction.
- **Tests requis** :
  - `test_gain_max_latent_correct_for_buy_yes_position`
  - `test_gain_max_latent_correct_for_buy_no_position`
  - `test_gain_max_latent_aggregate_across_mixed_yes_no`
- **Sources** : Audit M-011 + Session C implicite.
- **Charge item** : 0.5 jour

### MH.6 — Fix `Win rate` break-even handling (audit M-010)

- **Location** : [src/polycopy/dashboard/queries.py:949-951](../../src/polycopy/dashboard/queries.py#L949) + cohérence /home + /performance
- **Ce qu'il faut faire** :
  - Actuel : `wins = count(>0), losses = count(<0), decided = wins + losses, win_rate = wins/decided`. Break-even (`realized_pnl == 0`) exclu du dénominateur.
  - Problème (audit M-010) : 5 break-even + 1 win = `100% win rate` affiché, faux signal.
  - **Décision D5** : convention "break-even = neutre, exclure du count mais **documenter** le count total". Afficher "Win rate 100% (1W/0L, 5 break-even)" au lieu de "Win rate 100%".
  - **Alternative** : convention "break-even = half-win" (ρ=0.5 contribution). Plus controversé, pas retenu.
  - Appliquer uniformément /home + /performance (convergence audit C-005 même formule).
- **Tests requis** :
  - `test_win_rate_excludes_break_even_from_denominator`
  - `test_win_rate_label_shows_break_even_count`
  - `test_win_rate_home_and_performance_converge` (régression test C-005)
- **Sources** : Audit M-010 + convergence C-005.
- **Charge item** : 0.25 jour

### MH.7 — Fix arrondi `TOTAL USDC` cohérent 2 décimales (audit L-005)

- **Location** : [src/polycopy/dashboard/queries.py:632-637](../../src/polycopy/dashboard/queries.py#L632) `_format_card_usd` + [src/polycopy/dashboard/jinja_filters.py:22-42](../../src/polycopy/dashboard/jinja_filters.py#L22) `format_usd`
- **Ce qu'il faut faire** :
  - Actuel : `_format_card_usd` format entiers (`$1006`), `format_usd` 2 décimales (`$1006.50`). Rupture visuelle.
  - **Décision D6** : unifier sur **2 décimales** partout. Sauf si nombre ≥ $10k → rounded to $K (format "1.2k") pour éviter cluttering.
  - Formule unifiée `format_usd` :
    ```python
    def format_usd(amount: float) -> str:
        if abs(amount) >= 10000:
            return f"${amount / 1000:.1f}k"
        return f"${amount:,.2f}"
    ```
  - Appliquer sur toutes les cartes KPI + tableaux.
  - Sparkline sur /home : cohérent (pas de changement nécessaire).
- **Tests requis** :
  - `test_format_usd_2_decimals_for_normal_values`
  - `test_format_usd_k_notation_for_large_values`
  - `test_home_card_total_usdc_matches_calculation`
  - `test_home_card_sum_equals_initial_plus_realized_plus_latent` (within 1 cent)
- **Sources** : Audit L-005 + user feedback 2026-04-24.
- **Charge item** : 0.25 jour

### MH.8 — Dashboard `/scoring` : stability metric + delta top-10 + backtest status

- **Location** : [src/polycopy/dashboard/queries.py](../../src/polycopy/dashboard/queries.py) + [src/polycopy/dashboard/templates/scoring.html](../../src/polycopy/dashboard/templates/)
- **Ce qu'il faut faire** :
  - Nouvelle colonne `stability` par wallet : `std(score over last N cycles)` pour chaque version active (v2.1, v2.1.1, v2.2 post-MF).
  - Badge stability : 🟢 stable (std < 0.03), 🟡 volatile (0.03-0.08), 🔴 unstable (>0.08). Permet user voir quels wallets ont un scoring fiable.
  - Panel "Top-10 side-by-side" : deux colonnes verticales top-10 v2.1.1 vs v2.2 (post-MF), avec tags `[newcomer]`, `[fell off]`, `[stable]` pour chaque changement.
  - Section "Shadow / Cutover status" visible : affiche `SCORING_VERSION`, `SCORING_V2_2_CUTOVER_READY`, jours shadow écoulés vs planifié, next milestone.
  - Si backtest report `docs/development/scoring_v2_2_backtest_report.md` existe, lien depuis /scoring.
  - **Décision D7** : calcul stability côté query (pas client JS), caché 5 min.
- **Tests requis** :
  - `test_scoring_dashboard_stability_column`
  - `test_scoring_top10_side_by_side_with_tags`
  - `test_scoring_cutover_status_panel`
- **Sources** : Session C (extension) + Claude §6 item B6 stability + MF shadow period display.
- **Charge item** : 0.5 jour

### MH.9 — Spearman rank display fix (audit I-008)

- **Location** : [src/polycopy/dashboard/queries.py](../../src/polycopy/dashboard/queries.py) + [src/polycopy/dashboard/templates/scoring.html](../../src/polycopy/dashboard/templates/)
- **Ce qu'il faut faire** :
  - Actuel : Spearman calculé sur intersection v1∩v2 mais **rank column affichage** montre `rank_v1` du pool entier (33, 45, 52...).
  - Fix : afficher **les rangs locaux** (sur l'intersection utilisée pour Spearman), pas rangs pool-wide.
  - Alternative : afficher **les deux** côte-à-côte : `Rank v1 (pool)` | `Rank v1 (intersection)` | `Rank v2`. Plus complet mais plus chargé.
  - **Décision D8** : rang local par défaut (aligné avec la métrique Spearman affichée), tooltip explicatif.
- **Tests requis** :
  - `test_spearman_rank_display_uses_local_intersection_ranks`
  - `test_spearman_tooltip_explains_intersection`
- **Sources** : Audit I-008.
- **Charge item** : 0.25 jour

### MH.10 — Dashboard `/performance` : colonne `fee_drag` + badge wash-risk

- **Location** : [src/polycopy/dashboard/queries.py list_trader_performance](../../src/polycopy/dashboard/queries.py) + [src/polycopy/dashboard/templates/performance.html](../../src/polycopy/dashboard/templates/)
- **Ce qu'il faut faire** :
  - Nouvelle colonne `Fee drag (24h)` : `sum(fee_rate × notional) for trades last 24h`. Calculable post-MC ship (via `FeeRateClient` data).
  - Nouvelle colonne `Wash risk` badge (post-MF ship) : 🟢 low (< 0.2), 🟡 medium (0.2-0.5), 🔴 high (> 0.5). Source : `wash_cluster_scores.wash_score`.
  - Si MC ou MF pas encore shippé → colonne absente (pas rendue). Feature flag dans le template.
  - **Décision D9** : features conditionnelles selon ship state. Si MC shipped, fee_drag column. Si MF shipped, wash_risk badge.
- **Tests requis** :
  - `test_performance_fee_drag_column_shown_when_mc_shipped`
  - `test_performance_wash_risk_badge_shown_when_mf_shipped`
- **Sources** : Extension Session C + MC + MF dependencies.
- **Charge item** : 0.25 jour

### MH.11 — (Bonus) Fix N+1 queries `get_home_alltime_stats` (audit M-008)

- **Location** : [src/polycopy/dashboard/queries.py:803-817](../../src/polycopy/dashboard/queries.py#L803)
- **Ce qu'il faut faire** :
  - Actuel : boucle `await session.execute(...)` par position fermée non-simulée → slow sur /home à partir de ~50 positions fermées.
  - Fix : single query avec aggregation :
    ```python
    stmt = select(
        func.sum(MyPosition.realized_pnl).label("total_pnl"),
        func.count(MyPosition.id).label("count_closed")
    ).where(
        MyPosition.closed_at.is_not(None),
        MyPosition.simulated == simulated_flag  # post-MD filter
    )
    ```
  - Élimine N+1, gain p50 probable ~500ms à ~50ms sur /home quand pool grows.
- **Tests requis** :
  - `test_home_alltime_stats_single_query_aggregation`
  - `test_home_alltime_stats_p50_under_100ms_on_500_positions` (benchmark)
- **Sources** : Audit M-008.
- **Charge item** : 0.25 jour

## 4. Architecture / décisions clefs

- **D1** : Copy button vanilla JS, pas de clipboard.js external. Justification : cohérence M6 zéro build step, 15 lignes suffisent.
- **D2** : 4 tiers format_size_precise (int, 3 déc, 4 déc, scientific). Justification : balance lisibilité/précision sur toutes les échelles observables.
- **D3** : Tooltip HTML natif `<span title>`. Justification : pas de dépendance JS, fonctionne sans JavaScript activé, accessibility OK.
- **D4** : `MyPosition.outcome_side` column ajoutée (migration 0011). Justification : évite N+1 Gamma lookup pour formule gain_max side-aware. Query cost minimal.
- **D5** : Break-even exclu du winner/losser count, affiché séparément. Justification : honnête statistiquement, pas de biais 100% WR.
- **D6** : Unifié 2 décimales partout + k-notation ≥ $10k. Justification : cohérence + lisibilité à grande valeur.
- **D7** : Stability metric compute côté query cache 5min. Justification : évite recompute N cycles par view.
- **D8** : Spearman rank display = rangs locaux par défaut, tooltip explicatif. Justification : cohérent avec métrique affichée.
- **D9** : MH.10 features conditionnelles selon ship state (feature flag template). Justification : ne bloque pas MH ship tant que MC/MF pas ready.

## 5. Invariants sécurité

- **Dashboard read-only strict** (M4.5 invariant) : intact. MH ajoute 0 POST/PUT/DELETE. Seulement macros + filters + display.
- **Zéro secret** : le copy button copie uniquement l'adresse wallet (public on-chain). Aucun secret exposé.
- **Grep automatisé anti-leak préservé** : `test_dashboard_security.py` continue à vérifier absence `POLYMARKET_PRIVATE_KEY`, `TELEGRAM_BOT_TOKEN`, etc. dans tous les templates.
- **CDN pinned** : aucune nouvelle dépendance CDN (Tailwind, HTMX, Chart.js, Lucide versions inchangées).
- **localStorage** : si user preferences stored (ex: filter preset /logs cohérent M9), pas de secret stocké.

## 6. Hypothèses empiriques à valider AVANT ship

Aucune hypothèse critique — MH est UX polish déterministe. Validation post-ship :
- **UX test** : user copie une adresse depuis chaque page → workflow fluide (<2 clicks).
- **Perf test** : /home p50 sous 200ms sur 500 positions fermées (post-MH.11).

## 7. Out of scope

- **Endpoint POST action (pin/unpin wallet via dashboard)** : hors scope MH, décision M4.5 "read-only strict" préservée. Future spec si besoin.
- **Dashboard `/scoring` panel drill-down breakdown v2** : drill-down sur facteurs individuels. Hors scope MH, future spec si besoin.
- **Export CSV `/traders/scoring?format=csv`** : bonus utile mais hors scope MH, future spec.
- **Histogramme distribution sous-scores pool** : visualization sophistiquée, hors scope MH, future spec.
- **Dashboard `/logs` improvements** : workflow logs déjà OK post-M9, pas de friction signalée.
- **Dashboard dark-mode toggle audit** : M6 dark-first mais toggle localStorage, fonctionne. Pas de fix needed.
- **Responsive mobile audit** : M6 responsive via `<details>` sidebar, pas de régression signalée.
- **Refactor complet UI** : M6 UX récente, pas de refonte nécessaire.

## 8. Success criteria

1. **Tests ciblés verts** : ~12 nouveaux tests unit + 2 integration.
2. **Copy button fonctionnel** sur les 11 vues qui listent des wallets, testé sur Chrome/Firefox/Safari.
3. **Win rate cohérent /home ↔ /performance** (régression C-005).
4. **Gain max latent correct sur BUY NO** (audit M-011 fix validé).
5. **TOTAL USDC = initial + realized + latent** à 1 cent près, pas de divergence d'arrondi.
6. **/home p50 < 200ms sur 500 positions fermées** (MH.11 optimisation).
7. **Spearman rank display cohérent** avec métrique affichée (post-MH.9).
8. **User self-test** : user peut blacklister un wallet en <30s depuis /performance (copy address → modif .env → restart) sans SSH.

## 9. Mapping origines (traçabilité)

| Item | Audit | Session | Deep-search | Synthèse roadmap |
|---|---|---|---|---|
| MH.1 | — | C (C1 core) | User feedback + synthèse §6.3 | #32 |
| MH.2 | [L-027] | C (C2) | — | UX fix |
| MH.3 | — | C (C3) | — | #33 |
| MH.4 | — | C (C5) | User feedback | #34 |
| MH.5 | [M-011] | C (C5 implicite) | — | audit |
| MH.6 | [M-010] | C (new) | — | audit |
| MH.7 | [L-005] | C (C4) | — | audit |
| MH.8 | — | C (extend) | Claude §6 B6 + MF shadow display | #29 |
| MH.9 | [I-008] | B (B5 partial) | — | Session B partial |
| MH.10 | — | C (extend) | MC + MF dependencies | — |
| MH.11 | [M-008] | C (new bonus) | — | audit |

## 10. Prompt de génération de spec

````markdown
# Contexte

Lis `docs/next/MH.md` en entier. C'est le brief actionnable du module MH
(Dashboard UX polish + consistency). 11 items de friction UX + cohérence data
identifiés dans l'audit + session C + feedback utilisateur direct. Inclut
extensions post-MC/MF (fee_drag, wash risk badge) conditionnelles au ship
state.

# Tâche

Produire `docs/specs/M21-dashboard-ux-polish.md` suivant strictement le format
des specs M1..M20 existantes.

Numéro : M21 (après MA=M14, MB=M15, MC=M16, MD=M17, ME=M18, MG=M19, MF=M20).

# Prérequis (à lire avant de rédiger)

- `CLAUDE.md` §Dashboard + §Sécurité (read-only strict, grep anti-leak)
- `docs/specs/M4.5-dashboard.md` + `docs/specs/M6-dashboard-2026.md` (dashboard
  architecture actuelle)
- `docs/specs/M9-silent-cli-and-readme.md` (logs viewer pattern)
- `docs/specs/M13_dry_run_observability_spec.md` comme template de forme
- Audit L-004, L-005, L-027, M-008, M-010, M-011, I-008
- Synthèse §6.3 Session C extensions + §2.5 scoring display patterns

# Contraintes

- Lecture seule src/ + docs
- Écriture uniquement `docs/specs/M21-dashboard-ux-polish.md`
- Longueur cible : 900-1200 lignes
- Migration Alembic **optionnelle** (0011 si `outcome_side` column requis MH.5,
  sinon compute at runtime via Gamma)
- Grep security test `test_dashboard_security.py` doit continuer à passer
- CDN versions préservées M6

# Livrable

- Le fichier `docs/specs/M21-dashboard-ux-polish.md` complet
- Un ping final ≤ 10 lignes : tests estimés, charge cumulée, ordre commits
  (recommandé : MH.1 macro → MH.7 format_usd → MH.2 format_size → MH.3 24h →
  MH.4 tooltips → MH.6 win rate → MH.9 Spearman → MH.5 gain max → MH.8 stability
  → MH.11 N+1 opt → MH.10 post-MC/MF features)
````

## 11. Notes d'implémentation

### Piège : CDN version pinning

Les versions Tailwind 3.4.16, HTMX 2.0.4, Chart.js 4.4.7, Lucide 0.469.0 sont pinned (CLAUDE.md §M6 Front-end dashboard). MH ne doit **pas** modifier ces versions. Si besoin de feature manquante, utiliser vanilla ou ajouter ≤1 dépendance CDN pinned documentée.

### Piège : grep security `test_dashboard_security.py`

Test existant grep `POLYMARKET_PRIVATE_KEY`, `TELEGRAM_BOT_TOKEN`, `CLOB_API_SECRET`, `REMOTE_CONTROL_TOTP_SECRET` dans les templates rendered. MH ajoute macros + JS vanilla — **vérifier** que rien ne leak accidentellement (unlikely mais safety check).

### Piège : format_size_precise edge case size=0

`format_size_precise(0)` doit retourner `"0"` pas `"0.00e+00"` (notation scientifique brouille l'œil). Cas edge dans le code.

### Piège : tooltip HTML natif mobile

Sur mobile tactile, `<span title>` ne s'affiche pas au hover. **Acceptable** pour v1 UX polish : la plupart des utilisateurs audits sont sur desktop. Future amélioration mobile-friendly via `details/summary` HTML ou bibliothèque tooltip (hors scope MH).

### Piège : migration 0011 `outcome_side` column

Si ajoutée, backfill nécessaire pour positions existantes :
```sql
-- For BUY YES : outcome_side = 'YES'
-- For BUY NO : outcome_side = 'NO'
-- Derive from condition_id + asset_id matching Gamma clobTokenIds[0] vs [1]
UPDATE my_positions SET outcome_side = CASE
    WHEN asset_id = (SELECT clob_token_ids[0] FROM gamma_markets WHERE condition_id = my_positions.condition_id) THEN 'YES'
    ELSE 'NO'
END;
```

Backfill complexe si Gamma metadata pas en cache local. **Alternative** : compute à la volée via Gamma lookup cached (slower mais pas de migration). Trade-off à trancher en spec M21.

### Piège : spearman rank display — rangs locaux vs globaux

Confusion possible user : "pourquoi le rang sur /scoring est différent de celui sur /traders ?". Tooltip **doit** expliquer clairement : "Rang sur l'intersection v1∩v2 (N=13), pas sur le pool entier (N=50)".

### Références audit

- **M-008** [audit §3 MEDIUM](../../docs/audit/2026-04-24-polycopy-code-audit.md#L255) — N+1 /home.
- **M-010** [audit §3 MEDIUM](../../docs/audit/2026-04-24-polycopy-code-audit.md#L265) — Win rate break-even.
- **M-011** [audit §3 MEDIUM](../../docs/audit/2026-04-24-polycopy-code-audit.md#L270) — Gain max YES-only.
- **L-004, L-005** [audit §3 LOW](../../docs/audit/2026-04-24-polycopy-code-audit.md#L370) — sparkline, format_usd.
- **L-027** [audit §3 LOW](../../docs/audit/2026-04-24-polycopy-code-audit.md#L395) — Size 0.00.
- **I-008** [audit §3 INFO](../../docs/audit/2026-04-24-polycopy-code-audit.md#L405) — Spearman rank display.

### Questions ouvertes pertinentes à MH

Aucune question directe. MH est déterministe.
