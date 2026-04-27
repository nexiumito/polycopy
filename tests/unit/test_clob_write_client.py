"""Tests du `ClobWriteClient` (4 garde-fous + post_order avec mock SDK)."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from polycopy.config import Settings
from polycopy.executor import clob_write_client as cwc_module
from polycopy.executor.clob_write_client import ClobWriteClient
from polycopy.executor.dtos import BuiltOrder


def _dry_settings() -> Settings:
    return Settings(_env_file=None, execution_mode="dry_run")  # type: ignore[call-arg]


def _real_settings(*, key: str | None = "0x" + "1" * 64, funder: str | None = "0xF") -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        execution_mode="live",
        polymarket_private_key=key,
        polymarket_funder=funder,
    )


@pytest.fixture
def mock_clob_class(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patche `ClobClient` dans le module `clob_write_client`."""
    mock_class = MagicMock()
    instance = mock_class.return_value
    instance.create_or_derive_api_key.return_value = MagicMock(
        api_key="uuid",
        api_secret="base64",
        api_passphrase="phrase",
    )
    monkeypatch.setattr(cwc_module, "ClobClient", mock_class)
    return mock_class


# --- Garde-fou §2.1 + §2.2 (constructor) ------------------------------------


def test_garde_fou_constructor_in_dry_run_raises() -> None:
    with pytest.raises(RuntimeError, match="execution_mode='live'"):
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


# --- M18 ME.1 : SDK V1 → V2 swap --------------------------------------------


def test_clob_write_client_imports_from_v2() -> None:
    """Aucun symbole `py_clob_client.*` (V1) ne doit subsister dans le module."""
    import inspect

    source = inspect.getsource(cwc_module)
    # `py_clob_client_v2` autorisé. `py_clob_client` (V1) interdit.
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("from py_clob_client"):
            assert stripped.startswith("from py_clob_client_v2"), (
                f"V1 import detected in clob_write_client.py: {line!r}"
            )
        if stripped.startswith("import py_clob_client"):
            assert stripped.startswith("import py_clob_client_v2"), (
                f"V1 import detected in clob_write_client.py: {line!r}"
            )


def test_clob_write_client_calls_create_or_derive_api_key(
    mock_clob_class: MagicMock,
) -> None:
    """M18 D2 : `create_or_derive_api_creds` (V1) → `create_or_derive_api_key` (V2)."""
    ClobWriteClient(_real_settings())
    instance = mock_clob_class.return_value
    instance.create_or_derive_api_key.assert_called_once()
    # Pas de fallback sur l'ancien nom V1.
    instance.create_or_derive_api_creds.assert_not_called()


def test_clob_write_client_passes_use_server_time_to_sdk(
    mock_clob_class: MagicMock,
) -> None:
    """M18 ME.2 D8 : `use_server_time` propagé au constructor SDK V2 L2."""
    ClobWriteClient(_real_settings())
    # 2nd call to ClobClient is the L2 (real) client.
    second_call = mock_clob_class.call_args_list[1]
    assert second_call.kwargs["use_server_time"] is True


# --- M18 ME.5 : Builder code optionnel --------------------------------------


def _real_settings_with_builder(*, builder_code: str | None, builder_address: str | None = None) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        execution_mode="live",
        polymarket_private_key="0x" + "1" * 64,
        polymarket_funder="0xF000000000000000000000000000000000000000",
        polymarket_builder_code=builder_code,
        polymarket_builder_address=builder_address,
    )


def test_settings_polymarket_builder_code_pattern_validates_hex32() -> None:
    """M18 ME.5 : pattern strict ^0x + 64 hex (bytes32)."""
    from pydantic import ValidationError

    valid_code = "0x" + "a" * 64
    settings = _real_settings_with_builder(builder_code=valid_code)
    assert settings.polymarket_builder_code == valid_code

    with pytest.raises(ValidationError):
        _real_settings_with_builder(builder_code="0xshort")
    with pytest.raises(ValidationError):
        _real_settings_with_builder(builder_code="not-hex" + "a" * 60)


def test_clob_write_client_passes_builder_config_when_set(
    mock_clob_class: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M18 ME.5 D9 : `BuilderConfig(builder_address, builder_code)` plombé au L2 SDK."""
    spy_builder_calls: list[Any] = []

    def _spy_builder_config(*args: Any, **kwargs: Any) -> Any:
        spy_builder_calls.append({"args": args, "kwargs": kwargs})
        return MagicMock(builder_code=kwargs.get("builder_code"))

    monkeypatch.setattr(cwc_module, "BuilderConfig", _spy_builder_config)

    code = "0x" + "b" * 64
    settings = _real_settings_with_builder(builder_code=code)
    ClobWriteClient(settings)

    assert spy_builder_calls, "BuilderConfig was not instantiated"
    last = spy_builder_calls[-1]
    assert last["kwargs"]["builder_code"] == code
    # Default à polymarket_funder quand builder_address non set.
    assert last["kwargs"]["builder_address"] == "0xF000000000000000000000000000000000000000"

    # 2nd call to ClobClient is the L2 (real) client — reçoit builder_config.
    second_call = mock_clob_class.call_args_list[1]
    assert second_call.kwargs["builder_config"] is not None


def test_clob_write_client_passes_no_builder_config_when_unset(
    mock_clob_class: MagicMock,
) -> None:
    """M18 ME.5 D9 : `polymarket_builder_code=None` → builder_config=None (default)."""
    ClobWriteClient(_real_settings())  # default = no builder_code
    second_call = mock_clob_class.call_args_list[1]
    assert second_call.kwargs["builder_config"] is None


def test_clob_write_client_uses_polymarket_clob_host_setting(
    mock_clob_class: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M18 ME.2 D7 : `host` propagé depuis ``settings.polymarket_clob_host``."""
    monkeypatch.setenv("POLYMARKET_CLOB_HOST", "https://clob-v2.polymarket.com")
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        execution_mode="live",
        polymarket_private_key="0x" + "1" * 64,
        polymarket_funder="0xF",
    )
    ClobWriteClient(settings)
    first_call = mock_clob_class.call_args_list[0]
    assert first_call.args[0] == "https://clob-v2.polymarket.com"


async def test_build_order_args_passes_string_side_directly(
    mock_clob_class: MagicMock,
    sample_clob_order_response: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M18 D4 : `built.side` (string) passe directement à OrderArgs(side=...).

    Plus de conversion via `BUY`/`SELL` constants V1.
    """
    captured_args: list[Any] = []

    def _spy_order_args(**kwargs: Any) -> Any:
        captured_args.append(kwargs)
        return MagicMock(**kwargs)

    monkeypatch.setattr(cwc_module, "OrderArgs", _spy_order_args)
    instance = mock_clob_class.return_value
    instance.create_and_post_order.return_value = sample_clob_order_response

    client = ClobWriteClient(_real_settings())
    built = BuiltOrder(
        token_id="123",
        side="SELL",
        size=10.0,
        price=0.5,
        tick_size=0.01,
        neg_risk=False,
        order_type="FOK",
    )
    await client.post_order(built)
    assert captured_args, "OrderArgs spy never called"
    last = captured_args[-1]
    assert last["side"] == "SELL", "side passed to OrderArgs MUST be the raw string"
    assert isinstance(last["side"], str)
