import pandas as pd

from asxbot.io import write_parquet_atomic, write_text_atomic
from asxbot.log import EventLog


def test_event_log_roundtrip(tmp_path):
    ev = EventLog(tmp_path)
    ev.append("orders", {"ticker": "BHP", "qty": 10})
    ev.append("orders", {"ticker": "RIO", "qty": 5})
    rows = ev.read("orders")
    assert [r["ticker"] for r in rows] == ["BHP", "RIO"]
    assert all("ts" in r for r in rows)
    assert ev.read("nothing") == []


def test_parquet_atomic(tmp_path):
    df = pd.DataFrame({"a": [1, 2]}, index=pd.Index([0, 1], name="i"))
    p = write_parquet_atomic(df, tmp_path / "x" / "y.parquet")
    assert p.exists()
    assert not list((tmp_path / "x").glob("*.tmp"))
    assert pd.read_parquet(p)["a"].tolist() == [1, 2]


def test_text_atomic(tmp_path):
    p = write_text_atomic("hello", tmp_path / "r" / "a.md")
    assert p.read_text(encoding="utf-8") == "hello"
    assert not list((tmp_path / "r").glob("*.tmp"))


def test_safe_stem_windows_reserved_names():
    from asxbot.io import safe_stem

    assert safe_stem("PRN") == "PRN_" and safe_stem("CON") == "CON_" and safe_stem("BHP") == "BHP"
