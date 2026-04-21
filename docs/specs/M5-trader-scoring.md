# M5 — Trader scoring & discovery

Spec d'implémentation du **dernier module** de polycopy : découvrir automatiquement des wallets Polymarket "smart money", les **scorer** selon une formule déterministe non-gameable, promouvoir les meilleurs en `target_traders.active=true` (consommés par le Watcher M1) et retirer ceux qui décrochent. M5 est **le module qui décide quel capital sera mis au travail**. Une erreur ici = le bot copie des traders médiocres = capital réel perdu.

Source de vérité fonctionnelle : `docs/architecture.md` (à compléter §11). Conventions : `CLAUDE.md`. Schémas API : skill `/polymarket:polymarket` + endpoints documentés §4. Templates structurels : `specs/M1-watcher-storage.md` à `specs/M4.5-dashboard.md`. Source d'attache storage : `src/polycopy/storage/models.py:31` (`TargetTrader.score: Mapped[float | None]` déjà déclaré, peuplé à M5).

---

## 0. Pré-requis environnement

### 0.1 Bootstrap (déjà fait)

`bash scripts/setup.sh` (idempotent). Aucun patch config structurel requis — M5 ajoute des env vars optionnelles + 0 deps obligatoires (Goldsky subgraph est `httpx` direct, pas de SDK GraphQL). Si on choisit `gql[httpx]` pour les queries (cf. §2.6), `scripts/setup.sh` re-tirera la dep via `pip install -e ".[dev]"`.

### 0.2 Skill Polymarket (déjà installé)

Source de vérité endpoints. Invocation : `/polymarket:polymarket`. Si réinstall :

```
/plugin marketplace add atompilot/polymarket-skill
/plugin install polymarket@atompilot-polymarket-skill
```

**Action obligatoire avant code M5** : capturer 6 fixtures (cf. §4). Sans fixture = pas de DTO. Toute divergence schéma vs OpenAPI documentée par écrit dans la spec et un commentaire dans le code.

### 0.3 `.env` — nouvelles variables (toutes OPTIONNELLES)

| Variable env | Champ Settings | Default | Description |
|---|---|---|---|
| `DISCOVERY_ENABLED` | `discovery_enabled` | `false` | Opt-in strict (cf. §2.5). Si `false`, `__main__` n'instancie **pas** le `DiscoveryOrchestrator`. M5 invisible. |
| `DISCOVERY_INTERVAL_SECONDS` | `discovery_interval_seconds` | `21600` | Cadence d'un cycle complet de scoring (6h). Borne min 3600, borne max 604800 (7j). Validé Pydantic. |
| `DISCOVERY_CANDIDATE_POOL_SIZE` | `discovery_candidate_pool_size` | `100` | Wallets candidats scannés par cycle (budget API : ~2 calls / wallet → 200 calls / cycle). |
| `DISCOVERY_TOP_MARKETS_FOR_HOLDERS` | `discovery_top_markets_for_holders` | `20` | Marchés Gamma top-liquidité dont on extrait les `/holders` pour bootstrap. |
| `DISCOVERY_GLOBAL_TRADES_LOOKBACK_HOURS` | `discovery_global_trades_lookback_hours` | `24` | Fenêtre de scan du feed `/trades` global pour bootstrap. |
| `MAX_ACTIVE_TRADERS` | `max_active_traders` | `10` | Plafond DUR sur `target_traders.active=true`. Si dépassement, M5 refuse d'ajouter et alerte (jamais retire arbitrairement). |
| `BLACKLISTED_WALLETS` | `blacklisted_wallets` | `[]` | CSV ou JSON array de wallets jamais ajoutables, même si scorés haut. Même format que `TARGET_WALLETS` (`Annotated[list[str], NoDecode]` + validator). |
| `SCORING_VERSION` | `scoring_version` | `"v1"` | Version de la formule. Loggée + écrite avec chaque décision pour reproductibilité (cf. §7.6). |
| `SCORING_MIN_CLOSED_MARKETS` | `scoring_min_closed_markets` | `10` | Cold start : un wallet avec < 10 marchés résolus est scoré 0.0 et flaggé `low_confidence` (cf. §7.4). |
| `SCORING_LOOKBACK_DAYS` | `scoring_lookback_days` | `90` | Fenêtre glissante de PnL/volume retenue (cf. §2.3). |
| `SCORING_PROMOTION_THRESHOLD` | `scoring_promotion_threshold` | `0.65` | Score ≥ 0.65 → candidat à promouvoir (sous réserve de cap §2.4 et hystérésis §2.4). |
| `SCORING_DEMOTION_THRESHOLD` | `scoring_demotion_threshold` | `0.40` | Score < 0.40 pendant K cycles → demote. |
| `SCORING_DEMOTION_HYSTERESIS_CYCLES` | `scoring_demotion_hysteresis_cycles` | `3` | K = nombre de cycles consécutifs sous seuil avant retrait (anti-whipsaw). |
| `TRADER_SHADOW_DAYS` | `trader_shadow_days` | `7` | Jours d'observation `shadow` avant qu'un wallet auto-promu devienne `active` (cf. §3.5). Bornes [0, 90]. **0 = bypass shadow** uniquement avec `DISCOVERY_SHADOW_BYPASS=true`. |
| `DISCOVERY_SHADOW_BYPASS` | `discovery_shadow_bypass` | `false` | Si `true` ET `TRADER_SHADOW_DAYS=0`, autorise l'auto-promote immédiat. **Logguer un WARNING au boot si `true`.** |
| `DISCOVERY_BACKEND` | `discovery_backend` | `"data_api"` | `"data_api"` (default) | `"goldsky"` | `"hybrid"` (cf. §2.6). |
| `GOLDSKY_POSITIONS_SUBGRAPH_URL` | `goldsky_positions_subgraph_url` | `https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/positions-subgraph/0.0.7/gn` | Override possible (subgraphs Goldsky changent de version périodiquement). |
| `GOLDSKY_PNL_SUBGRAPH_URL` | `goldsky_pnl_subgraph_url` | `https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/pnl-subgraph/0.0.14/gn` | Idem. Optionnel : seul utilisé si `discovery_backend ∈ {goldsky, hybrid}`. |

À ajouter à `config.py` ET `.env.example`. Aucune n'est requise — défauts safe. Validation Pydantic v2 :

- `discovery_interval_seconds`: `Field(ge=3600, le=604800)`.
- `max_active_traders`: `Field(ge=1, le=100)`.
- `scoring_promotion_threshold`: `Field(ge=0.0, le=1.0)`.
- `scoring_demotion_threshold`: `Field(ge=0.0, le=1.0)`. Validator cross-field : `demotion < promotion`.
- `trader_shadow_days`: `Field(ge=0, le=90)`.

### 0.4 Critère de validation "environnement"

```bash
DISCOVERY_ENABLED=true DISCOVERY_INTERVAL_SECONDS=3600 python -m polycopy --dry-run
```

Doit logger en plus des M1/M2/M3/M4/M4.5 events :

- `discovery_starting` (binding : `interval_s=3600`, `pool_size=100`, `backend=data_api`, `scoring_version=v1`).
- `discovery_started`.
- `discovery_cycle_started` (au tick).
- `discovery_cycle_completed` (binding : `candidates_seen`, `scored`, `promoted`, `demoted`, `duration_ms`).
- `discovery_stopped` au shutdown.

Exit 0 sur SIGINT.

### 0.5 Sécurité — rappels stricts pour M5

- **Read-only stricte** : M5 n'a **aucun accès aux creds CLOB L2** ni à `polymarket_private_key`. Les seuls endpoints réseau sont publics (Data API, Gamma, Goldsky). Aucune signature L1/L2.
- **Capital safety (NON NÉGOCIABLE)** : un wallet auto-découvert ne peut **jamais** déclencher un ordre réel avant `TRADER_SHADOW_DAYS` jours en statut `shadow`. Le Watcher M1 (cf. §3.5) doit ignorer les wallets `status='shadow'`. Bypass uniquement via `TRADER_SHADOW_DAYS=0` ET `DISCOVERY_SHADOW_BYPASS=true` ET log WARNING au boot.
- **Kill switch M4 souverain** : si `stop_event.set()` (kill switch déclenché), M5 stoppe immédiatement le cycle en cours et n'écrit plus rien (idem watcher/strategy/executor).
- **Plafond `MAX_ACTIVE_TRADERS`** : M5 ne retire **jamais** un wallet existant pour faire de la place à un nouveau candidat. Si `count(active=true) >= MAX_ACTIVE_TRADERS`, M5 logue `discovery_cap_reached` warning, push `Alert(level="WARNING", event="discovery_cap_reached")`, n'ajoute personne. C'est à l'utilisateur de retirer manuellement (ou de monter `MAX_ACTIVE_TRADERS`).
- **Blacklist absolue** : un wallet présent dans `BLACKLISTED_WALLETS` n'est **jamais** ajouté, même s'il scorerait 1.0. Vérifié en début de pipeline de promotion.
- **Pas d'API key tierce loggée** : si un jour Goldsky impose un token (`GOLDSKY_API_KEY`), il vit en env var et n'est jamais loggé même partiellement (cf. discipline `TELEGRAM_BOT_TOKEN` M4).
- **Reproductibilité** : la formule de scoring est versionnée (`SCORING_VERSION`). Chaque ligne `TraderScore` (§5.2) écrit avec sa version. Permet de comparer 2 versions sur historique sans rewrite.
- **Audit log obligatoire** : chaque décision (`add_shadow`, `promote_active`, `demote_paused`, `remove`, `keep`) est loggée structlog ET écrite dans `trader_events` (§5.3). Auditable a posteriori.
- **Rate limit Data API** : à `DISCOVERY_INTERVAL_SECONDS=21600` (6h) avec `pool_size=100` + ~2 calls/wallet = 200 calls / 6h = ~0.5 req/min. Marge énorme vs ~100 req/min documenté. Si `pool_size > 500` ou `interval < 3600`, log `rate_limit_risk_high` warning au boot.

---

## 1. Objectif M5 (scope exact)

Donner au bot une **boucle de découverte + scoring** qui maintient `target_traders` à jour automatiquement :

- Découvrir un pool de wallets candidats (cf. §3.1) via la Data API publique et/ou Goldsky.
- Pour chaque candidat, fetch ses metrics (positions résolues, PnL, volume, diversité) via `/positions` + `/activity`.
- Calculer un `score ∈ [0.0, 1.0]` déterministe et non-gameable (cf. §7).
- Persister le score (overwrite `target_traders.score`) + une ligne historique dans `trader_scores` (audit, comparaison de versions).
- Décider : promote `shadow → active`, demote `active → paused`, ou `keep`. Logger + écrire dans `trader_events`.
- Émettre des alertes Telegram sur les événements importants (`trader_added`, `trader_demoted`, `discovery_cap_reached`, `discovery_cycle_failed`).
- Le Watcher M1 (modifié à minima) ignore les wallets `status ∈ {shadow, paused}` — seuls les `active` sont pollés.

**Livrable fonctionnel** : `DISCOVERY_ENABLED=true python -m polycopy` lance un `DiscoveryOrchestrator` co-orchestré dans le `TaskGroup` `__main__`. Toutes les `DISCOVERY_INTERVAL_SECONDS`, le cycle découvre + score + décide. Le dashboard M4.5 (extension §10.4) affiche `/traders` avec scores et statuts.

**Hors livrable M5** : ML / réseau de neurones, copy-trading "consensus" multi-traders, scoring multi-chaînes, interface Telegram bidirectionnelle (`/score 0xabc`), recompute rétroactif des scores historiques sur changement de formule.

---

## 2. Arbitrages techniques (10 points à trancher explicitement)

### 2.1 Stratégie de découverte des candidats

**Recommandation : Data API hybride `/holders` + `/trades` (option (b)+(c) du prompt).**

Pipeline de découverte par cycle :

1. **Bootstrap par marchés top-liquidité** (`/holders`) :
   - Fetch via `GammaApiClient.list_top_markets(limit=DISCOVERY_TOP_MARKETS_FOR_HOLDERS)` les marchés actifs triés `liquidityNum DESC`.
   - Pour chaque marché, `GET /holders?market=<conditionId>&limit=20` → top 20 holders par marché.
   - Union des wallets uniques. Cap final à `DISCOVERY_CANDIDATE_POOL_SIZE` (ex: 20 marchés × 20 holders = 400 candidats potentiels avant dédup).
2. **Bootstrap par feed global** (`/trades?limit=500`) :
   - Fetch les 500 trades les plus récents du feed global (sans `user`).
   - Filtrer les `usdcSize >= 100` (seuil de volume : exclut les bots et micro-trades).
   - Union des `proxyWallet` distincts.
