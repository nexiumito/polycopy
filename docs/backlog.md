# Backlog — idées d'amélioration (non planifiées)

Registre d'idées produit/tech notées au fil de l'eau pour ne pas les perdre.
**Rien ici n'est planifié** — à retrier/spec/prioriser plus tard. Pas
d'implémentation tant qu'il n'y a pas eu arbitrage + spec dédiée.

---

## Multi-machine & contrôle à distance via Telegram

**Contexte** : le user peut lancer `polycopy` sur plusieurs PC simultanément
(test sur poste principal + backup/laptop). Les alertes Telegram arrivent
sans distinction de source → impossible de savoir quel bot a déclenché
l'alerte. Et quand le kill switch se déclenche à distance pour une cause
non-algo (ex. drawdown dû aux `TARGET_WALLETS` pinned, pas à la qualité
du scoring), il faut aujourd'hui SSH ou physiquement toucher la machine
pour relancer.

**Idée 1 — Identifiant machine dans les alertes Telegram**
- Ajouter env var `MACHINE_ID` (ou `NODE_NAME`, ex: `desktop-debian`,
  `laptop-ubuntu`) lu par `StartupNotifier`, `AlertDigestWindow`,
  `HeartbeatScheduler`, `DailySummaryScheduler`.
- Injecter dans tous les templates `monitoring/templates/*.md.j2` (badge
  en tête : `🏠 desktop-debian` / `💻 laptop-ubuntu`).
- Fallback `socket.gethostname()` si env var absente.
- **Invariants sécurité M7** : `telegram_md_escape` appliqué à `MACHINE_ID`
  (valeur user-controlled).

**Idée 2 — Relance à distance via commandes Telegram**
- Aujourd'hui la spec M7 §13 ferme explicitement "bot emitter-only,
  aucune commande entrante". Cette décision est à **ré-ouvrir** pour ce
  cas d'usage.
- Modèle : un bot Telegram par machine, chacun avec son token + chat_id
  dédié. Commandes : `/status`, `/restart`, `/stop`, `/resume`
  (après kill switch). Filtrage strict par `chat_id` autorisé (whitelist
  env `TELEGRAM_ALLOWED_CHAT_IDS`).
- Attention à la surface d'attaque : un token Telegram compromis = prise
  de contrôle du bot. Discipline identique `TELEGRAM_BOT_TOKEN` actuelle
  + rotation trimestrielle + commande `/restart` doit exiger
  confirmation 2FA (ex: mot de passe one-time dans le message ou double
  check via 2e chat).
- Alternative plus safe : pas de commandes, mais un endpoint
  HTTP-over-Tailscale (hors dashboard localhost-only) protégé par mTLS
  ou Wireguard. Pas de surface Telegram donc pas de risque token.
- **Ne pas traiter avant** d'avoir clos le test 14j et backtest v2 —
  priorité produit.

**Use-case déclencheur** : kill switch du 2026-04-19 soir, dû aux wallets
pinned de `TARGET_WALLETS` et non à l'algo → relance saine le lendemain
matin aurait pu se faire depuis le téléphone.

---

## Fix Spearman rank sur le dashboard `/traders/scoring`

**Contexte** : la métrique `Spearman rank(v1, v2)` affichée dans
`/traders/scoring` peut sortir bien en dehors de `[-1, 1]` (observé
-3.500 le 2026-04-20 avec 4 wallets comparés).

**Cause** : [queries.py:1030-1042](../src/polycopy/dashboard/queries.py)
applique la formule standard `ρ = 1 - (6·Σd²) / (n·(n²-1))` en mélangeant
deux référentiels de rangs incompatibles :
- `rank_v1` vient du classement **global** sur tous les wallets scorés
  (rangs 1..19 aujourd'hui).
- `rank_v2` est restreint aux wallets ayant v1 ET v2 (4 aujourd'hui,
  rangs 1..4).

La formule Spearman suppose des rangs `1..n` sur **le même ensemble**.
Mélanger rangs globaux et rangs de sous-ensemble casse la borne `[-1, 1]`.

**Fix** : avant d'appeler `_spearman_rank`, re-ranker `rank_v1` et
`rank_v2` sur l'intersection `with_both` uniquement (équivalent
`scipy.stats.rankdata` sur chaque liste). Trivial (3 lignes).

**Tests à ajouter** : cas `n=4` avec ranks globaux (7, 2, 6, 4) vs locaux
(1, 2, 3, 4) → doit produire un ρ ∈ `[-1, 1]`, pas -3.5.

**Priorité** : basse. Tant que la shadow period v2 n'a pas convergé
(peu de wallets avec v1+v2), la métrique n'est pas statistiquement
significative de toute façon — la doc actuelle filtre déjà à `n≥3` mais
devrait remonter à `n≥10` pour avoir du sens.

---

## Latence watcher : métrique `watcher_detected_ms` trompeuse

**Contexte** : sur `/latence` du dashboard (remote 2026-04-23), le stage
`watcher_detected_ms` affiche p50=16 182 ms, p95=32 642 ms, **p99=1 713 411 ms
(28 minutes)**. Les autres stages sont nominaux (`strategy_sized_ms`
2.71 ms p50, `executor_submitted_ms` 635 ms p50).

