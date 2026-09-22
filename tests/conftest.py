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
