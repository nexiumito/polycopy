# M19 — Dashboard UX polish + consistency

**Status** : Draft — 2026-04-27 soir
**Depends on** : M4.5 (dashboard FastAPI + HTMX read-only), M6 (Tailwind CDN
+ Lucide + zéro build step + CDN versions pinned), M9 (logs viewer pattern +
`localStorage` UI prefs), M17 (cross-layer integrity — invariants
`MyPosition.simulated` filter MD.1, `realized_pnl`/`unrealized_pnl` peuplés
MD.6), M18 (V2 migration — `FeeQuote(rate, exponent)` ME.3, alias deprecated
`get_fee_rate` warning structlog)
**Bloque** : — (P3 slack time, parallélisable à toute autre session)
**Workflow git** : commits directement sur `main` (pas de branche, pas de
PR — règle projet)
**Charge estimée** : M (2-3 jours dev, peut être éclaté en 11 commits indépendants)
**Numéro** : M19 (post-M18 shipped 2026-04-27 ; le brief MK
[`docs/next/MK.md`](../next/MK.md) latency phase 1b prendra M20 en suivant)

---

## 0. TL;DR

M19 livre un **bundle UX polish 11 items (MH.1 → MH.11)** mappés depuis le
brief [`docs/next/MH.md`](../next/MH.md) qui ferme les 6 frictions
quotidiennes signalées par l'utilisateur pendant la session d'audit
2026-04-24 + les 7 findings audit (L-004, L-005, L-027, M-008, M-010, M-011,
I-008) + 4 extensions deep-search (`/scoring` stability, side-by-side
top-10, fee_drag column post-M16 shipped, wash-risk badge feature-flag
post-MF).

**11 items couplés** :

- **MH.1** — Bouton copier adresse + tooltip fullhash sur **toutes** les vues
  qui listent un wallet ou un `condition_id`. Macro Jinja
  `render_address(value, *, kind)` + JS vanilla `copyToClipboard()` (~15
  lignes, zéro dep externe — cf. décision **D1** §4.1). Résout la friction
  user "Je n'arrive pas à blacklist car je n'ai pas l'adresse complète".
- **MH.2** — Fix `Size 0.00` sur `/activité`, `/exécution`, `/positions` via
  nouveau filter Jinja `format_size_precise(size)` 4-tier (entier ≥ 1, 3
  décimales ≥ 0.01, 4 décimales ≥ 0.0001, scientifique sous le seuil — cf.
  **D2** §4.2). Tooltip avec valeur exacte full precision.
- **MH.3** — Métrique `APPROVE STRATÉGIE` côté `/home` re-baseée sur la
  fenêtre glissante **24h** (cohérent avec les autres stats `trades_detected_24h`,
  `volume_24h` etc.) au lieu de tout-temps. Label UI explicite "APPROVE
  STRATÉGIE (24h)". Évite le biais persistant après reset positions
  (`detected_trades` continue à grossir mais le ratio reflète le comportement
  courant). Cf. **D3.5** §4.3.
- **MH.4** — Tooltips explicatifs `<span title="...">` sur les **6 cartes
  KPI principales** /home : PnL réalisé, PnL latent, Gain max latent,
  Exposition, Drawdown, Win rate. Décomposition mathématique exposée
  (`total_usdc = initial_capital + realized_pnl + latent_pnl`). Cf. **D3**
  §4.4 — tooltip natif HTML, pas de JS / modal.
