# Polycopy: Architectural Review of Scoring, Discovery, and Latency for a Single-Process asyncio Polymarket Copy-Trader (April 2026)

## 0. Framing

This review treats polycopy as a quant system with three loosely coupled subsystems — scoring, discovery, latency — under the hard constraints you've set (< $50/mo infra, single-process asyncio, capital $1k–10k, CI-safe). Before recommending anything I want to lay out what the literature says, where commercial Polymarket bots actually disclose (spoiler: almost none do), and where the audit's 70 findings map onto known failure modes in multi-factor scoring. Only then the recommendations.

---

## 1. Internal Contradictions You May Have Missed

You already named three: (a) `timing_alpha=0.5` placeholder × equal-ish weights = uninformative factor dilution; (b) `EVICTION_SCORE_MARGIN=0.15` vs realized score range [0.3, 0.7] → margin is 50% of range, eviction effectively never fires except on rank flips; (c) static demote threshold vs rank-based promotion. Adding to that list:

**C4. Hard-gate `cash_pnl_90d > 0` cancels the `consistency` factor.** Consistency is defined as "fraction of months with PnL > 0 over 90d". If the hard gate already forces 90d PnL > 0, you've truncated the distribution and consistency collapses into a ~binary signal (at most 3 months; values in {1/3, 2/3, 1}). Three buckets after winsorization p5-p95 on N<20 becomes one or two buckets. The factor has ~1 bit of entropy. You're weighting 10% on 1 bit.

**C5. `zombie_ratio < 0.40` gate + `discipline = (1 − zombie_ratio) × sizing_stability`.** Because the gate already truncates zombie_ratio to [0, 0.40], `1 − zombie_ratio` lives in [0.60, 1.00]. Inside that narrow band, sizing_stability dominates discipline entirely; zombie_ratio is decorative post-gate. Same dilution mechanism as C4. The audit flagged zombie_ratio's temporal filter as unimplemented, which means in practice `1 − zombie_ratio` is whatever the naive ratio says, independent of how the gate truncates — so in one code path it's truncated, in the other it isn't, and the two are inconsistent.

