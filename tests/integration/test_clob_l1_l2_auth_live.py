"""Test d'intégration : dérivation L1→L2 avec une clé privée jetable.

Génère une clé fresh via `eth_account.Account.create()`, ZÉRO fonds, ZÉRO POST.
Vérifie uniquement que `create_or_derive_api_key()` retourne une triplet valide.

M18 : import path V1 → V2 + method rename `create_or_derive_api_creds` →
`create_or_derive_api_key`.

Run via `pytest -m integration`.
"""

import pytest
from eth_account import Account
from py_clob_client_v2 import ClobClient


@pytest.mark.integration
def test_l1_l2_auth_derivation_with_throwaway_key() -> None:
    private_key = Account.create().key.hex()
    client = ClobClient("https://clob.polymarket.com", chain_id=137, key=private_key)
    creds = client.create_or_derive_api_key()
    assert creds.api_key
    assert creds.api_secret
    assert creds.api_passphrase
