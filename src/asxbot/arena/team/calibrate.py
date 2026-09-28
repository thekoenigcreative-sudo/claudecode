"""Each live day of the team replayed in the simulator: how closely did the simulated team
match what it did live? (28 Sep 2026, Rick: "score it ... in the simulator's calibration").

Two checks, both against the simulator's stored history for the day (IBKR's 1-minute history
fetched at 17:30, the announcements the live collector kept):

  A. THE SAME DECISIONS (plain code, free, every evening before the report). The team's own
     order actions, exactly as they reached the live broker and at the moments they did,
     are sent to the simulator's broker over the stored bars, from the same start-of-day book.
     Any difference is the simulator's market and fills against the live feed's: orders
     filled or not, the fill prices (in basis points), the day's P&L.
  B. THE SAME TEAM (model calls, after the report; budgeted, stops at the usage stop). The
     simulated team runs the whole day again from the same start-of-day book, with the same
     lessons, on the stored market - the simulator exactly as the lab uses it. What it traded
     (stocks, sides), how often it was woken, its P&L, against the live day. The models do not
     answer the same way twice, so B measures the simulator AND the team's own variance; A
     isolates the market.

Results: data/arena/team/calibration/<day>.json and reports/team_calibration_<day>.md; the
evening report carries one line each.
"""

from __future__ import annotations

import json
import statistics
from datetime import date, datetime
from pathlib import Path

from asxbot.arena.team.runner import replay_session, shortable, team_root
from asxbot.arena.team.session import Book
from asxbot.io import write_text_atomic
from asxbot.lab.tsim.anon import NoAnon
from asxbot.lab.tsim.broker import Account, SimBroker
from asxbot.lab.tsim.costs import CostModel
from asxbot.lab.tsim.market import FEED_LAG, SLOTS, slot_time
from asxbot.lab.tsim.tools import TraderView
from asxbot.log import get_logger

log = get_logger("asxbot.arena.team")


def cal_path(cfg, day: date) -> Path:
    return team_root(cfg) / "calibration" / f"{day.isoformat()}.json"


def load(cfg, day: date) -> dict:
    p = cal_path(cfg, day)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def save(cfg, day: date, d: dict) -> Path:
    p = cal_path(cfg, day)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(json.dumps(d, indent=1, default=str), p)
    return p


def _stored_market(cfg, day: date, codes: list[str]):
    from asxbot.arena.replay_ibkr import announcements, history_root
    from asxbot.arena.team.market import build_summaries
    from asxbot.lab.tsim.market import History, Market
    from asxbot.lab.tsim.run import _ann_window

    summ = build_summaries(codes, day)
    from datetime import timedelta

    ann = announcements(cfg, day - timedelta(days=7), day)
    return Market(
        day, History(history_root()), list(codes), _ann_window(ann, day), summ, shortable(cfg)
    )


