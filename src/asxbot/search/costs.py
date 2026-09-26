"""Honest costs for the rules-only search (CLOUD_BRIEF_SEARCH.md 1): brokerage from a named
broker's published schedule, the spread by price and liquidity tier (never less than one tick
each way), and market impact that grows with the order's size against the stock's volume.

Fixed 26 Sep 2026 BEFORE any strategy was run through it. Changing a number here after a
result has been seen is forbidden (CLAUDE.md "Verification"); a change is a new dated entry.

There are no quotes in the 1-minute TRADES history, so the spread is an assumption by tier,
not a measurement. The tiers were set wide on purpose (the repo's own slippage base, 0.10%,
stands in for the spread of a typical ASX 300 name; small, thin names get more).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def tick(price: float) -> float:
    """ASX price steps (ASX Operating Rules Procedure 4010): up to 10c: 0.1c; over 10c and
    under $2: 0.5c; $2 and over: 1c."""
    if price <= 0.10:
        return 0.001
    if price < 2.00:
        return 0.005
    return 0.01


def round_to_tick(price: float, up: bool) -> float:
    t = tick(price)
    n = price / t
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * t, 6)


@dataclass(frozen=True)
class Broker:
    """Brokerage per order (GST included for an Australian resident)."""

    name: str
    pct: float  # of the order's value, in percent
    minimum: float  # AUD per order
    source: str

    def fee(self, value: float) -> float:
        return max(self.minimum, abs(value) * self.pct / 100.0)


# IBKR Australia, fixed pricing for ASX shares: 0.08% of trade value, AUD 6.00 minimum, plus
# 10% GST for Australian residents -> 0.088% / AUD 6.60 (the repo's config.yaml `costs`).
IBKR = Broker(
    "IBKR fixed",
    0.088,
    6.60,
    "interactivebrokers.com.au commissions (ASX fixed: 0.08%, min AUD 6, + GST); "
    "config.yaml costs.brokerage_pct / brokerage_min_aud",
)

# The cheapest ASX broker with an order API a personal bot can use - see BROKERS below and
# reports/cloud_rules_search_20260926.md "Brokers" for the research and its sources.
CHEAPEST_API = Broker(
    "Tiger Brokers AU",
    0.0275,
    5.50,
    "itiger.com/au/commissions (via search summaries, 26 Sep 2026): A$5 flat per order up "
    "to A$20,000, then 0.025%; GST treatment not confirmed, so 10% GST is ADDED here "
    "(A$5.50 / 0.0275%) to stay on the dear side. Its Open API SDK lists Australian "
    "stocks; ASX order placement by API for a retail AU account is inferred, not proven. "
    "Saxo/Totality closed its API to AU accounts (Aug 2025); moomoo's API is US/HK only; "
    "Webull AU's API is unconfirmed for ASX.",
)

BROKERS = {"ibkr": IBKR, "cheapest_api": CHEAPEST_API}


# Half-spread paid on each side, by the stock's median daily dollar turnover (20 sessions
# before the day): (turnover at least, half-spread in percent). Never less than one tick.
SPREAD_TIERS: tuple[tuple[float, float], ...] = (
    (50e6, 0.03),
    (10e6, 0.06),
    (2e6, 0.15),
    (0.0, 0.35),
)
IMPACT_K = 0.5  # impact = K x daily volatility x sqrt(order value / daily turnover)
IMPACT_CAP = 0.02  # never more than 2% a side from impact alone
MAX_BAR_SHARE = 0.20  # fills capped at 20% of a bar's volume (the arena's rule)
AUCTION_SHARE = 0.20  # and at 20% of an auction's volume (Rick, 23 Sep)


def half_spread_frac(price: float, turnover: float | None) -> float:
    pct = SPREAD_TIERS[-1][1]
    if turnover is not None and turnover == turnover:
        for floor, p in SPREAD_TIERS:
            if turnover >= floor:
                pct = p
                break
    return max(pct / 100.0, tick(price) / price)


def impact_frac(value: float, turnover: float | None, vol_daily: float | None) -> float:
    if not turnover or turnover != turnover or turnover <= 0:
        return IMPACT_CAP
    sigma = vol_daily if vol_daily and vol_daily == vol_daily else 0.03
    return min(IMPACT_CAP, IMPACT_K * sigma * math.sqrt(abs(value) / turnover))


@dataclass(frozen=True)
class Costs:
    """Everything a fill pays beyond the printed price. `multiplier` scales spread and impact
    (the 2x stress run); brokerage is never scaled - it is a schedule, not an estimate."""

    broker: Broker = IBKR
    multiplier: float = 1.0

    def side_frac(self, price: float, value: float, turnover, vol_daily) -> float:
        return self.multiplier * (
            half_spread_frac(price, turnover) + impact_frac(value, turnover, vol_daily)
        )

    def buy_px(self, price: float, value: float, turnover, vol_daily) -> float:
        return price * (1 + self.side_frac(price, value, turnover, vol_daily))

    def sell_px(self, price: float, value: float, turnover, vol_daily) -> float:
        return price * (1 - self.side_frac(price, value, turnover, vol_daily))

    def round_trip(self, price: float, qty: float, turnover, vol_daily) -> float:
        """Dollars: brokerage both ways plus spread and impact both ways."""
        value = abs(price * qty)
        return 2 * self.broker.fee(value) + 2 * value * self.side_frac(
            price, value, turnover, vol_daily
        )


__all__ = [
    "AUCTION_SHARE",
    "BROKERS",
    "Broker",
    "CHEAPEST_API",
    "Costs",
    "IBKR",
    "MAX_BAR_SHARE",
    "half_spread_frac",
    "impact_frac",
    "round_to_tick",
    "tick",
]
