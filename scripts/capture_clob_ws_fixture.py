#!/usr/bin/env python3
"""Capture-live du channel WebSocket CLOB `market` Polymarket (outil M11).

Connecte-toi à ``wss://ws-subscriptions-clob.polymarket.com/ws/market``,
souscris à un ``token_id`` (passé en arg), enregistre les N premiers messages
reçus dans ``tests/fixtures/clob_ws_market_sample.jsonl`` et imprime les types
d'événements observés. Utile pour rafraîchir la fixture si Polymarket fait
évoluer le schéma (cf. spec M11 §8 étape 1).

Usage:
    python scripts/capture_clob_ws_fixture.py <TOKEN_ID> [--count 30] [--out PATH]

Hors du package ``polycopy`` : script de maintenance. Ne pas importer depuis
le code métier.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

import websockets

_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_DEFAULT_COUNT = 30
_DEFAULT_OUT = Path("tests/fixtures/clob_ws_market_sample.jsonl")


async def _capture(token_id: str, count: int, out_path: Path) -> None:
    sub = {
        "type": "market",
        "assets_ids": [token_id],
        "custom_feature_enabled": True,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    seen: Counter[str] = Counter()
    async with websockets.connect(_WS_URL, ping_interval=10, ping_timeout=20) as ws:
        await ws.send(json.dumps(sub))
        sys.stderr.write(f"subscribed token_id={token_id}; capturing {count} messages\n")
        with out_path.open("w", encoding="utf-8") as handle:
            i = 0
            while i < count:
                raw = await ws.recv()
                if not isinstance(raw, str):
                    raw = raw.decode("utf-8")
                if raw.strip() in {"PING", "PONG"}:
                    continue
                payload = json.loads(raw)
                messages = payload if isinstance(payload, list) else [payload]
                for msg in messages:
                    event_type = msg.get("event_type", "<missing>")
                    seen[event_type] += 1
                    handle.write(json.dumps(msg, separators=(",", ":")) + "\n")
                    i += 1
                    if i >= count:
                        break
    sys.stderr.write(f"wrote {i} messages to {out_path}\n")
    for event_type, n in seen.most_common():
        sys.stderr.write(f"  {event_type}: {n}\n")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture live WS CLOB market messages.")
    parser.add_argument("token_id", help="asset_id (ERC-1155 CTF token) à souscrire")
    parser.add_argument("--count", type=int, default=_DEFAULT_COUNT, help="nb messages à capturer")
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT, help="fichier JSONL cible")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    asyncio.run(_capture(args.token_id, args.count, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