def same_decisions(cfg, day: date, root: Path | None = None) -> dict:
    """Check A: the live day's own actions, replayed through the simulator's broker."""
    from asxbot.arena.team.limits import conf_of

    book = Book(root or team_root(cfg))
    iso = day.isoformat()
    live = book.read_day(iso)
    start = book.read_start(iso)
    if live is None or start is None:
        return {"day": iso, "ran": False, "why": "no live day recorded for the team"}
    acts = [
        a
        for a in book.actions(iso)
        if (a.get("sent") or a.get("action")) and (a.get("result") or {}).get("ok")
    ]
    codes = sorted(
        set(live.get("universe") or [])
        | {str((a.get("sent") or a["action"]).get("code", "")).upper() for a in acts} - {""}
    )
    conf = conf_of(cfg)
    m = _stored_market(cfg, day, codes)
    acct = Account.from_dict(start["account"])
    sb = SimBroker(
        acct,
        CostModel.from_config(cfg, conf.get("broker")),
        float(conf.get("max_volume_share", 0.20)),
        m.shortable,
    )
    sb.market = m
    v = TraderView(m, sb, NoAnon(), None)
    start_eq = acct.equity(m.prev_close)
    ids: dict[str, str] = {}
    acts.sort(key=lambda a: a["at"])
    i = 0
    refused = []

    def send(a: dict) -> None:
        at = datetime.fromisoformat(a["at"])
        x = dict(a.get("sent") or a["action"])
        if x.get("id"):
            x["id"] = ids.get(str(x["id"]), x["id"])
        if a.get("by") == "code":
            try:
                o = sb.place(
                    x["code"],
                    x["side"],
                    int(x["qty"]),
                    x.get("type", "market"),
                    at=at,
                    reduce_only=True,
                    by="code",
                    why="replayed",
                )
                r = {"ok": True, "id": o.id}
            except Exception as e:  # noqa: BLE001
                r = {"ok": False, "error": str(e)}
        else:
            m.now = at
            r = v.do(x, at, "team")
        live_id = (a.get("result") or {}).get("id")
        if r.get("ok") and live_id and r.get("id"):
            ids[str(live_id)] = str(r["id"])
        elif not r.get("ok"):
            refused.append({"at": a["at"], "action": x, "error": r.get("error")})

    for slot in range(SLOTS):
        delivered = slot_time(day, slot + 1) + FEED_LAG
        while i < len(acts) and datetime.fromisoformat(acts[i]["at"]) <= delivered:
            send(acts[i])
            i += 1
        m.now = max(delivered, slot_time(day, slot))
        sb.work_slot(slot)
        sb.events = []
    while i < len(acts):
        send(acts[i])
        i += 1
    m.now = slot_time(day, SLOTS)
    sb._expire_day_orders(m.now)
    eq = acct.equity(m.last)
    # order by order: the live book's orders of the day against their replayed twins
    live_acct = Account.from_dict((book.load() or {}).get("account") or start["account"])
    rows, bps = [], []
    for lid, sid in ids.items():
        lo, so = live_acct.orders.get(lid), acct.orders.get(sid)
        if lo is None or so is None or lo.submitted_at[:10] != day.isoformat():
            continue
        row = {
            "order": lid,
            "code": lo.code,
            "side": lo.side,
            "type": lo.type,
            "live_filled": lo.filled,
            "sim_filled": so.filled,
            "live_avg": lo.avg_price,
            "sim_avg": so.avg_price,
        }
        if lo.avg_price and so.avg_price:
            d_bps = (so.avg_price / lo.avg_price - 1) * 1e4 * (1 if lo.buy else -1)
            row["sim_worse_bps"] = round(d_bps, 1)
            bps.append(d_bps)
        rows.append(row)
    same = sum(1 for r in rows if r["live_filled"] == r["sim_filled"])
    out = {
        "day": iso,
        "ran": True,
        "actions": len(acts),
        "orders": len(rows),
        "filled_the_same": same,
        "refused_in_sim": refused,
        "median_sim_worse_bps": round(float(statistics.median(bps)), 1) + 0.0 if bps else None,
        "live_pnl": live.get("pnl"),
        "sim_pnl": round(eq - start_eq, 2),
        "rows": rows,
    }
    return out


def same_team(cfg, day: date, *, max_rise: float | None = None, ask=None) -> dict:
    """Check B: the simulated team re-runs the live day from the same start (model calls)."""
    from asxbot.arena.team.limits import conf_of
    from asxbot.lab.tsim.run import tsim_local

    book = Book(team_root(cfg))
    iso = day.isoformat()
    live = book.read_day(iso)
    start = book.read_start(iso)
    if live is None or start is None:
        return {"day": iso, "ran": False, "why": "no live day recorded for the team"}
    conf = conf_of(cfg)
    rise = (
        max_rise
        if max_rise is not None
        else float((conf.get("calibration") or {}).get("max_rise", 0.03))
    )
    root = tsim_local() / "team_replays" / f"cal_{iso}_{datetime.now():%Y%m%d%H%M%S}"
    s = replay_session(
        cfg,
        day,
        root,
        codes=list(live.get("universe") or []),
        state={"account": start["account"], "alerts": start.get("alerts")},
        max_rise=rise,
        lessons_from=team_root(cfg),
        ask=ask,
    )
    res = s.run()
    lt = {(t["code"], t["direction"]) for t in live.get("trades") or []}
    st = {(t["code"], t["direction"]) for t in res.get("trades") or []}
    lo = _placed(live)
    so = _placed(res)
    both = lt | st
    return {
        "day": iso,
        "ran": True,
        "folder": str(root),
        "silenced": res.get("silenced") or "",
        "live": {
            "pnl": live.get("pnl"),
            "trades": sorted(map(list, lt)),
            "orders": lo,
            "wakes": live.get("wakes"),
            "calls": live.get("calls"),
        },
        "sim": {
            "pnl": res.get("pnl"),
            "trades": sorted(map(list, st)),
            "orders": so,
            "wakes": res.get("wakes"),
            "calls": res.get("calls"),
        },
        "trades_in_common": sorted(map(list, lt & st)),
        "overlap": round(len(lt & st) / len(both), 2) if both else None,
    }


def _placed(rec: dict) -> int:
    return int(rec.get("orders_placed") or 0)


