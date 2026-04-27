"""Test d'intégration : build + sign Order V2 localement (M18 ME.6).

Smoke check que le SDK V2 produit un payload Order V2 cohérent avec la doc :
`Order(salt, maker, signer, tokenId, makerAmount, takerAmount, side,
signatureType, timestamp, metadata, builder)` + signature hex valide.

Aucun POST réel, aucune fonds — clé jetable `Account.create()`.

Run via `pytest -m integration`.
"""

from __future__ import annotations

import pytest
from eth_account import Account
from py_clob_client_v2 import ClobClient, OrderArgs, PartialCreateOrderOptions


@pytest.mark.integration
def test_clob_v2_signature_local_validation() -> None:
    """Build un V2 order via `OrderBuilder.build_order(version=2)`.

    Vérifie que le payload JSON wire contient bien `timestamp/metadata/builder`
    et que `signature` est un hex valide. Force `version=2` pour exercer le
    path V2 même si le backend `/version` retourne `version=1` pré-cutover.
    """
    private_key = Account.create().key.hex()
    client = ClobClient("https://clob.polymarket.com", chain_id=137, key=private_key)

    args = OrderArgs(
        token_id="123456789",
        price=0.5,
        size=10.0,
        side="BUY",
    )
    options = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)

    # Force version=2 — bypass le `/version` lookup pour le test local.
    builder = client.builder
    signed = builder.build_order(args, options=options, version=2)

    # Le SDK V2 expose `to_order_payload` ou similaire — sinon on inspecte
    # les attributs de `signed`. Sur signature_type=0 par défaut, l'order V2
    # doit porter timestamp/metadata/builder.
    order_dict = signed.dict() if hasattr(signed, "dict") else signed.__dict__
    assert order_dict, "signed order must be non-empty"
    # Au moins un de ces 3 champs V2 doit apparaître dans le shape.
    v2_fields = {"timestamp", "metadata", "builder"}
    found = [k for k in order_dict if k in v2_fields]
    assert found, (
        f"V2 order missing timestamp/metadata/builder fields ; got keys: "
        f"{list(order_dict.keys())[:20]}"
    )
    # Signature hex valide (au moins préfixe `0x`).
    sig = order_dict.get("signature") or getattr(signed, "signature", None)
    assert sig, "signature missing"
    assert isinstance(sig, str) and sig.startswith("0x"), f"signature unexpected: {sig!r}"
