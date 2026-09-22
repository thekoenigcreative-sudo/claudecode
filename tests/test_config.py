import pytest

from asxbot.config import ConfigError, load_config


def test_defaults_load(config_file, tmp_path):
    cfg = load_config(config_file(), env_file=tmp_path / "nonexistent.env")
    assert cfg.broker == "sim"
    assert cfg.get("capital.starting_aud") == 10000
    assert cfg.get("capital.max_positions") == 4
    assert cfg.position_size_aud == 2500.0
    assert "plumbing test" in cfg.data_label()


def test_frozen_dates_present(base_config):
    for section in ("strategy", "baseline", "costs", "universe"):
        assert base_config[section]["frozen_on"] == "2026-09-22"


def test_live_refused_without_env(config_file, tmp_path, monkeypatch):
    monkeypatch.delenv("LIVE_TRADING_CONFIRMED", raising=False)
    with pytest.raises(ConfigError, match="LIVE_TRADING_CONFIRMED"):
        load_config(config_file(broker="live"), env_file=tmp_path / "none.env")


def test_live_refused_with_wrong_env(config_file, tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_CONFIRMED", "no")
    with pytest.raises(ConfigError):
        load_config(config_file(broker="live"), env_file=tmp_path / "none.env")


def test_live_allowed_with_both_keys(config_file, tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_CONFIRMED", "yes")
    cfg = load_config(config_file(broker="live"), env_file=tmp_path / "none.env")
    assert cfg.broker == "live"


def test_bad_broker(config_file, tmp_path):
    with pytest.raises(ConfigError, match="broker must be"):
        load_config(config_file(broker="robinhood"), env_file=tmp_path / "none.env")


def test_limit_below_position_size_refused(config_file, tmp_path, base_config):
    limits = dict(base_config["limits"], max_position_aud=100)
    with pytest.raises(ConfigError, match="max_position_aud"):
        load_config(config_file(limits=limits), env_file=tmp_path / "none.env")
