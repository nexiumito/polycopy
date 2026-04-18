# Polymarket smart money scoring, log hygiene, dry-run parity et latence – notes de design

## 1. Scoring v2 – sources externes et facteurs

### 1.1. Paysage d’outils existants (Kreo, PolyHub, etc.)

Plusieurs outils positionnés sur le « smart money » Polymarket illustrent déjà un set de métriques assez convergent : PnL absolu, ROI, win rate, fréquence de trade et éventuellement un score de timing ou de spécialisation.
Kreo / KreoPoly se présente comme un bot de copy-trading Telegram qui suit les « top wallets » Polymarket et Kalshi, avec un ranking fondé sur win rate, ROI, fréquence de trade et sélection de marchés, mis à jour en continu pour refléter la performance récente plutôt que de longs historiques lissés.[^1]
PolyHub (Hubble) et d’autres outils similaires filtrent les wallets par PnL, ROI, fréquence de trading sur une fenêtre récente (ex. 30 jours) et parfois un score de timing, l’objectif étant de distinguer les arbitrageurs à très haute fréquence des parieurs « informationnels » plus rares mais plus alpha.[^2][^3]
Plusieurs guides « smart money copy trading » insistent sur la combinaison win rate + PnL + profil de risque, en excluant d’abord les wallets avec grosses pertes extrêmes ou drawdowns massifs, même s’ils ont un PnL net positif.[^4][^5]

La doc officielle Polymarket expose un leaderboard API orienté PnL et volume (orderBy = PNL ou VOL, timePeriod = DAY/WEEK/MONTH/ALL, category = OVERALL/SPORTS/CRYPTO/etc.), qui donne une base de scoring brut mais sans ajustement du risque ni contrôle de régularité.[^6][^7]

### 1.2. Contributions de la littérature et d’analyses sur l’« informed trading »

Une étude académique récente sur Polymarket construit un score composite d’« informed trading » au niveau (wallet, market) à partir de cinq signaux : taille de mise relative (cross-sectional bet size), taille par rapport à l’historique du wallet (within-trader bet size), profitabilité, timing pré‑événement et concentration directionnelle (position très directionnelle sur un seul côté).[^8][^9]
Ce score s’applique à des paires wallet‑marché, pas aux wallets globalement, mais les facteurs sont transposables à un score par wallet via agrégation (fraction de trades « informed-like », intensité moyenne du signal, etc.).[^10][^8]
Les auteurs montrent que les paires wallet‑marché les plus « suspectes » (selon ce score) ont un win rate ≈ 70%, largement au‑dessus du hasard, ce qui valide empiriquement que la combinaison de taille, timing et concentration directionnelle capture bien une forme d’alpha informationnel.[^8][^10]

Des articles orientés « smart money filtering » sur Polymarket et d’autres dérivés proposent aussi des règles de screening semi‑empiriques :
- PnL net positif significatif sur une fenêtre récente (ex. 30–90 jours) et sur l’historique global.[^11][^2]
- Win rate e 55–60%, mais en contrôlant la taille moyenne de trade pour éviter les stratégies de « grinding » à faible mise et haute fréquence.[^5][^12]
- ROI raisonnable par rapport au risque pris, en évitant les profils « all‑in occasional » qui affichent un ROI énorme mais avec très peu de trades ou un historique de pertes massives.[^13][^4]
- Spécialisation sectorielle : certains wallets très profitables ne tradent que des segments spécifiques (politique, crypto macro, sports), ce qui est corrélé à des stratégies plus informationnelles.[^14][^13]
- Timing alpha : prise de position significative avant le gros mouvement de prix ou avant la diffusion de la news dans les médias généralistes.[^11][^8]

### 1.3. Facteurs candidats pour un scoring v2

À partir des sources ci‑dessus, on peut extraire un set de facteurs susceptibles d’améliorer le scoring v1.
L’idée est de garder le côté interprétable/ingénieur‑friendly, mais en s’alignant davantage sur les métriques de risk‑adjusted return (Sharpe/Sortino/Calmar) et les signaux d’« informed trading ».

Facteurs principaux candidats :

1. **Profitabilité globale et récente**
   - PnL total (lifetime) et PnL sur fenêtre glissante (ex. 30/90 jours).
   - ROI global et sur fenêtre récente, idéalement event‑weighted plutôt que notional simple pour corriger certains artefacts Polymarket.[^12][^4]

2. **Win rate ajusté et profil de risque**
   - Win rate sur trades, éventuellement pondéré par taille (trade‑weighted win rate).
   - Maximum drawdown sur l’historique du wallet ou sur une série de PnL par période (ex. par semaine), pour capter les profils « kamikaze ».[^15][^12]
   - Volatilité du PnL ou variance des returns par trade ou par période.

3. **Ratios de performance ajustés du risque**
   - Sharpe ratio du wallet sur une série agrégée (ex. returns journaliers/hebdomadaires ou par event), mesurant le retour moyen par unité de volatilité.[^16][^15]
   - Sortino ratio (ou Calmar) pour mieux pénaliser les drawdowns et la volatilité downside, ce qui est pertinent pour des stratégies avec payoff très asymétrique.[^17][^15]
   - Ces ratios doivent être calculés sur une période suffisamment longue et avec prudence (non‑normalité des returns, clustering d’événements), mais restent des indicateurs standard bien compris par les quants.[^17][^15]

4. **Consistance temporelle**
   - Fraction de périodes (ex. semaines ou mois) avec PnL positif.
   - Stabilité du Sharpe/Sortino dans le temps (rolling metrics) pour éviter les winners éphémères.
   - Décroissance exponentielle des contributions anciennes (plus de poids au récent, comme KreoPoly et PolyHub).[^3][^1]

