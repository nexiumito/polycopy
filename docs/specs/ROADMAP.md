# Roadmap des specs polycopy

**Mise à jour** : 2026-04-26 (post-ship M14 / M15 / M16, pré-CTF Exchange V2 Polymarket).

**Source de vérité brouillons** : [`docs/next/`](../next/) — 10 modules MA → MJ
consolidés depuis l'audit 2026-04-24 + 3 deep-searches.

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

### Phase scoring robuste + anti-toxic + fees (2026-Q2)

| Spec | Brief | Titre | Statut | Notes |
|---|---|---|---|---|
| **M14** | [MA](../next/MA.md) | Scoring v2.1-ROBUST (rank-transform + Sortino sentinel + Brier P(YES) + zombie filter <30j + HHI direct) | ✅ shipped | 8 commits MA.x. Validation H-EMP-1 + H-EMP-2 via [`scripts/validate_ma_hypotheses.py`](../../scripts/validate_ma_hypotheses.py). |
| **M15** | [MB](../next/MB.md) | Anti-toxic lifecycle + internal PnL feedback (v2.1.1 nouveau facteur, ranking-based active, gate arbitrage_bot, probation 0.25×, auto-blacklist) | ✅ shipped | 8 commits MB.x. Migration Alembic 0009. Bloque MF jusqu'à 30j de collecte `internal_pnl_score`. |
| **M16** | [MC](../next/MC.md) | Dynamic taker fees + EV adjustment (FeeRateClient + base_fee binary flag + formule officielle Polymarket) | ✅ shipped | 5 commits MC.x. **Refactor obligatoire post-CTF Exchange V2** — cf. §"Polymarket V2 migration" infra. |

---

## Roadmap restante

10 modules consolidés dans [`docs/next/`](../next/). Source : audit
2026-04-24 + synthèse deep-search §40 items + 4 sessions bug A-D.

| # | Brief | Spec | Priorité | Charge | Prérequis | Bloque |
|---|---|---|---|---|---|---|
| 1 | [MD](../next/MD.md) | M17 Cross-layer integrity (5 CRITICALs audit) | 🟠 P2 | M (3-4j) | aucun | passage live |
| 2 | [ME](../next/ME.md) | Pipeline latency phase 1b (WSS market detection p50 13s → 2-4s) | 🟠 P2 | M (3-4j) | aucun | (améliore MF) |
| 3 | [MG](../next/MG.md) | CLV + Kelly + Kyle's λ + latency tolerance scoring factors | 🟠 P2 | M (3-4j) | M14 | (optionnel pour MF) |
| 4 | [MF](../next/MF.md) | Wash detection (Sirolly) + Mitts-Ofir composite — v2.2-DISCRIMINATING capstone | 🟠 P2 | L (6-8j) | M14 + M15 + **30j data** | — |
| 5 | [MH](../next/MH.md) | Dashboard UX polish (adresses non-tronquées, Size 0.00, divergences PnL) | 🟡 P3 | M (2-3j) | aucun | — |
| 6 | [MI](../next/MI.md) | Ops hygiene (shutdown lent, setup script, Goldsky Starter free) | 🟡 P3 | M (2-3j) | aucun | — |
| 7 | [MJ](../next/MJ.md) | (opt) MEV Private Mempool instrumentation | 🟢 P4 | S-M (1-3j) | M11 | — |

**Légende priorité** :
- 🔥 **P1** : débloque le test business (déjà shippé avec M14/M15/M16)
- 🟠 **P2** : capstones ou blockers pré-live — ship avant cutover live
- 🟡 **P3** : infra / UX / ops — slack time, parallélisable
- 🟢 **P4** : optionnel, hypothèses à valider avant engagement

### Plan d'exécution recommandé (post-M16, ~6 semaines)

**Semaines 1-2 — préparer le passage live (parallélisable)** :
- **MD** (M17) : 5 CRITICALs cross-couche. Bloque le flip `EXECUTION_MODE=live`.
- **ME** : remplacer le polling REST `/activity` par WSS market channel — gain
  immédiat dry-run (latence p50 13s observée → 2-4s).

