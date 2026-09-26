"""python -m asxbot.search <command>

  build     turn the IBKR 1-minute history into the search's cache (once; days kept)
  check     what data is there: sessions per window, stocks, announcements, auction prints
  run       every registered idea not yet tried: practice -> check -> finalists
  sealed    each finalist's ONE locked-test run (WINNER.md)
  report    rebuild reports/research_log.md and reports/cloud_rules_search_<date>.md
  all       build, check, run, sealed, report
  synthetic write a random-walk market to a folder (plumbing only - never a result)

Data: --history (or ASXBOT_HISTORY_DIR) and --data (or ASXBOT_DATA_DIR), see search/data.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def data_note(m, s) -> str:
    from asxbot.search.data import INDEX

    codes = m.codes()
    on_disk = m.days_on_disk()
    if not on_disk:
        return (f"**No IBKR history was found** at `{m.history}` (no `{INDEX}` folder). "
                "Nothing below was run on market data.")  # fmt: skip
    ann = m.announcements(date(2000, 1, 1), date(2100, 1, 1))
    d = m.daily
    auc = float(d["has_auction"].mean() * 100) if len(d) else 0.0
    w = {k: len(v) for k, v in s.win.items()} if s else {}
    return (f"IBKR 1-minute history for {len(codes)} codes and the ASX 200 index, "
            f"{on_disk[0]} to {on_disk[-1]} ({len(on_disk)} sessions): practice (TUNE) "
            f"{w.get('tune', 0)} sessions, check (VALIDATE) {w.get('validate', 0)}, sealed "
            f"(LOCKED) {w.get('locked', 0)}. Announcements in the archive: {len(ann)} "
            f"({int(ann['price_sensitive'].sum()) if len(ann) else 0} price-sensitive). "
            f"Stock-days with a closing-auction print: {auc:.0f}%. Frozen rule bots for "
            f"comparison: {s.replay_json.name if s and s.replay_json else 'none found'}."
            )  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m asxbot.search")
    ap.add_argument("command", choices=["build", "check", "run", "sealed", "report", "all",
                                        "synthetic"])  # fmt: skip
    ap.add_argument("--history")
    ap.add_argument("--data")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", help="comma-separated idea ids")
    ap.add_argument("--out", help="synthetic: the folder to write")
    ap.add_argument("--summary", default="", help="report: the plain-English summary")
    ap.add_argument("--next", default="", help="report: what the AI trader should try next")
    a = ap.parse_args(argv)

    if a.command == "synthetic":
        from asxbot.search.synthetic import make

        h, dd = make(Path(a.out))
        print(f"synthetic history {h}\nsynthetic data {dd}")
        return 0

    from asxbot.search.data import Market
    from asxbot.search.run import Search

    m = Market(a.history, a.data)
    if a.command in ("build", "all"):
        got = m.build(workers=a.workers,
                      progress=lambda i, n: print(f"cached {i}/{n}", flush=True)
                      if i % 20 == 0 or i == n else None)  # fmt: skip
        print(json.dumps(got))
    s = Search(m, REPO) if len(m.daily) else None
    if a.command in ("check", "all"):
        print(data_note(m, s))
    if a.command in ("run", "all") and s:
        s.run(a.only.split(",") if a.only else None)
    if a.command in ("sealed", "all") and s:
        s.sealed()
    if a.command in ("report", "all"):
        from asxbot.search import report

        tried = s.tried() if s else []
        meta = {"date": datetime.now().strftime("%Y-%m-%d"), "data_note": data_note(m, s),
                "summary": a.summary, "next": a.next}  # fmt: skip
        log, rep = report.write(REPO, tried, meta)
        print(f"wrote {log}\nwrote {rep}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
