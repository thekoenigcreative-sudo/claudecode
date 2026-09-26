"""A shadow variant with 10 good shadow days becomes FINAL: frozen, one locked-test run (the seal
counts it), then WINNER.md's eleven tests. All pass -> `winner_message.txt` for Rick (the
Foreman sends it once on the Trader chat) and stage `winner`. Otherwise `retired`, for good."""

from __future__ import annotations

import time
from datetime import datetime

from asxbot.lab import runner, shadow, splits, store, winner
from asxbot.lab.variants import Registry


def anon_flips(cfg, v: dict, book: str, deadline) -> bool | None:
    """For an agent book: does anonymising its validation days flip the sign of its P&L?"""
    if book != "agent":
        return None
    days = splits.sessions_in("validate")
    r = runner.run_days(cfg, v, "validate", days, ["bot", "agent"], anon=True, deadline=deadline)
    if r != "done":
        raise TimeoutError(r)
    raw = runner.cards(v["id"], "validate", v["playbook"], ["agent"], days=days)["agent"][
        "pnl_after_fees"
    ]
    anon = runner.cards(v["id"], "validate", v["playbook"], ["agent"], anon=True, days=days)[
        "agent"
    ]["pnl_after_fees"]
    return (raw > 0) != (anon > 0)


def final(cfg, reg: Registry, v: dict, deadline) -> str:
    lab = store.lab_data(cfg)
    if v["stage"] != "final":
        reg.set_stage(v, "final", "10 shadow days in profit: frozen for its one locked-test run")
        v["final"] = {
            "n_finals": splits.unseal(lab, v, "final candidate"),
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        reg.save(v)
    days = splits.sessions_in("locked")
    for who, books in ((v, runner.books_for(v)), (runner.BASE_VARIANT, ["bot"])):
        r = runner.run_days(cfg, who, "locked", days, books, allow_locked=True, deadline=deadline)
        if r != "done":
            return f"final {v['id']}: locked run {r} (continues next tick)"
    base_locked = runner.cards("baseline", "locked", v["playbook"], ["bot"], days=days)["bot"]
    sdays = shadow.shadow_days(v)
    base_shadow = runner.cards("baseline", "shadow", v["playbook"], ["bot"], days=sdays)["bot"]
    verdicts = {}
    for book in runner.books_for(v):
        locked = runner.cards(v["id"], "locked", v["playbook"], [book], days=days)[book]
        sh = runner.cards(v["id"], "shadow", v["playbook"], [book], days=sdays)[book]
        try:
            flip = anon_flips(cfg, v, book, deadline)
        except TimeoutError as e:
            return f"final {v['id']}: anonymised validation {e} (continues next tick)"
        res = winner.check(v, locked, base_locked, sh, base_shadow, v["final"]["n_finals"], flip)
        verdicts[book] = {
            "results": res,
            "winner": winner.is_winner(res),
            "locked": {k: locked[k] for k in locked if k != "daily"},
            "shadow": {k: sh[k] for k in sh if k != "daily"},
        }
        if verdicts[book]["winner"]:
            msg = winner.message(v, book, res, locked, sh)
            store.write_json(
                lab / "winner_message.json",
                {
                    "variant": v["id"],
                    "book": book,
                    "text": msg,
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "sent": False,
                },
            )
    v["results"]["locked"] = verdicts
    won = [b for b, r in verdicts.items() if r["winner"]]
    reg.set_stage(
        v,
        "winner" if won else "retired",
        "met every WINNER.md test"
        if won
        else "; ".join(
            f"{b}: failed " + ", ".join(n for n, ok, _ in r["results"] if not ok)
            for b, r in verdicts.items()
        ),
    )
    reg.log_run(
        {
            "event": "locked",
            "variant": v["id"],
            "winner": won,
            "n_finals": v["final"]["n_finals"],
            "at": time.time(),
        }
    )
    return f"final {v['id']}: {'WINNER ' + ','.join(won) if won else 'retired'}"
