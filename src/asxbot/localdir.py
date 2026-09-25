"""%LOCALAPPDATA% as the rest of Windows sees it.

A process started inside the Claude desktop app's container (a Claude Code shell, and
everything it runs) sees a VIRTUALISED %LOCALAPPDATA%: its writes land in
%LOCALAPPDATA%\\Packages\\Claude_...\\LocalCache\\Local, and a file it once wrote there shadows
the real one on every later read. Found on 25 Sep 2026 twice: the watcher's log looked
stale from inside a job (the real one was fine, read through \\\\localhost\\C$), and the first
release export "deployed" into the container's copy, where no scheduled task would ever
find it. The scheduled tasks run outside the container and see the real folder.

`real_local_appdata` writes a marker through the plain path and looks for it through the
administrative share, which the container does not virtualise. If it is not there, the
share's path is used for everything under %LOCALAPPDATA% (releases, shims, logs, the Gateway
supervisor's state). Probed once per process; ASXBOT_LOCAL_APPDATA overrides it.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

ENV = "ASXBOT_LOCAL_APPDATA"
_cache: dict[str, Path] = {}


def _plain() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"))


def _share_path(plain: Path) -> Path | None:
    s = str(plain)
    if len(s) < 3 or s[1] != ":" or s[2] not in ("\\", "/"):
        return None  # not a drive-letter path: nothing to map through the share
    return Path(f"\\\\localhost\\{s[0].upper()}$" + s[2:])


def real_local_appdata() -> Path:
    override = os.environ.get(ENV)
    if override:
        return Path(override)
    plain = _plain()
    key = str(plain)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    result = plain
    unc = _share_path(plain)
    if unc is not None:
        marker = plain / "asx-bot" / f".realpath-probe-{os.getpid()}"
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(time.time()), encoding="utf-8")
            seen = (unc / "asx-bot" / marker.name).exists()
            try:
                marker.unlink()
            except OSError:
                pass
            if not seen and unc.exists():
                result = unc
                from asxbot.log import get_logger

                get_logger("asxbot.localdir").warning(
                    "this process sees a virtualised %%LOCALAPPDATA%% (the Claude desktop app's "
                    "container); using %s for asx-bot's local files instead", unc,
                )  # fmt: skip
        except OSError:
            result = plain
    _cache[key] = result
    return result


def asx_local() -> Path:
    """%LOCALAPPDATA%\\asx-bot, real."""
    return real_local_appdata() / "asx-bot"


__all__ = ["ENV", "asx_local", "real_local_appdata"]