5. **Spécialisation et style**
   - Entropie de la distribution de volume par catégorie (politics, sports, crypto, etc.) pour identifier les spécialistes vs généralistes.
   - Performance relative par catégorie : certains wallets peuvent être premium en politique mais médiocres en sports.
   - Volume médian par trade et nombre de trades pour distinguer arbitrageurs HFT (énorme nombre de petits trades, PnL stable) des parieurs info (moins de trades, plus grossiers et concentrés).[^2][^13]

6. **Timing alpha / pré‑news**
   - Mesure de la fraction de PnL générée sur des trades ouverts avant de grands mouvements de prix ou avant des timestamps associés à des annonces publiques (news, décisions, résultats électoraux).
   - Reprise explicite des signaux « pre‑event timing » du papier académique (gap entre entrée et timestamp clé).[^8][^11]

7. **Concentration et conviction**
   - Concentration du capital sur quelques markets (Herfindahl sur valeur de position par event) vs dispersion excessive.
   - Ratio entre la taille des grosses positions et la médiane des trades du wallet.
   - Directional concentration au niveau des markets (ex. longs très asymétriques sur un outcome) comme dans le score d’« informed trading ».[^9][^8]

8. **Robustesse et filtrage de « mauvais profils »**
   - Filtres d’exclusion : énormes pertes single‑trade, comportement de type loterie, ou drawdown extrême, même si PnL net reste positif.[^4][^5]
   - Éventuels signaux de manipulation/insider borderline (split/merge suspects, wash trading, etc.) d’après les guides d’insider detection – soit exclure, soit flaguer séparément.[^18]

### 1.4. Esquisse de formules de scoring v2

Une direction plausible est de construire un score composé de trois blocs : performance ajustée du risque, consistance/style, et signal d’« informed trading ».

On peut par exemple envisager un score v2 de la forme (à raffiner) :

\[
S_{v2}(w) = w_1 \cdot S_{perf}(w) + w_2 \cdot S_{consistency}(w) + w_3 \cdot S_{informed}(w),
\]

avec :

- \(S_{perf}\) : combinaison normalisée de Sharpe, Sortino, ROI et max drawdown (par exemple Sharpe/Sortino wins, ROI wins et faible drawdown).[^16][^15]
- \(S_{consistency}\) : fraction de périodes gagnantes, stabilité des métriques, absence de huge spikes isolés.[^12]
- \(S_{informed}\) : projection simplifiée du score du papier Polymarket (taille relative, timing pré‑news, directional concentration) agrégée à l’échelle du wallet.[^10][^8]

Approches plus concrètes pour un premier « candidate formula » :

1. **Bloc performance** (score entre 0 et 1) :
   - Normaliser Sharpe et Sortino sur un intervalle raisonnable (ex. 0 à 4) puis clipper.
   - Pénaliser fortement les wallets avec max drawdown e 60–70% ou avec un VaR extrême.
   - Ajouter un terme ROI_{lifetime} et ROI_{recent} avec plus de poids au récent.

2. **Bloc consistance** :
   - \(S_{consistency} = 0.5 \cdot f(\text{win rate}) + 0.5 \cdot f(\text{fraction de périodes positives})\), où f est une fonction lisse type logistique.

3. **Bloc informed** :
   - Pour chaque trade, calculer un score de type \(s_i = a \cdot size\_z + b \cdot timing\_z + c \cdot concentration\_z\) (z‑scores), puis agréger au niveau wallet via moyenne pondérée par la taille.
   - Normaliser ce score sur  pour en faire \(S_{informed}\).

Un candidat concret de formule v2 inspiré de ton v1 pourrait ressembler à :

\[
S_{v2} = 0.30 \cdot S_{SharpeSortino} + 0.20 \cdot S_{ROI} + 0.20 \cdot S_{consistency} + 0.15 \cdot S_{specialization} + 0.15 \cdot S_{informed},
\]

avec chaque sous‑score \(S_*\) obtenu par normalisation robuste (percentiles) et winsorisation pour limiter l’impact des outliers.

Ce design est compatible avec tes signaux existants (win rate, ROI, diversité, volume) mais les encapsule dans un cadre plus « quant » et inspiré de la littérature Polymarket.


## 2. Log hygiene – filtrer le bruit StructlogAccessMiddleware

### 2.1. Problème actuel

Le middleware de logging du dashboard écrit un event `dashboard_request` pour chaque requête HTTP, y compris :
- `/api/health-external` (polling récurrent via HTMX depuis le footer).
- `/partials/*` polled toutes les quelques secondes pour rafraîchir des KPIs et listes.
- `/partials/logs-tail` polled toutes les 2 s pour le live tail.

Cela produit un bruit massif dans les logs applicatifs et l’onglet /logs (M9), qui masque les événements métier importants comme `trade_detected`, `order_*`, alertes de drawdown, etc.

Le besoin :
- Conserver une traçabilité suffisante pour diagnostiquer les erreurs HTTP (statut 5xx, temps de réponse extrêmes).
- Ne plus polluer le log file ni l’onglet dashboard avec des centaines de requêtes « santé » ou « polling ».

### 2.2. Analyse des options (a–e)

#### (a) Skip total (ne plus logger les requêtes)

Avantages :
- Suppression immédiate du bruit.
- Aucun coût CPU/IO lié à ces logs.

Inconvénients :
- Plus de visibilité sur les erreurs HTTP côté dashboard.
- Plus de corrélation simple entre logs métier et requêtes utilisateur.
- Risque de perdre des signaux utiles en prod (ex. 500 sur `/logs/download`).

Conclusion : solution trop radicale, acceptable seulement si on a d’autres observability channels (APM, reverse proxy logs, Sentry) couvrant ces besoins.

#### (b) Downgrade en DEBUG