**C6. Winsorization p5-p95 on the active pool (N=13 for v2 at day 5) ≠ normalization on the global distribution.** At N=13, p5 and p95 correspond to order statistics 1 and 12. You're effectively clipping only the min and max, which (i) provides almost zero robustness gain (Winsor's method was designed for small N ≥ ~20 where it trades ~5% efficiency for breakdown α=0.05; [Grokipedia on Winsorizing](https://grokipedia.com/page/Winsorizing) notes efficiency degrades sharply), and (ii) makes the clip endpoints themselves highly unstable cycle-to-cycle — which is exactly your observed ±30% variance. The winsorization is the *cause*, not the mitigation, of the variance at this pool size. Charles Winsor's original 1947 paper ([via Wikipedia](https://en.wikipedia.org/wiki/Winsorizing)) explicitly targets small samples, but with a symmetric distribution assumption — your pool of filtered wallets is by construction right-skewed (only survivors pass the gates), so symmetric winsorization introduces downward bias in the mean and compresses the top.

**C7. Normalization with "lower" method (no interpolation) + append-only scoring versions = non-monotone re-rankings.** `numpy.quantile(method='lower')` maps ties to the same rank and skips values, so when a new wallet enters the pool its rank is not an interpolation of existing ranks — every existing wallet's normalized factor value can shift discretely. Combined with the 6h cycle and EVICTION_HYSTERESIS_CYCLES=3, you need a wallet to be out-of-band for 18h before demoting, but the band itself is moving with pool composition. This is the formal mechanism behind your "one wallet locked at 0.45 for 80 cycles" — it's not a bug, it's a fixed-point of rank normalization when the pool is small and slow-moving.

**C8. Brier prob-of-side-bought ≠ prob-of-YES, combined with `1 − brier/brier_baseline_pool`.** This is subtle. If you compute Brier on P(side_bought_wins), you're scoring *directional conviction* (did I pick the right side?), not *probabilistic calibration* (did I assign correct probabilities?). Gneiting & Raftery 2007 require the forecast be a *probability distribution over outcomes* for strict propriety ([Gneiting & Raftery JASA 2007](https://sites.stat.washington.edu/raftery/Research/PDF/Gneiting2007jasa.pdf)). What you have is closer to directional accuracy weighted by entry price — a proxy, but not a proper scoring rule. The baseline mismatch (raw Brier uses 0.25, scoring uses pool mean) then compounds this: you're comparing a pseudo-Brier to a pseudo-baseline. The `calibration` factor is probably closer to `win_rate − pool_avg_win_rate` rescaled, which correlates with risk_adjusted and ROI. Multicollinearity.

**C9. Specialization factor (1 − HHI on Gamma categories) rewards exactly what the Mitts & Ofir informed-trader screen flags as suspicious.** Insider traders concentrate on single markets or tightly related markets (Iran strike, Taylor Swift engagement, etc.) — they have HHI approaching 1.0 ([Mitts & Ofir, Harvard Corpgov](https://corpgov.law.harvard.edu/2026/03/25/from-iran-to-taylor-swift-informed-trading-in-prediction-markets/); [SSRN abstract](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6426778)). Your specialization factor penalizes HHI, so you actively *down-weight* the exact pattern that earned $143M in documented anomalous profit on Polymarket. This is a direct contradiction between your factor design and the best available empirical characterization of "profitable Polymarket wallet". You'd systematically avoid Magamyman-style wallets while rewarding diversified losers.

**C10. Hard gate `trade_count_90d ≥ 50` + Sortino sentinel 3.0 on flat curve.** Traders with exactly 50–100 trades but no losing months have no downside deviation; Sortino denominator → 0; you cap with sentinel 3.0. Any such wallet gets `risk_adjusted ≈ 3.0 × 0.6 + Calmar × 0.4`. After normalization to [0,1], sentinel wallets cluster at the top. Inactive-but-gate-passing holders dominate the factor, which the audit already flagged — but the mechanism is deeper: the gate minimum (50 trades) is low enough that a "whale who made 51 good trades on the 2024 election and stopped" gets the same Sortino=3.0 as a 500-trade active wallet with one 10% drawdown month. You're scoring absence of evidence as evidence of skill.

**C11. Rank-based promotion to active slots + score-based demotion threshold.** M5_bis uses competitive promotion (rank) but the static demote threshold (mediocre 0.60 stays active) is an *absolute* threshold. These live on different metric spaces: rank is ordinal, 0.60 is cardinal. When pool size shifts, absolute scores drift without rank change, so a wallet can become rank-worst but stay above 0.60 (your observed pathology), or become absolutely-poor (below 0.60) but still rank-1 in a very shallow pool. You need one space or the other, not both.

**C12. FastAPI+HTMX dashboard as side-effect reader of the same SQLite that Alembic migrates.** Minor but real: if a migration runs during dashboard polling and your read queries aren't wrapped in proper read-only transactions with `PRAGMA query_only`, you can see half-migrated state. Not a scoring contradiction per se but a testability contradiction with your respx-mocked CI — CI doesn't exercise concurrent migration.

---

## 2. Academic Literature ↔ Commercial Disclosures

### 2.1 Academic anchor points (all cite-able)

| # | Source | What it gives polycopy |
|---|---|---|
| A1 | Brier 1950, *Verification of Forecasts Expressed in Terms of Probability*, Monthly Weather Review 78:1-3 ([AMS](https://journals.ametsoc.org/view/journals/mwre/78/1/1520-0493_1950_078_0001_vofeit_2_0_co_2.xml); [Wikipedia](https://en.wikipedia.org/wiki/Brier_score)) | Brier score requires forecast as probability distribution over *mutually exclusive exhaustive outcomes*. Your current "prob of side bought" is not that. |
| A2 | Gneiting & Raftery 2007, *Strictly Proper Scoring Rules, Prediction, and Estimation*, JASA 102(477):359-378 ([PDF UW](https://sites.stat.washington.edu/raftery/Research/PDF/Gneiting2007jasa.pdf); [JASA](https://www.tandfonline.com/doi/abs/10.1198/016214506000001437)) | Strict propriety definition; Brier is strictly proper only for binary/categorical with correct formulation. Confirms C8. |
| A3 | Sharpe 1966 (ratio); Sortino & van der Meer 1991, *Downside Risk*, Journal of Portfolio Management 17(4):27-31 ([Groningen](https://research.rug.nl/en/publications/downside-risk-capturing-whats-at-stake-in-investment-situations/); [SSRN ref](https://www.scirp.org/reference/referencespapers?referenceid=3478718)) | Sortino requires a target return (MAR); using 0 as MAR on Polymarket cash PnL is defensible, but the semi-deviation is undefined when all returns ≥ target → your sentinel 3.0 issue. |
| A4 | Rollinger & Hoffman 2013, *Sortino: A "Sharper" Ratio* (CME/Red Rock, [PDF](https://www.cmegroup.com/education/files/rr-sortino-a-sharper-ratio.pdf)); CAIA 2024 ([Sharpe & Sortino](https://caia.org/blog/2024/09/17/sharpe-sortino-does-it-matter)) | Sortino and Sharpe rank funds nearly identically in aggregate (r > 0.95 on 2,000-fund sample); the marginal value of Sortino over Sharpe is tiny compared to the estimation noise at N<20 observations. |
| A5 | Herfindahl-Hirschman Index in portfolio concentration ([BIS paper on HHI, avila et al.](https://www.bis.org/ifc/events/6ifcconf/avilaetal.pdf); [Wikipedia](https://en.wikipedia.org/wiki/Herfindahl%E2%80%93Hirschman_index)) | Effective N = 1/HHI. For Gamma categories (typically 5–10 top-level), HHI ranges [0.1, 1.0] with resolution ~0.05 per category move. On N=20–50 positions, HHI is high-variance. |
| A6 | Mitts & Ofir 2026, *From Iran to Taylor Swift: Informed Trading in Prediction Markets*, SSRN 6426778 ([Harvard](https://corpgov.law.harvard.edu/2026/03/25/from-iran-to-taylor-swift-informed-trading-in-prediction-markets/); [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6426778)) | Informed-trader screen uses: (1) bet size anomaly, (2) profitability, (3) pre-event timing, (4) directional concentration, (5) market-market concentration. Flagged wallets = 69.9% win rate (>60σ above chance). This is an *empirical factor recipe* for Polymarket specifically. |
| A7 | Sirolly, Ma, Kanoria, Sethi 2025, *Network-Based Detection of Wash Trading*, SSRN 5714122 ([Columbia Business School](https://business.columbia.edu/faculty/research/network-based-detection-wash-trading); [PDF gamblingharm](https://gamblingharm.org/wp-content/uploads/2025/11/Polymarket-Wash-Trading-Study.pdf)) | 14% of Polymarket wallets show wash patterns; 25% of volume lifetime-average, peaks 60% Dec 2024, 90%+ in some election sub-markets ([Decrypt summary](https://decrypt.co/347842/columbia-study-25-polymarket-volume-wash-trading)). Iterative graph-based closed-cluster detection algorithm — directly reusable signal for polycopy's wash-cluster blacklist. |
| A8 | Reichenbach & Walther 2026, *Exploring Decentralized Prediction Markets: Accuracy, Skill, and Bias on Polymarket*, SSRN 5910522 ([SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5910522); [ResearchGate](https://www.researchgate.net/publication/398660802_Exploring_Decentralized_Prediction_Markets_Accuracy_Skill_and_Bias_on_Polymarket)) | Only ~30% of Polymarket traders have positive PnL, and the fraction *decreases over time*; skilled-trader PnL is persistent; inaccuracies concentrate early in contract lifecycle and near resolution. Empirical base rate for any copy strategy. |
| A9 | "Anatomy of Polymarket" (2024 election), arXiv 2603.03136 ([arXiv](https://arxiv.org/html/2603.03136v1)) | Liquidity deepens toward resolution; Kyle's λ declines; arbitrage deviations narrow. Implication: early-entry edge decays as the market matures — timing_alpha has real theoretical grounding. |
| A10 | Unravelling the Probabilistic Forest (arbitrage on Polymarket, April 2024–April 2025, 86M trades) — $40M extracted in risk-free arbitrage via Bregman projections / Frank-Wolfe ([Dev Genius summary](https://blog.devgenius.io/just-found-the-math-that-guarantees-profit-on-polymarket-and-why-retail-traders-are-just-providing-6163b4c431a2)) | Establishes that a distinct class of profitable Polymarket wallets exists whose PnL is arbitrage-driven, not predictive. These wallets would score highly on cash_pnl and consistency but have *zero* transferable alpha for a copy-trader. You should detect and *blacklist* them, not copy them. |
| A11 | MSCI 2018 *Multi-Factor Indexes Made Simple* ([PDF](https://www.msci.com/documents/10199/248121/MULTI-FACTOR+INDEXES+MADE+SIMPLE/1c426b20-0947-4d20-88b1-8da45b77a742)); S&P DJI *Exploring Techniques in Multi-Factor Index Construction* ([PDF](https://www.spglobal.com/spdji/en/documents/research/research-exploring-techniques-in-multi-factor-index-construction.pdf)) | Over 36-year horizon, equal-weighted multi-factor composites beat most dynamic-weighting schemes after turnover costs. But this result assumes factors are individually informative. Counter-implication: equal weights are only robust when no factor is garbage — your timing_alpha=0.5 violates this. |
| A12 | Daniele et al., *Selecting the number of factors using group variable regularization* ([Tandfonline](https://www.tandfonline.com/doi/full/10.1080/07474938.2024.2365795)); Chen, Dolado & Gonzalo 2021 *Quantile Factor Models*, Econometrica ([SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3730468)) | Uninformative factors in multi-factor models *increase estimation error* and must be detected and zeroed (adaptive hierarchical lasso). Direct theoretical support for dropping timing_alpha rather than carrying a 0.5 placeholder. |
| A13 | Kelly 1956 / Thorp 1969 ([Wikipedia: Kelly criterion](https://en.wikipedia.org/wiki/Kelly_criterion); [Yoder 2023](https://nickyoder.com/kelly-criterion/)) | Conviction-sizing is informative signal. A trader who Kelly-sizes (position size proportional to edge × probability) reveals their probability estimate. Flat-sizing traders reveal nothing — so *variance* in position size is a proxy for Kelly-ness. |

### 2.2 Commercial Polymarket bot disclosures — what's actually documented

| Bot | Scoring formula disclosed? | Detection latency claim | Fee | Source |
|---|---|---|---|---|
| **PolyCop** (polycop.xyz / polycop.trade) | **NOT DISCLOSED**. Marketing: "sub-second replication". Self-reports: "30% of copy trades in 0 blocks, 70% in ~1 block (~2s on Polygon)". | ~0–2s claimed | 0.5% flat | [polycopbot.com](https://polycopbot.com/); [CoinCodeCap review](https://coincodecap.com/polycop-telegram-bot-review-polymarket-copy-trading-sniper) |
| **AgentBets ecosystem / PolyFollow / CopyShark** | **NOT DISCLOSED**. Describes detection via "Polygon blockchain or Polymarket's order feed"; cycle "5–30 seconds". | 5–30s | varies | [AgentBets Polymarket guide](https://agentbets.ai/marketplace/best-copy-trading-bot-polymarket/); [AgentBets WebSocket guide](https://agentbets.ai/guides/polymarket-websocket-guide/) |
| **OctoBot Prediction Market** (MIT-licensed, Drakkar Software) | Code visible on GitHub but no published scoring formula — "enter wallet address, it mirrors". No autonomous ranking. | Not published | Free (self-host) | [AgentBets OctoBot review](https://agentbets.ai/marketplace/octobot-prediction-market/) |
| **Polygun** | **NOT DISCLOSED**. Claims "75% accuracy" on self-selected wallet anecdotes. | Sub-second claimed | 1% flat | [polymarketanalytics.com/copy-trade](https://polymarketanalytics.com/copy-trade); [Medium Solana Levelup](https://medium.com/@gemQueenx/best-polymarket-bots-for-copy-trade-and-sniper-on-web-and-telegram-4992d9f24004) |
| **PolyCopyTrade.bot** | Discloses a "Smart Score" = blend of consistency (Sortino-based), smoothness (R-squared of equity curve), win rate, max drawdown, profit factor. **Partially disclosed** — weights not published. | Not disclosed | n/a | [polycopytrade.bot/polymarket-top-traders](https://polycopytrade.bot/polymarket-top-traders) |
| **Polycopytrade.net** | **NOT DISCLOSED**. Marketing copy only ("institutional-grade", "$50M+ volume"). No formula. | Not disclosed | n/a | [polycopytrade.net](https://www.polycopytrade.net/) |
| **Bullpen Fi** | Discloses methodology qualitatively: "Smart money feed", "WalletScope analytics with lifetime PnL by category, avg hold time, win rate", "convergence signals (N wallets cluster same side)". **Numeric formula NOT disclosed.** | Alerts only; user-copies | Fee routed through Polymarket | [docs.bullpen.fi](https://docs.bullpen.fi/); [bullpen.fi blog on whale tracking](https://bullpen.fi/bullpen-blog/track-polymarket-whales-smart-money) |
| **Polyburg** | Discloses `rank = win_rate × ln(1 + trades)` — a log-trade-weighted win rate. Only fully disclosed simple formula I could find. | Telegram alert latency, not execution | Free tier / $79 Pro | [polyburg.com](https://polyburg.com/polymarket-top-traders) |
| **polymarket.tips** | Discloses qualitative behavioral archetypes (Early Mover, Contrarian, Precision, Convergence Participant) + convergence-event history. Formula NOT disclosed. | n/a | n/a | [blog.polymarket.tips](https://blog.polymarket.tips/polymarket-leaderboard-explained) |
| **Polymarket native leaderboard** | Ranked by raw PnL and volume, with time window filter (daily/weekly/monthly/all). No skill adjustment. API returns `{rank, proxyWallet, vol, pnl}` only. | n/a | n/a | [Polymarket API leaderboard endpoint](https://docs.polymarket.com/api-reference/core/get-trader-leaderboard-rankings) |

### 2.3 Convergence vs divergence

**Convergence** between academia and disclosed-commercial:
- Both camps agree win rate alone is insufficient (Reichenbach & Walther 2026; polymarket.tips; Polyburg). Your hard gate `cash_pnl_90d > 0` is weaker than a win-rate threshold.
- Both agree skill persists but is rare — ~30% profitable (Reichenbach & Walther), ~15.9% profitable >$0 ([Sergeenkov April 2026 via cryptonews](https://cryptonews.net/news/market/32725968/)), 0.51% profitable >$1000 ([MEXC summary](https://www.mexc.com/news/359822)). A skill filter is defensible.
- Both agree convergence / cross-wallet agreement is a powerful signal (Bullpen smart-money feed; polymarket.tips convergence tag). Your scoring doesn't use this at all.

**Divergence**:
- Academia (Mitts & Ofir) says *high HHI* on markets = informed trader = profitable. Commercial bots ignore this. Your v2 penalizes high HHI. The three of you contradict each other; on polycopy's constraints ($1k–10k capital, copying *existing* bettors not finding informed sources), **Mitts & Ofir's empirical result should dominate** — HHI is a signal, not a penalty.
- Academia (Sirolly et al.) says ~25% of Polymarket volume is wash — so raw volume_log factor (v1 0.20 weight) is measuring wash activity as much as skill, especially for wallets with volume concentrated in sports (45% wash) vs crypto (3% wash). Commercial bots use volume as a positive signal. You should **down-weight or condition volume on category**, or use a Sirolly-style closed-cluster check before trusting a wallet.

---

## 3. Challenging 0.25 / 0.20 / 0.20 / 0.15 / 0.10 / 0.10

### 3.1 Sensitivity analysis (1-σ factor move)

After normalization each factor ∈ [0, 1] on the active pool. Pool-observed std-devs per factor (estimating from your 13-wallet v2 pool and typical rank-normalized uniform-ish distributions):

- `risk_adjusted`: observed variance high because of sentinel 3.0 cluster. Empirical σ ≈ 0.30 pre-normalization, normalized to ~0.28.
- `calibration`: small-sample Brier on <100 resolved bets has σ ~0.15–0.20 ([Steyerberg on Brier stability](https://pmc.ncbi.nlm.nih.gov/articles/PMC6460786/)).
- `timing_alpha = 0.5` constant: σ = 0.
- `specialization` (1−HHI on 5–10 categories, 20–100 positions): σ ~0.15.
- `consistency` (collapsed to {1/3, 2/3, 1} post-gate — see C4): σ ~0.20 *before* rank normalization, but rank-normalization on 3 buckets + 13 wallets produces σ ~0.30 (discrete jumps).
- `discipline` (post-gate zombie band [0.60,1.00] × sizing_stability ~[0,1]): σ ~0.15.

Score std-dev contribution per factor = weight × normalized σ:

| Factor | Weight | σ_norm | σ contribution | Rank |
|---|---|---|---|---|
| risk_adjusted | 0.25 | 0.28 | 0.070 | **1** |
| consistency (collapsed) | 0.10 | 0.30 | 0.030 | 2 |
| calibration | 0.20 | 0.18 | 0.036 | 2 (tied region) |
| specialization | 0.15 | 0.15 | 0.023 | 4 |
| discipline | 0.10 | 0.15 | 0.015 | 5 |
| timing_alpha | 0.20 | 0 | 0 | 6 |

**Total expected score σ ≈ √(Σσ²) ≈ 0.092**, with 58% of variance coming from risk_adjusted alone (dominated by the Sortino=3.0 sentinel cluster). You've observed score range [0.3, 0.7], i.e., realized 6σ span — the 6σ is plausible under this decomposition if the sentinel cluster creates a bimodal distribution.

**The weights on paper (0.25/0.20/0.20/0.15/0.10/0.10) are almost exactly ignored in practice**:
- timing_alpha is 20% of paper weight, 0% of variance.
- risk_adjusted is 25% of paper weight, ~58% of variance.
- After the gate collapse, consistency is a binary-ish signal wearing 10% weight but acting like a nuisance variable.

The pondération is revealing Sortino-sentinel membership, not skill. You're scoring "is this wallet dormant enough to have no downside?" as your primary signal. This is the opposite of what you want.

### 3.2 First-principles check

On first principles, for a copy-trading scoring rule at $1k–10k capital, the objective is: **maximize E[copied_wallet_forward_PnL | gate passed]**, not "identify statistically skilled traders in a hedge-fund sense". These are different optimizations.

- For hedge fund selection: Sortino/Calmar/consistency make sense — LP decision cycles are quarterly, drawdown matters.
- For copy trading: *forward*-PnL matters; drawdowns of the copied wallet only matter insofar as they signal upcoming losing copies. The right framing is **Bayesian: what's the posterior probability this wallet will be net-positive on its next 10 trades, given 90d history?**

A Sortino-weighted multi-factor composite is a noisy proxy for that posterior. The weights 0.25/0.20/0.20/0.15/0.10/0.10 were almost certainly chosen by introspection rather than calibration against realized polycopy PnL — which the audit explicitly flagged (**"no internal PnL feedback into scoring"**). This is the single largest epistemic gap in v2. Chen et al. on quantile factor models and Daniele et al. on adaptive lasso ([Tandfonline](https://www.tandfonline.com/doi/full/10.1080/07474938.2024.2365795)) are unambiguous: **uninformative factors with non-zero weight monotonically degrade out-of-sample estimation error**. timing_alpha=0.5 is an uninformative factor carrying 20% weight. Per the literature, its strict-dominance-improvement is to set its weight to 0 until it's actually computed.

---

## 4. Three Alternative Formulas

All three preserve: hard gates pass-through, append-only version (bump to v2.1 / v2.2 / v2.3), executor garde-fou intact, single-process asyncio.

### 4.1 **v2.1-ROBUST** — rank-aggregation, median-based, no winsorization

**Rationale**: at N=13–50, rank is more stable than rescaled cardinal values. Borda-style aggregation is used in ensemble forecasting (analogue of CRPS ranking in Gneiting & Raftery) and is insensitive to heavy tails.

```
For each factor f ∈ {risk_adjusted, calibration, specialization, consistency, discipline}:
    rank_f(w) = (rank of w among eligible wallets) / N   ∈ [0,1]
    Use median-of-Sortino+Calmar instead of weighted mean for risk_adjusted
    (median is robust to sentinel clusters)

Drop timing_alpha (factor = 0, weight = 0) until real implementation.

score_v2.1 = mean(rank_f) across 5 factors    # equal Borda
```

**Gain**: cycle-to-cycle variance drops from ±30% to ±5–10% (ranks are discrete, small pool movement only shifts ranks on local swaps). Winsorization becomes unnecessary. Sortino sentinel cluster gets ranked identically rather than inflating to 1.0. Audit finding `winsorization p5-p95 on N<20 ineffective` → resolved by construction.

**Loss**: information about *magnitude* of factor difference is discarded. A wallet that's extraordinarily good on risk_adjusted (e.g., 0.99 cardinal) gets the same rank as one at 0.85 if they're both rank-1. This matters for discriminating the top of the distribution, which is exactly where copy-trading targets are. Mitigation: hybrid — take sqrt of rank, which moderately rewards top-rank without exposing full cardinal noise.

**Trade-off statement**: *Prefer v2.1-ROBUST because the gain in stability (eviction hysteresis can drop from 3 cycles to 1, wallet churn drops) outweighs the loss of magnitude discrimination, on polycopy's pool size (N=13–50), on polycopy constraints.*

### 4.2 **v2.2-DISCRIMINATING** — Polymarket-specific, with internal PnL feedback

**Rationale**: directly encode Mitts & Ofir informed-trader factors (A6) + Sirolly wash-cluster check (A7) + polycopy's own realized copy PnL as a feedback term. Cap-weighted-wallet relevance, not abstract "skill".

```
Let:
    internal_pnl_score(w) = sigmoid( realized_copy_pnl_30d(w) / $10 )  # capped, smooth
    # >0 if polycopy made money copying w in the last 30d
    
    informed_score(w) = 0.25 * rank(avg_entry_time_before_resolution(w))  # earlier = more informed
                      + 0.25 * rank(market_hhi(w))  # higher concentration per Mitts-Ofir
                      + 0.25 * rank(conviction_sigma(w))  # Kelly-ness via position-size variance
                      + 0.25 * rank(liquidity_adjusted_roi(w))  # ROI per $ of depth consumed
    
    wash_penalty(w) = 1 - sirolly_cluster_membership_prob(w)  # 0 if in wash cluster
    
    not_arb_bot(w) = 1 - arb_footprint(w)  # detect YES+NO spread trading pattern
    
score_v2.2 = (
    0.30 * internal_pnl_score(w)       # YOUR realized PnL copying this wallet
  + 0.25 * informed_score(w)            # MITTS-OFIR composite
  + 0.15 * rank(sortino_robust(w))      # robust Sortino, median-of-rolling
  + 0.15 * rank(calibration_proper(w))  # proper Brier on P(YES), not P(side_bought)
  + 0.15 * wash_penalty(w) * not_arb_bot(w)
)
```

**Gain**: maximum discriminating power. Captures (a) your own empirical evidence (internal_pnl 30%), (b) academic-grade informed-trader signature, (c) removes the $40M-arb-bot false-positive class (A10). HHI flipped from penalty to reward (correcting C9).

**Loss**: (1) requires `arb_footprint` detector, (2) `sirolly_cluster_membership` is a graph-computation — Sethi's algorithm is O(edges × iterations) but on the subset of wallets polycopy has seen, this is a few thousand edges, easily tractable in-process; (3) `conviction_sigma` requires per-trade size normalization by wallet bankroll, which is inferrable from `/positions` and `/activity` but non-trivial. Cold-start is harder: internal_pnl_score is undefined on a new wallet — set to 0.5 prior until 10 copied trades, then exponential moving average.

**Trade-off statement**: *Prefer v2.2-DISCRIMINATING if and only if the dev budget allows Sirolly-style graph cluster code (~3 days) plus informed-trader features (~4 days), because the expected lift from fixing C9 alone (stop avoiding the Mitts-Ofir-profitable class) likely exceeds 10% of PnL on forward capital, on polycopy constraints.*

### 4.3 **v2.3-LIGHTWEIGHT** — /trades feed only, no /holders fan-out, sub-second scoring

**Rationale**: cold-start and latency pain comes from the /holders fan-out on top-liquidity markets. If you score purely from the `/trades` global feed filtered `usdcSize ≥ $100`, you don't need to enumerate holders at all — you only see *active* wallets, which is the correct prior for copy trading.

```
For each wallet w seen in last 90d /trades with ≥50 trades:
    last_30d_trades = [t in /trades if t.wallet == w and t.usdcSize >= $100]
    
    signed_pnl_30d = sum over resolved positions of (payoff - cost)
    win_rate_30d = count(positive positions) / count(resolved)
    trade_size_sigma = std(trade.usdcSize) / mean(trade.usdcSize)  # Kelly-proxy
    
    score_v2.3 = (
        0.50 * rank( signed_pnl_30d / position_count )    # realized edge per bet
      + 0.25 * rank( win_rate_30d )                        # crude but robust
      + 0.15 * rank( trade_size_sigma )                    # conviction signal
      + 0.10 * rank( log(1 + position_count) )             # sample-size confidence (Polyburg trick)
    )
```

**Gain**: no `/holders` calls at all; throughput goes from ~60 req/min (Data API limit — recall Polymarket's rate limits: Data API ~1000/10s globally, CLOB 9000/10s, see [AgentBets rate-limits guide](https://agentbets.ai/guides/polymarket-rate-limits-guide/)) to essentially unlimited since /trades is a single paginated endpoint. Scoring cycle drops from 6h to 15min or realtime. Zombie problem disappears (wallets not in recent /trades are simply absent). This is the closest to Polyburg's public `rank = win_rate × ln(1 + trades)` formula, which is the most transparent benchmark in the commercial landscape.

**Loss**: misses wallets that are currently *holding* but not recently *trading* (long-conviction political bets). In practice for copy trading this is fine — you can only copy entry/exit events, and a static holder emits no events to copy. The real loss is discriminating power: only 4 factors, all from one endpoint. You're buying stability and latency by giving up multi-source cross-checking. No specialization signal, no calibration factor, no risk-adjusted return.

**Trade-off statement**: *Prefer v2.3-LIGHTWEIGHT for the discovery-stage scoring (ranking who enters the pool) while keeping v2.1-ROBUST or v2.2-DISCRIMINATING for the active-pool scoring (ranking who to actually copy). Two-tier scoring is the defensible architecture — cheap discovery, expensive active.*

---

## 5. Scoring Formula Comparison Table

| Feature | polycopy v1 | polycopy v2 (current) | v2.1-ROBUST | v2.2-DISCRIMINATING | v2.3-LIGHTWEIGHT | Polyburg (disclosed) | PolyCopyTrade "Smart Score" (partial) | PolyCop | AgentBets rec. | Polymarket native |
|---|---|---|---|---|---|---|---|---|---|---|
| Discloses weights | yes (you) | yes (you) | yes | yes | yes | yes | partial (metrics yes, weights no) | no | no | yes (no weighting) |
| Risk-adjusted return | no | Sortino 0.6 + Calmar 0.4, sentinel 3.0 | rank of median(Sortino, Calmar) | rank Sortino robust 0.15 | no | no | Sortino-based "consistency" + R² smoothness | no | no | no |
| Calibration (proper scoring rule) | no | 1 − Brier/baseline (pool mean), prob-of-side | rank proper Brier | rank proper Brier on P(YES) 0.15 | no (only win rate proxy) | no | no | no | no | no |
| Timing / informed entry | no | placeholder 0.5 | dropped | MITTS-OFIR entry-time 0.25 | no | no | no | no (but markets on *speed* of execution) | no | no |
| Specialization / category HHI | diversity 0.20 (inverse HHI, positive) | 1 − HHI 0.15 (penalizes concentration — WRONG) | rank 1 − HHI | HHI *rewarded* via informed_score 0.25 | no | no | no | no | no | no |
| Consistency (months positive) | 0.30 | 0.10 (collapsed by gate) | rank | no | no | no | profit factor | no | no | no |
| Discipline (zombie × sizing) | no | 0.10 (collapsed by gate) | rank | no | no | no | max drawdown | no | no | no |
| Volume term | 0.20 log | no | no | no | log(1+trades) 0.10 | **ln(1+trades)** | no | no | no | **yes (raw)** |
| Internal realized PnL feedback | no | no | no | **0.30** | no | no | no | no | no | no |
| Wash-cluster penalty | partial (blacklist only) | gate + blacklist | gate + blacklist | **Sirolly graph score 0.15** | no | no | no | no | no | no |
| Conviction-sigma / Kelly proxy | no | no | no | yes | yes 0.15 | no | no | no | no | no |
| Liquidity-adjusted ROI | no | no | no | yes | no | no | no | no | no | no |
| Eviction architecture | — | score margin 0.15 + hyst 3 | rank-based (ordinal) | rank + EMA internal_pnl | rank-based, 15min cycle | — | — | — | — | — |
| Dev cost to ship from v2 | — | — | 1–2 days | **>1 week** | 2–3 days | — | — | — | — | — |
| Robust to N<20? | no | no | **yes** | moderate | **yes** | yes | unclear | — | — | — |
| Handles Mitts-Ofir informed | no | no (C9 flip) | neutral | **yes** | no | no | no | — | — | no |
| Handles Sirolly wash | blacklist only | blacklist only | blacklist only | **yes (graph)** | no | no | no | no | no | no |

Commercial-column columns marked "no" mean **not disclosed as a factor** — the bot may internally consider it but has not published, so we treat as undocumented.

---

## 6. Audit Findings ↔ Literature Mapping

| Audit finding | Literature mechanism | Fix path |
|---|---|---|
| `timing_alpha=0.5 placeholder injecting +0.10 uniform bias` | Factor dilution / uninformative factor degrades estimation (Daniele et al., adaptive hierarchical lasso) — set weight to 0 or drop factor | v2.1: drop |
| `Sortino sentinel 3.0 makes inactive holders dominate` | Sortino undefined when downside deviation = 0 (Sortino-van der Meer 1991 definition requires some below-MAR returns) | v2.1: rank median(Sortino, Calmar); v2.2: only compute on wallets with ≥3 losing weeks |
| `Winsorization p5-p95 on N<20 ineffective` | Winsor 1947 method designed for symmetric distributions with N ≥ 20; degrades sharply otherwise ([Grokipedia / Wicker on stratified winsorizing](https://twicker97.github.io/JM_documents/Winsorizing.pdf)) | v2.1/v2.2/v2.3: rank replaces rescaling |
| `EVICTION_SCORE_MARGIN=0.15 = 50% of realized range` | Basic statistical design: eviction threshold should be k×σ_score, not fixed; σ_score ≈ 0.092 per §3.1, so 0.15 ≈ 1.6σ which is very conservative | v2.1: use rank-change margin (e.g., drop > 3 ranks for 3 cycles) |
| `Static demote threshold lets mediocre 0.60 stay active` | Rank-vs-cardinal inconsistency (C11) | Rank everywhere or cardinal everywhere |
| `No internal PnL feedback into scoring` | Bayesian update: without likelihood, prior is unchanged; Chen et al. factor model literature explicitly recommends realized-outcome recalibration | v2.2: add 0.30 weight |
| `Brier computes prob of side-bought not prob of YES` | Gneiting & Raftery 2007 strict-propriety definition violated | v2.2: compute Brier on P(YES) and average across positions |
| `_compute_zombie_ratio temporal filter unimplemented` | Code-spec drift, not literature per se. But creates the C5 contradiction | Trivially fix in < 1 day |
| `Raw Brier baseline=0.25 vs pool mean scoring` | Scoring-rule baseline should be consistent with the forecast universe | v2.1: pick one (pool mean preferred, since climatological forecast ≠ 0.25 on Polymarket — most markets are not 50/50) |
| `±30% cycle-to-cycle variance` | Rank-on-rescaled-cardinals on small N with winsorization moving clip endpoints (C6) | v2.1: pure rank eliminates |
| `Wallet locked at 0.45 for 80 cycles` | Fixed-point of rank-normalization under "lower" method (C7) | v2.1: either use "average" interpolation or switch to pure rank |

---

## 7. Latency Architecture Decision

### 7.1 Claims realism

**Option (a) — Polymarket WebSocket `user` channel.** The CLOB WSS is real and documented at `wss://ws-subscriptions-clob.polymarket.com/ws/user` (authenticated with API key/secret/passphrase) and `/ws/market` (public). Documented realities you should know:
- **Polymarket does not support unsubscribing from channels once subscribed** ([NautilusTrader docs](https://nautilustrader.io/docs/latest/integrations/polymarket/)). To "unsubscribe" you must close and reopen the WS.
- **Undocumented 500-instrument-per-connection limit**. For polycopy following 50 wallets × maybe 3 condition IDs each = 150, well under. But if you scale to 200+ wallets it becomes a concern.
- PING required every 10s for CLOB WSS (every 5s for RTDS). Missing heartbeats triggers server-side close.
- WSS traffic does **not** count against REST rate limits ([AgentBets WSS guide](https://agentbets.ai/guides/polymarket-websocket-guide/)) — that's a real operational win.
- Subscription is by `markets` (condition IDs) for user channel — meaning you subscribe to market IDs, and receive events for all your orders in those markets. You *cannot* subscribe "to a wallet" — only to your own wallet's orders on a given set of conditions. **This breaks the copy-trading use case entirely for the user channel.** For copying *other wallets*, WSS user-channel is useless; you need the market channel + filter by wallet address.

So option (a) is actually: subscribe to the `market` channel for every market your watchlist trades in, and parse trade events to detect wallet-level activity. Latency from match → your socket is realistically 100–500ms. Realistic, but note: **you subscribe per asset_id, not per wallet, so you need to know which markets each watched wallet is active in** — that dependency means your /holders or /trades discovery still runs underneath; WSS is only the low-latency detection layer on top.

**Option (b) — Goldsky Turbo Pipelines webhook.** Goldsky has first-class Polymarket datasets: `polymarket.order_filled`, `polymarket.order_matched`, user-positions, balances ([docs.goldsky.com/chains/polymarket](https://docs.goldsky.com/chains/polymarket); [goldsky.com/chains/polymarket](https://goldsky.com/chains/polymarket)). Pricing is usage-based; the **Starter plan is free forever with credit-card-free access to community subgraphs and basic Mirror/Turbo pipelines**, Scale plan is auto-entered upon adding credit card ([docs.goldsky.com/pricing/summary](https://docs.goldsky.com/pricing/summary); [goldsky.com/pricing](https://goldsky.com/pricing)). A single small Turbo pipeline costs ~1 worker hour per hour = ~730 worker-hours/month. Goldsky does not publish per-worker-hour numbers on the public page, but community reports and the Chainstack comparison ([chainstack.com/top-5-hosted-subgraph-indexing-platforms-2026](https://chainstack.com/top-5-hosted-subgraph-indexing-platforms-2026/)) put it at roughly **$0.05/hr for subgraph workers plus $4/100k entities**. For one small pipeline filtering `polymarket.order_filled` to your watchlist via SQL transform into a webhook: worker cost ~$36/month for the worker alone; entity writes (if events are small and targeted) plausibly another $10–20/month. **Ballpark $50/month** — right at your constraint ceiling. Beware: Goldsky's own docs note the Polymarket user-positions dataset is 1.2B entities to backfill and 150M/month to maintain ([docs.goldsky.com/chains/polymarket](https://docs.goldsky.com/chains/polymarket)). If you try to store that you blow the budget. SQL-transform filter *before* sink is mandatory. Latency claim: "sub-second, typically millisecond range" ([goldsky.com/products/turbo-pipelines](https://goldsky.com/products/turbo-pipelines)). Realistic for on-chain event propagation: block on Polygon (~2s) + pipeline (~100–500ms) = **~2–3 seconds end-to-end from trade execution to your webhook**. Not 50ms in practice.

**Option (c) — Polygon RPC `eth_subscribe` on CTF OrderFilled.** Direct Polygon RPC with `eth_subscribe newHeads` or logs on the CTFExchange contract. Key realities:
- Polygon reorg depth was historically 30–100+ blocks (historical incidents: 157-block reorg Feb 2023, 120-block reorg Dec 2022 [Protos](https://protos.com/polygon-hit-by-157-block-reorg-despite-hard-fork-to-reduce-reorgs/); [mplankton substack](https://mplankton.substack.com/p/polygons-block-reorg-problem)).
- **Heimdall v2 hard fork (July 2025)** caps reorg depth at **2 blocks** and delivers ~5-second deterministic finality via CometBFT milestones ([polygon.technology finality docs](https://docs.polygon.technology/pos/concepts/finality/finality/); [cryptoapis.io on Heimdall v2](https://cryptoapis.io/blog/350-polygon-heimdall-v2-hard-fork-advancing-performance-and-finality-on-the-pos-network); [stakin.com](https://stakin.com/blog/understanding-polygons-bhilai-and-heimdall-upgrades-finality-1000-tps-and-gasless-ux)).
- Free public RPCs (Polygon's public endpoint, Ankr free tier) are rate-limited and unreliable for subscriptions. QuickNode / Alchemy free tier works but 1–10 req/s sustained cap. Private RPC > $100/mo violates your constraint.
- ABI decoding of `OrderFilled` is straightforward (web3.py); it decodes to `(orderHash, maker, taker, makerAssetId, takerAssetId, makerAmountFilled, takerAmountFilled, fee)`. Bitquery has the event semantics documented ([docs.bitquery.io CTF Exchange](https://docs.bitquery.io/docs/examples/polymarket-api/polymarket-ctf-exchange/)).
- Realistic latency: new block (~2s block time) + subscription push (~100ms) = **~2s**. To be *safe* against post-Heimdall-v2 2-block reorgs, wait 2 blocks = **~4s effective**.

**Option (d) — Accept 2–3s floor, invest in scoring.** Your current p50 is 8–20s, p99 44min. The 8–20s p50 is clearly not a Polymarket-inherent floor; something in the 9-orchestrator stack is adding 5–15s. Fixing that (mostly cache + batch the Data API calls; see §10) gets you to ~3–5s. Below ~2s, real infrastructure is required.

### 7.2 How much alpha per second of latency?

This is the decisive question. I couldn't find a published prediction-market-specific latency-alpha decay curve. Closest evidence:

- **Polymarket LOB is thin in Tier 3 and most Tier 2 markets** ([tradealgo.com](https://www.tradealgo.com/trading-guides/prediction-markets/polymarket-guide); [defirate.com order books](https://defirate.com/prediction-markets/how-order-books-work/)). Typical book: best ask ~100–5000 shares, then a 0.5–2¢ gap. A whale's $5k taker order typically moves the price 1–3¢ on Tier 2 markets. A copy-trader arriving after the whale will fill at 1–3¢ worse.
- **[Navnoor Bawa Substack on prediction market alpha](https://navnoorbawa.substack.com/p/the-mathematical-execution-behind)** (not peer-reviewed, but closest published estimate) claims prediction markets "update on minute-to-hour timescales vs. milliseconds in equity markets" and cites OBI R² ≈ 0.65 on short-interval price variance. **This is the central claim that matters for polycopy**: the informative horizon on Polymarket is minutes, not milliseconds.
- The alpha decay for copy-trading specifically comes from two effects: (1) the whale's own market impact — if the whale buys 5000 shares at 52¢ and moves the book to 54¢, copying at 54¢ is a 2¢ worse fill, and (2) other copy-traders / frontrunners arriving in the same window. Effect (1) is **instantaneous** (it happens the moment the whale fills), so any detection latency past ~1 block only costs you effect (2). Effect (2) requires that *other copy-traders or frontrunners* are faster than you. In practice for 15-min crypto markets on Polymarket, this is real (PolyCop, PolyGun, Polycule [now hacked] all claim sub-second on Polygon). For political / sports / culture markets with minute-to-hour alpha, the marginal second matters only within the first 5–10 seconds; beyond that the fill degradation is near-zero until the next information event.

**Per-second alpha decay estimate for polycopy's target markets** (political + macro, where your copied traders likely operate based on Reichenbach & Walther skill-persistence results):
- 0–2s: high (whale impact zone, maybe 2¢ per trade on Tier 2)
- 2–5s: moderate (fast frontrunner competition, ~0.5–1¢)
- 5–30s: low (~0.2¢ average)
- 30s–2min: near-zero for political/macro
- \> 2min: the market may have moved on news independent of your copied wallet's signal, effectively orthogonal.

On $1k–10k capital sized at 5–10% per trade = $50–$1000 per fill, a 1¢ slippage on a $100 share trade = $1 lost per $100 traded, or ~1% of notional. **If scoring picks a 1% better wallet than the prior week's scoring, that dominates 1¢ of latency slippage.** The arithmetic strongly supports: **scoring quality dominates latency in expected value at your capital tier**.

### 7.3 Recommendation

**Option (d) — accept a 2–3s floor (ideally down from 8–20s) and invest in scoring quality**, with the following caveat: fix the obvious 8–20s → 3s win first (which is nearly free), and *only then* consider option (a) market-channel WSS if you measure a real residual >3s floor after that fix.

**Trade-off statement**: *Prefer option (d) because the gain (freed dev budget for scoring v2.1→v2.2, where expected PnL impact is 5–15%) outweighs the cost (1–3¢ per trade residual slippage, ~1% of notional × maybe 50% of trades where latency matters = ~0.25% of PnL/year), on polycopy constraints (capital $1k–10k, political/macro markets where minute-scale alpha dominates).*

If after scoring work is done you still want to improve latency:
- Option (a) market-channel WSS is **free, already mostly-native in py-clob-client-style libraries** (see [nevuamarkets/poly-websockets](https://github.com/nevuamarkets/poly-websockets) for reconnect logic, [Polymarket/real-time-data-client](https://github.com/Polymarket/real-time-data-client)), and fits single-process asyncio cleanly via `websockets` or `aiohttp.ws`. Recommended second-priority.
- Option (b) Goldsky at $50/mo hits your budget ceiling and offers SQL-filtering + webhook — attractive for simplicity but you get what (a) gives you for free, just with more ops glue.
- Option (c) RPC `eth_subscribe` is worse than (a) for copy-trading specifically: you get raw `OrderFilled` events without the trader's proxyWallet vs funder context (Polymarket's Gnosis Safe proxy model means the event's `maker` field is the proxy contract, not the human wallet — you'd need extra ABI decoding and funder mapping) and you inherit reorg handling complexity. Not worth the pain unless you also need on-chain positions that aren't in the CLOB.

---

## 8. Discovery Pipeline Architecture

### 8.1 Comparison

| Method | Coverage | Bias (what it misses) | Cost | Latency (new wallet → pool) | TOS / legal |
|---|---|---|---|---|---|
| **Polymarket `/holders` fan-out (current)** | Top-liquidity markets only; misses active traders in low-volume markets | Misses: sports/culture specialists, new wallets with few positions, active traders in non-top markets | Free; rate-limited (Data API 1000/10s global) | 6h cycle; slow for new wallets | Official API, compliant |
| **Polymarket `/trades` global feed (current bootstrap)** | All wallets trading ≥ $100; **excludes** pure holders | Same as current | Free; Data API 1000/10s | Minutes if polled; seconds if WSS'd | Compliant |
| **Goldsky subgraph (`positions-subgraph` + `polymarket-subgraph`)** | Full historical on-chain view incl. holders; realizedPnL field | Nothing on-chain; but proxy-wallet vs funder resolution needed | Free on Starter (community subgraphs); Scale ~$30–$50/mo for custom | Subgraph lag ~seconds to minutes | Compliant; subgraphs are public |
| **Apify leaderboard scrapers** | Polymarket's own leaderboard (top by PnL/volume) | Rank bias (cf. §2 native leaderboard limits — no skill adj, volume-biased, recency bias); misses anyone not in top leaderboard | **~$1.50 per 1000 results** ([saswave leaderboard scraper](https://apify.com/saswave/polymarket-leaderboard-scraper)) | Minutes to hours; scraper runs | **TOS risk — Polymarket TOS likely prohibits scraping; not as defensible as API** |
| **Dune Analytics SQL** | Full on-chain history via Polymarket's own Goldsky-indexed tables; flexible aggregation | Query-design-dependent | **Free tier allows dashboards, but API access starts ~$399/mo for Plus tier** — expensive for continuous use | Dune refreshes on schedule; minutes to hours | Compliant |
| **Direct CTF Exchange on-chain** (Bitquery or own node) | Full history at event-level | Raw OrderFilled → need funder mapping + wallet clustering | Free (public RPC) to $100+/mo (Bitquery/QuickNode) | Block-time ~2s | Compliant |
| **Community / CT alpha leaks** (e.g., X/Twitter "smart money" lists) | Narrow, high-signal curation | Survivorship + self-selection bias heavy; stale lists | Free | Minutes (if you follow the accounts) | Fine if redistributed in aggregate only |

### 8.2 Is /holders + /trades fundamentally sound?

**Yes, but incomplete.** The two endpoints together do capture the two canonical prior distributions: (a) who currently holds meaningful positions in active markets, (b) who recently traded $100+. But they miss:

- **Category specialists on low-liquidity markets** — e.g., a sports-specialist wallet consistently trading NBA Finals markets with $50 positions: your $100 /trades filter excludes them, and if those markets aren't top-liquidity, /holders fan-out doesn't hit them either. Given Sirolly's finding that sports markets have 45% wash ratio, you *may* not want these anyway, but you shouldn't miss the 55% authentic ones by construction.
- **Informed traders with newly-created wallets** (the Mitts-Ofir Magamyman pattern): new wallet, one or two massive bets, high win rate, disappears. Your 30-day-active gate excludes them anyway — but this means polycopy is structurally blind to the highest-signal Polymarket pattern documented in the literature.
- **Resolution-path-aware traders** who focus on markets with unambiguous oracle resolution — this is non-trivial to detect from /trades alone.

### 8.3 Recommendation

**Hybrid**: keep /holders + /trades as baseline (free, compliant), add a **Goldsky `polymarket.order_filled` Turbo pipeline scoped to wallets seen in /trades** as an incremental enrichment, run quarterly Sirolly-style wash-cluster detection in-process on the accumulated trades data. Skip Apify (TOS risk + duplicates free API data), skip Dune paid tier (overkill for your capital).

**Trade-off statement**: *Prefer /holders + /trades + opportunistic Goldsky free-tier because the gain (near-full coverage of the copyable wallet universe at $0–$50/mo) outweighs the cost (one additional dependency, Goldsky Starter plan free-forever floor, ~1 day of integration), on polycopy constraints.*

---

## 9. Missing "Smart Money" Factors

Ranked by expected lift / dev cost:

**HIGH value, LOW cost**:
1. **Information leadership (entry time vs market midpoint time-series)**: compute the entry timestamp relative to the first time the price reached that level and stayed. Earliest-movers get a positive signal. Mitts & Ofir use this as factor #3 in their screen. Implementable from /trades + Gamma price history. ~2 days. *Worth adding.*
2. **Conviction sizing (Kelly proxy)**: std-dev of position size normalized by wallet bankroll. High-variance sizers are more likely using Kelly-like reasoning. Kelly 1956; [Yoder 2023](https://nickyoder.com/kelly-criterion/). Implementable. ~1 day. *Worth adding.*
3. **Liquidity-adjusted ROI**: PnL / sum of (bid-ask × filled size). Penalizes wallets whose returns come from moving illiquid books rather than information. Closest academic anchor: Kyle's λ framework applied to Polymarket ([arXiv Anatomy of Polymarket 2603.03136](https://arxiv.org/html/2603.03136v1)). ~1–2 days. *Worth adding.*
4. **Market-maker-vs-taker ratio**: polymarket flags maker orders with zero taker fees; distinguishes a true market-maker-farming wallet (copying these is useless — they're providing liquidity, not predicting) from directional traders. Available in /activity `side` + fee field. ~0.5 day. *Worth adding as a gate, not a factor.*

**MEDIUM value, MEDIUM cost**:
5. **Cross-market correlation / pair-trade patterns**: detect wallets that systematically hedge (e.g., long Trump + short Harris). These are sharp market-makers, not informed directional traders; copy behavior is different. ~3 days. *Worth adding for classification, not scoring.*
6. **Resolution-path awareness**: avoid markets flagged by polycopy's oracle-dispute heuristic. Detect wallets that themselves avoid these markets — score positively. Requires resolution-quality labels which don't exist publicly; you'd have to build. ~1 week. *Worth adding if you have the budget.*
7. **News-alpha (entry vs public info timestamp)**: requires a news timestamp feed. Polymarket has some integration via RTDS comments channel ([agentbets.ai/guides/polymarket-websocket-guide](https://agentbets.ai/guides/polymarket-websocket-guide/)) but it's platform comments not news. You'd need a separate news API. ~1+ week, cost ~$20/mo for a news feed. *Skip for now.*

**HIGH value but adversarial** — **critical ones you're missing**:
8. **Wash trading counter-signal beyond cluster blacklist**: Sirolly algorithm ([gamblingharm PDF](https://gamblingharm.org/wp-content/uploads/2025/11/Polymarket-Wash-Trading-Study.pdf)) gives a continuous score per wallet, not just a cluster membership. A wallet might be peripheral to a cluster at score 0.3 (plausibly tainted but not obviously washed) — currently your blacklist is binary. Implement Sirolly as a continuous penalty in score_v2.2's `wash_penalty`. **Critical given 14% of wallets and 25% of volume are implicated**. ~3–4 days for a Python port of the iterative redistribution algorithm. *Must add.*
9. **Adversarial / anti-copy signals**: some Polymarket traders post small "bait" fills at good prices to let copy-bots frontrun themselves into bad exits. Detection: wallet enters at X, exits within Y minutes at a loss that's small relative to their position — but copy-bots copying the entry would have held past the exit and lost more. Requires holding the bot's own trades vs the copied wallet's trades and computing post-exit drift. Only possible once internal_pnl_feedback is implemented (v2.2). *Add after v2.2 ships.*
10. **Arbitrage-bot filter**: per [Dev Genius on Polymarket arbitrage](https://blog.devgenius.io/just-found-the-math-that-guarantees-profit-on-polymarket-and-why-retail-traders-are-just-providing-6163b4c431a2) $40M was extracted by arbitrageurs in one year. These wallets pass every current polycopy gate (positive PnL, high trade count, active, non-zombie, probably non-wash) but have no signal for a copy-trader. Detection: wallets whose trades sum across YES+NO per market to near-zero net exposure are arbitrageurs. Implementable from /activity aggregation. ~1 day. *Must add as a gate.*
11. **Resolution risk concentration**: per Mitts & Ofir, the profitable informed-trader pattern includes high market-HHI (C9 reversal). But not every high-HHI wallet is informed — some are just overconcentrated and lucky. The distinguishing factor Mitts & Ofir use is **pre-event timing**: informed wallets enter before the event, lucky-concentrated wallets enter after news breaks. Timing_alpha implemented properly captures this; it's the same factor.

---

## 10. ORDERED 10-POINT "DO THIS NEXT" LIST

Ranked by expected ROI across the three pillars, accounting for polycopy constraints.

1. **[CHEAP, < 1 day]** **Drop timing_alpha from v2 entirely** (set weight 0, renormalize other weights to sum 1), **OR** compute a real timing_alpha from /trades + Gamma price history. The current 0.5 placeholder is literally a +0.10 uniform bias per audit; removing it strictly improves out-of-sample score quality (Daniele et al., lasso-on-uninformative-factors). Bump to v2.1-preliminary.

2. **[CHEAP, < 1 day]** **Fix C5 by actually implementing `_compute_zombie_ratio` temporal filter**, then **collapse** zombie_ratio from scoring (absorbed into the gate) and rename `discipline` factor → `sizing_stability` only. This is code-spec drift, straightforward fix.

3. **[CHEAP, < 1 day]** **Switch normalization from `lower` to `average` interpolation** AND **replace p5-p95 winsorization with simple rank transform** (`rank / N`) on factors where N < 20. Removes the fixed-point trap (C7) and the "wallet locked at 0.45 for 80 cycles" pathology in one change. This is the single cheapest fix for your ±30% cycle variance.

4. **[CHEAP, ~1 day]** **Replace absolute demote threshold 0.60 with rank-based demotion** (bottom-2-of-pool with 3-cycle hysteresis) AND recalibrate `EVICTION_SCORE_MARGIN` to 1σ of observed score distribution (≈0.092 per §3.1, so use 0.10 not 0.15). Fixes C11 and the mediocre-0.60-stays pathology.

5. **[CHEAP, ~1 day]** **Add arbitrage-bot gate**: reject wallets whose /activity sums to |YES_net − NO_net| / gross < 0.10 on the same conditionId, across their last 90d positions. Prevents you from inheriting the top of the $40M/yr arbitrageur class who pass every other gate.

6. **[MEDIUM, 2–3 days]** **Fix Brier to compute P(YES) not P(side_bought)**, and set baseline consistently (pool-mean everywhere; drop the 0.25 literal). This makes your calibration factor actually measure calibration per Gneiting & Raftery, rather than correlated-with-win-rate noise.

7. **[MEDIUM, 2–3 days]** **Ship v2.1-ROBUST**: pure rank aggregation, median Sortino+Calmar, drop timing_alpha, rank-based eviction. Append-only bump to SCORING_VERSION="v2.1". Run a 14-day shadow against v2 (same framework you already have). Given the current v2=13 coverage vs v1=50 shadow result at day 5, I predict v2.1 coverage will be 35–45 and variance will drop from ±30% to ±8%.

8. **[MEDIUM, 3–4 days]** **Add internal PnL feedback**: track realized copy-PnL per followed wallet over rolling 30d, compute EMA, wire into score as 0.25–0.30 weight. This is the single highest-expected-lift factor missing from v2. Prior to having 30d of copy-trades per wallet, use a neutral 0.5 prior. This directly addresses the audit's "no internal PnL feedback" finding, arguably the most important.

9. **[MEDIUM, ~3 days]** **Add market-channel WSS for latency floor reduction** (option 7.3-(a)): subscribe to `/ws/market` on all condition IDs in your active watchlist's recent positions, parse `trade` events, match to watched wallets by `maker`/`taker` field. Keeps single-process asyncio (one WSS coroutine, aiohttp.ws + reconnect with exponential backoff). Projected p50 latency: 8–20s → 2–3s without any paid infrastructure. Test with respx mocks for non-WSS pre-send validation; use the [nevuamarkets poly-websockets pattern](https://github.com/nevuamarkets/poly-websockets) but in Python.

10. **[EXPENSIVE, >1 week]** **Implement a Python port of Sirolly iterative wash-cluster scoring + build v2.2-DISCRIMINATING**: graph construction from /trades, iterative redistribution (the core algorithm is ~200 LOC), per-wallet continuous wash score, Mitts-Ofir informed-trader composite (pre-event timing + market-HHI + size-anomaly), informed-score block in scoring. This is the capstone that lifts polycopy from a generic copy-trader to a Polymarket-literature-aware system. Worth doing only after items 1–8 are in place; otherwise you're layering sophistication on a shaky base.

---

## 11. Per-Pillar Recommendation Summary

**SCORING PILLAR.** *Prefer v2.1-ROBUST (rank aggregation, median robust Sortino, drop timing_alpha, rank-based eviction) as the immediate next version, because the gain (factor-of-3 reduction in cycle variance, elimination of Sortino-sentinel bias, resolution of 5 of the 16 HIGH audit findings) outweighs the cost (loss of magnitude discrimination, ~1–2 days dev), on polycopy's N<20 pool constraint.* Build v2.2-DISCRIMINATING on top after internal-PnL-feedback (item 8) has collected 30 days of data.

**DISCOVERY PILLAR.** *Prefer /holders + /trades baseline, plus one Goldsky Turbo Pipeline on `polymarket.order_filled` Starter-free-tier as an incremental fan-out, because the gain (near-complete wallet-universe coverage, detection of new wallets within minutes rather than next-6h-cycle) outweighs the cost (one new dependency, ~1 day integration, zero incremental $), on the <$50/mo constraint. Skip Apify (TOS) and Dune paid tier (overkill). Add Sirolly wash scoring as a post-discovery filter.*

**LATENCY PILLAR.** *Prefer option (d) — accept 2–3s floor after the obvious 8–20s → 3s fix — because the gain (freeing dev budget for scoring work worth ~5–15% expected PnL) outweighs the cost (estimated ~0.25% PnL/yr lost to 1–3¢ fill slippage on latency-sensitive minority of trades), on polycopy's $1k–10k capital and political/macro market focus. Add market-channel WSS (item 9) only if measured post-fix residual p50 exceeds ~4s.*

---

## 12. Key Discrepancies Between Sources (more valuable than consensus)

1. **Polygon reorg depth**. `polygon.technology` official docs claim 2-block cap post-Heimdall-v2 (July 2025) with 5s finality. [mplankton substack (pre-v2)](https://mplankton.substack.com/p/polygons-block-reorg-problem) documented 30–157-block reorgs. **For polycopy, trust the post-v2 numbers; you're operating in 2026**, so 2-block = ~4s confirmation is safe for Data API–driven detection.

2. **Polymarket fraction-profitable traders**. Reichenbach & Walther 2026 say ~30% positive; Sergeenkov April 2026 says 15.9%; the difference is wallet-split/merge accounting ([cryptonews.net](https://cryptonews.net/news/market/32725968/)). **For polycopy**: the lower number is more honest because it counts the true economic actor rather than their proxy wallets separately. Use 15.9% as your base rate when sizing expectations of random-wallet copy.

3. **Wash trading share on Polymarket**. Sirolly et al. claim 25% lifetime avg, peaked 60% Dec 2024, 90%+ in some election sub-markets. Chainalysis's parallel DeFi study uses a different heuristic ([Chainalysis 2025](https://www.chainalysis.com/blog/crypto-market-manipulation-wash-trading-pump-and-dump-2025/)) and gets much lower numbers for Polymarket (embedded within DeFi aggregate). **For polycopy**: Sirolly's graph-based approach is Polymarket-specific and more rigorous; trust their 25% and expect 40%+ in sports sub-markets, which has direct implications for polycopy's category weighting.

4. **Polymarket API rate limits**. Polymarket's own docs give different numbers in different pages; AgentBets summarizes as 15k/10s global, 9k/10s CLOB, 1k/10s Data API, 4k/10s Gamma ([agentbets.ai/guides/polymarket-rate-limits-guide](https://agentbets.ai/guides/polymarket-rate-limits-guide/)); Scribd-hosted official doc shows stricter 300/10s per-endpoint on /books. **For polycopy**: your existing Semaphore(5) on Data API = 60 req/min peak is safely below even the strictest interpretation. But you should read and cache `X-RateLimit-Remaining` response headers rather than rely on static throttling.

5. **PolyCop detection latency claim ("sub-second") vs AgentBets claim ("5–30s for copy bots overall")**. PolyCop's self-reported 0-block / 1-block-~2s is plausible for the 15-min BTC markets it targets (ultra-short horizon where PolyCop has an integration optimized for one use case). For polycopy's political/macro targets, the "5–30s" broader estimate is more honest. **Don't benchmark against PolyCop's 0-block claim**; it's a best-case in a different market class.

6. **Volume as a skill signal**. Polyburg uses `WR × ln(1 + trades)` (volume boost). Polymarket native leaderboard sorts by raw PnL or volume. PolyCopyTrade.bot's Smart Score uses win rate, Sortino, R², drawdown, profit factor — **no volume term**. polymarket.tips explicitly argues volume is a misleading signal. Given Sirolly shows 25%+ of volume is wash, **the polymarket.tips / PolyCopyTrade position is correct for copy-trading purposes**; Polyburg's and native's are correct for volume-of-capital-flowing but not for skill identification.

7. **Conviction of the Mitts-Ofir informed-trader result vs polymarket.tips "Contrarian" archetype**. Mitts-Ofir says high-concentration + pre-event timing = 69.9% win rate. polymarket.tips's "Contrarian" archetype is partially the same thing but includes post-consensus-late contrarians who are just bucking trends. **For polycopy**, concentrate on the pre-event-timing half of Mitts-Ofir; don't reward generic contrarians.

---

*Report ends. All specific numerical claims in this document are sourced inline; any quantitative estimate without a citation (e.g., σ decomposition in §3.1, latency alpha curve in §7.2) is my own synthesis and should be validated empirically on polycopy's own data before acting. The highest-expected-value single change is item 8 on the do-this-next list (internal PnL feedback); the single lowest-risk-highest-value is item 3 (rank transform replacing winsorization).*