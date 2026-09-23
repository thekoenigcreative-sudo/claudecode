"""Every child process this project starts goes through here, and none of them opens a window.

The watcher, its watchdog and the evening routine run under pythonw, with no console
(TRACKER #19, #23). A child started without CREATE_NO_WINDOW gets a console of its own, and
Windows shows it: a window that appears over whatever Rick is doing, and that closes the
child if someone closes it. On 23 Sep a closed window killed the watcher.

So there is one way to start a process - `run` or `popen` below - and it always adds
CREATE_NO_WINDOW. tests/test_proc.py reads every module under src/ and scripts/ and fails if
anything else starts a process (subprocess, os.system, os.startfile and the rest).
"""

from __future__ import annotations

import subprocess
import sys

# 0x08000000; only Windows has it, and only Windows needs it.
CREATE_NO_WINDOW = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if (sys.platform == "win32") else 0
)

PIPE = subprocess.PIPE
STDOUT = subprocess.STDOUT
DEVNULL = subprocess.DEVNULL
TimeoutExpired = subprocess.TimeoutExpired
CompletedProcess = subprocess.CompletedProcess


def _hidden(kw: dict) -> dict:
    kw["creationflags"] = int(kw.get("creationflags", 0)) | CREATE_NO_WINDOW
    return kw


def run(args, **kw) -> subprocess.CompletedProcess:
    """subprocess.run, windowless."""
    return subprocess.run(args, **_hidden(kw))


def popen(args, **kw) -> subprocess.Popen:
    """subprocess.Popen, windowless."""
    return subprocess.Popen(args, **_hidden(kw))
