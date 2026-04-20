# M12_bis — Multi-machine & Remote Control

**Status** : Draft — 2026-04-20
**Depends on** : M7 (Telegram alerts), M4 (monitoring orchestrator), M9 (CLI runner), M4.5/M6 (dashboard)
**ADR** : [idea2_remote_control_decision.md](./idea2_remote_control_decision.md)
**Setup guide** (livré en fin de phase F) : [M12_bis_remote_control_setup_guide.md](./M12_bis_remote_control_setup_guide.md)

---

## 0. TL;DR

Ajout d'un badge `MACHINE_ID` dans toutes les alertes Telegram (Idée 1, trivial) et d'un canal de contrôle à distance via **Tailscale mesh + FastAPI + TOTP** (Idée 2). Commandes `/status`, `/restart`, `/stop`, `/resume` depuis le téléphone, fonctionnent derrière NAT universitaire. Kill switch force un respawn en mode "paused" (halt.flag sentinel) ; le user reprend depuis téléphone via `/resume`. Zéro régression M12 : tout est opt-in strict (`REMOTE_CONTROL_ENABLED=false`, `DASHBOARD_BIND_TAILSCALE=false` par défaut).

## 1. Invariants (non-régressions dures)

- **M7 §13 préservé** : Telegram reste strictement emitter-only (aucun `getUpdates`, aucun webhook). Le canal incoming passe exclusivement par Tailscale.
- **M4.5/M6 dashboard** : read-only (`GET` only), zéro secret dans HTML/JSON, Swagger off, CDN pinned — `test_dashboard_security.py` + `test_dashboard_security_m6.py` passent à l'identique.
- **Triple garde-fou M3 Executor** : inchangé, jamais touché.
- **`stop_event` propagation** : sémantique inchangée (set = tout le TaskGroup exit). Le mode paused se construit **au-dessus** du respawn superviseur, pas en modifiant stop_event.
- **CLI M9** : `LOG_FILE` + rotation + permissions 0o600 identiques. Pas de nouveau handler de log.
- **Backward compat** : `REMOTE_CONTROL_ENABLED=false` (default) ⇒ zero-diff utilisateur M12. `MACHINE_ID` non set ⇒ fallback `socket.gethostname()`, zéro crash.
- **Discipline secrets M7** : `REMOTE_CONTROL_TOTP_SECRET` discipline identique `TELEGRAM_BOT_TOKEN` (jamais loggé, rotation trimestrielle documentée, permissions `.env` 0o600).
- **Kill switch sémantique** (M4/M10) : inchangé sur la partie détection ; ajout additif d'un `sentinel.touch()` **avant** `stop_event.set()`.

## 2. Configuration (env vars)

| Nom | Type | Default | Validation boot | Gate fatal | Loggé au boot ? | Notes |
|---|---|---|---|---|---|---|
| `MACHINE_ID` | `str \| None` | `None` | Si non-None : strip + upper, regex `^[A-Z0-9_-]+$`, ≤32 chars. Si None/vide : fallback `socket.gethostname()` puis même regex | Crash si regex KO après fallback | ✓ clair, event `machine_id_resolved` avec `source=env\|hostname` | Idée 1 — public, non sensible |
| `MACHINE_EMOJI` | `str` | `"🖥️"` | ≤4 chars (grapheme cluster unicode) | Non (défaut safe) | ✓ clair | Idée 1 — user choisit l'emoji (`💻` MacBook, `🏫` uni, `🖥️` fixe). Pas de mapping automatique |
| `REMOTE_CONTROL_ENABLED` | `bool` | `False` | — | — | ✓ clair | Maître opt-in. Off = zero code-path M12_bis activé |
| `REMOTE_CONTROL_PORT` | `int` | `8765` | Range 1024-65535 | Crash si port occupé au boot | ✓ clair | Pas 8000 (conflit dashboard) ni 8080 (conflit commun dev) |
| `REMOTE_CONTROL_TOTP_SECRET` | `str \| None` | `None` | Base32 valide, ≥16 chars | **Crash si `REMOTE_CONTROL_ENABLED=true` et absent** | ✗ **jamais loggé** même tronqué | Généré via `pyotp.random_base32()` (cf. setup guide §5) |
| `REMOTE_CONTROL_SENTINEL_PATH` | `str` | `"~/.polycopy/halt.flag"` | Expansion `~`, parent créé au boot avec 0o700, fichier 0o600 quand touché | Non | ✓ clair (chemin expandu) | Override utile pour tests intégration |
| `REMOTE_CONTROL_ALLOWED_TAILSCALE_PEERS` | `str` (CSV) | `""` | CSV hostnames minuscules ; chaque entrée matche regex `^[a-z0-9-]+$` | Non (vide = tous peers tailnet autorisés) | ✓ clair | Pour tailnet perso solo, vide est safe. Durcir en multi-user |
| `REMOTE_CONTROL_TAILSCALE_IP_OVERRIDE` | `str \| None` | `None` | IPv4 dotted-quad si set | Non | ✓ clair | Bypass `tailscale ip -4` (tests, edge cases) |
| `DASHBOARD_BIND_TAILSCALE` | `bool` | `False` | Si `true` : résolution IP Tailscale même logique que remote_control | **Crash si `true` et Tailscale absent/offline** | ✓ clair | Cohabite avec `DASHBOARD_HOST` ; warning explicite si les deux settés (priorité `DASHBOARD_BIND_TAILSCALE`) |

Toutes les env vars ci-dessus sont ajoutées à `.env.example` avec une ligne de commentaire. `REMOTE_CONTROL_TOTP_SECRET` est marqué `# ROTATE EVERY 90 DAYS — NEVER COMMIT`.

---

## 3. Idée 1 — `MACHINE_ID` dans les alertes Telegram

### 3.1 Contrat

- Env var `MACHINE_ID: str | None` + `MACHINE_EMOJI: str = "🖥️"` ajoutées à `Settings` dans [config.py](../../src/polycopy/config.py).
- **Validator Pydantic** (model_validator mode="after") :
  - Si `MACHINE_ID` est None, string vide, ou whitespace-only → fallback `socket.gethostname()`.
  - Résultat : `strip().upper()` → regex `^[A-Z0-9_-]+$`, cap 32 chars.
  - Si regex KO (hostnames avec points, accents, etc.) → `value = re.sub(r"[^A-Z0-9_-]", "-", value)[:32]`. Si résultat vide après sub → `value = "UNKNOWN"`.