def calibrate(cfg, day: date, *, rerun: bool = False, ask=None) -> dict:
    if Book(team_root(cfg)).read_day(day.isoformat()) is None:
        # a day the team did not trade (before its first day, the bots off): nothing written
        return {"day": day.isoformat(), "nothing": "the team did not trade that day"}
    d = load(cfg, day)
    try:
        d["same_decisions"] = same_decisions(cfg, day)
    except Exception as e:  # noqa: BLE001
        log.exception("team calibration A failed: %s", e)
        d["same_decisions"] = {"ran": False, "why": f"failed: {type(e).__name__}: {e}"}
    if rerun:
        try:
            d["same_team"] = same_team(cfg, day, ask=ask)
        except Exception as e:  # noqa: BLE001
            log.exception("team calibration B failed: %s", e)
            d["same_team"] = {"ran": False, "why": f"failed: {type(e).__name__}: {e}"}
    d["at"] = datetime.now().isoformat(timespec="seconds")
    save(cfg, day, d)
    write_report(cfg, day, d)
    return d


def lines(d: dict) -> list[str]:
    out = []
    a = d.get("same_decisions") or {}
    if a.get("ran"):
        bps = a.get("median_sim_worse_bps")
        out.append(
            f"Same decisions on the simulator's market ({a['day']}): {a['filled_the_same']} of "
            f"{a['orders']} orders filled the same"
            + (f", median fill {bps:+.1f} bp worse in the simulator" if bps is not None else "")
            + f"; P&L live {_m(a.get('live_pnl'))} vs simulator {_m(a.get('sim_pnl'))}"
            + (
                f"; {len(a['refused_in_sim'])} refused in the simulator"
                if a.get("refused_in_sim")
                else ""
            )
            + "."
        )
    elif a:
        out.append(f"Same decisions on the simulator's market: not run ({a.get('why')}).")
    b = d.get("same_team") or {}
    if b.get("ran"):
        ov = b.get("overlap")
        out.append(
            f"The simulated team re-run on {b['day']}: P&L {_m(b['sim']['pnl'])} vs live "
            f"{_m(b['live']['pnl'])}; trades {len(b['sim']['trades'])} vs {len(b['live']['trades'])}"
            f", {len(b['trades_in_common'])} the same stock and side"
            + (f" (overlap {ov:.0%})" if ov is not None else "")
            + f"; woken {b['sim']['wakes']} vs {b['live']['wakes']} times"
            + (f"; cut short: {b['silenced'][:100]}" if b.get("silenced") else "")
            + "."
        )
    elif b:
        out.append(f"The simulated team re-run: not run ({b.get('why')}).")
    return out


def _m(x) -> str:
    return "-" if x is None else f"{float(x):+,.2f}"


def report_lines(cfg, day: date) -> list[str]:
    """For the evening report: today's check A and the newest check B (B runs after the
    report, so it is usually the previous live day's)."""
    root = team_root(cfg) / "calibration"
    out = []
    today = load(cfg, day)
    if today.get("same_decisions"):
        out += [x for x in lines({"same_decisions": today["same_decisions"]})]
    latest_b = None
    for p in sorted(root.glob("*.json"), reverse=True) if root.exists() else []:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (d.get("same_team") or {}).get("ran"):
            latest_b = d
            break
    if latest_b:
        out += lines({"same_team": latest_b["same_team"]})
    return ["Simulator check: " + x for x in out]


