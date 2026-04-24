# MJ — MEV Private Mempool instrumentation (optionnel, conditionnel)

**Priorité** : 🟢 P4 (optionnel, hypothèse à valider avant engagement)
**Charge estimée** : S-M (1-3 jours selon résultat instrumentation)
**Branche suggérée** : `chore/mev-instrumentation` puis `chore/polygon-private-mempool` si conditionnel validé
**Prérequis** : aucun
**Bloque** : — (optionnel)

---

## 1. Objectif business

**Mesurer** si le trading polycopy subit un front-running MEV significatif sur la mempool Polygon publique, avant d'engager l'action corrective (switch RPC vers Private Mempool Polygon). **Seule convergence 1/3** des deep-searches (Gemini unique, Perplexity et Claude silence). **Hypothèse à vérifier**, pas à acter directement. Si instrumentation révèle impact → one-line fix RPC URL (coût nul). Si pas d'impact → documenter non-issue et passer.

## 2. Contexte & problème observé

### Finding source unique Gemini

- **[F47] ⚠️ 1/3 Gemini unique** (synthèse §4.4) : **Gemini §"MEV Risk"** : "Sandwiching occurs frequently on nominal sizes as small as **$50-$100** if the liquidity pool is thin and the user's slippage parameter is loose. Polygon has recently integrated a Private Mempool architecture natively into the network. Transactions routed through this specific private RPC endpoint bypass the public mempool entirely and are sent directly to the elected block producers. **Arbitrage and MEV bots have zero visibility into these pending transactions, completely neutralizing the threat of front-running**. This single configuration change guarantees 100% protection against sandwich attacks on FOK copy-trades". Sources primaires :
  - [Polygon blog Private Mempool launch](https://polygon.technology/blog/polygon-launches-private-mempool-mev-protection-is-now-a-one-line-integration)
  - [Digital Today article](https://www.digitaltoday.co.kr/en/view/45527/polygon-unveils-private-mempool-to-block-frontrunning-and-sandwich-attacks)
  - [Arkham MEV beginner guide](https://info.arkm.com/research/beginners-guide-to-mev)

### Silence Perplexity + Claude — pourquoi ?

- **Perplexity (quantitatif)** : aurait dû capter un changement infra documenté. Absence = threat non quantifié sur Polymarket spécifiquement dans les sources que Perplexity consulte.
- **Claude (architectural)** : aurait pu mentionner MEV. Absence = sur capital $1k-10k avec FOK via Polymarket Relayer, la surface MEV est peut-être faible.

### Analyse technique polycopy (synthèse §4.4)

**Architecture flow polycopy** :
```
py-clob-client.create_and_post_order()
  → POST /order (Polymarket CLOB API)
  → Matching engine OFF-CHAIN (Polymarket infra privée)
  → Trades settled ON-CHAIN via CTF Exchange contract
```

La **mempool Polygon n'intervient que côté settlement** (après le matching). Les searchers MEV peuvent théoriquement front-run les **settlement transactions**, mais ces transactions sont **déjà matched** off-chain — le prix du trade est **fixé** avant le settlement, l'ordre de settlement ne peut pas être changé par MEV.

**Conclusion synthèse §4.4** : "La surface MEV est donc **très réduite** en copy-trading Polymarket via CLOB + Relayer. Le MEV peut affecter les settlements gasless via relayer (pas polycopy direct) ou les manual withdrawals/deposits USDC (rare en ops polycopy). **Verdict** : flagger comme sujet à creuser session I optionnelle, pas priorité immédiate."

### Asymmetric bet

**Coût d'instrumentation** (MJ.1) : 1-2 jours de travail.
**Coût de switch RPC** (MJ.2 conditionnel) : 0.5 jour + 1 ligne env.
**Bénéfice si MEV réel** : protection complète sandwich attacks sur FOK orders.
**Coût si MEV non-issue** : documenter + skip, travail limité à instrumentation.

→ **Asymmetric bet positif** : bas coût, haute valeur si positif. Justifie MJ même en P4.

## 3. Scope (items détaillés)

### MJ.1 — Instrumentation MEV impact

- **Location** : [src/polycopy/executor/pipeline.py](../../src/polycopy/executor/pipeline.py) (path live M3) + nouvelle analytics `scripts/analyze_mev_impact.py`
- **Ce qu'il faut faire** :
  - Pour chaque FOK order **live** sent, logger :
    ```
    t_expected_fill_price = mid_price at submission time (depuis WSS cache ME.1)
    t_actual_fill_price = price retourné par CLOB API
    slippage = actual - expected  (en USDC)
    slippage_pct = slippage / expected_fill_price
    notional = size × price
    order_size_bucket = bucket($50-100, $100-200, $200-500, >$500)
    ```
  - Persister dans nouvelle table `mev_slippage_samples` (migration 0012) :
    ```sql
    CREATE TABLE mev_slippage_samples (
        id INTEGER PRIMARY KEY,
        order_id VARCHAR(66) NOT NULL,
        expected_price FLOAT NOT NULL,
        actual_price FLOAT NOT NULL,
        slippage_pct FLOAT NOT NULL,
        notional FLOAT NOT NULL,
        size_bucket VARCHAR(16) NOT NULL,
        timestamp DATETIME NOT NULL,
        token_id VARCHAR(66),
        tx_hash VARCHAR(66)
    );
    ```
  - Script `scripts/analyze_mev_impact.py` : sur 100 FOK orders collected :
    - Distribution slippage_pct par size_bucket
    - Detect pattern systematic adverse (mean slippage > 0 significatif = front-running potentiel)
    - p50, p95, p99 slippage_pct
    - Comparaison pre/post switch (si MJ.2 shippé)
  - **Décision D1** : sample size minimum 100 FOK orders **live** (pas dry-run, qui n'a pas de slippage réel). Implique attendre post-go-live pour mesurer. Timing MJ : POST-live seulement.
  - **Alternative** (pre-live) : mesurer en dry-run M8 realistic_fill via `/book` simulation. Sert de baseline, mais **ne mesure pas le vrai MEV** (simulation sans soumission mempool).
  - Dashboard optionnel `/mev-analysis` read-only (hors scope MJ v1, MH.10 extension future).
- **Tests requis** :
  - `test_mev_slippage_sample_persisted_on_live_order`
  - `test_mev_slippage_sample_computes_slippage_pct_correctly`
  - `test_migration_0012_mev_slippage_samples_schema`
- **Sources** : F47 Gemini + synthèse §4.4 analysis.
- **Charge item** : 1-2 jours (selon complexité data capture)

### MJ.2 — Switch RPC Private Mempool (conditionnel si MJ.1 positif)

- **Location** : [src/polycopy/config.py](../../src/polycopy/config.py) + `.env.example`
- **Ce qu'il faut faire** :
  - **Pré-condition** : MJ.1 instrumentation sur ≥100 FOK orders live révèle **slippage median > 0.5% notional** ou **pattern systematic adverse** (slippage corrélé avec taille order ou timing suspicious).
  - Si pré-condition satisfaite :
    - Ajouter setting `POLYGON_RPC_URL: str = "<default public RPC>"` qui est l'URL utilisée par py-clob-client pour submit transactions.
    - Documenter dans `.env.example` : "Si MEV impact mesuré, switch vers Private Mempool : `POLYGON_RPC_URL=https://polygon-rpc.thirdwebapp.com/private-mempool` (exemple URL, vérifier Polygon blog officiel pour l'URL canonique)".
    - Mise à jour CLAUDE.md §Sécurité avec note MEV.
    - Restart → MJ.1 continue l'instrumentation post-switch → comparison backtest.
  - **Si pré-condition NON satisfaite** : documenter dans `docs/development/mev_non_issue_polycopy_2026.md` avec données mesurées + conclusion + recommandation "pas de switch RPC nécessaire au moment de l'étude".
  - **Décision D2** : pas d'auto-switch. Décision humaine basée sur data MJ.1. User modifie `.env` s'il veut appliquer.
- **Tests requis** :
  - `test_config_accepts_custom_polygon_rpc_url`
  - `test_private_mempool_url_still_works_for_transaction_submission` (integration post-switch)
- **Sources** : Gemini §"MEV Risk" one-line fix + Polygon Private Mempool blog.
- **Charge item** : 0.5 jour (si switch) OR 0 jours (si skip)

## 4. Architecture / décisions clefs

- **D1** : instrumentation MJ.1 post-go-live uniquement. Justification : dry-run ne simule pas mempool submission, pas de MEV réel à mesurer. Si polycopy reste dry-run, MJ.1 n'a pas de cible.
- **D2** : switch RPC MJ.2 manuel humain, pas auto. Justification : décision business (coût switch = 0, bénéfice si positif = concret), user decide après review data.
- **D3** : sample minimum 100 FOK orders. Justification : statistique suffisante pour détecter pattern systematic (p95, p99 stables sur 100 samples).
- **D4** : table dédiée `mev_slippage_samples` séparée de `trade_latency_samples`. Justification : ségrégation data (latence vs MEV = concerns différents), permet drop table si MEV confirmed non-issue.

## 5. Invariants sécurité

- **Triple garde-fou M3 + 4ᵉ M8** : intact. MJ ajoute instrumentation post-submission, pas de nouveau path signature.
- **Zéro secret loggé** : `mev_slippage_samples` contient `order_id`, `prices`, `size_bucket`, `tx_hash` — tous publics on-chain. Aucun secret.
- **Read-only instrumentation** : MJ.1 n'influence **pas** la décision d'envoyer l'order. Capture + persistance uniquement.
- **Private Mempool URL** (si MJ.2 appliqué) : URL publique Polygon, pas de credential. Discipline cohérente avec autres `POLYGON_RPC_URL` configs.
- **Ne pas activer MJ en dry-run** : data trompeuse (pas de vraie soumission mempool). Guard : `if settings.execution_mode != "live": skip MJ.1 collection`.

## 6. Hypothèses empiriques à valider AVANT ship

- **Q4** (synthèse §11) : "MEV réel sur nos tailles $50-$200 sur notre stack actuelle ?". **Méthode** : MJ.1 mesure directement.
- **H-EMP-9** (synthèse §8) : "MEV impact réel sur nos FOK orders < 0.5% notional moyen". **Seuil go** (pour MJ.2) :
  - Slippage median > 0.5% notional moyen → Positive signal → switch RPC.
  - Slippage median < 0.1% notional moyen → Negative signal → skip switch.
  - Entre 0.1% et 0.5% → zone grise, décision user basée sur distribution p95/p99.

## 7. Out of scope

- **Flashbots-like MEV protection** autre que Polygon Private Mempool : non applicable sur Polygon.
- **MEV bot development défensif** : spec futur hypothétique.
- **Avellaneda-Stoikov market making** : Gemini §"long terme uniquement". Hors scope polycopy 2026.
- **RPC upgrade vers premium paid tiers** (Alchemy, QuickNode, Infura) : Gemini §"Cross-Pillar" suggère si latence critique. Hors scope MJ (latence = ME).
- **Dashboard `/mev-analysis` panel** : extension MH future si MJ confirme issue + user veut monitoring continu.
- **Goldsky Turbo Pipelines MEV detection** : trop coûteux + hors scope.

## 8. Success criteria

1. **Tests ciblés verts** : ~5 nouveaux tests unit + 2 integration (si MJ.2 appliqué).
2. **MJ.1 instrumentation opérationnelle** : post-go-live, `mev_slippage_samples` se peuple avec ≥10 rows sur première semaine live.
3. **Rapport `docs/development/mev_analysis_2026.md`** : post 100 samples collectés, rapport avec distribution + conclusion.
4. **Décision documentée** : user décide switch OU skip, documenté dans rapport.
5. **Si switch appliqué** : post-switch 100 samples, comparaison pre/post shows slippage reduction statistiquement significative.

## 9. Mapping origines (traçabilité)

| Item | Audit | Session | Deep-search | Synthèse roadmap |
|---|---|---|---|---|
| MJ.1 | — | — (new) | F47 (1/3 Gemini) + synthèse §4.4 | #30 |
| MJ.2 | — | — (new, conditionnel) | F47 + Polygon Private Mempool blog | #40 |

## 10. Prompt de génération de spec

````markdown
# Contexte

Lis `docs/next/MJ.md` en entier. C'est le brief actionnable du module MJ (MEV
Private Mempool instrumentation). **P4 optionnel** — hypothèse MEV impact à
valider empiriquement avant switch RPC. Asymmetric bet : bas coût
instrumentation, haute valeur si positif.

# Tâche

Produire `docs/specs/M23-mev-instrumentation.md` suivant strictement le format
des specs M1..M22 existantes.

Numéro : M23 (après séquentiel — ajuster selon ship réel).

# Prérequis (à lire avant de rédiger)

- `CLAUDE.md` §Sécurité (triple+4ᵉ garde-fou, invariants CLOB creds)
- `docs/specs/M3-executor.md` (path live FOK order, py-clob-client usage)
- `docs/specs/M13_dry_run_observability_spec.md` comme template de forme
- Synthèse §4.4 MEV analysis complète
- Gemini §"MEV Risk" + Polygon Private Mempool [blog](https://polygon.technology/blog/polygon-launches-private-mempool-mev-protection-is-now-a-one-line-integration)

# Contraintes

- Lecture seule src/ + docs
- Écriture uniquement `docs/specs/M23-mev-instrumentation.md`
- Longueur cible : 600-900 lignes (plus court car MJ est focused)
- Migration Alembic 0012 : documenter (mev_slippage_samples table)
- Clarifier §Architecture que MJ.1 = pre-requisite + MJ.2 conditionnel
- Ne pas activer MJ instrumentation en dry-run (guard explicit)

# Livrable

- Le fichier `docs/specs/M23-mev-instrumentation.md` complet
- Un ping final ≤ 8 lignes : tests estimés, charge cumulée, ordre commits
  (MJ.1 instrumentation → collecte 100 samples → analyse → décision →
  éventuellement MJ.2 switch)
````

## 11. Notes d'implémentation

### Piège : `expected_fill_price` vs `actual_fill_price` timing

Pour mesurer MEV précisément, `expected_fill_price` doit être capturé **exactement** au moment de la soumission de l'order, pas avant. Sinon on mesure le mouvement de marché normal + MEV, pas juste MEV.

Pattern :
```python
# Just before submit :
expected = await self._mid_price_client.get(token_id)
t_submit = now()

# Submit :
response = await self._clob_client.post_order(...)

# Parse actual :
actual = response.price_filled
slippage = actual - expected
```

### Piège : dry-run doesn't have MEV

MJ.1 ne peut mesurer MEV qu'en **live mode**. En dry-run M8 realistic_fill simulation, `actual_price` est calculé via `/book` snapshot, pas via submission mempool. Pas de front-runner possible. **Guard strict** dans MJ.1 : `if settings.execution_mode != "live": return` silencieusement.

### Piège : Private Mempool URL canonique

Polygon a plusieurs providers RPC qui hébergent Private Mempool (Alchemy, Infura, ThirdWeb, etc.). URL exacte à confirmer via [Polygon blog](https://polygon.technology/blog/polygon-launches-private-mempool-mev-protection-is-now-a-one-line-integration). Ne pas hardcoder une URL qui pourrait disparaître — documenter l'**endpoint pattern** dans `.env.example`, user choisit son provider.

### Piège : slippage attribution

`slippage = actual - expected` peut être positif **sans MEV** :
- Market move naturel entre submission et fill (ms window, usually ignorable sur Polymarket).
- Depth insufficient → fills plus loin dans le book.
- Wrong side ordering (BUY YES = slippage positive = bad fill).

**Mitigation** : filtrer samples par `size_bucket` (MEV targetingles bigger orders typically), comparer distribution cross-size. Si pattern MEV, grosse asymmetry sur les `>$500` bucket.

### Piège : sampling bias

Post-go-live, polycopy ne fait probablement **pas** 100 orders par jour (avec sizing conservateur + scoring strict). Sample 100 orders = **plusieurs semaines voire mois**. Documenter dans MJ.1 que timing collection = patient.

### Références externes

- **Gemini §"MEV Risk" complete** :
  - [Polygon blog Private Mempool](https://polygon.technology/blog/polygon-launches-private-mempool-mev-protection-is-now-a-one-line-integration)
  - [Digital Today](https://www.digitaltoday.co.kr/en/view/45527/polygon-unveils-private-mempool-to-block-frontrunning-and-sandwich-attacks)
  - [Arkham MEV guide](https://info.arkm.com/research/beginners-guide-to-mev)
  - [Dev.to Vathsaman MEV implementation](https://dev.to/vathsaman/mempool-monitoring-for-mev-bots-technical-implementation-guide-4k38)
  - [arXiv Private L2 mempool](https://arxiv.org/html/2601.19570v1)
  - [Chainlink front-running DeFi](https://chain.link/article/front-running-defi)
  - [SolidQuant MEV bot sim](https://medium.com/@solidquant/first-key-to-building-mev-bots-your-simulation-engine-c9c0420d2e1)
- **Synthèse §4.4 "MEV / Private Mempool"** : analyse complète + conclusion "surface MEV réduite en copy-trading Polymarket via CLOB + Relayer".

### Questions ouvertes pertinentes à MJ

- **Q4** (synthèse §11) : réponse directe via MJ.1.
- **Q9** (implicite) : is MEV threat real pour polycopy-specific stack ? Resolved par rapport MJ analysis.
