"""Broker interface. sim now; paper/live via IBKR later behind the same calls.

The broker is the ONLY source of truth for order IDs, fills and positions. Nothing else in
the system may claim an order was placed or filled.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class OrderResult:
    order_id: str
    status: str  # filled | partial | open | rejected | cancelled
    ticker: str
    side: str
    qty: int
    limit: float
    filled_qty: int = 0
    avg_price: float | None = None
    commission: float = 0.0
    message: str = ""
    ts: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class BrokerPosition:
    ticker: str
    qty: int
    avg_cost: float


class Broker(ABC):
    mode: str = "base"

    @abstractmethod
    def place_limit_order(
        self, ticker: str, side: str, qty: int, limit: float, tag: str
    ) -> OrderResult: ...

    @abstractmethod
    def order_status(self, order_id: str) -> OrderResult | None: ...

    @abstractmethod
    def positions(self) -> list[BrokerPosition]: ...

    @abstractmethod
    def cash(self) -> float: ...

    def open_orders(self) -> list[OrderResult]:
        return []