def write_report(cfg, day: date, d: dict) -> Path:
    # reports/ beside data/ (the settings checkout; a test's own folder in the tests)
    p = Path(cfg.data_dir).parent / "reports" / f"team_calibration_{day.isoformat()}.md"
    body = [
        f"# The AI team, {day.isoformat()}: the live day against the simulator",
        "",
        "Written by code (arena/team/calibrate.py). A: the team's own live order actions, "
        "at their live moments, through the simulator's broker on the stored history. B: the "
        "simulated team re-running the day from the same start (the models' answers vary "
        "from run to run, so B measures the simulator and the team's own variance).",
        "",
    ]
    body += [f"- {x}" for x in lines(d)] or ["- nothing to compare"]
    a = d.get("same_decisions") or {}
    if a.get("rows"):
        body += [
            "",
            "| order | stock | side | type | live filled | sim filled | live avg | sim avg "
            "| sim worse (bp) |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in a["rows"]:
            body.append(
                f"| {r['order']} | {r['code']} | {r['side']} | {r['type']} | "
                f"{r['live_filled']} | {r['sim_filled']} | {r['live_avg']} | "
                f"{r['sim_avg']} | {r.get('sim_worse_bps', '')} |"
            )
    p.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic("\n".join(body) + "\n", p)
    return p


def frozen_books_day(cfg, day: date) -> list[tuple[str, float | None]]:
    """Each frozen arena book's P&L on `day`, from its daily marks (read only)."""
    out = []
    marks = Path(cfg.data_dir) / "arena" / "marks"
    for p in sorted(marks.glob("*.jsonl")) if marks.exists() else []:
        rows = []
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        rows.sort(key=lambda r: r.get("day", ""))
        today = [r for r in rows if r.get("day") == day.isoformat()]
        before = [r for r in rows if r.get("day", "") < day.isoformat()]
        if not today:
            continue
        start = before[-1]["equity"] if before else 20_000.0
        out.append((p.stem, round(float(today[-1]["equity"]) - float(start), 2)))
    return out


def write_dry_run(cfg, day: date, res: dict, root: Path) -> Path:
    """reports/team_dryrun_<day>.md: the team's day on stored history, beside what the frozen
    books did live that day. A plumbing proof on one day, never a verdict on the team."""
    p = Path(cfg.data_dir).parent / "reports" / f"team_dryrun_{day.isoformat()}.md"
    u = res.get("usage") or {}

    def tok(tag):
        return int(
            sum(u.get(f"{tag}:{k}", 0) for k in ("input", "cache_write", "cache_read", "output"))
        )

    body = [
        f"# The AI team's dry run on {day.isoformat()} (stored history, a scratch book)",
        "",
        "One recorded day, replayed through the LIVE driver (arena/team/session.py) on a virtual "
        "clock: the simulator's team, its broker and costs, the hard limits, the usage stop. A "
        "plumbing proof on one day - not a result, and nothing is scored from it.",
        "",
        f"- Outcome: {res['how']}"
        + (f" (not asked at times: {res['silenced']})" if res.get("silenced") else ""),
        f"- Book: {root}",
        f"- Stocks it could see: {res.get('stocks')}",
        f"- P&L after all costs: {res['pnl']:+,.2f} (start {res['start_equity']:,.2f}, close "
        f"{res['equity_close']:,.2f}); fees {res.get('fees_today', 0):,.2f}",
        f"- Woken {res['wakes']} times; {res['calls']} model calls; thinking {res['think_s']:.0f} s",
        f"- Tokens: Opus {tok('opus'):,}, Sonnet {tok('sonnet'):,}; API-equivalent cost "
        f"${u.get('cost_usd', 0):,.2f}",
        f"- Models: {res.get('models')}",
        f"- Orders placed by the team: {res['orders_placed']}; by code (kill/close sweeps): "
        f"{res['orders_by_code']}; refused by the hard limits: {len(res['refused_by_limits'])}; "
        f"rejected by the broker: {len(res['rejected'])}",
        f"- Round trips closed: {len(res['trades'])}; held at the close: "
        f"{res.get('carried') or 'nothing'}",
        "",
        "## The frozen books, live that day (their daily marks)",
        "",
    ]
    fb = frozen_books_day(cfg, day)
    body += [f"- {n}: {v:+,.2f}" for n, v in fb] or ["- no marks recorded that day"]
    body += ["", "## Round trips", ""]
    body += [
        f"- {t['code']} {t['direction']} {t['opened']} -> {t['closed']}: {t['net']:+,.2f} "
        f"after costs (gross {t['gross']:+,.2f}, fees {t['fees']:,.2f})"
        for t in res["trades"]
    ] or ["- none"]
    body += ["", "## Every wake", ""]
    for w in res.get("wake_log") or []:
        if w.get("silenced"):
            body.append(f"- {w['at']}: NOT ASKED - {w['silenced']}")
            continue
        body.append(
            f"- {w['at']} ({w.get('phase')}) -> lands {w.get('lands')}; {w.get('calls')} "
            f"calls, {w.get('think_s')} s; {w.get('orders')} order action(s); woken by "
            f"{', '.join(w.get('woken_by') or []) or '-'}; note: "
            f"{(w.get('note') or '').replace(chr(10), ' ')[:300]}"
        )
    if res.get("refused_by_limits"):
        body += ["", "## Refused by the hard limits", ""]
        body += [f"- {x['at']} {x.get('code')}: {x['why']}" for x in res["refused_by_limits"]]
    if res.get("rejected"):
        body += ["", "## Rejected by the broker", ""]
        body += [f"- {x['at']} {x.get('code')}: {x['error']}" for x in res["rejected"]]
    p.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic("\n".join(body) + "\n", p)
    return p


__all__ = [
    "calibrate",
    "frozen_books_day",
    "lines",
    "load",
    "report_lines",
    "same_decisions",
    "same_team",
    "write_dry_run",
]