3. **Union & dédup** : merge des 2 pools, dédup par adresse lowercase, exclure `BLACKLISTED_WALLETS`, exclure `target_traders` déjà présents en `status='paused'` (volontairement retirés par le user).
4. **Cap** : tronquer aux `DISCOVERY_CANDIDATE_POOL_SIZE` premiers selon un score de "promesse initial" simple (fréquence d'apparition holders × log(volume max observé)). Ce pré-filtre évite de gaspiller le budget API sur des candidats faibles.

**Alternatives écartées** :

- **(a) Scraping page leaderboard** : `polymarket.com/leaderboard` est server-side rendered, scraper fragile. Pas de garantie de stabilité, risque ToS. **Rejeté.**
- **(b) Endpoint leaderboard officiel** : **n'existe pas** côté Polymarket public (cf. §4 — confirmé par recherche : "There is no dedicated leaderboard endpoint"). `polymarket-cli` agrège lui-même. **Rejeté.**
- **(d) Manuel + rotation seule** : zéro alpha de découverte, M5 inutile. **Rejeté** comme stratégie primaire (mais reste utilisable via `TARGET_WALLETS` env, cf. §2.7).
- **(e) Goldsky subgraph en primary** : alternative valable et plus puissante (cf. §2.6) mais introduit une nouvelle dépendance critique. Repoussé en **backend secondaire opt-in** (`DISCOVERY_BACKEND=goldsky` ou `hybrid`), pas par défaut.

**Backups documentés** si `/holders` ou `/trades` casse :

- Backup 1 : `DISCOVERY_BACKEND=goldsky` → query `positions-subgraph` `orderBy: realizedPnl, orderDirection: desc`.
- Backup 2 : seed manuel via `TARGET_WALLETS` env (le user reprend le contrôle).

### 2.2 Métriques de scoring

**Recommandation : 4 métriques, formule pondérée. Score ∈ [0, 1].**

Métriques retenues (toutes calculables depuis Data API public, non-gameables individuellement) :

| Métrique | Source | Formule | Justification |
|---|---|---|---|
| `consistency` | `/positions` (positions résolues sur fenêtre `SCORING_LOOKBACK_DAYS`) | `nb_winning_positions / nb_resolved_positions` | Win rate ex-post. Robuste au cherry-picking si combiné avec `min_closed_markets`. |
| `roi` | `/positions` | `sum(realizedPnl) / sum(initialValue)` (clipped à `[-2.0, 2.0]`) puis remappé `[0,1]` via `(x+2)/4` | ROI réalisé sur la fenêtre. Clip pour éviter qu'un coup extrême ne sature. |
| `diversity` | `/activity?type=TRADE` | `1 - HHI(volume_per_market)` où `HHI = sum((vol_i / total_vol)^2)` | Indice Herfindahl normalisé. 1.0 = portefeuille bien diversifié, 0.0 = tout sur 1 marché ("one-hit wonder"). |
| `volume_score` | `/activity?type=TRADE` | `min(1.0, log10(total_usd_volume / 1000) / 3)` (cap à 1.0 pour `≥ $1M`) | Évite que des wallets minuscules avec 1 win lucky soient promus. Log scale pour ne pas surpondérer les whales. |

**Formule finale** :

```python
def compute_score(metrics: TraderMetrics) -> float:
    """v1 — pondération conservatrice."""
    if metrics.resolved_positions_count < settings.scoring_min_closed_markets:
        return 0.0  # cold start → low confidence flag
    consistency = metrics.win_rate                              # ∈ [0, 1]
    roi = max(-2.0, min(2.0, metrics.realized_roi))
    roi_norm = (roi + 2.0) / 4.0                                # ∈ [0, 1]
    diversity = 1.0 - metrics.herfindahl_index                  # ∈ [0, 1]
    volume_norm = min(1.0, math.log10(max(1, metrics.total_volume_usd) / 1000) / 3)
    score = (
        0.30 * consistency
        + 0.30 * roi_norm
        + 0.20 * diversity
        + 0.20 * volume_norm
    )
    return max(0.0, min(1.0, score))
```

**Anti-gaming explicite** :

- **PnL farming sur marchés évidents** (acheter à 0.99 quand le marché est presque résolu) : `consistency` reste haut mais `roi` est minuscule (peu de gain par dollar engagé), donc `roi_norm` n'augmente pas significativement. + `volume_score` log scale réduit l'incitation. **Mitigation : 70% du score n'est PAS lié au win rate seul.**
- **Wash trading** entre 2 wallets contrôlés : les wins compensent les losses (PnL net ~0), `consistency=0.5`, `roi_norm=0.5`. Score plafonne autour de 0.4-0.5, sous le seuil de promotion 0.65. **Mitigation : ROI réalisé doit être positif net.**
- **One-hit wonder** sur 1 marché viral : `diversity → 0`, `volume_score` faible (1 trade), `min_closed_markets` non atteint → score = 0. **Mitigation : seuil cold start dur.**
- **Pourquoi PAS juste `realized_pnl`** : gameable par whales sur marchés à faible volatilité où le PnL absolu est élevé mais l'edge nul. ROI normalisé > PnL absolu.

**Alternatives écartées** :

- **Sharpe-like ratio** : nécessite un timestamp fin par trade et le calcul du return par période, pénible avec `/positions` qui agrège. Reporté à v2 si la formule v1 sous-performe en backtest.
- **Drawdown max observé** : nécessite la série temporelle complète des positions. Coût API rédhibitoire pour 100+ wallets / cycle.
- **Holding time moyen** : peut être une signature de smart money (positions long-horizon) mais aussi de bagholders. Trop ambigu pour v1.

### 2.3 Fenêtre d'évaluation

**Recommandation : fenêtre fixe glissante de `SCORING_LOOKBACK_DAYS=90` (3 mois).**

- Permet de capturer un cycle complet de marchés (élections, sports saisonniers).
- Pas de décroissance exponentielle à v1 : ajout de complexité sans gain démontré, et dépend du rythme de publication des résolutions Polymarket.
- Cold start : `SCORING_MIN_CLOSED_MARKETS=10`. Sous ce seuil → `score=0.0`, flag `low_confidence=True` dans `trader_scores`. Ces wallets ne sont **jamais** promus (`score < promotion_threshold` automatiquement).
- Budget API par cycle : `DISCOVERY_CANDIDATE_POOL_SIZE=100` × 2 calls/wallet (positions + activity) = 200 calls. À 100 req/min documenté, le cycle prend ~2 min hors latence. OK pour `interval=6h`.

**Alternative écartée** : fenêtre glissante avec décroissance exponentielle `weight = exp(-age_days / 30)`. Conceptuellement séduisant (pondère récent > ancien), mais nécessite trade-by-trade granularity → 5x plus d'appels API. Reporté à v2 dans `SCORING_VERSION="v2"` si v1 sous-performe.

### 2.4 Rotation et cycle (cadence + hystérésis)

**Recommandation : cycle 6h, hystérésis 3 cycles, cap dur.**

- **Cadence** `DISCOVERY_INTERVAL_SECONDS=21600` (6h). Justification : Polymarket bouge à l'échelle de l'heure-jour, pas de la seconde. 6h = 4 cycles/jour, 28 cycles/sem → suffisant pour réagir à un changement de profil sans whipsaw.
- **Hystérésis demote** : `SCORING_DEMOTION_HYSTERESIS_CYCLES=3`. Un wallet doit avoir `score < SCORING_DEMOTION_THRESHOLD` pendant 3 cycles consécutifs avant `active → paused`. Tracking dans `target_traders.consecutive_low_score_cycles` (cf. §5.1). Reset à 0 si le score remonte au-dessus du seuil.
- **Promotion immédiate** (pas d'hystérésis) : un score élevé déclenche `shadow → active` au premier cycle qui la valide (sous réserve de `TRADER_SHADOW_DAYS` écoulés et `cap < MAX_ACTIVE_TRADERS`). Justification : on veut capter rapidement un trader qui s'améliore.
- **Cap actif** `MAX_ACTIVE_TRADERS=10`. Si dépassement potentiel par promotion : refuse, `Alert(WARNING, "discovery_cap_reached")`, log audit. Jamais retire arbitrairement un actif existant pour faire place.
- **Manual blacklist** `BLACKLISTED_WALLETS` : wallets jamais ajoutés, même top-scoring. Vérifié à 2 endroits : pre-bootstrap (filtrage du pool) et pre-promotion (defense in depth).

**Alternative écartée — cycle horaire** : trop agressif, pollue les logs et risque de tomber sur des fenêtres bruitées (ex: une heure sans résolution).

**Alternative écartée — cycle hebdo** : trop lent à réagir, un wallet peut basculer de "smart" à "réckless" sans qu'on s'en aperçoive pendant 7j.

### 2.5 Interaction avec `TARGET_WALLETS` existant

**Recommandation : `TARGET_WALLETS` = seed initial + whitelist permanente. M5 opt-in strict (`DISCOVERY_ENABLED=false` par défaut).**

- **Comportement par défaut (M4.5 → M5 sans changement `.env`)** : `DISCOVERY_ENABLED=false` → M5 totalement inactif. Le bot continue exactement comme M4.5 avec ses `TARGET_WALLETS`. **Backwards compat 100%.**
- **Avec `DISCOVERY_ENABLED=true`** : `TARGET_WALLETS` reste autoritaire — ces wallets sont **toujours** `status='active'`, **jamais** demote-able par M5 (champ `pinned: bool` à True dans `target_traders`, cf. §5.1). M5 peut **ajouter** d'autres wallets en `shadow` puis `active`, mais ne touche pas aux pinned.
- **Justification opt-in (sécurité capital)** : M5 modifie `target_traders.active` → impacte directement quels wallets le bot copie réellement. Une formule v1 mal calibrée pourrait promouvoir des traders médiocres. **L'utilisateur doit explicitement consentir** à laisser le bot choisir ses cibles. Cohérent avec la philosophie M4.5 (dashboard opt-in) et l'avertissement README ("trade à tes risques").
- **Documentation** : `.env.example` doit contenir un commentaire explicite "⚠️ DISCOVERY_ENABLED=true délègue à M5 le choix de quels wallets copier — n'active qu'après avoir audité ≥1 cycle complet via le dashboard /traders".

**Alternatives écartées** :

- **Override total** : si `TARGET_WALLETS` défini → M5 désactivé. Trop binaire, casse l'usage hybride légitime "j'ai 3 wallets favoris + je veux que tu en découvres 7 de plus".
- **Whitelist stricte** : M5 ne peut ajouter que dans `TARGET_WALLETS`. Annule l'intérêt de M5.

### 2.6 Architecture module + scheduling

**Recommandation : nouveau module `src/polycopy/discovery/`, scheduling `asyncio.sleep` interruptible (pattern M4 `PnlSnapshotWriter`).**

- **Nouveau module** `src/polycopy/discovery/` (analogue à `dashboard/`) plutôt qu'extension `watcher/` ou `strategy/` :
  - Cohérence règle CLAUDE.md "Aucun module ne dépend d'un autre module fonctionnel" → `discovery → storage` uniquement.
  - Évite de polluer `watcher/` (déjà hot path) avec une logique périodique lourde.
  - Permet d'évoluer sa formule sans toucher M1/M2/M3/M4.
- **Scheduling** : `asyncio.wait_for(stop_event.wait(), timeout=DISCOVERY_INTERVAL_SECONDS)` interruptible (même pattern que `PnlSnapshotWriter` M4 `pnl_writer.py`). **Pas d'APScheduler** : nouvelle dep, surface plus large, et notre besoin = "1 tâche périodique simple". Pas de dépendance ajoutée.
- **Pas de script CLI cron-système** : un cron externe perd l'accès au `stop_event` partagé (kill switch) et au `Settings` (re-parsing à chaque run). Inférieur en intégration.
- **Intégration `__main__`** : nouveau `DiscoveryOrchestrator.run_forever(stop_event)` ajouté au TaskGroup, garde par `if settings.discovery_enabled`. Identique au pattern M4.5 dashboard.

**Backend de découverte** (`DISCOVERY_BACKEND` env) :

- `"data_api"` (default) : Data API publique uniquement (`/holders`, `/trades`, `/positions`, `/activity`). Zéro nouvelle dep.
- `"goldsky"` : queries GraphQL aux subgraphs Goldsky. Plus puissant (top-N par realized PnL en 1 query) mais introduit `gql` ou `httpx` GraphQL custom. **Dep optionnelle dans `[project.optional-dependencies] discovery_goldsky`**, pas dans le critical path.
- `"hybrid"` : Goldsky pour le ranking initial, Data API pour la validation des metrics. Recommandé pour grosse volumétrie (>500 candidats).

À M5 v1, **default = `data_api`**. `goldsky` et `hybrid` documentés comme alternatives, testés a minima (cf. §9.7) mais pas en path critique.

**Règle de dépendance** (CLAUDE.md) :

```
discovery/  →  storage/   (TargetTraderRepository, TraderScoreRepository, TraderEventRepository)
discovery/  →  config       (Settings)
discovery/  →  monitoring/dtos    (Alert)   # uniquement le DTO, pas l'orchestrator
```

**Interdit** : `discovery → strategy`, `discovery → watcher`, `discovery → executor`. M5 communique uniquement via la DB (set `target_traders.active`) et la `alerts_queue`.

### 2.7 Persistance et historique

**Recommandation : 3 tables — `target_traders` étendu + `trader_scores` (append-only) + `trader_events` (audit append-only).**

- `target_traders` (existant) : ajouts §5.1 (`status`, `pinned`, `consecutive_low_score_cycles`, `discovered_at`, `promoted_at`, `last_scored_at`, `scoring_version`). Le champ `score` existant reste **overwrite par cycle** (latest score visible directement). `active: bool` reste pour backcompat code Watcher mais **dérivé** de `status='active'`.
- `trader_scores` (nouveau, append-only) : 1 ligne par (wallet, cycle). Conserve l'historique pour audit + comparaison de versions de formule. Cf. §5.2.
- `trader_events` (nouveau, append-only) : 1 ligne par décision (`add_shadow`, `promote_active`, `demote_paused`, `remove_pool`, `keep`, `discovery_skipped_blacklist`, `discovery_skipped_cap`). Cf. §5.3.

**Justification** :

- Overwrite `score` direct : utilisé en lecture par le dashboard / Watcher. Une simple colonne suffit, pas besoin de joindre `trader_scores` en chemin chaud.
- `trader_scores` append : permet "comment a évolué le score de 0xabc sur 30j ?" sans recalculer.
- `trader_events` append : audit légal "pourquoi ce wallet a-t-il été ajouté à mon `target_traders` le 12 mars 2026 ?". Réponse : ligne `trader_events` avec metrics qui ont motivé.

**Alternative écartée — overwrite seul** : perd l'historique, pas auditable.

**Alternative écartée — tout append, pas d'overwrite `score`** : Watcher devrait joindre `trader_scores ORDER BY cycle_at DESC LIMIT 1` à chaque tick. Coûteux, complique la lecture, casse le contrat existant (score colocalisé avec wallet).

**Migration Alembic** : nouvelle revision `0003_m5_discovery_schema.py`. Jamais modifier `0001_baseline_m3.py` ni `0002_m4_pnl_snapshot.py`.

### 2.8 Backtesting

**Recommandation : script `scripts/score_backtest.py` obligatoire AVANT mise en prod d'une formule.**

- **Source de données historiques** : pas la DB locale `detected_trades` (échantillon trop biaisé — uniquement les wallets déjà copiés). On re-fetch depuis `/activity?user=<addr>&start=<lookback>` et `/positions?user=<addr>`. Le script accepte une liste de wallets en input (`--wallets-file backtest_seed.txt`).
- **Logique** : pour chaque wallet input, calcule les metrics + score à un horizon T (param `--as-of YYYY-MM-DD`), puis observe la performance ex-post sur la fenêtre `[T, T+30j]`. Output : table CSV `wallet, score_at_T, observed_roi_t_to_t30, observed_resolved_count_t_to_t30`.
- **Critère d'acceptation v1** : sur un échantillon de **≥50 wallets** (top holders historiques), la **corrélation Spearman entre `score_at_T` et `observed_roi_t_to_t30` doit être ≥ 0.30** (positive et statistiquement significative à p<0.05). Si non atteint → on **n'active pas M5 en prod** (l'utilisateur doit retravailler la formule en `SCORING_VERSION=v2`).
- **Obligatoire dans M5** : la formule v1 ne doit pas être mise en `DISCOVERY_ENABLED=true` sans avoir produit un rapport backtest. Documenté dans `docs/setup.md` §14 (cf. §10.4).

**Script** :

```bash
python scripts/score_backtest.py \
  --wallets-file specs/m5_backtest_seed.txt \
  --as-of 2026-01-15 \
  --observe-days 30 \
  --output backtest_v1_report.html
```

`specs/m5_backtest_seed.txt` (créé à M5) contient ~50 adresses publiques de wallets actifs sur Polymarket (sourcées via `/holders` sur les top 5 marchés à T-90j). Le seed est versionné dans le repo (les adresses sont publiques, pas de PII).

### 2.9 Dashboard M4.5 — extensions M5

**Recommandation : 2 nouvelles pages au dashboard, modif minime.**

- **`/traders`** : table des wallets `target_traders` avec colonnes `wallet_address`, `label`, `status` (`shadow|active|paused|pinned`), `score`, `last_scored_at`, `pinned`, `consecutive_low_score_cycles`. Filtrage par `?status=...`. Sort par `score DESC` par défaut. HTMX polling 10s.
- **`/backtest`** : si un fichier `backtest_v1_report.html` existe à la racine, lien vers lui. Sinon message "Run `python scripts/score_backtest.py` to generate".
- **Modif Home KPIs** : ajouter `discovery_last_cycle_at`, `discovery_cycles_24h`, `discovery_promotions_24h`, `discovery_demotions_24h` (depuis `trader_events`).

Modif a minima : pas de refactor du dashboard, juste 2 templates + 2 routes + 1 query par route (cf. patterns `dashboard/queries.py`).

### 2.10 Alertes M4 — extensions M5

**Recommandation : 4 nouveaux events Telegram, cooldown par event_type.**

| Event | Niveau | Cooldown key | Quand |
|---|---|---|---|
| `trader_promoted` | INFO | `"trader_promoted_<wallet>"` | Wallet `shadow → active`. Body : wallet, score, metrics. |
| `trader_demoted` | WARNING | `"trader_demoted_<wallet>"` | Wallet `active → paused` après hystérésis. Body : wallet, score, cycles_under_threshold. |
| `discovery_cap_reached` | WARNING | `"discovery_cap_reached"` | Tentative de promote bloquée par `MAX_ACTIVE_TRADERS`. Cooldown 1h. |
| `discovery_cycle_failed` | ERROR | `"discovery_cycle_failed"` | Cycle a crashé après backoff. Body : exception type. |

Les alertes utilisent l'`alerts_queue` partagée (M4) — aucune nouvelle infrastructure.

---

## 3. Arborescence du module — `src/polycopy/discovery/`

```
src/polycopy/discovery/
├── __init__.py
├── dtos.py                    # DTOs Pydantic (CandidateWallet, TraderMetrics, ScoringResult, DiscoveryDecision)
├── data_api_client.py         # Client async pour /holders, /trades, /positions, /activity (étend les patterns M1/M3)
├── goldsky_client.py          # Client GraphQL Goldsky (opt-in via DISCOVERY_BACKEND)
├── candidate_pool.py          # Construction du pool : holders + global trades + dédup + cap
├── metrics_collector.py       # Pour 1 wallet → fetch positions+activity → compute TraderMetrics
├── scoring.py                 # compute_score(metrics, version) + table de versions ("v1", "v2", ...)
├── decision_engine.py         # promote/demote/keep/skip selon score + status + hystérésis + cap
├── orchestrator.py            # DiscoveryOrchestrator — entrée TaskGroup __main__
└── ...

scripts/
└── score_backtest.py          # CLI sync (asyncio.run interne) — backtest off-line

specs/
└── m5_backtest_seed.txt       # ~50 wallets publics seed pour backtest (versionné)
```

**Pas de `models.py` local** : ajouts `target_traders` + `trader_scores` + `trader_events` vivent dans `src/polycopy/storage/models.py` (cohérence avec M1-M4).

**Pas de `repositories.py` local** : `TraderScoreRepository` + `TraderEventRepository` + extensions `TargetTraderRepository` vivent dans `src/polycopy/storage/repositories.py`.

**Pas de `__main__.py` discovery** : pas de script CLI dédié. Tout passe par `python -m polycopy` + flag env.

---

## 4. API Polymarket (endpoints verrouillés + fixtures)

### 4.1 Endpoints utilisés (NON couverts par M1-M4.5)

| Endpoint | Statut skill | Fixture à capturer | Usage M5 |
|---|---|---|---|
| `GET https://data-api.polymarket.com/holders?market=<conditionId>&limit=20` | **Confirmé via doc tierce** (gist shaunlebron, polymarket-cli). **Non couvert par SKILL.md cached.** | `tests/fixtures/data_api_holders_sample.json` | Bootstrap candidats par marché. |
| `GET https://data-api.polymarket.com/trades?limit=500` | **Confirmé** (idem). Sans `user` = feed global. | `tests/fixtures/data_api_trades_global_sample.json` | Bootstrap candidats par feed global. |
| `GET https://data-api.polymarket.com/value?user=<addr>` | **Confirmé**. | `tests/fixtures/data_api_value_sample.json` | Sanity check capital total wallet. |
| `GET https://gamma-api.polymarket.com/markets?limit=50&order=liquidityNum&ascending=false&active=true&closed=false` | Étend l'usage Gamma de M2 (était condition_ids unique). | `tests/fixtures/gamma_top_markets_sample.json` | Liste top markets pour `/holders`. |
| `POST https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/positions-subgraph/0.0.7/gn` (GraphQL) | **Documenté tierce** (polytrackhq.app, thegraph.com docs). Schéma à confirmer empiriquement. | `tests/fixtures/goldsky_positions_topn_sample.json` | Backend `goldsky` opt-in. |

### 4.2 Endpoints réutilisés depuis M1-M3 (rappel pour cohérence)

| Endpoint | Fixture existante | Usage M5 |
|---|---|---|
| `GET /activity?user=<addr>&type=TRADE&start=<ts>&limit=500` | `tests/fixtures/activity_sample.json` (M1) | Calcul `total_volume_usd`, `herfindahl_index`. |
| `GET /positions?user=<addr>&limit=500&sortBy=CASHPNL&sortDirection=DESC` | `tests/fixtures/data_api_positions_sample.json` (M3) | Calcul `realized_pnl`, `win_rate`, positions résolues. |
| `GET /markets?condition_ids=<id>` | `tests/fixtures/gamma_market_sample.json` (M2) | Récupérer `endDate` pour distinguer marché résolu vs ouvert. |

### 4.3 Schéma `/holders` (capture obligatoire)

D'après gist shaunlebron + cli Polymarket :

```yaml
# Réponse = array<HolderGroup>
HolderGroup:
  token: string                    # token_id ERC1155 d'un outcome
  holders: array<Holder>

Holder:
  proxyWallet: string              # adresse du wallet (= "user" param d'autres endpoints)
  amount: number                   # taille en outcome tokens
  pseudonym: string | null
  outcomeIndex: integer
  # autres champs à confirmer empiriquement (name, profileImage, etc. — possibles)
```

**Query params** :

| Param | Type | Default | M5 usage |
|---|---|---|---|
| `market` | string (conditionId) | — | **obligatoire** |
| `limit` | int | 100 | `20` (top 20 par marché) |

**Pièges anticipés** :

- Multi-outcome : 1 marché binaire YES/NO retourne 2 `HolderGroup` (1 par token). M5 prend l'union des holders sans distinction (un wallet long YES et un wallet long NO sont tous deux candidats).
- Pas de pagination documentée → cap au `limit`.
- `outcomeIndex` peut servir à distinguer mais on l'ignore à v1.

**À capturer** : `curl 'https://data-api.polymarket.com/holders?market=<un_conditionId_actif>&limit=20'`.

### 4.4 Schéma `/trades` global (capture obligatoire)

```yaml
# Réponse = array<Trade> (max 500)
Trade:
  proxyWallet: string              # = wallet qui a fait le trade (taker par défaut)
  asset: string                    # token_id
  conditionId: string
  side: string                     # BUY | SELL
  size: number                     # shares
  price: number
  usdcSize: number                 # USD value du trade
  timestamp: integer (unix s)
  transactionHash: string
  title: string                    # nom du marché
  slug: string
  outcome: string
  # autres champs (pseudonym, profileImage) — à confirmer
```

**Query params** :

| Param | Type | Default | M5 usage |
|---|---|---|---|
| `limit` | int (max 500) | 100 | `500` |
| `offset` | int | 0 | non utilisé (1 page suffit pour le bootstrap, ~5-10 minutes de feed à fort volume) |
| `takerOnly` | bool | true | true (on veut voir qui prend l'initiative) |
| `filterType` | enum CASH \| TOKENS | — | `CASH` |
| `filterAmount` | number | — | `100` (USD min : exclut bots et micro-trades) |
| `market` | conditionId CSV | — | non utilisé en bootstrap (on veut diversité) |
| `user` | address | — | **JAMAIS pour M5 bootstrap** (sinon pas global). Présent uniquement pour validation downstream. |
| `side` | BUY \| SELL | — | non utilisé |

**Piège** : `takerOnly=true` exclut les market makers. Justifié pour M5 (on veut détecter les "smart money" qui prennent du risque, pas les MM).

**À capturer** : `curl 'https://data-api.polymarket.com/trades?limit=500&filterType=CASH&filterAmount=100&takerOnly=true'`.

### 4.5 Schéma `/value` (capture obligatoire)

```yaml
# Réponse = array<ValueEntry>
ValueEntry:
  user: string                     # = param user
  value: number                    # USD value totale des positions ouvertes
```

**Query params** :

| Param | Type | M5 usage |
|---|---|---|
| `user` | address | obligatoire |
| `market` | conditionId CSV | non utilisé |

Usage : sanity check rapide qu'un candidat a effectivement du capital engagé. Filtre les candidats `value < $50` avant d'appeler `/positions` complet (économie de calls).

**À capturer** : `curl 'https://data-api.polymarket.com/value?user=<addr>'`.

### 4.6 Gamma `/markets` extended (capture obligatoire)

Pour `list_top_markets` :

```
GET https://gamma-api.polymarket.com/markets
  ?limit=50
  &order=liquidityNum
  &ascending=false
  &active=true
  &closed=false
```

Schéma item identique à M2 `MarketMetadata` (cf. `specs/M2-strategy-engine.md` §5.3). Différence : on récupère un array de 50 marchés, pas 1 seul. Le DTO réutilise `MarketMetadata` existant — pas de nouveau modèle.

**Piège connu (M2)** : `clobTokenIds`, `outcomes`, `outcomePrices` sont des **strings JSON-stringifiées**. Le validator `mode="before"` existant gère.

### 4.7 Schéma Goldsky `positions-subgraph` (capture obligatoire si `discovery_backend=goldsky`)

```graphql
query TopByRealizedPnl($first: Int = 100) {
  positions(
    first: $first
    orderBy: realizedPnl
    orderDirection: desc
    where: { realizedPnl_gt: "0" }
  ) {
    user { id }            # adresse hex lowercase
    condition              # conditionId
    outcomeIndex
    balance                # shares restants
    averagePrice           # prix moyen entrée
    realizedPnl            # USD réalisé
  }
}
```

Réponse JSON :

```json
{
  "data": {
    "positions": [
      {
        "user": {"id": "0xabc..."},
        "condition": "0x4a67...",
        "outcomeIndex": 0,
        "balance": "12.5",
        "averagePrice": "0.34",
        "realizedPnl": "1234.56"
      }
    ]
  }
}
```

**Pièges anticipés** :

- Versions de subgraph changent (`positions-subgraph/0.0.7/gn` à T0, peut-être `0.0.8` à T+3 mois). URL paramétrée via `GOLDSKY_POSITIONS_SUBGRAPH_URL`.
- Numbers retournés en **string** (convention GraphQL/Goldsky pour BigInt/Decimal) — toujours parser via `float(s)` ou `Decimal(s)`.
- Pas d'authentification à v1 (fair use) ; si Goldsky impose token plus tard → ajout `GOLDSKY_API_KEY` env, no-log.
- `realizedPnl` est cumulé total, pas filtrable par fenêtre glissante directement. Pour `SCORING_LOOKBACK_DAYS=90`, il faut **filtrer côté client** ou utiliser `pnl-subgraph` séparé (à vérifier empiriquement).

**À capturer** :

```bash
curl -s 'https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/positions-subgraph/0.0.7/gn' \
  -H 'content-type: application/json' \
  -d '{"query":"{ positions(first: 5, orderBy: realizedPnl, orderDirection: desc) { user { id } condition realizedPnl balance } }"}' \
  > tests/fixtures/goldsky_positions_topn_sample.json
```

### 4.8 Rate limits + budget M5

| Endpoint | Rate limit documenté | Usage M5 par cycle (`pool=100`, `top_markets=20`) | Budget |
|---|---|---|---|
| `/holders` | non documenté ; ~100 req/min retenu (CLAUDE.md) | 20 calls (1 par marché top) | safe |
| `/trades` | idem | 1 call (limit=500) | safe |
| `/value` | idem | 100 calls (1 par candidat) | safe |
| `/positions` | idem (cache 30s déjà côté `WalletStateReader`) | 100 calls | safe |
| `/activity` | idem | 100 calls | safe |
| Gamma `/markets` | ~50 req/min retenu (CLAUDE.md) | 1 call | safe |
| Goldsky | "fair use" | 1-3 calls par cycle si backend opt-in | safe |

Total par cycle (data_api backend) : ~322 calls / 6h = **~0.9 req/min** moyenné. **Pic local** : 322 calls en ~3-5 min de cycle = ~100 req/min en pointe. **Risque** : on se rapproche du seuil documenté. **Mitigation §8.4** : throttle in-process à 60 req/min via `asyncio.Semaphore` ou `asyncio.sleep(0.05)` entre calls.

---

## 5. Modèles DB + migrations Alembic

### 5.1 `TargetTrader` — extensions

| Colonne | Type | Default | Notes |
|---|---|---|---|
| `id` | int PK | (existant) | inchangé |
| `wallet_address` | str(42) UNIQUE | (existant) | inchangé |
| `label` | str(64) \| None | (existant) | inchangé |
| `score` | float \| None | (existant) | **overwritten** à chaque cycle M5 (latest score) |
| `active` | bool | True (existant) | **dérivé** : `True ⟺ status='active' OR status='pinned'`. Maintenu en sync par M5 pour backcompat code Watcher (qui reste ignorant du `status`). |
| `added_at` | datetime UTC | (existant) | inchangé |
| `status` | str(8) | `'active'` | NOUVEAU. Enum `'shadow'` \| `'active'` \| `'paused'` \| `'pinned'`. Indexé. |
| `pinned` | bool | False | NOUVEAU. True ⟺ wallet vient de `TARGET_WALLETS` env. Jamais demote-able. |
| `consecutive_low_score_cycles` | int | 0 | NOUVEAU. Compteur d'hystérésis demote. Reset à 0 si score remonte ≥ promotion_threshold. |
| `discovered_at` | datetime UTC \| None | None | NOUVEAU. Timestamp d'auto-découverte (None si seed manuel). |
| `promoted_at` | datetime UTC \| None | None | NOUVEAU. Timestamp `shadow → active`. |
| `last_scored_at` | datetime UTC \| None | None | NOUVEAU. Dernier scoring cycle. |
| `scoring_version` | str(16) \| None | None | NOUVEAU. Version de la formule au dernier scoring. |

**Backcompat `active`** : à M5, `status='active'` ET `status='pinned'` impliquent `active=True`. `status='shadow'` ET `status='paused'` impliquent `active=False`. M5 maintient cette invariante manuellement (1 update SQL par transition). Le Watcher M1 continue de lire `WHERE active=True` (cf. `TargetTraderRepository.list_active`) — pas de modif Watcher requise. **À M5, on ajoute** un filtre supplémentaire dans `list_active` : `WHERE active=True AND status IN ('active', 'pinned')` pour defense in depth (cas où l'invariante glisse).

**Migration** : `alembic revision --autogenerate -m "m5_target_trader_extensions"` → `alembic/versions/0003_*.py`. Auditer manuellement (cf. piège M4 §7.4 SQLite ALTER TABLE limité). Backfill au upgrade :

```sql
UPDATE target_traders SET status = 'pinned' WHERE active = 1;
-- les wallets pre-M5 viennent forcément de TARGET_WALLETS env → pinned
```

### 5.2 `TraderScore` (nouveau, append-only)

| Colonne | Type | Contrainte |
|---|---|---|
| `id` | int | PK autoincrement |
| `target_trader_id` | int | FK logique → `target_traders.id`, **indexed** |
| `wallet_address` | str(42) | indexed (lookup direct sans join) |
| `score` | float | nullable=False, ∈ [0, 1] |
| `scoring_version` | str(16) | nullable=False, ex: `"v1"` |
| `cycle_at` | datetime UTC | nullable=False, **indexed** |
| `low_confidence` | bool | nullable=False, default False (True si cold start, score=0) |
| `metrics_snapshot` | JSON | nullable=False, dict des metrics qui ont produit ce score (`win_rate`, `realized_roi`, `herfindahl_index`, `total_volume_usd`, `resolved_positions_count`, etc.) |

Append-only. Pas d'update. Index composé `(wallet_address, cycle_at DESC)` pour requêtes "score history of X".

### 5.3 `TraderEvent` (nouveau, append-only)

| Colonne | Type | Contrainte |
|---|---|---|
| `id` | int | PK autoincrement |
| `wallet_address` | str(42) | nullable=False, **indexed** |
| `event_type` | str(32) | nullable=False, enum (`'discovered'`, `'scored'`, `'promoted_active'`, `'demoted_paused'`, `'kept'`, `'skipped_blacklist'`, `'skipped_cap'`, `'manual_override'`) |
| `at` | datetime UTC | nullable=False, **indexed** |
| `from_status` | str(8) \| None | ex: `"shadow"` |
| `to_status` | str(8) \| None | ex: `"active"` |
| `score_at_event` | float \| None | snapshot du score au moment de la décision |
| `scoring_version` | str(16) \| None | snapshot version |
| `reason` | str(128) \| None | message libre (ex: `"score 0.72 > threshold 0.65"`, `"3 cycles under 0.40"`) |
| `metadata` | JSON \| None | tout context utile (cycle_id, metrics, etc.) |

Append-only. Sert d'audit trail "pourquoi ce wallet a ce statut maintenant ?".

### 5.4 Migration Alembic — `alembic/versions/0003_m5_discovery_schema.py`

Étapes (autogenerate puis audit manuel) :

1. `ALTER TABLE target_traders ADD COLUMN status VARCHAR(8) NOT NULL DEFAULT 'active'` (SQLite friendly).
2. `ALTER TABLE target_traders ADD COLUMN pinned BOOLEAN NOT NULL DEFAULT 0`.
3. `ALTER TABLE target_traders ADD COLUMN consecutive_low_score_cycles INTEGER NOT NULL DEFAULT 0`.
4. 4 colonnes nullables `discovered_at`, `promoted_at`, `last_scored_at`, `scoring_version`.
5. Backfill `UPDATE target_traders SET status = 'pinned', pinned = 1 WHERE active = 1`.
6. CREATE TABLE `trader_scores` (cf. §5.2).
7. CREATE TABLE `trader_events` (cf. §5.3).
8. CREATE INDEX `ix_trader_scores_wallet_cycle` ON `trader_scores(wallet_address, cycle_at)`.
9. CREATE INDEX `ix_trader_events_wallet_at` ON `trader_events(wallet_address, at)`.

**Test obligatoire** `tests/unit/test_m5_alembic_migration.py` :

- Apply baseline + 0002 + 0003 → vérifie présence des 3 tables, colonnes, index.
- Test backfill : pre-insert un `target_traders` row avec `active=True`, run migration, vérifier `status='pinned'`.

**Pas de modif** sur `0001_baseline_m3_schema.py` ni `0002_m4_pnl_snapshot_columns.py`.

---

## 6. DTOs + repositories

### 6.1 DTOs Pydantic — `src/polycopy/discovery/dtos.py`

```python
class CandidateWallet(BaseModel):
    """Wallet candidat extrait du pool de découverte."""
    model_config = ConfigDict(frozen=True)

    wallet_address: str          # lowercase
    discovered_via: Literal["holders", "global_trades", "goldsky", "seed_target_wallets"]
    initial_signal: float        # score préliminaire (volume, fréquence holder, etc.)
    sample_market: str | None    # 1 marché où on l'a vu (debug)


class TraderMetrics(BaseModel):
    """Metrics agrégées sur la fenêtre SCORING_LOOKBACK_DAYS d'un wallet."""
    model_config = ConfigDict(frozen=True)

    wallet_address: str
    resolved_positions_count: int
    open_positions_count: int
    win_rate: float                  # ∈ [0, 1]
    realized_roi: float              # peut être négatif, clipped en scoring
    total_volume_usd: float
    herfindahl_index: float          # ∈ [0, 1] (1.0 = tout sur 1 marché)
    nb_distinct_markets: int
    largest_position_value_usd: float
    measurement_window_days: int     # = SCORING_LOOKBACK_DAYS
    fetched_at: datetime


class ScoringResult(BaseModel):
    """Résultat du scoring d'un wallet pour 1 cycle."""
    model_config = ConfigDict(frozen=True)

    wallet_address: str
    score: float                  # ∈ [0, 1]
    scoring_version: str
    low_confidence: bool          # True si cold start
    metrics: TraderMetrics
    cycle_at: datetime


class DiscoveryDecision(BaseModel):
    """Décision prise par le decision_engine pour 1 wallet sur 1 cycle."""
    model_config = ConfigDict(frozen=True)

    wallet_address: str
    decision: Literal["promote_active", "demote_paused", "keep", "skip_blacklist", "skip_cap", "discovered_shadow"]
    from_status: Literal["shadow", "active", "paused", "pinned", "absent"] | None
    to_status: Literal["shadow", "active", "paused", "pinned"]
    score_at_event: float | None
    scoring_version: str
    reason: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class TraderScoreDTO(BaseModel):
    """DTO pour TraderScoreRepository.insert."""
    model_config = ConfigDict(frozen=True)

    target_trader_id: int
    wallet_address: str
    score: float
    scoring_version: str
    low_confidence: bool
    metrics_snapshot: dict[str, Any]


class TraderEventDTO(BaseModel):
    """DTO pour TraderEventRepository.insert."""
    model_config = ConfigDict(frozen=True)

    wallet_address: str
    event_type: Literal[
        "discovered", "scored", "promoted_active", "demoted_paused",
        "kept", "skipped_blacklist", "skipped_cap", "manual_override",
    ]
    from_status: str | None = None
    to_status: str | None = None
    score_at_event: float | None = None
    scoring_version: str | None = None
    reason: str | None = None
    metadata: dict[str, Any] | None = None
```

### 6.2 Repositories — `src/polycopy/storage/repositories.py` (extensions)

```python
class TargetTraderRepository:
    # méthodes existantes (list_active, upsert) restent

    async def list_active(self) -> list[TargetTrader]:
        """Retourne traders avec status ∈ {'active', 'pinned'} ET active=True (defense)."""
        ...

    async def list_all(self) -> list[TargetTrader]:
        """Retourne TOUS les traders (toutes statuts), pour cycle de scoring."""
        ...

    async def list_by_status(self, status: Literal["shadow", "active", "paused", "pinned"]) -> list[TargetTrader]:
        ...

    async def update_score(
        self, wallet_address: str, *, score: float, scoring_version: str
    ) -> None:
        """Overwrite target_traders.score + last_scored_at + scoring_version."""
        ...

    async def transition_status(
        self,
        wallet_address: str,
        *,
        new_status: Literal["shadow", "active", "paused"],
        reset_hysteresis: bool = False,
    ) -> None:
        """Atomic status change. Met active=True si new_status='active', sinon False.
        Set promoted_at si shadow→active. Reset consecutive_low_score_cycles si demandé.
        Ne touche jamais aux wallets pinned (raise ValueError)."""
        ...

    async def insert_shadow(
        self,
        wallet_address: str,
        *,
        label: str | None = None,
        discovered_at: datetime,
    ) -> TargetTrader:
        """Insert en status='shadow', active=False, pinned=False, discovered_at=...."""
        ...

    async def increment_low_score(self, wallet_address: str) -> int:
        """Incrémente consecutive_low_score_cycles, retourne la nouvelle valeur."""
        ...

    async def reset_low_score(self, wallet_address: str) -> None:
        ...


class TraderScoreRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None: ...

    async def insert(self, dto: TraderScoreDTO) -> TraderScore: ...

    async def latest_for_wallet(self, wallet_address: str) -> TraderScore | None: ...

    async def list_for_wallet(
        self, wallet_address: str, *, limit: int = 100
    ) -> list[TraderScore]: ...

    async def latest_per_wallet(self, *, limit: int = 200) -> list[TraderScore]:
        """Pour le dashboard /traders : 1 ligne par wallet, le score le plus récent."""
        ...


class TraderEventRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None: ...

    async def insert(self, dto: TraderEventDTO) -> TraderEvent: ...

    async def list_recent(
        self, *, since: datetime | None = None, limit: int = 200
    ) -> list[TraderEvent]: ...

    async def count_by_event_type_since(
        self, since: datetime
    ) -> dict[str, int]:
        """Pour KPIs Home dashboard."""
        ...
```

Style SQLAlchemy 2.0, async, sessions courtes, `select()`. Append-only sur les 2 nouveaux repos.

---

## 7. Scoring engine — `src/polycopy/discovery/scoring.py`

### 7.1 Fonction principale

```python
SCORING_VERSIONS_REGISTRY: dict[str, Callable[[TraderMetrics, Settings], float]] = {
    "v1": _compute_score_v1,
    # "v2": _compute_score_v2,  # à ajouter si v1 sous-performe
}


def compute_score(metrics: TraderMetrics, *, settings: Settings) -> tuple[float, bool]:
    """Calcule le score selon settings.scoring_version.

    Retourne (score, low_confidence_flag). low_confidence=True si le wallet
    n'a pas atteint scoring_min_closed_markets (cold start).
    """
    fn = SCORING_VERSIONS_REGISTRY.get(settings.scoring_version)
    if fn is None:
        raise ValueError(f"Unknown SCORING_VERSION: {settings.scoring_version}")
    if metrics.resolved_positions_count < settings.scoring_min_closed_markets:
        return 0.0, True
    return fn(metrics, settings), False
```

### 7.2 `_compute_score_v1`

```python
def _compute_score_v1(metrics: TraderMetrics, settings: Settings) -> float:
    """Formule v1 : 0.30·consistency + 0.30·roi_norm + 0.20·diversity + 0.20·volume_norm."""
    consistency = max(0.0, min(1.0, metrics.win_rate))
    roi_clipped = max(-2.0, min(2.0, metrics.realized_roi))
    roi_norm = (roi_clipped + 2.0) / 4.0
    diversity = max(0.0, min(1.0, 1.0 - metrics.herfindahl_index))
    volume_norm = min(
        1.0,
        max(0.0, math.log10(max(1.0, metrics.total_volume_usd) / 1000.0) / 3.0),
    )
    score = 0.30 * consistency + 0.30 * roi_norm + 0.20 * diversity + 0.20 * volume_norm
    return max(0.0, min(1.0, score))
```

### 7.3 Calcul `TraderMetrics` — `metrics_collector.py`

```python
class MetricsCollector:
    def __init__(
        self,
        data_api: DiscoveryDataApiClient,
        gamma: GammaApiClient,
        settings: Settings,
    ) -> None: ...

    async def collect(self, wallet_address: str) -> TraderMetrics:
        """Fetch positions + activity, calcule les metrics."""
        ...
```

Pseudocode :

```python
async def collect(self, wallet_address: str) -> TraderMetrics:
    since_dt = utc_now() - timedelta(days=settings.scoring_lookback_days)
    positions = await data_api.get_positions(wallet_address)
    activity = await data_api.get_activity_trades(wallet_address, since=since_dt)

    # Win rate ex-post : positions avec realizedPnl != 0 (= résolues)
    resolved = [p for p in positions if p.realized_pnl_nonzero]
    wins = sum(1 for p in resolved if p.cash_pnl > 0)
    win_rate = wins / len(resolved) if resolved else 0.0

    # ROI réalisé sur la fenêtre
    total_initial = sum(p.initial_value for p in resolved)
    total_realized = sum(p.realized_pnl for p in resolved)
    realized_roi = total_realized / total_initial if total_initial > 0 else 0.0

    # Volume + diversity sur activity
    volume_per_market: dict[str, float] = defaultdict(float)
    for trade in activity:
        volume_per_market[trade.condition_id] += trade.usdc_size
    total_volume = sum(volume_per_market.values())
    if total_volume > 0:
        hhi = sum((v / total_volume) ** 2 for v in volume_per_market.values())
    else:
        hhi = 1.0  # pas de diversité par défaut

    return TraderMetrics(
        wallet_address=wallet_address,
        resolved_positions_count=len(resolved),
        open_positions_count=len(positions) - len(resolved),
        win_rate=win_rate,
        realized_roi=realized_roi,
        total_volume_usd=total_volume,
        herfindahl_index=hhi,
        nb_distinct_markets=len(volume_per_market),
        largest_position_value_usd=max((p.current_value for p in positions), default=0.0),
        measurement_window_days=settings.scoring_lookback_days,
        fetched_at=utc_now(),
    )
```

**Définition "position résolue"** : `realizedPnl != 0` ou `redeemable=True` ou `currentValue == 0` (heuristique : si Polymarket ne donne pas de flag explicite, ces 3 indicateurs combinés capturent les positions clôturées). À confirmer empiriquement sur la fixture `data_api_positions_sample.json`.

### 7.4 Cold start

`SCORING_MIN_CLOSED_MARKETS=10`. Si `resolved_positions_count < 10`, `compute_score` retourne `(0.0, True)`. Le wallet est inséré dans `trader_scores` avec `low_confidence=True` mais ne sera jamais promu (`score < promotion_threshold`). Au cycle suivant, si le wallet a cumulé d'autres résolutions et atteint 10, il est scoré normalement.

### 7.5 Décision — `decision_engine.py`

```python
class DecisionEngine:
    def __init__(
        self,
        target_repo: TargetTraderRepository,
        event_repo: TraderEventRepository,
        settings: Settings,
        alerts_queue: asyncio.Queue[Alert] | None,
    ) -> None: ...

    async def decide(
        self, scoring: ScoringResult, current_state: TargetTrader | None,
    ) -> DiscoveryDecision: ...
```

Logique :

```
if wallet ∈ BLACKLISTED_WALLETS:
    return skip_blacklist (no DB write)
if current_state is None:
    if score >= promotion_threshold AND active_count < MAX_ACTIVE_TRADERS:
        if TRADER_SHADOW_DAYS == 0 AND DISCOVERY_SHADOW_BYPASS:
            insert as 'active' directly (alert: trader_promoted)
        else:
            insert as 'shadow' (event: discovered, alert: trader_shadow_added)
    else:
        ignore (no DB write — pas de bruit pour les low scores cold start)
elif current_state.pinned:
    return keep (jamais touché)
elif current_state.status == 'shadow':
    if (now - discovered_at).days >= TRADER_SHADOW_DAYS AND score >= promotion_threshold:
        if active_count < MAX_ACTIVE_TRADERS:
            transition_status('active') (event: promoted_active, alert: trader_promoted)
        else:
            return skip_cap (event: skipped_cap, alert: discovery_cap_reached)
    else:
        return keep (still observing)
elif current_state.status == 'active':
    if score < demotion_threshold:
        new_count = increment_low_score(wallet)
        if new_count >= demotion_hysteresis_cycles:
            transition_status('paused') (event: demoted_paused, alert: trader_demoted)
        else:
            return keep
    else:
        reset_low_score(wallet)
        return keep
elif current_state.status == 'paused':
    if score >= promotion_threshold:
        # Auto-revival possible : on remet en shadow pour ré-observation
        transition_status('shadow', reset_hysteresis=True) (event: discovered/revived)
    else:
        return keep
```

Tous les events DB + alerts vivent dans cette fonction. Atomic via 1 session par décision.

### 7.6 Reproducibilité

Chaque ligne `trader_scores` et `trader_events` écrit `scoring_version`. À l'avenir, `SCORING_VERSION="v2"` :

- Ajouter `_compute_score_v2` dans `SCORING_VERSIONS_REGISTRY`.
- Pas de rewrite de l'historique (cf. §13).
- Le dashboard `/traders` peut afficher le mix de versions (utile pour audit transition).

---

## 8. Discovery loop + orchestrator + intégration `__main__`

### 8.1 `DiscoveryOrchestrator` — `src/polycopy/discovery/orchestrator.py`

```python
class DiscoveryOrchestrator:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        alerts_queue: asyncio.Queue[Alert] | None = None,
    ) -> None:
        self._sf = session_factory
        self._settings = settings
        self._alerts = alerts_queue

    async def run_forever(self, stop_event: asyncio.Event) -> None: ...
```

`run_forever` :

```python
async def run_forever(self, stop_event):
    log.info(
        "discovery_starting",
        interval_s=self._settings.discovery_interval_seconds,
        pool_size=self._settings.discovery_candidate_pool_size,
        backend=self._settings.discovery_backend,
        scoring_version=self._settings.scoring_version,
    )
    if (
        self._settings.trader_shadow_days == 0
        and self._settings.discovery_shadow_bypass
    ):
        log.warning(
            "discovery_shadow_bypass_enabled",
            reason="auto_promote_immediate",
        )
    async with httpx.AsyncClient() as http_client:
        data_api = DiscoveryDataApiClient(http_client)
        gamma = GammaApiClient(http_client)
        goldsky = GoldskyClient(http_client, self._settings) \
            if self._settings.discovery_backend != "data_api" else None
        candidate_pool = CandidatePool(data_api, gamma, goldsky, self._settings)
        metrics_collector = MetricsCollector(data_api, gamma, self._settings)
        target_repo = TargetTraderRepository(self._sf)
        score_repo = TraderScoreRepository(self._sf)
        event_repo = TraderEventRepository(self._sf)
        decision_engine = DecisionEngine(target_repo, event_repo, self._settings, self._alerts)
        log.info("discovery_started")
        while not stop_event.is_set():
            try:
                await self._run_one_cycle(
                    candidate_pool, metrics_collector, score_repo, event_repo,
                    target_repo, decision_engine,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("discovery_cycle_failed")
                self._push_alert(
                    Alert(level="ERROR", event="discovery_cycle_failed",
                          body="cycle exception", cooldown_key="discovery_cycle_failed")
                )
                await self._sleep_or_stop(stop_event, 60.0)
                continue
            await self._sleep_or_stop(stop_event, self._settings.discovery_interval_seconds)
    log.info("discovery_stopped")
```

### 8.2 `_run_one_cycle`

```python
async def _run_one_cycle(self, pool, mc, score_repo, event_repo, target_repo, engine):
    cycle_at = utc_now()
    log.info("discovery_cycle_started", cycle_at=cycle_at.isoformat())
    t0 = time.monotonic()

    # 1. Build candidate pool
    candidates = await pool.build()
    blacklist = {w.lower() for w in self._settings.blacklisted_wallets}
    candidates = [c for c in candidates if c.wallet_address not in blacklist]
    log.info("discovery_candidates_built", count=len(candidates))

    # 2. Merge with existing target_traders (ré-évaluation des shadow + active + paused)
    existing = await target_repo.list_all()
    existing_by_wallet = {t.wallet_address: t for t in existing}
    all_to_score = list({c.wallet_address for c in candidates} | set(existing_by_wallet.keys()))

    # 3. Score chaque wallet (avec throttle)
    sem = asyncio.Semaphore(5)  # max 5 wallets en parallèle (= ~10 calls/sec en pic)
    promoted = demoted = kept = 0

    async def _score_one(wallet: str):
        async with sem:
            try:
                metrics = await mc.collect(wallet)
            except Exception:
                log.exception("metrics_collect_failed", wallet=wallet)
                return
            score, low_conf = compute_score(metrics, settings=self._settings)
            scoring = ScoringResult(
                wallet_address=wallet, score=score,
                scoring_version=self._settings.scoring_version,
                low_confidence=low_conf, metrics=metrics, cycle_at=cycle_at,
            )
            current = existing_by_wallet.get(wallet)
            tt_id = current.id if current is not None else None
            if tt_id is not None:
                await score_repo.insert(TraderScoreDTO(
                    target_trader_id=tt_id, wallet_address=wallet,
                    score=score, scoring_version=self._settings.scoring_version,
                    low_confidence=low_conf, metrics_snapshot=metrics.model_dump(),
                ))
                await target_repo.update_score(
                    wallet, score=score, scoring_version=self._settings.scoring_version,
                )
            decision = await engine.decide(scoring, current)
            await event_repo.insert(TraderEventDTO(
                wallet_address=wallet, event_type=_decision_to_event(decision.decision),
                from_status=decision.from_status, to_status=decision.to_status,
                score_at_event=decision.score_at_event,
                scoring_version=self._settings.scoring_version,
                reason=decision.reason, metadata=decision.metadata,
            ))
            # update counters via nonlocal locks…

    await asyncio.gather(*(_score_one(w) for w in all_to_score))

    duration_ms = int((time.monotonic() - t0) * 1000)
    log.info(
        "discovery_cycle_completed",
        candidates_seen=len(candidates),
        scored=len(all_to_score),
        promoted=promoted, demoted=demoted, kept=kept,
        duration_ms=duration_ms,
    )
```

### 8.3 Throttle Data API

`asyncio.Semaphore(5)` limite à 5 wallets en parallèle. Avec ~2-3 calls/wallet, pic ~15 calls in flight = OK pour ~100 req/min. Si besoin d'aller plus prudent, ajouter `asyncio.sleep(0.05)` entre calls dans `DiscoveryDataApiClient._fetch_page`.

### 8.4 Intégration `__main__`

```python
discovery: DiscoveryOrchestrator | None = None
if settings.discovery_enabled:
    discovery = DiscoveryOrchestrator(session_factory, settings, alerts_queue)

async with asyncio.TaskGroup() as tg:
    tg.create_task(watcher.run_forever(stop_event))
    tg.create_task(strategy.run_forever(stop_event))
    tg.create_task(executor.run_forever(stop_event))
    tg.create_task(monitoring.run_forever(stop_event))
    if dashboard is not None:
        tg.create_task(dashboard.run_forever(stop_event))
    if discovery is not None:
        tg.create_task(discovery.run_forever(stop_event))
```

Si `discovery_enabled=false` : zéro instanciation, zéro overhead. Identique au pattern dashboard M4.5.

### 8.5 Refactor minimal `WatcherOrchestrator.list_active`

Vérifier que `TargetTraderRepository.list_active` filtre `status IN ('active', 'pinned')` en plus de `active=True`. Defense in depth — l'invariante doit déjà tenir mais on protège contre une corruption M5.

Aucun autre changement Watcher/Strategy/Executor.

---

## 9. Tests

### 9.1 Arborescence

```
tests/
├── fixtures/
│   ├── (M1-M4.5 existants)
│   ├── data_api_holders_sample.json
│   ├── data_api_trades_global_sample.json
│   ├── data_api_value_sample.json
│   ├── gamma_top_markets_sample.json
│   └── goldsky_positions_topn_sample.json
├── unit/
│   ├── (M1-M4.5 existants)
│   ├── test_discovery_dtos.py
│   ├── test_discovery_data_api_client.py
│   ├── test_goldsky_client.py
│   ├── test_candidate_pool.py
│   ├── test_metrics_collector.py
│   ├── test_scoring_v1.py
│   ├── test_decision_engine.py
│   ├── test_discovery_orchestrator.py
│   ├── test_target_trader_repo_extensions.py
│   ├── test_trader_score_repository.py
│   ├── test_trader_event_repository.py
│   ├── test_m5_alembic_migration.py
│   └── test_score_backtest_script.py
└── integration/
    ├── (existants)
    ├── test_data_api_holders_live.py     # @pytest.mark.integration
    └── test_goldsky_positions_live.py    # @pytest.mark.integration, opt-in
```

### 9.2 `test_discovery_data_api_client.py` (respx)

- Mock `/holders?market=<id>&limit=20` → array de holders. Vérifier dédup par `proxyWallet`.
- Mock `/trades?limit=500&filterAmount=100&takerOnly=true` → array trades. Vérifier query params exacts.
- 429 retry tenacity.
- 404 `/holders` → retourne `[]` sans crash.

### 9.3 `test_candidate_pool.py`

- Mock data_api (holders + global trades) + gamma top_markets → `CandidatePool.build()` retourne union dédup.
- Cap respecté : si union > `pool_size`, tronqué selon score préliminaire.
- Blacklist filtrée AVANT cap (pas de wallet noir dans le résultat).
- Pas d'inclusion des `target_traders.status='paused'` (respect intention manuelle de l'utilisateur).

### 9.4 `test_metrics_collector.py`

- Fixture `data_api_positions_sample.json` (M3) + `activity_sample.json` (M1) → assert `TraderMetrics` champs cohérents.
- Wallet sans trades → `total_volume_usd=0`, `herfindahl_index=1.0`, `win_rate=0`.
- Wallet avec 1 marché unique → `herfindahl_index=1.0`.
- Wallet avec N marchés équilibrés → `herfindahl_index ≈ 1/N`.
- Position résolue détection : `realizedPnl != 0` OU `currentValue == 0`.

### 9.5 `test_scoring_v1.py`

- Cold start (`resolved_positions_count < 10`) → `score=0.0`, `low_confidence=True`.
- Profil parfait (win_rate=1, roi=2, hhi=0, vol=$10M) → score proche de 1.0 (mais ≤ 1.0).
- Profil zéro (win_rate=0, roi=-2, hhi=1, vol=0) → score proche de 0.0.
- Wash trading simulé (win_rate=0.5, roi=0, hhi=0.5, vol=$50k) → score < `promotion_threshold` (0.65).
- Whale 1-marché (win_rate=1, roi=0.5, hhi=1, vol=$1M) → score plafonné par diversity=0.
- Property test (`hypothesis`) : score toujours dans [0, 1] pour metrics arbitraires.
- Versions : `SCORING_VERSION="unknown"` raise `ValueError`.

### 9.6 `test_decision_engine.py`

- Wallet absent + score>=promotion + cap OK + shadow_days>0 → décision `discovered_shadow`, insert en `shadow`.
- Wallet absent + score>=promotion + shadow_days=0 + bypass=true → décision `promote_active` direct.
- Wallet absent + score>=promotion + cap atteint → `skip_cap`, alerte `discovery_cap_reached`.
- Wallet shadow + days écoulés + score OK + cap OK → `promote_active`.
- Wallet shadow + days écoulés + cap atteint → `skip_cap`.
- Wallet active + score < demotion + 1 cycle → `keep`, increment counter.
- Wallet active + score < demotion + 3 cycles → `demote_paused`, reset counter.
- Wallet active + score < demotion 1 cycle, puis ≥ demotion → reset counter.
- Wallet pinned → toujours `keep` (jamais demote, jamais touch status).
- Wallet blacklist → `skip_blacklist`, aucun write DB ni alert.
- Wallet paused + score >= promotion → réinjection en `shadow` (revival).

### 9.7 `test_goldsky_client.py` (respx)

- Mock POST GraphQL → 200 avec payload positions array → DTO valide.
- Erreur GraphQL (200 + `{"errors": [...]}`) → raise + retry.
- Pagination via `first` et `skip`.
- Backend goldsky désactivé → `GoldskyClient` non instancié dans orchestrator.

### 9.8 `test_discovery_orchestrator.py`

- Init avec `discovery_enabled=true` : `run_forever(stop_event)` lance, set `stop_event` après 1 cycle, sort proprement.
- 1 cycle complet en bout-en-bout (mocks data_api + gamma) : asserts sur le nombre de promotions/demotions selon scénario.
- Cycle qui crash (mock data_api 500 persistant) → log `discovery_cycle_failed` + alert + backoff + retry.
- `discovery_shadow_bypass=true` ET `trader_shadow_days=0` au boot → log warning `discovery_shadow_bypass_enabled`.
- `MAX_ACTIVE_TRADERS=2` + 5 candidats top-scoring + 0 actifs → 2 premières promotions, 3 `skip_cap` + alerts.

### 9.9 `test_target_trader_repo_extensions.py`

- `insert_shadow` → row avec `status='shadow'`, `active=False`, `discovered_at` set.
- `transition_status('active')` → `active=True`, `promoted_at` set, `consecutive_low_score_cycles=0` si `reset_hysteresis=True`.
- `transition_status` sur pinned → raise `ValueError`.
- `update_score` overwrite, met `last_scored_at` + `scoring_version`.
- `list_active` ne retourne PAS les `shadow` ni `paused`.
- `increment_low_score` retourne nouvelle valeur incrémentée.
- `reset_low_score` met à 0.

### 9.10 `test_trader_score_repository.py` + `test_trader_event_repository.py`

- Append-only : pas de méthode update/delete exposée.
- `latest_for_wallet` retourne le dernier score.
- `latest_per_wallet` retourne 1 ligne par wallet (subquery max).
- `count_by_event_type_since` agrège correctement.

### 9.11 `test_m5_alembic_migration.py`

```python
def test_alembic_upgrade_to_0003_creates_m5_tables(tmp_path):
    db_path = tmp_path / "test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("target_traders")}
    assert {"status", "pinned", "consecutive_low_score_cycles",
            "discovered_at", "promoted_at", "last_scored_at",
            "scoring_version"} <= cols
    assert "trader_scores" in insp.get_table_names()
    assert "trader_events" in insp.get_table_names()


def test_backfill_pinned_for_existing_active(tmp_path):
    # apply 0001+0002, insert active trader, apply 0003, vérifier pinned=1
    ...
```

### 9.12 `test_score_backtest_script.py`

- Run `score_backtest.py` sur DB vide + 0 wallets → ne crash pas, message clair.
- Run avec 3 wallets fixtures + mock data_api → CSV avec 3 lignes, headers OK.
- Spearman corr calculée correctement (test sur dataset jouet).

### 9.13 Tests live opt-in (`@pytest.mark.integration`)

```python
@pytest.mark.integration
async def test_holders_live():
    # fetch top 5 holders d'un marché actif connu, vérifier schéma
    ...

@pytest.mark.integration
async def test_goldsky_positions_live():
    # 1 query top-10, vérifier que ça revient avec realizedPnl > 0
    ...
```

### 9.14 Couverture

```bash
pytest --cov=src/polycopy/discovery --cov-report=term-missing
```

Seuil : **≥ 80% sur `src/polycopy/discovery/`** (équivalent zone critique avec executor/strategy). M1..M4.5 ≥ 80% (pas de régression — refactor minimal `TargetTraderRepository.list_active` doit garder les tests M1 verts).

---

## 10. Mises à jour de documentation (même PR)

### 10.1 `README.md`

- Cocher M5 dans "Roadmap" : `[x] **M5** : Scoring de traders + sélection automatique`.
- Ajouter à la table env vars : 14 nouvelles lignes (cf. §0.3).
- Nouvelle section **"Découverte automatique de traders (optionnel, M5)"** :

```markdown
## Découverte automatique (optionnel, M5)

Polycopy peut découvrir et scorer des wallets candidats automatiquement, puis promouvoir les meilleurs en cibles actives. **Opt-in strict** : par défaut, le bot ne suit que `TARGET_WALLETS`.

### Activation

```env
DISCOVERY_ENABLED=true
DISCOVERY_INTERVAL_SECONDS=21600   # 6h
MAX_ACTIVE_TRADERS=10              # plafond dur
TRADER_SHADOW_DAYS=7               # observation avant promotion réelle
SCORING_VERSION=v1                 # formule actuelle
```

⚠️ Avant d'activer, **lance le backtest** :

```bash
python scripts/score_backtest.py --as-of 2026-01-15 --observe-days 30 \
  --output backtest_v1_report.html
```

Tu dois obtenir une corrélation Spearman ≥ 0.30 entre `score_at_T` et `observed_roi_t_to_t30`. Sinon → ne pas activer M5 en prod.

### Comment ça marche

1. Toutes les 6h, M5 scanne les top-holders des marchés Polymarket actifs + le feed global de trades.
2. Pour chaque candidat, fetch positions et activité publiques, calcule un score ∈ [0, 1].
3. Wallets avec score ≥ 0.65 sont mis en `shadow` (observation 7j) puis promus en `active` (suivis par le watcher pour copy).
4. Wallets actifs avec score < 0.40 pendant 3 cycles consécutifs sont `paused` (plus copiés).
5. Tes `TARGET_WALLETS` restent `pinned` — jamais retirés par M5.

Dashboard : `http://127.0.0.1:8787/traders` montre tous les wallets avec scores et statuts.
```

### 10.2 `docs/architecture.md`

Ajouter section **après Dashboard** :

```markdown
## Module : Discovery (optionnel)

> **Status M5** ✅ — implémenté. Module de découverte et scoring de wallets candidats.
> Lancé dans le `asyncio.TaskGroup` du `__main__` si `DISCOVERY_ENABLED=true`.
> Read-only sur la Data API publique + Gamma + Goldsky (opt-in). Aucune signature CLOB.
> Voir `specs/M5-trader-scoring.md` et `src/polycopy/discovery/`.

- **Pool de candidats** : top holders des top markets + feed global de trades.
- **Metrics** : win rate, ROI réalisé, indice Herfindahl (diversité), volume (log scale).
- **Score v1** : `0.30·consistency + 0.30·roi + 0.20·diversity + 0.20·volume`. Versionné via `SCORING_VERSION`.
- **Statuts** : `shadow` (observation) → `active` (copié) → `paused` (retiré). `pinned` = jamais touché (`TARGET_WALLETS`).
- **Garde-fous** : `MAX_ACTIVE_TRADERS` cap dur, `TRADER_SHADOW_DAYS` observation obligatoire avant copy, `BLACKLISTED_WALLETS` exclusion absolue, hystérésis `K=3` cycles avant demote.
```

### 10.3 `CLAUDE.md`

Section "Architecture (rappel)" — ajouter `discovery/` :

```
├── discovery/    Pool candidats + scoring + decisions (M5, opt-in, read-only)
```

Section "APIs Polymarket utilisées" — ajouter :

```markdown
- **Data API endpoints M5** :
  - `GET /holders?market=<conditionId>` — top holders d'un marché (bootstrap).
  - `GET /trades?limit=500&filterAmount=100&takerOnly=true` — feed global (bootstrap).
  - `GET /value?user=<addr>` — sanity check capital.
- **Goldsky subgraph** (`positions-subgraph` v0.0.7) — backend opt-in `DISCOVERY_BACKEND=goldsky`. URL versionnée via `GOLDSKY_POSITIONS_SUBGRAPH_URL`. Numbers retournés en string (parser via `Decimal`).
- **Pas de leaderboard endpoint** côté Polymarket public — bootstrap dérivé manuellement (cf. `specs/M5-trader-scoring.md` §4).
```

Section "Sécurité — RÈGLES STRICTES" — ajouter :

```markdown
- **Discovery M5** : `DISCOVERY_ENABLED=false` par défaut. Read-only stricte (Data API + Gamma + Goldsky publics, aucune creds CLOB). Un wallet auto-découvert reste en `status='shadow'` `TRADER_SHADOW_DAYS` jours avant `active` (capital safety). `MAX_ACTIVE_TRADERS` est un cap dur — M5 ne retire jamais arbitrairement un wallet existant pour faire place. `BLACKLISTED_WALLETS` est une exclusion absolue. Toute décision (`promote/demote/keep/skip`) est loggée structlog ET écrite dans `trader_events` pour audit. Formule de scoring versionnée (`SCORING_VERSION`) — pas de rewrite rétroactif des scores historiques.
```

### 10.4 `docs/setup.md`

Ajouter **section 14** :

```markdown
## 14. Activer la découverte automatique (M5, optionnel)

⚠️ **Pré-requis bloquant** : lance le backtest avant d'activer.

\`\`\`bash
source .venv/bin/activate
python scripts/score_backtest.py \
  --wallets-file specs/m5_backtest_seed.txt \
  --as-of 2026-01-15 \
  --observe-days 30 \
  --output backtest_v1_report.html
\`\`\`

Ouvre `backtest_v1_report.html`. Si la corrélation Spearman score ↔ ROI observé est < 0.30 → **n'active pas M5 en prod** (la formule v1 ne prédit pas suffisamment ; remontée à Elie pour itération en `SCORING_VERSION=v2`).

Si la corrélation est OK :

\`\`\`env
DISCOVERY_ENABLED=true
DISCOVERY_INTERVAL_SECONDS=21600
MAX_ACTIVE_TRADERS=10
TRADER_SHADOW_DAYS=7
SCORING_VERSION=v1
SCORING_PROMOTION_THRESHOLD=0.65
SCORING_DEMOTION_THRESHOLD=0.40
\`\`\`

Relance le bot. Ouvre `http://127.0.0.1:8787/traders` (dashboard M4.5 doit être actif). Tu verras :

- Tes wallets pinned (de `TARGET_WALLETS`) en haut.
- Les premiers wallets `shadow` apparaître après le 1er cycle (~6h).
- Les promotions `shadow → active` après `TRADER_SHADOW_DAYS` (=7j) si le score reste ≥ 0.65.

Pour désactiver à chaud : mettre `DISCOVERY_ENABLED=false` et redémarrer. Aucun wallet n'est retiré au shutdown — l'état persiste en DB.

Pour blacklister un wallet : ajouter à `BLACKLISTED_WALLETS=0xabc,0xdef` puis redémarrer.

Troubleshooting :

- `discovery_cycle_failed` répété → vérifier connectivité Data API (`curl https://data-api.polymarket.com/trades?limit=5`).
- `discovery_cap_reached` répété → augmenter `MAX_ACTIVE_TRADERS` ou retirer manuellement des wallets via SQL : `UPDATE target_traders SET status='paused', active=0 WHERE wallet_address='0xabc'`.
- Score d'un wallet bloqué à 0.0 → cold start (`resolved_positions_count < 10`). Attendre que le wallet accumule des positions résolues.
```

---

## 11. Commandes de vérification finale

```bash
ruff check .
ruff format --check .
mypy src
pytest --cov=src/polycopy/discovery --cov-report=term-missing  # ≥ 80%
pytest --cov=src/polycopy/dashboard --cov=src/polycopy/monitoring --cov=src/polycopy/executor --cov=src/polycopy/strategy --cov=src/polycopy/watcher --cov=src/polycopy/storage --cov-report=term-missing  # non-régression
pytest -m integration                          # opt-in (Data API holders, Goldsky)
alembic upgrade head                           # 0001 + 0002 + 0003 OK
alembic current                                # heads = 0003
DISCOVERY_ENABLED=true DISCOVERY_INTERVAL_SECONDS=3600 python -m polycopy --dry-run &
sleep 5
# Vérifier logs : discovery_starting, discovery_started
kill %1 && wait                                # exit 0, log discovery_stopped
python scripts/score_backtest.py --wallets-file specs/m5_backtest_seed.txt \
  --as-of 2026-01-15 --observe-days 30 --output /tmp/backtest_test.html
# → fichier généré, ouvrir manuellement pour validation
```

---

## 12. Critères d'acceptation

- [ ] `DISCOVERY_ENABLED=true python -m polycopy --dry-run` (avec `DISCOVERY_INTERVAL_SECONDS=3600` pour test rapide) tourne ≥ 60 s sans crash. Logs : `polycopy_starting`, `db_initialized`, `watcher_started`, `strategy_started`, `executor_started`, `monitoring_started`, `dashboard_started` (si M4.5 actif), `discovery_starting`, `discovery_started`. Exit 0 sur SIGINT avec `discovery_stopped` loggé.
- [ ] `DISCOVERY_ENABLED=false` (default) → aucun log `discovery_*`, zéro overhead, comportement strictement M4.5.
- [ ] `alembic upgrade head` applique les 3 migrations (0001, 0002, 0003) sans erreur sur DB vide. Backfill `pinned=1` pour les `target_traders` existants vérifié en test.
- [ ] M5 ne retire jamais un wallet `pinned` (testé unit + asserté en code via `transition_status` qui raise sur pinned).
- [ ] M5 ne dépasse jamais `MAX_ACTIVE_TRADERS` (testé unit avec scénario cap=2 + 5 candidats high-score).
- [ ] Aucun wallet `BLACKLISTED_WALLETS` n'apparaît dans `target_traders` post-cycle (testé unit).
- [ ] Cold start : wallet avec `resolved_positions_count < 10` → score=0, `low_confidence=True`, jamais promu.
- [ ] Hystérésis : wallet active avec score < demotion 2 cycles n'est PAS demote ; au 3e cycle → demote (testé unit).
- [ ] Pas de promotion immédiate sauf `TRADER_SHADOW_DAYS=0 AND DISCOVERY_SHADOW_BYPASS=true` (testé unit, log warning au boot).
- [ ] Toute décision écrite dans `trader_events` (vérifié par count en test orchestrator).
- [ ] Toute formule scoring traçable via `scoring_version` colonne (testé unit avec changement de version).
- [ ] Aucun secret loggé (`POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`, `TELEGRAM_BOT_TOKEN`, hypothétique `GOLDSKY_API_KEY`) — vérifié par grep automatisé (test_discovery_security.py si nécessaire, sinon manuel `grep -ri "secret\|private_key" src/polycopy/discovery/` doit retourner uniquement noms de variables).
- [ ] Throttle Data API : sémaphore concurrence max 5, observé au pic ≤ ~60 req/min (testé via instrumentation).
- [ ] Dashboard `/traders` rend la liste avec scores et statuts (testé routes M4.5 étendues).
- [ ] Telegram alertes nouvelles : `trader_promoted`, `trader_demoted`, `discovery_cap_reached`, `discovery_cycle_failed` — testées unit + cooldown respecté.
- [ ] `python scripts/score_backtest.py --as-of <date> --observe-days 30 --output report.html` génère un rapport HTML lisible avec corrélation Spearman + table par wallet.
- [ ] **Backtest v1** sur les 50 wallets seed : Spearman corr ≥ 0.30 documentée dans le PR (output backtest joint dans la description). **Si non atteint, M5 ne sort pas de PR `feat` mais reste en `wip`** — itération formule v2 d'abord.
- [ ] `ruff check .` + `ruff format --check .` + `mypy src` (`--strict`) : 0 erreur.
- [ ] `pytest` : 0 échec. Coverage ≥ 80 % sur `src/polycopy/discovery/`. Pas de régression ≥ 80 % sur les autres couches (M1..M4.5).
- [ ] Tests M1..M4.5 passent toujours (les seuls diffs partagés sont `__main__` + `TargetTraderRepository.list_active` filtre étendu).
- [ ] Docs §10 à jour (`README.md`, `docs/architecture.md`, `CLAUDE.md`, `docs/setup.md` §14) dans le **même** commit que le code.
- [ ] Commit final unique : `feat(discovery): implement M5 trader scoring and auto-discovery`.

---

## 13. Hors scope M5 (NE PAS implémenter — définitif, M5 = dernier module)

Ces items sont **explicitement abandonnés** ou reportés à une éventuelle reprise post-M5 hors roadmap actuelle :

- **Machine learning / XGBoost / réseau de neurones**. Formule déterministe et explicable obligatoire.
- **Copy-trading "consensus" multi-traders** (N traders votent, on copie sur consensus). Hors scope, design différent.
- **Scoring multi-chaînes**. Polymarket = Polygon-only.
- **Interface Telegram bidirectionnelle** (`/score 0xabc`, `/promote 0xdef`). Polycopy reste emitter-only.
- **Recompute rétroactif des scores historiques** sur changement de formule. `SCORING_VERSION=v2` n'écrase pas les `trader_scores` existants — historique préservé tel quel.
- **Auto-tuning des seuils** (`promotion_threshold`, `demotion_threshold` ajustés en boucle fermée). Hors scope, danger d'overfitting.
- **Dashboard interactif backtest** (slider window, drag thresholds). Le backtest est CLI + HTML statique.
- **Persistance des `Alert` en DB** (M4 §6.2). Reste éphémère à M5.
- **Refactor M2 RiskManager** pour utiliser `TraderScore`. Trop chirurgical, M2 reste inchangé.
- **WebSocket pour le scoring temps réel**. Cycles 6h suffisent.
- **Endpoint `/metrics` Prometheus**. Hors scope (idem M4 §13).
- **Multi-process/multi-VPS**. Hors scope.
- **Cleanup périodique de `trader_scores`** (table grossit en append). À 100 wallets × 4 cycles/jour × 365j = ~146 k lignes/an. Acceptable. Cleanup post-1an si besoin.
- **Score normalisé contre la moyenne du pool** (z-score). v1 reste absolu pour interprétabilité.
- **Anti-Sybil avancé** (clustering wallets liés via on-chain analysis). Trop coûteux, reporté.
- **Goldsky payant / The Graph Network token-pinned**. Restons sur fair-use Goldsky public. Si rate limit serré → fallback `data_api` backend.
- **Vendoring `gql` library si `discovery_backend=goldsky`**. À la place, on fait `httpx.post(json={"query": ...})` direct — 5 lignes vs 200 KB de dep.

---

## 14. Notes d'implémentation + décisions auto-arbitrées + zones d'incertitude

### 14.1 Ordre de travail suggéré

1. **Capture obligatoire des 5 fixtures** (§4) avant tout code. 1 commit dédié `chore(fixtures): capture M5 polymarket fixtures` accepté pour faciliter la review.
2. **Backfill Alembic** : créer `alembic/versions/0003_m5_discovery_schema.py`. Auditer manuellement (SQLite ALTER limité). Test `test_m5_alembic_migration.py`.
3. **Modèles + repos** : `target_traders` extensions, `trader_scores`, `trader_events` + repos + tests.
4. **DTOs** `dtos.py` + tests.
5. **Clients API** : `data_api_client.py` (`/holders`, `/trades`, `/value`) + tests respx. Étendre Gamma client pour `list_top_markets` (1 méthode, 5 lignes). Goldsky client + tests **opt-in** (skip si `discovery_backend=data_api`).
6. **`candidate_pool.py`** + tests (mocks complets data_api + gamma).
7. **`metrics_collector.py`** + tests (utilise fixtures M1+M3 existantes).
8. **`scoring.py`** + tests (incl. property test hypothesis si dep dispo).
9. **`decision_engine.py`** + tests exhaustifs (8 scénarios listés §9.6).
10. **`orchestrator.py`** + tests (1 cycle complet en bout-en-bout).
11. **Refactor minimal `TargetTraderRepository.list_active`** + non-régression tests M1.
12. **Intégration `__main__`** : ajout `if settings.discovery_enabled` block.
13. **Producteurs alertes** : push sur events 4 alerts (`trader_promoted`, `trader_demoted`, `discovery_cap_reached`, `discovery_cycle_failed`).
14. **Dashboard extensions** : 2 nouvelles routes + 2 templates + queries dans `dashboard/queries.py`.
15. **`scripts/score_backtest.py`** + tests basiques.
16. **`specs/m5_backtest_seed.txt`** : capturer ~50 adresses publiques via `/holders` sur top 5 marchés à T-90j. Versionné dans le repo.
17. **Lancer backtest réel** : `python scripts/score_backtest.py --wallets-file ... --as-of <date> --observe-days 30 --output report.html`. **Vérifier Spearman ≥ 0.30**. Joindre rapport au PR.
18. **Doc updates §10** dans le même commit code.
19. **Smoke test** : `DISCOVERY_ENABLED=true DISCOVERY_INTERVAL_SECONDS=3600 python -m polycopy --dry-run` tourne ≥ 1h, observer ≥ 1 cycle, vérifier `discovery_cycle_completed` log.
20. **Commit unique** (sauf pour le commit fixtures séparé optionnel) : `feat(discovery): implement M5 trader scoring and auto-discovery`.

### 14.2 Principes

- **Pas d'abstraction prématurée** : 1 `DataApiClient`, 1 `GoldskyClient` (opt-in), 1 `MetricsCollector`, 1 `DecisionEngine`. Pas d'`AbstractScoringStrategy` à v1 (registry dict suffit pour permettre v1/v2).
- **Sessions courtes, append-only** : `trader_scores` et `trader_events` n'ont jamais d'update. Lectures dashboard via `latest_per_wallet` (subquery max).
- **Logs structurés partout** : tous les events listés §0.4 + `discovery_candidates_built`, `metrics_collect_failed`, `score_computed`, `decision_taken`, `discovery_throttle_engaged`. Bindings : `wallet_address`, `score`, `from_status`, `to_status`, `cycle_at`, `scoring_version`.
- **Pas de `print`** hors scripts CLI.
- **Pas de leak** de creds (au cas où Goldsky introduit `GOLDSKY_API_KEY` un jour, discipline `TELEGRAM_BOT_TOKEN` M4 répliquée).
- **Reproducibilité** : `SCORING_VERSION` loggé partout. Pas de "dark formula".

### 14.3 Décisions auto-arbitrées (pour éviter les allers-retours)

1. **Endpoint leaderboard public Polymarket : N'EXISTE PAS** — confirmé via 3 sources (skill, gist shaunlebron, holypoly.io). Bootstrap dérivé via `/holders` + `/trades` global. Documenté dans la spec comme limitation.
2. **Cycle 6h, pool 100 wallets** : équilibre rate limit Data API + réactivité acceptable. Plus court = budget API serré. Plus long = manque les pivots.
3. **Formule v1 4-métriques pondérée** : pas de sortie ML "boîte noire". Explicable à un user non-tech via le dashboard.
4. **Statuts `shadow|active|paused|pinned`** : 4 états suffisent pour modéliser le lifecycle. Pas de `'archived'` distinct (un wallet retiré reste `paused` indéfiniment).
5. **`pinned` flag dérivé de `TARGET_WALLETS`** : invariante forte, pas de UI pour le toggle. Le user doit modifier `.env` + redémarrer pour changer.
6. **Backtest obligatoire avant prod** : hard requirement documenté dans `docs/setup.md` §14. Pas de garde-fou code (le user est adulte) mais la doc et les critères d'acceptation insistent.
7. **`/value` endpoint** utilisé en pré-filtre seulement (économie de calls) — pas dans la formule de score directement.
8. **Goldsky opt-in (pas default)** : par défaut on reste 100% Data API public (zéro nouvelle dep). Goldsky disponible via env pour les power users.
9. **`gql` library : NON** — `httpx.post(json={"query": ...})` direct. 5 lignes vs 200 KB.
10. **Dashboard /traders polling 10s** (vs 3s pour les autres pages M4.5) : les statuts changent toutes les 6h, pas besoin de 3s.
11. **Score backtest seed file** (`specs/m5_backtest_seed.txt`) versionné : 50 adresses publiques sourcées via `/holders` sur top 5 markets à T-90j. Pas de PII (les wallets sont publics).
12. **`MAX_ACTIVE_TRADERS=10` default** : équilibre risque/diversité. À 10 wallets × `MAX_POSITION_USD=100` = $1k exposition max. Cohérent avec la défault `RISK_AVAILABLE_CAPITAL_USD_STUB=1000`.
13. **`SCORING_LOOKBACK_DAYS=90`** : 1 trimestre, capte un cycle complet d'événements (élections, sports). Plus court = bruyant. Plus long = lent à réagir.
14. **`SCORING_MIN_CLOSED_MARKETS=10`** : seuil cold start. < 10 = échantillon trop petit pour conclure. > 20 = wallets très expérimentés seuls éligibles, manque de découverte.

### 14.4 Pièges anticipés

1. **`/holders` sans pagination documentée** : si le `limit` max réel est < 100, capper à 20 reste safe. À vérifier en fixture.
2. **`/trades` sans `user` global feed** : le param `takerOnly=true` exclut les market makers — voulu pour M5 (smart money = takers). Ne pas mettre `takerOnly=false` accidentellement.
3. **`positions.realizedPnl=0` pour positions ouvertes** : ne PAS compter comme résolue. Discriminer via `currentValue == 0` OU `redeemable=true` OU `realizedPnl != 0`.
4. **Goldsky version drift** : `positions-subgraph/0.0.7/gn` peut devenir `0.0.8` sans préavis. URL en env var, monitorer 404 → log error + alerte + fallback `data_api`.
5. **HHI = NaN si volume nul** : init à 1.0 (pas de diversité = max concentration) en cas de division par 0. Testé unit.
6. **Async with semaphore + asyncio.gather** : si une coroutine raise, gather raise tout. Wrap chaque `_score_one` dans try/except + log + continue (déjà prévu §8.2).
7. **`asyncio.Queue` alerts saturée** : le push d'alertes utilise `put_nowait` + log warning sur `QueueFull` (pattern M4 préservé).
8. **`update_score` race condition** : 2 cycles simultanés impossible (1 orchestrator unique). Mais deux décisions sur le même wallet dans le même cycle (cas `discovered` + `promote`) : sérialisées par le `await` séquentiel dans `_run_one_cycle`.
9. **Migration backfill `pinned=1`** : si l'utilisateur avait des wallets en `target_traders` AVANT M5 mais avec `active=False` (rare), ils restent `pinned=False, status='paused'`. Documenter dans setup.md §14.
10. **SQLite ALTER TABLE** : ajout colonnes `target_traders` doit utiliser la stratégie "create new + copy + drop old" si Alembic ne génère pas correctement (tester autogenerate vs op manuel).
11. **`discovery_shadow_bypass=true` + cycle plus rapide que le sleep `discovery_interval_seconds`** : un wallet pourrait être promu à 2 cycles consécutifs (faux). Garde : `discovered → promote` n'a lieu QU'après `(now - discovered_at).days >= TRADER_SHADOW_DAYS`. Si `shadow_days=0` ET cycle=10s → promote au cycle suivant. C'est l'intention (`bypass=true`), explicite et loggué.
12. **Filtrage `data_api/value`** sur `value < $50` peut exclure des candidats légitimes qui viennent de cash-out. Documenter en code que c'est un trade-off d'économie API. Bypass possible via `DISCOVERY_VALUE_FILTER_USD=0` env (pas inclus à v1 — KISS).

### 14.5 Zones d'incertitude à lever AVANT implémentation

(Section critique : signaler à l'utilisateur ces points avant `/implement-module M5`.)

1. **`/holders` schéma exact** : `outcomeIndex` toujours présent ? `pseudonym` peut-il être `null` ? Pagination existe-t-elle au-delà du `limit` ? → **CONFIRMÉ en fixture (2026-04-18)** : `outcomeIndex` toujours présent (integer), `pseudonym` et `name` peuvent être null, pagination non documentée (capper au `limit`). Schéma additionnel : `bio`, `displayUsernamePublic`, `profileImage`, `profileImageOptimized`, `verified`, `asset` sont renvoyés en bonus. Marché binaire YES/NO retourne bien 2 `HolderGroup`. Assumption §4.3 validée.

2. **`/trades` global avec `filterAmount` vs sans** : `filterType=CASH&filterAmount=100` exclut bien les trades < $100 ? → **CONFIRMÉ AVEC DIVERGENCE en fixture (2026-04-18)** : le server-side filter fonctionne (trades observés = grosse taille) MAIS **`usdcSize` n'est PAS renvoyé dans la réponse** — contrairement à l'assumption §4.4. Fix appliqué : calculer `usdc_size = size * price` côté client dans `DiscoveryDataApiClient`. Champs renvoyés en bonus : `bio`, `icon`, `eventSlug`, `name`, `profileImage`, `profileImageOptimized`, `pseudonym`, `outcomeIndex`.

3. **Goldsky `realizedPnl`** : agrégé total ou filtrable par fenêtre temporelle ? → **CONFIRMÉ AVEC DIVERGENCE MAJEURE en fixture (2026-04-18)** : le subgraph `positions-subgraph/0.0.7/gn` **n'a pas** d'entité `positions` ni de champ `realizedPnl` (introspection donne `userBalance`, `condition`, `tokenIdCondition`, etc. — sans PnL). L'entité avec `realizedPnl` vit en fait dans **`pnl-subgraph/0.0.14/gn`**, s'appelle **`UserPosition`** (pas `Position`), et ses champs sont `id`, `user` (String), `tokenId` (BigInt), `amount`, `avgPrice`, `realizedPnl`, `totalBought` — pas de `condition` ni `outcomeIndex` directs. Fix appliqué : `GOLDSKY_POSITIONS_SUBGRAPH_URL` pointe par défaut vers `pnl-subgraph/0.0.14/gn` (historique : ancien chemin gardé en commentaire `.env.example`). Query ré-écrite pour `userPositions`. `realizedPnl` reste cumulé total (pas de filtre fenêtre) — limitation documentée et acceptée pour backend `goldsky` v1 (backend opt-in, fallback `data_api` toujours possible). Numbers parsés comme BigInt échelle USDC 10⁶ (empirique : `avgPrice="500000"` = $0.50/share).

4. **Définition "position résolue"** : `realizedPnl != 0` est-il fiable, ou un trader peut-il avoir `realizedPnl != 0` sur une position partiellement vendue mais encore ouverte ? → **À vérifier empiriquement** sur le fixture `data_api_positions_sample.json` capturée en M3 (devrait contenir des cas mixtes).

5. **Backtest seed quality** : 50 wallets via `/holders` à T-90j risquent d'être biaisés vers les whales sur les marchés populaires. Faut-il diversifier (mix top + random) ? → **Décision prise** : 50 wallets top + 20 wallets random (depuis `/trades` global archivé). Total = 70 wallets seed.

6. **Promotion auto vs validation user** : dans M5 v1, la promotion `shadow → active` est entièrement automatique. Faut-il un endpoint dashboard `POST /traders/<wallet>/approve` pour validation user ? → **Décision spec** : NON à v1 (M4.5 dashboard est read-only strict). Le user contrôle via `MAX_ACTIVE_TRADERS` (cap dur) + `BLACKLISTED_WALLETS` (refus explicite). Si v2 nécessaire, ajouter `DASHBOARD_ALLOW_WRITES=true` + CSRF (cf. M4.5 §13).

7. **`SCORING_VERSION="v1"` vs date** : utiliser un git SHA tronqué (`"v1-a1b2c3"`) serait plus précis pour traçabilité. Trade-off lisibilité dashboard. → **Décision spec** : `"v1"` simple. Ajouter SHA comme metadata `trader_scores.metrics_snapshot` si utile post-mortem.

8. **Alembic batch ALTER TABLE** : SQLite + 4 nouvelles colonnes `target_traders` peut nécessiter `op.batch_alter_table(...)`. Auto-generated peut louper. → **Action requise lors de l'audit manuel** de la migration générée. Bloquant pour le merge.

9. **Subgraph URL hardcodée vs env** : `GOLDSKY_POSITIONS_SUBGRAPH_URL` est dans `.env.example` avec une valeur default. Si la version change (`0.0.8`), un user qui a override va casser. → **Décision** : default-only-if-empty avec un fallback hardcodé code-side. Le user qui set override prend la responsabilité.

10. **Concurrency `transition_status` + `update_score`** : 2 transactions séquentielles sur la même wallet row. Si crash entre les deux → score updated mais status pas changé. → **Décision** : 1 session par wallet décision, commit atomique des 2 ops dans le même `async with`. Sinon utiliser un compensating mechanism au prochain cycle (idempotent).

---

## 15. Prompt à m'envoyer pour lancer l'implémentation

Copie-colle tel quel quand tu es prêt :

```
/implement-module M5

Suis specs/M5-trader-scoring.md à la lettre. Avant tout code, action obligatoire :

1. Invoque /polymarket:polymarket pour reconfirmer les schémas (note : le skill ne couvre PAS les endpoints /holders, /trades global, /value — vérifie la doc tierce + capture systématique en fixture).

2. Capture 5 fixtures réelles via curl ou httpx (1 call chacune) :
   - tests/fixtures/data_api_holders_sample.json (curl 'https://data-api.polymarket.com/holders?market=<un_conditionId_top_market>&limit=20')
   - tests/fixtures/data_api_trades_global_sample.json (curl 'https://data-api.polymarket.com/trades?limit=500&filterType=CASH&filterAmount=100&takerOnly=true')
   - tests/fixtures/data_api_value_sample.json (curl 'https://data-api.polymarket.com/value?user=<wallet_actif>')
   - tests/fixtures/gamma_top_markets_sample.json (curl 'https://gamma-api.polymarket.com/markets?limit=20&order=liquidityNum&ascending=false&active=true&closed=false')
   - tests/fixtures/goldsky_positions_topn_sample.json (POST GraphQL au subgraph positions, query top-5 par realizedPnl, cf. spec §4.7)

   Pour chaque fixture : si le schéma diffère significativement des assumptions §4 spec, STOP, signale-moi avant de continuer.

3. Crée specs/m5_backtest_seed.txt (~70 wallets : 50 via /holders sur top 5 markets à T-90j + 20 random via /trades global archivé). Versionné dans le repo (publics, pas de PII).

Ensuite suis l'ordre §14.1.

Contraintes non négociables :

- DISCOVERY_ENABLED=false par défaut. Si false → aucun code discovery ne s'instancie (zéro overhead). Vérifié en unit test.
- M5 read-only stricte : aucune signature CLOB, aucune dep sur creds, aucun POST sauf Goldsky GraphQL (opt-in).
- Jamais retirer un wallet pinned (vient de TARGET_WALLETS env). transition_status raise sur pinned.
- Jamais dépasser MAX_ACTIVE_TRADERS (cap dur). Refuser + alerter, jamais retirer arbitrairement.
- BLACKLISTED_WALLETS = exclusion absolue, vérifiée 2x (pre-bootstrap + pre-promotion).
- TRADER_SHADOW_DAYS observation obligatoire avant active. Bypass uniquement TRADER_SHADOW_DAYS=0 ET DISCOVERY_SHADOW_BYPASS=true ET log warning au boot.
- Hystérésis demote = SCORING_DEMOTION_HYSTERESIS_CYCLES=3 par défaut, configurable.
- Toute décision (promote/demote/keep/skip) écrite dans trader_events ET loggée structlog.
- SCORING_VERSION loggué + écrit avec chaque score. Pas de rewrite rétroactif sur changement formule.
- Backtest obligatoire AVANT mise en prod : Spearman corr score↔ROI ≥ 0.30 sur ≥50 wallets seed. Sinon spec marque le PR comme "wip" et reste à itérer formule v2.
- Pas de leak GOLDSKY_API_KEY (hypothétique) ni autre creds. Discipline TELEGRAM_BOT_TOKEN M4 répliquée.
- Throttle Data API : asyncio.Semaphore(5) max concurrent. Pic ≤ 60 req/min observé en test.
- Migration Alembic : 0003_m5_discovery_schema.py NEW. Ne PAS toucher 0001 ni 0002. Audit manuel obligatoire pour SQLite ALTER + backfill pinned=1 pour traders existants.
- Refactor minimal TargetTraderRepository.list_active : filtrage status IN ('active', 'pinned'). Tests M1 doivent rester verts.
- Pas de modif Watcher / Strategy / Executor / Monitoring / Dashboard core (uniquement extensions dashboard /traders et /backtest pages, alerts producers).
- Conventions CLAUDE.md (async, Pydantic v2, SQLAlchemy 2.0, structlog, docstrings FR / code EN, pas de print).
- mypy --strict propre, ruff propre, pytest ≥ 80 % coverage sur src/polycopy/discovery/ ET pas de régression sur M1..M4.5.
- Tests via httpx AsyncClient + ASGITransport (dashboard) + respx (data API/Goldsky). Pas de socket réel sauf tests integration opt-in.
- Goldsky : pas de dep `gql`. httpx.post(json={"query": ...}) direct.
- Doc updates §10 dans le même commit (README + architecture + CLAUDE + setup §14).
- Commit final unique : feat(discovery): implement M5 trader scoring and auto-discovery
- (Optionnel) commit séparé fixtures : chore(fixtures): capture M5 polymarket fixtures (avant le feat).

Demande-moi confirmation avant tout patch sensible :
- config.py (les 14 nouvelles env vars OK, mais validators cross-field à valider).
- .env.example (ajout des 14 vars + commentaires sécurité).
- pyproject.toml (rien à ajouter par défaut ; gql/asgi-lifespan refusés).
- alembic/versions/0003_*.py (audit manuel post-autogenerate).
- specs/m5_backtest_seed.txt (validate les wallets capturés sont publics et pertinents).

Si tu identifies une zone d'incertitude §14.5 qui se confirme problématique pendant l'implémentation (ex: /holders schéma diverge, Goldsky realizedPnl pas filtrable par fenêtre, definition position-résolue ambiguë), STOP et signale-moi — ne tranche pas au pif. Si tu identifies un trade-off nouveau non anticipé dans la spec, ajoute-le en §14.5 et signale-le.

Backtest result reporting : à la fin, joins le backtest_v1_report.html (ou un screenshot de la corrélation Spearman) à la PR. Si Spearman < 0.30, NE PAS merger M5 en prod — ouvrir issue "M5 v1 sous-performe, itérer formule v2".
```
