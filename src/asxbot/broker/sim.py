"""Simulated broker. State in data/broker_sim/state.json (atomic writes, no database).

Fill model: a limit BUY fills at min(limit, ref * (1 + slippage)) if that is <= limit,
otherwise it stays open (unfilled). A limit SELL fills at max(limit, ref * (1 - slippage)) if
that is >= limit. `ref` is the current quote (last price) from the quote provider, or the last
daily close when no quote is available. Brokerage: IBKR AU fixed pricing. Order IDs are
SIM-000001, ... and are the only proof an order exists.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from asxbot.backtest.costs import CostModel
from asxbot.broker.base import Broker, BrokerPosition, OrderResult
from asxbot.io import write_text_atomic
from asxbot.log import get_logger

log = get_logger("asxbot.broker.sim")


class SimBroker(Broker):
    mode = "sim"

    def __init__(
        self,
        data_dir: Path,
        starting_cash: float,
        costs: CostModel,
        price_lookup,  # callable(ticker) -> (ref_price, adv_dollar) or None
    ):
        self.dir = Path(data_dir) / "broker_sim"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "state.json"
        self.costs = costs
        self.price_lookup = price_lookup
        self.state = self._load(starting_cash)

    def _load(self, starting_cash: float) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        st = {"cash": float(starting_cash), "positions": {}, "orders": {}, "next_id": 1,
              "created": datetime.now().isoformat(timespec="seconds")}  # fmt: skip
        self._save(st)
        return st

    def _save(self, st: dict | None = None) -> None:
        write_text_atomic(json.dumps(st or self.state, indent=2), self.path)

    def reset(self, starting_cash: float) -> None:
        self.state = {"cash": float(starting_cash), "positions": {}, "orders": {}, "next_id": 1,
                      "created": datetime.now().isoformat(timespec="seconds")}  # fmt: skip
        self._save()

    # -- Broker API ----------------------------------------------------------
    def place_limit_order(
        self, ticker: str, side: str, qty: int, limit: float, tag: str
    ) -> OrderResult:
        oid = f"SIM-{self.state['next_id']:06d}"
        self.state["next_id"] += 1
        res = OrderResult(oid, "open", ticker, side, qty, limit, message=tag)
        ref = self.price_lookup(ticker)
        if ref is None:
            res.status = "rejected"
            res.message = f"{tag}; no reference price for {ticker}"
        else:
            ref_px, adv = ref
            value = qty * limit
            if side == "buy":
                fill = min(limit, self.costs.buy_price(ref_px, value, adv))
                if fill <= limit and ref_px <= limit:
                    cost = fill * qty
                    fee = self.costs.brokerage(cost)
                    if cost + fee > self.state["cash"]:
                        res.status, res.message = "rejected", f"{tag}; insufficient cash"
                    else:
                        self.state["cash"] -= cost + fee
                        pos = self.state["positions"].get(ticker, {"qty": 0, "avg_cost": 0.0})
                        tot = pos["qty"] + qty
                        pos["avg_cost"] = (pos["avg_cost"] * pos["qty"] + fill * qty) / tot
                        pos["qty"] = tot
                        self.state["positions"][ticker] = pos
                        res.status, res.filled_qty, res.avg_price, res.commission = (
                            "filled",
                            qty,
                            fill,
                            fee,
                        )
                else:
                    res.message = f"{tag}; limit {limit} below market {ref_px:.3f}, resting"
            elif side == "sell":
                pos = self.state["positions"].get(ticker)
                if not pos or pos["qty"] < qty:
                    res.status, res.message = "rejected", f"{tag}; no/insufficient position to sell"
                else:
                    fill = max(limit, self.costs.sell_price(ref_px, value, adv))
                    if fill >= limit and ref_px >= limit:
                        proceeds = fill * qty
                        fee = self.costs.brokerage(proceeds)
                        self.state["cash"] += proceeds - fee
                        pos["qty"] -= qty
                        if pos["qty"] == 0:
                            del self.state["positions"][ticker]
                        res.status, res.filled_qty, res.avg_price, res.commission = (
                            "filled",
                            qty,
                            fill,
                            fee,
                        )
                    else:
                        res.message = f"{tag}; limit {limit} above market {ref_px:.3f}, resting"
            else:
                res.status, res.message = "rejected", f"{tag}; bad side {side}"
        self.state["orders"][oid] = res.to_dict()
        self._save()
        log.info(
            "sim order %s %s %s %d @ %.3f -> %s %s",
            oid,
            side,
            ticker,
            qty,
            limit,
            res.status,
            res.message,
        )
        return res

    def order_status(self, order_id: str) -> OrderResult | None:
        d = self.state["orders"].get(order_id)
        return OrderResult(**d) if d else None

    def positions(self) -> list[BrokerPosition]:
        return [
            BrokerPosition(t, int(p["qty"]), float(p["avg_cost"]))
            for t, p in self.state["positions"].items()
        ]

    def cash(self) -> float:
        return float(self.state["cash"])

    def open_orders(self) -> list[OrderResult]:
        return [OrderResult(**d) for d in self.state["orders"].values() if d["status"] == "open"]
