# Prompt pour générer la spec M12 — Discovery auto-lockout sur backtest fail

> Copie le bloc ci-dessous tel quel dans une nouvelle session Claude Code.
> Génère `specs/M12-discovery-auto-lockout.md` au format détaillé de M3-M8.

---

```
Tu vas écrire la spec d'implémentation `specs/M12-discovery-auto-lockout.md`
pour le projet polycopy (bot de copy-trading Polymarket en Python 3.11 /
asyncio).

Avant tout, lis :
- `CLAUDE.md` (conventions code + sécurité)
- `docs/architecture.md`
- `specs/M5-trader-scoring.md` (M5 discovery + garde-fous existants —
  MAX_ACTIVE_TRADERS, BLACKLISTED_WALLETS, hystérésis demote)
- `specs/M11-backtest-scheduler.md` (DOIT être implémenté AVANT M12 — M12
  consomme la table `backtest_runs` produite par M11)
- `specs/M8-dry-run-realistic.md` (format de référence + pattern garde-fou
  4ᵉ niveau defense in depth)
- `src/polycopy/discovery/orchestrator.py` (DiscoveryOrchestrator à
  étendre)
- `src/polycopy/discovery/decision_engine.py` (logique de promotion
  existante)
- `src/polycopy/storage/repositories.py` (BacktestRunRepository créé par
  M11)

## Pré-requis dur : M11 doit être déjà mergé

M12 lit la table `backtest_runs` créée par M11. Si M11 n'est pas mergé,
M12 n'a aucune donnée à consommer et la spec est inapplicable. Le tout
premier paragraphe §0 de la spec doit le rappeler.

## Contexte du problème (à formaliser)

M11 produit un signal continu sur la qualité de la formule de scoring v1 :
chaque jour, un row dans `backtest_runs` avec `passed_threshold` ∈ {True,
False}. Mais ce signal n'a aucun effet opérationnel — discovery continue
de promouvoir des wallets même si la formule est cassée (Spearman < 0.30).

Conséquence : si la formule v1 dérive (régime de marché change, smart
money se déplace), discovery promeut des "faux positifs" pendant des
semaines avant qu'un humain le remarque. Capital virtuel gaspillé en
dry-run M8, capital réel à risque en live.

M12 répond : un mécanisme de lockout automatique de discovery basé sur
les 3 derniers `backtest_runs`, avec hystérésis (3 fail consécutifs avant
lockout, comme M5 `SCORING_DEMOTION_HYSTERESIS_CYCLES`), réactivation
strictement manuelle (option B documentée dans nos discussions).

## Décisions tranchées en amont (à reproduire fidèlement)

1. **Hystérésis = 3 runs failed consécutifs** avant lockout. Cohérent
   avec `SCORING_DEMOTION_HYSTERESIS_CYCLES=3` (M5). Non négociable —
   évite le yo-yo si Spearman oscille autour du seuil.
2. **Réactivation manuelle uniquement** (option B). Trois chemins de
   sortie de lockout :
   a. **Bump de `SCORING_VERSION`** (env) → considéré comme nouvelle
      formule, le compteur d'hystérésis reset à 0 et discovery
      re-démarre dès le prochain cycle. Implémentation : M12 mémorise la
      `scoring_version` au moment du lockout ; si la version courante
      diffère → unlock auto.
   b. **`DISCOVERY_FORCE_ENABLED=true`** (env) → override manuel
      temporaire qui bypass le lockout. Log WARNING au boot
      `discovery_force_enabled_bypassing_lockout`. À utiliser en
      conscience (ex: humain a ajusté le seed, veut re-tester).
   c. **3 runs `passed_threshold=True` consécutifs** → unlock auto.
      Possible uniquement si le seed a été enrichi (humain) ou si une
      régression de scoring a été corrigée. Improbable en pratique, mais
      documenté comme garde-fou final.
3. **Aucune auto-réactivation périodique** (= AUCUN re-test sur la
   formule failed pour voir si elle re-passe). M11 continue de tourner
   et de loguer/alerter, mais M12 n'évalue le statut qu'à chaque cycle
   discovery (pas un timer indépendant).
4. **Pinned (`TARGET_WALLETS` env) PAS impactés par le lockout** :
   - Le watcher continue de poller les pinned (M5 invariant).
   - Strategy + executor continuent de traiter leurs trades.
   - Le lockout bloque UNIQUEMENT le cycle de scoring/promotion M5.
   - Donc même en lockout, polycopy reste opérationnel sur la whitelist
     curated par l'utilisateur.
5. **Alerte CRITICAL au lockout** (Telegram), INFO à l'unlock. Cohérent
   avec discipline M4 (les events critiques sont rares et explicites).

## Objectif M12 (livrable)

1. **Évaluation du lockout dans `DiscoveryOrchestrator.run_forever`** :
   - Au début de chaque cycle, lire les 3 derniers `backtest_runs` via
     `BacktestRunRepository.list_recent(limit=3,
     scoring_version=settings.scoring_version)`.
   - Compter ceux avec `passed_threshold=False`.
   - Si 3/3 → state `LOCKED_OUT` → skip le cycle entier (pas de bootstrap,
     pas de scoring, pas de décisions, log structlog
     `discovery_locked_out_by_backtest`).
2. **Gestion du state** :
   - Pas de table dédiée. Le state est dérivé à chaque cycle depuis
     `backtest_runs`.
   - Si à un cycle on passe LOCKED_OUT → UNLOCKED (3 last passed,
     `SCORING_VERSION` bump, ou `DISCOVERY_FORCE_ENABLED=true`), log
     INFO `discovery_unlocked_after_backtest` + alerte INFO Telegram.
3. **Gestion `SCORING_VERSION` bump** :
   - À chaque cycle, comparer `settings.scoring_version` (env courant)
     à la `scoring_version` du dernier `backtest_runs`. Si différent :
     - Considérer qu'on attend de nouveaux runs sur la nouvelle version.
     - `LOCKED_OUT` UNIQUEMENT s'il y a 3 runs failed sur la **version
       courante**. Si moins de 3 runs sur la version courante → UNLOCKED
       (assumer la nouvelle version "innocente jusqu'à preuve du
       contraire").
4. **Gestion `DISCOVERY_FORCE_ENABLED=true`** :
   - Nouveau env var `DISCOVERY_FORCE_ENABLED=false` par défaut.
   - Si `true` → bypass total du lockout (log WARNING au boot
     `discovery_force_enabled_bypassing_lockout`, repush warning toutes
     les 24h via cooldown).
5. **Alertes Telegram** :
   - Transition UNLOCKED → LOCKED_OUT : alerte CRITICAL
     `discovery_locked_out_by_backtest` (body : 3 derniers Spearman,
     scoring_version, action humaine requise).
   - Transition LOCKED_OUT → UNLOCKED : alerte INFO
     `discovery_unlocked_after_backtest` (body : raison de l'unlock —
     bump version, force_enabled, 3 passed).
   - Cooldown 24h pour ne pas repush la même alerte chaque cycle si
     l'utilisateur ne répond pas.
6. **Audit trail** :
   - Chaque cycle qui skip → log structlog avec
     `cycle_skipped_reason='backtest_lockout'`, `last_3_spearman=[…]`,
     `scoring_version=…`.
   - Pas de nouvelle table — `trader_events` (M5) n'est pas adapté car
     ce n'est pas un événement par-wallet. Log structlog suffit.
7. **Garde-fou défense en profondeur (4ᵉ garde-fou cohérent avec le
   pattern M3/M8)** :
   - Dans `DiscoveryOrchestrator.run_forever`, juste avant
     l'écriture de toute promotion en DB :
     `assert not self._is_locked_out(), "discovery lockout breached"`.
     Defense in depth contre un bug de refactor qui aurait sauté
     l'évaluation initiale.
   - Test breakglass associé.

## Contraintes non négociables

- **Diff additif sur M5** : zéro ligne modifiée dans `scoring.py`,
  `decision_engine.py`. M12 ajoute une couche d'évaluation préalable
  dans `DiscoveryOrchestrator.run_forever` uniquement.
- **Pas d'effet sur les pinned** : tests dédiés vérifient que watcher
  continue de poller TARGET_WALLETS même en lockout, et que les pinned
  ne disparaissent jamais de `target_traders.status='pinned'`.
- **Pas d'effet sur M8** : M8 dry-run continue de simuler les fills sur
  les wallets actifs existants. Le lockout bloque les futures
  promotions, pas les positions virtuelles déjà ouvertes.
- **Reset hystérésis sur SCORING_VERSION bump** : crucial pour permettre
  le test d'une nouvelle formule. Documenter dans §14.
- **Pas d'auto-réactivation périodique** : si l'utilisateur ne fait
  rien (ni bump, ni force_enabled, ni mise à jour seed), discovery
  reste off indéfiniment. C'est l'intention — protection capital.
- **Aucun secret loggé** : les env override loggés ne révèlent jamais
  les creds Polymarket / Telegram / Goldsky.
- **Conventions CLAUDE.md** : async, type hints stricts, structlog,
  docstrings FR, code/identifiants EN.
- **mypy --strict propre, ruff propre, pytest ≥ 80% coverage** sur
  `src/polycopy/discovery/` (M5 + M11 + M12 cumulé).

## Cas de test obligatoires (à lister §9)

1. **3 last passed → no lockout** : cycle normal s'exécute,
   `discovery_cycle_started` loggé.
2. **3 last failed (même version) → lockout** : cycle skipped, log
   `discovery_locked_out_by_backtest`, alerte CRITICAL pushed.
3. **2 failed + 1 passed → no lockout** (l'hystérésis exige 3
   consécutifs).
4. **3 failed mais SCORING_VERSION change** : cycle s'exécute (nouvelle
   version, on attend ses propres runs).
5. **3 failed sur v1 + 1 nouveau run failed sur v2** : pas de lockout
   (1 run sur v2 < 3, on assume innocence).
6. **3 failed v1 + 3 failed v2** : lockout sur v2.
7. **DISCOVERY_FORCE_ENABLED=true** : bypass lockout, log WARNING au
   boot, cycle s'exécute. Alerte WARNING pushed avec cooldown 24h.
8. **Pinned non impactés** : 3 failed → lockout discovery, mais
   `target_traders.status='pinned'` inchangés, watcher continue de
   poller.
9. **Promotion existing wallets non impactée** : un wallet `active`
   M5 reste `active` en lockout (le lockout bloque le cycle, pas les
   états DB).
10. **Alerte cooldown 24h** : 3 cycles successifs en lockout → 1 seule
    alerte CRITICAL (cooldown M4 préservé).
11. **Transition LOCKED → UNLOCKED via 3 passed** : alerte INFO pushed,
    cycle suivant s'exécute normalement.
12. **Transition LOCKED → UNLOCKED via SCORING_VERSION bump** : alerte
    INFO pushed avec reason='scoring_version_changed'.
13. **4ᵉ garde-fou (assert)** : appel direct du code de promotion en
    state lockout → AssertionError (test breakglass).
14. **DB sans backtest_runs (cas où M11 n'a pas encore tourné)** : M12
    considère UNLOCKED par défaut (pas de lockout sans evidence). Log
    debug `discovery_lockout_no_backtest_data`.
15. **Aucun secret dans les logs M12** (grep automatisé).

## Mises à jour de doc

- `README.md` : section M12 "Discovery auto-lockout" + 1 env var
  (`DISCOVERY_FORCE_ENABLED`).
- `docs/architecture.md` : "Module : Discovery" gagne § "Status M12"
  décrivant l'évaluation lockout + chemins de réactivation.
- `CLAUDE.md` : section sécurité gagne 1 bullet sur l'invariant pinned
  préservé en lockout + l'absence d'auto-réactivation.
- `docs/setup.md` : §20 "Sortie de lockout discovery" — guide pratique
  des 3 chemins (bump version, force_enabled temporaire, attendre 3
  passed).
- `.env.example` : `DISCOVERY_FORCE_ENABLED=false` avec commentaire
  explicite.

## Format de la spec

Suis le format exhaustif de `specs/M8-dry-run-realistic.md` :
- §0 Pré-requis (insister sur dépendance dure à M11 mergé)
- §1 Objectif scope exact
- §2 Arbitrages techniques :
  - 2.1 Hystérésis 3 vs 5 vs 1 (3 cohérent M5, justifié)
  - 2.2 Réactivation auto vs manuelle (manuelle = option B retenue,
    auto rejetée pour éviter yo-yo)
  - 2.3 Lockout = skip cycle vs disable complet de
    DiscoveryOrchestrator (skip cycle plus chirurgical)
  - 2.4 Reset hystérésis sur version bump (oui)
  - 2.5 Force_enabled override (oui, avec WARNING repush)
  - 2.6 Pas d'effet sur pinned ni sur états existants (volontaire)
  - 2.7 Alerte CRITICAL au lockout vs WARNING (CRITICAL = action
    humaine requise)
- §3 Arborescence
- §4 APIs (aucune)
- §5 Storage (aucune migration — lit `backtest_runs` créée par M11)
- §6 DTOs (LockoutState dataclass interne)
- §7 Évaluation du lockout dans DiscoveryOrchestrator
- §8 Algorithme transitions UNLOCKED ↔ LOCKED_OUT
- §9 Tests détaillés
- §10 Doc updates
- §11 Commandes de vérification finale
- §12 Critères d'acceptation
- §13 Hors scope M12 (auto-réactivation périodique, auto-rotation
  formules, exposition `/lockout-history` dashboard = reportable M12.1,
  notification daily summary du statut lockout = reportable M12.1)
- §14 Notes d'implémentation + zones d'incertitude :
  - 14.5 doit lister explicitement : "réactivation = humaine" + "v2 =
    code humain commité" + "pinned préservés" + "pas d'effet sur les
    positions M8 déjà ouvertes"
- §15 Prompt à m'envoyer pour lancer l'implémentation

Cible 900-1200 lignes (M12 plus simple que M11 car pas de scheduler ni
nouvelle table).

À la fin, propose un commit message :
`feat(discovery): M12 auto-lockout based on backtest hysteresis`
```