**Semaines 3-4 — collecte data v2.1.1 + factors enrichissement** :
- **MG** : CLV + Kelly + Kyle's λ — facteurs académiquement validés, enrichit
  MF capstone. Indépendant.
- **MH** : UX polish dashboard (slack time si bandwidth).

**Semaines 5-6 — MF capstone v2.2-DISCRIMINATING** :
- **MF** : Sirolly wash + Mitts-Ofir composite — **prérequis strict** :
  M14 ✅ + M15 ✅ + ≥30j de data `internal_pnl_score` collectée. Capstone
  scoring v2.2.

**Slack time** : MI (ops hygiene) + MJ (MEV optionnel) — insertable n'importe
où.

### Parallélisation

- **Indépendants** (peuvent ship en parallèle) : MD, ME, MG, MH, MI
- **Dépend de M14 (✅)** : MG (déjà débloqué)
- **Dépend de M14 + M15 + 30j data** : MF (capstone)

**Charge totale estimée roadmap restante** : ~28-40 jours dev single-person,
~6 semaines avec parallélisation modérée.

---

## Polymarket V2 migration (CTF Exchange V2 + Polymarket USD)

**⚠️ Annonce officielle Polymarket Dev** (avril 2026) — upgrade complète de
la stack exchange dans **2-3 semaines** post-annonce :

- **CTF Exchange V2** : nouveaux contrats, struct Order simplifiée, support
  EIP-1271 signatures (smart contract accounts), builder codes onchain.
- **Polymarket USD** (PYUSD-style) : migration USDC.e → wrapped USDC 1:1
  via `CollateralOnramp.wrap(asset, to, amount)`. **Action manuelle one-time**
  pour les API-only traders (notre cas).
- **Nouveau SDK CLOB-Client** : `py-clob-client-v2` (package séparé,
  **non in-place**). Auto-switch V1 ↔ V2 via version endpoint.
- **Maintenance window** : ~1h, tous les open orders wiped (pas un problème
  pour polycopy — on est en FOK strict, pas de GTC).

### Impact sur polycopy

| Module touché | Refactor | Charge | Prérequis |
|---|---|---|---|
| [src/polycopy/executor/clob_client.py](../../src/polycopy/executor/clob_client.py) | Bumper `py-clob-client` → `py-clob-client-v2`. Constructeur change (options object, `chain` au lieu de `chainId`). EIP-712 domain version `"1"` → `"2"`. | M (1-2j) | SDK release officielle |
| [src/polycopy/executor/order_builder.py](../../src/polycopy/executor/order_builder.py) ou équivalent | Nouvelle struct Order : drop `nonce`/`feeRateBps`/`taker`/`expiration`, ajout `timestamp` ms / `metadata` (bytes32) / `builder` (bytes32). | S (0.5j) | SDK V2 |
| Funder USDC.e → Polymarket USD | Wrap one-time via `CollateralOnramp.wrap(USDC.e_addr, funder, amount)` (Polygon). Approve USDC.e d'abord. | S (manuel ~30min) | Adresse onramp officielle confirmée |
| [src/polycopy/executor/fee_rate_client.py](../../src/polycopy/executor/fee_rate_client.py) (M16) | V2 expose `getClobMarketInfo()` au lieu de `/fee-rate?token_id=`. Même math EV (formule officielle), source différente. Garder `STRATEGY_FEES_AWARE_ENABLED` flag, swap le client. | S (0.5-1j) | SDK V2 |
| [src/polycopy/storage/models.py](../../src/polycopy/storage/models.py) | Si on stocke l'adresse du token de collateral en hard-coded quelque part → swap pour Polymarket USD. À auditer. | XS (~1h) | confirmé adresses publiées |
| Builder code (optionnel) | Réclamer un builder code via `polymarket.com/settings?tab=builder`, plomber dans les ordres pour fee rebates. | XS | feature live |

