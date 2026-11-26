"""実験1件分 (抽出 -> 生成) の共通処理. 単発実行・一括実行の両方から使う.

    Requirements
      -> 抽出モデルで Decision Items を抽出 (1回だけ)
      -> [従来方式] 同じ Decision Items を各生成モデルへ渡し、DT 全体を生成
         [新方式]   条件の全組合せ (共通DT枠組み) をプログラムで作り、
                    同じ枠組みを各生成モデルへ渡して動作結果だけを補完させる
      -> (新方式) 同じルールIDどうしでモデル間の動作結果を比較

新方式は2段階に分けても実行できる:
    create_framework_record()  抽出 -> 枠組みを作って保存庫 (frameworks/) に保存するだけ
    execute(saved_framework=)  確認・承認済みの枠組みを使い、抽出せずに生成だけを行う

保存は呼び出し側 (ExperimentWorker / BatchWorker) が行う。Qt 非依存。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable

from models.experiment import METHOD_COMMON_FRAMEWORK, METHOD_CONVENTIONAL, Experiment, GenerationResult
from services import (
    dt_framework,
    extraction_service,
    framework_generation_service,
    framework_store,
    generation_service,
    step_runner,
)
from services.framework_comparison import compare_fills, fills_from_records
from services.framework_generation_service import ERROR_KIND_KEY_LIMIT, ERROR_KIND_SKIPPED
from services.framework_store import FrameworkRecord
from services.openrouter_client import KeyLimitError, OpenRouterClient
from utils import config_manager
from utils.file_manager import load_prompt
from utils.version import get_tool_version

MAX_PARALLEL = 4  # 1つの仕様の中で同時に生成するモデル数

KEY_LIMIT_ABORT_MESSAGE = "APIキーの利用上限 (または残高不足) に達したため、新しいリクエストの送信を停止しました"


@dataclass
class RunOptions:
    method: str = METHOD_CONVENTIONAL
    extraction_max_tokens: int = 0
    conventional_max_tokens: int = 0
    framework_max_tokens: int = 32000
    framework_max_conditions: int = 6
    framework_batch_size: int = 64
    reuse_cached_results: bool = True

    @classmethod
    def from_settings(cls, settings: dict, method: str | None = None) -> "RunOptions":
        defaults = config_manager.DEFAULT_SETTINGS
        get = lambda key: settings.get(key, defaults[key])  # noqa: E731
        return cls(
            method=method or get("generation_method"),
            extraction_max_tokens=int(get("extraction_max_tokens")),
            conventional_max_tokens=int(get("conventional_max_tokens")),
            framework_max_tokens=int(get("framework_max_tokens")),
            framework_max_conditions=int(get("framework_max_conditions")),
            framework_batch_size=int(get("framework_batch_size")),
            reuse_cached_results=bool(get("reuse_cached_results")),
        )

    def to_dict(self) -> dict:
        data = asdict(self)
        if self.method == METHOD_COMMON_FRAMEWORK:
            data["framework_method"] = dt_framework.FRAMEWORK_METHOD
            data["framework_version"] = dt_framework.FRAMEWORK_VERSION
            data["framework_strategy"] = dt_framework.STRATEGY_FULL
        return data


class ExtractionFailed(Exception):
    def __init__(self, message: str, key_limit: bool = False) -> None:
        super().__init__(message)
        self.key_limit = key_limit


def _prompt_snapshot(names: list[str]) -> dict:
    snapshot = {}
    for name in names:
        try:
            snapshot[name] = load_prompt(name)
        except OSError as exc:
            snapshot[name] = f"(読み込み失敗: {exc})"
    return snapshot


def _generation_prompt_name(method: str) -> str:
    if method == METHOD_COMMON_FRAMEWORK:
        return framework_generation_service.PROMPT_NAME
    return generation_service.PROMPT_NAME


# ---------------------------------------------------------------- 抽出
def _extract(
    client: OpenRouterClient,
    model_id: str,
    requirements: str,
    options: RunOptions,
    stop_event: threading.Event,
) -> dict:
    """抽出を1回行い、結果を dict で返す. 失敗時は ExtractionFailed."""
    if stop_event.is_set():
        raise ExtractionFailed(KEY_LIMIT_ABORT_MESSAGE + " (未実行)", key_limit=True)
    try:
        chat, elapsed, reused = step_runner.run_extraction(
            client, model_id, requirements,
            max_tokens=options.extraction_max_tokens,
            reuse_cache=options.reuse_cached_results,
        )
    except Exception as exc:
        key_limit = isinstance(exc, KeyLimitError)
        if key_limit:
            stop_event.set()
        raise ExtractionFailed(str(exc), key_limit) from exc
    return {
        "model": model_id,
        "content": chat.content,
        "elapsed_sec": round(elapsed, 2),
        "prompt_tokens": chat.prompt_tokens,
        "completion_tokens": chat.completion_tokens,
        "total_tokens": chat.total_tokens,
        "reasoning_tokens": chat.reasoning_tokens,
        "reused_from_cache": reused,
    }


def _apply_extraction(experiment: Experiment, extraction: dict) -> None:
    experiment.decision_items = extraction.get("content", "")
    experiment.extraction_elapsed_sec = float(extraction.get("elapsed_sec") or 0.0)
    experiment.extraction_prompt_tokens = int(extraction.get("prompt_tokens") or 0)
    experiment.extraction_completion_tokens = int(extraction.get("completion_tokens") or 0)
    experiment.extraction_total_tokens = int(extraction.get("total_tokens") or 0)
    experiment.extraction_reasoning_tokens = int(extraction.get("reasoning_tokens") or 0)
    experiment.extraction_reused_from_cache = bool(extraction.get("reused_from_cache"))


# ------------------------------------------------- 新方式 1段目: 枠組みの作成のみ
def create_framework_record(
    client: OpenRouterClient,
    *,
    source_name: str,
    source_label: str,
    source_path: str,
    source_meta: dict,
    requirements: str,
    extraction_model: str,
    name_of: Callable[[str], str],
    options: RunOptions,
    progress: Callable[[str], None],
    stop_event: threading.Event,
) -> FrameworkRecord:
    """抽出 -> 共通DT枠組みを作り、保存庫に「下書き」として保存する (生成はしない)."""
    progress(f"Decision Items 抽出中... ({name_of(extraction_model)})")
    extraction = _extract(client, extraction_model, requirements, options, stop_event)
    extraction["model_name"] = name_of(extraction_model)
    extraction["prompt"] = _prompt_snapshot([extraction_service.PROMPT_NAME])
    extraction["max_tokens"] = options.extraction_max_tokens
    record = framework_store.build_record(
        source_name=source_name,
        source_label=source_label,
        source_path=source_path,
        source_meta=source_meta,
        requirements=requirements,
        extraction=extraction,
        decision_items=extraction["content"],
        max_conditions=options.framework_max_conditions,
        tool_version=get_tool_version(),
    )
    path = framework_store.save(record)
    if record.usable:
        progress(f"枠組みを保存しました (条件 {record.condition_count} 個 / {record.rule_count} ルール、未確認): {path}")
    else:
        progress(f"[警告] 枠組みを作れませんでした (抽出結果は保存しました): {record.framework_error}")
    return record


# ---------------------------------------------------------------- 実験本体
def execute(
    client: OpenRouterClient,
    experiment: Experiment,
    generation_models: list[str],
    name_of: Callable[[str], str],
    options: RunOptions,
    progress: Callable[[str], None],
    stop_event: threading.Event,
    saved_framework: FrameworkRecord | None = None,
) -> None:
    """experiment に抽出・生成結果を書き込む. 抽出失敗時は ExtractionFailed を送出する.

    saved_framework を渡した場合 (新方式のみ) は抽出を行わず、その枠組みをそのまま使う。
    """
    experiment.generation_method = options.method
    experiment.generation_settings = options.to_dict()
    experiment.tool_version = get_tool_version()
    experiment.prompts = _prompt_snapshot([extraction_service.PROMPT_NAME, _generation_prompt_name(options.method)])

    if saved_framework is not None:
        if options.method != METHOD_COMMON_FRAMEWORK:
            raise ValueError("保存済みの枠組みは新方式でのみ使えます。")
        # 抽出は枠組み作成時のものを使う (抽出プロンプトも作成時点のもの)
        extraction = saved_framework.extraction
        experiment.extraction_model = extraction.get("model", experiment.extraction_model)
        experiment.extraction_model_name = extraction.get("model_name") or experiment.extraction_model
        _apply_extraction(experiment, extraction)
        experiment.decision_items = saved_framework.decision_items  # 修正版なら修正後の項目
        prompt = (extraction.get("prompt") or {}).get(extraction_service.PROMPT_NAME)
        if prompt:
            experiment.prompts[extraction_service.PROMPT_NAME] = prompt
        experiment.framework_source = saved_framework.source_summary()
        # 枠組みは作成時の本文から作ったもの。生成には現在の本文 (experiment.requirements) を渡す
        experiment.framework_source["requirements_changed"] = (
            saved_framework.requirements_sha256 != framework_store.requirements_hash(experiment.requirements)
        )
        experiment.framework_status = "ok"
        experiment.framework = saved_framework.framework
        experiment.generation_settings["framework_max_conditions"] = saved_framework.max_conditions
        framework = saved_framework.common_framework()
        experiment.generation_settings["framework_strategy"] = framework.strategy
        experiment.generation_settings["excluded_count"] = len(framework.excluded_rules)
        progress(
            f"保存済みの枠組みを使用します ({saved_framework.framework_id}、"
            f"条件 {len(framework.conditions)} 個 / {len(framework.rules)} ルール"
            + (f"、全 {framework.total_combinations} 通りから {len(framework.excluded_rules)} 通りを除外済み"
               if framework.excluded_rules else "")
            + ")"
        )
    else:
        # 1. Decision Items の抽出 (1回だけ)
        progress(f"Decision Items 抽出中... ({name_of(experiment.extraction_model)})")
        extraction = _extract(client, experiment.extraction_model, experiment.requirements, options, stop_event)
        _apply_extraction(experiment, extraction)
        progress("Decision Items 抽出完了" + (" (保存済みの成功結果を再利用)" if extraction["reused_from_cache"] else ""))
        framework = None
        if options.method == METHOD_COMMON_FRAMEWORK:
            framework = _build_framework(experiment, options, progress)
            if framework is None:
                return

    # 2. 生成方式ごとのジョブを用意する
    if options.method == METHOD_COMMON_FRAMEWORK:
        batches = len(dt_framework.split_batches(framework.rules, options.framework_batch_size))
        if batches > 1:
            progress(f"{len(framework.rules)} ルールを {batches} バッチに分けて生成します")

        def job(model_id: str) -> GenerationResult:
            return step_runner.run_framework_generation(
                client, model_id, name_of(model_id), experiment.requirements, framework,
                batch_size=options.framework_batch_size,
                max_tokens=options.framework_max_tokens,
                reuse_cache=options.reuse_cached_results,
                stop_event=stop_event,
            )
    else:
        def job(model_id: str) -> GenerationResult:
            return step_runner.run_generation(
                client, model_id, name_of(model_id), experiment.requirements, experiment.decision_items,
                max_tokens=options.conventional_max_tokens,
                reuse_cache=options.reuse_cached_results,
                stop_event=stop_event,
            )

    # 3. 各生成モデルで並列に生成
    _generate_all(experiment, generation_models, name_of, job, progress)

    if options.method == METHOD_COMMON_FRAMEWORK and experiment.framework:
        fw = dt_framework.CommonFramework.from_dict(experiment.framework)
        fills = fills_from_records([r.to_dict() for r in experiment.results], fw)
        experiment.framework_comparison = compare_fills(fw, fills).to_dict()

    experiment.finished_at = datetime.now()


def _build_framework(
    experiment: Experiment, options: RunOptions, progress: Callable[[str], None]
) -> dt_framework.CommonFramework | None:
    try:
        items = dt_framework.parse_decision_items(experiment.decision_items)
        framework = dt_framework.build_framework(items, options.framework_max_conditions)
    except dt_framework.TooManyConditionsError as exc:
        experiment.framework_status = "too_many_conditions"
        experiment.framework_error = str(exc)
        experiment.framework = {
            **items.to_dict(),
            "condition_count": exc.condition_count,
            "max_conditions": exc.max_conditions,
        }
        experiment.finished_at = datetime.now()
        progress(f"[警告] {exc}")
        return None
    except dt_framework.FrameworkError as exc:
        experiment.framework_status = "invalid_items"
        experiment.framework_error = str(exc)
        experiment.finished_at = datetime.now()
        progress(f"[エラー] 共通DTの枠組みを作れませんでした: {exc}")
        return None
    experiment.framework_status = "ok"
    experiment.framework = framework.to_dict()
    progress(f"共通DT枠組みを生成しました (条件 {len(framework.conditions)} 個 / {len(framework.rules)} ルール)")
    return framework


def _generate_all(
    experiment: Experiment,
    generation_models: list[str],
    name_of: Callable[[str], str],
    job: Callable[[str], GenerationResult],
    progress: Callable[[str], None],
) -> None:
    results: dict[str, GenerationResult] = {}
    workers = max(1, min(MAX_PARALLEL, len(generation_models)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for model_id in generation_models:
            progress(f"{name_of(model_id)} で生成中...")
            futures[pool.submit(job, model_id)] = model_id
        for future in as_completed(futures):
            result = future.result()
            results[result.model_id] = result
            if result.ok:
                note = " (保存済みの成功結果を再利用)" if result.reused_from_cache else f" ({result.elapsed_sec:.1f}秒)"
                progress(f"生成完了: {result.model_name}{note}")
            elif result.error_kind == ERROR_KIND_SKIPPED:
                progress(f"未実行: {result.model_name} (APIキー上限超過のため中止)")
            else:
                progress(f"生成失敗: {result.model_name} - {result.error.splitlines()[0] if result.error else ''}")
                if result.error_kind == ERROR_KIND_KEY_LIMIT:
                    progress(f"[停止] {KEY_LIMIT_ABORT_MESSAGE}")

    experiment.results = [results[m] for m in generation_models if m in results]
    if any(r.error_kind in (ERROR_KIND_KEY_LIMIT, ERROR_KIND_SKIPPED) for r in experiment.results):
        experiment.aborted_reason = KEY_LIMIT_ABORT_MESSAGE
