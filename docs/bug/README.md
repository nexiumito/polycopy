# Bug registry — polycopy

Registre des bugs, incohérences UX et dettes de conception identifiés lors de l'audit
dashboard du **2026-04-24** (après 5 j de run dry-run sur uni-debian, reset positions/capital
post-fix Bug 5 PositionSizer side-aware).

Les bugs sont regroupés en **5 sessions de travail** de charge comparable, conçues
pour être claims indépendamment par Claude Code (une session = une branche = une PR).

Sessions A/B/C/D issues de l'audit dashboard 2026-04-24. Session E ajoutée suite
à l'audit code complet du [2026-04-24](../audit/2026-04-24-polycopy-code-audit.md)
qui a révélé 5 CRITICALs cross-couche non couverts par A/B/C/D.

## Vue d'ensemble

| Session | Titre | Priorité | Charge | Prérequis |
|---|---|---|---|---|
| [A](session_A_anti_toxic_trader_lifecycle.md) | Anti-toxic trader lifecycle | 🔥 1 | L (2-3 j) | — |
| [B](session_B_scoring_v2_reliability.md) | Scoring v2 reliability | 🔥 1 | L (2-3 j) | — (parallélisable avec A) |
| [E](session_E_cross_layer_integrity_and_hardening.md) | Cross-layer integrity + hardening | 🔥 1 | M (1-2 j) | — (issue de l'audit code) |
| [C](session_C_dashboard_ux_and_consistency.md) | Dashboard UX & consistency | 🟡 3 | M (1-2 j) | — (parallélisable) |
| [D](session_D_pipeline_metrics_and_ops.md) | Pipeline metrics + ops hygiene | 🟡 4 | M (1-2 j) | — (parallélisable) |

**Ordre recommandé** :
- **A, B, E en priorité haute** — stoppent des fuites capital ou débloquent le
  cutover v2 (critère business principal)
- **C, D** en slack time ou en parallèle

**Mises à jour suite à l'audit code 2026-04-24** :
- Session B gagne 3 nouvelles causes racines (timing_alpha +0.10 gratuit,
  Sortino sentinel zombies, Brier ambigu)
- Session A confirmée + 2 items complémentaires
- Session E créée (5 CRITICALs cross-couche)
- Session F optionnelle si deep-search confirme les hypothèses scoring v2

## Criteria d'une session

Chaque session suit la même structure :
- **Objectif business** (quoi ça débloque)
- **Items** (bugs précis à résoudre, avec pointeurs code)
- **Hypothèses à valider** (pour les bugs dont la cause racine n'est pas tranchée)
- **Livrables** (fix + tests + docs + éventuelles specs dédiées)
- **Out of scope** (pour éviter le scope creep)

## Source des bugs listés

- Audit visuel dashboard 2026-04-24 (sessions A/B/C/D — cf. contexte fichiers)
- Audit code complet 2026-04-24 — [docs/audit/2026-04-24-polycopy-code-audit.md](../audit/2026-04-24-polycopy-code-audit.md) (70 findings dédupliqués, session E)
- [docs/backlog.md](../backlog.md) (idées parquées reprises là où pertinent)
- Queries SQL sur `trader_scores` ayant révélé la variance anormale v2
- Observations runtime uni-debian (PnL négatif -$0.55 sur 5 j, trader toxique intouchable)
