"""OpenRouter API との通信だけを担当する薄いクライアント.

LLM の呼び出しは必ずここを通す (各ベンダーの API は直接使わない)。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from services import token_usage

API_BASE = "https://openrouter.ai/api/v1"
APP_TITLE = "Decision Table Experiment Tool"
APP_URL = "https://localhost/decision-table-experiment-tool"


class OpenRouterError(Exception):
    """OpenRouter 呼び出しの失敗 (通信エラー・APIエラー・応答不正)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class KeyLimitError(OpenRouterError):
    """API キーの利用上限・残高不足. 一括実行はこれを受けたら新規リクエストを止める."""


# 上限超過・残高不足を表すメッセージ (小文字で部分一致)。
# 一般的な 403 (モデルへのアクセス拒否・モデレーション等) を誤判定しないよう、
# 403 はこれらの語を含む場合だけ上限超過とみなす。
_KEY_LIMIT_PATTERNS = (
    "key limit exceeded",
    "total limit exceeded",
    "insufficient credits",
    "insufficient balance",
    "insufficient funds",
    "credit limit",
    "out of credits",
    "requires more credits",
    "can only afford",
    "spending limit",
)
_PAYMENT_PATTERNS = ("credit", "balance", "limit", "afford", "payment", "funds", "quota")


def is_key_limit_error(status_code: int | None, message: str) -> bool:
    """エラー応答が「API キーの累積上限・残高不足」によるものかを判定する."""
    text = (message or "").lower()
    if any(p in text for p in _KEY_LIMIT_PATTERNS):
        # 429 の "rate limit exceeded" は一時的なレート制限なので除外する
        return "rate limit" not in text
    if status_code == 402:
        # 402 Payment Required: 本文が空でも残高・上限起因とみなす
        return not text.strip() or any(p in text for p in _PAYMENT_PATTERNS)
    return False


def make_api_error(status_code: int | None, message: str) -> OpenRouterError:
    cls = KeyLimitError if is_key_limit_error(status_code, message) else OpenRouterError
    return cls(message, status_code)


@dataclass
class ChatResult:
    """1回の chat completion の結果 (本文 + トークン使用量)."""

    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    finish_reason: str = ""


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

    def chat(
        self,
        model: str,
        prompt: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict | None = None,
        purpose: str = "",
    ) -> ChatResult:
        """1回の chat completion を実行し、本文テキストとトークン使用量を返す.

        max_tokens: None / 0 の場合は送らない (モデルの既定値に任せる)。
        推論トークンも max_tokens に含まれるモデルがあるので、小さくしすぎないこと。
        purpose: トークン累計 (outputs/token_usage/) の用途欄に記録する名前 (services/token_usage.PURPOSE_*)。
        """
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
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        if response_format:
            payload["response_format"] = response_format

        started = time.perf_counter()
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
            raise make_api_error(response.status_code, self._error_message(response))

        try:
            body = response.json()
        except ValueError as exc:
            raise OpenRouterError("応答を解析できませんでした") from exc

        if isinstance(body.get("error"), dict):
            error = body["error"]
            code = error.get("code") if isinstance(error.get("code"), int) else None
            raise make_api_error(code, str(error.get("message", "不明なエラー")))

        usage = body.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        choices = body.get("choices") or []
        finish_reason = str(choices[0].get("finish_reason") or "") if choices else ""
        content = ((choices[0].get("message") or {}).get("content") or "") if choices else ""
        if isinstance(content, list):  # マルチパート応答への保険
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        reasoning_tokens = int(details.get("reasoning_tokens") or 0) if isinstance(details, dict) else 0
        # 応答が空でもトークンは消費されているので、エラーにする前に累計へ記録する
        token_usage.record_call(
            purpose=purpose,
            model=model,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            reasoning_tokens=reasoning_tokens,
            total_tokens=int(usage.get("total_tokens") or 0),
            cost=usage.get("cost") if isinstance(usage.get("cost"), (int, float)) else None,
            elapsed_sec=time.perf_counter() - started,
            finish_reason=finish_reason,
            status="ok" if choices and str(content).strip() else "empty_response",
        )

        if not choices:
            raise OpenRouterError("応答に choices が含まれていません")

        if not content.strip():
            if finish_reason == "length":
                raise OpenRouterError(
                    "応答が空でした (出力上限 max_tokens に達しました。推論トークンで上限を使い切った"
                    "可能性があります。設定の出力上限を増やしてください)"
                )
            raise OpenRouterError("応答が空でした")

        return ChatResult(
            content=content.strip(),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
            reasoning_tokens=reasoning_tokens,
            finish_reason=finish_reason,
        )
