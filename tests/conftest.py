from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def repo_root() -> Path:
    return REPO


@pytest.fixture
def base_config() -> dict:
    with open(REPO / "config.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture
def config_file(tmp_path, base_config):
    """Write a modified copy of config.yaml to tmp and return its path."""

    def _make(**overrides):
        raw = dict(base_config)
        for k, v in overrides.items():
            raw[k] = v
        p = tmp_path / "config.yaml"
        p.write_text(yaml.safe_dump(raw), encoding="utf-8")
        return p

    return _make


@pytest.fixture(autouse=True)
def local_logs(tmp_path, monkeypatch) -> Path:
    """Every test logs to its own folder, never to the real one in %LOCALAPPDATA%.

    ASXBOT_LOG_DIR is what asxbot.log.logs_dir() and the scripts/ launchers both read, and
    a launcher run as a subprocess inherits it.
    """
    d = tmp_path / "local_logs"
    monkeypatch.setenv("ASXBOT_LOG_DIR", str(d))
    monkeypatch.delenv("ASXBOT_STDOUT_LOG", raising=False)
    return d


@pytest.fixture(autouse=True)
def foreman_home(tmp_path, monkeypatch) -> Path:
    """Every test hands over to its own Foreman inbox, never the real one in
    %USERPROFILE%\\.foreman: the real Foreman would answer Rick and act on it."""
    d = tmp_path / "foreman"
    monkeypatch.setenv("FOREMAN_HOME", str(d))
    return d
