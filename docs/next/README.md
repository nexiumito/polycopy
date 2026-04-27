# docs/next/ — Modules d'implémentation polycopy

**Source de vérité** pour la roadmap d'implémentation active (2026-04-24 → ~2026-06).

Refactor consolidé des brouillons éparpillés (`docs/bug/` sessions A-E, `docs/audit/`,
`docs/deepsearch/` synthèse triangulée) en **10 modules d'implémentation cohérents**
`MA` → `MJ`, chacun de charge équilibrée (~3-5 jours) et d'ordre d'exécution clair.

## Vue d'ensemble

| # | Module | Titre | Priorité | Charge | Prérequis | Bloque |
|---|---|---|---|---|---|---|
| 1 | [MA](MA.md) | Scoring v2.1-ROBUST (foundation) | 🔥 P1 | M (3-4j) | aucun | MB, MF, MG |
| 2 | [MB](MB.md) | Anti-toxic lifecycle + internal PnL | 🔥 P1 | M (4-5j) | MA shipped | MF |
| 3 | [MC](MC.md) | Fees dynamic + EV adjustment | 🔥 P1 | M (2-3j) | aucun | — |
| 4 | [MD](MD.md) | Cross-layer integrity patches | 🟠 P2 | M (3-4j) | aucun | (bloque passage live) |
| 5 | [MK](MK.md) | Pipeline latency phase 1b (WSS + counters) | 🟠 P2 | M (3-4j) | aucun | MF (partiel) |
| 6 | [MF](MF.md) | Wash detection + Mitts-Ofir (v2.2 capstone) | 🟠 P2 | L (6-8j) | MA + MB + 30j data | — |
| 7 | [MG](MG.md) | Additional scoring factors (CLV + Kelly + λ) | 🟠 P2 | M (3-4j) | MA shipped | MF (optionnel) |
| 8 | [MH](MH.md) | Dashboard UX polish + consistency | 🟡 P3 | M (2-3j) | aucun | — |
| 9 | [MI](MI.md) | Ops hygiene + Goldsky integration | 🟡 P3 | M (2-3j) | aucun | — |
| 10 | [MJ](MJ.md) | (opt) MEV Private Mempool instrumentation | 🟢 P4 | S-M (1-3j) | aucun | — |

**Légende priorité** :
- 🔥 **P1** : débloque le test business (scoring stable + anti-toxic + fees) — ship d'abord
- 🟠 **P2** : capstones ou blockers pré-live — ship après P1
- 🟡 **P3** : infra / UX / ops — slack time, parallélisable
- 🟢 **P4** : optionnel, hypothèses à valider avant engagement

## Plan d'exécution recommandé (8 semaines)

### Semaine 1-2 — Fondation

**Parallélisable, ship en priorité**  :
- **MA** Scoring v2.1-ROBUST (3-4j) — stabilise le scoring, débloque MB
- **MC** Fees dynamic + EV (2-3j) — critique post-March 2026, indépendant de MA

**Optionnel si bandwidth** : démarrer MD en parallèle (intégrité audit)

### Semaine 3-4 — Lifecycle + Latence

**Parallélisable** :
- **MB** Anti-toxic lifecycle + internal PnL (4-5j) — nécessite MA shippé (scoring stable). Démarre la collecte de 30j d'internal_pnl_data.
- **MK** Pipeline latency phase 1b (3-4j) — ship WSS market channel pour détection. Indépendant.

**Slack time** : **MH** Dashboard UX polish (2-3j) peut s'insérer

### Semaine 5-6 — Capstones

**MG** CLV + Kelly + Kyle's λ (3-4j) — indépendant, peut ship seul ou en prérequis de MF v2.2.

**MD** peut shipper ici si reporté (audit integrity patches avant MF).

**Slack time** : **MI** Ops hygiene + Goldsky free tier (2-3j).

### Semaine 7-8 — v2.2-DISCRIMINATING ship

**MF** Wash detection + Mitts-Ofir (6-8j) — **prérequis strict** : MA + MB shippés + 30j d'internal_pnl collectée. Capstone scoring v2.2.

### Post-ship / optionnel

**MJ** MEV Private Mempool instrumentation (1-3j conditionnel) — ship si I1 mesure révèle un impact MEV significatif sur notre stack.

## Parallélisation

