"""Costs, frozen 2026-09-22 (config.yaml `costs`).

Brokerage: IBKR Australia fixed, 0.088% of value, $6.60 minimum, each side, GST included.
Slippage:  pct = min(cap, base + coeff * value / average daily dollar turnover), adverse on
           both entry and exit; `multiplier` reruns everything at 2x.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    brokerage_pct: float = 0.088
    brokerage_min: float = 6.60
    slip_base_pct: float = 0.10
    slip_impact_coeff: float = 0.5
    slip_cap_pct: float = 1.0
    multiplier: float = 1.0

    def brokerage(self, value: float) -> float:
        return max(self.brokerage_min, value * self.brokerage_pct / 100.0)

    def slippage_pct(self, value: float, adv_dollar: float | None) -> float:
        """As a fraction (0.001 = 0.1%)."""
        if adv_dollar is None or adv_dollar != adv_dollar or adv_dollar <= 0:
            pct = self.slip_cap_pct
        else:
            pct = min(
                self.slip_cap_pct, self.slip_base_pct + self.slip_impact_coeff * value / adv_dollar
            )
        return pct * self.multiplier / 100.0

    def buy_price(self, px: float, value: float, adv_dollar: float | None) -> float:
        return px * (1 + self.slippage_pct(value, adv_dollar))

    def sell_price(self, px: float, value: float, adv_dollar: float | None) -> float:
        return px * (1 - self.slippage_pct(value, adv_dollar))

    @classmethod
    def from_config(cls, cfg, multiplier: float = 1.0) -> CostModel:
        s = cfg.get("costs.slippage")
        return cls(
            brokerage_pct=float(cfg.get("costs.brokerage_pct")),
            brokerage_min=float(cfg.get("costs.brokerage_min_aud")),
            slip_base_pct=float(s["base_pct"]),
            slip_impact_coeff=float(s["impact_coeff"]),
            slip_cap_pct=float(s["cap_pct"]),
            multiplier=multiplier,
        )
