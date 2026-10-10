"""Coinbase Exchange public L2 -> verified local quotes. READ-ONLY; NO ORDERS.

A snapshot supplies absolute price levels, l2update changes replace sizes. This
is not an exchange-authenticated, guaranteed gap-free feed: TLS, Level2 ordering,
raw journals, sequence of local persisted quotes, and quarantine-on-disconnect
are defenses, not evidence of executable fills. Coinbase L2 does not provide a
per-product contiguous message sequence suitable for direct DurableQuoteFeed
sequence IDs. Our feed sequence is explicitly LOCAL derived order.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any
import json
import os

from .contracts import D, Quote, IntegrityFailure, utc
from .market_feed import DurableQuoteFeed
from .store import canonical

MAX_LEVELS = 150_000
MAX_RECORD_BYTES = 8_000_000
ZERO = Decimal(0)


class BookUntrusted(IntegrityFailure):
    """The book is unusable until a new verified L2 snapshot is acquired."""


def _price(value: Any) -> Decimal:
    v = D(value)
    if v <= 0:
        raise BookUntrusted('Nonpositive price level')
    return v


def _size(value: Any) -> Decimal:
    v = D(value)
    if v < 0:
        raise BookUntrusted('Negative order-book level size')
    return v


def _parse_exchange_time(value: Any, received: datetime) -> datetime:
    if not isinstance(value, str):
        raise BookUntrusted('Exchange timestamp missing from L2 update')
    try:
        stamp = utc(datetime.fromisoformat(value.replace('Z', '+00:00')))
    except (ValueError, TypeError) as exc:
        raise BookUntrusted('Invalid L2 exchange timestamp') from exc
    if stamp > received + timedelta(seconds=2):
        raise BookUntrusted('Exchange timestamp more than two seconds in future')
    if received - stamp > timedelta(seconds=30):
        raise BookUntrusted('Stale L2 change; resnapshot required')
    # Quote contract requires receive >= source; a slight apparent positive
    # offset is unsafe for this implementation and explicitly rejected.
    if stamp > received:
        raise BookUntrusted('Exchange timestamp ahead of local wall clock')
    return stamp


@dataclass
class L2Book:
    product: str
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    synchronized: bool = False
    last_exchange_time: datetime | None = None
    last_receive_time: datetime | None = None
    generation: int = 0
    messages: int = 0
    halted: bool = False

    def _replace_levels(self, raw: Any, side: str) -> dict[Decimal, Decimal]:
        if not isinstance(raw, list) or len(raw) > MAX_LEVELS:
            raise BookUntrusted(f'Invalid {side} book snapshot size')
        dest: dict[Decimal, Decimal] = {}
        for entry in raw:
            if not isinstance(entry, list) or len(entry) != 2:
                raise BookUntrusted('Invalid snapshot level tuple')
            px, qty = _price(entry[0]), _size(entry[1])
            if px in dest:
                raise BookUntrusted('Duplicate snapshot price level')
            if qty:
                dest[px] = qty
        return dest

    def _check_crossed(self) -> None:
        if not self.bids or not self.asks:
            raise BookUntrusted('Empty bid or ask book')
        if max(self.bids) >= min(self.asks):
            raise BookUntrusted('Locked or crossed book; no executable quote')
        if len(self.bids) > MAX_LEVELS or len(self.asks) > MAX_LEVELS:
            raise BookUntrusted('Book level limit exceeded')

    def apply(self, message: dict, received_at: datetime) -> Quote | None:
        """Returns a quote only for a timestamped update AFTER a full snapshot.

        A Coinbase L2 snapshot normally lacks exchange timestamp. Its levels
        are stored locally, but never published as a source-timestamped Quote.
        """
        recv = utc(received_at)
        if self.halted:
            raise BookUntrusted('Book quarantined; create fresh connection/book')
        if not isinstance(message, dict) or message.get('product_id') != self.product:
            raise BookUntrusted('Product mismatch or invalid market-data envelope')
        if self.last_receive_time and recv < self.last_receive_time:
            self.halted = True
            raise BookUntrusted('Local receipt clock rollback')
        typ = message.get('type')
        try:
            if typ == 'snapshot':
                if self.synchronized:
                    raise BookUntrusted('Unexpected extra snapshot; new connection required')
                bids = self._replace_levels(message.get('bids'), 'bid')
                asks = self._replace_levels(message.get('asks'), 'ask')
                old_bid, old_ask = self.bids, self.asks
                self.bids, self.asks = bids, asks
                try:
                    self._check_crossed()
                except BookUntrusted:
                    self.bids, self.asks = old_bid, old_ask
                    raise
                self.synchronized = True
                self.generation += 1
                self.messages += 1
                self.last_receive_time = recv
                return None
            if typ != 'l2update':
                raise BookUntrusted('Only snapshot and l2update accepted by book')
            if not self.synchronized:
                raise BookUntrusted('Update before full snapshot')
            exchange_time = _parse_exchange_time(message.get('time'), recv)
            if self.last_exchange_time and exchange_time < self.last_exchange_time:
                raise BookUntrusted('Out-of-order L2 exchange timestamps')
            changes = message.get('changes')
            if not isinstance(changes, list) or not 0 < len(changes) <= MAX_LEVELS:
                raise BookUntrusted('Invalid or empty L2 update changes')
            # Work on copies, so partial mutation is impossible after a bad row.
            bids, asks = self.bids.copy(), self.asks.copy()
            for change in changes:
                if not isinstance(change, list) or len(change) != 3 or change[0] not in ('buy', 'sell'):
                    raise BookUntrusted('Invalid L2 absolute update')
                side, price, size = change
                p, s = _price(price), _size(size)
                levels = bids if side == 'buy' else asks
                if s:
                    levels[p] = s
                else:
                    levels.pop(p, None)
            old_bid, old_ask = self.bids, self.asks
            self.bids, self.asks = bids, asks
            try:
                self._check_crossed()
            except BookUntrusted:
                self.bids, self.asks = old_bid, old_ask
                raise
            self.messages += 1
            self.last_receive_time = recv
            self.last_exchange_time = exchange_time
            best_bid, best_ask = max(bids), min(asks)
            return Quote(self.product, best_bid, best_ask, bids[best_bid], asks[best_ask],
                         exchange_time, recv)
        except (BookUntrusted, ValueError, TypeError, KeyError) as exc:
            self.halted = True
            if isinstance(exc, BookUntrusted):
                raise
            raise BookUntrusted(f'Invalid book update: {type(exc).__name__}') from exc


class RawL2Journal:
    """Local append-only hash chain; detects accidental edits, not malicious root.

    Every message has a capture timestamp. No raw API keys or credentials are
    accepted. A writer uses O_APPEND, fsync per entry and single-writer lock.
    """
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last_hash = 'GENESIS'
        self._count = 0
        self._known_bytes = 0
        self.verify()

    def verify(self) -> dict:
        previous, n = 'GENESIS', 0
        if not self.path.exists():
            if self._count:
                raise IntegrityFailure('Open raw journal was deleted externally')
            return {'entries': 0, 'tail_hash': previous, 'verified': True}
        with self.path.open('rb') as src:
            for raw in src:
                if len(raw) > MAX_RECORD_BYTES or not raw.endswith(b'\n'):
                    raise IntegrityFailure('Corrupt or oversized raw market event')
                try:
                    record = json.loads(raw)
                    if set(record) != {'index', 'received_at', 'message', 'prev_hash', 'sha256'}:
                        raise ValueError('Unexpected market journal fields')
                    if record['index'] != n + 1 or record['prev_hash'] != previous:
                        raise ValueError('Raw event sequence mismatch')
                    utc(datetime.fromisoformat(record['received_at']))
                    payload = {k: record[k] for k in ('index', 'received_at', 'message', 'prev_hash')}
                    digest = sha256(canonical(payload).encode()).hexdigest()
                    if record['sha256'] != digest:
                        raise ValueError('Market event digest mismatch')
                except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                    raise IntegrityFailure('Raw market journal modified/corrupted') from exc
                previous = digest
                n += 1
        if n < self._count:
            raise IntegrityFailure('Open raw journal truncated externally')
        self._last_hash, self._count = previous, n
        self._known_bytes = self.path.stat().st_size
        return {'entries': n, 'tail_hash': previous, 'verified': True}

    def append(self, received_at: datetime, message: dict) -> int:
        recv = utc(received_at)
        if not isinstance(message, dict):
            raise ValueError('Market event must be JSON object')
        # Refuse concurrent writers; never silently repair an existing journal.
        import fcntl
        fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if os.fstat(fd).st_size != self._known_bytes:
                raise IntegrityFailure('Journal size changed outside this writer')
            # Full hash-chain verification happens on open and every 512 records.
            # Reading the entire journal on EACH event would have quadratic
            # runtime and render even modest L2 feeds unusable.
            if self._count and self._count % 512 == 0:
                self.verify()
            payload = {'index': self._count + 1, 'received_at': recv.isoformat(),
                       'message': message, 'prev_hash': self._last_hash}
            digest = sha256(canonical(payload).encode()).hexdigest()
            record = canonical({**payload, 'sha256': digest}) + '\n'
            encoded = record.encode()
            if len(encoded) > MAX_RECORD_BYTES:
                raise ValueError('Raw market message exceeds size cap')
            sent = 0
            while sent < len(encoded):
                step = os.write(fd, encoded[sent:])
                if step <= 0:
                    raise OSError('Raw event journal write incomplete')
                sent += step
            os.fsync(fd)
            self._known_bytes += len(encoded)
            self._count += 1
            self._last_hash = digest
            return self._count
        finally:
            try:fcntl.flock(fd, fcntl.LOCK_UN)
            finally:os.close(fd)

    def entries(self):
        self.verify()
        with self.path.open(encoding='utf-8') as src:
            for line in src:
                if line.strip():
                    yield json.loads(line)


class L2QuoteBridge:
    """Trusts only source envelopes from fixed products; publishes safe L1 quotes.

    An invalid update or disconnection permanently quarantines this feed
    session. A *new* risk configuration and a *fresh* isolated feed are required
    for subsequent operation. No automatic unlock of a compromised book.
    """
    def __init__(self, feed: DurableQuoteFeed, products: tuple[str, ...], *,
                 journal: RawL2Journal | None = None, min_publish_ms: int = 0):
        if not products or len(set(products)) != len(products) or min_publish_ms < 0:
            raise ValueError('Invalid product whitelist or publish schedule')
        self.feed = feed
        self.products = tuple(products)
        self.books = {s: L2Book(s) for s in products}
        self.seq = {}
        self.last_published = {}
        self.journal = journal
        self.min_publish_ms = min_publish_ms
        self.suspended = False

    def _quarantine(self, reason: str):
        self.suspended = True
        for symbol in self.products:
            self.feed.quarantine(symbol, reason)

    def ingest(self, message: dict, *, received_at: datetime) -> dict:
        if self.suspended:
            raise BookUntrusted('Market feed session suspended')
        if not isinstance(message, dict):
            self._quarantine('INVALID_MARKET_MESSAGE')
            raise BookUntrusted('Raw market payload not an object')
        kind = message.get('type')
        if kind == 'error':
            self._quarantine('EXCHANGE_SUBSCRIPTION_ERROR')
            raise BookUntrusted('Exchange reported public market-data error')
        if kind in ('subscriptions', 'heartbeat'):
            # Heartbeats do NOT refresh executable prices.
            return {'status': 'CONTROL_ONLY', 'type': kind}
        if kind not in ('snapshot', 'l2update'):
            # Unknown/extension messages are ignored, not treated as prices.
            return {'status': 'IGNORED_MESSAGE'}
        symbol = message.get('product_id')
        if symbol not in self.books:
            self._quarantine('UNKNOWN_PRODUCT')
            raise BookUntrusted('Unapproved product market data')
        try:
            # A raw audit write failure MUST suspend order eligibility. Never
            # leave a fresh L1 quote active after evidence cannot be recorded.
            if self.journal is not None:
                self.journal.append(received_at, message)
            quote = self.books[symbol].apply(message, received_at)
            if quote is None:
                return {'status': 'BOOK_SNAPSHOT','symbol':symbol}
            recv = utc(received_at)
            previous = self.last_published.get(symbol)
            if previous and (recv - previous).total_seconds()*1000 < self.min_publish_ms:
                return {'status':'BOOK_UPDATED_THROTTLED','symbol':symbol}
            if symbol not in self.seq:
                current = self.feed.sequence_for(symbol)
                self.seq[symbol] = current
            n = self.seq[symbol] + 1
            saved = self.feed.publish(n, quote)
            self.seq[symbol] = n
            self.last_published[symbol] = recv
            return {'status':'QUOTE_PUBLISHED','symbol':symbol,'sequence':n,'source_time':quote.source_time.isoformat()}
        except (BookUntrusted, IntegrityFailure, ValueError, TypeError):
            self._quarantine('BOOK_OR_FEED_INTEGRITY_FAILURE')
            raise

    def disconnect(self, reason: str = 'WEBSOCKET_DISCONNECT'):
        self._quarantine(reason)

    def status(self):
        return {'mode':'READ_ONLY_L2_TO_L1','suspended':self.suspended,
                'books':{name:{'synchronized':book.synchronized,'messages':book.messages,
                               'last_exchange_time':book.last_exchange_time.isoformat() if book.last_exchange_time else None,
                               'halted':book.halted} for name,book in self.books.items()},
                'feed':self.feed.status(),'live_trading_enabled':False}
