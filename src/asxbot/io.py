"""Atomic Parquet/CSV/text writes. Google Drive syncs the repo, so never leave a
half-written file where a reader (or the sync client) can pick it up.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pandas as pd

_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
}  # fmt: skip


def safe_stem(code: str) -> str:
    """ASX codes like PRN or CON cannot be file names on Windows. Suffix them."""
    return f"{code}_" if code.upper() in _WINDOWS_RESERVED else code


def _tmp_beside(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.stem + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    return tmp


def write_parquet_atomic(df: pd.DataFrame, path: Path) -> Path:
    path = Path(path)
    tmp = _tmp_beside(path)
    try:
        df.to_parquet(tmp, index=True)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


def write_csv_atomic(df: pd.DataFrame, path: Path, **kw) -> Path:
    path = Path(path)
    tmp = _tmp_beside(path)
    try:
        df.to_csv(tmp, **kw)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


def write_text_atomic(text: str, path: Path) -> Path:
    path = Path(path)
    tmp = _tmp_beside(path)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return path
