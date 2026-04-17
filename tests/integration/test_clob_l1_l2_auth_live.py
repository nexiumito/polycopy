"""Test d'intégration : dérivation L1→L2 avec une clé privée jetable.

Génère une clé fresh via `eth_account.Account.create()`, ZÉRO fonds, ZÉRO POST.
Vérifie uniquement que `create_or_derive_api_creds()` retourne une triplet valide.

Run via `pytest -m integration`.
"""

import pytest
from eth_account import Account
from py_clob_client.client import ClobClient


@pytest.mark.integration
def test_l1_l2_auth_derivation_with_throwaway_key() -> None:
    private_key = Account.create().key.hex()
    client = ClobClient("https://clob.polymarket.com", key=private_key, chain_id=137)
    creds = client.create_or_derive_api_creds()
    assert creds.api_key
    assert creds.api_secret
    assert creds.api_passphrase
