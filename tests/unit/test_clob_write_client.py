"""Tests du `ClobWriteClient` (4 garde-fous + post_order avec mock SDK)."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from polycopy.config import Settings
from polycopy.executor import clob_write_client as cwc_module
from polycopy.executor.clob_write_client import ClobWriteClient
from polycopy.executor.dtos import BuiltOrder


def _dry_settings() -> Settings:
    return Settings(_env_file=None, dry_run=True)  # type: ignore[call-arg]


def _real_settings(*, key: str | None = "0x" + "1" * 64, funder: str | None = "0xF") -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        dry_run=False,
        polymarket_private_key=key,
        polymarket_funder=funder,
    )


@pytest.fixture
def mock_clob_class(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patche `ClobClient` dans le module `clob_write_client`."""
    mock_class = MagicMock()
    instance = mock_class.return_value
    instance.create_or_derive_api_creds.return_value = MagicMock(
        api_key="uuid",
        api_secret="base64",
        api_passphrase="phrase",
    )
    monkeypatch.setattr(cwc_module, "ClobClient", mock_class)
    return mock_class


# --- Garde-fou §2.1 + §2.2 (constructor) ------------------------------------


def test_garde_fou_constructor_in_dry_run_raises() -> None:
    with pytest.raises(RuntimeError, match="dry-run mode"):
        ClobWriteClient(_dry_settings())


def test_garde_fou_constructor_without_private_key_raises() -> None:
    with pytest.raises(RuntimeError, match="POLYMARKET_PRIVATE_KEY"):
        ClobWriteClient(_real_settings(key=None))


def test_garde_fou_constructor_without_funder_raises() -> None:
    with pytest.raises(RuntimeError, match="POLYMARKET_PRIVATE_KEY"):
        ClobWriteClient(_real_settings(funder=None))


def test_constructor_real_mode_derives_creds(mock_clob_class: MagicMock) -> None:
    client = ClobWriteClient(_real_settings())
    assert client is not None
    # ClobClient appelé 2 fois : 1 pour L1 (temp), 1 pour L2 (real).
    assert mock_clob_class.call_count == 2
    # 2e call (L2) reçoit creds + signature_type + funder.
    second_call = mock_clob_class.call_args_list[1]
    assert second_call.kwargs["funder"] == "0xF"
    assert second_call.kwargs["signature_type"] == 1  # default config


# --- post_order -------------------------------------------------------------


async def test_post_order_returns_parsed_order_result(
    mock_clob_class: MagicMock,
    sample_clob_order_response: dict[str, Any],
) -> None:
    instance = mock_clob_class.return_value
    instance.create_and_post_order.return_value = sample_clob_order_response

    client = ClobWriteClient(_real_settings())
    built = BuiltOrder(
        token_id="123",
        side="BUY",
        size=10.0,
        price=0.5,
        tick_size=0.01,
        neg_risk=False,
        order_type="FOK",
    )
    result = await client.post_order(built)
    assert result.success is True
    assert result.clob_order_id == sample_clob_order_response["orderID"]
    assert result.status == "matched"
    assert result.making_amount == "100000000"
    assert result.taking_amount == "200000000"


async def test_post_order_passes_neg_risk_in_options(
    mock_clob_class: MagicMock,
    sample_clob_order_response: dict[str, Any],
) -> None:
    instance = mock_clob_class.return_value
    instance.create_and_post_order.return_value = sample_clob_order_response

    client = ClobWriteClient(_real_settings())
    built = BuiltOrder(
        token_id="123",
        side="BUY",
        size=10.0,
        price=0.5,
        tick_size=0.01,
        neg_risk=True,
        order_type="FOK",
    )
    await client.post_order(built)
    call = instance.create_and_post_order.call_args
    options = call.args[1]
    assert options["neg_risk"] is True
    assert options["tick_size"] == "0.01"
