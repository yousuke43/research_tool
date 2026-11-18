"""成功した抽出・生成結果の再利用 (再実行時に成功済みの処理をスキップする).

キーは「処理の種類 + モデルID + 実際に送ったプロンプト全文 + 生成方式など」のハッシュ。
プロンプト全文には要求仕様・プロンプトテンプレート・抽出項目・共通DT枠組みが含まれるので、
どれか1つでも違えば別のキーになり、以前の結果は使われない。

保存先: outputs/_cache/<stage>/<sha256>.json (成功した結果だけを保存する)
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path

from utils import config_manager

STAGE_EXTRACT = "extract"
STAGE_CONVENTIONAL = "generate_conventional"
STAGE_FRAMEWORK = "generate_framework"

_lock = threading.Lock()


def _cache_dir() -> Path:
    return config_manager.CACHE_DIR


def make_key(stage: str, **parts: object) -> str:
    payload = json.dumps({"stage": stage, **parts}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load(stage: str, key: str) -> dict | None:
    path = _cache_dir() / stage / f"{key}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save(stage: str, key: str, data: dict) -> None:
    """失敗しても実験自体は止めない (キャッシュは補助機能)."""
    directory = _cache_dir() / stage
    record = dict(data)
    record.setdefault("cached_at", datetime.now().isoformat(timespec="seconds"))
    try:
        with _lock:
            directory.mkdir(parents=True, exist_ok=True)
            tmp = directory / f"{key}.tmp"
            tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(directory / f"{key}.json")
    except OSError:
        pass
