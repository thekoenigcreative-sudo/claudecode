"""Network test. Runs only with ASXBOT_NETWORK_TESTS=1. Plumbing test - not a go/no-go."""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ASXBOT_NETWORK_TESTS") != "1", reason="set ASXBOT_NETWORK_TESTS=1"
)


def test_yfinance_two_tickers_and_index():
    from asxbot.data.base import COLUMNS
    from asxbot.data.yf import YFinanceProvider

    p = YFinanceProvider()
    out = p.daily_many(["BHP", "STW", "^AXJO", "NOTATICKERXYZ"], "2024-01-01", "2024-03-01")
    assert list(out["BHP"].columns) == COLUMNS
    assert len(out["BHP"]) > 30 and len(out["^AXJO"]) > 30 and len(out["STW"]) > 30
    assert out["NOTATICKERXYZ"].empty
    assert p.label == "plumbing test - not a go/no-go"