- Log boot unique (via `configure_logging` puis juste avant `init_db`) :
  ```
  log.info("machine_id_resolved", machine_id=<resolved>, source="env"|"hostname", emoji=<emoji>)
  ```
- Aucune persistance DB. Aucun impact schéma.

### 3.2 Intégration `AlertRenderer`

Pattern strict copy-paste de `_MODE_BADGE` ([alert_renderer.py:54-60, 146-155](../../src/polycopy/monitoring/alert_renderer.py#L54)).

Changements à [alert_renderer.py](../../src/polycopy/monitoring/alert_renderer.py) :

1. Signature `__init__` étendue :
   ```
   def __init__(
       self,
       project_root: Path | None = None,
       mode: str = "dry_run",
       machine_id: str = "UNKNOWN",
       machine_emoji: str = "🖥️",
   ) -> None:
   ```
2. Nouveaux attributs `self._machine_id`, `self._machine_emoji`.
3. Helper `_inject_mode()` renommé `_inject_context()` — injecte `mode`, `mode_badge`, `machine_id`, `machine_emoji`. Retour-compat : le binding `mode_badge` reste identique.
4. `_startup_vars()` injecte aussi les deux bindings (comme pour `mode_badge`).
5. Appelant : `MonitoringOrchestrator` ([orchestrator.py:82](../../src/polycopy/monitoring/orchestrator.py#L82)) passe `machine_id=settings.machine_id, machine_emoji=settings.machine_emoji`.

### 3.3 Templates — format header unifié

**Format retenu** (ligne séparée, sous le titre principal, emoji en premier pour scan visuel rapide) :

```jinja2
_\[{{ mode_badge | telegram_md_escape }}\]_
{{ machine_emoji }} *{{ machine_id | telegram_md_escape }}*
```

**Décision tranchée** : **emoji configurable par machine via env var `MACHINE_EMOJI`**, pas de mapping automatique basé sur hostname. Raison : le mapping (`🏠`/`💻`/`🏫`) est subjectif, coûte une table de correspondance, et la contrainte user ("beaux messages clairs") est satisfaite par un emoji choisi par lui-même par machine.

Liste exhaustive des **14 templates** à patcher ([src/polycopy/monitoring/templates/](../../src/polycopy/monitoring/templates/)) :

1. `startup.md.j2` — header `🤖 *polycopy démarré*` reçoit la ligne machine_id juste avant.
2. `shutdown.md.j2`
3. `heartbeat.md.j2`
4. `daily_summary.md.j2`
5. `digest.md.j2`
6. `fallback.md.j2` (important : tous les event types non spécialisés passent par là)
7. `kill_switch_triggered.md.j2`
8. `pnl_snapshot_drawdown.md.j2`
9. `executor_auth_fatal.md.j2`
10. `executor_error.md.j2`
11. `order_filled_large.md.j2`
12. `discovery_cap_reached.md.j2`
13. `discovery_cycle_failed.md.j2`
14. `trader_promoted.md.j2`
15. `trader_demoted.md.j2`

(Note : 15 fichiers, pas 14 — recomptés dans [templates/](../../src/polycopy/monitoring/templates/). `partials/common_partials.md.j2` n'est pas modifié.)

### 3.4 Tests

- **Nouveau** `tests/unit/test_machine_id_fallback.py` :
  - `MACHINE_ID` absent → fallback hostname (monkeypatch `socket.gethostname`).
  - `MACHINE_ID=""` / whitespace-only → fallback hostname.
  - `MACHINE_ID="pc-fixe.local"` → normalisé `PC-FIXE-LOCAL` (point remplacé par tiret).
  - `MACHINE_ID=" @@@ "` (tout invalide) → `"UNKNOWN"`.
  - Log `machine_id_resolved` émis avec bon `source`.
- **Étendre** `tests/unit/test_telegram_badge.py` :
  - Chaque template modifié contient `{{ machine_emoji }}` et `{{ machine_id | telegram_md_escape }}` dans son source Jinja (grep).
  - Rendu d'un `Alert` fake : le résultat contient exactement `MACHINE_EMOJI *MACHINE_ID*` sur la 2e ligne.
- **Étendre** `tests/unit/test_telegram_template_rendering.py` :
  - Fixture `machine_id="PC-FIXE"`, `machine_emoji="🖥️"` passée au renderer.
  - Vérifier échappement : `MACHINE_ID="A-1_B"` → rendu `A\-1\_B` (tiret et underscore échappés en MarkdownV2).
- **Étendre** `test_telegram_template_rendering.py::test_no_secret_leak` : MACHINE_ID contenant `xxx:yyy` ou URL doit être échappé, pas en lien cliquable.

### 3.5 Mise à jour CLAUDE.md

Ajouter à la section "Conventions de code", bloc "Front-end dashboard" : une ligne sur `MACHINE_ID` + `MACHINE_EMOJI` comme étant "publics, non sensibles, loggés clair, injection identique `mode_badge` M10".

---

## 4. Idée 2 — Remote Control Tailscale

### 4.1 Architecture — arborescence du package

```
src/polycopy/remote_control/
├── __init__.py              # Exporte RemoteControlOrchestrator uniquement
├── orchestrator.py          # RemoteControlOrchestrator.run_forever(stop_event) — wire uvicorn dans TaskGroup
├── server.py                # build_app() -> FastAPI, dependencies injection
├── handlers.py              # routes /status /restart /stop /resume /health
├── auth.py                  # TOTPGuard (pyotp), RateLimiter, AutoLockdown
├── sentinel.py              # SentinelFile — read/touch/rm atomic, permissions 0o600
├── tailscale.py             # resolve_tailscale_ipv4(timeout=5s) + validate_peer(ip)
└── dtos.py                  # Pydantic models: StatusResponse, CommandResponse, ErrorResponse
```

**Règle de dépendance** (cohérent CLAUDE.md) :
- `remote_control` → `storage` (lecture seule pour `/status`), `monitoring` (alerts), `config`.
- `remote_control` ne dépend **pas** de `watcher`, `strategy`, `executor`, `discovery`, `dashboard` (modules fonctionnels).
- Communication avec le reste : via `stop_event` (write), `session_factory` (read), `alerts_queue` (push alerts sécurité).

### 4.2 Paused mode — lifecycle

**Diagramme boot flow** :

```
cli/runner.py::main()
    ├── _parse_args()               # incl. nouveau --force-resume
    ├── configure_logging()
    ├── if --force-resume: SentinelFile.clear()
    ├── halt_flag = SentinelFile(settings.remote_control_sentinel_path)
    └── asyncio.run(_async_main())
         └── _async_main()
              ├── init_db()
              ├── stop_event = asyncio.Event()
              ├── install_signal_handlers()
              ├── mode = "paused" if halt_flag.exists() else "normal"
              ├── log.info("polycopy_boot_mode", mode=mode, reason=halt_flag.reason())
              ├── orchestrators = _build_orchestrators(mode, session_factory, settings, queues)
              └── async with TaskGroup() as tg:
                    for orch in orchestrators:
                        tg.create_task(orch.run_forever(stop_event))
```

**Décision tranchée** : **TaskGroup unique avec helper `_build_orchestrators(mode, ...)`**. Raisons :
- Shutdown handler unique (`except*` inchangé).
- Pas de duplication de boilerplate (init_db, queues, signal handlers factorisés).
- Lecture plus simple : le mode décide du contenu de la liste, la mécanique reste identique.

**Tableau composants par mode** :

| Composant | Mode normal | Mode paused | Justification |
|---|---|---|---|
| `WatcherOrchestrator` | ✓ | ✗ | Pas de détection trades en paused (pas de signaux consommés) |
| `StrategyOrchestrator` | ✓ | ✗ | Pas de pipeline filtrage/sizing |
| `ExecutorOrchestrator` | ✓ | ✗ | Aucun ordre possible, donc aucune raison de démarrer (évite aussi le check RuntimeError auth L1/L2 en paused) |
| `MonitoringOrchestrator` | ✓ (complet) | ✓ (réduit) | Voir §4.2.1 ci-dessous |
| `DashboardOrchestrator` | ✓ (si enabled) | ✓ (si enabled) | Utile pour inspecter PnL/traders et décider `/resume` ou pas |
| `DiscoveryOrchestrator` | ✓ (si enabled) | ✗ | Pas de nouveaux wallets promus en paused |
| `LatencyPurgeScheduler` | ✓ (si enabled) | ✗ | Purge non critique, peut attendre resume |
| **`RemoteControlOrchestrator`** | ✓ (si enabled) | ✓ (mandatory) | Seule surface disponible pour `/resume` — **non-optionnel en paused** |

**§4.2.1 — `MonitoringOrchestrator` en mode réduit** :

En paused, `MonitoringOrchestrator.run_forever(stop_event, paused=True)` reçoit un flag. Impact :
- `PnlSnapshotWriter` : **OFF** (pas de calcul drawdown, pas de re-trigger kill switch alors que `halt.flag` est déjà posé).
- `AlertDispatcher` : **ON** (doit pouvoir envoyer l'alerte `paused_mode_entered` + heartbeats).
- `StartupNotifier` : **ON** (variante `startup_paused.md.j2` — nouveau template, ou discriminant dans `startup.md.j2` via `{% if paused %}`). Décision : discriminant dans `startup.md.j2` (diff minimal).
- `HeartbeatScheduler` : **ON** (mentionne `paused since <timestamp>` dans le message — enrichir `HeartbeatContext` avec `paused: bool`).
- `DailySummaryScheduler` : **OFF** (pas de trades ni décisions à résumer en paused).

**Règle safety** : si une coroutine détecte `halt_flag.exists() == True` au milieu de son cycle (rare, mais possible si `/stop` survient pendant un tick monitoring), elle log `monitoring_paused_inflight` et continue son tick courant puis attend le shutdown.

**§4.2.2 — Transition de modes** :

```
running --> /stop       -->  touch halt.flag  -->  stop_event.set  -->  exit  -->  [superviseur respawn]  -->  paused
running --> kill switch -->  touch halt.flag  -->  stop_event.set  -->  exit  -->  [superviseur respawn]  -->  paused
running --> /restart    -->                       stop_event.set  -->  exit  -->  [superviseur respawn]  -->  running (pas de sentinel)
paused  --> /resume     -->  rm halt.flag     -->  stop_event.set  -->  exit  -->  [superviseur respawn]  -->  running
paused  --> /restart    -->                       stop_event.set  -->  exit  -->  [superviseur respawn]  -->  paused  (sentinel toujours là)
```

Le respawn systématique via superviseur (cf. §4.8) garantit qu'on rebascule toujours dans le bon mode selon la présence du sentinel.

### 4.3 Handlers HTTP — contrat route-par-route

**Convention globale** :
- URL prefix : `/v1` (future-proof pour `/v2` non-breaking).
- Path param `<machine>` : case-insensitive, normalisé upper avant compare. Mismatch → **HTTP 404 body vide** (silence strict, zéro info leak).
- Response body : JSON, schema Pydantic `CommandResponse` ou `StatusResponse` (cf. `dtos.py`).
- Header obligatoire `X-Peer-Tailscale: <hostname>` pour identification logs (client la set via header ou on dérive de l'IP via `tailscale status --json`). Pas d'auth de ce header (informatif only).
- **Aucune réponse ne fuite** `MACHINE_ID`, `mode`, ou existence du bot à un caller non autorisé.

**Flow 2FA retenu** : **TOTP 1-call dans le body POST** (pas de challenge-response 2-call).

Raisons :
- Le code TOTP est déjà éphémère (30s) → la rejouabilité est limitée par construction, pas besoin d'ajouter un challenge serveur à vérifier ensuite.
- UX : 1 tap dans l'iOS Shortcut (lire TOTP dans 1Password/Authenticator → injecter dans body JSON → POST).
- Moins d'état serveur à gérer (pas de table "challenges en cours" à expirer).
- Durcissement : `pyotp.verify(valid_window=1)` ⇒ ±30s de tolérance clock skew, suffisant pour un phone synced NTP.

#### 4.3.1 `GET /v1/health`

Aucune auth, aucun TOTP. Liveness probe tailnet-only (bind Tailscale garantit déjà qu'aucun peer hors tailnet ne frappe ici).

Response 200 : `{"ok": true}`
Response 503 : `{"ok": false, "reason": "<paused|booting>"}`

Usage : Tailscale ACL health probe, tests intégration.

#### 4.3.2 `GET /v1/status/<machine>`

Pas de TOTP (lecture). Rate-limit sur l'IP source (10/min).

Réponse 200 `StatusResponse` :
```json
{
  "machine_id": "PC-FIXE",
  "mode": "running" | "paused",
  "uptime_seconds": 12345,
  "version": "1.2.0",
  "execution_mode": "live" | "dry_run" | "simulation",
  "heartbeat_index": 42,
  "positions_open": 3,
  "pnl_today_usdc": 12.34,
  "halt_reason": null | "kill_switch" | "manual_stop",
  "halted_since": null | "2026-04-19T23:45:00Z"
}
```
Réponse 404 (path `<machine>` ≠ `MACHINE_ID` local) : body vide.

#### 4.3.3 `POST /v1/restart/<machine>`

Body JSON : `{"totp": "123456"}` (string 6 chiffres). Schéma Pydantic strict.

Flow :
1. Vérif `<machine>` matche `MACHINE_ID` — sinon 404 body vide.
2. Vérif rate limiter IP — si dépassé, 429 + log `remote_control_rate_limited`.
3. Vérif TOTP via `pyotp.TOTP(secret).verify(code, valid_window=1)`.
4. Si échec : compteur lockdown++ (cf. §4.4 auto-lockdown). Retourne 401 `{"ok": false, "error": "invalid_totp"}`.
5. Si ok : `stop_event.set()` (pas de sentinel touch → respawn en mode normal). Retourne 202 `{"ok": true, "action": "restart", "respawn_eta_seconds": 5}`.

Log : `remote_control_command`, `command="restart"`, `machine_id=...`, `peer_ip=...`, `ok=true|false`.

#### 4.3.4 `POST /v1/stop/<machine>`

Body JSON : `{"totp": "123456"}`.

Flow identique `/restart` mais entre 4 et 5 :
- `sentinel.touch(reason="manual_stop")` **avant** `stop_event.set()`.
- Retour 202 `{"ok": true, "action": "stop", "respawn_eta_seconds": 5, "respawn_mode": "paused"}`.

#### 4.3.5 `POST /v1/resume/<machine>`

Body JSON : `{"totp": "123456"}`.

Flow identique `/stop` mais :
- `sentinel.clear()` (rm halt.flag) **avant** `stop_event.set()`.
- Pré-check : si `sentinel.exists() == False`, retourne 409 `{"ok": false, "error": "not_paused"}` (pas de 404, le user doit savoir que le bot tourne déjà).
- Retour 202 `{"ok": true, "action": "resume", "respawn_eta_seconds": 5, "respawn_mode": "running"}`.

**Décision POST (pas GET) pour /restart /stop /resume** — raisons :
1. Convention REST (effets de bord ≠ GET).
2. Prefetch browsers/reverse-proxies peuvent pré-charger un GET au hover → risque de déclenchement accidentel.
3. iOS Shortcuts supportent POST+body JSON nativement (zéro coût UX).
4. Logs plus propres : body en JSON plutôt que query string (moins de leak via access log si jamais on en ajoute un).

### 4.4 Sécurité & authn

#### 4.4.1 Résolution IP Tailscale au boot

Fonction `remote_control/tailscale.py::resolve_tailscale_ipv4()`:

```
if settings.remote_control_tailscale_ip_override is not None:
    return validate(settings.remote_control_tailscale_ip_override)

result = subprocess.run(
    ["tailscale", "ip", "-4"],
    capture_output=True, timeout=5.0, text=True,
)
if result.returncode != 0:
    raise RemoteControlBootError("tailscale_not_running", stderr=result.stderr)
ip = result.stdout.strip().splitlines()[0]  # première IPv4
if not is_tailscale_range(ip):  # 100.64.0.0/10
    raise RemoteControlBootError("tailscale_ip_not_in_cgnat_range", ip=ip)
return ip
```

Appelée exactement 1 fois au boot (cache dans `RemoteControlOrchestrator.__init__`).

#### 4.4.2 Binding strict

uvicorn : `host=<tailscale_ip>`, port `REMOTE_CONTROL_PORT`. **Jamais** `0.0.0.0` ni `127.0.0.1`. Test boot : si `host.startswith("127.")` ou `host == "0.0.0.0"` après résolution → crash explicite.

#### 4.4.3 `TOTPGuard` middleware

Vérification dans un dependency FastAPI (`Depends(verify_totp)`), pas un middleware global — seules les routes destructives l'invoquent.

```
def verify_totp(body: dict = Body(...), ip: str = Depends(get_peer_ip)) -> None:
    code = body.get("totp", "")
    if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
        raise HTTPException(400, detail="malformed")
    if not _rate_limiter.allow(ip):
        raise HTTPException(429, detail="rate_limited")
    if not pyotp.TOTP(settings.remote_control_totp_secret).verify(code, valid_window=1):
        _lockdown.record_failure(ip)
        raise HTTPException(401, detail="invalid_totp")
    _lockdown.record_success(ip)  # reset compteur
```

#### 4.4.4 Rate limiter in-memory

`collections.deque[float]` par IP source, sliding window 60 s, max 5 tentatives. Implémentation thread-safe pas nécessaire (asyncio single-thread).

#### 4.4.5 Auto-lockdown

État global par IP (dict[str, int] in-memory). **3 échecs TOTP consécutifs en 60 s** :
1. `sentinel.touch(reason="auto_lockdown_brute_force")`.
2. Émet `Alert(level="CRITICAL", event="remote_control_brute_force_detected", body=f"{failure_count} invalid TOTP attempts from {ip}", cooldown_key="remote_control_brute_force")` → dispatché via Telegram.
3. Retourne 423 Locked au caller.
4. Toutes les requêtes suivantes (tous IPs) sur `/stop` `/restart` `/resume` renvoient 423 jusqu'à respawn.

Recovery : user se connecte en SSH (ou ouvre un terminal physiquement), lance `rm ~/.polycopy/halt.flag && systemctl --user restart polycopy` (ou `polycopy --force-resume`).

#### 4.4.6 Logs — hygiène

Événements structlog émis par remote_control :
- `remote_control_boot` (avec `tailscale_ip`, `port`, pas de secret).
- `remote_control_request` (avec `route`, `peer_ip`, `status_code`).
- `remote_control_totp_verify` (`ok: bool`, `ip: str` — **jamais** le code ni le secret).
- `remote_control_command` (`command`, `machine_id`, `peer_ip`, `ok`).
- `remote_control_rate_limited` (`peer_ip`, `window_attempts`).
- `remote_control_auto_lockdown` (`peer_ip`, `failure_count`).
- `remote_control_sentinel_touched` / `remote_control_sentinel_cleared`.

**Test sécurité automatisé** : nouveau `tests/unit/test_remote_control_no_secret_leak.py` — grep le secret TOTP + le code 6-digit en dur dans tous les logs JSON capturés via `caplog`.

### 4.5 Intégration runner

Patch de [cli/runner.py](../../src/polycopy/cli/runner.py) :

#### 4.5.1 Parser args — ajout `--force-resume`

Dans `_parse_args()` (ligne ~45) :
```
parser.add_argument(
    "--force-resume",
    action="store_true",
    help="Supprime halt.flag avant boot (recovery urgence).",
)
```

#### 4.5.2 Handling `--force-resume`

Dans `main()`, **avant** `configure_logging()` (pour que le rm soit loggé dans le fichier juste après) :
```
if args.force_resume:
    SentinelFile(settings.remote_control_sentinel_path).clear(log=log)
```

#### 4.5.3 Bifurcation mode dans `_async_main()`

Remplacer le TaskGroup hard-codé (lignes 186-197) par :
```
sentinel = SentinelFile(settings.remote_control_sentinel_path)
mode = "paused" if sentinel.exists() else "normal"
log.info("polycopy_boot_mode", mode=mode, reason=sentinel.reason() if sentinel.exists() else None)

orchestrators = _build_orchestrators(
    mode=mode,
    session_factory=session_factory,
    settings=settings,
    detected_trades_queue=detected_trades_queue,
    approved_orders_queue=approved_orders_queue,
    alerts_queue=alerts_queue,
    sentinel=sentinel,
)

try:
    async with asyncio.TaskGroup() as tg:
        for orch in orchestrators:
            tg.create_task(orch.run_forever(stop_event))
except* asyncio.CancelledError:
    pass
```

`_build_orchestrators()` vit dans un nouveau `cli/boot.py` (extraction propre, testable unitairement sans passer par `asyncio.run`).

#### 4.5.4 Signal handlers

Inchangés. `SIGINT`/`SIGTERM` → `stop_event.set()` → exit → superviseur respawn en **même mode qu'avant** (sentinel non touché par les signaux OS).

### 4.6 Intégration `PnlSnapshotWriter`

Modif exacte dans [monitoring/pnl_writer.py](../../src/polycopy/monitoring/pnl_writer.py), dans la branche kill switch triggered (autour de `stop_event.set()` existant) :

**Avant** :
```
await self._emit_kill_switch_alert(drawdown_pct)
stop_event.set()
```

**Après** :
```
await self._emit_kill_switch_alert(drawdown_pct)
self._sentinel.touch(reason="kill_switch")  # NEW — AVANT stop_event.set
stop_event.set()
```

Idempotence : si crash entre `touch` et `stop_event.set()` (crash process, kill -9), le respawn superviseur trouvera le sentinel posé → mode paused correct. Inverse impossible (stop_event set avant touch) : on se réveillerait en mode normal malgré un drawdown → **unsafe** — d'où l'ordre obligatoire.

Injection du `sentinel` dans `PnlSnapshotWriter.__init__` via `MonitoringOrchestrator` (qui reçoit lui-même le sentinel dans son `__init__`, passé par `_build_orchestrators`).

**Template `kill_switch_triggered.md.j2`** — ajout d'une ligne :
```
🔁 _La machine redémarrera en mode_ *paused* _au respawn\._
_Utiliser_ `POST /v1/resume/<machine>` _depuis le téléphone pour reprendre\._
```

### 4.7 Dashboard Tailscale binding

#### 4.7.1 Changement `Settings`

Ajout de `dashboard_bind_tailscale: bool = Field(default=False)` dans `Settings`.

Validator (model_validator mode="after") :
- Si `dashboard_bind_tailscale=True` ET `dashboard_enabled=False` → warning `dashboard_bind_tailscale_without_enabled_noop` (pas fatal, juste informer que le flag n'a pas d'effet).
- Si `dashboard_bind_tailscale=True` ET `dashboard_host != "127.0.0.1"` (défaut) → warning `dashboard_host_overridden_by_tailscale_bind` (priorité Tailscale).

#### 4.7.2 Changement `DashboardOrchestrator`

Dans [dashboard/orchestrator.py](../../src/polycopy/dashboard/orchestrator.py), au boot (avant uvicorn.Server) :
```
if settings.dashboard_bind_tailscale:
    host = resolve_tailscale_ipv4(settings)  # même helper que remote_control
else:
    host = settings.dashboard_host
uvicorn.Config(app=..., host=host, port=settings.dashboard_port, ...)
```

**Pas de TOTP** sur le dashboard — il est read-only, conforme invariant M4.5/M6. La protection vient du binding `tailscale0` (réseau mesh privé = 1er facteur).

#### 4.7.3 Tests

- `tests/unit/test_dashboard_tailscale_binding.py` (nouveau) :
  - `DASHBOARD_BIND_TAILSCALE=true` + `resolve_tailscale_ipv4` mocké → uvicorn.Config appelé avec `host=<ip mockée>`.
  - `DASHBOARD_BIND_TAILSCALE=true` + Tailscale absent → `RemoteControlBootError` levé clair.
  - `DASHBOARD_BIND_TAILSCALE=false` → `host=settings.dashboard_host` inchangé.
- `tests/unit/test_dashboard_security.py` + `test_dashboard_security_m6.py` : passent identiques (le binding ne change pas les routes).
- Nouveau assert dans `test_dashboard_security_m6.py` : avec `DASHBOARD_BIND_TAILSCALE=true`, même grep secrets, même GET-only, même `docs_url=None`.

### 4.8 Superviseurs — artefacts

Tous les fichiers sous `scripts/supervisor/<os>/`. **Templates** — chaque fichier contient des placeholders `{{USER}}`, `{{POLYCOPY_PATH}}`, `{{VENV_PATH}}` — substitués par l'utilisateur lors du setup (pas de Jinja/CI, juste sed documenté dans le setup guide).

#### 4.8.1 `scripts/supervisor/systemd/polycopy.service`

```ini
[Unit]
Description=Polycopy — copy trading bot
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={{POLYCOPY_PATH}}
EnvironmentFile={{POLYCOPY_PATH}}/.env
ExecStart={{VENV_PATH}}/bin/python -m polycopy --no-cli
Restart=always
RestartSec=5s
StandardOutput=journal
StandardError=journal
# Sécurité
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
```

**`Restart=always`** (pas `on-failure`) — nécessaire car `/restart`/`/resume` font exit 0 mais doivent respawner.

Install : `~/.config/systemd/user/polycopy.service` + `loginctl enable-linger <user>` (pour survie à la déconnexion SSH) + `systemctl --user enable --now polycopy`.

#### 4.8.2 `scripts/supervisor/launchd/fr.polycopy.bot.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>fr.polycopy.bot</string>
    <key>ProgramArguments</key>
    <array>
        <string>{{VENV_PATH}}/bin/python</string>
        <string>-m</string>
        <string>polycopy</string>
        <string>--no-cli</string>
    </array>
    <key>WorkingDirectory</key><string>{{POLYCOPY_PATH}}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>KeepAlive</key><true/>
    <key>RunAtLoad</key><true/>
    <key>ThrottleInterval</key><integer>5</integer>
    <key>StandardOutPath</key><string>{{POLYCOPY_PATH}}/logs/launchd_stdout.log</string>
    <key>StandardErrorPath</key><string>{{POLYCOPY_PATH}}/logs/launchd_stderr.log</string>
</dict>
</plist>
```

Notes : macOS ne lit pas `.env` automatiquement — le fichier `.env` reste consommé par Pydantic Settings au démarrage Python. `EnvironmentVariables` ne contient que ce qui influence PATH/shell, pas les secrets.

Install : `~/Library/LaunchAgents/fr.polycopy.bot.plist` + `launchctl load ~/Library/LaunchAgents/fr.polycopy.bot.plist`.

#### 4.8.3 Windows 10 + WSL2

**Deux couches** :

**(a) Couche systemd** dans WSL2 Ubuntu — **identique Debian 12** (§4.8.1). Prérequis : WSL2 avec systemd activé (`wsl --update` + `/etc/wsl.conf` `[boot] systemd=true`). Setup guide §4 détaille.

**(b) Couche Windows Task Scheduler** — auto-start WSL2 au logon utilisateur.

Fichier `scripts/supervisor/windows/polycopy-wsl-autostart.xml` (exportable depuis Task Scheduler GUI) :

```xml
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{{WIN_USER}}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{{WIN_USER}}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <Hidden>true</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>wsl.exe</Command>
      <Arguments>-d Ubuntu --exec /bin/true</Arguments>
    </Exec>
  </Actions>
</Task>
```

`wsl.exe -d Ubuntu --exec /bin/true` démarre WSL2 (et donc systemd inside) puis sort. systemd prend le relais et démarre le service polycopy.

**Fallback `polycopy-wsl-respawn.ps1`** (pour WSL2 sans systemd — anciennes versions non patchées) :

```powershell
# scripts/supervisor/windows/polycopy-wsl-respawn.ps1
# Utilisé uniquement si systemd indisponible dans WSL2.
while ($true) {
    wsl.exe -d Ubuntu --exec bash -lc "cd {{POLYCOPY_PATH_WSL}} && {{VENV_PATH_WSL}}/bin/python -m polycopy --no-cli"
    Start-Sleep -Seconds 5
}
```

Appelé par Task Scheduler action `powershell.exe -File polycopy-wsl-respawn.ps1 -WindowStyle Hidden`.

**Décision** : **systemd WSL2 recommandé**, fallback PowerShell documenté pour les vieux WSL2. Setup guide §4 tranche avec user selon sa version.

### 4.9 Alertes Telegram — nouveaux event types

Trois nouveaux templates à créer dans `src/polycopy/monitoring/templates/` :

1. `remote_control_brute_force_detected.md.j2` — CRITICAL, alerte auto-lockdown.
2. `remote_control_command_executed.md.j2` — INFO, trace d'une commande exécutée (optionnel, feature flag `REMOTE_CONTROL_NOTIFY_COMMANDS: bool = True`).
3. `paused_mode_entered.md.j2` — WARNING, émis au boot si `halt.flag` détecté au démarrage.

Tous reçoivent le badge `MACHINE_ID` (Idée 1) automatiquement via `_inject_context`.

---

## 5. Plan d'implémentation phasé

Chaque phase = une branche `feat/m12bis-phase-<X>` + une PR squashée vers `main` + validation humaine avant phase suivante.

### Phase A — Idée 1 (badges MACHINE_ID)

**Commits attendus** (ordre) :
1. `feat(config): MACHINE_ID + MACHINE_EMOJI env vars with hostname fallback`
2. `feat(monitoring): inject machine_id/machine_emoji in AlertRenderer (M10 pattern)`
3. `feat(monitoring): update 15 templates with machine badge line`
4. `test(monitoring): machine_id fallback + badge in all templates`
5. `docs(claude): document MACHINE_ID as public/non-sensitive`

**Critères d'acceptation Phase A** :
- Tous tests Telegram M7 passent identiques.
- Avec `MACHINE_ID=PC-FIXE`, un boot fake en `execution_mode=dry_run` génère un startup message qui contient littéralement `🖥️ *PC\-FIXE*`.
- Sans `MACHINE_ID`, le hostname machine apparaît en majuscules.
- Smoke test CLI réel (1 run de 30s) : aucune régression.
- Phase A **peut ship seule** en v1.x (Idée 2 peut attendre).

### Phase B — Scaffold RemoteControlServer (read-only)

**Commits** :
1. `feat(remote_control): scaffold package + tailscale IP resolver`
2. `feat(remote_control): FastAPI skeleton + GET /v1/health + GET /v1/status/<machine>`
3. `feat(remote_control): RemoteControlOrchestrator wired in TaskGroup (opt-in, feature flag OFF in CI)`
4. `feat(config): REMOTE_CONTROL_ENABLED/PORT/TAILSCALE_IP_OVERRIDE`
5. `test(remote_control): status endpoint, 404 sur machine mismatch, rate limit basique`

**Critères d'acceptation Phase B** :
- `REMOTE_CONTROL_ENABLED=false` (default) → aucun diff observable (aucun import supplémentaire dans le path chaud).
- `REMOTE_CONTROL_ENABLED=true` sans Tailscale → crash boot clair.
- `REMOTE_CONTROL_ENABLED=true` avec override IP 127.0.0.1 → crash boot (refuse loopback explicit).
- GET `/v1/status/PC-FIXE` depuis un client local retourne 200 + JSON conforme schema.
- GET `/v1/status/OTHER` → 404 body vide.

### Phase C — TOTP + rate limit + lockdown

**Commits** :
1. `chore(deps): add pyotp`
2. `feat(remote_control): TOTPGuard dependency + pyotp verify`
3. `feat(remote_control): in-memory rate limiter (deque sliding 60s)`
4. `feat(remote_control): auto-lockdown 3-strikes → sentinel + Telegram alert`
5. `feat(remote_control): POST /v1/restart /v1/stop /v1/resume (sentinel stub, pas encore wired kill switch)`
6. `test(remote_control): TOTP edge cases (clock skew, replay, malformed), lockdown, no-leak`

**Critères d'acceptation Phase C** :
- `test_remote_control_no_secret_leak.py` passe.
- 3 échecs TOTP en ≤60s → sentinel présent + Telegram alert CRITICAL émise.
- TOTP valide → commande exécutée, stop_event set (observé dans test).
- Rate limit 5/min vérifié (6e tentative → 429).

### Phase D — Paused mode + PnlSnapshotWriter hook

**Commits** :
1. `refactor(cli): extract _build_orchestrators into cli/boot.py`
2. `feat(cli): halt.flag sentinel drives running|paused mode at boot`
3. `feat(cli): --force-resume flag`
4. `feat(monitoring): PnlSnapshotWriter touches sentinel before stop_event on kill switch`
5. `feat(monitoring): startup/heartbeat/kill_switch templates mention paused state`
6. `test(cli): boot mode bifurcation, paused mode component list, --force-resume`
7. `test(monitoring): kill switch → sentinel posed before stop_event (ordering)`

**Critères d'acceptation Phase D** :
- Kill switch déclenché en dry-run (fake PnL → drawdown > threshold) → respawn en paused (mock systemd via test).
- Bot en paused → seuls `MonitoringOrchestrator` (réduit) + `RemoteControlOrchestrator` + `DashboardOrchestrator` (si enabled) tournent — vérifié via liste des tasks du TaskGroup.
- `/resume` via HTTP → sentinel cleared → stop_event set → re-boot simulé → mode normal.
- `--force-resume` : sentinel présent → clear au boot → mode normal + log `sentinel_force_cleared`.

### Phase E — Dashboard Tailscale binding

**Commits** :
1. `feat(config): DASHBOARD_BIND_TAILSCALE flag + validator warnings`
2. `feat(dashboard): bind to tailscale0 IP when flag set, reuse remote_control resolver`
3. `test(dashboard): tailscale binding, fatal if Tailscale absent, security grep inchangé`

**Critères d'acceptation Phase E** :
- `DASHBOARD_BIND_TAILSCALE=true` + Tailscale UP → dashboard joignable depuis phone via `http://<hostname>.ts.net:8000/`.
- `DASHBOARD_BIND_TAILSCALE=true` + Tailscale DOWN → crash boot.
- Tests sécurité M4.5/M6 tous verts.

### Phase F — Artefacts superviseur + setup guide

**Commits** :
1. `feat(scripts): systemd unit template + install doc`
2. `feat(scripts): launchd plist template + install doc`
3. `feat(scripts): Windows Task Scheduler XML + PowerShell fallback`
4. `docs(setup): M12_bis_remote_control_setup_guide.md rédigé avec commandes testées`
5. `docs(claude): CLAUDE.md étendu avec invariants M12_bis`

**Critères d'acceptation Phase F** :
- Setup guide §11 smoke test passé end-to-end sur **au moins 1 des 3 OS** (preuve screenshot ou log extract).
- Troubleshooting §12 rempli avec 10 symptômes concrets rencontrés lors du setup.
- CLAUDE.md section Sécurité contient un paragraphe M12_bis final.

---

## 6. Critères d'acceptation globaux (check-list finale)

- [ ] `ruff check . && ruff format .` clean sur toutes les phases.
- [ ] `mypy --strict src` clean (remote_control inclus).
- [ ] `pytest` : 100 % vert, couverture `remote_control/` ≥ 85 %, couverture `monitoring/` sans régression (≥ niveau M12).
- [ ] `REMOTE_CONTROL_ENABLED=false` (default, scénario M12 utilisateur existant) : zéro impact, smoke test M12 inchangé.
- [ ] `REMOTE_CONTROL_ENABLED=true` sans `REMOTE_CONTROL_TOTP_SECRET` : boot fatal avec message clair.
- [ ] `REMOTE_CONTROL_ENABLED=true` sans Tailscale installé : boot fatal avec message clair.
- [ ] Smoke test end-to-end Tailscale local (fixture `tailscale_ip_override=100.64.0.1` + uvicorn TestClient) : `/status`, `/restart`, `/stop`, `/resume` passent le flow complet.
- [ ] Kill switch déclenché (mock PnL) → respawn en paused → heartbeat Telegram contient `🖥️ *PC-FIXE*` + mention paused → `/resume` HTTP → respawn mode normal.
- [ ] Setup guide `docs/specs/M12_bis_remote_control_setup_guide.md` publié avec sections 1-12 remplies.
- [ ] CLAUDE.md section "Sécurité" étendue avec invariants M12_bis (§remote_control binding, TOTP discipline, sentinel lifecycle).
- [ ] Tests sécurité automatisés : `test_remote_control_no_secret_leak.py`, `test_dashboard_tailscale_binding.py`, `test_machine_id_fallback.py` tous verts.

---

## 7. Risques & mitigations

| Risque | Probabilité | Impact | Mitigation |
|---|---|---|---|
| Échec résolution Tailscale IP au boot (daemon down) | Moyenne (redémarrage machine) | Critique (bot ne boote pas) | `Restart=always` systemd+5s retry → Tailscale finit par être up. Boot error explicite. Option `REMOTE_CONTROL_TAILSCALE_IP_OVERRIDE` pour bypass tests |
| `halt.flag` corrompu (permissions cassées, FS full) | Faible | Élevé (paused bloqué) | Sentinel read-only fallback (si read KO → log warning + assume not paused → mode normal). Option `--force-resume` CLI pour nettoyage urgence |
| Clock skew TOTP (phone desync NTP) | Faible | Moyen (user bloqué) | `valid_window=1` (±30s). Setup guide §12 troubleshooting TOTP |
| WSL2 Win10 sans systemd (anciennes versions) | Faible | Moyen (respawn manuel) | Fallback `polycopy-wsl-respawn.ps1` documenté. Setup guide §4 check version WSL |
| Tailscale hors ligne (panne réseau prolongée) | Faible | Bas (remote injoignable, trading OK) | Bot continue trading normal. Dashboard reste accessible localhost. Remote control servi mais injoignable depuis l'extérieur — acceptable |
| Fuite secret TOTP (commit accidentel `.env`) | Faible | Critique | Rotation trimestrielle documentée. `.gitignore` vérifié. Setup guide §9 warning |
| Conflit getUpdates futur (user change d'avis et veut Telegram incoming) | Faible | N/A | Hors scope M12_bis — tranché ADR §6 |
| Fuite `MACHINE_ID` compromet anonymat trading | Très faible | Bas | Hostname = déjà dans logs M9 du user. Aucune donnée sensible fuite |
| Auto-lockdown false positive (user tape mal 3×) | Faible | Moyen (doit SSH) | Rate limit avant lockdown évite le cas trivial. Recovery `--force-resume` documenté |

---

## 8. Annexe — Setup guide

Livrable séparé : [M12_bis_remote_control_setup_guide.md](./M12_bis_remote_control_setup_guide.md). Rempli **en fin de phase F** avec commandes et screenshots testés sur les 3 OS cibles.

---

## 9. Prompt d'implémentation

Prompt ready-to-paste pour lancer l'implémentation phase par phase :

```
Tu es l'implémenteur de la milestone M12_bis (polycopy). Les specs font autorité :

- Lecture obligatoire :
  - docs/specs/M12_bis_multi_machine_remote_control_spec.md
  - docs/specs/idea2_remote_control_decision.md (ADR)
  - CLAUDE.md (conventions + sécurité)

- Workflow strict :
  1. Tu implémentes phase par phase (A → F, cf. spec §5). Une phase = une branche
     `feat/m12bis-phase-<lettre>` + une PR squashée vers main.
  2. Avant chaque phase, tu initialises un TodoWrite avec les commits attendus
     listés dans §5 de la spec. Tu marques chaque todo completed dès que le
     commit est fait (pas de batch).
  3. Après chaque phase, tu lances :
     - `ruff check . && ruff format .`
     - `mypy --strict src`
     - `pytest`
     Tout doit être vert. Si rouge, tu corriges avant de me rendre la main.
  4. Tu ne passes PAS à la phase suivante sans ma validation explicite. Tu me
     dis "Phase A done, ready for review, go phase B ?" et tu attends.

- Règles code :
  - Python 3.11+ strict type hints, Pydantic v2, async partout, mypy --strict.
  - Pas de commentaires superflus (cf. CLAUDE.md).
  - Cite `path:line` dans les messages de commit quand tu touches du code existant.
  - Conventional commits (feat/fix/refactor/test/docs/chore).
  - Jamais de .env, token, ou secret committé. Vérifier .gitignore.
  - Écrire des tests AVANT ou EN MÊME TEMPS que le code, pas après.

- Sécurité (non-négociable) :
  - Secret TOTP jamais loggé (même partiellement, même en debug).
  - Binding uvicorn vérifié Tailscale-only (jamais 0.0.0.0 ni 127.0.0.1).
  - Invariants M7 §13 (Telegram emitter-only) et M4.5/M6 (dashboard read-only) préservés.
  - test_no_secret_leak.py automatisé sur chaque phase sensible.

- Décisions déjà tranchées (ne pas re-débattre) :
  - Emoji par machine via MACHINE_EMOJI (pas de mapping auto).
  - TaskGroup unique avec helper _build_orchestrators(mode).
  - POST pour routes destructives (pas GET).
  - TOTP 1-call dans body JSON (pas challenge-response 2-call).
  - Superviseur Restart=always (systemd) / KeepAlive=true (launchd).
  - Port 8765 remote_control, 8000 dashboard (inchangé).
  - systemd WSL2 recommandé, fallback PowerShell documenté.

- Démarrage :
  Commence par lire les 3 fichiers obligatoires ci-dessus, puis résume-moi en
  5 lignes ce que tu vas faire en Phase A (Idée 1, MACHINE_ID), crée la branche
  feat/m12bis-phase-a, initialise le TodoWrite avec les 5 commits de §5 Phase A,
  et attaque le premier commit. Rends-moi la main après Phase A pour review.
```

---

_Fin de la spec M12_bis. Prochaine étape : me lancer via le prompt §9._
