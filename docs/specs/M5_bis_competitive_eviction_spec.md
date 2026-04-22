# M5_bis — Competitive Eviction

**Status** : Draft — 2026-04-21
**Depends on** : M5 (Discovery + Scoring v1), M7 (Telegram alerts), M4.5/M6 (dashboard), M12 (scoring v2 optionnel — cohabitation)
**Ne bloque pas** : M13 (fees), M14 (latency phase 2), M15 (Goldsky streaming) — M5_bis vit dans `discovery/`, surface isolée.

---

## 0. TL;DR

M5 fait pousser le pool de wallets *à sens unique* : dès que `MAX_ACTIVE_TRADERS` est atteint, un shadow même excellent reste bloqué, et un active médiocre occupe son slot tant qu'il ne descend pas sous `SCORING_DEMOTION_THRESHOLD` × `SCORING_DEMOTION_HYSTERESIS_CYCLES`. La conséquence : le pool converge vers *les premiers wallets à avoir passé le seuil*, pas *les meilleurs wallets historiques*.

M5_bis ajoute une **compétition adaptative** entre wallets : un `shadow` significativement meilleur qu'un `active` peut **l'évincer**. L'active évincé passe en `sell_only` (wind-down réversible — SELL/exits copiés, nouveaux BUY bloqués, score continue à tourner) le temps de liquider ses positions existantes. Symétrique : si un `sell_only` regagne du score, il peut revenir `active` en cascadant le nouveau worst. Un nouveau status terminal `blacklisted` remplace la résolution manuelle opaque via `BLACKLISTED_WALLETS` par un état visible dans le dashboard.

Opt-in strict : `EVICTION_ENABLED=false` par défaut ⇒ lifecycle M5 préservé à l'identique (zéro transition `sell_only`, zéro cascade, zéro alerte supplémentaire).

---

## 1. Motivation & use case concret

### 1.1 Le problème

Situation observée en production M5/M12 (scénario de référence utilisateur) :

> *« J'ai 7 `active` dont un à **0.66**, et un shadow à **0.91** qui passe son temps à attendre. Mon cap est 7. Je veux que le bot converge sur les meilleurs wallets, pas sur les premiers servis. »*

