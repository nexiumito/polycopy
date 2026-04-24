**Polymarket Copy-Trading Metrics & Latency Benchmarks (Jan 2025–Apr 2026\)**

**Executive summary**

* Top Polymarket “smart money” wallets show headline win rates from roughly 60–95%, but deep audits of top whales find true win rates closer to 50–60% once thousands of unclosed “zombie” losses are included, undermining naive win-rate filters for wallet scoring.\[1\]\[2\]\[3\]\[4\]

* Columbia’s 2025 wash‑trading study estimates about 25% of historical Polymarket volume as wash trades, peaking near 60% of weekly volume in December 2024 and still around 20% by October 2025, so copy‑trading discovery pipelines must aggressively exclude wash clusters and sub‑cent churn.\[5\]\[6\]\[7\]\[8\]\[^9\]

* Public docs put Polymarket’s Data API caps at 1,000 requests per 10 seconds overall (200 /trades, 150 /positions), and Goldsky’s public subgraphs at 100 requests per second per IP, which together bound scalable polling‑based wallet tracking to at most hundreds of wallets per 10‑second window without WebSockets.\[10\]\[11\]

* Realistic end‑to‑end execution latency on Polymarket’s CLOB for production systems clusters between \~250–350 ms for taker orders from well‑placed VPS locations (Dublin/Frankfurt), with independent measurements reporting 250–300 ms taker matching and 268 ms round‑trip latencies, making anything below \~200 ms difficult without privileged infra.\[12\]\[13\]\[14\]\[15\]

* Dynamic taker fees rolled out in late March 2026 with peak effective rates up to 1.80% on high‑velocity crypto markets and 0.75–1.50% on sports, finance, politics, and other categories, meaning copy‑trading strategies must now explicitly budget for fee drag that was absent in the prior zero‑fee era.\[16\]\[17\]\[18\]\[19\]

**Group A — Empirical scoring metrics**

**A1 — Smart‑money win rates**

* Case studies of top Polymarket traders (LucasMeow, tsybka, BAdiosB) in a February 2026 performance review show win rates of 94.9%, 85.9%, and 90.8% respectively, all with six‑figure realized PnL.\[^1\]

* The Struct Explorer “Top Polymarket Traders” dashboard lists multiple seven‑figure PnL wallets with win rates between roughly 60–100%, for example GA GamblingIsAllYouNeed at 78.9% over 58,370 markets and BetMechanic at 85.4% over 11,435 markets as of February–April 2026.\[^2\]

* PANews’ January 2026 analysis of 27,000 trades by the top 10 whales finds that after including thousands of unclosed losing “zombie orders”, the headline 73.7% win rate of wallet “SeriouslySirius” falls to a true win rate of 53.3%, indicating that many smart‑money wallets sit only slightly above coin‑flip accuracy when measured honestly.\[3\]\[20\]\[^4\]

* A Reddit analysis of 1,297 “real” (non‑bot) Polymarket traders in January 2026 reports systematic “zombie positions” left near zero that keep win rates artificially high, again implying that naive win‑rate screens are materially biased upward.\[^21\]

* No large‑sample, platform‑wide distribution (e.g., median, p90 win rate for top‑100 wallets by realized PnL) has been published as of April 2026; existing sources are cohort‑ or case‑study‑based only.\[22\]\[23\]

**A2 — Sortino ratios, Brier scores, calibration thresholds**

* Polymarket’s official accuracy page (April 2026\) reports an overall Brier score of 0.0641 across resolved markets when measured at fixed horizons (1 month to 4 hours before resolution), with 90.4% accuracy one month out and 96.7% four hours before resolution, indicating very strong market‑level calibration.\[^24\]

* An October 2025 analysis by Alex McCullough cited on Marginal Revolution reports a Polymarket Brier score of 0.0581 at a 12‑hour horizon, explicitly labeling Brier scores below 0.125 as “good” and below 0.1 as “great”, putting Polymarket’s aggregate calibration in the “excellent” range.\[^25\]

* A February 2026 third‑party accuracy audit across 2,847 Polymarket markets finds segment‑level Brier scores between roughly 0.16 and 0.20 depending on category (politics, sports, crypto, entertainment), with an overall Brier score of 0.187 and political markets at 81% accuracy.\[^26\]

* Convexly’s 2026 Polymarket Wallet Analyzer defines an individual‑wallet calibration threshold as “edge” when a wallet beats a naive base‑rate baseline Brier score by at least 0.05 over 50+ resolved positions; wallets that do not beat this threshold are treated as uncalibrated even if profitable.\[^27\]

* No public distribution of wallet‑level Sortino ratios on Polymarket exists; commercial tools like PolyVision and Convexly expose per‑wallet Sharpe/Sortino and max drawdown via APIs but do not publish aggregate histograms or explicit “good vs random” cutoffs beyond the Brier‑skill threshold above.\[28\]\[29\]

**A3 — Zombie capital in sub‑2‑cent positions (zombie\_ratio)**

* PANews’ top‑10‑whale audit documents that wallet “SeriouslySirius” held 2,369 open orders versus 4,690 settled, with 1,791 open orders already total losses but left unclosed, implying thousands of positions priced near zero that artificially preserve a 73.7% reported win rate which drops to 53.3% once zombies are counted.\[20\]\[4\]\[^3\]

* Multiple analyses (PANews, Reddit) emphasize that high‑Pnl whales often keep clearly lost positions unredeemed to avoid crystallizing losses in leaderboard stats, but none quantify a formal “zombie\_ratio” distribution (capital or count share of sub‑2‑cent positions) across the top‑200 wallets.\[21\]\[3\]\[^20\]

* Columbia’s 2025 wash‑trading study and follow‑on coverage highlight clusters of 43,000+ wallets transacting almost exclusively in sub‑cent markets, contributing nearly 1 million dollars of volume with little or no net profit, but the focus is on wash‑trade loops rather than long‑lived zombie holdings.\[8\]\[9\]\[^5\]

* No public dashboard or paper as of April 2026 publishes a numeric distribution of zombie capital across leading wallets or an explicit zombie\_ratio threshold to flag manipulation; this metric remains bespoke to private tools.\[23\]\[30\]

**A4 — Prevalence of wash trading on Polymarket (2025–2026)**

* The Columbia University paper “Network‑Based Detection of Wash Trading” (November 6, 2025\) estimates that roughly 25% of Polymarket’s historical trading volume between 2022 and mid‑October 2025 is likely wash trading, with suspicious weekly volume peaking near 60% in December 2024.\[31\]\[7\]\[5\]\[8\]

* The same study reports that wash trading fell to about 5% of total volume in May 2025 before rising again to around 20% by October 2025, indicating that manipulation is highly time‑varying and sensitive to airdrop and incentive rumors.\[6\]\[7\]\[^31\]

* Researchers flag approximately 14% of all trading wallets as suspicious, including a single cluster of more than 43,000 wallets responsible for nearly 1 million dollars in mostly sub‑cent trades and rapid pass‑through USDC transfers.\[9\]\[31\]\[^8\]

* Follow‑up coverage in outlets such as Fortune, CryptoNews, and Coin360 reiterates the 25% average wash‑trade estimate and 60% December 2024 peak; no peer‑reviewed replication or strong refutation has been published as of April 2026.\[7\]\[32\]\[5\]\[6\]\[^9\]

