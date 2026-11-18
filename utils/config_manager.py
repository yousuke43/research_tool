"""設定の管理.

- .env から OpenRouter API キーを読む
- config/settings.json に「選択した抽出モデル / 生成モデル / 生成方式と生成設定」を保存・復元する

アプリ内のパス定義もここに集約している。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

# プロジェクトルート (このファイルの1つ上のディレクトリ)
ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT_DIR / "config"
PROMPTS_DIR = ROOT_DIR / "prompts"
OUTPUTS_DIR = ROOT_DIR / "outputs"
DATA_DIR = ROOT_DIR / "data"

SETTINGS_PATH = CONFIG_DIR / "settings.json"
MODELS_CACHE_PATH = CONFIG_DIR / "models_cache.json"
# 成功した抽出・生成結果の再利用用キャッシュ (services/result_cache.py)
CACHE_DIR = OUTPUTS_DIR / "_cache"
# 確認・承認用に保存する共通DT枠組み (仕様ごとのフォルダ)
FRAMEWORKS_DIR = ROOT_DIR / "frameworks"

# 生成方式
# 従来方式: LLM が DT 全体を生成 / 新方式: 共通DT枠組み + 動作結果補完
from models.experiment import METHOD_COMMON_FRAMEWORK, METHOD_CONVENTIONAL  # noqa: E402
GENERATION_METHODS = {
    METHOD_CONVENTIONAL: "従来方式 (LLMがDT全体を生成)",
    METHOD_COMMON_FRAMEWORK: "新方式 (共通DT枠組み＋動作結果補完)",
}

DEFAULT_SETTINGS = {
    "extraction_model": "",
    "generation_models": [],
    "generation_method": METHOD_CONVENTIONAL,
    # 新方式: 全組合せを自動生成する条件数の上限 (2^n 通り)
    "framework_max_conditions": 6,
    # 新方式: 1回のリクエストで補完させるルール数 (これを超えるとバッチ分割)
    "framework_batch_size": 64,
    # 新方式の出力上限 (推論トークンを含む)。0 = 指定しない
    "framework_max_tokens": 32000,
    # 従来方式の出力上限。0 = 指定しない (従来どおり)
    "conventional_max_tokens": 0,
    # 抽出の出力上限。0 = 指定しない (従来どおり)
    "extraction_max_tokens": 0,
    # 成功済みの抽出・生成結果を再利用する (失敗した処理だけ再実行)
    "reuse_cached_results": True,
}

_INT_SETTINGS = {
    # key: (最小, 最大)
    "framework_max_conditions": (1, 12),
    "framework_batch_size": (1, 4096),
    "framework_max_tokens": (0, 1_000_000),
    "conventional_max_tokens": (0, 1_000_000),
    "extraction_max_tokens": (0, 1_000_000),
}


def load_env() -> None:
    """プロジェクト直下の .env を読み込む (既存の環境変数は上書きしない)."""
    load_dotenv(ROOT_DIR / ".env")


def get_api_key() -> str:
    """OpenRouter の API キー. 未設定なら空文字を返す."""
    return os.getenv("OPENROUTER_API_KEY", "").strip()


def load_settings() -> dict:
    """config/settings.json を読む. 無い・壊れている場合は既定値を返す."""
    settings = dict(DEFAULT_SETTINGS)
    if not SETTINGS_PATH.exists():
        return settings
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return settings

    if isinstance(data.get("extraction_model"), str):
        settings["extraction_model"] = data["extraction_model"]
    if isinstance(data.get("generation_models"), list):
        settings["generation_models"] = [m for m in data["generation_models"] if isinstance(m, str)]
    if data.get("generation_method") in GENERATION_METHODS:
        settings["generation_method"] = data["generation_method"]
    for key, (low, high) in _INT_SETTINGS.items():
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            settings[key] = max(low, min(high, value))
    if isinstance(data.get("reuse_cached_results"), bool):
        settings["reuse_cached_results"] = data["reuse_cached_results"]
    return settings


def save_settings(settings: dict) -> None:
    """選択したモデルを config/settings.json へ保存する."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "extraction_model": settings.get("extraction_model", ""),
        "generation_models": list(settings.get("generation_models", [])),
    }
    for key, default in DEFAULT_SETTINGS.items():
        if key not in payload:
            payload[key] = settings.get(key, default)
    SETTINGS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
