"""抽出・生成 1 回分の実行をまとめたヘルパー.

ExperimentWorker (単発実行) と BatchWorker (dataフォルダ一括実行) の
両方から使う共通処理 (所要時間計測・例外の GenerationResult 化・成功結果の再利用)。
"""

from __future__ import annotations

import threading
import time

from models.experiment import METHOD_COMMON_FRAMEWORK, GenerationResult
from services import extraction_service, framework_generation_service, generation_service, result_cache
from services.dt_framework import CommonFramework
from services.framework_generation_service import (
    ERROR_KIND_API,
    ERROR_KIND_KEY_LIMIT,
    ERROR_KIND_SKIPPED,
)
from services.openrouter_client import ChatResult, KeyLimitError, OpenRouterClient
from utils.file_manager import load_prompt, render_prompt

SKIPPED_MESSAGE = "APIキーの利用上限に達したため実行を中止しました (未実行)"


def _chat_from_cache(cached: dict) -> ChatResult:
    return ChatResult(
        content=cached.get("content", ""),
        prompt_tokens=int(cached.get("prompt_tokens") or 0),
        completion_tokens=int(cached.get("completion_tokens") or 0),
        total_tokens=int(cached.get("total_tokens") or 0),
        reasoning_tokens=int(cached.get("reasoning_tokens") or 0),
        finish_reason=cached.get("finish_reason", ""),
    )


def _cache_payload(model_id: str, chat: ChatResult, elapsed: float) -> dict:
    return {
        "model": model_id,
        "content": chat.content,
        "elapsed_sec": elapsed,
        "prompt_tokens": chat.prompt_tokens,
        "completion_tokens": chat.completion_tokens,
        "total_tokens": chat.total_tokens,
        "reasoning_tokens": chat.reasoning_tokens,
        "finish_reason": chat.finish_reason,
    }


def run_extraction(
    client: OpenRouterClient,
    model_id: str,
    requirements: str,
    max_tokens: int = 0,
    reuse_cache: bool = False,
) -> tuple[ChatResult, float, bool]:
    """Decision Items を抽出する. (結果, 所要時間, キャッシュ再利用したか) を返す.

    失敗時は例外をそのまま送出する (呼び出し側で処理)。
    """
    prompt = render_prompt(load_prompt(extraction_service.PROMPT_NAME), requirements=requirements)
    key = result_cache.make_key(result_cache.STAGE_EXTRACT, model=model_id, prompt=prompt)
    if reuse_cache:
        cached = result_cache.load(result_cache.STAGE_EXTRACT, key)
        if cached is not None:
            return _chat_from_cache(cached), float(cached.get("elapsed_sec") or 0.0), True

    started = time.perf_counter()
    chat_result = extraction_service.extract_decision_items(
        client, model_id, requirements, max_tokens=max_tokens
    )
    elapsed = time.perf_counter() - started
    result_cache.save(result_cache.STAGE_EXTRACT, key, _cache_payload(model_id, chat_result, elapsed))
    return chat_result, elapsed, False


def _skipped(model_id: str, model_name: str, method: str) -> GenerationResult:
    return GenerationResult(
        model_id=model_id, model_name=model_name, method=method,
        error=SKIPPED_MESSAGE, error_kind=ERROR_KIND_SKIPPED,
    )