Avantages :
- En configuration prod standard (LOG_LEVEL=INFO), le fichier de logs ne contient plus ces events.
- Conservation possible en dev/staging en mettant LOG_LEVEL=DEBUG.

Inconvénients :
- Même problème que (a) pour la prod : un crash 500 ne génère plus de log `dashboard_request` visible.
- Le /logs dashboard ne verra plus ces requêtes, sauf à changer le niveau de log dynamiquement.

Conclusion : améliore le bruit dans les logs, mais perd de la valeur de diagnostic en prod.

#### (c) Filtre par path (whitelist/blacklist)

Idée : le middleware garde son niveau INFO, mais ne log que :
- Les requêtes sur un ensemble de paths d’intérêt (ex. `/logs/download`, `/api/version` à la demande, endpoints d’actions user critiques).
- Toutes les requêtes avec status ≥ 400 (ou ≥ 500), indépendamment du path.

Les paths de polling récurrents (`/api/health-external`, `/partials/*`, `/partials/logs-tail`, `/api/version` en heartbeat) sont soit totalement ignorés, soit down‑gradés en DEBUG.

Avantages :
- On conserve la visibilité sur les erreurs HTTP.
- Le volume de logs INFO baisse drastiquement.
- Logique alignée avec les bonnes pratiques d’« access logging with sampling/filters ».

Inconvénients :
- Complexité de configuration (liste de paths, éventuellement patterns regex).
- Risque de louper un endpoint nouvellement ajouté qui devrait être whitelisté.

Conclusion : très bon compromis si bien paramétré, surtout si on a déjà une notion de « paths sensibles » à surveiller.

#### (d) Filtre côté lecteur (log_reader)

Idée : laisser le middleware logger tout, mais dans la route /logs M9 :
- Exclure par défaut les events `dashboard_request`.
- Les afficher uniquement si l’utilisateur filtre explicitement sur `dashboard_request` ou change une case à cocher « Include HTTP access logs ».

Avantages :
- Zéro modification sur les logs écrits, donc aucun risque pour d’autres outils qui consomment ce log.
- Expérience utilisateur du dashboard /logs grandement améliorée par défaut.

Inconvénients :
- Le fichier de log sous‑jacent reste très volumineux, ce qui peut poser problème (disque, rotation, perf).
- Pour investiguer une erreur, il faut penser à réinclure ces logs côté UI.

Conclusion : simple à implémenter et safe, mais ne résout pas le bruit au niveau storage.

#### (e) Agrégation de requêtes

Idée : ne pas logger chaque requête individuellement, mais produire des summaries du type : « 234 `dashboard_request` /partials/kpis dans les 5 dernières minutes ».

Avantages :
- Volume de log réduit.
- Informations encore disponibles pour diagnostiquer une explosion de trafic ou un polling anormal.

Inconvénients :
- Mise en œuvre plus complexe (stateful compteur en mémoire, flush périodique, risque de perte en cas de crash).
- Moins utile pour debugger une requête spécifique (on ne voit plus le détail par timestamp/id).

Conclusion : intéressant mais probablement overkill pour un dashboard interne; le coût de complexité n’est pas évident à justifier.

### 2.3. Recommandation et plan de migration

Recommandation principale : combiner (c) et (d).

- **Côté middleware** :
  - Conserver le log `dashboard_request` en INFO uniquement pour :
    - Paths explicitement whitelistes (ex. endpoints critiques, téléchargement de logs, actions ayant un effet côté trading).
    - Toute requête avec statut HTTP ≥ 400.
  - Ne rien logger (ou logger en DEBUG) pour les paths de polling récurrents (`/api/health-external`, `/partials/*`, `/partials/logs-tail`, heartbeat `/api/version`).

- **Côté lecteur /logs** :
  - Exclure les events `dashboard_request` par défaut de l’affichage.
  - Ajouter une option UI pour les réinclure (filtre par event type ou checkbox).

Plan de migration :
1. Introduire une configuration centralisée pour la whitelist/blacklist de paths dans `StructlogAccessMiddleware`.
2. Adapter le middleware pour :
   - Calculer le status code de la réponse.
   - Appliquer la politique (log INFO si whitelisted ou status ≥ 400, sinon DEBUG ou rien).
3. Mettre à jour `log_reader.py` et l’onglet /logs pour :
   - Exclure par défaut `dashboard_request`.
   - Gérer un filtre `event_type` incluant `dashboard_request` si demandé.
4. Vérifier et adapter les tests M9 :
   - `test_dashboard_logs_route` et autres tests qui s’attendent à voir des `dashboard_request` devront être mis à jour pour :
     - Soit consommer uniquement des events métier (`trade_detected`, etc.).
     - Soit activer explicitement la visibilité des `dashboard_request` dans le test (via le paramètre de filtre).
5. En prod, monitorer le volume de log et la capacité à diagnostiquer les erreurs HTTP sur quelques releases; ajuster la whitelist si nécessaire.


## 3. Dry-run / live parity – changement d’invariant

### 3.1. Invariant actuel et raisons probables

Selon la doc existante (`CLAUDE.md` et spécifications M4/M8), deux règles principales :
- « Kill switch JAMAIS en dry-run ».
- `dry_run_virtual_drawdown` signalé seulement en INFO (pas de CRITICAL ni d’arrêt du bot en dry-run).

Motivations probables à l’époque :
- Sécurité capital : s’assurer que des tests en dry-run ne bloquent jamais un bot live par erreur (confusion de config, env mixing, etc.).
- Réduction du bruit : éviter que des dry-runs expérimentaux floodent les alertes Telegram critiques et soient confondus avec des incidents live.
- Simplicité mentale : considérer le dry-run comme un sandbox « sans conséquences », donc tolérer des écarts de comportement.

