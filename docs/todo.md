# Todo machine prod (post-M14 + M16 + M15)

Actions à effectuer sur le PC qui fait tourner le bot après le merge
M14 (scoring v2.1) + M16 (dynamic taker fees + EV adjustment) + M15
(anti-toxic lifecycle + internal PnL feedback).
Ordre recommandé. Garde ce fichier à jour ou raye au fur et à mesure.

---

## ⚠️ §0. PRÉ-RESTART : timing Polymarket V2 (cutover 28 avril 2026 ~11h UTC)

**Polymarket annonce officiellement** ([docs.polymarket.com/v2-migration](https://docs.polymarket.com/v2-migration)) :

- **Go-live** : 28 avril 2026, ~11h00 UTC, ~1 heure de downtime.
- **Tous les open orders V1 wiped** au cutover.
- **Tous les bots/intégrations API doivent migrer au SDK V2** (`py-clob-client-v2`,
  package séparé) avec re-sign de la nouvelle struct Order avant le cutover.
- **Polymarket USD** (pUSD) remplace USDC.e comme collateral — pour les API-only
  traders (notre cas), un wrap manuel one-time est requis via le contrat
  `CollateralOnramp`.
- **Test environnement live** post-annonce : `https://clob-v2.polymarket.com`
  (la prod URL `clob.polymarket.com` switche automatiquement le 28 avril ~11h UTC).

**Conséquence pour polycopy** : si tu restart le bot V1 cette semaine, **il va
casser mardi 28 avril ~11h UTC** car la struct Order V1 sera rejetée par le
nouvel orderbook V2. Solution : **combiner restart + reset DB + migration V2
en un seul deploy mardi 28 avril**.

**Plan d'action recommandé** :

1. **Aujourd'hui → lundi 27 avril** : préparer la branche `feat/ctf-exchange-v2` :
   - Bumper `py-clob-client` → `py-clob-client-v2==1.0.0` (à confirmer sur PyPI).
   - Adapter `ClobWriteClient` constructeur (options object, `chain` au lieu
     de `chainId`).
   - Adapter `OrderBuilder` / signature path : drop `nonce` / `feeRateBps` /
     `taker` / `expiration`, ajout `timestamp` (ms) / `metadata` (bytes32) /
     `builder` (bytes32). EIP-712 domain version `"1"` → `"2"`.
   - Swap `FeeRateClient` (M16) : `/fee-rate?token_id=` → `getClobMarketInfo()`
     V2. Garder le flag `STRATEGY_FEES_AWARE_ENABLED`.
   - Tests intégration contre `https://clob-v2.polymarket.com`.
2. **Lundi 27 avril soir** : merger la branche sur `main` après tests verts.
   Ne pas restart le bot prod tant que Polymarket n'a pas cutover (V1
   toujours fonctionnel jusqu'au 28 avril 11h UTC).
3. **Mardi 28 avril ~10h30 UTC** : exécuter les sections §1 → §6 ci-dessous
   pour le restart + reset DB + cutover V2 combiné. Cf. nouvelle §14 infra
   pour la procédure spécifique migration V2 (wrap USDC.e → pUSD, etc.).

**Si tu n'as pas le temps de préparer la branche V2 d'ici lundi** : reporte
le restart **après** le cutover Polymarket. Le bot V1 actuel continue de
tourner sur la DB legacy jusqu'à mardi matin sans risque (ses positions
virtuelles s'éteignent naturellement à résolution des marchés).

---

## 1. Pull main (immédiat, ~10s)

```bash
ssh <prod-machine>
cd ~/Documents/GitHub/polycopy   # ou le path de ton install
git pull origin main
```

Les 8 commits MA.x + script H-EMP + spec M14 doivent être visibles,
**plus** les 7 commits M16 (spec + 5 MC.x + CLAUDE.md), **plus** les
8 commits MB.x (M15 — MB.1 storage / MB.7 arbitrage gate / MB.2 scoring
v2.1.1 / MB.3 ranking-based / MB.4 H-007 fix / MB.5 empirical margin /
MB.6 probation / MB.8 auto-blacklist) dans
`git log --oneline -25`.

## 2. Vérifier que rien ne casse au boot avec la DB existante (immédiat)

**Important** : `Settings.scoring_version: Literal["v1", "v2.1", "v2.1.1"]`
(M15 étend `v2.1.1`). Si ton `.env` contient `SCORING_VERSION=v2` (M12),
le boot va **crasher** avec `ValidationError: scoring_version`.

```bash
grep "SCORING_VERSION" .env
# Si ça affiche SCORING_VERSION=v2 → soit retire la ligne (default v1),
# soit mets SCORING_VERSION=v1 explicitement.
# v2.1.1 est accepté mais NE PAS flip avant cutover post-30j (cf. §11).
```

**M15 défaut sécurisé** : `SCORING_VERSION=v1` reste default — v2.1.1
ne pilote pas tant que tu ne flip pas explicitement. La migration 0009
(MB.1) ajoute `my_positions.source_wallet_address` (nullable) +
`target_traders.is_probation` (NOT NULL default False) au boot via
`alembic upgrade head` dans `init_db`. **Defaults safe**, aucune action
requise. Le facteur `internal_pnl` collecte cold-start (None) jusqu'à
≥10 closed positions copiées par wallet.

**M16 défaut sécurisé** : `STRATEGY_FEES_AWARE_ENABLED=true` par défaut
active le fee adjustment. Aucune action requise pour activer — ça marche
out-of-the-box. **Si** tu observes en runtime un comportement bizarre
(beaucoup de rejets `ev_negative_after_fees` sur des marchés Politics
qui devraient être fee-free), set explicitement `STRATEGY_FEES_AWARE_ENABLED=false`
dans `.env` pour désactiver et investiguer.

```bash
# Optionnel : confirmer que les 3 settings M16 sont bien lus.
grep -E "STRATEGY_FEES_AWARE|STRATEGY_MIN_EV|STRATEGY_FEE_RATE_CACHE_MAX" .env
# Pas de match attendu (defaults s'appliquent : true / 0.05 / 500).
```

Restart le bot :

```bash
# Via systemd (si configuré) :
sudo systemctl restart polycopy
# OU manuel :
pkill -f "polycopy" && python -m polycopy --verbose &
```

Vérifie les premiers logs (pas d'erreur au boot) :

```bash
tail -30 ~/.polycopy/logs/polycopy.log | grep -E "ERROR|WARNING|scoring_version|fee_rate_client_instantiated"
# Doit montrer :
#   scoring_version=v1 (default)
#   fee_rate_client_instantiated cache_max=500 (M16)
#   strategy_started ... fees_aware=true (M16)
# Aucune erreur.
```

**M16 smoke test fee fetch** : après ~5 min de bot tournant, vérifier
qu'il y a bien des events `fee_rate_fetched` dans les logs (au moins
1 par token actif distinct vu en BUY) :

```bash
grep "fee_rate_fetched" ~/.polycopy/logs/polycopy.log | head -5
# Attendu : events JSON avec token_id, base_fee_bps, rate.
# Sur un marché crypto récent : base_fee_bps=1000 attendu.
# Sur un marché Politics : base_fee_bps=0 attendu (fee-free).
```

**Si tu vois en boucle** `fee_rate_fetch_failed_using_conservative_fallback` :
- Soit Polymarket /fee-rate endpoint est down (regarder Twitter/status)
- Soit ta connexion est bloquée vers `https://clob.polymarket.com/fee-rate`
- Le fallback Decimal(0.018) protège quand même, le bot continue à
  fonctionner mais sur-rejette des trades légitimes. Pas urgent.

## 3. Reset DB (recommandé avant cutover v2.1.1, optionnel pour rester en v1 shadow)

**Quand le faire** : avant de flip `SCORING_VERSION=v2.1.1`. Pas obligatoire
si tu restes en `v1` (default) — la DB existante continue à fonctionner.

**Pourquoi M15 amplifie le besoin** :
1. La DB actuelle contient les rows `trader_scores.scoring_version="v2"`
   M12 obsolète + des positions `my_positions` héritées **sans**
   `source_wallet_address` (NULL) car ouvertes avant la migration 0009.
   Le collecteur internal_pnl (MB.1) **filtre exact match** sur ce champ
   → ces rows historiques sont ignorées (cold-start naturel pour TOUS
   les wallets jusqu'à ce que tu accumules ≥10 closed positions copiées
   post-merge).
2. Si tu reset la DB, les nouvelles `my_positions` seront créées avec
   `source_wallet_address` populated dès le premier fill → la collecte
   30j commence proprement à T0.
3. Si tu ne reset PAS, le shadow period internal_pnl démarre quand
   même mais avec un retard ~1-7j (le temps que le bot ouvre puis ferme
   ≥10 nouvelles positions par wallet ACTIVE).

```bash
# 1. Arrête le bot.
sudo systemctl stop polycopy   # OU pkill -f polycopy

# 2. Backup au cas où (optionnel, recommandé) :
cp ~/.polycopy/data/polycopy.db ~/.polycopy/data/polycopy.db.bak.$(date +%Y%m%d)

# 3. Wipe.
rm ~/.polycopy/data/polycopy.db

# 4. Restart — Alembic upgrade head crée la DB fraîche (chain
#    0001 → 0002 → ... → 0007 → 0009 ; le 0008 a été sauté par M15
#    pour garder la place réservée à un fix MC futur sans renumérotation).
sudo systemctl start polycopy   # OU python -m polycopy --verbose
```

**Conséquence** : tu perds toutes les données dry-run actuelles (positions
virtuelles, snapshots PnL, trader_scores historiques). Si tu veux les
analyser plus tard, exporte avant le reset :

```bash
sqlite3 ~/.polycopy/data/polycopy.db ".dump" > ~/.polycopy/backup_pre_m15.sql
```

**Smoke test post-reset** :

```bash
# Le schéma DB doit contenir les colonnes M15 MB.1.
sqlite3 ~/.polycopy/data/polycopy.db ".schema my_positions" | grep source_wallet
# → source_wallet_address VARCHAR(42)
sqlite3 ~/.polycopy/data/polycopy.db ".schema target_traders" | grep is_probation
# → is_probation BOOLEAN NOT NULL DEFAULT 0
```

## 3.5. Configuration `.env` recommandée pour le restart en dry-run réel

Bloc complet à appliquer après le reset DB (§3) et avant le restart. Couvre
tous les modules shippés (M1 → M16). **Mode dry-run** — capital virtuel
$1000, aucun ordre live signé.

```dotenv
# ─── Mode d'exécution ────────────────────────────────────────────────────
EXECUTION_MODE=dry_run
DRY_RUN_REALISTIC_FILL=true              # M8 — simulate FOK contre /book réel
DRY_RUN_NEG_RISK_RESOLUTION_ENABLED=true # M13 v2 — resolve neg_risk markets
DRY_RUN_VIRTUAL_CAPITAL_USD=1000         # capital virtuel, cohérent avec H-EMP

# ─── Scoring v2.1 décisionnel direct (recommandation : pas de shadow v1) ─
SCORING_VERSION=v2.1                     # v1 cassé sur 5 fronts (audit) — flip direct
SCORING_V2_SHADOW_DAYS=0                 # pas besoin de shadow en dry-run
SCORING_V2_COLD_START_MODE=false         # gates strict 50 trades / 30j
# v2.1.1 (M15 internal_pnl) calculé en parallèle automatiquement via le
# registry — flip vers SCORING_VERSION=v2.1.1 décisionnel à J+30 si
# H-EMP-3 + H-EMP-11 OK (cf. §8 todo.md).

# ─── Discovery automatique ──────────────────────────────────────────────
DISCOVERY_ENABLED=true
DISCOVERY_INTERVAL_SECONDS=21600         # 6h (default)
DISCOVERY_BACKEND=data_api               # zéro dépendance externe
DISCOVERY_CANDIDATE_POOL_SIZE=100
TRADER_DAILY_PNL_ENABLED=true            # prérequis Sortino/Calmar v2.1
TRADER_SHADOW_DAYS=7                     # 7j d'observation avant promote shadow→active
DISCOVERY_SHADOW_BYPASS=false            # ne pas bypass — observer 7j est important
MAX_ACTIVE_TRADERS=10                    # cap dur pool ACTIVE

# ─── Eviction compétitive (M5_bis) ──────────────────────────────────────
EVICTION_ENABLED=true                    # observer le mécanisme en dry-run
EVICTION_SCORE_MARGIN=0.10               # MA.7 default — recalibre via §11 après 7j
EVICTION_HYSTERESIS_CYCLES=3
MAX_SELL_ONLY_WALLETS=10

# ─── M15 anti-toxic (defaults safe) ─────────────────────────────────────
SCORING_ABSOLUTE_HARD_FLOOR=0.30
PROBATION_MIN_TRADES=10
PROBATION_FULL_TRADES=50
PROBATION_MIN_DAYS=7
PROBATION_FULL_DAYS=30
PROBATION_SIZE_MULTIPLIER=0.25           # quarter-Kelly
AUTO_BLACKLIST_PNL_THRESHOLD_USD=-5.0    # MB.8 fire si pnl observé < -$5 / 30j
AUTO_BLACKLIST_MIN_POSITIONS_FOR_WR=30
SCORING_INTERNAL_MIN_POSITIONS=10        # cold-start internal_pnl
SCORING_INTERNAL_PNL_SCALE_USD=10.0

# ─── M16 fees dynamiques (post-V2 : refactor swap source — cf. §14) ─────
STRATEGY_FEES_AWARE_ENABLED=true
STRATEGY_MIN_EV_USD_AFTER_FEE=0.05
STRATEGY_FEE_RATE_CACHE_MAX=500

# ─── Strategy core ──────────────────────────────────────────────────────
COPY_RATIO=0.01                          # 1% du source size
MAX_POSITION_USD=20.0                    # cap par position
MAX_SLIPPAGE_PCT=2.0
KILL_SWITCH_DRAWDOWN_PCT=20.0
STRATEGY_MAX_ENTRY_PRICE=0.97            # rejet BUY > 0.97 (M13 bug 4)
STRATEGY_CLOB_WS_ENABLED=true            # WSS market channel M11
STRATEGY_CLOB_WS_MAX_SUBSCRIBED=500

# ─── Monitoring + dashboard ─────────────────────────────────────────────
DASHBOARD_ENABLED=true
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8787
TELEGRAM_BOT_TOKEN=<ton_token>
TELEGRAM_CHAT_ID=<ton_chat_id>
TELEGRAM_STARTUP_MESSAGE=true

# ─── Identité multi-machine (M12_bis) ───────────────────────────────────
MACHINE_ID=UNI-DEBIAN                    # ou ton hostname
MACHINE_EMOJI=🏫                          # au choix

# ─── BLACKLISTED_WALLETS — laisse vide initialement ─────────────────────
# Le système MB.8 va auto-blacklister les wallets toxiques (PnL < -$5
# OU win-rate < 25% sur 30+ positions). Ajouter manuellement seulement
# si tu connais des wallets spécifiquement à exclure.
BLACKLISTED_WALLETS=

# ─── TARGET_WALLETS — pinned wallets (jamais demote-ables) ──────────────
# Si tu veux suivre des wallets spécifiques, ajoute-les ici (CSV ou JSON).
# Sinon, DISCOVERY_ENABLED=true va remplir le pool automatiquement.
TARGET_WALLETS=
```

**Conditions critiques à vérifier avant restart** :

```bash
# 1. .env n'a pas de SCORING_VERSION=v2 (M12 obsolète, refusé par Pydantic)
grep "SCORING_VERSION" .env
# Attendu : SCORING_VERSION=v2.1

# 2. Pas de DRY_RUN=true legacy (remplacé par EXECUTION_MODE M10)
grep "^DRY_RUN=" .env
# Attendu : aucune ligne (ou bien commentée)

# 3. POLYMARKET_PRIVATE_KEY + POLYMARKET_FUNDER absents (pas requis en dry-run)
grep -E "POLYMARKET_PRIVATE_KEY|POLYMARKET_FUNDER" .env
# Attendu : absent ou vide en dry-run. Ces credentials ne sont consommés
# qu'au flip EXECUTION_MODE=live.
```

## 4. Smoke test 2 cycles shadow v2.1 (recommandé avant cutover)

À cadence default (`DISCOVERY_INTERVAL_SECONDS=21600` = 6h), 2 cycles =
~12h. Pendant ce temps, le bot doit calculer v1 (pilote) ET v2.1 (shadow).

```bash
# Wait ~12h, puis vérifie :
sqlite3 ~/.polycopy/data/polycopy.db \
  "SELECT scoring_version, COUNT(*), COUNT(DISTINCT wallet_address) \
   FROM trader_scores GROUP BY scoring_version;"
```

Attendu :
- `v1` : N rows, M wallets distincts.
- `v2.1` : N rows similaires, M wallets distincts (couverture ≥ 0.8 × v1).

**Régression-clé à valider** : aucun wallet avec la même valeur exacte sur
≥ 10 cycles consécutifs (élimination du fixed-point trap C7) :

```bash
sqlite3 ~/.polycopy/data/polycopy.db \
  "SELECT wallet_address, score, COUNT(*) AS n_repeats \
   FROM trader_scores WHERE scoring_version='v2.1' \
   GROUP BY wallet_address, score HAVING n_repeats >= 10;"
# Doit retourner 0 row.
```

## 5. Validation H-EMP après ~14 jours de shadow

```bash
cd ~/Documents/GitHub/polycopy
source .venv/bin/activate
python scripts/validate_ma_hypotheses.py \
  --db ~/.polycopy/data/polycopy.db \
  --output /tmp/h_emp_post_shadow.txt
cat /tmp/h_emp_post_shadow.txt
echo "Exit: $?"
```

Seuils go (cf. spec M14 §14.4) :
- **H-EMP-1** : `risk_adjusted` contribue ≥ 40 % de la variance totale.
- **H-EMP-2** : σ relatif < 10 % sur ≥ 80 % des wallets ACTIVE.

Si exit code 0 → pass, OK pour cutover v2.1.
Si exit code 1 → investiguer (cf. spec M14 §14.4 causes probables).

## 6. Flip cutover v2.1 (si H-EMP OK + observation dashboard cohérente)

```bash
# Édite .env
SCORING_VERSION=v2.1

# Restart
sudo systemctl restart polycopy
```

Observe le dashboard pendant 7-14 jours :
- `/pnl` : pas de drawdown anormal.
- `/traders/scoring` : top-10 v2.1 stable cycle-to-cycle.
- `/performance` : pas de régression vs baseline v1.

Si régression : revert avec `SCORING_VERSION=v1` + restart. Aucune migration
DB à rollback (versioning sacré, append-only).

## 7. M15 — Démarrer la collecte 30j d'`internal_pnl_score` (post cutover v2.1)

Une fois `SCORING_VERSION=v2.1` flipped (étape 6) et le bot tournant,
M15 MB.2 calcule **en parallèle** le score v2.1.1 dès qu'il y a un
`PoolContext` posé. Mais le facteur `internal_pnl` (poids 0.25) est
**cold-start** sur tous les wallets jusqu'à ≥10 closed positions copiées
par wallet — soit ~7-30 jours selon ton volume de copy-trading.

**Aucune action requise** — la collecte est automatique. Vérifie au
bout de ~7j que des `my_positions.realized_pnl` se cristallisent :

```bash
sqlite3 ~/.polycopy/data/polycopy.db \
  "SELECT
     source_wallet_address,
     COUNT(*) FILTER (WHERE closed_at IS NOT NULL) AS closed,
     COUNT(*) FILTER (WHERE realized_pnl IS NOT NULL) AS pnl_realized,
     ROUND(SUM(realized_pnl), 4) AS pnl_total
   FROM my_positions
   WHERE source_wallet_address IS NOT NULL
   GROUP BY source_wallet_address
   ORDER BY closed DESC LIMIT 20;"
```

Attendu post-7j : ~5-15 wallets avec `closed ≥ 5` (sur le pool ACTIVE).
Post-30j : ≥50 % des ACTIVE avec `closed ≥ 10` → coverage internal_pnl
suffisante pour H-EMP-3.

## 8. M15 — Validation H-EMP-3 + H-EMP-11 + H-EMP-13 (post-30j)

**Quand** : 30 jours calendaires après le cutover v2.1 (et donc après le
démarrage de la collecte v2.1.1 shadow). **Bloquant** avant le flip
`SCORING_VERSION=v2.1.1`.

```bash
# Le script existe déjà côté MA pour H-EMP-1/2 (v2.1).
# Pour les H-EMP MB (M15), un script dédié sera ajouté par MF (cf. spec
# M15 §14.8 — pas créé dans le scope MB.1..MB.8). En attendant, audit
# manuel via SQL :

# H-EMP-3 : Spearman ρ(internal_pnl_score, score v2.1.1) ∈ [0.1, 0.7]
# (ni redondance ni bruit). Calcul off-DB via Python pandas/scipy après
# export trader_scores.metrics_snapshot JSON.
# Seuil go : 0.1 < ρ < 0.7.

# H-EMP-11 : ≥90 % des wallets pool passent le gate not_arbitrage_bot.
sqlite3 ~/.polycopy/data/polycopy.db \
  "SELECT COUNT(*) FROM trader_events
   WHERE event_type='gate_rejected'
   AND event_metadata LIKE '%arbitrage_bot_pattern%'
   AND created_at >= datetime('now', '-30 days');"
# Compare au total des wallets candidats scorés sur 30j. Ratio rejet
# attendu < 10% si H-EMP-11 valide.

# H-EMP-13 (informatif) : % wallets avec cumulative_pnl_90d > 0 dans
# le pool ACTIVE+SHADOW. Pas go/no-go, juste sanity sur la qualité du
# pool de candidats discovered.
```

**Si H-EMP-3 hors [0.1, 0.7]** :
- ρ < 0.1 → internal_pnl est du bruit pur (pas de signal). Investiguer
  le filtrage `simulated`/`source_wallet_address` ou augmenter
  `SCORING_INTERNAL_MIN_POSITIONS` (+ shadow plus long).
- ρ > 0.7 → redondance avec score v2.1. Réduire `SCORING_INTERNAL_PNL_SCALE_USD`
  à 50.0 (sigmoid plus plat) ou réduire le poids 0.25 → 0.15 (bumper
  `SCORING_VERSION` à `v2.1.2` cohérent versioning sacré).

**Si H-EMP-11 < 90 %** : gate `not_arbitrage_bot` trop strict. Investiguer
si `RawPosition.outcome_index` est rarement présent dans Data API
`/positions` (< 70 %) → MB.7 net_exposure_ratio peu fiable, à reporter
à MF pour fallback Goldsky subgraph.

**STOP cutover v2.1.1 si H-EMP-3 ou H-EMP-11 fail.**

## 9. M15 — Flip cutover v2.1.1 (si H-EMP OK + observation cohérente)

```bash
# Édite .env
SCORING_VERSION=v2.1.1

# Restart
sudo systemctl restart polycopy
```

Observe le dashboard pendant 7-14 jours :
- `/pnl` : pas de drawdown anormal (la branche full v2.1.1 doit
  améliorer ou maintenir vs v2.1).
- `/traders/scoring` : top-10 v2.1.1 différent de v2.1 par ≥3 ranks
  moyens (signal du facteur internal_pnl actif).
- `/performance` : aucun wallet ACTIVE avec PnL observé < -$5 ne reste
  ACTIVE plus de 24h — **MB.8 auto-blacklist** doit le sortir.
- Telegram : 0-2 alertes `trader_auto_blacklisted` attendues sur 14j.

Si régression : revert avec `SCORING_VERSION=v2.1` + restart. Aucune
migration DB à rollback (versioning sacré, append-only).

## 10. M15 — Activer EVICTION_ENABLED après cutover v2.1.1 (optionnel)

`EVICTION_ENABLED=false` reste default. M15 MB.4 fixe l'audit H-007
(scores stale dans `_delta_vs_worst`) — le M5_bis competitive eviction
est désormais cohérent avec les scores fresh. M15 MB.5 logge au boot
la recommandation 1σ pour `EVICTION_SCORE_MARGIN` (observe la std réelle
de ton pool) :

```bash
grep "eviction_margin_empirical_recommendation" ~/.polycopy/logs/polycopy.log | tail -3
# event JSON avec empirical_1_sigma + recommended_min/max.
```

Si tu veux activer eviction post-cutover v2.1.1 :

```bash
EVICTION_ENABLED=true
EVICTION_SCORE_MARGIN=<valeur recommandée par MB.5>
EVICTION_HYSTERESIS_CYCLES=3
```

## 11. Recalibrer EVICTION_SCORE_MARGIN après ~7j post-ship (post cutover)

Le default M14 `EVICTION_SCORE_MARGIN=0.10` est calculé comme ≈ 1σ empirique
**théorique** post-rank-transform. La vraie σ se mesure après ~7j de
shadow v2.1 :

```bash
# Calcule la σ effective du pool v2.1
sqlite3 ~/.polycopy/data/polycopy.db \
  "SELECT cycle_at, score FROM trader_scores \
   WHERE scoring_version='v2.1' \
   ORDER BY cycle_at DESC LIMIT 200;" \
  | awk -F'|' '{print $2}' | python3 -c "import sys, statistics; \
xs=[float(l) for l in sys.stdin if l.strip()]; \
print(f'σ = {statistics.pstdev(xs):.4f}, suggested margin = {statistics.pstdev(xs):.4f}')"
```

Si la σ observée diffère significativement de 0.10 (ex : 0.07 ou 0.15),
ajuste :

```bash
# Édite .env
EVICTION_SCORE_MARGIN=0.07   # ou ce que tu observes
sudo systemctl restart polycopy
```

## 12. M16 — Surveiller l'impact fees sur 7-14 jours (post-ship)

Après ~7j de bot tournant en mode dry-run avec M16 actif, vérifier
empiriquement l'impact des fees sur le pool actuel. Cf. spec M16 §6
H-EMP-10 + Q5.

**Compteur rejets `ev_negative_after_fees`** :

```bash
sqlite3 ~/.polycopy/data/polycopy.db \
  "SELECT reason, COUNT(*) FROM strategy_decisions \
   WHERE decision='REJECTED' \
   AND decided_at >= datetime('now', '-7 days') \
   GROUP BY reason ORDER BY 2 DESC;"
```

**Seuils d'interprétation** :

- Si `ev_negative_after_fees == 0` sur 7j : la majorité de tes BUYs sont
  sur des markets fee-free (Politics, Tech, etc.) — comportement
  attendu. M16 silencieux mais protège quand même au cas où.
- Si `ev_negative_after_fees < 5%` du total rejets : seuil bien calibré,
  protection efficace sans bloquer trop de trades.
- Si `ev_negative_after_fees > 30%` du total rejets : seuil trop strict
  OU notre pool actif prend structurellement des trades sub-marginaux.
  → Investiguer : (1) baisser `STRATEGY_MIN_EV_USD_AFTER_FEE` à `0.02`
  ou `0.01`, (2) recalibrer `STRATEGY_MAX_ENTRY_PRICE` (rejet en amont
  des BUYs à ≥ 0.95 qui n'ont pas d'upside réel post-fee), (3) signal
  pour MA / MB que la pool sélectionne mal les wallets directionnels.

**Mesure de fee_drag total** sur les trades approuvés :

```bash
sqlite3 ~/.polycopy/data/polycopy.db \
  "SELECT
     COUNT(*) AS n_trades,
     SUM(CAST(json_extract(pipeline_state, '\$.fee_cost_usd') AS REAL)) AS total_fees,
     ROUND(AVG(CAST(json_extract(pipeline_state, '\$.fee_rate') AS REAL))*100, 4) AS avg_rate_pct
   FROM strategy_decisions
   WHERE decision='APPROVED'
   AND decided_at >= datetime('now', '-7 days')
   AND json_extract(pipeline_state, '\$.fee_cost_usd') IS NOT NULL;"
```

Si `total_fees > $10/jour` sur capital virtuel $1000 = 1% drag mensuel
non négligeable. Validation H-EMP-10 (impact fees ≥ 1% post-fees) =
**confirmée empiriquement** → MC sera quanti-utile en live.

## 14. Polymarket V2 cutover — procédure mardi 28 avril 2026 ~11h UTC

**Doc officielle** : [docs.polymarket.com/v2-migration](https://docs.polymarket.com/v2-migration).
**Spec M18** : [docs/specs/M18-polymarket-v2-migration.md](specs/M18-polymarket-v2-migration.md).

**M18 D11 — découverte clé** : le SDK V2 est **dual-version capable** — il
signe V1 ou V2 selon le résultat de `/version` endpoint backend. **On peut
shipper AVANT le cutover** (lundi 27 avril ~22h UTC) — le bot tourne en
continu pendant la fenêtre 11h UTC, le SDK auto-flip à la première erreur
`order_version_mismatch`. Élimine l'urgence ~30 min critique pré-cutover.

### Phase 1 — Ship pré-cutover (lundi 27 avril ~22h UTC)

Objectif : merge sur main + restart bot V2. Le SDK query `/version` au boot
(backend = V1 prod), signe V1 orders en attendant le cutover.

```bash
# 1. Sur la machine de dev — merge M18 sur main
cd ~/code/polycopy
git checkout main
git pull origin main
# (s'assurer que les commits ME.1 → ME.7 sont mergés)

# 2. Sur la machine prod — pull + restart
ssh prod-machine
cd ~/Documents/GitHub/polycopy
git pull origin main

# 3. Update les deps (récupère py-clob-client-v2)
source .venv/bin/activate
pip install -e .
pip uninstall py-clob-client -y   # remplacer V1 par V2 strictement
# Vérifier :
python -c "import py_clob_client_v2; print(py_clob_client_v2.__file__)"
# → ~/.../site-packages/py_clob_client_v2/__init__.py
python -c "import py_clob_client" 2>&1 | grep ModuleNotFoundError
# → doit lever (V1 désinstallé)

# 4. Vérifier .env (settings M18 nouveaux ; defaults OK)
grep -E "POLYMARKET_CLOB_HOST|POLYMARKET_USE_SERVER_TIME" .env  # absent OU defaults

# 5. Restart
sudo systemctl restart polycopy
# OU
pkill -f polycopy && python -m polycopy --verbose &
```

**Smoke validation Phase 1** :

```bash
tail -50 ~/.polycopy/logs/polycopy.log | grep -E "ERROR|executor_creds|machine_id"
# Doit montrer (en live mode) :
# - executor_creds_ready signature_type=2 use_server_time=true builder_code_set=false
# - machine_id_resolved
# - aucun ERROR
# En dry_run : pas de executor_creds_ready (lazy init préservé).
```

**Décision builder code** : optionnel mais ROI direct (~$50-150/an sur capital
$1k). Réclame ton code via
[polymarket.com/settings?tab=builder](https://polymarket.com/settings?tab=builder)
et set `POLYMARKET_BUILDER_CODE=0x...` (bytes32 hex). Le SDK V2 plomb la
valeur dans chaque `Order.builder` → fee rebates apparents sur le Builder
Leaderboard. Default `None` = comportement strict M3..M16 (fallback fonder
si non set + builder_code set).

### Phase 2 — Auto-flip backend (mardi 28 avril ~10h-12h UTC)

**Aucune action utilisateur requise.**

Polymarket bascule backend ~11h UTC. Si le bot POSTe un ordre live :

- Pré-flip (`/version=1`) : SDK signe V1, POST OK.
- Flip-window : SDK détecte `order_version_mismatch` au prochain POST, call
  `_resolve_version(force_update=True)`, retry en V2.
- Post-flip : tous les futurs orders signés V2.

En `dry_run` : pas de POST réel, pas d'opportunité de detect le flip.
**OK** — la transition se fait automatiquement à la première vraie POST live
post-flip `EXECUTION_MODE=live`.

### Phase 3 — Smoke post-cutover (mardi 28 avril ~11h30-12h UTC)

```bash
# 1. Vérifier que le bot tourne sans erreur signature_invalid
tail -100 ~/.polycopy/logs/polycopy.log | grep -E "ERROR|signature_invalid|order_version_mismatch"
# → 0 ERROR, 0 signature_invalid

# 2. Vérifier que get_fee_quote V2 path est exercé (au moins 1 BUY fee-aware)
grep "clob_market_fee_quote_fetched" ~/.polycopy/logs/polycopy.log | tail -5
# → events JSON avec condition_id, rate, exponent, taker_only

# 3. Surveiller les events fee_rate fallback V2 (instabilité endpoint)
grep "clob_market_fetch_failed_using_conservative_fallback" ~/.polycopy/logs/polycopy.log | tail -5
# → vide ou très rare. Si plein → V2 endpoint instable, alert dev.

# 4. Vérifier qu'aucun ordre dry-run n'est rejeté pour signature
sqlite3 ~/.polycopy/data/polycopy.db \
  "SELECT status, error_msg, COUNT(*) FROM my_orders \
   WHERE sent_at >= datetime('now', '-1 hour') GROUP BY 1, 2;"
# → uniquement SIMULATED (en dry_run) ou FILLED (en live).
# → AUCUN REJECTED avec error_msg LIKE '%signature%'.
```

### Phase 4 — Monitoring 24h post-cutover (mercredi 29 avril)

- Telegram heartbeat OK (event `heartbeat_sent` toutes les
  `TELEGRAM_HEARTBEAT_INTERVAL_HOURS`).
- Dashboard `/strategie` : decisions APPROVED se concrétisent en `MyOrder`
  valides.
- Aucune erreur `executor_error` ou `executor_auth_fatal`.
- Dashboard `/exécution` : decisions de sizing fee-aware cohérentes (le
  compteur `ev_negative_after_fees` peut bouger légèrement vs pré-M18 car la
  formule M16 est plus précise post-D6).

### Wrap pUSD pré-flip live (one-time, optionnel en dry_run)

En dry-run pur (`EXECUTION_MODE=dry_run`), le bot ne signe aucun ordre live
→ le wrap n'est **PAS** nécessaire. Devient obligatoire au flip live :

```bash
# Install web3 optional dep (~30MB, dry_run users skip)
pip install -e ".[live]"

# Set RPC Polygon
export POLYGON_RPC_URL=https://polygon-mainnet.g.alchemy.com/v2/<KEY>

# Wrap 100 USDC.e → 100 pUSD
python scripts/wrap_usdc_to_pusd.py --amount 100
# Logs : wrap_usdc_to_pusd_completed pusd_balance=100.0 gas_total=...

# Flip live mode
echo "EXECUTION_MODE=live" >> .env
sudo systemctl restart polycopy
```

### Rollback (impossible post-cutover, hotfix uniquement)

- **Pré-cutover (lundi 27 ~22h UTC à mardi 28 ~10h59 UTC)** : possible
  `git revert` + `pip install py-clob-client>=0.20.0` + restart V1.
  Backend Polymarket V1 encore live. Fenêtre ~13h.
- **Post-cutover** : Polymarket V1 backend offline. Pas de retour V1 possible.
  Le rollback se fait par hotfix git sur main uniquement.

### Risques et mitigations

| Risque | Probabilité | Impact | Mitigation |
|---|---|---|---|
| SDK V2 PyPI release retiré entre 27/04 et 28/04 | Faible | Bloquant | Pin version exacte `==1.0.0` ; backup wheel locale `pip download py-clob-client-v2==1.0.0` |
| `clob-v2.polymarket.com` schéma change post-spec writing | Moyen | Tests intégration cassés | Tests intégration opt-in (`-m integration`), pas dans CI critique. |
| `getClobMarketInfo` rate limit plus strict que V1 | Moyen | Fallback fréquent | Cache TTL 60s amortit. Single-flight évite burst. Si 429 répété → augmenter TTL en hotfix. |
| Polymarket re-deploy `CollateralOnramp` post-spec writing | Très faible | Wrap script échoue | Setting `POLYMARKET_COLLATERAL_ONRAMP_ADDRESS` permet override sans redeploy code. |
| Clock drift Macbook prod après suspend nuit | Moyen | Order rejection | `polymarket_use_server_time=True` default — anti drift natif. |
| Cutover Polymarket repoussé (ex: 29 avril) | Faible | Pas d'urgence | Le SDK V2 est dual-version, continue à signer V1 OK. Ship M18 reste safe. |
| `polymarket-apis>=0.5.0` incompatible V2 | Faible | Discovery cassée | Cette dep utilise Gamma API publique (pas CLOB write), inchangée par V2. Surveiller. |

## 13. M16 — Si nouveau feeType apparaît post-rollout

Polymarket prévoit d'étendre les fees à Finance/Politics/Tech/etc.
post-March 30 2026 (cf. spec M16 §11.5). Si tu observes en runtime un
`fee_type` non-mappé dans les logs :

```bash
grep "fee_rate_fetched" ~/.polycopy/logs/polycopy.log | tail -50 | \
  python3 -c "import json, sys; \
seen=set(); \
[seen.add(json.loads(l).get('fee_type', '?')) for l in sys.stdin if 'fee_rate_fetched' in l]; \
print(seen)"
# Affiche les fee_types vus. Attendu : {'crypto_fees_v2', 'sports_fees_v2', None}.
# Si nouveau type (ex: 'politics_fees_v1') → ajouter le mapping dans
# PositionSizer._compute_effective_fee_rate (src/polycopy/strategy/pipeline.py).
```

**Important** : sans mapping explicite, le fallback Crypto formula
s'applique (conservateur, over-estimate fee). Les trades passent quand
même mais peut-être avec sur-estimation. Spec patch facile (1 ligne dans
le `if fee_type == ...`).

---

## Tests flaky pré-existants à surveiller (non-bloquants)

`test_watcher_live_reload.py::test_active_to_sell_only_is_noop` et
`test_ten_cycles_noop_no_info_spam` échouent parfois en suite full
(timing race) mais passent en isolation. Connu, pré-M14, non urgent.

```bash
# Pour vérifier en isolation si un soucis :
pytest tests/unit/test_watcher_live_reload.py -v
```

---

## 15. M19 (Dashboard UX polish) — risques résiduels post-merge 2026-04-27

11 commits MH.1 → MH.11 mergés sur main 2026-04-27 soir. Aucune action
ops requise (UX polish only, pas de migration Alembic, pas de cutover).
Smoke validation post-pull recommandée :

```bash
EXECUTION_MODE=dry_run DASHBOARD_ENABLED=true python -m polycopy --verbose &
sleep 8
for path in home detections strategy orders positions pnl activity traders performance scoring; do
  curl -sf "http://127.0.0.1:8000/$path" > /dev/null && echo "OK $path" || echo "FAIL $path"
done
# Vérifier markers M19 visuellement :
curl -sf http://127.0.0.1:8000/home | grep -E "copy-btn|info-icon|Approve stratégie \(24h\)" | head -3
curl -sf http://127.0.0.1:8000/scoring | grep -E "Stability|intersection v1∩v2"
curl -sf http://127.0.0.1:8000/performance | grep -E "Fee drag \(24h\)"
pkill -f polycopy
```

### Risques résiduels à surveiller

1. **MH.5 fallback YES sur outcome NULL** : positions M3..M14 historiques
   sans `DetectedTrade.outcome` matché → `Gain max latent` calculé via
   formule YES `(1 − avg_price) × size`. Conservateur (cohérent legacy
   M13), mais surévalue le payoff max pour positions BUY NO historiques.
   Mitigation : observer `/home` post-restart prod. Si divergence
   visible vs analyse manuelle, considérer migration Alembic 0011
   (`MyPosition.outcome_side` + backfill SQL) en M20+ (D4 alternative).
2. **MH.5 JOIN runtime perf** : `/home` p50 mesurée +N ms à monitorer
   sur prod 500+ positions. Si dégradation > 50ms, idem migration 0011
   à reconsidérer.
3. **MH.9 ranks locaux** : si pool v1 ou v2 trop petit (< 3 wallets),
   intersection vide → tous rangs locaux `None`, le tableau affiche `—`
   sur les colonnes Rank. Acceptable v1 (Spearman déjà retourne `None`
   dans ce cas), tooltip header documente.
4. **MH.10 wash_risk** : feature flag template strict
   `{% if scoring_version == "v2.2" %}` — colonne absente tant que MF
   non shippé. Si on bump `SCORING_VERSION` à v2.2 sans avoir shippé MF
   (i.e. `wash_score` non peuplé dans `TraderPerformanceRow`), la colonne
   apparaît avec tous `—`. Solution : ne flip `SCORING_VERSION` qu'après
   ship MF (cohérent versioning sacré).
5. **MH.4 tooltips mobile** : `<span title>` natif HTML invisible sur
   tactile (acceptable v1 desktop-first). Future amélioration via
   `details/summary` ou bibliothèque tooltip si demande user.

Aucune intervention ops bloquante. Tous les risques sont monitorables
post-restart sans rollback.

---

## Documenté (référence rapide)

- Spec M14 complète : [specs/M14-scoring-v2.1-robust.md](specs/M14-scoring-v2.1-robust.md)
- Spec M15 complète : [specs/M15-anti-toxic-lifecycle.md](specs/M15-anti-toxic-lifecycle.md)
- Spec M16 complète : [specs/M16-dynamic-fees-ev.md](specs/M16-dynamic-fees-ev.md)
- Spec M17 complète : [specs/M17-cross-layer-integrity.md](specs/M17-cross-layer-integrity.md)
- Spec M18 complète : [specs/M18-polymarket-v2-migration.md](specs/M18-polymarket-v2-migration.md)
- Spec M19 complète : [specs/M19-dashboard-ux-polish.md](specs/M19-dashboard-ux-polish.md)
- Script H-EMP MA : [scripts/validate_ma_hypotheses.py](../scripts/validate_ma_hypotheses.py)
- Brief original MA : [next/MA.md](next/MA.md)
- Brief original MB : [next/MB.md](next/MB.md)
- Brief original MC (fees) : [next/MC.md](next/MC.md)
- Brief original MH (UX polish) : [next/MH.md](next/MH.md)
- Roadmap consolidée : [next/README.md](next/README.md)
- **Prochain module recommandé** : **MK** (M20 — latency phase 1b WSS
  market detection p50 13s → 2-4s). Indépendant, parallélisable. MF
  (Wash + Mitts-Ofir capstone) reste bloqué jusqu'à 30j post-MB.
- **Migration Polymarket V2** : doc officielle
  [docs.polymarket.com/v2-migration](https://docs.polymarket.com/v2-migration).
  Cutover 28 avril 2026 ~11h UTC, ~1h downtime. Procédure complète §14.