**Groupe A (semaine 1-2, indépendants)** : MA, MC, MD, MH, MI, MJ

**Groupe B (semaine 3-4, dépend de MA)** : MB, MG

**Groupe C (semaine 3-4, indépendant de MA)** : MK

**Groupe D (semaine 7-8, dépend MA + MB + 30j)** : MF

## Charge totale estimée

- **Base (P1)** : MA + MB + MC = **9-12 jours** (ship 2 semaines avec parallélisation)
- **Blockers pré-live (P2)** : MD + MK + MF + MG = **14-19 jours** (ship 4-5 semaines)
- **Slack (P3)** : MH + MI = **4-6 jours**
- **Optionnel (P4)** : MJ = **1-3 jours**

**Total roadmap** : ~28-40 jours dev single-person, ~8 semaines avec parallélisation modérée.

## Traçabilité vers sources

Chaque module cite explicitement :

- **Audit** : [docs/audit/2026-04-24-polycopy-code-audit.md](../audit/2026-04-24-polycopy-code-audit.md) — 70 findings (5 CRITICAL, 16 HIGH, 28 MEDIUM, 27 LOW, 9 INFO)
- **Synthèse deep-search** : [docs/deepsearch/SYNTHESIS-2026-04-24-polycopy-scoring-discovery-latency.md](../deepsearch/SYNTHESIS-2026-04-24-polycopy-scoring-discovery-latency.md) — findings F01-F70, roadmap 40 items, hypothèses H-EMP-1 à 15, questions ouvertes Q1-Q10
- **Rapports deep-search** : `docs/deepsearch/{perplexity,gemini,claude}_*.md` — ~150 citations externes dédupliquées
- **Sessions originales** : `docs/bug/session_[A-E]_*.md` — **dépréciées** (historique uniquement)

## Dépréciation

**`docs/bug/` est désormais historique** (brouillons). La source de vérité
d'implémentation est **ce dossier `docs/next/`**. Les fichiers `docs/bug/session_*.md`
restent pour traçabilité historique mais **ne doivent plus être mis à jour**.

Mapping session → modules pour référence rapide :
- Session A (anti-toxic) → absorbée dans **MA** (scoring fix) + **MB** (lifecycle + internal PnL)
- Session B (scoring v2 reliability) → absorbée principalement dans **MA**, partiellement dans **MF**
- Session C (dashboard UX) → absorbée dans **MH**
- Session D (pipeline metrics + ops) → scindée entre **MK** (latence + counters, ex-`ME` renommé pour éviter collision avec M18 V2 migration) et **MI** (ops hygiene)
- Session E (cross-layer integrity) → absorbée dans **MD**

## Workflow de démarrage d'un module

Chaque module `MX.md` contient à sa **§10** un **prompt de génération de spec** à coller
dans une nouvelle conversation Claude Code pour produire le fichier
`docs/specs/M<n>-<titre>.md` suivant le format des specs M1..M13 existantes.

**Séquence type d'implémentation d'un module** :
1. Ouvrir `docs/next/MX.md` (brief complet)
2. Copier la §10 prompt dans une nouvelle session Claude Code → génère `docs/specs/M<n>-<titre>.md`
3. Relire le spec, ajuster si besoin, valider
4. Nouvelle session Claude Code avec `/implement-module docs/specs/M<n>-<titre>.md` → implémentation
5. Tests + review + commits + merge

## Hypothèses empiriques à valider

Certains modules (notamment MA, MB, MF) dépendent d'hypothèses empiriques
H-EMP-1 à H-EMP-15 documentées dans la synthèse §8. **À valider sur nos data
SQL avant ship** pour les modules qui en dépendent. Chaque MX §6 liste les
hypothèses pertinentes.

## Questions ouvertes

10 questions (Q1-Q10, synthèse §11) que les deep-searches n'ont pas pu trancher.
À résoudre empiriquement via instrumentation :
- Q1 : "250ms taker delay" réel ou network+matching ? → instrumenter post-MK
- Q3 : corrélation Brier/PnL négative Convexly tient-elle sur nos wallets ? → valider post-MB
- Q4 : MKV réel sur nos tailles ? → MJ instrumentation
- Q5 : impact fees sur notre EV ? → mesurer post-MC
- ...

Chaque module MX liste les questions Q qui le concernent.
