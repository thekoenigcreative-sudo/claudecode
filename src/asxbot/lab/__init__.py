"""The Practice Lab (PRACTICE_LAB.md): the day trader practising constantly on real IBKR history.

sim        the time machine: one day, one variant, rule bot and agent, through the live code
anon       anonymised agent packets for days before the models' knowledge cutoff
agentcall  the lab's own calls to the decider's model (claude -p; never OpenClaw), cached
splits     TUNE / VALIDATE / LOCKED TEST / SHADOW windows, and the locked test's seal
variants   the registry of every variant tried, and what a variant may change
score      scorecards, the screen and validation gates
winner     WINNER.md in code
runner     `asxbot lab tick`: the next unit of work, whenever the market is closed
research   packets for the research session, and ingesting its proposals
shadow     forward paper trading of promoted variants, day by day
report     the lab report, the scoreboard, the evening-report section
"""
