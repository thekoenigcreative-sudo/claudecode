"""The announcement reader (CLOUD_BRIEF "MODELS AND USAGE" 3): Claude Sonnet 5 reads each
announcement ONCE and its structured summary is cached, so replaying a day costs almost nothing
in reading. It reports; it never decides.

Only for announcements released AFTER the models' knowledge cutoff (1 Jul 2026) and only in
runs that are not disguised: CLAUDE.md forbids AI classification of historical announcements.
The text is the PDF the live collector kept (data/announcements/pdf/<day>/<CODE>_<id>.pdf);
with no PDF, the reader is not called and the trader sees the headline only.
"""

from __future__ import annotations

import json
from pathlib import Path

from asxbot.lab import store
from asxbot.lab.tsim import llm

SYSTEM = """You read one ASX company announcement and report its facts for a trader. You never
recommend a trade. Reply with ONE JSON object: {"category": "results|guidance|contract|
acquisition|capital_raising|exploration|clinical|management|other", "direction": "positive|
negative|mixed|neutral", "materiality": 1-5, "key_numbers": ["..."], "summary": "at most 3
sentences of facts: what happened, the numbers, versus what was expected if stated"}."""

CUTOFF_ISO = "2026-07-01"


def cache_dir() -> Path:
    return store.lab_local() / "tsim" / "reader"


def pdf_text(cfg, row) -> str:
    from asxbot.io import safe_stem

    rel = str(row["released_at"])[:10]
    p = Path(cfg.data_dir) / "announcements" / "pdf" / rel / f"{safe_stem(str(row['code']))}_{row['ids_id']}.pdf"
    if not p.exists():
        return ""
    with p.open("rb") as f:
        if f.read(5) != b"%PDF-":  # never hand an unverified body to a parser
            return ""
    try:
        from pypdf import PdfReader

        r = PdfReader(str(p))
        return "\n".join((pg.extract_text() or "") for pg in r.pages[:8])[:20000]
    except Exception:  # noqa: BLE001
        return ""


def cached_reader(cfg):
    model, effort = llm.READER
    conf = (cfg.get("tradesim") or {}).get("reader") or {}
    model = str(conf.get("model", model))

    def read(row) -> dict | None:
        if str(row["released_at"])[:10] < CUTOFF_ISO:
            return None
        cp = cache_dir() / f"{row['ids_id']}.json"
        if cp.exists():
            return json.loads(cp.read_text(encoding="utf-8"))
        text = pdf_text(cfg, row)
        if not text.strip():
            return None
        try:
            res = llm.ask(f"HEADLINE: {row['headline']}\n\nTEXT:\n{text}", system=SYSTEM,
                          model=model, effort=effort, cfg=cfg)  # fmt: skip
        except llm.UsageStop:
            return None
        d = llm.parse_json(res.get("text", "")) if not res.get("error") else None
        if d is None:
            return None
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(d), encoding="utf-8")
        return d

    return read