Avec un dry-run M8 plus réaliste (simulation d’ordres, PnL virtuel, drawdown), cet invariant devient problématique :
- Toute logique de risk management (kill switch, drawdown, taille dynamique) n’est plus testée de bout en bout.
- On ne peut pas valider que les seuils, alertes et réponses automatiques (stop_event) déclenchent correctement.
- L’écart de comportement entre dry-run et live augmente le risque de régression silencieuse : un changement sur le kill switch peut fonctionner en dry-run mais casser en live ou l’inverse.

### 3.2. Objectif cible : parité de comportement

Objectif : le dry-run reproduit fidèlement le comportement live, y compris :
- Application du `KILL_SWITCH_DRAWDOWN_PCT` et autres limites de risque.
- Génération d’alertes avec la même sévérité (CRITICAL/ERROR/WARN/INFO) sur les mêmes conditions.
- Emission d’un `stop_event` (ou équivalent) qui arrête la stratégie dry-run comme en live, à ceci près que les ordres sont simulés.

En pratique, on veut que :
- Pour une même séquence de signaux de marché et une même config, le bot dry-run prenne les mêmes décisions que le bot live, sauf pour l’exécution on‑chain et les side effects extérieurs (vrai PnL, vrai coût).
- Les tests de non‑régression puissent s’exécuter en dry-run en couvrant la logique kill switch, sans toucher le capital réel.

### 3.3. Inventaire des divergences actuelles dry-run vs live (à lister précisément dans le code)

Liste à construire (non exhaustive a priori) :
- Kill switch :
  - En live : drawdown e= `KILL_SWITCH_DRAWDOWN_PCT` déclenche un CRITICAL, un stop_event global, potentiellement un message Telegram urgent.
  - En dry-run : seulement un log INFO/ WARNING, pas de stop_event, possiblement pas de Telegram ou dans un canal différent.

- Alertes de taille / comportement d’ordre (ex. `order_filled_large`, `order_failed`, `order_stuck`) :
  - En live : niveau WARN/ERROR/CRITICAL.
  - En dry-run : downgradé en INFO, voire ignoré.

- PnL et drawdown :
  - En live : calculé à partir des fills réels, fees, etc.
  - En dry-run : basé sur un moteur de simulation qui peut être plus grossier; certains coins peuvent être approximés (slippage, partial fills).
  - La sévérité des logs d’anomalie de PnL peut être atténuée en dry-run.

- Télégram/notifications :
  - Live : certaines alertes partent vers un canal « prod ».
  - Dry-run : soit pas d’envoi, soit envoi vers un canal spécifique, soit silencieux.

- Monitoring / métriques (Prometheus, dashboards) :
  - Live : métriques de latence d’ordres, nombre d’ordres, etc.
  - Dry-run : parfois désactivées pour éviter de polluer les dashboards.

L’objectif de la note de design sera de dresser cette liste complète, module par module (trading engine, risk, notifications, monitoring), en taguant chaque divergence comme « à supprimer », « à garder », ou « à paramétrer ».

### 3.4. Stratégie de redesign

Principes de redesign :
- **Un seul chemin de code pour la logique métier de risk management**, avec un paramètre `mode` (LIVE/DRY_RUN) uniquement pour :
  - Choisir le backend d’exécution des ordres (on‑chain vs simulateur).
  - Paramétrer le *canal* des notifications (ex. Telegram dry-run vs Telegram prod), mais pas leur présence ni leur sévérité.

- **Kill switch identique** :
  - Le module risk doit, dans les deux modes, calculer un drawdown virtuel et déclencher un événement `kill_switch_triggered` qui :
    - Pose un `stop_event` partagé.
    - Loggue un CRITICAL avec le même message structurel.
    - Envoie une notification Telegram dans le canal approprié.

- **Alertes d’ordres identiques** :
  - Les signaux `order_filled_large`, `order_failed`, `order_retry_exhausted`, etc., doivent être émis en dry-run avec le même niveau que live.
  - Ce qui change, c’est seulement un tag contextuel (`mode=dry-run`) et éventuellement la cible des notifications.

- **Configuration** :
  - Ajouter un paramètre global `DRY_RUN_MODE_PARITY=true` (qui deviendra le default à terme) pour activer cette nouvelle logique.
  - Garder temporairement la possibilité d’un dry-run « light » pour éviter de casser des workflows existants, mais le marquer comme deprecated.

### 3.5. Risques de régression et plan de migration

Risques :
- Tests qui assument l’absence de kill switch en dry-run vont commencer à échouer (stop_event déclenché là où le test attend que la stratégie continue).
- Tests qui vérifient le niveau de log (INFO vs CRITICAL) devront être mis à jour.
- Outils externes (alerting, dashboards) pourraient recevoir un bruit d’alertes dry-run inattendu.

Plan de migration :
1. **Cartographie** :
   - Rechercher dans le code tous les `if dry_run:` ou équivalents.
   - Documenter dans la note « dry-run / live parity » chaque divergence.

2. **Refactor par couches** :
   - D’abord le module risk / kill switch : unifier le comportement et introduire `DRY_RUN_MODE_PARITY`.
   - Ensuite les alertes d’ordres et erreurs réseau.
   - Enfin les notifications et métriques.

3. **Tests** :
   - Dupliquer certains tests live en version dry-run : même scénario, mêmes expectations de kill switch, PnL, alertes.
   - Adapter les tests M4/M8 qui assumaient un dry-run silencieux; à minima, faire passer ces tests en mode « legacy dry-run » si nécessaire pendant la transition.

4. **Rollout** :
   - Activer la parité sur un environnement de staging avec des scénarios de drawdown forcés.
   - Vérifier que les alertes Telegram et les stops se comportent comme attendu.
   - Une fois validé, basculer la config par défaut (parité ON) et déprécier l’ancien invariant dans la doc.


