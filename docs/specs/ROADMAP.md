# Roadmap des specs polycopy

**Mise à jour** : 2026-04-27 (post-ship M14 / M15 / M16 / M17, **M18 V2 migration spec rédigée**, cutover Polymarket le 28 avril ~11h UTC).

**Source de vérité brouillons** : [`docs/next/`](../next/) — modules de
roadmap consolidés depuis l'audit 2026-04-24 + 3 deep-searches. Convention
de mapping brief → spec :
**MA→M14, MB→M15, MC→M16, MD→M17, ME→M18** (V2 migration, urgent), puis
production temporelle pour les suivants (MK→M19 latency, MF/MG/MH/MI/MJ
assignés au moment de la rédaction).

**Refactoring 2026-04-27** : le brief V2 migration `M_migrate_v2.md`
(urgent, P0) a été renommé `ME.md` pour rétablir la convention
alphabétique. L'ancien `ME.md` (Pipeline latency phase 1b, P2) est
désormais `MK.md` et prend M19 à la prochaine production de spec.

**Source de vérité ops post-ship** : [`docs/todo.md`](../todo.md) — 13 sections
de procédures pull / reset / cutover M14 → M15 → M16.

---

## Jalons implémentés

### Phase fondation M1 → M9 (2025-Q4 → 2026-Q1)

| Spec | Titre | Statut |
|---|---|---|
| M1 | Watcher + Storage | ✅ shipped |
| M2 | Strategy Engine (5 filtres) | ✅ shipped |
| M3 | Executor dry-run safe + 4 garde-fous | ✅ shipped |
| M4 | Monitoring (Telegram + PnL + kill switch) | ✅ shipped |
| M4.5 | Dashboard FastAPI + HTMX read-only | ✅ shipped |
| M5 | Discovery + Scoring v1 | ✅ shipped |
| M6 | Dashboard 2026 (Tailwind + Radix + Lucide) | ✅ shipped |
| M7 | Telegram enhanced (Jinja2 + scheduler) | ✅ shipped |
| M8 | Dry-run realistic fill (FOK orderbook) | ✅ shipped |
| M9 | Silent CLI + /logs tab + README | ✅ shipped |

### Phase compétition + parité M5_bis → M13 (2026-Q1)

| Spec | Titre | Statut |
|---|---|---|
| M5_bis | Competitive eviction (shadow / active / sell_only) | ✅ shipped |
| M5_ter | Watcher live-reload (poll-set refresh 5min) | ✅ shipped |
| M10 | EXECUTION_MODE 3 modes + parity + log hygiene | ✅ shipped |
| M11 | Real-time pipeline phase 1 (WSS market + 6 stages latence) | ✅ shipped |
| M12 | Scoring v2 (formule hybride 6 facteurs + 6 gates durs) | ✅ shipped |
| M12_bis | Multi-machine remote control (Tailscale + TOTP + sentinel) | ✅ shipped |
| M13 | Dry-run observability (cristallisation realized_pnl + neg_risk) | ✅ shipped |

### Phase scoring robuste + anti-toxic + fees + integrity (2026-Q2)

| Spec | Brief | Titre | Statut | Notes |
|---|---|---|---|---|
| **M14** | [MA](../next/MA.md) | Scoring v2.1-ROBUST (rank-transform + Sortino sentinel + Brier P(YES) + zombie filter <30j + HHI direct) | ✅ shipped | 8 commits MA.x. Validation H-EMP-1 + H-EMP-2 via [`scripts/validate_ma_hypotheses.py`](../../scripts/validate_ma_hypotheses.py). |
| **M15** | [MB](../next/MB.md) | Anti-toxic lifecycle + internal PnL feedback (v2.1.1 nouveau facteur, ranking-based active, gate arbitrage_bot, probation 0.25×, auto-blacklist) | ✅ shipped | 8 commits MB.x. Migration Alembic 0009. Bloque MF jusqu'à 30j de collecte `internal_pnl_score`. |
| **M16** | [MC](../next/MC.md) | Dynamic taker fees + EV adjustment (FeeRateClient + base_fee binary flag + formule officielle Polymarket) | ✅ shipped | 5 commits MC.x. Math fee partiellement obsolète post-V2 — cf. M18 §D6 (formule V2 expose `fd.e` directement, M16 hardcoded mapping retiré). |
| **M17** | [MD](../next/MD.md) | Cross-layer integrity patches (5 CRITICALs audit : `simulated` filter, kill switch CRITICAL bypass digest, `pnl_snapshots.execution_mode`, `last_known_mid` TTL, `realized_pnl` peuplé, `TraderEvent("kill_switch")` audit) | ✅ shipped | 7 commits MD.x. Migration Alembic 0010. Bloque le flip `EXECUTION_MODE=live`. |

