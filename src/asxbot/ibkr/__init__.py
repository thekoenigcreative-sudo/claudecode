"""IBKR market data through IB Gateway. DATA ONLY: quotes, 1-minute bars, the opening
auction price, halts.

Nothing in this package places, changes or cancels an order, and nothing may:
tests/test_ibkr_no_orders.py fails the build if any module here names an order call. The
connection is opened read-only, the order methods on it are replaced with ones that raise
(gateway.disable_orders), and IB Gateway's own "Read-Only API" setting refuses orders at the
broker. The arena's fills stay simulated (arena/broker.py). The real-money adapter is
broker/ibkr.py, a separate module, disabled while `broker:` is sim.
"""