## 4. Réduction de la latence détection → exécution

### 4.1. Architecture de données Polymarket utile pour la latence

Polymarket expose plusieurs sources de données pertinentes pour réduire la latence de détection des signaux :
- WebSocket CLOB (`wss://ws-subscriptions-clob.polymarket.com/ws/market` pour le canal `market`, plus un canal `user` pour les ordres) qui permet de suivre en temps réel le carnet d’ordres, les trades et des événements comme `best_bid_ask` ou `market_resolved`.[^19][^20]
- RTDS (Real‑Time Data Socket) pour un flux plus large (prices, activity, clob_market, etc.), recommandé pour les dashboards et l’analytics.
- Subgraphs GraphQL (Goldsky) pour positions, PnL, activity, open interest, etc., qui sont quasi temps réel mais surtout utiles pour l’historique et les agrégations.[^21][^22]
- APIs externes comme Bitquery qui offrent aussi un streaming (Kafka) ultra low‑latency de trades et settlements Polymarket.[^23]

La doc officielle Polymarket recommande d’utiliser les WebSockets pour les données temps réel plutôt que de poller les APIs REST / Data API, en citant explicitement que les websockets fournissent des updates avec latence minimale.[^24][^20]

### 4.2. Le problème : 10–15 s de latence actuelle

D’après `docs/architecture.md`, la latence end‑to‑end détection → exécution est ≈ 10–15 s.
Probables contributeurs :
- Polling Data API avec un intervalle de 5 s.
- Pipeline de stratégie séquentielle (plusieurs étapes de filtrage/score exécutées à la suite, pas en pipeline ou parallèle).
- Cache Gamma (metadata markets) avec TTL = 60 s, qui peut retarder la prise en compte d’un nouveau marché ou d’un changement significatif.
- Monoprocess / monothread ou GIL‑bound sur la partie CPU.

Objectif : rapprocher la latence d’un ordre de grandeur compatible avec les guides d’arbitrage Polymarket, où des stratégies entre Binance et Polymarket profitent de fenêtres 30–90 s, et où les traders compétitifs se battent sur des échelles de quelques centaines de millisecondes à quelques secondes.[^25]

### 4.3. Axes d’amélioration proposés

1. **Remplacer le polling Data API 5 s par un flux WebSocket CLOB/RTDS**
   - Utiliser `wss://ws-subscriptions-clob.polymarket.com/ws/market` pour suivre les marchés surveillés :
     - Event `book` pour snapshots complets.
     - Events `price_change`, `last_trade_price`, `best_bid_ask` pour updates incrémentales.[^26][^20]
   - En pratique, charge CPU/IO moindre que 5 s de polling massif.
   - Latence data principalement limitée par la propagation réseau (quelques dizaines de ms à quelques centaines de ms).

2. **Utiliser Goldsky subgraphs ou Bitquery pour la détection on‑chain quasi instantanée**
   - Goldsky fournit des subgraphs pour `activity` (trades, splits/merges, redemptions) et `positions`, mis à jour en temps quasi réel.[^22][^21]
   - Bitquery propose un dataset `realtime` pour les trades de prédiction sur Polymarket, avec streaming continu (WebSocket, Kafka).[^23]
   - Pour des signaux de type « smart money achete X », on peut alimenter la pipeline stratégie depuis un flux on‑chain (subgraph/Bitquery) plutôt que depuis la Data API classique.

3. **Parallélisation de la strategy pipeline**
   - Au lieu d’un pipeline séquentiel unique, séparer en micro‑étapes :
     - Ingestion / normalization (flux WebSocket → events internes).
     - Enrichissement (lookup metadata Gamma, scoring wallet, risk checks).
     - Génération de décisions (targets de position, ordres candidats).
     - Exécution (placement d’ordres via CLOB API).
   - Ces blocs peuvent fonctionner en parallèle sur des workers distincts (threads ou process), avec des queues internes.
   - Cela réduit le temps entre « signal reçu » et « décision prise », en particulier si certaines étapes CPU lourdes (scoring multi‑wallet) sont déportées sur des workers séparés.

4. **Cache Gamma plus agressif et intelligent**
   - La doc Polymarket distingue clairement les usages : Gamma REST pour metadata events/markets, CLOB WebSocket pour prix temps réel.[^21][^24]
   - Plutôt que de re‑quérir Gamma sur chaque signal, maintenir un cache en mémoire, rafraîchi :
     - Par invalidation ciblée (quand un event/market irrélevant a déjà expiré ou est résolu).
     - Par refresh périodique moins fréquent (ex. 5–10 min) pour metadata non urgentes.
   - TTL de 60 s pour toutes les metadata est probablement trop conservateur et peut être rendu adaptatif (marchés actifs vs inactifs).

5. **Multi‑process : un worker par wallet cible**
   - En s’inspirant des bots Polymarket open source (certains utilisent plusieurs workers pour marchés/stratégies différenciés), on peut déléguer chaque wallet cible à un process ou un worker dédié.[^27][^28]
   - Avantage :
     - Meilleure isolation (un wallet saturé ne bloque pas les autres).
     - Exploitation de plusieurs cœurs CPU.
   - Inconvénient : complexité de coordination (partage de flux d’events, orchestrateur de workers).

### 4.4. Tableau latence avant / après (conceptuel)

Le tableau ci‑dessous propose un découpage de la latence par étape avec un avant/après cible (ordres de grandeur, à affiner avec des métriques réelles) :

