"""設定の管理.

- .env から OpenRouter API キーを読む
- config/settings.json に「選択した抽出モデル / 生成モデル」を保存・復元する

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

DEFAULT_SETTINGS = {
    "extraction_model": "",
    "generation_models": [],
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
    return settings


def save_settings(settings: dict) -> None:
    """選択したモデルを config/settings.json へ保存する."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "extraction_model": settings.get("extraction_model", ""),
        "generation_models": list(settings.get("generation_models", [])),
    }
    SETTINGS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
