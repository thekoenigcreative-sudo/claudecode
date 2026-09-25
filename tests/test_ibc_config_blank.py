"""scripts/ibc/config.ini never holds Rick's IBKR login (26 Sep 2026).

The launcher hands the login to IBC from Windows Credential Manager at each start
(docs/ibgateway.md, "Where the login goes"). The file is committed and copied into every
release, so a username or password typed into it would be in git history for good.
"""

import re
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "scripts" / "ibc" / "config.ini"
KEYS = ("IbLoginId", "IbPassword")


def _settings(key: str) -> list[str]:
    """Every value given to `key` on a non-comment line, however it is spaced or cased."""
    pat = re.compile(rf"^\s*{key}\s*=(.*)$", re.I)
    out = []
    for line in CONFIG.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = pat.match(line)
        if m:
            out.append(m.group(1))
    return out


def test_the_login_lines_are_there_once_and_blank():
    for key in KEYS:
        values = _settings(key)
        assert len(values) == 1, f"{key} is set {len(values)} times in {CONFIG.name}"
        assert values[0].strip() == "", f"{key} holds a value in {CONFIG.name}"