| Étape | Implémentation actuelle (approx.) | Après optimisation (cible) | Remarque |
|-------|-----------------------------------|-----------------------------|----------|
| Data fetch marché | Polling Data API 5 s (batch) | WebSocket CLOB/RTDS, latence 0.1–0.5 s | Suppression des 5 s structurels.[^20][^24] |
| Décodage + normalisation | 0.5–1 s (I/O + parsing) | 0.1–0.2 s | Streaming incrémental, buffers plus petits. |
| Enrichissement (Gamma, subgraphs) | 1–3 s (requests synchrones, TTL 60 s) | 0.2–0.5 s (cache agressif + subgraph optimisé) | Moins de round‑trips, plus de cache.[^21][^22] |
| Scoring + stratégie | 1–3 s (séquentiel) | 0.2–0.5 s (workers parallèles) | Exécuter les scorings en parallèle sur plusieurs workers.[^28] |
| Construction et envoi d’ordres | 0.5–1.0 s | 0.2–0.5 s | Optimisation de la stack HTTP/REST, pooling de connexions.[^29] |
| Exécution CLOB côté Polymarket | 0.3–0.5 s | 0.3–0.5 s (inévitable) | Latence de matching engine, problème externe.[^30] |

Cible globale : passer d’un pipeline 10–15 s à quelque chose comme 1–3 s dans les cas courants, ce qui rend beaucoup plus réalistes les stratégies de copy trading et d’arbitrage documentées publiquement.[^31][^25]

### 4.5. Recommandation d’implémentation

Phase 1 (gros gains faciles) :
- Basculer la détection sur CLOB WebSocket/RTDS pour les marchés pertinents.
- Introduire un cache metadata Gamma plus agressif et factorisé.
- Instrumenter les temps par étape (logging structuré) pour mesurer la latence actuelle.

Phase 2 (scaling) :
- Refactoriser la pipeline stratégie en blocs asynchrones/queués.
- Introduire des workers parallèles pour les scorings wallets.
- Tester un mode « 1 worker par wallet » pour les gros wallets.

Phase 3 (on‑chain fine‑tuning) :
- Si nécessaire, intégrer un flux subgraph ou Bitquery pour les signaux plus avancés (e.g., insider pattern detection, positions). 


## 5. Patterns de « smart money » Polymarket – sources internet

De nombreux articles (Binance, MEXC, HTX, blogs Polymarket‑centric, Harvard Law, etc.) listent explicitement des patterns observés sur des wallets considérés comme « smart money ».
Voici une liste de 10–20 patterns concrets, avec sources indicatives :

1. **Win rate élevé mais non extrême, avec PnL significatif** :
   - Smart money tend à avoir un win rate 60–70% sur un nombre meaningfully large de trades, sans chercher un 90–100% suspect.[^5][^12]

2. **ROI élevé mais avec contrôle du risque par trade** :
   - Limiter la taille de chaque bet à 20–40% du capital total plutôt que des all‑in, même pour des convictions fortes.[^12]

3. **Spécialisation thématique** :
   - Wallets très profitables se concentrent souvent sur 1–2 domaines (politique, sports, crypto macro) et évitent de disperser le capital sur des marchés qu’ils ne comprennent pas.[^14][^13]

4. **Entrées pré‑news avec tailles anormalement grandes** :
   - L’étude Polymarket sur l’« informed trading » montre des wallets créés récemment qui engagent de grosses tailles peu avant des annonces majeures (frappes militaires, changements politiques, etc.) et réalisent des profits importants.[^10][^8]

5. **Directionnalité forte sur un seul outcome** :
   - Les wallets « informed » concentrent leurs positions sur un côté de marché, au lieu de se hedger avec des structures plus complexes.[^9][^8]

6. **Faible nombre d’événements mais forte conviction** :
   - Nombre de trades annuel limité (10–30 marchés) mais chaque trade est large, fondé sur un edge clair (info, modèle, etc.).[^13]

7. **Utilisation d’arbitrages structurels** :
   - Exploitation répétée d’inefficiences de pricing, notamment entre Polymarket et Binance (lags de 30–90 s), arbitrages Yes/No sum, cross‑event manipulation.[^25][^13]

8. **Gestion active du risque via stop loss et daily loss limit** :
   - Outils comme Kreo intègrent des daily loss limits et des stops configurables, reflétant la pratique des traders à edge qui privilégient la survie du capital.[^1][^5]

9. **Réduction d’exposition après gros gains ou drawdowns** :
   - Les analyses qualitatives mentionnent des wallets qui réduisent la taille de leurs positions après une série de gains importants ou de pertes marquées, plutôt que de « tilt ».[^2][^12]

10. **Entrées rapides après apparition de nouvelles informations publiques** :
    - Pour les non‑insiders, l’edge provient de la vitesse de réaction aux news publiques (tweets, déclarations, chiffres), combinée à des modèles de probas internes.[^32][^14]

11. **Patterns d’activité liés à des fuseaux horaires/événements spécifiques** :
    - Certains wallets sont particulièrement actifs à des moments où les nouvelles tombent (annonces macro, décisions de cour, events sportifs), ce qui suggère une organisation autour des flux info.[^14]

12. **Utilisation de multiples wallets et clustering** :
    - Les analyses d’insider trading montrent que des groupes d’adresses liées (par flux de fonds, gas, timing) agissent de concert, ce qui peut être un signe de desk ou de collusion.[^18][^11]

13. **Tendance à sortir tôt de positions gagnantes avant la résolution** :
    - Smart money prend souvent ses profits avant la résolution finale pour éviter le risque d’events idiosyncratiques ou de problèmes d’oracle.[^18]

14. **Évitement de certains types de marchés** :
    - Les wallets de qualité évitent les marchés à structure de payoff bancale, faible liquidité ou risque réglementaire/éthique élevé (certains marchés « sensibles » sur la géopolitique).[^33][^18]

