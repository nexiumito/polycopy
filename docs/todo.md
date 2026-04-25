# Todo machine prod (post-M14 + M16)

Actions à effectuer sur le PC qui fait tourner le bot après le merge
M14 (scoring v2.1) + M16 (dynamic taker fees + EV adjustment).
Ordre recommandé. Garde ce fichier à jour ou raye au fur et à mesure.

---

## 1. Pull main (immédiat, ~10s)

```bash
ssh <prod-machine>
cd ~/Documents/GitHub/polycopy   # ou le path de ton install
git pull origin main
```

Les 8 commits MA.x + script H-EMP + spec M14 doivent être visibles
**plus** les 7 commits M16 (spec + 5 MC.x + CLAUDE.md) dans
`git log --oneline -20`.

## 2. Vérifier que rien ne casse au boot avec la DB existante (immédiat)

**Important** : `Settings.scoring_version: Literal["v1", "v2.1"]`. Si ton
`.env` contient `SCORING_VERSION=v2` (M12), le boot va **crasher** avec
`ValidationError: scoring_version`.

```bash
grep "SCORING_VERSION" .env
# Si ça affiche SCORING_VERSION=v2 → soit retire la ligne (default v1),
# soit mets SCORING_VERSION=v1 explicitement.
```

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

## 3. Reset DB (recommandé avant cutover v2.1, optionnel pour rester en v1 shadow)

**Quand le faire** : avant de flip `SCORING_VERSION=v2.1`. Pas obligatoire
si tu restes en `v1` (default) — la DB existante continue à fonctionner.

**Pourquoi** : la DB actuelle contient ~39 rows `trader_scores.scoring_version="v2"`
de la M12 obsolète. Inutile (v2 n'est plus dans le registry) mais inoffensif —
elles polluent juste les queries dashboard si tu filtres par version.

```bash
# 1. Arrête le bot.
sudo systemctl stop polycopy   # OU pkill -f polycopy

# 2. Backup au cas où (optionnel, recommandé) :
cp ~/.polycopy/data/polycopy.db ~/.polycopy/data/polycopy.db.bak.$(date +%Y%m%d)

# 3. Wipe.
rm ~/.polycopy/data/polycopy.db

# 4. Restart — Alembic upgrade head crée la DB fraîche.
sudo systemctl start polycopy   # OU python -m polycopy --verbose
```

**Conséquence** : tu perds toutes les données dry-run actuelles (positions
virtuelles, snapshots PnL, trader_scores historiques). Si tu veux les
analyser plus tard, exporte avant le reset :

```bash
sqlite3 ~/.polycopy/data/polycopy.db ".dump" > ~/.polycopy/backup_pre_m14.sql
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

## 7. Recalibrer EVICTION_SCORE_MARGIN après ~7j post-ship (post cutover)

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

## 8. (Plus tard) Activer eviction si pas encore fait

`EVICTION_ENABLED=false` par défaut. Pour bénéficier du M5_bis +
recalibration MA.7, il faut explicitement activer :

```bash
EVICTION_ENABLED=true
EVICTION_SCORE_MARGIN=0.10  # ou la valeur recalibrée à l'étape 7
EVICTION_HYSTERESIS_CYCLES=3
```

Attention : eviction implique cascade `active → sell_only`. Lis la spec
[M5_bis](specs/M5_bis_competitive_eviction_spec.md) avant si tu veux
comprendre la mécanique.

## 9. M16 — Surveiller l'impact fees sur 7-14 jours (post-ship)

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

## 10. M16 — Si nouveau feeType apparaît post-rollout

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
- Spec M16 complète : [specs/M16-dynamic-fees-ev.md](specs/M16-dynamic-fees-ev.md)
- Script H-EMP MA : [scripts/validate_ma_hypotheses.py](../scripts/validate_ma_hypotheses.py)
- Brief original MA : [next/MA.md](next/MA.md)
- Brief original MC (fees) : [next/MC.md](next/MC.md)
- Roadmap consolidée : [next/README.md](next/README.md)
- **Prochain module recommandé** : MB (anti-toxic lifecycle + internal PnL) —
  démarrer immédiatement après reset DB pour amorcer la collecte 30j
  d'`internal_pnl_score` requise par MF capstone.
