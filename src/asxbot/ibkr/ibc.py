"""Starting IB Gateway through IBC, the way IBC's own StartIBC.bat does, without cmd.exe.

IBC (github.com/IbcAlpha/IBC, installed in C:\\IBC; version and checksum in docs/ibgateway.md)
runs Gateway inside its own Java process: it fills in the login dialog, handles Gateway's
dialogs (an existing session elsewhere, the auto-restart, incoming API connections) and sets
the API options from the config file (scripts/ibc/config.ini, copied to C:\\IBC\\asxbot).

Why not StartIBC.bat: it passes the login through cmd.exe, where a password holding
% ! ^ & or " is mangled or cut short, and its window is a console. So `command` below builds
the same java command line the script builds - classpath, VM options, the auto-restart
option, IBC's entry point - and scripts/ibgateway.pyw starts it through asxbot.proc (no
window) and loops as the script does (`after_exit`).

The login: IBC takes it only as two program arguments (or as plain text in its config file,
which is not used). So while Gateway runs, the username and password are in the Java
process's command line, readable by Rick's own Windows account and by administrators, like
IBC's own scripts. They are never written to a file or a log: `redacted` is the only form
of the command anything here prints, and the launcher's log line uses it.
"""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

IBC_VERSION = "3.24.2"
IBC_ZIP_SHA256 = "ba8e95f61f3c7c252620baef41e550c6aab98f6034179156e11a9a1623fdd892"
ENTRY_POINT = "ibcalpha.ibc.IbcGateway"

# IBC's exit codes (scripts/StartIBC.bat)
E_2FA_TIMED_OUT = 1111  # the IBKR Mobile approval was not given in time
E_LOGIN_DIALOG_TIMEOUT = 1112  # Gateway never showed its login dialog

# What the launcher's loop does after Gateway's Java process ends (`after_exit`)
RESTART = "restart"  # start again at once: IBC's own rules say so
STOP = "stop"  # a normal exit, or one a restart cannot help: the supervisor takes over


def _local() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")


@dataclass(frozen=True)
class Paths:
    gateway: Path  # IB Gateway's install folder (jars, .install4j, ibgateway.vmoptions)
    settings: Path  # Gateway's settings folder (jts.ini and the per-user folder)
    ibc: Path  # IBC's install folder (IBC.jar)
    config: Path  # the IBC config file used (a copy of scripts/ibc/config.ini)

    @classmethod
    def default(cls) -> Paths:
        gw = Path(os.environ.get("ASXBOT_IBGATEWAY_DIR") or _local() / "Programs" / "ibgateway")
        ibc = Path(os.environ.get("ASXBOT_IBC_DIR") or r"C:\IBC")
        return cls(gateway=gw, settings=gw, ibc=ibc, config=ibc / "asxbot" / "config.ini")


def vm_options(vmoptions_file: Path) -> list[str]:
    """The first token of each non-comment line, as StartIBC.bat reads ibgateway.vmoptions."""
    out = []
    for line in vmoptions_file.read_text(encoding="utf-8", errors="replace").splitlines():
        tok = line.strip().split(" ")[0] if line.strip() else ""
        if tok and not tok.startswith("#"):
            out.append(tok)
    return out


def extra_java_options(install4j: Path) -> list[str]:
    """install4j's javaOptions (what IBC's getExtraJavaOptions.ps1 reads)."""
    conf = install4j / "i4jparams.conf"
    if not conf.exists():
        return []
    m = re.search(r'<variable name="javaOptions"[^>]*value="([^"]*)"',
                  conf.read_text(encoding="utf-8", errors="replace"))  # fmt: skip
    return m.group(1).split() if m else []


def java_exe(p: Paths) -> Path:
    """The Java that came with Gateway, found as StartIBC.bat finds it."""
    i4j = p.gateway / ".install4j"
    for cfg in ("pref_jre.cfg", "inst_jre.cfg"):
        f = i4j / cfg
        if f.exists():
            first = f.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            if first:
                exe = Path(first[0].strip()) / "bin" / "java.exe"
                if exe.exists():
                    return exe
    exe = p.gateway / "jre" / "bin" / "java.exe"
    if exe.exists():
        return exe
    raise FileNotFoundError(f"no java.exe for IB Gateway under {p.gateway}")


def autorestart_user_dir(settings: Path) -> str | None:
    """The per-user folder holding Gateway's `autorestart` file, if exactly one does.

    Gateway writes that file when it restarts itself (23:45 daily); started again with
    -Drestart=<folder> it logs straight back in, with no password and no phone approval.
    StartIBC.bat deletes every copy when there is more than one; so does this."""
    found = [f for f in settings.glob("*/autorestart") if f.is_file()]
    if len(found) == 1:
        return found[0].parent.name
    for f in found:
        try:
            f.unlink()
        except OSError:
            pass
    return None