15. **Utilisation d’outils analytics et data externes** :
    - Recours à des flux ICE/Polymarket Signals, Twitter lists, etc., pour intégrer des signaux probas dans des modèles de trading systématiques.[^32][^3]

Ces patterns peuvent alimenter :
- Une pipeline de features pour le scoring v2.
- Des règles heuristiques de filtrage des wallets à suivre/ignorer.
- Des expérimentations (feature importance) pour voir quels patterns sont réellement prédictifs dans ton univers de données.


## 6. Autres améliorations latentes à considérer

En lisant la doc Polymarket, des repos de bots publics, et les articles sur smart money/copy trading, quelques pistes d’amélioration possibles (liées à tes specs M10/M11/M12) émergent :

1. **Meilleure séparation entre simulation/paper trading et vrai dry-run**
   - Certains workflows (comme « Polymarket Autopilot » pour paper trading automatisé) séparent clairement le mode simulation, avec base de données dédiée, des modes live/dry-run intégrés au même engine.[^34]
   - Tu pourrais aligner les modes :
     - `SIMULATION` (offline/backtest, pas de notifications critiques).
     - `DRY_RUN` (online, parité comportementale avec live, mais exécution simulée).
     - `LIVE` (tout réel).

2. **Intégration plus riche de l’écosystème Polymarket**
   - Repos Polymarket/agents, bots open source (copy trading, BTC 15m, market making) montrent des patterns d’architecture (7‑phase architecture, dual‑mode simulation/live, monitoring Grafana/Prometheus) qui peuvent inspirer tes futures specs M10–M12.[^35][^28]

3. **Normalisation des métriques et dashboards**
   - Construire un jeu standard de métriques de performance (Sharpe, Sortino, Calmar, max DD, PnL par catégorie, PnL par wallet suivi) inspiré des guides institutionnels crypto, pour unifier ce que tu affiches dans ton dashboard.[^15][^32]

4. **Alerte et compliance autour de patterns potentiellement insider**
   - Vu la montée des préoccupations réglementaires sur Polymarket (articles CNN, Bloomberg Law, Harvard Law, etc.), tu peux envisager une couche de flagging de patterns trop proches de l’insider trading pour tes propres règles internes (par exemple ne pas copier certains types de wallets).[^36][^8]

5. **Meilleure utilisation des rate limits et de la stack Polymarket**
   - La doc rate limits CLOB/Data API montre des marges assez confortables, mais il faut rester dans les boundaries : un design WebSocket + subgraphs permet de réduire fortement la charge REST tout en améliorant la latence.[^29][^37]

Ces points peuvent nourrir tes futures specs M10/M11/M12 :
- M10 : scoring et smart money selection.
- M11 : architecture temps réel / latence / multi‑wallet.
- M12 : observability, compliance et risk management avancés.

---

## References

