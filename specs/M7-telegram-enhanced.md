# M7 — Bot Telegram enrichi (heartbeat, résumé quotidien, templates soignés)

Spec d'implémentation de la **couche de communication Telegram** de polycopy. M4 a posé la fondation : `TelegramClient` httpx direct, `AlertDispatcher` avec cooldown in-memory par `event_type`, 4 events critiques (`kill_switch_triggered`, `executor_auth_fatal`, `executor_error`, `pnl_snapshot_drawdown`, `order_filled_large`). M5 a ajouté 4 events discovery (`trader_promoted`, `trader_demoted`, `discovery_cap_reached`, `discovery_cycle_failed`).

M7 transforme ce bot d'**alarme silencieuse** en **compagnon conversationnel** : heartbeat au démarrage, résumé quotidien configurable, templates Jinja2 soignés par event_type, lifebeat périodique 12 h, digest mode anti-spam. Le bot reste **emitter-only** (aucune commande entrante, cohérence M5 §13).

Source de vérité fonctionnelle : `docs/architecture.md` §Module Monitoring (étendu à M7). Conventions : `CLAUDE.md`. Code existant : `src/polycopy/monitoring/telegram_client.py` + `alert_dispatcher.py` + `dtos.py`. Spec de référence pour le ton : `specs/M4-monitoring.md` + `specs/M5-trader-scoring.md`.

---

## 0. Pré-requis environnement

### 0.1 Bootstrap (déjà fait)

`bash scripts/setup.sh` (idempotent). M7 n'ajoute **aucune dep Python nouvelle** — `jinja2` est déjà présent (introduit par M4.5 dashboard). `httpx` est déjà en place. Pas de patch config structurel.

### 0.2 Pas d'invocation skill Polymarket

M7 est 100 % couche Telegram + templating. Aucun endpoint Polymarket nouveau consommé.

### 0.3 `.env` — nouvelles variables (toutes OPTIONNELLES)

| Variable env | Champ Settings | Default | Description |
|---|---|---|---|
| `TELEGRAM_HEARTBEAT_ENABLED` | `telegram_heartbeat_enabled` | `false` | Opt-in pour le heartbeat périodique (§2.4). Si `false` → aucun heartbeat émis. |
| `TELEGRAM_HEARTBEAT_INTERVAL_HOURS` | `telegram_heartbeat_interval_hours` | `12` | Intervalle entre 2 heartbeats silencieux. `Field(ge=1, le=168)` (1 h à 7 j). |
| `TELEGRAM_DAILY_SUMMARY` | `telegram_daily_summary` | `false` | Opt-in pour le résumé quotidien (§2.2). |
| `TG_DAILY_SUMMARY_HOUR` | `tg_daily_summary_hour` | `9` | Heure locale (`[0, 23]`) d'envoi du résumé quotidien. `Field(ge=0, le=23)`. |
| `TG_DAILY_SUMMARY_TIMEZONE` | `tg_daily_summary_timezone` | `"Europe/Paris"` | Nom IANA (ex: `Europe/Paris`, `America/New_York`). Validé via `zoneinfo.ZoneInfo(...)` au boot. |
| `TELEGRAM_STARTUP_MESSAGE` | `telegram_startup_message` | `true` | Si `true` ET `TELEGRAM_BOT_TOKEN` défini → envoie un message de démarrage avec statut des modules (§2.1). |
| `TELEGRAM_DIGEST_THRESHOLD` | `telegram_digest_threshold` | `5` | Nombre d'alertes du même event_type dans 1 h pour activer le digest mode (§2.5). `Field(ge=2, le=100)`. |
| `TELEGRAM_DIGEST_WINDOW_MINUTES` | `telegram_digest_window_minutes` | `60` | Fenêtre glissante pour compter les alertes avant digest. `Field(ge=5, le=1440)`. |

À ajouter à `config.py` ET `.env.example` avec commentaires sécurité pour `TELEGRAM_*` (discipline M4 préservée : aucun token loggé, aucune URL en clair).

**Variables M4 inchangées** : `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ALERT_COOLDOWN_SECONDS`, `ALERT_LARGE_ORDER_USD_THRESHOLD`. Backwards compat : un `.env` M4 sans les 8 nouvelles vars fonctionne — tous les defaults sont `false` ou `0`-effect.

### 0.4 Interdépendance avec les autres specs post-M5

- **M6 (dashboard 2026)** : M7 émet un message de démarrage qui contient un lien vers le dashboard (`http://127.0.0.1:8787/`). Si `DASHBOARD_ENABLED=false`, le lien est omis. Pas de dépendance forte — M7 peut shipper avant ou après M6.
- **M8 (dry-run réaliste)** : M7 enrichit le résumé quotidien avec les metrics PnL virtuel si M8 actif. Hook optionnel (le template Jinja gère `is None`). Aucun blocage.
- **M9 (CLI silencieux + README)** : le README tutorial M9 documente l'activation Telegram pas-à-pas. M7 doit shipper **avant** M9 (ou en parallèle) pour que les captures README soient complètes. **Recommandation d'ordre : M9 → M6 → M8 → M7**, mais M7 peut glisser avant M8 sans conséquence.

**Conclusion** : M7 est le plus **indépendant** des 4 specs post-M5. Aucune dépendance forte.

### 0.5 Critère de validation "environnement"

```bash
TELEGRAM_BOT_TOKEN=<token> TELEGRAM_CHAT_ID=<chat> \
TELEGRAM_STARTUP_MESSAGE=true \
TELEGRAM_HEARTBEAT_ENABLED=true TELEGRAM_HEARTBEAT_INTERVAL_HOURS=1 \
TELEGRAM_DAILY_SUMMARY=true TG_DAILY_SUMMARY_HOUR=<heure_dans_5_min> \
python -m polycopy --dry-run
```

Doit logger :

- `telegram_startup_sent` dans les 3 premières secondes (un message arrive côté Telegram avec la liste des modules + lien dashboard).
- `telegram_heartbeat_sent` toutes les 1 h (configuré).
- `telegram_daily_summary_sent` à l'heure configurée (± 1 min).
- `alert_sent` sur les alertes classiques (inchangé M4).
- `alert_digest_sent` si ≥ 5 alertes du même type en 1 h.

Aucun de ces logs ne doit contenir de token ni chat_id en clair.

### 0.6 Sécurité — rappels stricts pour M7

**Invariants M4 préservés** :

