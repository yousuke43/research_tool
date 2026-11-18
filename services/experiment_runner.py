"""実験1回分の流れをまとめたワーカー.

    Requirements
      -> 抽出モデルで Decision Items を抽出 (実験につき1回だけ)
      -> 生成方式に応じて各生成モデルで生成 (モデルごとに並列)
         - 従来方式: 同じ Decision Items から DT 全体を生成
         - 新方式  : 共通DT枠組み (全条件組合せ) の動作結果だけを補完
      -> outputs/ へ保存

処理本体は services/pipeline.py (一括実行と共通)。
GUI スレッドを止めないよう QThread 上で run() される。
進捗は progress シグナルで通知する。
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal

from models.experiment import Experiment
from services import pipeline
from services.openrouter_client import OpenRouterClient
from services.pipeline import MAX_PARALLEL, RunOptions  # noqa: F401 (MAX_PARALLEL は互換のため再公開)
from utils import file_manager


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
        options: RunOptions | None = None,
    ) -> None:
        super().__init__()
        self.client = client
        self.requirements = requirements
        self.extraction_model = extraction_model
        self.generation_models = list(generation_models)
        self.names = names or {}
        self.options = options or RunOptions()
        self.stop_event = threading.Event()

    def _name(self, model_id: str) -> str:
        return self.names.get(model_id, model_id)

    # ------------------------------------------------------------------
    def run(self) -> None:
        experiment = Experiment(
            requirements=self.requirements,
            extraction_model=self.extraction_model,
            extraction_model_name=self._name(self.extraction_model),
        )
        try:
            pipeline.execute(
                self.client,
                experiment,
                self.generation_models,
                self._name,
                self.options,
                self.progress.emit,
                self.stop_event,
            )
        except pipeline.ExtractionFailed as exc:
            # 抽出に失敗したら生成へは進まない
            self.failed.emit(f"Decision Items の抽出に失敗しました。\n\n{exc}")
            return

        # 保存 (1モデルだけ失敗していても、残りの結果は保存する。
        # 新方式で条件数が上限を超えた場合も、抽出結果は保存する)
        try:
            out_dir = file_manager.save_experiment(experiment)
            self.progress.emit(f"保存しました: {out_dir}")
        except Exception as exc:
            self.progress.emit(f"[警告] 実験結果の保存に失敗しました: {exc}")

        self.progress.emit("完了")
        self.finished.emit(experiment)
