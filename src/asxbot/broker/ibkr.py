"""IBKR adapter via ib_async and IB Gateway. Phase 2 (paper) and Phase 3 (live).

Disabled until an account exists. Refuses to construct unless:
  - broker mode is `paper` or `live` (config.yaml), and
  - for `live`, LIVE_TRADING_CONFIRMED=yes is in .env (checked again here, independently
    of config loading), and
  - ib_async is installed:  uv pip install -e ".[ibkr]"
Orders are LMT, DAY, on SMART/ASX in AUD. The broker's order id and fill are the only truth.
"""

from __future__ import annotations

import os

from asxbot.broker.base import Broker, BrokerPosition, OrderResult
from asxbot.config import Config, ConfigError
from asxbot.log import get_logger

log = get_logger("asxbot.broker.ibkr")


class IBKRBroker(Broker):
    def __init__(self, cfg: Config):
        mode = cfg.broker
        if mode not in ("paper", "live"):
            raise ConfigError(f"IBKRBroker needs broker: paper or live, got {mode}")
        if mode == "live" and os.environ.get("LIVE_TRADING_CONFIRMED", "").lower() != "yes":
            raise ConfigError("live mode without LIVE_TRADING_CONFIRMED=yes; refusing")
        try:
            from ib_async import IB, LimitOrder, Stock  # type: ignore
        except ImportError as e:
            raise ConfigError('ib_async not installed: uv pip install -e ".[ibkr]"') from e
        self.mode = mode
        self._Stock, self._LimitOrder = Stock, LimitOrder
        self.ib = IB()
        host = os.environ.get("IB_HOST", "127.0.0.1")
        port = int(os.environ.get("IB_PORT", "4002" if mode == "paper" else "4001"))
        client_id = int(os.environ.get("IB_CLIENT_ID", "7"))
        self.ib.connect(host, port, clientId=client_id, timeout=20)
        acct = self.ib.managedAccounts()
        log.info("IBKR connected (%s) accounts=%s", mode, acct)
        # paper accounts start with "DU"; refuse a live account in paper mode and vice versa
        if mode == "paper" and not any(a.startswith("DU") for a in acct):
            raise ConfigError("paper mode but the connected IB account is not a paper account")
        if mode == "live" and any(a.startswith("DU") for a in acct):
            raise ConfigError("live mode but the connected IB account is a paper account")

    def _contract(self, ticker: str):
        c = self._Stock(ticker, "ASX", "AUD")
        self.ib.qualifyContracts(c)
        return c

    def place_limit_order(
        self, ticker: str, side: str, qty: int, limit: float, tag: str
    ) -> OrderResult:
        contract = self._contract(ticker)
        order = self._LimitOrder("BUY" if side == "buy" else "SELL", qty, limit, tif="DAY")
        order.orderRef = tag[:64]
        trade = self.ib.placeOrder(contract, order)
        self.ib.sleep(2)
        st = trade.orderStatus
        status = {"Filled": "filled", "Submitted": "open", "PreSubmitted": "open",
                  "Cancelled": "cancelled", "Inactive": "rejected"}  # fmt: skip
        status = status.get(st.status, "open")
        if 0 < st.filled < qty:
            status = "partial"
        commission = sum((f.commissionReport.commission or 0.0) for f in trade.fills)
        return OrderResult(
            str(trade.order.orderId), status, ticker, side, qty, limit,
            filled_qty=int(st.filled), avg_price=float(st.avgFillPrice) if st.filled else None,
            commission=float(commission), message=f"{tag}; ib status {st.status}",
        )  # fmt: skip

    def order_status(self, order_id: str) -> OrderResult | None:
        for t in self.ib.trades():
            if str(t.order.orderId) == order_id:
                st = t.orderStatus
                return OrderResult(
                    order_id, st.status.lower(), t.contract.symbol, t.order.action.lower(),
                    int(t.order.totalQuantity), float(t.order.lmtPrice), int(st.filled),
                    float(st.avgFillPrice) if st.filled else None,
                )  # fmt: skip
        return None

    def positions(self) -> list[BrokerPosition]:
        return [
            BrokerPosition(p.contract.symbol, int(p.position), float(p.avgCost))
            for p in self.ib.positions()
            if p.contract.exchange in ("ASX", "SMART", "") and p.position
        ]

    def cash(self) -> float:
        for v in self.ib.accountValues():
            if v.tag == "TotalCashValue" and v.currency == "AUD":
                return float(v.value)
        return float("nan")

    def open_orders(self) -> list[OrderResult]:
        out = []
        for t in self.ib.openTrades():
            out.append(
                OrderResult(
                    str(t.order.orderId),
                    "open",
                    t.contract.symbol,
                    t.order.action.lower(),
                    int(t.order.totalQuantity),
                    float(t.order.lmtPrice),
                )  # fmt: skip
            )
        return out
