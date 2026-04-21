# M6 — Dashboard 2026 (UX moderne)

Spec d'implémentation du **relooking UX** du dashboard polycopy. M4.5 a livré la tuyauterie fonctionnelle (FastAPI + HTMX + Chart.js, 8 pages read-only, localhost-only) ; M5 y a ajouté `/traders` et `/backtest`. M6 **ne touche pas au back-end** : mêmes routes, mêmes queries, mêmes garde-fous sécurité. M6 remplace la couche visuelle (Pico.css classless + polling agressif + tables brutes) par une interface qu'un utilisateur débarquant sans contexte comprend en **10 secondes**.

Source de vérité fonctionnelle : `docs/architecture.md` §Module Dashboard (inchangé). Schémas DB : `src/polycopy/storage/models.py` (inchangé). Templates existants : `src/polycopy/dashboard/templates/` (à réécrire en place). Conventions : `CLAUDE.md`. Spec de référence pour le ton et les rubriques : `specs/M4.5-dashboard.md` + `specs/M5-trader-scoring.md`.

---

## 0. Pré-requis environnement

### 0.1 Bootstrap (déjà fait)

`bash scripts/setup.sh` (idempotent). M6 n'introduit **aucune dépendance Python** (tout le "framework" visuel vit côté client via CDN). Pas de patch config structurel nécessaire — M6 ajoute 2 env vars optionnelles cosmétiques.

### 0.2 Pas d'invocation skill Polymarket

M6 est 100 % front-end + rendering Jinja. Aucun nouvel endpoint Polymarket consommé, aucune fixture API à capturer.

### 0.3 `.env` — nouvelles variables (toutes OPTIONNELLES)

| Variable env | Champ Settings | Default | Description |
|---|---|---|---|
| `DASHBOARD_THEME` | `dashboard_theme` | `"dark"` | `"dark"` (défaut) ou `"light"`. Pose le `data-theme` racine au rendering serveur. Un toggle front-end override via `localStorage` (clé `polycopy.theme`, préférence UI non sensible). |
| `DASHBOARD_POLL_INTERVAL_SECONDS` | `dashboard_poll_interval_seconds` | `5` | HTMX polling (3 s à M4.5, on passe à 5 s — assez réactif, moins de bruit log). Borne `Field(ge=2, le=60)`. |

À ajouter à `config.py` ET `.env.example` avec un commentaire "UI cosmétique, pas de sécurité associée". Ne jamais lire ces champs côté Jinja pour brancher une logique métier — ils restent visuels.

**Variables M4.5 inchangées** : `DASHBOARD_ENABLED`, `DASHBOARD_HOST`, `DASHBOARD_PORT`. Pas de renommage, pas de suppression. Backwards compat stricte : un `.env` M4.5 + M5 fonctionne sans rien changer.

### 0.4 Interdépendance avec les autres specs post-M5

M6 touche le dashboard. Deux specs en parallèle posent question :

- **M8 (dry-run réaliste)** veut un onglet PnL avec toggle "virtuel / réel". M6 livre le layout PnL (area chart + overlay drawdown + timeline milestones). M8 ajoutera ultérieurement un query param `?mode=dry_run|real` au même template sans refactor. **Recommandation d'ordre : M6 avant M8** — ça évite à M8 de devoir refaire le layout PnL une 2ᵉ fois.
- **M9 (CLI silencieux + README)** ajoute un onglet `/logs`. M6 réserve l'item dans la sidebar (stub "Logs — M9") pour que l'ajout M9 soit un simple swap de placeholder, pas un refactor de nav. **Recommandation d'ordre : M9 avant M6** pour que les captures README M9 utilisent le nouveau look. Mais si priorité UX > onboarding → inverser.
- **M7 (Telegram enrichi)** est isolé : pas d'interaction M6.

**Ordre d'implémentation recommandé** (cohérent avec le brief utilisateur) : **M9 → M6 → M8 → M7**. M9 en premier pour que les captures d'écran du README M9 montrent le nouveau look M6 ; M6 ensuite pour livrer l'UX de base ; M8 ensuite branche son toggle PnL sur le layout existant ; M7 en dernier, indépendant.

### 0.5 Critère de validation "environnement"

```bash
DASHBOARD_ENABLED=true DASHBOARD_THEME=dark python -m polycopy --dry-run
```

Doit logger identique à M5 : `dashboard_starting`, `dashboard_started`, `discovery_starting` (si M5 actif), etc. Aucun nouveau log dashboard à M6 (la partie UX n'émet pas de log spécifique — elle est purement rendering).

Un `curl -sSf http://127.0.0.1:8787/` retourne 200 HTML. Charger la page dans un navigateur → voir :

- Sidebar gauche (pas top-nav) avec 8 entrées + icônes.
- 4 KPI cards avec sparkline SVG en haut de la Home.
- Palette dark-first, couleurs sémantiques (vert=profit, rouge=perte, bleu ardoise=neutral).
- Typo Inter via Google Fonts.
- Badge `DRY-RUN` ambre si `settings.dry_run=true`.
- Footer avec version git SHA + statuts API Gamma/Data API.

Bundle JS+CSS servi au premier hit (tout via CDN) < 300 KB total (vérifiable via DevTools Network).

### 0.6 Sécurité — rappels stricts pour M6

**Invariants M4.5/M5 préservés tels quels — M6 n'a PAS le droit de les éroder** :

- **Aucun endpoint write.** Toutes les routes FastAPI restent `GET`. Aucune action user (valider un trade, éditer un wallet, toggle dry-run) n'est ajoutée à M6. Test `test_dashboard_security.py` M4.5 reste passant tel quel.
- **Bind `127.0.0.1` par défaut.** Inchangé. Pas de routage CORS, pas de preflight, pas de header `Access-Control-Allow-Origin` ajouté.
- **Aucun secret leaké.** Les snippets JS client n'ont jamais accès à `settings.polymarket_private_key` / `polymarket_funder` / `telegram_bot_token` / creds L2 / `goldsky_api_key`. Grep automatisé en test : `grep -ri "private_key\|funder\|bot_token\|api_secret\|api_passphrase" src/polycopy/dashboard/templates/` doit retourner 0 match.
- **Aucun cookie, aucun localStorage sensible.** Seule utilisation autorisée de `localStorage` : préférence UI dark/light (clé `polycopy.theme`, valeur `"dark"` | `"light"`). Aucun token, aucune session, aucune donnée DB stockée client-side.
- **Swagger/OpenAPI toujours OFF** : `docs_url=None`, `redoc_url=None`, `openapi_url=None` inchangés.
- **CDN exclusivement HTTPS** (jsdelivr, cdnjs, fonts.googleapis.com). Pas de CDN HTTP. Pas de CDN tiers non réputés.
- **CSP (optionnel, §2.8)** : header `Content-Security-Policy` conservateur ajoutable si on juge utile. À documenter comme "defense in depth", pas critique pour bind localhost.
- **Pas de nouveau secret à gérer.** M6 n'introduit aucun token API (pas de Lucide cloud, pas de Tailwind cloud auth, pas d'analytics). Si un jour c'est tentant → refuser.

---

## 1. Objectif M6 (scope exact)

**Transformer l'apparence et la lisibilité du dashboard existant** sans changer ses capacités fonctionnelles.

Livrable fonctionnel :

- Même ensemble de pages (Home, Détection, Stratégie, Exécution, Positions, PnL, Traders, Backtest) + 1 placeholder `/logs` (stub M9) + `/healthz` inchangé.
- Même routes, mêmes queries, mêmes partials HTMX, mêmes réponses JSON (Chart.js `/partials/pnl-data.json`).
- Nouvelle couche templates Jinja2 dans `src/polycopy/dashboard/templates/` (réécriture en place).
- Nouveau CSS Tailwind servi via CDN JIT + palette Radix Colors + typo Inter/Geist.
- Sparklines SVG inline, cards KPI, timeline PnL, jauge score SVG pour Traders.
- Footer avec healthcheck Gamma + Data API (ping HEAD toutes les 30 s, cached in-memory).
- Mobile responsive (tested sur iOS Safari via DevTools device mode).
- Toggle dark/light (préférence `localStorage`, défaut `DASHBOARD_THEME`).
- Lighthouse ≥ 90 performance ET accessibility ET best practices, sur la Home en dark mode, sur un profil Moto G4 3G throttling.

**Hors livrable M6** :

- Aucun ajout d'endpoint write (reporté indéfiniment, cf. M4.5 §13 "hors scope").
- Aucune nouvelle source de données (ni nouveau endpoint Polymarket, ni nouvelle table DB).
- Pas de refactor `queries.py` / `routes.py` sauf ajouts minimalistes (cf. §6.2 : un endpoint `GET /api/health-external` pour le footer).
- Pas de dark-light auto basé sur `prefers-color-scheme` (M6 v1 reste sur `DASHBOARD_THEME` env + toggle manuel ; auto detection reportable à M6.1).
- Pas d'i18n (UI en français, cohérent avec les conventions CLAUDE.md).
- Pas d'authentification (bind localhost suffit, cf. M4.5).
- Pas de PWA / installable / notifications navigateur (cf. M4.5 §13).
- Pas de refactor M1..M5 : aucun module métier n'est touché.

---

## 2. Arbitrages techniques (8 points à trancher explicitement)

### 2.1 Système de design — Tailwind CSS JIT CDN + palette Radix Colors

**Recommandation : Tailwind via CDN JIT + Radix Colors via variables CSS hand-pickées.**

Justification :