def run_generation(
    client: OpenRouterClient,
    model_id: str,
    model_name: str,
    requirements: str,
    decision_items: str,
    max_tokens: int = 0,
    reuse_cache: bool = False,
    stop_event: threading.Event | None = None,
) -> GenerationResult:
    """従来方式: Decision Table を1モデル分生成する. 例外はここで受け止め、結果オブジェクトに包む."""
    if stop_event is not None and stop_event.is_set():
        return _skipped(model_id, model_name, "conventional")

    started = time.perf_counter()
    try:
        prompt = render_prompt(
            load_prompt(generation_service.PROMPT_NAME),
            requirements=requirements,
            decision_items=decision_items,
        )
        key = result_cache.make_key(result_cache.STAGE_CONVENTIONAL, model=model_id, prompt=prompt)
        cached = result_cache.load(result_cache.STAGE_CONVENTIONAL, key) if reuse_cache else None
        if cached is not None:
            chat_result = _chat_from_cache(cached)
            elapsed = float(cached.get("elapsed_sec") or 0.0)
        else:
            chat_result = generation_service.generate_decision_table(
                client, model_id, requirements, decision_items, max_tokens=max_tokens
            )
            elapsed = time.perf_counter() - started
            result_cache.save(
                result_cache.STAGE_CONVENTIONAL, key, _cache_payload(model_id, chat_result, elapsed)
            )
    except Exception as exc:
        if isinstance(exc, KeyLimitError) and stop_event is not None:
            stop_event.set()
        return GenerationResult(
            model_id=model_id,
            model_name=model_name,
            error=str(exc),
            error_kind=ERROR_KIND_KEY_LIMIT if isinstance(exc, KeyLimitError) else ERROR_KIND_API,
            elapsed_sec=time.perf_counter() - started,
        )
    return GenerationResult(
        model_id=model_id,
        model_name=model_name,
        content=chat_result.content,
        elapsed_sec=elapsed,
        prompt_tokens=chat_result.prompt_tokens,
        completion_tokens=chat_result.completion_tokens,
        total_tokens=chat_result.total_tokens,
        reasoning_tokens=chat_result.reasoning_tokens,
        finish_reason=chat_result.finish_reason,
        reused_from_cache=cached is not None,
    )


def run_framework_generation(
    client: OpenRouterClient,
    model_id: str,
    model_name: str,
    requirements: str,
    framework: CommonFramework,
    batch_size: int = 64,
    max_tokens: int = 0,
    reuse_cache: bool = False,
    stop_event: threading.Event | None = None,
) -> GenerationResult:
    """新方式: 共通DT枠組みの動作結果を1モデル分補完する. 例外は結果オブジェクトに包む."""
    if stop_event is not None and stop_event.is_set():
        return _skipped(model_id, model_name, METHOD_COMMON_FRAMEWORK)

    started = time.perf_counter()
    try:
        outcome = framework_generation_service.fill_framework(
            client, model_id, requirements, framework,
            batch_size=batch_size, max_tokens=max_tokens,
            reuse_cache=reuse_cache, stop_event=stop_event,
        )
    except Exception as exc:  # プロンプトファイルの不備など
        return GenerationResult(
            model_id=model_id, model_name=model_name, method=METHOD_COMMON_FRAMEWORK,
            error=str(exc), error_kind=ERROR_KIND_API, elapsed_sec=time.perf_counter() - started,
        )
    if outcome.error_kind == ERROR_KIND_KEY_LIMIT and stop_event is not None:
        stop_event.set()

    batches = outcome.batches
    return GenerationResult(
        model_id=model_id,
        model_name=model_name,
        method=METHOD_COMMON_FRAMEWORK,
        content=outcome.markdown if outcome.ok else "",
        error=outcome.error,
        error_kind=outcome.error_kind,
        elapsed_sec=sum(b.elapsed_sec for b in batches),
        prompt_tokens=outcome.total("prompt_tokens"),
        completion_tokens=outcome.total("completion_tokens"),
        total_tokens=outcome.total("total_tokens"),
        reasoning_tokens=outcome.total("reasoning_tokens"),
        finish_reason=",".join(b.finish_reason for b in batches if b.finish_reason),
        reused_from_cache=bool(batches) and all(b.reused_from_cache for b in batches),
        raw_response=batches[0].raw_response if len(batches) == 1 else "",
        batches=[b.to_dict() for b in batches],
        validated_results=outcome.values,
        raw_results=outcome.raw_values,
        validation_errors=[e for b in batches for e in b.validation_errors],
        validation_warnings=[w for b in batches for w in b.validation_warnings],
    )
