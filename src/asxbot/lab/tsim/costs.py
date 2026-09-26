"""Honest costs on every simulated trade (CLOUD_BRIEF "HONEST TESTING").

Three separate charges, none of them standing in for another:

* COMMISSION, per broker (`tradesim.brokers` in config.yaml): pct of the order's filled value,
  with a minimum per order, GST included. Charged once per ORDER on its cumulative filled
  value (a partly filled order pays the minimum once, as IBKR bills an order filled in parts on
  one day). Every run is scored under every configured broker (report.py), because the fee
  schedule is additive per order and does not change what fills.
* SPREAD. 1-minute bars carry no bid/ask, so a marketable order pays half a modelled spread
  on the side it crosses, and never less than ONE TICK (the ASX price steps). The modelled
  spread comes from a price and liquidity tier (the stock's median daily dollar turnover
  before the day). Auction fills pay no spread (one price for everyone), resting limit orders
  pay none (they are the passive side; they fill only when the price trades THROUGH them).
* SLIPPAGE / IMPACT, growing with the size of the slice against the volume that traded in the
  bar it fills in: impact = coeff x slice/bar volume, capped.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


def tick_size(price: float) -> float:
    """ASX equity price steps (ASX Operating Rules, Procedure 3110): below 10c: 0.1c;
    10c to $2.00: 0.5c; $2.00 and above: 1c."""
    if price < 0.10:
        return 0.001
    if price < 2.00:
        return 0.005
    return 0.01


def round_to_tick(price: float, up: bool) -> float:
    t = tick_size(price)
    n = price / t
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * t, 6)


@dataclass(frozen=True)
class Fees:
    """One broker's ASX commission: max(minimum, pct of value), per order, GST included."""

    name: str
    pct: float  # percent of value
    minimum: float  # AUD per order
    source: str = ""

    def commission(self, value: float) -> float:
        if value <= 0:
            return 0.0
        return round(max(self.minimum, abs(value) * self.pct / 100.0), 4)


# Defaults when config.yaml has no `tradesim.brokers` block. IBKR is the arena's own figure
# (config.yaml costs:, frozen 22 Sep: 0.08% + GST, AUD 6 + GST). The second is the result of
# the broker research in docs/tsim.md (sources there).
DEFAULT_BROKERS = {
    "ibkr": Fees("ibkr", 0.088, 6.60, "config.yaml costs (IBKR Australia fixed, GST incl.)"),
}


@dataclass(frozen=True)
class SpreadTier:
    min_turnover: float  # median daily dollar turnover at or above this
    spread_pct: float  # the full quoted spread, percent of price


DEFAULT_TIERS = (
    SpreadTier(50e6, 0.05),
    SpreadTier(10e6, 0.10),
    SpreadTier(2e6, 0.25),
    SpreadTier(5e5, 0.50),
    SpreadTier(0.0, 1.00),
)


@dataclass(frozen=True)
class CostModel:
    fees: dict = field(default_factory=lambda: dict(DEFAULT_BROKERS))
    primary: str = "ibkr"  # the schedule the trader sees and the account is charged
    tiers: tuple = DEFAULT_TIERS
    impact_coeff: float = 0.5  # percent of price per 100% of the bar's volume
    impact_cap_pct: float = 2.0
    auction_impact_coeff: float = 0.25
    borrow_pct_annual: float = 3.0  # short borrow, charged nightly on the short's value

    @property
    def broker(self) -> Fees:
        return self.fees[self.primary]

    def spread_pct(self, turnover: float | None) -> float:
        t = 0.0 if turnover is None or turnover != turnover else float(turnover)
        for tier in self.tiers:
            if t >= tier.min_turnover:
                return tier.spread_pct
        return self.tiers[-1].spread_pct

    def half_spread(self, price: float, turnover: float | None) -> float:
        """Dollars per share paid crossing the spread once: at least one tick."""
        return max(tick_size(price), price * self.spread_pct(turnover) / 200.0)

    def impact(self, price: float, qty: float, bar_volume: float, auction: bool = False) -> float:
        """Dollars per share of market impact for a slice of `qty` in a bar of `bar_volume`."""
        share = qty / bar_volume if bar_volume and bar_volume > 0 else 1.0
        coeff = self.auction_impact_coeff if auction else self.impact_coeff
        return price * min(self.impact_cap_pct, coeff * share) / 100.0

    def aggressive_price(
        self, ref: float, buy: bool, qty: float, bar_volume: float, turnover, auction=False
    ) -> float:
        """What a marketable slice pays: the reference price, plus half the spread (not in an
        auction), plus impact, adverse to the side."""
        cost = self.impact(ref, qty, bar_volume, auction)
        if not auction:
            cost += self.half_spread(ref, turnover)
        return ref + cost if buy else max(0.0005, ref - cost)

    @classmethod
    def from_config(cls, cfg, primary: str | None = None) -> CostModel:
        conf = (cfg.get("tradesim") or {}) if cfg is not None else {}
        fees = dict(DEFAULT_BROKERS)
        for name, b in (conf.get("brokers") or {}).items():
            fees[name] = Fees(name, float(b["pct"]), float(b["min_aud"]), str(b.get("source", "")))
        sp = conf.get("spread") or {}
        tiers = tuple(
            SpreadTier(float(t["min_turnover_aud"]), float(t["spread_pct"]))
            for t in (sp.get("tiers") or [])
        ) or DEFAULT_TIERS
        imp = conf.get("impact") or {}
        return cls(
            fees=fees,
            primary=primary or str(conf.get("primary_broker", "ibkr")),
            tiers=tuple(sorted(tiers, key=lambda t: -t.min_turnover)),
            impact_coeff=float(imp.get("coeff_pct", 0.5)),
            impact_cap_pct=float(imp.get("cap_pct", 2.0)),
            auction_impact_coeff=float(imp.get("auction_coeff_pct", 0.25)),
            borrow_pct_annual=float(conf.get("borrow_pct_annual", 3.0)),
        )
