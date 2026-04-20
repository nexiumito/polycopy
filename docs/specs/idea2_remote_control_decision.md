# ADR — Contrôle à distance multi-machine (Idée 2)

**Status** : Proposed — 2026-04-20
**Decision maker** : Elie (user)
**Reviewers** : Claude

---

## 1. Contexte

Polycopy peut tourner simultanément sur 3 machines (Debian université, Windows/WSL2 maison, MacBook portable). Le kill switch ([pnl_writer.py](../../src/polycopy/monitoring/pnl_writer.py)) pose `stop_event` sur drawdown et tout le process sort via le TaskGroup top-level ([runner.py:186-197](../../src/polycopy/cli/runner.py#L186-L197)). Aujourd'hui, relancer un bot à distance = SSH ou accès physique. Objectif : commande depuis téléphone, en < 10 s, depuis n'importe quel réseau, de manière sûre.

## 2. Contraintes

- **C1** 3 OS : Debian 12 (root), Windows 11/WSL2 Ubuntu, macOS récent.
- **C2** NAT universitaire et autres réseaux mobiles : **pas d'inbound**.
- **C3** UX simple : 1-2 taps depuis téléphone, sans VPN à allumer manuellement.
- **C4** Un seul bot Telegram global (1 token, 1 chat) avec `MACHINE_ID` en badge.
- **C5** Scope v1 : `/status`, `/stop`, `/restart`, `/resume` avec cible machine explicite.
- **C6** 2FA double-confirmation sur commandes destructives (`/stop`, `/restart`).
- **C7** Whitelist stricte des appelants, silence total pour requêtes non autorisées.
- **C8** Tailscale acceptable si justifié.

Invariants CLAUDE.md à préserver : §13 M7 (bot Telegram emitter-only), discipline secrets (aucun log token), `stop_event` propagation inchangée, pas d'inbound public.

## 3. Alternatives étudiées

### 3.1 Channel

**A. Telegram long-polling** (`getUpdates` dans un worker du `MonitoringOrchestrator`) — réutilise `TelegramClient` ([telegram_client.py](../../src/polycopy/monitoring/telegram_client.py)). Outbound HTTPS uniquement, aucun inbound.

**B. Telegram webhook** — nécessite endpoint HTTPS public + TLS. **Éliminé par C2** (inbound impossible derrière NAT uni).

**C. Tailscale mesh + endpoint HTTP sur chaque bot** — Tailscale est un VPN mesh basé WireGuard qui assigne une IP privée `100.x.x.x` à chaque device inscrit dans un même "tailnet". Peer-to-peer avec hole-punching NAT (outbound UDP 41641), aucun port inbound exposé côté bot. Chaque machine expose un petit FastAPI sur `0.0.0.0:8765` lié à l'interface `tailscale0` uniquement. Le téléphone (app Tailscale iOS/Android en arrière-plan, impact batterie négligeable) accède via `http://pc-fixe.<tailnet>.ts.net:8765/...` ou Shortcut iOS en 1 tap. Plan gratuit ≤ 100 devices suffit largement.

**D. Cloudflare Tunnel (`cloudflared`)** — tunnel outbound vers Cloudflare puis URL publique + Cloudflare Access (OTP email). Fonctionne derrière NAT, mais surface publique (même avec Access), friction OTP email, domaine Cloudflare requis, vendor-lock fort.

**E. ngrok** — free tier = sous-domaine éphémère, stable = payant. Éliminé.

**F. Hybride Telegram emit-only + Tailscale inbound** — conserve M7 §13 strict pour Telegram, ajoute Tailscale pour commandes. Redondant si C seul couvre déjà tout (status HTTP + alertes Telegram existantes).

### 3.2 Superviseur (par OS)

- **Debian 12** : `systemd --user` unit, `Restart=on-failure`, `RestartSec=5s`. User a root, unit-level système OK mais user-level plus propre pour un bot personnel.
- **macOS** : `launchd` LaunchAgent dans `~/Library/LaunchAgents/fr.polycopy.bot.plist`, `KeepAlive={SuccessfulExit:false}`, `ThrottleInterval=5`.
- **Windows/WSL2** : systemd dans WSL2 supporté depuis 2022 (`[boot] systemd=true` dans `/etc/wsl.conf`). On unifie sur systemd user unit avec Debian. Couche extérieure : Windows Task Scheduler démarre `wsl.exe -d Ubuntu --exec /bin/true` au logon pour garantir que WSL2 est up (sans ça, systemd inside ne peut pas démarrer avant la première invocation WSL).
- **tmux + `while true`** : fragile (pas de journal, pas de dépendances, redémarrage au logout). Rejeté.
- **Docker Compose `restart: always`** : uniforme cross-OS mais ajoute Docker comme dep, complique le réseau Polymarket + Tailscale inside container, conflit avec le binding dashboard 127.0.0.1. Rejeté.

## 4. Scoring

Critères et poids — score /5 × poids = contribution.

| Critère (poids) | A. TG polling | C. Tailscale | D. CF Tunnel | F. Hybride |
|---|---|---|---|---|
| UX simplicité (×3) | 5 → 15 | 4 → 12 | 4 → 12 | 3 → 9 |
| NAT traversal (×3) | 5 → 15 | 5 → 15 | 5 → 15 | 5 → 15 |
| Sécurité (×3) | 2 → 6 | 5 → 15 | 3 → 9 | 4 → 12 |
| Setup friction (×2) | 4 → 8 | 3 → 6 | 2 → 4 | 2 → 4 |
| Cohérence M7 (×2) | 2 → 4 | 4 → 8 | 3 → 6 | 4 → 8 |
| Maintenance (×1) | 3 → 3 | 4 → 4 | 3 → 3 | 3 → 3 |
| Vendor lock-in (×1) | 3 → 3 | 4 → 4 | 1 → 1 | 3 → 3 |
| **Total** | **54** | **64** | **50** | **54** |

Notes scores :
- A. pénalisé sur sécurité (2FA in-band cassée si téléphone volé déverrouillé + chat ouvert, cf. §5.3) et cohérence M7 (viole §13 emitter-only). **Disqualifiant** : conflit `getUpdates` concurrent (cf. §5.1).
- C. pénalisé sur setup (4 installs) mais gagne sur sécurité (mTLS WireGuard + TOTP) et préserve §13.
- D. vendor-lock fort (domaine + Access) et surface publique résiduelle.
- F. UX dégradée (2 canaux à choisir selon commande) sans gain vs C pur.

## 5. Points durs

### 5.1 Concurrent `getUpdates` (Telegram)

Telegram Bot API **n'autorise qu'un seul `getUpdates` actif par token**. Deuxième poller simultané → HTTP 409 `Conflict: terminated by other getUpdates request`. Avec C4 (1 bot global) + 3 machines qui polleraient simultanément = 2/3 perma-409. Solutions possibles :

1. **Élection de master poller** via lease partagé (Redis/DB/Supabase) — demande infra centrale, complexe pour 3 machines perso.
2. **Un bot par machine** — contredit C4, triplication secrets.
3. **Service central toujours on** (VPS ou Cloudflare Worker) qui poll + relaye — ajoute une 4ème boîte à maintenir.
4. **Ne pas poller du tout** — approche retenue via Tailscale (cf. §6).

→ La contrainte C4 + l'absence d'infra centrale **disqualifient Telegram polling en l'état**. Tailscale élimine le problème par construction (request/response, aucun polling).

### 5.2 Sémantique `/stop`, `/restart`, `/resume`

`stop_event.set()` = process entier quitte ([runner.py:144-197](../../src/polycopy/cli/runner.py#L144-L197)). Un HTTP endpoint intégré au process meurt avec lui → **impossible de recevoir `/resume` si le bot est down**.

Solution : **mode "paused" intégré au bot**, piloté par un sentinel file `~/.polycopy/halt.flag`.

```
Boot flow :
1. `cli/runner.py` vérifie `halt.flag` avant `asyncio.run(_async_main())`.
2. Si présent → mode paused : lance UNIQUEMENT `MonitoringOrchestrator`
   (heartbeat + alerts dispatcher) + nouveau `RemoteControlServer`.
   Pas de watcher/strategy/executor/discovery/dashboard.
3. Si absent → mode normal : TaskGroup complet actuel + `RemoteControlServer`
   en plus.

Commandes :
- /status        → JSON {machine_id, mode: "running"|"paused", uptime, pnl_today, ...}
- /restart       → 2FA → pose rien → stop_event.set() → superviseur respawn → mode normal.
- /stop          → 2FA → touch halt.flag → stop_event.set() → superviseur respawn →
                   mode paused (seul RemoteControlServer + heartbeat tournent).
- /resume        → 2FA → rm halt.flag → stop_event.set() → superviseur respawn → mode normal.
```

Avantages :
- `RemoteControlServer` **toujours up** (en running comme en paused), donc `/resume` est toujours servi.
- Kill switch automatique : `PnlSnapshotWriter` touch `halt.flag` **avant** `stop_event.set()` → respawn en paused → heartbeat Telegram alerte "paused depuis kill_switch", user `/resume` depuis phone quand prêt. Remplace le besoin M7 §13 actuel où le kill switch force un SSH manuel.
- Sentinel file lisible par superviseur en pre-exec optionnel (`systemd ExecStartPre`), mais non nécessaire puisque le bot gère lui-même la bifurcation.

Signal propagation : `RemoteControlServer` prend le `stop_event` du TaskGroup en référence + expose une méthode `request_shutdown(halt: bool)` qui (1) touch/rm `halt.flag`, (2) set `stop_event`, (3) répond HTTP 202 au client, (4) laisse le TaskGroup terminer proprement.

### 5.3 Surface 2FA réelle

Modèle de menace : téléphone volé déverrouillé + app Tailscale active.
- Attaquant a accès réseau Tailscale (1er facteur compromis).
- Il peut ouvrir `http://pc-fixe.ts.net:8765/stop`.
- **2ème facteur obligatoire** : TOTP via `pyotp` (RFC 6238), secret dans env var `REMOTE_CONTROL_TOTP_SECRET`, app Google Authenticator / 1Password sur même téléphone.
- Si Authenticator protégé par biométrie (Face ID / Touch ID) indépendante du déverrouillage phone → vol déverrouillé ne donne pas accès au TOTP.

Pattern challenge-response double-confirm (C6) :
1. `GET /stop/PC-FIXE` sans TOTP → bot répond HTTP 202 "Confirm with TOTP: `GET /stop/PC-FIXE?totp=123456`" + log `remote_control_confirmation_requested`. Valide 60 s.
2. `GET /stop/PC-FIXE?totp=123456` → `pyotp.TOTP(secret).verify(code, valid_window=1)` → exécute. Rate-limit : 5 tentatives / minute par IP, puis 429.
3. Échec TOTP 3× en 60 s → `halt.flag` touché automatiquement (auto-lockdown), alerte Telegram CRITICAL `remote_control_brute_force_detected`.

Pas d'alternative "mot de passe one-time partagé" (TOCTOU : visible dans l'historique Telegram ou headers HTTP loggés). TOTP seul.

**Commandes read-only sans TOTP** (safe-by-design) :
- `GET /status/<machine>` — pas de 2FA (pas destructive).
- `GET /health` — liveness probe Tailscale-only.

## 6. Recommandation

**Channel retenu** : **C. Tailscale mesh + endpoint HTTP local lié à `tailscale0`**.
- FastAPI minimal (3-4 routes) lancé dans le TaskGroup top-level ([runner.py:186-197](../../src/polycopy/cli/runner.py#L186-L197)), bind sur `100.x.x.x:8765` (interface Tailscale uniquement, jamais `0.0.0.0`).
- Authn : TOTP sur commandes destructives, réseau Tailscale fait office de premier facteur.
- Préserve **strictement** M7 §13 (Telegram reste emitter-only).

**Superviseur retenu par OS** :
- **Debian 12** : `systemd --user` avec `Restart=on-failure`, `RestartSec=5`, fourni dans `scripts/systemd/polycopy.service`.
- **macOS** : `launchd` LaunchAgent, fourni dans `scripts/launchd/fr.polycopy.bot.plist`.
- **Windows/WSL2** : systemd user unit identique Debian (prérequis : `/etc/wsl.conf` avec `systemd=true`) + Task Scheduler Windows qui démarre WSL2 au logon (fourni dans `scripts/windows/polycopy-wsl-autostart.xml`).

**2FA retenu** : **TOTP RFC 6238 via `pyotp`** sur `/stop` et `/restart` uniquement, challenge-response double-call avec fenêtre 60 s + rate-limit 5/min + auto-lockdown après 3 échecs.

**Mapping `MACHINE_ID` ↔ Tailscale hostname** : recommandé — user définit `MACHINE_ID=PC-FIXE` ET enregistre la machine dans Tailscale avec hostname `pc-fixe`. Le bot expose `/status/PC-FIXE`, `/restart/PC-FIXE`, etc. (case-insensitive, normalisé upper en interne). Validation boot : si `tailscale status` n'a pas un hostname matching `MACHINE_ID.lower()`, warning au démarrage (pas fatal).

## 7. Conséquences

### Changements architecture

Nouveaux fichiers :
- `src/polycopy/remote_control/__init__.py`
- `src/polycopy/remote_control/server.py` — FastAPI app, bind Tailscale-only.
- `src/polycopy/remote_control/auth.py` — TOTP verify, rate limiter in-memory, auto-lockdown.
- `src/polycopy/remote_control/handlers.py` — `/status`, `/stop`, `/restart`, `/resume`.
- `src/polycopy/remote_control/sentinel.py` — `halt.flag` lifecycle (read/touch/rm, permissions 0o600).
- `src/polycopy/remote_control/orchestrator.py` — `RemoteControlOrchestrator.run_forever(stop_event)` ; wire uvicorn dans le TaskGroup.

Modifications existantes (diff minimal) :
- `cli/runner.py` : check `halt.flag` au boot, bifurcation `run_full_mode` vs `run_paused_mode`. Ajoute `RemoteControlOrchestrator` au TaskGroup (mode normal) ou TaskGroup réduit (mode paused avec `MonitoringOrchestrator` + `RemoteControlOrchestrator` seulement).
- `monitoring/pnl_writer.py` : kill switch touch `halt.flag` **avant** `stop_event.set()` (2 LOC). Alerte Telegram mentionne "machine reprendra en mode paused au respawn".
- `monitoring/startup_notifier.py` : boot message mentionne `mode: running|paused` (ré-utilise le binding `mode_badge` de [alert_renderer.py:146-155](../../src/polycopy/monitoring/alert_renderer.py#L146-L155)).
- `config.py` : nouvelles env vars (voir ci-dessous).

Nouvelles env vars :
- `REMOTE_CONTROL_ENABLED: bool = False` — opt-in strict, off par défaut.
- `REMOTE_CONTROL_PORT: int = 8765` — bind Tailscale-only.
- `REMOTE_CONTROL_TOTP_SECRET: str | None = None` — base32, 32 chars, généré via `pyotp.random_base32()`. Fatal si `REMOTE_CONTROL_ENABLED=true` et absent.
- `REMOTE_CONTROL_ALLOWED_TAILSCALE_PEERS: str = ""` — CSV hostnames Tailscale autorisés (ex. `iphone-elie,macbook-elie`). Vide = tous les peers du tailnet acceptés (OK pour tailnet perso 1-user).
- `REMOTE_CONTROL_SENTINEL_PATH: str = "~/.polycopy/halt.flag"` — override testable.
- `MACHINE_ID: str | None = None` — fallback `socket.gethostname()` (cf. Idée 1, env partagée).

Nouvelle dépendance : `pyotp` (MIT, ~200 LOC, zero-dep). Pas de `python-telegram-bot` — Tailscale sidestep.

### Impact lifecycle M7

Zéro. Telegram reste emitter-only. `RemoteControlOrchestrator` est un module frère indépendant. `MonitoringOrchestrator` inchangé (sauf 2 lignes pnl_writer pour sentinel). TaskGroup top-level ajoute 1 task conditionnel.

### Surface sécurité ajoutée & mitigations

| Surface | Mitigation |
|---|---|
| Port TCP 8765 ouvert | Bind `tailscale0` uniquement via `--host=100.x.x.x` (résolu dynamiquement au boot via `tailscale ip -4`). Boot fatal si interface absente. |
| TOTP secret leak | Discipline identique `TELEGRAM_BOT_TOKEN` : jamais loggé, env var only, rotation trimestrielle, permissions `.env` 0o600. |
| Rejouabilité TOTP | `pyotp.verify(valid_window=1)` = ±30s, rate-limit 5/min, auto-lockdown 3 échecs. |
| Tailscale account compromis | Activer MFA sur compte Tailscale. Révocation device = coupure immédiate (1 clic web UI). |
| Sentinel file tampering | 0o600 strict. Local-only, pas de network path. |

### Tests nécessaires

- `tests/unit/test_remote_control_auth.py` : TOTP verify, rate-limit, auto-lockdown (mock `time`).
- `tests/unit/test_remote_control_handlers.py` : `/status`, `/stop`, `/restart`, `/resume` avec pytest-asyncio + httpx AsyncClient.
- `tests/unit/test_remote_control_sentinel.py` : halt.flag lifecycle, permissions, override path.
- `tests/unit/test_runner_paused_mode.py` : boot avec sentinel → TaskGroup réduit, kill switch → sentinel touché avant stop_event.
- `tests/unit/test_remote_control_security.py` : bind interface non-Tailscale refuse de démarrer, TOTP secret absent en enabled=true fatal.
- `tests/integration/test_remote_control_tailscale.py` (opt-in `integration`) : vrai Tailscale hostname resolution end-to-end.

Coverage cible : 85% sur `remote_control/` (zone sensible sécurité).

## 8. Questions restantes au user

Aucune décision bloquante pour la spec d'implémentation — Tailscale résout la contrainte C4 + le conflit `getUpdates` par construction (§5.1). Cependant, 3 confirmations utiles avant spec complète :

1. **OS téléphone** : iPhone (iOS Shortcuts supportés nativement, expérience optimale) ou Android (Tasker ou HTTP Shortcuts app tiers) ? Détermine les exemples UX livrés avec la spec.
2. **Tailscale** : OK pour créer compte gratuit (Google/GitHub/email) + installer le daemon sur les 3 machines + app sur téléphone ? Alternative Headscale (self-hosted) si méfiance vendor — mais ajoute un VPS à maintenir, recommandation = Tailscale hébergé pour un projet perso.
3. **WSL2 systemd** : confirmer que la machine Windows tourne WSL2 récent (Win 11 ≥ 22H2) et que `/etc/wsl.conf` peut être édité. Sinon on fallback sur wrapper script + Task Scheduler plus verbeux.

Ces 3 points affinent le volet setup/superviseur de la spec mais ne remettent pas en cause l'architecture retenue.
