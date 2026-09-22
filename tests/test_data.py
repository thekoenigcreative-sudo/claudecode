import numpy as np
import pandas as pd
import pytest

from asxbot.data.base import COLUMNS, ProviderUnavailable, normalise
from asxbot.data.benchmark import regime_on
from asxbot.data.store import PriceStore
from asxbot.data.universe import parse_directory, turnover_eligibility
from fake_provider import FakeProvider, synthetic_bars


def test_normalise_shapes_columns_and_index():
    df = synthetic_bars(10)
    df.columns = ["Open", "High", "Low", "Close", "Volume"]
    df.index = df.index.tz_localize("UTC")
    out = normalise(df)
    assert list(out.columns) == COLUMNS
    assert out.index.name == "date"
    assert out.index.tz is None
    assert out.index.is_monotonic_increasing and out.index.is_unique


def test_normalise_rejects_missing_columns():
    with pytest.raises(ValueError, match="missing columns"):
        normalise(pd.DataFrame({"close": [1.0]}, index=pd.to_datetime(["2020-01-01"])))


def test_store_caches_and_refreshes(tmp_path):
    prov = FakeProvider({"AAA": synthetic_bars(50), "BBB": synthetic_bars(50, seed=1)})
    store = PriceStore(tmp_path, prov)
    got = store.get_many(["AAA", "BBB", "ZZZ"], "2020-01-01")
    assert len(got["AAA"]) == 50 and got["ZZZ"].empty
    assert prov.calls == [["AAA", "BBB", "ZZZ"]]
    # second call: everything served from cache, provider not touched
    got2 = store.get_many(["AAA", "BBB"], "2020-01-01")
    assert prov.calls == [["AAA", "BBB", "ZZZ"]]
    pd.testing.assert_frame_equal(got["AAA"], got2["AAA"], check_freq=False)
    # refresh forces a fetch
    store.get_many(["AAA"], "2020-01-01", refresh=True)
    assert prov.calls[-1] == ["AAA"]
    assert not list(store.dir.glob("*.tmp"))
    assert set(store.cached_tickers()) == {"AAA", "BBB", "ZZZ"}


def test_panel():
    frames = {"A": synthetic_bars(5), "B": synthetic_bars(5, seed=2), "C": pd.DataFrame()}
    p = PriceStore.panel(frames, "close")
    assert list(p.columns) == ["A", "B"] and len(p) == 5


def test_turnover_eligibility_uses_only_prior_days():
    idx = pd.bdate_range("2021-01-01", periods=30)
    close = pd.DataFrame({"X": np.full(30, 10.0)}, index=idx)
    vol = pd.DataFrame({"X": np.full(30, 1_000.0)}, index=idx)  # $10k/day, below floor
    vol.iloc[25:, 0] = 100_000.0  # jumps to $1m/day on day 25
    elig = turnover_eligibility(close, vol, floor_aud=250_000, window=5)
    # day t looks at t-5..t-1. Day 27 sees days 22-26: three low, two high -> median low.
    # Day 28 sees days 23-27: two low, three high -> median high -> first eligible day.
    assert not elig["X"].iloc[:28].any()
    assert elig["X"].iloc[28:].all()


def test_regime_lags_one_day():
    s = pd.Series(np.arange(1, 11, dtype=float), index=pd.bdate_range("2021-01-01", periods=10))
    r = regime_on(s, sma_days=3)
    assert r.dtype == bool
    assert not r.iloc[:3].any() and r.iloc[3:].all()


def test_parse_directory_markit():
    text = (
        '"ASX code","Company name","GICs industry group","Listing date","Market Cap"\n'
        '"BHP","BHP GROUP LIMITED","Materials","13/08/1885",250000000000\n'
        '"ABC","ALPHA","Energy","01/01/2020",\n'
        '"BHPPA","NOT A SHARE","Materials","01/01/2020",1\n'
    )
    df = parse_directory(text, "markit")
    assert df["code"].tolist() == ["BHP", "ABC"]
    assert df.loc[0, "market_cap"] == 250_000_000_000
    assert pd.isna(df.loc[1, "market_cap"])


def test_parse_directory_classic():
    text = (
        "ASX listed companies as at Tue Sep 22 2026\n\n"
        "Company name,ASX code,GICS industry group\n"
        '"1414 DEGREES LIMITED","14D","Capital Goods"\n'
    )
    df = parse_directory(text, "classic")
    assert df["code"].tolist() == ["14D"] and df["market_cap"].isna().all()


def test_norgate_unavailable_without_library():
    from asxbot.data.norgate import NorgateProvider

    with pytest.raises(ProviderUnavailable, match="Norgate"):
        NorgateProvider().daily("BHP", "2020-01-01")