def command(p: Paths, login=None, session_id: str | None = None,
            restart_dir: str | None = None) -> list[str]:  # fmt: skip
    """The java command line that starts IBC and Gateway (StartIBC.bat's, as a list)."""
    jars = sorted((p.gateway / "jars").glob("*.jar"))
    if not jars:
        raise FileNotFoundError(f"no jars under {p.gateway / 'jars'}: is IB Gateway installed?")
    i4j = p.gateway / ".install4j"
    classpath = ";".join([*map(str, jars), str(i4j / "i4jruntime.jar"), str(p.ibc / "IBC.jar")])
    sid = session_id or f"{secrets.randbelow(10**9)}"
    vm = [
        *vm_options(p.gateway / "ibgateway.vmoptions"),
        "-Dtwslaunch.autoupdate.serviceImpl=com.ib.tws.twslaunch.install4j.Install4jAutoUpdateService",
        "-Dchannel=latest",
        "-Dexe4j.isInstall4j=true",
        "-Dinstall4jType=standalone",
        f"-DjtsConfigDir={p.settings}",
        f"-Dibcsessionid={sid}",
    ]  # fmt: skip
    if restart_dir:
        vm.append(f"-Drestart={restart_dir}")
    args = [str(java_exe(p)), *extra_java_options(i4j), "-cp", classpath, *vm, ENTRY_POINT,
            str(p.config)]  # fmt: skip
    if login is not None:
        args += [login.user, login.password]
    args.append("live")
    return args


def redacted(cmd: list[str], login=None) -> str:
    """The command with the login replaced by ***: the only form that may be printed."""
    secret = {login.user, login.password} if login is not None else set()
    shown = []
    for a in cmd:
        if a in secret:
            shown.append("***")
        elif ";" in a:  # the classpath
            shown.append(f"<{a.count(';') + 1} jars>")
        elif a.startswith("-DjxBrowserKey="):
            shown.append("-DjxBrowserKey=...")
        else:
            shown.append(a)
    return " ".join(shown)


def scrub(line: str, login=None) -> str:
    """A line of IBC's output with the login blanked out, before it goes near a log. IBC
    masks the password itself; this is the second lock, and it covers the username too."""
    if login is not None:
        for secret in (login.password, login.user):
            if secret:
                line = line.replace(secret, "***")
    return line


def after_exit(code: int, restart_dir: str | None, twofa_timeouts: int,
               max_twofa_timeouts: int) -> tuple[str, str]:  # fmt: skip
    """What the launcher does when Gateway's Java process ends, and why (StartIBC.bat's loop,
    with a cap on the phone-approval retries so an unanswered phone is not pinged all night).
    """
    if code == E_2FA_TIMED_OUT:
        if twofa_timeouts >= max_twofa_timeouts:
            return STOP, (f"the phone approval timed out {twofa_timeouts} times in a row; "
                          "stopping until the supervisor tries again")  # fmt: skip
        return RESTART, "the phone approval timed out; logging in again (a new approval)"
    if code == E_LOGIN_DIALOG_TIMEOUT:
        return RESTART, "Gateway did not show its login dialog in time; starting again"
    if restart_dir:
        return RESTART, "Gateway's daily auto-restart; starting again without a new login"
    return STOP, f"Gateway exited with code {code}"


# IBC's own log lines (IBC.jar 3.24.2), as they show where a login has got to.
PHASES = (
    ("awaiting_2fa", re.compile(
        r"Second Factor Authentication initiated|SECOND FACTOR AUTHENTICATION|"
        r"Login frame has now become SecondFactorAuthenticationDialog", re.I)),
    ("logged_in", re.compile(r"Login has completed|Configuration tasks completed", re.I)),
    ("login_dialog", re.compile(r"Login dialog WINDOW_OPENED|detected dialog entitled: "
                                r"(IBKR Gateway|IB Gateway|Login)", re.I)),
    ("existing_session", re.compile(r"Existing session detected", re.I)),
)  # fmt: skip


def phase_of(line: str) -> str | None:
    for name, pat in PHASES:
        if pat.search(line):
            return name
    return None


__all__ = [
    "E_2FA_TIMED_OUT", "E_LOGIN_DIALOG_TIMEOUT", "ENTRY_POINT", "IBC_VERSION", "Paths",
    "RESTART", "STOP", "after_exit", "autorestart_user_dir", "command", "phase_of", "redacted",
    "scrub",
]  # fmt: skip
