"""OpenRouter API との通信だけを担当する薄いクライアント.

LLM の呼び出しは必ずここを通す (各ベンダーの API は直接使わない)。
"""

from __future__ import annotations

from dataclasses import dataclass

import requests

API_BASE = "https://openrouter.ai/api/v1"
APP_TITLE = "Decision Table Experiment Tool"
APP_URL = "https://localhost/decision-table-experiment-tool"


class OpenRouterError(Exception):
    """OpenRouter 呼び出しの失敗 (通信エラー・APIエラー・応答不正)."""


@dataclass
class ChatResult:
    """1回の chat completion の結果 (本文 + トークン使用量)."""

    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class OpenRouterClient:
    def __init__(self, api_key: str = "", timeout: int = 180) -> None:
        self.api_key = api_key
        self.timeout = timeout

    # ------------------------------------------------------------------
    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "HTTP-Referer": APP_URL,
            "X-Title": APP_TITLE,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @staticmethod
    def _error_message(response: requests.Response) -> str:
        """エラーレスポンスから読みやすいメッセージを作る."""
        detail = ""
        try:
            body = response.json()
            error = body.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message", ""))
            elif isinstance(error, str):
                detail = error
        except ValueError:
            detail = response.text[:300]
        return f"HTTP {response.status_code}: {detail or 'OpenRouter からエラーが返されました'}"

    # ------------------------------------------------------------------
    def list_models(self) -> list[dict]:
        """利用可能なモデル一覧を取得する (GET /models)."""
        try:
            response = requests.get(
                f"{API_BASE}/models", headers=self._headers(), timeout=30
            )
        except requests.RequestException as exc:
            raise OpenRouterError(f"モデル一覧の取得に失敗しました: {exc}") from exc

        if response.status_code != 200:
            raise OpenRouterError(f"モデル一覧の取得に失敗しました: {self._error_message(response)}")

        try:
            data = response.json().get("data")
        except ValueError as exc:
            raise OpenRouterError("モデル一覧の応答を解析できませんでした") from exc

        if not isinstance(data, list) or not data:
            raise OpenRouterError("モデル一覧が空でした")
        return data

    def chat(self, model: str, prompt: str, temperature: float | None = None) -> ChatResult:
        """1回の chat completion を実行し、本文テキストとトークン使用量を返す."""
        if not self.api_key:
            raise OpenRouterError(
                "OPENROUTER_API_KEY が設定されていません。.env を確認してください。"
            )

        payload: dict = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if temperature is not None:
            payload["temperature"] = temperature

        try:
            response = requests.post(
                f"{API_BASE}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise OpenRouterError(f"通信に失敗しました: {exc}") from exc

        if response.status_code != 200:
            raise OpenRouterError(self._error_message(response))

        try:
            body = response.json()
        except ValueError as exc:
            raise OpenRouterError("応答を解析できませんでした") from exc

        if isinstance(body.get("error"), dict):
            raise OpenRouterError(str(body["error"].get("message", "不明なエラー")))

        choices = body.get("choices") or []
        if not choices:
            raise OpenRouterError("応答に choices が含まれていません")

        content = (choices[0].get("message") or {}).get("content") or ""
        if isinstance(content, list):  # マルチパート応答への保険
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if not content.strip():
            raise OpenRouterError("応答が空でした")

        usage = body.get("usage") or {}
        return ChatResult(
            content=content.strip(),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
        )