- Tailwind JIT CDN (`<script src="https://cdn.tailwindcss.com">`) rend les classes utility **à la volée côté navigateur** sans build step. ~80 KB gzipped. Fait le job à M6 (on n'a pas besoin de purging — on le paye au premier load une fois, puis cache agressif).
- Classes utility directement dans les templates Jinja → pas de fichier CSS custom à maintenir.
- Config inline `tailwind.config = { ... }` dans un `<script>` du `base.html` pour déclarer la palette Radix + les tokens custom (spacing, breakpoints).
- Radix Colors (`@radix-ui/colors`) fournit des échelles sémantiques pensées pour l'accessibilité (contrast-ratio validé). **On n'importe PAS le package npm** — on hardcode 12 variables CSS dans `base.html` (les 3 teintes utiles : `slate` pour neutral, `green` pour profit, `red` pour loss, `amber` pour warning).

Pros :

- Zéro build step (règle non-négociable conservée depuis M4.5).
- Utility-first → pas de file-level CSS bloat, facile de refactorer.
- Radix = accessibilité WCAG AA par construction.
- Compatible avec HTMX : le swap HTMX préserve les classes car elles sont déclarées dans le HTML, pas dynamiques.

Cons :

- Taille JIT ~80 KB (vs Pico ~10 KB). Mitigé : cache HTTP longue durée, 1 seul download par user.
- Le CDN JIT de Tailwind v3 n'est PAS recommandé pour prod **publique** (latence, dependency externe). Pour un dashboard local single-user, parfaitement acceptable.

**Alternatives écartées** :

- **Pico.css v2 + custom variables CSS** : la plus petite upgrade possible — garder Pico classless et juste surcharger 10 variables CSS. Trop bridé pour livrer des cards KPI, jauges, timeline. **Rejeté** car casse le "comprendre en 10 s".
- **Water.css / Simple.css** : mêmes limites que Pico.
- **Tailwind build-time (postcss + purgecss)** : meilleur rendu en prod mais introduit `node_modules/` → **interdit** par contrainte §0.4 "Zéro build step" explicite.
- **UnoCSS CDN** : alternative plus légère à Tailwind JIT mais moins documentée, plus risqué.
- **Pure CSS custom** : réinventer la roue, pénible à maintenir sur 8 pages.

**Décision tranchée** : Tailwind CDN JIT. Doc du trade-off perf dans §14.

### 2.2 Typographie — Inter via Google Fonts, fallback system-ui

**Recommandation : Inter (Google Fonts) + system-ui fallback.**

- Inter est la typo "SaaS moderne 2026" par excellence (fintech, dashboards).
- Chargement via `<link rel="preconnect"> + <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">`.
- `display=swap` → pas de FOIT, la page reste lisible pendant le download (~30 KB).
- Stack CSS : `font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;`.

**Alternatives écartées** :

- **Geist Sans (Vercel)** : aussi bien que Inter mais CDN moins standard (fonts.vercel.com) ; la stabilité de l'URL pour usage libre est moins documentée. Reportée à M6.1 si Inter déçoit.
- **System-ui seul** : réduit la dép réseau mais perd la cohérence visuelle cross-OS. Sur Windows (stack principale user), system-ui=Segoe UI = vieillot.
- **Self-host Inter dans `static/vendor/`** : alternative offline. Pas fait à M6 (CDN suffit — cohérent avec M4.5). Vendoring reportable à M6.1 si jsdelivr/Google Fonts deviennent flakys.

### 2.3 Icônes — Lucide Icons via unpkg ESM

**Recommandation : Lucide v0.x via `https://unpkg.com/lucide@latest`.**

- Lucide = fork de Feather, maintenu activement, SVG purs, tree-shakeable.
- Init JS minimal : `<script type="module"> import { createIcons, icons } from 'https://unpkg.com/lucide@latest/dist/esm/lucide.js'; createIcons(); </script>`.
- Dans les templates : `<i data-lucide="arrow-up"></i>` → remplacé par le SVG au chargement DOM.
- Côté HTMX swap : `lucide.createIcons()` re-appelé dans un `htmx:afterSwap` listener (10 lignes de JS total).

**Alternatives écartées** :

- **Material Icons / Font Awesome** : bundle plus lourd (~100 KB de polices).
- **Heroicons direct SVG inline** : pas de CDN officiel, il faut copy-paste les SVG dans les templates. Lourd en lignes.
- **Emoji natif Unicode** : utilisé ponctuellement en accentuation mais pas comme icônes primaires (rendu inconsistant cross-OS).

### 2.4 Sparklines et jauges — SVG inline hand-crafted

**Recommandation : SVG inline dans les templates Jinja, zéro lib.**

- Pour les 4 KPI cards Home : sparkline 7 jours calculée côté serveur (cf. `pnl_report.py` déjà en place qui génère un SVG similaire) → inline dans le HTML. Aucun JS requis pour le rendu.
- Pour la jauge score /traders : arc SVG circulaire, gradient linéaire `green → amber → red`, `stroke-dasharray` calculé via Jinja filter `score_to_dasharray`. Pas de JS.
- Pour le chart PnL Chart.js : inchangé depuis M4.5 (Chart.js 4.x via CDN), juste restylé (dataset gradient fill, drawdown overlay, annotations kill switch).

**Alternatives écartées** :

- **D3.js** : overkill pour des sparklines statiques. Ajoute 150 KB.
- **Chart.js pour les sparklines** : rendu canvas, moins performant pour 4 cards + HTMX swap. Trop lourd.
- **uPlot** : alternative légère à Chart.js mais introduit une 2ᵉ lib — garder la cohérence avec Chart.js déjà en place.

### 2.5 Layout — sidebar gauche fixe, main scrollable, responsive mobile

**Recommandation : flex layout sidebar 240px + main `flex-1 overflow-y-auto`.**

- Desktop ≥ 1024 px : sidebar visible, `<nav>` vertical.
- Tablet 640-1024 px : sidebar réduite à 64 px (icônes seules, labels cachés). Tooltip hover.
- Mobile < 640 px : sidebar cachée, bouton hamburger en haut, overlay coulissante via `<details>` natif ou toggle class Tailwind (`hidden md:flex`).

Pros :

- Pas de JS custom pour le menu mobile : `<details>` est natif, accessible, SEO-friendly.
- Sidebar fixe → navigation constante, familier aux utilisateurs de Linear / Vercel / Grafana.

**Alternatives écartées** :

- **Top nav horizontal** (M4.5 actuel) : casse sur > 8 items, difficile pour les labels français ("Détection", "Exécution", "Positions"), peu scalable.
- **Bottom tab bar mobile (iOS-style)** : trop mobile-first pour un dashboard supervision dont l'usage primaire est desktop.
- **Menu burger desktop** : moins efficace que la sidebar persistante.

### 2.6 Rafraîchissement temps réel — HTMX polling 5 s + backoff visibility API

**Recommandation : garder HTMX polling, passer de 3 s (M4.5) à 5 s (M6), ajouter un garde `hx-trigger="every 5s[document.visibilityState==='visible']"`.**

- `DASHBOARD_POLL_INTERVAL_SECONDS=5` (cf. §0.3) — compromis bruit log / fraîcheur perçue.
- Si l'onglet n'est pas visible : le modifier `[document.visibilityState==='visible']` suspend les requêtes. Réduit la charge serveur inutile.
- Infinite scroll pour les tables (§2.7) : `hx-trigger="revealed"` chargé à la demande.

**Alternatives écartées** :

- **SSE** : ajoute une route FastAPI dédiée, complique le lifecycle uvicorn, gain perçu faible.
- **WebSocket** : idem + casse le contrat "read-only GET-only".

### 2.7 Tables → rows + infinite scroll

**Recommandation : transformer les 5 tables brutes (détections, stratégie, ordres, positions, traders) en "rows" stackables avec pagination HTMX revealed.**

- Chaque row = une card mini (2 lignes de hauteur sur desktop) avec badges colorés, icône side (BUY/SELL), timestamp relatif ("il y a 2 min" calculé côté serveur via un Jinja filter `humanize_dt`), slug marché tronqué.
- Pagination : `GET /partials/detections-rows?before=<id>&limit=50`. Le dernier row contient `<div hx-trigger="revealed" hx-get="/partials/detections-rows?before=<last_id>&limit=50" hx-swap="outerHTML">` → chargement à l'apparition dans le viewport.
- Cap dur : 500 rows chargés dans le DOM (au-delà, bouton "Charger plus" manuel pour éviter de péter la RAM navigateur sur des runs longs).

**Alternatives écartées** :

- **Table HTML classique + pagination numérique** : plus lourde visuellement, moins moderne, pénible en mobile.
- **DataTables / AG Grid** : sur-ingéniorage pour du read-only simple.
- **Virtual scrolling** : nécessite JS custom (~200 lignes), gain marginal sur des tables de 500 rows.

### 2.8 Contenu sémantique — couleurs, formatage, icônes

**Palette sémantique** (variables CSS dans `base.html`) :

| Variable | Rôle | Dark default | Light default |
|---|---|---|---|
| `--color-bg` | Fond global | `#0b1220` (slate-12) | `#f6f8fa` (slate-1) |
| `--color-surface` | Cards / nav | `#111827` (slate-11) | `#ffffff` (white) |
| `--color-text` | Texte primaire | `#e6edf3` (slate-2) | `#1f2933` (slate-11) |
| `--color-muted` | Texte secondaire | `#9ba7b8` (slate-6) | `#55606e` (slate-7) |
| `--color-border` | Bordures | `#1f2933` (slate-10) | `#d9dee3` (slate-4) |
| `--color-profit` | PnL positif | `#30a46c` (green-9) | `#208354` (green-10) |
| `--color-loss` | PnL négatif | `#e5484d` (red-9) | `#cd2b31` (red-10) |
| `--color-neutral` | Info neutre | `#6ea8fe` (blue-9) | `#3451b2` (blue-10) |
| `--color-warning` | Attention | `#f5a623` (amber-9) | `#ad5700` (amber-10) |
| `--color-critical` | Kill switch / erreur | `#c92a2a` (red-11) | `#a81d1d` (red-11) |

**Formatage humain** (filtres Jinja dans `src/polycopy/dashboard/jinja_filters.py`, nouveau fichier, §6.3) :

- `format_usd(1234.56)` → `"$1.2k"` (si ≥ 1000), `"$0.45"` (si < 1).
- `format_size(3.5)` → `"3.50"` (2 décimales pour les outcome tokens).
- `format_pct(0.0392)` → `"+3.9%"` (vert) / `"-1.2%"` (rouge) avec span coloré.
- `humanize_dt(datetime)` → `"il y a 2 min"`, `"hier à 14:32"`, `"il y a 3 jours"`. Unit: seconds → days. Cap à 30 jours puis ISO date.
- `short_hash(0xabcdef...)` → `"0xabc…def"` (4 caractères chaque côté).
- `wallet_label(wallet_obj)` → `label` si défini, sinon `short_hash(address)`.

**Icônes Lucide usage** :

| Contexte | Icône |
|---|---|
| BUY side | `arrow-up-circle` (vert) |
| SELL side | `arrow-down-circle` (rouge) |
| Status APPROVED | `check-circle` (vert) |
| Status REJECTED | `x-circle` (rouge pâle) |
| Status FILLED | `check-check` (vert) |
| Status SIMULATED | `flask-conical` (bleu) |
| Kill switch | `shield-off` (rouge saturé) |
| Wallet | `wallet` |
| Trader pinned | `pin` |
| Trader shadow | `eye` |
| Trader active | `play-circle` (vert) |
| Trader paused | `pause-circle` (ambre) |
| Discovery | `search` |
| Monitoring | `activity` |
| Logs (M9 stub) | `file-text` |

**Alternatives écartées** :

- **Couleurs hardcodées en Tailwind (`bg-green-500`)** : perd la sémantique. Si demain on ajoute un mode "high-contrast accessibility", on doit chercher dans 30 fichiers. Variables CSS → 1 fichier.
- **Icônes emoji Unicode seules** : rendu inconsistant (🟢 sur Windows ≠ 🟢 sur macOS).

### 2.9 CSP — header optionnel defense in depth (reporté à M6.1)

Un Content-Security-Policy strict (`default-src 'self'; script-src 'self' cdn.jsdelivr.net unpkg.com fonts.googleapis.com; ...`) est une bonne pratique. À M6 on documente la contrainte CSP "acceptable" mais on ne l'implémente pas (complexifie le debug CDN, risque de casser le JIT Tailwind qui utilise `eval`-like). Reporté à M6.1 ou M10 (security audit).

---

## 3. Arborescence du module — `src/polycopy/dashboard/`

Réécriture en place, pas de nouveau sous-module. Fichiers ajoutés :

```
src/polycopy/dashboard/
├── __init__.py                     (inchangé)
├── dtos.py                         (inchangé)
├── queries.py                      (+ 2 fonctions : get_external_health, get_app_version)
├── routes.py                       (+ 2 routes : /api/health-external, /api/version)
├── server.py                       (inchangé)
├── orchestrator.py                 (inchangé)
├── middleware.py                   (inchangé)
├── health_check.py                 NOUVEAU : ping Gamma + Data API (HEAD, cached 30 s)
├── jinja_filters.py                NOUVEAU : format_usd, humanize_dt, format_pct, etc.
├── templates/
│   ├── base.html                   RÉÉCRIT (layout sidebar + dark-first)
│   ├── macros.html                 RÉÉCRIT (kpi_card, sparkline_svg, badge, score_gauge, row_card)
│   ├── home.html                   RÉÉCRIT (KPIs + derniers trades + discovery status)
│   ├── detections.html             RÉÉCRIT (rows + infinite scroll)
│   ├── strategy.html               RÉÉCRIT
│   ├── orders.html                 RÉÉCRIT
│   ├── positions.html              RÉÉCRIT
│   ├── pnl.html                    RÉÉCRIT (area chart + overlay drawdown + timeline milestones)
│   ├── traders.html                RÉÉCRIT (jauge score + dépliables)
│   ├── backtest.html               RÉÉCRIT (card simple)
│   ├── logs_stub.html              NOUVEAU : placeholder M9 "Logs — arrive en M9"
│   └── partials/
│       ├── kpis.html               RÉÉCRIT
│       ├── detections_rows.html    RÉÉCRIT (row_card macro)
│       ├── strategy_rows.html      RÉÉCRIT
│       ├── orders_rows.html        RÉÉCRIT
│       ├── positions_rows.html     RÉÉCRIT
│       ├── traders_rows.html       RÉÉCRIT
│       ├── pnl_data.html           (inchangé : embed JSON Chart.js)
│       ├── external_health.html    NOUVEAU : fragment footer health Gamma/Data API
│       └── discovery_summary.html  NOUVEAU : fragment Home "Discovery status"
└── static/
    ├── dashboard.css               RÉÉCRIT (overrides Tailwind minimalistes, palette CSS vars, keyframes, scrollbar)
    └── dashboard.js                NOUVEAU : theme toggle + HTMX hooks (lucide re-init, chart update)
```

**Pas de nouveau fichier Python** sauf `health_check.py` et `jinja_filters.py`. Pas de refactor `routes.py` / `queries.py` massif — juste 2 ajouts §6.

---

## 4. API externes — health check léger (new)

### 4.1 `health_check.py` — le SEUL appel réseau ajouté par M6

M6 ajoute **un** point de ping pour le footer : "Gamma ✅ / Data API ✅ / dernière vérif il y a 15s". Aucun autre appel réseau.

```python
class ExternalHealthChecker:
    """Ping Gamma + Data API toutes les N secondes, cache in-memory."""

    GAMMA_URL = "https://gamma-api.polymarket.com/markets?limit=1"
    DATA_API_URL = "https://data-api.polymarket.com/trades?limit=1"
    CACHE_TTL_SECONDS = 30.0
    TIMEOUT_SECONDS = 3.0

    def __init__(self, http_client: httpx.AsyncClient) -> None: ...

    async def check(self) -> ExternalHealthSnapshot:
        """Retourne dernier snapshot du cache (force refresh si TTL expiré)."""
        ...
```

- Appels **HEAD** (pas GET) pour minimiser le payload. Si `HEAD` non supporté côté Polymarket → fallback `GET` avec `limit=1`.
- Timeout 3 s. Si dépassé → snapshot avec `status='degraded'` + `error_type='timeout'`.
- Cache TTL 30 s → max 2 calls/min, transparent vs les ~20 req/min du watcher.
- **Pas de retry / backoff**. Un health check qui rate remonte comme "degraded" — c'est le comportement voulu.

### 4.2 DTO `ExternalHealthSnapshot`

```python
class ExternalHealthSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    gamma_status: Literal["ok", "degraded", "unknown"]
    gamma_latency_ms: int | None
    data_api_status: Literal["ok", "degraded", "unknown"]
    data_api_latency_ms: int | None
    checked_at: datetime
```

### 4.3 Rate limit

`CACHE_TTL_SECONDS=30` ⇒ pic 4 calls/min (2 endpoints × 2 si cache expiré pile). Aucun risque vs ~100 req/min documenté.

---

## 5. Storage (inchangé à M6)

M6 ne touche pas aux modèles SQLAlchemy, aux repositories, aux migrations Alembic. Aucune nouvelle table, aucune nouvelle colonne.

**Rappel des tables lues à M6** (read-only, inchangé depuis M5) :

- `target_traders` (status, pinned, score, last_scored_at) — page Traders + Home.
- `detected_trades` — page Détection.
- `strategy_decisions` — page Stratégie.
- `my_orders` — page Exécution.
- `my_positions` — page Positions.
- `pnl_snapshots` — page PnL (Chart.js).
- `trader_scores` — sparkline historique Traders (M5).
- `trader_events` — timeline milestones PnL + Discovery status Home.

Si M8 ajoute une colonne `my_orders.realistic_fill` (TBD), M6 n'en dépend pas (les templates M6 se contentent de `status`).

---

## 6. DTOs, queries, routes (extensions minimales)

### 6.1 Nouvelles DTOs `src/polycopy/dashboard/dtos.py`

```python
class KpiCard(BaseModel):
    """DTO pour une card KPI Home."""
    model_config = ConfigDict(frozen=True)

    title: str
    value: str                     # déjà formaté ("$1.2k", "3", "12 %")
    delta: str | None              # "+3.9 %" ou None si pas applicable
    delta_sign: Literal["positive", "negative", "neutral"] | None
    sparkline_points: list[tuple[datetime, float]]  # max 30 points
    icon: str                      # nom Lucide (ex: "dollar-sign")


class DiscoveryStatus(BaseModel):
    """Fragment 'Discovery status' Home."""
    model_config = ConfigDict(frozen=True)

    enabled: bool
    active_count: int
    shadow_count: int
    paused_count: int
    pinned_count: int
    last_cycle_at: datetime | None
    promotions_24h: int
    demotions_24h: int


class PnlMilestone(BaseModel):
    """Marqueur temporel dans la timeline PnL."""
    model_config = ConfigDict(frozen=True)

    at: datetime
    event_type: Literal["first_trade", "first_fill", "kill_switch", "trader_promoted", "cycle_completed"]
    label: str
    wallet_address: str | None
    market_slug: str | None
```

### 6.2 Extensions `queries.py`

Nouvelles fonctions :

```python
async def get_home_kpis(session_factory, settings) -> list[KpiCard]:
    """Construit les 4 cards Home : total_usdc, drawdown, positions ouvertes, trades 24h."""
    ...


async def get_discovery_status(session_factory) -> DiscoveryStatus:
    """Agrégat target_traders + trader_events 24h (si M5 actif, sinon DiscoveryStatus(enabled=False))."""
    ...


async def get_pnl_milestones(session_factory, since: datetime) -> list[PnlMilestone]:
    """Extrait 5-10 moments clés (premier fill, kill switch, promotions M5) depuis my_orders + trader_events."""
    ...


async def get_app_version() -> str:
    """Retourne git SHA tronqué (via `git rev-parse --short HEAD` au boot, cached)."""
    ...
```

**Aucune query M4.5/M5 existante n'est modifiée**. On ajoute, on ne refactor pas. Tests M4.5 `test_dashboard_queries.py` passent toujours.

### 6.3 Extensions `routes.py`

```python
@router.get("/logs", response_class=HTMLResponse)
async def logs_stub(request: Request) -> HTMLResponse:
    """Stub M9 — template qui dit 'arrive en M9'."""
    ...


@router.get("/api/health-external", response_class=HTMLResponse)
async def external_health_partial(request: Request) -> HTMLResponse:
    """Fragment HTMX footer — ping Gamma + Data API, cached 30 s."""
    ...


@router.get("/api/version", response_class=JSONResponse)
async def version_json(request: Request) -> JSONResponse:
    """{'version': '0.6.0-<git_sha>'}. Consommé par le footer client-side."""
    ...
```

Toujours `GET`. Aucun POST/PUT/DELETE/PATCH. Test `test_dashboard_security.py` M4.5 couvre déjà cette invariante et continue à passer.

### 6.4 Jinja filters — `src/polycopy/dashboard/jinja_filters.py`

```python
def format_usd(value: float | None) -> str: ...
def format_size(value: float) -> str: ...
def format_pct(value: float, with_sign: bool = True) -> str: ...
def humanize_dt(dt: datetime | None) -> str: ...
def short_hash(h: str, width: int = 4) -> str: ...
def wallet_label(trader) -> str: ...
def score_to_dasharray(score: float, circumference: float = 339.292) -> str: ...
def side_icon(side: str) -> str: ...         # "arrow-up-circle" ou "arrow-down-circle"
def status_badge_class(status: str) -> str:  # "badge-ok" / "badge-rejected" / etc.
    ...
```

Enregistrés au boot FastAPI :

```python
templates = Jinja2Templates(directory="src/polycopy/dashboard/templates")
templates.env.filters.update({
    "format_usd": format_usd,
    "humanize_dt": humanize_dt,
    # ...
})
```

Tests unit §9.2 couvrent chaque filter avec 5-10 cases boundary.

---

## 7. Templates — spec détaillée par page

### 7.1 `base.html` (layout racine)

Structure :

```html
<!doctype html>
<html lang="fr" data-theme="{{ theme }}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>polycopy — {% block title %}dashboard{% endblock %}</title>
  <link rel="icon" href="data:image/svg+xml;utf8,...">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = { theme: { extend: { colors: { /* mapping Radix */ } } } };
  </script>
  <link rel="stylesheet" href="/static/dashboard.css">
  <script src="https://cdn.jsdelivr.net/npm/htmx.org@2.0.4/dist/htmx.min.js" defer></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js" defer></script>
  <script type="module" src="https://unpkg.com/lucide@latest/dist/esm/lucide.js"></script>
  <script src="/static/dashboard.js" defer></script>
</head>
<body class="bg-[var(--color-bg)] text-[var(--color-text)] antialiased">
  <div class="flex min-h-screen">
    <aside id="sidebar" class="w-60 shrink-0 border-r border-[var(--color-border)]">...</aside>
    <main class="flex-1 min-w-0 flex flex-col">
      {% block breadcrumb %}{% endblock %}
      <div class="flex-1 overflow-y-auto px-6 py-4">{% block content %}{% endblock %}</div>
      {% include "partials/external_health.html" %}
    </main>
  </div>
</body>
</html>
```

**Palette et tokens** déclarés inline dans `<style>` CSS variables (cf. §2.8 table). Dark par défaut.

**Badge DRY-RUN** : si `settings.dry_run=true`, badge ambre `class="badge-warning"` dans la sidebar en haut, sous le logo.

### 7.2 `home.html` — 4 KPIs + Discovery status + derniers trades

Layout :

```
┌─────────────────────────────────────────────────────────────────┐
│ [Total USDC]  [Drawdown]  [Positions ouvertes]  [Trades 24h]   │
│ $1.2k         -3.2 %       3 ($45)               12             │
│ sparkline     sparkline    sparkline             sparkline      │
├─────────────────────────────────────────────────────────────────┤
│ Discovery                                                        │
│ 🟢 3 actifs · 👁 2 shadow · ⏸ 1 paused · 📌 2 pinned           │
│ Dernier cycle : il y a 2 h · 0 promotions · 0 demotions 24h     │
├─────────────────────────────────────────────────────────────────┤
│ Derniers trades détectés                                         │
│ ▸ 0xabc… a acheté "Trump 2028" à 0.34  — il y a 2 min          │
│ ▸ 0xdef… a vendu  "NYC Mayor"  à 0.72  — il y a 8 min          │
│ ▸ ...                                                            │
└─────────────────────────────────────────────────────────────────┘
```

4 macros `kpi_card` (grid Tailwind `grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4`). HTMX polling `hx-trigger="every 5s[document.visibilityState==='visible']"` sur `/partials/kpis`.

`partial kpi_card` :

```html
<article class="rounded-xl border border-[var(--color-border)] bg-[var(--color-surface)] p-4">
  <header class="flex items-center justify-between">
    <span class="text-sm text-[var(--color-muted)]">{{ card.title }}</span>
    <i data-lucide="{{ card.icon }}" class="w-4 h-4 text-[var(--color-muted)]"></i>
  </header>
  <p class="mt-2 text-2xl font-semibold">{{ card.value }}</p>
  {% if card.delta %}
    <p class="text-sm {% if card.delta_sign == 'positive' %}text-[var(--color-profit)]{% elif card.delta_sign == 'negative' %}text-[var(--color-loss)]{% else %}text-[var(--color-muted)]{% endif %}">
      {{ card.delta }}
    </p>
  {% endif %}
  {{ macros.sparkline_svg(card.sparkline_points, width=240, height=32) }}
</article>
```

### 7.3 `detections.html`, `strategy.html`, `orders.html`, `positions.html` — rows pattern

Exemple `detections_rows.html` row :

```html
<article class="flex items-center gap-4 p-3 border-b border-[var(--color-border)] hover:bg-[var(--color-surface)]">
  <i data-lucide="{{ trade.side | side_icon }}" class="w-5 h-5 {% if trade.side == 'BUY' %}text-[var(--color-profit)]{% else %}text-[var(--color-loss)]{% endif %}"></i>
  <div class="flex-1 min-w-0">
    <p class="text-sm font-medium truncate">{{ trade.slug or trade.condition_id | short_hash }}</p>
    <p class="text-xs text-[var(--color-muted)]">
      {{ trade.target_wallet | short_hash }} · {{ trade.size | format_size }} shares @ {{ trade.price | format_usd }}
    </p>
  </div>
  <div class="text-right">
    <p class="text-sm">{{ trade.usdc_size | format_usd }}</p>
    <p class="text-xs text-[var(--color-muted)]">{{ trade.timestamp | humanize_dt }}</p>
  </div>
</article>
```

Au bas de la liste :

```html
<div hx-get="/partials/detections-rows?before={{ last_id }}&limit=50"
     hx-trigger="revealed"
     hx-swap="outerHTML"
     hx-indicator=".htmx-indicator">
  <div class="htmx-indicator text-center py-4 text-[var(--color-muted)]">Chargement…</div>
</div>
```

Cap à 500 rows total → au-delà, on rend un `<button hx-get="..." hx-trigger="click">Charger plus</button>` à la place.

### 7.4 `pnl.html` — area chart + overlay drawdown + timeline milestones

Structure :

```
┌─────────────────────────────────────────────────────────────────┐
│ PnL · [24h] [7j] [30j]                       ● réel ○ dry-run  │
├─────────────────────────────────────────────────────────────────┤
│ ┌──────────────────────────────────────────────────────────┐    │
│ │              area chart USDC avec gradient fill          │    │
│ │  ▁▃▇▅▂▄▆█▄  overlay drawdown (semi-transparent rouge)    │    │
│ │  🔴 kill switch                                            │    │
│ └──────────────────────────────────────────────────────────┘    │
├─────────────────────────────────────────────────────────────────┤
│ Milestones                                                        │
│ ● 2026-04-10 Premier trade détecté  (wallet 0xabc…)             │
│ ● 2026-04-12 Premier fill           (market "Trump 2028")       │
│ ● 2026-04-14 Trader promu           (wallet 0xdef…)             │
│ ● 2026-04-17 Kill switch déclenché  (drawdown 20.3 %)           │
└─────────────────────────────────────────────────────────────────┘
```

Chart.js config (JS dans `pnl.html`) :

```js
const cfg = {
  type: 'line',
  data: {
    labels: data.timestamps.map(t => new Date(t)),
    datasets: [
      {
        label: 'Total USDC',
        data: data.total_usdc,
        borderColor: 'rgb(48, 164, 108)',
        backgroundColor: (ctx) => {
          const g = ctx.chart.ctx.createLinearGradient(0, 0, 0, 240);
          g.addColorStop(0, 'rgba(48, 164, 108, 0.35)');
          g.addColorStop(1, 'rgba(48, 164, 108, 0)');
          return g;
        },
        fill: true,
        tension: 0.25,
        yAxisID: 'y',
      },
      {
        label: 'Drawdown %',
        data: data.drawdown_pct,
        borderColor: 'rgba(229, 72, 77, 0.6)',
        backgroundColor: 'rgba(229, 72, 77, 0.08)',
        fill: true,
        tension: 0.25,
        yAxisID: 'y1',
      },
    ],
  },
  options: {
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { display: true }, annotation: { annotations: data.kill_switch_markers || [] } },
    scales: { y: {...}, y1: { position: 'right', reverse: true, max: 0, min: -50 }, x: { type: 'time' } },
  },
};
```

Toggle `[24h] [7j] [30j]` → 3 liens `<a href="/pnl?since=24h">` rechargent la page avec un query param. Pas de JS client pour switcher — swap serveur.

**Hook M8 (dry-run réaliste)** : le toggle "réel / dry-run" est préparé ici mais non-actif à M6 (les 2 séries sont identiques pour l'instant). M8 branchera un `?mode=real|dry_run|both` en filtre SQL sur `is_dry_run`. Zéro refactor côté M6 → M8.

### 7.5 `traders.html` — jauge score + dépliables

Chaque wallet rendu comme :

```html
<details class="rounded-xl border border-[var(--color-border)] mb-2">
  <summary class="flex items-center gap-4 p-4 cursor-pointer">
    <!-- Jauge SVG circulaire -->
    <svg viewBox="0 0 120 120" class="w-14 h-14">
      <circle cx="60" cy="60" r="54" fill="none" stroke="var(--color-border)" stroke-width="8"/>
      <circle cx="60" cy="60" r="54" fill="none"
              stroke="url(#gradient-{{ trader.wallet_address }})"
              stroke-width="8"
              stroke-dasharray="{{ trader.score | score_to_dasharray }}"
              stroke-linecap="round"
              transform="rotate(-90 60 60)"/>
      <text x="60" y="68" text-anchor="middle" class="text-xl font-semibold">{{ '%.2f' | format(trader.score) }}</text>
    </svg>
    <div class="flex-1">
      <p class="font-medium">{{ trader | wallet_label }}
        {% if trader.pinned %}<i data-lucide="pin" class="w-4 h-4 inline text-[var(--color-warning)]"></i>{% endif %}
      </p>
      <p class="text-xs text-[var(--color-muted)]">{{ trader.wallet_address | short_hash }}</p>
    </div>
    <span class="badge badge-{{ trader.status }}">{{ trader.status }}</span>
    <!-- sparkline historique score -->
    {{ macros.sparkline_svg(trader.score_history, width=120, height=28) }}
  </summary>
  <div class="border-t border-[var(--color-border)] p-4 text-sm">
    <dl class="grid grid-cols-2 md:grid-cols-4 gap-4">
      <div><dt class="text-[var(--color-muted)]">Win rate</dt><dd>{{ trader.metrics.win_rate | format_pct }}</dd></div>
      <div><dt class="text-[var(--color-muted)]">ROI</dt><dd>{{ trader.metrics.realized_roi | format_pct }}</dd></div>
      <div><dt class="text-[var(--color-muted)]">HHI</dt><dd>{{ '%.2f' | format(trader.metrics.herfindahl_index) }}</dd></div>
      <div><dt class="text-[var(--color-muted)]">Volume</dt><dd>{{ trader.metrics.total_volume_usd | format_usd }}</dd></div>
    </dl>
    {% if trader.last_scored_at %}
      <p class="mt-2 text-xs text-[var(--color-muted)]">Dernier scoring : {{ trader.last_scored_at | humanize_dt }} (v{{ trader.scoring_version }})</p>
    {% endif %}
  </div>
</details>
```

Gradient par wallet pour le stroke (vert → ambre → rouge) — un `<defs><linearGradient>` partagé en haut du template, `id` référencé via `wallet_address` pour unicité.

### 7.6 `backtest.html`, `logs_stub.html`

- `backtest.html` : card simple, "Statut : rapport disponible / non généré", lien vers le fichier HTML si présent, instruction CLI sinon. Pas de refactor.
- `logs_stub.html` : placeholder "Logs — Cette page arrive en M9. En attendant, consulte `~/.polycopy/logs/polycopy.log` ou lance `tail -f ...`." Visible dans la sidebar avec l'icône `file-text` grisée. Empêche l'accès à une page 404 perçue comme un bug.

### 7.7 Footer `external_health.html`

```html
<footer class="border-t border-[var(--color-border)] px-6 py-2 text-xs text-[var(--color-muted)] flex items-center justify-between"
        hx-get="/api/health-external"
        hx-trigger="load, every 30s[document.visibilityState==='visible']"
        hx-swap="outerHTML">
  <span>polycopy v{{ version }}</span>
  <span class="flex items-center gap-4">
    <span>Gamma
      <i data-lucide="{% if snapshot.gamma_status == 'ok' %}check-circle{% else %}alert-circle{% endif %}"
         class="inline w-3 h-3 {% if snapshot.gamma_status == 'ok' %}text-[var(--color-profit)]{% else %}text-[var(--color-warning)]{% endif %}"></i>
      {% if snapshot.gamma_latency_ms %}({{ snapshot.gamma_latency_ms }}ms){% endif %}
    </span>
    <span>Data API {{ ... même pattern ... }}</span>
    <span>Vérifié {{ snapshot.checked_at | humanize_dt }}</span>
  </span>
</footer>
```

---

## 8. JavaScript client — `static/dashboard.js` (~60 lignes)

Contenu minimal :

```js
(function () {
  const THEME_KEY = 'polycopy.theme';
  const stored = localStorage.getItem(THEME_KEY);
  if (stored === 'light' || stored === 'dark') {
    document.documentElement.setAttribute('data-theme', stored);
  }

  document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-theme-toggle]');
    if (!btn) return;
    const current = document.documentElement.getAttribute('data-theme') || 'dark';
    const next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem(THEME_KEY, next);
  });

  document.addEventListener('htmx:afterSwap', () => {
    if (window.lucide && typeof lucide.createIcons === 'function') {
      lucide.createIcons();
    }
  });

  // Fetch de version (affichée au footer)
  fetch('/api/version').then(r => r.json()).then(d => {
    const el = document.querySelector('[data-app-version]');
    if (el) el.textContent = d.version;
  }).catch(() => {});
})();
```

Zéro dépendance JS custom. Lighthouse passe.

---

## 9. Tests

### 9.1 Arborescence

```
tests/
├── fixtures/
│   └── (M1..M5 existants — aucun ajout)
├── unit/
│   ├── (M1..M5 existants — aucun diff)
│   ├── test_dashboard_jinja_filters.py       NOUVEAU
│   ├── test_dashboard_health_check.py        NOUVEAU
│   ├── test_dashboard_templates_regression.py NOUVEAU (snapshot light)
│   ├── test_dashboard_queries_m6.py          NOUVEAU (KPIs, discovery status, milestones)
│   └── test_dashboard_security_m6.py         NOUVEAU (re-vérifier no leak, no write, Tailwind classes rendues)
└── integration/
    └── (inchangé)
```

### 9.2 `test_dashboard_jinja_filters.py`

- `format_usd(1234.56)` → `"$1.2k"`.
- `format_usd(0.01)` → `"$0.01"`.
- `format_usd(None)` → `"—"`.
- `format_pct(0.0392)` → `"+3.9%"`.
- `format_pct(-0.012)` → `"-1.2%"`.
- `humanize_dt(now - 30s)` → `"il y a 30 s"`.
- `humanize_dt(now - 3h)` → `"il y a 3 h"`.
- `humanize_dt(now - 30d)` → ISO date.
- `humanize_dt(None)` → `"—"`.
- `short_hash("0xabcdef1234567890")` → `"0xabcd…7890"`.
- `score_to_dasharray(0.5)` → `"169.646 169.646"` (circonférence 339.292).
- `side_icon("BUY")` → `"arrow-up-circle"`.

### 9.3 `test_dashboard_health_check.py` (respx)

- Mock Gamma 200 + Data API 200 → snapshot `ok`/`ok` avec latency_ms > 0.
- Mock Gamma timeout → snapshot `degraded` pour Gamma, `ok` pour Data API.
- Cache TTL 30 s : 2 calls en < 30 s → 1 seul call réseau réel.
- Refresh après TTL → nouveau call.

### 9.4 `test_dashboard_templates_regression.py` (ASGITransport)

Snapshot light — on vérifie la **présence** des tokens Tailwind + structure, pas le pixel exact :

- `GET /home` contient `class="grid"`, `data-lucide="dollar-sign"`, `data-lucide="activity"`, `DRY-RUN` si dry_run, au moins 4 `kpi_card`.
- `GET /detections` contient `hx-trigger="revealed"` (infinite scroll).
- `GET /traders` contient `<svg` avec `stroke-dasharray` (jauge score) si ≥ 1 trader.
- `GET /pnl` contient `<canvas id="pnl-chart">` + `Chart.js` config inline.
- `GET /logs` contient `"arrive en M9"`.
- `GET /api/health-external` retourne 200 HTML avec `Gamma` et `Data API`.
- Toutes les pages contiennent `<aside id="sidebar">` et `<footer>`.

**Pas de snapshot pixel-perfect** : on n'utilise pas `pytest-snapshot` / `syrupy` — le HTML Tailwind change à chaque version lib. On teste les invariants structurels seulement.

### 9.5 `test_dashboard_queries_m6.py`

- `get_home_kpis` avec 0 snapshots → cards avec `value="—"`.
- `get_home_kpis` avec ≥ 2 snapshots → sparkline non vide, delta calculé.
- `get_discovery_status` avec `discovery_enabled=false` → `DiscoveryStatus(enabled=False, ...zero...)`.
- `get_discovery_status` avec traders en `shadow + active + paused` → counts OK.
- `get_pnl_milestones` avec 1 kill_switch + 1 promotion → 2 items ordonnés chronologiquement.

### 9.6 `test_dashboard_security_m6.py`

**Invariants M4.5 vérifiés à nouveau** :

- Toutes les routes FastAPI sont `GET`. Parse `app.routes` → assert `"POST" not in route.methods`.
- `app.docs_url is None`, `app.openapi_url is None`.
- Aucun secret dans les responses HTML : pour chaque route, fetch la page et `assert "private_key" not in html.lower()` (et `"bot_token"`, `"api_secret"`, `"api_passphrase"`, `"funder"`).
- Aucun secret dans les templates source : `grep -ri "private_key\|bot_token\|api_secret\|funder" src/polycopy/dashboard/templates/` retourne 0 match (hors commentaires explicatifs §0.6).
- `dashboard.js` ne référence aucun endpoint externe autre que CDN listés.
- `localStorage` usage grep : uniquement `polycopy.theme` clé, pas de `polycopy.token`, pas de `polycopy.creds`.

### 9.7 Test Lighthouse (manuel, non automatisé)

**Procédure** (documentée dans `docs/setup.md` §15 nouvelle section) :

```bash
DASHBOARD_ENABLED=true python -m polycopy --dry-run &
sleep 5
# Dans Chrome DevTools → Lighthouse tab → "Analyze page load" sur http://127.0.0.1:8787/
```

Seuils attendus (profil Moto G4 3G throttling, mode incognito) :

- Performance ≥ 90
- Accessibility ≥ 90
- Best Practices ≥ 90
- SEO : n/a (localhost, aucune indexation)

Si un seuil est < 90, documenter en §14.5 "zones d'incertitude" avec plan de remédiation M6.1.

### 9.8 Couverture

```bash
pytest --cov=src/polycopy/dashboard --cov-report=term-missing
```

Seuil : **≥ 80 % sur `src/polycopy/dashboard/`** (non-régression M4.5 + nouveautés M6). M1..M5 : aucun impact, coverage inchangée.

---

## 10. Mises à jour de documentation (même PR)

### 10.1 `README.md`

- Ajouter à "Roadmap" (après la section M5) :

```markdown
- [x] **M6** : Dashboard 2026 (refonte UX, sidebar, cards KPI, jauge score, timeline PnL)
```

- Ajouter à la table env vars :

```markdown
| `DASHBOARD_THEME` | Thème initial `dark` / `light` (toggle front persiste en localStorage) | `dark` | non |
| `DASHBOARD_POLL_INTERVAL_SECONDS` | Fréquence de rafraîchissement HTMX des partials (2–60 s) | `5` | non |
```

- Mettre à jour le paragraphe "Dashboard local" :

```markdown
## Dashboard local (optionnel, M4.5 + M6)

Dashboard web **read-only** pour superviser live détections, décisions, ordres, positions, PnL et traders. M6 (2026) : refonte UX moderne — sidebar persistante, cards KPI avec sparkline, jauge score SVG pour la page Traders, timeline milestones sur la page PnL, footer health Gamma/Data API.

Opt-in via `.env` :
\`\`\`
DASHBOARD_ENABLED=true
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8787
DASHBOARD_THEME=dark           # ou "light"
\`\`\`

Capture d'écran : [assets/screenshots/dashboard-home.png](assets/screenshots/dashboard-home.png) (ajoutée à M9).
```

- Retirer la ligne roadmap "Dashboard 2026-style (design moderne…)" maintenant livrée.

### 10.2 `docs/architecture.md`

Étendre la section "Module : Dashboard" avec un sous-paragraphe M6 :

```markdown
> **Status M6** ✅ — refonte UX. Même back-end, templates réécrits en Tailwind CDN JIT + palette Radix Colors + typo Inter + icônes Lucide. Sidebar gauche, 4 KPI cards Home avec sparkline SVG, jauge score SVG sur Traders, area chart + overlay drawdown + timeline milestones sur PnL, footer avec health Gamma/Data API (cache 30 s). Lighthouse ≥ 90 sur les 4 catégories. Dark-first, toggle light en localStorage. Responsive mobile via `<details>` sidebar. Voir `specs/M6-dashboard-2026.md`.
```

### 10.3 `CLAUDE.md`

Section "Sécurité — RÈGLES STRICTES", étendre la puce Dashboard M4.5 :

```markdown
- **Dashboard M4.5 / M6** : bind `127.0.0.1` exclusif par défaut. Aucun endpoint write (vérifié en test, invariant M4.5). Aucun secret rendu côté HTML/JSON. M6 conserve strictement ces invariants — les ajouts UX ne touchent pas au back-end sécurité. `localStorage` client uniquement pour préférence UI `polycopy.theme` (pas de token, pas de session). CDN HTTPS uniquement (jsdelivr, unpkg, Google Fonts).
```

Section "Conventions de code", ajouter une puce :

```markdown
- **Front-end dashboard (M6)** : pas de build step. Tailwind CDN JIT + Radix Colors palette (CSS variables) + Inter (Google Fonts) + Lucide icons + HTMX + Chart.js, tout via CDN. SVG sparklines inline côté serveur (Jinja macro). Zéro `node_modules/`.
```

### 10.4 `docs/setup.md`

Ajouter **section 15** :

```markdown
## 15. Dashboard M6 (nouveau look 2026)

Le dashboard M4.5 a été relooké en M6 sans changement d'API. Si tu mets à jour `main` :

- Thème initial : `DASHBOARD_THEME=dark` (défaut) ou `DASHBOARD_THEME=light`. Toggle front en haut à droite, persiste dans `localStorage`.
- Fréquence de polling HTMX : `DASHBOARD_POLL_INTERVAL_SECONDS=5` (défaut). Monter à 10 ou 15 pour réduire la charge en cas de runs longs.
- Vérifier Lighthouse : ouvrir `http://127.0.0.1:8787/` en Chrome incognito, DevTools → Lighthouse → Analyze. Score attendu : ≥ 90 sur Performance / Accessibility / Best Practices.

Troubleshooting M6 :

- **Page blanche / icônes manquantes** : premier chargement requiert internet pour les 4 CDN (Tailwind JIT, HTMX, Chart.js, Lucide, Inter). Ensuite cache navigateur. Vérifier DevTools Network.
- **Thème qui ne s'applique pas** : clear `localStorage.removeItem('polycopy.theme')`, recharger.
- **Footer Gamma/Data API `degraded`** : ping à 3 s timeout. Vérifier `curl -sS https://gamma-api.polymarket.com/markets?limit=1`. Si rouge persistant → network issue locale ou Polymarket down.
```

---

## 11. Commandes de vérification finale

```bash
ruff check .
ruff format --check .
mypy src
pytest --cov=src/polycopy/dashboard --cov-report=term-missing  # ≥ 80 %
pytest                                                           # non-régression M1..M5

# Smoke test M6 UX
DASHBOARD_ENABLED=true DASHBOARD_THEME=dark python -m polycopy --dry-run &
sleep 3
curl -sSf http://127.0.0.1:8787/home | grep -q 'data-lucide="dollar-sign"'
curl -sSf http://127.0.0.1:8787/traders | grep -q 'stroke-dasharray'
curl -sSf http://127.0.0.1:8787/pnl | grep -q 'pnl-chart'
curl -sSf http://127.0.0.1:8787/logs | grep -q 'M9'
curl -sSf http://127.0.0.1:8787/api/health-external | grep -q 'Gamma'
ss -tlnp | grep 8787 | grep -q '127.0.0.1'                       # bind localhost
kill %1 && wait

# Lighthouse manuel (documenter score dans la PR)
# Ouvrir Chrome incognito, DevTools → Lighthouse → analyser http://127.0.0.1:8787/
```

---

## 12. Critères d'acceptation

- [ ] `DASHBOARD_ENABLED=true python -m polycopy --dry-run` tourne ≥ 60 s. Logs identiques à M5 (aucun nouveau type de log métier à M6). Exit 0 sur SIGINT.
- [ ] `DASHBOARD_ENABLED=false` (défaut) → comportement strict M5 préservé (aucun port ouvert, aucun code M6 instancié).
- [ ] `GET /home` rend 4 KPI cards avec sparkline SVG inline + "Discovery status" + "Derniers trades".
- [ ] `GET /detections`, `/strategy`, `/orders`, `/positions` rendent des rows (pas des tables HTML) avec infinite scroll `hx-trigger="revealed"`.
- [ ] `GET /traders` rend des `<details>` avec jauge SVG circulaire (`stroke-dasharray` calculée), sparkline historique score, sous-tableau de metrics au dépli.
- [ ] `GET /pnl` rend un area chart Chart.js avec gradient fill, overlay drawdown semi-transparent, timeline milestones sous le graph, toggle [24h] [7j] [30j].
- [ ] `GET /logs` rend un stub "arrive en M9" (pas de 404).
- [ ] `GET /api/health-external` rend le footer HTML avec Gamma ✅ + Data API ✅ et latence en ms (ou `degraded` si ping fail).
- [ ] Sidebar gauche visible sur desktop ≥ 1024 px, réduite à 64 px en 640-1024 px, cachée (hamburger `<details>`) en < 640 px.
- [ ] Dark-first : `<html data-theme="dark">` au premier chargement. Toggle en haut à droite → stocke dans `localStorage.polycopy.theme` et applique sans reload.
- [ ] Aucun secret (`POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`, `TELEGRAM_BOT_TOKEN`, creds L2, `GOLDSKY_API_KEY`) dans le HTML / JSON / sources templates — vérifié par grep automatisé.
- [ ] Toutes les routes FastAPI restent `GET` (test `test_dashboard_security_m6.py` + héritage M4.5).
- [ ] `docs_url=None`, `openapi_url=None` inchangés.
- [ ] Bundle total CSS+JS servi au premier load < 300 KB (DevTools Network, hors Google Fonts).
- [ ] Lighthouse ≥ 90 sur Performance + Accessibility + Best Practices (profil Moto G4 3G, mode incognito). Capture attachée à la PR.
- [ ] Mobile testé en Chrome DevTools device mode (iPhone 12, iPad) — navigation hamburger fonctionnelle, cards lisibles, infinite scroll OK.
- [ ] `ruff check .` + `ruff format --check .` + `mypy src` (`--strict`) : 0 erreur.
- [ ] `pytest` : 0 échec. Coverage ≥ 80 % sur `src/polycopy/dashboard/` (inchangé par rapport à M4.5 + M5, vérifié). Non-régression M1..M5 ≥ 80 % sur les autres modules.
- [ ] Aucun refactor M1..M5 (diff = uniquement `src/polycopy/dashboard/` + `config.py` +2 champs + `.env.example` +2 lignes + docs).
- [ ] Docs §10 à jour (`README.md`, `docs/architecture.md`, `CLAUDE.md`, `docs/setup.md` §15) dans le **même** commit que le code.
- [ ] Commit final unique : `feat(dashboard): M6 2026 UX overhaul (Tailwind CDN + Radix Colors + Lucide + sparklines + score gauge)`.

---

## 13. Hors scope M6 (NE PAS implémenter)

- **Auth applicative** (password, JWT, cookie de session). Bind localhost suffit. Reporté indéfiniment, cf. M4.5 §13.
- **Endpoints write** (POST/PUT/DELETE). M6 reste read-only strict. Si un jour utile → nouveau milestone avec CSRF + header secret + audit sécurité.
- **Auto dark/light via `prefers-color-scheme`**. À M6 on utilise `DASHBOARD_THEME` + toggle user. Reportable à M6.1.
- **i18n / multi-langue**. UI en français, cohérent CLAUDE.md docstrings FR.
- **PWA / manifest / notifications navigateur / installable**. Pas de service worker.
- **Export CSV / Excel depuis le dashboard**. `scripts/pnl_report.py --output csv` couvre déjà.
- **WebSocket / SSE** real-time. HTMX polling 5 s suffit.
- **Graphiques candlestick, OHLC, order book depth**. Line/area chart + sparklines suffisent.
- **Custom dashboard builder** (drag/drop widgets). Over-engineering pour 1 user.
- **Analytics / télémétrie** (Plausible, GoAccess). Vie privée du user, pas de mouchard.
- **A/B testing UX**. 1 user, pas applicable.
- **Refactor `queries.py` / `routes.py` / `orchestrator.py`** M4.5/M5 existants. M6 ajoute, ne refactor pas.
- **Tests end-to-end Playwright / Selenium**. Snapshot light ASGI + Lighthouse manuel suffisent à M6.
- **Vendoring offline** (Tailwind, Inter, Chart.js, HTMX, Lucide en `static/vendor/`). Reportable M6.1 si CDN flaky observé.
- **CSP stricte** (`script-src 'self' + whitelist CDN`). Complexifie le debug JIT Tailwind, reportable M6.1.
- **Logs onglet `/logs`** fonctionnel. Stub M6, implémenté en M9.
- **Toggle dry-run / réel sur la page PnL**. Layout réservé, branchement en M8.
- **Backtest interactif** (slider window, drag thresholds). `/backtest` reste une card statique.

---

## 14. Notes d'implémentation + zones d'incertitude

### 14.1 Ordre de travail suggéré

1. **Ajouter 2 env vars** dans `config.py` + `.env.example` (avec commentaires "UI cosmétique").
2. **Créer `jinja_filters.py`** + tests unit exhaustifs (≥ 15 test cases pour les 9 filters).
3. **Créer `health_check.py`** + tests respx (ok / timeout / cache TTL).
4. **Ajouter queries `get_home_kpis`, `get_discovery_status`, `get_pnl_milestones`, `get_app_version`** + tests.
5. **Ajouter routes `/logs`, `/api/health-external`, `/api/version`** + enregistrement filters Jinja + test `test_dashboard_security_m6.py`.
6. **Réécrire `base.html`** (layout sidebar, palette CSS vars, loads CDN Tailwind + Inter + Lucide).
7. **Réécrire `macros.html`** (`kpi_card`, `sparkline_svg`, `badge`, `score_gauge`, `row_card`, `milestone_item`).
8. **Réécrire `home.html`** + test de rendu (assertions structurelles).
9. **Réécrire les 4 pages tables → rows** (`detections`, `strategy`, `orders`, `positions`) + partials.
10. **Réécrire `pnl.html`** avec area chart + drawdown overlay + timeline milestones.
11. **Réécrire `traders.html`** avec jauge SVG + dépliables.
12. **Créer `logs_stub.html`** (placeholder M9).
13. **Réécrire `backtest.html`** en card Tailwind minimale.
14. **Réécrire `dashboard.css`** (overrides Tailwind minimalistes, keyframes, scrollbar custom).
15. **Créer `dashboard.js`** (theme toggle + HTMX hooks lucide + version fetch).
16. **Test regression templates** (assertions HTML structurelles).
17. **Smoke test manuel** : parcourir chaque page, dark + light, desktop + mobile device mode.
18. **Lighthouse audit** : score ≥ 90 sur Performance + A11y + Best Practices. Capture attachée à la PR.
19. **Doc updates §10** dans le même commit.
20. **Commit unique** : `feat(dashboard): M6 2026 UX overhaul (...)`.

### 14.2 Principes

- **Pas d'abstraction prématurée** : `macros.html` contient 5-8 macros concrètes, pas un framework macro générique. Si demain une 6ᵉ page apparaît, on ajoute sa macro sans factoriser.
- **Templates lisibles** : préférer du markup clair avec `{% include %}` qu'une macro avec 15 paramètres optionnels.
- **Aucune logique métier dans les templates** : les filtres Jinja sont purement cosmétiques (formatage). Les calculs (sparkline points, delta) vivent dans `queries.py`.
- **HTMX > JS custom** : si une interaction peut se faire par swap HTMX, la préférer à 20 lignes de JS.
- **Dark first** : on design en dark, on vérifie en light. L'inverse casse souvent les contrastes.
- **Mobile après desktop** : on livre desktop, on teste mobile, on corrige avec `md:` / `lg:` utilities Tailwind. Pas de templates séparés.

### 14.3 Décisions auto-arbitrées

1. **Tailwind CDN JIT** plutôt que build-time : cohérent avec la contrainte "zéro build step" M4.5. Trade-off accepté (80 KB au premier load, cache ensuite).
2. **Radix Colors via variables CSS hardcodées** (pas via `npm install @radix-ui/colors`) : zéro dep Python/JS supplémentaire, 12 lignes CSS suffisent pour les 10 teintes utiles.
3. **Inter via Google Fonts** (pas Geist, pas self-host) : stack la plus prévisible en 2026.
4. **Lucide** (pas Feather, pas Heroicons) : maintenance active, ESM via unpkg.
5. **Sparklines SVG inline côté serveur** (pas Chart.js / uPlot) : zéro JS requis, rendu instantané, pas de re-render au swap HTMX.
6. **Pas de CSP à M6** : documenté comme M6.1. Ajouter une CSP permissive (`default-src 'self' https: data:`) pourrait être tenté mais le JIT Tailwind utilise de l'`eval`-style — risque de casser.
7. **Pas d'auto dark/light** : `DASHBOARD_THEME` + toggle manuel. Moins magique, plus prévisible.
8. **Infinite scroll cap 500 rows** : compromis RAM navigateur / UX. Au-delà, bouton "Charger plus" manuel.
9. **Footer health check TTL 30 s** : 2 endpoints × 2 calls/min max = négligeable. Plus court = bruit. Plus long = user verra une panne trop tard.
10. **`/logs` stub** au lieu d'absence : évite qu'un user qui voit l'item sidebar se plaigne d'un bug 404.
11. **`DASHBOARD_POLL_INTERVAL_SECONDS=5`** (vs 3 s M4.5) : gain négligeable en réactivité perçue, -40 % de bruit log.
12. **`wallet_label` filter fallback `short_hash`** : si le user a mis un label en DB, on l'affiche ; sinon truncation de l'adresse. Évite les strings de 42 chars qui cassent le layout rows.
13. **Score gauge gradient vert → ambre → rouge** : même convention que les dashboards fintech (Robinhood, Trading212). Lisibilité universelle.
14. **Lighthouse seuil ≥ 90** : ambitieux mais réaliste sur une page statique pas monétisée. Si < 90, investiguer avant merge.
15. **Pas de refactor `routes.py` existant** : M6 ajoute 3 routes, modifie 0 route existante. Tests M4.5 + M5 passent sans diff.

### 14.4 Pièges anticipés

1. **Tailwind JIT + HTMX swap** : si une classe utility n'existe pas au premier render mais apparaît après un swap (ex: `hover:bg-slate-800` dans un row chargé par HTMX), Tailwind JIT peut ne pas la générer. Mitigation : déclarer toutes les classes potentielles dans `base.html` via un commentaire `<!-- tailwind: bg-slate-800 text-red-500 ... -->` ou les utiliser dans `home.html` (qui est toujours chargé au premier hit).
2. **Lucide createIcons après HTMX swap** : sans le listener `htmx:afterSwap`, les icônes d'un fragment swappé ne sont pas rendues. Listener obligatoire dans `dashboard.js`.
3. **Chart.js destroy avant re-create** : sur swap page `/pnl`, si on ne destroy pas l'ancien chart, fuite mémoire + double rendering. Implementer pattern `if (window._pnlChart) { window._pnlChart.destroy(); } window._pnlChart = new Chart(...)`.
4. **SVG gradient id uniques** : `<linearGradient id="gradient-0xabc">` — utiliser `wallet_address` pour unicité. Sinon 2 jauges se partagent le gradient → rendu cassé.
5. **`<details>` + `<summary>` sur Safari iOS** : pas de bug connu mais CSS custom sur `details[open]` peut glitcher. Tester explicitement.
6. **Google Fonts FOIT** : sans `display=swap`, texte invisible pendant 3 s sur slow 3G. **Obligatoire** d'ajouter `&display=swap` dans l'URL.
7. **Infinite scroll + tab inactive** : `hx-trigger="revealed[document.visibilityState==='visible']"` évite le firing en tab caché. Important sinon charge serveur gratuite.
8. **Health check timeout = fuite socket httpx** : si 100 users font `hx-trigger="load"` simultanément → 100 connexions httpx. Mitigation : 1 `httpx.AsyncClient` partagé dans `ExternalHealthChecker` (singleton), max 1 request concurrente via `asyncio.Lock`.
9. **`localStorage` inexistant** (navigateur privé Safari iOS) : try/catch silencieux dans `dashboard.js`. Thème fallback = `DASHBOARD_THEME` env.
10. **Dark/light toggle avant charge HTMX** : si l'user clique toggle avant que le DOM soit prêt, `document.documentElement` peut être null. Listener `DOMContentLoaded` obligatoire.
11. **Tailwind config inline `tailwind.config = { ... }`** : doit être déclaré **avant** le `<script src="https://cdn.tailwindcss.com">`. L'ordre est sensible.
12. **CDN version pinning** : `tailwindcss@3` sans minor = risque de breaking change. Pinner `tailwindcss@3.4.x`. Idem HTMX, Chart.js, Lucide.
13. **Responsive media queries** : Tailwind `md:` = ≥ 768 px, `lg:` = ≥ 1024 px. Notre breakpoint tablet 640-1024 utilise `sm:` (≥ 640) + `lg:`. Vérifier en DevTools que les 3 états (mobile / tablet / desktop) rendent correctement.
14. **Jinja `format` filter** : `'%.2f' | format(score)` fonctionne mais si `score=None` → `TypeError`. Wrapper dans un filter custom `safe_format("%.2f", score)` ou garde `{% if score is not none %}`.
15. **Scrollbar custom** : `scrollbar-width: thin` (Firefox) + `::-webkit-scrollbar` (Chromium/Safari). Pas de lib `overlayscrollbars`. Styling minimal dans `dashboard.css`.
16. **iOS Safari bottom bar** : ajouter `padding-bottom: env(safe-area-inset-bottom)` sur le main pour éviter que du contenu se cache derrière la bottom bar iOS.

### 14.5 Zones d'incertitude à lever AVANT implémentation

(Section critique : signaler ces points à l'utilisateur avant `/implement-module M6`.)

1. **Tailwind JIT CDN stability 2026** : l'avenir du script `cdn.tailwindcss.com` en 2026 est incertain — Tailwind v4 pousse vers `tailwindcss.com/docs/v4-beta` et un mode CDN différent. **Risque** : si le CDN est deprecated pendant la durée de vie M6, le dashboard devient cassé. **Mitigation** : vendorer Tailwind 3.x compiled en `static/vendor/tailwind.min.css` (~80 KB) est un fallback M6.1 trivial. Documenter.
2. **Performance JIT sur Firefox** : Tailwind JIT utilise du CSS generation en runtime. Sur Firefox, les performances peuvent être différentes de Chrome. À tester sur Firefox dernière version — si Lighthouse score < 90 sur Firefox mais ≥ 90 sur Chrome, prioriser Chrome (stack WSL primary).
3. **Lighthouse reproductibilité** : le score Lighthouse dépend du CPU/réseau au moment du test. Un passage à 88 au lieu de 90 n'est pas forcément une régression. **Décision** : la PR joint un **screenshot** Lighthouse en mode "Desktop" (pas Moto G4 3G qui est trop volatile) ; le seuil 90 est indicatif.
4. **Google Fonts GDPR** : techniquement, charger Inter depuis Google Fonts expose l'IP de l'user à Google. Pour un dashboard localhost monouser, impact négligeable. **Décision** : accepter. Self-host reportable M6.1 si remontée.
5. **Lucide package size** : le script ESM `unpkg.com/lucide@latest/dist/esm/lucide.js` charge toutes les icônes (~200 KB). **Mitigation si trop lourd** : remplacer par `lucide-static` + import individual SVG à la demande (plus de travail). À mesurer au premier build.
6. **`health_check.py` à l'init** : si Gamma/Data API sont joignables au boot, OK. Sinon, le 1ᵉʳ rendering footer attendra 3 s (timeout). Documenter que le footer peut être "Vérifié il y a 0 s / degraded" pendant les 30 premières s après un cold start offline.
7. **Typo Geist Sans** : mentionnée dans le brief utilisateur mais Geist n'est pas sur Google Fonts (self-host via Vercel CDN ou npm). **Décision** : M6 utilise **Inter** ; Geist reportable M6.1 via self-host.
8. **Dark/light toggle position** : en haut à droite du `main` ? en bas de la sidebar ? Décision spec : en haut à droite du main, à côté du breadcrumb, icône `sun` / `moon` Lucide.
9. **Sparkline 7j avec < 7 snapshots** : si le bot vient de démarrer, `pnl_snapshots` est quasi-vide. Fallback : afficher sparkline plate avec message "Pas assez de données". Alternative : masquer la sparkline complètement tant que < 5 points. **Décision** : masquer (cohérence visuelle) + ajouter un `<span class="text-muted">—</span>`.
10. **Lazy-load icons** : tout Lucide ~200 KB au boot est acceptable ? Alternative : inline SVG dans `macros.html` pour les 10 icônes utilisées effectivement (pas de dep réseau). **Recommandation** : mesurer au premier build ; si > 100 KB, passer en inline SVG.
11. **Jauge score 0.0 à l'affichage** : si un wallet a `score=0.0` (cold start M5), la jauge est vide. Afficher `"0.00"` + texte "Cold start" plutôt que juste un cercle vide (évite de confondre avec un bug de rendering).
12. **Pnl timeline milestones limit** : 5 ? 10 ? 20 ? Trop → clutter. **Décision** : cap 8 milestones, tri par `at DESC`, afficher "voir plus" lien vers `/orders?filter=milestone` (reportable M6.1).
13. **Git SHA availability** : le bot peut tourner depuis un tarball (pas de `.git`). Fallback version string : `"0.6.0-unknown"` si `git rev-parse` échoue. Silencieux, pas de crash.
14. **Routes `/api/*` vs `/partials/*`** : M4.5 utilise `/partials/*` pour les fragments HTMX. M6 ajoute `/api/health-external` et `/api/version`. Cohérence ? **Décision** : les fragments HTMX (retournent HTML) restent en `/partials/*`. Les endpoints JSON (version, peut-être d'autres M8+) vivent en `/api/*`. Accepter la dualité.
15. **Testability des templates** : snapshot test pixel-perfect casse à chaque itération Tailwind. **Décision** : on teste les invariants structurels uniquement (`hx-trigger="revealed"` présent, `data-lucide="arrow-up-circle"` présent, etc.). Pas de `syrupy` / `pytest-snapshot`.

---

## 15. Prompt à m'envoyer pour lancer l'implémentation

Copie-colle tel quel quand tu es prêt :

```
/implement-module M6

Suis specs/M6-dashboard-2026.md à la lettre. Pas d'invocation skill Polymarket requise — M6 est 100 % front-end + 1 health check sur Gamma/Data API déjà documentés. Commence par l'ordre §14.1.

Contraintes non négociables :

- M6 ne touche PAS au back-end métier (watcher / strategy / executor / monitoring / discovery / storage). Diff autorisé : src/polycopy/dashboard/ + config.py (+2 champs) + .env.example (+2 lignes) + docs.
- Toutes les routes FastAPI restent GET. Zéro POST/PUT/DELETE/PATCH ajouté. Test test_dashboard_security.py M4.5 doit passer sans diff + nouveau test_dashboard_security_m6.py renforce.
- Bind 127.0.0.1 par défaut inchangé. docs_url=None / openapi_url=None inchangés.
- Aucun secret (POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER, TELEGRAM_BOT_TOKEN, creds L2, GOLDSKY_API_KEY hypothétique) ne doit apparaître dans les HTML / JSON rendered ni dans les templates sources — vérifié par grep automatisé.
- localStorage UI uniquement (clé 'polycopy.theme' = 'dark' | 'light'). Pas de token, pas de session, pas de donnée DB.
- Zéro build step : Tailwind CDN JIT + Inter Google Fonts + Lucide unpkg + HTMX + Chart.js, tout via CDN HTTPS. Pas de node_modules/, pas de pyproject.toml diff dépendances.
- Bundle CSS+JS < 300 KB au premier load (hors Google Fonts). Mesuré DevTools Network.
- Lighthouse ≥ 90 sur Performance + Accessibility + Best Practices sur /home en dark mode (Chrome incognito). Capture jointe à la PR.
- Responsive desktop + tablet + mobile testé en DevTools device mode (iPhone 12, iPad, Moto G4). Sidebar hamburger via <details> natif en mobile.
- Toggle dark/light persistant via localStorage + préférence initiale via DASHBOARD_THEME env (défaut 'dark').
- HTMX polling avec garde `[document.visibilityState==='visible']` pour éviter le firing tab caché. DASHBOARD_POLL_INTERVAL_SECONDS=5 par défaut.
- Infinite scroll via `hx-trigger="revealed"` sur tables (détections, stratégie, ordres, positions) — cap 500 rows total avant bouton manuel.
- Palette Radix Colors via 12 variables CSS hardcodées dans base.html. Typo Inter via Google Fonts display=swap. Icônes Lucide via unpkg ESM + createIcons() au afterSwap HTMX.
- Stub /logs "arrive en M9" pour éviter 404 perçu en bug. M9 remplacera le template.
- Stub toggle "réel / dry-run" sur /pnl préparé dans le layout mais non actif — M8 branchera.
- Sparkline SVG inline côté serveur (Jinja macro), pas Chart.js pour les cards KPI.
- Jauge score SVG circulaire pour /traders (stroke-dasharray calculé via Jinja filter score_to_dasharray), gradient vert→ambre→rouge par wallet (id unique).
- Chart.js /pnl : area chart avec gradient fill, overlay drawdown semi-transparent, annotations kill switch, timeline milestones sous le graph.
- Footer health Gamma + Data API (HEAD ping, timeout 3s, cache 30s, 1 AsyncClient partagé + Lock).
- Conventions CLAUDE.md (async, Pydantic v2, SQLAlchemy 2.0, structlog, docstrings FR / code EN, pas de print).
- mypy --strict propre, ruff propre, pytest ≥ 80% coverage sur src/polycopy/dashboard/ et pas de régression ≥ 80% sur M1..M5.
- Tests via httpx AsyncClient + ASGITransport (templates regression light) + respx (health_check).
- Pas de snapshot pixel-perfect (syrupy/pytest-snapshot refusés). Tests d'invariants structurels uniquement.
- Doc updates §10 dans le même commit (README + architecture + CLAUDE + setup §15).
- Commit final unique : feat(dashboard): M6 2026 UX overhaul (Tailwind CDN + Radix Colors + Lucide + sparklines + score gauge)

Demande-moi confirmation avant tout patch sensible :
- config.py (les 2 env vars DASHBOARD_THEME + DASHBOARD_POLL_INTERVAL_SECONDS).
- .env.example (ajout des 2 vars + commentaires "UI cosmétique").
- Suppression d'un fichier template existant (préférer écrasement via Write, pas rm).

Si une zone §14.5 se confirme problématique pendant l'implémentation (ex: CDN Tailwind JIT deprecated, Lucide bundle > 200 KB, Lighthouse score < 85), STOP et signale — ne tranche pas au pif. Trade-off nouveau non anticipé → ajoute-le en §14.5 et signale.

Lighthouse audit obligatoire avant merge : capture écran attachée à la PR avec scores ≥ 90 (ou justification de l'écart).
```