### Phase Polymarket CLOB V2 migration (urgent, P0)

| Spec | Brief | Titre | Statut | Notes |
|---|---|---|---|---|
| **M18** | [ME](../next/ME.md) | Polymarket CLOB V2 + pUSD migration (SDK swap V1→V2, `getClobMarketInfo` + `FeeQuote(rate, exponent)`, `POLYMARKET_CLOB_HOST` configurable, builder code optionnel, wrap helper script `web3.py` optional dep) | 📋 spec rédigée — implémentation pending | Spec [`M18-polymarket-v2-migration.md`](M18-polymarket-v2-migration.md) (~3000 lignes, §0 → §18). 7 commits ME.1 → ME.7. Hard deadline ship lundi 27 avril ~22h UTC pour cutover Polymarket mardi 28 avril ~11h UTC. SDK V2 dual-version capable → ship pré-cutover safe (cf. spec §D11). |

---

## Roadmap restante

Briefs consolidés dans [`docs/next/`](../next/). Source : audit 2026-04-24 +
synthèse deep-search §40 items + 4 sessions bug A-D.

| # | Brief | Spec à produire | Priorité | Charge | Prérequis | Bloque |
|---|---|---|---|---|---|---|
| 1 | [MK](../next/MK.md) (ex-`ME` renommé 2026-04-27) | M19 Pipeline latency phase 1b (WSS market detection p50 13s → 2-4s) | 🟠 P2 | M (3-4j) | aucun | (améliore MF) |
| 2 | [MG](../next/MG.md) | M?? CLV + Kelly + Kyle's λ + latency tolerance scoring factors | 🟠 P2 | M (3-4j) | M14 | (optionnel pour MF) |
| 3 | [MF](../next/MF.md) | M?? Wash detection (Sirolly) + Mitts-Ofir composite — v2.2-DISCRIMINATING capstone | 🟠 P2 | L (6-8j) | M14 + M15 + **30j data** | — |
| 4 | [MH](../next/MH.md) | M?? Dashboard UX polish (adresses non-tronquées, Size 0.00, divergences PnL) | 🟡 P3 | M (2-3j) | aucun | — |
| 5 | [MI](../next/MI.md) | M?? Ops hygiene (shutdown lent, setup script, Goldsky Starter free) | 🟡 P3 | M (2-3j) | aucun | — |
| 6 | [MJ](../next/MJ.md) | M?? (opt) MEV Private Mempool instrumentation | 🟢 P4 | S-M (1-3j) | M11 | — |

**Numérotation** : M18 a été consommé par la migration V2 (urgent, hors
séquence). Le prochain spec à produire (MK / latency) prend M19. Les
suivants se numérotent dans l'ordre temporel de production.

**Légende priorité** :
- 🔥 **P1** : débloque le test business (déjà shippé avec M14/M15/M16)
- 🟠 **P2** : capstones ou blockers pré-live — ship avant cutover live
- 🟡 **P3** : infra / UX / ops — slack time, parallélisable
- 🟢 **P4** : optionnel, hypothèses à valider avant engagement

### Plan d'exécution recommandé (post-M17 + M18, ~6 semaines)

**Semaine 0 (cette semaine) — cutover Polymarket V2** :
- **M18** (V2 migration) : ship lundi 27 avril ~22h UTC, auto-flip
  backend mardi 28 avril ~11h UTC, smoke post-cutover.

**Semaines 1-2 — préparer le passage live (parallélisable)** :
- **MK** (M19) : remplacer le polling REST `/activity` par WSS market
  channel — gain immédiat dry-run (latence p50 13s observée → 2-4s).
- **MG** : CLV + Kelly + Kyle's λ — facteurs académiquement validés,
  enrichit MF capstone. Indépendant.