**Cause racine — la métrique conflate deux latences différentes** :
`watcher_detected_ms = now() - trade.timestamp` où `trade.timestamp` est
le timestamp on-chain renvoyé par la Data API
([wallet_poller.py:94-98](../src/polycopy/watcher/wallet_poller.py#L94)).
Quand un wallet fraîchement promu a un gros backlog historique
(> 2900 trades), la pagination time-cursor
([data_api_client.py:30-96](../src/polycopy/watcher/data_api_client.py#L30))
fait jusqu'à 50 cursor resets séquentiels pour tout rattraper — chaque
reset = 1 HTTP call supplémentaire. Au moment où le trade le plus ancien
du backlog atteint la DB, sa "détection latence" apparente = `now - timestamp_ancien`
= des heures ou jours. Le p99 à 28 min = très probablement **un wallet
promu récemment dont on aspire l'historique**, pas une vraie latence
opérationnelle.

**Pistes de fix** (à trancher après le test 14 jours) :
1. **Refacto métrique (recommandé)** — séparer 2 stages distincts :
   - `watcher_realtime_detected_ms` = `now() - trade.timestamp` **uniquement
     pour les trades < 5 min** (zone temps-réel). Filtre en amont de
     `latency_repo.insert()`. C'est la seule valeur pertinente pour
     évaluer la compétitivité du bot.
   - `watcher_backfill_duration_ms` = durée totale du cycle `get_trades()`
     lors d'une promotion (mesurée autour du call `_poll_once`). Audit
     pur, pas comparé aux autres stages.
2. **Quick mitigation** : baisser `_MAX_CURSOR_RESETS` de 50 à 10 dans
   [data_api_client.py:30](../src/polycopy/watcher/data_api_client.py#L30). Risque : on loupe des
   trades historiques sur les gros wallets (seuil 10 resets × 2900 trades
   = 29 000 trades max capturés au boot). Acceptable si on préfère
   privilégier le temps-réel.
3. **Non-fix** : accepter que le p50 réel ~16 s est en grande partie
   "vraie" latence (poll interval 5 s + propagation Data API 1-5 s +
   traitement). Dans ce cas passer `POLL_INTERVAL_SECONDS=2` dans `.env`
   mais attention au rate-limit Data API (~100 req/min — avec 7 pollers
   actifs à 2 s = 210 req/min, on frôle).

**Use-case déclencheur** : test 14 jours en cours (démarré 2026-04-22).
Le `position_already_open` domine les rejets strategy (cf. /strategy),
en partie dû à cette latence (le temps qu'on traite, le source wallet
a déjà enchaîné le trade suivant). À revisiter fin 2026-05-06 avec les
métriques du test pour prioriser fix #1 vs fix #3.

**Priorité** : moyenne — ne bloque pas le test dry-run en cours, mais
obligatoire avant passage live.

---

## Réduction de latence bout-en-bout (watcher → executor)

**Contexte** : la latence actuelle source-trade → notre POST CLOB
plafonne à 8-20 s, dominée par l'indexation Data API Polymarket (5-15 s
incompressible côté client). C'est acceptable pour le copy-trading de
stratégies à horizon minutes/heures (RN1 style — sell favorites,
over/under) où le prix ne bouge pas dans la fenêtre. C'est *bloquant*
pour :
- Les stratégies d'arbitrage sub-minute (sportsbook ↔ Polymarket).
- Les wallets massivement copiés (>100k views) où la compétition d'ordre
  d'arrivée sur le carnet compresse le edge en quelques secondes.

**Pistes (par ordre de ROI décroissant)** :

1. **Goldsky subgraph `pnl-subgraph`** en remplacement du polling Data
   API HTTP — gain estimé **~5-10 s**. Déjà partiellement supporté via
   `DISCOVERY_BACKEND=goldsky|hybrid` (M5) ; à étendre au watcher
   principal. Risque : versions subgraph qui drift (hardcoder un
   fallback URL + override env, pattern déjà en place).

2. **Polygon logs directs** — `WebSocket eth_subscribe` sur les events
   `OrderFilled` du CTF contract Polymarket. Détection à la confirmation
   de bloc (~2 s), bypass total de l'indexation Data API. Gain estimé
   **~10 s**. Coût d'implémentation non-trivial : nouveau module
   `watcher/polygon_logs_client.py` + gestion des reorgs + decoding
   ABI. Dépendance : un RPC WebSocket stable (public free RPC = pas
   fiable, besoin d'un provider).

3. **RPC Polygon premium** (Alchemy / QuickNode / Infura) — quand on
   aura #2 en place, passer du RPC public (latence 200-500 ms) à un
   plan payant (~30-50 ms). Gain marginal **~200 ms**, mais nécessaire
   pour que #2 soit fiable (le public RPC coupe les WebSocket
   régulièrement).

4. **Deploy géographique** — passer du WSL/laptop local à un VPS AWS
   us-east-1 (proche des validators Polygon et du DC de Polymarket).
   Gain estimé **-50 à -100 ms** sur tous les round-trips. ROI faible
   vs coût d'ops (supervisor systemd remote, réplication DB, etc.).

**Ordre d'implémentation suggéré** : #1 en priorité (retour faible
effort vs gain substantiel, code déjà en place pour discovery). #2 si
#1 ne suffit pas pour débloquer les copy-trades rapides. #3 et #4
uniquement si #2 est adopté.

**Budget latence cible** (si tout fait) : 8-20 s → **2-5 s**. Rapproche
polycopy de la classe bot "niveau semi-pro", en-dessous du HFT
institutionnel (<1 s) mais largement compétitif sur les stratégies à
horizon trade-to-trade > 30 s.

**Ne pas traiter avant** d'avoir les résultats du test 14 j M13 —
priorité produit. Si le test montre +10 % net réalisé via copy-trading
sans optimisation latence, la latence n'est pas le bottleneck. Si le
test montre du slippage marqué sur les wallets très copiés, #1 devient
prioritaire.

**Use-case déclencheur** : analyse 2026-04-23 de wallets candidats
`swisstony` / `RN1` / `0xD9E0` — tweet viral qui affiche 461-675k views
sur ces wallets → compétition de copy-trading documentée.

**Priorité** : moyenne-basse — dépendante des résultats du test M13.

---
