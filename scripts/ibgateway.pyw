"""IB Gateway through IBC, with no window of its own, restarted as IBC's scripts would.

Started ONLY by the scheduled task "ASXBot IB Gateway" (scripts/register_ibgateway_tasks.ps1)
as

    C:\\venvs\\asx-bot\\Scripts\\pythonw.exe "G:\\My Drive\\asx-bot\\scripts\\ibgateway.pyw"

and that task is started by the supervisor (src/asxbot/ibkr/supervisor.py) when Gateway is
down. A Gateway started from a terminal or a Claude session dies with it: on 25 Sep 2026 a
Claude desktop update at 08:58:29 took the Gateway it had started down with it.

Each start:
  * copies scripts/ibc/config.ini to C:\\IBC\\asxbot\\config.ini (IBC's settings: live, the
    read-only API, reject unknown API clients, keep this session primary, 23:45 restart);
  * reads Rick's IBKR login from Windows Credential Manager (asxbot.ibkr.credentials; he
    stores it once with ibkr_login_setup.cmd). With it IBC logs in by itself and only the
    IBKR Mobile approval is left; without it Gateway opens its login window for Rick;
  * renames Gateway's own ibgateway.exe to ibgateway1.exe, as IBC's StartIBC.bat does, so
    Gateway's built-in restarter cannot start a second Gateway outside IBC;
  * runs IBC's Java command (asxbot.ibkr.ibc.command) and copies its output, line by line,
    into ibgateway.log in the local logs folder - with the login blanked out of every line
    (ibc.scrub);
  * keeps what the login has reached (starting, login_dialog, awaiting_2fa, logged_in) in
    %LOCALAPPDATA%\\asx-bot\\ibgateway\\launcher.json, for the supervisor.
When the Java process ends it starts again if IBC's rules say so (ibc.after_exit: the daily
auto-restart, a login dialog that never came, or an unanswered phone approval - at most 3 of
those in a row), otherwise it exits and leaves the rest to the supervisor.

Nothing here reads, stores or sends anything about orders. The login is never printed.
"""

import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

# The release this file is part of (scripts/deploy.py, asxbot.release), and the checkout
# holding config.yaml (ASXBOT_HOME, set by the task shim); the same folder when run from the
# checkout. IBC's settings template comes from the release, config.yaml from the checkout.
RELEASE = Path(__file__).resolve().parents[1]
REPO = Path(os.environ.get("ASXBOT_HOME") or RELEASE)
MAX_TWOFA_TIMEOUTS = 3
FAILURES = Path(r"C:\venvs\asx-bot\task-failures.log")


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def main() -> int:
    if not (REPO / "config.yaml").exists():
        FAILURES.parent.mkdir(parents=True, exist_ok=True)
        with open(FAILURES, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp()}  ABORTED (ibgateway): {REPO} is not available. Google Drive "
                     "is not mounted, which usually means nobody is logged in.\n")  # fmt: skip
        return 1
    os.chdir(REPO)  # 26 Sep 2026: never the folder the task started in
    sys.path.insert(0, str(RELEASE / "src"))
    from asxbot import proc as hidden
    from asxbot.ibkr import credentials
    from asxbot.ibkr import ibc
    from asxbot.ibkr.supervisor import (
        LAUNCHER_STATE, gateway_log, launcher_alive, read_json, write_json,
    )  # fmt: skip

    paths = ibc.Paths.default()
    log = gateway_log()

    prior = read_json(LAUNCHER_STATE)
    if prior.get("pid") and prior.get("pid") != os.getpid() and launcher_alive(prior):
        log(f"another launcher (pid {prior['pid']}) is already running; this one exits")
        return 0

    state = {"pid": os.getpid(), "started_at": stamp(), "phase": "starting",
             "phase_at": stamp(), "java_pid": None, "mode": None, "restart": False,
             "twofa_timeouts": 0, "exit": None}  # fmt: skip

    def save(**kw):
        state.update(kw)
        write_json(LAUNCHER_STATE, state)

    save()
    template = RELEASE / "scripts" / "ibc" / "config.ini"
    twofa = 0
    while True:
        paths.config.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(template, paths.config)
        exe = paths.gateway / "ibgateway.exe"
        if exe.exists():
            spare = paths.gateway / "ibgateway1.exe"
            try:
                if spare.exists():
                    spare.unlink()
                exe.rename(spare)
                log("renamed ibgateway.exe to ibgateway1.exe (as IBC does): Gateway's own "
                    "restarter can no longer start a Gateway outside IBC")  # fmt: skip
            except OSError as e:
                log(f"could not rename ibgateway.exe ({e}); carrying on")

        login = credentials.load()
        restart_dir = ibc.autorestart_user_dir(paths.settings)
        mode = "auto" if login else "manual"
        try:
            cmd = ibc.command(paths, login, restart_dir=restart_dir)
        except FileNotFoundError as e:
            log(f"cannot start Gateway: {e}")
            save(phase="exited", phase_at=stamp(), exit={"code": None, "reason": str(e),
                                                          "at": stamp(), "gave_up_2fa": False})
            return 2
        log(f"=== starting IB Gateway via IBC {ibc.IBC_VERSION} (from {RELEASE.name}): login "
            f"{'from Credential Manager' if login else 'NOT stored - Gateway will ask Rick'}"
            f"{', auto-restart (no new login needed)' if restart_dir else ''} ===")  # fmt: skip
        log("command: " + ibc.redacted(cmd, login))
        try:
            child = hidden.popen(cmd, cwd=str(paths.settings), stdin=hidden.DEVNULL,
                                 stdout=hidden.PIPE, stderr=hidden.STDOUT)  # fmt: skip
        except OSError as e:
            log(f"could not start Java: {e}")
            save(phase="exited", phase_at=stamp(), exit={"code": None, "reason": str(e),
                                                          "at": stamp(), "gave_up_2fa": False})
            return 3
        del cmd
        save(java_pid=child.pid, mode=mode, restart=bool(restart_dir), phase="starting",
             phase_at=stamp())  # fmt: skip
        for raw in child.stdout:
            line = ibc.scrub(raw.decode("utf-8", errors="replace").rstrip("\r\n"), login)
            log(line)
            ph = ibc.phase_of(line)
            if ph and ph != state["phase"]:
                save(phase=ph, phase_at=stamp())
                if ph == "logged_in":
                    twofa = 0
        code = child.wait()
        if code == ibc.E_2FA_TIMED_OUT:
            twofa += 1
        again = ibc.autorestart_user_dir(paths.settings)
        what, why = ibc.after_exit(code, again, twofa, MAX_TWOFA_TIMEOUTS)
        log(f"=== Gateway's Java process exited with {code}: {why} ===")
        if what == ibc.RESTART:
            save(phase="starting", phase_at=stamp(), java_pid=None, twofa_timeouts=twofa)
            time.sleep(2)
            continue
        save(phase="exited", phase_at=stamp(), java_pid=None, twofa_timeouts=twofa,
             exit={"code": code, "reason": why, "at": stamp(),
                   "gave_up_2fa": code == ibc.E_2FA_TIMED_OUT})  # fmt: skip
        return 0


if __name__ == "__main__":
    sys.exit(main())
