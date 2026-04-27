# ME — Polymarket CLOB V2 + pUSD migration

**Priorité** : 🔥 P0 (hard deadline imposé, le bot casse mardi 28 avril ~11h UTC sinon)
**Charge estimée** : M (2-3 jours dev + smoke test cutover day)
**Branche suggérée** : `feat/ctf-exchange-v2`
**Prérequis** : aucun (indépendant des autres modules de la roadmap)
**Bloque** : tous les futurs modules orientés executor/fees/order si pas shippé avant 28 avril
**Numéro de spec proposé** : **M18** (après MD=M17 cross-layer integrity)

---

## 1. Objectif business

Polymarket déploie une **upgrade complète de la stack exchange** annoncée
officiellement le 2026-04-06 et confirmée par la doc
[docs.polymarket.com/v2-migration](https://docs.polymarket.com/v2-migration).
Cutover **mardi 28 avril 2026 ~11h00 UTC**, ~1h de downtime, **aucune
backward-compat post-go-live**.

3 changements simultanés :

1. **CTF Exchange V2** (nouveaux contrats Polygon) : Order struct simplifiée,
   matching optimisé, support EIP-1271 (smart contract wallets), builder
   codes onchain pour fee rebates.
2. **Polymarket USD (pUSD)** : nouveau collateral token ERC-20 wrappant
   USDC.e 1:1 via `Collateral Onramp.wrap()`. Action manuelle one-time pour
   les API-only traders (notre cas) — non requise tant qu'on reste en
   `EXECUTION_MODE=dry_run` (pas de signature live).
3. **Nouveau SDK CLOB-Client** : packages `@polymarket/clob-client-v2`
   (TypeScript) et `py-clob-client-v2` (Python) **séparés** des V1 — pas
   d'in-place upgrade. Auto-switch V1↔V2 via version endpoint côté backend
   pour les clients à jour.

**Conséquence pour polycopy** : si on n'adapte pas l'Executor M3 + le
FeeRateClient M16 + le schema collateral d'ici lundi 27 avril soir, le bot
qui restart en V1 cassera silencieusement le 28 avril ~11h UTC (signatures
rejetées par le nouvel orderbook V2). Le mode dry-run **ne nous protège
pas** : les builders d'order et les queries fee-rate touchent quand même
les endpoints V2-only après le cutover.

## 2. Contexte & problème observé

### 2.1 Source de vérité

- **Doc officielle** : [https://docs.polymarket.com/v2-migration](https://docs.polymarket.com/v2-migration)
  (page principale migration guide, confirmée 2026-04-26 par le user via
  screenshot)
- **Annonce** : @PolymarketDev tweet 2026-04-06 + Discord developer channel
  hands-on onboarding
- **Test environnement live depuis 2026-04-17** : `https://clob-v2.polymarket.com`
  — point ton client ici pour tester avant cutover. Le 28 avril ~11h UTC,
  l'URL `https://clob.polymarket.com` (prod) bascule sur le backend V2 ;
  pas de changement client-side post-cutover si on a déjà pointé sur la
  prod URL.
- **Repos GitHub à vérifier** :
  - [github.com/Polymarket/py-clob-client-v2](https://github.com/Polymarket/py-clob-client-v2) (v1.0.0)
  - [github.com/Polymarket/clob-client-v2](https://github.com/Polymarket/clob-client-v2) (TypeScript v1.0.0)
  - [github.com/Polymarket/ctf-exchange](https://github.com/Polymarket/ctf-exchange) (contrats V2)
- **Page contrats** : `docs.polymarket.com/resources/contracts` (référencée
  par la doc V2 pour les adresses CollateralOnramp + pUSD ; à fetcher
  pendant la rédaction de la spec)

### 2.2 Order struct V2 — diff exact V1 → V2

**Champs supprimés du signed struct V2** :
- `nonce` (remplacé par `timestamp` ms pour l'unicité)
- `feeRateBps` (fees calculés à match-time côté protocol, pas signés)
- `taker` (toujours `address(0)` à zéro)
- `expiration` (retiré du signed struct V2)

**Champs ajoutés** :
- `timestamp` (uint256, **millisecondes** — order creation time, ne PAS
  confondre avec V1 expiration)
- `metadata` (bytes32, opaque — usage non-documenté à date, défaut `0x0`)
- `builder` (bytes32, builder code attribution onchain, défaut `0x0`)

**Champs conservés (inchangés)** :
- `salt` (uint256)
- `maker` (address)
- `signer` (address)
- `tokenId` (uint256)
- `makerAmount` (uint256)
- `takerAmount` (uint256)
- `side` (uint8 : 0=BUY, 1=SELL — encodage uint8 pour signing, le wire body
  reste string)
- `signatureType` (uint8 : 0=EOA, 1=Magic/proxy, 2=Gnosis Safe — inchangé
  vs V1)

**EIP-712 typed data complet V2** :

```text
Order(uint256 salt, address maker, address signer, uint256 tokenId,
      uint256 makerAmount, uint256 takerAmount, uint8 side,
      uint8 signatureType, uint256 timestamp, bytes32 metadata,
      bytes32 builder)
```

### 2.3 EIP-712 domain V2

- **Version bump** : `"1"` → `"2"` sur le domain Exchange uniquement.
  `ClobAuthDomain` (utilisé pour L1 API auth) **reste version `"1"`**.
- **Standard Risk Exchange `verifyingContract`** :
  `0xE111180000d2663C0091e4f400237545B87B996B`
- **Neg Risk Exchange `verifyingContract`** :
  `0xe2222d279d744050d28e00520010520000310F59`
- chainId reste **137** (Polygon mainnet).

### 2.4 SDK package

| Lang | Old | New | Note |
|---|---|---|---|
| Python | `py-clob-client` | `py-clob-client-v2==1.0.0` | Package séparé, **pas un in-place upgrade**. Désinstaller l'ancien pour éviter shadow imports. |
| TypeScript | `@polymarket/clob-client` | `@polymarket/clob-client-v2@1.0.0` | Idem |

### 2.5 Constructor signature change

- **V1 (positional args)** : `ClobClient(host, chainId, signer, creds, signatureType, ...)`
- **V2 (options object)** :
  ```python
  client = ClobClient({
      "host": ...,
      "chain": ...,           # renommé depuis chainId
      "signer": ...,
      "creds": ...,
      "signature_type": ...,
      "funder_address": ...,
      "use_server_time": ...,
      "builder_config": ...,
      "get_signer": ...,
      "retry_on_error": ...,
      "throw_on_error": ...,
  })
  ```
- **Removed args V2** : `tickSizeTtlMs`, `geoBlockToken`.

### 2.6 New API endpoint `getClobMarketInfo()`

Remplace les queries directes `/fee-rate?token_id=` du M16 par un endpoint
consolidé qui retourne (par `condition_id`) :

| Champ | Type | Description |
|---|---|---|
| `mts` | float | minimum tick size |
| `mos` | float | minimum order size |
| `fd` | object | fee details : `{ r: rate, e: exponent, to: takerOnly }` |
| `t` | array | tokens array (token_id YES + NO) |
| `rfqe` | bool | RFQ enabled flag |

**Conséquence pour M16** : le `FeeRateClient.get_fee_rate(token_id)` doit
swap son backend de `/fee-rate?token_id=` (V1) vers
`getClobMarketInfo(condition_id)` (V2) puis extraire `fd.r` et `fd.e`. La
formule polycopy reste **identique** (`fee_rate × (p × (1-p))^exponent`)
mais les params viennent du nouvel endpoint.

### 2.7 Match-time fee calculation V2

Formule officielle (cf. doc V2) :

```text
fee = C × feeRate × p × (1 - p)
```

avec :
- `C` = collateral amount (collateral notional du fill)
- `feeRate` = `fd.r` du `getClobMarketInfo()` réponse
- `p` = probabilité implicite (= prix du side acheté)

**Invariants V2** :
- **Makers ne paient JAMAIS de fees** (taker-only).
- **Fees calculés à match-time côté protocol**, pas signés dans l'order
  (donc `feeRateBps` retiré du signed struct).
- **`userUSDCBalance` à passer sur market buy orders** pour que le SDK
  calcule un fill amount post-fees correct.

### 2.8 Polymarket USD (pUSD)

- **Type** : ERC-20 standard sur Polygon, backed 1:1 par USDC onchain.
- **Decimals** : non explicitement précisé dans la doc V2 (à vérifier sur
  `/resources/contracts` ; probable 6 decimals comme USDC).
- **Wrap function** (via contrat `CollateralOnramp`) :
  ```solidity
  function wrap(address asset, address to, uint256 amount) external
  function unwrap(address asset, address to, uint256 amount) external
  ```
- **Approval flow** : `approve(USDC.e, onramp_address, amount)` puis
  `onramp.wrap(USDC.e, funder_address, amount)`.
- **Frontend polymarket.com** : wrap automatique transparent (one-time
  approval prompt utilisateur).
- **API-only traders (nous)** : wrap manuel obligatoire AVANT le premier
  ordre live V2.
- **Adresses contrats** : à fetcher sur `docs.polymarket.com/resources/contracts`
  pendant la spec writing — **ne pas hardcoder**, utiliser une env var
  `POLYMARKET_COLLATERAL_ONRAMP_ADDRESS` + `POLYMARKET_PUSD_ADDRESS`.

**En mode dry-run (`EXECUTION_MODE=dry_run`)** : aucun wrap requis, le bot
ne signe aucun ordre live. Le wrap devient obligatoire au flip
`EXECUTION_MODE=live`.

### 2.9 Builder codes (optionnel, ROI direct)

- **Registration** : récupère ton builder code via la page UI
  [polymarket.com/settings?tab=builder](https://polymarket.com/settings?tab=builder)
  (Builder Profile). Pas de signup form séparé.
- **Attribution onchain** : le code (bytes32) est inclus dans le signed
  Order struct via le champ `builder`. Apparaît sur le "Builder Leaderboard"
  Polymarket.
- **Fee rebate** : la doc mentionne "revenue sharing" mais les % exacts
  ne sont pas publiés (référence : `docs.polymarket.com/builders/fees`).
- **Headers V1 supprimés** : `POLY_BUILDER_*` HTTP headers eliminated en V2
  — l'attribution se fait uniquement via le signed `builder` field.
- **Plombage SDK V2** : passer une fois en `builderConfig: { builderCode }`
  dans le constructor, OU per-order via le param `builderCode`.

### 2.10 EIP-1271 smart contract wallet support

Annoncé dans le tweet officiel mais **détails non documentés** sur la page
migration. Hors scope direct polycopy : on utilise EOA (`signature_type=0`)
ou Gnosis Safe (`signature_type=2`). À noter pour mémoire — pas de refactor
requis côté polycopy.

### 2.11 Maintenance window

- **Tous les open orders V1 wiped au cutover** (cancellation forcée). Pas
  un problème pour polycopy : on est en FOK strict via `OrderType="FOK"`,
  aucun GTC maintenu côté serveur.
- **SDK auto-switch** : les clients V2 query un version endpoint côté
  backend. Au cutover, le backend renvoie la nouvelle version, le SDK
  switch sans intervention. Mais ça fonctionne uniquement si tu es déjà
  sur le SDK V2 — sinon V1 client reçoit une erreur signature_invalid sur
  le nouvel orderbook.
- **URL switching** : la prod URL `clob.polymarket.com` bascule
  automatiquement sur V2 backend au cutover. Aucune env var `CLOB_BASE_URL`
  à changer côté polycopy si on a déjà pointé sur prod.

### 2.12 Pourquoi c'est P0 hard deadline

Contrairement aux autres modules de la roadmap (P1/P2/P3 différables), V2
migration a un **deadline imposé externe** : Polymarket cutover le 28
avril, après quoi tout client V1 est cassé. Le scope est **borné et
documenté** par Polymarket (pas d'invention possible) — donc l'incertitude
est sur l'**execution timing**, pas sur le scope.

## 3. Scope (items détaillés)

### ME.1 — Bumper SDK `py-clob-client` → `py-clob-client-v2`

- **Location** : [pyproject.toml](../../pyproject.toml) +
  [src/polycopy/executor/clob_client.py](../../src/polycopy/executor/clob_client.py) +
  [src/polycopy/executor/clob_write_client.py](../../src/polycopy/executor/clob_write_client.py)
  (ou équivalent — à confirmer en lecture du codebase).
- **Ce qu'il faut faire** :
  - Désinstaller `py-clob-client` strictement (sinon shadow imports en
    Python). Test : `python -c "import py_clob_client; print(py_clob_client.__file__)"`
    doit lever `ModuleNotFoundError` après désinstall, puis
    `python -c "import py_clob_client_v2"` doit succeed.
  - Bumper la dep dans `pyproject.toml` : `py-clob-client-v2==1.0.0`.
  - Adapter le constructor : passage en options object dict, renommage
    `chain_id` → `chain`. Mapping détaillé §2.5.
  - Vérifier les imports : `from py_clob_client_v2 import ClobClient` (le
    nom du package change, le nom de la classe peut rester).
- **Tests requis** :
  - `test_clob_client_v2_construction_options_object`
  - `test_clob_client_v2_chain_param_replaces_chain_id`
  - `test_clob_client_v2_no_geoblock_no_tick_size_ttl_args`
- **Sources** : Doc V2 §SDK Migration + Constructor (§2.4 + §2.5 ci-dessus).
- **Charge item** : 0.5 jour

### ME.2 — Adapter Order struct V2 + EIP-712 signing path

- **Location** : `src/polycopy/executor/order_builder.py` (à confirmer
  en lecture) + tout fichier qui touche la signature ou le payload `/order`.
- **Ce qu'il faut faire** :
  - Drop les champs V1 : `nonce`, `feeRateBps`, `taker`, `expiration`.
  - Add les champs V2 : `timestamp` (ms, `int(time.time() * 1000)`),
    `metadata` (bytes32 default `b'\x00' * 32`), `builder` (bytes32 default
    `b'\x00' * 32` ou builder_code si configuré).
  - Bump EIP-712 domain version `"1"` → `"2"` sur le domain Exchange (mais
    PAS sur ClobAuthDomain qui reste `"1"` — vérifier que les 2 paths sont
    bien séparés côté SDK).
  - Update `verifyingContract` : Standard Risk
    `0xE111180000d2663C0091e4f400237545B87B996B`, Neg Risk
    `0xe2222d279d744050d28e00520010520000310F59`. Les exposer en env vars
    `POLYMARKET_EXCHANGE_V2_ADDRESS` + `POLYMARKET_NEG_RISK_EXCHANGE_V2_ADDRESS`
    (cohérent avec l'invariant CLAUDE.md "ne pas hardcoder les adresses").
  - Vérifier que `salt` est toujours généré pareil V2 (random uint256).
  - `side` reste un uint8 dans le signed struct (0=BUY, 1=SELL) — aucun
    changement côté polycopy si on utilise déjà des Literal `BUY`/`SELL`
    qui sont mappés au uint8 par le SDK.
- **Tests requis** :
  - `test_order_struct_v2_includes_timestamp_metadata_builder`
  - `test_order_struct_v2_drops_nonce_fee_rate_bps_taker_expiration`
  - `test_eip712_domain_version_bumped_to_2_for_exchange`
  - `test_eip712_clob_auth_domain_remains_version_1`
  - `test_signature_validates_against_v2_test_env`
- **Sources** : Doc V2 §Order signing flow + EIP-712 domain (§2.2 + §2.3).
- **Charge item** : 1 jour

### ME.3 — Migrer FeeRateClient M16 vers `getClobMarketInfo()`

- **Location** : [src/polycopy/executor/fee_rate_client.py](../../src/polycopy/executor/fee_rate_client.py) +
  [src/polycopy/strategy/pipeline.py:200-260](../../src/polycopy/strategy/pipeline.py#L200)
  (`PositionSizer._check_buy` qui consomme le fee rate).
- **Ce qu'il faut faire** :
  - **Préserver intact le contrat M16** : `FeeRateClient.get_fee_rate(token_id) -> Decimal`
    (signature publique inchangée pour ne pas casser MC.5 wiring + tests
    M16). Le swap est interne au client.
  - Implémenter le nouveau backend : `getClobMarketInfo(condition_id)`
    response → extract `fd.r` (rate) et `fd.e` (exponent) → calculer
    le fee rate effective via la formule M16 existante
    `fee_rate × (p × (1-p))^exponent`.
  - **Piège** : `getClobMarketInfo` prend un `condition_id`, pas un
    `token_id`. Le `FeeRateClient.get_fee_rate(token_id)` doit donc
    résoudre `token_id → condition_id` en interne (cache LRU déjà en place
    M16, conserver) avant le call.
  - **Conserver** la cache TTL 60s + LRU 500 + single-flight + tenacity
    + fallback `Decimal("0.018")` (M16 §11). Pas de refactor de
    l'infrastructure cache — uniquement le HTTP path interne.
  - **Conserver** le check `base_fee == 0` court-circuit (§M16) : V2
    expose la même sémantique via `fd.to` (takerOnly) et la valeur de
    `fd.r` qui peut être 0 sur les markets fee-free.
  - **Vérifier** que `/fee-rate?token_id=` est définitivement supprimé
    en V2 (la doc semble l'indiquer mais à confirmer en testant le 404
    sur clob-v2.polymarket.com).
- **Tests requis** :
  - `test_fee_rate_client_uses_get_clob_market_info_v2`
  - `test_fee_rate_client_resolves_token_id_to_condition_id_via_cache`
  - `test_fee_rate_client_extracts_fd_r_and_fd_e_from_response`
  - `test_fee_rate_client_preserves_m16_signature_get_fee_rate(token_id) -> Decimal`
  - `test_fee_rate_client_fallback_018_on_v2_endpoint_down`
  - `test_position_sizer_unchanged_post_v2_fee_client_swap` (régression)
- **Sources** : Doc V2 §getClobMarketInfo + Match-time fee (§2.6 + §2.7).
- **Charge item** : 1 jour

### ME.4 — Polymarket USD collateral (env vars + helper wrap)

- **Location** : [src/polycopy/config.py](../../src/polycopy/config.py) +
  nouveau `scripts/wrap_usdc_to_pusd.py` (helper one-time, hors run).
- **Ce qu'il faut faire** :
  - Add settings :
    - `POLYMARKET_COLLATERAL_ONRAMP_ADDRESS: str | None = None`
    - `POLYMARKET_PUSD_ADDRESS: str | None = None`
    - `POLYMARKET_USDC_E_ADDRESS: str = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"`
      (USDC.e Polygon canonique, pré-V2)
  - Validator Pydantic : si `EXECUTION_MODE=live` ET (`onramp_address` OU
    `pusd_address`) absent → raise au boot avec message clair "set both for
    Polymarket V2 live trading". En `EXECUTION_MODE=dry_run` → no-op
    (settings restent None, pas de wrap requis).
  - Helper script `scripts/wrap_usdc_to_pusd.py` (~80 LOC) :
    - Lit `POLYMARKET_PRIVATE_KEY` + `POLYMARKET_FUNDER` + onramp address.
    - Approve USDC.e → onramp pour `amount`.
    - Call `onramp.wrap(USDC.e_addr, funder, amount)`.
    - Vérifie le solde pUSD post-wrap.
    - Logs structlog uniquement (pas de `print` cf. CLAUDE.md
      conventions).
  - **Pas de modification du DB schema** : les `MyOrder` / `MyPosition` ne
    stockent pas l'adresse collateral, juste `condition_id`/`asset_id`.
  - **Adresses définitives à fetcher** sur
    `docs.polymarket.com/resources/contracts` au moment de la spec writing.
- **Tests requis** :
  - `test_settings_v2_collateral_env_vars_optional_in_dry_run`
  - `test_settings_v2_collateral_env_vars_required_in_live_mode`
  - `test_wrap_script_smoke` (mock web3, vérifier l'ordre approve → wrap)
- **Sources** : Doc V2 §pUSD mechanics (§2.8).
- **Charge item** : 0.5 jour (pas urgent en dry-run, le wrap script peut
  être ajouté en post-cutover si seulement live env)

### ME.5 — Builder code support (optionnel mais ROI direct)

- **Location** : [src/polycopy/config.py](../../src/polycopy/config.py) +
  fichier executor V2 (post-ME.1).
- **Ce qu'il faut faire** :
  - Setting : `POLYMARKET_BUILDER_CODE: str | None = None` (bytes32
    représenté en hex, ex `0x...`).
  - Validator : si set, doit être un valid hex 32 bytes (regex `^0x[0-9a-f]{64}$`).
  - Plombage : passer `builderConfig={"builderCode": settings.builder_code}`
    une fois dans le ClobClient V2 constructor (cf. §2.5). Le SDK le plomb
    automatiquement dans chaque Order.builder à la signature.
  - **Pas de plombage per-order** : on évite la duplication, le constructor
    config suffit.
  - **Aucun secret** : le builder code est public (apparaît onchain) — pas
    de discipline TELEGRAM_BOT_TOKEN-style. Loggé OK.
- **Tests requis** :
  - `test_settings_builder_code_validator_accepts_valid_hex32`
  - `test_settings_builder_code_validator_rejects_invalid_format`
  - `test_clob_client_constructor_includes_builder_config_when_set`
- **Sources** : Doc V2 §Builder codes (§2.9).
- **Charge item** : 0.5 jour
- **Décision** : à shipper **avec ou après ME.1-ME.3** selon bandwidth.
  Pas critique pour le cutover (default `None` = aucun builder code, cohérent
  M3..M16). À activer post-restart en setting le code en `.env`.

### ME.6 — Update tests intégration vs `clob-v2.polymarket.com`

- **Location** : [tests/integration/](../../tests/integration/) (à
  enrichir).
- **Ce qu'il faut faire** :
  - Add fixture env var `POLYMARKET_CLOB_HOST_TEST=https://clob-v2.polymarket.com`
    pour les tests intégration opt-in (`pytest -m integration`).
  - 2-3 tests intégration ciblés :
    - Smoke test `getClobMarketInfo(condition_id)` retourne la structure
      attendue (`mts/mos/fd/t/rfqe`).
    - Smoke test signature flow : créer un Order V2 dummy en dry-run mode
      du SDK, vérifier qu'il valide localement (pas de POST réel).
    - Smoke test fee rate calculation : feed un token_id réel, vérifier
      que la formule retourne un Decimal cohérent.
  - **Capturer les fixtures** dans `tests/fixtures/clob_v2_*.json` (cohérent
    pattern M2/M11) pour les tests unit ne dépendant pas du réseau.
- **Tests requis** :
  - `test_integration_clob_v2_get_market_info_real`
  - `test_integration_clob_v2_signature_local_validation`
  - `test_integration_fee_rate_v2_real_token`
- **Sources** : test environnement Polymarket §2.1.
- **Charge item** : 0.5 jour

### ME.7 — Procédure cutover ops + rollback

- **Location** : [docs/todo.md §14](../todo.md#14-polymarket-v2-cutover) +
  enrichir avec les détails post-spec writing.
- **Ce qu'il faut faire** :
  - Documenter la procédure exacte cutover dans la spec :
    - Phase 1 (lundi 27 avril soir) : merger `feat/ctf-exchange-v2` sur
      main si tests verts, ne PAS restart.
    - Phase 2 (mardi 28 ~10h30 UTC) : stop bot, reset DB (cohérent §3
      todo.md), wrap pUSD si EXECUTION_MODE=live (sinon skip), restart.
    - Phase 3 (mardi 28 ~11h30 UTC) : smoke test post-V2 (3 vérifications
      dans la spec).
    - Phase 4 (mercredi 29) : monitoring 24h.
  - Documenter le rollback : si erreur post-V2, **pas de retour V1
    possible** (Polymarket V1 offline post-cutover). Le rollback se fait
    via `git revert` + hotfix sur la branche V2 uniquement.
  - Documenter les risques connus + mitigations (cf. §15 todo.md tableau).
- **Pas de tests** (spec ops/runbook).
- **Charge item** : 0.5 jour

## 4. Architecture / décisions clefs

- **D1** : SDK swap sans abstraction custom — on utilise `py-clob-client-v2`
  directement, pas de wrapper polycopy autour. Cohérent M3 décision originale
  (trust py-clob-client). Si le SDK V2 introduit un bug, c'est un upstream
  fix Polymarket.
- **D2** : `FeeRateClient` (M16) garde sa **signature publique inchangée**
  (`get_fee_rate(token_id) -> Decimal`). Le swap V1→V2 est strictement
  interne au client. Préserve M16 MC.5 wiring + tests sans modification.
- **D3** : pas de wrap automatique USDC.e → pUSD en startup. **Action
  manuelle one-time via script Python** quand on flip live. Évite des
  appels onchain non-attendus au boot du bot.
- **D4** : adresses contrats Polymarket en env vars, **pas hardcodées**.
  Cohérent invariant CLAUDE.md + facilite les changes futurs (nouveaux
  exchanges, multi-chain).
- **D5** : EIP-712 `ClobAuthDomain` **reste version `"1"`** — uniquement
  l'Exchange domain bump à `"2"`. Confusion fréquente côté implémenteur,
  documenter explicitement dans la spec §11 piège.
- **D6** : `metadata` field bytes32 = `0x0` par défaut. Pas d'usage
  documenté côté Polymarket à date. Si futur usage (ex: tags d'analyse),
  reportable en feature add-on.
- **D7** : builder code optionnel par défaut. Default `None` → pas de
  plombage builder, comportement strict M3..M16 préservé. À activer
  via env var setting post-restart.
- **D8** : tests intégration opt-in via `pytest -m integration` pour ne
  pas casser CI dev (cohérent M2/M11).

## 5. Invariants sécurité

### 5.1 Triple garde-fou M3 + 4ᵉ M8 strictement préservés

- ❌ **Pas de modification de** `_persist_realistic_simulated` (M8) ni
  `_persist_sent_order` (M3) hors du strict nécessaire (constructor
  arguments).
- ❌ **Pas de modification de** `WalletStateReader.get_state()` (re-fetch
  pré-POST).
- ✅ **L'assert `dry_run=False` AVANT chaque `create_and_post_order`**
  reste intact dans la branche V2.
- ✅ **Lazy init `ClobClient`** : pas instancié si `EXECUTION_MODE=dry_run`
  (préserve la sémantique M3).

### 5.2 Discipline credentials

- `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`, CLOB L2 creds
  (`api_key`/`api_secret`/`api_passphrase`) — discipline IDENTIQUE M3.
  Aucun log même partiel, même en debug.
- Le seul log nouveau autorisé : `clob_v2_client_initialized` SANS aucun
  champ creds (juste le host + chain id + signature_type).
- **Builder code N'EST PAS un secret** (apparaît onchain) — discipline
  publique cohérente `MACHINE_ID`.

### 5.3 Signature V2 — anti-replay

- `salt` reste un uint256 random — anti-replay strict V2 cohérent V1.
- `timestamp` ms ajouté en V2 protège aussi contre les replays old-orders.
- **Piège** : `signature_type` mismatch en V2 = transactions rejetées
  silencieusement par CLOB V2 (cohérent V1). Documenter clairement les 3
  valeurs (0/1/2) dans la spec §11 pour éviter la confusion.

### 5.4 Wrap USDC.e → pUSD en mode dry-run

- **NE PAS exécuter le wrap script en dry-run**. Le bot dry-run ne signe
  aucun ordre live, donc le wrap n'a pas d'effet utile. Mais déclencher
  le wrap consume de la fee onchain (~$0.01-0.05 Polygon).
- Validator Pydantic : si script `wrap_usdc_to_pusd.py` lancé avec
  `EXECUTION_MODE=dry_run` → log WARNING + abort (sauf flag explicite
  `--force-dry-run`).

### 5.5 Versioning sacré (M14/M15)

V2 migration **n'introduit aucune nouvelle version de scoring**. Les
formules `compute_score_v2` (M14 v2.1) et `compute_score_v2_1_1` (M15)
sont 100% indépendantes de la couche executor. Aucun changement scoring.

## 6. Hypothèses empiriques à valider AVANT ship

Cette migration est **hard deadline imposé externe** — pas d'hypothèses
empiriques bloquantes au sens habituel des autres modules MA/MB/MC.
**Validations factuelles à confirmer avant rédaction de la spec** :

- **H-V2-1** : `py-clob-client-v2==1.0.0` est-il publié sur PyPI au moment
  de la spec writing ? (vérifier `pip index versions py-clob-client-v2`).
  Si retard côté Polymarket → repousser la spec d'1 jour.
- **H-V2-2** : la doc `/resources/contracts` liste-t-elle les adresses
  Polymarket USD + CollateralOnramp en clair ? À fetcher et confirmer
  pendant la spec writing. Si absence → bloquer ME.4 (spec ne peut pas
  être écrite avec adresses TBD).
- **H-V2-3** : le test environnement `clob-v2.polymarket.com` retourne
  bien `getClobMarketInfo(...)` avec le schéma documenté ? Smoke test
  curl à exécuter pendant la spec writing pour confirmer.
- **H-V2-4** : les fees calculés par V2 sur un trade dummy sont
  numériquement cohérents avec notre formule M16 actuelle ? Vérifier sur
  un token fee-enabled connu (crypto_fees_v2 ou sports_fees_v2).

## 7. Out of scope

- ❌ **EIP-1271 smart contract wallets support** — annoncé V2 mais pas
  documenté en détail. polycopy utilise EOA ou Gnosis Safe (legacy V1
  pattern). Reportable feature future si on a besoin de plomber un compte
  smart contract.
- ❌ **Market making algorithmique V2** (Avellaneda-Stoikov, etc.) — hors
  scope. polycopy reste un copy-trader.
- ❌ **Migration des positions historiques V1 vers schema V2** — pas
  applicable, on reset la DB au cutover (cf. todo.md §3).
- ❌ **Multi-collateral support** (USDC.e + pUSD coexistant) — V2
  élimine USDC.e du collateral. Pas de besoin de support dual.
- ❌ **Builder code marketplace / fee tier optimization** — feature
  business future, pas dev infra.
- ❌ **Backward-compat layer V1↔V2** — Polymarket le fait côté backend
  (auto-switch via version endpoint). Inutile de réimplémenter côté
  polycopy.
- ❌ **Refactor du DB schema collateral_token** — `MyOrder` /
  `MyPosition` ne stockent pas le collateral. Aucune migration DB requise.

## 8. Success criteria

1. **Branche `feat/ctf-exchange-v2` mergée sur `main` avant lundi 27 avril
   minuit UTC**, tests verts (ruff + mypy + pytest unit ≥ 80% coverage
   sur fichiers modifiés).
2. **Tests intégration sur `clob-v2.polymarket.com`** passent (3 tests
   §ME.6 verts).
3. **Mardi 28 avril ~11h UTC post-cutover** : le bot redémarre sans erreur
   en dry-run, premier cycle discovery + 1 trade simulé OK.
4. **Pas de régression M16** : le rejet `ev_negative_after_fees` continue
   de fonctionner sur les markets fee-enabled (test smoke post-restart).
5. **Telegram heartbeat OK** post-restart (`heartbeat` event arrive
   normalement).
6. **DB schema cohérent** : `alembic upgrade head` n'apporte aucune
   nouvelle migration (V2 ne change pas le schema polycopy).

## 9. Mapping origines (traçabilité)

| Item | Source primaire | Source secondaire |
|---|---|---|
| ME.1 | Doc V2 §SDK Migration | py-clob-client-v2 GitHub README |
| ME.2 | Doc V2 §Order signing flow + EIP-712 | EIP-712 spec ([eips.ethereum.org/EIPS/eip-712](https://eips.ethereum.org/EIPS/eip-712)) |
| ME.3 | Doc V2 §getClobMarketInfo | M16 spec [docs/specs/M16-dynamic-fees-ev.md](../specs/M16-dynamic-fees-ev.md) §11 (rétrocompat path) |
| ME.4 | Doc V2 §Polymarket USD + Collateral Onramp | docs.polymarket.com/resources/contracts |
| ME.5 | Doc V2 §Builder Program | polymarket.com/settings?tab=builder UI |
| ME.6 | Test environnement V2 | tests/integration/ pattern M2/M11 |
| ME.7 | docs/todo.md §14 | Discord Polymarket Dev hands-on onboarding |

## 10. Prompt de génération de spec

Bloc à coller dans une nouvelle conversation Claude Code pour générer
`docs/specs/M18-polymarket-v2-migration.md`.

````markdown
# Contexte

Lis [docs/next/ME.md](docs/next/ME.md) en entier.
C'est le brief actionnable de la migration Polymarket CLOB V2 + pUSD.
Il regroupe les détails techniques exhaustifs extraits de la doc officielle
[docs.polymarket.com/v2-migration](https://docs.polymarket.com/v2-migration)
+ les 7 items ME.1 → ME.7 de scope.

# Tâche

Produire `docs/specs/M18-polymarket-v2-migration.md` suivant strictement
le format des specs M14/M15/M16 récentes (§ numérotées : TL;DR, Motivation,
Scope, User stories, Architecture, Algorithmes, DTOs, Settings, Invariants
sécurité, Test plan, Impact existant, Migration, Commandes vérif, Hors
scope, Notes implémentation, Prompt implémentation, Commit message proposé,
Critères d'acceptation).

Numéro : M18 (après MA=M14, MB=M15, MC=M16, MD=M17 cross-layer integrity).

**Hard deadline externe** : Polymarket cutover **mardi 28 avril 2026
~11h UTC**. La spec doit être actionnable d'ici lundi 27 avril soir
maximum.

# Prérequis (à lire avant de rédiger)

- `CLAUDE.md` §Conventions + §Sécurité (triple garde-fou M3 + 4ᵉ M8,
  discipline credentials POLYMARKET_PRIVATE_KEY/CLOB L2, pas de hardcode
  d'adresses contrats)
- `docs/specs/M3-executor.md` (référence Executor original — section
  Triple garde-fou + ClobWriteClient signature path à adapter)
- `docs/specs/M16-dynamic-fees-ev.md` (référence FeeRateClient + formule
  fee polycopy à préserver pendant le swap V1→V2 endpoint)
- `docs/specs/M13_dry_run_observability_spec.md` comme **template de
  forme** (le plus récent, section structure + style cohérent)
- **Lire en live** la doc officielle Polymarket V2 :
  - https://docs.polymarket.com/v2-migration (page principale)
  - https://docs.polymarket.com/concepts/pusd (Polymarket USD)
  - https://docs.polymarket.com/resources/contracts (adresses contrats —
    récupérer pUSD + CollateralOnramp à JOUR au moment de la spec writing)
  - https://docs.polymarket.com/builders/fees (builder codes fees, optionnel)
- Confirmer la disponibilité PyPI :
  `pip index versions py-clob-client-v2` doit retourner `1.0.0`
  (ou supérieur si Polymarket re-released entre-temps). Si absent → STOP
  et signale à l'utilisateur (la spec ne peut pas être écrite si le SDK
  V2 n'existe pas encore officiellement).
- Hypothèses H-V2-1 → H-V2-4 du brief §6 — exécuter les checks factuels
  avant la rédaction.

# Contraintes

- **Lecture seule** sur `src/`, `tests/`, docs sources, doc officielle
  Polymarket
- **Écriture uniquement** `docs/specs/M18-polymarket-v2-migration.md`
- **Longueur cible** : 1100-1400 lignes (cohérent M13/M14/M15)
- **Pas de migration DB Alembic** : le schema collateral n'est pas stocké
  côté polycopy (cf. brief §7 Out of scope). Si la rédaction révèle un
  besoin contraire, signale à l'utilisateur AVANT d'écrire la migration.
- **Préserver le contrat M16 `FeeRateClient.get_fee_rate(token_id) -> Decimal`** :
  le swap V1→V2 est strictement interne. Aucune modification de
  `PositionSizer._check_buy` (M16 MC.2) ni des tests M16 existants.
- **Triple garde-fou M3 + 4ᵉ M8 strictement préservés** : aucune
  modification des asserts `dry_run=False` ni du re-fetch
  `WalletStateReader.get_state()` pré-POST.
- **Adresses contrats en env vars** : pas de hardcode (cohérent CLAUDE.md).
  Setting names alignés sur le pattern existant (ex
  `POLYMARKET_EXCHANGE_V2_ADDRESS`, `POLYMARKET_NEG_RISK_EXCHANGE_V2_ADDRESS`,
  `POLYMARKET_COLLATERAL_ONRAMP_ADDRESS`, `POLYMARKET_PUSD_ADDRESS`,
  `POLYMARKET_BUILDER_CODE`).
- **Ordre commits recommandé** : ME.1 (SDK bump) → ME.2 (Order struct +
  signature) → ME.3 (FeeRateClient swap) → ME.4 (env vars + wrap helper) →
  ME.5 (builder code optionnel) → ME.6 (tests intégration) → ME.7 (procédure
  cutover dans spec). 7 commits atomiques.

# Demande-moi confirmation AVANT

- Modifier `pyproject.toml` (bumper `py-clob-client` → `py-clob-client-v2`).
- Modifier le constructor de `ClobWriteClient` ou équivalent (signature
  options object).
- Toucher la chaîne EIP-712 signing (Order struct fields V2).
- Refactor `FeeRateClient` (swap endpoint).
- Update CLAUDE.md (§Conventions + §Sécurité).

# STOP et signale si

- `py-clob-client-v2` n'est pas encore publié sur PyPI au moment de la
  spec writing → impossible de rédiger ME.1 avec confiance.
- Les adresses pUSD ou CollateralOnramp ne sont pas publiées sur
  `/resources/contracts` au moment de la spec writing → ME.4 inactionnable.
- Le test environnement `clob-v2.polymarket.com` retourne 404 ou erreur
  inattendue sur `getClobMarketInfo` → schéma de l'endpoint à confirmer
  avant ME.3.
- Discovery Polymarket Discord d'un délai du cutover (ex: 28 avril →
  29 avril) → ajuster les success criteria de la spec.

# Smoke test final obligatoire avant merge

```bash
# 1. Tests unitaires
pytest tests/unit/test_clob_*v2* tests/unit/test_fee_rate_client*v2* \
       tests/unit/test_order_builder_v2* -x --tb=short

# 2. Tests intégration opt-in (vs clob-v2.polymarket.com)
pytest -m integration tests/integration/test_clob_v2_*.py -x --tb=short

# 3. Lint + types
ruff check . && ruff format --check . && mypy src --strict

# 4. Smoke runtime (dry-run, contre clob-v2 testnet) — doit démarrer
#    sans erreur, premier cycle discovery OK.
EXECUTION_MODE=dry_run \
SCORING_VERSION=v2.1 \
DISCOVERY_ENABLED=true \
POLYMARKET_CLOB_HOST=https://clob-v2.polymarket.com \
python -m polycopy --verbose
# Wait ~30s, vérifier les logs : aucun ERROR, presence de
# "clob_v2_client_initialized" + "discovery_cycle_started".
```

# Livrable

- Le fichier `docs/specs/M18-polymarket-v2-migration.md` complet
- Un ping final ≤ 12 lignes :
  - Tests estimés (cible : ~18-24 unit + 3 intégration)
  - Charge cumulée (cible : 2-3 jours dev avant lundi 27 avril)
  - Ordre commits recommandé (cohérent §10 du brief)
  - Risques résiduels post-spec writing (ex: SDK pas encore release)
  - Procédure cutover ops mardi 28 ~11h UTC (référence todo.md §14)
````

## 11. Notes d'implémentation

### Piège : EIP-712 domain version dual

Confusion fréquente : V2 bump `Exchange` domain à `"2"` MAIS `ClobAuthDomain`
(utilisé pour L1 API auth signing) **reste `"1"`**. Si l'implémenteur
bump les deux par erreur, l'auth headers L1 deviennent invalides → `401
Unauthorized` au premier API call. Documenter explicitement dans la spec
§Architecture + §Tests (`test_eip712_clob_auth_domain_remains_version_1`).

### Piège : token_id ↔ condition_id resolution

`getClobMarketInfo()` V2 prend un `condition_id` en input, alors que le
`FeeRateClient` M16 expose `get_fee_rate(token_id)`. Le mapping
`token_id → condition_id` doit être en cache LRU (idéalement réutiliser
le cache market metadata Gamma déjà en place dans `MarketFilter`). Si pas
de cache → 1 query Gamma par token_id par cycle = ~50× plus de calls
qu'avec le cache.

### Piège : `salt` toujours unique

V2 conserve `salt` dans le signed struct (cf. §2.2). Le SDK V2 le génère
random, ne pas le forcer côté polycopy. Si un test passe un `salt` fixe,
2 ordres consécutifs sur le même `tokenId/maker` au même `timestamp` ms
seraient strictement identiques → l'un des deux rejeté pour replay.

### Piège : `timestamp` ms vs sec

V2 `timestamp` est en **millisecondes** (`int(time.time() * 1000)`). V1
`expiration` était en secondes. Si un dev confond les deux unités lors de
la migration, les ordres sont signés avec un timestamp énorme (futur dans
~50 millénaires) ou minuscule (1970), et le matching engine peut rejeter
ou tolérer selon implem upstream. Test : `test_order_v2_timestamp_is_milliseconds_int`.

### Piège : `metadata` field bytes32

Doc V2 ne précise pas l'usage de `metadata`. Polymarket reserve
probablement ce champ pour des extensions futures (analytics tags ?
internal correlation ?). **Default `0x0000...0000`** strict — ne pas
inventer de contenu. Si jamais `metadata != 0x0` est rejeté par le
backend (à tester sur clob-v2), forcer `0x0` partout.

### Piège : test environnement post-cutover

Après le cutover (28 avril ~11h UTC), `clob-v2.polymarket.com` peut être
maintenu en parallèle de `clob.polymarket.com` (testnet permanent) ou
être déprécié. La doc V2 dit "test against V2 before go-live" suggère
qu'il devient redondant post-cutover. Pour les tests intégration
post-cutover, utiliser directement la prod URL.

### Piège : dry-run et wrap pUSD

En `EXECUTION_MODE=dry_run`, le bot ne signe aucun ordre live → le wrap
USDC.e → pUSD n'est PAS requis. Mais le validator Pydantic doit quand
même accepter des onramp/pusd addresses None en dry-run (config valide).
Au flip live, validator raise si l'un des deux manque + le user doit
exécuter le wrap script avant de relaunch en live.

### Piège : `signature_type` dans Order V2

Conserve la sémantique V1 (0=EOA, 1=Magic/proxy, 2=Gnosis Safe). Aucun
changement requis si l'utilisateur trade depuis un wallet déjà connu en
V1 (cas de polycopy). Mais ouvrir prochainement EIP-1271 (smart contract
wallet) en V2 → ajouter `signature_type=3` ou similaire (TBD doc V2,
non documenté à date).

### Références externes

- **Doc officielle** : [docs.polymarket.com/v2-migration](https://docs.polymarket.com/v2-migration)
- **Polymarket USD** : [docs.polymarket.com/concepts/pusd](https://docs.polymarket.com/concepts/pusd)
- **Adresses contrats** : [docs.polymarket.com/resources/contracts](https://docs.polymarket.com/resources/contracts)
- **Builder fees** : [docs.polymarket.com/builders/fees](https://docs.polymarket.com/builders/fees)
- **EIP-712 spec** : [eips.ethereum.org/EIPS/eip-712](https://eips.ethereum.org/EIPS/eip-712)
- **EIP-1271 spec** : [eips.ethereum.org/EIPS/eip-1271](https://eips.ethereum.org/EIPS/eip-1271)
- **py-clob-client-v2 GitHub** : [github.com/Polymarket/py-clob-client-v2](https://github.com/Polymarket/py-clob-client-v2)
- **clob-client-v2 (TS) GitHub** : [github.com/Polymarket/clob-client-v2](https://github.com/Polymarket/clob-client-v2)
- **CTF Exchange contracts repo** : [github.com/Polymarket/ctf-exchange](https://github.com/Polymarket/ctf-exchange)

### Questions ouvertes pertinentes

- **Q-V2-1** : EIP-1271 implémentation timing — Polymarket activera-t-il
  le support smart contract wallet au cutover du 28 avril, ou en
  rolling release post-V2 ? À surveiller via Discord Polymarket Dev.
- **Q-V2-2** : Builder code fee rebate % exact non documenté — solliciter
  Polymarket Dev pour clarifier avant d'investir le temps de plombage
  builder côté polycopy. Si rebate < 10 bps → ROI faible, ME.5 reportable.
- **Q-V2-3** : `getClobMarketInfo` rate limit — la doc V2 ne précise pas
  les rate limits. Tester via load test avant cutover. Si plus restrictif
  que V1 `/fee-rate` → revoir le TTL cache 60s du M16.
- **Q-V2-4** : `metadata` field future usage — Polymarket Dev annoncera-t-il
  un usage spec dans les 30 jours post-cutover ? Si oui, polycopy peut y
  plomber un correlation_id structlog.
