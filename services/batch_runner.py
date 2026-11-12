"""data/ フォルダの複数仕様をまとめて実行するワーカー.

仕様 (テキストファイル1つ、または Excel の1行) ごとに ExperimentWorker と同じ流れ
(抽出 -> 各生成モデルで並列生成 -> 保存) を順番に行う。
1件の抽出/生成に失敗しても、残りの処理は続行する。

GUI スレッドを止めないよう QThread 上で run() される。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from PySide6.QtCore import QObject, Signal

from models.experiment import Experiment, GenerationResult
from models.spec_source import SpecSource
from services import step_runner
from services.experiment_runner import MAX_PARALLEL
from services.openrouter_client import OpenRouterClient
from utils import file_manager


class BatchWorker(QObject):
    progress = Signal(str)                 # 処理状況の1行メッセージ
    file_started = Signal(str, int, int)   # 表示名, 現在の番号(1始まり), 総数
    file_finished = Signal(str, object)    # 表示名, Experiment
    file_failed = Signal(str, str)         # 表示名, エラーメッセージ
    finished = Signal(list)                # list[Experiment] (保存できたものだけ)

    def __init__(
        self,
        client: OpenRouterClient,
        sources: list[SpecSource],
        extraction_model: str,
        generation_models: list[str],
        names: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.client = client
        self.sources = list(sources)
        self.extraction_model = extraction_model
        self.generation_models = list(generation_models)
        self.names = names or {}
        self._stop_requested = False

    def _name(self, model_id: str) -> str:
        return self.names.get(model_id, model_id)

    def request_stop(self) -> None:
        """現在処理中の仕様の完了後に停止する."""
        self._stop_requested = True

    # ------------------------------------------------------------------
    def run(self) -> None:
        total = len(self.sources)
        completed: list[Experiment] = []

        for index, source in enumerate(self.sources, start=1):
            if self._stop_requested:
                self.progress.emit("ユーザーの指示により中断しました")
                break

            self.file_started.emit(source.label, index, total)

            try:
                requirements = source.read_requirements()
            except OSError as exc:
                self.file_failed.emit(source.label, f"ファイルを読み込めませんでした: {exc}")
                continue
            if not requirements:
                self.file_failed.emit(source.label, "仕様が空です")
                continue

            experiment = self._run_one(source, requirements, index, total)
            if experiment is not None:
                completed.append(experiment)
                self.file_finished.emit(source.label, experiment)

        self.finished.emit(completed)

    # ------------------------------------------------------------------
    def _run_one(
        self, source: SpecSource, requirements: str, index: int, total: int
    ) -> Experiment | None:
        experiment = Experiment(
            requirements=requirements,
            extraction_model=self.extraction_model,
            extraction_model_name=self._name(self.extraction_model),
            source_name=source.name,
            source_path=str(source.path),
            source_meta=dict(source.meta),
        )

        prefix = f"[{index}/{total}] {source.label}"
        self.progress.emit(f"{prefix}: Decision Items 抽出中... ({experiment.extraction_model_name})")
        try:
            chat_result, elapsed = step_runner.run_extraction(
                self.client, self.extraction_model, requirements
            )
        except Exception as exc:
            self.file_failed.emit(source.label, f"Decision Items の抽出に失敗しました: {exc}")
            return None

        experiment.decision_items = chat_result.content
        experiment.extraction_elapsed_sec = elapsed
        experiment.extraction_prompt_tokens = chat_result.prompt_tokens
        experiment.extraction_completion_tokens = chat_result.completion_tokens
        experiment.extraction_total_tokens = chat_result.total_tokens
        self.progress.emit(f"{prefix}: Decision Items 抽出完了")

        results: dict[str, GenerationResult] = {}
        workers = max(1, min(MAX_PARALLEL, len(self.generation_models)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for model_id in self.generation_models:
                self.progress.emit(f"{prefix}: {self._name(model_id)} で生成中...")
                futures[
                    pool.submit(
                        step_runner.run_generation,
                        self.client,
                        model_id,
                        self._name(model_id),
                        requirements,
                        experiment.decision_items,
                    )
                ] = model_id
            for future in as_completed(futures):
                result = future.result()
                results[result.model_id] = result
                if result.ok:
                    self.progress.emit(
                        f"{prefix}: 生成完了: {result.model_name} ({result.elapsed_sec:.1f}秒)"
                    )
                else:
                    self.progress.emit(f"{prefix}: 生成失敗: {result.model_name} - {result.error}")

        experiment.results = [
            results[model_id] for model_id in self.generation_models if model_id in results
        ]
        experiment.finished_at = datetime.now()

        try:
            out_dir = file_manager.save_experiment(experiment)
            self.progress.emit(f"{prefix}: 保存しました -> {out_dir}")
        except Exception as exc:
            self.progress.emit(f"{prefix}: [警告] 保存に失敗しました: {exc}")

        return experiment
