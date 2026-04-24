# Session A — Anti-toxic trader lifecycle

**Priorité** : 🔥 #1 (stoppe la fuite capital immédiate)
**Charge estimée** : L (2-3 jours, ~1 spec dédiée + implémentation)
**Branche suggérée** : `feat/anti-toxic-lifecycle`

---

## Objectif business

Aujourd'hui un trader qui nous fait perdre de l'argent **peut rester ACTIVE
indéfiniment** parce que :

1. `DecisionEngine._decide_active` ([decision_engine.py:290](../../src/polycopy/discovery/decision_engine.py#L290)) compare uniquement au `SCORING_DEMOTION_THRESHOLD` statique — tant que le score M5 (basé sur 90 j d'historique **externe** Data API) reste > threshold, pas de demote.
2. `CascadePlanner` ([cascade_planner.py:119](../../src/polycopy/discovery/eviction/cascade_planner.py#L119)) demande `score_shadow > score_worst_active + 0.15` sur 3 cycles consécutifs — mathématiquement irréalisable quand les shadows culminent à 0.79 et un active est à 0.66 (delta max possible = 0.13).
3. **Aucun signal de performance interne** (realized_pnl observé dans notre DB) ne remonte au scoring.

**Cas concret 2026-04-24** : `0x21ffd2b7…0d71` avec 19 % win rate, −$0.55 PnL
cumulé sur 56 positions fermées, score 0.66, **encore actif** au moment de
l'audit. Le user a dû le blacklister manuellement via `.env` + restart.

## Items

### A1 — Gate de performance interne dans scoring

Nouveau facteur `internal_performance` dérivé de `my_positions` observé :
- `observed_win_rate` (wins / (wins + losses)) sur positions closed par le bot
- `observed_cumulative_pnl` (`SUM(realized_pnl)` filtré `simulated=True` en dry-run, `False` en live)
- `observed_position_count` (N positions closed)

Injection dans la formule v1 ET v2 derrière un flag `SCORING_INTERNAL_FACTOR_ENABLED`
(défaut `true` post-merge). Pondération à trancher dans la spec — suggestion de départ :

- v1 : 0.25 (consistency) + 0.25 (roi) + 0.15 (diversity) + 0.15 (volume) + **0.20 (internal)**
- v2 : peut s'intégrer dans le factor `discipline` existant (déjà slot 0.10)

Ne s'active que si `observed_position_count >= SCORING_INTERNAL_MIN_POSITIONS` (défaut 20)
— sinon neutre 0.5 (cold start).

**Pointeurs** :
- [src/polycopy/discovery/metrics_collector.py](../../src/polycopy/discovery/metrics_collector.py) pour le collecteur M5
- [src/polycopy/discovery/scoring/v1.py](../../src/polycopy/discovery/scoring/v1.py) pour la formule v1
- [src/polycopy/discovery/scoring/v2/factors/discipline.py](../../src/polycopy/discovery/scoring/v2/factors/) pour intégration v2

### A2 — Ranking-based activation (remplace threshold statique)

Aujourd'hui : `if score >= scoring_demotion_threshold: keep`.
Proposition : maintenir **exactement `MAX_ACTIVE_TRADERS` wallets actifs**, choisis
par rang. Le reste est `shadow` ou `sell_only`.

Changements :
- `DecisionEngine._decide_active` : un active qui sort du top-N par score bascule
  automatiquement en shadow (ou `sell_only` via EvictionScheduler si cap atteint).
- Supprimer/déprécier `SCORING_DEMOTION_THRESHOLD` (ou le garder comme garde-fou
  "si score < 0.3 force demote même si dans le top").

### A3 — EvictionScheduler margin configurable + dynamique

Aujourd'hui `EVICTION_SCORE_MARGIN = 0.15` hardcoded. Proposition :
- Exposer `EVICTION_SCORE_MARGIN` en env (déjà le cas ?), défaut ramené à `0.05`.
- Optionnel : margin **dynamique** = `max(0.05, 0.5 × std(active_scores))`. Sur un pool
  dense (écart-type faible), le margin diminue → plus de rotation. Sur un pool
  polarisé, le margin reste exigeant.

### A4 — Auto-blacklist sur seuil PnL cumulé observé

Nouveau garde-fou indépendant du scoring : si un trader ACTIVE atteint
`observed_cumulative_pnl < AUTO_BLACKLIST_PNL_THRESHOLD_USD` (défaut −$5
sur capital $1000) OU `observed_win_rate < 0.25 AND observed_position_count >= 30`
→ bascule automatique en `blacklisted` + alerte Telegram CRITICAL.

Garde-fou de dernier recours, indépendant de la formule de scoring. Implémenté
dans un nouveau `ToxicTraderWatcher` (scheduler 1h) co-lancé par
`DiscoveryOrchestrator`, OU directement dans le `DecisionEngine` au moment de
`_decide_active`.

### A5 — Alertes Telegram sur auto-blacklist

Nouveau template `auto_blacklisted_toxic_trader.md.j2` :
- Wallet concerné
- Raison (PnL < seuil OU win rate < seuil)
- Stats observées (N closed, win rate, cumulative PnL)
- Lien dashboard

### A6 — `sell_without_position` visibilité /stratégie

Le reason code `sell_without_position` (M13 Bug 5) apparaît dans
`strategy_decisions` mais sans distinction UI du `liquidity_too_low`. Ajouter un
compteur dédié sur /stratégie (ou /détection) pour tracer combien de SELL
orphelins on saute (signal de wallet fraîchement promu).

## Hypothèses à valider

- **H1** : le seuil PnL cumulé $−5 sur capital $1000 (−0.5 %) est-il assez strict ?
  À discuter. Si on vise $−$1 net, beaucoup de petites pertes normales déclenchent.
- **H2** : ranking-based activation peut créer du flip-flop en fin de top-N si
  deux wallets sont à ±0.01 — d'où l'hystérésis `SCORING_DEMOTION_HYSTERESIS_CYCLES`
  existant à conserver.
- **H3** : si `EVICTION_SCORE_MARGIN=0.05`, est-ce qu'on a un effet ping-pong
  shadow↔active↔sell_only ? À tester en simulation avant merge.

## Livrables

- Spec dédiée `docs/specs/M14_anti_toxic_lifecycle_spec.md` (reprise du format M13)
- Migration DB si besoin (probablement pas — tout se calcule depuis `my_positions`)
- ~12-15 tests unitaires nouveaux (ranking logic, auto-blacklist trigger, internal factor)
- ~5 tests d'intégration scheduler E2E
- Mise à jour CLAUDE.md §Conventions + §Sécurité sur le nouveau factor
- Update `.env.example` avec les nouvelles vars (`AUTO_BLACKLIST_PNL_THRESHOLD_USD`, etc.)

## Out of scope

- Pas de retrain du scoring v2 (session B).
- Pas de refacto du dashboard /performance (affichage des stats internes — peut
  venir dans session C).
- Pas d'intégration des signaux "wash cluster" externes (trop ambitieux ici).

## Success criteria

1. Un trader simulé avec −$5 de PnL cumulé doit être automatiquement blacklisté
   en ≤ 1 h après le déclenchement du seuil (validable par test d'intégration).
2. Sur le test 14 j actuel, aucun wallet avec win rate observé < 25 % sur 30+
   positions ne doit être ACTIVE à J+1 post-merge.
3. Zéro faux-positif : le top trader `0xe8dd…ec86` (52 % WR, PnL +$0.01) ne doit
   PAS être auto-blacklisté.
