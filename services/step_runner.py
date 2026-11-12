"""抽出・生成 1 回分の実行をまとめたヘルパー.

ExperimentWorker (単発実行) と BatchWorker (dataフォルダ一括実行) の
両方から使う共通処理 (所要時間計測・例外の GenerationResult 化)。
"""

from __future__ import annotations

import time

from models.experiment import GenerationResult
from services import extraction_service, generation_service
from services.openrouter_client import ChatResult, OpenRouterClient


def run_extraction(
    client: OpenRouterClient, model_id: str, requirements: str
) -> tuple[ChatResult, float]:
    """Decision Items を抽出する. 失敗時は例外をそのまま送出する (呼び出し側で処理)."""
    started = time.perf_counter()
    chat_result = extraction_service.extract_decision_items(client, model_id, requirements)
    return chat_result, time.perf_counter() - started


def run_generation(
    client: OpenRouterClient,
    model_id: str,
    model_name: str,
    requirements: str,
    decision_items: str,
) -> GenerationResult:
    """Decision Table を1モデル分生成する. 例外はここで受け止め、結果オブジェクトに包む."""
    started = time.perf_counter()
    try:
        chat_result = generation_service.generate_decision_table(
            client, model_id, requirements, decision_items
        )
    except Exception as exc:
        return GenerationResult(
            model_id=model_id,
            model_name=model_name,
            error=str(exc),
            elapsed_sec=time.perf_counter() - started,
        )
    return GenerationResult(
        model_id=model_id,
        model_name=model_name,
        content=chat_result.content,
        elapsed_sec=time.perf_counter() - started,
        prompt_tokens=chat_result.prompt_tokens,
        completion_tokens=chat_result.completion_tokens,
        total_tokens=chat_result.total_tokens,
    )