* No newer 2026‑dated academic work refining the 25% figure specifically for Polymarket has surfaced; later prediction‑market volume overviews continue to quote Columbia’s numbers when discussing wash trading on the platform.\[^33\]

**A5 — Commercial scoring formulas and disclosed weights**

* Polycopybot’s Polycop bot reports that its AI wallet scoring evaluates every active Polymarket wallet on 14 signals grouped into four dimensions: return quality (absolute ROI, win rate, Sharpe ratio), calibration accuracy (position size vs implied edge), behavioural consistency, and market selection, and that under 3% of active wallets pass all thresholds at any time; however, the product explicitly does not publish numeric weights or per‑signal coefficients.\[34\]\[35\]\[^12\]

* PolyVision describes its copy‑trading score (1–10) as a weighted composite of track‑record length, risk‑adjusted returns (Sharpe, Sortino, max drawdown), win rate consistency, recent performance, position sizing discipline, and “loss hiding” red flags, with hard caps limiting scores for small sample sizes and aggressive risk, but again no exact weight vector is disclosed.\[36\]\[29\]\[^28\]

* Convexly’s Edge Score is described as a composite fitted on 8,656 wallets using calibration (Brier score), sizing discipline, and concentration risk against signed log PnL, and the team claims to publish methodology, coefficients, and underlying data in a 10k‑wallet calibration study, making it one of the rare examples with explicit statistical weights, though those coefficients are hosted in their own paper rather than in simple product docs.\[37\]\[27\]

* PolyCopyTrade marketing emphasizes “multi‑factor trader verification” and execution speed \<100 ms versus 500 ms+ for generic bots, but its public documentation and press releases do not reveal any scoring formula or weights beyond listing standard metrics such as PnL, win rate, and volume used for trader selection.\[38\]\[39\]\[40\]\[41\]

* Bullpen Fi, AgentBets, and “Polymarket Alpha” tooling focus on terminal features, alerts, or open‑source strategy bots; available guides and repositories describe what metrics to watch (e.g., volume, PnL, category specialization) but none publish an explicit, weighted scoring formula for wallet ranking as of April 2026.\[42\]\[43\]\[44\]\[45\]\[46\]\[47\]

**Group B — Discovery pipeline performance**

**B1 — Documented Polymarket Data API rate limits (as of early 2026\)**

* Polymarket’s official Rate Limits documentation (last updated February 22, 2026\) sets Data API general limits at 1,000 requests per 10 seconds, with specific caps of 200 requests per 10 seconds for /trades, 150 per 10 seconds for /positions, 150 per 10 seconds for /closed-positions, and 100 per 10 seconds for /ok, all enforced via Cloudflare throttling.\[^10\]

* The same doc specifies Gamma API limits (e.g., 4,000 requests per 10 seconds general, 500 /events, 300 /markets, 900 combined markets/events listings), and core CLOB market‑data endpoints (e.g., /book, /price, /midpoint, /prices-history) at up to 1,500 requests per 10 seconds per instrument type, bounding how aggressively a discovery pipeline can poll without WebSockets.\[48\]\[49\]\[^10\]

* Goldsky’s public Polymarket subgraphs expose order‑book and position data via GraphQL with a separate rate limit of 100 requests per second per IP (equivalent to 1,000 every 10 seconds), matching the order of magnitude of Polymarket’s own Data API caps but via a different infra path.\[^11\]

* Polymarket’s builder Tiers page (April 1, 2026\) clarifies that Daily Relayer transaction limits and API rate limits differ by account tier, with unverified accounts capped at 100 relayer transactions per day and verified accounts at 10,000 per day, though per‑endpoint non‑relayer limits remain “Standard” for most bot developers.\[^50\]

**B2 — Endpoint latency (REST)**

