"""Typed production-boundary contracts. All monetary quantities use Decimal.

This package implements an OFFLINE simulated broker exclusively. No real broker
adapter, credential loader, or outbound order endpoint is present.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal


class RiskRejected(ValueError):
    """A proposed order violates a mandatory pre-trade rule."""


class IntegrityFailure(RuntimeError):
    """Durable records disagree with their immutable accounting invariants."""


class UnknownSubmission(RuntimeError):
    """Submission status cannot safely be inferred; never blindly retry."""


def D(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError('Boolean cannot be a financial quantity')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as ex:
        raise ValueError('Invalid decimal quantity') from ex
    if not result.is_finite():
        raise ValueError('Nonfinite financial quantity')
    return result


def utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('UTC-aware timestamp required')
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class Instrument:
    symbol: str
    venue: str
    currency: str
    tick: Decimal
    lot: Decimal
    max_units: Decimal
    authorized: bool = True

    def __post_init__(self):
        for key in ('tick','lot','max_units'):
            object.__setattr__(self,key,D(getattr(self,key)))
        if any(not x or not x.replace('_','').replace('-','').isalnum() for x in
               (self.symbol, self.venue, self.currency)):
            raise ValueError('Invalid instrument identity')
        if not self.currency.isupper():
            raise ValueError('Uppercase ISO-like quote currency required')
        if any(D(v) <= 0 for v in (self.tick,self.lot,self.max_units)):
            raise ValueError('Invalid instrument price tick, lot or maximum units')
        if self.max_units % self.lot:
            raise ValueError('Maximum units must be lot aligned')


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    source_time: datetime
    received_time: datetime

    def __post_init__(self):
        for key in ('bid','ask','bid_size','ask_size'):
            object.__setattr__(self,key,D(getattr(self,key)))
        utc(self.source_time); utc(self.received_time)
        if self.bid <= 0 or self.ask < self.bid or self.bid_size < 0 or self.ask_size < 0:
            raise ValueError('Invalid executable quote')
        if self.received_time < self.source_time:
            raise ValueError('Received timestamp before exchange timestamp')


@dataclass(frozen=True)
class OrderIntent:
    client_id: str
    symbol: str
    side: Literal['BUY','SELL']
    quantity: Decimal
    limit: Decimal
    created_at: datetime
    strategy: str
    model_version: str

    def __post_init__(self):
        object.__setattr__(self,'quantity',D(self.quantity))
        object.__setattr__(self,'limit',D(self.limit))
        utc(self.created_at)
        if not self.client_id or len(self.client_id) > 100 or not self.client_id.replace('-','').replace('_','').isalnum():
            raise ValueError('Invalid client id')
        if self.side not in ('BUY','SELL') or self.quantity<=0 or self.limit<=0:
            raise ValueError('Invalid order side, quantity or limit')
        if not self.strategy or not self.model_version or len(self.strategy)>120 or len(self.model_version)>120:
            raise ValueError('Missing auditable strategy identity')


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: Decimal = Decimal('1000')
    max_gross_notional: Decimal = Decimal('3000')
    max_symbol_notional: Decimal = Decimal('2000')
    cash_reserve: Decimal = Decimal('100')
    max_daily_realized_loss: Decimal = Decimal('200')
    max_peak_drawdown_fraction: Decimal = Decimal('0.08')
    max_spread_bps: Decimal = Decimal('35')
    max_quote_age_seconds: int = 5
    max_intent_age_seconds: int = 10
    max_receive_delay_seconds: int = 5
    max_open_orders: int = 5
    max_daily_orders: int = 30
    fee_reserve_bps: Decimal = Decimal('30')

    def __post_init__(self):
        for key in ('max_order_notional','max_gross_notional','max_symbol_notional',
                    'cash_reserve','max_daily_realized_loss','max_peak_drawdown_fraction',
                    'max_spread_bps','fee_reserve_bps'):
            object.__setattr__(self,key,D(getattr(self,key)))
        money=(self.max_order_notional,self.max_gross_notional,self.max_symbol_notional,
               self.cash_reserve,self.max_daily_realized_loss,self.max_spread_bps,self.fee_reserve_bps)
        if any(D(v)<0 for v in money) or self.max_order_notional<=0 or self.max_gross_notional<=0 or self.max_symbol_notional<=0:
            raise ValueError('Invalid nonnegative risk limits')
        if not Decimal(0)<self.max_peak_drawdown_fraction<Decimal(1):
            raise ValueError('Invalid drawdown limit')
        if min(self.max_quote_age_seconds,self.max_intent_age_seconds,self.max_receive_delay_seconds,self.max_open_orders,self.max_daily_orders)<1:
            raise ValueError('Invalid safety limits')