Avec les règles M5 actuelles (cf. [decision_engine.py:242-299](../../src/polycopy/discovery/decision_engine.py#L242)) :

- Le wallet 0.66 tourne en `active` tant que son score reste ≥ `SCORING_DEMOTION_THRESHOLD` (0.40 par défaut). Il n'est **jamais** demote.
- Le wallet 0.91 reste en `shadow` tant qu'il ne passe pas `SCORING_PROMOTION_THRESHOLD` (0.65) **ET** qu'un slot `active` se libère — ce qui n'arrive pas tant que les 7 sont au-dessus de 0.40.
- Les 7 cycles `shadow` bloqués → `trader_events` pleins de `skipped_cap` sans jamais convertir.

Le pool se fige. Le skill-delta moyen du pool n'augmente plus.

### 1.2 La solution

Introduire un **seuil de compétition** `EVICTION_SCORE_MARGIN` (défaut 0.15) : si un `shadow` (ou un `sell_only` en rebond) score **≥ 0.15 points au-dessus du pire `active`** pendant `EVICTION_HYSTERESIS_CYCLES` (défaut 3) cycles consécutifs, la cascade suivante est déclenchée :

```
worst_active  ──►  sell_only   (wind-down : SELL copiés, BUY bloqués)
candidate_top ──►  active      (prend le slot libéré)
```

L'ancien active reste connu du bot : il continue d'être scoré, ses positions existantes se liquident normalement, et il peut **revenir active** si son score rebondit au-dessus du nouveau worst courant. La safety nette : on ne ferme **jamais** une position de force — on laisse le wallet source la fermer via SELL normal.

### 1.3 Pourquoi pas juste « augmenter MAX_ACTIVE_TRADERS » ?

- `MAX_ACTIVE_TRADERS` est un cap capital (chaque active consomme de la liquidité potentielle via copy-ratio).
- Un cap élastique casserait la symétrie M3 du `RiskManager` qui raisonne sur le capital total.
- On veut **garder la meilleure fraction** d'un nombre fixé de wallets, pas diluer le signal en élargissant le pool.

### 1.4 Pourquoi un `sell_only` réversible plutôt qu'un `paused` direct ?

- `paused` (M5) désactive tout : positions ouvertes restent orphelines côté copy trading — on les garde mais on ne copie plus les SELL du wallet source qui pourraient les fermer.
- `sell_only` **continue à copier les SELL/exits** du wallet source → les positions se ferment *naturellement* via le comportement du wallet copié. Fonctionnellement : liquidation propre sans force-close.
- Un active évincé temporairement peut rebondir (score redevient bon) sans avoir à re-traverser 7 jours de `TRADER_SHADOW_DAYS` — il était déjà validé, on préserve cet historique.

---

## 2. Scope / non-goals

### 2.1 Dans le scope

- Nouveau status `sell_only` dans l'enum `TraderStatus`.
- Nouveau status `blacklisted` (terminal, piloté par `BLACKLISTED_WALLETS` env).
- Fusion de `paused` → `shadow` avec flag UX `previously_demoted_at`.
- Transitions compétitives `shadow→active*`, `active→sell_only`, `sell_only→active*`, `sell_only→shadow`, `sell_only→active` (abort).
- Cascade strictement séquentielle : **1 swap max par cycle** Discovery (le candidat avec la plus grande delta gagne).
- Audit trail étendu : 4 nouveaux `event_type`, `event_metadata` enrichi.
- 4 nouveaux templates Telegram.
- Dashboard `/traders` : badges status 4 couleurs, filtre `sell_only`, colonnes delta + cycles observés si eviction in-progress.
- Reconciliation dynamique `BLACKLISTED_WALLETS` : si l'user ajoute/retire un wallet à chaud (re-run sans restart), le prochain cycle Discovery bascule le status.
- Blocage strategy pipeline : un trade `side="BUY"` d'un wallet `sell_only` est rejeté par un nouveau filtre `TraderLifecycleFilter`, les `SELL` passent.

### 2.2 Hors scope (explicites)

- ❌ **Force-close de positions** — jamais. Le bot n'émet pas d'ordre SELL qu'il n'a pas vu côté wallet source. Les positions d'un `sell_only` se ferment via les SELL copiés du wallet source ou à la résolution du marché.
- ❌ **Rééquilibrage de tailles** — le copy-ratio reste tel quel, M5_bis ne touche pas le sizing.
- ❌ **Slot reservation parallèle** — on reste séquentiel (1 swap/cycle) pour éviter les pathologies de cascade simultanée. Une optimisation « N swaps/cycle » est en §15 open questions.
- ❌ **Mapping automatique wallet→catégorie pour différencier les compétitions** — tous les wallets compétent dans le même pool, indépendamment de leur spécialisation. La diversification reste portée par le facteur `specialization` de M12 scoring v2 (HHI), pas par le scheduler eviction.
- ❌ **Bouton dashboard « force demote/promote »** — intentionnellement out : risque de désynchroniser le state machine. Déplacé en §15 open questions.
- ❌ **Auto-flip `EVICTION_ENABLED=true`** — décision humaine uniquement, comme le cutover `SCORING_VERSION=v1→v2` (cf. [M12 §12.7](./M12-scoring-v2.md)).

---

## 3. User stories

### 3.1 Story A — Eviction classique

**Contexte** : `EVICTION_ENABLED=true`, 7 actives dont `0xAAA…` score 0.66 (worst_active), shadow `0xBBB…` score 0.91.

**Cycle N** (delta = 0.25 ≥ 0.15) :
```
[cycle 1/3 under eviction watch]
Telegram : — rien — (hystérésis pas encore atteinte)
```

**Cycle N+2** (3ᵉ cycle consécutif) :
```
Telegram (INFO) :
🟣 [trader_eviction_started] INFO
wallet candidate  : 0xBBB…  (score 0.91, shadow)
wallet évincé     : 0xAAA…  (score 0.66, active → sell_only)
delta             : +0.25 ≥ 0.15 sur 3 cycles
Positions ouvertes 0xAAA : 2 (wind-down en cours)
📊 Dashboard
```

**Suite** : 0xAAA continue d'être scoré, ses 2 positions restent ouvertes, le bot continue de copier les SELL de 0xAAA mais pas les BUY. 0xBBB entre en `active` dès le même cycle transactionnel.

### 3.2 Story B — Abort

**Contexte** : Story A au cycle N+2, mais au cycle N+3 le delta(0xBBB, 0xAAA) repasse à 0.08 (le nouveau score de 0xBBB a chuté) — 0xAAA est en `sell_only` avec encore 2 positions ouvertes.

**Cycles N+3, N+4, N+5** (3 cycles consécutifs avec delta < 0.15) :
```
Telegram (INFO) :
🟣 [trader_eviction_aborted] INFO
Wallet 0xAAA revient en active (abort eviction).
Wallet 0xBBB retourne en shadow.
Raison : delta tombé sous 0.15 pendant 3 cycles consécutifs.
📊 Dashboard
```

0xAAA retrouve son slot, ses positions sont toujours là (jamais fermées de force). 0xBBB doit retraverser l'hystérésis classique pour re-évincer quelqu'un.

### 3.3 Story C — Rebond `sell_only → active*`

**Contexte** : 0xAAA en `sell_only` depuis 4 cycles avec 1 position ouverte restante. Son score remonte à 0.82. Le nouveau worst_active est `0xCCC…` score 0.64.

**Cycle N+10** (delta 0.18 ≥ 0.15, 3ᵉ cycle consécutif) :
```
Telegram (INFO) :
🟣 [trader_eviction_completed_to_active_via_rebound] INFO
Rebond : 0xAAA (sell_only, score 0.82) → active
Nouveau évincé : 0xCCC (active, score 0.64 → sell_only)
📊 Dashboard
```

### 3.4 Story D — Retour shadow + blacklist live

**Contexte 1** : 0xAAA en `sell_only`, sa dernière position vient de fermer (position_closed via SELL copié). Au prochain cycle Discovery, après check positions_open == 0 ET pas de rebond en cours :
```
Telegram (INFO) :
🟣 [trader_eviction_completed_to_shadow] INFO
0xAAA (sell_only) → shadow (toutes positions fermées).
Conservation du score, re-observation possible.
📊 Dashboard
```

**Contexte 2** : L'utilisateur édite `.env` et ajoute `0xAAA` à `BLACKLISTED_WALLETS`, puis envoie `POST /v1/restart/<machine>` (M12_bis) OU attend le prochain cycle si `SETTINGS_HOT_RELOAD` (hors scope) — au minimum, un restart propage la nouvelle liste. Au cycle suivant :
```
Telegram (WARNING) :
🔴 [trader_blacklisted] WARNING
0xAAA → blacklisted (ajouté manuellement par user à BLACKLISTED_WALLETS).
Status terminal jusqu'à retrait de l'env var.
📊 Dashboard
```

---

## 4. State machine détaillée

### 4.1 États

| État | `target_traders.active` | `target_traders.status` | Score calculé ? | Trading BUY | Trading SELL | Terminal ? |
|---|---|---|---|---|---|---|
| `shadow` | `False` | `shadow` | ✓ | ✗ | ✗ (watcher ne poll pas) | ✗ |
| `active` | `True` | `active` | ✓ | ✓ | ✓ | ✗ |
| `sell_only` | `True` | `sell_only` | ✓ | ✗ (rejet pipeline) | ✓ | ✗ |
| `pinned` | `True` | `pinned` | ✓ | ✓ | ✓ | ~terminal M5 |
| `blacklisted` | `False` | `blacklisted` | ✗ | ✗ | ✗ | ✓ (jusqu'à retrait env) |

**Différence fondamentale `sell_only` vs `shadow`** : `sell_only` a `active=True` — le watcher continue à poller le wallet pour détecter ses SELL. Un `shadow` est invisible au watcher (filtre [repositories.py:55-58](../../src/polycopy/storage/repositories.py#L55)). Cette différence impose d'étendre `list_active()` pour inclure `sell_only`.

> **Note M5_ter** ✅ — la propagation DB→watcher des transitions M5_bis (`active→sell_only`, `sell_only→shadow`, etc.) est assurée par le cycle de reload `WATCHER_RELOAD_INTERVAL_SECONDS` (default 300s) ajouté dans [`WatcherOrchestrator.run_forever`](../../src/polycopy/watcher/orchestrator.py#L57). Une nouvelle méthode `TargetTraderRepository.list_wallets_to_poll()` remplace `list_active()` dans ce cycle pour greffer un double-check blacklist Python-side. Sans M5_ter, M5_bis ne livre pas sa valeur métier (cascade invisible jusqu'au prochain restart). Cf. spec [docs/specs/M5_ter_watcher_live_reload_spec.md](./M5_ter_watcher_live_reload_spec.md).

### 4.2 Diagramme ASCII

```
                       new wallet discovered
                                │
                                v
                            ┌─────────┐
                            │ shadow  │◄──────────────────────────┐
                            └────┬────┘                           │
                                 │                                │
                score ≥ 0.65     │ delta(self, worst_active)      │ positions_open=0
                + days ≥ SHADOW  │ ≥ EVICTION_SCORE_MARGIN × 3    │ ET pas de rebond
                + slot free      │ (mode compétitif *)            │ en cours
                                 │                                │
                                 v                                │
                            ┌─────────┐                           │
                 ┌─────────►│ active  │────► ∃ candidat C avec    │
                 │          └────┬────┘      delta(C, self) ≥ 0.15│
   delta < 0.15  │               │           × 3 cycles ET self = │
   × N cycles    │               │           worst_active         │
   (abort)       │               │                 │              │
                 │               │                 v              │
                 │          ┌────────────┐                        │
                 └──────────│ sell_only  │────────────────────────┘
   rebond :                 └─────┬──────┘
   delta(self, worst_active)      │
   ≥ 0.15 × 3 cycles              │ (cascade : worst_active → sell_only)
                                  │
                                  v
                              [retour active
                               ou shadow selon
                               positions & delta]

                        any state ◄──► blacklisted
                     (via BLACKLISTED_WALLETS
                      ajout/retrait user)
```

*(*) Cascade : promotion + démotion au même cycle transactionnel. Un nouveau `active*` implique toujours un `worst_active → sell_only`. Si `len(actives) < MAX_ACTIVE_TRADERS`, promotion classique M5 (pas de cascade nécessaire).*

### 4.3 Table exhaustive des transitions

| # | From | To | Condition | Cascade ? | Write DB | Event type |
|---|---|---|---|---|---|---|
| T1 | `absent` | `shadow` | score ≥ promo ET pas blacklist | non | `insert_shadow` | `discovered` |
| T2 | `shadow` | `active` | score ≥ promo ET days ≥ SHADOW_DAYS ET active_count < cap | non | `transition_status → active` | `promoted_active` |
| T3 | `shadow` | `active*` | **EVICTION_ENABLED** ET delta(self, worst_active) ≥ 0.15 × 3 cycles | **oui** (T5 simultané) | `transition_status → active` + `transition_status worst → sell_only` | `promoted_active_via_eviction` + `demoted_to_sell_only` |
| T4 | `active` | `shadow` | score < demo × 3 cycles (hystérésis M5) | non | `transition_status → shadow` + set `previously_demoted_at` | `demoted_to_shadow` |
| T5 | `active` | `sell_only` | **EVICTION_ENABLED** ET ∃ C : delta(C, self) ≥ 0.15 × 3 ET self = worst_active | **oui** (T3 simultané) | `transition_status → sell_only` + set `eviction_state_entered_at` + `eviction_triggering_wallet` | `demoted_to_sell_only` |
| T6 | `sell_only` | `active` | delta(triggering_C, self) < 0.15 × N cycles (**abort symétrique**) | non | `transition_status → active` + clear eviction fields + `transition_status C → shadow` (si C était `active*`) | `eviction_aborted` |
| T7 | `sell_only` | `active*` | **Rebond** : delta(self, worst_active) ≥ 0.15 × 3 cycles | **oui** | `transition_status → active` + clear eviction fields + cascade nouveau worst → sell_only | `promoted_active_via_rebound` + `demoted_to_sell_only` |
| T8 | `sell_only` | `shadow` | positions_open(wallet, simulated=any) == 0 ET pas de rebond en cours | non | `transition_status → shadow` + clear eviction fields | `eviction_completed_to_shadow` |
| T9 | `paused` (legacy) | `shadow` | **Migration 0007** : tous les `paused` restants → `shadow` + `previously_demoted_at = now()` | non (one-shot) | migration | data migration, pas d'event émis |
| T10 | `any` | `blacklisted` | wallet ∈ `BLACKLISTED_WALLETS` | non | `transition_status_unsafe → blacklisted` (nouvelle méthode) | `blacklisted` |
| T11 | `blacklisted` | `shadow` | wallet retiré de `BLACKLISTED_WALLETS` ET wallet ∉ `TARGET_WALLETS` | non | `transition_status_unsafe → shadow` + reset score/hysteresis | `blacklist_removed` |
| T12 | `blacklisted` | `pinned` | wallet retiré de `BLACKLISTED_WALLETS` ET wallet ∈ `TARGET_WALLETS` | non | `transition_status_unsafe → pinned` + pinned=True | `blacklist_removed_pinned` |

**Note T6 (abort)** : si le `triggering_candidate` qui a causé le `sell_only` était déjà devenu `active` au cycle N (via T3 cascade), alors `abort` au cycle N+3 le renvoie en `shadow` — pas en `sell_only`. C'est un retour en arrière propre, pas une nouvelle eviction.

**Note T3 & T7 — hystérésis identique** : 3 cycles pour déclencher (shadow qui grimpe OU sell_only qui rebondit). Un nombre différent par direction serait en §15 open questions.

### 4.4 Invariants durs

Les invariants suivants doivent être **testés explicitement** (cf. §8) et vérifiés à chaque cycle Discovery (log WARNING + auto-correction si violation) :

1. `count_by_status("active") + count_by_status("pinned") ≤ MAX_ACTIVE_TRADERS` à tout instant.
2. `count_by_status("sell_only") ≤ MAX_SELL_ONLY_WALLETS`.
3. `wallet ∈ BLACKLISTED_WALLETS ⟺ status == "blacklisted"` — vérifié au démarrage (reconciliation) + à chaque cycle Discovery (idempotent).
4. `wallet.status == "pinned" ⟺ wallet.pinned == True` (invariant M5 préservé).
5. `wallet.status == "sell_only" ⟹ wallet.active == True` (sinon le watcher ne pollera pas les SELL à copier — bug).
6. Un `pinned` n'est **jamais** sujet à eviction (safeguard non-négociable, inchangé depuis M5).
7. Si `EVICTION_ENABLED=false` : aucune transition T3/T5/T6/T7/T8 ne doit jamais se produire — seules T1/T2/T4/T9/T10/T11/T12 restent actives (T4 remplace l'ancien `paused` M5 par `shadow` + flag). Vérifié par un test de non-régression M5 explicite (§8.4).
8. Une transition T5 (active → sell_only) ne peut pas se produire sans un T3 ou T7 simultané au même cycle transactionnel (pas de `sell_only` sans un candidat qui prend le slot — sinon on se tire une balle dans le pied en perdant une position active).
9. **`EVICTION_SCORE_MARGIN` asymétrique par direction** : non — une seule valeur pour entrée (T3/T7) ET sortie (T6). Simplicité + cohérence. Asymétrie en §15.

### 4.5 Edge cases tranchés

**EC-1 — Position qui se ferme pile au cycle d'abort** : un `sell_only` a 1 position ouverte, le cycle N observe simultanément `delta(triggering_C, self) < 0.15` (3ᵉ cycle sous seuil, conditions d'abort T6 remplies) ET `positions_open == 0` (conditions T8 remplies).
- **Décision** : priorité à l'abort (T6 > T8). Raison : le wallet a regagné la confiance relative vs le candidat, on préserve son slot. S'il avait été en `sell_only` *uniquement* parce que ses positions étaient en train de wind-down sans regain de confiance, T8 se serait déclenché au cycle précédent (puisque la condition abort ne demande pas `positions == 0`).
- Implémentation : dans `EvictionScheduler.resolve_sell_only_transitions`, évaluer T6 avant T8 dans un `if / elif` strict.

**EC-2 — Deux candidats shadow atteignent l'hystérésis au même cycle** : top shadow A (delta +0.30) et B (delta +0.22) sont tous les deux éligibles.
- **Décision** : séquentiel strict, 1 swap/cycle. A gagne (plus grande delta). B attend le cycle N+1 — son compteur d'hystérésis reste armé (il n'est pas reset).
- Implémentation : trier les candidats par `delta_desc`, prendre le premier, append `trader_events.event_type="eviction_deferred_one_per_cycle"` pour les autres (audit visible). Pas d'alerte Telegram (trop bavard).

**EC-3 — Le worst_active change entre le cycle N et N+1 pendant hystérésis** : au cycle N, worst_active = 0xA (score 0.66). Le shadow 0xB arme l'hystérésis (delta +0.20). Au cycle N+1, 0xA a gagné en score → worst_active = 0xC (score 0.60). Delta(0xB, 0xC) = +0.26, toujours ≥ 0.15.
- **Décision** : l'hystérésis est portée par **le candidat** (le shadow 0xB), pas par le couple (candidat, worst). Au cycle N+1, on re-évalue vs le worst_active *courant*. Si delta toujours ≥ margin, le compteur s'incrémente. Si `EVICTION_HYSTERESIS_CYCLES=3` cycles consécutifs ≥ margin, T3 déclenche avec le worst_active *du cycle de déclenchement*.
- `eviction_triggering_wallet` stocké sur le wallet évincé = worst_active du cycle où T5 déclenche.

**EC-4 — Plusieurs wallets SHADOW rebondissent et éligibles par T7 au même cycle** : même règle qu'EC-2 (séquentiel, tri par delta).

**EC-5 — `sell_only` dont le score reste stagnant (ni rebond ni abort)** : il reste en `sell_only` indéfiniment tant que `positions_open > 0`. Au moment où les positions ferment (via SELL copié), T8 s'applique → passage en `shadow`, conservation du score. Conforme au contrat user « wind-down réversible ».

**EC-6 — `MAX_SELL_ONLY_WALLETS` atteint**: un T3/T7 voudrait cascader un worst → sell_only, mais `count_by_status("sell_only") == MAX_SELL_ONLY_WALLETS`.
- **Décision** : skip la cascade. Pas de T3/T7 au cycle N. Log WARNING structlog `eviction_deferred_sell_only_cap`, persist `trader_events.event_type="eviction_deferred_sell_only_cap"` avec candidat + triggering. Pas d'alerte Telegram (répétition possible bruyante).
- Raison : si on ignorait le cap, une cascade pathologique (scores très volatils) pourrait accumuler les `sell_only` jusqu'à saturation mémoire / DB.

**EC-7 — Wallet `pinned` avec score inférieur au worst_active candidate margin** : un pinned est **jamais** worst_active au sens eviction. Exclu du pool `active` sujet à cascade. Implémentation : `worst_active = min(score for t in traders if t.status == 'active' AND NOT t.pinned)`. Si tous les actives sont pinned, **aucune eviction possible** — log WARNING `eviction_all_actives_pinned` une fois par cycle, continuer cycle normalement.

**EC-8 — `EVICTION_ENABLED` flippe de `true` → `false` entre deux cycles** : un wallet déjà en `sell_only` au cycle de flip reste en `sell_only` jusqu'à T8 (positions closed) — on ne force pas un retour active immédiat (cela cascaderait un autre wallet). Log WARNING au boot `eviction_disabled_with_pending_sell_only` listant les wallets concernés. Ils se résoudront via T8 naturellement.

---

## 5. Configuration

### 5.1 Nouvelles env vars

| Nom | Type | Default | Validation | Loggé au boot ? | Notes |
|---|---|---|---|---|---|
| `EVICTION_ENABLED` | `bool` | `False` | — | ✓ clair | Maître opt-in. Off = zéro diff M5. |
| `EVICTION_SCORE_MARGIN` | `float` | `0.15` | range [0.05, 0.50] | ✓ clair | Delta minimum candidat vs worst_active. |
| `EVICTION_HYSTERESIS_CYCLES` | `int` | `3` | range [1, 10] | ✓ clair | Cycles consécutifs où la condition doit tenir. S'applique T3, T6, T7 (même valeur — cf. §15 asymétrie). |
| `MAX_SELL_ONLY_WALLETS` | `int` | `= MAX_ACTIVE_TRADERS` | range [1, 100] | ✓ clair | Cap dur taille pool `sell_only`. Si non set → même valeur que `MAX_ACTIVE_TRADERS`. |

Ajouts dans `.env.example` (bloc discovery, après `SCORING_DEMOTION_HYSTERESIS_CYCLES`) :

```ini
# M5_bis — Competitive eviction (opt-in strict, off = M5 lifecycle inchangé).
EVICTION_ENABLED=false
EVICTION_SCORE_MARGIN=0.15
EVICTION_HYSTERESIS_CYCLES=3
# Cap taille pool sell_only (wind-down). Non set = MAX_ACTIVE_TRADERS.
# MAX_SELL_ONLY_WALLETS=10
```

### 5.2 Env vars existantes impactées

| Env var | Impact M5_bis |
|---|---|
| `BLACKLISTED_WALLETS` | Écriture du status `blacklisted` (plutôt que skip-without-persist M5). Reconciliation boot + cycle. |
| `MAX_ACTIVE_TRADERS` | Inchangé. Cap `active + pinned` ; `sell_only` compte séparément. |
| `TARGET_WALLETS` | Les pinned restent intouchables par T3/T5/T7. Interaction avec blacklist : un wallet simultanément dans `TARGET_WALLETS` et `BLACKLISTED_WALLETS` → **crash boot** (incohérence non récupérable, validator Pydantic). |
| `SCORING_PROMOTION_THRESHOLD` / `SCORING_DEMOTION_THRESHOLD` | Inchangés. T2 (promotion M5 classique) et T4 (demote M5 classique) les utilisent. T3/T5/T6/T7 utilisent **uniquement** `EVICTION_SCORE_MARGIN` (delta-based, pas absolute-threshold-based). |

### 5.3 Validator cross-field Pydantic

Dans `Settings._validate_discovery_thresholds` ([config.py:922-934](../../src/polycopy/config.py#L922)), ajouter :

```python
if self.eviction_enabled:
    overlap = set(w.lower() for w in self.blacklisted_wallets) & set(w.lower() for w in self.target_wallets)
    if overlap:
        raise ValueError(
            f"Conflict: wallets {sorted(overlap)} are in both TARGET_WALLETS and "
            f"BLACKLISTED_WALLETS. Pick one."
        )
    if self.max_sell_only_wallets is None:
        self.max_sell_only_wallets = self.max_active_traders
```

---

## 6. Migration DB (Alembic 0007)

### 6.1 Changements de schéma

- Étendre la contrainte applicative sur `target_traders.status` : ajout de `'sell_only'` et `'blacklisted'`, retrait de `'paused'` (fusion vers `'shadow'`).
- 3 nouvelles colonnes sur `target_traders` :
  - `previously_demoted_at: datetime | None` — flag UX qui survit à la migration 0007 (ex-`paused` deviennent `shadow` avec ce champ set).
  - `eviction_state_entered_at: datetime | None` — tracking du timer pour calcul durée `sell_only` dans dashboard + audit.
  - `eviction_triggering_wallet: str | None` — wallet qui a causé le `sell_only` courant. Nullable. FK logique (pas contrainte SQLite) vers `wallet_address`. Clé pour évaluer la condition T6 abort.
- Pas d'index additionnel : les colonnes eviction sont jamais query-filtered (toujours lookup par `wallet_address` ou `status`, déjà indexés).

### 6.2 Alembic 0007 — pseudo-code

```python
"""M5_bis competitive eviction schema.

Révision : 0007_m5_bis_eviction
Revises  : 0006_m12_trader_daily_pnl
"""

def upgrade() -> None:
    # 1. Ajout colonnes sur target_traders (batch SQLite-friendly).
    with op.batch_alter_table("target_traders", recreate="always") as batch:
        batch.add_column(sa.Column("previously_demoted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("eviction_state_entered_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("eviction_triggering_wallet", sa.String(42), nullable=True))

    # 2. Data migration idempotente : paused → shadow avec audit.
    #    Rerun safe : `WHERE status='paused'` sera vide au 2e run.
    op.execute(
        """
        UPDATE target_traders
        SET status = 'shadow',
            active = 0,
            previously_demoted_at = COALESCE(last_scored_at, CURRENT_TIMESTAMP)
        WHERE status = 'paused'
        """,
    )
    # Note : on ne reset pas consecutive_low_score_cycles — le prochain cycle
    # Discovery le remettra à 0 via reset_hysteresis=True au prochain score >= demo.

    # 3. Aucune transition de type blacklisted à la migration : ce status est
    #    posé dynamiquement au boot + à chaque cycle par EvictionScheduler
    #    via BLACKLISTED_WALLETS env. La migration laisse ces wallets
    #    inchangés ; ils passeront en 'blacklisted' au 1er boot post-upgrade.


def downgrade() -> None:
    # Rollback safe : on remet les sell_only en shadow (wind-down ajourné),
    # on garde les blacklisted tels quels (leur status reviendra via M5).
    op.execute("UPDATE target_traders SET status='shadow' WHERE status='sell_only'")
    op.execute("UPDATE target_traders SET status='paused' WHERE status='shadow' AND previously_demoted_at IS NOT NULL")
    op.execute("UPDATE target_traders SET status='shadow' WHERE status='blacklisted'")
    with op.batch_alter_table("target_traders", recreate="always") as batch:
        batch.drop_column("eviction_triggering_wallet")
        batch.drop_column("eviction_state_entered_at")
        batch.drop_column("previously_demoted_at")
```

**Pattern SQLite-friendly** : identique migration 0003 ([alembic/versions/0003_m5_discovery_schema.py:34](../../alembic/versions/0003_m5_discovery_schema.py#L34)) — `batch_alter_table(recreate="always")`.

### 6.3 Rollout plan

1. **Staging** : run migration sur un dump DB prod copié → vérifier count `status` before/after (doit être `active+shadow+paused+pinned = active+shadow+pinned` avec `paused → shadow` conservé via `previously_demoted_at`).
2. **Test idempotence** : run migration deux fois de suite sur même DB — 2ᵉ run no-op (log `Rows affected: 0`).
3. **Prod** : `alembic upgrade head` au boot comme d'habitude ([cli/boot.py](../../src/polycopy/cli/boot.py) → `init_db`). Pas de downtime spécifique.
4. **Backup pre-upgrade** : copier `polycopy.db` → `polycopy.db.bak-$(date +%Y%m%d)` avant le premier boot M5_bis (convention existante, cf. fichier `polycopy.db.bak-20260419` dans le repo).

---

## 7. Composants & intégration code

### 7.1 Nouveau package `src/polycopy/discovery/eviction/`

```
src/polycopy/discovery/eviction/
├── __init__.py              # Exports : EvictionScheduler, EvictionDecision
├── state_machine.py         # classify_transitions(pool, settings) → list[EvictionDecision]
├── scheduler.py             # EvictionScheduler : coordonne T3/T5/T6/T7/T8 + cascade
├── cascade_planner.py       # plan_single_cascade(candidates, actives, caps) → (candidate, worst)
├── hysteresis_tracker.py    # HysteresisTracker : compteurs in-memory par wallet
└── dtos.py                  # EvictionDecision, EvictionPlan, HysteresisState
```

**Règle de dépendance** (cohérent CLAUDE.md) :
- `eviction/` → `storage` (lecture/écriture TargetTrader), `monitoring` (alerts queue), `config`.
- `eviction/` ne dépend **pas** de `watcher`, `strategy`, `executor` directement — les consommateurs de `sell_only` (watcher via `list_active`, strategy via `TraderLifecycleFilter`) lisent la DB, pas le package eviction.

**Classes clés** :

```python
# dtos.py
@dataclass(frozen=True)
class EvictionDecision:
    wallet_address: str
    transition: Literal[
        "promote_via_eviction",      # T3 (shadow → active*)
        "demote_to_sell_only",       # T5 (active → sell_only)
        "abort_to_active",           # T6 (sell_only → active)
        "promote_via_rebound",       # T7 (sell_only → active*)
        "complete_to_shadow",        # T8 (sell_only → shadow, positions closed)
        "blacklist",                 # T10 (any → blacklisted)
        "unblacklist",               # T11/T12 (blacklisted → shadow/pinned)
    ]
    from_status: str
    to_status: str
    score_at_event: float | None
    delta_vs_worst_active: float | None
    triggering_wallet: str | None
    cycles_observed: int | None
    reason_code: str

# scheduler.py
class EvictionScheduler:
    def __init__(
        self,
        target_repo: TargetTraderRepository,
        position_repo: MyPositionRepository,  # M3+, existant
        event_repo: TraderEventRepository,
        settings: Settings,
        alerts_queue: asyncio.Queue[Alert] | None = None,
    ) -> None:
        ...
        self._hysteresis = HysteresisTracker()  # in-memory, vie = uptime process

    async def run_cycle(
        self,
        scoring_results: dict[str, ScoringResult],
    ) -> list[EvictionDecision]:
        """Appelée par DiscoveryOrchestrator APRÈS DecisionEngine.decide(),
        AVANT persistance finale. Retourne décisions à appliquer en cascade.
        """
```

### 7.2 Hook dans `DiscoveryOrchestrator`

Modif [orchestrator.py:259-406](../../src/polycopy/discovery/orchestrator.py#L259) — la boucle per-wallet reste M5. **Après** le scoring + DecisionEngine, **avant** le commit final du cycle, ajouter un bloc :

```python
# Après la boucle per-wallet decision_engine.decide() :
if cfg.eviction_enabled:
    eviction_decisions = await eviction_scheduler.run_cycle(scoring_by_wallet)
    for dec in eviction_decisions:
        await self._apply_eviction_decision(dec)
        await self._persist_event_from_eviction(dec, version=cfg.scoring_version)
        await self._push_eviction_alert(dec)
    # Reconciliation blacklist (idempotent)
    await eviction_scheduler.reconcile_blacklist()
```

Estimé : **+40 LOC** dans `orchestrator.py` (hook + 3 helpers privés).

### 7.3 Extension `TargetTraderRepository`

Méthodes à ajouter dans [repositories.py:41-236](../../src/polycopy/storage/repositories.py#L41) :

```python
# Nouvelle signature _StatusTransition (accepte sell_only + blacklisted)
_StatusTransition = Literal["shadow", "active", "sell_only", "blacklisted"]

async def list_active(self) -> list[TargetTrader]:
    """ÉTENDU M5_bis — inclut sell_only pour que le watcher continue
    à poller ces wallets (SELL à copier).
    """
    async with self._session_factory() as session:
        stmt = select(TargetTrader).where(
            TargetTrader.active.is_(True),
            TargetTrader.status.in_(("active", "pinned", "sell_only")),
        )
        ...

async def list_eviction_candidates(self) -> list[TargetTrader]:
    """Retourne shadow + sell_only triés par score desc (candidats T3/T7)."""

async def list_actives_eligible_for_eviction(self) -> list[TargetTrader]:
    """Retourne active non-pinned triés par score asc (worst en tête)."""

async def transition_status_unsafe(
    self,
    wallet_address: str,
    *,
    new_status: Literal["blacklisted", "shadow", "pinned"],
) -> TargetTrader:
    """Override pinned-safeguard pour les transitions blacklist-driven (T10/T11/T12).
    Usage strict par EvictionScheduler.reconcile_blacklist UNIQUEMENT.
    """

async def set_eviction_state(
    self,
    wallet_address: str,
    *,
    entered_at: datetime | None,
    triggering_wallet: str | None,
) -> None:
    """Écrit eviction_state_entered_at + eviction_triggering_wallet (ou les clear si None)."""

async def set_previously_demoted_at(
    self,
    wallet_address: str,
    *,
    at: datetime,
) -> None:
    """Écrit le flag UX après T4 (demote M5) ou T9 (migration paused→shadow)."""
```

**Signature `transition_status` existante** ([repositories.py:178-212](../../src/polycopy/storage/repositories.py#L178)) — étendre `_StatusTransition` à `"sell_only"`. Le ValueError pinned existant reste applicable pour T5 (un pinned ne peut pas devenir sell_only). Pour T10 (→ blacklisted) on utilise la méthode unsafe séparée.

Estimé : **+80 LOC** dans `repositories.py`.

### 7.4 Extension `DecisionEngine`

[decision_engine.py:49-180](../../src/polycopy/discovery/decision_engine.py#L49) — modifications :

1. Retrait de la branche `paused` ([decision_engine.py:301-337](../../src/polycopy/discovery/decision_engine.py#L301)) — ce status n'existe plus post-migration 0007.
2. Ajout d'une branche `sell_only` minimale : si current.status == "sell_only", retourne `keep` (la logique de transitions T6/T7/T8 vit dans `EvictionScheduler`, pas dans `DecisionEngine` — séparation stricte M5 lifecycle / M5_bis compétition).
3. Inchangé pour blacklist — M5 skip-without-persist (lignes 67-77) reste tant que la reconciliation blacklist est pilotée par `EvictionScheduler.reconcile_blacklist` en dehors de `decide()`.

Estimé : **-35 LOC** (branche paused supprimée) **+10 LOC** (branche sell_only) = **-25 LOC net** dans `decision_engine.py`.

### 7.5 Nouveau filtre `TraderLifecycleFilter` (strategy pipeline)

Ajout en **première position** du pipeline [pipeline.py:210-215](../../src/polycopy/strategy/pipeline.py#L210) — cheap DB lookup, bail out early si wallet non-tradable côté BUY :

```python
class TraderLifecycleFilter:
    """Bloque les BUY pour les wallets en sell_only. Les SELL passent tous.

    Pipeline order M5_bis : TraderLifecycleFilter → MarketFilter →
    PositionSizer → SlippageChecker → RiskManager. Si EVICTION_ENABLED=false,
    le filtre retourne toujours passed=True (fast path).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._sf = session_factory
        self._settings = settings

    async def check(self, ctx: PipelineContext) -> FilterResult:
        if not self._settings.eviction_enabled:
            return FilterResult(passed=True)
        if ctx.trade.side == "SELL":
            return FilterResult(passed=True)
        # BUY : lookup wallet status
        async with self._sf() as session:
            stmt = select(TargetTrader.status).where(
                TargetTrader.wallet_address == ctx.trade.target_wallet.lower(),
            )
            status = (await session.execute(stmt)).scalar_one_or_none()
        if status == "sell_only":
            return FilterResult(passed=False, reason="wallet_sell_only")
        return FilterResult(passed=True)
```

**Intégration dans `run_pipeline`** ([pipeline.py:210-215](../../src/polycopy/strategy/pipeline.py#L210)) : ajouter `("TraderLifecycleFilter", TraderLifecycleFilter(session_factory, settings))` en tête du tuple `filters`. Pas d'instrumentation latence dédiée (coût négligeable, reste agrégé dans `strategy_filtered_ms` M11).

Estimé : **+45 LOC** dans `pipeline.py` (nouvelle classe + intégration).

### 7.6 Reconciliation blacklist

Méthode `EvictionScheduler.reconcile_blacklist` appelée :
- **Au boot** (dans `_build_orchestrators`, avant lancement TaskGroup) — one-shot.
- **À chaque cycle Discovery** (dernière étape avant commit cycle) — idempotent.

Logique :
```python
async def reconcile_blacklist(self) -> list[EvictionDecision]:
    cfg = self._settings
    blacklist = {w.lower() for w in cfg.blacklisted_wallets}
    all_traders = await self._target_repo.list_all()
    decisions = []
    for t in all_traders:
        in_blacklist = t.wallet_address in blacklist
        if in_blacklist and t.status != "blacklisted":
            await self._target_repo.transition_status_unsafe(
                t.wallet_address, new_status="blacklisted",
            )
            decisions.append(EvictionDecision(
                wallet_address=t.wallet_address,
                transition="blacklist",
                from_status=t.status,
                to_status="blacklisted",
                score_at_event=t.score,
                reason_code="user_added_to_env",
                ...
            ))
        elif not in_blacklist and t.status == "blacklisted":
            restore_to = "pinned" if t.wallet_address in {w.lower() for w in cfg.target_wallets} else "shadow"
            await self._target_repo.transition_status_unsafe(
                t.wallet_address, new_status=restore_to,
            )
            decisions.append(EvictionDecision(
                wallet_address=t.wallet_address,
                transition="unblacklist",
                from_status="blacklisted",
                to_status=restore_to,
                ...
            ))
    return decisions
```

Idempotent : 2ᵉ appel sans changement env = 0 décision retournée.

### 7.7 `HysteresisTracker` in-memory

```python
# hysteresis_tracker.py
@dataclass
class HysteresisState:
    direction: Literal["eviction", "abort", "rebound"]
    target_wallet: str | None  # wallet worst_active ou triggering_candidate, selon direction
    cycles_observed: int
    first_observed_at: datetime

class HysteresisTracker:
    """État in-memory par wallet. Vie = uptime du process Discovery.

    Trade-off : un restart reset les compteurs → peut retarder une eviction
    (jusqu'à 3 cycles supplémentaires). Acceptable : Discovery cycle = 6h par
    défaut, et les restarts sont rares (live). Alternative persistante (DB)
    rejetée pour éviter un nouveau schéma + une query par cycle.
    """
    def __init__(self) -> None:
        self._states: dict[str, HysteresisState] = {}

    def tick(self, wallet: str, direction: str, target: str | None) -> int:
        """Incrémente le compteur, reset si direction/target changent."""
        ...

    def reset(self, wallet: str) -> None: ...
    def count(self, wallet: str) -> int: ...
```

Estimé : **+90 LOC** pour le package `eviction/` entier.

---

## 8. Test plan

Couverture cible **≥90 %** sur `src/polycopy/discovery/eviction/`, **≥80 %** sur les extensions `decision_engine.py`, `orchestrator.py`, `repositories.py`.

### 8.1 Unitaires `EvictionStateMachine` (table-driven)

`tests/unit/test_eviction_state_machine.py` — table ≥15 cas couvrant toutes les transitions :

| ID | Scénario | Attendu |
|---|---|---|
| SM-01 | shadow 0.91, worst_active 0.66, 3 cycles → T3 | `promote_via_eviction` + cascade T5 |
| SM-02 | shadow 0.91, worst_active 0.66, 2 cycles seulement | `keep` (hystérésis pas atteinte) |
| SM-03 | shadow 0.91, worst_active 0.80 (delta 0.11) | `keep` (sous margin) |
| SM-04 | sell_only delta(triggering, self) 0.10 × 3 | `abort_to_active` |
| SM-05 | sell_only score 0.82, worst_active 0.64, 3 cycles | `promote_via_rebound` + cascade |
| SM-06 | sell_only, positions_open=0, pas rebond | `complete_to_shadow` |
| SM-07 | sell_only, positions_open=0, ET abort conditions | `abort_to_active` (EC-1 priorité) |
| SM-08 | EVICTION_ENABLED=false, shadow 0.91, worst_active 0.66 × 3 | `keep` (pas de transition — non-régression M5) |
| SM-09 | Cap MAX_SELL_ONLY_WALLETS atteint | `defer_cap` + event_type="eviction_deferred_sell_only_cap" |
| SM-10 | 2 shadows éligibles même cycle (delta +0.30 et +0.22) | 1 T3 pour le +0.30, `defer_one_per_cycle` pour l'autre |
| SM-11 | worst_active change entre cycles pendant hystérésis (EC-3) | compteur porté par candidat, déclenche au cycle 3 avec worst courant |
| SM-12 | Tous les actives sont pinned | `keep` + log `eviction_all_actives_pinned` |
| SM-13 | wallet ajouté à BLACKLISTED_WALLETS live | `blacklist` au prochain reconcile |
| SM-14 | wallet retiré de BLACKLISTED_WALLETS ET ∈ TARGET_WALLETS | `unblacklist` → pinned |
| SM-15 | wallet retiré de BLACKLISTED_WALLETS ET ∉ TARGET_WALLETS | `unblacklist` → shadow, reset hysteresis |

### 8.2 Unitaires `CascadePlanner` + `HysteresisTracker`

`tests/unit/test_cascade_planner.py` :
- Tri par delta desc, sélection du top.
- Respect `MAX_SELL_ONLY_WALLETS`.
- Exclusion pinned du worst_active.

`tests/unit/test_hysteresis_tracker.py` :
- Reset sur changement de direction.
- Comptage monotone si direction + target stables.
- Reset explicite.

### 8.3 Intégration `DiscoveryOrchestrator`

`tests/integration/test_discovery_eviction_integration.py` (opt-in `-m integration`, mais **DB SQLite in-memory uniquement** — pas de réseau) :
- Setup 7 actives + 2 shadows avec scores pré-calculés.
- Run 3 cycles Discovery consécutifs (mock `DataApiClient` via `respx`).
- Vérifier après cycle 3 : 1 shadow → active, 1 active → sell_only, events persistés avec bon `event_type`, alert Telegram poussée dans la queue.

### 8.4 Non-régression M5 (`EVICTION_ENABLED=false`)

`tests/unit/test_eviction_disabled_m5_strict.py` :
- Tous les tests existants de `test_decision_engine.py` passent avec `EVICTION_ENABLED=false` (copie la suite, vérifie output identique).
- 7 actives, 2 shadows qui atteindraient normalement T3 → tous en `keep` (aucune transition eviction).
- Vérification explicite : aucun wallet n'entre en `sell_only` après 10 cycles simulés.

### 8.5 Migration Alembic 0007

`tests/unit/test_alembic_0007_migration.py` :
- DB seed avec 2 `active`, 3 `shadow`, 1 `paused` (ancien M5), 1 `pinned`.
- `alembic upgrade head` → `paused` devient `shadow` + `previously_demoted_at` set à `last_scored_at` (ou `CURRENT_TIMESTAMP` si NULL).
- Re-run `alembic upgrade head` → no-op (idempotent).
- `alembic downgrade -1` → `shadow` avec `previously_demoted_at IS NOT NULL` redevient `paused`.

### 8.6 Pipeline strategy — `TraderLifecycleFilter`

`tests/unit/test_trader_lifecycle_filter.py` :
- wallet `active`, side BUY → passed.
- wallet `sell_only`, side BUY → rejected avec `reason="wallet_sell_only"`.
- wallet `sell_only`, side SELL → passed (contrat wind-down).
- wallet `shadow`, side BUY → passed (le watcher ne génère pas ce cas, mais défensif).
- EVICTION_ENABLED=false → toujours passed (fast path).

### 8.7 Dashboard + alertes

`tests/unit/test_dashboard_eviction_ui.py` :
- Filtre `/traders?status=sell_only` retourne uniquement les sell_only.
- Badge `sell_only` rendu avec classe CSS `badge-warning` (orange) + attribut `data-status="sell_only"`.
- Colonne `eviction_state_entered_at` affichée si status=sell_only, vide sinon.

`tests/unit/test_telegram_eviction_templates.py` :
- Les 4 templates compilent avec `StrictUndefined`.
- Rendu d'un `Alert(event="trader_eviction_started", ...)` contient `mode_badge` + `machine_id` + `dashboard_url`.
- Aucun secret (TELEGRAM_BOT_TOKEN, PRIVATE_KEY, etc.) n'apparaît — grep automatisé.

---

## 9. Invariants sécurité

- **Cap dur M5 préservé** : `active + pinned ≤ MAX_ACTIVE_TRADERS`, jamais assoupli par M5_bis (une cascade = -1 active +1 active, bilan zéro).
- **Cap sell_only** : `MAX_SELL_ONLY_WALLETS` (défaut = `MAX_ACTIVE_TRADERS`) évite la cascade pathologique (scores très volatils saturant le wind-down).
- **Feature flag off = M5 strict** : `EVICTION_ENABLED=false` ⇒ aucun code-path eviction activé. `EvictionScheduler` n'est **pas instancié** dans `DiscoveryOrchestrator` si flag off. `TraderLifecycleFilter` est instancié mais retourne `passed=True` instantanément (fast path ligne 1).
- **Audit trail append-only** : chaque transition T1..T12 (sauf T9 migration) écrit une ligne `trader_events` avec `event_metadata` riche (from_status, to_status, score_before, score_after, delta_vs_worst_active, triggering_wallet, cycles_observed, reason_code). Jamais supprimé (conforme invariant M5 `trader_events` = sacré).
- **Jamais de force-close** : `sell_only` n'émet aucun ordre SELL. Les positions ferment via copy-trading classique (le wallet source émet un SELL → le bot copie via pipeline normal, side=SELL autorisé par `TraderLifecycleFilter`) OU à résolution du marché (M8 resolution watcher).
- **État terminal manuel uniquement** : seul `blacklisted` est terminal, et seul l'utilisateur peut y placer ou en retirer un wallet via `BLACKLISTED_WALLETS`. Le bot n'a aucun mécanisme pour blacklister automatiquement (cf. §15 open question `auto_blacklist_on_wash_cluster`).
- **Pas de secret loggé** : les 4 nouveaux templates héritent du filtre `telegram_md_escape` + discipline M7 (aucun `TELEGRAM_BOT_TOKEN`, aucune clé privée dans `event_metadata`). Test `test_telegram_eviction_templates.py` grep automatisé.
- **Reconciliation `BLACKLISTED_WALLETS`** : idempotente, re-run safe. Un flip live ajoute/retire sans corruption de l'état machine.
- **Cohérence `sell_only.active=True`** : vérifiée au démarrage (auto-correction si divergence, log WARNING). Sans cela le watcher ne pollerait plus le wallet et on raterait ses SELL → positions orphelines.

---

## 10. Impact sur l'existant — fichiers touchés

| Fichier | LOC estimés | Nature du changement |
|---|---|---|
| [src/polycopy/discovery/eviction/\*](../../src/polycopy/discovery/) | +400 (nouveau package) | Création complète (scheduler, state_machine, cascade_planner, hysteresis_tracker, dtos) |
| [src/polycopy/discovery/decision_engine.py](../../src/polycopy/discovery/decision_engine.py) | -25 net | Retrait branche paused + ajout branche sell_only (minimale) |
| [src/polycopy/discovery/orchestrator.py](../../src/polycopy/discovery/orchestrator.py) | +40 | Hook EvictionScheduler + 3 helpers privés |
| [src/polycopy/storage/models.py](../../src/polycopy/storage/models.py) | +15 | 3 nouvelles colonnes TargetTrader |
| [src/polycopy/storage/repositories.py](../../src/polycopy/storage/repositories.py) | +80 | list_active étendu, list_eviction_candidates, transition_status_unsafe, set_eviction_state, set_previously_demoted_at |
| [src/polycopy/storage/dtos.py](../../src/polycopy/storage/dtos.py) | +10 | TraderEventDTO : ajout field event_type enum étendu |
| [src/polycopy/strategy/pipeline.py](../../src/polycopy/strategy/pipeline.py) | +45 | TraderLifecycleFilter + intégration tuple filters |
| [src/polycopy/config.py](../../src/polycopy/config.py) | +40 | 4 env vars + validator cross-field target/blacklist |
| [src/polycopy/dashboard/queries.py](../../src/polycopy/dashboard/queries.py) | +20 | `_VALID_TRADER_STATUSES` étendu (sell_only, blacklisted), TraderRow ajout 3 champs |
| [src/polycopy/dashboard/templates/traders.html](../../src/polycopy/dashboard/templates/traders.html) | +15 | Filter-chips sell_only + blacklisted, colonnes delta/cycles |
| [src/polycopy/dashboard/templates/partials/traders_rows.html](../../src/polycopy/dashboard/templates/partials/) | +10 | Badge rendering par status |
| [src/polycopy/dashboard/templates/macros.html](../../src/polycopy/dashboard/templates/) | +5 | badge() macro couleurs Radix pour sell_only (orange) + blacklisted (rouge) |
| [src/polycopy/monitoring/templates/\*.md.j2](../../src/polycopy/monitoring/templates/) | +4 fichiers | trader_eviction_started, trader_eviction_aborted, trader_eviction_completed_to_shadow, trader_eviction_completed_to_active_via_rebound, trader_blacklisted (5 au total — cf. §11) |
| [alembic/versions/0007_m5_bis_eviction.py](../../alembic/versions/) | +70 (nouveau) | Migration up/down |
| [.env.example](../../.env.example) | +10 | Bloc commenté M5_bis |
| [CLAUDE.md](../../CLAUDE.md) | +25 | Paragraphe Discovery M5_bis dans section Conventions + Sécurité |
| [docs/specs/ROADMAP.md](./ROADMAP.md) | +2 | Ligne `M5_bis Competitive eviction ✅ spec` dans tableau |
| tests/unit/test_eviction_\*.py | +600 (8 fichiers) | Suite test unitaire complète |
| tests/integration/test_discovery_eviction_integration.py | +200 | Intégration 3 cycles |

**Total LOC production** : ~**780 LOC ajoutés**, **-25 LOC retirés**, soit **~755 LOC net production** + **~800 LOC tests**. Aucun module d'infrastructure transverse (monitoring, executor, watcher hors filtre) profondément modifié.

---

## 11. Alertes Telegram — nouveaux event types

5 nouveaux templates à créer dans [src/polycopy/monitoring/templates/](../../src/polycopy/monitoring/templates/), tous recevant automatiquement `mode_badge`, `machine_id`, `machine_emoji`, `dashboard_url` via `_inject_context` ([alert_renderer.py:157-193](../../src/polycopy/monitoring/alert_renderer.py#L157)).

| Fichier | Event type | Level | Émis par |
|---|---|---|---|
| `trader_eviction_started.md.j2` | `trader_eviction_started` | INFO | `EvictionScheduler._push_alert(T3)` |
| `trader_eviction_aborted.md.j2` | `trader_eviction_aborted` | INFO | `EvictionScheduler._push_alert(T6)` |
| `trader_eviction_completed_to_shadow.md.j2` | `trader_eviction_completed_to_shadow` | INFO | `EvictionScheduler._push_alert(T8)` |
| `trader_eviction_completed_to_active_via_rebound.md.j2` | `trader_eviction_completed_to_active_via_rebound` | INFO | `EvictionScheduler._push_alert(T7)` |
| `trader_blacklisted.md.j2` | `trader_blacklisted` | WARNING | `EvictionScheduler.reconcile_blacklist` |

**Pattern template** (copie stricte de [trader_promoted.md.j2](../../src/polycopy/monitoring/templates/trader_promoted.md.j2)) :

```jinja2
_\[{{ mode_badge | telegram_md_escape }}\]_
{{ machine_emoji }} *{{ machine_id | telegram_md_escape }}*
🟣 *\[trader\_eviction\_started\]* INFO

{{ body | telegram_md_escape }}

_Discovery M15\_bis — compétition adaptative, un candidat évince un active\._
{% if dashboard_url %}
[📊 Dashboard]({{ dashboard_url }})
{% endif %}
```

**Body construction** (côté `EvictionScheduler` avant push dans la queue) :

```python
body = (
    f"Candidat : {short(candidate)} (score {candidate_score:.2f}, {from_status_c})\n"
    f"Évincé   : {short(worst)} (score {worst_score:.2f}, active → sell_only)\n"
    f"Delta    : +{delta:.2f} ≥ {cfg.eviction_score_margin:.2f} sur "
    f"{cycles} cycles\n"
    f"Positions ouvertes {short(worst)} : {open_positions} (wind-down)"
)
```

`short(wallet)` = `wallet[:6] + "…" + wallet[-4:]` — hygiène visuelle Telegram.

**Pas d'alerte pour** : `eviction_deferred_one_per_cycle`, `eviction_deferred_sell_only_cap`, `eviction_all_actives_pinned` — trop bavard. Trace uniquement dans `trader_events` + log structlog (conforme décision M7 §13 silencieux sur events basse valeur, identique `gate_rejected` M12).

---

## 12. Dashboard UX `/traders`

### 12.1 Nouveaux filter-chips

Dans [templates/traders.html:13-19](../../src/polycopy/dashboard/templates/traders.html) :

```html
<a href="/traders" class="filter-chip {% if not status_filter %}filter-chip-active{% endif %}">Tous</a>
<a href="/traders?status=pinned" class="filter-chip {% if status_filter == 'pinned' %}filter-chip-active{% endif %}">Pinned</a>
<a href="/traders?status=active" class="filter-chip {% if status_filter == 'active' %}filter-chip-active{% endif %}">Active</a>
<a href="/traders?status=shadow" class="filter-chip {% if status_filter == 'shadow' %}filter-chip-active{% endif %}">Shadow</a>
<a href="/traders?status=sell_only" class="filter-chip {% if status_filter == 'sell_only' %}filter-chip-active{% endif %}">Sell-only</a>
<a href="/traders?status=blacklisted" class="filter-chip {% if status_filter == 'blacklisted' %}filter-chip-active{% endif %}">Blacklisted</a>
```

### 12.2 Badges status — palette Radix

| Status | Classe CSS | Couleur Radix | Rationale |
|---|---|---|---|
| `pinned` | `badge-info` | blue-9 | Whitelist user, confiance explicite |
| `active` | `badge-success` | green-9 | Trading normal |
| `shadow` | `badge-neutral` | gray-9 | Observation, neutre |
| `sell_only` | `badge-warning` | orange-9 | Wind-down, attention visuelle |
| `blacklisted` | `badge-danger` | red-9 | Exclusion terminale, alerte forte |

Ajouts dans [templates/macros.html](../../src/polycopy/dashboard/templates/) (extension `badge(value, label)`).

### 12.3 Colonnes additionnelles conditionnelles

Pour un `sell_only`, afficher dans le `<details>` expandable :
- `eviction_state_entered_at` formaté relatif (« il y a 2h »).
- `eviction_triggering_wallet` short + lien.
- `positions_open` count (via query JOIN sur `my_positions WHERE closed_at IS NULL`).

Pour un `shadow` avec `previously_demoted_at IS NOT NULL` : label UX « re-observation » avec date.

### 12.4 Tri par score

Inchangé : `ORDER BY score DESC NULLS LAST`. Les 4 status mixés dans la liste `/traders` sans filtre — l'utilisateur filtre si besoin. Les `blacklisted` apparaissent en bas (score souvent figé).

### 12.5 Accessibilité / non-régression M6

- Conforme invariant M4.5/M6 : endpoint `GET` only, aucun bouton write, aucun secret exposé.
- `test_dashboard_security.py` + `test_dashboard_security_m6.py` doivent passer identiques.
- CDN Tailwind + Radix inchangés — pas de nouveau bundle.

---

## 13. Plan d'implémentation phasé

Chaque phase = une branche `feat/m5bis-phase-<X>` + PR squashée vers `main` + validation humaine.

### Phase A — Migration + modèle + enum

**Commits attendus** :
1. `feat(storage): extend TargetTrader with eviction columns (previously_demoted_at, eviction_state_entered_at, eviction_triggering_wallet)`
2. `feat(storage): extend _TraderStatus enum (+sell_only, +blacklisted, -paused)`
3. `feat(alembic): 0007 migration — paused→shadow data migration + add eviction columns`
4. `test(alembic): 0007 upgrade/downgrade idempotent, paused→shadow backfill`

**Critères d'acceptation A** :
- `alembic upgrade head` tourne clean sur une DB seed (2 active, 3 shadow, 1 paused, 1 pinned).
- Re-run `alembic upgrade head` = no-op.
- `alembic downgrade -1` reverse proprement.
- Tous les tests existants passent (M5 + M12) — le status `paused` n'est plus généré par aucun code-path, mais les fixtures de test ne doivent pas le référencer en sortie.

### Phase B — EvictionScheduler + state machine (flag off)

**Commits** :
1. `feat(discovery): scaffold discovery/eviction package + DTOs`
2. `feat(discovery): HysteresisTracker in-memory`
3. `feat(discovery): CascadePlanner + EvictionStateMachine`
4. `feat(discovery): EvictionScheduler.run_cycle + reconcile_blacklist`
5. `feat(config): EVICTION_ENABLED/SCORE_MARGIN/HYSTERESIS_CYCLES/MAX_SELL_ONLY_WALLETS env vars + validator cross-field`
6. `test(discovery): eviction state_machine table-driven ≥15 scenarios`
7. `test(discovery): cascade_planner + hysteresis_tracker units`

**Critères d'acceptation B** :
- EvictionScheduler testé unitairement, pas encore wired dans DiscoveryOrchestrator.
- `EVICTION_ENABLED=false` (default) → aucun code-path modifié côté orchestrator.
- Couverture ≥90% sur `eviction/`.

### Phase C — Intégration DiscoveryOrchestrator + DecisionEngine + strategy pipeline

**Commits** :
1. `refactor(discovery): DecisionEngine — remove paused branch (post-migration 0007)`
2. `feat(discovery): DecisionEngine — add sell_only keep branch`
3. `feat(discovery): DiscoveryOrchestrator — hook EvictionScheduler.run_cycle + reconcile_blacklist`
4. `feat(storage): TargetTraderRepository.list_active includes sell_only (watcher continues to poll SELL)`
5. `feat(strategy): TraderLifecycleFilter blocks BUY for sell_only wallets`
6. `test(discovery): DiscoveryOrchestrator integration — full cycle with eviction triggered`
7. `test(strategy): TraderLifecycleFilter BUY/SELL/disabled variants`

**Critères d'acceptation C** :
- Avec `EVICTION_ENABLED=true` + seed 7 actives + 2 shadows high-score, run 3 cycles → 1 T3 + cascade T5 observées, events persistés, alerts dans la queue.
- Avec `EVICTION_ENABLED=false`, même seed → zéro transition eviction (non-régression M5).
- Watcher continue à poller un `sell_only` (vérifié via log `watcher_started_for_wallet` avec status=sell_only).
- Strategy pipeline rejette un BUY d'un `sell_only` avec `reason="wallet_sell_only"`.

### Phase D — Alertes Telegram + Dashboard UX + docs

**Commits** :
1. `feat(monitoring): 5 nouveaux templates Jinja (eviction + blacklisted)`
2. `feat(monitoring): EvictionScheduler push alerts via asyncio.Queue[Alert]`
3. `feat(dashboard): /traders ajout filter-chips sell_only + blacklisted`
4. `feat(dashboard): TraderRow + templates — colonnes eviction_state + previously_demoted`
5. `feat(dashboard): badge() macro étendue avec couleurs Radix sell_only/blacklisted`
6. `test(monitoring): 5 templates compilent + rendu contient badge machine_id + mode_badge + dashboard_url`
7. `test(dashboard): /traders?status=sell_only filter + UI rendering`
8. `docs(claude): CLAUDE.md paragraph M5_bis discovery + sécurité`
9. `docs(specs): ROADMAP.md update + M5_bis marked shipped`

**Critères d'acceptation D** :
- Smoke test CLI réel en dry-run avec `EVICTION_ENABLED=true` + 2 wallets seed + fixture discovery data → 1 alerte Telegram `trader_eviction_started` formatée correctement (incluant badge machine + dashboard URL cliquable).
- `test_dashboard_security.py` + `test_dashboard_security_m6.py` verts (pas de régression security).
- Couverture globale `discovery/` + `strategy/` ≥ baseline M14.
- Docs CLAUDE.md mentionne `EVICTION_ENABLED=false` dans les invariants sécurité.

---

## 14. Risques & mitigations

| Risque | Probabilité | Impact | Mitigation |
|---|---|---|---|
| Cascade pathologique (scores ultra-volatils oscillant au-dessus/sous margin) | Faible | Moyen (bruit events, CPU cycle) | `MAX_SELL_ONLY_WALLETS` cap dur + `EVICTION_HYSTERESIS_CYCLES=3` amortit oscillations. Un wallet qui oscille est aussi rejeté par facteur `consistency` M12 scoring v2 |
| `sell_only` avec position qui ne ferme jamais (wallet source inactif) | Moyen | Bas (position dormante en portefeuille) | T8 ne déclenche jamais → wallet reste en `sell_only` indéfiniment. Acceptable (pas de blocage fonctionnel). Dashboard affiche durée entered_at → user peut manuellement blacklister si 30+ jours |
| État in-memory `HysteresisTracker` perdu au restart | Moyen | Bas (eviction retardée de 3 cycles max) | Documenté §7.7. Persistance DB rejetée pour éviter surcoût query/cycle. Un restart reset est quasi-invisible user (cycle = 6h) |
| Conflit `TARGET_WALLETS` ∩ `BLACKLISTED_WALLETS` | Faible | Critique (status incohérent) | Validator Pydantic cross-field crash boot clair (§5.3) |
| Migration 0007 échoue sur DB live avec custom status non-standard | Très faible | Critique (DB corrompue) | `ALTER TABLE` batch SQLite = copy-table idempotent. `polycopy.db.bak` convention user déjà en place. Tests staging obligatoires §6.3 |
| `TraderLifecycleFilter` latence ajoutée (query DB par trade) | Faible | Bas (~1ms) | Query indexée sur `wallet_address` (index existant). Instrumentation latence M11 détectera toute dérive — le stage `strategy_filtered_ms` inclut ce filtre |
| Watcher poll `sell_only` mais wallet source change de strat et n'émet plus de SELL | Moyen | Bas (position dormante — identique cas précédent) | Pas une régression M5_bis, c'était déjà le cas pour les actives. Kill switch M4 reste vigilant sur le drawdown global |
| Oubli de support `sell_only` dans une nouvelle requête future (régression silencieuse) | Moyen | Moyen | Enum étendu Literal dans repositories + Pydantic DTOs → mypy --strict rattrape la majorité. Test non-régression §8.4 automatise |
| Fuite d'un event `sell_only` dans un log partagé (wallet public mais détail strat) | Très faible | Bas | `wallet_address` est déjà public on-chain (CLAUDE.md M5 sécurité). Identique à M5 |

---

## 15. Open questions & futures extensions

- **Hystérésis asymétrique** : actuellement `EVICTION_HYSTERESIS_CYCLES` s'applique aux 3 directions (entrée T3, abort T6, rebond T7). Cas d'usage : user pourrait vouloir `eviction_hysteresis_entry=3`, `eviction_hysteresis_abort=1` (abort plus rapide pour minimiser durée sell_only). Décision v1 : valeur unique, simplicité. À revoir si user trouve le comportement trop conservateur.

- **Bouton dashboard « force demote/promote »** : intentionnellement hors scope — un write-endpoint casserait l'invariant M4.5/M6 `GET`-only. Alternative future : commande POST via remote_control M12_bis (`POST /v1/eviction/<machine>/force-demote/<wallet>` + TOTP). Pas nécessaire tant que `BLACKLISTED_WALLETS` live reload suffit comme escape hatch.

- **Metrics Prometheus/structlog eviction rate** : `eviction_triggered_total`, `eviction_aborted_total`, `eviction_completed_shadow_total`, `sell_only_duration_seconds_histogram`. Utile pour observer le taux de churn et détecter une cascade pathologique. Prévu M14+ (si on intègre Prometheus).

- **Auto-flip `EVICTION_ENABLED=true`** : sur décision humaine après analyse des events `trader_eviction_*` pendant une période d'observation en dry-run. Aucun auto-flip prévu — même discipline que `SCORING_VERSION=v1→v2` M12.

- **Pondération dynamique de la fenêtre de score pour eviction** : le score utilisé pour comparer delta(candidat, worst) est le score *courant* (typiquement 90d). Extension possible : utiliser un score pondéré (ex. 70% 30d + 30% 90d) pour favoriser les wallets en bonne forme récente. Décision v1 : utiliser le score courant tel quel (cohérent avec le reste de M5/M12).

- **Auto-blacklist sur cluster Sybil/wash** : si un scheduler détecte un cluster de wallets corrélés (M12 `WASH_CLUSTER_WALLETS`), proposer auto-blacklist via M5_bis. Décision v1 : non — l'user garde le contrôle final, et `WASH_CLUSTER_WALLETS` agit au niveau gate scoring pas status terminal.

- **N swaps par cycle au lieu de 1** : optimisation potentielle si le pool a beaucoup d'inertie (sous-utilisation evidente). Risque : cascade en chaîne + bruit alertes. Décision v1 : 1/cycle, simplicité + prédictibilité.

- **Persister `HysteresisTracker` en DB** : pour survivre aux restarts. Rejeté v1 pour éviter un nouveau schéma + query/cycle. Réévaluer si l'user remarque des delays post-restart problématiques.

- **Interaction avec M12 scoring v2 dual-compute** : pendant la shadow period `SCORING_V2_SHADOW_DAYS`, quel score pilote l'eviction ? **Décision v1** : le score qui pilote `DecisionEngine` (donc v1 par défaut tant que `SCORING_VERSION=v1`). L'eviction suit la même formule que les promotions/demotions M5 — cohérence. Quand cutover v2, l'eviction bascule automatiquement sur v2.

---

## 16. Prompt d'implémentation

Prompt ready-to-paste pour lancer l'implémentation phase par phase :

```
Tu es l'implémenteur de la milestone M5_bis (polycopy). La spec fait autorité :

- Lecture obligatoire :
  - docs/specs/M5_bis_competitive_eviction_spec.md (cette spec)
  - docs/specs/M5-trader-scoring.md (lifecycle M5 qu'on étend)
  - CLAUDE.md (conventions + sécurité, lire section Discovery M5)

- Workflow strict :
  1. Tu implémentes phase par phase (A → D, cf. spec §13). Une phase = une
     branche `feat/m5bis-phase-<lettre>` + une PR squashée vers main.
  2. Avant chaque phase, initialise un TodoWrite avec les commits attendus
     listés dans §13. Marque chaque todo completed dès que le commit est fait
     (pas de batch).
  3. Après chaque phase, lance :
     - `ruff check . && ruff format .`
     - `mypy --strict src`
     - `pytest`
     Tout doit être vert. Si rouge, corriger avant de rendre la main.
  4. Ne passe PAS à la phase suivante sans validation explicite. Dis
     "Phase A done, ready for review, go phase B ?" et attend.

- Règles code :
  - Python 3.11+ strict type hints, Pydantic v2, async partout, mypy --strict.
  - Pas de commentaires superflus (CLAUDE.md).
  - Cite `path:line` dans les messages de commit quand tu touches du code existant.
  - Conventional commits (feat/fix/refactor/test/docs/chore).
  - Écrire les tests EN MÊME TEMPS que le code, pas après.

- Sécurité (non-négociable) :
  - `EVICTION_ENABLED=false` default = zéro diff observable (non-régression M5).
  - Aucun force-close de position, jamais. Les positions sell_only ferment via
    SELL copié naturellement ou résolution marché M8.
  - Reconciliation BLACKLISTED_WALLETS idempotente (re-run safe).
  - Cap MAX_SELL_ONLY_WALLETS dur (évite cascade pathologique).
  - Pas de secret loggé dans les 5 nouveaux templates — test grep automatisé.

- Décisions déjà tranchées (ne pas re-débattre) :
  - 1 swap max par cycle Discovery (séquentiel strict).
  - Hystérésis unique (3 cycles, même valeur pour T3/T6/T7).
  - HysteresisTracker in-memory (reset au restart, acceptable).
  - Priorité T6 (abort) > T8 (complete_to_shadow) en EC-1.
  - Pinned exclus du worst_active (EC-7).
  - EvictionScheduler hook en aval de DecisionEngine, pas intégré dedans.
  - TraderLifecycleFilter en tête de pipeline (fast path si flag off).

- Démarrage :
  Lis les 3 fichiers obligatoires, résume en 5 lignes ce que tu vas faire en
  Phase A (migration + modèle), crée la branche feat/m5bis-phase-a, initialise
  le TodoWrite avec les 4 commits de §13 Phase A, et attaque le 1er commit.
  Rends-moi la main après Phase A pour review.
```

---

_Fin de la spec M5_bis. Prochaine étape : me lancer via le prompt §16._
