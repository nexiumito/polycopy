# Roadmap des specs polycopy

Mise à jour : 2026-04-18 (suite à brainstorming + deep-searches Gemini/Perplexity).

Référence de conception : [`docs/development/M10_synthesis_reference.md`](../docs/development/M10_synthesis_reference.md).

## Jalons implémentés (M1 → M9)

| Spec | Titre | Statut | Commit |
|---|---|---|---|
| M1 | Watcher + Storage | ✅ shipped | `8a5b47d` |
| M2 | Strategy Engine | ✅ shipped | `236e10d` |
| M3 | Executor (dry-run safe + garde-fous) | ✅ shipped | `2da4732` |
| M4 | Monitoring (Telegram + PnL + kill switch) | ✅ shipped | `4dddedc` |
| M4.5 | Dashboard read-only (FastAPI + HTMX) | ✅ shipped | — |
| M5 | Discovery + Scoring v1 | ✅ shipped | — |
| M6 | Dashboard 2026 (Tailwind + Radix + Lucide) | ✅ shipped | `14a9ed9` |
| M7 | Telegram enhanced (Jinja2 + scheduler) | ✅ shipped | `11093b1` |
| M8 | Dry-run realistic fill (FOK orderbook) | ✅ shipped | `e6ce6ac` |
| M9 | Silent CLI + /logs tab + README | ✅ shipped | `026f61c` |
| M5_bis | Competitive eviction (shadow/active/sell_only compétitif) | ✅ shipped | `feat/m5bis` |

## Jalons à venir (M10 → M16)

Issue de la synthèse [`M10_synthesis_reference.md`](../docs/development/M10_synthesis_reference.md). L'ordre est optimisé pour que chaque milestone débloque ou prépare le terrain du suivant.

| # | Spec | Priorité | Effort | Dépend de | Contenu principal |
|---|---|---|---|---|---|
| **M10** | Parity + Log hygiene + 3 modes | 🔥 1 | S (1 sem.) | — | `EXECUTION_MODE` enum (SIMULATION/DRY_RUN/LIVE), kill switch parité, alertes Telegram identiques avec badges mode, filtrage middleware via `structlog.DropEvent`, exclusion `dashboard_request` par défaut dans `/logs`. |
| **M11** | Real-time pipeline phase 1 | 🔥 2 | M (2 sem.) | M10 | WebSocket CLOB channel `market` pour `SlippageChecker`, cache Gamma adaptatif (TTL par market state), instrumentation latence `trade_id` + nouvel onglet `/latency`. Cible : 10-15 s → 2-3 s. |
| **M12** | Scoring v2 (hybride académique) | 🔥 3 | L (2-3 sem. impl. + 6 sem. shadow) | M11 | Formule : `0.25·risk_adjusted + 0.20·calibration + 0.20·timing_alpha + 0.15·specialization + 0.10·consistency + 0.10·discipline`. Gates durs anti-zombie + anti-Sybil. Shadow period A/B via `SCORING_VERSION=2`. |
| **M13** | Taker fees dynamiques | 🟡 4 | S (2-3 jours) | M11 | Endpoint `GET /fee-rate?tokenID=` intégré au `Sizer.calculate()` EV. Cache TTL 60 s. Protège l'EV face à l'évolution tarifaire Polymarket sur crypto/sports rapides. |
| **M14** | Real-time pipeline phase 2 | 🟢 5 (optionnel) | M (2-4 sem.) | M11, M12 | Parallélisation strategy pipeline (`asyncio.gather` sur filtres indépendants) + WebSocket `user` channel pour détection on-chain quasi-instantanée. Cible : 2-3 s → <1 s. Déclenche seulement si post-M11 on rate >10 % d'opportunités. |
| **M15** | Goldsky Turbo / Bitquery streaming | 🟢 6 (optionnel) | L | M11 | Webhook direct depuis Polygon RPC (~50 ms). Alternative Bitquery Kafka. Justifier par ROI clair avant d'engager. |
| **M16+** | MEV defense + market making Avellaneda-Stoikov | ⚪ futur | XL | M12 validé | Private transactions Polygon (Flashbots-like) quand position size > $500. Market making algorithmique. Nécessite que le scoring v2 ait démontré un edge robuste. |

## Prompts mis en suspens (MX_)

Anciens drafts M10/M11/M12 repositionnés pour plus tard. Pertinents mais non prioritaires vs la synthèse actuelle.

| Fichier | Sujet | Pourquoi parké |
|---|---|---|
| [`MX_watcher_live_reload_prompt.md`](MX_watcher_live_reload_prompt.md) | Re-fetch wallets actifs dynamiquement via TaskGroup | Blind spot réel M1+M5 mais résolu partiellement par discipline restart. À reprendre après M12. |
| [`MX_backtest_scheduler_prompt.md`](MX_backtest_scheduler_prompt.md) | Backtest quotidien + persistance `backtest_runs` | Sera très utile pour valider scoring v2 pendant la shadow period M12. Peut être fusionné avec M12. |
| [`MX_discovery_auto_lockout_prompt.md`](MX_discovery_auto_lockout_prompt.md) | Hystérésis lockout si backtest fail 3× consécutifs | Dépend de `MX_backtest_scheduler`. Pertinent après M12 en rodage. |

## Principes de nommage

- `M<n>-<titre-kebab-case>.md` pour une spec active ou implémentée.
- `MX_<description>_prompt.md` pour un prompt/draft en suspens.
- Un seul `M<n>` actif par numéro — ne pas écraser les historiques.

## Ordre d'exécution conseillé

```
M10 (parity + logs) ─┬──> M11 (WebSocket + latence) ─┬──> M12 (scoring v2)
                     │                                │
                     │                                └──> M13 (fees) [parallèle M12]
                     │
                     └──> Optionnel post-M12 validé :
                             M14 (latency phase 2) / M15 (Goldsky) / M16+ (MEV, MM)
```

Chaque spec suit le template M1-M9 (sections numérotées, exhaustive : contexte, design, DTOs, migrations, config, changements module par module, plan d'implémentation, tests, CLAUDE.md deltas, risques, rollout).
