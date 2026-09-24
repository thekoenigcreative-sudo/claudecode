"""The IBKR data layer must never be able to place, change or cancel an order.

Rick's brief (24 Sep 2026): IB Gateway is logged in to his LIVE account. Three locks:
Gateway's "Read-Only API" setting, the connection opened read-only with its order methods
replaced (gateway.disable_orders), and this test, which fails the build if any module in
the data layer names an order call. The real-money adapter (broker/ibkr.py) is a separate
module, never imported by the data layer, and is checked for that here too.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "asxbot"
DATA_LAYER = sorted((SRC / "ibkr").glob("*.py")) + [SRC / "arena" / "intraday.py"]

# Every order-related call in ib_async / the TWS API, and the order classes.
BANNED = {
    "placeOrder", "cancelOrder", "reqGlobalCancel", "bracketOrder", "oneCancelsAll",
    "whatIfOrder", "whatIfOrderAsync", "exerciseOptions", "reqOpenOrders", "reqAllOpenOrders",
    "reqAutoOpenOrders", "reqCompletedOrders", "reqOpenOrdersAsync", "reqAllOpenOrdersAsync",
    "reqCompletedOrdersAsync", "Order", "LimitOrder", "MarketOrder", "StopOrder",
    "StopLimitOrder", "Trade", "IBKRBroker", "place_limit_order", "place_order",
    "arena_place_order",
}  # fmt: skip
BANNED_MODULES = {"asxbot.broker.ibkr", "asxbot.broker", "asxbot.arena.orders"}


def order_names(source: str) -> list[str]:
    """Every banned name the code uses: called, referenced as an attribute or a name, or
    imported. Strings and comments do not count (the guard names them as text)."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in BANNED:
            found.append(node.attr)
        elif isinstance(node, ast.Name) and node.id in BANNED:
            found.append(node.id)
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "") in BANNED_MODULES:
                found.append(f"from {node.module}")
            found += [a.name for a in node.names if a.name in BANNED]
        elif isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name in BANNED_MODULES]
    return found


def test_the_scan_finds_a_planted_order_call():
    """The test tests itself: each way an order could creep in is caught."""
    assert order_names("ib.placeOrder(c, o)") == ["placeOrder"]
    assert order_names("from ib_async import IB, LimitOrder") == ["LimitOrder"]
    assert order_names("x = MarketOrder('BUY', 1)") == ["MarketOrder"]
    assert order_names("from asxbot.broker.ibkr import IBKRBroker") == [
        "from asxbot.broker.ibkr", "IBKRBroker",
    ]  # fmt: skip
    assert order_names("f = getattr(ib, 'placeOrder')") == []  # text only: see the guard test
    assert order_names("ib.reqHistoricalDataAsync(c, '', '1 D', '1 min', 'TRADES', False)") == []


@pytest.mark.parametrize("path", DATA_LAYER, ids=lambda p: p.name)
def test_no_order_call_in_the_ibkr_data_layer(path):
    names = order_names(path.read_text(encoding="utf-8"))
    assert names == [], f"{path.relative_to(SRC)} names order calls: {names}"


def test_the_data_layer_is_the_whole_ibkr_package():
    got = {p.name for p in DATA_LAYER}
    assert {"__init__.py", "gateway.py", "feed.py", "check.py", "intraday.py"} <= got


def test_every_order_method_on_the_connection_refuses():
    from ib_async import IB

    from asxbot.ibkr.gateway import disable_orders

    ib = IB()
    disabled = set(disable_orders(ib))
    for name in ("placeOrder", "cancelOrder", "reqGlobalCancel", "whatIfOrder",
                 "exerciseOptions", "bracketOrder"):  # fmt: skip
        assert name in disabled
        with pytest.raises(PermissionError):
            getattr(ib, name)(None, None)
    for name in ("placeOrder", "cancelOrder", "reqGlobalCancel"):
        with pytest.raises(PermissionError):
            getattr(ib.client, name)(1, None, None)
    # the data calls, and ib_async's own events, are untouched
    assert callable(ib.reqHistoricalDataAsync) and hasattr(ib.orderStatusEvent, "emit")
