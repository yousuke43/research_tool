"""新方式: 共通DT枠組みを1つの生成モデルに渡し、動作結果だけを補完させる.

ルール数が batch_size を超える場合は複数バッチに分けて順に問い合わせ、最後に統合する。
各バッチの応答は検証し (services/dt_framework.validate_fill_response)、
1バッチでも検証に失敗したら、そのモデルの DT は「形式不正」として扱う
(不完全な DT を正常な結果として扱わない)。

プロンプトは prompts/generate_framework.txt
(プレースホルダ: {requirements} {decision_items} {rule_table} {output_example})。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from services import dt_framework, result_cache
from services.dt_framework import CommonFramework, FrameworkRule
from services.openrouter_client import KeyLimitError, OpenRouterClient, OpenRouterError
from utils.file_manager import load_prompt, render_prompt

PROMPT_NAME = "generate_framework"

ERROR_KIND_API = "api_error"
ERROR_KIND_KEY_LIMIT = "key_limit"
ERROR_KIND_INVALID = "invalid_output"
ERROR_KIND_SKIPPED = "skipped"


@dataclass
class BatchRecord:
    """1バッチ (1リクエスト) 分の記録. results.json にそのまま残す."""

    index: int
    rule_ids: list[str]
    raw_response: str = ""
    elapsed_sec: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    finish_reason: str = ""
    reused_from_cache: bool = False
    api_error: str = ""
    validation_errors: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.api_error and not self.validation_errors

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "rule_ids": self.rule_ids,
            "status": "ok" if self.ok else "error",
            "elapsed_sec": round(self.elapsed_sec, 2),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "finish_reason": self.finish_reason,
            "reused_from_cache": self.reused_from_cache,
            "api_error": self.api_error,
            "validation_errors": self.validation_errors,
            "validation_warnings": self.validation_warnings,
            "raw_response": self.raw_response,
        }


@dataclass
class FillOutcome:
    batches: list[BatchRecord] = field(default_factory=list)
    values: dict = field(default_factory=dict)
    raw_values: dict = field(default_factory=dict)
    markdown: str = ""
    error: str = ""
    error_kind: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def total(self, attr: str) -> int:
        return sum(getattr(b, attr) for b in self.batches)


def build_prompt(
    template: str, requirements: str, framework: CommonFramework, rules: list[FrameworkRule]
) -> str:
    return render_prompt(
        template,
        requirements=requirements,
        decision_items=framework.items.to_json(),
        rule_table=dt_framework.rule_table_markdown(framework, rules),
        output_example=dt_framework.output_example_json(framework, rules),
    )


def fill_framework(
    client: OpenRouterClient,
    model_id: str,
    requirements: str,
    framework: CommonFramework,
    *,
    batch_size: int = 64,
    max_tokens: int = 0,
    reuse_cache: bool = False,
    stop_event: threading.Event | None = None,
    template: str | None = None,
) -> FillOutcome:
    """1モデル分の補完を行う. 例外は送出せず、API エラー・上限超過・形式不正は outcome.error_kind に記録する."""
    template = template if template is not None else load_prompt(PROMPT_NAME)
    outcome = FillOutcome()
    reports = []

    for index, rules in enumerate(dt_framework.split_batches(framework.rules, batch_size), start=1):
        record = BatchRecord(index=index, rule_ids=[r.rule_id for r in rules])
        outcome.batches.append(record)
        if stop_event is not None and stop_event.is_set():
            record.api_error = "APIキーの利用上限に達したため実行を中止しました"
            outcome.error, outcome.error_kind = record.api_error, ERROR_KIND_SKIPPED
            return outcome

        prompt = build_prompt(template, requirements, framework, rules)
        cache_key = result_cache.make_key(
            result_cache.STAGE_FRAMEWORK,
            model=model_id,
            prompt=prompt,
            method=framework.method,
            version=framework.version,
        )
        cached = result_cache.load(result_cache.STAGE_FRAMEWORK, cache_key) if reuse_cache else None

        if cached is not None:
            record.raw_response = cached.get("content", "")
            for name in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"):
                setattr(record, name, int(cached.get(name) or 0))
            record.elapsed_sec = float(cached.get("elapsed_sec") or 0.0)
            record.finish_reason = cached.get("finish_reason", "")
            record.reused_from_cache = True
        else:
            started = time.perf_counter()
            try:
                chat = client.chat(model_id, prompt, max_tokens=max_tokens or None)
            except OpenRouterError as exc:
                record.elapsed_sec = time.perf_counter() - started
                record.api_error = str(exc)
                kind = ERROR_KIND_KEY_LIMIT if isinstance(exc, KeyLimitError) else ERROR_KIND_API
                outcome.error, outcome.error_kind = f"バッチ{index}: {exc}", kind
                return outcome
            record.elapsed_sec = time.perf_counter() - started
            record.raw_response = chat.content
            record.prompt_tokens = chat.prompt_tokens
            record.completion_tokens = chat.completion_tokens
            record.total_tokens = chat.total_tokens
            record.reasoning_tokens = chat.reasoning_tokens
            record.finish_reason = chat.finish_reason

        report = dt_framework.validate_fill_response(framework, record.raw_response, record.rule_ids)
        if record.finish_reason == "length" and not report.ok:
            report.errors.insert(0, "出力が上限 (max_tokens) に達して途中で切れています。設定の出力上限を増やしてください。")
        record.validation_errors = report.errors
        record.validation_warnings = report.warnings
        reports.append(report)

        if report.ok and not record.reused_from_cache:
            result_cache.save(
                result_cache.STAGE_FRAMEWORK,
                cache_key,
                {
                    "model": model_id,
                    "content": record.raw_response,
                    "elapsed_sec": record.elapsed_sec,
                    "prompt_tokens": record.prompt_tokens,
                    "completion_tokens": record.completion_tokens,
                    "total_tokens": record.total_tokens,
                    "reasoning_tokens": record.reasoning_tokens,
                    "finish_reason": record.finish_reason,
                },
            )

    outcome.values, outcome.raw_values = dt_framework.merge_batch_values(reports)
    errors = [f"バッチ{b.index}: {e}" for b in outcome.batches for e in b.validation_errors]
    if errors:
        shown = errors[:10] + ([f"... 他 {len(errors) - 10} 件"] if len(errors) > 10 else [])
        outcome.error = "生成結果の JSON が不正です:\n" + "\n".join(shown)
        outcome.error_kind = ERROR_KIND_INVALID
        return outcome

    outcome.markdown = dt_framework.to_markdown(framework, outcome.values)
    return outcome
