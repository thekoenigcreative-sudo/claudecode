"""Version 1 of the announcements playbook as it ran on 23-24 Sep 2026, for the tests of
its machinery. v1 was retired on 2026-09-24 (disabled in config.yaml, values kept), and
Level 1 gained a $5,000 per-position cap, 3 open and 6 new a day the same evening; the v1
tests keep testing v1 as it was."""

import dataclasses

from asxbot.arena.levels import load_playbook


def v1_playbook(cfg):
    pb = load_playbook(cfg, "asx_announcements")
    level = dataclasses.replace(
        pb.level, max_open_positions=4, max_position_aud=None, max_new_positions_per_day=None
    )
    return dataclasses.replace(pb, enabled=True, level=level)
