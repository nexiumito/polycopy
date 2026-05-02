# M21 — Scoring comparison page generic refactor

**Status** : Draft — 2026-05-02 soir
**Depends on** : M12 (Scoring v2 — page `/traders/scoring` initiale, hardcoded
v1/v2), M14 (Scoring v2.1-ROBUST — registry `SCORING_VERSIONS_REGISTRY` +
`Settings.scoring_version: Literal["v1", "v2.1", "v2.1.1"]`), M15
(Anti-toxic lifecycle — ajoute `v2.1.1` au registry), M19 (Dashboard UX
polish — MH.8 `compute_scoring_stability_for_pool()` consommé, MH.9 ranks
locaux intersection, `render_address` macro)
**Bloque** : décision cutover scoring v2.1 → v2.1.1 prévue 2026-05-29 J+30
(cf. [`docs/todo.md §9`](../todo.md#L574)) — la page actuelle est vide
pendant le test 30j en cours, audit cutover possible uniquement via SQL
manuel.
**Workflow git** : commits directement sur `main` (pas de branche, pas de
PR — règle projet).
**Charge estimée** : S (1-2 jours dev, éclatable en 6 commits indépendants).
**Numéro** : M21 (M20 réservé pour MK latency phase 1b, cf. ROADMAP.md).
**Brief origine** : aucun brief pré-existant dans `docs/next/` — la spec
dérive directement de [`docs/todo.md §16`](../todo.md#L934). Items prefixés
**MN** (next available letter post-MK).

---

## 0. TL;DR

M21 livre un **bundle refactor 6 items (MN.1 → MN.6)** qui rend la page
dashboard `/traders/scoring` **générique sur 2 versions arbitraires** (au
lieu du couple hardcoded `v1` / `v2` hérité de M12). Bloquant pour la
décision cutover v2.1 → v2.1.1 prévue J+30 (2026-05-29).

**6 items couplés** :

- **MN.1** — Nouveau helper
  [`dashboard/queries.py::detect_comparison_versions`](../../src/polycopy/dashboard/queries.py)
  qui détecte dynamiquement les 2 versions à comparer : `pilot =
  Settings.scoring_version` + `shadow = 2ᵉ scoring_version la plus
  fréquente dans trader_scores sur fenêtre 30j glissante`. Si shadow =
  pilot ou aucune 2ᵉ version → `shadow=None` → page rend en single-version
  mode (cf. **D3** §4.3).
- **MN.2** — Refactor 2 fonctions repository / queries
  [`list_scoring_comparison`](../../src/polycopy/dashboard/queries.py#L1415)
  et
  [`scoring_comparison_aggregates`](../../src/polycopy/dashboard/queries.py#L1529)
  pour accepter `pilot_version: str` + `shadow_version: str | None`
  paramètres explicites au lieu des littéraux `'v1'` / `'v2.1'` hardcodés.
  Aucune nouvelle migration Alembic — `trader_scores.scoring_version
  VARCHAR(16)` accepte déjà n'importe quelle string.
- **MN.3** — Route handler
  [`/traders/scoring`](../../src/polycopy/dashboard/routes.py#L193)
  appelle `detect_comparison_versions(sf)` au boot du request, passe
  `pilot_version` + `shadow_version` au template + ajuste l'appel
  `compute_scoring_stability_for_pool(version=pilot_version)` (cohérent
  M19 MH.8).
- **MN.4** — Template
  [`traders_scoring.html`](../../src/polycopy/dashboard/templates/traders_scoring.html)
  headers dynamiques (`Score {{ pilot_version | upper }}` / `Score {{
  shadow_version | upper }}`) + bloc "Cutover status" conditionnel
  (`{% if shadow_version %}`) + label cutover dynamique selon le couple
  pilot/shadow détecté + retire toutes les chaînes `'v1'` / `'v2'`
  hardcodées du markup.
- **MN.5** — Spearman et top-N delta génériques :
  [`_spearman_rank`](../../src/polycopy/dashboard/queries.py#L1605) reste
  pure (pas touché). Les ranks locaux intersection (M19 MH.9) sont déjà
  paramétrables — MN.5 confirme via tests régression que les fonctions
  consomment `pilot_version` / `shadow_version` arbitraires correctement.
  Top-10 delta calculé sur les 2 versions détectées dynamiquement.
- **MN.6** — Tests dashboard adaptés + 4 nouveaux dans
  [`tests/unit/test_dashboard_scoring_route.py`](../../tests/unit/test_dashboard_scoring_route.py)
  : `test_detect_comparison_versions_returns_pilot_only_when_db_empty`,
  `test_detect_comparison_versions_picks_second_most_frequent`,
  `test_dashboard_scoring_render_with_v2_1_only_pilot`,
  `test_dashboard_scoring_v2_1_vs_v2_1_1_shadow`. Tests M12 existants
  adaptés (passage de versions explicites). Régression M19 MH.8 + MH.9
  préservée (stability column + ranks locaux).

Diff strictement additif sur les invariants critiques :

- **Dashboard read-only strict** (M4.5 invariant) : intact. M21 ajoute 0
  POST/PUT/DELETE — le handler `/traders/scoring` reste `@router.get`.
- **CDN versions pinned** (M6) : Tailwind 3.4.16, HTMX 2.0.4, Chart.js
  4.4.7, Lucide 0.469.0 — **inchangées**. Aucune nouvelle dep CDN, aucun
  build step.
- **Grep security anti-leak** (M4.5/M6) : `test_dashboard_security.py` +
  `test_dashboard_security_m6.py` continuent à passer (zéro fuite
  `POLYMARKET_PRIVATE_KEY`, `TELEGRAM_BOT_TOKEN`, `CLOB_API_SECRET`,
  `REMOTE_CONTROL_TOTP_SECRET`). Wallet addresses exposées par
  `render_address` sont publiques on-chain (cohérent invariant M19 MH.1).
- **Filtre `simulated` strict M17 MD.1** : non touché par M21 — la page
  scoring n'utilise pas `MyPosition`. Mention explicite dans §8.
- **Convergence /home ↔ /performance M17 MD.6** : non concernée — la
  page scoring n'affiche ni PnL ni positions.
- **Versioning sacré** : M21 ne touche aucune fonction `compute_score_*`
  (v1, v2.1, v2.1.1). M21 est read-only sur `trader_scores` append-only.
  Registry `SCORING_VERSIONS_REGISTRY` intact.
- **Append-only DB** : aucune migration Alembic 0011, aucune row
  réécrite. `alembic upgrade head` retourne "no migrations to apply"
  post-M21. Head reste **0010**.
- **`localStorage` discipline M9/M10** : aucune nouvelle clé
  `polycopy.*` ajoutée.

Tests cumulés estimés : **~6 tests unit nouveaux** (MN.1=2, MN.2=1,
MN.4=1, MN.6=2) + **8 tests M12 existants adaptés** (passent les
versions explicites en paramètres) + **régressions M19 MH.8 + MH.9
préservées** (stability column + Spearman intersection).

Charge cumulée : **1-2 jours dev**, possible en 6 commits atomiques. La
parallélisation est naturelle car MN.1 (helper) + MN.2 (queries refactor)
sont indépendants ; MN.3 (route) consomme les 2 ; MN.4 (template) +
MN.5 (Spearman regression) + MN.6 (tests) consomment MN.3.

---

## 1. Motivation & use case concret

### 1.1 Friction observée 2026-05-02 J+3 du test 30j scoring v2.1

Pendant le test 30j scoring v2.1 démarré 2026-04-29 (cf.
[`docs/todo.md §0bis`](../todo.md#L10)), l'utilisateur ouvre le dashboard
sur la machine prod (`http://uni-debian.<tailnet>.ts.net:8787/`) et
navigue vers `/traders/scoring` pour vérifier la progression de la
shadow period v2.1.1.

**Constat** : la page est **totalement vide** :

- KPI "Wallets compared (v1 ∩ v2)" affiche `0`.
- KPI "Spearman rank(v1, v2)" affiche `—`.
- Bloc "Cutover status" affiche "Shadow period not started (aucune row
  `trader_scores` avec `scoring_version='v2'`)".
- Le tableau des wallets affiche "Aucun score v1 ou v2 persisté. Lance
  un cycle discovery pour voir apparaître les rows."

**Cause racine** : la page a été conçue M12 (cf.
[`docs/specs/M12-scoring-v2.md`](M12-scoring-v2.md) §5.5) pour comparer
**`v1` (pilote) vs `v2` (shadow)** — les chaînes `'v1'` et `'v2'` (puis
`'v2.1'` pour M14, par patch) sont **hardcodées** dans :

- Les colonnes du template
  [`traders_scoring.html:83-84`](../../src/polycopy/dashboard/templates/traders_scoring.html#L83)
  : `Score v1` / `Score v2` / `Rank v1` / `Rank v2`.
- Les filtres SQL côté repository
  [`queries.py:1435,1446,1455,1466,1582`](../../src/polycopy/dashboard/queries.py#L1435)
  : `WHERE scoring_version == 'v1'` / `'v2.1'`.
- Les KPI "Pilot version" affichant `Settings.scoring_version` upper —
  fonctionne mais labels colonnes ne suivent pas (rupture cohérente
  visuelle).
- Le bloc "Cutover status" qui lit `SCORING_V2_CUTOVER_READY` (flag M12
  **plus pertinent** post-M14 où la décision cutover est manuelle après
  validation H-EMP via `scripts/validate_ma_hypotheses.py`).

Avec `SCORING_VERSION=v2.1` (état de prod actuel) :

- Discovery écrit uniquement des rows `scoring_version='v2.1'` (cf.
  [`orchestrator.py:444-460`](../../src/polycopy/discovery/orchestrator.py#L444)
  — fix Bug #2 + #3 du 2026-05-02 skip v1 path quand pilote v2.1).
- Aucune row `trader_scores` n'existe avec `scoring_version IN ('v1',
  'v2')`.
- → page vide (les filtres SQL hardcoded ne matchent rien).

**Workaround actuel** (cf. [`docs/todo.md §0bis`](../todo.md#L130)) :
SQL manuel sur la DB prod via SSH, parsing visuel des scores. Pénible
pour la **décision cutover v2.1 → v2.1.1** prévue à J+30 (2026-05-29) qui
nécessite une comparaison side-by-side des 2 versions sur les 14 jours
de shadow precedent (cf. [`docs/todo.md §9`](../todo.md#L574)).

### 1.2 Findings audit 2026-04-24 référencés

L'audit code 2026-04-24 et la session deep-search post-audit n'ont pas
identifié spécifiquement ce point — c'est une **dette technique
émergeante** depuis M14 (`v2` → `v2.1` rename) puis M15 (`v2.1.1`
ajouté au registry). M19 MH.8 + MH.9 ont enrichi la page (stability
metric + ranks locaux intersection) mais ont préservé la signature
hardcoded v1/v2 des fonctions sous-jacentes (cf.
[`docs/specs/M19-dashboard-ux-polish.md`](M19-dashboard-ux-polish.md)
§2.1 MH.8 + MH.9 — strictement additif, aucun refactor de la signature).

### 1.3 Vue de haut des changements

| Module | Diff | Référence MN |
|---|---|---|
| [src/polycopy/dashboard/queries.py](../../src/polycopy/dashboard/queries.py) | nouveau helper `detect_comparison_versions(session_factory) -> tuple[str, str \| None]` (~30 LOC, fenêtre 30j) | MN.1 |
| [src/polycopy/dashboard/queries.py:1415-1526](../../src/polycopy/dashboard/queries.py#L1415-L1526) | `list_scoring_comparison` accepte `pilot_version: str`, `shadow_version: str \| None` paramètres ; les littéraux `'v1'` / `'v2.1'` retirés | MN.2 |
| [src/polycopy/dashboard/queries.py:1529-1602](../../src/polycopy/dashboard/queries.py#L1529-L1602) | `scoring_comparison_aggregates` idem ; le `shadow_days_elapsed` lit `MIN(cycle_at)` filtré par `shadow_version` (au lieu de `'v2.1'` hardcoded) | MN.2 |
| [src/polycopy/dashboard/routes.py:193-230](../../src/polycopy/dashboard/routes.py#L193-L230) | route `/traders/scoring` appelle `detect_comparison_versions` ; passe `pilot_version` + `shadow_version` au template ; ajuste `compute_scoring_stability_for_pool(version=pilot_version)` | MN.3 |
| [src/polycopy/dashboard/templates/traders_scoring.html](../../src/polycopy/dashboard/templates/traders_scoring.html) | headers dynamiques + bloc cutover conditionnel + retrait des chaînes `v1`/`v2` hardcodées + label dynamique pilote/shadow | MN.4 |
| [src/polycopy/dashboard/templates/base.html:152,182](../../src/polycopy/dashboard/templates/base.html#L152) | sidebar label `Scoring v1/v2` → `Scoring comparison` (générique) | MN.4 |
| [src/polycopy/dashboard/queries.py:1363-1394](../../src/polycopy/dashboard/queries.py#L1363-L1394) | `ScoringComparisonRow` étendu avec alias génériques `score_pilot` / `score_shadow` / `rank_pilot_local` / `rank_shadow_local` (les anciens `_v1` / `_v2` deviennent obsolète mais préservés 1 release pour rétrocompat tests) | MN.2 |
| [tests/unit/test_dashboard_scoring_route.py](../../tests/unit/test_dashboard_scoring_route.py) | 4 nouveaux tests + 6 tests M12 existants adaptés | MN.6 |

---

## 2. Scope / non-goals

### 2.1 Dans le scope (MN.1 → MN.6)

#### MN.1 — Helper `detect_comparison_versions`

- Nouveau helper dans
  [src/polycopy/dashboard/queries.py](../../src/polycopy/dashboard/queries.py)
  (cf. **D1** §4.1 — `queries.py` cohérent avec le reste des helpers
  scoring) :
  ```python
  async def detect_comparison_versions(
      session_factory: async_sessionmaker[AsyncSession],
      *,
      settings: Settings,
      window_days: int = 30,
  ) -> tuple[str, str | None]:
      """M21 MN.1 — Détection dynamique des 2 versions à comparer.

      Retourne ``(pilot_version, shadow_version)`` :

      - ``pilot_version`` = ``settings.scoring_version`` (source de vérité
        config — la version qui pilote ``DecisionEngine``).
      - ``shadow_version`` = la 2ᵉ ``scoring_version`` la plus fréquente
        dans ``trader_scores`` sur les ``window_days`` derniers jours,
        EXCLUSION faite de ``pilot_version``.
      - ``shadow_version=None`` si :
        - aucune row ``trader_scores`` (DB neuve) ;
        - pilot est la seule version présente (pas de shadow calculé) ;
        - 2ᵉ version trouvée == pilot (cas dégénéré logique).

      Si ``shadow_version=None`` → page rend en **single-version mode**
      (template branche `{% if shadow_version %}` qui masque les
      colonnes shadow + le bloc cutover).

      Cf. spec §4.2 (D2 — fenêtre 30j) + §4.3 (D3 — fallback None silent).
      """
      cutoff = datetime.now(tz=UTC) - timedelta(days=window_days)
      async with session_factory() as session:
          stmt = (
              select(TraderScore.scoring_version, func.count().label("n"))
              .where(TraderScore.cycle_at >= cutoff)
              .where(TraderScore.scoring_version != settings.scoring_version)
              .group_by(TraderScore.scoring_version)
              .order_by(func.count().desc())
              .limit(1)
          )
          result = (await session.execute(stmt)).first()
      shadow_version = str(result.scoring_version) if result is not None else None
      return settings.scoring_version, shadow_version
  ```
- **Aucun cache nécessaire** : 1 query SQL aggregée O(n) sur
  `trader_scores` filtré par cycle_at + version. Index existant
  `ix_trader_scores_cycle_at` couvre la fenêtre. p50 attendu < 5ms sur
  10k rows.
- **Sources** : todo §16 lignes 938-941 (5 actions principales — cette
  action est la 1ère prérequise).

#### MN.2 — Repository / queries paramétrés

- Refactor des 2 fonctions principales pour accepter `pilot_version` +
  `shadow_version` paramètres explicites :

  [`list_scoring_comparison`](../../src/polycopy/dashboard/queries.py#L1415)
  signature avant :
  ```python
  async def list_scoring_comparison(
      session_factory,
      *,
      limit: int = 100,
  ) -> list[ScoringComparisonRow]:
  ```
  signature après :
  ```python
  async def list_scoring_comparison(
      session_factory,
      *,
      pilot_version: str,
      shadow_version: str | None,
      limit: int = 100,
  ) -> list[ScoringComparisonRow]:
  ```
  Diff interne : remplacer `WHERE scoring_version == 'v1'` →
  `WHERE scoring_version == pilot_version` (parameterized SQLAlchemy),
  pareil pour `'v2.1'` → `shadow_version`. Si `shadow_version is None`,
  short-circuit le bloc shadow (pas de subquery `latest_v2_subq`,
  `v2_by_wallet={}`, ranks shadow locaux vides).

  [`scoring_comparison_aggregates`](../../src/polycopy/dashboard/queries.py#L1529)
  idem : accepte `pilot_version` + `shadow_version` ; le
  `shadow_days_elapsed` lit `MIN(cycle_at)` filtré par
  `shadow_version` au lieu de hardcoded `'v2.1'`. Si `shadow_version is
  None`, retourne `wallets_compared=0`, `spearman_rank=None`,
  `top10_delta=0`, `shadow_days_elapsed=None`.

- **DTO `ScoringComparisonRow` étendu** (cf. table §1.4) avec alias
  génériques :
  ```python
  @dataclass(frozen=True)
  class ScoringComparisonRow:
      ...  # existing fields preserved
      # Generic aliases (same values as score_v1/score_v2 — convention :
      # _pilot = pilot_version score, _shadow = shadow_version score).
      # Aux templates de consommer les nouveaux noms idéalement.
      score_pilot: float | None = None
      score_shadow: float | None = None
      rank_pilot_pool: int | None = None
      rank_shadow_pool: int | None = None
      rank_pilot_local: int | None = None
      rank_shadow_local: int | None = None
  ```
  Les anciens `score_v1` / `score_v2` / `rank_v1*` / `rank_v2*` restent
  populés (rétrocompat 1 release) avec les mêmes valeurs que les alias
  pilot/shadow. Le template MN.4 consomme uniquement les nouveaux
  noms ; les tests M12 existants adaptés consomment progressivement les
  nouveaux noms.

- **Aucune nouvelle migration Alembic** : `trader_scores.scoring_version
  VARCHAR(16)` accepte déjà n'importe quelle string. Confirmé par
  `sqlite3 polycopy.db ".schema trader_scores"` → `scoring_version
  VARCHAR(16)`.

#### MN.3 — Route handler `/traders/scoring`

- Mise à jour
  [`routes.py:193-230`](../../src/polycopy/dashboard/routes.py#L193-L230)
  :
  ```python
  @router.get("/traders/scoring", response_class=HTMLResponse)
  async def traders_scoring_page(
      request: Request,
      sf: SFDep,
      settings: STDep,
  ) -> HTMLResponse:
      """Onglet M12 + M21 — comparaison pilot vs shadow generic.

      M21 MN.3 : versions détectées dynamiquement via
      `detect_comparison_versions(sf, settings=settings)`. Page rend en
      single-version mode si `shadow_version is None`.
      """
      pilot_version, shadow_version = await queries.detect_comparison_versions(
          sf,
          settings=settings,
      )
      rows = await queries.list_scoring_comparison(
          sf,
          pilot_version=pilot_version,
          shadow_version=shadow_version,
          limit=200,
      )
      aggregates = await queries.scoring_comparison_aggregates(
          sf,
          pilot_version=pilot_version,
          shadow_version=shadow_version,
          shadow_days=settings.scoring_v2_shadow_days,
          cutover_ready=settings.scoring_v2_cutover_ready,
      )
      stability = await queries.compute_scoring_stability_for_pool(
          sf,
          window=10,
          version=pilot_version,
      )
      return _render(
          request,
          "traders_scoring.html",
          {
              "rows": rows,
              "aggregates": aggregates,
              "pilot_version": pilot_version,
              "shadow_version": shadow_version,
              "shadow_days_config": settings.scoring_v2_shadow_days,
              "stability": stability,
              "stability_window": 10,
          },
      )
  ```
- **Aucun nouvel import** dans le handler — tout passe par `queries`
  module exposé.
- **`compute_scoring_stability_for_pool(version=pilot_version)`** : la
  stability metric M19 MH.8 reste calculée sur la version pilote
  (cohérent — la stabilité est une propriété de la formule pilote, pas
  de la shadow).

#### MN.4 — Templates dynamiques

- Refactor
  [`traders_scoring.html`](../../src/polycopy/dashboard/templates/traders_scoring.html)
  :

  **§ heading** (l. 4) — passer de hardcoded à dynamique :
  ```jinja
  {% block heading %}Scoring comparison — {{ pilot_version | upper }}{% if shadow_version %} | {{ shadow_version | upper }} | delta_rank{% endif %} (M12+M21){% endblock %}
  ```

  **§ description** (l. 7-13) — texte dynamique :
  ```jinja
  <p class="text-sm mb-4" style="color: var(--color-muted);">
    Comparaison côte-à-côte de la formule <strong>{{ pilot_version }}</strong> (pilote)
    {% if shadow_version %}
    et de la formule <strong>{{ shadow_version }}</strong> (shadow). Tant que
    la version pilote (<code>SCORING_VERSION</code>) reste <strong>{{ pilot_version }}</strong>,
    {{ shadow_version }} n'influence aucune décision (<code>DecisionEngine</code>) — elle
    est uniquement calculée en parallèle pour comparaison empirique et audit.
    {% else %}
    seule. Aucune autre version n'est calculée en parallèle (single-version mode).
    {% endif %}
  </p>
  ```

  **§ KPI cards** (l. 15-34) — labels dynamiques :
  ```jinja
  <section class="grid grid-cols-1 md:grid-cols-4 gap-3 mb-4">
    <div class="kpi-card">
      <div class="kpi-label">Pilot version</div>
      <div class="kpi-value">{{ pilot_version | upper }}</div>
    </div>
    {% if shadow_version %}
    <div class="kpi-card">
      <div class="kpi-label">Wallets compared ({{ pilot_version }} ∩ {{ shadow_version }})</div>
      <div class="kpi-value">{{ aggregates.wallets_compared }}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Spearman rank({{ pilot_version }}, {{ shadow_version }})</div>
      <div class="kpi-value">
        {% if aggregates.spearman_rank is none %}—{% else %}{{ '%.3f' | format(aggregates.spearman_rank) }}{% endif %}
      </div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Top-10 delta</div>
      <div class="kpi-value">{{ aggregates.top10_delta }}</div>
    </div>
    {% endif %}
  </section>
  ```

  **§ Cutover status** (l. 36-74) — bloc entièrement enveloppé dans
  `{% if shadow_version %}` :
  ```jinja
  {% if shadow_version %}
  <section class="mb-6 p-4 rounded" style="background: var(--color-panel); border: 1px solid var(--color-border);">
    <h3 class="font-semibold mb-2">
      {% if pilot_version == "v2.1" and shadow_version == "v2.1.1" %}
        Préparation cutover {{ shadow_version }}
      {% elif pilot_version == "v1" and shadow_version == "v2.1" %}
        Préparation cutover v2.1 (legacy M12 path)
      {% else %}
        Comparaison shadow ({{ pilot_version }} vs {{ shadow_version }})
      {% endif %}
    </h3>
    <div class="text-sm" style="color: var(--color-muted);">
      {% if aggregates.shadow_days_elapsed is none %}
        <p>Shadow period not started (aucune row <code>trader_scores</code> avec <code>scoring_version='{{ shadow_version }}'</code>).</p>
      {% else %}
        <p>
          Days elapsed: <strong>{{ aggregates.shadow_days_elapsed }}</strong> /
          {{ shadow_days_config }}
          {% if aggregates.shadow_days_remaining > 0 %}
            · Remaining: <strong>{{ aggregates.shadow_days_remaining }}</strong> days
          {% else %}
            · <strong style="color: var(--color-accent-9);">Shadow period elapsed ✓</strong>
          {% endif %}
        </p>
      {% endif %}
      <p class="mt-2">
        Cutover ready flag: <strong>{{ aggregates.cutover_ready }}</strong>
        {% if not aggregates.cutover_ready %}
          <br>
          <span style="color: var(--color-muted);">
            Set <code>SCORING_V2_CUTOVER_READY=true</code> in <code>.env</code>
            after backtest validation (<code>brier_{{ shadow_version }} &lt; brier_{{ pilot_version }} - 0.01</code>).
          </span>
        {% endif %}
      </p>
      <p class="mt-3">
        {% if aggregates.cutover_ready and (aggregates.shadow_days_remaining == 0 or aggregates.shadow_days_remaining is none) %}
          <strong>Ready to flip to {{ shadow_version }}.</strong> Run manually (dashboard is read-only):
          <pre class="mt-1 p-2 rounded text-xs" style="background: var(--color-bg); border: 1px solid var(--color-border);"># .env
SCORING_VERSION={{ shadow_version }}
# Restart the bot to apply
python -m polycopy --verbose</pre>
        {% else %}
          Flip {{ shadow_version }} not yet available — complete shadow period + backtest first.
        {% endif %}
      </p>
    </div>
  </section>
  {% endif %}
  ```

  **§ table headers** (l. 76-107) — colonnes dynamiques :
  ```jinja
  <table class="w-full text-sm">
    <thead>
      <tr style="border-bottom: 1px solid var(--color-border);">
        <th class="text-left py-2 px-2">Wallet</th>
        <th class="text-left py-2 px-2">Label</th>
        <th class="text-left py-2 px-2">Status</th>
        <th class="text-right py-2 px-2">Score {{ pilot_version | upper }}</th>
        {% if shadow_version %}
        <th class="text-right py-2 px-2">Score {{ shadow_version | upper }}</th>
        <th class="text-right py-2 px-2">
          <span class="inline-flex items-center">Rank {{ pilot_version | upper }}
            <span class="info-icon" title="Rang sur l'intersection {{ pilot_version }}∩{{ shadow_version }} (N={{ aggregates.wallets_compared }}) — cohérent avec le calcul de Spearman ρ. Pas le rang pool-entier (M19 MH.9).">
              <i data-lucide="info" class="w-3 h-3"></i>
            </span>
          </span>
        </th>
        <th class="text-right py-2 px-2">
          <span class="inline-flex items-center">Rank {{ shadow_version | upper }}
            <span class="info-icon" title="Rang sur l'intersection {{ pilot_version }}∩{{ shadow_version }} (N={{ aggregates.wallets_compared }}) — cohérent avec le calcul de Spearman ρ.">
              <i data-lucide="info" class="w-3 h-3"></i>
            </span>
          </span>
        </th>
        <th class="text-right py-2 px-2">Δ rank</th>
        {% endif %}
        <th class="text-left py-2 px-2">
          <span class="inline-flex items-center">Stability
            <span class="info-icon" title="std(score) sur les {{ stability_window }} derniers cycles {{ pilot_version }}. 🟢 stable < 0.03, 🟡 volatile, 🔴 unstable ≥ 0.08, ⏳ insufficient (n < {{ stability_window }}).">
              <i data-lucide="info" class="w-3 h-3"></i>
            </span>
          </span>
        </th>
      </tr>
    </thead>
  ```

  **§ table body** (l. 110-167) — accède aux nouveaux noms `score_pilot`
  / `rank_pilot_local` / etc. :
  ```jinja
  {% for row in rows %}
  <tr style="border-bottom: 1px solid var(--color-border-subtle, var(--color-border));">
    <td class="py-1 px-2 text-xs">{{ render_address(row.wallet_address, kind="wallet") }}</td>
    <td class="py-1 px-2">{{ row.label or "—" }}</td>
    <td class="py-1 px-2">
      {% if row.pinned %}<span class="status-pinned">pinned</span>
      {% else %}<span class="status-{{ row.status }}">{{ row.status }}</span>{% endif %}
    </td>
    <td class="py-1 px-2 text-right">
      {% if row.score_pilot is none %}—{% else %}{{ '%.3f' | format(row.score_pilot) }}{% endif %}
    </td>
    {% if shadow_version %}
    <td class="py-1 px-2 text-right">
      {% if row.score_shadow is none %}—{% else %}{{ '%.3f' | format(row.score_shadow) }}{% endif %}
    </td>
    <td class="py-1 px-2 text-right">
      {% if row.rank_pilot_local is not none %}
        {{ row.rank_pilot_local }}
        {% if row.rank_pilot_pool is not none and row.rank_pilot_pool != row.rank_pilot_local %}
          <span class="text-xs" style="color: var(--color-muted);" title="pool: {{ row.rank_pilot_pool }}">✦</span>
        {% endif %}
      {% elif row.rank_pilot_pool is not none %}
        <span title="hors intersection — rang pool affiché">{{ row.rank_pilot_pool }}*</span>
      {% else %}—{% endif %}
    </td>
    <td class="py-1 px-2 text-right">
      {% if row.rank_shadow_local is not none %}
        {{ row.rank_shadow_local }}
        {% if row.rank_shadow_pool is not none and row.rank_shadow_pool != row.rank_shadow_local %}
          <span class="text-xs" style="color: var(--color-muted);" title="pool: {{ row.rank_shadow_pool }}">✦</span>
        {% endif %}
      {% elif row.rank_shadow_pool is not none %}
        <span title="hors intersection — rang pool affiché">{{ row.rank_shadow_pool }}*</span>
      {% else %}—{% endif %}
    </td>
    <td class="py-1 px-2 text-right">
      {% if row.delta_rank is none %}—
      {% elif row.delta_rank > 0 %}<span style="color: var(--color-accent-9);">+{{ row.delta_rank }}</span>
      {% elif row.delta_rank < 0 %}<span style="color: var(--color-danger);">{{ row.delta_rank }}</span>
      {% else %}0{% endif %}
    </td>
    {% endif %}
    <td class="py-1 px-2">
      {# M19 MH.8 — badge stability dispatch (std sur N derniers cycles {{ pilot_version }}). #}
      {% set st = stability.get(row.wallet_address, (none, 0)) %}
      {% set _std = st[0] %}
      {% set _n = st[1] %}
      {% if _n < stability_window %}
        <span title="n={{ _n }}/{{ stability_window }} cycles">⏳ insufficient</span>
      {% elif _std is none %}
        <span>⏳ —</span>
      {% elif _std < 0.03 %}
        <span title="std={{ '%.4f' | format(_std) }}">🟢 stable</span>
      {% elif _std < 0.08 %}
        <span title="std={{ '%.4f' | format(_std) }}">🟡 volatile</span>
      {% else %}
        <span title="std={{ '%.4f' | format(_std) }}">🔴 unstable</span>
      {% endif %}
    </td>
  </tr>
  {% endfor %}
  {% if not rows %}
  <tr>
    <td colspan="{% if shadow_version %}9{% else %}5{% endif %}" class="text-center py-6" style="color: var(--color-muted);">
      Aucun score {{ pilot_version }}{% if shadow_version %} ou {{ shadow_version }}{% endif %} persisté. Lance un cycle discovery pour voir apparaître les rows.
    </td>
  </tr>
  {% endif %}
  ```

- **Sidebar** [`base.html:152,182`](../../src/polycopy/dashboard/templates/base.html#L152)
  : label `Scoring v1/v2` → `Scoring comparison` (générique). Même
  fichier modifié 2× (desktop + mobile sidebar).

#### MN.5 — Spearman et top-N delta génériques

- Le calcul Spearman (M19 MH.9 fix) est déjà **paramétrique** — il
  consomme `with_both` qui contient les wallets ayant les 2 versions.
  Pas de touche à `_spearman_rank` (pure function reste pure).
- Top-10 delta calcul existant fonctionne déjà sur les ranks pool-wide
  des 2 versions. La généralisation MN.2 propage automatiquement.
- **Garde-fou** : test régression `test_spearman_uses_intersection_ranks_not_pool_ranks`
  M19 MH.9 (cf.
  [`tests/unit/test_dashboard_scoring_route.py:228-308`](../../tests/unit/test_dashboard_scoring_route.py#L228-L308))
  doit continuer à passer après MN.2 refactor — confirme que la
  généralisation ne casse pas la sémantique Spearman.

#### MN.6 — Tests dashboard adaptés + nouveaux

**Nouveaux tests** dans
[`tests/unit/test_dashboard_scoring_route.py`](../../tests/unit/test_dashboard_scoring_route.py)
(4 tests) :

```python
@pytest.mark.asyncio
async def test_detect_comparison_versions_returns_pilot_only_when_db_empty(
    session_factory: Any,
) -> None:
    """DB vide → shadow_version=None (single-version mode)."""
    pilot, shadow = await dashboard_queries.detect_comparison_versions(
        session_factory,
        settings=_settings(scoring_version="v2.1"),
    )
    assert pilot == "v2.1"
    assert shadow is None


@pytest.mark.asyncio
async def test_detect_comparison_versions_picks_second_most_frequent(
    session_factory: Any,
    target_trader_repo: TargetTraderRepository,
    trader_score_repo: TraderScoreRepository,
) -> None:
    """Pool avec v2.1 (pilot, 5 rows) + v2.1.1 (3 rows) + v1 (1 row).
    Détecte shadow=v2.1.1 (2ᵉ plus fréquent, exclu pilot)."""
    for i in range(5):
        t = await target_trader_repo.insert_shadow(f"0xa{i:03d}")
        await trader_score_repo.insert(
            TraderScoreDTO(
                target_trader_id=t.id, wallet_address=f"0xa{i:03d}",
                score=0.5, scoring_version="v2.1",
                low_confidence=False, metrics_snapshot={},
            ),
        )
    for i in range(3):
        t = await target_trader_repo.insert_shadow(f"0xb{i:03d}")
        await trader_score_repo.insert(
            TraderScoreDTO(
                target_trader_id=t.id, wallet_address=f"0xb{i:03d}",
                score=0.6, scoring_version="v2.1.1",
                low_confidence=False, metrics_snapshot={},
            ),
        )
    t = await target_trader_repo.insert_shadow("0xc000")
    await trader_score_repo.insert(
        TraderScoreDTO(
            target_trader_id=t.id, wallet_address="0xc000",
            score=0.7, scoring_version="v1",
            low_confidence=False, metrics_snapshot={},
        ),
    )

    pilot, shadow = await dashboard_queries.detect_comparison_versions(
        session_factory,
        settings=_settings(scoring_version="v2.1"),
    )
    assert pilot == "v2.1"
    assert shadow == "v2.1.1"  # 3 rows > 1 row v1


@pytest.mark.asyncio
async def test_dashboard_scoring_render_with_v2_1_only_pilot(
    session_factory: Any,
    target_trader_repo: TargetTraderRepository,
    trader_score_repo: TraderScoreRepository,
) -> None:
    """Pool avec uniquement v2.1 (cas test 30j actuel) → page rend
    en single-version mode, headers dynamiques, bloc cutover masqué."""
    t = await target_trader_repo.insert_shadow("0xaaa")
    await trader_score_repo.insert(
        TraderScoreDTO(
            target_trader_id=t.id, wallet_address="0xaaa",
            score=0.65, scoring_version="v2.1",
            low_confidence=False, metrics_snapshot={},
        ),
    )
    app = build_app(session_factory, _settings(scoring_version="v2.1"))
    with TestClient(app) as client:
        resp = client.get("/traders/scoring")
    assert resp.status_code == 200
    # Header dynamique pilote uniquement (pas de shadow).
    assert "Score V2.1" in resp.text
    assert "Score V2" not in resp.text  # ne contient pas le hardcoded legacy
    # Bloc cutover masqué (pas de "Cutover status" car shadow=None).
    assert "Cutover status" not in resp.text
    # Wallet rendu.
    assert "0xaaa" in resp.text
    assert "0.650" in resp.text


@pytest.mark.asyncio
async def test_dashboard_scoring_v2_1_vs_v2_1_1_shadow(
    session_factory: Any,
    target_trader_repo: TargetTraderRepository,
    trader_score_repo: TraderScoreRepository,
) -> None:
    """Pool avec v2.1 (pilot) + v2.1.1 (shadow) → page rend la
    comparaison avec headers dynamiques + label cutover dédié."""
    for wallet, s_pilot, s_shadow in [
        ("0xaaa", 0.9, 0.85),
        ("0xbbb", 0.6, 0.65),
        ("0xccc", 0.4, 0.45),
    ]:
        t = await target_trader_repo.insert_shadow(wallet)
        await trader_score_repo.insert(
            TraderScoreDTO(
                target_trader_id=t.id, wallet_address=wallet,
                score=s_pilot, scoring_version="v2.1",
                low_confidence=False, metrics_snapshot={},
            ),
        )
        await trader_score_repo.insert(
            TraderScoreDTO(
                target_trader_id=t.id, wallet_address=wallet,
                score=s_shadow, scoring_version="v2.1.1",
                low_confidence=False, metrics_snapshot={},
            ),
        )
    app = build_app(session_factory, _settings(scoring_version="v2.1"))
    with TestClient(app) as client:
        resp = client.get("/traders/scoring")
    assert resp.status_code == 200
    # Headers dynamiques.
    assert "Score V2.1" in resp.text
    assert "Score V2.1.1" in resp.text
    # Bloc cutover dédié.
    assert "Préparation cutover v2.1.1" in resp.text
    # Wallets affichés.
    assert "0xaaa" in resp.text
    assert "0xbbb" in resp.text
    assert "0xccc" in resp.text
```

**Tests M12 existants à adapter** :

- `test_traders_scoring_page_renders_v1_only_rows` (l. 47-72) : reste
  fonctionnel mais passe `scoring_version="v1"` au Settings pour que
  pilot=v1. L'assertion sur `"M12"` devient `"M12+M21"` (heading mis
  à jour). L'assertion sur `"0.650"` reste valide.
- `test_scoring_comparison_query_with_v1_and_v2` (l. 75-117) : passer
  versions explicites :
  ```python
  rows = await dashboard_queries.list_scoring_comparison(
      session_factory,
      pilot_version="v1",
      shadow_version="v2.1",
      limit=10,
  )
  ```
  Assertions sur `rank_v1` / `rank_v2` deviennent `rank_pilot_pool` /
  `rank_shadow_pool` (utilisation des nouveaux noms).
- `test_scoring_comparison_aggregates_spearman_computed` (l. 120-161)
  + `_aggregates_none_spearman_below_3` (l. 165-189) +
  `_intersection_ranks_not_pool_ranks` (l. 228-308) +
  `_shadow_days_elapsed_calculated_from_first_v2_row` (l. 311-350) :
  passer `pilot_version="v1"`, `shadow_version="v2.1"` explicitement.
- `test_cutover_ready_flag_passed_through_from_settings` (l. 193-202) :
  fournir `_settings(scoring_version="v1", scoring_v2_cutover_ready=True)`
  + seed 1 row v2.1 pour activer le bloc cutover (sans seed shadow,
  bloc masqué donc l'assertion `"Cutover ready flag"` échouerait).
- `test_traders_scoring_page_renders_empty_when_no_scores` (l. 31-43) :
  l'assertion `"Aucun score v1 ou v2 persisté"` devient `"Aucun score
  {{ pilot_version }}"` — adapter à `f"Aucun score {settings.scoring_version}"`
  selon le pilot. Sans shadow, le message dit "Aucun score V1" (ou
  pilot detected). Cf. template MN.4 ci-dessus pour le wording exact.

**Régressions à préserver strict** :

- `test_spearman_rank_function_edge_cases` (l. 218-224) : pure
  function, intact.
- `test_sidebar_link_present_in_base_template` (l. 205-215) :
  l'assertion `"Scoring v1/v2"` devient `"Scoring comparison"` (nouveau
  label sidebar MN.4).
- M19 MH.8 stability column tests (`test_dashboard_m19.py`) intacts.
- M19 MH.9 ranks locaux test (`_intersection_ranks_not_pool_ranks`)
  intact post-refactor.

### 2.2 Hors scope explicite

- **Pas de page "scoring history graph"** : la stability metric M19
  MH.8 (badge std) suffit pour la v1. Un graphe Chart.js des scores
  cycle-by-cycle ferait l'objet d'un module dédié futur.
- **Pas d'export CSV** : la copie d'adresses wallet (M19 MH.1) +
  inspection visuelle + SQL CLI sont les workarounds suffisants.
- **Pas de filtres temporels avancés** : la fenêtre 30j de
  `detect_comparison_versions` est figée par **D2** §4.2. Pas de
  query string `?since=...` v1.
- **Pas de touche au scoring engine** :
  [src/polycopy/discovery/scoring/](../../src/polycopy/discovery/scoring/)
  n'est pas modifié. Versioning sacré préservé (cf. CLAUDE.md
  §Sécurité).
- **Pas de nouvelle migration Alembic** : `trader_scores.scoring_version`
  est déjà `VARCHAR(16)`, n'importe quelle string acceptée.
- **Pas de création de brief `docs/next/MN.md`** : la spec dérive
  directement de todo §16, le brief est intégré via §1 + §15.
- **Pas de touche au `compute_scoring_stability_for_pool`** : signature
  M19 MH.8 préservée (`version: str = "v2.1"`). MN.3 passe juste
  `version=pilot_version`.
- **Pas de touche au flag `SCORING_V2_CUTOVER_READY`** : reste un flag
  Pydantic Settings legacy M12. M21 le lit pour rendu UI uniquement.
  Si refactor de ce flag → module dédié futur.

---

## 3. User stories

### 3.1 Decision cutover v2.1 → v2.1.1 à J+30 (2026-05-29)

**Acteur** : utilisateur (Elie).

**Contexte** : test 30j scoring v2.1 démarré 2026-04-29, restart à
2026-05-02 (post J+3 fixes). À J+30 (2026-05-29), Elie doit décider du
cutover v2.1 → v2.1.1 (cf. todo §9). La validation H-EMP-3 + H-EMP-11
+ H-EMP-13 nécessite une comparaison side-by-side des scores des 2
versions sur les 14j de shadow precedent.

**Avant M21** : Elie ouvre `/traders/scoring`, trouve la page vide. Il
doit ouvrir SSH sur Debian, copier-coller des SQL queries (cf.
`docs/todo.md §8`), parser visuellement les rangs, calculer Spearman
manuellement via Python REPL.

**Après M21** : Elie ouvre `/traders/scoring`, voit immédiatement :

- Header `Scoring comparison — V2.1 | V2.1.1 | delta_rank (M12+M21)`.
- KPI `Pilot version: V2.1`, `Wallets compared (v2.1 ∩ v2.1.1): 47`,
  `Spearman rank(v2.1, v2.1.1): 0.823`, `Top-10 delta: 2`.
- Bloc "Préparation cutover v2.1.1" avec progression 14/14 jours
  écoulés + flag `cutover_ready` + commande `.env` à appliquer.
- Tableau side-by-side : 47 wallets avec scores V2.1 + V2.1.1 + ranks
  locaux + delta_rank + stability column.

**Décision** : flip ou attendre, basée sur l'observation visuelle +
audit Spearman.

### 3.2 Audit hebdomadaire pendant le test 30j

**Acteur** : utilisateur.

**Contexte** : pendant les 30 jours de test, Elie veut vérifier la
progression toutes les semaines (J+7, J+14, J+21).

**Après M21** : ouverture `/traders/scoring` montre direct combien de
wallets sont scorés v2.1, leur stability metric (badge 🟢/🟡/🔴), le
top-10 actuel. Si v2.1.1 commence à être calculé en parallèle (cas
post-flip shadow v2.1.1), les 2 colonnes apparaissent avec le delta_rank.

**Avant M21** : page vide → pas d'audit possible sans SSH SQL.

### 3.3 Détection rapide d'instabilité scoring

**Acteur** : utilisateur.

**Contexte** : un wallet ACTIVE qui devrait être stable a un score qui
oscille fortement cycle-après-cycle.

**Après M21** : la stability column (M19 MH.8 préservée par MN.4)
montre badge 🔴 unstable pour ce wallet. Elie peut investiguer (SQL
detail trader_scores) sans attendre J+30.

### 3.4 Single-version mode (DB neuve, V1 pur, ou v2.1.1 only)

**Acteur** : utilisateur, après reset DB ou flip terminal.

**Contexte** : DB fraîche (1ᵉʳ cycle Discovery pas encore tourné) ou
seul `SCORING_VERSION` calcule, pas de shadow.

**Après M21** : page rend en single-version mode (colonnes shadow et
bloc cutover masqués). Elie voit clairement la version pilote + la
stability column + les scores. Pas de message faux misleading "Aucun
score v1 ou v2 persisté" alors que la pilot est calculée.

**Avant M21** : page hardcoded v1/v2 affiche message confus.

---

## 4. Architecture / décisions clefs

### 4.1 D1 — Helper `detect_comparison_versions` dans `queries.py`

**Décision** : helper dans
[`src/polycopy/dashboard/queries.py`](../../src/polycopy/dashboard/queries.py)
(pas dans un nouveau module dédié).

**Justification** : cohérent avec le pattern existant — toutes les
fonctions support de la page scoring vivent dans `queries.py`
(`list_scoring_comparison`, `scoring_comparison_aggregates`,
`compute_scoring_stability_for_pool`, `_spearman_rank`). Créer un
nouveau module `scoring_versions.py` ferait du sur-engineering pour
30 LOC. Le module `queries.py` est déjà ~1700 LOC, +30 LOC c'est
marginal.

**Alternative rejetée** : module dédié
`src/polycopy/dashboard/scoring_versions.py` — overkill pour 1
fonction. À reconsidérer si M22+ ajoute 5+ helpers liés.

### 4.2 D2 — Fenêtre 30 jours pour la détection shadow

**Décision** : `window_days=30` (default, paramètre du helper).

**Justification** :

1. **Cohérent avec la durée du test scoring v2.1** (30 jours, cf.
   todo §0bis). À J+30, la 2ᵉ version la plus fréquente sera bien
   identifiée.
2. **Couvre les flips récents** : si Elie flip `SCORING_VERSION` de
   `v2.1` → `v2.1.1` aujourd'hui, la fenêtre 30j capture les rows
   précédentes (l'ancienne pilote devient automatiquement la shadow
   visible pendant 30j post-flip).
3. **Ignorance des très anciennes versions** : si la DB contient des
   rows `v1` héritées de M12 (1+ an), elles sortent de la fenêtre 30j
   et n'interfèrent plus. Cohérent avec versioning sacré (rows
   préservées append-only) tout en offrant une UI propre.
4. **Cycle Discovery 6h** → 30 jours = 120 cycles × ~100 wallets =
   ~12k rows. Volume confortable pour détecter la 2ᵉ version sans
   bruit.

**Alternative rejetée** : `window_days=7` — trop court (couvre 28
cycles seulement, peut louper la transition shadow→pilot si flip
récent). `window_days=90` — trop long, capture du bruit historique
M12 `v1` sur DB pas-resetée.

### 4.3 D3 — Fallback `shadow_version=None` silent

**Décision** : si shadow detected = pilot OU aucune 2ᵉ version → retourner
`shadow_version=None` (pas d'exception, pas de log warning).

**Justification** :

1. **Cas légitime** : DB neuve (1ᵉʳ boot), seul pilot calculé encore →
   shadow=None est l'état correct, pas une erreur.
2. **Single-version mode UX clean** : template branche `{% if
   shadow_version %}` qui masque les colonnes shadow + le bloc cutover
   → page rend la version pilote seule sans message misleading.
3. **Pas de noise log** : un boot frais émettrait un warning au 1ᵉʳ
   request. Acceptable mais bruyant. Mieux : silent.
4. **Race condition pendant le 1ᵉʳ cycle Discovery** : si le helper
   est appelé entre le 1ᵉʳ cycle pilot complete et le 1ᵉʳ cycle shadow
   complete (transitoire ~6h), retour `None` cohérent.

**Alternative rejetée** : `raise NoShadowVersionError` — casse la page
sur DB neuve, contre-productif.

### 4.4 D4 — Header dynamique côté template (Jinja idiomatic)

**Décision** : la dynamique des labels (`Score V2.1` / `Score V2.1.1`
/ etc.) vit dans le template Jinja (`{{ pilot_version | upper }}`).

**Justification** :

1. **Idiomatic Jinja** : Jinja est conçu pour ce genre de templating.
   Le filtre `| upper` est natif.
2. **Handler reste minimal** : route handler ne fait que passer 2
   strings. Pas de logique de formatage côté Python (cohérent
   séparation MVC).
3. **Réutilisable** : si une autre page veut afficher la version
   formattée, elle peut consommer la même variable.
4. **Tests faciles** : assertion sur `"Score V2.1"` dans `resp.text`
   est triviale.

**Alternative rejetée** : pré-formater côté handler (`pilot_label =
f"Score {pilot_version.upper()}"` puis passer `pilot_label` au
template) — ajoute du code Python sans bénéfice.

### 4.5 D5 — Préserver tous les patterns M19

**Décision** : MN.4 préserve **strictement** les patterns introduits
par M19 :

- [`render_address`](../../src/polycopy/dashboard/templates/macros.html)
  macro pour les wallets (M19 MH.1).
- `info-icon` `<span title="...">` tooltip pattern (M19 MH.4).
- Stability column avec dispatch badge 🟢/🟡/🔴/⏳ (M19 MH.8).
- Ranks locaux intersection avec badge ✦ si divergence pool (M19 MH.9).
- Aucun nouveau filter Jinja, aucune nouvelle macro, aucune nouvelle
  classe CSS.

**Justification** :

1. **Pas de duplication** : tous les patterns existent déjà.
2. **Cohérence visuelle** : la page reste visuellement identique post-M21
   sur les éléments inchangés (seuls les headers et les labels du
   bloc cutover changent dynamiquement).
3. **Tests régression simplifiés** : les tests `test_dashboard_m19.py`
   passent inchangés.

**Alternative rejetée** : profiter de M21 pour refactoriser les
tooltips ou la stability column — out of scope (cf. §2.2).

---

## 5. Algorithmes

### 5.1 `detect_comparison_versions` (MN.1)

```python
async def detect_comparison_versions(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    window_days: int = 30,
) -> tuple[str, str | None]:
    """M21 MN.1 — détection dynamique des 2 versions à comparer.

    Algorithme :

    1. ``pilot_version = settings.scoring_version`` (source de vérité config).
    2. SQL query : ``SELECT scoring_version, COUNT(*) FROM trader_scores
       WHERE cycle_at >= now - window_days
       AND scoring_version != pilot_version
       GROUP BY scoring_version
       ORDER BY COUNT(*) DESC
       LIMIT 1``
    3. Si row → ``shadow_version = row.scoring_version``. Sinon → None.

    Complexité : O(n) sur les rows ``trader_scores`` filtrées par cycle_at
    + version. Index ``ix_trader_scores_cycle_at`` couvre la fenêtre.
    p50 attendu < 5ms sur 10k rows.
    """
    cutoff = datetime.now(tz=UTC) - timedelta(days=window_days)
    async with session_factory() as session:
        stmt = (
            select(TraderScore.scoring_version, func.count().label("n"))
            .where(TraderScore.cycle_at >= cutoff)
            .where(TraderScore.scoring_version != settings.scoring_version)
            .group_by(TraderScore.scoring_version)
            .order_by(func.count().desc())
            .limit(1)
        )
        result = (await session.execute(stmt)).first()
    shadow_version = str(result.scoring_version) if result is not None else None
    return settings.scoring_version, shadow_version
```

### 5.2 `list_scoring_comparison` refactor (MN.2)

```python
async def list_scoring_comparison(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    pilot_version: str,
    shadow_version: str | None,
    limit: int = 100,
) -> list[ScoringComparisonRow]:
    """M12 + M21 MN.2 — liste les wallets avec leurs scores pilot + shadow.

    Refactor M21 : les littéraux ``'v1'`` et ``'v2.1'`` hardcodés sont
    remplacés par les paramètres ``pilot_version`` + ``shadow_version``.
    Si ``shadow_version is None`` → mode single-version (la sous-requête
    shadow est skippée, ``score_shadow=None`` partout).

    Préserve M19 MH.9 ranks locaux intersection (cf. §5.3).
    """
    limit = _clamp_limit(limit)
    async with session_factory() as session:
        # Latest pilot score per wallet (toujours évalué).
        latest_pilot_subq = (
            select(
                TraderScore.wallet_address,
                func.max(TraderScore.cycle_at).label("max_cycle_at"),
            )
            .where(TraderScore.scoring_version == pilot_version)
            .group_by(TraderScore.wallet_address)
            .subquery()
        )
        pilot_stmt = (
            select(TraderScore)
            .join(
                latest_pilot_subq,
                (TraderScore.wallet_address == latest_pilot_subq.c.wallet_address)
                & (TraderScore.cycle_at == latest_pilot_subq.c.max_cycle_at),
            )
            .where(TraderScore.scoring_version == pilot_version)
        )
        pilot_rows = list((await session.execute(pilot_stmt)).scalars().all())

        # Latest shadow score per wallet (skippé si shadow_version is None).
        shadow_rows: list[TraderScore] = []
        if shadow_version is not None:
            latest_shadow_subq = (
                select(
                    TraderScore.wallet_address,
                    func.max(TraderScore.cycle_at).label("max_cycle_at"),
                )
                .where(TraderScore.scoring_version == shadow_version)
                .group_by(TraderScore.wallet_address)
                .subquery()
            )
            shadow_stmt = (
                select(TraderScore)
                .join(
                    latest_shadow_subq,
                    (TraderScore.wallet_address == latest_shadow_subq.c.wallet_address)
                    & (TraderScore.cycle_at == latest_shadow_subq.c.max_cycle_at),
                )
                .where(TraderScore.scoring_version == shadow_version)
            )
            shadow_rows = list((await session.execute(shadow_stmt)).scalars().all())

        # TargetTrader metadata (toujours évalué).
        traders_stmt = select(TargetTrader)
        traders = list((await session.execute(traders_stmt)).scalars().all())
        trader_by_wallet = {t.wallet_address: t for t in traders}

    pilot_by_wallet = {r.wallet_address: float(r.score) for r in pilot_rows}
    shadow_by_wallet = {r.wallet_address: float(r.score) for r in shadow_rows}

    # Ranks 1-based pool-wide (1 = meilleur).
    pilot_ranked = sorted(pilot_by_wallet.items(), key=lambda kv: kv[1], reverse=True)
    shadow_ranked = sorted(shadow_by_wallet.items(), key=lambda kv: kv[1], reverse=True)
    rank_pilot_pool = {w: i + 1 for i, (w, _) in enumerate(pilot_ranked)}
    rank_shadow_pool = {w: i + 1 for i, (w, _) in enumerate(shadow_ranked)}

    # M19 MH.9 — ranks locaux intersection (cohérent Spearman ρ).
    intersection = set(pilot_by_wallet) & set(shadow_by_wallet)
    pilot_local_sorted = sorted(intersection, key=lambda w: pilot_by_wallet[w], reverse=True)
    shadow_local_sorted = sorted(intersection, key=lambda w: shadow_by_wallet[w], reverse=True)
    rank_pilot_local = {w: i + 1 for i, w in enumerate(pilot_local_sorted)}
    rank_shadow_local = {w: i + 1 for i, w in enumerate(shadow_local_sorted)}

    all_wallets = set(pilot_by_wallet) | set(shadow_by_wallet) | set(trader_by_wallet)
    rows: list[ScoringComparisonRow] = []
    for wallet in all_wallets:
        s_pilot = pilot_by_wallet.get(wallet)
        s_shadow = shadow_by_wallet.get(wallet)
        r_pilot_pool = rank_pilot_pool.get(wallet)
        r_shadow_pool = rank_shadow_pool.get(wallet)
        delta = (
            (r_pilot_pool - r_shadow_pool)
            if (r_pilot_pool is not None and r_shadow_pool is not None)
            else None
        )
        t = trader_by_wallet.get(wallet)
        rows.append(
            ScoringComparisonRow(
                wallet_address=wallet,
                label=t.label if t is not None else None,
                status=t.status if t is not None else "absent",
                pinned=bool(t.pinned) if t is not None else False,
                # Generic aliases (template consumes these post-M21).
                score_pilot=s_pilot,
                score_shadow=s_shadow,
                rank_pilot_pool=r_pilot_pool,
                rank_shadow_pool=r_shadow_pool,
                rank_pilot_local=rank_pilot_local.get(wallet),
                rank_shadow_local=rank_shadow_local.get(wallet),
                # Legacy v1/v2 aliases (deprecated, retire 1 release).
                score_v1=s_pilot,
                score_v2=s_shadow,
                rank_v1=r_pilot_pool,
                rank_v2=r_shadow_pool,
                rank_v1_pool=r_pilot_pool,
                rank_v2_pool=r_shadow_pool,
                rank_v1_local=rank_pilot_local.get(wallet),
                rank_v2_local=rank_shadow_local.get(wallet),
                delta_rank=delta,
                last_scored_at=(t.last_scored_at if t is not None else None),
            ),
        )
    rows.sort(
        key=lambda r: (
            -(r.score_shadow if r.score_shadow is not None else -1),
            -(r.score_pilot if r.score_pilot is not None else -1),
        ),
    )
    return rows[:limit]
```

### 5.3 `scoring_comparison_aggregates` refactor (MN.2)

```python
async def scoring_comparison_aggregates(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    pilot_version: str,
    shadow_version: str | None,
    shadow_days: int,
    cutover_ready: bool,
) -> ScoringComparisonAggregates:
    """M12 + M21 MN.2 — agrégats pool-wide pour la section header.

    Refactor M21 : si ``shadow_version is None`` → court-circuit
    immédiat (wallets_compared=0, spearman=None, top10_delta=0,
    shadow_days_elapsed=None). Sinon, logique préservée.
    """
    if shadow_version is None:
        return ScoringComparisonAggregates(
            wallets_compared=0,
            median_delta_rank=None,
            spearman_rank=None,
            top10_delta=0,
            shadow_days_elapsed=None,
            shadow_days_remaining=None,
            cutover_ready=cutover_ready,
        )

    rows = await list_scoring_comparison(
        session_factory,
        pilot_version=pilot_version,
        shadow_version=shadow_version,
        limit=_MAX_LIMIT,
    )
    with_both = [r for r in rows if r.score_pilot is not None and r.score_shadow is not None]

    median_delta: float | None = None
    spearman: float | None = None
    if len(with_both) >= 1:
        deltas = sorted(r.delta_rank for r in with_both if r.delta_rank is not None)
        if deltas:
            mid = len(deltas) // 2
            median_delta = (
                float(deltas[mid])
                if len(deltas) % 2 == 1
                else (deltas[mid - 1] + deltas[mid]) / 2.0
            )
    if len(with_both) >= 3:
        # M19 MH.9 — Spearman calculé sur ranks locaux intersection.
        pilot_sorted = sorted(with_both, key=lambda r: r.score_pilot or 0.0, reverse=True)
        local_rank_pilot = {r.wallet_address: i + 1 for i, r in enumerate(pilot_sorted)}
        shadow_sorted = sorted(with_both, key=lambda r: r.score_shadow or 0.0, reverse=True)
        local_rank_shadow = {r.wallet_address: i + 1 for i, r in enumerate(shadow_sorted)}
        spearman = _spearman_rank(
            [float(local_rank_pilot[r.wallet_address]) for r in with_both],
            [float(local_rank_shadow[r.wallet_address]) for r in with_both],
        )

    top10_pilot = {
        r.wallet_address for r in rows if r.rank_pilot_pool is not None and r.rank_pilot_pool <= 10
    }
    top10_shadow = {
        r.wallet_address for r in rows if r.rank_shadow_pool is not None and r.rank_shadow_pool <= 10
    }
    top10_delta = len(top10_shadow - top10_pilot)

    async with session_factory() as session:
        first_shadow_cycle = (
            await session.execute(
                select(func.min(TraderScore.cycle_at)).where(
                    TraderScore.scoring_version == shadow_version,
                ),
            )
        ).scalar_one_or_none()
    shadow_elapsed: int | None = None
    shadow_remaining: int | None = None
    if first_shadow_cycle is not None:
        if first_shadow_cycle.tzinfo is None:
            first_shadow_cycle = first_shadow_cycle.replace(tzinfo=UTC)
        shadow_elapsed = (datetime.now(tz=UTC) - first_shadow_cycle).days
        shadow_remaining = max(0, shadow_days - shadow_elapsed)

    return ScoringComparisonAggregates(
        wallets_compared=len(with_both),
        median_delta_rank=median_delta,
        spearman_rank=spearman,
        top10_delta=top10_delta,
        shadow_days_elapsed=shadow_elapsed,
        shadow_days_remaining=shadow_remaining,
        cutover_ready=cutover_ready,
    )
```

### 5.4 `_spearman_rank` (intact)

`_spearman_rank` reste pure function inchangée. Cf.
[`queries.py:1605-1617`](../../src/polycopy/dashboard/queries.py#L1605).
Préservé tel quel par M21 (versioning sacré algorithm).

---

## 6. DTOs

### 6.1 `ScoringComparisonRow` (étendu)

```python
@dataclass(frozen=True)
class ScoringComparisonRow:
    """Row pour le tableau pilot|shadow|delta_rank de ``/traders/scoring``.

    M21 MN.2 : ajoute alias génériques ``score_pilot`` / ``score_shadow``
    / ``rank_pilot_*`` / ``rank_shadow_*`` consommés par le template.
    Les anciens alias ``score_v1`` / ``score_v2`` / ``rank_v1*`` / ``rank_v2*``
    restent populés (rétrocompat 1 release pour tests M12/M19) avec les
    mêmes valeurs.
    """

    wallet_address: str
    label: str | None
    status: str
    pinned: bool
    # M21 MN.2 — alias génériques (consommés par template).
    score_pilot: float | None = None
    score_shadow: float | None = None
    rank_pilot_pool: int | None = None
    rank_shadow_pool: int | None = None
    rank_pilot_local: int | None = None
    rank_shadow_local: int | None = None
    # M12 legacy aliases (deprecated 1 release, encore consommés par tests).
    score_v1: float | None = None
    score_v2: float | None = None
    rank_v1: int | None = None
    rank_v2: int | None = None
    rank_v1_pool: int | None = None
    rank_v2_pool: int | None = None
    rank_v1_local: int | None = None
    rank_v2_local: int | None = None
    # Communs.
    delta_rank: int | None = None
    last_scored_at: datetime | None = None
```

### 6.2 `ScoringComparisonAggregates` (inchangé)

Pas de modification. Les champs existants `wallets_compared`,
`median_delta_rank`, `spearman_rank`, `top10_delta`,
`shadow_days_elapsed`, `shadow_days_remaining`, `cutover_ready` restent
agnostiques de la version.

---

## 7. Settings (env vars + Pydantic)

**Aucune nouvelle env var ajoutée par M21.**

Settings consommés (existants, inchangés) :

- `Settings.scoring_version: Literal["v1", "v2.1", "v2.1.1"]` (M14 MA +
  M15 MB.2). Source de vérité pour `pilot_version`.
- `Settings.scoring_v2_shadow_days: int` (M12). Durée totale shadow
  period configurée. Lue par MN.3 et passée à `scoring_comparison_aggregates`
  via `shadow_days=settings.scoring_v2_shadow_days`.
- `Settings.scoring_v2_cutover_ready: bool` (M12). Lu par MN.3 et passé
  à l'aggregates via `cutover_ready=settings.scoring_v2_cutover_ready`.

**Note explicite sur `SCORING_VERSION`** : `Literal["v1", "v2.1", "v2.1.1"]`
restreint la valeur acceptable. Si M22+ ajoute `v2.2` (post-MF wash
detection capstone), Pydantic Literal devra être étendu — c'est une
modification CLAUDE.md §Conventions hors scope M21. M21 ne pré-suppose
pas un set fini de versions ; la fonction `detect_comparison_versions`
accepte n'importe quelle string trouvée dans `trader_scores.scoring_version`.

---

## 8. Invariants sécurité

### 8.1 Dashboard read-only strict (M4.5 invariant)

M21 ajoute **0 endpoint POST/PUT/DELETE**. Le handler
`/traders/scoring` reste `@router.get(...)`. Vérifié par grep
automatisé `test_dashboard_security.py`.

### 8.2 Pas de fuite de secrets

Les rows `trader_scores` et `target_traders` contiennent uniquement :

- `wallet_address` (publique on-chain).
- `score: float` (calcul dérivé).
- `scoring_version: str` (`v1` / `v2.1` / `v2.1.1` — labels publics
  dans CLAUDE.md).
- `metrics_snapshot: JSON` (metrics dérivés Data API publique).
- `cycle_at: datetime`.

Aucun secret. Le helper `detect_comparison_versions` lit uniquement
`scoring_version` + `cycle_at` agrégés. Aucun risque de leak.

`render_address` macro M19 MH.1 expose les wallets fullhash (publique
on-chain) — invariant préservé (cf. M19 §8).

### 8.3 Bind localhost-only

Le dashboard reste bind sur `127.0.0.1` par défaut (M4.5 invariant). Si
`DASHBOARD_BIND_TAILSCALE=true` (M12_bis Phase E), bind Tailscale CGNAT
strict (validator Pydantic refuse `0.0.0.0` / `127.0.0.1` overrides en
mode Tailscale). M21 ne touche aucun de ces paramètres.

### 8.4 CDN versions M6 inchangées

Tailwind 3.4.16, HTMX 2.0.4, Chart.js 4.4.7, Lucide 0.469.0 — strict
no-bump. M21 n'ajoute aucune nouvelle dep CDN. Vérifié par grep dans
`base.html`.

### 8.5 Versioning sacré préservé

M21 ne touche **aucune** fonction `compute_score_*` (v1 dans
`v1.py`, v2.1 dans `v2/aggregator.py`, v2.1.1 dans
`v2/aggregator.py`). Le registre `SCORING_VERSIONS_REGISTRY` reste
intact.

Vérifié par : `git diff --stat src/polycopy/discovery/scoring/` retourne
0 lignes post-merge.

### 8.6 Append-only DB préservé

M21 est **read-only sur `trader_scores`** (uniquement queries SELECT).
Aucune row réécrite ou supprimée. Aucune migration Alembic 0011 ajoutée
— `alembic upgrade head` retourne "no migrations to apply" post-M21,
head reste **0010** (M17 MD.3 dernière migration).

### 8.7 Filtre `simulated` strict M17 MD.1 préservé par construction

Note explicite : la page scoring n'utilise **pas** `MyPosition`. Le
filtre `simulated == (execution_mode != "live")` n'a aucun chemin
d'exécution dans M21. Préservé par non-touche.

### 8.8 `localStorage` discipline M9/M10 préservée

Aucune nouvelle clé `polycopy.*` ajoutée par M21. La page scoring n'a
pas besoin de persister de préférence UI v1.

---

## 9. Test plan

### 9.1 Tests unit nouveaux (4)

Fichier :
[`tests/unit/test_dashboard_scoring_route.py`](../../tests/unit/test_dashboard_scoring_route.py).

| Test | Cible | Assertions clés |
|---|---|---|
| `test_detect_comparison_versions_returns_pilot_only_when_db_empty` | MN.1 | DB vide + Settings v2.1 → `(pilot="v2.1", shadow=None)` |
| `test_detect_comparison_versions_picks_second_most_frequent` | MN.1 | Pool {v2.1: 5, v2.1.1: 3, v1: 1}, pilot=v2.1 → shadow=v2.1.1 (2ᵉ plus fréquent exclu pilot) |
| `test_dashboard_scoring_render_with_v2_1_only_pilot` | MN.3+MN.4 | Pool v2.1 only → page rend single-version mode, headers `Score V2.1`, bloc cutover masqué |
| `test_dashboard_scoring_v2_1_vs_v2_1_1_shadow` | MN.3+MN.4 | Pool v2.1+v2.1.1 → headers dynamiques + label cutover "Préparation cutover v2.1.1" |

### 9.2 Tests M12 existants à adapter (8)

Fichier idem.

| Test | Adaptation |
|---|---|
| `test_traders_scoring_page_renders_empty_when_no_scores` | Assertion message empty change : `"Aucun score V1"` ou pilot detected |
| `test_traders_scoring_page_renders_v1_only_rows` | Settings `scoring_version="v1"` ; assertion `"M12+M21"` au lieu de `"M12"` |
| `test_scoring_comparison_query_with_v1_and_v2` | Passer `pilot_version="v1"`, `shadow_version="v2.1"` ; consommer `rank_pilot_pool` au lieu de `rank_v1` (alias legacy gardé fonctionnel) |
| `test_scoring_comparison_aggregates_spearman_computed` | Passer versions explicites |
| `test_scoring_comparison_aggregates_none_spearman_below_3` | Idem |
| `test_cutover_ready_flag_passed_through_from_settings` | Settings `scoring_version="v1"` + seed 1 row v2.1 (shadow détecté) sinon bloc cutover masqué |
| `test_sidebar_link_present_in_base_template` | Assertion `"Scoring comparison"` au lieu de `"Scoring v1/v2"` |
| `test_spearman_uses_intersection_ranks_not_pool_ranks` | Passer versions explicites ; assertions ρ=−0.5 préservées |

### 9.3 Tests régression M19 (préservés intacts)

- `test_dashboard_m19.py::test_compute_scoring_stability_for_pool_*`
  → intacts. M21 passe `version=pilot_version` mais le helper M19 MH.8
  est paramétrique depuis le début.
- `test_dashboard_m19.py::test_dashboard_stability_badge_*`
  → intacts. Le rendu badge dans `traders_scoring.html` est inchangé
  par MN.4.

### 9.4 Tests régression sécurité

- `test_dashboard_security.py` → intact, aucune nouvelle fuite.
- `test_dashboard_security_m6.py` → intact, aucune nouvelle dep CDN.

### 9.5 Test régression Spearman intersection (M19 MH.9)

- `test_spearman_uses_intersection_ranks_not_pool_ranks`
  → adapté à passer versions explicites mais sémantique préservée.
  Garde-fou : ρ=−0.5 sur la fixture spécifique.

### 9.6 Test pure function

- `test_spearman_rank_function_edge_cases` → intact (pure function).

### 9.7 Couverture totale post-M21

- 6 tests M12 adaptés + 4 tests M21 nouveaux = **10 tests dans
  `test_dashboard_scoring_route.py`**.
- M19 stability + M19 security + M6 security préservés = **régressions
  zéro**.

---

## 10. Impact existant

### 10.1 Fichiers modifiés

| Fichier | Type | Diff | Item |
|---|---|---|---|
| [src/polycopy/dashboard/queries.py](../../src/polycopy/dashboard/queries.py) | feature | +1 helper (~35 LOC) + refactor 2 fonctions (~15 LOC modif) + DTO étendu (~10 LOC) | MN.1 + MN.2 |
| [src/polycopy/dashboard/routes.py](../../src/polycopy/dashboard/routes.py#L193) | feature | +5 LOC (call detect + pass versions au template) | MN.3 |
| [src/polycopy/dashboard/templates/traders_scoring.html](../../src/polycopy/dashboard/templates/traders_scoring.html) | feature | template entier réécrit (headers + cutover + table dynamiques) | MN.4 |
| [src/polycopy/dashboard/templates/base.html](../../src/polycopy/dashboard/templates/base.html#L152) | feature | sidebar label `Scoring v1/v2` → `Scoring comparison` (2 occurrences) | MN.4 |
| [tests/unit/test_dashboard_scoring_route.py](../../tests/unit/test_dashboard_scoring_route.py) | tests | +4 tests + 8 tests adaptés | MN.6 |

### 10.2 Fichiers non modifiés (mais consommés)

| Fichier | Rôle | Lien |
|---|---|---|
| `src/polycopy/dashboard/templates/macros.html` | `render_address` macro M19 MH.1 | consommé par MN.4 |
| `src/polycopy/discovery/scoring/__init__.py` | Registry SCORING_VERSIONS_REGISTRY | non touché (versioning sacré) |
| `src/polycopy/storage/models.py` | TraderScore model | non touché (lecture seule) |
| `alembic/versions/` | aucun nouveau fichier | head reste 0010 |

### 10.3 Charge dev

| Item | LOC ajoutées | LOC modifiées | Tests ajoutés |
|---|---|---|---|
| MN.1 | ~35 | 0 | 2 |
| MN.2 | ~50 | ~30 | 1 (réutilise) |
| MN.3 | ~10 | ~5 | 0 (regression via existing) |
| MN.4 | ~150 (template) | ~100 (template) | 1 |
| MN.5 | 0 | 0 | 0 (regression existante) |
| MN.6 | ~150 | ~80 | 4 nouveaux + 8 adaptés |

Total : ~395 LOC ajoutées + ~215 LOC modifiées + 12 tests dont 4 nouveaux.

---

## 11. Migration / rollout

### 11.1 Diff strictement additif sur API

- `detect_comparison_versions` est une nouvelle fonction publique →
  pas de breaking change.
- `list_scoring_comparison` + `scoring_comparison_aggregates` ajoutent
  des paramètres `pilot_version` + `shadow_version` keyword-only.
  Refactor breaking pour callers — mais le seul caller est le route
  handler `/traders/scoring` (MN.3 met à jour). Aucun caller externe.
- `ScoringComparisonRow` étendu avec alias génériques. Anciens alias
  `score_v1` / `score_v2` / `rank_v1*` / `rank_v2*` préservés 1 release
  (drop M22+) pour rétrocompat tests legacy.

### 11.2 Pas de feature flag

M21 est livré direct sur `main`. Pas de variable `M21_ENABLED` —
diff est trivialement réversible via `git revert` si problème.

### 11.3 Pas de migration Alembic

`alembic upgrade head` retourne "no migrations to apply" post-M21.
Head reste **0010** (M17 MD.3).

Vérifié manuellement :

```bash
cd ~/Documents/GitHub/polycopy
source .venv/bin/activate
alembic upgrade head
# → "INFO  [alembic.runtime.migration] Will assume non-transactional DDL.
#    INFO  [alembic.runtime.migration] Context impl SQLiteImpl.
#    INFO  [alembic.runtime.migration] Will assume non-transactional DDL."
# Aucune nouvelle revision affichée.
```

### 11.4 Procédure ops post-merge

```bash
# 1. Pull main
ssh debian-prod
cd ~/Documents/GitHub/polycopy
git pull origin main

# 2. Restart bot (settings inchangés)
systemctl --user restart polycopy

# 3. Smoke validation dashboard
curl -sf http://127.0.0.1:8787/traders/scoring | head -50
# Attendu : HTML rendant la page sans erreur, headers dynamiques.

# 4. Vérifier alembic
alembic current
# → "0010_m17_pnl_snapshot_execution_mode (head)"
```

### 11.5 Rollback

```bash
cd ~/Documents/GitHub/polycopy
git revert <commit-MN.6>..<commit-MN.1>  # 6 commits
systemctl --user restart polycopy
```

Aucune migration DB à rollback (versioning sacré, append-only).

---

## 12. Commandes de vérification

### 12.1 Tests unit

```bash
cd ~/code/polycopy
source .venv/bin/activate
pytest tests/unit/test_dashboard_scoring_route.py -x --tb=short
# Attendu : 10 tests verts (4 nouveaux + 6 adaptés)

pytest tests/unit/test_dashboard*.py -x --tb=short
# Attendu : ~50+ tests verts (M12 + M19 + M21 cumulés)
```

### 12.2 Lint + types

```bash
ruff check . && ruff format --check .
# Attendu : 0 erreur

mypy src --strict
# Attendu : 0 erreur
```

### 12.3 Sécurité

```bash
pytest tests/unit/test_dashboard_security.py tests/unit/test_dashboard_security_m6.py -x
# Attendu : tous verts (zéro régression)

grep -rn "POLYMARKET_PRIVATE_KEY\|TELEGRAM_BOT_TOKEN\|CLOB_API_SECRET\|REMOTE_CONTROL_TOTP_SECRET" \
  src/polycopy/dashboard/templates/
# Attendu : aucune ligne

grep -rn '"v1"\|"v2"\|"v2.1"' src/polycopy/dashboard/templates/traders_scoring.html
# Attendu : 0 ligne (chaînes hardcoded retirées)
```

### 12.4 Migration Alembic head

```bash
alembic current
# Attendu : "0010_m17_pnl_snapshot_execution_mode (head)"

alembic upgrade head
# Attendu : aucune nouvelle revision appliquée
```

### 12.5 Smoke runtime dashboard

```bash
EXECUTION_MODE=dry_run DASHBOARD_ENABLED=true python -m polycopy --verbose &
sleep 8

curl -sf http://127.0.0.1:8000/traders/scoring | grep -E "Scoring comparison|Pilot version"
# Attendu : ≥1 match

# Single-version mode (DB neuve, pilot=v2.1, shadow=None)
curl -sf http://127.0.0.1:8000/traders/scoring | grep "Cutover status"
# Attendu : 0 match (bloc masqué)

# Vérifier sidebar
curl -sf http://127.0.0.1:8000/home | grep "Scoring comparison"
# Attendu : ≥1 match (sidebar label MN.4)

pkill -f polycopy
```

### 12.6 Versioning sacré (régression test)

```bash
git diff --stat HEAD~6 HEAD -- src/polycopy/discovery/scoring/
# Attendu : 0 fichiers modifiés (zéro touche au scoring engine)
```

---

## 13. Notes d'implémentation (pièges fréquents)

### 13.1 Race condition pendant le 1ᵉʳ cycle Discovery

**Piège** : `detect_comparison_versions` est appelé à chaque request
GET `/traders/scoring`. Si la requête arrive entre le 1ᵉʳ cycle pilot
complete et le 1ᵉʳ cycle shadow complete, la fonction retourne
`(pilot, None)` — page rend en single-version mode pendant ~6h
(intervalle Discovery), puis switche au cycle suivant.

**Acceptable** : transitoire 6h, le UX est cohérent (pas d'erreur
visible). Documenté dans la docstring de `detect_comparison_versions`.

### 13.2 Pool sub-3 wallets → Spearman None mais headers OK

**Piège** : si `with_both` < 3 wallets, `_spearman_rank` retourne
`None`. La KPI affiche "—". Mais les headers (`Score V2.1`, etc.)
restent corrects. Pas de bug.

**Test** : `test_scoring_comparison_aggregates_none_spearman_below_3`
adapté. KPI Spearman doit afficher "—" sans crash.

### 13.3 SQLite naïve datetime → ré-injection UTC

**Piège** : SQLite ne persiste pas `tzinfo` sur `DateTime(timezone=True)`.
Le code existant gère déjà via :

```python
if first_shadow_cycle.tzinfo is None:
    first_shadow_cycle = first_shadow_cycle.replace(tzinfo=UTC)
```

MN.2 préserve cette gestion lors du refactor. **À ne pas oublier** dans
le port MN.2.

### 13.4 Templates : retire toutes les chaînes hardcoded

**Piège** : grep agressif requis post-merge :

```bash
grep -rn '"v1"\|"v2"\|"v2.1"' src/polycopy/dashboard/templates/traders_scoring.html
```

Si match → fichier pas entièrement refactor. Cas connus à vérifier :

- Heading `{% block heading %}` : passé à dynamique.
- Description text : passé à dynamique.
- KPI labels (`Wallets compared`, `Spearman rank`) : passés à dynamique.
- Cutover commande pre `SCORING_VERSION=v2` → `SCORING_VERSION={{ shadow_version }}`.
- Headers tableau (`Score v1`, `Score v2`, `Rank v1`, `Rank v2`) :
  tous passés à dynamique.
- Empty row message `"Aucun score v1 ou v2 persisté"` : passé à
  dynamique avec `{{ pilot_version }}` + `{{ shadow_version }}`.

### 13.5 Sidebar label change

**Piège** : `base.html` a 2 occurrences (desktop sidebar + mobile
sidebar). Les deux doivent passer de `"Scoring v1/v2"` à
`"Scoring comparison"`. Test
`test_sidebar_link_present_in_base_template` confirme.

### 13.6 ScoringComparisonRow alias retrocompat

**Piège** : si on retire trop tôt les alias `score_v1` / `score_v2` /
`rank_v1*` / `rank_v2*` du DTO, les tests M12 existants cassent.
Convention : préserver 1 release (drop M22+). Documenté dans la
docstring du DTO (`# M12 legacy aliases (deprecated 1 release)`).

### 13.7 Détection version excluant la pilote

**Piège** : `detect_comparison_versions` filtre `WHERE
scoring_version != pilot_version`. Si on oublie ce filtre, le pilote
peut se retrouver lui-même comme `shadow` (cas dégénéré).

**Test** : `test_detect_comparison_versions_picks_second_most_frequent`
seed un pool où pilot a le plus de rows mais on attend shadow=2ᵉ
plus fréquent.

### 13.8 Tests adaptés : importer le DTO refactor avec rétrocompat

**Piège** : les tests M12 existants accèdent `row.rank_v1` / `row.score_v2`.
Avec MN.2 refactor, les nouveaux noms sont `row.rank_pilot_pool` /
`row.score_shadow`. Les anciens noms restent populés en rétrocompat —
les tests fonctionnent **inchangés** sur cette dimension. Mais les
nouveaux tests utilisent les nouveaux noms.

---

## 14. Hors scope (liste exhaustive)

- **Page "scoring history graph"** : Chart.js cycle-by-cycle des scores
  par wallet — overkill v1, MH.8 stability badge suffit.
- **Export CSV** : workaround SQL CLI suffisant.
- **Filtres temporels avancés** : pas de query string `?since=...`.
- **Touche au scoring engine** : zero diff dans `discovery/scoring/`.
- **Migration Alembic** : zero migration 0011, head reste 0010.
- **Création brief `docs/next/MN.md`** : non créé (spec dérive todo §16).
- **Touche au flag `SCORING_V2_CUTOVER_READY`** : reste flag M12 legacy
  Settings.
- **Compute_scoring_stability paramètre version sortie** : signature
  M19 MH.8 préservée (`version: str = "v2.1"`). MN.3 passe juste
  `version=pilot_version`.
- **Refactor sidebar global** : seul le label de `/traders/scoring`
  est changé. Les autres labels sidebar (`Home`, `Détection`, etc.)
  intacts.
- **Touche aux templates `home.html`, `performance.html`, etc.** :
  zéro modif, M21 ne touche que `traders_scoring.html` + `base.html`.
- **Création nouveau filter Jinja** : tous les filters M19 (`format_usd`,
  `format_size_precise`, `short_hash`, `telegram_md_escape`) intacts.
- **Création nouvelle macro Jinja** : `render_address` (M19 MH.1)
  consommée intacte.
- **Touche au `SCORING_VERSIONS_REGISTRY`** : registry intact (cf. §8.5).

---

## 15. Mapping origines (traçabilité)

| Item | Source | Reference |
|---|---|---|
| MN.1 | todo §16 ligne 941 ("Header de colonne dynamique : insérer la version réelle") | [todo.md §16](../todo.md#L934) |
| MN.2 | todo §16 ligne 944 ("Adapter SQL : `WHERE scoring_version IN (:pilot, :shadow)` paramétré") | [todo.md §16](../todo.md#L944) |
| MN.3 | todo §16 ligne 938-941 (refactor template + handler) | [todo.md §16](../todo.md#L938) |
| MN.4 | todo §16 ligne 938-944 (renommage colonnes + headers dynamiques + cutover conditionnel) | [todo.md §16](../todo.md#L938) |
| MN.5 | todo §16 ligne 947 ("Spearman + Top-N delta génériques sur 2 versions arbitraires") | [todo.md §16](../todo.md#L947) |
| MN.6 | todo §16 ligne 951-958 ("Tests à ajouter / adapter") | [todo.md §16](../todo.md#L951) |
| Constraint "pas de migration" | CLAUDE.md §Conventions append-only DB + M19 D4 (JOIN runtime retenu pattern) | [CLAUDE.md](../../CLAUDE.md) |
| Constraint "versioning sacré" | CLAUDE.md §Sécurité scoring v2.1-ROBUST + M14/M15 specs | [CLAUDE.md](../../CLAUDE.md) |
| Constraint "read-only dashboard" | M4.5 spec + M6 spec + M19 §8 invariants | [M4.5](M4.5-dashboard.md) [M19](M19-dashboard-ux-polish.md#L1163) |
| Pattern "render_address macro" | M19 MH.1 | [M19](M19-dashboard-ux-polish.md#L253) |
| Pattern "stability column M19 MH.8" | M19 MH.8 | [M19](M19-dashboard-ux-polish.md) |
| Pattern "ranks locaux intersection" | M19 MH.9 + queries.py:1487-1494 | [M19](M19-dashboard-ux-polish.md) |
| Observation déclencheur | 2026-05-02 J+3 test 30j (page vide observée) | [todo.md §0bis](../todo.md#L10) |

---

## 16. Prompt d'implémentation pour `/implement-module`

```markdown
# Tâche

Implémenter M21 (Scoring comparison page generic refactor) selon la spec
[docs/specs/M21-scoring-comparison-page-generic-refactor.md](docs/specs/M21-scoring-comparison-page-generic-refactor.md).

6 commits atomiques (cf. spec §17), ordre recommandé du plus simple au
plus structurant :

1. MN.1 — Helper `detect_comparison_versions` dans `queries.py` (~35 LOC + 2 tests)
2. MN.2 — Refactor `list_scoring_comparison` + `scoring_comparison_aggregates` paramétrés (versions explicites)
3. MN.3 — Route handler `/traders/scoring` consume `detect_comparison_versions`
4. MN.4 — Template `traders_scoring.html` + sidebar `base.html` dynamiques
5. MN.5 — Régression test Spearman intersection préservée + top-N delta paramétré
6. MN.6 — Tests dashboard adaptés (8) + nouveaux (4)

# Prérequis (à lire avant)

- [docs/specs/M21-scoring-comparison-page-generic-refactor.md](docs/specs/M21-scoring-comparison-page-generic-refactor.md)
- [docs/specs/M12-scoring-v2.md](docs/specs/M12-scoring-v2.md) §5.5 (page initiale hardcoded)
- [docs/specs/M19-dashboard-ux-polish.md](docs/specs/M19-dashboard-ux-polish.md)
  §2.1 MH.8 (stability metric) + MH.9 (ranks locaux intersection) — invariants strict no-touch
- [docs/specs/M4.5-dashboard.md](docs/specs/M4.5-dashboard.md) (read-only
  strict invariant — strict no-touch sur `@router.post/put/delete`)
- [docs/specs/M6-dashboard-2026.md](docs/specs/M6-dashboard-2026.md) (CDN
  versions pinned Tailwind 3.4.16, HTMX 2.0.4, Chart.js 4.4.7, Lucide 0.469.0
  — strict no-bump)
- [CLAUDE.md](CLAUDE.md) §Conventions versioning sacré + §Sécurité Dashboard
- [docs/todo.md §16](docs/todo.md#L934) (source du besoin)

# Contraintes

- **Lecture seule** sur `src/polycopy/discovery/`, `tests/`, docs sources.
- **Dashboard read-only strict** (M4.5 invariant) : M21 ajoute 0 POST/PUT/DELETE.
- **CDN versions pinned** (M6) inchangées : Tailwind 3.4.16, HTMX 2.0.4,
  Chart.js 4.4.7, Lucide 0.469.0. Aucune nouvelle dep CDN.
- **Grep security anti-leak** : `test_dashboard_security.py` +
  `test_dashboard_security_m6.py` doivent passer inchangés.
- **Versioning sacré** : aucune fonction `compute_score_*` touchée.
  M21 est read-only sur `trader_scores`.
- **Pas de migration Alembic** : `alembic upgrade head` retourne "no
  migrations to apply" post-M21. Head reste **0010**.
- **Append-only DB** : aucune row réécrite.
- **`localStorage` discipline M9/M10** : aucune nouvelle clé en v1.
- **Filtre `simulated` strict M17 MD.1** : non concerné par M21 (la page
  scoring n'utilise pas `MyPosition`). Préservé par non-touche.

# Demande-moi confirmation AVANT

- Création d'un nouveau brief `docs/next/MN.md` — spec retient l'option
  intégrer le brief dans la spec elle-même via §1 + §15.
- Migration Alembic 0011 — pas nécessaire selon la spec, signaler avant
  d'en créer une.
- Refactor du DTO `ScoringComparisonRow` au-delà des extensions §6
  (alias génériques + retrocompat 1 release).
- Touche au `SCORING_VERSIONS_REGISTRY` — versioning sacré, hors scope
  M21.
- Update `CLAUDE.md` §Conventions ou §Sécurité (M21 = UX polish + dette
  M12, ne devrait pas nécessiter de bloc CLAUDE.md dédié — confirmer
  si exception).
- Toucher la signature `compute_scoring_stability_for_pool` — M19 MH.8
  paramétrique préservé strict.
- Retirer plus tôt les alias legacy `score_v1` / `rank_v1*` du DTO —
  préservé 1 release.

# STOP et signale si

- `detect_comparison_versions` p50 > 50ms en bench local sur 10k rows
  → revisiter D2 (réduire window_days à 14j ou ajouter cache 5 min).
- Le test `test_dashboard_security.py` casse à cause d'un nouveau
  template ou d'une macro — enquêter sur la fuite avant tout
  workaround.
- Les tests M12 existants ne fonctionnent plus avec les alias legacy
  préservés (devrait pas arriver mais signaler si oui — débugger
  retrocompat).
- Le rendu `traders_scoring.html` casse en single-version mode (cas
  shadow=None) — vérifier que `{% if shadow_version %}` est posé
  correctement sur tous les blocs concernés (KPI cards, cutover
  section, table headers shadow, table cells shadow).
- Le scénario "DB resetée + 1 cycle pilot v2.1 only" génère un message
  fallback misleading — vérifier l'empty row message dynamique.
- Un test integration smoke nécessite un fixture DB seedée non
  trivial — proposer un fixture neuf `tests/fixtures/db_two_versions.sql`
  avant d'écrire le test.

# Smoke test final obligatoire avant merge

Cf. spec §12.1 + §12.2 + §12.3 + §12.4 + §12.5 (5 commandes vérification).
Notamment :

- `pytest tests/unit/test_dashboard_scoring_route.py -x --tb=short` —
  10 tests verts (4 nouveaux + 6 adaptés).
- `pytest tests/unit/test_dashboard*.py -x --tb=short` — 0 failure
  (~50 tests, M12 + M19 + M21 cumulés).
- `ruff check . && ruff format --check . && mypy src --strict` — 0 erreur.
- `grep -rn '"v1"\|"v2"\|"v2.1"' src/polycopy/dashboard/templates/traders_scoring.html` — 0 ligne post-merge (chaînes hardcoded retirées).
- `alembic upgrade head` — "no migrations to apply" (head reste 0010).
- `git diff --stat HEAD~6 HEAD -- src/polycopy/discovery/scoring/` —
  0 fichiers modifiés (versioning sacré préservé).
- Smoke runtime `EXECUTION_MODE=dry_run DASHBOARD_ENABLED=true python -m
  polycopy --verbose` boot < 10s, fetch /traders/scoring rend 200 + HTML
  contient `Scoring comparison`, `Pilot version`.
- Test mode single-version (DB sans shadow) : grep `Cutover status`
  retourne 0 match.

# Livrable

- 6 commits sur `main` (pas de branche, pas de PR — règle projet).
- Spec M21 mise à jour si la rédaction révèle un facteur non-anticipé
  (sinon laisser intacte).
- ROADMAP.md mis à jour : MN/M21 marqué shipped, déplacé de "Roadmap
  restante" vers "Phase dashboard UX polish" (statut shipped + date).
- Ping final ≤ 10 lignes :
  - 6 commits MN.1 → MN.6 mergés
  - Tests : 4 unit nouveaux + 6 adaptés + 8 régressions M12 verts +
    M19 MH.8 / MH.9 régressions verts
  - Smoke runtime OK (curl /traders/scoring single-version + dual-version)
  - Charge réelle dev (vs estimé 1-2 jours)
  - Risques résiduels post-merge (notamment race 1ᵉʳ cycle, retrocompat
    aliases legacy à drop M22+)
```

---

## 17. Commit messages proposés

Ordre recommandé (du plus simple au plus complexe, parallélisable) :

### MN.1 — Helper `detect_comparison_versions`

```text
feat(dashboard): M21 MN.1 detect_comparison_versions helper

- New helper queries.py::detect_comparison_versions(session_factory,
  *, settings, window_days=30) -> tuple[str, str | None]
- pilot = settings.scoring_version (source de vérité config)
- shadow = 2ᵉ scoring_version la plus fréquente trader_scores fenêtre
  30j, exclu pilot ; None si DB vide ou seul pilot calculé
- Tests : test_detect_comparison_versions_returns_pilot_only_when_db_empty
  + test_detect_comparison_versions_picks_second_most_frequent
- Cf. spec M21 §2.1 MN.1 + §4.2 D2 (fenêtre 30j) + §5.1 algo
```

### MN.2 — Refactor queries paramétrés

```text
feat(dashboard): M21 MN.2 list_scoring_comparison + aggregates accept versions

- list_scoring_comparison signature : +pilot_version: str, +shadow_version: str | None
- scoring_comparison_aggregates signature idem
- Court-circuit immédiat si shadow_version is None (single-version mode)
- ScoringComparisonRow étendu : +score_pilot/_shadow, +rank_pilot_pool/_local,
  +rank_shadow_pool/_local (alias génériques) ; legacy v1/v2 préservés
  1 release pour rétrocompat tests M12/M19
- Refactor SQL : retire littéraux 'v1' et 'v2.1' hardcoded
- Préserve M19 MH.9 ranks locaux intersection
- Cf. spec M21 §2.1 MN.2 + §5.2 algo + §6.1 DTO étendu
```

### MN.3 — Route handler

```text
feat(dashboard): M21 MN.3 /traders/scoring consume dynamic versions

- routes.py::traders_scoring_page appelle detect_comparison_versions(sf,
  settings=settings) au boot du request
- Passe pilot_version + shadow_version au template traders_scoring.html
- compute_scoring_stability_for_pool(version=pilot_version) — la
  stability M19 MH.8 reste calculée sur la pilote
- Cf. spec M21 §2.1 MN.3 + §3 user stories
```

### MN.4 — Templates dynamiques

```text
feat(dashboard): M21 MN.4 traders_scoring.html + base.html dynamiques

- traders_scoring.html : headers ({{ pilot_version | upper }} /
  {{ shadow_version | upper }}), KPI labels, bloc Cutover status
  conditionnel ({% if shadow_version %}), label cutover dynamique
  selon couple (v2.1+v2.1.1 → "Préparation cutover v2.1.1")
- Tableau headers + cells consument score_pilot/score_shadow + ranks
  locaux pilote/shadow
- Empty row message dynamique : "Aucun score {{ pilot_version }}
  {% if shadow_version %}ou {{ shadow_version }}{% endif %} persisté"
- Sidebar base.html : "Scoring v1/v2" → "Scoring comparison" (2x desktop+mobile)
- Préserve M19 MH.1 render_address macro + MH.4 tooltips + MH.8
  stability + MH.9 ranks locaux ✦ badge
- Cf. spec M21 §2.1 MN.4 + §4.4 D4 (header dynamique côté template)
```

### MN.5 — Spearman + top-N delta génériques

```text
feat(dashboard): M21 MN.5 Spearman + top-N delta paramétrés sur 2 versions

- _spearman_rank reste pure function intacte (versioning sacré algo)
- Top-N delta calcul propage dynamiquement via MN.2 list_scoring_comparison
  refactor
- Garde-fou régression : test_spearman_uses_intersection_ranks_not_pool_ranks
  M19 MH.9 préservé strict — assertion ρ=−0.5 sur fixture intentionellement
  déséquilibrée
- Cf. spec M21 §2.1 MN.5 + §5.4
```

### MN.6 — Tests adaptés + nouveaux

```text
test(dashboard): M21 MN.6 adapt + new dashboard scoring tests

- 4 tests nouveaux dans test_dashboard_scoring_route.py :
  - test_detect_comparison_versions_returns_pilot_only_when_db_empty
  - test_detect_comparison_versions_picks_second_most_frequent
  - test_dashboard_scoring_render_with_v2_1_only_pilot
  - test_dashboard_scoring_v2_1_vs_v2_1_1_shadow
- 6 tests M12 existants adaptés : passent versions explicites en params,
  consument noms génériques (rank_pilot_pool au lieu de rank_v1)
- Préservé strict : M19 stability M19 MH.8 + ranks locaux M19 MH.9
- Régression sécurité : test_dashboard_security + _m6 inchangés
- Cf. spec M21 §2.1 MN.6 + §9 test plan
```

---

## 18. Critères d'acceptation

### 18.1 Critères fonctionnels (binaires GO/NO-GO)

- [ ] **F1** — `detect_comparison_versions(sf, settings=Settings(scoring_version="v2.1"))`
  sur DB vide retourne `("v2.1", None)`.
- [ ] **F2** — Idem avec pool {v2.1: 5, v2.1.1: 3, v1: 1} retourne
  `("v2.1", "v2.1.1")` (2ᵉ plus fréquent excluant pilot).
- [ ] **F3** — `/traders/scoring` avec pool v2.1 only rend en
  single-version mode : headers `Score V2.1` présents, headers `Score V2`
  ou `Score V1` absents, bloc `Cutover status` masqué.
- [ ] **F4** — `/traders/scoring` avec pool v2.1+v2.1.1 rend dual : headers
  `Score V2.1` + `Score V2.1.1`, label `Préparation cutover v2.1.1`,
  KPI `Spearman rank(v2.1, v2.1.1)`.
- [ ] **F5** — Sidebar label `Scoring comparison` (pas `Scoring v1/v2`).
- [ ] **F6** — Empty row message dynamique : `Aucun score V2.1 persisté`
  (single) ou `Aucun score V2.1 ou V2.1.1 persisté` (dual).
- [ ] **F7** — `compute_scoring_stability_for_pool(version="v2.1")` reste
  appelée avec `version=pilot_version` détecté dynamiquement.
- [ ] **F8** — Anciens noms DTO (`row.score_v1`, `row.rank_v2`, etc.)
  encore accessibles 1 release pour rétrocompat tests M12.

### 18.2 Critères tests

- [ ] **T1** — `pytest tests/unit/test_dashboard_scoring_route.py -x --tb=short`
  retourne 0 failure (10 tests : 4 nouveaux + 6 adaptés).
- [ ] **T2** — `pytest tests/unit/test_dashboard*.py -x --tb=short`
  retourne 0 failure (~50+ tests M12+M19+M21 cumulés).
- [ ] **T3** — `ruff check . && ruff format --check . && mypy src --strict`
  retourne 0 erreur.
- [ ] **T4** — Tests régression `test_dashboard_security.py` +
  `test_dashboard_security_m6.py` verts inchangés.

### 18.3 Critères runtime smoke

- [ ] **R1** — Smoke runtime `EXECUTION_MODE=dry_run DASHBOARD_ENABLED=true
  python -m polycopy --verbose` boot < 10s sans ERROR.
- [ ] **R2** — `curl -sf http://127.0.0.1:8000/traders/scoring` retourne
  200 + HTML contient `Scoring comparison`, `Pilot version`.
- [ ] **R3** — `alembic upgrade head` retourne "no migrations to apply"
  (head reste 0010, pas de migration 0011).

### 18.4 Critères perf

- [ ] **P1** — `detect_comparison_versions` p50 < 50ms sur 10k rows
  `trader_scores`. Mesure via `pytest-benchmark` ou
  `time.perf_counter()` dans un test integration.
- [ ] **P2** — `/traders/scoring` p50 < 200ms sur DB peuplée standard
  (50 wallets × 2 versions × 10 cycles = 1000 rows). Mesure via `curl
  -w "%{time_total}\n"` 10 itérations.

### 18.5 Critères invariants préservés (zéro régression)

- [ ] **I1** — Dashboard read-only strict M4.5 : grep automatisé sur
  `@router.post`/`put`/`delete` retourne 0 nouveau callsite côté
  `dashboard/routes.py`.
- [ ] **I2** — CDN versions M6 inchangées : grep `cdn.tailwindcss.com/3.4.16`,
  `unpkg.com/htmx.org@2.0.4`, `cdn.jsdelivr.net/npm/chart.js@4.4.7`,
  `unpkg.com/lucide@0.469.0` présents inchangés dans `base.html`.
- [ ] **I3** — Versioning sacré : `git diff --stat HEAD~6 HEAD --
  src/polycopy/discovery/scoring/` retourne 0 fichiers modifiés.
- [ ] **I4** — Schema DB intact : `git diff --stat HEAD~6 HEAD --
  alembic/versions/` retourne 0 nouveau fichier. `ls
  alembic/versions/` montre la dernière revision = `0010_m17_*`.
- [ ] **I5** — Filtre `simulated` strict M17 MD.1 : non concerné, mais
  vérifié que `MyPosition` n'est pas importé dans le path scoring.
- [ ] **I6** — Aucun secret dans `traders_scoring.html` : grep
  automatisé `grep -E "POLYMARKET_PRIVATE_KEY|TELEGRAM_BOT_TOKEN|CLOB_API_SECRET|REMOTE_CONTROL_TOTP_SECRET"
  src/polycopy/dashboard/templates/traders_scoring.html` retourne
  0 lignes.
- [ ] **I7** — M19 MH.8 stability column préservée : grep
  `'🟢 stable'` dans `traders_scoring.html` retourne ≥1 match.
- [ ] **I8** — M19 MH.9 ranks locaux intersection préservés : grep
  `rank_pilot_local\|rank_shadow_local` dans `queries.py` retourne ≥2
  matches.
- [ ] **I9** — Aucune chaîne `'v1'` ou `'v2'` hardcodée dans
  `traders_scoring.html` post-merge : grep retourne 0 ligne.

### 18.6 Critères doc

- [ ] **D1** — Spec `docs/specs/M21-scoring-comparison-page-generic-refactor.md`
  mergée sur main.
- [ ] **D2** — `docs/specs/ROADMAP.md` mis à jour : MN/M21 marqué
  shipped, déplacé de la table "Roadmap restante" vers "Phase dashboard
  UX polish" avec date ship.
- [ ] **D3** — Pas de bloc CLAUDE.md M21 ajouté (refactor scope contenu,
  pas de nouvelle convention/sécurité — sauf si refactor révèle un
  invariant non-documenté qui mérite formalisation).

---

**Fin de la spec M21.**

Document actionnable seul — un implémenteur fresh qui lit M21.md doit
pouvoir merger les 6 commits sans revenir lire M12/M19 ou todo §16.
Les décisions D1-D5 sont **figées** et reposent sur inspection live du
code dashboard 2026-05-02 + observation page vide pendant le test 30j
scoring v2.1.
