"""DhanHQ v2 read-only account inspection for the loopback operator GUI.

No trading operations, account funding, token exchange, renewal, TOTP or PIN
collection. The access token resides in this Python process's memory only.
The token provided by the broker itself may have BROADER privileges; do not
interpret this wrapper as a broker-enforced read-only scope.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from threading import RLock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json
import re

DHAN_BASE = 'https://api.dhan.co/v2'
_ALLOWED_PATHS = frozenset({'/profile', '/fundlimit', '/holdings', '/positions', '/orders'})
_TOKEN_RE = re.compile(r'^[A-Za-z0-9._~+\-/=]{16,4096}$')
_CLIENT_RE = re.compile(r'^[0-9]{5,24}$')
_MAX_RESPONSE = 1_000_000


class DhanConnectionError(RuntimeError):
    """Message is safe to show to an operator; excludes secrets and raw URLs."""


def _number(value):
    try:
        d = Decimal(str(value))
        return str(d) if d.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _string(value, limit=90):
    if not isinstance(value, (str, int)):
        return ''
    return str(value)[:limit]


class DhanReadOnlyClient:
    """Fixed-origin GET-only DhanHQ client; opener is injectable for offline tests."""

    def __init__(self, *, opener=None, timeout=8):
        self.opener = opener or urlopen
        if not 1 <= timeout <= 15:
            raise ValueError('Invalid remote timeout')
        self.timeout = timeout

    def fetch(self, path, token):
        if path not in _ALLOWED_PATHS:
            raise DhanConnectionError('Unsupported broker operation; read-only GET endpoints only')
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            raise DhanConnectionError('Invalid Dhan access token format')
        request = Request(DHAN_BASE + path,
                          headers={'access-token': token, 'Accept': 'application/json'},
                          method='GET')
        try:
            with self.opener(request, timeout=self.timeout) as response:
                buf = response.read(_MAX_RESPONSE + 1)
        except HTTPError as exc:
            code = exc.code
            # NEVER include remote JSON error text or Authorization data.
            if code in (401, 403):
                raise DhanConnectionError('Dhan authorization failed or expired; regenerate your token on Dhan Web') from None
            if code == 429:
                raise DhanConnectionError('Dhan request throttled; wait before retrying') from None
            raise DhanConnectionError(f'Dhan endpoint returned HTTP {code}') from None
        except (OSError, URLError, TimeoutError) as exc:
            raise DhanConnectionError('Dhan network request unavailable; check connection or retry later') from None
        if len(buf) > _MAX_RESPONSE:
            raise DhanConnectionError('Broker response exceeded safe size')
        try:
            data = json.loads(buf)
        except (ValueError, UnicodeError):
            raise DhanConnectionError('Broker response is not valid JSON') from None
        if not isinstance(data, (dict, list)):
            raise DhanConnectionError('Unexpected broker response type')
        if isinstance(data, dict) and ('errorCode' in data or data.get('status') == 'failure'):
            raise DhanConnectionError('Dhan rejected the read-only request')
        return data


def _brief_positions(records, *, category):
    if not isinstance(records, list):
        raise DhanConnectionError('Broker returned an invalid portfolio list')
    output=[]
    for row in records[:50]:
        if not isinstance(row, dict):
            continue
        if category == 'holdings':
            output.append({'symbol': _string(row.get('tradingSymbol')),
                           'security_id': _string(row.get('securityId')),
                           'quantity': _number(row.get('totalQty')),
                           'available_quantity': _number(row.get('availableQty')),
                           'avg_cost': _number(row.get('avgCostPrice'))})
        elif category == 'positions':
            output.append({'symbol': _string(row.get('tradingSymbol')),
                           'security_id': _string(row.get('securityId')),
                           'net_quantity': _number(row.get('netQty')),
                           'product': _string(row.get('productType'))})
        else:
            output.append({'symbol': _string(row.get('tradingSymbol')),
                           'side': _string(row.get('transactionType')),
                           'status': _string(row.get('orderStatus')),
                           'quantity': _number(row.get('quantity'))})
    return {'total': len(records), 'rows': output, 'truncated': len(records) > 50}


class DhanSession:
    """In-memory verified identity. Never writes tokens or balances to SQLite."""

    def __init__(self, client=None):
        self.client = client or DhanReadOnlyClient()
        self._lock = RLock()
        self._token = None
        self._client_id = None
        self._profile = None
        self._verified_at = None

    def status(self):
        with self._lock:
            return {
                'provider': 'dhan', 'connected': self._token is not None,
                'client_id_masked': ('••••' + self._client_id[-4:]) if self._client_id else None,
                'active_segments': _string(self._profile.get('activeSegment', ''), 120) if self._profile else None,
                'token_valid_until': _string(self._profile.get('tokenValidity', '')) if self._profile else None,
                'verified_at': self._verified_at,
                'api_mode': 'BROKER_ACCOUNT_READ_ONLY_CALLS',
                'live_orders_enabled': False,
                'token_retention': 'MEMORY_ONLY_UNTIL_APP_EXIT_OR_DISCONNECT',
                'warning': 'Dhan access tokens can have trading privileges at broker; Alpha_OTO only makes GET requests.'
            }

    def connect(self, token, client_id):
        if not isinstance(client_id, str) or not _CLIENT_RE.fullmatch(client_id):
            raise DhanConnectionError('Invalid Dhan client ID format')
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            raise DhanConnectionError('Invalid token; obtain a fresh access token from web.dhan.co')
        # Verify with the broker BEFORE saving credentials in process memory.
        profile = self.client.fetch('/profile', token)
        if not isinstance(profile, dict) or str(profile.get('dhanClientId', '')) != client_id:
            raise DhanConnectionError('Verified broker client ID does not match the entered ID')
        with self._lock:
            self._token = token
            self._client_id = client_id
            self._profile = {k: profile.get(k) for k in ('activeSegment', 'tokenValidity')}
            self._verified_at = datetime.now(timezone.utc).isoformat()
        return self.status()

    def disconnect(self):
        with self._lock:
            self._token = self._client_id = self._profile = self._verified_at = None
        return self.status()

    def snapshot(self):
        with self._lock:
            token = self._token
            if token is None:
                raise DhanConnectionError('Dhan is not connected; authenticate first')
            try:
                # All remote calls are GET; no order, deposit or withdrawal routes.
                funds = self.client.fetch('/fundlimit', token)
                holdings = self.client.fetch('/holdings', token)
                positions = self.client.fetch('/positions', token)
                orders = self.client.fetch('/orders', token)
            except DhanConnectionError as exc:
                if 'authorization failed or expired' in str(exc):
                    self.disconnect()
                raise
            if not isinstance(funds, dict):
                raise DhanConnectionError('Invalid broker funds response')
            return {
                'as_of_utc': datetime.now(timezone.utc).isoformat(),
                'mode': 'READ_ONLY_ACCOUNT_SNAPSHOT', 'currency': 'INR',
                'live_orders_enabled': False,
                # Dhan documents the field name with this misspelling.
                'available_balance': _number(funds.get('availabelBalance', funds.get('availableBalance'))),
                'utilized': _number(funds.get('utilizedAmount')),
                'withdrawable': _number(funds.get('withdrawableBalance')),
                'holdings': _brief_positions(holdings, category='holdings'),
                'positions': _brief_positions(positions, category='positions'),
                'orders': _brief_positions(orders, category='orders'),
                'note': 'Real broker balances, read-only snapshot. No transactions performed by Alpha_OTO.'
            }