- **MH.5** — Fix `Gain max latent` formule **side-aware** (audit M-011 :
  `(1 − avg_price) × size` invalide pour BUY NO). Calcul correct :
  ```
  max_profit = (1 − avg_price) × size  pour YES
             =  avg_price       × size  pour NO
  ```
  Décision **D4** §4.5 : compute outcome side à la volée via JOIN cached
  sur `DetectedTrade.outcome` (déjà peuplé par M1 watcher) — **pas** de
  migration Alembic 0011 (préserve invariant "no migration tant que pas
  indispensable").
- **MH.6** — Fix `Win rate` break-even handling (audit M-010 : `5
  break-even + 1 win = 100% WR` faux signal). Convention **D5** §4.6 :
  break-even (`realized_pnl == 0`) exclu du dénominateur (status quo) MAIS
  affiché séparément dans le label : "Win rate 100% (1W / 0L / 5 break-even)".
  Cohérence /home ↔ /performance (régression test C-005 préservé).
- **MH.7** — Fix arrondi `TOTAL USDC` cohérent **2 décimales** (audit L-005 :
  `_format_card_usd` queries.py:632 produit entiers `$1006`, `format_usd`
  jinja_filters.py produit `$1.0k`/`$0.45` selon l'échelle — rupture
  visuelle). Décision **D6** §4.7 : unifier sur `format_usd` côté filter
  (déjà 2 décimales sous $1k, k-notation ≥ $1k, M-notation ≥ $1M).
  `_format_card_usd` retiré ; toutes les cartes consument `format_usd`.
  Garantit `total = initial + realized + latent` à < 1 cent.
- **MH.8** — Dashboard `/scoring` enrichi 3 panels : **stability metric**
  (`std(score over last N=10 cycles)` par wallet, badge 🟢/🟡/🔴 selon
  std-thresholds 0.03/0.08), **top-10 side-by-side** (v2.1.1 vs v2.2 si
  shippé, tags `[newcomer] [fell off] [stable]`), **cutover status panel**
  (lit `SCORING_VERSION`, `SCORING_V2_SHADOW_DAYS`, jours écoulés,
  next-milestone). Cf. **D7** §4.8.
- **MH.9** — Spearman rank display cohérent (audit I-008 : ranks pool-entier
  affichés alors que la métrique Spearman est calculée sur l'intersection
  v1∩v2). Fix : afficher les rangs **locaux** (sur l'intersection) par
  défaut, tooltip explicatif "Rang sur intersection v1∩v2 (N=13)". Cf.
  **D8** §4.9.
- **MH.10** — Dashboard `/performance` : nouvelle colonne **`Fee drag (24h)`**
  (`Σ fee_quote.rate × notional` sur trades dernières 24h, post-M18
  ME.3 — consume `FeeQuote` pas `get_fee_rate` deprecated). Nouvelle
  colonne **`Wash risk`** badge 🟢/🟡/🔴 conditionnelle au ship MF
  (feature-flag template, absente sinon). Cf. **D9** §4.10.
- **MH.11** — Bonus N+1 fix `get_home_alltime_stats` (audit M-008 : boucle
  `await session.execute(...)` par position fermée — slow sur /home dès
  ~50 positions). Single-query aggregation `func.sum(realized_pnl) +
  func.count(id)`. Gain p50 visé : ~500ms → ~50ms sur 500 positions.
  Préserve filtre `simulated == (execution_mode != "live")` M17 MD.1.

Diff strictement additif sur les invariants critiques :

- **Dashboard read-only strict** (M4.5 invariant) : intact. M19 ajoute 0
  POST/PUT/DELETE — toutes les routes ajoutées (le cas échéant pour MH.8
  cutover panel) en `@router.get(...)`.
- **CDN versions pinned** (M6) : Tailwind 3.4.16, HTMX 2.0.4, Chart.js
  4.4.7, Lucide 0.469.0 — **inchangées**. MH.1 utilise vanilla JS (~15
  lignes), aucune nouvelle dep CDN.
- **Grep security anti-leak** (M4.5/M6) : `test_dashboard_security.py` +
  `test_dashboard_security_m6.py` continuent à passer (aucune fuite
  `POLYMARKET_PRIVATE_KEY`, `TELEGRAM_BOT_TOKEN`, `CLOB_API_SECRET`,
  `REMOTE_CONTROL_TOTP_SECRET`). Wallet addresses + condition_ids sont
  publics on-chain — copy button OK.
- **Filtre `simulated` strict M17 MD.1** : MH.5 (gain max latent) + MH.11
  (alltime stats) préservent `MyPosition.simulated == (execution_mode != "live")`.
- **Convergence /home ↔ /performance M17 MD.6** : MH.6 + MH.7 garantissent
  `total = initial + realized + latent` à < 1 cent (régression test C-005
  préservé).
- **`FeeQuote` M18 ME.3** : MH.10 fee_drag column consume `get_fee_quote()`
  pas `get_fee_rate()` (alias deprecated qui émettrait un warning structlog
  1× par token sinon).
- **Versioning sacré** : MH ne touche aucun scoring (v1/v2.1/v2.1.1). MH.8
  ajoute juste un panel display de la stability — read-only sur
  `trader_scores` et `target_traders`.
- **`localStorage` discipline M9/M10** : aucune nouvelle clé ajoutée. Si
  MH.4 introduit un dismiss tooltip (hors scope v1), respect du pattern
  `polycopy.dashboard.<feature>` cohérent `polycopy.theme` / `polycopy.logs.preset`.
- **Append-only DB** : aucune migration Alembic, aucune row réécrite.
  `alembic upgrade head` retourne "no migrations to apply" post-M19. Le
  current head reste **0010** (M17 MD.3 — la dernière migration).

Tests cumulés estimés : **~12 tests unit nouveaux** (MH.1=3, MH.2=2,
MH.3=1, MH.4=1, MH.5=2, MH.6=2, MH.7=2, MH.8=2, MH.9=1, MH.10=1, MH.11=2)
+ **2 tests integration** (smoke /home + /performance + /scoring rendu
contre fixtures DB seedée) + **8 tests régression** existants préservés
(security_dashboard, format_usd, win_rate convergence C-005, sparkline
filtering MD.6).

Charge cumulée : **2-3 jours dev**, possible en 11 commits atomiques
parallélisables (chaque MH.x est indépendant des autres sauf MH.6 ↔ MH.7
convergence).

---

## 1. Motivation & use case concret

### 1.1 Frictions quotidiennes observées 2026-04-24

L'utilisateur a passé une session d'audit 2026-04-24 sur le dashboard
production et a signalé 6 frictions concrètes :

1. **"Je n'arrive pas à blacklist un wallet car je n'ai pas l'adresse
   complète"** : adresses tronquées partout (`0x21ff…0d71`) sans moyen de
   copier. Workaround actuel : ouvrir SQL CLI sur prod, query
   `SELECT wallet_address FROM target_traders WHERE wallet_address LIKE
   '0x21ff%'`. Trop de friction pour une action quotidienne.
2. **`Size 0.00` partout sur `/activité`** : toutes les lignes affichent
   `Size 0.00`. Les sizes réelles sont dans la plage `[0.001, 0.05]` shares
   (copy_ratio 0.01 × source size). Le filter `format_size` actuel
   ([jinja_filters.py:45-49](../../src/polycopy/dashboard/jinja_filters.py#L45))
   utilise `f"{value:.2f}"` qui produit `"0.00"` pour tout < 0.005. Colonne
   inutile visuellement.
3. **`APPROVE STRATÉGIE: 5.0%`** sur `/home` : 95% des trades détectés
   rejetés. Mais biaisé : `count(detected_trades)` continue à grossir
   après chaque reset positions/capital, alors que les decisions APPROVED
   du moment sont noyées dans le ratio all-time. Source de confusion (user
   feedback : "5% c'est nul mais je sais pas si c'est juste").
4. **Confusion PnL** : user pose la question "1,006 USDC, PnL réalisé
   −0.54, PnL latent +7.04 — je ne comprend pas trop comment ça s'agence".
   Décomposition `total = initial_capital + realized_pnl + latent_pnl`
   (M17 MD.6) correcte mais pas évidente sans tooltip explicatif sur les
   cartes KPI.
5. **Arrondi `TOTAL USDC`** : la card affiche `$1006`, le calcul exact
   = `$1006.50`. `_format_card_usd` ([queries.py:632-637](../../src/polycopy/dashboard/queries.py#L632))
   utilise `round(value)` puis `f"{...:,}"` sans décimales, alors que
   `format_usd` ([jinja_filters.py:22-42](../../src/polycopy/dashboard/jinja_filters.py#L22))
   produit `$1.0k` ou `$0.45` selon l'échelle. Incohérence inter-views.
6. **Spearman rank `/scoring`** : la métrique Spearman est calculée sur
   l'intersection v1∩v2 (N=13 wallets) mais les ranks affichés à côté
   sont les ranks **du pool entier** (N=50). Confusion : user voit `Rank
   v1: 33` et `Rank v2: 7` puis lit `Spearman = 0.92` et ne comprend pas
   le mismatch.

### 1.2 Findings audit 2026-04-24 référencés

L'audit code 2026-04-24 ([docs/audit/2026-04-24-polycopy-code-audit.md](../audit/2026-04-24-polycopy-code-audit.md))
a exposé **7 findings** dashboard pertinents :

| ID | Niveau | Sujet | Fix MH |
|---|---|---|---|
| L-004 | LOW | Sparkline filtre `is_dry_run=False` mais `latest_snapshot` pas filtré → incohérence 24h vs dernier point | MH.7 (cohérence rendu) |
| L-005 | LOW | `_format_card_usd` entiers vs `format_usd` 2 décimales — rupture visuelle | MH.7 |
| L-027 | LOW | Size 0.00 sur `/activité` (filter `format_size` 2 décimales) | MH.2 |
| M-008 | MEDIUM | N+1 queries dans `get_home_alltime_stats` — slow sur /home dès ~50 positions | MH.11 |
| M-010 | MEDIUM | Win rate break-even exclusion silencieuse — `5 break-even + 1 win = 100% WR` | MH.6 |
| M-011 | MEDIUM | Gain max latent assume YES pour toutes positions (`(1 − avg_price) × size`) — invalide pour BUY NO | MH.5 |
| I-008 | INFO | Spearman rank display utilise ranks pool-wide vs intersection | MH.9 |

### 1.3 Extensions deep-search post-audit

Par ailleurs, la session deep-search §6.3 ("Session C extensions") a
identifié 4 améliorations dashboard hautement valuable post-cutover M14/
M15/M16/M17/M18 :

- **Stability metric `/scoring`** : `std(score over last N cycles)` par
  wallet — permet à l'user d'identifier les wallets dont le scoring est
  fiable (stable) vs volatile (signal bruité). Critique post-M14 où la
  formule v2.1-ROBUST utilise rank_normalize ; un wallet stable a un
  rang qui bouge peu cycle après cycle.
- **Top-10 side-by-side v2.1.1 vs v2.2 (post-MF)** : permet à l'user de
  voir les changements lors du cutover scoring. Tags `[newcomer] [fell
  off] [stable]` pour chaque wallet du top-10. Conditionnel au ship MF
  capstone (numéro spec à attribuer post-M19).
- **Fee_drag column `/performance`** : `Σ fee_quote.rate × notional` sur
  trades dernières 24h. Post-M18 ME.3, on consume le nouveau path V2
  (`get_fee_quote(token_id, condition_id=cid)`). Permet à l'user de
  voir l'impact fee par marché — utile pour identifier les marchés
  fee-enabled vs fee-free et arbitrer le sizing.
- **Wash risk badge `/performance`** : `wash_score` calculé par MF
  (Sirolly cluster detection) → badge 🟢 low (< 0.2) / 🟡 medium /
  🔴 high (> 0.5). Conditionnel au ship MF (feature flag).

### 1.4 Vue de haut des changements

| Module | Diff | Référence MH |
|---|---|---|
| [src/polycopy/dashboard/jinja_filters.py](../../src/polycopy/dashboard/jinja_filters.py) | +`format_size_precise` filter (4-tier) ; alias deprecated `format_size` redirige avec warning | MH.2 |
| [src/polycopy/dashboard/jinja_filters.py](../../src/polycopy/dashboard/jinja_filters.py) | retire référence à `_format_card_usd` ; le filter `format_usd` reste l'unique source | MH.7 |
| [src/polycopy/dashboard/queries.py:632-637](../../src/polycopy/dashboard/queries.py#L632-L637) | retire `_format_card_usd` ; cards consume `format_usd` filter directement (rendu Jinja-side) | MH.7 |
| [src/polycopy/dashboard/queries.py:875-879](../../src/polycopy/dashboard/queries.py#L875-L879) | `strategy_approve_rate_pct` filtré sur `decided_at >= now() - 24h` (avec le `decision_rows` SQL) | MH.3 |
| [src/polycopy/dashboard/queries.py:914-929](../../src/polycopy/dashboard/queries.py#L914-L929) | `open_max_profit_usd` query side-aware via JOIN sur `DetectedTrade.outcome` (cf. **D4**) | MH.5 |
| [src/polycopy/dashboard/queries.py:949-952](../../src/polycopy/dashboard/queries.py#L949-L952) | `win_rate_pct` enrichi avec `breakeven_count` exposé séparément ; HomeAllTimeStats étendu | MH.6 |
| [src/polycopy/dashboard/queries.py:803-817](../../src/polycopy/dashboard/queries.py#L803-L817) | `get_home_alltime_stats` retire la boucle N+1 ; `func.sum(realized_pnl)` aggregé en 1 query | MH.11 |
| [src/polycopy/dashboard/queries.py](../../src/polycopy/dashboard/queries.py) | nouveau helper `compute_scoring_stability(wallet, window=10)` lit `trader_scores` (cache 5 min) | MH.8 |
| [src/polycopy/dashboard/queries.py](../../src/polycopy/dashboard/queries.py) | nouveau helper `compute_fee_drag_24h()` consume `MyOrder.fee_rate` (post-M16 stocké) ou via `FeeRateClient.get_fee_quote` (path nominal) | MH.10 |
| [src/polycopy/dashboard/templates/macros.html](../../src/polycopy/dashboard/templates/macros.html) | nouvelle macro `render_address(value, *, kind="wallet"\|"condition")` produisant `<span>` + bouton copy | MH.1 |
| [src/polycopy/dashboard/templates/base.html](../../src/polycopy/dashboard/templates/base.html) | inline JS `copyToClipboard(text, btn)` (~15 lignes vanilla, default Clipboard API + fallback) | MH.1 |
| [src/polycopy/dashboard/templates/home.html](../../src/polycopy/dashboard/templates/home.html) | use `render_address` sur top_trader + recent trades ; tooltips KPI cards (`<span title="...">`) | MH.1 + MH.4 |
| [src/polycopy/dashboard/templates/activity.html](../../src/polycopy/dashboard/templates/activity.html) | `format_size` → `format_size_precise` ; tooltip valeur exacte ; `render_address` sur wallet + condition_id | MH.1 + MH.2 |
| [src/polycopy/dashboard/templates/strategy.html](../../src/polycopy/dashboard/templates/strategy.html) | `render_address` sur wallet + condition_id ; `format_size_precise` | MH.1 + MH.2 |
| [src/polycopy/dashboard/templates/orders.html](../../src/polycopy/dashboard/templates/orders.html) | idem | MH.1 + MH.2 |
| [src/polycopy/dashboard/templates/positions.html](../../src/polycopy/dashboard/templates/positions.html) | idem ; tooltip `outcome_side` (computed) | MH.1 + MH.5 |
| [src/polycopy/dashboard/templates/pnl.html](../../src/polycopy/dashboard/templates/pnl.html) | `render_address` sur milestones | MH.1 |
| [src/polycopy/dashboard/templates/traders.html](../../src/polycopy/dashboard/templates/traders.html) | `render_address` sur wallet | MH.1 |
| [src/polycopy/dashboard/templates/traders_scoring.html](../../src/polycopy/dashboard/templates/traders_scoring.html) | `render_address` sur wallet ; ranks locaux (intersection) ; `Spearman tooltip` | MH.1 + MH.9 |
| [src/polycopy/dashboard/templates/performance.html](../../src/polycopy/dashboard/templates/performance.html) | `render_address` sur wallet ; `fee_drag_24h_usd` column ; `wash_risk_badge` feature-flag | MH.1 + MH.10 |
| [src/polycopy/dashboard/templates/macros.html](../../src/polycopy/dashboard/templates/macros.html) | `kpi_card` macro étendu : accept `tooltip` kwarg pour rendu `<span title>` | MH.4 |
| [src/polycopy/dashboard/dtos.py](../../src/polycopy/dashboard/dtos.py) | `HomeAllTimeStats` étendu : +`breakeven_count: int` (MH.6), +`approve_rate_window_hours: int = 24` (MH.3) | MH.3 + MH.6 |
| [src/polycopy/dashboard/dtos.py](../../src/polycopy/dashboard/dtos.py) | `TraderRow` (scoring) étendu : +`stability_std: float \| None`, `stability_label: Literal["stable","volatile","unstable"]` | MH.8 |
| [src/polycopy/dashboard/dtos.py](../../src/polycopy/dashboard/dtos.py) | `PerformanceRow` étendu : +`fee_drag_24h_usd: float \| None`, +`wash_score: float \| None` (feature flag) | MH.10 |
| [tests/unit/test_dashboard_*.py](../../tests/unit/) | ~12 tests nouveaux + 8 régressions préservés (sécurité, convergence) | tous MH |

---

## 2. Scope / non-goals

### 2.1 Dans le scope (MH.1 → MH.11)

#### MH.1 — Bouton copier adresse + tooltip fullhash

- Nouvelle macro Jinja
  [src/polycopy/dashboard/templates/macros.html](../../src/polycopy/dashboard/templates/macros.html) :
  ```jinja
  {% macro render_address(value, kind="wallet") -%}
  {% if not value %}
    <span class="text-xs" style="color: var(--color-muted);">—</span>
  {% else %}
  <span class="addr-cell {{ 'condition-id' if kind == 'condition' else 'wallet-addr' }}"
        title="{{ value }}" data-addr="{{ value }}">
    {{ value | short_hash(width=4) }}
    <button type="button" class="copy-btn" aria-label="Copier {{ value }}"
            onclick="copyToClipboard('{{ value }}', this)">
      <i data-lucide="copy" class="w-3 h-3"></i>
    </button>
  </span>
  {% endif %}
  {%- endmacro %}
  ```
- Inline JS dans
  [src/polycopy/dashboard/templates/base.html](../../src/polycopy/dashboard/templates/base.html) :
  ```javascript
  async function copyToClipboard(text, btn) {
    try {
      await navigator.clipboard.writeText(text);
      btn.classList.add('copied');
      setTimeout(() => btn.classList.remove('copied'), 1500);
    } catch (err) {
      // Fallback : selectable textbox + execCommand pour browsers anciens
      const ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); } finally { document.body.removeChild(ta); }
      btn.classList.add('copied');
      setTimeout(() => btn.classList.remove('copied'), 1500);
    }
  }
  ```
- Style Tailwind dans `base.html` :
  ```css
  .copy-btn { opacity: 0.4; transition: opacity 0.15s; cursor: pointer; }
  .copy-btn:hover { opacity: 1; }
  .copy-btn.copied { opacity: 1; }
  .copy-btn.copied::after { content: " ✓"; color: var(--color-profit); }
  .addr-cell { display: inline-flex; align-items: center; gap: 4px; }
  ```
- **Appliqué sur 11 vues** (cf. table §1.4) listant des wallets ou
  `condition_ids` : `/home` (top trader + recent trades), `/détection`,
  `/stratégie`, `/exécution` (source + cond_id), `/positions` (cond_id),
  `/pnl` (milestones), `/activité`, `/traders`, `/traders/scoring`,
  `/performance`. Logs viewer (`/logs`) pas concerné (texte brut, déjà
  copyable via select).
- **Décision D1** §4.1 : pas de dépendance clipboard.js externe, vanilla
  JS suffit (~15 lignes, ~200 bytes).
- **Sources** : Session C C1 + user feedback direct 2026-04-24.

#### MH.2 — Fix `Size 0.00` display

- Nouveau filter Jinja `format_size_precise(value: float | None) -> str`
  dans [jinja_filters.py](../../src/polycopy/dashboard/jinja_filters.py) :
  ```python
  def format_size_precise(value: float | None) -> str:
      """4-tier formatage size pour balance lisibilité × précision (cf. spec §5.2)."""
      if value is None:
          return _EMPTY
      if value == 0:
          return "0"
      abs_v = abs(value)
      sign = "-" if value < 0 else ""
      if abs_v >= 1:
          return f"{sign}{abs_v:.2f}"
      if abs_v >= 0.01:
          return f"{sign}{abs_v:.3f}"
      if abs_v >= 0.0001:
          return f"{sign}{abs_v:.4f}"
      return f"{sign}{abs_v:.2e}"
  ```
- Templates `/activité`, `/exécution`, `/positions` : remplacer
  `{{ size | format_size }}` par `{{ size | format_size_precise }}` +
  ajouter `<span title="{{ size }}">` pour valeur exacte au hover.
- L'ancien filter `format_size` reste exposé mais déprécié (warning
  structlog 1× par template au boot via `templates.env.tests`). Pas de
  fallback obligatoire — les vues qui utilisent encore `format_size`
  (legacy) fonctionnent identiquement (rétrocompat).
- **Décision D2** §4.2 : 4 tiers (entier, 3 dec, 4 dec, scientifique).
- **Sources** : Session C C2 + audit L-027.

#### MH.3 — `APPROVE STRATÉGIE` 24h fenêtre glissante

- [queries.py:864-878](../../src/polycopy/dashboard/queries.py#L864-L878) :
  filtrer la query `decision_rows` sur `decided_at >= now() - 24h`.
  ```python
  since_24h = datetime.now(tz=UTC) - timedelta(hours=24)
  decision_rows = list(
      (await session.execute(
          select(StrategyDecision.decision, func.count(StrategyDecision.id))
          .where(StrategyDecision.decided_at >= since_24h)
          .group_by(StrategyDecision.decision)
      )).all()
  )
  ```
- [home.html](../../src/polycopy/dashboard/templates/home.html) : label UI
  explicite "APPROVE STRATÉGIE (24h)" au lieu de "APPROVE STRATÉGIE" pour
  signaler la fenêtre.
- `HomeAllTimeStats.approve_rate_window_hours: int = 24` exposé pour la
  cohérence du label.
- **Décision** : cohérent avec le pattern existant `trades_detected_24h`,
  `volume_24h` côté `/home`. Pas de migration, juste un filtre SQL ajouté.
- **Sources** : Session C C3 + user feedback 2026-04-24.

#### MH.4 — Tooltips explicatifs KPI cards

- Étendre la macro `kpi_card(card)` dans
  [macros.html](../../src/polycopy/dashboard/templates/macros.html) pour
  accepter un kwarg `tooltip: str | None = None` qui rend
  `<span class="info-icon" title="{{ tooltip }}">…</span>` à côté du title.
- Wirer les 6 cartes /home avec leur tooltip respectif :
  - **PnL réalisé** : "Gains/pertes cristallisés sur positions fermées (SELL + résolutions marché). Définitif, ne bouge plus."
  - **PnL latent** : "Mark-to-market positions ouvertes : Σ (mid_price − avg_buy) × size. Change avec les prix marché. Formule : total_usdc = initial_capital + realized_pnl + latent_pnl."
  - **Gain max latent** : "Payoff théorique si toutes les positions YES gagnent ET toutes les NO perdent : Σ (1 − avg_price) × size sur YES + Σ avg_price × size sur NO (post-MH.5)."
  - **Exposition** : "Capital engagé dans les positions ouvertes : Σ avg_price × size. Ce qu'on perdrait si tout tombe à 0."
  - **Drawdown** : "Chute depuis le plus haut historique du total_usdc. Fermeture quand ≥ KILL_SWITCH_DRAWDOWN_PCT."
  - **Win rate** : "Positions fermées avec realized_pnl > 0 / (wins + losses). Break-even (= 0) exclus mais comptés séparément (post-MH.6)."
- Icône Lucide `info` à côté du title (`<i data-lucide="info" class="w-3 h-3 opacity-40">`) — discret, pas de modal.
- **Décision D3** §4.4 : tooltip natif HTML `<span title>` — fonctionne
  sans JS, accessibility OK. Mobile : ne s'affiche pas au hover (acceptable
  v1, hors scope mobile-friendly).
- **Sources** : Session C C5 + user feedback 2026-04-24 "je ne comprend pas trop".

#### MH.5 — Fix `Gain max latent` formule side-aware

- **Bug actuel** ([queries.py:921-924](../../src/polycopy/dashboard/queries.py#L921-L924)) :
  ```python
  func.coalesce(func.sum(MyPosition.size * (1.0 - MyPosition.avg_price)), 0.0)
      .label("max_profit"),
  ```
  Cette formule traite **toutes** les positions comme YES → invalide pour BUY NO.
- **Fix D4** §4.5 : compute `outcome_side` via JOIN à `DetectedTrade` qui
  porte déjà la colonne `outcome` peuplée par
  [wallet_poller.py:177](../../src/polycopy/watcher/wallet_poller.py#L177)
  ([dtos.py:38](../../src/polycopy/watcher/dtos.py#L38)) :
  ```python
  # SQL : JOIN sur (condition_id, asset_id) — match sur la dernière trade
  # détectée pour cette position (proxy fiable pour le side YES/NO).
  open_stats_q = (
      select(
          func.coalesce(func.sum(MyPosition.size * MyPosition.avg_price), 0.0)
              .label("exposition"),
          func.coalesce(
              func.sum(
                  case(
                      (DetectedTrade.outcome == "Yes",
                       MyPosition.size * (1.0 - MyPosition.avg_price)),
                      else_=MyPosition.size * MyPosition.avg_price,
                  )
              ), 0.0,
          ).label("max_profit"),
      )
      .select_from(MyPosition)
      .outerjoin(
          DetectedTrade,
          and_(
              DetectedTrade.condition_id == MyPosition.condition_id,
              DetectedTrade.asset_id == MyPosition.asset_id,
          ),
      )
      .where(*open_filter)
      .group_by(MyPosition.id)  # ou subquery
  )
  ```
  Note : si plusieurs `DetectedTrade` matchent (BUY puis SELL puis BUY
  d'un même asset), prendre la **plus récente** (`func.max(DetectedTrade.timestamp)`).
  Si aucune `DetectedTrade` matche (cas dégénéré M3 → live mode où la
  position vient d'un fill non-detected), fallback YES (cohérent
  comportement legacy, conservateur).
- Préserve le filtre `simulated == (execution_mode != "live")` M17 MD.1.
- **Décision D4** §4.5 trade-off : compute via JOIN (option A : pas de
  migration, +1 query simple cached) vs migration `MyPosition.outcome_side`
  (option B : 0 query supplémentaire mais migration 0011 + backfill SQL).
  **Option A retenue** car aligne avec invariant "no migration tant que pas
  indispensable" + tests M17 préservés sans nouvelle DTOs. Le coût query
  (~2-5ms en SQLite local pour ~50 positions) est négligeable.
- **Sources** : Audit M-011 + Session C implicite C5.

#### MH.6 — Fix `Win rate` break-even handling

- **Bug actuel** ([queries.py:949-952](../../src/polycopy/dashboard/queries.py#L949-L952)) :
  ```python
  wins = sum(1 for p in closed_pnls if p is not None and float(p) > 0)
  losses = sum(1 for p in closed_pnls if p is not None and float(p) < 0)
  decided = wins + losses  # break-even (=0) exclus
  win_rate_pct = (wins / decided * 100.0) if decided > 0 else None
  ```
  → 5 break-even + 1 win = `100% win rate` affiché, faux signal.
- **Fix D5** §4.6 : convention "break-even = neutre, exclure du win/loss
  count mais **documenter** le count séparément" :
  ```python
  wins = sum(1 for p in closed_pnls if p is not None and float(p) > 0)
  losses = sum(1 for p in closed_pnls if p is not None and float(p) < 0)
  breakevens = sum(1 for p in closed_pnls if p is not None and float(p) == 0)
  decided = wins + losses
  win_rate_pct = (wins / decided * 100.0) if decided > 0 else None
  ```
  + `HomeAllTimeStats.breakeven_count: int = 0` ajouté.
- Template `/home` win rate KPI :
  - Value : `{{ alltime.win_rate_pct | format_pct }}` inchangé.
  - Subtext : `"{{ alltime.wins }}W / {{ alltime.losses }}L
    {% if alltime.breakeven_count %}({{ alltime.breakeven_count }} break-even){% endif %}"`.
- **Cohérence /home ↔ /performance** : appliquer la même formule dans
  `list_trader_performance` ([queries.py:1851](../../src/polycopy/dashboard/queries.py#L1851)) ;
  régression test C-005 préservé (le test vérifie déjà la convergence).
- **Sources** : Audit M-010 + convergence C-005.

#### MH.7 — Fix arrondi `TOTAL USDC` cohérent

- **Bug actuel** : `_format_card_usd` ([queries.py:632-637](../../src/polycopy/dashboard/queries.py#L632-L637))
  produit entiers `$1006` ; `format_usd` ([jinja_filters.py:22-42](../../src/polycopy/dashboard/jinja_filters.py#L22-L42))
  produit `$1.0k` (≥ 1k) ou `$0.45` (< 1) avec 2 décimales sous $1k.
  Rupture visuelle : `/home` card → entier, `/activité` cell → 2 décimales.
- **Fix D6** §4.7 : retirer `_format_card_usd` ; toutes les cartes consume
  `format_usd` filter directement côté template Jinja :
  ```jinja
  <p class="text-2xl font-semibold">{{ card.value_raw | format_usd }}</p>
  ```
  + `KpiCard.value_raw: float | None` exposé (le `value: str` actuel
  pré-formaté `_format_card_usd` est retiré côté `get_home_kpi_cards`).
- Le filter `format_usd` actuel est **déjà cohérent** : 2 décimales sous
  $1k, k-notation ≥ $1k, M-notation ≥ $1M. Pas de changement côté filter.
- Cohérence garantie /home ↔ /activité ↔ /performance (tous consument le
  même filter).
- **Note** : la sparkline reste pilotée par `total_points: list[(dt, float)]`
  inchangé (numeric). Pas de régression.
- **Sources** : Audit L-005 + user feedback 2026-04-24.

#### MH.8 — Dashboard `/scoring` enrichi

- Nouveau helper
  [src/polycopy/dashboard/queries.py](../../src/polycopy/dashboard/queries.py)
  `compute_scoring_stability_for_pool(*, window: int = 10) -> dict[str, float | None]` :
  ```python
  async def compute_scoring_stability_for_pool(
      session_factory: async_sessionmaker[AsyncSession],
      *,
      window: int = 10,
      version: Literal["v1", "v2.1", "v2.1.1"] | None = None,
  ) -> dict[str, float | None]:
      """Calcule std(score) sur les `window` derniers cycles par wallet.

      Retourne dict {wallet_address: std_decimal | None} (None si <window cycles).
      """
      async with session_factory() as session:
          # Single query : window function ROW_NUMBER + GROUP BY pour std.
          subq = (
              select(
                  TraderScore.wallet_address,
                  TraderScore.score,
                  func.row_number().over(
                      partition_by=TraderScore.wallet_address,
                      order_by=TraderScore.computed_at.desc(),
                  ).label("rn"),
              )
              .where(TraderScore.scoring_version == version_filter)
              .subquery()
          )
          stmt = (
              select(
                  subq.c.wallet_address,
                  func.coalesce(
                      _stddev_func(subq.c.score), None
                  ).label("std"),
                  func.count(subq.c.score).label("n"),
              )
              .where(subq.c.rn <= window)
              .group_by(subq.c.wallet_address)
          )
          rows = (await session.execute(stmt)).all()
      # SQLite n'a pas STDDEV natif → fallback Python via list aggregate.
      return {row.wallet_address: float(row.std) if row.n >= window else None for row in rows}
  ```
  Note : SQLite n'expose pas `STDDEV` nativement (pas de math extension par
  défaut) → fallback compute Python via 1 SELECT `score` + Python
  `statistics.stdev`. Cache 5 min côté query (lru_cache lifetime).
- Badge stability dans
  [traders_scoring.html](../../src/polycopy/dashboard/templates/traders_scoring.html) :
  - 🟢 stable (std < 0.03)
  - 🟡 volatile (0.03 ≤ std < 0.08)
  - 🔴 unstable (std ≥ 0.08)
  - ⏳ insufficient (n < window — wallet récent, on attend `window` cycles)
- **Top-10 side-by-side** : panel HTML simple comparant le top-10 v2.1.1
  vs v2.2 (post-MF capstone). Tags `[newcomer]` / `[fell off]` / `[stable]`
  selon présence dans les 2 listes. Conditionnel au flag
  `SCORING_VERSION` exposé (si `v2.2` pas registry-disponible, rendu absent).
- **Cutover status panel** : section visible avec `SCORING_VERSION`,
  `SCORING_V2_SHADOW_DAYS`, jours écoulés depuis première row v2.x dans
  `trader_scores`, next-milestone (ex: "Validation H-EMP-1 dans 7 jours" si
  flip v2.1 attendu).
  Si rapport `docs/development/scoring_v2_2_backtest_report.md` existe →
  lien depuis le panel ; sinon **TODO feature flag**.
- **Décision D7** §4.8 : compute stability côté query cache 5 min, pas
  client JS. Aligne avec le pattern Gamma cache (M2) + alert digest (M7).
- **Sources** : Session C extensions + Claude §6 item B6 + MF shadow display.

#### MH.9 — Spearman rank display

- **Bug actuel** ([traders_scoring.html](../../src/polycopy/dashboard/templates/traders_scoring.html)) :
  affiche `Rank v1: 33` (rank pool entier sur 50) à côté de `Spearman 0.92`
  (calculé sur intersection v1∩v2 N=13). Confusion rank.
- **Fix D8** §4.9 :
  - Ajouter au DTO `TraderScoringRow` les champs `rank_v1_local` et
    `rank_v2_local` (rangs sur l'intersection utilisée pour Spearman, pas
    pool-wide).
  - Template affiche les rangs locaux par défaut.
  - Tooltip explicatif sur le header de colonne :
    "Rang sur intersection v1∩v2 (N={{ intersection_size }}), pas le pool
    entier (N={{ pool_size }})."
  - Optionnel : badge ✦ à côté du rank si `rank_local != rank_global`
    (signale visuellement la divergence).
- **Sources** : Audit I-008.

#### MH.10 — `/performance` enrichi

- Nouveau helper
  [src/polycopy/dashboard/queries.py](../../src/polycopy/dashboard/queries.py)
  `compute_fee_drag_24h(*, simulated: bool) -> dict[str, float]` :
  ```python
  async def compute_fee_drag_24h(
      session_factory: async_sessionmaker[AsyncSession],
      *,
      simulated: bool,
  ) -> dict[str, float]:
      """Σ fee_rate × notional sur trades dernières 24h, par wallet.

      Source : `MyOrder.fee_rate` (peuplé par M16/M18 ME.3 dans pipeline_state)
      ou via `FeeRateClient.get_fee_quote(asset_id, condition_id=cid)` lookup
      (post-M18 path nominal — plus consommer `get_fee_rate` deprecated).

      Retourne dict {wallet_address: total_drag_usd}.
      """
  ```
- Le caller principal `list_trader_performance` ajoute la colonne
  `fee_drag_24h_usd: float | None` à `PerformanceRow`. Affichée comme
  4ème colonne après `realized_pnl`, `volume`, `fills`.
- **Wash risk badge** : `wash_score: float | None` dans `PerformanceRow`,
  rendu badge 🟢/🟡/🔴 selon thresholds 0.2/0.5. Conditionnel au flag
  `SCORING_VERSION == "v2.2"` (donc post-MF). Si MF pas shippé, colonne
  absente (template `{% if scoring_version == "v2.2" %}`).
- **Décision D9** §4.10 : features conditionnelles selon ship state.
  Évite de bloquer M19 ship sur MF. Le fee_drag column est **active** post-M19
  car M16 + M18 ME.3 sont déjà shippés.
- **Sources** : Extension Session C + MC + MF dependencies.

#### MH.11 — Bonus N+1 fix `get_home_alltime_stats`

- **Bug actuel** ([queries.py:803-817](../../src/polycopy/dashboard/queries.py#L803-L817)) :
  boucle `await session.execute(...)` par position fermée non-simulée.
  Slow sur /home dès ~50 positions fermées (p50 observé 600ms post-100
  positions).
- **Fix** :
  ```python
  # AVANT : boucle N+1
  for pos in closed_positions:
      pnl = await session.execute(...)
      total_pnl += pnl
  # APRÈS : single aggregation
  agg = (await session.execute(
      select(
          func.coalesce(func.sum(MyPosition.realized_pnl), 0.0).label("total_pnl"),
          func.count(MyPosition.id).label("count_closed"),
      ).where(
          MyPosition.closed_at.is_not(None),
          MyPosition.simulated.is_(simulated_flag),  # M17 MD.1 strict
      )
  )).first()
  ```
- Préserve filtre `simulated` strict M17 MD.1 (un test de régression vérifie
  qu'un flip dry_run → live n'inclut pas les positions virtuelles).
- Gain perf p50 attendu : ~600ms → ~50ms sur 500 positions fermées.
- **Sources** : Audit M-008.

### 2.2 Hors scope explicites

- ❌ **Endpoint POST action** (pin/unpin wallet via dashboard) — décision
  M4.5 "read-only strict" préservée. Future spec si besoin.
- ❌ **Drill-down panel `/scoring` breakdown facteurs v2.1.1** —
  visualisation 7 facteurs côté wallet individual, hors scope MH. Future
  spec post-MF.
- ❌ **Export CSV `/traders/scoring?format=csv`** — bonus utile, hors scope
  M19. Future spec.
- ❌ **Histogramme distribution sous-scores pool** — visualisation
  sophistiquée, hors scope.
- ❌ **Dashboard `/logs` improvements** — workflow logs déjà OK post-M9,
  pas de friction signalée.
- ❌ **Dashboard dark-mode toggle audit** — M6 dark-first + toggle
  `localStorage`, fonctionne OK.
- ❌ **Responsive mobile audit** — M6 responsive via `<details>` sidebar,
  pas de régression signalée.
- ❌ **Refactor complet UI** — M6 récente, pas de refonte.
- ❌ **Migration Alembic 0011 `MyPosition.outcome_side` column** — décision
  D4 retient l'option JOIN (cf. §4.5). Si MH.5 perf insuffisante en prod,
  re-considérer migration en M20+.
- ❌ **Refactor `_format_card_usd` impactant `format_usd` lui-même** —
  D6 §4.7 retire `_format_card_usd` mais ne touche pas `format_usd`
  (déjà 2-décimales aware).
- ❌ **CDN bumps Tailwind/Lucide/HTMX/Chart.js** — pinning M6 strict.
- ❌ **Wash risk badge si MF non shippé** — feature flag template, absent
  par défaut. Activable dès le ship MF.
- ❌ **`localStorage` clé pour dismiss tooltips KPI** — tooltips persistent
  par défaut. Future amélioration si user le demande.
- ❌ **Toucher invariants M17** (MD.1 simulated filter, MD.6 realized_pnl
  populated, MD.2 kill switch CRITICAL bypass digest, MD.7 audit trail
  insert order) — préservés strict.
- ❌ **Toucher invariants M18** (FeeQuote V2 contract, ClobClient V2,
  builder code) — préservés strict.
- ❌ **Toucher scoring** (v1, v2.1, v2.1.1) — read-only sur `trader_scores`.
- ❌ **Migration Alembic** — aucune (D4 trade-off).

---

## 3. User stories

### 3.1 Story A — Blacklister un wallet en < 30s

**Avant M19** :

- User repère un wallet toxique sur `/performance` : `0x21ff…0d71` PnL −$0.55,
  WR 19%, 5 jours actif. Décision : blacklist.
- Click sur la cellule wallet → rien ne se passe (pas interactive).
- Hover : pas de tooltip, juste l'adresse tronquée.
- User ouvre un terminal SSH sur prod : `ssh prod-vm`,
  `sqlite3 ~/.polycopy/data/polycopy.db "SELECT wallet_address FROM target_traders WHERE wallet_address LIKE '0x21ff%';"`,
  copie le full hex 42 chars `0x21ff7c8a3b9f4e2d5c6a8b1d3f5e7c9a0b2d4f60d71`,
  l'ajoute à `BLACKLISTED_WALLETS` dans `.env`,
  `sudo systemctl restart polycopy`.
- Total temps : ~3-5 minutes, friction haute.

**Avec M19 (MH.1)** :

- User repère le wallet sur `/performance`.
- Hover sur la cellule → tooltip natif HTML révèle le full address.
- Click sur le bouton 📋 à côté du tronqué → l'adresse est copiée, tick ✓
  pendant 1.5s confirme l'action.
- User colle dans `.env` `BLACKLISTED_WALLETS=...,0x21ff7c8a3b9f...`,
  `systemctl restart polycopy`.
- Total temps : ~30s, friction basse.

### 3.2 Story B — Comprendre la décomposition PnL

**Avant M19** :

- User regarde /home : `Total USDC $1,006` ; `PnL réalisé −$0.54` ;
  `PnL latent +$7.04`. Pose la question "comment ça s'agence ?".
- Doit re-lire la spec M13 / M17 / docs pour comprendre
  `total_usdc = initial_capital + realized_pnl + latent_pnl`.

**Avec M19 (MH.4)** :

- User hover sur la card "PnL latent" → tooltip natif :
  "Mark-to-market positions ouvertes : Σ (mid_price − avg_buy) × size.
  Change avec les prix marché. Formule : total_usdc = initial_capital +
  realized_pnl + latent_pnl."
- User comprend immédiatement la décomposition. Vérifie : $1000 +
  (−$0.54) + $7.04 ≈ $1006.50 (round → $1,006 pré-MH.7, $1,006.50 post-MH.7).

### 3.3 Story C — Identifier un bug `Gain max latent` BUY NO

**Avant M19** :

- User a 2 positions : BUY YES @ 0.30 size 1 (asset_id A) ; BUY NO @ 0.60
  size 1 (asset_id B sur la même condition). Gain max théorique :
  - YES wins → +0.70 sur YES, perte −0.60 sur NO = +$0.10.
  - NO wins → perte −0.30 sur YES, gain +0.40 sur NO = +$0.10.
- Card /home affiche `Gain max latent +$1.10` :
  - `(1 − 0.30) × 1 = 0.70` (YES OK)
  - `(1 − 0.60) × 1 = 0.40` (NO **incorrect** — formule YES appliquée à NO)
- User suspecte mais pas certain.

**Avec M19 (MH.5)** :

- Fix side-aware : NO formula `avg_price × size = 0.60 × 1 = 0.60`.
- Card affiche `Gain max latent +$1.30` (= 0.70 + 0.60). Cohérent avec
  l'analyse manuelle.
- Tooltip MH.4 explicite la formule side-aware : "Σ (1 − avg_price) × size
  sur YES + Σ avg_price × size sur NO".

### 3.4 Story D — Identifier un wallet à scoring instable

**Avant M19** :

- User regarde `/scoring` : wallet X score 0.65 v2.1, wallet Y score 0.62.
  Difficile de savoir lequel est plus fiable.

**Avec M19 (MH.8)** :

- Stability column ajoute std-badge :
  - X : 🔴 unstable (std 0.12 — score saute de 0.45 à 0.85 cycle après cycle)
  - Y : 🟢 stable (std 0.02 — score oscille 0.61 à 0.63)
- User préfère ajouter Y à `TARGET_WALLETS` : signal robuste, moins
  susceptible de fluctuer post-promotion.

---

## 4. Architecture / décisions clefs

### 4.1 D1 — Copy button vanilla JS, pas de clipboard.js

- Brief MH propose 2 options : (A) clipboard.js (~3 KB minified, dep
  externe), (B) vanilla JS Clipboard API.
- **Option B retenue** : vanilla suffit (~15 lignes), Clipboard API
  supportée par Chrome 66+, Firefox 63+, Safari 13.1+ — couvre 99% des
  desktop browsers. Fallback `execCommand('copy')` pour les <1% restants.
- Cohérent invariant M6 "zéro build step" + "CDN versions pinned".
- Coût implémentation : ~30 minutes (JS + macro + style Tailwind).

### 4.2 D2 — `format_size_precise` 4-tier

- Brief MH propose 3 tiers ; je passe à 4 (entier ≥ 1, 3 dec ≥ 0.01,
  4 dec ≥ 0.0001, scientifique sous le seuil) pour couvrir le cas
  edge `size = 0.00005` (BUY copié à 1% sur source 0.005 share = 5e-5).
- Tooltip avec valeur exacte (`<span title="{{ size }}">`) garantit que
  l'user peut toujours auditer la valeur full précision.
- L'ancien filter `format_size` reste exposé pour rétrocompat ; pas de
  warning structlog pour éviter pollution log (pure UX).

### 4.3 D3.5 — Fenêtre 24h pour APPROVE rate

- Choix de fenêtre : 1h (trop volatile), 24h (cohérent autres /home
  stats), 7j (lag trop long pour détection régression).
- **24h retenu** : aligne avec `trades_detected_24h`, `volume_24h`,
  cohérence visuelle pour l'user.
- Coût query : indexé sur `decided_at` ([models.py:177-181](../../src/polycopy/storage/models.py#L177-L181))
  donc négligeable.

### 4.4 D3 — Tooltip natif HTML

- Pas de JS, pas de modal. `<span title="{{ tooltip }}">` natif.
- Inconvénient : ne s'affiche pas sur mobile tactile (acceptable v1 — la
  plupart des utilisateurs audits sont sur desktop, cf. CLAUDE.md
  M6 dark-first).
- Avantage : zero overhead, accessibility OK (screen readers lisent le
  title).

### 4.5 D4 — Compute outcome via JOIN, pas migration

- Trade-off détaillé :
  - **Option A (JOIN runtime)** : +1 query JOIN par fetch /home,
    ~2-5ms en SQLite local pour 50 positions. Cohérent invariant "no
    migration tant que pas indispensable". Aucun backfill SQL complexe.
  - **Option B (migration 0011 `outcome_side` column)** : 0 query
    runtime extra, mais migration Alembic + backfill SQL complexe (lookup
    `DetectedTrade.outcome` par `(condition_id, asset_id)` pour ~all
    historical positions). Risque de divergence si DetectedTrade.outcome
    pas peuplé pour positions M3 → M14 legacy.
- **Option A retenue**. Si /home p50 dégrade > +50ms en prod sur 500+
  positions, re-considérer en M20+. M19 préserve `alembic upgrade head`
  no-op (head reste 0010).
- Edge case : `DetectedTrade` absent pour la position (cas M3 historique
  où le watcher n'a pas inseré la row, ou résolution market sans trade
  détecté) → fallback `outcome = "Yes"` (cohérent comportement legacy
  conservateur). Documenté dans la docstring helper.

### 4.6 D5 — Break-even exclu denominator + count séparé

- Convention possibles :
  - "break-even = neutre" (exclu dénominateur) : honnête statistiquement
    mais cache les break-even.
  - "break-even = half-win" (ρ=0.5) : controversé, biais vers
    optimisme.
  - "break-even = loss" : pessimiste, biais vers pessimisme.
- **Convention 1 retenue** + **count exposé séparément** dans le subtext.
  Honnête + transparent. User décide d'interpréter `1W/0L/5BE` comme bon
  signal (peu de pertes) ou mauvais (peu de gains).
- Cohérence /home ↔ /performance : appliquée uniformément (régression
  test C-005).

### 4.7 D6 — Unifier sur `format_usd` filter

- `format_usd` actuel
  ([jinja_filters.py:22-42](../../src/polycopy/dashboard/jinja_filters.py#L22-L42))
  est déjà cohérent : `≥ $1M → 1.2M`, `≥ $1k → 1.2k`, `≥ $1 → 12.34`,
  `< $1 → 0.45`. 2 décimales sous $1k.
- `_format_card_usd` ([queries.py:632-637](../../src/polycopy/dashboard/queries.py#L632-L637))
  utilise `round(value)` et `f"{...:,}"` sans décimales — bug.
- **Décision** : retirer `_format_card_usd` ; les cards exposent `value_raw:
  float | None` au template, qui consume `format_usd` filter. Garantit
  cohérence /home ↔ /activité ↔ /performance.
- Risque : autre consommateur de `_format_card_usd` ? Grep ne révèle aucun
  autre callsite. Safe à retirer.

### 4.8 D7 — Stability metric compute query, cache 5 min

- Compute `std(score over last 10 cycles)` côté query.
- SQLite n'expose pas `STDDEV` natif → fallback Python `statistics.stdev`
  sur la liste `score` aggregée (ROW_NUMBER + filter rn ≤ window).
- Cache 5 min via `lru_cache` côté query helper. Invalidation triviale
  par redémarrage (cohérent autres caches Gamma M2, FeeRate M16/M18).
- Display côté template : badge couleur uniquement, std-numeric exposé
  en tooltip pour les power users.

### 4.9 D8 — Spearman rank display rangs locaux

- Le DTO `TraderScoringRow` a déjà `rank_v1` et `rank_v2` (pool-wide).
- Ajouter `rank_v1_local` et `rank_v2_local` (computed sur l'intersection
  v1∩v2). Le caller dispose déjà de l'intersection via le calcul Spearman.
- Template : afficher rangs locaux par défaut + tooltip explicatif.
  Optionnel : badge ✦ si `rank_local != rank_global`.

### 4.10 D9 — Features conditionnelles fee_drag + wash_risk

- `fee_drag_24h_usd` : **active** par défaut post-M19 (M16 + M18 ME.3
  shippés, donnée disponible).
- `wash_risk` : feature-flag template `{% if scoring_version == "v2.2" %}`.
  Si MF non shippé (cas actuel post-M18), colonne absente. Activable
  automatiquement dès le bump `SCORING_VERSION`.

---

## 5. Algorithmes

### 5.1 `format_usd` unified (déjà existant)

```python
def format_usd(value: float | None) -> str:
    if value is None:
        return "—"
    abs_v = abs(value)
    sign = "-" if value < 0 else ""
    if abs_v >= 1_000_000:
        return f"{sign}${abs_v / 1_000_000:.1f}M"
    if abs_v >= 1_000:
        return f"{sign}${abs_v / 1_000:.1f}k"
    return f"{sign}${abs_v:.2f}"
```

Pas de changement vs état actuel. Le retrait de `_format_card_usd`
côté queries est ce qui débloque la cohérence.

### 5.2 `format_size_precise` 4-tier

```python
def format_size_precise(value: float | None) -> str:
    """Cf. spec §4.2 — 4 tiers entier/3dec/4dec/scientifique."""
    if value is None:
        return _EMPTY
    if value == 0:
        return "0"  # PAS "0.00e+00" qui brouille l'œil
    abs_v = abs(value)
    sign = "-" if value < 0 else ""
    if abs_v >= 1:
        return f"{sign}{abs_v:.2f}"
    if abs_v >= 0.01:
        return f"{sign}{abs_v:.3f}"
    if abs_v >= 0.0001:
        return f"{sign}{abs_v:.4f}"
    return f"{sign}{abs_v:.2e}"
```

Cas test exhaustifs :

| Input | Output |
|---|---|
| `None` | `"—"` |
| `0` | `"0"` |
| `0.00001` | `"1.00e-05"` |
| `0.0005` | `"5.00e-04"` (under 0.0001) — NON, 0.0005 ≥ 0.0001 → `"0.0005"` |
| `0.0234` | `"0.023"` (3 dec) |
| `0.5` | `"0.500"` |
| `1.5` | `"1.50"` |
| `123.456` | `"123.46"` |
| `−0.0023` | `"-0.0023"` (4 dec) |

### 5.3 Gain max latent side-aware (MH.5)

```python
# SQL via SQLAlchemy ORM
from sqlalchemy import case, and_, select, func

subq_latest_trade = (
    select(
        DetectedTrade.condition_id,
        DetectedTrade.asset_id,
        DetectedTrade.outcome,
        func.row_number().over(
            partition_by=(DetectedTrade.condition_id, DetectedTrade.asset_id),
            order_by=DetectedTrade.timestamp.desc(),
        ).label("rn"),
    )
).subquery()

stmt = (
    select(
        func.coalesce(
            func.sum(MyPosition.size * MyPosition.avg_price), 0.0
        ).label("exposition"),
        func.coalesce(
            func.sum(
                case(
                    (subq_latest_trade.c.outcome == "Yes",
                     MyPosition.size * (1.0 - MyPosition.avg_price)),
                    else_=MyPosition.size * MyPosition.avg_price,
                )
            ), 0.0,
        ).label("max_profit"),
    )
    .select_from(MyPosition)
    .outerjoin(
        subq_latest_trade,
        and_(
            subq_latest_trade.c.condition_id == MyPosition.condition_id,
            subq_latest_trade.c.asset_id == MyPosition.asset_id,
            subq_latest_trade.c.rn == 1,
        ),
    )
    .where(*open_filter)
)
```

Edge case : `subq_latest_trade.c.outcome IS NULL` (no detected trade
matching) → `else_` branch (NO formula). Conservateur sur position
inconnue. Documenter dans la docstring.

### 5.4 Win rate avec break-even count (MH.6)

```python
wins = sum(1 for p in closed_pnls if p is not None and float(p) > 0)
losses = sum(1 for p in closed_pnls if p is not None and float(p) < 0)
breakevens = sum(1 for p in closed_pnls if p is not None and float(p) == 0)
decided = wins + losses
win_rate_pct = (wins / decided * 100.0) if decided > 0 else None
```

Display side : Jinja
```jinja
{{ alltime.win_rate_pct | format_pct }}
{% if alltime.breakeven_count > 0 %}
  <span class="text-xs">({{ alltime.wins }}W / {{ alltime.losses }}L / {{ alltime.breakeven_count }} break-even)</span>
{% else %}
  <span class="text-xs">({{ alltime.wins }}W / {{ alltime.losses }}L)</span>
{% endif %}
```

### 5.5 Stability metric (MH.8)

```python
import statistics

async def compute_scoring_stability_for_pool(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    window: int = 10,
    version: str = "v2.1",
) -> dict[str, tuple[float | None, int]]:
    """Retourne dict {wallet: (std | None, n_cycles)}.

    SQLite n'a pas STDDEV natif → fallback Python statistics.stdev.
    """
    async with session_factory() as session:
        # Fetch les `window` derniers scores par wallet, version filter.
        subq = (
            select(
                TraderScore.wallet_address,
                TraderScore.score,
                func.row_number().over(
                    partition_by=TraderScore.wallet_address,
                    order_by=TraderScore.computed_at.desc(),
                ).label("rn"),
            )
            .where(TraderScore.scoring_version == version)
            .subquery()
        )
        rows = (await session.execute(
            select(subq.c.wallet_address, subq.c.score)
            .where(subq.c.rn <= window)
        )).all()
    # Group par wallet + Python stdev.
    by_wallet: dict[str, list[float]] = {}
    for row in rows:
        by_wallet.setdefault(row.wallet_address, []).append(float(row.score))
    return {
        wallet: (statistics.stdev(scores) if len(scores) >= 2 else None, len(scores))
        for wallet, scores in by_wallet.items()
    }
```

Badge dispatch côté Jinja :

```jinja
{% set std, n = stability.get(wallet, (None, 0)) %}
{% if n < 10 %}
  <span class="badge">⏳ insufficient</span>
{% elif std is none or std < 0.03 %}
  <span class="badge badge-ok">🟢 stable</span>
{% elif std < 0.08 %}
  <span class="badge badge-warning">🟡 volatile</span>
{% else %}
  <span class="badge badge-error">🔴 unstable</span>
{% endif %}
```

### 5.6 N+1 fix `get_home_alltime_stats` (MH.11)

```python
# AVANT (boucle N+1, p50 ~600ms sur 500 positions) :
for pos_id in closed_position_ids:
    pnl = (await session.execute(
        select(MyPosition.realized_pnl).where(MyPosition.id == pos_id)
    )).scalar_one()
    total += pnl

# APRÈS (single aggregation, p50 ~50ms) :
agg = (await session.execute(
    select(
        func.coalesce(func.sum(MyPosition.realized_pnl), 0.0).label("total_pnl"),
        func.count(MyPosition.id).label("count_closed"),
    ).where(
        MyPosition.closed_at.is_not(None),
        MyPosition.simulated.is_(simulated_flag),  # M17 MD.1 strict
    )
)).first()
```

---

## 6. DTOs

### 6.1 `HomeAllTimeStats` étendu

```python
class HomeAllTimeStats(BaseModel):
    realized_pnl_total: float
    volume_usd_total: float
    fills_count: int
    fills_rate_pct: float | None
    strategy_approve_rate_pct: float | None
    approve_rate_window_hours: int = 24  # MH.3 — explicite la fenêtre
    top_trader: dict[str, float | str | None] | None
    uptime: timedelta | None
    open_exposition_usd: float = 0.0
    open_max_profit_usd: float = 0.0
    open_latent_pnl_usd: float = 0.0
    win_rate_pct: float | None = None
    wins: int = 0  # MH.6 — exposé pour le subtext
    losses: int = 0  # MH.6
    breakeven_count: int = 0  # MH.6
```

### 6.2 `TraderScoringRow` étendu (MH.8 + MH.9)

```python
class TraderScoringRow(BaseModel):
    wallet_address: str
    label: str | None
    score_v1: float | None
    score_v2: float | None
    rank_v1_pool: int | None  # rank pool entier (existant, renommé)
    rank_v2_pool: int | None
    rank_v1_local: int | None  # MH.9 — rank intersection v1∩v2
    rank_v2_local: int | None  # MH.9
    stability_std: float | None  # MH.8
    stability_n: int = 0  # MH.8 — count de cycles disponibles
```

### 6.3 `PerformanceRow` étendu (MH.10)

```python
class PerformanceRow(BaseModel):
    wallet_address: str
    label: str | None
    realized_pnl_usd: float
    volume_usd: float
    fills_count: int
    win_rate_pct: float | None
    breakeven_count: int = 0  # cohérence MH.6
    fee_drag_24h_usd: float | None = None  # MH.10 — peuplé si M16/M18 actif
    wash_score: float | None = None  # MH.10 — feature flag MF
```

### 6.4 `KpiCard` simplifié (MH.7)

```python
class KpiCard(BaseModel):
    title: str
    value_raw: float | None  # MH.7 — numeric, formaté côté Jinja via format_usd
    delta: str | None
    delta_sign: Literal["positive", "negative", "neutral"] | None
    sparkline_points: list[tuple[datetime, float]]
    icon: str
    tooltip: str | None = None  # MH.4 — explicatif KPI
```

Le champ `value: str` actuel pré-formaté (via `_format_card_usd`) est
**remplacé** par `value_raw: float | None`. Migration template inline :
```jinja
<p class="text-2xl">{{ card.value_raw | format_usd }}</p>
```
au lieu de `{{ card.value }}`.

---

## 7. Settings (env vars + Pydantic)

**Aucun nouveau setting M19**. UX-only — tout côté display.

Settings existants impactés (read-only) :
- `SCORING_VERSION` : MH.8 affiche dans le cutover panel.
- `SCORING_V2_SHADOW_DAYS` : MH.8 affiche dans le cutover panel.
- `EXECUTION_MODE` : MH.5 + MH.11 préservent le filtre `simulated`.

---

## 8. Invariants sécurité

### 8.1 Dashboard read-only strict (M4.5) — préservé

M19 ajoute 0 POST/PUT/DELETE. Toutes les nouvelles routes (le cas échéant
pour MH.8 cutover panel) en `@router.get(...)`. Vérifié par grep dans
`tests/unit/test_dashboard_security.py`.

### 8.2 CDN versions pinned (M6) — inchangées

- Tailwind 3.4.16, HTMX 2.0.4, Chart.js 4.4.7, Lucide 0.469.0.
- MH.1 utilise vanilla JS (~15 lignes), aucune nouvelle dep CDN.
- MH.4 tooltips natifs HTML, aucune nouvelle dep.

### 8.3 Grep security anti-leak — préservé

- `tests/unit/test_dashboard_security.py` + `test_dashboard_security_m6.py`
  continuent à passer (zéro fuite `POLYMARKET_PRIVATE_KEY`,
  `TELEGRAM_BOT_TOKEN`, `CLOB_API_SECRET`, `REMOTE_CONTROL_TOTP_SECRET`).
- Wallet addresses + condition_ids sont **publics** on-chain (pattern M5
  TARGET_WALLETS, MH.1 copy button OK).

### 8.4 `localStorage` discipline M9/M10 — préservée

- M19 n'ajoute aucune clé `localStorage` v1.
- Pattern `polycopy.<feature>` cohérent `polycopy.theme` /
  `polycopy.logs.preset` si extension future.
- Aucun secret stocké côté client.

### 8.5 Filtre `simulated` strict M17 MD.1 — préservé

- MH.5 (gain max latent) : query JOIN avec `subq_latest_trade` préserve
  `MyPosition.simulated.is_(simulated_flag)` aux 3 sites du pipeline.
- MH.11 (alltime stats N+1 fix) : single aggregation préserve le filtre.
- Test régression : `test_alltime_stats_filter_simulated_strict_under_live_mode`
  (vérifie qu'un flip dry_run → live n'inclut pas les positions virtuelles).

### 8.6 Convergence /home ↔ /performance M17 MD.6 — préservée

- MH.6 (win rate break-even) : convention identique des 2 côtés.
- MH.7 (TOTAL USDC arrondi) : `format_usd` filter unique → cohérence
  garantie.
- Test régression C-005 préservé : `test_home_performance_converge_within_1cent`.

### 8.7 `FeeQuote` M18 ME.3 — consommé

- MH.10 fee_drag column consume `get_fee_quote(token_id, condition_id=cid)`,
  **pas** `get_fee_rate(token_id)` (alias deprecated qui émet warning
  structlog 1× par token sinon).
- Préfère lire `MyOrder.fee_rate` si déjà stocké (post-M16 : pipeline_state
  JSON contient `fee_rate`, `fee_cost_usd`, `ev_after_fee_usd`).

### 8.8 Versioning sacré scoring — préservé

- M19 ne touche aucune fonction `compute_score_*`.
- `SCORING_VERSIONS_REGISTRY` intact.
- Aucune row `trader_scores` réécrite.
- MH.8 stability metric : pure read sur `trader_scores` append-only.

### 8.9 Append-only DB — préservé

- Aucune migration Alembic 0011 (D4 trade-off retient JOIN runtime).
- `alembic upgrade head` retourne "no migrations to apply" post-M19. Head
  reste **0010**.

---

## 9. Test plan

### 9.1 Tests unitaires (~12 nouveaux + 8 régressions)

#### MH.1 — Copy button (3 tests)

1. `test_render_address_macro_truncates_correctly` — la macro
   `render_address("0x1234...abcd")` produit le HTML contenant
   `0x1234…abcd` + bouton copy.
2. `test_render_address_includes_full_address_in_title` — `title=` attribut
   contient le full address (fullhash).
3. `test_copy_btn_rendered_on_every_wallet_display_page` — grep
   automatique sur les 11 templates pour vérifier la présence de
   `render_address` (pas de regression sur un template oublié).

#### MH.2 — `format_size_precise` (2 tests)

4. `test_format_size_precise_4_tiers` — table cas (None, 0, 0.00001,
   0.0234, 0.5, 1.5, 123.456, −0.0023) match les outputs §5.2.
5. `test_format_size_precise_zero_returns_zero_not_scientific` — edge case
   `format_size_precise(0) == "0"`.

#### MH.3 — APPROVE 24h (1 test)

6. `test_strategy_approve_rate_uses_24h_window` — seed 5 decisions @ now,
   3 decisions @ now-25h ; le approve_rate calcule sur les 5 récentes
   uniquement.

#### MH.4 — Tooltips (1 test)

7. `test_kpi_card_renders_tooltip_when_provided` — `kpi_card(card,
   tooltip="...")` produit `<span title="...">`.

#### MH.5 — Gain max side-aware (2 tests)

8. `test_gain_max_latent_correct_for_buy_yes` — seed 1 BUY YES @ 0.30
   size 1, gain max = 0.70.
9. `test_gain_max_latent_correct_for_buy_no` — seed 1 BUY NO @ 0.60 size
   1, gain max = 0.60.

#### MH.6 — Win rate break-even (2 tests)

10. `test_win_rate_excludes_break_even_from_denominator` — seed 1W + 0L +
    5BE → win_rate = 100%, breakeven_count = 5.
11. `test_win_rate_label_shows_break_even_count` — template /home affiche
    `(1W / 0L / 5 break-even)`.

#### MH.7 — Format USD cohérent (2 tests)

12. `test_kpi_card_total_usdc_uses_format_usd_filter` — render `/home`,
    KPI `Total USDC` affiche `$1,006.50` (pas `$1006`).
13. `test_home_performance_converge_within_1cent` — régression C-005,
    `total_usdc /home == sum(realized_pnl) /performance + initial_capital
    + latent_pnl` à < 1 cent.

#### MH.8 — Stability (2 tests)

14. `test_compute_scoring_stability_returns_std_per_wallet` — seed 10
    cycles avec scores [0.5, 0.5, ..., 0.5] → std=0.0 ; [0.3, 0.7, 0.3,
    0.7, ...] → std ≈ 0.21.
15. `test_scoring_dashboard_stability_badge_dispatch` — render
    `/scoring`, badge 🟢 si std < 0.03, 🔴 si ≥ 0.08.

#### MH.9 — Spearman rank locaux (1 test)

16. `test_spearman_rank_display_uses_local_intersection_ranks` — seed 13
    wallets sur intersection N=13, render `/scoring`, ranks affichés sont
    1-13 pas 1-50.

#### MH.10 — Performance enrichi (1 test)

17. `test_performance_fee_drag_column_active_post_m16_m18` —
    `SCORING_VERSION="v2.1"` + M16/M18 shippés → colonne fee_drag
    rendue. `wash_risk` absent (MF non shippé).

#### MH.11 — N+1 fix (2 tests)

18. `test_home_alltime_stats_single_query_aggregation` — instrument SQL
    counter, vérifie 1 query au lieu de N.
19. `test_home_alltime_stats_filter_simulated_strict_M17_MD1_preserved` —
    flip `EXECUTION_MODE=live` + 5 positions virtuelles + 3 positions
    live → alltime_stats calcule sur 3 live, ignore les 5 virtuelles.

### 9.2 Tests régression (8 préservés)

- `test_dashboard_security.py` — aucun secret leak (M4.5 invariant).
- `test_dashboard_security_m6.py` — aucun secret leak (M6 invariant).
- `test_format_usd_basic` — `format_usd(1006.50) == "$1.0k"` (pas changé).
- `test_format_usd_under_1k_uses_2_decimals` — `format_usd(0.45) == "$0.45"`.
- `test_home_performance_converge_within_1cent` — convergence C-005.
- `test_alltime_stats_filter_simulated_strict_under_live_mode` — M17 MD.1.
- `test_kpi_card_sparkline_filter_consistency` — L-004 (latest_snapshot
  filter cohérent).
- `test_short_hash_truncation_correctness` — short_hash inchangé.

### 9.3 Tests integration (2)

- `test_dashboard_smoke_home_renders_with_seeded_db` — boot dashboard,
  fetch /home + /scoring + /performance, vérifier que le HTML contient
  `copy-btn`, `tooltip`, `format_usd`, `stability` selon settings.
- `test_dashboard_smoke_size_displays_precise` — fetch /activité avec
  fixture size=0.0234 → output contient `0.023` (pas `0.00`).

### 9.4 Total

- **~19 tests unit nouveaux** + **8 régressions** + **2 integration**.
- Charge : ~30 min/test simple, ~1h/test integration → ~10h tests.

---

## 10. Impact existant

### 10.1 Fichiers modifiés

Cf. table §1.4. Récapitulation par fichier :

| Fichier | Lignes touchées | MH |
|---|---|---|
| [src/polycopy/dashboard/jinja_filters.py](../../src/polycopy/dashboard/jinja_filters.py) | +`format_size_precise` (~20 lines) | MH.2 |
| [src/polycopy/dashboard/queries.py](../../src/polycopy/dashboard/queries.py) | retire `_format_card_usd` ; refactor `get_home_alltime_stats` (N+1 fix) ; query side-aware gain max ; +helpers stability + fee_drag (~120 lines net) | MH.5+MH.6+MH.7+MH.8+MH.10+MH.11 |
| [src/polycopy/dashboard/dtos.py](../../src/polycopy/dashboard/dtos.py) | étend HomeAllTimeStats / TraderScoringRow / PerformanceRow / KpiCard (~30 lines) | MH.3+MH.4+MH.6+MH.8+MH.9+MH.10 |
| [src/polycopy/dashboard/templates/macros.html](../../src/polycopy/dashboard/templates/macros.html) | +`render_address` macro ; étend `kpi_card` (~30 lines) | MH.1+MH.4 |
| [src/polycopy/dashboard/templates/base.html](../../src/polycopy/dashboard/templates/base.html) | +inline JS `copyToClipboard` + style `copy-btn` (~30 lines) | MH.1 |
| [src/polycopy/dashboard/templates/home.html](../../src/polycopy/dashboard/templates/home.html) | use `render_address` ; tooltips ; format `value_raw` | MH.1+MH.4+MH.6+MH.7 |
| [src/polycopy/dashboard/templates/activity.html](../../src/polycopy/dashboard/templates/activity.html) | use `render_address` + `format_size_precise` | MH.1+MH.2 |
| [src/polycopy/dashboard/templates/strategy.html](../../src/polycopy/dashboard/templates/strategy.html) | idem | MH.1+MH.2 |
| [src/polycopy/dashboard/templates/orders.html](../../src/polycopy/dashboard/templates/orders.html) | idem | MH.1+MH.2 |
| [src/polycopy/dashboard/templates/positions.html](../../src/polycopy/dashboard/templates/positions.html) | idem ; tooltip outcome | MH.1+MH.5 |
| [src/polycopy/dashboard/templates/pnl.html](../../src/polycopy/dashboard/templates/pnl.html) | use `render_address` sur milestones | MH.1 |
| [src/polycopy/dashboard/templates/traders.html](../../src/polycopy/dashboard/templates/traders.html) | use `render_address` | MH.1 |
| [src/polycopy/dashboard/templates/traders_scoring.html](../../src/polycopy/dashboard/templates/traders_scoring.html) | use `render_address` ; ranks locaux ; Spearman tooltip ; stability badge ; cutover panel | MH.1+MH.8+MH.9 |
| [src/polycopy/dashboard/templates/performance.html](../../src/polycopy/dashboard/templates/performance.html) | use `render_address` ; fee_drag column ; wash_risk feature flag | MH.1+MH.10 |
| [src/polycopy/dashboard/templates/detections.html](../../src/polycopy/dashboard/templates/detections.html) | use `render_address` | MH.1 |
| [tests/unit/test_dashboard_*.py](../../tests/unit/) | +~19 tests | tous |
| [tests/integration/test_dashboard_smoke.py](../../tests/integration/) | nouveau ; 2 tests | MH.1+MH.2 |

### 10.2 Fichiers nouveaux

| Fichier | Description | MH |
|---|---|---|
| [tests/integration/test_dashboard_smoke.py](../../tests/integration/test_dashboard_smoke.py) | Smoke render /home + /scoring + /performance contre DB seedée | MH.1+MH.2+MH.7 |

### 10.3 Fichiers strictement intacts

- [src/polycopy/storage/models.py](../../src/polycopy/storage/models.py) —
  pas de migration, schema DB inchangé (D4 trade-off).
- [alembic/versions/](../../alembic/versions/) — pas de migration 0011.
- [src/polycopy/strategy/](../../src/polycopy/strategy/) — pipeline /
  pricer / scoring intacts.
- [src/polycopy/executor/](../../src/polycopy/executor/) — fee client /
  write client intacts (M18 préservé).
- [src/polycopy/discovery/](../../src/polycopy/discovery/) — scoring v1/
  v2.1/v2.1.1 intacts.
- [src/polycopy/monitoring/](../../src/polycopy/monitoring/) — alertes /
  PnL writer intacts.
- [src/polycopy/remote_control/](../../src/polycopy/remote_control/) —
  Tailscale auth intact.
- [src/polycopy/watcher/](../../src/polycopy/watcher/) — DetectedTrade.outcome
  déjà peuplé (pas de changement watcher).
- [CLAUDE.md](../../CLAUDE.md) — pas de bloc M19 dédié (UX polish, pas de
  conventions/sécurité nouvelle à documenter).

---

## 11. Migration / rollout

### 11.1 Rollout

M19 = ship 11 commits atomiques + restart bot. Aucune intervention
externe (pas de Polymarket cutover, pas de new env var).

```bash
# 1. Sur la machine de dev — merge sur main
cd ~/code/polycopy
git pull origin main

# 2. Sur la machine prod — pull + restart
ssh prod-machine
cd ~/Documents/GitHub/polycopy
git pull origin main
source .venv/bin/activate
pip install -e .   # pas de nouvelle dep mais regen metadata

# 3. Restart
sudo systemctl restart polycopy
```

Smoke validation immédiate :

```bash
curl -sf http://127.0.0.1:8000/home | grep -E "copy-btn|render_address" | head -5
# → présence des éléments MH.1
curl -sf http://127.0.0.1:8000/activité | grep "Size" | head -3
# → Size displays 0.001 / 0.023 / 1.50 (pas 0.00)
curl -sf http://127.0.0.1:8000/scoring | grep -E "stability|🟢|🟡|🔴"
# → badges stability rendus
```

### 11.2 Rollback

- Trivial : `git revert <merge-sha>` + restart. Aucun side-effect DB
  (pas de migration, pas d'écriture nouvelle).

### 11.3 Risques + mitigations

| Risque | Probabilité | Impact | Mitigation |
|---|---|---|---|
| MH.5 JOIN runtime dégrade /home p50 > +50ms | Moyen | UX dégradée | Mesurer post-merge ; si +50ms réel, considérer migration 0011 en M20+. |
| Templates oublient `render_address` sur une vue | Faible | Adresse non-copiable | Test grep automatisé `test_copy_btn_rendered_on_every_wallet_display_page`. |
| `format_size_precise` edge case 0.00009 (sous 0.0001 mais ≥ 0) | Faible | Affichage scientifique surprenant | Couvert par cas test §5.2 ; docstring explicite. |
| Browser ancien sans Clipboard API | Très faible | Copy fail silent | Fallback `execCommand` couvre <1% browsers. |
| Stability metric SQLite STDDEV missing | Certain | N/A | Fallback Python `statistics.stdev` (cf. §5.5). |
| Tooltip `<span title>` sur mobile tactile | Certain | UX dégradée mobile | Acceptable v1, hors scope. |
| Win rate breakeven count cohérence /performance | Moyen | Divergence C-005 | Test régression `test_home_performance_converge_within_1cent`. |
| `_format_card_usd` retiré, autre callsite ? | Faible | Render error | Grep automatisé pré-merge `grep -rn "_format_card_usd" src/` doit retourner 0. |
| Wash risk badge feature flag mal configuré (rendu si MF pas shipped) | Faible | Colonne avec valeurs `None` | Template guard `{% if scoring_version == "v2.2" %}` strict. |
| Tests dashboard fixtures DB ne seed pas DetectedTrade pour MH.5 | Moyen | Test echec | Vérifier fixture `seed_dashboard_db` peuple `DetectedTrade.outcome` ; sinon enrichir. |

---

## 12. Commandes de vérification

### 12.1 Smoke test final avant merge

```bash
# 1. Tests unit
pytest tests/unit/test_dashboard_*.py \
       tests/unit/test_jinja_filters*.py \
       tests/unit/test_dashboard_queries*.py \
       -x --tb=short

# 2. Tests sécurité dashboard (régression)
pytest tests/unit/test_dashboard_security*.py -x --tb=short

# 3. Lint + types
ruff check . && ruff format --check . && mypy src --strict

# 4. Tests integration (smoke render)
pytest tests/integration/test_dashboard_smoke.py -m integration -x --tb=short

# 5. Smoke runtime — boot dashboard, fetch toutes les pages
EXECUTION_MODE=dry_run DASHBOARD_ENABLED=true python -m polycopy --verbose &
sleep 8
for path in home détection stratégie exécution positions pnl activité traders performance scoring; do
  curl -sf "http://127.0.0.1:8000/$path" > /dev/null && echo "OK $path" || echo "FAIL $path"
done
pkill -f polycopy

# 6. Vérification visuelle (manuelle, ~5 min, cf. spec §3 user stories)
```

### 12.2 Vérification anti-régression `_format_card_usd`

```bash
# Aucun callsite résiduel hors queries.py historique
grep -rn "_format_card_usd" src/ tests/ --include="*.py"
# → doit retourner 0 lignes (post-merge)
```

### 12.3 Vérification `render_address` sur 11 vues

```bash
# Toutes les vues qui listent un wallet ou condition_id doivent l'utiliser
for tpl in home.html activity.html strategy.html orders.html positions.html \
           pnl.html traders.html traders_scoring.html performance.html \
           detections.html; do
  count=$(grep -c "render_address" "src/polycopy/dashboard/templates/$tpl" 2>/dev/null || echo 0)
  echo "$tpl: $count usages"
done
# → tous ≥ 1 (sauf logs.html / backtest.html / latency.html non-concernées)
```

### 12.4 Vérification head Alembic inchangé

```bash
alembic upgrade head 2>&1 | tail -3
# → "no migrations to apply" ; head reste 0010
```

---

## 13. Notes d'implémentation (pièges fréquents)

### 13.1 Piège : `format_size_precise(0)` notation scientifique

`format_size_precise(0)` doit retourner `"0"` pas `"0.00e+00"` (notation
scientifique brouille l'œil). Cas edge dans le code §5.2.

### 13.2 Piège : tooltip HTML natif mobile

Sur mobile tactile, `<span title>` ne s'affiche pas au hover. **Acceptable**
v1 UX polish : la plupart des audits sont sur desktop. Future amélioration
mobile-friendly via `details/summary` HTML ou bibliothèque tooltip (hors
scope MH).

### 13.3 Piège : Spearman rank confusion locales vs globales

Confusion possible user : "pourquoi le rang sur /scoring est différent de
celui sur /traders ?". Tooltip MH.9 **doit** expliquer clairement : "Rang
sur intersection v1∩v2 (N=13), pas sur le pool entier (N=50)". Documenter
explicitement dans le template.

### 13.4 Piège : MH.5 JOIN edge case `outcome IS NULL`

Si `DetectedTrade` absent pour la position (cas M3 historique où le
watcher n'a pas inséré la row, ou résolution market sans trade détecté
préalable), `subq_latest_trade.c.outcome IS NULL` → `case else_` branche
prend le NO formula. **Conservateur** mais peut surestimer si la position
était en fait YES. Documenter dans la docstring.

Alternative : prendre la formule YES en fallback (cohérent comportement
legacy). **Décision** à figer dans la spec — recommande YES fallback car
c'est le comportement legacy avant MH.5, donc régression-free pour les
positions historiques sans DetectedTrade.

### 13.5 Piège : CDN version pinning

Les versions Tailwind 3.4.16, HTMX 2.0.4, Chart.js 4.4.7, Lucide 0.469.0
sont pinned (CLAUDE.md §M6). M19 ne doit **pas** modifier ces versions.

### 13.6 Piège : grep security `test_dashboard_security.py`

Test existant grep `POLYMARKET_PRIVATE_KEY`, `TELEGRAM_BOT_TOKEN`,
`CLOB_API_SECRET`, `REMOTE_CONTROL_TOTP_SECRET` dans les templates
rendered. M19 ajoute macros + JS vanilla — **vérifier** que rien ne leak
accidentellement (unlikely mais safety check).

### 13.7 Piège : SQLite STDDEV missing

SQLite n'expose pas `STDDEV` natif. Fallback Python `statistics.stdev`
sur la liste `score` aggregée (cf. §5.5). PostgreSQL/MySQL exposent
`STDDEV` natif → si polycopy migre vers PG futur, simplifier en SQL.

### 13.8 Piège : `KpiCard.value` retiré, autres callsites ?

`get_home_kpi_cards` retire `_format_card_usd` mais d'autres callsites de
`KpiCard.value`(_str_) ? Grep automatisé pré-merge :
```bash
grep -rn "card.value" src/polycopy/dashboard/templates/ | grep -v "value_raw"
```
Doit retourner 0 lignes (sauf migration template effectuée).

### 13.9 Piège : test fixture DetectedTrade pour MH.5

Les fixtures dashboard existantes seedent `MyPosition` mais pas forcément
`DetectedTrade.outcome`. Vérifier `tests/conftest.py::seed_dashboard_db`
ou équivalent — enrichir si nécessaire pour couvrir BUY YES + BUY NO.

### 13.10 Piège : MH.6 `breakeven_count` cohérence

`breakeven_count` doit être exposé **côté /home et /performance**. Si
seulement /home, divergence C-005 implicite (test passe car les deux
formules donnent le même `wins/decided` mais `/performance` n'expose
pas le compte BE).

---

## 14. Hors scope (liste exhaustive)

Cf. §2.2. Récapitulation pour easier search :

- ❌ POST endpoints dashboard (pin/unpin via UI).
- ❌ Drill-down breakdown facteurs scoring v2.1.1.
- ❌ Export CSV `/traders/scoring`.
- ❌ Histogramme distribution sous-scores.
- ❌ Improvements `/logs` (déjà OK post-M9).
- ❌ Dark-mode toggle audit.
- ❌ Responsive mobile audit.
- ❌ Refactor complet UI.
- ❌ Migration Alembic 0011 `outcome_side` column (D4 retient JOIN).
- ❌ Refactor `format_usd` filter (déjà OK).
- ❌ CDN bumps (Tailwind/Lucide/HTMX/Chart.js).
- ❌ Wash risk badge si MF non shippé (feature flag).
- ❌ `localStorage` dismiss tooltips.
- ❌ Toucher invariants M17 / M18 (FeeQuote, kill switch ordering, etc.).
- ❌ Toucher scoring (v1, v2.1, v2.1.1).
- ❌ Migration Alembic.

---

## 15. Mapping origines (traçabilité)

| Item | Audit | Session bug | Synthèse roadmap | Commentaire |
|---|---|---|---|---|
| MH.1 | — | C (C1 core) | User feedback + §6.3 | Friction quotidienne signalée explicitement 2026-04-24 |
| MH.2 | [L-027] | C (C2) | UX fix | Filter format_size existant produit `0.00` |
| MH.3 | — | C (C3) | User feedback | Biais reset positions/capital |
| MH.4 | — | C (C5) | User feedback | "je ne comprend pas trop" 2026-04-24 |
| MH.5 | [M-011] | C (implicite) | Audit M-011 | (1 − avg_price) × size invalide BUY NO |
| MH.6 | [M-010] | C (new) | Audit M-010 | 1W/0L/5BE = 100% WR faux signal |
| MH.7 | [L-005] | C (C4) | Audit L-005 | _format_card_usd vs format_usd |
| MH.8 | — | C (extension) | Claude §6 B6 + MF shadow | Stability metric utile post-MA |
| MH.9 | [I-008] | B (B5 partial) | Session B partial | Spearman ranks pool vs intersection |
| MH.10 | — | C (extension) | MC + MF dependencies | fee_drag post-M16/M18, wash post-MF |
| MH.11 | [M-008] | C (new bonus) | Audit M-008 | N+1 alltime stats sur 50+ positions |

---

## 16. Prompt de génération de spec

Cf. message session 2026-04-27 soir (ce document est le résultat).
Prompt corrigé du brief MH.md §10 disponible dans la session de chat.

---

## 17. Commit messages proposés

Ordre recommandé (du plus simple au plus complexe, parallélisable) :

### MH.1 — Bouton copier adresse + tooltip fullhash

```text
feat(dashboard): M19 MH.1 render_address macro + clipboard copy button

- New Jinja macro `render_address(value, kind="wallet"|"condition")` in
  templates/macros.html : tronque à `0xabcd…7890`, tooltip `title=` avec
  fullhash, bouton copy avec icône Lucide
- Inline JS `copyToClipboard(text, btn)` dans base.html (~15 lines vanilla,
  Clipboard API + execCommand fallback) — zéro dep externe (D1)
- Style Tailwind `.copy-btn` opacity 0.4 → 1 hover, ✓ post-copy 1.5s
- Appliqué sur 11 vues : home, activity, strategy, orders, positions, pnl,
  traders, traders_scoring, performance, detections (logs/backtest/latency
  non-concernées)
- Tests : test_render_address_macro_truncates + test_copy_btn_rendered_on_every_wallet_page
- Cf. spec M19 §2.1 MH.1 + §4.1 D1
```

### MH.7 — Fix arrondi TOTAL USDC cohérent

```text
fix(dashboard): M19 MH.7 unify format_usd across all KPI cards

- Retire `_format_card_usd` dans queries.py (entiers `$1006`)
- KpiCard expose `value_raw: float | None` au template ; le rendu Jinja
  consume `format_usd` filter (déjà 2-décimales aware sous $1k, k-notation
  ≥ $1k, M-notation ≥ $1M)
- Garantit cohérence /home ↔ /activité ↔ /performance (régression test C-005)
- Tests : test_kpi_card_uses_format_usd + test_home_performance_converge_1cent
- Audit L-005 fix
- Cf. spec M19 §2.1 MH.7 + §4.7 D6
```

### MH.2 — Fix Size 0.00 display

```text
feat(dashboard): M19 MH.2 format_size_precise 4-tier filter

- New filter `format_size_precise(value)` jinja_filters.py — 4 tiers :
  entier ≥ 1, 3 décimales ≥ 0.01, 4 décimales ≥ 0.0001, scientifique sinon
- Edge case `value == 0` retourne `"0"` (pas `"0.00e+00"`)
- Tooltip avec valeur exacte (`<span title="{{ size }}">`)
- Appliqué sur /activité, /exécution, /positions
- L'ancien `format_size` reste exposé (rétrocompat)
- Tests : test_format_size_precise_4_tiers + test_zero_returns_zero_not_scientific
- Audit L-027 fix
- Cf. spec M19 §2.1 MH.2 + §4.2 D2
```

### MH.3 — APPROVE STRATÉGIE 24h

```text
fix(dashboard): M19 MH.3 strategy_approve_rate uses 24h sliding window

- queries.py:864-878 : filtre `decision_rows` sur `decided_at >= now - 24h`
- Cohérent avec autres /home stats (trades_detected_24h, volume_24h)
- Label UI "APPROVE STRATÉGIE (24h)" explicite la fenêtre
- HomeAllTimeStats.approve_rate_window_hours: int = 24 exposé
- Pas de migration, juste un filtre SQL ajouté (decided_at déjà indexé)
- Tests : test_strategy_approve_rate_uses_24h_window
- Cf. spec M19 §2.1 MH.3 + §4.3
```

### MH.4 — Tooltips KPI cards

```text
feat(dashboard): M19 MH.4 explanatory tooltips on home KPI cards

- macros.html : kpi_card accept tooltip kwarg → <span title="...">
- Wirer 6 cartes /home : PnL réalisé, PnL latent, Gain max latent,
  Exposition, Drawdown, Win rate
- Décomposition `total_usdc = initial_capital + realized_pnl + latent_pnl`
  exposée dans tooltip PnL latent
- Icône Lucide `info` discret à côté du title
- Tooltip natif HTML, zéro JS, accessibility OK (D3)
- Tests : test_kpi_card_renders_tooltip_when_provided
- Cf. spec M19 §2.1 MH.4 + §4.4 D3
```

### MH.6 — Win rate break-even handling

```text
fix(dashboard): M19 MH.6 win rate exposes break-even count separately

- queries.py:949-952 : compute breakevens = count(realized_pnl == 0)
- HomeAllTimeStats étendu : +breakeven_count, +wins, +losses
- Template /home subtext : "1W / 0L / 5 break-even" si breakeven_count > 0
- Convention D5 : break-even neutre, exclu denominator + count exposé
- Cohérence /home ↔ /performance (régression test C-005)
- Tests : test_win_rate_excludes_break_even + test_label_shows_count
- Audit M-010 fix
- Cf. spec M19 §2.1 MH.6 + §4.6 D5
```

### MH.9 — Spearman rank locaux

```text
fix(dashboard): M19 MH.9 spearman uses local intersection ranks

- TraderScoringRow étendu : +rank_v1_local, rank_v2_local (sur intersection
  v1∩v2 utilisée pour Spearman, pas pool entier)
- Template traders_scoring.html affiche rangs locaux par défaut
- Tooltip header colonne explique : "Rang sur intersection v1∩v2 (N=...)"
- Pas de changement côté calcul Spearman (correct depuis 1ba8ae3)
- Tests : test_spearman_rank_display_uses_local_intersection_ranks
- Audit I-008 fix
- Cf. spec M19 §2.1 MH.9 + §4.9 D8
```

### MH.5 — Gain max latent side-aware

```text
fix(dashboard): M19 MH.5 gain max latent computes side-aware via JOIN

- queries.py:921-924 : JOIN MyPosition × DetectedTrade (latest by timestamp)
  via subquery row_number() ; case branch YES (1 - avg_price) × size,
  else (NO) avg_price × size
- Pas de migration Alembic (D4 trade-off — JOIN runtime vs migration 0011)
- Edge case outcome IS NULL → fallback YES (legacy behavior conservateur)
- Préserve filtre simulated == (execution_mode != "live") M17 MD.1
- Tests : test_gain_max_latent_buy_yes + test_gain_max_latent_buy_no
- Audit M-011 fix
- Cf. spec M19 §2.1 MH.5 + §4.5 D4
```

### MH.8 — Scoring stability + cutover panel

```text
feat(dashboard): M19 MH.8 scoring stability metric + cutover status panel

- New helper compute_scoring_stability_for_pool(window=10, version=...)
  — std(score) sur N derniers cycles par wallet, fallback Python
  statistics.stdev (SQLite n'a pas STDDEV natif)
- TraderScoringRow étendu : +stability_std, +stability_n
- Badge dispatch traders_scoring.html : 🟢 stable < 0.03, 🟡 volatile,
  🔴 unstable ≥ 0.08, ⏳ insufficient (n < window)
- Cutover status panel : SCORING_VERSION, SCORING_V2_SHADOW_DAYS, jours
  écoulés, next-milestone
- Top-10 side-by-side v2.1.1 vs v2.2 : conditionnel ship MF (feature flag)
- Cache 5 min côté query (D7)
- Tests : test_compute_scoring_stability + test_dashboard_stability_badge
- Cf. spec M19 §2.1 MH.8 + §4.8 D7
```

### MH.11 — N+1 fix get_home_alltime_stats

```text
perf(dashboard): M19 MH.11 single-query aggregation on get_home_alltime_stats

- queries.py:803-817 : remplace boucle N+1 par func.sum(realized_pnl) +
  func.count(id) en single query
- Préserve filtre simulated == (execution_mode != "live") M17 MD.1 strict
- Gain perf p50 attendu : ~600ms → ~50ms sur 500 positions fermées
- Tests : test_alltime_stats_single_query + test_filter_simulated_strict_preserved
- Audit M-008 fix
- Cf. spec M19 §2.1 MH.11
```

### MH.10 — Performance enrichi (fee_drag + wash_risk)

```text
feat(dashboard): M19 MH.10 /performance fee_drag column + wash_risk feature flag

- New helper compute_fee_drag_24h(simulated) : Σ fee_quote.rate × notional
  sur trades dernières 24h, par wallet
- Source : MyOrder.fee_rate (post-M16) ou FeeRateClient.get_fee_quote()
  (post-M18 ME.3) — PAS get_fee_rate deprecated alias
- PerformanceRow étendu : +fee_drag_24h_usd, +wash_score (feature flag)
- Template performance.html : colonne fee_drag active post-M19, wash_risk
  badge conditionnel SCORING_VERSION == "v2.2" (feature flag MF)
- Tests : test_performance_fee_drag_active_post_m18
- Cf. spec M19 §2.1 MH.10 + §4.10 D9
```

---

## 18. Critères d'acceptation

### 18.1 Critères fonctionnels (binaires GO/NO-GO)

- [ ] **F1** — `render_address` macro disponible dans `macros.html` et
  utilisée sur 11 vues (test grep).
- [ ] **F2** — Click sur copy button copie l'adresse fullhash dans le
  presse-papiers (test browser manuel).
- [ ] **F3** — `format_size_precise(0.0234) == "0.023"` (pas `"0.02"`).
- [ ] **F4** — `/home` `APPROVE STRATÉGIE` calculé sur 24h glissantes,
  label UI inclut "(24h)".
- [ ] **F5** — `/home` cards KPI affichent tooltips au hover (`<span title>`).
- [ ] **F6** — BUY NO position size 1 @ 0.60 contribue +0.60 à `Gain max
  latent` (pas +0.40).
- [ ] **F7** — `/home` `Win rate` subtext affiche `"1W/0L/5 break-even"`
  si breakeven présent.
- [ ] **F8** — `/home` card `Total USDC` affiche `$1,006.50` (pas `$1006`).
- [ ] **F9** — `/scoring` colonne stability avec badges 🟢/🟡/🔴/⏳.
- [ ] **F10** — `/scoring` ranks affichés sont locaux à l'intersection
  v1∩v2.
- [ ] **F11** — `/performance` colonne `Fee drag (24h)` rendue (post-M16/M18).
- [ ] **F12** — `/performance` colonne `Wash risk` absente si
  `SCORING_VERSION != "v2.2"`.

### 18.2 Critères tests

- [ ] **T1** — `pytest tests/unit/test_dashboard*.py -x --tb=short`
  retourne 0 failure (~19 tests M19 verts + 8 régressions).
- [ ] **T2** — `ruff check . && ruff format --check . && mypy src --strict`
  retourne 0 erreur.
- [ ] **T3** — `pytest tests/integration/test_dashboard_smoke.py
  -m integration -x --tb=short` retourne 0 failure (2 tests verts).
- [ ] **T4** — Tests régression `test_dashboard_security.py` +
  `test_dashboard_security_m6.py` verts inchangés.
- [ ] **T5** — Test régression C-005 `test_home_performance_converge_within_1cent`
  vert.
- [ ] **T6** — Test régression M17 MD.1 `test_alltime_stats_filter_simulated_strict_under_live_mode`
  vert.

### 18.3 Critères runtime smoke

- [ ] **R1** — Smoke runtime `EXECUTION_MODE=dry_run DASHBOARD_ENABLED=true
  python -m polycopy --verbose` boot < 10s sans ERROR.
- [ ] **R2** — `curl -sf http://127.0.0.1:8000/home` retourne 200 + HTML
  contient `copy-btn`, `info-icon`, `format_usd`.
- [ ] **R3** — `curl -sf http://127.0.0.1:8000/scoring` retourne 200 +
  HTML contient `stability` + badges.
- [ ] **R4** — `alembic upgrade head` retourne "no migrations to apply"
  (head reste 0010, pas de migration 0011).

### 18.4 Critères perf

- [ ] **P1** — `/home` p50 < 200ms sur 500 positions fermées (post-MH.11
  N+1 fix). Mesure via `curl -w "%{time_total}\n"` 10 itérations.
- [ ] **P2** — `/home` p95 < 400ms sur 500 positions fermées.

### 18.5 Critères invariants préservés (zéro régression)

- [ ] **I1** — Dashboard read-only strict M4.5 : grep automatisé sur
  `@router.post`/`put`/`delete` retourne 0 nouveau callsite.
- [ ] **I2** — CDN versions M6 inchangées : grep `cdn.tailwindcss.com/3.4.16`,
  `unpkg.com/htmx.org@2.0.4`, `cdn.jsdelivr.net/npm/chart.js@4.4.7`,
  `unpkg.com/lucide@0.469.0` présents.
- [ ] **I3** — Filtre `simulated` strict M17 MD.1 : tests régression verts.
- [ ] **I4** — Convergence /home ↔ /performance M17 MD.6 : test C-005 vert.
- [ ] **I5** — `FeeQuote` M18 ME.3 consommé (MH.10) : grep `get_fee_quote`
  côté queries.py retourne ≥ 1 ; grep `get_fee_rate` retourne 0 hors
  alias deprecated dans fee_rate_client.py.
- [ ] **I6** — Versioning sacré : `git diff --stat` sur
  `src/polycopy/discovery/scoring/` retourne 0 changement.
- [ ] **I7** — Schema DB intact : `git diff --stat` sur
  `alembic/versions/` retourne 0 nouveau fichier.

### 18.6 Critères doc

- [ ] **D1** — Spec `docs/specs/M19-dashboard-ux-polish.md` mergée sur main.
- [ ] **D2** — `docs/specs/ROADMAP.md` mis à jour : MH/M19 marqué shipped.
- [ ] **D3** — Pas de bloc CLAUDE.md M19 ajouté (UX polish — pas de
  conventions/sécurité nouvelle à documenter, sauf si refactor révèle
  un invariant non-documenté).

---

**Fin de la spec M19.**

Document actionnable seul — un implémenteur fresh qui lit M19.md doit
pouvoir merger les 11 commits sans revenir lire MA/MB/MC/MD/ME ou les
briefs MH/MK. Les décisions D1-D9 sont **figées** et reposent sur
inspection live du code dashboard 2026-04-27 + audit findings
2026-04-24 + user feedback direct.
