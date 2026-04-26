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

### Phase 1 — Préparation branche V2 (avant cutover)

**À faire avant lundi 27 avril soir** :

```bash
# Crée la branche
git checkout -b feat/ctf-exchange-v2

# Bumper le SDK Python (nom exact du package à confirmer sur PyPI)
pip install py-clob-client-v2==1.0.0
pip uninstall py-clob-client   # remplacement, pas in-place
# Mettre à jour pyproject.toml en conséquence

# Adapter src/polycopy/executor/ (les 4 points clés) :
# 1. ClobWriteClient constructeur — options object, `chain` au lieu de `chainId`.
# 2. Order struct : drop nonce/feeRateBps/taker/expiration, ajout
#    timestamp(ms)/metadata(bytes32)/builder(bytes32). EIP-712 domain "1" → "2".
# 3. FeeRateClient : swap `/fee-rate?token_id=` → `getClobMarketInfo()`.
# 4. Tests intégration via https://clob-v2.polymarket.com (testnet jusqu'au cutover).

# Tests stricts avant merge :
pytest tests/unit/test_clob_write_client.py tests/integration/ -x
ruff check . && mypy src --strict
```

**Décision builder code** : optionnel mais ROI direct. Réclame ton code via
[polymarket.com/settings?tab=builder](https://polymarket.com/settings?tab=builder)
et stocke-le dans une nouvelle env var `POLYMARKET_BUILDER_CODE`. Le SDK V2
le plomb dans chaque `Order.builder` field → fee rebates sur les fills.

### Phase 2 — Cutover (28 avril ~10h30 UTC)

**Séquence stricte ~30 min avant le go-live Polymarket** :

```bash
# 1. Stop le bot V1 (proprement)
sudo systemctl stop polycopy
# OU pkill -f polycopy && sleep 5

# 2. Pull la branche V2 mergée sur main
cd ~/Documents/GitHub/polycopy
git pull origin main

# 3. Update les deps
source .venv/bin/activate
pip install -r requirements.txt   # ou poetry install / uv sync selon ton setup
# Vérifier que py-clob-client-v2 est bien installé :
pip show py-clob-client-v2

# 4. Reset DB (cohérent §3 de ce todo)
cp ~/.polycopy/data/polycopy.db ~/.polycopy/data/polycopy.db.bak.pre-v2
rm ~/.polycopy/data/polycopy.db

# 5. Wrap USDC.e → Polymarket USD via le contrat CollateralOnramp
#    Adresse exacte à vérifier sur docs.polymarket.com/concepts/pusd
#    Action ONE-TIME, à faire APRÈS le cutover Polymarket (~11h UTC)
#    via cast/foundry ou un petit script Python web3.py.
#    Approve USDC.e d'abord, puis appel onramp.wrap(USDC.e, funder, amount).
#    Vérifie ensuite ton solde pUSD avant restart.
#    
#    NOTE : en dry-run pur (EXECUTION_MODE=dry_run), ce wrap n'est PAS
#    nécessaire — le bot ne signe aucun ordre live. Le wrap devient
#    obligatoire au moment où tu flip EXECUTION_MODE=live.

# 6. Update .env (cf. §3.5) — vérifier que TOUS les paramètres recommandés
#    sont en place. Confirmer SCORING_VERSION=v2.1.

# 7. Restart
sudo systemctl start polycopy
# OU python -m polycopy --verbose
```

### Phase 3 — Smoke test post-V2 (28 avril ~11h30 UTC)

```bash
# Vérifie que le bot a bien démarré sur l'API V2
tail -50 ~/.polycopy/logs/polycopy.log | grep -E "ERROR|clob_v2|exchange_v2"

# Le first cycle discovery doit tourner sans erreur signature
grep "discovery_cycle_started" ~/.polycopy/logs/polycopy.log | tail -3

# Surveiller les events fee_rate côté V2 (nouveau client signature)
grep "fee_rate_fetched" ~/.polycopy/logs/polycopy.log | head -5
# Format JSON peut différer vs M16 V1 — les nouveaux events doivent contenir
# au minimum : token_id, fee_rate (Decimal), source="clob_v2"

# Vérifier qu'aucun ordre dry-run n'est rejeté pour signature_invalid
sqlite3 ~/.polycopy/data/polycopy.db \
  "SELECT reason, COUNT(*) FROM strategy_decisions
   WHERE decided_at >= datetime('now', '-1 hour')
   GROUP BY reason;"
```

### Phase 4 — Monitoring 24h post-cutover

- Telegram : surveiller les events `executor_error` ou `executor_auth_fatal`.
- Dashboard `/strategie` : vérifier que les decisions APPROVED se concrétisent
  en `MyOrder` avec le nouveau format.
- Si erreur : `git revert <merge-sha>` + restart V1 IMPOSSIBLE (Polymarket
  V1 est offline post-cutover). Le rollback se fait sur la branche V2 par
  hotfix git seulement.

### Risques et mitigations

| Risque | Mitigation |
|---|---|
| SDK V2 pas release officielle au 28 avril | Vérifier PyPI dimanche soir. Si retard → repousser le restart d'1 jour. |
| Adresse `CollateralOnramp` change post-doc | Vérifier l'adresse 1h avant cutover sur la page doc officielle. **Ne pas hardcoder** dans le code — env var `POLYMARKET_COLLATERAL_ONRAMP_ADDRESS`. |
| `getClobMarketInfo()` schéma diffère du `/fee-rate` V1 | Garder le fallback Decimal(0.018) M16 — protection conservatrice. Test par token actif post-restart. |
| Ordres dry-run rejetés pour `signature_invalid` | Bug dans la branche V2 — investigation immédiate. Le mode dry-run ne signe pas réellement, donc le seul path testé est le builder de la struct. |

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

## Documenté (référence rapide)

- Spec M14 complète : [specs/M14-scoring-v2.1-robust.md](specs/M14-scoring-v2.1-robust.md)
- Spec M15 complète : [specs/M15-anti-toxic-lifecycle.md](specs/M15-anti-toxic-lifecycle.md)
- Spec M16 complète : [specs/M16-dynamic-fees-ev.md](specs/M16-dynamic-fees-ev.md)
- Script H-EMP MA : [scripts/validate_ma_hypotheses.py](../scripts/validate_ma_hypotheses.py)
- Brief original MA : [next/MA.md](next/MA.md)
- Brief original MB : [next/MB.md](next/MB.md)
- Brief original MC (fees) : [next/MC.md](next/MC.md)
- Roadmap consolidée : [next/README.md](next/README.md)
- **Prochain module recommandé** : **MD** (cross-layer integrity patches —
  5 CRITICALs audit 2026-04-24 qui bloquent le passage live). Indépendant
  de MA/MB/MC, parallélisable à la collecte 30j d'`internal_pnl_score`.
  Ship avant le flip `EXECUTION_MODE=live`. Cf. [next/MD.md](next/MD.md).
  MF (Wash + Mitts-Ofir capstone) reste bloqué jusqu'à 30j post-MB.
- **Migration Polymarket V2** : doc officielle
  [docs.polymarket.com/v2-migration](https://docs.polymarket.com/v2-migration).
  Cutover 28 avril 2026 ~11h UTC, ~1h downtime. Procédure complète §14.