**Semaines 3-4 — slack + UX polish** :
- **MH** : UX polish dashboard (slack time si bandwidth).
- **MI** : ops hygiene (shutdown graceful, Goldsky free tier).

**Semaines 5-6 — MF capstone v2.2-DISCRIMINATING** :
- **MF** : Sirolly wash + Mitts-Ofir composite — **prérequis strict** :
  M14 ✅ + M15 ✅ + ≥30j de data `internal_pnl_score` collectée. Capstone
  scoring v2.2.

**Slack time** : MJ (MEV optionnel) — insertable n'importe où.

### Parallélisation

- **Indépendants** (peuvent ship en parallèle) : MK, MG, MH, MI
- **Dépend de M14 (✅)** : MG (déjà débloqué)
- **Dépend de M14 + M15 + 30j data** : MF (capstone)
- **Dépend de M16 + M17 + cutover réussi** : tous les modules post-M18

**Charge totale estimée roadmap restante** : ~24-35 jours dev single-person,
~5-6 semaines avec parallélisation modérée (M17 + M18 retirés du compte).

---

## Polymarket V2 migration (M18) — spec actionnable

**Cutover Polymarket** : mardi 28 avril 2026 ~11h00 UTC (~1h downtime).
**Spec produite 2026-04-27** : [`M18-polymarket-v2-migration.md`](M18-polymarket-v2-migration.md)
(~3000 lignes, format M14/M15/M16/M17 strict).

**Brief origine** : [`docs/next/ME.md`](../next/ME.md) (items ME.1 → ME.7).
Le brief a été renommé depuis `M_migrate_v2.md` 2026-04-27 pour rétablir
la convention alphabétique MA→M14, …, **ME→M18**. L'ancien `ME.md`
(latency phase 1b, P2) est désormais `MK.md` et prendra M19.

**Validations factuelles 2026-04-27 intégrées dans la spec** :
- ✅ `pip index versions py-clob-client-v2` retourne `1.0.0` (publié 2026-04-17).
- ✅ Adresses confirmées live sur `docs.polymarket.com/resources/contracts` :
  pUSD `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`, CollateralOnramp
  `0x93070a847efEf7F70739046A929D47a521F5B8ee`.
- ✅ Schéma `getClobMarketInfo` validé sur `clob-v2.polymarket.com` (crypto
  fee-enabled : `fd:{r:0.072, e:1, to:true}` / politics fee-free : `fd`
  absent).
- ✅ SDK V2 dual-version capable (`_resolve_version` + `_retry_on_version_update`)
  → ship pré-cutover safe, élimine la fenêtre critique 11h UTC.

**Décisions architecturales clefs (corrigent le brief ME initial)** :
- **D1** : SDK V2 garde le constructor V1-style positionnel (PAS d'options
  object — brief ME.5 inexact).
- **D5** : adresses Exchange V2 / Neg Risk V2 / pUSD restent SDK-managed
  via `get_contract_config(137)` ; seul `CollateralOnramp` exposé en env
  var (consommé par le wrap script).
- **D6** : `FeeRateClient` introduit `FeeQuote(rate, exponent)` consommant
  `fd.r` et `fd.e` directement — disparition du mapping hardcodé
  `feeType → (rate_param, exponent)` M16 (qui sous-estimait fees crypto
  de 3× aux p extrêmes).
- **D8** : `polymarket_use_server_time=True` par défaut (anti clock-skew
  sur le `timestamp` ms V2).
- **D11** : ship lundi 27 avril ~22h UTC, auto-flip mardi 28 avril ~11h
  UTC sans intervention humaine.

**Plan d'implémentation** : 7 commits atomiques ME.1 → ME.7 (cf. spec §17).
Charge cumulée : 2 jours dev. Tests : 22 unit + 3 intégration.