- **`TELEGRAM_BOT_TOKEN` jamais loggé**, même partiellement, même en `DEBUG`. Vérifié par grep automatisé en test (§9.6).
- **URL `api.telegram.org/bot<TOKEN>/sendMessage` jamais loggée** en clair. httpx defaults = pas de log URL mais on audit.
- **Pas de persist en DB** des messages envoyés à M7 (reste éphémère, cf. M4 §13). Si ajoute `telegram_events` table à M8+ pour audit → nouveau milestone.
- **Rotation token** : documentée dans `docs/setup.md` §16 (nouvelle section M7) → "tous les 6 mois ou si soupçon de compromission, régénérer via BotFather `/token`".
- **Bypass silencieux** (M4) : si `TELEGRAM_BOT_TOKEN` ou `TELEGRAM_CHAT_ID` absent → toutes les features M7 sont inactives sans crash (startup message, heartbeat, daily summary, digest). Identique à M4 `TelegramClient.enabled`.
- **Rate limit Telegram Bot API** : 30 messages/second par bot, 20 msgs/minute par chat. M7 au pic envoie ~2 msg/min (startup + daily + digest + alerts), largement safe. Documenter le seuil.
- **HTTPS exclusif** : httpx default + hardcoded `https://api.telegram.org/...`. Aucun fallback HTTP.
- **Templates Jinja2 autoescape=False** pour Markdown v2 (sinon `*bold*` devient `&lt;b&gt;`). **Mais** : escape explicite des valeurs user-controlled via `telegram_md_escape(value)` filter (§7.5 piège n°3).
- **Pas de dépendance à une lib Telegram externe** (pas de `python-telegram-bot`, pas d'`aiogram`). M4 utilise httpx direct, M7 continue — moins de surface d'attaque, update trivial si Bot API évolue.

---

## 1. Objectif M7 (scope exact)

Faire passer la communication Telegram de **4-9 alertes critiques** à un **flux structuré + personnalisé** qui donne confiance au user sur la santé du bot sans le spammer.

Livrable fonctionnel :

- **Startup message** (`TELEGRAM_STARTUP_MESSAGE=true`) : à chaque `python -m polycopy`, un message formaté envoyé 1 fois avec mode, version, wallets pinned, modules actifs, lien dashboard.
- **Daily summary** (`TELEGRAM_DAILY_SUMMARY=true`) : à l'heure `TG_DAILY_SUMMARY_HOUR` de `TG_DAILY_SUMMARY_TIMEZONE`, un résumé structuré (trades 24h, PnL, ordres, positions, discovery, alertes résumées).
- **Heartbeat périodique** (`TELEGRAM_HEARTBEAT_ENABLED=true`) : toutes les `N` heures, un ping minimaliste "✅ polycopy tourne depuis Xh".
- **Templates Jinja2 soignés** (`src/polycopy/monitoring/templates/`) : 1 template `.md.j2` par `event_type` — tous les messages d'alerte passent par un template au lieu d'un f-string inline. Templates surchargeables via `assets/telegram/` user-land.
- **Digest mode** : si ≥ `TELEGRAM_DIGEST_THRESHOLD=5` alertes du même `event_type` dans `TELEGRAM_DIGEST_WINDOW_MINUTES=60` → batch en un seul message "5 alertes X dans la dernière heure" avec count par sous-sous-type éventuel.
- **Shutdown message** : à l'extinction propre (SIGINT), bref "🛑 polycopy arrêté — durée de run 2h30, 0 ordres passés".
- **Tests** : mock respx sur `api.telegram.org`, rendu templates sur fixtures, cooldown + digest, startup formatting.

**Hors livrable M7** :

- **Aucune commande entrante** (`/score 0xabc`, `/pause`, `/resume`). Cohérence M5 §13. Si jamais utile → milestone séparé avec `TelegramUpdater` + signature vérification + CSRF-like token. Décision explicite : **fermé jusqu'à nouvel ordre.**
- **Aucune persistance DB** des messages envoyés. Restent éphémères (cf. M4 §13).
- **Pas d'interface multi-chat** (1 bot = 1 `TELEGRAM_CHAT_ID`). Si un user veut un chat groupe + un chat perso → M7.1.
- **Pas de retry queue persistée** sur panne Telegram. Un message qui rate → loggué comme erreur, pas retenté (idempotence difficile à garantir sur un résumé).
- **Pas de refactor de l'`AlertDispatcher`** existant M4. M7 étend via composition (nouveau `AlertRenderer` + nouveau `HeartbeatScheduler` + nouveau `DailySummaryScheduler` coordonnés par le même orchestrator M4).
- **Pas de traduction i18n** (templates FR uniquement, cohérent `CLAUDE.md` docstrings FR).

---

## 2. Arbitrages techniques (7 points à trancher explicitement)

### 2.1 Startup message — comment trigger, quel contenu

**Recommandation : message envoyé depuis `MonitoringOrchestrator.run_forever` au tout début du TaskGroup, avant le polling watcher/strategy/executor.**

Contenu (rendu via template `startup.md.j2`) :

```
🤖 *polycopy démarré*

_Mode :_ dry-run
_Version :_ 0.7.0 \\(abcd1234\\)
_Heure :_ 2026\\-04\\-18 14:32 UTC

*Wallets suivis \\(pinned\\)* :
• `0xabc…def` Smart Money #1
• `0xdef…abc` Polymarket Jean

*Modules actifs* :
✅ Watcher \\(3 wallets\\)
✅ Strategy \\(4 filtres\\)
✅ Executor \\(simulé\\)
✅ Monitoring \\(Telegram ON, PnL 5 min\\)
✅ Dashboard \\(http://127\\.0\\.0\\.1:8787\\)
✅ Discovery \\(6 h cycle, v1\\)

_Bot accessible_ ✓
```

Pros :

- Premier signal "le bot tourne" visible de n'importe où.
- Version git SHA = debugabilité au cas où l'user a plusieurs instances.
- Lien dashboard = 1 clic pour superviser.
- Markdown v2 Telegram pour bold/italique/monospace.

Cons :

- Un restart fréquent (ex: crash loop) = spam. **Mitigation** : cooldown in-memory partagé avec l'`AlertDispatcher` — même cooldown_key `"startup_message"`, TTL = `ALERT_COOLDOWN_SECONDS * 5 = 5 min` par défaut. Si user veut forcer → `ALERT_COOLDOWN_SECONDS=0` temporairement.

**Alternatives écartées** :

- **Message 100 % ASCII sans emoji** : plus sobre mais moins scannable. Rejeté (l'icône ✅ par module rend la lecture plate).
- **Inclure tous les seuils (MAX_POSITION_USD, MAX_SLIPPAGE_PCT, etc.)** : trop long, noie le signal. Préférer "résumé exécutif" + lien dashboard pour le détail.
- **Envoyer startup message aussi en `dry-run`** : oui (déjà le cas par défaut). Cohérent — le user veut savoir que son runs dry-run a bien démarré.
- **Ne pas envoyer startup si `DRY_RUN=true`** : non — un user qui teste une nouvelle config dry-run veut la confirmation.

### 2.2 Daily summary — scheduler APScheduler vs `asyncio.sleep` natif

**Recommandation : `asyncio.sleep` calculé vers le prochain `TG_DAILY_SUMMARY_HOUR` + TZ, pas de dep APScheduler.**

Algorithme :

```python
def _next_summary_at(now: datetime, hour: int, tz: ZoneInfo) -> datetime:
    """Retourne le prochain datetime UTC correspondant à l'heure locale cible."""
    local_now = now.astimezone(tz)
    target_local = local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target_local <= local_now:
        target_local += timedelta(days=1)
    return target_local.astimezone(UTC)


async def run_forever(self, stop_event):
    while not stop_event.is_set():
        next_at = _next_summary_at(utc_now(), self.hour, self.tz)
        delta_s = max(1.0, (next_at - utc_now()).total_seconds())
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delta_s)
            return  # stop_event set → sortie propre
        except TimeoutError:
            pass
        if stop_event.is_set():
            return
        try:
            await self._send_summary()
        except Exception:
            log.exception("telegram_daily_summary_failed")
            # pas de retry — au prochain jour
```

Pros :

- Zéro nouvelle dep (APScheduler = 250 KB + async-compat modéré).
- Contrôle fin du lifecycle (stop_event interruptible).
- Testable : on mocke `utc_now()` + `asyncio.wait_for` via `freezegun` ou `pytest-asyncio`.

Cons :

- DST transitions : 2h → 3h saut (printemps) ou 3h → 2h double (automne). **Solution** : recalcul `_next_summary_at` à chaque itération (pas de cache). Le `timedelta(days=1)` + reconversion TZ gère correctement le saut (ZoneInfo aware).
- Un crash DB pendant `_send_summary` ne re-tente pas — à M7, accepté.

**Alternatives écartées** :

- **APScheduler** : overkill pour 1 schedule.
- **cron système** : perd `stop_event`, config externe, moins ergonomique.
- **Trigger sur `PnlSnapshotWriter` tick** : couple 2 responsabilités différentes.

### 2.3 Heartbeat périodique — intervalle, formatage, cooldown

**Recommandation : boucle simple `asyncio.wait_for(stop, timeout=H*3600)`, message minimaliste.**

Template `heartbeat.md.j2` :

```
💚 *polycopy actif*
Depuis _12h03_ \\(heartbeat #5\\)
_{{ watcher_count }} wallets, {{ positions_open }} positions ouvertes_
_Silencieux, aucune alerte sur la fenêtre._
```

Cadence `TELEGRAM_HEARTBEAT_INTERVAL_HOURS=12` par défaut (2x/jour). Utile pour détecter une panne process ("plus d'heartbeat depuis 24h → quelque chose cloche").

**Cooldown & silence** : si une alerte critique (`kill_switch_triggered`, `executor_auth_fatal`) a été envoyée dans les `heartbeat_interval / 2` dernières heures → **skip** le heartbeat (le user sait déjà que le bot vit, pas besoin de renchérir). Évite la dissonance "🚨 kill switch" puis 5 min plus tard "💚 polycopy actif".

**Alternatives écartées** :

- **Heartbeat 1 h** : trop verbeux, flood de notifications.
- **Heartbeat 24 h** : trop laxiste, on ne détecte pas une panne de 20 h.
- **Heartbeat mixé avec `pnl_snapshot_drawdown` warning** : mélange 2 signaux différents, confusant.

### 2.4 Templates Jinja2 — structure, surcharge user-land

**Recommandation : Jinja2 `FileSystemLoader` avec 2 chemins en cascade — `assets/telegram/` (user overrides) puis `src/polycopy/monitoring/templates/` (defaults).**

Avantages :

- Séparation données ↔ rendering : l'`AlertDispatcher` construit un contexte dict, le template consomme.
- User peut overrider sans fork : créer `assets/telegram/kill_switch_triggered.md.j2` shadow le default.
- Autoescape **désactivé** (Markdown v2 Telegram incompatible avec HTML escape) + helper `telegram_md_escape` pour échapper les `_*[]()~` dans les valeurs user-controlled (slugs, labels wallet).

Templates livrés à M7 (`src/polycopy/monitoring/templates/`) :

| Template | Event | Utilisation |
|---|---|---|
| `startup.md.j2` | — | `TELEGRAM_STARTUP_MESSAGE` au boot. |
| `shutdown.md.j2` | — | SIGINT shutdown propre. |
| `heartbeat.md.j2` | — | Heartbeat N-heures. |
| `daily_summary.md.j2` | — | Résumé quotidien. |
| `digest.md.j2` | — | Batch d'alertes du même type en fenêtre. |
| `kill_switch_triggered.md.j2` | `kill_switch_triggered` | CRITICAL. Corps large, actions. |
| `executor_auth_fatal.md.j2` | `executor_auth_fatal` | CRITICAL. CLOB auth failed. |
| `executor_error.md.j2` | `executor_error` | ERROR. Exception order. |
| `pnl_snapshot_drawdown.md.j2` | `pnl_snapshot_drawdown` | WARNING. 75% kill switch. |
| `order_filled_large.md.j2` | `order_filled_large` | INFO. Large fill. |
| `trader_promoted.md.j2` | `trader_promoted` | INFO (M5). |
| `trader_demoted.md.j2` | `trader_demoted` | WARNING (M5). |
| `discovery_cap_reached.md.j2` | `discovery_cap_reached` | WARNING (M5). |
| `discovery_cycle_failed.md.j2` | `discovery_cycle_failed` | ERROR (M5). |
| `fallback.md.j2` | — | Template default si `event_type` n'a pas de template dédié (preserves M4 compat). |

Total : 15 templates. ~50-100 lignes chacun. Tous sont **autonomes** (pas de `{% include %}` complexe, juste éventuellement un `{% from 'partials.md.j2' import badge %}` factorisé).

**Alternatives écartées** :

- **f-string inline dans `AlertDispatcher`** (situation actuelle M4) : rapide pour 4 events, devient ingérable à 15+. Templates = séparation claire.
- **Python `string.Template`** : pas de conditions ni boucles, insuffisant pour le daily summary.
- **`.md` files + placeholders maison** : réinvente Jinja.

### 2.5 Digest mode — éviter le spam

**Recommandation : compteur in-memory glissant par `event_type`, batch après threshold atteint.**

Algorithme :

```python
class AlertDigestWindow:
    """Compte les alertes par event_type dans une fenêtre glissante."""

    def __init__(self, window_seconds: int, threshold: int):
        self.window = window_seconds
        self.threshold = threshold
        self._buckets: dict[str, deque[tuple[datetime, Alert]]] = defaultdict(deque)

    def register(self, alert: Alert, now: datetime) -> DigestDecision:
        """Enregistre + décide : emit_single / hold_for_digest / emit_digest_now."""
        q = self._buckets[alert.event]
        q.append((now, alert))
        # Purge out-of-window
        cutoff = now - timedelta(seconds=self.window)
        while q and q[0][0] < cutoff:
            q.popleft()
        # Décide
        if len(q) < self.threshold:
            return DigestDecision(action="emit_single", count=len(q))
        return DigestDecision(action="emit_digest", count=len(q))
```

Flux :

1. Alerte arrive dans `AlertDispatcher._handle(alert)`.
2. Si `cooldown_key` présent → check cooldown M4 (inchangé).
3. Si passe cooldown → check digest window.
4. Si `emit_single` → rendu via template normal, POST Telegram.
5. Si `emit_digest` → purge la queue de cet event_type, rendu via `digest.md.j2` avec count, POST Telegram. **Reset du compteur** pour cet event_type (prochain tick redémarre à 0).

Exemple digest rendu :

```
📬 *Digest alertes polycopy*
_7 alertes `order_filled_large` dans la dernière heure_

• 0xabc… "Trump 2028" — $120
• 0xdef… "NYC Mayor" — $85
• 0x123… "NBA Finals" — $95
• \\.\\.\\. et 4 autres

_Voir le dashboard pour le détail_: http://127\\.0\\.0\\.1:8787/orders
```

**Alternatives écartées** :

- **Pas de digest** : spam sur marchés volatils (ex: breaking news = 15 `order_filled_large` en 10 min).
- **Digest par cooldown_key au lieu d'`event_type`** : casserait la granularité M4 (un user pinned à haute activité n'émet pas les mêmes keys).
- **Digest persisté en DB** : reporté (stateful = complexité, pas besoin pour M7).
- **Digest fenêtre = `ALERT_COOLDOWN_SECONDS`** : ça confond 2 concepts (cooldown = "pas deux fois le même exactement", digest = "trop = trop").

### 2.6 Architecture module — extension composite de `MonitoringOrchestrator`

**Recommandation : ajouter 3 nouveaux composants co-orchestrés par `MonitoringOrchestrator` (même TaskGroup interne que M4 `PnlSnapshotWriter` + `AlertDispatcher`).**

- `StartupNotifier` : un-shot au boot, envoie via `TelegramClient` le template `startup.md.j2`.
- `HeartbeatScheduler` : boucle `asyncio.wait_for(stop, timeout=H*3600)`, envoie `heartbeat.md.j2`.
- `DailySummaryScheduler` : calcule prochain `TG_DAILY_SUMMARY_HOUR`, boucle `asyncio.wait_for`, envoie `daily_summary.md.j2`.

Tous consomment `TelegramClient` (inchangé M4) + `AlertRenderer` (nouveau).

`AlertDispatcher` M4 est **étendu** (pas refactoré) : il gagne un `AlertRenderer` en injection et utilise le template au lieu du f-string inline. Backwards compat : si un `event_type` n'a pas de template dédié → fallback template `fallback.md.j2` qui reproduit l'ancien format M4.

**Règle de dépendance** :

```
monitoring/ → storage (read-only pour daily summary)
monitoring/ → config
monitoring/ → (pas de dep vers watcher/strategy/executor/dashboard/discovery directement)
```

Identique au contrat M4. Les données pour le daily summary sont obtenues via **queries SQL directes** (même `session_factory` que `PnlSnapshotWriter`).

### 2.7 Daily summary content — quoi agréger, quoi omettre

**Recommandation : sections en bullet list compact, max 25 lignes visibles (au-delà, Telegram coupe).**

Contenu ordonné (template `daily_summary.md.j2`) :

```
🗓 *polycopy — résumé du {{ date_human }}*

*🔍 Détection*
• Trades détectés : {{ trades_24h }}
• Top 3 wallets actifs :
  — {{ top_wallets[0] }}
  — {{ top_wallets[1] }}
  — {{ top_wallets[2] }}

*🎯 Stratégie*
• Décisions : {{ decisions_approved }} approuvées · {{ decisions_rejected }} rejetées
• Raisons top rejet : {{ top_reject_reason }}

*💼 Exécution*
• Ordres : {{ orders_sent }} envoyés · {{ orders_filled }} remplis · {{ orders_rejected }} rejetés
• Volume exécuté : ${{ volume_executed_usd }}

*📈 PnL*
• Total USDC : ${{ total_usdc }} \\(_{{ delta_24h_pct }} vs hier_\\)
• Drawdown max 24 h : {{ drawdown_24h_pct }}
• Positions ouvertes : {{ positions_open }} \\(valeur ${{ positions_value_usd }}\\)

*🧭 Discovery* \\(si M5 actif\\)
• Cycles : {{ discovery_cycles_24h }}
• Promotions : {{ discovery_promotions_24h }}
• Demotions : {{ discovery_demotions_24h }}
• Cap atteint : {{ discovery_cap_reached_24h }} fois

*🔔 Alertes*
• Total 24 h : {{ alerts_total_24h }}
• Par type : {{ alerts_by_type_compact }}

_Dashboard_ : http://127\\.0\\.0\\.1:8787/
```

Pros :

- Un bullet par section → parseable à l'œil en 15 s.
- Lien dashboard en bas → drill-down possible.
- Sections conditionnelles (`{% if discovery_enabled %}`, `{% if telegram_chat_id and dashboard_enabled %}`).

**Alternatives écartées** :

- **Graphique ASCII pour PnL** : peu lisible sur Telegram mobile.
- **Inclure la liste des 10 derniers ordres** : trop long, déjà accessible via dashboard.
- **Envoi en plusieurs messages** : coupe l'expérience "1 notification = 1 résumé".
- **HTML au lieu de Markdown v2** : Telegram support les deux. Markdown reste plus lisible en source + facile à échapper.

---

## 3. Arborescence du module — `src/polycopy/monitoring/`

```
src/polycopy/monitoring/
├── __init__.py                       (inchangé)
├── dtos.py                           (+ DigestDecision, StartupContext, DailySummaryContext, HeartbeatContext)
├── orchestrator.py                   (étendu — coordonne les 3 nouveaux schedulers)
├── telegram_client.py                (inchangé)
├── alert_dispatcher.py               (étendu — consomme AlertRenderer + AlertDigestWindow)
├── pnl_writer.py                     (inchangé)
├── alert_renderer.py                 NOUVEAU : Jinja2 loader + render_alert + helpers
├── alert_digest.py                   NOUVEAU : AlertDigestWindow + DigestDecision
├── startup_notifier.py               NOUVEAU : un-shot startup message
├── heartbeat_scheduler.py            NOUVEAU : boucle heartbeat N-hours
├── daily_summary_scheduler.py        NOUVEAU : scheduler hour-of-day TZ-aware
├── daily_summary_queries.py          NOUVEAU : aggregate queries (24h counts from DB)
├── md_escape.py                      NOUVEAU : helper escape Markdown v2 Telegram
└── templates/
    ├── startup.md.j2                 NOUVEAU
    ├── shutdown.md.j2                NOUVEAU
    ├── heartbeat.md.j2               NOUVEAU
    ├── daily_summary.md.j2           NOUVEAU
    ├── digest.md.j2                  NOUVEAU
    ├── fallback.md.j2                NOUVEAU (backward compat format M4)
    ├── kill_switch_triggered.md.j2   NOUVEAU
    ├── executor_auth_fatal.md.j2     NOUVEAU
    ├── executor_error.md.j2          NOUVEAU
    ├── pnl_snapshot_drawdown.md.j2   NOUVEAU
    ├── order_filled_large.md.j2      NOUVEAU
    ├── trader_promoted.md.j2         NOUVEAU (M5)
    ├── trader_demoted.md.j2          NOUVEAU (M5)
    ├── discovery_cap_reached.md.j2   NOUVEAU (M5)
    ├── discovery_cycle_failed.md.j2  NOUVEAU (M5)
    └── partials/
        └── common_partials.md.j2     NOUVEAU (macros partagées : wallet_short, dashboard_link)

assets/
└── telegram/                          NOUVEAU — overrides user-land optionnels
    └── README.md                      NOUVEAU — documente le mécanisme de surcharge
```

Total : **6 nouveaux fichiers Python** + **16 nouveaux templates** + **2 fichiers user-land** (`assets/telegram/README.md` + stub).

---

## 4. Structure des DTOs ajoutés — `dtos.py`

### 4.1 `DigestDecision`

```python
class DigestDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Literal["emit_single", "hold_for_digest", "emit_digest"]
    count: int
    event_type: str
    representative_alert: Alert | None  # Pour emit_digest : une alerte repr pour le titre
```

### 4.2 `StartupContext`

```python
class StartupContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str                          # "0.7.0 (abcd1234)"
    mode: Literal["dry-run", "live"]
    boot_at: datetime
    pinned_wallets: list[PinnedWallet]    # [{address_short, label}]
    modules: list[ModuleStatus]           # ordered
    dashboard_url: str | None             # None si DASHBOARD_ENABLED=false
    discovery_summary: DiscoverySummaryForStartup | None


class ModuleStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str                             # "Watcher"
    enabled: bool
    detail: str                           # "3 wallets" ou "désactivé"


class PinnedWallet(BaseModel):
    model_config = ConfigDict(frozen=True)

    wallet_short: str                     # "0xabc…def"
    label: str | None
```

### 4.3 `HeartbeatContext`

```python
class HeartbeatContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    uptime_human: str                     # "12 h 03 min"
    heartbeat_index: int                  # incrementé en in-memory
    watcher_count: int                    # count pinned + active wallets
    positions_open: int
    critical_alerts_in_window: int        # si > 0 → skip heartbeat
```

### 4.4 `DailySummaryContext`

```python
class DailySummaryContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    date_human: str                                # "18 avril 2026"
    trades_24h: int
    top_wallets: list[TopWalletEntry]              # 3 items
    decisions_approved: int
    decisions_rejected: int
    top_reject_reason: str | None
    orders_sent: int
    orders_filled: int
    orders_rejected: int
    volume_executed_usd: float
    total_usdc: float | None
    delta_24h_pct: float | None
    drawdown_24h_pct: float | None
    positions_open: int
    positions_value_usd: float
    discovery_enabled: bool
    discovery_cycles_24h: int
    discovery_promotions_24h: int
    discovery_demotions_24h: int
    discovery_cap_reached_24h: int
    alerts_total_24h: int
    alerts_by_type_compact: str                    # "drawdown:2 · filled:5"
    dashboard_url: str | None


class TopWalletEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    wallet_short: str
    label: str | None
    trade_count: int
```

### 4.5 `DigestContext`

```python
class DigestContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_type: str
    count: int
    window_minutes: int
    level: AlertLevel
    sample_lines: list[str]                        # max 4 lignes d'exemple
    truncated_count: int                           # count - 4 si > 4
    dashboard_url: str | None
```

---

## 5. Storage — aucune modification

M7 ne touche pas aux modèles SQLAlchemy ni aux migrations Alembic. Aucune nouvelle table, aucune nouvelle colonne.

**Tables lues à M7** (read-only, queries agrégation 24 h) :

- `detected_trades` → `trades_24h`, `top_wallets`.
- `strategy_decisions` → `decisions_approved/rejected`, `top_reject_reason`.
- `my_orders` → `orders_sent/filled/rejected`, `volume_executed_usd`.
- `my_positions` → `positions_open`, `positions_value_usd`.
- `pnl_snapshots` → `total_usdc`, `delta_24h_pct`, `drawdown_24h_pct`.
- `trader_events` (M5) → `discovery_*_24h`.
- `target_traders` (M5) → `pinned_wallets` pour startup message.

**Aucune persistance** des messages envoyés (cf. §13 hors scope). Si besoin audit → nouvelle table `telegram_events` reportable M7.1.

---

## 6. Queries agrégées — `daily_summary_queries.py`

Tous les agrégats 24 h en 1 session SQLAlchemy, lecture seule :

```python
async def collect_daily_summary_context(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    since: datetime,
) -> DailySummaryContext: ...
```

Queries internes :

```python
async def _count_trades_since(session, since) -> int: ...
async def _top_wallets_since(session, since, limit=3) -> list[TopWalletEntry]: ...
async def _decisions_stats_since(session, since) -> tuple[int, int, str | None]: ...
async def _orders_stats_since(session, since) -> OrderStats: ...
async def _positions_current(session) -> PositionStats: ...
async def _pnl_delta_since(session, since) -> PnlDeltaStats | None: ...
async def _discovery_stats_since(session, since) -> DiscoveryStats: ...
async def _alerts_type_counts_since(session, since) -> dict[str, int]: ...
```

`_alerts_type_counts_since` : à M7, les alertes **ne sont pas persistées en DB** (M4 §13 + M7 §1 hors scope). Cette query lit un **compteur in-memory** maintenu par `AlertDispatcher` (reset à chaque boot). Limitation documentée dans le template daily summary : "_Alertes 24 h depuis le dernier boot du bot_".

**Alternative (M7.1)** : persister `Alert` en DB pour vraie stat 24 h indépendante du boot. Reportée.

---

## 7. Rendu Jinja2 — `alert_renderer.py`

### 7.1 Loader

```python
class AlertRenderer:
    """Rendu Markdown v2 Telegram via templates Jinja2.

    Cascade : assets/telegram/*.md.j2 (user overrides) puis
    src/polycopy/monitoring/templates/*.md.j2 (defaults).
    """

    def __init__(self, settings: Settings) -> None:
        search_paths = [
            Path("assets/telegram"),                    # user overrides
            Path(__file__).parent / "templates",        # defaults
        ]
        loader = FileSystemLoader([str(p) for p in search_paths if p.exists()])
        self.env = Environment(
            loader=loader,
            autoescape=False,                            # Markdown v2, pas HTML
            trim_blocks=True,
            lstrip_blocks=True,
            undefined=StrictUndefined,                   # fail fast si variable manquante
        )
        self.env.filters["telegram_md_escape"] = telegram_md_escape
        self.env.filters["wallet_short"] = wallet_short
        self.env.filters["format_usd_tg"] = format_usd_tg
        self.env.filters["humanize_dt_tg"] = humanize_dt_tg

    def render_alert(self, alert: Alert, context_extra: dict[str, Any] | None = None) -> str: ...
    def render_startup(self, context: StartupContext) -> str: ...
    def render_shutdown(self, duration: str, stats: ShutdownStats) -> str: ...
    def render_heartbeat(self, context: HeartbeatContext) -> str: ...
    def render_daily_summary(self, context: DailySummaryContext) -> str: ...
    def render_digest(self, context: DigestContext) -> str: ...
```

### 7.2 Template resolution (cascade user-land → defaults)

Si `assets/telegram/kill_switch_triggered.md.j2` existe → utilisé. Sinon → fallback sur `src/polycopy/monitoring/templates/kill_switch_triggered.md.j2`. Test couverture : override sur 1 template, vérifier qu'il est utilisé ; absence → default.

### 7.3 Fallback template

Pour un `event_type` non listé (ex: futur `executor_slippage_exceeded`), render via `fallback.md.j2` qui reproduit le format M4 :

```
{{ emoji }} *\\[{{ event_type | telegram_md_escape }}\\]*
{{ body | telegram_md_escape }}
```

Garantit **zéro régression** si M7 est installé sur un bot qui émet des events non documentés.

### 7.4 Helper `md_escape.py`

```python
_ESCAPE_CHARS = r"_*[]()~`>#+-=|{}.!"

def telegram_md_escape(value: Any) -> str:
    """Escape les caractères spéciaux Markdown v2 Telegram.

    Référence : https://core.telegram.org/bots/api#markdownv2-style
    """
    s = "" if value is None else str(value)
    return "".join(f"\\{c}" if c in _ESCAPE_CHARS else c for c in s)


def wallet_short(wallet: str, width: int = 4) -> str:
    if not wallet or len(wallet) < 2 * width + 2:
        return wallet
    return f"{wallet[: 2 + width]}…{wallet[-width:]}"


def format_usd_tg(value: float | None) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1000:
        return f"\\${value / 1000:.1f}k"
    return f"\\${value:.2f}"


def humanize_dt_tg(dt: datetime | None) -> str: ...
```

Escape obligatoire car Markdown v2 est strict : un `.` non échappé dans `http://127.0.0.1:8787` plante le rendu Telegram (erreur 400). Tests exhaustifs §9.4.

### 7.5 Pièges Markdown v2

1. **`.` doit être échappé** dans les URLs (`127\\.0\\.0\\.1`).
2. **`-` dans les numéros négatifs** (`-3.2%` → `\\-3\\.2%`).
3. **`(` `)` dans les exemples** (`\\(voir dashboard\\)`).
4. **`_` dans les noms de wallets** si user met un label `my_wallet` → escape.
5. **`[` `]` dans les slugs** si un marché a un crochet → escape.
6. **Liens cliquables** : syntaxe `[texte](url)` mais l'URL elle-même ne doit pas contenir de caractères non-échappés. Utiliser `https://...` raw (Telegram auto-linkify) ou pré-échapper.

Test `test_md_escape.py` couvre les 6 cas.

---

## 8. Orchestration + intégration `__main__`

### 8.1 `MonitoringOrchestrator` étendu

```python
class MonitoringOrchestrator:
    def __init__(
        self,
        session_factory,
        settings: Settings,
        alerts_queue: asyncio.Queue[Alert],
    ) -> None:
        self._sf = session_factory
        self._settings = settings
        self._alerts_queue = alerts_queue

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        telegram_client = TelegramClient(self._settings)
        renderer = AlertRenderer(self._settings)
        digest = AlertDigestWindow(
            window_seconds=self._settings.telegram_digest_window_minutes * 60,
            threshold=self._settings.telegram_digest_threshold,
        )
        dispatcher = AlertDispatcher(
            queue=self._alerts_queue,
            telegram_client=telegram_client,
            renderer=renderer,
            digest=digest,
            settings=self._settings,
        )
        pnl_writer = PnlSnapshotWriter(...)
        startup = StartupNotifier(self._sf, telegram_client, renderer, self._settings)
        heartbeat = HeartbeatScheduler(self._sf, telegram_client, renderer, self._settings)
        daily = DailySummaryScheduler(self._sf, telegram_client, renderer, self._settings)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(dispatcher.run(stop_event))
            tg.create_task(pnl_writer.run(stop_event))
            if self._settings.telegram_startup_message:
                tg.create_task(startup.send_once(stop_event))
            if self._settings.telegram_heartbeat_enabled:
                tg.create_task(heartbeat.run(stop_event))
            if self._settings.telegram_daily_summary:
                tg.create_task(daily.run(stop_event))

        # Après stop_event.set() : envoyer shutdown message 1 fois
        if self._settings.telegram_startup_message and telegram_client.enabled:
            try:
                body = renderer.render_shutdown(...)
                await telegram_client.send(body)
            except Exception:
                log.exception("telegram_shutdown_failed")
        log.info("monitoring_stopped")
```

**Garde-fous** :

- Si `telegram_client.enabled is False` (pas de token / chat_id) → toutes les features M7 sont no-op (`startup.send_once` log "telegram_disabled_startup_skipped" et sort).
- Dans le startup message, les wallets pinned sont lus **une seule fois** au boot — si le user édite `.env` + restart, le nouveau startup reflète le changement.

### 8.2 `StartupNotifier`

```python
class StartupNotifier:
    async def send_once(self, stop_event: asyncio.Event) -> None:
        if not self._telegram.enabled:
            log.info("telegram_startup_skipped", reason="telegram_disabled")
            return
        ctx = await self._build_context()
        try:
            body = self._renderer.render_startup(ctx)
            await self._telegram.send(body)
            log.info("telegram_startup_sent", version=ctx.version, mode=ctx.mode)
        except Exception:
            log.exception("telegram_startup_failed")
```

`_build_context` interroge la DB pour `pinned_wallets` + déduit les modules actifs depuis `settings`.

### 8.3 `HeartbeatScheduler`

```python
class HeartbeatScheduler:
    def __init__(self, ..., settings):
        self._interval_seconds = settings.telegram_heartbeat_interval_hours * 3600
        self._boot_at = utc_now()
        self._count = 0

    async def run(self, stop_event):
        if not self._telegram.enabled:
            return
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval_seconds)
                return
            except TimeoutError:
                pass
            self._count += 1
            try:
                ctx = await self._build_context()
                if ctx.critical_alerts_in_window > 0:
                    log.debug("telegram_heartbeat_skipped", reason="recent_critical")
                    continue
                body = self._renderer.render_heartbeat(ctx)
                await self._telegram.send(body)
                log.info("telegram_heartbeat_sent", index=self._count)
            except Exception:
                log.exception("telegram_heartbeat_failed")
```

### 8.4 `DailySummaryScheduler`

Voir §2.2 algorithme.

### 8.5 `AlertDispatcher` étendu

Modif minimale :

```python
async def _handle(self, alert: Alert) -> None:
    if alert.cooldown_key is not None:
        if self._cooldown_active(alert.cooldown_key):
            log.debug("alert_throttled", cooldown_key=alert.cooldown_key, alert_event=alert.event)
            return
        self._touch_cooldown(alert.cooldown_key)

    decision = self._digest.register(alert, self._now())
    if decision.action == "emit_digest":
        digest_ctx = self._build_digest_context(alert.event, decision)
        formatted = self._renderer.render_digest(digest_ctx)
    else:
        formatted = self._renderer.render_alert(alert)

    sent = await self._telegram.send(formatted)
    log.info("alert_sent", alert_event=alert.event, digest_action=decision.action, count=decision.count, sent=sent)
```

**Compatibilité descendante M4** : si `AlertRenderer` n'a pas de template pour un `event_type`, il utilise `fallback.md.j2` qui reproduit l'ancien format.

---

## 9. Tests

### 9.1 Arborescence

```
tests/
├── fixtures/
│   └── telegram/
│       ├── startup_context_full.json
│       ├── daily_summary_context.json
│       └── digest_context.json
├── unit/
│   ├── test_telegram_md_escape.py              NOUVEAU
│   ├── test_alert_renderer.py                  NOUVEAU
│   ├── test_alert_digest_window.py             NOUVEAU
│   ├── test_startup_notifier.py                NOUVEAU
│   ├── test_heartbeat_scheduler.py             NOUVEAU
│   ├── test_daily_summary_scheduler.py         NOUVEAU
│   ├── test_daily_summary_queries.py           NOUVEAU
│   ├── test_alert_dispatcher_m7.py             NOUVEAU (étend M4)
│   ├── test_monitoring_orchestrator_m7.py      NOUVEAU (étend M4)
│   └── test_telegram_template_rendering.py     NOUVEAU (15 templates × 1-2 snapshots)
└── integration/
    └── test_telegram_live.py                   NOUVEAU @pytest.mark.integration (opt-in réseau)
```

### 9.2 `test_telegram_md_escape.py`

- `telegram_md_escape("hello")` → `"hello"`.
- `telegram_md_escape("127.0.0.1")` → `"127\\.0\\.0\\.1"`.
- `telegram_md_escape("-3.2%")` → `"\\-3\\.2%"`.
- `telegram_md_escape("(voir)")` → `"\\(voir\\)"`.
- `telegram_md_escape("my_wallet")` → `"my\\_wallet"`.
- `telegram_md_escape(None)` → `""`.
- `telegram_md_escape(3.14)` → `"3\\.14"`.
- Property test `hypothesis` : `telegram_md_escape(s)` ne contient **pas** de char dans `_ESCAPE_CHARS` qui ne soit pas précédé de `\`.
- `wallet_short("0xabcdef1234567890abcdef", 4)` → `"0xabcd…cdef"`.
- `format_usd_tg(1234.56)` → `"\\$1\\.2k"`.
- `format_usd_tg(0.45)` → `"\\$0\\.45"`.
- `format_usd_tg(None)` → `"—"`.

### 9.3 `test_alert_renderer.py`

- Rendu chaque template avec un context fixture → retourne string non vide, ne raise pas.
- `render_alert` pour event connu (`kill_switch_triggered`) → utilise le template dédié.
- `render_alert` pour event inconnu (`future_event`) → utilise `fallback.md.j2`.
- Override user-land : crée `tmp_path / "telegram" / "kill_switch_triggered.md.j2"` avec contenu custom → `AlertRenderer` init avec search_paths dupliqué + fixture → utilise le custom.
- `StrictUndefined` : passer un context sans `version` field → raise `UndefinedError`.

### 9.4 `test_alert_digest_window.py`

- 4 alertes en 1 h, threshold=5 → 4× `emit_single`.
- 5 alertes en 1 h, threshold=5 → 4× `emit_single` + 1× `emit_digest`.
- 10 alertes en 1 h, threshold=5 → 4× `emit_single`, 1× `emit_digest` (count=5), reset, 4× `emit_single`, 1× `emit_digest` (count=5).
- Alertes hors fenêtre (> window_seconds) → purgées, pas comptées.
- 2 event_types distincts → compteurs indépendants.
- `freezegun` pour simuler le temps.

### 9.5 `test_startup_notifier.py`

- `telegram.enabled=False` → `send_once` no-op, log `telegram_startup_skipped`.
- `telegram.enabled=True` + `telegram_startup_message=false` → pas d'instanciation (`MonitoringOrchestrator` ne lance pas la task, testé via `test_monitoring_orchestrator_m7.py`).
- Context build : fixture DB avec 2 traders pinned, 1 shadow → startup context contient 2 pinned_wallets (pas le shadow).
- Modules liste : settings `DASHBOARD_ENABLED=true` → module "Dashboard" dans la liste avec `detail="http://127.0.0.1:8787"`.
- Modules liste : settings `DISCOVERY_ENABLED=false` → module "Discovery" avec `detail="désactivé"` + `enabled=False` → emoji `⏸️` dans le template.
- Mock respx Telegram 200 → `telegram_startup_sent` log.
- Mock respx Telegram 400 → exception catchée, log `telegram_startup_failed`.

### 9.6 `test_heartbeat_scheduler.py`

- `telegram.enabled=False` → no-op.
- Boot + set stop_event immédiat → retour propre, 0 heartbeat envoyé.
- 2 h intervalle + mock `asyncio.wait_for` → 2 heartbeats envoyés en 4 h simulées.
- `critical_alerts_in_window > 0` → heartbeat skippé, log `telegram_heartbeat_skipped`.

### 9.7 `test_daily_summary_scheduler.py`

- `_next_summary_at(now=2026-04-18 08:00 UTC, hour=9, tz=UTC)` → `2026-04-18 09:00 UTC`.
- `_next_summary_at(now=2026-04-18 10:00 UTC, hour=9, tz=UTC)` → `2026-04-19 09:00 UTC` (jour suivant).
- TZ Europe/Paris (UTC+2 en été DST) : `now=2026-04-18 12:00 UTC` (= 14:00 Paris), `hour=9` → `2026-04-19 07:00 UTC` (= 9:00 Paris).
- DST spring forward : `now=2026-03-28 01:00 UTC`, `hour=3`, `tz=Europe/Paris` → 2026-03-29 correspond à 3:00 Paris qui n'existe pas (skip to 4:00 CEST) — `_next_summary_at` gère proprement via `ZoneInfo`.
- Mock respx Telegram 200 → summary envoyé.
- DB vide → `DailySummaryContext` avec zéros partout, rendu valide (pas de `None` qui casserait `StrictUndefined`).

### 9.8 `test_daily_summary_queries.py`

- 5 trades insérés sur les 24 h → `trades_24h == 5`, `top_wallets[0].trade_count == 3` si un wallet a 3 trades.
- 0 trades → `trades_24h == 0`, `top_wallets == []`.
- Decisions mix → counts séparés APPROVED/REJECTED.
- Orders mix → counts par status.
- M5 events : 2 promotions, 1 demotion insérés → `discovery_promotions_24h == 2`.
- Bornes `since` : un event à `since - 1 min` n'est pas compté.

### 9.9 `test_alert_dispatcher_m7.py`

- Alerte M4 classique (`kill_switch_triggered`) → rendu via template dédié, pas fallback.
- Alerte event_type inconnu (`future_something`) → rendu via `fallback.md.j2`.
- 5 alertes `order_filled_large` en < 1 h → 4 emit_single, 1 emit_digest.
- Test **non-régression M4** : cooldown_key activé + 2 alertes mêmes key → 1 seule envoyée (inchangé).

### 9.10 `test_monitoring_orchestrator_m7.py`

- `telegram_enabled=False` (pas de token) → aucun des 3 nouveaux schedulers ne lance (log `telegram_*_skipped`).
- `telegram_startup_message=true` + `telegram_daily_summary=false` + `telegram_heartbeat_enabled=false` → seul StartupNotifier lance.
- Stop event set → orchestrator sort, shutdown message envoyé 1 fois (si `telegram_startup_message=true`).

### 9.11 `test_telegram_template_rendering.py`

Pour chaque template (15 au total) :

1. Charge un context fixture minimal + complet.
2. Render le template → chaîne non vide, ne raise pas.
3. Vérifie `\\` présent sur les caractères spéciaux (preuve de l'escape).
4. Vérifie présence de la section attendue (ex: kill_switch template contient `"kill switch"`, `"drawdown"`).
5. Longueur ≤ 4096 chars (limite Telegram message).

### 9.12 `test_telegram_live.py` (opt-in)

```python
@pytest.mark.integration
async def test_real_telegram_send(monkeypatch):
    token = os.environ.get("TELEGRAM_BOT_TOKEN_TEST")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID_TEST")
    if not token or not chat_id:
        pytest.skip("Set TELEGRAM_BOT_TOKEN_TEST and TELEGRAM_CHAT_ID_TEST")
    # Envoie un message de test réel, vérifie 200 OK
    ...
```

Run : `TELEGRAM_BOT_TOKEN_TEST=... TELEGRAM_CHAT_ID_TEST=... pytest -m integration`. Pas en CI default.

### 9.13 Couverture

```bash
pytest --cov=src/polycopy/monitoring --cov-report=term-missing
```

Seuil : **≥ 80 %** sur `src/polycopy/monitoring/` (M4 déjà à ≥ 80 %, M7 ajoute ~6 fichiers à couvrir).

---

## 10. Mises à jour de documentation (même PR)

### 10.1 `README.md`

Table env vars : ajouter 8 lignes :

```markdown
| `TELEGRAM_STARTUP_MESSAGE` | Envoie un message de démarrage au boot | `true` | non |
| `TELEGRAM_HEARTBEAT_ENABLED` | Active les heartbeats périodiques | `false` | non |
| `TELEGRAM_HEARTBEAT_INTERVAL_HOURS` | Intervalle entre 2 heartbeats (1–168 h) | `12` | non |
| `TELEGRAM_DAILY_SUMMARY` | Envoie un résumé quotidien | `false` | non |
| `TG_DAILY_SUMMARY_HOUR` | Heure locale d'envoi du résumé (0–23) | `9` | non |
| `TG_DAILY_SUMMARY_TIMEZONE` | TZ IANA du résumé quotidien | `Europe/Paris` | non |
| `TELEGRAM_DIGEST_THRESHOLD` | Alertes/heure pour batch digest | `5` | non |
| `TELEGRAM_DIGEST_WINDOW_MINUTES` | Fenêtre de comptage digest | `60` | non |
```

Section "Alertes Telegram" étendue :

```markdown
## Alertes Telegram (optionnel, enrichi à M7)

Depuis M7, le bot Telegram est un **compagnon conversationnel** :

- **Startup message** (défaut ON) : à chaque `python -m polycopy`, un message avec version, mode, wallets suivis, modules actifs, lien dashboard.
- **Heartbeat périodique** (opt-in) : toutes les 12 h, un ping "✅ polycopy tourne". Permet de détecter une panne process.
- **Résumé quotidien** (opt-in) : à 9h00 heure locale, un digest des trades 24 h, décisions, ordres, PnL, discovery, alertes.
- **Digest anti-spam** : si ≥ 5 alertes du même type en 1 h, batch en 1 seul message.
- **Templates soignés** : chaque type d'alerte passe par un template Markdown v2. Les templates sont surchargeables dans `assets/telegram/`.

Pour activer toutes les features :

\`\`\`env
TELEGRAM_BOT_TOKEN=<ton_token>
TELEGRAM_CHAT_ID=<ton_chat>
TELEGRAM_STARTUP_MESSAGE=true
TELEGRAM_HEARTBEAT_ENABLED=true
TELEGRAM_DAILY_SUMMARY=true
TG_DAILY_SUMMARY_HOUR=9
TG_DAILY_SUMMARY_TIMEZONE=Europe/Paris
\`\`\`

Les defaults (`STARTUP_MESSAGE=true`, le reste `false`) garantissent qu'un user M4/M5 qui met à jour **main** sans toucher son `.env` ne sera pas spammé d'un coup.
```

Roadmap : cocher `[x] **M7** : Bot Telegram enrichi (heartbeat, résumé quotidien, templates soignés, digest)`.

### 10.2 `docs/architecture.md`

Étendre la section "Module : Monitoring" :

```markdown
> **Status M7** ✅ — refonte couche Telegram. Heartbeat périodique (opt-in), résumé quotidien TZ-aware (opt-in), templates Jinja2 surchargeables (`assets/telegram/`), digest mode anti-spam. L'orchestrator M4 co-lance `StartupNotifier`, `HeartbeatScheduler`, `DailySummaryScheduler` dans le même TaskGroup. `AlertRenderer` consomme le context d'un `Alert` + templates `.md.j2` ; fallback pour les event_types non documentés préserve le format M4 (zéro régression). Voir `specs/M7-telegram-enhanced.md` et `src/polycopy/monitoring/templates/`.
```

### 10.3 `CLAUDE.md`

Section "Sécurité", étendre :

```markdown
- **Telegram M7** : `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID` jamais loggés, même partiellement. URL `sendMessage` jamais loggée en clair (httpx default). Bypass silencieux si absents. M7 étend M4 : startup message, heartbeat, daily summary, digest — tous opt-in (sauf `TELEGRAM_STARTUP_MESSAGE=true` par défaut, no-op si pas de token). Rotation token recommandée tous les 6 mois. Templates Jinja2 `autoescape=False` (Markdown v2) → escape explicite via filter `telegram_md_escape`. Bot reste **emitter-only** : aucune commande entrante reçue.
```

### 10.4 `docs/setup.md`

Ajouter **section 16** :

```markdown
## 16. Activer les notifications Telegram enrichies (M7)

Prérequis : `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` déjà configurés (cf. §10).

Options disponibles à M7 :

- **Startup message** (ON par défaut dès que le token est configuré) : à chaque `python -m polycopy`, un message de démarrage avec liste des modules. Désactiver avec `TELEGRAM_STARTUP_MESSAGE=false`.
- **Heartbeat périodique** : `TELEGRAM_HEARTBEAT_ENABLED=true` + `TELEGRAM_HEARTBEAT_INTERVAL_HOURS=12`. Utile pour détecter une panne silencieuse.
- **Résumé quotidien** : `TELEGRAM_DAILY_SUMMARY=true` + `TG_DAILY_SUMMARY_HOUR=9` + `TG_DAILY_SUMMARY_TIMEZONE=Europe/Paris`. Le résumé arrive à l'heure locale configurée.
- **Digest anti-spam** : activé par défaut dès 5 alertes du même type en 1 h (`TELEGRAM_DIGEST_THRESHOLD=5` + `TELEGRAM_DIGEST_WINDOW_MINUTES=60`). Adaptatif selon ton profil.

### Surcharger un template

Les templates vivent dans `src/polycopy/monitoring/templates/` (ex: `kill_switch_triggered.md.j2`). Pour personnaliser :

\`\`\`bash
mkdir -p assets/telegram/
cp src/polycopy/monitoring/templates/kill_switch_triggered.md.j2 assets/telegram/
# Édite assets/telegram/kill_switch_triggered.md.j2 à ton goût
\`\`\`

Au prochain démarrage, le template surchargé est utilisé. Pour revenir au default, supprime le fichier user-land.

Troubleshooting :

- **Startup message manquant** → vérifier `TELEGRAM_STARTUP_MESSAGE=true` et que le token n'est pas vide.
- **Daily summary à mauvaise heure** → vérifier `TG_DAILY_SUMMARY_TIMEZONE`. Test : `python -c "from zoneinfo import ZoneInfo; print(ZoneInfo('Europe/Paris'))"`.
- **Message Markdown tronqué / erreur 400 Telegram** → probable escape Markdown v2 manquant. Vérifier les logs `alert_error` + éventuellement override le template coupable avec un format simplifié.
- **Token rotation** : via BotFather `/token`, remplacer `TELEGRAM_BOT_TOKEN` dans `.env`, restart. Aucune autre action requise côté polycopy.
```

---

## 11. Commandes de vérification finale

```bash
ruff check .
ruff format --check .
mypy src
pytest --cov=src/polycopy/monitoring --cov-report=term-missing   # ≥ 80 %
pytest -m integration                                             # opt-in (test_telegram_live.py)

# Smoke test startup
TELEGRAM_BOT_TOKEN=<dummy> TELEGRAM_CHAT_ID=<dummy> TELEGRAM_STARTUP_MESSAGE=true \
python -m polycopy --dry-run &
sleep 3
# Vérifier logs : telegram_startup_sent (ou telegram_startup_failed si token invalide)
kill %1 && wait

# Test manuel templates (sans lancer le bot)
python -c "
from polycopy.monitoring.alert_renderer import AlertRenderer
from polycopy.monitoring.dtos import StartupContext, ModuleStatus, PinnedWallet
from polycopy.config import settings
from datetime import datetime, UTC
r = AlertRenderer(settings)
ctx = StartupContext(
    version='0.7.0 (abcd1234)', mode='dry-run', boot_at=datetime.now(UTC),
    pinned_wallets=[PinnedWallet(wallet_short='0xabc…def', label='Test')],
    modules=[ModuleStatus(name='Watcher', enabled=True, detail='3 wallets')],
    dashboard_url='http://127.0.0.1:8787', discovery_summary=None,
)
print(r.render_startup(ctx))
"
```

---

## 12. Critères d'acceptation

- [ ] `TELEGRAM_BOT_TOKEN=<valide> TELEGRAM_CHAT_ID=<valide> TELEGRAM_STARTUP_MESSAGE=true python -m polycopy --dry-run` → un message de démarrage arrive côté Telegram dans les 3 s. Log `telegram_startup_sent` émis.
- [ ] Sans `TELEGRAM_BOT_TOKEN` → aucun appel Telegram, aucun crash, log `telegram_disabled`. Toutes les features M7 no-op silencieusement.
- [ ] `TELEGRAM_HEARTBEAT_ENABLED=true` + `TELEGRAM_HEARTBEAT_INTERVAL_HOURS=1` → heartbeat après 1 h (observé via log `telegram_heartbeat_sent` en test avec mock wait_for).
- [ ] Heartbeat skippé si `kill_switch_triggered` a été émis dans la fenêtre.
- [ ] `TELEGRAM_DAILY_SUMMARY=true` + `TG_DAILY_SUMMARY_HOUR=<heure>` + `TG_DAILY_SUMMARY_TIMEZONE=UTC` → résumé envoyé à l'heure UTC configurée (± 1 min tolérance).
- [ ] Scheduler daily gère correctement le passage de jour (23h → 00h) et les DST transitions (tests `freezegun`).
- [ ] Digest mode : 5 alertes `order_filled_large` en 1 h → 4 messages simples + 1 digest en 5ᵉ, puis reset.
- [ ] Tous les 15 templates rendent sans raise sur un context fixture minimal ET complet.
- [ ] `StrictUndefined` : un template avec variable manquante raise `UndefinedError` (pas de rendu silencieux cassé).
- [ ] Override user-land : `assets/telegram/kill_switch_triggered.md.j2` shadow le default — testé unit.
- [ ] Aucun secret (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `POLYMARKET_PRIVATE_KEY`, etc.) dans les logs ni les rendered messages — vérifié par grep automatisé.
- [ ] Fallback template `fallback.md.j2` pour un `event_type` non documenté — préserve format M4 (zéro régression).
- [ ] Escape Markdown v2 : URLs avec `.`, `-`, `(`, `)`, `_` échappés dans tous les templates — testé unit `test_md_escape.py` + property test.
- [ ] Longueur des messages rendered ≤ 4096 chars (limite Telegram) — testé unit pour chaque template.
- [ ] `AlertDispatcher` M4 non cassé : tests M4 `test_alert_dispatcher.py` passent sans diff (seul le format de `body` passe par `AlertRenderer` ; le cooldown + TelegramClient sont inchangés).
- [ ] `ruff check .` + `ruff format --check .` + `mypy src` (`--strict`) : 0 erreur.
- [ ] `pytest` : 0 échec. Coverage ≥ 80 % sur `src/polycopy/monitoring/`. Non-régression M1..M6 ≥ 80 %.
- [ ] Docs §10 à jour (`README.md`, `docs/architecture.md`, `CLAUDE.md`, `docs/setup.md` §16) dans le **même** commit.
- [ ] `assets/telegram/README.md` livre la doc de surcharge user-land.
- [ ] Commit final unique : `feat(monitoring): M7 Telegram enhanced (startup, heartbeat, daily summary, digest, templates)`.

---

## 13. Hors scope M7 (NE PAS implémenter)

- **Commandes entrantes Telegram** (`/score 0xabc`, `/pause`, `/resume`, `/status`). Polycopy reste **emitter-only**. Si un jour utile → milestone séparé avec `TelegramUpdater` + vérification auteur (chat_id whitelist) + token de validation.
- **Persistance DB des messages envoyés** (`telegram_events` table). Reportable M7.1.
- **Multi-chat / multi-audience** (chat groupe + chat perso + channel). 1 `TELEGRAM_CHAT_ID` suffit à M7.
- **Retry queue persistée** en cas de panne Telegram. Pas de retry → loggué comme erreur uniquement.
- **i18n / traduction** des templates. FR uniquement.
- **Templates HTML** (Telegram supporte `parse_mode=HTML`). Markdown v2 suffit et reste plus lisible en source.
- **Graphique inline dans le daily summary** (PNG généré côté serveur). Lien vers le dashboard suffit.
- **Slack / Discord / email fallback**. Telegram seul à M7. Un adapter multi-backend → milestone séparé.
- **Webhook entrant** Telegram (vs polling updates). Pas applicable — bot emitter-only.
- **Signature / authentification** des messages sortants (preuve cryptographique "c'est bien polycopy qui envoie"). Hors scope, l'user trust son propre token.
- **Auto-clear** des anciens messages (delete après N jours via Bot API). Hors scope, le user peut mute ou archiver le chat.
- **Presets users-friendly** (`TELEGRAM_PROFILE=quiet|normal|verbose`). Over-engineering — on laisse les env vars fines.
- **Dashboard `/monitoring`** qui montre les templates rendus + éditeur in-browser. Hors scope.
- **Rate limit client-side stricte** (< 30 msg/s). On fait confiance à Telegram (retourne 429 qu'on log).
- **Persistance du compteur heartbeat `_count`** entre redémarrages. Le reset au boot est acceptable.

---

## 14. Notes d'implémentation + zones d'incertitude

### 14.1 Ordre de travail suggéré

1. **Ajouter 8 env vars** dans `config.py` + `.env.example` + validators (`TG_DAILY_SUMMARY_TIMEZONE` via `ZoneInfo` pour fail-fast).
2. **Créer `md_escape.py`** + tests property-based exhaustifs.
3. **Créer `alert_renderer.py`** + Environment + filters + tests basiques.
4. **Créer 15 templates** `.md.j2` (defaults) + partials communs.
5. **Créer `alert_digest.py`** (`AlertDigestWindow` + `DigestDecision`) + tests `freezegun`.
6. **Étendre `AlertDispatcher`** : injection `AlertRenderer` + `AlertDigestWindow` + backward compat via `fallback.md.j2`.
7. **Créer `startup_notifier.py`** + tests (respx Telegram 200/400).
8. **Créer `heartbeat_scheduler.py`** + tests (mock `asyncio.wait_for`).
9. **Créer `daily_summary_queries.py`** + tests (insertions DB test + assertions counts).
10. **Créer `daily_summary_scheduler.py`** + tests (TZ + DST cases).
11. **Étendre `MonitoringOrchestrator`** (§8.1) : co-lance 3 schedulers conditionnels + shutdown message.
12. **Créer `assets/telegram/README.md`** (docs surcharge).
13. **Smoke test manuel** : token+chat_id réel, vérifier les 4 types de messages (startup, heartbeat, daily, digest).
14. **Tests template rendering** (15 templates × 1-2 fixtures).
15. **Doc updates §10**.
16. **Commit unique** : `feat(monitoring): M7 Telegram enhanced (...)`.

### 14.2 Principes

- **Opt-in strict** : sauf `TELEGRAM_STARTUP_MESSAGE=true` (peu intrusif, tout ou rien selon présence du token), toutes les features M7 sont `=false` par défaut. Upgrade main branch M6 → M7 = zéro message nouveau sauf si le user active explicitement.
- **Pas d'abstraction prématurée** : `AlertRenderer` est concret, pas `AbstractRenderer`. `HeartbeatScheduler` est concret, pas `AbstractPeriodicTask`. Si demain on ajoute Slack → nouvelle classe, pas factorisation.
- **Templates simples** : pas d'héritage massif Jinja (`{% extends %}`). Un template = un event = autosuffisant. Partials réservés aux utilitaires (macro `wallet_short_md`).
- **StrictUndefined** : une variable manquante au rendu = crash explicite, pas "string vide silencieuse". Les bugs se trouvent en dev, pas en prod.
- **Sessions DB courtes** : chaque query agrégée `_count_*` ouvre sa propre session, ferme immédiatement. `collect_daily_summary_context` coordonne.
- **Cooldown unique M4 + digest** : sémantiquement distincts. Cooldown protège "même alerte exactement deux fois". Digest protège "trop d'alertes différentes mais même type". Tests croisés §9.9.

### 14.3 Décisions auto-arbitrées

1. **`TELEGRAM_STARTUP_MESSAGE=true` par défaut** (ON dès que token configuré) : le bénéfice UX est énorme (confirmation visuelle du démarrage), le coût est 1 message/boot. Les autres features restent OFF par défaut (plus intrusives).
2. **Heartbeat 12 h par défaut** : 2 messages/jour, équilibre détection de panne vs bruit.
3. **Daily summary 9h00 heure locale** : choix arbitraire raisonnable. Correspond à la lecture du premier café.
4. **Digest threshold 5 / window 60 min** : compromis empirique. < 5 = digest trop agressif (alerte unique mérite son message). > 5 = on tolère trop de spam avant batch.
5. **FileSystemLoader cascade user → defaults** : pattern connu (Django overrides, Sphinx). Pas de nouveau concept à apprendre.
6. **`autoescape=False`** : Markdown v2 incompatible avec HTML escape (`&` n'est pas spécial). Escape explicite via filter est le pattern Telegram docs.
7. **Pas de librairie Telegram externe** : httpx direct + Bot API documented. Patron M4 conservé.
8. **Daily summary "alertes 24 h depuis le dernier boot"** : limitation documentée. Compteur in-memory reset au restart. M7.1 persiste en DB si besoin.
9. **Lien dashboard en bas du daily summary** : drill-down facile. Si dashboard OFF → pas de lien affiché.
10. **`ZoneInfo` pour TZ** : stdlib Python 3.9+, zéro dep. `pytz` déprécié.
11. **Fallback template `fallback.md.j2`** reproduit format M4 pour zéro régression : critère d'acceptation bloquant.
12. **Shutdown message seulement si `TELEGRAM_STARTUP_MESSAGE=true`** : cohérence symétrique. On démarre avec un ping → on s'arrête avec un ping.
13. **Wallet pinned label dans startup message** : lu depuis `target_traders.label`. Si `None` → juste l'adresse courte.
14. **`_build_context` reads synchronously** : pas d'async appels externes dans le build context (pas de call Polymarket). Seulement queries DB locales rapides.
15. **Pas de compteur `heartbeat_index` persisté en DB** : in-memory suffit, reset au boot accepté.

### 14.4 Pièges anticipés

1. **Markdown v2 escape oublié sur une nouvelle valeur** : à chaque ajout de field dans un template, vérifier qu'on l'escape. Test `test_telegram_template_rendering.py` assertions `"\\."` présence sur les URLs.
2. **Timezone absente du système** : `ZoneInfo("Europe/Paris")` peut raise `ZoneInfoNotFoundError` sur minimal Linux. Mitigation : try/except au boot + fallback UTC + log warning.
3. **DST ambiguous datetime** : en automne, 2h→3h peut représenter deux instants. `ZoneInfo` gère via `disambiguate=` param mais par défaut c'est acceptable (on tolère un jour décalé).
4. **`PinnedWallet.label` None** : template conditionnel `{% if wallet.label %}{{ wallet.label }}{% else %}{{ wallet.wallet_short }}{% endif %}`. StrictUndefined strict → label doit toujours être passé (même `None`).
5. **Digest timing race** : 2 alertes arrivent dans la même ms. `register` n'est pas thread-safe. Mais `AlertDispatcher` est mono-coroutine → séquentiel de facto. OK.
6. **Telegram 429 rate limit** : `TelegramClient.send` M4 log l'erreur, ne retry pas. Acceptable (M7 envoie ~2 msg/min pic).
7. **httpx AsyncClient partagé** : `TelegramClient` garde un client, `AlertRenderer` n'en utilise pas, `StartupNotifier` consomme `TelegramClient`. 1 client partagé, 1 pool, économe.
8. **Template syntax error** : au boot, charger et compiler tous les templates via `env.get_template(...)` (preflight) + log si erreur. Évite un crash runtime en prod sur un template jamais testé.
9. **Shutdown message pendant cleanup** : si `stop_event` set → envoyer le shutdown peut timeout (httpx default 10 s) et retarder l'exit. Timeout agressif 3 s pour ce message, log warning si fail.
10. **Overrides user-land mal formés** : un user casse son `assets/telegram/kill_switch_triggered.md.j2`. Mitigation : try/except autour de `render_alert` + fallback au rendering default (`_render_with_fallback`) + log `telegram_user_template_failed`.
11. **Boot sans DB** : `StartupNotifier._build_context` query `target_traders`. Si DB vide (jamais init_db) → 0 pinned wallets, message reste valide.
12. **Environment variable `TG_DAILY_SUMMARY_TIMEZONE` typo** : `"Europe/Pariss"` → ZoneInfoNotFoundError au boot. Validator Pydantic cross-field `@model_validator(mode="after")` valide l'existence via `ZoneInfo(name)` try/except.
13. **Heartbeat quand monitoring est partiellement down** : si `TelegramClient.send` fail, log `telegram_heartbeat_failed` mais ne crash pas la boucle. Au tick suivant, retry.
14. **Cross-minute daily summary** : si `_send_summary` prend > 1 min, le prochain `_next_summary_at` calcule correctement (recalcul chaque itération).
15. **Emoji rendering cross-device** : certains emojis récents (🧭 Polygon compass) rendent mal sur iOS vieux. Testé — fallback : remplacer par "[Discovery]" text si problème remonté.

### 14.5 Zones d'incertitude à lever AVANT implémentation

1. **Format Markdown v2 vs v1** : Telegram `parse_mode=MarkdownV2` est strict sur l'escape. Faut-il garder `MarkdownV1` (plus lax, utilisé par M4) ? **Décision** : passer en `MarkdownV2` à M7 pour robustesse + test escape. **Risque** : si un template existant M4 utilise un formatage MarkdownV1 non-compat v2 → break. Mitigation : `fallback.md.j2` wrap en `parse_mode=MarkdownV2` strict. Breaking change documenté.

2. **`ZoneInfo` disponibilité** : WSL Ubuntu minimal peut ne pas avoir `tzdata`. **Action requise au setup** : `scripts/setup.sh` check `python -c "from zoneinfo import ZoneInfo; ZoneInfo('UTC')"`, install `tzdata` Python package en fallback si fail. Documenté.

3. **Templates FR vs EN** : la spec dit "FR cohérent CLAUDE.md docstrings FR". Mais un user anglophone ? **Décision** : M7 FR only. Si feedback → ajouter `LOCALE=en` env var en M7.1 + variantes templates `*_en.md.j2`.

4. **Définition "pinned wallet"** : vient de `target_traders WHERE pinned=true` (M5 schema). À M7, un user pre-M5 n'a pas `pinned` → tous ses `TARGET_WALLETS` ont été migrés en `pinned=true` par la migration 0003. **À vérifier** : si user install M7 sans avoir jamais run migration 0003 → query retourne 0 pinned. Mitigation : `_build_startup_context` log warning "no pinned wallets detected" au lieu de crash. Documenté.

5. **Daily summary "alertes 24 h"** : compteur in-memory reset au boot. Si le bot a redémarré à 14h00, le résumé 9h00 lendemain ne comptera que les alertes depuis 14h00 la veille. **Décision** : acceptable à M7. Documenté dans template ("_Alertes depuis le dernier boot_").

6. **Daily summary prend N minutes à construire** : queries DB 24 h × 8 sections. Bench sur DB 6 mois = ~200 ms. **Acceptable**. Si > 30 s → investigation index DB.

7. **Digest content samples** : "5 alertes `order_filled_large` — • 0xabc… • 0xdef… • ...". **Problème** : si les alertes partagent un sub-type (ex: même wallet), montre 5× la même ligne ? **Décision** : le `digest.md.j2` montre les 4 premiers, tronque le reste avec "• … et N autres". Template paramétrable.

8. **Shutdown message sans context DB** : à l'extinction, le `session_factory` est-il encore vivant ? Pour `run_duration` simple, pas besoin de DB. Pour stats "0 ordres passés" → query DB nécessaire. **Décision** : shutdown message se limite à `duration` + `version`, **pas** de stats DB (évite race avec engine dispose). Template minimal.

9. **Overrides user-land versionning** : si un user override `trader_promoted.md.j2` à M7 et M7.1 change le context (ajout `is_pinned`), le template user casse. **Décision** : documenter dans `assets/telegram/README.md` que les templates user-land doivent être réévalués à chaque upgrade mineur. StrictUndefined le fait crasher bruyamment → le user voit.

10. **Test `test_telegram_live.py` en CI** : besoin d'un token + chat_id. **Décision** : `@pytest.mark.integration` + skip si env vars absentes. Pas en CI default — user lance manuellement.

11. **Template `pnl_snapshot_drawdown` content** : body M4 inclut drawdown %, threshold %, total_usdc. M7 reproduit + lien dashboard PnL. **Risque** si les keys changent entre M4 et M7 : fallback template M7 utilise les mêmes keys M4 pour compat. Audit des `Alert.metadata` keys existants avant rewrite.

12. **Heartbeat "depuis 12h03"** : calcul côté server UTC. Sur un run qui traverse midnight + DST → `uptime_human` doit être robust. Testé avec `freezegun` cross-midnight.

---

## 15. Prompt à m'envoyer pour lancer l'implémentation

Copie-colle tel quel quand tu es prêt :

```
/implement-module M7

Suis specs/M7-telegram-enhanced.md à la lettre. Pas d'invocation skill Polymarket requise — M7 est 100 % couche monitoring + Telegram, aucune nouvelle API Polymarket.

Avant tout code, actions obligatoires :

1. Vérifier la disponibilité de ZoneInfo :
   python -c "from zoneinfo import ZoneInfo; print(ZoneInfo('Europe/Paris'))"
   Si KeyError / ZoneInfoNotFoundError → ajouter `tzdata` aux dependencies optionnelles dans pyproject.toml et documenter dans setup.md (section §16).

2. Vérifier que les templates M4 existants produisent le format attendu (capture 1 fixture par event_type actuellement émis : kill_switch_triggered, pnl_snapshot_drawdown, order_filled_large, executor_error, executor_auth_fatal, trader_promoted, trader_demoted, discovery_cap_reached, discovery_cycle_failed). Sauvegarder chaque Alert.body + metadata en fixtures/ pour tests de non-régression via fallback.md.j2.

Ensuite suis l'ordre §14.1.

Contraintes non négociables :

- Bot reste emitter-only (pas de commandes entrantes, cf. §13 + M5 §13).
- Aucun secret loggé : TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER, creds L2. Grep automatisé en test.
- TELEGRAM_STARTUP_MESSAGE=true par défaut (ON si token configuré, no-op sinon). Toutes les autres features M7 OFF par défaut (backwards compat M4).
- Escape Markdown v2 obligatoire via telegram_md_escape filter sur toutes les valeurs user-controlled (slugs, labels, URLs). Testé property-based hypothesis.
- StrictUndefined dans Jinja Environment : une variable manquante dans un template → crash explicite, pas rendu silencieux cassé.
- FileSystemLoader cascade : assets/telegram/ (user overrides) puis src/polycopy/monitoring/templates/ (defaults). Test override unit.
- Fallback template (fallback.md.j2) pour event_types non documentés → préserve format M4. Zéro régression critère bloquant.
- Digest mode : threshold 5 + window 60 min par défaut. Compteur in-memory glissant. Reset après emit_digest.
- Heartbeat skippé si alerte critique récente dans la fenêtre (évite dissonance "kill_switch" → 5 min après "polycopy actif").
- Daily summary TZ-aware via ZoneInfo. Tests DST spring/fall via freezegun.
- Shutdown message limité à duration + version (pas de query DB après engine dispose).
- Pas de dep Python nouvelle (ZoneInfo stdlib, Jinja2 déjà via M4.5, httpx déjà via M4). `tzdata` opt-in si système minimal.
- Pas de persistance DB des messages envoyés à M7 (reste éphémère, cf. §13 hors scope).
- AlertDispatcher M4 étendu via injection de AlertRenderer + AlertDigestWindow, PAS refactor. Tests M4 doivent passer sans diff.
- MonitoringOrchestrator co-lance les 3 nouveaux schedulers via TaskGroup interne existant. Stop_event partagé pour shutdown propre.
- Tous les 15 templates rendent sans raise sur fixtures minimales + complètes. Longueur ≤ 4096 chars.
- URL dashboard dans startup et daily summary uniquement si DASHBOARD_ENABLED=true. Sinon omise.
- Rate limit Telegram (30 msg/s) : on fait confiance, log 429 si reçu. Pas de retry queue à M7.
- Conventions CLAUDE.md (async, Pydantic v2, SQLAlchemy 2.0, structlog, docstrings FR / code EN, pas de print).
- mypy --strict propre, ruff propre, pytest ≥ 80% coverage sur src/polycopy/monitoring/ et pas de régression ≥ 80% sur M1..M6.
- Tests via respx (Telegram mocks), freezegun (temps), pytest-asyncio. Pas de socket réel sauf integration opt-in (test_telegram_live.py).
- Doc updates §10 dans le même commit (README + architecture + CLAUDE + setup §16 + assets/telegram/README.md).
- Commit final unique : feat(monitoring): M7 Telegram enhanced (startup, heartbeat, daily summary, digest, templates)

Demande-moi confirmation avant tout patch sensible :
- config.py (les 8 env vars, validators ZoneInfo + cross-field).
- .env.example (ajout commentaires sécurité token).
- Modification AlertDispatcher (préserver M4 invariant cooldown + bypass silencieux si disabled).
- Si un template user-land existe déjà dans assets/telegram/ (ne PAS écraser).

Si une zone §14.5 se confirme problématique pendant l'implémentation (ex: MarkdownV2 break un format M4, ZoneInfo indisponible, template user-land cassé au upgrade), STOP et signale — ne tranche pas au pif.

Smoke test final obligatoire avant merge (avec un token+chat_id de test réel, .mark.integration) : startup message reçu, heartbeat reçu après 1 h simulée, daily summary reçu à l'heure cible, digest reçu après 5 alertes simulées. Capture screenshot des 4 messages reçus, joindre à la PR.
```
