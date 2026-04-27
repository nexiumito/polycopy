"""Wrap USDC.e → Polymarket USD (pUSD) via CollateralOnramp (M18 ME.4).

One-time helper avant le flip ``EXECUTION_MODE=live``. En dry_run, le bot ne
signe aucun ordre live → wrap inutile.

Usage :

    pip install -e ".[live]"   # web3.py optional dep
    python scripts/wrap_usdc_to_pusd.py --amount 100  # USDC à wrap

Validator preflight : si ``EXECUTION_MODE=dry_run`` → log WARNING + abort
(sauf flag ``--force-dry-run`` explicite).

Cf. spec [docs/specs/M18-polymarket-v2-migration.md](../docs/specs/M18-polymarket-v2-migration.md)
§5.4.
"""

from __future__ import annotations

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
        "Cf. spec M18 §ME.4 + docs/setup.md."
    ) from e

from polycopy.config import Settings

log = structlog.get_logger(__name__)

# ABIs minimaux (méthodes utilisées uniquement)
USDC_ABI = [
    {
        "name": "approve",
        "type": "function",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "value", "type": "uint256"},
        ],
        "outputs": [{"type": "bool"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"type": "uint256"}],
    },
]
ONRAMP_ABI = [
    {
        "name": "wrap",
        "type": "function",
        "inputs": [
            {"name": "asset", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [],
    },
]
# Même interface ERC-20 que USDC (balanceOf suffit pour la verif post-wrap).
PUSD_ABI = USDC_ABI

USDC_DECIMALS = 6

# Adresse pUSD V2 — owned par le SDK upstream `py_clob_client_v2`
# (config.get_contract_config(137).collateral). Listée ici en CONST pour le
# verify post-wrap UNIQUEMENT (pas de side-effect — read-only). Si Polymarket
# re-deploy le contrat, l'utilisateur bumpe le SDK ; ce script reste correct
# tant qu'il consume `polymarket_collateral_onramp_address` depuis settings.
PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"


def main() -> int:
    parser = argparse.ArgumentParser(description="Wrap USDC.e → pUSD (M18 ME.4)")
    parser.add_argument(
        "--amount",
        type=float,
        required=True,
        help="Amount USDC.e à wrap (e.g. 100.0)",
    )
    parser.add_argument(
        "--force-dry-run",
        action="store_true",
        help="Bypass dry-run preflight check (NOT RECOMMENDED)",
    )
    args = parser.parse_args()

    settings = Settings()  # type: ignore[call-arg]

    if settings.execution_mode == "dry_run" and not args.force_dry_run:
        log.warning(
            "wrap_usdc_to_pusd_aborted_dry_run",
            reason="EXECUTION_MODE=dry_run, wrap inutile en dry_run",
        )
        return 1

    if settings.polymarket_private_key is None or settings.polymarket_funder is None:
        log.error(
            "wrap_usdc_to_pusd_missing_creds",
            reason="POLYMARKET_PRIVATE_KEY ou POLYMARKET_FUNDER absent",
        )
        return 6

    rpc_url = os.environ.get("POLYGON_RPC_URL")
    if rpc_url is None:
        log.error(
            "wrap_usdc_to_pusd_missing_rpc",
            reason="POLYGON_RPC_URL env var requise",
        )
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

    amount_wei = int(args.amount * (10**USDC_DECIMALS))

    usdc = w3.eth.contract(address=usdc_addr, abi=USDC_ABI)
    onramp = w3.eth.contract(address=onramp_addr, abi=ONRAMP_ABI)
    pusd = w3.eth.contract(address=PUSD_ADDRESS, abi=PUSD_ABI)

    log.info(
        "wrap_usdc_to_pusd_starting",
        amount=args.amount,
        funder=funder,
        onramp=onramp_addr,
    )

    # Step 1 : approve USDC.e → onramp.
    nonce = w3.eth.get_transaction_count(account.address)
    approve_tx = usdc.functions.approve(onramp_addr, amount_wei).build_transaction({
        "from": account.address,
        "nonce": nonce,
        "gas": 100_000,
        "gasPrice": w3.eth.gas_price,
    })
    signed = account.sign_transaction(approve_tx)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    log.info("wrap_usdc_to_pusd_approve_sent", tx=tx_hash.hex())
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status != 1:
        log.error("wrap_usdc_to_pusd_approve_failed", tx=tx_hash.hex())
        return 4

    # Step 2 : call onramp.wrap(USDC.e, funder, amount).
    nonce += 1
    wrap_tx = onramp.functions.wrap(usdc_addr, funder, amount_wei).build_transaction({
        "from": account.address,
        "nonce": nonce,
        "gas": 200_000,
        "gasPrice": w3.eth.gas_price,
    })
    signed = account.sign_transaction(wrap_tx)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    log.info("wrap_usdc_to_pusd_wrap_sent", tx=tx_hash.hex())
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status != 1:
        log.error("wrap_usdc_to_pusd_wrap_failed", tx=tx_hash.hex())
        return 5

    # Step 3 : verify balance.
    pusd_balance = pusd.functions.balanceOf(funder).call()
    log.info(
        "wrap_usdc_to_pusd_completed",
        pusd_balance=pusd_balance / (10**USDC_DECIMALS),
        gas_total=receipt.gasUsed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