### Plan de migration polycopy

**Phase 1 — Prep (avant maintenance window)** :
1. Lire la doc officielle migration guide + py-clob-client-v2 changelog
   (vérifier les liens donnés par le sub-agent qui a investigué — **toutes
   les URLs ci-dessous demandent confirmation directe**).
2. Bumper `py-clob-client` en branche `feat/ctf-exchange-v2` séparée.
   Garder le bot en prod sur la branche actuelle (V1 fonctionne tant que
   le cutover Polymarket n'a pas eu lieu).
3. Adapter `ClobWriteClient` constructeur + signature path. Tests
   intégration via `clob-v2.polymarket.com` (testnet d'après le tweet).
4. Préparer le wrap manuel USDC.e → Polymarket USD (script ad-hoc une fois
   l'adresse `CollateralOnramp` confirmée).

**Phase 2 — Cutover (jour J Polymarket)** :
1. ~30 min avant maintenance : `systemctl stop polycopy`.
2. Pendant maintenance : merger la branche `feat/ctf-exchange-v2` sur main.
3. Wrap USDC.e → Polymarket USD via le script. Vérifier le solde.
4. Restart bot avec le nouveau SDK.
5. Smoke test : 1 ordre dry-run sur un marché actif post-restart Polymarket.

**Phase 3 — Post-cutover (suivi)** :
1. Surveiller les `executor_error` Telegram pendant 24h.
2. Réclamer le builder code (optionnel, fee rebates).

### Liens à vérifier avant action

⚠️ Les liens ci-dessous viennent d'une recherche web sub-agent et **doivent
être confirmés directement** sur `docs.polymarket.com` avant tout merge :

- `https://docs.polymarket.com/v2-migration` (migration guide)
- `https://docs.polymarket.com/changelog` (changelog officiel)
- `https://docs.polymarket.com/concepts/pusd` (Polymarket USD doc)
- `https://github.com/Polymarket/py-clob-client-v2` (Python SDK V2)
- `https://github.com/Polymarket/ctf-exchange` (contrats V2)
- `https://docs.polymarket.com/builders/fees` (builder codes)

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
- Briefs `MA..MJ` dans `docs/next/` → mappent à `M<n+13>` côté specs
  (MA=M14, MB=M15, MC=M16, MD=M17, ME=M18, etc. — séquence à respecter
  pour l'ordre temporel + traçabilité commits).

## Hypothèses empiriques par module

Module | Hypothèses à valider AVANT ship
---|---
M14 (MA) | H-EMP-1 (risk_adjusted ≥40% variance) + H-EMP-2 (σ relatif <10%) — `scripts/validate_ma_hypotheses.py`
M15 (MB) | H-EMP-3 (Spearman ρ internal_pnl ↔ score ∈ [0.1, 0.7]) + H-EMP-11 (≥90% pool pass arbitrage_bot gate) + H-EMP-13 informatif
M16 (MC) | H-EMP-10 (impact fees ≥1% post-fees) — empirique post-7j shadow
MF (capstone) | H-EMP-7 (wash cluster détection ≥80% precision) + H-EMP-8 (Mitts-Ofir HHI signal valide)

## Questions ouvertes (Q1 → Q10, synthèse §11)

À résoudre empiriquement post-ship des modules concernés :
- **Q1** : "250ms taker delay" réel ou network+matching ? → instrumenter post-ME
- **Q3** : corrélation Brier/PnL négative Convexly tient-elle sur nos wallets ? → valider post-M15 + 30j
- **Q4** : MEV réel sur nos tailles ? → MJ instrumentation
- **Q5** : impact fees sur notre EV ? → mesurer post-M16 (cf. todo.md §12)
- **Q6** : pool ACTIVE devient-il rotatif post-MB.3 ranking ? → observer post-M15
- **Q7..Q10** : cf. synthèse `docs/deepsearch/SYNTHESIS-2026-04-24-...md` §11
