"""Rule-based yardstick bots.

For every playbook, code also runs its plain starting rule as a bot with its own simulated
account and no model use at all. The gap between the agent's account and its bot's account
is what the agent's judgment added - or lost.

A bot never calls a model, never reads a PDF, and never deviates. That is the point.
"""

from asxbot.arena.bots.base import Bot, BotDecision

__all__ = ["Bot", "BotDecision"]
