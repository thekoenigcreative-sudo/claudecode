"""Release isolation (scripts/deploy.py, asxbot.release, asxbot.arena.lock; 25 Sep 2026):
the tasks run an exported commit through a shim, settings and data stay in the checkout,
and the watcher is not restarted in market hours or with a position open."""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from asxbot import proc
from asxbot import release as R
from asxbot.arena import lock as L
from asxbot.config import ConfigError, load_config, repo_root

SYD = ZoneInfo("Australia/Sydney")
REPO = Path(__file__).resolve().parents[1]


# -- ASXBOT_HOME --------------------------------------------------------------------------
def test_repo_root_follows_asxbot_home_and_refuses_a_bad_one(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text((REPO / "config.yaml").read_text(encoding="utf-8"),
                                      encoding="utf-8")  # fmt: skip
    monkeypatch.setenv(R.HOME_ENV, str(home))
    assert repo_root() == home
    cfg = load_config(env_file=tmp_path / "none.env")
    assert cfg.root == home and cfg.data_dir == home / "data"
    monkeypatch.setenv(R.HOME_ENV, str(tmp_path / "nowhere"))
    with pytest.raises(ConfigError, match="no config.yaml"):
        repo_root()
    monkeypatch.delenv(R.HOME_ENV)
    assert repo_root() == REPO


# -- the shim -------------------------------------------------------------------------------
SCRIPT = """
import json, os, sys
out = {"argv": sys.argv, "home": os.environ.get("ASXBOT_HOME"),
       "release": os.environ.get("ASXBOT_RELEASE"), "path0": sys.path[0],
       "pythonpath": os.environ.get("PYTHONPATH", "")}
open(os.environ["SHIM_OUT"], "w", encoding="utf-8").write(json.dumps(out))
sys.exit(7)
"""


def _release(base: Path, name: str, script: str = SCRIPT) -> Path:
    rel = base / "releases" / name
    (rel / "scripts").mkdir(parents=True)
    (rel / "src").mkdir()
    (rel / "scripts" / "probe.pyw").write_text(script, encoding="utf-8")
    return rel


def _run_shim(tmp_path, base: Path, home: Path, args=()):
    shim = base / "bin" / "probe.pyw"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text(R.shim_source("probe", home), encoding="utf-8")
    out = tmp_path / "out.json"
    env = {**os.environ, "ASXBOT_LOCAL": str(base), "SHIM_OUT": str(out)}
    env.pop("PYTHONPATH", None)
    r = proc.run([sys.executable, str(shim), *args], env=env, capture_output=True, text=True,
                 timeout=60)  # fmt: skip
    return r, out


def test_the_shim_runs_current_with_home_set_and_logs_which_release(tmp_path):
    base, home = tmp_path / "local", tmp_path / "checkout"
    home.mkdir()
    rel = _release(base, "20260925-1900-abcdef0123")
    (base / "releases" / "CURRENT").write_text("20260925-1900-abcdef0123\n", encoding="utf-8")
    r, out = _run_shim(tmp_path, base, home, ["--x"])
    assert r.returncode == 7, r.stderr  # the script's own exit code comes through
    got = json.loads(out.read_text(encoding="utf-8"))
    assert got["home"] == str(home) and got["release"] == str(rel)
    assert got["path0"] == str(rel / "src") and got["pythonpath"].startswith(str(rel / "src"))
    assert got["argv"][1:] == ["--x"] and got["argv"][0].endswith("probe.pyw")
    log = (base / "logs" / "releases.log").read_text(encoding="utf-8")
    assert "probe: running from 20260925-1900-abcdef0123" in log


def test_the_shim_fails_loudly_without_a_current_release(tmp_path):
    base, home = tmp_path / "local", tmp_path / "checkout"
    home.mkdir()
    r, out = _run_shim(tmp_path, base, home)
    assert r.returncode == 1 and not out.exists()
    assert "FAILED: no CURRENT" in (base / "logs" / "releases.log").read_text(encoding="utf-8")
    (base / "releases").mkdir(parents=True, exist_ok=True)
    (base / "releases" / "CURRENT").write_text("gone\n", encoding="utf-8")
    r, _ = _run_shim(tmp_path, base, home)
    assert r.returncode == 1
    assert "does not exist" in (base / "logs" / "releases.log").read_text(encoding="utf-8")


def test_the_shim_template_names_every_task_and_imports_no_asxbot():
    src = R.shim_source("arena_warmup", Path(r"G:\My Drive\asx-bot"))
    compile(src, "shim", "exec")
    assert "import asxbot" not in src and "from asxbot" not in src
    assert 'HOME = r"G:\\My Drive\\asx-bot"' in src
    for name in R.TASK_SCRIPTS:
        assert (REPO / "scripts" / f"{name}.pyw").exists(), name


def test_describe_says_checkout_or_release(tmp_path, monkeypatch):
    monkeypatch.delenv(R.HOME_ENV, raising=False)
    assert R.describe().startswith("the checkout ")
    info = {"commit": "271bb61d1f0000", "subject": "x"}
    monkeypatch.setattr(R, "running_from", lambda: tmp_path)
    (tmp_path / "RELEASE.json").write_text(json.dumps(info), encoding="utf-8")
    monkeypatch.setenv(R.HOME_ENV, r"G:\My Drive\asx-bot")
    line = R.describe()
    assert line.startswith(f"release {tmp_path.name} (commit 271bb61)")
    assert "settings and data" in line


# -- the launchers pass the release to their children ---------------------------------------
def test_the_launchers_pass_home_and_the_release_src_to_their_children():
    for name in ("arena_warmup", "arena_evening", "chat", "arena_filter_cost",
                 "ibkr_fetch_history"):  # fmt: skip
        src = (REPO / "scripts" / f"{name}.pyw").read_text(encoding="utf-8")
        assert 'os.environ.get("ASXBOT_HOME")' in src, name
        assert '"PYTHONPATH": str(RELEASE / "src")' in src, name
        assert '"ASXBOT_RELEASE": str(RELEASE)' in src, name
    src = (REPO / "scripts" / "ibgateway.pyw").read_text(encoding="utf-8")
    assert 'sys.path.insert(0, str(RELEASE / "src"))' in src
    assert 'template = RELEASE / "scripts" / "ibc" / "config.ini"' in src


# -- deploy.py: export, pointers, prune (the suite itself is not re-run here) ---------------
def _deploy_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("deploy", REPO / "scripts" / "deploy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_export_is_the_commit_not_the_working_tree(tmp_path):
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout (the suite is running inside an export)")
    d = _deploy_module()
    dest = tmp_path / "export"
    d.export("HEAD", dest)
    assert (dest / "config.yaml").exists() and (dest / "src" / "asxbot" / "cli.py").exists()
    assert not (dest / "data").exists() and not (dest / ".env").exists()


def test_pointers_shims_and_prune(tmp_path, monkeypatch):
    d = _deploy_module()
    base = tmp_path / "local" / "releases"
    base.mkdir(parents=True)
    monkeypatch.setenv("ASXBOT_LOCAL", str(tmp_path / "local"))
    for i in range(8):
        (base / f"2026092{i}-000000-{'a' * 10}").mkdir()
    d.set_pointer("CURRENT", "20260927-000000-aaaaaaaaaa", base)
    d.set_pointer("PREVIOUS", "20260920-000000-aaaaaaaaaa", base)
    d.prune(base)
    left = sorted(p.name for p in base.iterdir() if p.is_dir())
    assert "20260920-000000-aaaaaaaaaa" in left and "20260927-000000-aaaaaaaaaa" in left
    assert len(left) == d.KEEP + 1  # the newest KEEP, plus PREVIOUS
    changed = d.write_shims(REPO, tmp_path / "local")
    assert sorted(changed) == sorted(f"{n}.pyw" for n in R.TASK_SCRIPTS)
    assert d.write_shims(REPO, tmp_path / "local") == []  # unchanged: not rewritten
    assert R.current_release(base) == base / "20260927-000000-aaaaaaaaaa"


# -- the market-hours lock ----------------------------------------------------------------
@pytest.mark.parametrize(
    "when, locked",
    [
        (datetime(2026, 9, 25, 7, 24, tzinfo=SYD), False),
        (datetime(2026, 9, 25, 7, 25, tzinfo=SYD), True),
        (datetime(2026, 9, 25, 12, 0, tzinfo=SYD), True),
        (datetime(2026, 9, 25, 19, 24, tzinfo=SYD), True),
        (datetime(2026, 9, 25, 19, 25, tzinfo=SYD), False),
        (datetime(2026, 9, 26, 12, 0, tzinfo=SYD), False),  # Saturday
    ],
)
def test_the_lock_window(when, locked):
    assert L.in_market_lock(when, lambda d: True) is locked


def _book(data_dir: Path, name: str, positions=None, orders=None):
    d = data_dir / "arena" / "accounts"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps({
        "name": name, "positions": positions or {}, "orders": orders or {},
    }), encoding="utf-8")  # fmt: skip


def test_a_restart_is_refused_in_hours_unless_forced_with_a_reason(tmp_path):
    _book(tmp_path, "flat__bot")
    noon = datetime(2026, 9, 25, 12, 0, tzinfo=SYD)
    with pytest.raises(L.Locked, match="07:25 and 19:25"):
        L.check_restart(tmp_path, noon, is_trading_day=lambda d: True)
    with pytest.raises(L.Locked):
        L.check_restart(tmp_path, noon, force=True, reason="  ", is_trading_day=lambda d: True)
    note = L.check_restart(tmp_path, noon, force=True, reason="Gateway fix",
                           is_trading_day=lambda d: True)  # fmt: skip
    assert note.startswith("FORCED") and "Gateway fix" in note
    evening = datetime(2026, 9, 25, 20, 0, tzinfo=SYD)
    assert L.check_restart(tmp_path, evening, is_trading_day=lambda d: True).startswith("clear")


def test_a_restart_is_never_allowed_with_a_position_or_working_order_open(tmp_path):
    _book(tmp_path, "dt__bot", positions={"BHP": {"qty": 100}})
    evening = datetime(2026, 9, 25, 20, 0, tzinfo=SYD)
    with pytest.raises(L.Locked, match="never restarted with one open"):
        L.check_restart(tmp_path, evening, force=True, reason="x", is_trading_day=lambda d: True)
    _book(tmp_path, "dt__bot")
    _book(tmp_path, "v2__agent", orders={"ARN-1": {"status": "pending_fill", "side": "buy",
                                                    "ticker": "AAA"}})  # fmt: skip
    with pytest.raises(L.Locked, match="ARN-1 buy AAA"):
        L.check_restart(tmp_path, evening, is_trading_day=lambda d: True)
    _book(tmp_path, "v2__agent", orders={"ARN-1": {"status": "filled"}})
    assert L.check_restart(tmp_path, evening, is_trading_day=lambda d: True).startswith("clear")


def test_the_rule_is_written_in_claude_md():
    text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    assert "07:25" in text and "19:25" in text and "scripts/watcher.py" in text
    assert "releases" in text and "deploy.py" in text


# -- the real %LOCALAPPDATA% (asxbot.localdir) ---------------------------------------------
def test_the_share_path_maps_a_drive_letter_and_the_override_wins(tmp_path, monkeypatch):
    from asxbot import localdir as LD

    got = LD._share_path(Path(r"C:\Users\Richa\AppData\Local"))
    assert got is not None and got.as_posix() == "//localhost/C$/Users/Richa/AppData/Local"
    assert LD._share_path(Path("//server/share/x")) is None
    monkeypatch.setenv(LD.ENV, str(tmp_path))
    assert LD.real_local_appdata() == tmp_path and LD.asx_local() == tmp_path / "asx-bot"
    monkeypatch.delenv(LD.ENV)
    LD._cache.clear()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "plain"))
    got = LD.real_local_appdata()
    # Outside the Claude container the plain path stays; inside it (a Claude Code shell,
    # where a Temp path is itself virtualised) the share's path is used. Either way the
    # probe marker is cleaned up and the answer is cached.
    assert got in (tmp_path / "plain", LD._share_path(tmp_path / "plain"))
    assert not list((tmp_path / "plain" / "asx-bot").glob(".realpath-probe-*"))
    assert LD.real_local_appdata() == got
    LD._cache.clear()
