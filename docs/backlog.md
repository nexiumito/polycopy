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
