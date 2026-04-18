# M10 — Parité dry-run / live + hygiène des logs

Spec d'implémentation du **bundle "dégrossissage" post-M9**. M1..M9 ont bâti un bot fonctionnel, silencieux au boot et instrumenté. Deux résidus gênants subsistent :

1. **Asymétrie dry-run / live** : `DRY_RUN=true` désactive silencieusement le kill switch (`pnl_writer.py:108-110`), downgrade l'alerte de drawdown de `CRITICAL` vers `INFO`, et n'émet aucun signal visible distinctif. Un utilisateur qui observe 3 jours de dry-run ne voit **pas** se déclencher ce qui se déclencherait en live — le dry-run n'est donc **pas un miroir fidèle**. Ce piège est documenté depuis M4 comme un invariant de sécurité, mais les deep-searches (Gemini §3.2 / Perplexity §3.4) et l'audit 2026-04-18 le re-qualifient en **anti-pattern** : la valeur d'un dry-run, c'est précisément d'exercer *tous* les chemins du live.
2. **Bruit de logs sur le dashboard** : le `StructlogAccessMiddleware` (`dashboard/middleware.py:53-59`) émet un event `dashboard_request` par requête HTTP, incluant les polls HTMX haute fréquence (`/partials/kpis`, `/partials/detections-rows`, `/api/health-external`, `/api/version`). Sur un Home actif, le ratio bruit/signal dans `~/.polycopy/logs/polycopy.log` est **~28:1** (mesure synthèse §2.3), au point que l'onglet `/logs` M9 devient illisible sans filtrage manuel.

M10 traite ces deux résidus **ensemble** parce qu'ils ont la même racine fonctionnelle : la couche observabilité M4/M7/M9 a été construite en considérant le dry-run comme un mode "sandbox sans contrainte", donc on a laissé filer silence côté kill switch ET verbosité côté logs dashboard. M10 inverse les deux conventions : **dry-run = miroir fidèle du live**, **logs = business events par défaut, HTTP access opt-in**.

Source de vérité conception : `docs/development/M10_synthesis_reference.md` §2 (logs) + §3 (parity) + §8 (deltas CLAUDE.md). Deep-searches référencées : `gemini_deep_search_v2_and_more.md` §2.1-2.3, §3.1-3.3 ; `perplexity_deep_search_v2_and_more.md` §2.3, §3.3-3.5. Conventions : `CLAUDE.md`. Code existant : §6 ci-dessous (inventaire exhaustif file:line). Spec de référence format : `specs/M8-dry-run-realistic.md` + `specs/M9-silent-cli-and-readme.md`.

> ⚠️ **Invariants de sécurité inversés** par cette spec. M4 et M8 documentaient "kill switch JAMAIS en dry-run (invariant préservé)" comme **règle dure**. M10 assume ce renversement après décision explicite (synthèse §3.1, validée par Gemini ET Perplexity en consensus). §9 de la présente spec produit le texte de remplacement mot-pour-mot pour `CLAUDE.md`.

---

## 0. Résumé exécutif

