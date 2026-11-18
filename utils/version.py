"""実行したツールのバージョン (再現性のため results.json に記録する)."""

from __future__ import annotations

import subprocess
import sys
from functools import lru_cache

from utils.config_manager import ROOT_DIR


def _git(*args: str) -> str:
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW (コンソールを出さない)
    result = subprocess.run(
        ["git", *args], cwd=ROOT_DIR, capture_output=True, text=True, timeout=5, **kwargs
    )
    return result.stdout.strip() if result.returncode == 0 else ""


@lru_cache(maxsize=1)
def get_tool_version() -> dict:
    """{"commit": "4bd9dac...", "dirty": True} のような dict. git が使えなければ空の値."""
    try:
        commit = _git("rev-parse", "HEAD")
        dirty = bool(_git("status", "--porcelain", "--untracked-files=no")) if commit else False
    except (OSError, subprocess.SubprocessError):
        commit, dirty = "", False
    return {"commit": commit, "dirty": dirty, "python": sys.version.split()[0]}
