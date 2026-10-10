"""Coinbase Advanced Trade PUBLIC Level2 message adapter (no account keys).

Fail closed on any missing/repeated connection-wide sequence, malformed event,
corrupt raw-journal write or unapproved product. NEVER promotes live trading.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .l2_gateway import BookUntrusted, L2QuoteBridge, RawL2Journal
from .contracts import utc

ADVANCED_PUBLIC = "wss://advanced-trade-ws.coinbase.com"


def advanced_subscription(products: tuple[str, ...], channel: str = "level2") -> dict:
    if channel not in ("level2", "heartbeats") or not products or any(
        not isinstance(p, str) or not p or len(p) > 32 for p in products
    ) or len(set(products)) != len(products):
        raise ValueError("Unsupported public Advanced Trade subscription")
    return {"type": "subscribe", "product_ids": list(products), "channel": channel}


def safe_provider_error(message: dict) -> str:
    details = []
    for key in ("message", "reason"):
        value = message.get(key)
        if isinstance(value, str):
            details.append(value[:220].replace("\n", " ").replace("\r", " "))
    return "Coinbase market-data subscription error: " + ("; ".join(details) or "unspecified")


class AdvancedL2Adapter:
    """Sequence-check ALL subscribed channel envelopes before mutating books.

    The sequence counter is per connection, not per product. Quarantine every
    product if a sequence is lost; do not silently start a fresh generation.
    Raw journal stores original Coinbase frames, not synthetic legacy updates.
    """
    def __init__(self, bridge: L2QuoteBridge, *, journal: RawL2Journal | None = None):
        self.bridge = bridge
        self.journal = journal
        self.last_seq: int | None = None
        self.frames = 0

    def ingest(self, message: dict, *, received_at: datetime) -> dict:
        if self.bridge.suspended:
            raise BookUntrusted("Advanced Trade feed already quarantined")
        try:
            recv = utc(received_at)
            if not isinstance(message, dict):
                raise BookUntrusted("Advanced Trade frame is not an object")
            if self.journal:
                self.journal.append(recv, message)
            if message.get("type") == "error" or message.get("channel") == "error":
                raise BookUntrusted(safe_provider_error(message))
            seq = message.get("sequence_num")
            if type(seq) is not int or seq < 0:
                raise BookUntrusted("Missing/invalid Advanced Trade sequence_num")
            if self.last_seq is not None and seq != self.last_seq + 1:
                raise BookUntrusted(f"Advanced Trade connection sequence discontinuity: expected {self.last_seq + 1}, got {seq}")
            self.last_seq = seq
            channel = message.get("channel")
            if channel in ("heartbeats", "subscriptions"):
                self.frames += 1
                return {"status": "CONTROL_ONLY", "sequence_num": seq}
            if channel != "l2_data":
                raise BookUntrusted("Unexpected Advanced Trade channel")
            events = message.get("events")
            if not isinstance(events, list) or not events:
                raise BookUntrusted("Advanced Trade L2 frame has no events")
            n = 0
            last_status = None
            for event in events:
                last_status = self._apply_event(event, recv)
                n += 1
            self.frames += 1
            return {"status": last_status or "L2_FRAME_PROCESSED", "sequence_num": seq,
                    "events": n}
        except Exception:
            self.bridge.disconnect("ADVANCED_L2_UNTRUSTED_OR_SEQUENCE_GAP")
            raise

    def _apply_event(self, event: Any, recv: datetime) -> str:
        if not isinstance(event, dict):
            raise BookUntrusted("Malformed Advanced Trade event")
        symbol = event.get("product_id")
        if symbol not in self.bridge.products:
            raise BookUntrusted("Unapproved product in Advanced Trade stream")
        typ = event.get("type")
        updates = event.get("updates")
        if not isinstance(updates, list) or not updates or len(updates) > 150_000:
            raise BookUntrusted("Empty/oversized Advanced Trade updates")
        if typ == "snapshot":
            bids, asks = [], []
            for row in updates:
                side, price, size, _ = self._level(row)
                (bids if side == "bid" else asks).append([price, size])
            result = self.bridge.ingest({"type": "snapshot", "product_id": symbol,
                                         "bids": bids, "asks": asks}, received_at=recv)
            return result["status"]
        if typ != "update":
            raise BookUntrusted("Unexpected Advanced Trade event type")
        changes = []
        stamps = []
        for row in updates:
            side, price, size, event_time = self._level(row)
            if not isinstance(event_time, str):
                raise BookUntrusted("Missing update event_time")
            try:
                stamp = utc(datetime.fromisoformat(event_time.replace("Z", "+00:00")))
            except (TypeError, ValueError) as exc:
                raise BookUntrusted("Malformed Advanced Trade event_time") from exc
            if stamp > recv or recv - stamp > timedelta(seconds=30):
                raise BookUntrusted("Stale/future Advanced Trade event_time")
            stamps.append(stamp)
            changes.append(["buy" if side == "bid" else "sell", price, size])
        result = self.bridge.ingest({"type": "l2update", "product_id": symbol,
                                     "time": max(stamps).isoformat(), "changes": changes},
                                    received_at=recv)
        return result["status"]

    @staticmethod
    def _level(row: Any) -> tuple[str, str, str, Any]:
        if not isinstance(row, dict) or row.get("side") not in ("bid", "offer"):
            raise BookUntrusted("Invalid Advanced Trade price-level side")
        price = row.get("price_level")
        size = row.get("new_quantity")
        if not isinstance(price, str) or not isinstance(size, str):
            raise BookUntrusted("Invalid Advanced Trade price/quantity fields")
        return row["side"], price, size, row.get("event_time")