- **Scope** : deux changements ciblés, couplés dans un seul bundle par cohérence observabilité. (A) Remplacer `DRY_RUN: bool` par `EXECUTION_MODE: "simulation" | "dry_run" | "live"` + inverser le court-circuit kill switch M4 + uniformiser le niveau d'alerte Telegram dans les 3 modes + badge visuel mode dans chaque template. (B) Ajouter un processor structlog `filter_noisy_endpoints` qui drop les events `dashboard_request` 2xx des endpoints polling haute fréquence, **avant** le formatage JSON ; et appliquer une exclusion par défaut côté lecteur `/logs` + preset UI persisté localStorage.
- **Motivation** : le dry-run actuel n'est pas un miroir fidèle (piège sécurité documenté en §14.5 spec M8 post-mortem). Le fichier log est trop bruyant pour être lu tel quel (ratio 28:1). Les deux bloquent l'implémentation sereine de M11 (temps réel WebSocket) et M12 (scoring v2) car sans observabilité propre on ne saura pas distinguer "bot lent" de "scoring nul".
- **Invariants de sécurité inversés** : kill switch actif en dry-run ; alerte Telegram `kill_switch_triggered` CRITICAL en dry-run. Documentés en §9 (texte CLAUDE.md de remplacement). Les garde-fous M3 et M8 qui protègent le path live (lazy init `ClobWriteClient`, `RuntimeError` boot, `assert execution_mode == "live"` avant POST, `assert execution_mode == "dry_run"` avant `_persist_realistic_simulated`) sont **préservés textuellement**, simplement réécrits avec la nouvelle enum.
- **Hors scope strict** : pas de refactor M3 au-delà du rename `dry_run` → `execution_mode` ; pas de migration DB (`MyOrder.is_dry_run`, `MyOrder.simulated`, `MyPosition.simulated`, `PnlSnapshot.is_dry_run` inchangés) ; pas de WebSocket, cache adaptatif ou scoring v2 (c'est M11, M12) ; pas de nouveau template Telegram, juste un badge header ; pas de nouvel onglet dashboard au-delà du preset `/logs`.
- **Effort estimé** : ~1 semaine 1 dev (cohérent roadmap synthèse §0).
- **Risque principal** : un utilisateur avec `KILL_SWITCH_DRAWDOWN_PCT=20` bas et un dry-run 3-jours va voir son bot se couper, ce qui peut surprendre. Mitigé par (a) warning CLI au boot post-migration, (b) section README "dry-run réaliste mirroir", (c) env var de neutralisation explicite `KILL_SWITCH_DRAWDOWN_PCT=100`.

---

## 1. Contexte

### 1.1 État M1..M9 (rappel)

M1..M9 ont livré :

- Watcher → Strategy → Executor → Storage (M1-M3, boucle end-to-end live).
- Monitoring M4 : `PnlSnapshotWriter`, `AlertDispatcher`, kill switch **live-only**.
- Dashboard M4.5 / M6 : localhost-only, GET-only, `StructlogAccessMiddleware` logs chaque requête.
- Discovery M5 : scoring v1, shadow period, pool géré.
- Telegram M7 : `StartupNotifier`, `HeartbeatScheduler`, `DailySummaryScheduler`, `AlertDigestWindow`, templates Jinja2 MarkdownV2 (15 templates dans `src/polycopy/monitoring/templates/`).
- Dry-run réaliste M8 : `DRY_RUN_REALISTIC_FILL`, `ClobOrderbookReader`, `VirtualWalletStateReader`, `DryRunResolutionWatcher` — tous **silencieux côté kill switch**, alerte `dry_run_virtual_drawdown` INFO only (c'est précisément ce que M10 inverse).
- CLI silencieux M9 : `RotatingFileHandler` 10 MB × 10 fichiers, écran rich, onglet dashboard `/logs`, filtres levels/events/q — **sans filtrage du bruit HTTP access**.

### 1.2 Pourquoi cette spec maintenant

Le brainstorming 2026-04-18 et les deux deep-searches Gemini + Perplexity convergent sur deux conclusions **prérequises à M11 et M12** :

- **Avant de mesurer une latence** (M11 instrumentation) il faut des logs propres, sinon les stages individuels sont noyés dans le bruit `dashboard_request`.
- **Avant de valider un scoring v2** (M12) il faut que le dry-run exerce le kill switch et les alertes — sinon on ne peut pas comparer un backtest dry-run à un run live attendu.

M10 débloque les deux. Il est aussi **rapide** (~1 semaine) et **additif-dominant** : la plupart des changements sont du rename, du déplacement de 2 lignes (suppression `_maybe_push_dry_run_drawdown`), et l'ajout d'un processor structlog de 30 lignes. Pas de migration DB, pas de nouveau module, pas de nouveau endpoint.

### 1.3 Références externes

- **Gemini DeepResearch §2.1-2.3** (middleware filtering, DropEvent processor) — code proposé `filter_noisy_endpoints`.
- **Gemini DeepResearch §3.1-3.3** (parité kill switch, badge visuel mode) — recommande alertes identiques avec distinction visuelle seule.
- **Perplexity DeepResearch §2.3** (exclusion lecteur par défaut, preset opt-in).
- **Perplexity DeepResearch §3.3-3.5** (3 modes, plan migration, inventaire divergences dry-run).
- **Synthèse §8** : texte CLAUDE.md à propager mot pour mot (§9 ci-dessous).

---

## 2. Objectifs et non-objectifs

### 2.1 Objectifs

**A. Parité dry-run / live**

- Remplacer `DRY_RUN: bool` par `EXECUTION_MODE: Literal["simulation", "dry_run", "live"]`. Default `"dry_run"`.
- Lire `DRY_RUN=true/false` legacy une dernière version avec warning de deprecation (`"true"` → `execution_mode=dry_run`, `"false"` → `execution_mode=live`).
- **Supprimer** le court-circuit `_maybe_push_dry_run_drawdown()` (`pnl_writer.py:108-110`). Le kill switch fire **identiquement** dans SIMULATION / DRY_RUN / LIVE.
- **Uniformiser** toutes les alertes Telegram : plus de downgrade INFO en dry-run. `kill_switch_triggered` reste CRITICAL dans les 3 modes. `order_filled_large`, `order_failed`, `executor_auth_fatal`, `pnl_snapshot_drawdown`, etc. conservent leur niveau natif.
- Injecter un **badge header** `{{ mode | upper }}` dans tous les templates `src/polycopy/monitoring/templates/*.md.j2` :
  - 🟢 SIMULATION
  - 🟢 DRY-RUN
  - 🔴 LIVE
- Préserver **textuellement** les garde-fous M3 et M8 :
  - `RuntimeError` boot si `execution_mode == "live"` ET clés absentes.
  - Lazy init `ClobWriteClient` (jamais instancié sauf LIVE).
  - Triple `assert execution_mode == "live"` avant chaque `create_and_post_order`.
  - 4ᵉ M8 : `assert execution_mode == "dry_run"` avant `_persist_realistic_simulated`.
- Définir la sémantique du mode `SIMULATION` (nouveau) : backtest offline, pas d'appel réseau, fixtures locales, `stop_event` local au run.

**B. Log hygiene**

- Processor structlog `filter_noisy_endpoints` qui `raise structlog.DropEvent` sur `dashboard_request` 2xx (ou 3xx) des paths polling whitelist, inséré **avant** `TimeStamper` et `JSONRenderer` (économie CPU formatage).
- Liste des paths par défaut : `^/api/health-external$`, `^/partials/.*$`, `^/api/version$`. Configurable via `DASHBOARD_LOG_SKIP_PATHS`.
- Les erreurs 4xx/5xx **passent toujours** (observabilité préservée sur les crashes middleware).
- Lecteur `/logs` (`log_reader.py`) : nouvelle constante `_DEFAULT_EXCLUDED_EVENTS = frozenset({"dashboard_request"})` appliquée automatiquement aux routes `/logs` + `/partials/logs-tail`. Opt-in via query `events=dashboard_request`.
- Template `logs.html` : bouton preset "Business events only" (actif par défaut) vs "Include HTTP access" (opt-in). Choix persisté en `localStorage` clé `polycopy.logs.preset`, cohérent avec M6 `polycopy.theme`.

### 2.2 Non-objectifs

- Pas de changement scoring (reste v1, c'est M12 qui introduit v2).
- Pas de WebSocket CLOB (c'est M11).
- Pas de cache Gamma adaptatif (c'est M11).
- Pas de taker fees dynamiques (c'est M13).
- Pas de nouveau onglet dashboard au-delà du preset `/logs` existant.
- Pas de refactor executor au-delà du rename `dry_run` → `execution_mode` (zéro refactor path live M3, zéro refactor path M8 `_persist_realistic_simulated`).
- Pas de migration DB. `MyOrder.is_dry_run`, `MyOrder.simulated`, `MyOrder.realistic_fill`, `MyPosition.simulated`, `PnlSnapshot.is_dry_run` **inchangés** : la parité est un changement de *comportement runtime*, pas de *stockage* (cf. synthèse §3.3).
- Pas de nouveau template Telegram. Juste un badge header 1 ligne ajouté à chaque template existant.
- Pas de canal Telegram séparé pour DRY_RUN. Le badge visuel suffit (cf. §12 open question).
- Pas de refactor processors structlog M1..M8 (cf. CLAUDE.md §Logs file M9). On **ajoute** un processor au début de la chaîne, c'est tout.

---

## 3. Design A : 3 modes d'exécution + parité

### 3.1 Taxonomie des modes — tableau comportemental exhaustif

Source : synthèse §3.1, inspiration Perplexity §3.3 (3 modes) contre Gemini §3.1 (binaire).

| Comportement | SIMULATION | DRY_RUN | LIVE |
|---|---|---|---|
| **Watcher** — Data API polling | Stub (fixtures `tests/fixtures/activity_sample.json`) | Actif (réseau) | Actif |
| **Strategy** — Gamma + CLOB midpoint | Stub local | Actif | Actif |
| **Executor** — `ClobWriteClient.post_order` | Jamais (pas instancié) | Jamais (pas instancié, path M8 ou stub M3 selon flag) | Actif (`create_and_post_order`) |
| **Realistic fill M8** — `/book` + `simulate_fill` | Non (fixture book) | Oui si `DRY_RUN_REALISTIC_FILL=true` | Non |
| **`DryRunResolutionWatcher`** | Non (résolution backtest différée) | Oui si `DRY_RUN_REALISTIC_FILL=true` | Non |
| **`PnlSnapshotWriter`** — insert DB | Actif (mémoire in-process OU fichier temporaire) | Actif (SQLite, `is_dry_run=true`) | Actif (SQLite, `is_dry_run=false`) |
| **Kill switch** — `stop_event.set()` | Actif, **local au run backtest** (coupe la simulation en cours, pas global) | **Actif identique LIVE** | Actif |
| **Alerte `kill_switch_triggered`** | Logged CRITICAL, Telegram no-op (pas de token attendu) | **CRITICAL Telegram**, badge 🟢 DRY-RUN | CRITICAL Telegram, badge 🔴 LIVE |
| **Alerte `pnl_snapshot_drawdown`** | Logged WARNING | WARNING Telegram, badge 🟢 DRY-RUN | WARNING Telegram, badge 🔴 LIVE |
| **Alerte `order_filled_large`** | N/A (aucun fill simulé en mode fixture pure) | INFO Telegram, badge 🟢 DRY-RUN | INFO Telegram, badge 🔴 LIVE |
| **Alerte `executor_auth_fatal`** | N/A (pas d'auth) | N/A (pas d'auth) | CRITICAL Telegram, badge 🔴 LIVE |
| **Dashboard /pnl toggle** | `?mode=simulation` (v1 : hidden dans UI, reportable M10.1) | `?mode=dry_run` | `?mode=real` |
| **Canal Telegram** | N/A | Même `TELEGRAM_CHAT_ID` que live, badge distingue | Même `TELEGRAM_CHAT_ID` |
| **Creds CLOB** | Jamais consommées | Jamais consommées (M8 invariant préservé) | Consommées |
| **Dry-run virtual drawdown INFO** (M8) | **Supprimé** (pas de double alerte) | **Supprimé** (remplacé par le vrai kill_switch_triggered CRITICAL) | — |

**Le changement majeur vs M8 actuel** : `DRY_RUN` devient un miroir fidèle de `LIVE` sur le front observabilité (kill switch + alertes). Le path exécution reste simulé (M3 stub ou M8 realistic selon flag), mais tout ce qui déclenche une alerte, un arrêt, ou un event structlog log le fait **comme en live**.

### 3.2 Configuration enum + deprecation `DRY_RUN`

#### 3.2.1 Nouveau champ `Settings.execution_mode`

Dans `config.py` (cf. §6.1 pour le diff concret), remplacer la ligne 77 `dry_run: bool = True` par :

```python
execution_mode: Literal["simulation", "dry_run", "live"] = Field(
    "dry_run",
    description=(
        "Mode d'exécution M10. "
        "'simulation' = backtest offline (fixtures, pas de réseau). "
        "'dry_run' = pipeline complet online, simulation fill (stub M3 ou "
        "realistic M8 selon DRY_RUN_REALISTIC_FILL), alertes + kill switch "
        "identiques à LIVE (badge visuel différent). "
        "'live' = exécution réelle CLOB. Backward-compat : legacy DRY_RUN=true "
        "ou DRY_RUN=false est lu avec warning de deprecation (1 version)."
    ),
)
```

#### 3.2.2 Backward-compat legacy `DRY_RUN`

Validator Pydantic `@model_validator(mode="before")` (car on veut manipuler les raw env vars avant validation des autres champs) :

```python
@model_validator(mode="before")
@classmethod
def _migrate_legacy_dry_run(cls, data: Any) -> Any:
    # data = dict des raw values (env + .env + defaults)
    if not isinstance(data, dict):
        return data
    raw_legacy = data.get("dry_run") or data.get("DRY_RUN")
    explicit_mode = data.get("execution_mode") or data.get("EXECUTION_MODE")
    if raw_legacy is not None and explicit_mode is None:
        # Traduction legacy → nouveau
        if str(raw_legacy).lower() in {"true", "1", "yes", "on"}:
            data["execution_mode"] = "dry_run"
        else:
            data["execution_mode"] = "live"
        # Warning dédié émis par cli/runner.py au boot (via structlog),
        # pas ici pour préserver la pureté du validator.
        data["_legacy_dry_run_detected"] = True
    return data
```

**Note** : le warning est émis **après** la construction de `Settings`, dans `cli/runner.py` (cf. §6.13), pour avoir la chaîne structlog déjà configurée. Pas de `warnings.warn` dans le validator (verbeux + pollue stderr avant config logging M9).

#### 3.2.3 Proxy `dry_run` en propriété lecture seule (1 version)

Pour minimiser la casse sur les tests et le code existant qui lit `settings.dry_run`, on ajoute une property :

```python
@property
def dry_run(self) -> bool:
    """Proxy deprecation-only — lit `execution_mode`. **Ne plus écrire**.

    Retourne True si `execution_mode in {"simulation", "dry_run"}`.
    Emet un `DeprecationWarning` au premier accès via un helper dédié
    (cf. `cli/runner.py`), **pas** ici (évite l'avalanche sur chaque lecture).
    """
    return self.execution_mode in {"simulation", "dry_run"}
```

Cette property **disparait à version+2** (cf. §11 rollout). Elle permet de rendre M10 non-invasif pour le code métier qui n'a pas besoin de distinguer simulation vs dry_run (ex: l'executor branche encore `if settings.dry_run: ... else: ...` et le comportement reste juste car `dry_run=True` en simulation ET dry_run).

Les sites qui **ont besoin** de distinguer simulation vs dry_run (ex: `MonitoringOrchestrator` qui lance `VirtualWalletStateReader`, ou le `DryRunResolutionWatcher`) sont mis à jour explicitement pour lire `execution_mode` (cf. §6.5, §6.7).

### 3.3 Kill switch parité — code avant/après `pnl_writer.py:96-171`

#### 3.3.1 Actuel (M8)

```python
async def _maybe_trigger_alerts(self, total, drawdown_pct, stop_event):
    """Kill switch + drawdown warning. **Jamais en dry-run** (spec §2.3)."""
    if self._settings.dry_run:
        self._maybe_push_dry_run_drawdown(total, drawdown_pct)  # INFO only
        return                                                   # ⟵ COURT-CIRCUIT
    threshold = self._settings.kill_switch_drawdown_pct
    if drawdown_pct >= threshold:
        log.error("kill_switch_triggered", ...)
        self._push_alert(Alert(level="CRITICAL", event="kill_switch_triggered", ...))
        stop_event.set()
        return
    # warning à 75 % du seuil
    ...

def _maybe_push_dry_run_drawdown(self, total, drawdown_pct):
    threshold = self._settings.kill_switch_drawdown_pct
    if drawdown_pct < _DRY_RUN_VIRTUAL_DRAWDOWN_RATIO * threshold:
        return
    self._push_alert(Alert(level="INFO", event="dry_run_virtual_drawdown", ...))
```

#### 3.3.2 Nouveau (M10)

```python
async def _maybe_trigger_alerts(self, total, drawdown_pct, stop_event):
    """Kill switch + drawdown warning — **identique dans les 3 modes**.

    En mode SIMULATION le ``stop_event`` est local au backtest (réduit la
    simulation en cours), pas global (aucun impact sur le bot hôte).
    La sémantique "stop" reste portée par le caller.
    """
    threshold = self._settings.kill_switch_drawdown_pct
    mode = self._settings.execution_mode
    if drawdown_pct >= threshold:
        log.error(
            "kill_switch_triggered",
            mode=mode,
            drawdown_pct=drawdown_pct,
            threshold=threshold,
            total_usdc=total,
        )
        self._push_alert(
            Alert(
                level="CRITICAL",
                event="kill_switch_triggered",
                body=(
                    f"Kill switch — drawdown {drawdown_pct:.2f}% "
                    f">= seuil {threshold:.2f}%. "
                    f"total_usdc={total:.2f}. Stop du bot."
                ),
                cooldown_key="kill_switch",
            ),
        )
        stop_event.set()
        return
    warning_threshold = _DRAWDOWN_WARNING_RATIO * threshold
    if drawdown_pct >= warning_threshold:
        self._push_alert(
            Alert(
                level="WARNING",
                event="pnl_snapshot_drawdown",
                body=(
                    f"Drawdown warning — {drawdown_pct:.2f}% "
                    f"(seuil kill switch {threshold:.2f}%)."
                ),
                cooldown_key="drawdown_warning",
            ),
        )
```

**Diff net** :

- Supprime la branche `if self._settings.dry_run: ... return` (2 lignes + 1 call).
- Supprime la méthode `_maybe_push_dry_run_drawdown` (15 lignes).
- Supprime la constante `_DRY_RUN_VIRTUAL_DRAWDOWN_RATIO` (1 ligne).
- Ajoute `mode=self._settings.execution_mode` dans le log `kill_switch_triggered` + dans le context utilisé pour `Alert.body` (via le badge du template — cf. §3.4).

Les tests `test_pnl_writer_m8_mode.py` et `test_pnl_snapshot_writer.py::test_dry_run_writes_snapshot_and_never_kills` inversent leur invariant (cf. §8.2).

### 3.4 Alertes Telegram parité + badges

#### 3.4.1 Injection `mode` dans chaque contexte template

Plutôt que modifier 15 templates avec des conditions `{% if dry_run %}`, on injecte **systématiquement** le champ `mode` dans le context Jinja2 via un helper `AlertRenderer._inject_mode(base_ctx)`.

Modification `AlertRenderer.render_alert()` (cf. §6.4 diff précis) :

```python
def render_alert(self, alert: Alert) -> str:
    template_name = f"{alert.event}.md.j2"
    context = {
        "event_type": alert.event,
        "level": alert.level,
        "body": alert.body,
        "emoji": _LEVEL_EMOJI.get(alert.level, ""),
        "mode": self._mode,                # ⟵ NOUVEAU
        "mode_badge": _MODE_BADGE[self._mode],  # ⟵ NOUVEAU (ex: "🟢 DRY-RUN")
    }
    ...
```

où `_MODE_BADGE` est :

```python
_MODE_BADGE: dict[str, str] = {
    "simulation": "🟢 SIMULATION",
    "dry_run":    "🟢 DRY-RUN",
    "live":       "🔴 LIVE",
}
```

L'`AlertRenderer` prend `mode: str` en paramètre de constructor :

```python
class AlertRenderer:
    def __init__(self, project_root: Path | None = None, mode: str = "dry_run") -> None:
        self._mode = mode
        ...
```

Le `MonitoringOrchestrator` (cf. §6.5) construit l'`AlertRenderer(mode=settings.execution_mode)` au boot.

#### 3.4.2 Modification minimale des 15 templates

Chaque template `*.md.j2` (cf. §6.4) gagne **une ligne au-dessus de la première** :

```jinja
_\[{{ mode_badge | telegram_md_escape }}\]_
```

Escape obligatoire car le badge contient des caractères `-` qui sont réservés en MarkdownV2.

Les templates `startup.md.j2`, `shutdown.md.j2`, `daily_summary.md.j2`, `heartbeat.md.j2`, `digest.md.j2` ont **déjà** un contexte `StartupContext.mode: Literal["dry-run", "live"]` (cf. `monitoring/dtos.py:64`) qu'on étend en `Literal["simulation", "dry-run", "live"]`. Les autres (alerte events) reçoivent le nouveau binding `mode_badge` injecté via `AlertRenderer`.

Liste exhaustive des 15 templates à toucher (grep `ls monitoring/templates/*.md.j2`) :

| Template | Ajout badge header |
|---|---|
| `daily_summary.md.j2` | En tête, après `{% from ... %}` |
| `digest.md.j2` | En tête |
| `discovery_cap_reached.md.j2` | En tête |
| `discovery_cycle_failed.md.j2` | En tête |
| `executor_auth_fatal.md.j2` | En tête (badge `🔴 LIVE` toujours — pas d'auth en dry-run) |
| `executor_error.md.j2` | En tête |
| `fallback.md.j2` | En tête |
| `heartbeat.md.j2` | En tête |
| `kill_switch_triggered.md.j2` | En tête — **critique pour le test de parité** |
| `order_filled_large.md.j2` | En tête |
| `pnl_snapshot_drawdown.md.j2` | En tête |
| `shutdown.md.j2` | En tête |
| `startup.md.j2` | Retrait du `_Mode :_ {{ mode }}` existant ligne 4 (redondant avec badge) |
| `trader_demoted.md.j2` | En tête |
| `trader_promoted.md.j2` | En tête |

Templates user custom dans `assets/telegram/` (cascade `FileSystemLoader` cf. `alert_renderer.py:60-66`) : si un user a un template qui n'utilise pas `mode_badge`, le `StrictUndefined` de Jinja crash **explicitement** au 1er rendu. Behavior désiré (fail-fast, mieux qu'un silent fallback). Documenté en §10.

### 3.5 Garde-fous M3 et M8 — réaffirmés textuellement

Les 4 garde-fous du CLAUDE.md actuel sont **préservés intégralement**, avec rename `dry_run` → `execution_mode` :

#### 3.5.1 Garde-fou 1 — Lazy init `ClobWriteClient` (M3)

`executor/orchestrator.py:71-73` actuel :

```python
write_client: ClobWriteClient | None = None
if self._settings.dry_run is False:
    write_client = ClobWriteClient(self._settings)
```

Nouveau :

```python
write_client: ClobWriteClient | None = None
if self._settings.execution_mode == "live":
    write_client = ClobWriteClient(self._settings)
```

Aucune autre modification. Les 3 modes autres que LIVE n'instancient jamais ce client.

#### 3.5.2 Garde-fou 2 — `RuntimeError` boot si clés absentes (M3)

`executor/orchestrator.py:44-50` actuel :

```python
if settings.dry_run is False and (
    settings.polymarket_private_key is None or settings.polymarket_funder is None
):
    raise RuntimeError("Executor cannot start without Polymarket credentials...")
```

Nouveau :

```python
if settings.execution_mode == "live" and (
    settings.polymarket_private_key is None or settings.polymarket_funder is None
):
    raise RuntimeError(
        "Executor cannot start without Polymarket credentials when "
        "EXECUTION_MODE=live. Set POLYMARKET_PRIVATE_KEY and "
        "POLYMARKET_FUNDER in .env, or use EXECUTION_MODE=dry_run."
    )
```

Exception levée **avant** le TaskGroup (inchangé). Test `test_executor_orchestrator.py::test_orchestrator_raises_without_creds_in_live_mode` mis à jour (cf. §8.2).

#### 3.5.3 Garde-fou 3 — `assert execution_mode == "live"` avant POST (M3)

`executor/pipeline.py:190-191` actuel :

```python
if settings.dry_run:  # double check, defense in depth §2.3
    raise RuntimeError("dry_run flipped between checks (bug)")
```

Nouveau :

```python
if settings.execution_mode != "live":  # double check, defense in depth §2.3
    raise RuntimeError(
        f"execution_mode flipped to {settings.execution_mode!r} between "
        "checks — MUST be 'live' at this point (bug)"
    )
```

#### 3.5.4 Garde-fou 4 — `assert execution_mode == "dry_run"` avant realistic fill (M8)

`executor/pipeline.py:355-357` actuel :

```python
assert settings.dry_run is True, (
    "_persist_realistic_simulated must NEVER run in live mode (M8 4th guardrail breached)."
)
```

Nouveau :

```python
assert settings.execution_mode == "dry_run", (  # noqa: S101 — defense in depth invariant
    f"_persist_realistic_simulated must ONLY run in dry_run mode "
    f"(got {settings.execution_mode!r}) — M8 4th guardrail breached."
)
```

**Note** : on **n'autorise pas** `_persist_realistic_simulated` en mode `simulation` parce que le mode simulation utilise des fixtures de book locales, pas `ClobOrderbookReader`. Le dispatch SIMULATION vs DRY_RUN est fait en amont dans le pipeline (cf. §3.6).

### 3.6 Sémantique `SIMULATION` — kill switch local vs global

Le mode SIMULATION est un **stub M3-like** : il n'utilise pas `ClobOrderbookReader` et n'appelle **aucun** endpoint réseau (`Data API`, `Gamma`, `CLOB`, `Polymarket WS`). Il sert au backtest offline pur.

#### 3.6.1 Pipeline SIMULATION

```python
# executor/pipeline.py — branche nouvelle, entre M8 realistic et stub M3
if settings.execution_mode == "simulation":
    # Fill instantané au prix demandé (sans slippage book), comme M3 stub
    # mais sans persistance autre que mémoire (session in-memory SQLite ou
    # caller-provided repo).
    return await _persist_simulated_stub(...)  # réutilise M3 stub
```

Effectivement, SIMULATION se comporte comme DRY_RUN + `DRY_RUN_REALISTIC_FILL=false` côté pipeline fill. La différence est ailleurs :

- **Pas de réseau** : le `WatcherOrchestrator` en mode SIMULATION lit depuis un fichier `tests/fixtures/activity_sample.jsonl` au lieu de polling `data-api.polymarket.com`. Un stub `FixtureWatcher` non-réseau est introduit (cf. §6, hors scope strict M10 — v1 M10 **ne livre pas** le FixtureWatcher, seulement la **taxonomie** et les garde-fous pour que M10.1 ou M12 backtest l'ajoute). Cette décision est tranchée en §12 open question #2.
- **`stop_event` local** : un orchestrator SIMULATION est censé être lancé par un harness de backtest qui fournit son propre `stop_event`. Quand le kill switch `set()` cet event, le backtest se termine proprement — sans coup d'arrêt sur un bot hôte concurrent.

#### 3.6.2 v1 M10 — scope minimal SIMULATION

Pour rester dans l'enveloppe 1-semaine, v1 M10 **ne livre pas** de runner SIMULATION complet. On livre :

- ✅ L'enum `execution_mode == "simulation"` accepté par Pydantic.
- ✅ Le validator de cohérence cross-field (SIMULATION + creds LIVE incohérent mais pas bloquant).
- ✅ Les garde-fous qui distinguent correctement SIMULATION des 2 autres modes dans le pipeline (assertions, dispatch stub).
- ✅ Le kill switch actif mais **sans** Telegram output forcé (si pas de token, no-op — comme live sans token).
- ❌ Pas de `FixtureWatcher` / `FixtureGammaClient` / `FixtureClobClient`. **Reportable à M12 backtest** (synthèse §7.1).
- ❌ Pas de dashboard `/pnl?mode=simulation` (v1 : la valeur query string est lue mais non câblée à une série ; le toggle UI ne propose que real/dry_run/both, cf. M8 §9.11). Reportable.

Un utilisateur qui pose `EXECUTION_MODE=simulation` aujourd'hui sans runner backtest voit donc le bot **crasher au démarrage du watcher** (pas de stub réseau). C'est intentionnel et documenté en §10 risque + §12 open question #2.

---

## 4. Design B : log hygiene

### 4.1 Processor structlog `filter_noisy_endpoints`

Nouveau processor inséré **en premier** dans la chaîne structlog (avant `add_log_level`, donc avant tout le pipeline coûteux). L'intérêt : si on drop, on économise même le coût de `TimeStamper` (`strftime` est mesurable à haut débit).

Fichier : `src/polycopy/cli/logging_config.py` (extension, pas nouveau module).

```python
import re
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterable

_DEFAULT_NOISY_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/api/health-external$"),
    re.compile(r"^/partials/.*$"),
    re.compile(r"^/api/version$"),
)


def make_filter_noisy_endpoints(
    extra_patterns: "Iterable[str] | None" = None,
) -> structlog.types.Processor:
    """Factory : retourne un processor qui drop les dashboard_request 2xx/3xx noisy.

    Usage:
        processor = make_filter_noisy_endpoints(settings.dashboard_log_skip_paths)
        structlog.configure(processors=[processor, ...])

    Logique:
    - Laisse passer tout event qui n'est pas ``dashboard_request``.
    - Laisse passer les statuts 4xx/5xx (observabilité errors).
    - Drop les 2xx/3xx dont le path match la whitelist.

    La whitelist default est intentionnellement courte (3 patterns) — le but
    n'est pas de tout filtrer mais de supprimer les 3 patterns qui représentent
    ~95% du volume sur un Home actif (cf. synthèse §2.3 mesures).
    """
    compiled = list(_DEFAULT_NOISY_PATH_PATTERNS)
    if extra_patterns:
        for pat in extra_patterns:
            compiled.append(re.compile(pat))

    def _processor(logger, method_name, event_dict):
        if event_dict.get("event") != "dashboard_request":
            return event_dict
        status = event_dict.get("status", 200)
        if isinstance(status, int) and status >= 400:
            return event_dict  # errors always pass
        path = event_dict.get("path", "")
        for pattern in compiled:
            if pattern.match(path):
                raise structlog.DropEvent
        return event_dict

    return _processor
```

**Pourquoi factory** : pour pouvoir injecter `settings.dashboard_log_skip_paths` (env var surcharge) sans coupler `logging_config.py` à `config.py` au load (import circulaire potentiel avec CLI runner).

### 4.2 Intégration chaîne structlog — ordre des processors

Modification `logging_config.py:86-97`. Avant (M9) :

```python
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    ...
)
```

Après (M10) :

```python
structlog.configure(
    processors=[
        make_filter_noisy_endpoints(skip_paths),  # ⟵ EN PREMIER (économise CPU)
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    ...
)
```

`skip_paths` est lu via `settings.dashboard_log_skip_paths` (nouvelle env var §5). L'ordre est critique : le processor drop **avant** `TimeStamper` (économie `strftime`) et **avant** `JSONRenderer` (économie sérialisation dict).

**Note performance** : `re.match` sur 3 patterns précompilés = ~2 µs par requête. À 10 req/s dashboard polling = 20 µs/s. Vs `TimeStamper + JSONRenderer` combinés ~40-80 µs par requête droppée économisée. Gain net modeste en CPU mais important en volume fichier (ratio 28:1 → ~0.5:1 selon synthèse §2.3).

### 4.3 Lecteur `/logs` — exclusion par défaut

#### 4.3.1 Constante default + helper

Dans `src/polycopy/dashboard/log_reader.py`, ajouter au top du module :

```python
_DEFAULT_EXCLUDED_EVENTS: frozenset[str] = frozenset({"dashboard_request"})
```

Et modifier `filter_entries` pour accepter un paramètre `exclude_events`:

```python
def filter_entries(
    entries: list[LogEntry],
    *,
    levels: set[str] | None = None,
    event_types: set[str] | None = None,
    q: str | None = None,
    exclude_events: frozenset[str] | None = None,
) -> list[LogEntry]:
    result = entries
    if exclude_events:
        result = [e for e in result if e.event not in exclude_events]
    if levels:
        ...
```

Priorité : `exclude_events` **avant** les autres filtres (supprimer le bruit dès que possible), mais **après** `event_types` ? Non : l'utilisateur peut explicitement demander `events=dashboard_request` (opt-in), auquel cas on doit **ne pas** appliquer `exclude_events`. La logique de routes (cf. §6.10) résout ça :

```python
exclude = None if "dashboard_request" in validated_events else _DEFAULT_EXCLUDED_EVENTS
```

#### 4.3.2 Application dans les routes

`dashboard/routes.py:186-192` (page `/logs`) et `dashboard/routes.py:410-416` (partial `/partials/logs-tail`) passent le nouveau param à `filter_entries`. Diff en §6.10.

### 4.4 UI template preset — bouton localStorage

#### 4.4.1 Modification `templates/logs.html`

Ajout d'un bouton preset au-dessus du form de filtres existants (`logs.html:25-70`). Positionné dans la même bande horizontale que les autres filtres :

```html
<div class="flex items-center gap-2 mr-4">
  <span class="text-xs" style="color: var(--color-muted);">Preset :</span>
  <button type="button" id="preset-business"
          class="text-xs rounded-md border px-2 py-1"
          style="border-color: var(--color-border); color: var(--color-text);">
    Business events only
  </button>
  <button type="button" id="preset-access"
          class="text-xs rounded-md border px-2 py-1"
          style="border-color: var(--color-border); color: var(--color-muted);">
    Include HTTP access
  </button>
</div>
```

#### 4.4.2 JavaScript preset + localStorage

Sous le bloc JS existant pour `live-tail-toggle` :

```javascript
(function () {
  const LS_KEY = 'polycopy.logs.preset';
  const form = document.getElementById('logs-filter-form');
  const btnBusiness = document.getElementById('preset-business');
  const btnAccess = document.getElementById('preset-access');
  if (!form || !btnBusiness || !btnAccess) return;

  const eventsInput = form.querySelector('input[name="events"]');
  const initial = localStorage.getItem(LS_KEY) || 'business';

  function applyPreset(name) {
    if (!eventsInput) return;
    if (name === 'access') {
      // Opt-in : ajoute explicitement dashboard_request à la liste
      const existing = (eventsInput.value || '').split(',').map(s => s.trim()).filter(Boolean);
      if (!existing.includes('dashboard_request')) existing.push('dashboard_request');
      eventsInput.value = existing.join(',');
      btnAccess.style.color = 'var(--color-text)';
      btnBusiness.style.color = 'var(--color-muted)';
    } else {
      // Default : retire dashboard_request s'il y est
      const existing = (eventsInput.value || '').split(',').map(s => s.trim())
                         .filter(s => s && s !== 'dashboard_request');
      eventsInput.value = existing.join(',');
      btnBusiness.style.color = 'var(--color-text)';
      btnAccess.style.color = 'var(--color-muted)';
    }
    localStorage.setItem(LS_KEY, name);
    htmx.trigger(form, 'submit');
  }

  btnBusiness.addEventListener('click', () => applyPreset('business'));
  btnAccess.addEventListener('click', () => applyPreset('access'));
  applyPreset(initial);
})();
```

Cohérent avec M6 pattern `polycopy.theme` (localStorage clé unique, `polycopy.*` namespace).

---

## 5. Configuration — env vars

### 5.1 Ajoutées

| Env var | Champ Settings | Type | Default | Description |
|---|---|---|---|---|
| `EXECUTION_MODE` | `execution_mode` | `Literal["simulation", "dry_run", "live"]` | `"dry_run"` | Mode d'exécution M10. Remplace `DRY_RUN: bool`. Lu via `Field` + validator migration legacy. |
| `DASHBOARD_LOG_SKIP_PATHS` | `dashboard_log_skip_paths` | `list[str]` (CSV ou JSON) | `[]` (⇒ defaults `^/api/health-external$`, `^/partials/.*$`, `^/api/version$` en plus) | Patterns regex supplémentaires de paths pour lesquels les `dashboard_request` 2xx/3xx sont droppés au niveau processor structlog. Additif aux defaults hardcodés. |

### 5.2 Modifiées

| Env var | Changement |
|---|---|
| `DRY_RUN` | **Déprécié**. Encore lu 1 version (`true` → `execution_mode=dry_run`, `false` → `execution_mode=live`) avec `DeprecationWarning` émis au boot par `cli/runner.py`. Supprimé à version+2. |

### 5.3 Dépréciées / supprimées

Aucune env var supprimée à M10 (on garde `DRY_RUN` lu 1 version). Les env vars M8 (`DRY_RUN_REALISTIC_FILL`, `DRY_RUN_VIRTUAL_CAPITAL_USD`, `DRY_RUN_BOOK_CACHE_TTL_SECONDS`, `DRY_RUN_RESOLUTION_POLL_MINUTES`, `DRY_RUN_ALLOW_PARTIAL_BOOK`) sont **inchangées** — le préfixe `DRY_RUN_` fait sens avec la nouvelle enum (ces flags ne s'activent qu'en `execution_mode=dry_run`).

### 5.4 `.env.example` — mise à jour

Remplacer la ligne `DRY_RUN=true` par :

```dotenv
# Mode d'exécution M10 : "simulation" | "dry_run" | "live"
# simulation = backtest offline (pas de réseau, fixtures locales)
# dry_run    = pipeline complet online, simulation fill + MÊMES alertes/kill switch que LIVE
# live       = exécution réelle CLOB (nécessite POLYMARKET_PRIVATE_KEY + POLYMARKET_FUNDER)
EXECUTION_MODE=dry_run

# [M10] Patterns regex de paths dont les dashboard_request 2xx/3xx sont filtrés
# au niveau processor structlog (avant écriture fichier).
# Additif aux defaults : ^/api/health-external$, ^/partials/.*$, ^/api/version$
# DASHBOARD_LOG_SKIP_PATHS=^/internal/debug$,^/custom/noisy$
```

---

## 6. Changements module par module (file:line)

### 6.1 `src/polycopy/config.py`

**Contexte actuel** : ligne 77 `dry_run: bool = True`. Les env vars M8 `dry_run_*` sont lignes 79-122. Le modèle `@model_validator(mode="after")` existe ligne 450 pour `_validate_discovery_thresholds`.

**Diff M10** :

- Ligne 77 : remplacer `dry_run: bool = True` par :

  ```python
  execution_mode: Literal["simulation", "dry_run", "live"] = Field(
      "dry_run",
      description="...",  # voir §3.2.1
  )
  ```

- Ajouter un `@model_validator(mode="before")` **avant** les `@field_validator` existants : `_migrate_legacy_dry_run` (code §3.2.2).
- Ajouter la property `dry_run` en bas de classe (§3.2.3). Avec décorateur `@property` + `@deprecated` ? Non : `warnings.deprecated` est Python 3.13. On reste sur docstring + détection au boot (cf. §6.13).
- Ajouter la nouvelle env var `dry_run_skip_paths` entre la section `Dashboard M9` et `Discovery M5` :

  ```python
  dashboard_log_skip_paths: Annotated[list[str], NoDecode] = Field(default_factory=list)

  @field_validator("dashboard_log_skip_paths", mode="before")
  @classmethod
  def _parse_skip_paths(cls, v: object) -> object:
      """Accepte CSV (`^/a$,^/b$`) ou JSON array (`["^/a$","^/b$"]`)."""
      if isinstance(v, str):
          stripped = v.strip()
          if not stripped:
              return []
          if stripped.startswith("["):
              return json.loads(stripped)
          return [item.strip() for item in stripped.split(",") if item.strip()]
      return v
  ```

### 6.2 `src/polycopy/monitoring/pnl_writer.py`

**Contexte actuel** : ligne 27 `_DRY_RUN_VIRTUAL_DRAWDOWN_RATIO: float = 0.5`. Ligne 96 `_maybe_trigger_alerts`. Lignes 108-110 `if self._settings.dry_run: self._maybe_push_dry_run_drawdown(...); return`. Lignes 153-171 `_maybe_push_dry_run_drawdown`.

**Diff M10** :

- **Supprimer** ligne 27 `_DRY_RUN_VIRTUAL_DRAWDOWN_RATIO`.
- **Supprimer** lignes 108-110 (court-circuit dry-run).
- **Supprimer** lignes 153-171 (méthode `_maybe_push_dry_run_drawdown`).
- **Modifier** ligne 53 `log.info("pnl_snapshot_writer_started", interval=interval, dry_run=self._settings.dry_run)` → `mode=self._settings.execution_mode`.
- **Modifier** ligne 73 `only_real = not self._settings.dry_run` → `only_real = self._settings.execution_mode == "live"`.
- **Modifier** ligne 84 `is_dry_run=self._settings.dry_run` → `is_dry_run=(self._settings.execution_mode != "live")` **inchangé sémantiquement** (le stockage reste booléen pour l'instant — cf. §2.2 non-objectifs).
- **Modifier** ligne 92 `is_dry_run=self._settings.dry_run` (dans le log `pnl_snapshot_written`) → ajout d'un champ `mode=self._settings.execution_mode`.
- **Ajouter** à l'`Alert.body` du kill_switch et du drawdown warning : le badge est injecté côté `AlertRenderer` via le mode_badge. Pas de modif du body côté writer.

### 6.3 `src/polycopy/monitoring/telegram_notifier.py`

**Note** : ce fichier n'existe pas (le brief le mentionne mais le code actuel utilise `telegram_client.py` + `alert_dispatcher.py` + `alert_renderer.py`). Le diff est sur ces 3 fichiers plus les notifiers dédiés (`startup_notifier.py`, `heartbeat_scheduler.py`, `daily_summary_scheduler.py`).

- `telegram_client.py` : **aucune modification**. Transport uniquement.
- `alert_dispatcher.py` : injection du `renderer` qui porte le `mode`, aucune modif logique (le renderer existe déjà §6.4).
- `startup_notifier.py:77` : remplacer `mode = "dry-run" if self._settings.dry_run else "live"` par `mode = self._settings.execution_mode`. Mise à jour du type `StartupContext.mode` (cf. §6.4 dtos).
- `heartbeat_scheduler.py`, `daily_summary_scheduler.py` : aucune modif (les contexts n'ont pas de `mode` spécifique, le badge vient du template via le binding injecté par `AlertRenderer`).

### 6.4 `src/polycopy/monitoring/templates/*.md.j2` + `assets/telegram/`

Modifications §3.4.2 sur les 15 templates. Exemple `kill_switch_triggered.md.j2` actuel :

```jinja
🚨 *\[kill\_switch\_triggered\]* CRITICAL

{{ body | telegram_md_escape }}

_Action requise :_ vérifier les positions ouvertes et l'état du wallet\. Le bot est *arrêté*\.
```

Nouveau :

```jinja
_\[{{ mode_badge | telegram_md_escape }}\]_
🚨 *\[kill\_switch\_triggered\]* CRITICAL

{{ body | telegram_md_escape }}

_Action requise :_ vérifier les positions ouvertes et l'état du wallet\. Le bot est *arrêté*\.
```

Même pattern pour les 14 autres. `startup.md.j2` voit en plus son `_Mode :_ {{ mode | telegram_md_escape }}` ligne 4 **retiré** (redondant avec le badge header).

Les fichiers `assets/telegram/*.md.j2` (user override cascade) ne sont **pas** modifiés par polycopy — responsabilité user d'ajouter le badge. `StrictUndefined` crash au rendu si `mode_badge` n'est pas référencé MAIS le template n'en a pas besoin (strictundefined crash uniquement sur lookup d'une var absente — si le template n'utilise pas `mode_badge`, pas de crash).

**Modification `dtos.py:62-68`** : étendre `StartupContext.mode: Literal["dry-run", "live"]` à `Literal["simulation", "dry_run", "live"]`. Note la différence de casse : le literal backend est `"dry_run"` (underscore, enum `execution_mode`), la représentation template humain-lisible passe par le filter `mode_badge` donc le literal backend est libre.

### 6.5 `src/polycopy/monitoring/orchestrator.py`

**Contexte actuel** : lignes 63-76 dispatchent entre `WalletStateReader` et `VirtualWalletStateReader` via `if self._settings.dry_run and self._settings.dry_run_realistic_fill`.

**Diff M10** :

- Ligne 64 : remplacer `if self._settings.dry_run and self._settings.dry_run_realistic_fill` par `if self._settings.execution_mode == "dry_run" and self._settings.dry_run_realistic_fill` (sémantique inchangée).
- Ligne 82 : `renderer = AlertRenderer()` → `renderer = AlertRenderer(mode=self._settings.execution_mode)`.
- Ligne 100 : log `monitoring_started` gagne `execution_mode=self._settings.execution_mode` (en plus de ou remplacement de `dry_run` absent du log actuel).

### 6.6 `src/polycopy/executor/pipeline.py`

**Contexte actuel** : ligne 110 `if settings.dry_run:`. Ligne 111 `if settings.dry_run_realistic_fill and orderbook_reader is not None:`. Ligne 148 `# 3) Branche réelle`. Ligne 190 `if settings.dry_run:` (double-check). Ligne 355 `assert settings.dry_run is True`.

**Diff M10** :

- Ligne 110-146 : remplacer la branche `if settings.dry_run:` par :

  ```python
  # 2) Branche SIMULATION / DRY_RUN (non-LIVE).
  if settings.execution_mode != "live":
      # SIMULATION dispatch: pas de call /book, pas de position virtuelle
      # persistée (handled par caller backtest) → comportement stub M3.
      if settings.execution_mode == "simulation":
          await _persist_simulation_stub(
              approved, tick_size=tick_size, neg_risk=neg_risk,
              price_rounded=price_rounded, order_repo=order_repo, bound=bound,
          )
          return
      # DRY_RUN : branche realistic fill M8 si activée, sinon stub M3.
      if settings.dry_run_realistic_fill and orderbook_reader is not None:
          await _persist_realistic_simulated(...)  # M8 path inchangé
          return
      # DRY_RUN stub M3 path inchangé
      await order_repo.insert(MyOrderDTO(...))
      bound.info("order_simulated", ...)
      return
  ```

  `_persist_simulation_stub` est une nouvelle fonction locale (~20 lignes), essentiellement identique au stub M3 actuel (lignes 123-146) mais qui **n'écrit pas** la position si `settings.execution_mode == "simulation"` (ou plus simplement : qui utilise `order_repo.insert_simulation()` — méthode v1 minimaliste qui insère avec `simulated=True, is_dry_run=True` comme M3 stub mais tagged pour que les queries dashboard puissent filtrer).

- Ligne 190 : modifier le double-check (§3.5.3).
- Ligne 355 : modifier l'assertion (§3.5.4).

### 6.7 `src/polycopy/executor/orchestrator.py`

**Contexte actuel** : lignes 44-50 `RuntimeError` boot. Lignes 71-73 lazy init `ClobWriteClient`. Ligne 76 `m8_enabled = self._settings.dry_run and self._settings.dry_run_realistic_fill`. Ligne 94 `mode = "real" if self._settings.dry_run is False else "dry_run"`.

**Diff M10** :

- Ligne 44-50 : modification texte RuntimeError (§3.5.2).
- Ligne 72 : `if self._settings.dry_run is False:` → `if self._settings.execution_mode == "live":`.
- Ligne 76 : `m8_enabled = ... self._settings.dry_run and ...` → `m8_enabled = self._settings.execution_mode == "dry_run" and self._settings.dry_run_realistic_fill`.
- Ligne 94 : `mode = "real" if self._settings.dry_run is False else "dry_run"` → `mode = self._settings.execution_mode`.

### 6.8 `src/polycopy/dashboard/middleware.py`

**Aucune modification** — le middleware continue d'émettre `dashboard_request` avec `method/path/status/duration_ms`. Le filtrage est déplacé un cran plus bas, dans le processor structlog (plus performant, §4.2).

**Justification** : si l'on filtre au niveau middleware (ex: `if path in noisy: return` sans log), on perd la capacité à **re-activer** temporairement le log via query param / env override sans redémarrage. Au niveau processor structlog, `DropEvent` est contrôlé par la config processors — plus souple.

### 6.9 `src/polycopy/dashboard/log_reader.py`

**Diff M10** :

- Ligne ~17 : ajouter `_DEFAULT_EXCLUDED_EVENTS = frozenset({"dashboard_request"})`.
- Ligne 93-115 : modifier la signature de `filter_entries` (ajout `exclude_events: frozenset[str] | None = None`), et ajouter la pass exclusion en tête (§4.3.1).
- `read_log_tail` : **inchangé**.
- `LogEntry` : **inchangé**.

### 6.10 `src/polycopy/dashboard/routes.py`

**Contexte actuel** : lignes 162-204 route `/logs`. Lignes 393-421 partial `/partials/logs-tail`. Ligne 187 `filter_entries(all_entries, levels=..., event_types=..., q=q)`.

**Diff M10** :

- Après `validated_events = _validate_events(events)` (ligne 185, 409) :

  ```python
  exclude = (
      None
      if "dashboard_request" in validated_events
      else _DEFAULT_EXCLUDED_EVENTS
  )
  ```

- Passer `exclude_events=exclude` à `filter_entries`.

### 6.11 `src/polycopy/dashboard/templates/logs.html`

**Diff M10** : §4.4.1 (ajout bouton preset) + §4.4.2 (ajout `<script>` localStorage).

### 6.12 `src/polycopy/cli/logging_config.py`

**Diff M10** :

- Ajouter les imports `re` + `typing.Iterable`.
- Ajouter `_DEFAULT_NOISY_PATH_PATTERNS` + `make_filter_noisy_endpoints` (§4.1).
- Modifier la signature de `configure_logging` pour accepter `skip_paths: list[str] | None = None`.
- Insérer le processor en tête de la chaîne (§4.2).

### 6.13 `src/polycopy/cli/runner.py`

**Contexte actuel** : ligne 171-172 `if args.dry_run: settings.dry_run = True`. Ligne 178 `silent = (settings.cli_silent and not args.verbose) or args.no_cli`. Ligne 180-186 `configure_logging(...)`.

**Diff M10** :

- Ligne 49-52 : garder le flag CLI `--dry-run` mais le rediriger :

  ```python
  parser.add_argument(
      "--dry-run",
      action="store_true",
      help=(
          "[legacy] Force EXECUTION_MODE=dry_run. Préférer la variable "
          "d'environnement EXECUTION_MODE."
      ),
  )
  ```

- Ajouter un argument `--execution-mode` :

  ```python
  parser.add_argument(
      "--execution-mode",
      choices=["simulation", "dry_run", "live"],
      default=None,
      help="Force le mode d'exécution (override EXECUTION_MODE env).",
  )
  ```

- Lignes 171-172 : remplacer `if args.dry_run: settings.dry_run = True` par :

  ```python
  if args.execution_mode is not None:
      settings.execution_mode = args.execution_mode
  elif args.dry_run:
      settings.execution_mode = "dry_run"
      log.warning(
          "cli_deprecation_dry_run_flag",
          message="--dry-run is deprecated; use --execution-mode=dry_run",
      )
  ```

- Après `configure_logging(...)` et avant `asyncio.run(_async_main())`, ajouter le warning de deprecation `DRY_RUN` env var legacy :

  ```python
  if getattr(settings, "_legacy_dry_run_detected", False):
      log.warning(
          "config_deprecation_dry_run_env",
          message=(
              "DRY_RUN is deprecated since M10. "
              "Use EXECUTION_MODE=simulation|dry_run|live instead. "
              "DRY_RUN will be removed in version+2."
          ),
          resolved_execution_mode=settings.execution_mode,
      )
  ```

  Note : `_legacy_dry_run_detected` est posé par le validator `_migrate_legacy_dry_run` (§3.2.2). Passer ce flag par instance est inhabituel Pydantic v2 (extra fields interdits par défaut) — alternative propre : stocker dans une variable module-level `_LEGACY_DETECTED: bool = False` positionnée depuis le validator. Décision : **variable module-level** pour éviter de polluer le schéma Settings. Documenté §10 piège #4.

- Ligne 193-194 `render_status_screen(...)` : passer `settings.execution_mode` en plus pour le badge.

- Ligne 180-186 `configure_logging(...)` : ajouter `skip_paths=settings.dashboard_log_skip_paths`.

---

## 7. Plan d'implémentation

Ordre séquentiel, chaque étape testable isolément. Estimé ~1 semaine 1 dev.

### Étape 1 — Introduire `EXECUTION_MODE` avec fallback legacy (jour 1, matin)

- Modifier `config.py` §6.1.
- Ajouter les tests `test_execution_mode_enum_values`, `test_legacy_dry_run_true_maps_to_dry_run_mode`, `test_legacy_dry_run_false_maps_to_live_mode`, `test_explicit_execution_mode_wins_over_legacy`.
- Vérifier que `settings.dry_run` (property) retourne True pour `"simulation"` et `"dry_run"`, False pour `"live"`.
- Green sur `tests/unit/test_config.py` + `test_execution_mode_config.py` (nouveau).

### Étape 2 — Propager `mode` dans le pipeline observability (jour 1, après-midi)

- `pnl_writer.py` : supprimer court-circuit + `_DRY_RUN_VIRTUAL_DRAWDOWN_RATIO` + `_maybe_push_dry_run_drawdown`. Ajouter `mode=settings.execution_mode` dans les logs.
- `monitoring/orchestrator.py` : injecter `mode` dans `AlertRenderer(mode=...)`, passer `execution_mode` dans le log boot.
- `startup_notifier.py:77` : remplacer `mode = "dry-run" if ... else "live"`.
- **Pas encore** de renommage complet côté executor / pipeline (fait étape 3). Les tests qui lisent `settings.dry_run` via la property continuent de passer.
- Green sur `test_pnl_writer_m8_mode.py` **après inversion** (cf. étape 9).

### Étape 3 — Renommer `dry_run` → `execution_mode` côté executor (jour 2)

- `executor/orchestrator.py` §6.7 : 4 remplacements.
- `executor/pipeline.py` §6.6 : branche dispatch SIMULATION + assertions.
- Vérifier que les 4 garde-fous sont préservés (cf. §3.5).
- Green sur `test_executor_orchestrator.py`, `test_executor_pipeline.py`, `test_pipeline_m8_branch.py` (certains nécessitent update §8.2).

### Étape 4 — Alertes Telegram parité + badge (jour 3)

- `alert_renderer.py` §6.4 : constructor `mode=...`, dict `_MODE_BADGE`, injection `mode_badge` dans `render_alert`.
- 15 templates `*.md.j2` §6.4 : ajouter header badge.
- `startup.md.j2` : retirer `_Mode :_` ligne 4 (redondant).
- Green sur `test_alert_renderer.py`, `test_telegram_template_rendering.py` (update pour vérifier le badge).
- Nouveaux : `test_telegram_alert_shows_mode_badge_simulation/dry_run/live`.

### Étape 5 — SIMULATION stub minimal pipeline (jour 3, fin)

- `executor/pipeline.py` : fonction `_persist_simulation_stub` (v1 = alias du stub M3, pas de réseau mais réutilise `order_repo.insert`).
- **Pas** de `FixtureWatcher`. Cf. §3.6.2.
- Test `test_simulation_mode_dispatches_to_stub`.
- Documenter que SIMULATION + Watcher default (data-api polling) crash au boot (risque §10).

### Étape 6 — Processor structlog `filter_noisy_endpoints` (jour 4, matin)

- `cli/logging_config.py` §6.12 : `make_filter_noisy_endpoints` factory + insertion en tête.
- Signature `configure_logging(..., skip_paths=...)`.
- `cli/runner.py` : passer `settings.dashboard_log_skip_paths`.
- `config.py` : env var `DASHBOARD_LOG_SKIP_PATHS` + validator CSV/JSON.
- Tests `test_middleware_drops_noisy_endpoint_success`, `test_middleware_keeps_noisy_endpoint_error`, `test_skip_paths_env_override`.

### Étape 7 — Lecteur `/logs` exclusion default (jour 4, après-midi)

- `log_reader.py` §6.9 : `_DEFAULT_EXCLUDED_EVENTS`, param `exclude_events`.
- `dashboard/routes.py` §6.10 : passage param.
- Tests `test_logs_default_hides_dashboard_request`, `test_logs_opt_in_shows_dashboard_request`.

### Étape 8 — UI preset bouton (jour 4, fin)

- `templates/logs.html` §4.4.
- Test manuel navigateur (pas de test unit sur JS). Smoke test.

### Étape 9 — Inversion tests existants (jour 5, matin)

- `test_pnl_writer_m8_mode.py::test_dry_run_severe_drawdown_does_not_trigger_kill_switch` → renommer `test_dry_run_severe_drawdown_triggers_kill_switch_like_live` + inversion de l'assertion.
- `test_pnl_snapshot_writer.py::test_dry_run_writes_snapshot_and_never_kills` → idem.
- Voir §8.2 pour la liste complète.

### Étape 10 — Nouveaux tests (jour 5, matin)

- Liste §8.3.

### Étape 11 — CLI warning deprecation `DRY_RUN` (jour 5, après-midi)

- `cli/runner.py` §6.13 : warnings boot.
- Test `test_cli_deprecation_warning_on_legacy_dry_run_env`.
- Test `test_cli_deprecation_warning_on_dry_run_flag`.

### Étape 12 — Doc updates (jour 5, après-midi)

- `README.md` : section dry-run reformulée (dry-run = miroir live, pas sandbox silencieux). Warning encadré sur le kill switch en dry-run.
- `docs/architecture.md` : §Module Monitoring + §Module Executor mettent à jour la mention des 3 modes.
- `CLAUDE.md` : §9 ci-dessous (texte exact).
- `docs/setup.md` : §18 mentionner EXECUTION_MODE + deprecation.

Commit final unique : `feat(config,monitoring,executor,dashboard): M10 3-mode parity + log hygiene`.

---

## 8. Tests

### 8.1 À protéger (existants à ne pas casser)

Liste des tests existants qui **doivent continuer à passer** :

| Fichier | Test | Raison |
|---|---|---|
| `tests/unit/test_config.py:19` | `test_polymarket_keys_optional` | Config basique |
| `tests/unit/test_config.py:47` | `test_dry_run_default_true` | **Adapté** : property `settings.dry_run` retourne True si `execution_mode in {simulation, dry_run}`. Test **renommé** `test_execution_mode_default_is_dry_run` + adaptation. |
| `tests/unit/test_pnl_snapshot_writer.py:119` | `test_real_mode_kill_switch_triggered` | Non-régression live — invariant fort à préserver |
| `tests/unit/test_pnl_snapshot_writer.py:157` | `test_real_mode_drawdown_warning_no_stop` | Warning 75% en live — inchangé |
| `tests/unit/test_pnl_writer_m8_mode.py:140` | `test_real_mode_still_triggers_kill_switch` | Non-régression |
| `tests/unit/test_executor_orchestrator.py::test_orchestrator_raises_without_creds_in_real_mode` | — | Garde-fou 2 M3, **renommer** en `..._in_live_mode` |
| `tests/unit/test_pipeline_m8_branch.py:262` | `test_4th_guardrail_assert_dry_run_true` | Garde-fou 4 M8 — **renommer** + adapter à `execution_mode == "dry_run"` |
| `tests/unit/test_pipeline_m8_branch.py:117` | `test_dry_run_realistic_off_uses_m3_stub_path` | M8 stub path |
| `tests/unit/test_pipeline_m8_branch.py:139` | `test_dry_run_realistic_on_uses_m8_branch` | M8 realistic path |
| `tests/unit/test_pipeline_m8_branch.py:168` | `test_live_mode_never_fetches_book` | Live path stricte |
| `tests/unit/test_dashboard_logs_route.py:91` | `test_logs_page_renders_entries` | Route `/logs` basique |
| `tests/unit/test_dashboard_logs_route.py:104` | `test_logs_filter_by_level` | Filtres existants |
| `tests/unit/test_dashboard_logs_route.py:118` | `test_logs_filter_by_q_substring` | Filtres existants |
| `tests/unit/test_dashboard_logs_route.py:130` | `test_logs_filter_invalid_level_returns_400` | Validation |
| `tests/unit/test_dashboard_logs_route.py:139` | `test_logs_filter_q_too_long_returns_422` | Validation |
| `tests/unit/test_dashboard_logs_route.py:148` | `test_logs_filter_too_many_events_returns_400` | Validation |
| `tests/unit/test_dashboard_logs_route.py:158` | `test_logs_disabled_renders_stub` | Feature flag |
| `tests/unit/test_dashboard_logs_route.py:168` | `test_partials_logs_tail_returns_fragment` | Partial HTMX |
| `tests/unit/test_dashboard_logs_route.py:191` | `test_logs_no_log_file_renders_empty_state` | Empty state |
| `tests/unit/test_dashboard_logs_download.py:44` | `test_download_serves_file_contents` | Download endpoint |
| `tests/unit/test_dashboard_logs_download.py:52` | `test_download_filename_is_hardcoded` | Sécurité filename |
| `tests/unit/test_dashboard_logs_download.py:58` | `test_download_404_when_log_file_missing` | Edge case |
| `tests/unit/test_dashboard_logs_download.py:76` | `test_download_403_when_disabled` | Feature flag |
| `tests/unit/test_telegram_template_rendering.py::*` | 15+ tests | Rendu templates — adaptation minimale (contexts ajoutent `mode_badge`) |
| `tests/unit/test_alert_renderer.py::*` | tests existants | Renderer — adaptation constructor (`mode=`) |
| `tests/unit/test_dashboard_security.py`, `test_dashboard_security_m6.py` | tous | Invariants sécurité dashboard — **zéro diff attendu** |
| `tests/integration/test_cli_subprocess_smoke.py::test_no_secret_leak_in_log_file` | — | Aucun secret loggé fichier (déjà robuste) |

### 8.2 À réécrire (inversions du comportement dry-run)

Tests dont l'assertion actuelle sera **fausse** après M10. Renommage + inversion.

| Ancien test | Nouveau test | Nouveau contrat |
|---|---|---|
| `test_pnl_writer_m8_mode.py::test_dry_run_severe_drawdown_does_not_trigger_kill_switch` | `test_dry_run_severe_drawdown_triggers_kill_switch_like_live` | `"kill_switch_triggered"` IN events, `stop.is_set()` True, `dry_run_virtual_drawdown` NOT IN events |
| `test_pnl_writer_m8_mode.py::test_dry_run_low_drawdown_no_alert` | `test_dry_run_low_drawdown_no_alert` | **Inchangé** (drawdown sous le seuil ET sous 75% du seuil = aucune alerte quelle que soit le mode) |
| `test_pnl_snapshot_writer.py::test_dry_run_writes_snapshot_and_never_kills` | `test_dry_run_writes_snapshot_and_triggers_kill_switch` | Assertion : kill_switch triggered dans les alerts + stop_event set |
| `test_executor_orchestrator.py::test_orchestrator_raises_without_creds_in_real_mode` | `test_orchestrator_raises_without_creds_in_live_mode` | `execution_mode="live"` + keys None → RuntimeError |
| `test_pipeline_m8_branch.py::test_4th_guardrail_assert_dry_run_true` | `test_4th_guardrail_assert_execution_mode_dry_run` | `execution_mode="live"` passé à `_persist_realistic_simulated` → AssertionError |
| `test_pipeline_m8_branch.py::test_live_mode_never_fetches_book` | **Renommé** `test_live_mode_never_fetches_book` (inchangé sauf `dry_run=False` → `execution_mode="live"`) | Non-régression |
| `test_telegram_template_rendering.py::test_startup_template_minimal` | **Adapté** | `StartupContext.mode` est `"dry_run"` au lieu de `"dry-run"` (cohérence enum). Output contient le badge `🟢 DRY-RUN`. |
| `test_alert_renderer.py::test_*` | **Adaptés** | Constructor `AlertRenderer(mode="dry_run")` ; output contient le badge header |

### 8.3 À ajouter (nouveaux)

Tests nouveaux exhaustivement listés.

| Test | Fichier | Contrat |
|---|---|---|
| `test_execution_mode_enum_values` | `tests/unit/test_config.py` | `Settings(execution_mode="simulation"/"dry_run"/"live")` OK ; `"other"` raise `ValidationError` |
| `test_legacy_dry_run_true_maps_to_dry_run_mode_with_warning` | `tests/unit/test_config.py` | env `DRY_RUN=true` + `EXECUTION_MODE` absent → `settings.execution_mode == "dry_run"` + flag legacy detected |
| `test_legacy_dry_run_false_maps_to_live_mode` | `tests/unit/test_config.py` | `DRY_RUN=false` → `execution_mode="live"` |
| `test_explicit_execution_mode_wins_over_legacy` | `tests/unit/test_config.py` | `DRY_RUN=true` + `EXECUTION_MODE=live` → `execution_mode="live"` + **pas** de warning |
| `test_dry_run_property_backward_compat` | `tests/unit/test_config.py` | `settings.dry_run is True` pour simulation/dry_run, False pour live |
| `test_kill_switch_fires_in_dry_run_mode` | `tests/unit/test_pnl_writer_m10_parity.py` | drawdown >= seuil en dry_run → kill_switch_triggered CRITICAL + stop_event.set() |
| `test_kill_switch_fires_in_simulation_mode` | `tests/unit/test_pnl_writer_m10_parity.py` | idem en simulation |
| `test_drawdown_warning_fires_in_dry_run_mode` | `tests/unit/test_pnl_writer_m10_parity.py` | drawdown 75% en dry_run → pnl_snapshot_drawdown WARNING |
| `test_no_dry_run_virtual_drawdown_event_emitted` | `tests/unit/test_pnl_writer_m10_parity.py` | Plus jamais d'event `dry_run_virtual_drawdown` émis |
| `test_telegram_alert_shows_mode_badge_simulation` | `tests/unit/test_telegram_badge.py` | `AlertRenderer(mode="simulation").render_alert(...)` contient `🟢 SIMULATION` |
| `test_telegram_alert_shows_mode_badge_dry_run` | `tests/unit/test_telegram_badge.py` | contient `🟢 DRY-RUN` |
| `test_telegram_alert_shows_mode_badge_live` | `tests/unit/test_telegram_badge.py` | contient `🔴 LIVE` |
| `test_telegram_badge_escaped_in_markdown_v2` | `tests/unit/test_telegram_badge.py` | `-` dans badge est escapé `\-` |
| `test_logs_default_hides_dashboard_request` | `tests/unit/test_dashboard_logs_route.py` | route `/logs` sans filtre ne montre **aucune** entrée `dashboard_request` |
| `test_middleware_drops_noisy_endpoint_success` | `tests/unit/test_middleware_log_filter.py` | GET 200 sur `/partials/kpis` → aucune ligne dans le fichier log |
| `test_middleware_keeps_noisy_endpoint_error` | `tests/unit/test_middleware_log_filter.py` | GET 500 sur `/partials/kpis` → ligne présente (error always pass) |
| `test_middleware_keeps_non_noisy_endpoint_success` | `tests/unit/test_middleware_log_filter.py` | GET 200 sur `/home` → ligne présente (pas dans whitelist) |
| `test_logs_opt_in_shows_dashboard_request` | `tests/unit/test_dashboard_logs_route.py` | route `/logs?events=dashboard_request` restore l'affichage |
| `test_skip_paths_env_override_adds_extra` | `tests/unit/test_middleware_log_filter.py` | `DASHBOARD_LOG_SKIP_PATHS="^/custom$"` + GET 200 sur `/custom` → droppé |
| `test_simulation_mode_dispatches_to_stub` | `tests/unit/test_pipeline_simulation.py` | `execution_mode="simulation"` + order approved → pas d'appel `ClobOrderbookReader`, pas d'appel `ClobWriteClient`, order persisté `simulated=True` |
| `test_simulation_mode_never_instantiates_clob_write_client` | `tests/unit/test_executor_orchestrator.py` | Garde-fou 1 en mode simulation |
| `test_4th_guardrail_rejects_simulation_mode` | `tests/unit/test_pipeline_m8_branch.py` | `_persist_realistic_simulated` raise si `execution_mode="simulation"` |
| `test_cli_deprecation_warning_on_legacy_dry_run_env` | `tests/integration/test_cli_subprocess_smoke.py` | subprocess avec env `DRY_RUN=true` → log `config_deprecation_dry_run_env` présent dans fichier |
| `test_cli_deprecation_warning_on_dry_run_flag` | `tests/integration/test_cli_subprocess_smoke.py` | subprocess avec `--dry-run` → log `cli_deprecation_dry_run_flag` |
| `test_structlog_processor_filter_order` | `tests/unit/test_logging_config.py` | `make_filter_noisy_endpoints` est le 1er processor retourné dans le config |
| `test_4th_guardrail_assert_dry_run_true` (renommé) | `test_pipeline_m8_branch.py` | **préservé** sous nouveau nom `test_4th_guardrail_assert_execution_mode_dry_run` (§8.2) |
| `test_legacy_dry_run_flag_maps_to_dry_run_mode` | `tests/unit/test_cli_runner.py` | `args.dry_run=True` → `settings.execution_mode="dry_run"` |

---

## 9. Impact CLAUDE.md — texte de remplacement exact

Les 4 passages à remplacer mot pour mot. Cohérent avec synthèse §8.

### 9.1 Section "Monitoring M4"

**Actuel** (ligne ~97 du CLAUDE.md) :

```
- **Monitoring M4** : kill switch déclenché EXCLUSIVEMENT par `PnlSnapshotWriter`, **jamais en dry-run** (sécurité critique). `RiskManager` (M2) reste inchangé — pas de refactor.
```

**Remplacer par** :

```
- **Monitoring M4** : kill switch déclenché EXCLUSIVEMENT par `PnlSnapshotWriter` sur `KILL_SWITCH_DRAWDOWN_PCT`. **Identique dans les 3 modes** SIMULATION/DRY_RUN/LIVE depuis M10 — le dry-run utilise capital virtuel + positions simulées pour le calcul du drawdown. Les alertes Telegram en dry-run portent un badge visuel `🟢 DRY-RUN` pour différencier de `🔴 LIVE`, mais la sévérité (CRITICAL) est identique. En SIMULATION (backtest offline), le `stop_event` est local au run, pas global. `RiskManager` (M2) reste inchangé — pas de refactor.
```

### 9.2 Section "Dry-run M8"

**Actuel** (ligne ~103) :

```
- **Dry-run M8** : `DRY_RUN_REALISTIC_FILL=true` (opt-in strict, default `false`) active la simulation orderbook FOK via `GET /book` read-only public. **Triple garde-fou M3 préservé intact** + 4ᵉ garde-fou M8 : `assert dry_run is True` avant chaque `_persist_realistic_simulated`. Diff strictement additif sur M3 (zéro ligne modifiée dans `ClobWriteClient`, `_persist_sent_order`, `_assert_capital_available`). Aucune creds consommée par le path M8 (uniquement `/book`, `/midpoint`, Gamma `/markets`). Ségrégation data : `MyOrder.realistic_fill=True` + `MyPosition.simulated=True` + contrainte unique triple `(condition_id, asset_id, simulated)`. **Kill switch JAMAIS en dry-run** (invariant M4 préservé). Alerte `dry_run_virtual_drawdown` INFO only à 50 % du seuil — pas WARNING/CRITICAL, pas de `stop_event.set()`. v1 : SELL sur position virtuelle inexistante → `dry_run_sell_without_position` warning + skip. Marchés `neg_risk` → résolution skipped (`dry_run_resolution_neg_risk_unsupported`), position reste open virtuellement. `DryRunResolutionWatcher` lancé conditionnellement par `ExecutorOrchestrator` (TaskGroup, pas un nouveau top-level module). `VirtualWalletStateReader` alimente `PnlSnapshotWriter` M4 sans refactor. Cache book in-memory TTL 5 s + LRU 500 entries. `Decimal` pour les calculs orderbook, `float` pour la persistance (jamais `Decimal(float)`). Migration `0004` audit manuel (batch_alter_table SQLite-friendly). Aucun secret loggé — vérifié par `test_m8_security_grep.py`.
```

**Remplacer par** :

```
- **Dry-run M8** : `DRY_RUN_REALISTIC_FILL=true` (opt-in strict, default `false`) active la simulation orderbook FOK via `GET /book` read-only public — utilisable uniquement si `EXECUTION_MODE=dry_run` (ignoré en SIMULATION et LIVE). **Triple garde-fou M3 préservé intact** + 4ᵉ garde-fou M8 réaffirmé M10 : `assert settings.execution_mode == "dry_run"` avant chaque `_persist_realistic_simulated`. Diff strictement additif sur M3 (zéro ligne modifiée dans `ClobWriteClient`, `_persist_sent_order`, `_assert_capital_available`). Aucune creds consommée par le path M8 (uniquement `/book`, `/midpoint`, Gamma `/markets`). Ségrégation data : `MyOrder.realistic_fill=True` + `MyPosition.simulated=True` + contrainte unique triple `(condition_id, asset_id, simulated)`. **Kill switch actif identique live depuis M10** (alerte CRITICAL `kill_switch_triggered` avec badge `🟢 DRY-RUN`, `stop_event.set()` déclenché). L'ancienne alerte `dry_run_virtual_drawdown` INFO est **supprimée** M10 — remplacée par le vrai kill_switch_triggered. v1 : SELL sur position virtuelle inexistante → `dry_run_sell_without_position` warning + skip. Marchés `neg_risk` → résolution skipped (`dry_run_resolution_neg_risk_unsupported`), position reste open virtuellement. `DryRunResolutionWatcher` lancé conditionnellement par `ExecutorOrchestrator` (TaskGroup, pas un nouveau top-level module). `VirtualWalletStateReader` alimente `PnlSnapshotWriter` M4 sans refactor. Cache book in-memory TTL 5 s + LRU 500 entries. `Decimal` pour les calculs orderbook, `float` pour la persistance (jamais `Decimal(float)`). Migration `0004` audit manuel (batch_alter_table SQLite-friendly). Aucun secret loggé — vérifié par `test_m8_security_grep.py`.
```

### 9.3 Section "Conventions de code"

**Ajouter** après le bullet "Logs structurés via `structlog`..." :

```
- **Modes d'exécution (M10+)** : `EXECUTION_MODE: "simulation" | "dry_run" | "live"` remplace `DRY_RUN: bool`. Ancien flag lu en fallback avec warning de deprecation 1 version. 3 modes testés séparément ; le dry-run est un **miroir fidèle** du live côté alertes/kill switch/logs — seule la signature CLOB (POST ordre réel) diffère. SIMULATION = backtest offline, pas de réseau, fixtures locales, `stop_event` local au run.
```

### 9.4 Section "Logs file M9" (optionnel, ajout précision M10)

Le bloc "Logs file M9" reste valide. **Ajouter** à la fin de ce bullet :

```
**M10 hygiene** : processor structlog `filter_noisy_endpoints` (inséré en tête de chaîne) drop les `dashboard_request` 2xx/3xx des paths polling whitelist (`^/api/health-external$`, `^/partials/.*$`, `^/api/version$`) avant formatage JSON — économie CPU + fichier log ~30× moins volumineux sur Home actif. Erreurs 4xx/5xx passent toujours. Override via env `DASHBOARD_LOG_SKIP_PATHS` (additif). Lecteur `/logs` exclut `dashboard_request` par défaut ; opt-in via query `events=dashboard_request` ou preset UI "Include HTTP access" (persisté `localStorage` clé `polycopy.logs.preset`, cohérent M6 `polycopy.theme`).
```

### 9.5 Section "Dashboard M4.5 / M6" — ajout M10

**Ajouter** à la fin de ce bullet existant :

```
**M10 preset** : `localStorage` client stocke en plus `polycopy.logs.preset` (valeur `business` | `access`). Strictement UI, aucun contenu DB ni secret.
```

---

## 10. Risques et mitigations

### 10.1 Risque critique — kill switch coupe un backtest dry-run long

**Scénario** : utilisateur avec `EXECUTION_MODE=dry_run`, `KILL_SWITCH_DRAWDOWN_PCT=20` (default), laisse tourner 3 jours pour observer le PnL simulé. Le marché est volatile, le drawdown virtuel atteint 20%, le bot **se coupe** — l'utilisateur ne s'y attendait pas (M4 documentait "jamais en dry-run").

**Impact** : perte d'observation post-coupure, impression de bug, perte de confiance.

**Mitigations** :

1. Warning CLI au **premier boot post-migration** (détecté via env `DRY_RUN=true` legacy ou absence de `EXECUTION_MODE` set dans `.env`) :
   ```
   ⚠️ M10: dry-run mirrors live kill switch. Set KILL_SWITCH_DRAWDOWN_PCT=100
      if you want unlimited simulation duration.
   ```
2. Nouvelle section README "Dry-run miroir live" qui explique explicitement cette décision + le workaround `KILL_SWITCH_DRAWDOWN_PCT=100`.
3. Message Telegram `kill_switch_triggered` au déclenchement inclut dans son body un hint "Si tu es en dry-run et que ce n'est pas voulu, augmente KILL_SWITCH_DRAWDOWN_PCT." (via template).
4. Documenté CLAUDE.md §9.1 (visible à toute future session Claude Code).

### 10.2 Risque moyen — tests CI existants qui assument dry-run silencieux

**Scénario** : la suite CI (`pytest`) contient ~5-8 tests dont l'assertion explicite est "pas de kill_switch, pas de WARNING/CRITICAL alerte en dry-run". Ces tests **vont casser**.

**Liste exhaustive** (cf. §8.2) :

- `test_pnl_writer_m8_mode.py::test_dry_run_severe_drawdown_does_not_trigger_kill_switch`
- `test_pnl_snapshot_writer.py::test_dry_run_writes_snapshot_and_never_kills`

**Mitigation** : inversion systématique listée en §8.2, renommage pour refléter le nouveau contrat.

### 10.3 Risque moyen — templates Telegram custom user cassent

**Scénario** : un user a surchargé un template via `assets/telegram/kill_switch_triggered.md.j2`. Ce template n'utilise pas `mode_badge` : **pas** de crash (StrictUndefined crash uniquement sur lookup absent, pas sur binding fourni mais non utilisé). Pas de crash donc — mais le badge mode est **absent** de son message custom.

**Impact** : UX dégradée (pas de badge visuel) mais non fatale.

**Mitigation** :

- Documenter dans `docs/setup.md` §Telegram overrides : "Depuis M10, les templates polycopy injectent `mode_badge` (valeur : `🟢 SIMULATION`/`🟢 DRY-RUN`/`🔴 LIVE`). Pense à l'ajouter à tes templates custom si tu veux distinguer les modes visuellement."
- Renderer test `test_user_template_without_mode_badge_renders_without_crash` : confirme le comportement non-fatal.

### 10.4 Risque faible — utilisateur legacy `DRY_RUN=true` en prod

**Scénario** : un user a `DRY_RUN=true` en prod depuis M7. Il pull `main` post-M10 sans lire le CHANGELOG. Au boot, le validator M10 détecte `DRY_RUN=true`, set `execution_mode=dry_run`, émet un warning.

**Impact** : fonctionne, mais le user ne voit pas le warning s'il ne tail pas le fichier log.

**Mitigation** :

- Warning émis à **log.warning** niveau (pas DEBUG) — donc visible dans l'onglet `/logs` dashboard et le fichier.
- Rendu **aussi** sur stdout en mode `--verbose`.
- Note dans le startup Telegram (si token configuré) : section "Avertissements récents" — reportable à M10.1.
- Documenté README + CHANGELOG.

### 10.5 Risque faible — incompatibilité DB `is_dry_run` sémantique

**Scénario** : les colonnes DB `MyOrder.is_dry_run`, `MyOrder.simulated`, `MyPosition.simulated`, `PnlSnapshot.is_dry_run` étaient **toujours** `True` en dry_run. Avec M10, **un ordre SIMULATION** pose aussi `is_dry_run=True` (car `execution_mode != "live"`). Donc le dashboard `/pnl?mode=dry_run` **mélange** DRY_RUN et SIMULATION.

**Impact** : confusion possible entre backtest et dry-run online.

**Mitigation** :

- v1 M10 : pas de migration DB (scope strict). Les colonnes restent binaires (`is_dry_run` = True si simulation OU dry_run).
- Documenter dans le CHANGELOG : "la colonne `is_dry_run` agrège SIMULATION + DRY_RUN jusqu'à une future M10.1 qui ajoutera une colonne `execution_mode: str`."
- Le dashboard v1 M10 cache `?mode=simulation` (pas exposé dans l'UI toggle). Cf. §3.6.2.
- Reportable : M10.1 migration `0005_execution_mode_column` qui ajoute une colonne `execution_mode` sur `MyOrder` / `MyPosition` / `PnlSnapshot`. Backfill `"live"` si `is_dry_run=False`, `"dry_run"` si `True` (meilleur-effort — perd la distinction historique SIM/DRY_RUN mais aucun user n'a run en SIMULATION à M10).

### 10.6 Risque très faible — `_legacy_dry_run_detected` variable module-level

**Scénario** : 2 instances de `Settings()` coexistent (rare mais possible en test). La variable module-level `_LEGACY_DETECTED` reflète la dernière construction.

**Mitigation** :

- Dans les tests, isoler via `monkeypatch` et reset explicite du flag.
- Alternative plus propre mais plus complexe : stocker le flag dans `Settings.model_extra` (Pydantic v2 extra fields) et warning au boot consulte `settings.model_extra.get("_legacy_dry_run_detected")`. **Décision** : variable module-level pour v1, reportable à v1.1 si test flakiness.

---

## 11. Rollout / migration

### 11.1 Séquence

Cohérent synthèse §7.

1. **T0** — Merge spec M10 sur `main` (sans code).
2. **T0 + 3j** — PR code M10 mergée derrière flag interne `EXECUTION_MODE_V2_ENABLED` (feature flag env non-documenté). Default `true` dès la PR mergée (pas vraiment un feature flag, plutôt un kill switch de rollback si régression observée — activable via `EXECUTION_MODE_V2_ENABLED=false` qui restaure le court-circuit M4).
3. **T0 + 3j** — Deprecation warning au CLI boot si `DRY_RUN` env var legacy détectée.
4. **T0 + 3j** — README + CLAUDE.md + docs/architecture.md + docs/setup.md dans le **même** commit.
5. **Next release (T0 + ~2 semaines)** — Si aucune régression signalée, retirer le kill switch `EXECUTION_MODE_V2_ENABLED`. Le comportement M10 est inconditionnel.
6. **Version+2 (T0 + ~6 semaines)** — Supprimer complètement la property `Settings.dry_run`, supprimer le validator `_migrate_legacy_dry_run`, supprimer la lecture legacy `DRY_RUN` env var. Un user qui n'a pas migré voit un warning Pydantic "extra field ignored" au boot.

### 11.2 Rollback

Si régression critique post-merge :

- Option A (runtime) : `EXECUTION_MODE_V2_ENABLED=false` env → restaure le comportement M9 (nécessite que le code conserve le chemin M9 derrière le flag — coût de complexité qui double certaines branches, mais transitoire).
- Option B (git) : revert du commit M10 complet.

**Décision** : privilégier option A pour T0+3j à T0+2s, passer à option B au-delà.

### 11.3 Communication

- CHANGELOG entry détaillé.
- Issue GitHub "M10 migration guide" épinglée.
- Section README "Breaking changes M9 → M10" explicite :
  - `DRY_RUN` → `EXECUTION_MODE` (lu 1 version avec warning).
  - Kill switch actif en dry-run (nouveau).
  - Alerte `dry_run_virtual_drawdown` supprimée (remplacée par `kill_switch_triggered`).
  - Badges visuels dans les messages Telegram.
  - Logs dashboard : onglet `/logs` montre "business events only" par défaut.

---

## 12. Open questions

Questions dont la réponse n'est pas critique pour démarrer l'implémentation mais à trancher avant cutover final.

1. **Canal Telegram séparé pour DRY_RUN ?** Synthèse §3.4 tranche "même canal, badge distingue". Alternative : env var `TELEGRAM_CHAT_ID_DRY_RUN` séparée. **Décision actuelle** : même canal — user peut filtrer via client Telegram sur le badge emoji si besoin. Reportable si feedback user.

2. **Comportement SIMULATION sans fixtures ?** Si user pose `EXECUTION_MODE=simulation` sans avoir de runner backtest + fixtures, le bot crash au boot du Watcher. **Décision actuelle** : laisser crasher, message d'erreur clair `"SIMULATION mode requires a backtest harness (not shipped in v1 M10). Use EXECUTION_MODE=dry_run for online pipeline."`. Reportable M10.1 ou M12.

3. **Badge header sur fallback.md.j2 ?** Le template `fallback.md.j2` rend les events inconnus. Le badge doit-il y apparaître ? **Décision actuelle** : oui, cohérence. Reportable si ça casse les tests d'alert renderer existants.

4. **`_LEGACY_DETECTED` module-level vs instance field** : cf. §10.6. À valider en code review.

5. **Préfixe `DRY_RUN_` des env vars M8** : `DRY_RUN_REALISTIC_FILL`, `DRY_RUN_VIRTUAL_CAPITAL_USD`, etc. Incohérent avec la nouvelle enum (seraient `EXECUTION_MODE_DRY_RUN_REALISTIC_FILL` ?). **Décision actuelle** : garder le préfixe `DRY_RUN_` (ces flags ne s'activent qu'en `execution_mode=dry_run`, le nom reste significatif). Ne pas renommer — éviter la churn.

6. **`_DEFAULT_NOISY_PATH_PATTERNS` custom par déploiement ?** Certains utilisateurs avec dashboards customs (ajout M10.1+) pourraient vouloir d'autres patterns. **Décision actuelle** : env var `DASHBOARD_LOG_SKIP_PATHS` additive suffit. Pas de mécanisme de "retirer" un pattern default. Reportable.

7. **Preset UI `polycopy.logs.preset` — valeurs autorisées** : `"business"` vs `"access"` vs futur `"errors_only"` ? **Décision actuelle** : 2 presets suffisent v1. Reportable si feedback.

8. **Position virtuelle M8 + SIMULATION** : un backtest SIMULATION a besoin de tracker des positions virtuelles — est-ce que la branche `_persist_realistic_simulated` devrait être autorisée en SIMULATION ? **Décision actuelle** : non v1 (cf. garde-fou 4 §3.5.4 exige strict dry_run). Reportable M12 backtest.

9. **Ordre des processors structlog — regression risk** : la synthèse §2.1 dit "avant TimeStamper". Un test de l'ordre (`test_structlog_processor_filter_order`) est prévu §8.3. Suffisant ?

10. **Behavior en mode SIMULATION avec token Telegram configuré** : faut-il que les alertes SIMULATION soient effectivement envoyées à Telegram, ou silenciées ? **Décision actuelle** : envoyées (comme dry_run), cohérent avec la philosophie "miroir fidèle". Reportable si noise.

---

## 13. Commandes de vérification finale

```bash
ruff check .
ruff format --check .
mypy src
pytest --cov=src/polycopy/config --cov=src/polycopy/monitoring --cov=src/polycopy/dashboard --cov=src/polycopy/cli --cov-report=term-missing   # ≥ 80%
pytest -m integration    # opt-in, inclut tests CLI subprocess

# Vérifier qu'aucune régression sur les garde-fous M3 et M8
pytest tests/unit/test_executor_orchestrator.py tests/unit/test_pipeline_m8_branch.py -v

# Smoke test M10 (dry_run mirrors live)
EXECUTION_MODE=dry_run DRY_RUN_REALISTIC_FILL=true \
KILL_SWITCH_DRAWDOWN_PCT=5 \
python -m polycopy &
sleep 15
# Forcer un drawdown virtuel (via seed ou script)
# Vérifier logs : kill_switch_triggered CRITICAL (pas dry_run_virtual_drawdown)
kill %1 && wait

# Smoke test hygiene logs
EXECUTION_MODE=dry_run DASHBOARD_ENABLED=true \
python -m polycopy &
sleep 5
for i in 1 2 3; do curl -s http://127.0.0.1:8787/api/health-external > /dev/null; done
grep -c '"dashboard_request"' ~/.polycopy/logs/polycopy.log   # Doit être 0 ou très bas
kill %1 && wait

# Vérifier legacy DRY_RUN warning
DRY_RUN=true python -m polycopy --verbose 2>&1 | grep -c "config_deprecation_dry_run_env"   # >= 1
```

---

## 14. Critères d'acceptation

- [ ] `EXECUTION_MODE=simulation|dry_run|live` accepté, `"other"` rejeté au boot.
- [ ] Legacy `DRY_RUN=true` + `EXECUTION_MODE` absent → `execution_mode="dry_run"` + log warning `config_deprecation_dry_run_env` émis.
- [ ] Legacy `DRY_RUN=false` → `execution_mode="live"` + warning idem.
- [ ] Explicit `EXECUTION_MODE=live` + `DRY_RUN=true` → `execution_mode="live"` (l'explicite gagne) + **pas** de warning.
- [ ] Property `settings.dry_run` retourne True pour simulation/dry_run, False pour live (backward-compat).
- [ ] `--execution-mode` CLI flag fonctionne, `--dry-run` legacy fonctionne avec warning `cli_deprecation_dry_run_flag`.
- [ ] Kill switch déclenche `kill_switch_triggered` CRITICAL + `stop_event.set()` en mode dry_run (**inversion invariant M4**).
- [ ] Alerte `dry_run_virtual_drawdown` INFO **plus jamais émise** (event supprimé du code).
- [ ] `_DRY_RUN_VIRTUAL_DRAWDOWN_RATIO` constante supprimée de `pnl_writer.py`.
- [ ] `_maybe_push_dry_run_drawdown` méthode supprimée de `PnlSnapshotWriter`.
- [ ] Garde-fou 1 (lazy init ClobWriteClient) : `ClobWriteClient` instancié uniquement si `execution_mode == "live"`.
- [ ] Garde-fou 2 (RuntimeError boot) : raise si `execution_mode == "live"` ET clés absentes, avant TaskGroup.
- [ ] Garde-fou 3 (double-check pipeline) : `RuntimeError` si `execution_mode != "live"` juste avant POST.
- [ ] Garde-fou 4 (M8 assertion) : `AssertionError` si `execution_mode != "dry_run"` avant `_persist_realistic_simulated`. Rejette SIMULATION et LIVE.
- [ ] 15 templates `*.md.j2` contiennent le header `_\[{{ mode_badge | telegram_md_escape }}\]_`.
- [ ] Rendu `AlertRenderer(mode="simulation").render_alert(...)` contient `🟢 SIMULATION` (escape MarkdownV2 correcte).
- [ ] Rendu dry_run contient `🟢 DRY\-RUN`.
- [ ] Rendu live contient `🔴 LIVE`.
- [ ] User template dans `assets/telegram/*.md.j2` sans `mode_badge` rend **sans crash** (StrictUndefined ne déclenche pas sur binding fourni-mais-non-utilisé).
- [ ] Processor `filter_noisy_endpoints` est le **1er** dans la config structlog (test unit vérifie l'ordre).
- [ ] GET 200 sur `/partials/kpis` → aucune ligne `dashboard_request` dans `~/.polycopy/logs/polycopy.log`.
- [ ] GET 500 sur `/partials/kpis` → ligne `dashboard_request` présente (errors always pass).
- [ ] GET 200 sur `/home` → ligne présente (pas dans whitelist default).
- [ ] `DASHBOARD_LOG_SKIP_PATHS="^/custom$"` + GET 200 sur `/custom` → droppé.
- [ ] Route `/logs` sans filtre → aucune entrée `dashboard_request` rendue.
- [ ] Route `/logs?events=dashboard_request` → restore l'affichage.
- [ ] Bouton preset "Business events only" actif par défaut, `localStorage.polycopy.logs.preset = "business"` au premier load.
- [ ] `ruff check .` + `ruff format --check .` + `mypy src --strict` : 0 erreur.
- [ ] `pytest` : 0 échec. Coverage ≥ 80 % sur `src/polycopy/monitoring/`, `src/polycopy/config.py`, `src/polycopy/cli/logging_config.py`, `src/polycopy/dashboard/log_reader.py`, `src/polycopy/dashboard/routes.py`. Non-régression M1..M9 ≥ 80 %.
- [ ] Doc updates §9 (`CLAUDE.md`) + README section dry-run miroir + `docs/architecture.md` + `docs/setup.md` §18 dans le **même** commit.
- [ ] Commit final unique : `feat(config,monitoring,executor,dashboard): M10 3-mode parity + log hygiene`.

---

## 15. Prompt à m'envoyer pour lancer l'implémentation

Copie-colle tel quel quand tu es prêt :

```
/implement-module M10

Suis specs/M10-parity-and-log-hygiene.md à la lettre. Pas d'invocation skill Polymarket requise — M10 est 100 % couche config + observability + dashboard.

Avant tout code, actions obligatoires :

1. Vérifier qu'aucune modification concurrente n'a rename dry_run en autre chose entre temps :
   grep -n "settings.dry_run\|settings\[.dry_run" src/polycopy/ | wc -l   # baseline count pour audit post-migration

2. Capturer l'état des tests existants :
   pytest tests/unit/test_pnl_snapshot_writer.py tests/unit/test_pnl_writer_m8_mode.py tests/unit/test_executor_orchestrator.py tests/unit/test_pipeline_m8_branch.py tests/unit/test_dashboard_logs_route.py -v | tee /tmp/m10_baseline.txt

Ensuite suis l'ordre §7 (12 étapes séquentielles).

Contraintes non négociables :

- EXECUTION_MODE default = "dry_run" (backward-compat : user M9 qui ne touche pas son .env voit son bot en dry_run — comme avant).
- Legacy DRY_RUN=true/false lu 1 version avec warning structlog `config_deprecation_dry_run_env`. Supprimé à version+2.
- Kill switch actif identique LIVE dans les 3 modes. INVERSION d'un invariant M4/M8 — documenter dans CHANGELOG + README.
- 4 garde-fous M3/M8 préservés textuellement avec rename dry_run → execution_mode (§3.5). Test de chaque garde-fou OBLIGATOIRE.
- Alerte dry_run_virtual_drawdown SUPPRIMÉE. Ne plus émettre cet event nulle part.
- _DRY_RUN_VIRTUAL_DRAWDOWN_RATIO constante supprimée.
- _maybe_push_dry_run_drawdown méthode supprimée.
- 15 templates Telegram gagnent 1 ligne header `_\[{{ mode_badge | telegram_md_escape }}\]_`. Pas d'autre modif template.
- AlertRenderer constructor gagne `mode: str = "dry_run"` paramètre. Tous les usages mis à jour (MonitoringOrchestrator).
- Zéro migration DB. is_dry_run et simulated colonnes inchangées.
- Zéro refactor executor path live (pipeline M3 paths) au-delà du rename.
- Processor filter_noisy_endpoints EN PREMIER dans la chaîne structlog. Test d'ordre obligatoire.
- Logs dashboard/logs exclut dashboard_request par défaut. Opt-in via events=dashboard_request query.
- Preset UI localStorage cohérent M6 (clé polycopy.logs.preset).
- Conventions CLAUDE.md (async, Pydantic v2, SQLAlchemy 2.0, structlog, docstrings FR / code EN, pas de print).
- mypy --strict propre, ruff propre, pytest ≥ 80% coverage sur les modules modifiés. Non-régression M1..M9 ≥ 80%.

Demande-moi confirmation avant tout patch sensible :
- config.py : suppression du `dry_run: bool` (remplacement par property deprecation).
- pnl_writer.py : suppression de _maybe_push_dry_run_drawdown (irreversible sémantiquement).
- Modification des 15 templates Telegram (injection badge).
- Inversion des tests existants §8.2 (renommage + inversion assertions).
- README.md section dry-run (rewrite du paragraphe "dry-run = sandbox silencieux").
- CLAUDE.md texte §9 (remplacement des 2 blocs Monitoring M4 + Dry-run M8).

Si une zone §12 open question devient bloquante pendant l'implémentation (ex: test SIMULATION crash fatal, badge escape markdownv2 casse le render, user template crash), STOP et signale — ne tranche pas au pif.

Smoke test final obligatoire avant merge :
- EXECUTION_MODE=dry_run + KILL_SWITCH_DRAWDOWN_PCT=5 + trade qui fait drawdown → kill_switch_triggered CRITICAL Telegram avec badge 🟢 DRY-RUN + stop_event.
- EXECUTION_MODE=live sans clés → RuntimeError boot.
- EXECUTION_MODE=simulation → pipeline démarre, Watcher crash au 1er poll (pas de stub fixture v1 — attendu).
- DRY_RUN=true legacy → log config_deprecation_dry_run_env + execution_mode="dry_run".
- Dashboard actif + 10 polls `/partials/kpis` → 0 ligne dashboard_request dans ~/.polycopy/logs/polycopy.log.
- Dashboard /logs sans filtre → aucune entrée dashboard_request visible.
- Dashboard /logs?events=dashboard_request → entrées visibles.
- Bouton preset UI : click "Include HTTP access" → liste inclut dashboard_request + localStorage polycopy.logs.preset="access".

Commit unique : feat(config,monitoring,executor,dashboard): M10 3-mode parity + log hygiene
```