**Procédure cutover ops** : cf. [`docs/todo.md §14`](../todo.md#L554) +
spec [§11](M18-polymarket-v2-migration.md).

**Références externes vérifiées 2026-04-27** :
- [`docs.polymarket.com/v2-migration`](https://docs.polymarket.com/v2-migration)
- [`docs.polymarket.com/concepts/pusd`](https://docs.polymarket.com/concepts/pusd)
- [`docs.polymarket.com/resources/contracts`](https://docs.polymarket.com/resources/contracts)
- [`github.com/Polymarket/py-clob-client-v2`](https://github.com/Polymarket/py-clob-client-v2)
- [`pypi.org/project/py-clob-client-v2/1.0.0/`](https://pypi.org/project/py-clob-client-v2/)

---

## Anciens drafts MX_ (historique uniquement)

Anciens prompts/brouillons. Trois ont été absorbés dans M5_ter / MF / MX
(scope absorbé) ; ne pas les mettre à jour, ce sont des fichiers historiques.

| Fichier | Sujet | Statut |
|---|---|---|
| [`MX_backtest_scheduler_prompt.md`](MX_backtest_scheduler_prompt.md) | Backtest quotidien + persistance `backtest_runs` | ✅ absorbé partiellement par MF (validation v2.2 capstone) |
| [`MX_discovery_auto_lockout_prompt.md`](MX_discovery_auto_lockout_prompt.md) | Hystérésis lockout si backtest fail 3× | ✅ absorbé par M15 MB.8 (auto-blacklist) — sémantique différente mais même intent |

## Principes de nommage

- `M<n>-<titre-kebab-case>.md` pour une spec active ou implémentée.
- `MX_<description>_prompt.md` pour un prompt/draft historique.
- Un seul `M<n>` actif par numéro — ne pas écraser les historiques.
- Briefs `MA..MJ` dans `docs/next/` mappent à des numéros `M<n>` côté specs
  selon la convention alphabétique tant que possible :
  - MA → M14, MB → M15, MC → M16, MD → M17, **ME → M18** (V2 migration,
    urgent — séquence régulière 2026-Q2 préservée).
  - **MK** (latency phase 1b, ex-`ME` renommé 2026-04-27) prend M19.
  - Briefs restants (MF / MG / MH / MI / MJ) : assignation `M<n>` au
    moment de la production de chaque spec, dans l'ordre temporel.
- Cohérence interne d'un spec : items `MX.1, MX.2, ...` reprennent la
  lettre du brief origine (ex: spec M18 utilise items `ME.x` car son
  brief s'appelle [`ME.md`](../next/ME.md) — alignement strict). Si un brief `MX.md` est renommé
  pour libérer une lettre, signaler le rename dans le commit + ROADMAP.

## Hypothèses empiriques par module

Module | Hypothèses à valider AVANT ship
---|---
M14 (MA) | H-EMP-1 (risk_adjusted ≥40% variance) + H-EMP-2 (σ relatif <10%) — `scripts/validate_ma_hypotheses.py`
M15 (MB) | H-EMP-3 (Spearman ρ internal_pnl ↔ score ∈ [0.1, 0.7]) + H-EMP-11 (≥90% pool pass arbitrage_bot gate) + H-EMP-13 informatif
M16 (MC) | H-EMP-10 (impact fees ≥1% post-fees) — empirique post-7j shadow
M18 (V2) | H-V2-1 ✅ + H-V2-2 ✅ + H-V2-3 ✅ + H-V2-4 ✅ (toutes validées 2026-04-27 — cf. spec §15)
M19 (MK) | H-EMP-4 (latence p50 détection 8-20s → 2-4s post-WSS) + H-EMP-6 (latence WSS market channel sur notre stack)
MF (capstone) | H-EMP-7 (wash cluster détection ≥80% precision) + H-EMP-8 (Mitts-Ofir HHI signal valide)

## Questions ouvertes (Q1 → Q10, synthèse §11)

À résoudre empiriquement post-ship des modules concernés :
- **Q1** : "250ms taker delay" réel ou network+matching ? → instrumenter post-MK (M19)
- **Q3** : corrélation Brier/PnL négative Convexly tient-elle sur nos wallets ? → valider post-M15 + 30j
- **Q4** : MEV réel sur nos tailles ? → MJ instrumentation
- **Q5** : impact fees sur notre EV ? → mesurer post-M16 + post-M18 (cf. todo.md §12)
- **Q6** : pool ACTIVE devient-il rotatif post-MB.3 ranking ? → observer post-M15
- **Q7..Q10** : cf. synthèse `docs/deepsearch/SYNTHESIS-2026-04-24-...md` §11
