"""The arena: fake-money accounts where every tactic is run by the AI agent.

Nothing in this package can place a real order. `arena_place_order` refuses unless the
broker mode is `sim`. The real-money path (asxbot.broker.orders.place_order, human approval
on every call) is untouched and stays the only route to a real broker.
"""