* A VPS provider’s April 2026 latency guide reports typical network latencies from a Dublin trading VPS to key Polymarket endpoints as 80–100 ms for [api.polymarket.com](http://api.polymarket.com) (metadata), 25 ms for maker‑side CLOB orders, and 250–300 ms for taker orders, with \~90–100 ms wire‑speed latency on the ws-subscriptions-clob WebSocket endpoint.\[^15\]

* A March 2026 Reddit thread on “Fastest trades you’re getting to Polymarket CLOB” reports a best observed 268 ms round‑trip latency for fill‑or‑kill orders (request to confirmation) and around 20 ms for denied requests, from an optimized bot with microsecond‑level internal processing.\[^14\]

* Polycopybot’s own benchmarks quote 50–100 ms from WebSocket event to detection inside the bot and 340 ms average end‑to‑end execution latency from event to confirmed on‑chain broadcast, implying that REST/API and chain latencies together dominate over internal compute for well‑written Python or TS code.\[13\]\[35\]\[51\]\[12\]

* No official Polymarket documentation publishes p50/p95 latencies per REST endpoint by region; available numbers come from third‑party latency tests and commercial bot benchmarks only.\[48\]\[15\]

**B3 — Active wallet universe and smart‑money subset**

* CryptoSlate and Yahoo Finance cite Dune data showing Polymarket monthly active traders of about 67,130 in September 2024, 241,000 by June 2025, and a peak of roughly 455,000 active traders in January 2025, reflecting rapid growth around the 2024 U.S. election cycle.\[52\]\[53\]

* A December 2025 analysis by DeFi Oasis (reported by Yahoo Finance) estimates that more than 1.7 million trading addresses have used Polymarket historically, with around 70% of those addresses realizing net losses and less than 0.04% of addresses capturing over 70% of total realized profits (\~3.7 billion dollars).\[^54\]

* A March 2026 article on wallet analytics tools claims a database of “7M+ Polymarket wallets across 80+ performance metrics”, implying that total addresses with any observable Polymarket interaction (including zero‑trade or passive) now exceed 7 million, though this includes inactive and non‑trading addresses.\[^30\]

* A Reddit guide to tracking high‑probability wallets mentions “more than 840,000 active wallets” as the rough crowd size from which genuine skill must be filtered, but does not define the measurement window (e.g., 90‑day vs. all‑time activity).\[^55\]

* No public Dune dashboard or Polymarket doc as of April 2026 gives a precise count of wallets with ≥1 trade in the prior 90 days, nor a platform‑official count of “smart money” wallets defined as ≥50 resolved markets plus positive cash PnL; public “smart money” lists (e.g., Polymarket’s own 26‑address list) and commercial tools (Polycop’s ≈200 tracked wallets, PolyVision’s coverage) are partial views only.\[56\]\[12\]\[^28\]

**B4 — Known wash‑trade or Sybil clusters**

* The Columbia wash‑trading paper and summaries identify a large wash cluster of more than 43,000 wallets generating nearly 1 million dollars of volume, mostly in markets priced below one cent, with 94.1% of trades occurring within cluster and negligible net profit, strongly suggesting coordinated wash behaviour.\[5\]\[8\]\[^9\]

* Crypto Bubble Tea’s November 2025 blog post enumerates “Level 1” and “Lander Network” wash patterns, including a network of 109,000 accounts trading 932.7 million shares and 79.9 million dollars of volume, 94.1% of which was intra‑cluster, with a collective loss of just 64,160 dollars, consistent with volume‑maximizing wash activity.\[^57\]

* Several overviews of the Columbia study mention that sports and election markets suffered wash‑trade peaks above 90% of weekly volume in specific weeks, underscoring that some categories are far more polluted than others.\[31\]\[7\]\[^9\]

* No official Polymarket‑maintained public blacklist or Sybil cluster registry exists as of April 2026; cluster data is instead surfaced via independent analytics tools like Polyloly’s Louvain‑based “whale syndicate” detector, which displays coordinated wallet clusters but does not label them explicitly as wash traders.\[^58\]

**B5 — Alternative data sources: cost, latency, freshness**

* Goldsky’s Polymarket integration offers public GraphQL subgraphs (orderbook, open interest, user positions) with a documented rate limit of 100 requests per second per IP (1,000 per 10 seconds) and emphasizes that Turbo Pipelines provide real‑time streaming of Polymarket datasets, including all historical data; public subgraphs are free within the Starter plan’s generous free tier.\[59\]\[60\]\[^11\]

* Goldsky’s pricing page states that the Starter plan is free (no card) and includes three always‑on subgraphs and 100,000 entities free, with Scale (pay‑as‑you‑go) charging roughly 0.05 dollars per worker‑hour beyond 2,250 hours and about 4 dollars per 100,000 additional entities stored, indicating that a single Polymarket PnL subgraph stays in the free tier at modest scale.\[60\]\[59\]

* Apify’s “Polymarket Leaderboard Scraper” lists pricing from 1.50 dollars per 1,000 leaderboard results (no proxies), returning for each wallet: proxy wallet address, username, volume, profit, time range filters (day/week/month/all), and category filters; another Apify scraper variant prices at 1.49 dollars per 1,000 full trader profiles with profit/loss, volume, positions, and recent trades.\[61\]\[62\]\[^63\]

* Dune Analytics offers a free tier with 2,500 credits per month and a 120‑second timeout on the smallest query engine, and paid tiers from 45 dollars/month (Analyst, 4,000 credits) to 849 dollars/month (Premium, 100,000 credits) with API export limits of 40–1,000 calls per minute depending on tier, so typical Polymarket dashboards used for discovery will execute in seconds but are bounded by these credit budgets.\[64\]\[65\]\[66\]\[67\]

* PolyVision exposes a REST API and MCP skill that returns per‑wallet scores, win rates, Sharpe/Sortino ratios, and red‑flag analysis, with a free API key tier and “no daily limits, 6‑hour result caching” advertised, implying effectively zero marginal cost at moderate query volume but only 4–6‑hour freshness versus Polymarket’s live APIs.\[68\]\[29\]\[^28\]

**Group C — Latency benchmarks for automation**

**C1 — End‑to‑end latency claims by commercial bots**

* Polycopybot’s Polycop bot repeatedly claims an average end‑to‑end latency of 340 ms from WebSocket signal detection to confirmed on‑chain execution, with 95th‑percentile latency around 680 ms, and 1.2% median fill deviation from the source wallet’s price across live trades; these figures are documented in blog posts dated March 18 and April 14, 2026.\[35\]\[51\]\[12\]\[13\]

* The same Polycop marketing material also summarizes its latency components as 50–100 ms for WebSocket event detection versus 5–30 seconds for HTTP polling, and 340 ms total pipeline time from detection to confirmation when using dual‑node infra in Frankfurt and Singapore.\[12\]\[13\]\[^35\]

* A Polycop‑related press release and Telegram‑bot promo page state that “execution lands in under 2 seconds on Polygon — 30% of the time it’s instant”, indicating that even when latency spikes, the common‑case remains below 2 seconds.\[69\]\[70\]

* Polymarket Copy Trade / PolyCopyTrade marketing contrasts its execution speed of “\<100 ms” with “500 ms+” for generic copy‑trading bots, but does not provide independent measurement methodology or p95 figures; the \<100 ms claim refers to bot‑side processing rather than blockchain confirmation times.\[40\]\[38\]

* Other commercial content (QuickNode, third‑party how‑to guides) generally describe well‑tuned bots executing in “under 200 ms” from signal detection to order submission, with total latency constrained by the 250–300 ms taker matching time reported by VPS benchmarks; no other vendor besides Polycopybot publishes a full breakdown.\[71\]\[72\]\[^15\]

**C2 — CLOB order post and matching latency**

* TradoxVPS’s April 2026 latency guide reports that from a Dublin VPS, maker orders to [clob.polymarket.com](http://clob.polymarket.com) see approximately 25 ms end‑to‑end latency under normal load, while taker orders experience 250–300 ms latency for matching and confirmation, with WebSocket market‑data updates arriving in roughly 90–100 ms.\[^15\]

* Community testing on Reddit notes best‑case 268 ms round‑trip latency for filled or killed orders and about 20 ms for denied requests, with internal processing times between 20 and 260 microseconds once a trade is detected, suggesting that network and matching engine dominate total latency.\[^14\]

* Polycopybot’s 340 ms average end‑to‑end latency measured “from WebSocket event receipt to on‑chain broadcast confirmation” is consistent with these independent network measurements, reinforcing that \~250–350 ms is the realistic floor for complete detection→order→confirmation cycles via public infra.\[13\]\[15\]

* No official Polymarket engineering blog currently publishes detailed p50/p95 matching engine latencies per region or endpoint beyond the VPS‑style measurements above.\[73\]\[48\]

**C3 — Matching‑engine region and colocation**

* TradoxVPS explicitly recommends Dublin as the fastest Polymarket VPS location, reporting 25 ms maker and 250–300 ms taker latency to the [clob.polymarket.com](http://clob.polymarket.com) endpoint and 90–100 ms to ws-subscriptions-clob, implying that the matching engine is hosted in or near an EU‑West region.\[^15\]

* Other latency guides (QuantVPS, developer blogs) broadly advise selecting VPS regions close to major Polygon RPC nodes and Polymarket infra (e.g., Dublin, Frankfurt) to shave tens of milliseconds off round‑trip times, but none provide exact host locations or IPs for colocation.\[74\]\[71\]

* There is no public evidence as of April 2026 that Polymarket offers formal colocation or cross‑connect services; all published advice targets cloud VPS proximity rather than data‑center‑level colocation.\[73\]\[15\]

**C4 — Taker fees and dynamic fee rollout**

* KuCoin’s March 24, 2026 trading‑fee explainer reports that Polymarket transitioned from a zero‑fee model to a dynamic taker‑fee system, with peak effective rates up to 1.80% on high‑velocity 15‑minute crypto markets and maker rebates funded entirely from collected taker fees.\[^16\]

* A FinanceFeeds article on April 1, 2026 notes that after a March 30 dynamic‑fee rollout, Polymarket’s fee revenue jumped from 560,000 dollars to over 1 million dollars on April 1, confirming that fee collection is now a material revenue source.\[^17\]

* MEXC coverage from March 23, 2026 details that as of March 30, 2026 Polymarket is expanding taker fees to eight additional market categories (Finance, Politics, Economics, Culture, Weather, Other/General, Mentions, Tech) on top of existing Crypto and Sports, with peak effective rates of 1.50% (Economics), 1.25% (Culture, Weather, Other/General, Mentions), 1.00% (Finance, Politics, Tech), 1.80% (Crypto), and 0.75% (Sports).\[^18\]

* Polymarket’s help center and fee‑schedule docs for its U.S. designated contract market note a Taker Fee Coefficient of 0.05 (maximum 1.25 dollars per 100 contracts at 0.50 dollars price) and a Maker Rebate Coefficient of 0.0125, with temporary 50% taker rebates on all markets through April 30, indicating a different but related fee regime on the regulated U.S. venue.\[75\]\[19\]

* No public evidence indicates that the dynamic fee model is applied retroactively to pre‑March‑2026 markets; instead, fees are rolled out by category and creation date, which copy‑trading systems must respect when estimating expected net edge.\[19\]\[18\]\[^16\]

**C5 — WebSocket market channel latency**

* PolytrackHQ’s December 2025 WebSocket tutorial states that the CLOB WebSocket (wss://ws-subscriptions-clob.polymarket.com/ws/market) is intended for real‑time order‑book streaming and that the service supports up to 500 instruments per connection, but does not provide explicit millisecond‑level latency metrics.\[^76\]

* Polycopybot’s internal measurements attribute 50–100 ms of its 340 ms end‑to‑end latency budget to WebSocket event detection, implying that from a well‑located VPS the additional delay from the matching engine to the bot process is on the order of tens of milliseconds.\[35\]\[12\]\[^13\]

* TradoxVPS’ endpoint table reports 90–100 ms latency for the ws-subscriptions-clob endpoint from Dublin, consistent with these application‑level measurements and with the idea that WebSocket propagation adds roughly \~100 ms on top of matching time for market data.\[^15\]

* No Polymarket doc or status page exposes p50/p95 message‑propagation latencies or jitter for the CLOB WebSocket, so developers rely on third‑party measurements and their own benchmarks.\[77\]\[76\]

**C6 — WebSocket user channel latency and stability**

* Official WebSocket quickstart docs (October 15, 2025\) describe both market and user channels on the CLOB WebSocket, showing sample Python code that uses heartbeats every 10 seconds and automatic reconnect loops, but they do not include quantified stability metrics (e.g., reconnects per hour).\[^77\]

* The Go SDK’s ws package and multiple third‑party bot guides emphasize automatic reconnection, heartbeat pings, and resilient error handling for user event streams, implying that intermittent disconnects are expected but manageable, yet again provide no numeric outage or reconnection rate data.\[78\]\[72\]

* No public benchmark as of April 2026 reports measured reconnect frequency or message‑loss rates for the user channel; all available information is qualitative, so copy‑trading bots must instrument their own metrics for production reliability.\[76\]\[77\]

**Group D — Recent Polymarket infrastructure changes**

**D1 — API and infra changes (last 12 months)**

* The Polymarket Rate Limits and Market‑Data docs, updated February 22, 2026, standardize explicit numerical caps across Gamma, Data API, and CLOB endpoints (e.g., 1,000 Data API requests per 10 seconds, 1,500 /book and /price requests per 10 seconds, and per‑verb CLOB order limits such as 1,000 POST /orders per 10 seconds and 25 relayer /submit requests per minute), making historical “soft limits” explicit and more constraining for high‑throughput bots.\[^10\]

* Matching‑engine restart docs (also February 22, 2026\) formalize a weekly restart every Tuesday at 7:00 AM ET, typically lasting \~90 seconds and returning HTTP 425 (Too Early) on order endpoints, which bots must now explicitly handle with exponential backoff rather than treating as hard failures.\[79\]\[73\]

* WebSocket documentation and tutorials published in late 2025 (e.g., WSS Quickstart, Polytrack tutorial) formalize the CLOB vs RTDS split, introduce market and user channels with specific subscription payloads, and recommend keeping a single long‑lived connection per bot with ping heartbeats, which strongly pushes discovery pipelines toward WebSocket rather than REST polling.\[77\]\[76\]

* The Clients & SDKs page (April 16, 2026\) lists official open‑source clients in TypeScript, Python, and Rust, all supporting full CLOB API coverage including market data, order management, and WebSockets, and effectively deprecating ad‑hoc HTTP wrappers for new integrations.\[^80\]

* Dynamic taker fees rolled out starting in late March 2026, first to Crypto and Sports and then to eight additional categories (Finance, Politics, Economics, Culture, Weather, Other/General, Mentions, Tech) as of March 30, 2026, representing a major structural change from the prior zero‑fee trading model.\[17\]\[18\]\[^16\]

* Polymarket’s builder “Blockchain Data Resources” page (January 20, 2026\) officially blesses Dune, Goldsky, and community dashboards as canonical off‑chain data sources, listing key Dune queries for volume, TVL, open interest, and historical accuracy, which many independent tools now rely on rather than direct chain indexing.\[^23\]

* A KuCoin report from April 22, 2026 notes that Polymarket delayed migration to CLOB V2, a new pUSD collateral token, and a rebuilt matching engine by at least one week due to integration concerns, highlighting an upcoming infra change that bots will need to adapt to once deployed.\[^81\]

**D2 — Outages, data delays, and incident reports (2025–2026)**

* Polymarket’s public status page logs a March 17, 2026 incident where the CLOB API suffered a partial outage from 8:06 PM to 8:21 PM (15 minutes) before being resolved, with the main status page otherwise showing 100% uptime for CLOB API, Markets API, and Polygon RPC over the prior 90 days.\[82\]\[83\]

* A 2025 incident described in a Polyloly trading‑terminal article recounts an automated market‑maker bot bleeding 420,000 dollars over four minutes due to a decimal‑shift error quoting YES at 10 dollars instead of 0.10, highlighting that logic bugs can have catastrophic impact even in the absence of formal outages.\[^84\]

* Community experiments in early 2026 tracking how quickly markets react to news (e.g., a Reddit “31‑minute lag” test) report that across 14 manually logged events, Polymarket markets moved on average 31 minutes after Reuters/Bloomberg headlines (range 8–54 minutes), suggesting that informational latency remains much larger than infra latency and offering a window for news‑driven bots.\[^85\]

**D3 — Polymarket SDK maturity (py-clob-client and others)**

* The official Python client py-clob-client shows active maintenance: the GitHub repository lists 70 releases with the latest release v0.28.0 dated October 22, 2025, 279 stars, 106 forks, and 8 contributors as of the current snapshot, indicating sustained community adoption.\[^86\]

* The same repo’s issue tracker includes a February 26, 2026 issue (“Service is not ready” \#286) where users reported order placement failures during matching‑engine restarts; maintainers clarified this as expected behaviour tied to restarts and closed the issue, showing active triage and alignment with new restart docs.\[79\]\[73\]

* Polymarket’s Clients & SDKs documentation (April 16, 2026\) references official TypeScript (@polymarket/clob-client), Python (py-clob-client and py-clob-client-v2), and Rust clients, all supporting the full CLOB API and WebSocket integration, and a Go WebSocket SDK for higher‑level connection management, suggesting that SDK support is now broad and multi‑language.\[87\]\[88\]\[78\]\[80\]

**D4 — Regulatory changes affecting accessibility and bots (2025–2026)**

* Reuters reported on September 3, 2025 that the CFTC approved Polymarket’s return to the U.S. via acquisition of a CFTC‑licensed exchange (QCEX) for 112 million dollars, granting it a Designated Contract Market licence and issuing a no‑action letter on certain reporting obligations, enabling self‑certification of event contracts under U.S. law.\[89\]\[90\]

* A regulatory‑oversight blog in December 2025 details that the CFTC formally approved Polymarket’s plan to resume limited U.S. operations via a registered intermediary, emphasizing enhanced surveillance, reporting, and customer protections comparable to other regulated derivatives markets.\[^91\]

* A November 25, 2025 PRNewswire release announces that the CFTC issued an Amended Order of Designation, allowing Polymarket to operate an intermediated trading platform with FCMs and brokerages, and requiring expanded surveillance, clearing, and regulatory reporting, all of which impact how bots may need to interface with U.S. venues (e.g., via intermediaries instead of direct wallets).\[^92\]

* A Metamask‑authored trends piece (March 26, 2026\) notes that Polymarket began a phased U.S. rollout under this intermediated model in late 2025 and as of March 2026 has self‑certified new market rules, suggesting that bots serving U.S. clients must now respect venue‑specific rulebooks and KYC constraints for certain contracts.\[^93\]

* No major EU‑specific regulatory actions targeting Polymarket automation were found for 2025–2026; most regulatory focus remains on U.S. CFTC oversight and general event‑contract policy rather than bot usage per se.\[90\]\[91\]

**Group E — Comparison targets and practical floors**

**E1 — Realistic end‑to‑end latency floor for a single‑process asyncio bot**

* TradoxVPS measurements show 25 ms maker and 250–300 ms taker latency from a Dublin VPS to the CLOB, plus around 90–100 ms for WebSocket market‑data propagation, implying that a well‑implemented bot cannot realistically do better than roughly 250–350 ms detection→execution round‑trip via public infra.\[^15\]

* A Reddit latency experiment reports a best observed 268 ms round‑trip time for fill‑or‑kill orders, with only 20 ms latency on denied orders and internal compute in the tens to hundreds of microseconds, corroborating that the infra rather than bot code sets the floor.\[^14\]

* Polycopybot’s consistently claimed 340 ms average end‑to‑end latency (with a 680 ms p95) using WebSocket‑based detection and dual nodes in Frankfurt and Singapore provides a practical benchmark: a single‑process Python asyncio bot with public infra and good placement is likely bounded to roughly 300–400 ms average latency in production.\[51\]\[12\]\[13\]\[35\]

**E2 — Empirically validated scoring formulas vs naive ROI**

* Convexly’s 10k‑wallet calibration audit reports that across an 8,656‑wallet Polymarket cohort, the Spearman rank correlation between wallet Brier score (calibration) and realized PnL is only \+0.148, indicating that good calibration alone explains little of profit variance; its Edge Score V3b combines calibration, sizing discipline, and concentration risk fitted against signed log PnL to improve predictive power.\[^37\]

* A Hacker News post by the same author describes a separate experiment on the top 100 profit wallets, finding a Spearman correlation of \+0.608 between Brier score and realized profit (worse calibration associated with higher profit) and 4.66x higher median profit for the worst‑calibrated whales versus better‑calibrated ones, emphasizing that naive calibration‑only scoring can backfire when whales exploit convex payoff structures.\[^94\]

* Convexly’s wallet analyzer defines a practical Brier‑skill threshold (0.05+ improvement over base‑rate baseline on ≥50 resolved bets) and uses regression‑fitted coefficients for calibration, sizing, and concentration, which in internal tests outperforms naive last‑90‑day ROI ranking, but the exact coefficient values and out‑of‑sample Brier‑skill uplift versus ROI are only detailed in their methodology paper, not summarized in high‑level docs.\[27\]\[37\]

* No other public 2025–2026 study was found that publishes a precise, peer‑reviewed scoring formula with weights and a quantified Brier‑skill or PnL improvement over a “last 90 days ROI” baseline specifically for Polymarket copy‑trading; commercial tools (Polycop, PolyVision, PolyCopyTrade) describe multi‑signal scoring qualitatively but keep weights proprietary.\[29\]\[34\]\[36\]\[12\]\[^35\]

**E3 — Optimal scoring window for Polymarket**

* Convexly’s methodology references a “frozen cohort” of 8,656 wallets and uses cross‑validation across historical windows, but public summaries do not specify an optimal lookback (e.g., 90 vs 180 vs 365 days); instead, emphasis is on sample size (≥50 resolved markets) and volume as more important than pure time window.\[27\]\[37\]

* Several copy‑trading guides (e.g., Ratio’s 3‑5‑1 method) recommend requiring at least 50 resolved trades and 60%+ win rate plus diversified categories before treating a wallet as copy‑worthy, effectively setting a position‑count‑based rather than time‑based scoring window.\[^95\]

* Merlin’s copy‑trading guide suggests focusing on traders with at least 10,000–50,000 dollars in all‑time volume and robust 30‑day performance metrics, again

**References**

1. [Top Polymarket Traders Show Exceptional Performance Metrics](https://phemex.com/news/article/top-polymarket-traders-show-exceptional-performance-metrics-57796) \- Discover the top Polymarket traders with high win rates and ROI. LucasMeow, tsybka, and BAdiosB lead...

2. [Top Polymarket Traders](https://explorer.struct.to/traders) \- Compare top Polymarket traders by PnL, volume, markets traded, and win rate.

3. [In-depth analysis of 27000 trades by Polymarket's top ten whales](https://www.mexc.co/en-PH/news/402926) \- Author: Frank, PANews Recently, the market prediction industry has seen a surge in popularity, espec...

4. [Inside Polymarket's Top 10 Whales: 27,000 Trades, the Illusion of ...](https://www.gate.com/learn/articles/inside-polymarkets-top-10-whales-27000-trades-the-illusion-of-smart-money-and-the-real-survival-rules/15440) \- PANews analyzed 27,000 trades by Polymarket's top 10 profit-making whales in December, revealing the...

5. [Columbia Study Flags 25% of Polymarket Volume as Fake \- COIN360](https://coin360.com/news/columbia-study-polymarket-wash-trading-25-volume) \- Columbia researchers find 25% of Polymarket trades may be wash trading, peaking at 60% in late 2024\.

6. [Study Finds One-Quarter of Polymarket Trading May Be Artificial](https://cryptonews.com.au/news/study-finds-one-quarter-of-polymarket-trading-may-be-artificial-131637/) \- A study by researchers at Columbia University has found that 25% of trading volume on prediction mar...

7. [Polymarket volume inflated by 'artificial' activity, study finds | Fortune](https://fortune.com/2025/11/07/polymarket-wash-trading-inflated-prediction-markets-columbia-research/) \- While wash trading may have accounted for around 60% of all Polymarket trading last December, it sub...

8. [$4.5B) of Polymarket volume may be wash trading; 14% of wallets ...](https://whale-alert.io/stories/9b5b35e8b44f/Columbia-study-finds-25-of-Polymarket-volume-likely-wash-trading-43k-wallet-cluster-and-60-weekly-peaks-point-to-manipulation-ahead-of-tokenairdrop-plans) \- Many suspected wash wallets showed no net profit, suggesting activity aimed at gaming future incenti...

9. [Polymarket Rebounds With Growing User Activity as Wash Trading ...](https://www.cointribune.com/en/polymarket-rebounds-with-growing-user-activity-as-wash-trading-concerns-rise/) \- Polymarket sees rising users and near-record volumes while a Columbia study flags major wash trading...

10. [Rate Limits \- Polymarket Documentation](https://docs.polymarket.com/api-reference/rate-limits) \- All API rate limits are enforced using Cloudflare's throttling system. When you exceed the limit for...

11. [Indexing Polymarket with Goldsky](https://docs.goldsky.com/chains/polymarket) \- These public subgraphs have a rate limit of 100 requests per second per IP (1,000 requests per 10 se...

12. [Polycop Bot \- The Automated Copy Trading Bot for Polymarket](https://www.polycopybot.app/blog/polycop-bot) \- How the Polycop bot works — AI wallet scoring across 14 signals, sub-second execution, and non-custo...

13. [Polymarket Copy Trading Bot \- Full Review \- Polycopybot.app](https://www.polycopybot.app/blog/polymarket-copy-trading-bot) \- Polymarket copy trading bot reviewed in depth — architecture, AI scoring integration, execution benc...

14. [Fastest trades you're getting to Polymarket CLOB? \- Reddit](https://www.reddit.com/r/algotrading/comments/1s4iena/fastest_trades_youre_getting_to_polymarket_clob/) \- Polymarket has 250ms delay for taker orders. Try to send a ... I built an AI-powered trading bot for...

15. [How to Test Latency of Polymarket VPS for Trading \- Full Tech Guide](https://tradoxvps.com/how-to-test-latency-of-your-polymarket-vps-for-trading/) \- Learn how to test Polymarket VPS latency for trading and why Dublin delivers the fastest speeds. Red...

16. [Polymarket Fees Explained: A Deep Dive into Trading, Winnings ...](https://www.kucoin.com/blog/polymarket-fees-trading-guide-2026) \- Dynamic Taker-Fee Model: Polymarket has transitioned from a zero-fee model to a Dynamic Taker Fee sy...

17. [Polymarket Sees Surge in Fees and Revenue After Expanding ...](https://financefeeds.com/polymarket-fees-revenue-surge-new-pricing-model/) \- The development signals Polymarket's transition from a zero-fee to a dynamic fee platform aimed at a...

18. [Polymarket Expands Taker Fees to 8 New Market Categories ...](https://www.mexc.com/news/976171) \- Polymarket will expand its taker fee structure to eight additional market categories on March 30, 20...

19. [Trading Fees | Polymarket Help Center](https://help.polymarket.com/en/articles/13364478-trading-fees) \- The maximum effective fee rate is 1.80% at 50% probability. Fees drop at the extremes: As a share's ...

20. [In-depth analysis of 27000 trades by Polymarket's top ten whales](https://www.mexc.co/en-NG/news/402926) \- Author: Frank, PANews Recently, the market prediction industry has seen a surge in popularity, espec...

21. [I analyzed 1,297 “real” Polymarket traders (not bots) by Win rate \-\> Here’s what’s weird](https://www.reddit.com/r/VibeCodersNest/comments/1qed6dh/i_analyzed_1297_real_polymarket_traders_not_bots/) \- I analyzed 1,297 “real” Polymarket traders (not bots) by Win rate \-\> Here’s what’s weird

22. [Polymarket | Dune](https://www.dune.com/witcheer/polymarket) \- Actual profitability rate likely 15-20% vs shown, as winning positions convert to realized gains upo...

23. [Blockchain Data Resources](https://docs.polymarket.com/developers/builders/blockchain-data-resources) \- Access Polymarket on-chain activity for data & analytics

24. [How accurate is Polymarket?](https://polymarket.com/accuracy) \- Explore Polymarket's accuracy metrics and Brier scores across different time periods and market volu...

25. [भविष्यवाणी बाजार बेहद सटीक होते हैं \- सीमांत क्रांति](https://marginalrevolution.com/marginalrevolution/2025/10/prediction-markets-are-very-accurate.html) \- alexmccullough at Dune has a very good post on the accuracy of Polymarket prediction markets. First,...

26. [Polymarket Prediction Accuracy: Track Record & Brier Score | Fensory](https://fensory.com/intelligence/predict/polymarket-accuracy-analysis-track-record-2026) \- Polymarket achieves 73% accuracy across 2,847 markets with 0.187 Brier score. See complete track rec...

27. [Polymarket Wallet Analyzer. Calibration, Sizing & Edge Score in 30s](https://www.convexly.app/tools/polymarket-wallet-analyzer) \- We compare the wallet's Brier score against a naive baseline: always predict the base rate. A trader...

28. [polyvision — Agent Skill — MCP.Directory](https://mcp.directory/skills/polyvision) \- PolyVision analyzes Polymarket prediction market ... Wait and retry — Polymarket API has upstream li...

29. [polyvision \- Skill \- Smithery](https://smithery.ai/skills/openclaw/polyvision) \- PolyVision — Polymarket Wallet Analyzer ... Use it to evaluate whether a trader is worth copy tradin...

30. [Polymarket Wallet Tracker and Analytics](https://www.walletmaster.tools/polymarket-wallet-tracker/) \- Analyze 7M+ Polymarket wallets across 80+ performance metrics. Find profitable prediction market tra...

31. [Up To 25% Of Polymarket Trading Volume May Be Wash Trading ...](https://finance.yahoo.com/news/25-polymarket-trading-volume-may-203104855.html) \- A substantial amount of Polymarket's trading is being faked by traders, according to a recent study ...

32. [Polymarket Volume Inflated by 'Artificial' Activity, Study Finds](https://www.bloomberg.com/news/articles/2025-11-07/polymarket-volume-inflated-by-artificial-activity-study-finds) \- The volume of activity on Polymarket, one of the most popular prediction markets, has been significa...

33. [How Prediction Markets Scaled to USD 21B in Monthly Volume in ...](https://www.trmlabs.com/resources/blog/how-prediction-markets-scaled-to-usd-21b-in-monthly-volume-in-2026) \- On March 23, 2026, both Kalshi and Polymarket publicly outlined new measures to curb insider trading...

34. [Polycop Bot Copy Trading — How the Bot Executes Your Strategy ...](https://www.polycopybot.app/blog/polycop-bot-copy-trading) \- app's scoring engine has already evaluated every active Polymarket wallet across 14 signals. This pr...

35. [Polycop Polymarket Copy Trading Bot — Architecture and Execution ...](https://www.polycopybot.app/blog/polycop-polymarket-copy-trading-bot) \- How the polycop Polymarket copy trading bot works — AI wallet scoring, signal detection, non-custodi...

36. [About \- Polyvision](https://polyvisionx.com/about.html) \- Polyvision Docs Help About Open App. How Polyvision Actually Works ... Polymarket's API limits to pr...

37. [Truth Leaderboard: the top 20 Polymarket wallets ranked ... \- Convexly](https://www.convexly.app/truth-leaderboard) \- Across the full 8,656-wallet Polymarket cohort in our V1 methodology paper, the Spearman rank correl...

38. [Polymarket-Copy-Trade---Documentation/README.md at main](https://github.com/mundoasdef/Polymarket-Copy-Trade---Documentation/blob/main/README.md) \- ✓ Polymarket Account \- With funded wallet for trading. Quick Start Steps. Step 1: Connect Your Walle...

39. [Polymarket Copy Trading Bot \- Automated Prediction Market ...](https://www.polycopytrade.net) \- Polymarket Copy Trade lets you copy top traders in real time with advanced risk controls, non-custod...

40. [Polymarket Copy Trade Introduces Copy Trading to Prediction Markets](https://www.openpr.com/news/4418835/polymarket-copy-trade-introduces-copy-trading-to-prediction) \- In a significant development for the prediction markets industry, [www.polycopytrade.net](http://www.polycopytrade.net) has official...

41. [Polymarket Copy Trade Bot 2026 — Mirror Top Traders Automatically](https://www.polymarketcopybot.com) \- PolyCopyTrade is the leading Polymarket copy trading platform built for prediction market traders on...

42. [How Does Polymarket Work? Step-by-Step Trading Guide (2026)](https://bullpen.fi/bullpen-blog/how-does-polymarket-work-step-by-step-trading-guide) \- Master Polymarket trading with this complete guide. Learn how to deposit, trade, and use Bullpen to ...

43. [How to Track Polymarket Whales: Find Smart Money Before Prices ...](https://bullpen.fi/bullpen-blog/track-polymarket-whales-smart-money) \- Learn how to track Polymarket whales and smart money wallets. Use Bullpen to find profitable traders...

44. [How to Build a Trading Bot for Polymarket (Step-by-Step) \- Bullpen](https://bullpen.fi/bullpen-blog/how-to-build-a-trading-bot-for-polymarket) \- This guide walks you through building your own trading bot for Polymarket, giving you the automation...

45. [Polymarket Developer Essentials: 18 Core Open-Source Tool ...](https://www.odaily.news/en/post/5209012) \- Polymarket Alpha Tool ... Use Case: Provides real-time alerts for Polymarket market movements and ne...

46. [chainstacklabs/polymarket-alpha-bot \- GitHub](https://github.com/chainstacklabs/polymarket-alpha-bot) \- Alphapoly \- Polymarket alpha detection platform. Find covering portfolios across correlated predicti...

47. [Best Copy-Trading Bots for Polymarket & Kalshi — 5 Ranked (2026)](https://agentbets.ai/guides/best-copy-trading-agents-prediction-markets/) \- Best copy-trading bots and agents for prediction markets in 2026\. Polymarket wallet tracking, Kalshi...

48. [Introduction \- Polymarket Documentation](https://docs.polymarket.com/api-reference/introduction) \- The Polymarket API provides programmatic access to the world's largest prediction market. The platfo...

49. [Overview \- Polymarket Documentation](https://docs.polymarket.com/market-data/overview) \- No API key, no authentication, no wallet required. curl "[https://gamma-api.polymarket.com/events?lim](https://gamma-api.polymarket.com/events?lim)...

50. [Tiers \- Polymarket Documentation](https://docs.polymarket.com/builders/tiers) \- API Rate Limits, Rate limits for non-relayer endpoints (CLOB, Gamma, etc ... Relayer requests beyond...

51. [Automated Copy Trading Bot for Polymarket \- Polycopybot.app](https://www.polycopybot.app/blog/polymarket-bot-ru) \- Polymarket Бот — what it is, how automated copy trading bots work on Polymarket, non-custodial execu...

52. [Polymarket bettors forecast 75% chance Bitcoin reaches $120k in ...](https://cryptoslate.com/polymarket-bettors-forecast-75-chance-bitcoin-reaches-120k-in-2025-as-prediction-volume-jumps-30/) \- On the decentralized platform Polymarket, a market asking what price Bitcoin will hit in 2025, has a...

53. [Polymarket sees surge in daily volume and users amid token launch ...](https://cryptoslate.com/polymarket-sees-surge-in-daily-volume-and-users-amid-token-launch-speculation/) \- Polymarket's daily trading volume surged 57.5% on Sept. 23, according to data from Dune Analytics' d...

54. [70% of Polymarket Traders Lost Money as Top 0.04% Captured ...](https://finance.yahoo.com/news/70-polymarket-traders-lost-money-192327162.html) \- Among over 1.7 million trading addresses on Polymarket, approximately 70% have realized losses while...

55. [Best Polymarket Traders to Follow in 2026 \- How to Find Profitable ...](https://www.reddit.com/r/polyman/comments/1sdw4ir/best_polymarket_traders_to_follow_in_2026_how_to/) \- TL;DR: The best Polymarket traders aren't the loudest on Twitter \- they're anonymous wallets with 50...

56. [Polymarket Lists 26 Smart Money Addresses by Market Category](https://phemex.com/news/article/polymarket-reveals-26-smart-money-addresses-across-key-market-categories-71362) \- Polymarket identifies 26 smart money addresses excelling in politics, weather, tech, culture, and sp...

57. [Massive Wash Trading Uncovered on Polymarket \- Crypto Bubble Tea](https://www.cryptobbt.com/blog/massive-wash-trading-uncovered-on-polymarket) \- Accounts that close positions quickly AND trade exclusively with similar accounts \= wash traders. Wh...

58. [How We Detect Polymarket Whale Syndicates — Louvain Clustering ...](https://polyloly.com/blog/detecting-polymarket-whale-syndicates-louvain-clustering) \- On the dashboard, this shows up as a 48-hour sparkline inside each cluster card: you can see at a gl...

59. [Pricing \- Goldsky Docs](https://docs.goldsky.com/pricing/summary) \- If you pause or delete a subgraph, it is no longer billed. One subgraph run for an entire month ther...

60. [Pricing \- Goldsky](https://goldsky.com/pricing) \- Subgraphs ; Starter (Free). 2250 (3 always-on subgraphs). 100,000. 20 / 10s. Unlimited ; Scale. $0.0...

61. [Polymarket Leaderboard Scraper \- Apify](https://apify.com/saswave/polymarket-leaderboard-scraper) \- Pricing · API · Issues. Polymarket Leaderboard Scraper. The Polymarket Leaderboard Scraper allows yo...

62. [Pricing · Polymarket Leaderboard Scraper · Apify](https://apify.com/saswave/polymarket-leaderboard-scraper/pricing) \- $1.5 / 1000 results, no proxies. Extract data from leaderboard [polymarket.com](http://polymarket.com) website. Filter on Pro...

63. [Polymarket Leaderboard Scraper: Wallets & Trade History OpenAPI ...](https://apify.com/parsebird/polymarket-scraper/api/openapi) \- Polymarket Scraper | $1 / 1k | Fast & Reliable ... Get live prediction market data from Polymarket w...

64. [Dune Analytics Pricing Plans and Subscription Options | Zoftware](https://zoftwarehub.com/products/dune-analytics/pricing)

65. [How you spend credits on Dune is changing \- Dune Blog](https://dune.com/blog/credits-changing) \- We're improving how Dune credit usage works to ensure our platform is fair and enables exciting new ...

66. [Pricing](https://dune.com/pricing) \- Dune is the all-in-one crypto data platform — query with SQL, stream data via APIs & DataShare, and ...

67. [Dune Review, Pricing, and Features (2026) | Find My Moat](https://www.findmymoat.com/tools/dune) \- Dune review: Community‑driven onchain data platform for SQL‑based dashboards, APIs and real‑time dat...

68. [Developer Docs \- Polyvision](https://polyvisionx.com/docs) \- These docs are for developers who want to integrate Polyvision into their own tools via REST API, MC...

69. [Prediction Markets Go Automated with the Polymarket Copy](https://www.openpr.com/news/4484285/prediction-markets-go-automated-with-the-polymarket-copy) \- When a copied trader places a trade, Polycop Bot replicates it from your wallet in under a second, p...

70. [PolyCop: Polymarket Telegram Bot, Copy Trading & Sniper ...](https://polycopbot.com) \- Copy top Polymarket traders automatically, snipe profitable trades, and automate your prediction mar...

71. [How to Build a Production-Ready Polymarket Copy Trading Bot](https://ericaai.tech.blog/2026/03/11/how-to-build-a-production-ready-polymarket-copy-trading-bot/) \- The bot connects lazily, starting the websocket once at least one subscription exists. This reduces ...

72. [Building a Polymarket Copy Trading Bot | Quicknode Guides](https://www.quicknode.com/guides/defi/polymarket-copy-trading-bot) \- Build a Polymarket trading bot that tracks a target wallet, logs trades in real time, and adds posit...

73. [Matching Engine Restarts \- Polymarket Documentation](https://docs.polymarket.com/trading/matching-engine) \- The matching engine restarts weekly on Tuesdays at 7:00 AM ET. During a restart window, the engine i...

74. [How to Setup a Polymarket Bot: Step-by-Step Guide for Beginners](https://www.quantvps.com/blog/setup-polymarket-trading-bot) \- Use risk-adjusted metrics like Sharpe and Sortino ratios to evaluate your bot's performance and conf...

75. [Trading Fees & Operating Hours \- Polymarket US DCM Schedule](https://www.polymarketexchange.com/fees-hours.html) \- Trading Fee Schedule · Taker Fee Coefficient (Θ) \= 0.05 (maximum $1.25 per 100 contracts at p \= $0.5...

76. [Polymarket WebSocket Tutorial: Real-Time Data Streaming ...](https://www.polytrackhq.app/blog/polymarket-websocket-tutorial) \- Connect to [ws-subscriptions-clob.polymarket.com](http://ws-subscriptions-clob.polymarket.com) for real-time prices. Python & JS examples for order...

77. [WSS Quickstart \- Polymarket Documentation](https://polymarket-292d1b1b.mintlify.app/quickstart/websocket/WSS-Quickstart)

78. [Types](https://pkg.go.dev/github.com/GoPolymarket/polymarket-go-sdk/pkg/clob/ws) \- Package ws provides a high-level WebSocket client for Polymarket.

79. [Service is not ready · Issue \#286 · Polymarket/py-clob-client \- GitHub](https://github.com/Polymarket/py-clob-client/issues/286) \- sometimes order placement/cancellation can fail during matching engine restarts \- this is expected b...

80. [Clients & SDKs \- Polymarket Documentation](https://docs.polymarket.com/api-reference/clients-sdks) \- Polymarket provides official open-source clients in TypeScript, Python, and Rust. All three support ...

81. [Polymarket Lags Behind Competitors Amid Operational Delays and ...](https://www.kucoin.com/news/flash/polymarket-falls-behind-competitors-amid-operational-delays-and-product-missteps) \- Last weekend, Polymarket delayed the migration to CLOB V2, the new pUSD collateral token, and the re...

82. [Incident resolved \- Incident details \- Polymarket \- Status](https://status.polymarket.com/cmmv1lxsa01diqlaztv6z254f) \- Incident resolved. Resolved. Partial outage. Started about 1 month agoLasted 15 minutes. Affected. C...

83. [Polymarket \- Status](https://status.polymarket.com) \- Polymarket Status. ... Report an issue. Get updates. All systems operational. Website \- Operational.

84. [The State of Polymarket Trading in 2026 — Why Manual Traders Are ...](https://polyloly.com/blog/polymarket-trading-terminals-2026) \- A well-known 2025 incident saw an automated market-maker bot bleed out $420,000 over four minutes be...

85. [Tracked polymarket delay on 14 events this month — average 31 ...](https://www.reddit.com/r/PredictionsMarkets/comments/1scc34x/tracked_polymarket_delay_on_14_events_this_month/) \- my numbers: out of 14 events i tracked manually, market moved avg 31 min after news broke on reuters...

86. [GitHub \- Polymarket/py-clob-client: Python client for the Polymarket CLOB](https://github.com/Polymarket/py-clob-client/tree/main) \- Python client for the Polymarket CLOB. Contribute to Polymarket/py-clob-client development by creati...

87. [Typescript client for the Polymarket CLOB \- GitHub](https://github.com/Polymarket/clob-client) \- Typescript client for the Polymarket CLOB. Contribute to Polymarket/clob-client development by creat...

88. [Polymarket/py-clob-client-v2 \- GitHub](https://github.com/Polymarket/py-clob-client-v2) \- PY Polymarket CLOB Client V2 · Usage · Market Orders · Authentication. The client has two authentica...

89. [Prediction Market Polymarket Poised to Relaunch in US Within Days](https://finance.yahoo.com/news/prediction-market-polymarket-poised-relaunch-195732656.html) \- Polymarket prepares to relaunch for U.S. users by self-certifying markets through its CFTC-licensed ...

90. [Polymarket receives green signal from CFTC for US return | Reuters](https://www.reuters.com/sustainability/boards-policy-regulation/polymarket-receives-green-signal-cftc-us-return-2025-09-03/) \- Polymarket is set to return to the U.S. more than three years after agreeing to block American users...

91. [CFTC Approval Allows Polymarket to Reenter the U.S. Market](https://www.regulatoryoversight.com/2025/12/cftc-approval-allows-polymarket-to-reenter-the-u-s-market/) \- Polymarket previously withdrew from the U.S. following a 2022 CFTC enforcement action that identifie...

92. [Polymarket Receives CFTC Approval of Amended Order of ...](https://www.prnewswire.com/news-releases/polymarket-receives-cftc-approval-of-amended-order-of-designation-enabling-intermediated-us-market-access-302625833.html) \- With this approval, Polymarket will be able to onboard brokerages and customers directly and facilit...

93. [Prediction markets in 2026: Key trends reshaping forecasting ...](https://metamask.io/news/prediction-market-overview-trends-2026) \- Polymarket began a phased US rollout under this intermediated model in late 2025, and as of March 20...

94. [We tested the insider trading claim on Polymarket with Taleb-proof ...](https://news.ycombinator.com/item?id=47765107) \- The Brier scoring math in the post is the same math that runs in the product. Disclosure out of the ...

95. [How to Build a Polymarket Copy Trading Portfolio (The 3-5-1 Method)](https://ratio.you/blog/polymarket-copy-trading-portfolio-method) \- Stop copy trading random wallets. The 3-5-1 method gives you a structured portfolio approach to copy...