1. [KreoPoly (kreo) Polymarket Telegram Bot, Kalshi Copy Trading Bot](https://kreopoly.app) - KreoPoly tracks the top Polymarket wallets and mirrors their trades on yours. Fully automatic, non-c...

2. [Polymarket Advanced: How to Build Your Smart Money Address ...](https://www.binance.com/en/square/post/294698309320177) - This article will break down how to build a high-win-rate information screening system by tracking s...

3. [Polymarket Advanced: How to Build Your Smart Money Address ...](https://www.mexc.com/news/765626) - PolyHub is a fully automated smart money copy trading tool designed specifically for Polymarket. Use...

4. [Polymarket Smart Money Copy Trading Guide - Binance](https://www.binance.com/en/square/post/306267222228834) - Abstract: A deep dive into how to identify true "smart money," compare different copy trading paths,...

5. [Polymarket Smart Money Copy Trading Guide - MEXC Exchange](https://www.mexc.com/news/988541) - Kreo (XHunt ranking 194239): A Telegram bot that monitors on-chain smart money activity in real time...

6. [Volume - Leaderboard | Polymarket](https://polymarket.com/leaderboard/overall/all/volume) - Leaderboard · 1. swisstony. +$5,984,734 · 2. 0xa61ef8773ec2e821962306ca87d4b57e39ff0abd. risk-manage...

7. [Get trader leaderboard rankings - Polymarket Documentation](https://docs.polymarket.com/api-reference/core/get-trader-leaderboard-rankings) - category · OVERALL. Market category for the leaderboard. Available options: OVERALL ,. POLITICS ,. S...

8. [From Iran to Taylor Swift: Informed Trading in Prediction Markets](https://corpgov.law.harvard.edu/2026/03/25/from-iran-to-taylor-swift-informed-trading-in-prediction-markets/)

9. [Lawmakers Call for Investigation into Suspiciously Timed ...](https://www.crowdbyte.ai/topics/lawmakers-call-for-investigation-into-suspiciously-timed-polymarket-bets-on-iran-war)

10. ['Informed' Polymarket Traders Have Netted $143 Million Since 2024](https://www.businessinsider.com/polymarket-informed-insider-trades-study-2026-3) - The study analyzed most Polymarket's trades between 2024 and 2026. It's the first to estimate the to...

11. [Tracking 'Insider Signals' from Public Data: Screening Strategies for ...](https://www.htx.com/it-it/news/tracking-insider-signals-from-public-data-screening-strategi-BP2DRS7i/) - Title: Tracking "Insider Signals" from Public Data: Strategies for Identifying High-Accuracy Address...

12. [Polymarket's 2025 report on six profitable business models starts ...](https://www.binance.com/en/square/post/34300800389401) - Calculate metrics such as Return on Investment (ROI) and Sharpe ratio. Excellent traders typically m...

13. [Polymarket's 2025 report on six profitable business models starts ...](https://www.mexc.com/news/359822) - Polymarket, a decentralized prediction market that has processed over $9 billion in trading volume a...

14. [Polymarket in Action: A Complete Guide to Finding and Following ...](https://www.odaily.news/en/post/5207604) - This article reviews smart money accounts on Polymarket covering sports competitions, political elec...

15. [Sharpe, Sortino & Calmar Ratios: Crypto Metrics Guide - XBTO](https://www.xbto.com/resources/sharpe-sortino-and-calmar-a-practical-guide-to-risk-adjusted-return-metrics-for-crypto-investors) - Master the risk-adjusted metrics institutional investors use to evaluate crypto. Learn Sharpe, Sorti...

16. [#multistrats #investing #sharpe #sortino #calmar #alpha | Sam Vogel](https://www.linkedin.com/posts/samvogel_multistrats-investing-sharpe-activity-7265139396125822978-DWky) - Sharpe, Sortino, and Calmar Ratios: How Investors Measure Risk-Adjusted Returns - In my conversation...

17. [Sharpe ratio vs Sortino ratio : r/quant - Reddit](https://www.reddit.com/r/quant/comments/1kjiu3r/sharpe_ratio_vs_sortino_ratio/) - Neither Sharpe nor Sortino are good metrics for returns that are very far from a normal distribution...

18. [Detect Insider Trading on Polymarket | Detection Guide | PolyTrack](https://www.polytrackhq.app/blog/detect-insider-trading-polymarket) - Learn to identify potential insider trading patterns on Polymarket. Spot unusual volume spikes, timi...

19. [Polymarket WebSocket Guide: Channels, Subscriptions & Real ...](https://agentbets.ai/guides/polymarket-websocket-guide/) - Connect to all four Polymarket WebSocket channels, subscribe to market and user feeds, build a local...

20. [Orderbook - Polymarket Documentation](https://docs.polymarket.com/trading/orderbook) - Reading the orderbook, prices, spreads, and midpoints

21. [Polymarket GraphQL Tutorial 2025: 5 Subgraphs, The Graph ...](https://www.polytrackhq.app/blog/polymarket-graphql-subgraph-guide) - Query gamma-api.polymarket.com and all 5 GraphQL subgraphs. Working code examples for Python & JavaS...

22. [Overview - Polymarket Documentation](https://polymarket-292d1b1b.mintlify.app/developers/subgraph/overview)

23. [Polymarket API - Trade, Prices & Market Data | Bitquery](https://docs.bitquery.io/docs/examples/polymarket-api/) - The Bitquery Polymarket API provides prediction market data on Polygon via GraphQL. ... Use Kafka St...

24. [Data Feeds](https://docs.polymarket.com/developers/market-makers/data-feeds) - Real-time and historical data sources for market makers

25. [Binance to Polymarket Arbitrage Strategies: Finding Edge Across ...](https://www.quantvps.com/blog/binance-to-polymarket-arbitrage-strategies) - Arbitrage trading between Binance and Polymarket offers traders a way to profit from price differenc...

26. [Polymarket WebSocket | barzoj/yet-another-polymarket-maker | DeepWiki](https://deepwiki.com/barzoj/yet-another-polymarket-maker/7.1-polymarket-websocket) - This document describes the WebSocket integration with Polymarket's Central Limit Order Book (CLOB) ...

27. [echandsome/Polymarket-betting-bot - GitHub](https://github.com/echandsome/Polymarket-betting-bot) - A comprehensive TypeScript/Node.js backend system for automated trading on Polymarket. This platform...

28. [GitHub - aulekator/Polymarket-BTC-15-Minute-Trading-Bot](https://github.com/aulekator/Polymarket-BTC-15-Minute-Trading-Bot) - A production-grade algorithmic trading bot for Polymarket's 15-minute BTC price prediction markets. ...

29. [Rate Limits - Polymarket Documentation](https://docs.polymarket.com/api-reference/rate-limits) - All API rate limits are enforced using Cloudflare's throttling system. When you exceed the limit for...

30. [Faster Polymarket API orders? : r/algotrading - Reddit](https://www.reddit.com/r/algotrading/comments/1sbwxbv/faster_polymarket_api_orders/) - This isn't about a data feed, it's more about the latency of the CLOB REST API. The API takes 25ms t...

31. [Mastering Polymarket, these 7 tools are enough (with ... - Binance](https://www.binance.com/en-IN/square/post/305665434864994) - Summary: A compilation of 7 Polymarket tools, covering three major scenarios: trading terminals, dat...

32. [Prediction Markets: How Sure Is the Bet? - GARP](https://www.garp.org/risk-intelligence/technology/prediction-markets-how-260227) - “Economists have noticed that betting markets like Kalshi and Polymarket are pretty good at predicti...

33. [Explainer: Insider Trading and Prediction Markets](https://mac.ncsu.edu/2026/03/26/explainer-insider-trading-and-prediction-markets/) - Prediction markets (such as Kalshi and Polymarket) allow participants to trade contracts tied to spe...

34. [Polymarket Autopilot: Automated Paper Trading - GitHub](https://github.com/hesamsheikh/awesome-openclaw-usecases/blob/main/usecases/polymarket-autopilot.md) - You want to test and refine trading strategies without risking real capital. This workflow automates...

35. [Trade autonomously on Polymarket using AI Agents - GitHub](https://github.com/polymarket/agents) - Polymarket Agents is a developer framework and set of utilities for building AI agents for Polymarke...

36. [Insider Trading in Prediction Markets Poses Compliance Risks](https://news.bloomberglaw.com/legal-exchange-insights-and-commentary/insider-trading-in-prediction-markets-poses-compliance-risks) - Last month, Polymarket announced it was barring trades on stolen confidential information, illegal t...

37. [API Rate Limits - Polymarket Documentationdocs.polymarket.com › quickstart › introduction › rate-limits](https://docs.polymarket.com/quickstart/introduction/rate-limits)

