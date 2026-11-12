"""実験1回分の流れをまとめたワーカー.

    Requirements
      -> 抽出モデルで Decision Items を抽出 (実験につき1回だけ)
      -> 同じ Requirements + 同じ Decision Items を各生成モデルへ
      -> Decision Table を生成 (モデルごとに並列)
      -> outputs/ へ保存

GUI スレッドを止めないよう QThread 上で run() される。
進捗は progress シグナルで通知する。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from PySide6.QtCore import QObject, Signal

from models.experiment import Experiment, GenerationResult
from services import step_runner
from services.openrouter_client import OpenRouterClient
from utils import file_manager

MAX_PARALLEL = 4


class ExperimentWorker(QObject):
    progress = Signal(str)          # 処理状況の1行メッセージ
    finished = Signal(object)       # Experiment
    failed = Signal(str)            # 実験を続行できないエラー (抽出失敗など)

    def __init__(
        self,
        client: OpenRouterClient,
        requirements: str,
        extraction_model: str,
        generation_models: list[str],
        names: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.client = client
        self.requirements = requirements
        self.extraction_model = extraction_model
        self.generation_models = list(generation_models)
        self.names = names or {}

    def _name(self, model_id: str) -> str:
        return self.names.get(model_id, model_id)

    # ------------------------------------------------------------------
    def run(self) -> None:
        experiment = Experiment(
            requirements=self.requirements,
            extraction_model=self.extraction_model,
            extraction_model_name=self._name(self.extraction_model),
        )

        # 1. Decision Items の抽出 (1回だけ)
        self.progress.emit(f"Decision Items 抽出中... ({self._name(self.extraction_model)})")
        try:
            chat_result, elapsed = step_runner.run_extraction(
                self.client, self.extraction_model, self.requirements
            )
        except Exception as exc:
            # 抽出に失敗したら生成へは進まない
            self.failed.emit(f"Decision Items の抽出に失敗しました。\n\n{exc}")
            return
        experiment.decision_items = chat_result.content
        experiment.extraction_elapsed_sec = elapsed
        experiment.extraction_prompt_tokens = chat_result.prompt_tokens
        experiment.extraction_completion_tokens = chat_result.completion_tokens
        experiment.extraction_total_tokens = chat_result.total_tokens
        self.progress.emit("Decision Items 抽出完了")

        # 2. Decision Table の生成 (同じ Decision Items を全モデルへ)
        results: dict[str, GenerationResult] = {}
        workers = max(1, min(MAX_PARALLEL, len(self.generation_models)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {}
            for model_id in self.generation_models:
                self.progress.emit(f"{self._name(model_id)} で生成中...")
                futures[
                    pool.submit(
                        step_runner.run_generation,
                        self.client,
                        model_id,
                        self._name(model_id),
                        self.requirements,
                        experiment.decision_items,
                    )
                ] = model_id
            for future in as_completed(futures):
                result = future.result()
                results[result.model_id] = result
                if result.ok:
                    self.progress.emit(
                        f"生成完了: {result.model_name} ({result.elapsed_sec:.1f}秒)"
                    )
                else:
                    self.progress.emit(f"生成失敗: {result.model_name} - {result.error}")

        # 設定した順番で並べ直す
        experiment.results = [
            results[model_id] for model_id in self.generation_models if model_id in results
        ]
        experiment.finished_at = datetime.now()

        # 3. 保存 (1モデルだけ失敗していても、残りの結果は保存する)
        try:
            out_dir = file_manager.save_experiment(experiment)
            self.progress.emit(f"保存しました: {out_dir}")
        except Exception as exc:
            self.progress.emit(f"[警告] 実験結果の保存に失敗しました: {exc}")

        self.progress.emit("完了")
        self.finished.emit(experiment)
