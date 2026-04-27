# M18 — Polymarket CLOB V2 + pUSD migration

**Status** : Draft — 2026-04-27
**Depends on** : M3 (Executor + triple garde-fou + lazy init `ClobClient`),
M16 (`FeeRateClient` contract + `_compute_effective_fee_rate` formule),
M17 (cross-layer integrity — invariants `execution_mode` segregation,
kill switch ordering, alert digest bypass CRITICAL, `last_known_mid` TTL,
config validators legacy reroute pattern)
**Bloque** : flip `EXECUTION_MODE=live` post-cutover (sans M18 le bot
casse silencieusement le 28 avril 2026 ~11h UTC — signatures V1 rejetées
par le nouvel orderbook V2)
**Workflow git** : commits directement sur `main` (pas de branche, pas de
PR — règle projet)
**Charge estimée** : M (2 jours dev, hard deadline lundi 27 avril 2026
~22h UTC)
**Numéro** : M18 (après MA=M14, MB=M15, MC=M16, MD=M17)

---

## 0. TL;DR

M18 livre la **migration complète polycopy → Polymarket CLOB V2 + pUSD
collateral** avant le cutover Polymarket annoncé officiellement
([docs.polymarket.com/v2-migration](https://docs.polymarket.com/v2-migration))
pour le **mardi 28 avril 2026 ~11h00 UTC** (~1h downtime, no
backward-compat post-go-live).

**7 items couplés** mappés ME.1 → ME.7 du brief
[docs/next/ME.md](../next/ME.md) :

- **ME.1** — Bump SDK `py-clob-client>=0.20.0` → `py-clob-client-v2==1.0.0`
  + 4 imports + 1 method rename (`create_or_derive_api_creds` →
  `create_or_derive_api_key`). Le SDK V2 est **dual-version capable** :
  signe V1 ou V2 selon le résultat du `/version` endpoint backend (cf.
  décision **D11** §4.11). On peut shipper **avant** le cutover —
  élimine la fenêtre critique 11h UTC.
- **ME.2** — `POLYMARKET_CLOB_HOST` setting configurable (default
  `https://clob.polymarket.com`) propagé aux 5 clients HTTP
  (`ClobReadClient`, `ClobMetadataClient`, `ClobOrderbookReader`,
  `FeeRateClient`, `ClobWriteClient` via SDK). Permet le test
  pré-cutover contre `https://clob-v2.polymarket.com`. Setting
  `POLYMARKET_USE_SERVER_TIME=true` par défaut — défense anti
  clock-skew sur le `timestamp` ms V2 (cf. **D8** §4.8).
- **ME.3** — `FeeRateClient` swap interne `/fee-rate?token_id=` (V1) →
  `/clob-markets/{condition_id}` (V2) + nouveau DTO `FeeQuote(rate,
  exponent)` + nouvelle méthode `get_fee_quote()`. Ancienne méthode
  `get_fee_rate(token_id) -> Decimal` **préservée** comme alias
  deprecated (zéro modif tests M16). `PositionSizer._compute_effective_fee_rate`
  réécrit pour consommer `fd.e` directement (le mapping hardcodé
  `feeType → (rate_param, exponent)` M16 disparaît — cf. **D6** §4.6).
- **ME.4** — pUSD collateral : `POLYMARKET_COLLATERAL_ONRAMP_ADDRESS`
  setting (default `0x93070a847efEf7F70739046A929D47a521F5B8ee`,
  confirmé live 2026-04-27 sur `/resources/contracts`) +
  `POLYMARKET_USDC_E_ADDRESS` (default
  `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`) + helper script
  `scripts/wrap_usdc_to_pusd.py` + dependency optionnelle
  `[project.optional-dependencies] live = ["web3>=6.0,<8.0"]` (cf.
  **D12** §4.12).
- **ME.5** — Builder code support optionnel : `POLYMARKET_BUILDER_CODE`
  + `POLYMARKET_BUILDER_ADDRESS` settings + plombage `BuilderConfig`
  au constructor `ClobClient` V2. Default `None` = aucun builder,
  comportement strict M3..M16 préservé (cf. **D9** §4.9).
- **ME.6** — Tests intégration `tests/integration/test_clob_v2_*.py`
  (3 tests opt-in `pytest -m integration`) + fixtures
  `tests/fixtures/clob_v2_market_*.json` capturées 2026-04-27.
- **ME.7** — Procédure cutover ops : phase 1 ship lundi 27 avril
  ~22h UTC (SDK V2 signe V1 orders en attendant), phase 2 auto-flip
  mardi 11h UTC (zéro intervention humaine), phase 3 smoke
  post-cutover, phase 4 monitoring 24h. Référence
  [docs/todo.md §14](../todo.md) enrichi.

Diff strictement additif sur les invariants critiques :

- **Triple garde-fou M3 + 4ᵉ M8** : intacts. M18 swappe le SDK
  signataire mais ne touche ni `_persist_realistic_simulated`, ni
  `_persist_sent_order`, ni le re-fetch `WalletStateReader.get_state()`,
  ni les asserts `execution_mode == "live"` AVANT `create_and_post_order`.
- **Contrat M16 `FeeRateClient.get_fee_rate(token_id) -> Decimal`** :
  préservé strict comme alias deprecated. Zéro modif côté
  `PositionSizer._check_buy` au niveau invocation — seul le *calcul*
  interne `_compute_effective_fee_rate` change pour consommer `fd.e`
  directement.
- **Kill switch parité 3 modes M10 + sentinel M12_bis Phase D + audit
  trail M17 MD.7** : intacts. L'ordre strict
  `insert_event → push_alert → touch_sentinel → stop_event.set()`
  préservé.
- **Versioning sacré M14/M15** : aucune touche au scoring (V2 est
  purement infra exchange-side, le scoring repose sur l'historique
  on-chain des wallets cibles — orthogonal — cf. **D14** §4.14).
- **Append-only DB** : aucune migration Alembic (cf. **D13** §4.13).
  pUSD = USDC.e 1:1, le numérique `total_usdc` reste pareil.
  `alembic upgrade head` doit retourner "no migrations to apply"
  post-M18.

Tests cumulés estimés : **~22 tests unit** (ME.1=3, ME.2=4, ME.3=8,
ME.4=4, ME.5=3) + **3 tests intégration** (`getClobMarketInfo` smoke,
signature local validation V2, fee rate V2 endpoint réel). Charge
cumulée : **2 jours dev**, 0 jour shadow (comportement déterministe).
Hard deadline externe : Polymarket cutover **mardi 28 avril 2026
~11h UTC**.

---

## 1. Motivation & use case concret

### 1.1 Le contexte cutover Polymarket

Polymarket déploie une **upgrade complète de la stack exchange**
annoncée officiellement le 2026-04-06 par
[@PolymarketDev](https://twitter.com/PolymarketDev) et confirmée par la
doc [docs.polymarket.com/v2-migration](https://docs.polymarket.com/v2-migration).
3 changements simultanés au cutover :

1. **CTF Exchange V2** (nouveaux contrats Polygon) : Order struct
   simplifiée (drop `nonce` / `feeRateBps` / `taker` / `expiration`
   dans l'EIP-712 signed typed data ; ajout `timestamp` ms / `metadata`
   bytes32 / `builder` bytes32). Domain Exchange V2 bump version
   `"1"` → `"2"`. `verifyingContract` Standard Risk
   `0xE111180000d2663C0091e4f400237545B87B996B`, Neg Risk
   `0xe2222d279d744050d28e00520010520000310F59`. Matching engine
   optimisé, support EIP-1271 (smart contract wallets — hors scope
   polycopy), builder codes onchain pour fee rebates.
2. **Polymarket USD (pUSD)** : nouveau collateral token ERC-20 sur
   Polygon, backed 1:1 par USDC.e via le contrat `CollateralOnramp.wrap()`.
   Adresse pUSD :
   `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` (6 decimals, identique
   USDC). Action manuelle one-time pour les API-only traders (notre
   cas) — non requise tant qu'on reste en `EXECUTION_MODE=dry_run`
   (pas de signature live).
3. **Nouveau SDK CLOB-Client** : packages
   `@polymarket/clob-client-v2` (TypeScript) et `py-clob-client-v2`
   (Python) **séparés** des V1 — pas d'in-place upgrade. Auto-switch
   V1↔V2 via `/version` endpoint backend pour les clients à jour
   (le SDK V2 signe **les deux** versions selon ce que renvoie le
   backend — cf. **D11** §4.11).

### 1.2 Pourquoi P0 hard deadline externe

Contrairement aux autres modules de la roadmap (P1/P2/P3 différables),
V2 migration a un **deadline imposé externe** : Polymarket cutover le
28 avril ~11h UTC, après quoi tout client V1 reçoit
`signature_invalid` sur le nouvel orderbook V2. Le scope est **borné
et documenté** par Polymarket (pas d'invention possible côté
polycopy) — l'incertitude est sur l'**execution timing**, pas sur le
scope.

**Conséquence si on ne ship pas avant le cutover** :

- `clob_write_client.py` continue d'importer `py_clob_client`
  (ancien SDK V1).
- Le SDK V1 signe avec le domain version `"1"` et l'Order struct
  V1 (avec `nonce`, `feeRateBps`, `taker`, `expiration`).
- Backend V2 reçoit la signature → reject `signature_invalid` (pas
  un crash, juste un rejet HTTP 400).
- `MyOrder.status="REJECTED"`, `error_msg="signature_invalid"`,
  pipeline continue à boucler sans alerte particulière.
- **Bot silencieusement inopérant** — l'utilisateur voit "0 trade en
  24h" sans signal clair de la cause.

Le mode `dry_run` **ne nous protège pas non plus** : le builder d'order
+ les queries `getClobMarketInfo`/`/fee-rate` touchent quand même les
endpoints V2-only après le cutover. Sur les markets fee-enabled, le
fee adjustment M16 commencerait à fallback `Decimal("0.018")` en
permanence (404/erreur sur `/fee-rate` V1 si Polymarket le retire).

### 1.3 Ce qui ne change PAS dans M18 (invariants préservés)

Diff strictement additif sur les invariants critiques — aucune ligne
modifiée sur ces sites :

- **Triple garde-fou M3** ([M3-executor.md §2](M3-executor.md#L60)) :
  - Lazy init `ClobClient` (pas instancié si `execution_mode != "live"`)
    [clob_write_client.py:33-43](../../src/polycopy/executor/clob_write_client.py#L33-L43).
  - `RuntimeError` au démarrage si `live` ET clés absentes
    [orchestrator.py:56-63](../../src/polycopy/executor/orchestrator.py#L56-L63).
  - Double check `assert execution_mode == "live"` avant chaque
    POST [clob_write_client.py:73-77](../../src/polycopy/executor/clob_write_client.py#L73-L77)
    + [pipeline.py:191-195](../../src/polycopy/executor/pipeline.py#L191-L195).
  - `WalletStateReader.get_state()` re-fetch wallet state pré-POST
    [pipeline.py:152-189](../../src/polycopy/executor/pipeline.py#L152-L189).
- **4ᵉ garde-fou M8** ([M8-dry-run-realistic.md](M8-dry-run-realistic.md)) :
  `assert settings.execution_mode == "dry_run"` avant
  `_persist_realistic_simulated`
  [pipeline.py:404-407](../../src/polycopy/executor/pipeline.py#L404-L407).
- **Pipeline order strategy** :
  `TraderLifecycle → Market → EntryPrice → PositionSizer →
  SlippageChecker → RiskManager`
  [strategy/pipeline.py:466-472](../../src/polycopy/strategy/pipeline.py#L466-L472)
  inchangé.
- **M16 contrat public `FeeRateClient.get_fee_rate(token_id) -> Decimal`** :
  préservé comme alias deprecated. Tests M16
  ([tests/unit/test_fee_rate_client.py](../../tests/unit/test_fee_rate_client.py))
  passent inchangés.
- **M17 cross-layer integrity** : MD.1 (filtre `simulated`), MD.2
  (bypass digest CRITICAL), MD.3 (`pnl_snapshots.execution_mode`),
  MD.4 (`MidpointUnavailableError` + last_known_mid TTL), MD.5
  (validator `_migrate_legacy_virtual_capital`), MD.6
  (`realized_pnl + unrealized_pnl` peuplés), MD.7
  (`TraderEvent("kill_switch")` insert avant
  `stop_event.set()`) — tous **intacts**.
- **Versioning sacré M14/M15** : aucune row `trader_scores` ni
  `pnl_snapshots` réécrite. Append-only préservé. Aucune fonction
  `compute_score_v2_1` ou `compute_score_v2_1_1` touchée.
- **Sentinel `halt.flag` 0o600 / parent 0o700** [(M12_bis Phase D)](M12_bis_multi_machine_remote_control_spec.md) :
  intact. Ordre strict `touch sentinel → stop_event.set()` préservé.
- **Discipline credentials** : `POLYMARKET_PRIVATE_KEY`,
  `POLYMARKET_FUNDER`, CLOB L2 `api_key/api_secret/api_passphrase`
  — aucun log même partiel, même en debug, même dans les
  exceptions. Cohérent invariant M3 §11.
- **Aucune migration Alembic** : `pUSD = USDC.e × 1` (1:1) → le
  numérique `total_usdc` côté DB reste identique. Pas de nouvelle
  colonne sur `MyOrder` / `MyPosition` / `pnl_snapshots` (cf. **D13**
  §4.13).

### 1.4 Ce que change explicitement M18 (vue de haut)

| Module | Diff | Référence ME |
|---|---|---|
| [pyproject.toml:11](../../pyproject.toml#L11) | `py-clob-client>=0.20.0` → `py-clob-client-v2==1.0.0` | ME.1 |
| [pyproject.toml:29-31](../../pyproject.toml#L29-L31) | Nouvelle section `[project.optional-dependencies] live = ["web3>=6.0,<8.0"]` | ME.4 |
| [pyproject.toml:75-77](../../pyproject.toml#L75-L77) | Mypy override : `py_clob_client.*` → `py_clob_client_v2.*` | ME.1 |
| [src/polycopy/executor/clob_write_client.py:14-16](../../src/polycopy/executor/clob_write_client.py#L14-L16) | Imports `from py_clob_client_v2 import ClobClient, OrderArgs, OrderType` ; suppression `from py_clob_client.order_builder.constants import BUY, SELL` | ME.1 |
| [src/polycopy/executor/clob_write_client.py:25-26](../../src/polycopy/executor/clob_write_client.py#L25-L26) | `_HOST = settings.polymarket_clob_host`, `_CHAIN_ID = 137` (centraliser) | ME.2 |
| [src/polycopy/executor/clob_write_client.py:60](../../src/polycopy/executor/clob_write_client.py#L60) | `temp_client.create_or_derive_api_creds()` → `temp_client.create_or_derive_api_key()` | ME.1 |
| [src/polycopy/executor/clob_write_client.py:62-69](../../src/polycopy/executor/clob_write_client.py#L62-L69) | ClobClient L2 constructor : ajout `use_server_time=settings.polymarket_use_server_time` + `builder_config=...` (si `polymarket_builder_code` set) | ME.2 + ME.5 |
| [src/polycopy/executor/clob_write_client.py:91-99](../../src/polycopy/executor/clob_write_client.py#L91-L99) | `_build_order_args` : passer `built.side` (string "BUY"/"SELL") direct → `OrderArgs(side=built.side, ...)` | ME.1 |
| [src/polycopy/executor/fee_rate_client.py](../../src/polycopy/executor/fee_rate_client.py) | Refactor swap V1→V2 endpoint (cf. §5.3) + `FeeQuote` DTO + `get_fee_quote()` + `get_fee_rate()` deprecated alias | ME.3 |
| [src/polycopy/executor/clob_metadata_client.py:27](../../src/polycopy/executor/clob_metadata_client.py#L27) | `BASE_URL` → consomme `settings.polymarket_clob_host` | ME.2 |
| [src/polycopy/executor/clob_orderbook_reader.py](../../src/polycopy/executor/clob_orderbook_reader.py) | Idem | ME.2 |
| [src/polycopy/strategy/clob_read_client.py](../../src/polycopy/strategy/clob_read_client.py) | Idem | ME.2 |
| [src/polycopy/strategy/pipeline.py:221-222](../../src/polycopy/strategy/pipeline.py#L221-L222) | `_check_buy` : `await self._fee_rate_client.get_fee_quote(ctx.trade.asset_id, condition_id=ctx.trade.condition_id)` | ME.3 |
| [src/polycopy/strategy/pipeline.py:293-334](../../src/polycopy/strategy/pipeline.py#L293-L334) | `_compute_effective_fee_rate` réécrit : consomme `quote.exponent` au lieu du mapping hardcodé `fee_type → (...)` | ME.3 |
| [src/polycopy/config.py](../../src/polycopy/config.py) | +5 settings (`POLYMARKET_CLOB_HOST`, `POLYMARKET_USE_SERVER_TIME`, `POLYMARKET_COLLATERAL_ONRAMP_ADDRESS`, `POLYMARKET_USDC_E_ADDRESS`, `POLYMARKET_BUILDER_CODE`, `POLYMARKET_BUILDER_ADDRESS`) + 1 cross-field validator | ME.2 + ME.4 + ME.5 |
| [.env.example](../../.env.example) | +1 bloc commenté M18 (5-6 lignes) | tous |
| [scripts/wrap_usdc_to_pusd.py](../../scripts/wrap_usdc_to_pusd.py) | **Nouveau fichier**. Helper one-time `web3.py` (~80 LOC) | ME.4 |
| [tests/integration/test_clob_v2_market_info_live.py](../../tests/integration/test_clob_v2_market_info_live.py) | **Nouveau fichier**. Smoke `getClobMarketInfo` réel | ME.6 |
| [tests/integration/test_clob_v2_signature_local.py](../../tests/integration/test_clob_v2_signature_local.py) | **Nouveau fichier**. Build V2 order localement, vérifier la signature passe le check SDK | ME.6 |
| [tests/integration/test_clob_v2_fee_rate_live.py](../../tests/integration/test_clob_v2_fee_rate_live.py) | **Nouveau fichier**. Fee rate via V2 endpoint réel | ME.6 |
| [tests/integration/test_clob_l1_l2_auth_live.py:11](../../tests/integration/test_clob_l1_l2_auth_live.py#L11) | Import path V1 → V2 + method rename | ME.1 |
| [tests/unit/test_clob_write_client.py:32](../../tests/unit/test_clob_write_client.py#L32) | Mock `create_or_derive_api_creds` → `create_or_derive_api_key` | ME.1 |
| [tests/fixtures/clob_v2_market_crypto_sample.json](../../tests/fixtures/clob_v2_market_crypto_sample.json) | **Nouveau**. Capture live 2026-04-27 — fee-enabled crypto | ME.3 + ME.6 |
| [tests/fixtures/clob_v2_market_fee_free_sample.json](../../tests/fixtures/clob_v2_market_fee_free_sample.json) | **Nouveau**. Capture live 2026-04-27 — fee-free politics | ME.3 + ME.6 |
| [docs/todo.md §14](../todo.md#L554) | Procédure cutover enrichie (4 phases, D11 ship pré-cutover) | ME.7 |
| [CLAUDE.md](../../CLAUDE.md) | §Conventions + §Sécurité — bloc M18 V2 migration | tous |

---

## 2. Scope / non-goals

### 2.1 Dans le scope (ME.1 → ME.7)

#### ME.1 — SDK swap V1 → V2

- `pyproject.toml` : bump `py-clob-client>=0.20.0` →
  `py-clob-client-v2==1.0.0` (publié 2026-04-17 sur PyPI, version
  vérifiée 2026-04-27 via `pip index versions py-clob-client-v2`).
- Mypy override correspondant : `[[tool.mypy.overrides]] module =
  "py_clob_client_v2.*"` ([pyproject.toml:75-77](../../pyproject.toml#L75-L77)).
- `clob_write_client.py` : 4 imports + 1 method rename (cf. table §1.4).
- **Découverte clé** ([SDK README + source](https://github.com/Polymarket/py-clob-client-v2)) :
  le constructor V2 garde la **même signature V1-style positionnelle**.
  Pas de refactor structurel comme le brief ME.5 le suggérait à tort
  (cf. **D1** §4.1).
- Le SDK V2 est **dual-version capable** : `OrderBuilder.build_order(version=N)`
  choisit V1 ou V2 selon le résultat de `_resolve_version()` qui query
  `/version` au boot puis re-retry sur `order_version_mismatch`. **Ship
  AVANT le cutover** est sûr (cf. **D11** §4.11).

#### ME.2 — `POLYMARKET_CLOB_HOST` configurable + `use_server_time`

- Nouveau setting `polymarket_clob_host: str = Field("https://clob.polymarket.com", ...)`
  consommé par les **5 clients HTTP** polycopy (cf. inventaire §1.4).
- Pré-cutover : permet override `POLYMARKET_CLOB_HOST=https://clob-v2.polymarket.com`
  pour tester contre le testnet V2.
- Post-cutover : default OK (la prod URL bascule automatiquement sur le
  backend V2 le 28 avril ~11h UTC).
- Nouveau setting `polymarket_use_server_time: bool = Field(True, ...)`
  passé au SDK constructor. Coût +50-200ms/sign, bénéfice : zéro risque
  silencieux de rejection sur clock drift (la VM prod peut dériver de
  plusieurs secondes après suspend/resume) — cf. **D8** §4.8.

#### ME.3 — `FeeRateClient` swap + `FeeQuote` + `_compute_effective_fee_rate` réécrit

- Nouveau DTO Pydantic
  [src/polycopy/executor/fee_rate_client.py](../../src/polycopy/executor/fee_rate_client.py) :
  ```python
  class FeeQuote(BaseModel):
      """V2 fee quote from `getClobMarketInfo(condition_id)["fd"]`.

      `rate` = `fd.r` (Decimal, fee rate parameter).
      `exponent` = `fd.e` (int, formula exponent).
      Fee-free markets : `fd` absent → `FeeQuote(Decimal("0"), 0)`.
      """
      model_config = ConfigDict(frozen=True)
      rate: Decimal
      exponent: int = Field(ge=0, le=4)
  ```
- Nouvelle méthode publique :
  ```python
  async def get_fee_quote(
      self,
      token_id: str,
      *,
      condition_id: str | None = None,
  ) -> FeeQuote:
      """Retourne `(rate, exponent)` du marché.

      Path nominal V2 : `condition_id` fourni → call direct
      `getClobMarketInfo(condition_id)`, zéro Gamma overhead.

      Fallback V1 / safety net : `condition_id=None` → résolution
      `token_id → condition_id` via Gamma `/markets-by-token/{token_id}`
      (cache LRU dédié TTL 5 min, max 500). Warning structlog
      `fee_rate_client_token_id_resolved_via_gamma`.

      Erreurs / fallback :
      - HTTP 404 sur `/clob-markets/{cid}` → `FeeQuote(Decimal("0"), 0)`
        (marché inconnu = pas de fee à appliquer).
      - HTTP 400 / 5xx post-tenacity → `FeeQuote(Decimal("0.018"), 1)`
        (worst-case 1.80% conservateur, cohérent M16 §11.5).
      - `result["fd"]` absent → `FeeQuote(Decimal("0"), 0)` (marché
        fee-free, comportement attendu sur la majorité Polymarket).
      """
  ```
- Ancienne méthode **préservée** comme alias deprecated :
  ```python
  async def get_fee_rate(self, token_id: str) -> Decimal:
      """[DEPRECATED M18] Utiliser `get_fee_quote()` pour accès `fd.e`.

      Wrapper rétrocompat M16 — retourne `quote.rate` du nouveau
      `get_fee_quote(token_id, condition_id=None)`. Un warning
      structlog `fee_rate_client_get_fee_rate_deprecated` est émis
      1× par `token_id` au premier appel (cap LRU 500 entries).

      À retirer en M19+ après audit que toutes les callers utilisent
      `get_fee_quote`.
      """
      quote = await self.get_fee_quote(token_id)
      return quote.rate
  ```
- `PositionSizer._compute_effective_fee_rate` réécrit pour consommer
  `quote.exponent` directement au lieu du mapping hardcodé
  `fee_type → (rate_param, exponent)`. La logique fallback Decimal(0.018)
  reste sur les chemins exceptionnels. Cf. §5.3 algorithme détaillé.

#### ME.4 — pUSD collateral env vars + helper script

- 2 nouveaux settings `Settings` :
  ```python
  polymarket_collateral_onramp_address: str = Field(
      "0x93070a847efEf7F70739046A929D47a521F5B8ee",
      pattern=r"^0x[a-fA-F0-9]{40}$",
      description=(
          "Adresse Polygon du contrat CollateralOnramp (V2). Confirmée "
          "live 2026-04-27 via docs.polymarket.com/resources/contracts. "
          "Consommée UNIQUEMENT par scripts/wrap_usdc_to_pusd.py — "
          "ClobClient V2 ne touche pas ce contrat. Override possible "
          "si Polymarket re-deploy."
      ),
  )
  polymarket_usdc_e_address: str = Field(
      "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
      pattern=r"^0x[a-fA-F0-9]{40}$",
      description=(
          "Adresse USDC.e Polygon canonique pré-V2. Consommée "
          "UNIQUEMENT par le wrap script (approve USDC.e → Onramp)."
      ),
  )
  ```
- Validator cross-field : si `execution_mode == "live"` ET
  `polymarket_collateral_onramp_address` empty → raise `ValueError`
  avec message clair (l'utilisateur doit confirmer l'adresse live
  ou utiliser le default).
- **Aucune env var pour Exchange V2 / Neg Risk Exchange V2 / pUSD
  proxy** : ces 3 adresses sont owned par le SDK V2
  ([config.py SDK](https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/config.py))
  via `get_contract_config(chain_id=137)`. Les ré-exposer côté
  polycopy crée un risque de drift (utilisateur set une adresse
  stale) sans bénéfice (cf. **D5** §4.5).
- Nouveau script
  [scripts/wrap_usdc_to_pusd.py](../../scripts/wrap_usdc_to_pusd.py) :
  ```python
  """Wrap USDC.e → Polymarket USD (pUSD) via CollateralOnramp.

  One-time helper avant le flip EXECUTION_MODE=live. En dry_run le
  bot ne signe aucun ordre live → wrap inutile.

  Usage :
      pip install -e ".[live]"   # web3.py optional dep
      python scripts/wrap_usdc_to_pusd.py --amount 100  # USDC à wrap

  Validator preflight : si EXECUTION_MODE=dry_run → log WARNING +
  abort (sauf flag `--force-dry-run` explicite).
  """
  ```
- Nouvelle dependency optionnelle :
  ```toml
  [project.optional-dependencies]
  live = ["web3>=6.0,<8.0"]
  ```
  Le helper import web3 **lazily** (try/except au top, raise clair
  "pip install -e \".[live]\"" si missing). Évite ~30 MB d'install
  forcée à 100% des utilisateurs dont 95%+ tournent en dry_run.

#### ME.5 — Builder code support optionnel

- 2 nouveaux settings :
  ```python
  polymarket_builder_code: str | None = Field(
      None,
      pattern=r"^0x[0-9a-fA-F]{64}$",
      description=(
          "Builder code Polymarket (bytes32, public, non-secret). Si "
          "set, le SDK V2 plomb la valeur dans chaque Order.builder "
          "via BuilderConfig — fee rebates apparents sur le Builder "
          "Leaderboard. Réclamer son code via "
          "polymarket.com/settings?tab=builder. Default None = aucun "
          "builder, comportement strict M3..M16 préservé. Cohérent "
          "discipline MACHINE_ID (public, loggé en clair)."
      ),
  )
  polymarket_builder_address: str | None = Field(
      None,
      pattern=r"^0x[a-fA-F0-9]{40}$",
      description=(
          "Adresse Ethereum du wallet builder (utilisée par "
          "BuilderConfig). Optional — si POLYMARKET_BUILDER_CODE "
          "set ET ce champ None, default à POLYMARKET_FUNDER. "
          "Public, loggé en clair."
      ),
  )
  ```
- `ClobWriteClient.__init__` instancie `BuilderConfig` uniquement si
  `polymarket_builder_code` est set, sinon laisse `None`. SDK skip
  naturellement le plombage Order.builder (default
  `BYTES32_ZERO`).
- ROI direct estimé : sur capital $1k + 5-10 BUYs/jour fee-enabled à
  ~1.5% fee = $0.75-1.50/jour fees. Rebate industriel 10-30% du fee
  = $0.07-0.45/jour économisés (extrapolation $50-150/an si capital
  reste). Coût implémentation ~30 LOC, trivial — ship dans le même
  PR (cf. **D9** §4.9).

#### ME.6 — Tests intégration V2

3 nouveaux tests opt-in (`pytest -m integration`) :

- [test_clob_v2_market_info_live.py](../../tests/integration/test_clob_v2_market_info_live.py)
  — smoke `getClobMarketInfo(condition_id)` retourne le schéma documenté
  (`mts/mos/fd/t/...`) sur un marché crypto fee-enabled réel + un marché
  politics fee-free.
- [test_clob_v2_signature_local.py](../../tests/integration/test_clob_v2_signature_local.py)
  — build un V2 order via `OrderBuilder.build_order(version=2)`,
  signe-le localement (via une clé de test dummy non-fundée),
  vérifie que le payload JSON wire contient bien
  `timestamp / metadata / builder` et que le `signature` est un hex
  valide. Aucun POST réel.
- [test_clob_v2_fee_rate_live.py](../../tests/integration/test_clob_v2_fee_rate_live.py)
  — feed un `condition_id` réel, vérifie que `FeeQuote.rate` et
  `FeeQuote.exponent` sont cohérents avec la doc (Crypto fee-enabled
  > 0, Politics fee-free = 0).

Captures fixtures associées :
- [tests/fixtures/clob_v2_market_crypto_sample.json](../../tests/fixtures/clob_v2_market_crypto_sample.json)
  ← capture live 2026-04-27 (BTC up-or-down crypto, `fd:{r:0.072, e:1, to:true}`).
- [tests/fixtures/clob_v2_market_fee_free_sample.json](../../tests/fixtures/clob_v2_market_fee_free_sample.json)
  ← capture live 2026-04-27 (politics market, pas de `fd`).

#### ME.7 — Procédure cutover ops

[docs/todo.md §14](../todo.md#L554) enrichi avec 4 phases :

- **Phase 1 — lundi 27 avril ~22h UTC** : merge sur main + restart
  bot V2. Le SDK V2 query `/version` → backend renvoie
  `version=1` → SDK signe des V1 orders. Sanity check : 1 cycle
  dry_run OK avec un `MyOrder.status="SIMULATED"` correct.
- **Phase 2 — mardi 28 avril ~10h-12h UTC** : pas d'action requise.
  Polymarket cutover backend ~11h UTC. Le SDK détecte le
  `order_version_mismatch` à la prochaine tentative POST → call
  `_resolve_version(force_update=True)` → backend renvoie
  `version=2` → SDK commence à signer V2 sans intervention humaine.
- **Phase 3 — mardi 28 avril ~11h30-12h00 UTC** : smoke test
  post-cutover (cf. §12 commandes vérification). Vérifier 1 ordre
  SIMULATED OK + au moins 1 `fee_rate_fetched` event sur un marché
  crypto.
- **Phase 4 — mercredi 29 avril toute la journée** : monitoring 24h.
  Telegram heartbeat OK, dashboard `/strategie` decisions
  APPROVED se concrétisent en `MyOrder` valides. Aucune erreur
  `signature_invalid` dans les logs.

### 2.2 Hors scope explicites (liste exhaustive)

- ❌ **EIP-1271 smart contract wallets support** — annoncé V2 mais
  pas documenté en détail. polycopy utilise EOA
  (`signature_type=0`) ou Gnosis Safe (`signature_type=2`).
  Reportable feature future (M19+) si on a besoin de plomber un
  compte smart contract.
- ❌ **Migration des positions historiques V1 vers schema V2** — pas
  applicable, pas de changement schema polycopy. Le user peut
  reset la DB via [docs/todo.md §3](../todo.md#L141) si désiré
  (orthogonal à V2).
- ❌ **Multi-collateral support** (USDC.e + pUSD coexistant) — V2
  élimine USDC.e du collateral. Pas de besoin de support dual.
- ❌ **Builder code marketplace / fee tier optimization** — feature
  business future, pas dev infra.
- ❌ **Backward-compat layer V1↔V2 polycopy-side** — Polymarket le
  fait côté backend (auto-switch via version endpoint) + le SDK V2
  gère les deux versions. Inutile de réimplémenter côté polycopy.
- ❌ **Refactor du DB schema collateral_token** — `MyOrder` /
  `MyPosition` ne stockent pas le collateral. Aucune migration DB
  requise (cf. **D13** §4.13).
- ❌ **Maker fees / rebates côté polycopy** — polycopy est
  taker-only (FOK orders). V2 invariant : makers ne paient JAMAIS
  de fees (`fd.to=true`). Pas de logique maker-side.
- ❌ **Fee-rate WebSocket** — endpoint REST + cache 60s suffit (rate
  Polymarket ne change pas par seconde). Pas de WSS dédié documenté
  côté Polymarket.
- ❌ **Backtest historique avec fees V2** — M18 gère le live +
  dry-run. Backtest fees historiques = spec future si besoin (par
  ex. SIMULATION mode harness).
- ❌ **Dashboard panel "V2 migration status"** — éphémère (utile
  uniquement la semaine du cutover). Pas de valeur long-terme.
  L'observabilité passe par les logs `executor_creds_ready` +
  `fee_rate_fetched` qui exposent déjà le mode V2.
- ❌ **Alertes Telegram dédiées cutover** — heartbeat M7 existant
  + alerte `executor_auth_fatal` M3 couvrent les cas de breakage.
  Pas d'alerte custom dédiée.
- ❌ **Mise à jour `polymarket-apis>=0.5.0`** ([pyproject.toml:12](../../pyproject.toml#L12)) :
  cette dep utilise des endpoints Gamma + Data API publics (pas
  CLOB write), donc inchangée par V2. Pas de bump nécessaire.
  À surveiller post-cutover si Polymarket modifie `gamma-api`
  rétroactivement.
- ❌ **Préservation V1 bot pour rollback** : Polymarket V1 backend
  est offline post-cutover. Aucune possibilité de "rollback to V1"
  côté polycopy. Le rollback se fait par hotfix sur la branche V2
  (cf. §11.5).

---

## 3. User stories

### 3.1 Story A — Bot continue de tourner pendant le cutover (D11)

**Avant M18** (situation hypothétique 28 avril ~11h UTC sans M18) :

- 10:55 UTC : bot polycopy V1 tourne, dernière SIMULATED OK.
- 11:00 UTC : Polymarket bascule backend V1 → V2.
- 11:01 UTC : prochain `OrderApproved` arrive depuis pipeline
  M2/M16. `clob_write_client.create_and_post_order(...)` appelle
  le SDK V1 → POST `/order` → backend V2 reçoit signature V1
  domain version `"1"` → renvoie 400 `signature_invalid`.
- `MyOrder.status="REJECTED"`, `error_msg="signature_invalid"`,
  log `executor_error`, alerte ERROR Telegram (cooldown 60s sur
  `executor_error`).
- Bot continue à boucler, **TOUS les BUY échouent silencieusement**.
- Utilisateur regarde le dashboard `/strategie` → "5/5 REJECTED
  signature_invalid sur 1h" → comprend le problème mais doit
  upgrader manuellement.

**Avec M18 + D11 ship pré-cutover** :

- Lundi 27 avril 22h UTC : merge M18 sur main, `git pull` sur
  prod, `pip install -e .` (récupère
  `py-clob-client-v2==1.0.0`), `systemctl restart polycopy`.
- Au boot : `ClobWriteClient.__init__` log
  `executor_creds_ready signature_type=2` (signature_type Magic
  proxy par ex.).
- Premier order SIMULATED en dry_run : pas de POST réel. Sanity
  check OK. Dashboard `/strategie` montre les SIMULATED nominaux.
- Mardi 28 avril 10:00 UTC : bot tourne depuis 12h, ~50
  SIMULATED, 0 erreur.
- 10:55 UTC : bot toujours nominal. SDK V2 signe des V1 orders
  car backend `/version=1`.
- 11:00 UTC : Polymarket bascule backend → V2.
- 11:01 UTC : prochain `OrderApproved`, SDK V2 tente POST avec
  signature V1. Backend V2 répond `order_version_mismatch`.
- SDK V2 catch ce code spécifique → `_resolve_version(force_update=True)`
  → call `/version` → backend renvoie `version=2` → SDK
  re-build l'order en V2 (avec `timestamp` ms + `metadata` +
  `builder`) → POST → 200 OK.
- En dry_run : pas de POST réel. Le SDK ne fait pas le
  re-resolve sans fail. À la prochaine erreur (ex: clock skew)
  il le ferait. Mais en dry_run on ne POST jamais → pas
  d'opportunité de trigger le resolve. **Solution** :
  `polymarket_use_server_time=True` force le SDK à query
  `/time` avant chaque sign, ce qui revalide la version
  implicitement (car `_get_timestamp` partage le même
  pipeline HTTP).
- Dashboard reste vert, utilisateur dort.

### 3.2 Story B — Fees V2 consommés directement depuis le protocole (D6)

**Avant M18** (M16 hardcoded mapping) :

- BUY copié sur marché crypto réel (BTC up-or-down) à p=0.5,
  `feeType="crypto_fees_v2"`.
- `_compute_effective_fee_rate` consomme le mapping hardcodé :
  `(0.25, 2)` → `effective_rate = 0.25 × 0.25^2 = 0.015625` (1.5625%).
- `fee_cost = my_size × p × 0.015625`.
- **Mais** la formule réelle V2 (capturée live 2026-04-27,
  fixture `clob_v2_market_crypto_sample.json`) est
  `fd.r=0.072, fd.e=1` → `effective_rate = 0.072 × 0.25^1 = 0.018` (1.80%).
- M16 sous-estime le fee de **0.235%** sur ce marché à p=0.5 →
  bot accepte des trades EV-marginaux qui sont en réalité
  EV-négatifs.
- Aux p extrêmes (ex: p=0.9) : M16 calcule
  `0.25 × 0.09^2 = 0.002025` (0.20%) ; V2 réel
  `0.072 × 0.09^1 = 0.00648` (0.65%). Encore sous-estimation.

**Avec M18 (D6)** :

- BUY copié sur même marché.
- `PositionSizer._check_buy` call `await
  fee_rate_client.get_fee_quote(asset_id, condition_id=ctx.trade.condition_id)`.
- `FeeRateClient` HTTP `GET /clob-markets/{condition_id}` →
  parse `result["fd"]` → `FeeQuote(rate=Decimal("0.072"), exponent=1)`.
- `_compute_effective_fee_rate(quote, p=Decimal("0.5"))` :
  `effective_rate = quote.rate × (p × (1-p)) ** quote.exponent`
  `= 0.072 × 0.25^1 = 0.018` (1.80%).
- Cohérent avec la doc Polymarket live + le calcul on-chain
  du protocole. Pipeline correct sans patch périodique du
  mapping `feeType`.
- Bonus : si Polymarket ajoute un nouveau `feeType` post-cutover
  (ex: `politics_fees_v1`), pipeline marche out-of-the-box (consume
  `fd.e` directement).

### 3.3 Story C — Builder code rebate (D9)

- Utilisateur réclame son builder code via
  [polymarket.com/settings?tab=builder](https://polymarket.com/settings?tab=builder).
  Reçoit `0xab12...cd34` (32 bytes hex).
- Édite `.env` : `POLYMARKET_BUILDER_CODE=0xab12...cd34`. Pas
  besoin de redeploy — restart suffit.
- `Settings._validate_builder_*` validators Pydantic checkent le
  format hex32. Si invalide → boot crash clair.
- Au prochain restart, `ClobWriteClient.__init__` log
  `executor_creds_ready signature_type=2 builder_code_set=true`
  (le code lui-même n'est PAS loggé — public mais pas de raison
  de le spam dans les logs).
- SDK V2 instancie `BuilderConfig(builder_address=settings.polymarket_funder,
  builder_code="0xab12...cd34")` dans le constructor.
- À chaque `create_and_post_order`, le SDK regarde
  `OrderArgs.builder_code` :
  - Si pas set côté caller → SDK auto-fill avec
    `builder_config.builder_code`.
  - Si set côté caller → conserve la valeur per-order (override).
- L'ordre signé V2 a `Order.builder = "0xab12...cd34"` plombé.
- À chaque fill, Polymarket calcule un rebate (% non documenté
  publiquement, range industrie 10-30%) et le crédite au
  `builder_address` onchain.
- Observation utilisateur : 7 jours plus tard, transactions
  Polymarket Builder Leaderboard montrent des rebates accumulés.

### 3.4 Story D — Wrap pUSD pré-flip live (D12)

- Utilisateur teste 14 jours en dry_run, satisfait des SIMULATED.
  Décide de flip live.
- Édite `.env` : `EXECUTION_MODE=live` (commenté pour l'instant —
  ne restart PAS encore).
- Vérifie son solde USDC.e Polygon via Etherscan : 100 USDC.e sur
  l'adresse `polymarket_funder`.
- Setup le wrap script :
  ```bash
  pip install -e ".[live]"   # installe web3>=6.0
  python scripts/wrap_usdc_to_pusd.py --amount 100
  ```
- Le script :
  1. Lit `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`,
     `POLYMARKET_COLLATERAL_ONRAMP_ADDRESS`,
     `POLYMARKET_USDC_E_ADDRESS` depuis `.env`.
  2. Connecte web3 à un endpoint RPC Polygon (env
     `POLYGON_RPC_URL`, default Alchemy/QuickNode public).
  3. Approve USDC.e → Onramp pour `100 × 10^6` (USDC = 6
     decimals).
  4. Wait 1 confirmation.
  5. Call `onramp.wrap(USDC.e_addr, funder, 100 × 10^6)`.
  6. Wait 1 confirmation.
  7. Vérifie le solde pUSD post-wrap (doit être 100).
  8. Logs structlog `wrap_usdc_to_pusd_completed` avec gas spent,
     tx hashes, balance pUSD.
- 5 minutes plus tard : 100 pUSD sur `polymarket_funder`.
- Édite `.env` : décomment `EXECUTION_MODE=live`. Restart bot.
- Premier ordre live : `ClobClient` POST V2 → backend reconnaît
  pUSD comme collateral → match OK → `MyOrder.status="FILLED"`.

---

## 4. Architecture / décisions clefs

Cette section détaille les **14 décisions architecturales** prises
pendant la spec writing, basées sur l'inspection live du SDK
[Polymarket/py-clob-client-v2](https://github.com/Polymarket/py-clob-client-v2)
+ doc officielle + tests endpoints `clob-v2.polymarket.com` (toutes
validées 2026-04-27).

### 4.1 D1 — Le SDK V2 garde le constructor V1-style

**Brief ME.5 dit** : `ClobClient({...})` avec dict options, `chain`
au lieu de `chainId`. Refactor structurel polycopy.

**Réalité** ([client.py L121-L142](https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/client.py)) :

```python
class ClobClient:
    def __init__(
        self,
        host: str,
        chain_id: int,         # PAS renommé en `chain`
        key: str = None,
        creds: ApiCreds = None,
        signature_type: int = None,
        funder: str = None,
        builder_config: BuilderConfig = None,    # nouveau V2
        use_server_time: bool = False,           # nouveau V2
        retry_on_error: bool = False,            # nouveau V2
    ):
```

Le brief était **factuellement inexact**. Le constructor V2 est
**90% identique au V1** — seuls 3 nouveaux kwargs (tous optionnels,
default `None`/`False`) s'ajoutent.

**Décision** : diff polycopy minimal — juste les 3 imports + 1
method rename (D2). Pas de refactor structurel. Le code de
[clob_write_client.py:55-69](../../src/polycopy/executor/clob_write_client.py#L55-L69)
reste 95% intact (les kwargs `key/creds/signature_type/funder`
sont passés tels quels).

### 4.2 D2 — Une seule méthode renommée

`temp_client.create_or_derive_api_creds()` (V1) →
`temp_client.create_or_derive_api_key()` (V2). C'est la seule
signature publique qui change côté SDK
([client.py:476-483](https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/client.py#L476-L483)).

Action polycopy :
- [clob_write_client.py:60](../../src/polycopy/executor/clob_write_client.py#L60) →
  rename.
- [tests/unit/test_clob_write_client.py:32](../../tests/unit/test_clob_write_client.py#L32) →
  mock rename.
- [tests/integration/test_clob_l1_l2_auth_live.py:18](../../tests/integration/test_clob_l1_l2_auth_live.py#L18) →
  rename.

### 4.3 D3 — `OrderArgsV2` est un superset additif de V1

**Brief ME.2 dit** : `expiration` retiré du signed struct V2.

**Réalité** ([clob_types.py L72-L88](https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/clob_types.py)) :

```python
@dataclass
class OrderArgsV2:
    token_id: str
    price: float
    size: float
    side: str
    expiration: int = 0          # CONSERVÉ (default 0 = no expiration)
    builder_code: str = BYTES32_ZERO
    metadata: str = BYTES32_ZERO


# Alias: default to V2
OrderArgs = OrderArgsV2
```

Ce qui est retiré du **signed EIP-712 typed data** (`Order(uint256
salt, …, uint256 timestamp, bytes32 metadata, bytes32 builder)`)
ce sont `feeRateBps`, `taker`, `nonce`. Mais `expiration` reste
dans `OrderDataV2` et le payload JSON wire (cf.
`order_to_json_v2` qui inclut `expiration` au top du `order` dict).

→ Polycopy n'a rien à faire : on n'a jamais set `expiration`
côté `BuiltOrder` ni `_build_order_args`. Le default 0 reste correct.

### 4.4 D4 — Side enum : passer la string directement

V1 : `from py_clob_client.order_builder.constants import BUY, SELL`
(str constants `"BUY"` / `"SELL"`).

V2 : `from py_clob_client_v2 import Side` (IntEnum
`Side.BUY=0, Side.SELL=1`), MAIS le SDK V2 accepte **aussi** les
strings `"BUY"`/`"SELL"`. Cf.
[builder.py L69-L82](https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/order_builder/builder.py#L69-L82) :

```python
def get_order_amounts(self, side, size: float, price: float, ...):
    if isinstance(side, Side):
        side = BUY if side == Side.BUY else SELL  # converti en string
    ...
```

→ **Décision** : passer `built.side` (string `"BUY"`/`"SELL"`)
directement à `OrderArgs(side=...)`. Plus simple, pas de
conversion intermédiaire, SDK-compatible. Élimine 2 imports
(`from py_clob_client.order_builder.constants import BUY, SELL`).

```python
# AVANT (V1)
side_const = BUY if built.side == "BUY" else SELL
return OrderArgs(token_id=built.token_id, price=built.price,
                 size=built.size, side=side_const)

# APRÈS (V2)
return OrderArgs(token_id=built.token_id, price=built.price,
                 size=built.size, side=built.side)
```

### 4.5 D5 — Adresses contrats : ne PAS exposer en env vars (hors CollateralOnramp)

**Brief ME.4 dit** : `POLYMARKET_EXCHANGE_V2_ADDRESS` +
`POLYMARKET_NEG_RISK_EXCHANGE_V2_ADDRESS` + `POLYMARKET_PUSD_ADDRESS`
en env vars.

**Réalité** ([config.py SDK](https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/config.py)) :

```python
def get_contract_config(chain_id: int) -> ContractConfig:
    CONFIG = {
        137: ContractConfig(
            exchange="0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
            neg_risk_exchange="0xC5d563A36AE78145C45a50134d48A1215220f80a",
            exchange_v2="0xE111180000d2663C0091e4f400237545B87B996B",
            neg_risk_exchange_v2="0xe2222d279d744050d28e00520010520000310F59",
            collateral="0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB",  # = pUSD
            conditional_tokens="0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
            neg_risk_adapter="0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
        ),
        80002: ContractConfig(...)  # Amoy testnet
    }
```

Le SDK gère déjà ces 6+ adresses. Les ré-exposer en env vars
polycopy crée :

1. **Risque de drift** : utilisateur set une adresse stale,
   silencieux, le bot signe contre le mauvais contrat → signatures
   rejetées sans message clair pour l'utilisateur.
2. **Aucun bénéfice** : si Polymarket re-deploy un contrat, le SDK
   bumpe sa version, l'utilisateur run `pip install -U
   py-clob-client-v2`. Pas besoin de polycopy.

**Décision** : **seul** `POLYMARKET_COLLATERAL_ONRAMP_ADDRESS` est
exposé en env var (default `0x93070a847efEf7F70739046A929D47a521F5B8ee`,
confirmé live 2026-04-27 sur
`docs.polymarket.com/resources/contracts`). Raison : ce contrat
n'est **pas** consommé par `ClobClient` ; il est utilisé uniquement
par le helper script `wrap_usdc_to_pusd.py`. Idem
`POLYMARKET_USDC_E_ADDRESS` (default
`0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`).

Les 4+ autres adresses (Exchange v1, Exchange v2, Neg Risk Exchange
v1, Neg Risk Exchange v2, pUSD/collateral, ConditionalTokens, Neg
Risk Adapter) restent owned par le SDK upstream.

Cohérent CLAUDE.md "pas de hardcode des adresses contrats" : le
constraint vise polycopy, pas les SDK upstream. Si un upstream
Polymarket hardcode des adresses, c'est leur responsabilité.
Polycopy reste libre du hardcode tant qu'il **ne réinvente pas**
les adresses.

### 4.6 D6 — `FeeRateClient` consomme `fd.r` ET `fd.e` du nouvel endpoint

**Brief ME.3 dit** : "extraire `fd.r` et `fd.e`, formule polycopy
préservée".

**Mais** l'inspection vivante 2026-04-27 (curl
`https://clob-v2.polymarket.com/clob-markets/{id}` sur 1 marché
crypto fee-enabled) révèle :

```json
{
  "fd": {"r": 0.072, "e": 1, "to": true},
  ...
}
```

Soit `effective_rate = 0.072 × (p × (1-p))^1 = 0.072 × 0.25^1 = 1.80%`
à p=0.5.

**Or M16's mapping hardcodé** ([pipeline.py:293-334](../../src/polycopy/strategy/pipeline.py#L293-L334)) :

```python
if fee_type == "crypto_fees_v2":
    fee_rate_param, exponent = Decimal("0.25"), 2
```

→ `effective_rate = 0.25 × 0.25^2 = 1.5625%` à p=0.5.

**M16 sous-estime le fee de 0.235%** sur les markets crypto. Aux
p extrêmes (ex p=0.9), M16 calcule 0.20% ; V2 réel = 0.65%. M16
sous-estime de 3× (!).

**Décision** :

1. `FeeRateClient` introduit un **nouveau DTO** `FeeQuote(rate:
   Decimal, exponent: int)`.
2. **Nouvelle méthode** `FeeRateClient.get_fee_quote(token_id, *,
   condition_id=None) -> FeeQuote` — c'est l'API canonique V2.
   Consomme `getClobMarketInfo(condition_id)` et extrait `(fd.r,
   fd.e)`.
3. **Ancienne méthode** `get_fee_rate(token_id) -> Decimal`
   **préservée** comme alias deprecated (retourne `quote.rate`) —
   backward-compat M16 stricte (zéro modif tests M16). Warning
   structlog 1× par token au premier appel (LRU 500).
4. **`PositionSizer._compute_effective_fee_rate` réécrit** pour
   consommer directement `quote.exponent` au lieu du mapping
   `feeType` :
   ```python
   effective_rate = quote.rate * (p * (1 - p)) ** quote.exponent
   ```
   Le mapping `feeType → (...)` hardcodé disparaît. La logique
   fallback reste : si réseau down → fallback `Decimal("0.018")`
   conservateur (M16 §11.5 préservé).
5. `ctx.market.fee_type` reste tracé pour audit/dashboard mais ne
   pilote plus la math fee.

**Justification long-terme** : la formule du protocole **EST** la
source de vérité. Mieux vaut consommer `fd.e` que dériver via
`feeType` (qui est un metadata Gamma, pas le protocole CLOB). Si
Polymarket ajoute un nouveau feeType post-cutover (ex:
`politics_fees_v1`, `finance_fees_v1`), le pipeline marche
out-of-the-box sans patch côté polycopy.

### 4.7 D7 — `POLYMARKET_CLOB_HOST` configurable, partagé par TOUS les clients HTTP

**Problème** : pré-cutover, il faut tester contre
`https://clob-v2.polymarket.com`. Plusieurs clients polycopy
hardcodent l'URL :

- [clob_write_client.py:25](../../src/polycopy/executor/clob_write_client.py#L25)
  `_HOST = "https://clob.polymarket.com"`
- [clob_metadata_client.py:27](../../src/polycopy/executor/clob_metadata_client.py#L27)
  `BASE_URL = "https://clob.polymarket.com"`
- [fee_rate_client.py:87](../../src/polycopy/executor/fee_rate_client.py#L87)
  `BASE_URL = "https://clob.polymarket.com"`
- [strategy/clob_read_client.py](../../src/polycopy/strategy/clob_read_client.py)
  (idem)
- [strategy/clob_orderbook_reader.py](../../src/polycopy/executor/clob_orderbook_reader.py)
  (idem)

**Décision** : nouveau setting

```python
polymarket_clob_host: str = Field(
    "https://clob.polymarket.com",
    pattern=r"^https://[a-zA-Z0-9.-]+(?::\d+)?$",
    description=(
        "Host CLOB Polymarket (REST). Pré-cutover V2 (avant 28 avril "
        "2026 ~11h UTC) : peut pointer sur "
        "https://clob-v2.polymarket.com pour tester contre le testnet. "
        "Post-cutover : prod URL bascule automatiquement sur le "
        "backend V2, default OK. Consommé par ClobReadClient, "
        "ClobMetadataClient, ClobOrderbookReader, FeeRateClient, "
        "ClobWriteClient (via SDK ClobClient.host). "
        "Pattern `^https://...` strict — pas de `http://` accepté."
    ),
)
```

Tous les clients HTTP polycopy consomment
`settings.polymarket_clob_host` au lieu de hardcoder. WebSocket
reste piloté par `STRATEGY_CLOB_WS_URL` (déjà existant) — séparé
car infra différente (wss vs https, channel `market` public, pas de
breakdown V2).

### 4.8 D8 — `use_server_time=True` par défaut

V2 introduit le champ `timestamp` (millisecondes,
`int(time.time_ns() // 1_000_000)`) dans le signed Order struct.
Clock-skew client → backend rejection `signature_invalid`. Le SDK
V2 offre `use_server_time=True` qui fetch `/time` avant chaque
sign et utilise l'horloge serveur (`OrderBuilder.build_order` line
135 dans
[builder.py](https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/order_builder/builder.py)
fait `ts = str(time.time_ns() // 1_000_000)`, mais le client.py
peut override avec `_get_timestamp()` qui consume `/time`).

**Coût** : +1 round-trip HTTP par order signing (~50-200ms via
HTTPS Polygon → Polymarket Cloudflare). Négligeable dans le
pipeline polycopy 10-15s end-to-end (cf. [docs/architecture.md
§Latence](../architecture.md#latence)).

**Bénéfice** : zéro risque silencieux de rejection sur clock drift.
La VM prod peut dériver de plusieurs secondes après suspend/resume
(cas typique : MacBook qui dort la nuit, NTP asynchrone au wake).
Sans ce flag, polycopy peut générer des orders avec `timestamp` qui
diverge de l'horloge backend → `signature_invalid` ou
`order_too_old`.

**Décision** : nouveau setting `polymarket_use_server_time: bool =
Field(True, ...)` (opt-out via env var pour tests/debug).
`ClobWriteClient.__init__` passe le flag au SDK constructor.

```python
polymarket_use_server_time: bool = Field(
    True,
    description=(
        "M18 : si True, le SDK V2 ClobClient fetch `/time` avant "
        "chaque order sign et utilise l'horloge serveur pour le "
        "champ `timestamp` (ms) du signed struct V2. Coût +1 "
        "round-trip HTTP/sign (~50-200ms), bénéfice zéro risque de "
        "rejection sur clock drift. Désactivable pour tests/debug."
    ),
)
```

### 4.9 D9 — Builder code : ship dans le cutover, default `None`

**ROI direct estimé** (calcul conservateur sur capital $1k) :

- 5-10 BUYs/jour, dont ~10% sur fee-enabled markets (crypto + sports
  v2 post-rollout March 30 2026 + extensions).
- Fee moyen sur fee-enabled : ~1.5% du notional (cf. M16 §1.1 obs).
- Fee total : 0.5-1.5 BUYs × $1k × 0.01 (capital ratio) × 1.5% =
  $0.075-0.225/jour.
- Builder rebate Polymarket (range industriel 10-30%, exact %
  non-documenté) : $0.007-0.067/jour = $2.5-25/an.
- Si capital ↗ à $10k : $25-250/an.

**Coût implémentation** : ~30 LOC (1 setting + validator hex32 +
constructor passthrough). Trivial.

**Décision** : ship ME.5 dans la même release. Le coût est
minuscule + l'utilisateur peut activer post-restart en éditant
`.env` (pas de redeploy).

```python
polymarket_builder_code: str | None = Field(
    None,
    pattern=r"^0x[0-9a-fA-F]{64}$",
    description=(
        "Builder code Polymarket (bytes32, public, non-secret). Si "
        "set, le SDK V2 plomb la valeur dans chaque Order.builder "
        "via BuilderConfig — fee rebates apparents sur le Builder "
        "Leaderboard. Réclamer son code via "
        "polymarket.com/settings?tab=builder. Default None = aucun "
        "builder, comportement strict M3..M16. Cohérent discipline "
        "MACHINE_ID (public, loggé en clair)."
    ),
)
polymarket_builder_address: str | None = Field(
    None,
    pattern=r"^0x[a-fA-F0-9]{40}$",
    description=(
        "Adresse Ethereum du wallet builder (utilisée par "
        "BuilderConfig). Optional — si POLYMARKET_BUILDER_CODE set "
        "ET ce champ None, default à POLYMARKET_FUNDER. Public, "
        "loggé en clair."
    ),
)
```

Builder code reste **PUBLIC** (apparaît onchain dans `Order.builder`).
Aucune discipline secret-style (vs `POLYMARKET_PRIVATE_KEY`). Loggé
OK dans `executor_creds_ready` (juste un flag bool
`builder_code_set=true/false`, pas la valeur — pour réduire le
spam log).

`ClobWriteClient.__init__` :

```python
builder_config: BuilderConfig | None = None
if settings.polymarket_builder_code is not None:
    builder_config = BuilderConfig(
        builder_address=(
            settings.polymarket_builder_address
            or settings.polymarket_funder
        ),
        builder_code=settings.polymarket_builder_code,
    )

self._client = ClobClient(
    self._host,
    chain_id=137,
    key=settings.polymarket_private_key,
    creds=api_creds,
    signature_type=settings.polymarket_signature_type,
    funder=settings.polymarket_funder,
    builder_config=builder_config,
    use_server_time=settings.polymarket_use_server_time,
)
```

### 4.10 D10 — `condition_id` propagé jusqu'à `FeeRateClient`

`getClobMarketInfo` prend un `condition_id` en input.
`FeeRateClient.get_fee_quote(token_id)` est exposé côté pipeline
qui a `token_id` mais pas immédiatement `condition_id`.

**Mauvaise option** : `FeeRateClient` fait 1 appel Gamma
`/markets-by-token/{token_id}` pour résoudre `token_id →
condition_id` à chaque cycle. = +50-200ms + 50× plus de calls
Gamma (vs cache 60s actuel). À chaque BUY copié sur un nouveau
token = 1 lookup Gamma supplémentaire.

**Bonne option** : la pipeline a déjà `condition_id` dans le `ctx`
au moment où `_check_buy` est appelée. `MarketFilter` tourne en
amont (premier filtre après TraderLifecycle dans
[strategy/pipeline.py:466-472](../../src/polycopy/strategy/pipeline.py#L466-L472)),
et `ctx.trade.condition_id` est dispo via le DTO
`DetectedTradeDTO`. On le passe explicitement.

**Décision** :

1. Signature `FeeRateClient.get_fee_quote(token_id: str, *,
   condition_id: str | None = None) -> FeeQuote`.
2. Si `condition_id` fourni → call direct
   `getClobMarketInfo(condition_id)`, zéro Gamma overhead. **Path
   nominal du pipeline polycopy**.
3. Si `condition_id=None` (legacy / tests / safety net) → fallback
   Gamma `/markets-by-token/{token_id}` avec cache LRU dédié
   (`_token_to_cid: OrderedDict[str, str]` TTL 5 min, max 500
   entries). Warning structlog
   `fee_rate_client_token_id_resolved_via_gamma` pour signaler
   l'inefficacité au dev.
4. `_check_buy` updated :
   ```python
   quote = await self._fee_rate_client.get_fee_quote(
       ctx.trade.asset_id,
       condition_id=ctx.trade.condition_id,
   )
   ```

### 4.11 D11 — Stratégie de déploiement : ship AVANT cutover

**Découverte clé** ([client.py L811-L838](https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/client.py#L811-L838)) :
le SDK V2 est **dual-version capable** :

```python
def post_order(self, order, order_type=OrderType.GTC, ...):
    self.assert_level_2_auth()
    owner = self.creds.api_key or ""
    order_payload = (
        order_to_json_v2(order, owner, order_type, ...) if _is_v2_order(order)
        else order_to_json_v1(order, owner, order_type, ...)
    )
    serialized = json.dumps(order_payload, ...)
    headers = self._l2_headers("POST", POST_ORDER, body=order_payload, ...)
    res = self._post(f"{self.host}{POST_ORDER}", headers=headers, data=serialized)

    if self._is_order_version_mismatch(res):
        self.__resolve_version(force_update=True)

    return res
```

Et `OrderBuilder.build_order(version=N)` choisit V1 ou V2 selon le
résultat de `_resolve_version()` qui query `/version` au boot puis
re-retry sur `order_version_mismatch`.

→ **On peut shipper le SDK V2 AVANT le cutover** (lundi 27 avril
~22h UTC), le SDK signera des V1 orders en attendant (backend
`/version=1`), puis flippera automatiquement le 28 avril ~11h UTC
(backend `/version=2` → SDK détecte le mismatch sur la prochaine
POST → re-resolve → signe V2 + retry).

**Décision** : la spec §11 Migration recommande **explicitement** :

1. **Phase 1 (lundi 27 avril ~22h UTC)** : merge sur main, restart
   bot V2. Le SDK V2 query `/version` au boot, backend renvoie
   `version=1`, SDK signe V1. Sanity check : 1 cycle dry_run OK.
2. **Phase 2 (mardi 28 avril ~11h UTC)** : pas d'action requise.
   Backend Polymarket bascule. À la prochaine POST tentative, le
   SDK reçoit `order_version_mismatch`, call
   `_resolve_version(force_update=True)`, détecte le flip, et
   signe V2.
3. **Phase 3 (mardi 28 avril ~11h30 UTC)** : smoke test
   post-cutover (cf. §12).

Cette stratégie **élimine la fenêtre 11h00-11h30 UTC du 28 avril**
comme moment critique → bot tourne en continu, zéro intervention
humaine.

**En dry_run il n'y a pas de POST**. Comment le SDK détecte-t-il le
flip backend ? Réponse : il ne le détecte pas tant que `_post_order`
n'est pas appelé. Mais en dry_run M3 + M8, polycopy ne POST jamais
en mode `dry_run` → ça n'a pas d'importance, le path live n'est
pas exercé. La transition V1→V2 se fait **automatiquement à la
première vraie POST live** post-flip `EXECUTION_MODE=live`. Si
l'utilisateur reste en dry_run éternellement, le bot fonctionne
identique.

### 4.12 D12 — Wrap helper : web3.py en optional dependency

Brief dit : nouveau `scripts/wrap_usdc_to_pusd.py` ~80 LOC.

**Problème** : web3.py n'est PAS dans
[pyproject.toml dependencies](../../pyproject.toml#L10-L27). L'ajouter en
dependency core impose ~30 MB d'install (eth-account + cryptography
+ pycryptodome + Cython) à 100% des utilisateurs, dont 95%+
tournent en dry_run.

**Décision** :

```toml
[project.optional-dependencies]
dev = [...existing...]
docs = [...existing...]
live = ["web3>=6.0,<8.0"]
```

Le helper [scripts/wrap_usdc_to_pusd.py](../../scripts/wrap_usdc_to_pusd.py)
import web3 **lazily** :

```python
try:
    from web3 import Web3
    from web3.middleware import geth_poa_middleware
except ImportError as e:
    raise SystemExit(
        "web3.py n'est pas installé. Pour utiliser ce script :\n"
        '   pip install -e ".[live]"\n'
        "Cf. spec M18 §ME.4 + docs/setup.md."
    ) from e
```

Documenté dans
[docs/setup.md](../setup.md) : "Avant le flip
`EXECUTION_MODE=live` (one-time), `pip install -e \".[live]\"` puis
exécuter le wrap." Cohérent avec l'invariant CLAUDE.md "ce bot ne
fait pas du HFT" — installer web3 juste pour le pré-flip live one-shot
est acceptable.

Le script :
- Lit `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`,
  `POLYMARKET_COLLATERAL_ONRAMP_ADDRESS` (default supplied),
  `POLYMARKET_USDC_E_ADDRESS` (default supplied),
  `POLYGON_RPC_URL` (env, sans default — utilisateur doit fournir),
  `WRAP_AMOUNT_USDC` (CLI arg `--amount`).
- Approve USDC.e → onramp pour `amount * 10^6`.
- Call `onramp.wrap(USDC.e_addr, funder, amount * 10^6)`.
- Vérifie le solde pUSD post-wrap.
- Logs structlog uniquement (pas de `print` cf. CLAUDE.md
  conventions).
- Validator preflight : si `EXECUTION_MODE=dry_run` → log WARNING
  + abort (sauf flag `--force-dry-run` explicite).

### 4.13 D13 — Pas de migration Alembic

Confirmé par lecture
[src/polycopy/storage/models.py](../../src/polycopy/storage/models.py) :
aucun schema DB polycopy ne stocke le collateral token. Les tables
`MyOrder` / `MyPosition` / `pnl_snapshots` ont `condition_id` /
`asset_id` / `total_usdc` (numérique abstrait — pUSD = USDC 1:1, le
nombre reste pareil). Aucune nouvelle colonne. Aucune migration.

`alembic upgrade head` doit retourner "no migrations to apply"
post-M18. Le current head reste **0010** (M17 MD.3 — la dernière
migration, cf.
[alembic/versions/](../../alembic/versions/)).

Cohérent stratégie versioning sacré : append-only sur `pnl_snapshots`,
`trader_scores`, `trader_events`. Aucune row réécrite. Aucun
rollback nécessaire.

### 4.14 D14 — Pas de touche au scoring (M14/M15)

V2 est purement infra exchange-side. Le scoring v2.1 (M14) /
v2.1.1 (M15) est calculé sur l'historique on-chain des wallets
cibles, pas sur nos ordres. Diff M18 strictement orthogonal au
scoring.

Vérifié par grep : aucun import croisé entre
`src/polycopy/discovery/scoring/` et `src/polycopy/executor/`.
Aucune fonction `compute_score_v2_1` / `compute_score_v2_1_1` ne
touche `ClobClient` / `OrderArgs` / `BuiltOrder`. Le registry
`SCORING_VERSIONS_REGISTRY` reste intact.

---

## 5. Algorithmes

### 5.1 SDK V2 dual-version dispatch

Code SDK qui pilote le hot-swap V1↔V2
([client.py L249-L259, L811-L838](https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/client.py)) :

```python
# client.py L249-L259
def get_version(self) -> int:
    try:
        result = self._get(f"{self.host}{VERSION}")
        return result.get("version", 2) if isinstance(result, dict) else 2
    except Exception:
        return 2

def __resolve_version(self, force_update: bool = False) -> int:
    if self.__cached_version is not None and not force_update:
        return self.__cached_version
    self.__cached_version = self.get_version()
    return self.__cached_version


# client.py L811-L838
def post_order(self, order, order_type=OrderType.GTC, ...):
    self.assert_level_2_auth()
    owner = self.creds.api_key or ""
    order_payload = (
        order_to_json_v2(order, owner, order_type, ...) if _is_v2_order(order)
        else order_to_json_v1(order, owner, order_type, ...)
    )
    serialized = json.dumps(order_payload, ...)
    headers = self._l2_headers("POST", POST_ORDER, body=order_payload, ...)
    res = self._post(f"{self.host}{POST_ORDER}", headers=headers, data=serialized)

    if self._is_order_version_mismatch(res):
        self.__resolve_version(force_update=True)

    return res


# client.py L676-L716 (extrait)
def create_order(self, order_args, options):
    ...
    version = self.__resolve_version()
    ...
    return self.builder.build_order(order_args, options, version=version)
```

**Sequence diagram pré-cutover (lundi 27 avril 22h UTC, polycopy en dry_run)** :

```text
polycopy.cli                ClobClient (V2 SDK)            backend prod (V1)
    │                              │                              │
    ├──── boot ────────────────────▶                              │
    │      ClobClient(host, chain) │                              │
    │                              │                              │
    │                              ├── GET /version ─────────────▶│
    │                              │◀──── {"version": 1} ─────────┤
    │                              │  __cached_version = 1        │
    │                              │                              │
    │ ── (dry_run, pas de POST)    │                              │
    │                              │                              │
```

**Sequence diagram post-cutover (mardi 28 avril 11h05 UTC, premier POST live)** :

```text
polycopy.cli                ClobClient (V2 SDK)            backend V2
    │                              │                              │
    ├── OrderApproved arrive       │                              │
    │   (live mode, POST réel)     │                              │
    │                              │                              │
    │ ── create_and_post_order ───▶│                              │
    │                              │ build_order(version=1)       │
    │                              │ (cached __cached_version=1)  │
    │                              │ → SignedOrderV1              │
    │                              │                              │
    │                              ├── POST /order (V1 sig) ─────▶│
    │                              │◀── 400 order_version_mismatch┤
    │                              │  __resolve_version(force=T)  │
    │                              ├── GET /version ─────────────▶│
    │                              │◀──── {"version": 2} ─────────┤
    │                              │  __cached_version = 2        │
    │                              │                              │
    │                              │ build_order(version=2)       │
    │                              │ → SignedOrderV2              │
    │                              ├── POST /order (V2 sig) ─────▶│
    │                              │◀──── 200 OK matched ─────────┤
    │ ◀── OrderResult success=True │                              │
```

Le SDK fait le re-resolve **une seule fois** par mismatch — la
deuxième POST utilise le `__cached_version=2` mis à jour. Coût
totale : 1 round-trip extra `/version` au moment du flip.

### 5.2 EIP-712 V2 typed data + domain

**Order struct V2 signed typed data** :

```text
Order(
  uint256 salt,
  address maker,
  address signer,
  uint256 tokenId,
  uint256 makerAmount,
  uint256 takerAmount,
  uint8 side,
  uint8 signatureType,
  uint256 timestamp,
  bytes32 metadata,
  bytes32 builder
)
```

**Domain Exchange V2** (Standard Risk OU Neg Risk selon `options.neg_risk`) :

```json
{
  "name": "Polymarket CTF Exchange",
  "version": "2",
  "chainId": 137,
  "verifyingContract": "<exchange_v2 OR neg_risk_exchange_v2>"
}
```

**`ClobAuthDomain`** (utilisé pour L1 API auth signing — création
d'API keys, headers L1) **reste version `"1"`**. Cf.
[headers.py SDK](https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/headers/headers.py).

Confusion fréquente : implementeur bumpe les deux à `"2"` par erreur
→ headers L1 deviennent invalides → `401 Unauthorized` au premier
API call. Documenter explicitement (§13.1).

**Polycopy n'a rien à signer manuellement** : le SDK V2
`OrderBuilder.build_order(version=2)` plomb tous les champs V2
(timestamp, metadata, builder, salt) automatiquement. Le SDK
`_l1_headers()` consume le bon `ClobAuthDomain` (v1).

### 5.3 FeeRateClient swap path (V1 vs V2)

**Pré-M18 (V1, M16)** :

```python
async def get_fee_rate(self, token_id: str) -> Decimal:
    payload = await self._fetch_v1(token_id)  # GET /fee-rate?token_id=
    base_fee_bps = int(payload.get("base_fee", 0))
    return Decimal(base_fee_bps) / Decimal(10_000)
```

`base_fee_bps` est un **flag binaire** : 0 = fee-free, > 0 =
fee-enabled. La valeur `1000` (= 10%) est constante sur tous les
fee-enabled markets — **pas un rate utilisable** (cf. spec M16
§11.5). M16 dérive le vrai rate via le mapping hardcodé
`feeType → (rate_param, exponent)`.

**Post-M18 (V2)** :

```python
async def get_fee_quote(
    self,
    token_id: str,
    *,
    condition_id: str | None = None,
) -> FeeQuote:
    """Path nominal V2 : condition_id fourni → call direct."""
    if condition_id is None:
        condition_id = await self._resolve_token_to_condition(token_id)

    cached = self._cache.get(condition_id)
    now = self._now()
    if cached is not None and (now - cached[1]) < _CACHE_TTL:
        return cached[0]

    existing = self._inflight.get(condition_id)
    if existing is not None:
        return await existing

    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    self._inflight[condition_id] = fut
    try:
        quote = await self._fetch_and_parse_v2(condition_id, now)
        fut.set_result(quote)
        return quote
    except BaseException as exc:
        fut.set_exception(exc)
        raise
    finally:
        self._inflight.pop(condition_id, None)


async def _fetch_and_parse_v2(
    self,
    condition_id: str,
    now: datetime,
) -> FeeQuote:
    try:
        payload = await self._fetch_v2(condition_id)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 404:
            log.debug("clob_market_not_found", condition_id=condition_id)
            quote = FeeQuote(rate=Decimal("0"), exponent=0)
        else:
            log.warning(
                "clob_market_fetch_failed_using_conservative_fallback",
                condition_id=condition_id, status=status,
            )
            quote = FeeQuote(rate=_CONSERVATIVE_FALLBACK_RATE, exponent=1)
    except httpx.TransportError as exc:
        log.warning(
            "clob_market_fetch_failed_using_conservative_fallback",
            condition_id=condition_id, error=type(exc).__name__,
        )
        quote = FeeQuote(rate=_CONSERVATIVE_FALLBACK_RATE, exponent=1)
    else:
        fd = payload.get("fd") or {}
        rate = Decimal(str(fd.get("r", 0))) if fd.get("r") is not None else Decimal("0")
        exponent = int(fd.get("e", 0))
        quote = FeeQuote(rate=rate, exponent=exponent)
        log.debug(
            "clob_market_fee_quote_fetched",
            condition_id=condition_id,
            rate=str(rate), exponent=exponent,
            taker_only=bool(fd.get("to", True)),
        )

    self._cache[condition_id] = (quote, now)
    self._cache.move_to_end(condition_id)
    while len(self._cache) > self._cache_max:
        evicted_cid, _ = self._cache.popitem(last=False)
        log.debug("clob_market_cache_lru_evicted", condition_id=evicted_cid)
    return quote


@retry(...)  # tenacity exponential backoff
async def _fetch_v2(self, condition_id: str) -> dict:
    url = f"{self._settings.polymarket_clob_host}/clob-markets/{condition_id}"
    response = await self._http.get(url, timeout=self.DEFAULT_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise httpx.HTTPStatusError(
            f"unexpected payload type: {type(data).__name__}",
            request=response.request, response=response,
        )
    return data
```

**Backward-compat alias M16** :

```python
async def get_fee_rate(self, token_id: str) -> Decimal:
    """[DEPRECATED M18] Wrapper rétrocompat M16. Retourne quote.rate.

    Émet un warning structlog 1× par token_id (LRU 500).
    """
    if token_id not in self._deprecated_warned:
        log.warning(
            "fee_rate_client_get_fee_rate_deprecated",
            token_id=token_id,
            reason=(
                "Utiliser get_fee_quote(token_id, condition_id=) pour "
                "accès au quote.exponent. À retirer en M19+."
            ),
        )
        self._deprecated_warned[token_id] = True
        while len(self._deprecated_warned) > 500:
            self._deprecated_warned.popitem(last=False)
    quote = await self.get_fee_quote(token_id)
    return quote.rate
```

**`PositionSizer._compute_effective_fee_rate` réécrit** :

```python
@staticmethod
def _compute_effective_fee_rate(
    *,
    quote: FeeQuote,
    price: Decimal,
) -> Decimal:
    """Calcule l'effective fee rate à partir du FeeQuote V2.

    Formule officielle Polymarket V2 (cf. spec M18 §5.2) :
        effective_rate = quote.rate × (price × (1 - price)) ** quote.exponent

    Cas d'usage :
    - Marché fee-free : `quote.rate == Decimal("0")` → court-circuit
      retour `Decimal("0")` (skip math).
    - Marché fee-enabled (Crypto v2 réel 2026-04-27) :
      `quote.rate=Decimal("0.072")`, `quote.exponent=1`. À p=0.5 →
      0.072 × 0.25^1 = 0.018 (1.80%). Cohérent doc Polymarket live.
    - Fallback réseau down : `quote.rate=Decimal("0.018")`,
      `quote.exponent=1` (cohérent worst-case M16 §11.5 préservé).

    Pas de fallback hardcodé `feeType → (rate, exponent)` — V2
    expose `fd.e` directement, on consume ce que le protocole dit.
    """
    if quote.rate == Decimal("0"):
        return Decimal("0")
    p_one_minus_p = price * (Decimal("1") - price)
    return quote.rate * (p_one_minus_p ** quote.exponent)
```

### 5.4 Wrap USDC.e → pUSD on-chain flow

```python
"""scripts/wrap_usdc_to_pusd.py — minimal helper one-time."""

import argparse
import os
import sys

import structlog

try:
    from web3 import Web3
    from web3.middleware import geth_poa_middleware
except ImportError as e:
    raise SystemExit(
        'web3.py non installé. Run : pip install -e ".[live]"\n'
        "Cf. spec M18 §ME.4."
    ) from e

from polycopy.config import Settings

log = structlog.get_logger()

# ABIs minimaux (méthodes utilisées uniquement)
USDC_ABI = [
    {"name": "approve", "type": "function", "inputs": [
        {"name": "spender", "type": "address"},
        {"name": "value", "type": "uint256"},
    ], "outputs": [{"type": "bool"}]},
    {"name": "balanceOf", "type": "function", "inputs": [
        {"name": "account", "type": "address"},
    ], "outputs": [{"type": "uint256"}]},
]
ONRAMP_ABI = [
    {"name": "wrap", "type": "function", "inputs": [
        {"name": "asset", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "amount", "type": "uint256"},
    ], "outputs": []},
]
PUSD_ABI = USDC_ABI  # même interface ERC-20

USDC_DECIMALS = 6


def main() -> int:
    parser = argparse.ArgumentParser(description="Wrap USDC.e → pUSD")
    parser.add_argument("--amount", type=float, required=True,
                        help="Amount USDC.e à wrap (e.g. 100.0)")
    parser.add_argument("--force-dry-run", action="store_true",
                        help="Bypass dry-run preflight check (NOT RECOMMENDED)")
    args = parser.parse_args()

    settings = Settings()  # type: ignore[call-arg]

    if settings.execution_mode == "dry_run" and not args.force_dry_run:
        log.warning("wrap_usdc_to_pusd_aborted_dry_run",
                    reason="EXECUTION_MODE=dry_run, wrap inutile en dry_run")
        return 1

    rpc_url = os.environ.get("POLYGON_RPC_URL")
    if rpc_url is None:
        log.error("wrap_usdc_to_pusd_missing_rpc",
                  reason="POLYGON_RPC_URL env var requise")
        return 2

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    w3.middleware_onion.inject(geth_poa_middleware, layer=0)

    if not w3.is_connected():
        log.error("wrap_usdc_to_pusd_rpc_disconnected", url=rpc_url)
        return 3

    account = w3.eth.account.from_key(settings.polymarket_private_key)
    funder = settings.polymarket_funder
    onramp_addr = settings.polymarket_collateral_onramp_address
    usdc_addr = settings.polymarket_usdc_e_address
    pusd_addr = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"  # SDK-managed

    amount_wei = int(args.amount * (10 ** USDC_DECIMALS))

    usdc = w3.eth.contract(address=usdc_addr, abi=USDC_ABI)
    onramp = w3.eth.contract(address=onramp_addr, abi=ONRAMP_ABI)
    pusd = w3.eth.contract(address=pusd_addr, abi=PUSD_ABI)

    log.info("wrap_usdc_to_pusd_starting",
             amount=args.amount, funder=funder, onramp=onramp_addr)

    # Step 1 : approve USDC.e → onramp
    nonce = w3.eth.get_transaction_count(account.address)
    approve_tx = usdc.functions.approve(onramp_addr, amount_wei).build_transaction({
        "from": account.address, "nonce": nonce,
        "gas": 100_000, "gasPrice": w3.eth.gas_price,
    })
    signed = account.sign_transaction(approve_tx)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    log.info("wrap_usdc_to_pusd_approve_sent", tx=tx_hash.hex())
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status != 1:
        log.error("wrap_usdc_to_pusd_approve_failed", tx=tx_hash.hex())
        return 4

    # Step 2 : call onramp.wrap(USDC.e, funder, amount)
    nonce += 1
    wrap_tx = onramp.functions.wrap(usdc_addr, funder, amount_wei).build_transaction({
        "from": account.address, "nonce": nonce,
        "gas": 200_000, "gasPrice": w3.eth.gas_price,
    })
    signed = account.sign_transaction(wrap_tx)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    log.info("wrap_usdc_to_pusd_wrap_sent", tx=tx_hash.hex())
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status != 1:
        log.error("wrap_usdc_to_pusd_wrap_failed", tx=tx_hash.hex())
        return 5

    # Step 3 : verify balance
    pusd_balance = pusd.functions.balanceOf(funder).call()
    log.info("wrap_usdc_to_pusd_completed",
             pusd_balance=pusd_balance / (10 ** USDC_DECIMALS),
             gas_total=receipt.gasUsed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

---

## 6. DTOs

### 6.1 `FeeQuote(rate, exponent)`

```python
class FeeQuote(BaseModel):
    """V2 fee quote from `getClobMarketInfo(condition_id)["fd"]`.

    `rate` : Decimal — `fd.r` du response, le rate parameter de la
             formule Polymarket. Range observé 2026-04-27 : 0.0
             (fee-free) ou 0.072 (crypto fee-enabled).
    `exponent` : int — `fd.e` du response, l'exposant de la
             formule. Range observé : 0 (fee-free) ou 1 (fee-enabled).

    Formule effective : `effective_rate = rate × (p × (1-p))^exponent`.

    Constantes documentées :
    - `FeeQuote.zero()` → `FeeQuote(rate=Decimal("0"), exponent=0)`
      (fee-free, observé sur tous les markets sans `fd` field).
    - `FeeQuote.conservative_fallback()` →
      `FeeQuote(rate=Decimal("0.018"), exponent=1)` (réseau down,
      worst-case observé 1.80% à p=0.5).
    """
    model_config = ConfigDict(frozen=True)
    rate: Decimal
    exponent: int = Field(ge=0, le=4)

    @classmethod
    def zero(cls) -> "FeeQuote":
        return cls(rate=Decimal("0"), exponent=0)

    @classmethod
    def conservative_fallback(cls) -> "FeeQuote":
        return cls(rate=Decimal("0.018"), exponent=1)
```

### 6.2 `BuiltOrder` (inchangé)

[src/polycopy/executor/dtos.py:14-27](../../src/polycopy/executor/dtos.py#L14-L27)
reste **strictement identique** — `BuiltOrder` ne porte aucun champ
V2-specific (`timestamp`, `metadata`, `builder`). Le SDK V2 plomb
ces champs automatiquement dans `OrderBuilder.build_order(version=2)`.

```python
class BuiltOrder(BaseModel):
    model_config = ConfigDict(frozen=True)
    token_id: str
    side: Literal["BUY", "SELL"]
    size: float
    price: float
    tick_size: float
    neg_risk: bool
    order_type: Literal["FOK", "FAK", "GTC"]
```

### 6.3 Imports SDK V2 mappés

| V1 import | V2 import |
|---|---|
| `from py_clob_client.client import ClobClient` | `from py_clob_client_v2 import ClobClient` |
| `from py_clob_client.clob_types import OrderArgs, OrderType` | `from py_clob_client_v2 import OrderArgs, OrderType` |
| `from py_clob_client.order_builder.constants import BUY, SELL` | (supprimé, passer string `built.side` direct) |
| `from py_clob_client.exceptions import PolyApiException` | `from py_clob_client_v2 import PolyException` |
| (n/a) | `from py_clob_client_v2 import BuilderConfig` |
| (n/a) | `from py_clob_client_v2 import MarketOrderArgs` |

Note : `MarketOrderArgs` n'est PAS utilisé par polycopy (on utilise
uniquement `OrderArgs` + `OrderType.FOK`). Listé pour exhaustivité.

---

## 7. Settings (env vars + Pydantic)

### 7.1 `POLYMARKET_CLOB_HOST`

```python
polymarket_clob_host: str = Field(
    "https://clob.polymarket.com",
    pattern=r"^https://[a-zA-Z0-9.-]+(?::\d+)?$",
    description=(
        "M18 — Host CLOB Polymarket (REST). Pré-cutover V2 (avant 28 "
        "avril 2026 ~11h UTC) : peut pointer sur "
        "https://clob-v2.polymarket.com pour tester contre le "
        "testnet. Post-cutover : prod URL bascule automatiquement "
        "sur le backend V2, default OK. Consommé par ClobReadClient, "
        "ClobMetadataClient, ClobOrderbookReader, FeeRateClient, "
        "ClobWriteClient (via SDK). Pattern strict — pas de http://."
    ),
)
```

### 7.2 `POLYMARKET_USE_SERVER_TIME`

```python
polymarket_use_server_time: bool = Field(
    True,
    description=(
        "M18 — Si True, le SDK V2 ClobClient fetch /time avant chaque "
        "order sign et utilise l'horloge serveur pour le champ "
        "timestamp (ms) du signed struct V2. Coût +1 round-trip HTTP "
        "par sign (~50-200ms), bénéfice zéro risque de rejection sur "
        "clock drift (VM prod peut dériver après suspend/resume). "
        "Désactivable pour tests/debug."
    ),
)
```

### 7.3 `POLYMARKET_COLLATERAL_ONRAMP_ADDRESS` + `POLYMARKET_USDC_E_ADDRESS`

```python
polymarket_collateral_onramp_address: str = Field(
    "0x93070a847efEf7F70739046A929D47a521F5B8ee",
    pattern=r"^0x[a-fA-F0-9]{40}$",
    description=(
        "M18 — Adresse Polygon du contrat CollateralOnramp (V2). "
        "Confirmée live 2026-04-27 via "
        "docs.polymarket.com/resources/contracts. Consommée "
        "UNIQUEMENT par scripts/wrap_usdc_to_pusd.py — ClobClient V2 "
        "ne touche pas ce contrat. Override possible si Polymarket "
        "re-deploy."
    ),
)
polymarket_usdc_e_address: str = Field(
    "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
    pattern=r"^0x[a-fA-F0-9]{40}$",
    description=(
        "M18 — Adresse USDC.e Polygon canonique pré-V2. Consommée "
        "UNIQUEMENT par le wrap script (approve USDC.e → Onramp)."
    ),
)
```

### 7.4 `POLYMARKET_BUILDER_CODE` + `POLYMARKET_BUILDER_ADDRESS`

```python
polymarket_builder_code: str | None = Field(
    None,
    pattern=r"^0x[0-9a-fA-F]{64}$",
    description=(
        "M18 — Builder code Polymarket (bytes32, public, "
        "non-secret). Si set, le SDK V2 plomb la valeur dans chaque "
        "Order.builder via BuilderConfig — fee rebates apparents sur "
        "le Builder Leaderboard. Réclamer son code via "
        "polymarket.com/settings?tab=builder. Default None = aucun "
        "builder, comportement strict M3..M16. Cohérent discipline "
        "MACHINE_ID (public, loggé en clair)."
    ),
)
polymarket_builder_address: str | None = Field(
    None,
    pattern=r"^0x[a-fA-F0-9]{40}$",
    description=(
        "M18 — Adresse Ethereum du wallet builder (utilisée par "
        "BuilderConfig). Optional — si POLYMARKET_BUILDER_CODE set "
        "ET ce champ None, default à POLYMARKET_FUNDER. Public, "
        "loggé en clair."
    ),
)
```

### 7.5 Cross-field validators

```python
@model_validator(mode="after")
def _validate_m18_v2_consistency(self) -> "Settings":
    """Cross-field validators M18.

    1. Si execution_mode=live ET polymarket_collateral_onramp_address
       empty → raise (l'utilisateur doit confirmer l'adresse).
    2. Si polymarket_builder_code set ET
       polymarket_builder_address None → log info que default
       fallback POLYMARKET_FUNDER sera utilisé.
    """
    if self.execution_mode == "live" and not self.polymarket_collateral_onramp_address:
        raise ValueError(
            "M18 : EXECUTION_MODE=live requires "
            "POLYMARKET_COLLATERAL_ONRAMP_ADDRESS to be set "
            "(default OK : "
            "0x93070a847efEf7F70739046A929D47a521F5B8ee). "
            "Vérifier que le default est encore valide via "
            "docs.polymarket.com/resources/contracts."
        )
    return self
```

### 7.6 `.env.example` bloc M18

```bash
# ── M18 — Polymarket CLOB V2 + pUSD migration (cutover 28 avril 2026) ──
# Host CLOB. Pré-cutover : https://clob-v2.polymarket.com pour tester.
# Post-cutover : default OK (prod URL bascule sur V2 backend).
# POLYMARKET_CLOB_HOST=https://clob.polymarket.com

# Anti clock-skew. Recommandé True (default).
# POLYMARKET_USE_SERVER_TIME=true

# Wrap helper (live mode only).
# POLYMARKET_COLLATERAL_ONRAMP_ADDRESS=0x93070a847efEf7F70739046A929D47a521F5B8ee
# POLYMARKET_USDC_E_ADDRESS=0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174

# Builder code (optionnel, ROI direct via fee rebates).
# Réclamer via polymarket.com/settings?tab=builder.
# POLYMARKET_BUILDER_CODE=
# POLYMARKET_BUILDER_ADDRESS=
```

---

## 8. Invariants sécurité

### 8.1 Triple garde-fou M3 + 4ᵉ M8 — strictement préservés

M18 swappe le SDK signataire mais ne touche aucun des 4 garde-fous :

1. **Lazy init `ClobClient`** — pas instancié si `execution_mode != "live"`.
   Vérifié par
   [test_clob_write_client.py:46-48](../../tests/unit/test_clob_write_client.py#L46-L48)
   `test_garde_fou_constructor_in_dry_run_raises`.
2. **`RuntimeError` boot** si `live` ET clés absentes.
   Vérifié par
   [test_clob_write_client.py:51-58](../../tests/unit/test_clob_write_client.py#L51-L58)
   `test_garde_fou_constructor_without_*_raises`.
3. **`assert execution_mode == "live"` AVANT chaque
   `create_and_post_order`** — défense en profondeur §2.3 M3.
   Préservé strict
   [clob_write_client.py:73-77](../../src/polycopy/executor/clob_write_client.py#L73-L77).
4. **4ᵉ garde-fou M8** : `assert settings.execution_mode == "dry_run"`
   avant `_persist_realistic_simulated`.
   Préservé strict
   [pipeline.py:404-407](../../src/polycopy/executor/pipeline.py#L404-L407).

Aucune modification de
`WalletStateReader.get_state()` (re-fetch wallet pré-POST). Aucune
modification de `_persist_*` côté executor.

### 8.2 Discipline credentials — IDENTIQUE M3

`POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`, CLOB L2 creds
(`api_key`, `api_secret`, `api_passphrase`) — discipline IDENTIQUE
M3. Aucun log même partiel, même en debug, même dans les
exceptions, même dans `repr(ClobWriteClient)`.

Le seul log nouveau autorisé : `executor_creds_ready` enrichi avec
2 nouveaux flags **bool** (pas la valeur) :

```python
log.info(
    "executor_creds_ready",
    signature_type=settings.polymarket_signature_type,
    use_server_time=settings.polymarket_use_server_time,    # bool
    builder_code_set=settings.polymarket_builder_code is not None,  # bool
)
```

**Test invariant** :
`tests/unit/test_clob_v2_no_secret_leak.py::test_creds_ready_does_not_log_secrets` —
grep le caplog pour `private_key`, `api_secret`, `api_passphrase`,
`funder`, `builder_code` (la valeur, pas la clé). Doit retourner 0
match.

### 8.3 Builder code = public, loggé OK (D9)

Le builder code est PUBLIC (apparaît onchain dans `Order.builder`).
Aucune discipline secret-style. Cohérent avec `MACHINE_ID`
([CLAUDE.md §M12_bis Phase A](../../CLAUDE.md)).

**MAIS** : par hygiène log (réduire le spam dans `executor_creds_ready`),
on logge `builder_code_set: bool` (pas la valeur). Si l'utilisateur
veut auditer la valeur active, il peut consulter `.env` directement.

### 8.4 Adresses contrats : SDK-managed pour Exchange/pUSD, env var pour Onramp uniquement

Cohérent **D5** §4.5 :

- ✅ **Exchange V2 / Neg Risk Exchange V2 / pUSD (collateral)** :
  hardcodés dans le SDK upstream
  [config.py SDK](https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/config.py).
  Polycopy ne les expose pas. Si Polymarket re-deploy, l'utilisateur
  bumpe le SDK (`pip install -U py-clob-client-v2`).
- ✅ **CollateralOnramp + USDC.e** : env vars polycopy (default
  supplied), consommés UNIQUEMENT par le wrap script. Override
  possible si re-deploy d'un seul contrat.
- ✅ **EIP-712 verifyingContract** : géré par le SDK (lookup
  via `get_contract_config(chain_id)`). Polycopy ne signe pas
  manuellement.

### 8.5 EIP-712 `ClobAuthDomain` reste v1 (piège fréquent)

V2 bump l'`Exchange` domain à `version="2"`, MAIS `ClobAuthDomain`
(utilisé pour L1 API auth signing — création d'API keys, headers
L1) **reste `version="1"`**.

Si l'implémenteur bump les deux par erreur, l'auth headers L1
deviennent invalides → `401 Unauthorized` au premier API call.

Le SDK V2 gère ces 2 domains séparément
([signing/eip712.py SDK](https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/signing/eip712.py)).
Polycopy n'a rien à faire — c'est un piège de **lecture** pour
quiconque audite la spec ou la doc Polymarket.

Documenter explicitement §13.1.

### 8.6 Aucune nouvelle creds consommée

M18 ne consomme **aucune nouvelle creds** :

- `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`, CLOB L2 creds —
  identique M3.
- `POLYMARKET_BUILDER_CODE`, `POLYMARKET_BUILDER_ADDRESS` —
  PUBLIC (cf. §8.3).
- `POLYMARKET_COLLATERAL_ONRAMP_ADDRESS`, `POLYMARKET_USDC_E_ADDRESS` —
  PUBLIC (adresses contrats sur Polygon mainnet).
- `POLYMARKET_CLOB_HOST` — URL publique.
- `POLYMARKET_USE_SERVER_TIME` — bool.
- `POLYGON_RPC_URL` (consommé UNIQUEMENT par le wrap script) —
  l'utilisateur doit fournir un endpoint RPC. Pas un secret en soi
  (les endpoints RPC publics sont nombreux), mais peut contenir un
  API key dans l'URL. Documenter en gardant la même discipline
  (jamais log).

### 8.7 Aucun secret loggé

Test invariant :
`tests/unit/test_clob_v2_no_secret_leak.py::test_no_secret_in_logs` —
boot `ClobWriteClient` avec creds dummy + caplog INFO/WARN, grep le
contenu pour les patterns suivants : `0x0123` (sample private key),
`api_secret_dummy`, `api_passphrase_dummy`, `0xfunder_dummy`,
`0xbuilder_code_dummy`.

Doit retourner 0 match (que les valeurs, pas les clés
`signature_type` etc. qui sont OK).

### 8.8 Versioning sacré préservé

M18 n'introduit aucune nouvelle version de scoring. `SCORING_VERSION`
literal type (`"v1"`, `"v2.1"`, `"v2.1.1"`) inchangé. Le registry
[scoring/__init__.py:SCORING_VERSIONS_REGISTRY](../../src/polycopy/discovery/scoring/__init__.py)
intact. Aucune row `trader_scores` réécrite.

---

## 9. Test plan

### 9.1 Tests unitaires (~22)

#### ME.1 — SDK swap (3 tests)

1. `test_clob_write_client_imports_from_v2` — grep AST sur
   `clob_write_client.py`, vérifie qu'aucun symbole `py_clob_client.*`
   (V1) n'est importé. `py_clob_client_v2` doit l'être.
2. `test_clob_write_client_calls_create_or_derive_api_key` — mock
   le SDK, vérifie que `create_or_derive_api_key` est appelée (pas
   `create_or_derive_api_creds`).
3. `test_build_order_args_passes_string_side_directly` — mock
   `OrderArgs`, build avec `built.side="BUY"`, vérifie que
   `OrderArgs(side="BUY", ...)` est appelé (pas conversion en
   constant).

#### ME.2 — `POLYMARKET_CLOB_HOST` + `USE_SERVER_TIME` (4 tests)

4. `test_settings_polymarket_clob_host_default` — default
   `https://clob.polymarket.com`.
5. `test_settings_polymarket_clob_host_pattern_rejects_http` — set
   `POLYMARKET_CLOB_HOST=http://insecure` → ValidationError.
6. `test_clob_clients_consume_polymarket_clob_host` — instancie
   `ClobMetadataClient(http, settings)` avec
   `settings.polymarket_clob_host="https://test"`, mock httpx,
   vérifie que la requête tape `https://test/tick-size`.
7. `test_clob_write_client_passes_use_server_time_to_sdk` — mock
   `ClobClient`, vérifie que le constructor reçoit
   `use_server_time=True` (default).

#### ME.3 — `FeeRateClient` swap (8 tests)

8. `test_fee_quote_dto_validates_decimal_and_int` — `FeeQuote(rate=Decimal("0.072"), exponent=1)` OK,
   `exponent=-1` raise.
9. `test_fee_quote_zero_classmethod` — `FeeQuote.zero()` ==
   `FeeQuote(rate=Decimal("0"), exponent=0)`.
10. `test_get_fee_quote_v2_endpoint_with_condition_id` — mock GET
    `/clob-markets/{cid}` retourne fixture crypto, vérifie
    `FeeQuote(rate=Decimal("0.072"), exponent=1)`.
11. `test_get_fee_quote_fallback_gamma_when_no_condition_id` — mock
    GET `/markets-by-token/{tid}` retourne `{"conditionId": "0xabc"}`,
    puis mock `/clob-markets/0xabc` → succès. Vérifie le warning
    `fee_rate_client_token_id_resolved_via_gamma`.
12. `test_get_fee_quote_fee_free_market_returns_zero` — mock GET
    `/clob-markets/{cid}` retourne fixture politics (sans `fd`),
    vérifie `FeeQuote.zero()`.
13. `test_get_fee_quote_404_returns_zero` — mock 404, vérifie
    `FeeQuote.zero()`.
14. `test_get_fee_quote_5xx_returns_conservative_fallback` — mock
    503 post-tenacity retries, vérifie
    `FeeQuote.conservative_fallback()`.
15. `test_get_fee_rate_legacy_alias_returns_quote_rate_with_warning` —
    appelle `get_fee_rate(token_id)`, vérifie qu'un warning
    `fee_rate_client_get_fee_rate_deprecated` est émis 1× (pas 2 sur
    2 appels mêmes token).

#### ME.4 — pUSD env vars (4 tests)

16. `test_settings_polymarket_collateral_onramp_default` — default
    `0x93070a847efEf7F70739046A929D47a521F5B8ee`.
17. `test_settings_collateral_onramp_pattern_rejects_invalid_hex` —
    set `POLYMARKET_COLLATERAL_ONRAMP_ADDRESS=invalid` →
    ValidationError.
18. `test_settings_live_mode_requires_collateral_onramp` — set
    `EXECUTION_MODE=live` + clear `POLYMARKET_COLLATERAL_ONRAMP_ADDRESS`
    via empty string → ValidationError "M18 : EXECUTION_MODE=live
    requires POLYMARKET_COLLATERAL_ONRAMP_ADDRESS".
19. `test_wrap_script_aborts_in_dry_run` — set
    `EXECUTION_MODE=dry_run`, run script via subprocess sans
    `--force-dry-run`, exit code != 0, log `wrap_usdc_to_pusd_aborted_dry_run`.

#### ME.5 — Builder code (3 tests)

20. `test_settings_polymarket_builder_code_pattern_validates_hex32` —
    set `POLYMARKET_BUILDER_CODE=0x` + 64 hex chars → OK ; 63 chars
    → ValidationError.
21. `test_clob_write_client_passes_builder_config_when_set` — mock
    `ClobClient` + `BuilderConfig`, vérifie que constructor reçoit
    `builder_config=BuilderConfig(builder_address=funder, builder_code=hex)`.
22. `test_clob_write_client_passes_no_builder_config_when_unset` —
    `polymarket_builder_code=None`, vérifie que constructor reçoit
    `builder_config=None`.

### 9.2 Tests intégration (3) opt-in

`pytest -m integration tests/integration/test_clob_v2_*.py`

23. `test_clob_v2_market_info_live` — fetch `/clob-markets/{condition_id}`
    sur 1 marché crypto fee-enabled + 1 marché politics fee-free.
    Asserte schema (présence/absence de `fd`).
24. `test_clob_v2_signature_local_validation` — build un V2 order
    via `OrderBuilder.build_order(version=2)` avec une clé dummy,
    vérifie que le payload JSON wire contient
    `timestamp/metadata/builder` et que la signature est un hex
    valide. Aucun POST.
25. `test_clob_v2_fee_rate_via_endpoint_real` — `FeeRateClient(http,
    settings)` + `get_fee_quote(asset_id, condition_id=cid)` sur 1
    marché crypto, vérifie que `quote.rate > 0` et
    `quote.exponent in (1, 2)`.

### 9.3 Smoke runtime (4 commandes)

Cf. §12 commandes vérification.

### 9.4 Tests M16 préservés (zéro régression)

[tests/unit/test_fee_rate_client.py](../../tests/unit/test_fee_rate_client.py)
existant : 5 tests M16. Tous doivent passer **inchangés** post-M18
grâce à l'alias deprecated `get_fee_rate(token_id) -> Decimal` (D6
point 3). Le warning structlog déprécié peut être muté dans le
test setup si nécessaire (pour ne pas polluer le log).

### 9.5 Total tests

- ME.1-ME.5 unit : **22 tests**.
- ME.6 intégration : **3 tests** (opt-in `-m integration`).
- M16 régression : **5 tests** (préservés).
- M3 régression : **8 tests** ([test_clob_write_client.py](../../tests/unit/test_clob_write_client.py))
  préservés.

**Total** : ~38 tests touchés, dont **22 nouveaux** + **5 régression
M16** + **8 régression M3**.

---

## 10. Impact existant

### 10.1 Fichiers modifiés (table exhaustive)

Cf. table §1.4. Récapitulation par fichier :

| Fichier | Lignes touchées | ME |
|---|---|---|
| [pyproject.toml](../../pyproject.toml) | 11, 29-31, 75-77 | ME.1 + ME.4 |
| [src/polycopy/executor/clob_write_client.py](../../src/polycopy/executor/clob_write_client.py) | 14-16, 25-26, 32-50, 60, 62-69, 91-99 | ME.1 + ME.2 + ME.5 |
| [src/polycopy/executor/fee_rate_client.py](../../src/polycopy/executor/fee_rate_client.py) | full refactor (~250 lines vs 226 actuel) | ME.3 |
| [src/polycopy/executor/clob_metadata_client.py](../../src/polycopy/executor/clob_metadata_client.py) | 27-30 | ME.2 |
| [src/polycopy/executor/clob_orderbook_reader.py](../../src/polycopy/executor/clob_orderbook_reader.py) | BASE_URL constante → settings | ME.2 |
| [src/polycopy/strategy/clob_read_client.py](../../src/polycopy/strategy/clob_read_client.py) | BASE_URL constante → settings | ME.2 |
| [src/polycopy/strategy/pipeline.py](../../src/polycopy/strategy/pipeline.py) | 221-222, 293-334 | ME.3 |
| [src/polycopy/config.py](../../src/polycopy/config.py) | +5 settings + 1 validator (~80 LOC ajout) | ME.2 + ME.4 + ME.5 |
| [.env.example](../../.env.example) | +1 bloc M18 (~10 lignes) | tous |
| [tests/unit/test_clob_write_client.py](../../tests/unit/test_clob_write_client.py) | 32 (mock rename) | ME.1 |
| [tests/integration/test_clob_l1_l2_auth_live.py](../../tests/integration/test_clob_l1_l2_auth_live.py) | 11, 18 (rename + import) | ME.1 |
| [docs/todo.md](../todo.md) | §14 enrichi avec D11 phases | ME.7 |
| [CLAUDE.md](../../CLAUDE.md) | §Conventions + §Sécurité bloc M18 | tous |

### 10.2 Fichiers nouveaux

| Fichier | Description | ME |
|---|---|---|
| [scripts/wrap_usdc_to_pusd.py](../../scripts/wrap_usdc_to_pusd.py) | Helper one-time web3.py (~80 LOC) | ME.4 |
| [tests/integration/test_clob_v2_market_info_live.py](../../tests/integration/test_clob_v2_market_info_live.py) | Smoke `getClobMarketInfo` | ME.6 |
| [tests/integration/test_clob_v2_signature_local.py](../../tests/integration/test_clob_v2_signature_local.py) | Build V2 order localement | ME.6 |
| [tests/integration/test_clob_v2_fee_rate_live.py](../../tests/integration/test_clob_v2_fee_rate_live.py) | Fee rate V2 endpoint réel | ME.6 |
| [tests/fixtures/clob_v2_market_crypto_sample.json](../../tests/fixtures/clob_v2_market_crypto_sample.json) | Capture live 2026-04-27 (fee-enabled crypto) | ME.3 |
| [tests/fixtures/clob_v2_market_fee_free_sample.json](../../tests/fixtures/clob_v2_market_fee_free_sample.json) | Capture live 2026-04-27 (fee-free politics) | ME.3 |
| [tests/unit/test_clob_v2_no_secret_leak.py](../../tests/unit/test_clob_v2_no_secret_leak.py) | Invariant grep secrets dans logs | §8 |

### 10.3 Fichiers strictement intacts

- [src/polycopy/executor/dtos.py](../../src/polycopy/executor/dtos.py) —
  `BuiltOrder` / `OrderResult` / `WalletState` inchangés (D3).
- [src/polycopy/executor/wallet_state_reader.py](../../src/polycopy/executor/wallet_state_reader.py) —
  triple garde-fou intact (§8.1).
- [src/polycopy/executor/pipeline.py](../../src/polycopy/executor/pipeline.py) —
  `execute_order` flow inchangé (le swap SDK est interne au
  `ClobWriteClient`).
- [src/polycopy/executor/orchestrator.py](../../src/polycopy/executor/orchestrator.py) —
  inchangé.
- [src/polycopy/discovery/](../../src/polycopy/discovery/) — scoring
  intact (D14).
- [src/polycopy/storage/](../../src/polycopy/storage/) — schema DB
  intact (D13).
- [src/polycopy/monitoring/](../../src/polycopy/monitoring/) —
  alertes/PnL intacts.
- [src/polycopy/dashboard/](../../src/polycopy/dashboard/) — UX
  dashboard intact (les events `fee_rate_fetched` continuent à
  apparaître mais le contenu interne change).
- [src/polycopy/remote_control/](../../src/polycopy/remote_control/) —
  Tailscale auth intact.
- [alembic/versions/](../../alembic/versions/) — pas de migration
  0011 (D13).

---

## 11. Migration / cutover ops

Référence à enrichir : [docs/todo.md §14](../todo.md#L554).

### 11.1 Phase 1 — lundi 27 avril ~22h UTC : ship V2 SDK

Objectif : merge sur main + restart bot V2. Le SDK V2 query
`/version` au boot (backend = V1 prod), signe V1 orders en
attendant.

Séquence :

```bash
# 1. Sur la machine de dev — merge M18 sur main
cd ~/UNIGE/GitHub/polycopy
git checkout main
git pull origin main
# (s'assurer que tous les commits ME.1 → ME.7 sont sur main —
# cf. §13 plan d'implémentation)

# 2. Sur la machine prod — pull + restart
ssh prod-machine
cd ~/Documents/GitHub/polycopy
git pull origin main

# 3. Update les deps (récupère py-clob-client-v2)
source .venv/bin/activate
pip install -e .
pip uninstall py-clob-client -y   # remplacer V1 par V2 strictement
# Vérifier :
python -c "import py_clob_client_v2; print(py_clob_client_v2.__file__)"
# → ~/.../site-packages/py_clob_client_v2/__init__.py
python -c "import py_clob_client" 2>&1 | grep ModuleNotFoundError
# → doit lever (V1 désinstallé)

# 4. Vérifier .env
grep "POLYMARKET_CLOB_HOST" .env  # absent OU = default OK
grep "POLYMARKET_USE_SERVER_TIME" .env  # absent OU = true

# 5. Restart
sudo systemctl restart polycopy
# OU
pkill -f polycopy && python -m polycopy --verbose &
```

Smoke validation Phase 1 :

```bash
# Vérifier les logs au boot
tail -50 ~/.polycopy/logs/polycopy.log | grep -E "ERROR|executor_creds|machine_id"
# Doit montrer :
# - executor_creds_ready signature_type=2 use_server_time=true builder_code_set=false
# - (pas d'ERROR)
# - machine_id_resolved
```

### 11.2 Phase 2 — mardi 28 avril ~10h-12h UTC : auto-flip backend

**Aucune action utilisateur requise.**

Polymarket bascule backend ~11h UTC. Si le bot tourne et POSTe un
ordre live :

- Pré-flip (`/version=1`) : SDK signe V1, POST OK.
- Flip-window (`/version` retourne `version=2` sur la prochaine
  query) : SDK detect `order_version_mismatch` au prochain POST,
  call `_resolve_version(force_update=True)`, retry en V2.
- Post-flip : tous les futurs orders signés V2.

Si le bot est en `dry_run` : pas de POST réel, pas d'opportunité de
detect le flip. Ce qui est OK — la transition se fera
automatiquement à la première vraie POST live post-flip
`EXECUTION_MODE=live`.

### 11.3 Phase 3 — mardi 28 avril ~11h30 UTC : smoke post-cutover

```bash
# 1. Vérifier que le bot a bien démarré sur l'API V2
tail -100 ~/.polycopy/logs/polycopy.log | grep -E "ERROR|signature_invalid|order_version_mismatch"
# → doit montrer 0 ERROR, 0 signature_invalid

# 2. Vérifier que le first cycle discovery + un éventuel BUY tournent
grep "discovery_cycle_started\|order_simulated\|fee_rate_fetched\|clob_market_fee_quote_fetched" ~/.polycopy/logs/polycopy.log | tail -20
# → présence des events nominaux

# 3. Vérifier qu'aucun ordre dry-run n'est rejeté pour signature_invalid
sqlite3 ~/.polycopy/data/polycopy.db \
  "SELECT status, error_msg, COUNT(*) FROM my_orders \
   WHERE sent_at >= datetime('now', '-1 hour') GROUP BY 1, 2;"
# → uniquement SIMULATED (en dry_run) ou FILLED (en live).
# → AUCUN REJECTED avec error_msg LIKE '%signature%'.

# 4. Vérifier que get_fee_quote V2 path est exercé
grep "clob_market_fee_quote_fetched" ~/.polycopy/logs/polycopy.log | tail -5
# → events JSON avec condition_id, rate, exponent, taker_only

# 5. Surveiller les events fee_rate côté V2 (nouveau path)
grep "clob_market_fetch_failed_using_conservative_fallback" ~/.polycopy/logs/polycopy.log | tail -5
# → doit être vide ou très rare. Si plein → V2 endpoint instable, alert dev.
```

### 11.4 Phase 4 — mercredi 29 avril : monitoring 24h

- Telegram heartbeat OK (event `heartbeat_sent` dans les logs
  toutes les `TELEGRAM_HEARTBEAT_INTERVAL_HOURS`).
- Dashboard `/strategie` : decisions APPROVED se concrétisent en
  `MyOrder` valides.
- Aucune erreur `executor_error` ou `executor_auth_fatal`.
- Dashboard `/exécution` : decisions de sizing fee-aware cohérentes
  (le compteur `ev_negative_after_fees` peut bouger légèrement vs
  pré-M18 car la formule M16 est plus précise post-D6).

### 11.5 Rollback (impossible post-cutover, hotfix uniquement)

- **Pré-cutover (lundi 27 ~22h UTC à mardi 28 ~10h59 UTC)** : si
  M18 introduit une régression observée, possible de
  `git revert` sur `main` + `pip install py-clob-client>=0.20.0`
  + restart V1. Backend Polymarket V1 est encore live. Fenêtre
  d'environ 13h.
- **Post-cutover (mardi 28 ~11h00 UTC et après)** : Polymarket V1
  backend est offline. **Pas de retour V1 possible**. Le rollback
  se fait par hotfix git sur la branche V18 V2 uniquement.

Stratégie hotfix :

```bash
# Identifier le commit fautif via git bisect ou observation logs
git bisect start HEAD <last-known-good-sha>

# Une fois identifié, soit revert de ce commit seul :
git revert <fautif-sha>
# OU patch ciblé :
git checkout main
# édit le fichier
git commit -m "hotfix(m18): <description>"
git push origin main

# Sur prod
git pull origin main
sudo systemctl restart polycopy
```

### 11.6 Risques + mitigations

| Risque | Probabilité | Impact | Mitigation |
|---|---|---|---|
| SDK V2 PyPI release retiré entre 27/04 et 28/04 | Faible | Bloquant | Pin version exacte `==1.0.0` ; backup wheel locale `pip download py-clob-client-v2==1.0.0` |
| `clob-v2.polymarket.com` schéma change post-spec writing | Moyen | Tests intégration cassés | Tests intégration opt-in (`-m integration`), pas dans CI critique. Capture fixture stable. |
| `getClobMarketInfo` rate limit plus strict que V1 | Moyen | Fallback fréquent | Cache TTL 60s amortit. Single-flight évite burst. Si 429 répété → augmenter TTL à 300s en hotfix. |
| `polymarket-apis>=0.5.0` incompatible V2 | Faible | Discovery cassée | Cette dep utilise Gamma API publique (pas CLOB write), donc inchangée. Surveiller les imports. |
| Polymarket re-deploy `CollateralOnramp` post-spec writing | Très faible | Wrap script échoue | Setting env var override `POLYMARKET_COLLATERAL_ONRAMP_ADDRESS` permet d'updater sans redeploy code. |
| `BuilderConfig` API change V2 | Faible | Builder code skip silencieux | Tests intégration vérifient le plombage. Default `None` = comportement strict M3..M16 préservé. |
| Clock drift Macbook prod après suspend nuit | Moyen | Order rejection | `polymarket_use_server_time=True` default — anti drift natif. |
| Cutover Polymarket repoussé (ex: 29 avril) | Faible | Pas d'urgence | Le SDK V2 est dual-version, continue à signer V1 OK. Ship M18 reste safe. |
| `get_version()` endpoint down | Très faible | SDK assume `version=2` (default) | Le SDK `get_version()` fait `except Exception: return 2`. Si backend V1 + endpoint down → SDK signe V2 sur backend V1 → reject. Mais ce cas est rarissime (cutover concerne le `/order` POST, pas `/version`). |

---

## 12. Commandes de vérification

### 12.1 Smoke test final avant merge

```bash
# 1. Tests unitaires
pytest tests/unit/test_clob_write_client.py \
       tests/unit/test_fee_rate_client.py \
       tests/unit/test_position_sizer*.py \
       tests/unit/test_settings*.py \
       tests/unit/test_clob_v2_no_secret_leak.py \
       -x --tb=short

# 2. Tests intégration opt-in (vs clob-v2.polymarket.com en pré-cutover,
#    OU clob.polymarket.com post-cutover)
POLYMARKET_CLOB_HOST=https://clob-v2.polymarket.com \
  pytest -m integration tests/integration/test_clob_v2_*.py \
  tests/integration/test_clob_l1_l2_auth_live.py -x --tb=short

# 3. Lint + types
ruff check . && ruff format --check . && mypy src --strict

# 4. Smoke runtime (dry-run, contre clob-v2 testnet pré-cutover)
EXECUTION_MODE=dry_run \
SCORING_VERSION=v2.1 \
DISCOVERY_ENABLED=true \
POLYMARKET_CLOB_HOST=https://clob-v2.polymarket.com \
python -m polycopy --verbose &

# Wait ~30s, vérifier les logs
sleep 30
tail -60 ~/.polycopy/logs/polycopy.log | grep -E "ERROR|executor_started|fee_rate|discovery_cycle|machine_id"
# Attendu :
# - executor_started mode=dry_run m8=true
# - clob_market_fee_quote_fetched (au moins 1 sur fee-enabled market)
# - discovery_cycle_started
# - aucun ERROR
# - aucun "executor_creds_ready" (lazy init préservé en dry_run)

# 5. Smoke runtime (dry-run, contre prod URL = backend V1 pré-cutover ou V2 post-cutover)
pkill -f polycopy
EXECUTION_MODE=dry_run \
python -m polycopy --verbose &
sleep 10
grep "polymarket_clob_host" ~/.polycopy/logs/polycopy.log | tail -1
# Attendu : polymarket_clob_host=https://clob.polymarket.com
```

### 12.2 Smoke test post-cutover (mardi 28 ~11h30 UTC)

```bash
# Cf. §11.3 phase 3 — répéter pour validation.
```

### 12.3 Vérification PyPI au moment du merge

```bash
pip index versions py-clob-client-v2
# Doit retourner : 1.0.0 (Available versions: 1.0.0, 0.0.4, 0.0.2, 0.0.1)
```

### 12.4 Vérification adresses contrats live (sanity)

```bash
# Vérifier que le default POLYMARKET_COLLATERAL_ONRAMP_ADDRESS est encore valide
curl -s "https://docs.polymarket.com/resources/contracts" | grep -c "0x93070a847efEf7F70739046A929D47a521F5B8ee"
# Doit retourner ≥ 1
```

### 12.5 Vérification grep py_clob_client residuel

```bash
# Aucun import V1 ne doit subsister
grep -rn "py_clob_client[^_]" src/ tests/ --include="*.py" | grep -v "py_clob_client_v2"
# Doit retourner 0 lignes (sauf docs/comments éventuelles).
```

### 12.6 Vérification build optional dep `live`

```bash
pip install -e ".[live]"
python -c "from web3 import Web3; print(Web3.__version__)"
# Doit retourner 6.x ou 7.x
```

---

## 13. Notes implémentation (pièges fréquents)

### 13.1 Piège : `ClobAuthDomain` reste version `"1"`

Confusion fréquente : V2 bump `Exchange` domain à `"2"` MAIS
`ClobAuthDomain` (utilisé pour L1 API auth signing) **reste `"1"`**.

Si l'implémenteur audite `signing/eip712.py` et bumpe les deux par
erreur (par exemple en faisant un find-replace `version="1"` →
`version="2"` global), les auth headers L1 deviennent invalides →
`401 Unauthorized` au premier API call.

Polycopy n'a rien à signer manuellement (le SDK V2 gère les 2
domains séparément). C'est juste un piège de **lecture** pour
quiconque audite la spec ou la doc Polymarket.

### 13.2 Piège : `timestamp` ms vs sec

V2 `timestamp` est en **millisecondes** (`int(time.time_ns() //
1_000_000)`). V1 `expiration` était en **secondes**.

Si un dev confond les deux unités lors d'un debug (par ex. en
inspectant un payload Order V2 et croit que `timestamp=1735320000000`
est invalide), il peut conclure à tort que la signature est cassée.
1735320000000 ms = 2025-12-27 00:00:00 UTC, valide.

Pas d'action polycopy — le SDK V2 gère. Documenter pour les
relectures.

### 13.3 Piège : `metadata` field bytes32

Doc V2 ne précise pas l'usage de `metadata`. Polymarket reserve
probablement ce champ pour des extensions futures (analytics tags,
internal correlation, etc.). Le SDK V2 default à
`BYTES32_ZERO = "0x" + "00" * 32`.

**Décision** : ne pas inventer de contenu. `OrderArgsV2.metadata`
reste `BYTES32_ZERO` côté polycopy. Si jamais `metadata != 0x0` est
rejeté par le backend (à tester sur clob-v2 si paranoïa), forcer
`0x0` partout.

### 13.4 Piège : `condition_id` ↔ `token_id` resolution

`getClobMarketInfo` prend un `condition_id` en input.
`FeeRateClient.get_fee_quote(token_id)` est exposé côté pipeline qui
a `token_id` mais pas immédiatement `condition_id`.

Path nominal D10 : `_check_buy` passe `condition_id=ctx.trade.condition_id`
explicitement. Zéro Gamma overhead.

Path fallback : si `condition_id=None`, lookup Gamma
`/markets-by-token/{token_id}` avec cache LRU. Warning structlog
`fee_rate_client_token_id_resolved_via_gamma`.

Si un dev oublie de passer `condition_id` (ex: dans un nouveau call
site), pipeline marche mais avec ~50-200ms latence supplémentaire et
warning visible. À monitorer post-merge.

### 13.5 Piège : SDK V2 dual-version capability

Le SDK V2 signe V1 orders quand backend `/version=1`. Cohérent D11.
**Conséquence importante** : pre-cutover testing contre prod URL
(qui est encore V1) **ne valide pas** le path V2 signing.

Pour tester le path V2 signing pré-cutover, il faut soit :
1. Pointer sur `clob-v2.polymarket.com` (testnet V2) via
   `POLYMARKET_CLOB_HOST` override.
2. Mock `_resolve_version` pour retourner 2 dans les tests.

Le test intégration `test_clob_v2_signature_local_validation` (§9.2)
utilise option 2 implicitement (build `OrderBuilder.build_order(version=2)`
direct).

### 13.6 Piège : `use_server_time` tradeoff (D8)

`use_server_time=True` ajoute +50-200ms par order signing (call
`/time`). En batch (par ex. 50 orders/s), cela peut devenir un
bottleneck.

**Polycopy fait au max ~10 orders/heure** (copy ratio 1% × 5-10
trades/jour des wallets cibles). Tradeoff totalement acceptable.

Si un futur module fait du batch sizing important, considérer
désactiver via `POLYMARKET_USE_SERVER_TIME=false` + sync NTP serveur
strict.

### 13.7 Piège : SDK V2 + builder_config + per-order override

Le SDK V2 fait
([client.py L676-L716](https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/client.py#L676-L716)) :

```python
def create_order(self, order_args, options):
    ...
    if self.builder_config and self.builder_config.builder_code:
        if not getattr(order_args, "builder_code", None) or order_args.builder_code == BYTES32_ZERO:
            order_args.builder_code = self.builder_config.builder_code
    ...
```

Soit : si `BuilderConfig` global set ET `OrderArgs.builder_code`
non-set/zéro → SDK auto-fill avec le global. Sinon `OrderArgs.builder_code`
gagne (override per-order).

**Polycopy ne set jamais `OrderArgs.builder_code` per-order** (cf.
`_build_order_args` qui ne passe que `token_id/price/size/side`).
Le default `BYTES32_ZERO` laisse le SDK faire l'auto-fill. Cohérent
D9.

### 13.8 Piège : pUSD = 6 decimals (pas 18)

L'utilisateur peut être habitué à ETH (18 decimals) ou WETH. **pUSD
suit USDC = 6 decimals**. Dans le wrap script :

```python
USDC_DECIMALS = 6
amount_wei = int(args.amount * (10 ** USDC_DECIMALS))   # PAS 10**18
```

Si oublié → wrap 100 pUSD réel = 0.000000000000000100 pUSD vs
attendu 100 pUSD → l'utilisateur voit 0 dans son wallet et panique.

Test smoke E2E sur Amoy testnet (chain 80002) avec un faucet USDC.e
testnet recommandé avant le run mainnet.

### 13.9 Piège : `_resolve_version` race condition

Le SDK V2 `__resolve_version` n'est pas thread-safe (mais polycopy
est mono-thread asyncio, donc OK). Si un futur module utilise
`asyncio.gather()` pour POST plusieurs orders en parallèle pendant
le flip backend, théoriquement 2 coroutines pourraient appeler
`_resolve_version(force_update=True)` simultanément et faire 2
calls `/version`. Bénin (idempotent), juste +1 round-trip.

Pas d'action requise. Documenter pour mémoire.

---

## 14. Hors scope (liste exhaustive)

Cf. §2.2. Récapitulation pour easier search :

- ❌ EIP-1271 smart contract wallets support — feature future.
- ❌ Migration positions historiques V1 → V2 — pas applicable.
- ❌ Multi-collateral support (USDC.e + pUSD) — V2 élimine USDC.e.
- ❌ Builder code fee marketplace optimization — feature business.
- ❌ Backward-compat layer V1↔V2 polycopy-side — fait par
  Polymarket backend + SDK V2 dual-version.
- ❌ Refactor DB schema collateral_token — pas de stockage côté
  polycopy (D13).
- ❌ Maker fees / rebates polycopy-side — taker-only par design.
- ❌ Fee-rate WebSocket — REST + cache 60s suffit.
- ❌ Backtest historique avec fees V2 — spec future si besoin.
- ❌ Dashboard panel V2 migration status — éphémère.
- ❌ Alertes Telegram dédiées cutover — heartbeat M7 + alerte
  `executor_auth_fatal` couvrent.
- ❌ Bump `polymarket-apis>=0.5.0` — utilise endpoints publics
  (Gamma, Data API), inchangée par V2.
- ❌ Préservation V1 bot pour rollback post-cutover — V1 backend
  offline post-cutover, hotfix uniquement.
- ❌ Toucher scoring v2.1 / v2.1.1 (M14/M15) — orthogonal (D14).
- ❌ Toucher invariants M17 cross-layer — préservés strict.
- ❌ Toucher dashboard `/scoring` ou `/traders` UX — orthogonal,
  reportable MH si désiré.
- ❌ Migration Alembic — aucune (D13).

---

## 15. Mapping origines (traçabilité)

| Item | Source primaire | Source secondaire |
|---|---|---|
| ME.1 | Doc V2 §SDK Migration | [py-clob-client-v2 GitHub README](https://github.com/Polymarket/py-clob-client-v2) + inspection [client.py L121-L142](https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/client.py) |
| ME.2 | Décision interne D7+D8 (testabilité + clock skew defense) | [docs/architecture.md §Latence](../architecture.md) |
| ME.3 | Doc V2 §getClobMarketInfo + capture live 2026-04-27 | M16 spec [docs/specs/M16-dynamic-fees-ev.md](M16-dynamic-fees-ev.md) §11 (rétrocompat path) |
| ME.4 | Doc V2 §Polymarket USD + Collateral Onramp + [docs.polymarket.com/resources/contracts](https://docs.polymarket.com/resources/contracts) | Vérification live 2026-04-27 (curl `/concepts/pusd`) |
| ME.5 | Doc V2 §Builder Program | [polymarket.com/settings?tab=builder](https://polymarket.com/settings?tab=builder) UI |
| ME.6 | Test environnement V2 ([clob-v2.polymarket.com](https://clob-v2.polymarket.com)) | tests/integration/ pattern M2/M11 |
| ME.7 | [docs/todo.md §14](../todo.md#L554) | Discord Polymarket Dev hands-on onboarding |
| D1-D14 | Inspection live SDK + endpoints + capture fixtures | session précédente [docs/next/ME.md](../next/ME.md) (corrigée) |

Sources H-V2-1 → H-V2-4 (validations empiriques 2026-04-27) :

- **H-V2-1 ✅** — `pip index versions py-clob-client-v2` retourne
  `1.0.0` (publié 2026-04-17 sur PyPI).
- **H-V2-2 ✅** — `/resources/contracts` liste pUSD + CollateralOnramp
  en clair (cf. table §1.4 adresses).
- **H-V2-3 ✅** — `clob-v2.polymarket.com/clob-markets/{cid}` retourne
  schéma documenté (`mts/mos/fd/t/...`) sur 1 marché crypto fee-enabled
  + 1 politics fee-free.
- **H-V2-4 ✅** — Fees calculés `0.072 × (0.5)² = 0.018` (1.80%) à
  p=0.5 sur crypto market réel. Cohérent doc + cohérent fallback
  conservateur.

---

## 16. Prompt d'implémentation pour `/implement-module`

```markdown
# Tâche

Implémenter M18 (Polymarket V2 + pUSD migration) selon la spec
[docs/specs/M18-polymarket-v2-migration.md](docs/specs/M18-polymarket-v2-migration.md).

7 commits atomiques (cf. spec §17) :

1. ME.1 — Bump SDK + 4 imports + method rename
2. ME.2 — `POLYMARKET_CLOB_HOST` + `USE_SERVER_TIME` settings
3. ME.3 — `FeeRateClient` swap + `FeeQuote` DTO + `_compute_effective_fee_rate`
4. ME.4 — pUSD env vars + wrap helper script + optional dep `live`
5. ME.5 — Builder code optionnel (`POLYMARKET_BUILDER_*`)
6. ME.6 — Tests intégration `test_clob_v2_*.py` + fixtures
7. ME.7 — `docs/todo.md §14` enrichi cutover ops

# Prérequis (à lire avant)

- [docs/specs/M18-polymarket-v2-migration.md](docs/specs/M18-polymarket-v2-migration.md)
- [docs/specs/M3-executor.md §2](docs/specs/M3-executor.md#L60) (triple
  garde-fou — strict no-touch)
- [docs/specs/M16-dynamic-fees-ev.md](docs/specs/M16-dynamic-fees-ev.md)
  (FeeRateClient contract M16 — alias deprecated préservé)
- [docs/specs/M17-cross-layer-integrity.md](docs/specs/M17-cross-layer-integrity.md)
  (invariants exec_mode + kill switch — strict no-touch)
- [CLAUDE.md](CLAUDE.md) §Conventions + §Sécurité

# Contraintes

- **Lecture seule** sur `src/`, `tests/`, docs sources, doc officielle
  Polymarket.
- **Préserver le contrat M16 `FeeRateClient.get_fee_rate(token_id) ->
  Decimal`** comme alias deprecated — tests M16 passent inchangés.
- **Triple garde-fou M3 + 4ᵉ M8** strictement préservés (cf. §8.1).
- **Pas de migration Alembic** (D13).
- **Pas de touche scoring** (D14).
- **Pas de touche invariants M17** (kill switch ordering, sentinel,
  bypass digest CRITICAL, exec_mode segregation, last_known_mid TTL).
- **Versioning sacré** : aucune row réécrite.

# Demande-moi confirmation AVANT

- Modifier `pyproject.toml` (bump dep + optional-dep `live`).
- Refactor `FeeRateClient` (introduction `FeeQuote` + `get_fee_quote`).
- Réécrire `_compute_effective_fee_rate` dans `pipeline.py` (D6).
- Toucher `clob_write_client.py` constructeur.
- Update `CLAUDE.md` §Conventions + §Sécurité.

# STOP et signale si

- `clob-v2.polymarket.com/clob-markets/<id>` retourne 404 ou un schéma
  divergent → la décision **D6** doit être revisitée.
- Polymarket annonce un délai cutover (Discord Dev / Twitter) →
  ajuster §11 phase timing.
- Une lecture du code révèle un consommateur de `py_clob_client` non
  inventorié dans §10.1 — élargir le scope d'imports.

# Smoke test final obligatoire avant merge

Cf. spec §12.1.

# Livrable

- 7 commits sur `main` (pas de branche, pas de PR — règle projet).
- Spec M18 mise à jour si la rédaction révèle un facteur non-anticipé
  (sinon laisser intacte).
- Ping final ≤ 10 lignes :
  - 7 commits ME.1 → ME.7 mergés
  - Tests : 22 unit + 3 intégration verts
  - Smoke runtime OK contre clob-v2 testnet
  - Charge réelle dev (vs estimé 2 jours)
  - Risques résiduels post-merge
```

---

## 17. Commit messages proposés

### ME.1 — Bump SDK V1 → V2

```text
feat(executor): M18 ME.1 swap py-clob-client → py-clob-client-v2

- pyproject.toml : py-clob-client>=0.20.0 → py-clob-client-v2==1.0.0
- mypy override : py_clob_client.* → py_clob_client_v2.*
- clob_write_client.py : 4 imports (ClobClient, OrderArgs, OrderType
  via package root) + suppression `BUY/SELL` constants (pass string
  directly per D4)
- create_or_derive_api_creds() → create_or_derive_api_key() (D2)
- _build_order_args : pass built.side (string) directly (D4)
- tests/unit + integration : mock + import path V1 → V2
- Le SDK V2 garde le constructor V1-style positionnel (D1) — diff
  minimal, pas de refactor structurel.
```

### ME.2 — `POLYMARKET_CLOB_HOST` + `USE_SERVER_TIME`

```text
feat(executor): M18 ME.2 POLYMARKET_CLOB_HOST configurable + use_server_time

- config.py : +polymarket_clob_host (default https://clob.polymarket.com,
  pattern strict https://...) + polymarket_use_server_time (default True)
- Propage settings.polymarket_clob_host aux 5 clients HTTP polycopy :
  ClobReadClient, ClobMetadataClient, ClobOrderbookReader,
  FeeRateClient, ClobWriteClient (via SDK ClobClient.host)
- ClobWriteClient passe use_server_time au constructor SDK V2
- .env.example : +bloc M18 commenté
- Permet test pré-cutover contre clob-v2.polymarket.com (D7)
- Anti clock-skew sur timestamp ms V2 (D8)
```

### ME.3 — `FeeRateClient` swap + `FeeQuote`

```text
feat(strategy): M18 ME.3 FeeRateClient V2 + FeeQuote DTO + reorganize fee math

- New DTO FeeQuote(rate, exponent) — exposes fd.r ET fd.e directement
  depuis getClobMarketInfo(condition_id) V2
- New method FeeRateClient.get_fee_quote(token_id, condition_id=) :
  path nominal V2 (condition_id fourni) zéro Gamma overhead ;
  fallback Gamma /markets-by-token/{token_id} avec cache LRU (D10)
- Legacy alias get_fee_rate(token_id) -> Decimal préservé (M16
  contract) avec warning structlog 1× par token (LRU 500)
- PositionSizer._compute_effective_fee_rate réécrit : consume
  quote.exponent directement (D6) — disparition du mapping hardcodé
  feeType → (rate_param, exponent) qui sous-estimait fees crypto
- _check_buy : await get_fee_quote(asset_id, condition_id=ctx.trade.condition_id)
- Fixtures : capture live 2026-04-27 — clob_v2_market_crypto_sample.json
  (fd:{r:0.072,e:1,to:true}) + clob_v2_market_fee_free_sample.json
  (sans fd)
- Tests M16 préservés inchangés (alias deprecated).
```

### ME.4 — pUSD env vars + wrap script

```text
feat(executor): M18 ME.4 pUSD collateral onramp + wrap helper script

- config.py : +polymarket_collateral_onramp_address
  (default 0x93070a847efEf7F70739046A929D47a521F5B8ee) +
  polymarket_usdc_e_address (default
  0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174)
- Cross-field validator : execution_mode=live + onramp empty → raise
- pyproject.toml : +[project.optional-dependencies] live = ["web3>=6.0,<8.0"]
- New scripts/wrap_usdc_to_pusd.py (~80 LOC) — helper one-time
  pré-flip live. Approve USDC.e → onramp.wrap(USDC.e, funder, amount)
- Lazy web3 import (clear error si pip install -e ".[live]" missing)
- Validator preflight EXECUTION_MODE=dry_run → abort sauf --force-dry-run
- Logs structlog uniquement (pas de print, cf. CLAUDE.md)
- pUSD = 6 decimals (D8 piège noté §13.8)
```

### ME.5 — Builder code optionnel

```text
feat(executor): M18 ME.5 builder code support optionnel

- config.py : +polymarket_builder_code (Optional, hex32 pattern)
  + polymarket_builder_address (Optional, hex40 pattern, default
  POLYMARKET_FUNDER si builder_code set)
- ClobWriteClient.__init__ : instancie BuilderConfig si builder_code
  set, passe au SDK V2 constructor builder_config kwarg
- Default None = comportement strict M3..M16 préservé (D9)
- Public, loggé OK (cohérent MACHINE_ID discipline) — log
  builder_code_set: bool (pas la valeur)
- ROI direct : fee rebates apparents sur Builder Leaderboard
  Polymarket (~$50-150/an estimé sur capital $1k)
```

### ME.6 — Tests intégration V2

```text
test(integration): M18 ME.6 tests V2 endpoints opt-in (-m integration)

- test_clob_v2_market_info_live.py : smoke getClobMarketInfo sur 1
  marché crypto fee-enabled + 1 politics fee-free
- test_clob_v2_signature_local.py : build V2 order via
  OrderBuilder.build_order(version=2), vérifie payload JSON wire
  contient timestamp/metadata/builder + signature hex valide
- test_clob_v2_fee_rate_live.py : FeeRateClient.get_fee_quote sur
  marché crypto réel, asserte rate > 0 et exponent in (1, 2)
- Fixtures clob_v2_market_*_sample.json capturées live 2026-04-27
```

### ME.7 — Procédure cutover ops

```text
docs(m18): M18 ME.7 procédure cutover Polymarket V2 + CLAUDE.md M18

- docs/todo.md §14 : 4 phases (ship lundi 27/04 ~22h UTC,
  auto-flip mardi 28/04 ~11h UTC, smoke ~11h30, monitoring 24h)
- D11 dual-version capability documentée — élimine fenêtre
  critique 11h UTC du 28 avril
- CLAUDE.md §Conventions + §Sécurité : bloc M18 (référence spec)
- alembic upgrade head reste 0010 (no migrations to apply, D13)
```

---

## 18. Critères d'acceptation

### 18.1 Critères fonctionnels (binaires GO/NO-GO)

- [ ] **F1** — `pip install py-clob-client-v2==1.0.0` succeed depuis
  PyPI.
- [ ] **F2** — `python -c "import py_clob_client"` lève
  `ModuleNotFoundError` (V1 désinstallé strict).
- [ ] **F3** — `python -c "import py_clob_client_v2; print(...)"` succeed.
- [ ] **F4** — Boot `python -m polycopy --verbose` en `EXECUTION_MODE=dry_run`
  log `executor_started mode=dry_run m8=true` sans ERROR.
- [ ] **F5** — Boot en dry_run NE log PAS `executor_creds_ready` (lazy
  init préservé — triple garde-fou §1).
- [ ] **F6** — Set `EXECUTION_MODE=live` SANS `POLYMARKET_PRIVATE_KEY`
  → boot raise `RuntimeError("Executor cannot start without...")`
  AVANT TaskGroup (triple garde-fou §2).
- [ ] **F7** — Set `EXECUTION_MODE=live` AVEC clés mais SANS
  `POLYMARKET_COLLATERAL_ONRAMP_ADDRESS` → boot raise ValueError clair.
- [ ] **F8** — Set `EXECUTION_MODE=live` AVEC clés + onramp default →
  boot succeed, log `executor_creds_ready signature_type=2
  use_server_time=true builder_code_set=false`.
- [ ] **F9** — Set `POLYMARKET_BUILDER_CODE=0x` + 64 hex → boot OK,
  log `builder_code_set=true`.
- [ ] **F10** — Set `POLYMARKET_BUILDER_CODE=invalid` → boot raise
  ValidationError pattern.

### 18.2 Critères tests

- [ ] **T1** — `pytest tests/unit/ -x --tb=short` retourne 0 failure
  (≥22 nouveaux tests M18 verts + ≥38 régression existants).
- [ ] **T2** — `ruff check . && ruff format --check . && mypy src --strict`
  retourne 0 erreur.
- [ ] **T3** — `pytest -m integration tests/integration/test_clob_v2_*.py
  -x --tb=short` retourne 0 failure (3 tests verts) — opt-in
  pré-merge avec connexion réseau.
- [ ] **T4** — Tests M16 existants (`test_fee_rate_client.py`) tous
  verts INCHANGÉS (alias deprecated préserve le contrat).
- [ ] **T5** — Tests M3 existants (`test_clob_write_client.py`) tous
  verts INCHANGÉS sauf le mock rename (1 line diff).
- [ ] **T6** — Test invariant `test_clob_v2_no_secret_leak.py` retourne
  0 match secrets dans caplog.

### 18.3 Critères runtime smoke

- [ ] **R1** — Smoke runtime `EXECUTION_MODE=dry_run
  POLYMARKET_CLOB_HOST=https://clob-v2.polymarket.com python -m polycopy
  --verbose` tourne 30s sans ERROR, log
  `clob_market_fee_quote_fetched` au moins 1× sur fee-enabled market.
- [ ] **R2** — Smoke runtime contre prod URL (default) tourne 30s sans
  ERROR.
- [ ] **R3** — `alembic upgrade head` retourne "no migrations to apply"
  (D13 — head reste 0010).

### 18.4 Critères ops cutover

- [ ] **O1** — Phase 1 (lundi 27/04 ~22h UTC) : merge sur main, pull
  + pip install + restart → bot tourne sans ERROR pendant 12h.
- [ ] **O2** — Phase 2 (mardi 28/04 ~11h UTC) : aucune intervention.
- [ ] **O3** — Phase 3 (mardi 28/04 ~11h30 UTC) : 0 erreur
  `signature_invalid` dans la dernière heure de logs.
- [ ] **O4** — Phase 4 (mercredi 29/04) : Telegram heartbeat OK,
  dashboard `/strategie` decisions cohérentes.

### 18.5 Critères invariants préservés (zéro régression)

- [ ] **I1** — Triple garde-fou M3 : 4 tests M3 existants verts
  inchangés (sauf method rename mock).
- [ ] **I2** — 4ᵉ garde-fou M8 : `_persist_realistic_simulated`
  assert intact, tests M8 existants verts.
- [ ] **I3** — Pipeline strategy ordre intact (TraderLifecycle →
  Market → EntryPrice → PositionSizer → SlippageChecker →
  RiskManager) — tests `test_pipeline_order.py` verts.
- [ ] **I4** — M16 contrat `get_fee_rate(token_id) -> Decimal` préservé
  comme alias — tests M16 existants verts.
- [ ] **I5** — M17 invariants intacts : kill switch ordering, sentinel
  permissions, bypass digest CRITICAL, exec_mode segregation —
  tests M17 verts.
- [ ] **I6** — Versioning sacré : `git diff --stat` sur
  `src/polycopy/discovery/scoring/` retourne 0 changement (D14).
- [ ] **I7** — Schema DB intact : `alembic upgrade head` no-op + `git
  diff` sur `alembic/versions/` retourne 0 changement (D13).

### 18.6 Critères doc

- [ ] **D1** — CLAUDE.md §Conventions + §Sécurité enrichis bloc M18.
- [ ] **D2** — docs/todo.md §14 enrichi 4 phases + D11.
- [ ] **D3** — .env.example bloc M18 commenté lisible.

---

**Fin de la spec M18.**

Document actionnable seul — un implémenteur fresh qui lit M18.md doit
pouvoir merger les 7 commits sans revenir lire MZ ou les briefs
MA/MB/MC/MD. Les décisions D1-D14 sont **figées** et reposent sur
inspection live du SDK + endpoints + doc officielle vérifiés
2026-04-27.
