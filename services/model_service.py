"""OpenRouter のモデル一覧の取得・キャッシュ・検証.

モデル一覧はハードコードせず、常に OpenRouter API から取得する。
取得結果は config/models_cache.json にキャッシュし、起動直後の表示に使う。
"""

from __future__ import annotations

import json
from datetime import datetime

from PySide6.QtCore import QObject, Signal

from services.openrouter_client import OpenRouterClient, OpenRouterError
from utils.config_manager import CONFIG_DIR, MODELS_CACHE_PATH


def _normalize(raw: dict) -> dict:
    """API の生データから、画面表示に必要な項目だけ取り出す."""
    top_provider = raw.get("top_provider") or {}
    pricing = raw.get("pricing") or {}
    model_id = raw.get("id", "")
    return {
        "id": model_id,
        "name": raw.get("name") or model_id,
        "context_length": raw.get("context_length") or top_provider.get("context_length") or 0,
        "prompt_price": str(pricing.get("prompt", "")),
    }


def fetch_models(client: OpenRouterClient) -> list[dict]:
    """OpenRouter からモデル一覧を取得して正規化し、キャッシュへ保存する."""
    models = [_normalize(m) for m in client.list_models() if m.get("id")]
    models.sort(key=lambda m: m["id"])
    save_cache(models)
    return models


def save_cache(models: list[dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "models": models,
    }
    MODELS_CACHE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def load_cached_models() -> list[dict]:
    """キャッシュされたモデル一覧. 無ければ空リスト (エラーにはしない)."""
    if not MODELS_CACHE_PATH.exists():
        return []
    try:
        data = json.loads(MODELS_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    models = data.get("models")
    return models if isinstance(models, list) else []


def find_missing(model_ids: list[str], models: list[dict]) -> list[str]:
    """選択済みモデルのうち、現在の一覧に存在しないものを返す."""
    if not models:
        return []
    available = {m.get("id") for m in models}
    return [mid for mid in model_ids if mid and mid not in available]


def display_name(models: list[dict], model_id: str) -> str:
    """モデルIDから表示名を引く. 見つからなければIDをそのまま返す."""
    for model in models:
        if model.get("id") == model_id:
            return model.get("name") or model_id
    return model_id


class ModelFetchWorker(QObject):
    """モデル一覧取得をバックグラウンドで行うワーカー (GUI を固めないため)."""

    finished = Signal(list)
    failed = Signal(str)

    def __init__(self, client: OpenRouterClient) -> None:
        super().__init__()
        self.client = client

    def run(self) -> None:
        try:
            models = fetch_models(self.client)
        except OpenRouterError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # 想定外でもアプリを落とさない
            self.failed.emit(f"モデル一覧の取得に失敗しました: {exc}")
        else:
            self.finished.emit(models)
