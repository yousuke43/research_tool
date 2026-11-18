"""data/ フォルダの複数仕様をまとめて実行するワーカー.

仕様 (テキストファイル1つ、または Excel の1行) ごとに ExperimentWorker と同じ流れ
(抽出 -> 各生成モデルで並列生成 -> 保存) を行う。
仕様どうしも最大 max_parallel_specs 件まで同時に実行する
(同時に飛ぶ API リクエストは最大で 仕様の並列数 x 生成モデルの並列数 (MAX_PARALLEL))。
1件の抽出/生成に失敗しても、残りの処理は続行する。
ただし API キーの利用上限超過・残高不足 (KeyLimitError) を受けた場合は、
新しい仕様・新しいリクエストの投入を止める (処理中の仕様は可能な限り保存する)。

保存先は一括実行1回ごとにまとめる:
    outputs/batch_<開始日時>/
        <仕様ごとのフォルダ>/ (results.json など。構成は単発実行と同じ)
        batch_summary.json   (この一括実行で実行した仕様と結果の一覧)

GUI スレッドを止めないよう QThread 上で run() される。
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from models.experiment import Experiment
from models.spec_source import SpecSource
from services import framework_store, pipeline
from services.openrouter_client import OpenRouterClient
from services.pipeline import RunOptions
from utils import file_manager

DEFAULT_PARALLEL_SPECS = 3
MAX_PARALLEL_SPECS = 8

# 実行モード (新方式では2段階に分けられる)
MODE_FULL = "full"                      # 抽出から生成まで続けて実行 (従来方式は常にこれ)
MODE_FRAMEWORK_ONLY = "framework_only"  # 新方式1段目: 抽出 -> 枠組みを作って frameworks/ に保存するだけ
MODE_USE_SAVED = "use_saved"            # 新方式2段目: 承認済みの保存枠組みで生成だけを行う
RUN_MODES = {
    MODE_FRAMEWORK_ONLY: "① 枠組みの作成のみ (確認用に保存。生成はしない)",
    MODE_USE_SAVED: "② 承認済みの保存枠組みで生成",
    MODE_FULL: "抽出から生成まで続けて実行 (確認なし)",
}


class BatchWorker(QObject):
    progress = Signal(str)                 # 処理状況の1行メッセージ
    file_started = Signal(str, int, int)   # 表示名, 仕様の番号(1始まり), 総数 (並列実行なので順不同)
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
        max_parallel_specs: int = DEFAULT_PARALLEL_SPECS,
        options: RunOptions | None = None,
        mode: str = MODE_FULL,
    ) -> None:
        super().__init__()
        self.options = options or RunOptions()
        self.mode = mode if self.options.method == "common_framework" else MODE_FULL
        self._saved_frameworks: list = []
        # API キー上限超過で立つ。立ったら新しいリクエストを送らない (仕様・モデル・バッチすべて)
        self.stop_event = threading.Event()
        self.abort_reason = ""
        self._status: dict[int, tuple[str, str]] = {}  # index -> (状態, エラー)
        self.max_parallel_specs = max(1, min(MAX_PARALLEL_SPECS, int(max_parallel_specs)))
        self.client = client
        self.sources = list(sources)
        self.extraction_model = extraction_model
        self.generation_models = list(generation_models)
        self.names = names or {}
        self._stop_requested = False
        self.batch_dir: Path | None = None

    def _name(self, model_id: str) -> str:
        return self.names.get(model_id, model_id)

    def request_stop(self) -> None:
        """まだ始まっていない仕様は実行しない (処理中の仕様は最後まで行う)."""
        self._stop_requested = True

    # ------------------------------------------------------------------
    def run(self) -> None:
        total = len(self.sources)
        completed: dict[int, Experiment] = {}
        stop_notified = threading.Event()
        started_at = datetime.now()

        if self.mode == MODE_FRAMEWORK_ONLY:
            # 枠組みは frameworks/<仕様名>/ に保存する (outputs には何も作らない)
            self.progress.emit(f"枠組みの保存先: {framework_store.frameworks_dir()}")
        else:
            try:
                self.batch_dir = file_manager.create_batch_dir(started_at, self.options.method)
            except OSError as exc:
                self.progress.emit(f"[エラー] 保存先フォルダを作成できませんでした: {exc}")
                self.finished.emit([])
                return
            self.progress.emit(f"保存先: {self.batch_dir}")
        if self.mode == MODE_USE_SAVED:
            self._saved_frameworks = framework_store.list_records()

        def process(index: int, source: SpecSource) -> None:
            if self.stop_event.is_set():
                self._status[index] = ("skipped_key_limit", pipeline.KEY_LIMIT_ABORT_MESSAGE)
                self.file_failed.emit(source.label, "未実行 (APIキー上限超過のため一括実行を停止しました)")
                return
            if self._stop_requested:
                self._status[index] = ("skipped_by_user", "")
                if not stop_notified.is_set():
                    stop_notified.set()
                    self.progress.emit("ユーザーの指示により、未開始の仕様は実行しません")
                return

            self.file_started.emit(source.label, index, total)
            try:
                requirements = source.read_requirements()
            except OSError as exc:
                self._status[index] = ("failed", f"ファイルを読み込めませんでした: {exc}")
                self.file_failed.emit(source.label, f"ファイルを読み込めませんでした: {exc}")
                return
            if not requirements:
                self._status[index] = ("failed", "仕様が空です")
                self.file_failed.emit(source.label, "仕様が空です")
                return

            if self.mode == MODE_FRAMEWORK_ONLY:
                record = self._create_framework(source, requirements, index, total)
                if record is not None:
                    completed[index] = record
                    self.file_finished.emit(source.label, record)
                return

            saved = None
            if self.mode == MODE_USE_SAVED:
                candidates = framework_store.find_approved(source.name, requirements, self._saved_frameworks)
                if not candidates:
                    message = "承認済みの枠組みがありません (「共通DT枠組み...」で確認・承認してください)"
                    self._status[index] = ("no_approved_framework", message)
                    self.file_failed.emit(source.label, message)
                    return
                saved = candidates[0]
                if len(candidates) > 1:
                    self.progress.emit(
                        f"[{index}/{total}] {source.label}: 承認済みの枠組みが {len(candidates)} 件あるため、"
                        f"最新の {saved.framework_id} を使います"
                    )

            experiment = self._run_one(source, requirements, index, total, saved)
            if experiment is not None:
                completed[index] = experiment
                self.file_finished.emit(source.label, experiment)

        workers = max(1, min(self.max_parallel_specs, total))
        self.progress.emit(f"仕様を最大 {workers} 件ずつ同時に実行します")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(process, i, src) for i, src in enumerate(self.sources, start=1)]
            for future in as_completed(futures):
                exc = future.exception()
                if exc is not None:  # 想定外の例外でも他の仕様は続ける
                    self.progress.emit(f"[エラー] 想定外のエラー: {exc}")

        if self.stop_event.is_set():
            self.abort_reason = pipeline.KEY_LIMIT_ABORT_MESSAGE
            self.progress.emit(
                f"[停止] {self.abort_reason}。成功した結果は保存済みです。"
                "再実行すると成功済みの処理はスキップされます (再利用が有効な場合)。"
            )

        # 完了順ではなく元の並び順で返す
        experiments = [completed[i] for i in sorted(completed)]
        if self.mode != MODE_FRAMEWORK_ONLY:
            self._write_summary(started_at, experiments)
        self.finished.emit(experiments)

    def _write_summary(self, started_at: datetime, experiments: list[Experiment]) -> None:
        """batch_summary.json: この一括実行で何を実行し、どこに保存したかの一覧."""
        done = {e.source_path + "|" + e.source_name: e for e in experiments}
        specs = []
        for index, source in enumerate(self.sources, start=1):
            exp = done.get(str(source.path) + "|" + source.name)
            status, error = self._status.get(index, ("done" if exp else "not_started", ""))
            specs.append(
                {
                    "label": source.label,
                    "status": status,
                    "error": error,
                    "framework_status": exp.framework_status if exp else "",
                    "framework_id": (exp.framework_source or {}).get("framework_id", "") if exp else "",
                    "output_folder": Path(exp.output_dir).name if exp and exp.output_dir else "",
                    "ok_models": sum(1 for r in exp.results if r.ok) if exp else 0,
                    "failed_models": sum(1 for r in exp.results if not r.ok) if exp else 0,
                    "reused_models": sum(1 for r in exp.results if r.reused_from_cache) if exp else 0,
                }
            )
        summary = {
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "generation_method": self.options.method,
            "run_mode": self.mode,
            "generation_settings": self.options.to_dict(),
            "extraction_model": self.extraction_model,
            "generation_models": self.generation_models,
            "max_parallel_specs": self.max_parallel_specs,
            "aborted_reason": self.abort_reason,
            "specs": specs,
        }
        try:
            (self.batch_dir / "batch_summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            self.progress.emit(f"[警告] batch_summary.json を保存できませんでした: {exc}")
        # 一括実行全体の解析用 CSV (全仕様分をまとめたもの)
        try:
            from services.result_export import export_records

            export_records(self.batch_dir, [(e.to_dict(), e.output_dir) for e in experiments])
        except Exception as exc:  # noqa: BLE001
            self.progress.emit(f"[警告] 集計 CSV を保存できませんでした: {exc}")

    # ------------------------------------------------------------------
    def _create_framework(self, source: SpecSource, requirements: str, index: int, total: int):
        prefix = f"[{index}/{total}] {source.label}"
        try:
            record = pipeline.create_framework_record(
                self.client,
                source_name=source.name,
                source_label=source.label,
                source_path=str(source.path),
                source_meta=dict(source.meta),
                requirements=requirements,
                extraction_model=self.extraction_model,
                name_of=self._name,
                options=self.options,
                progress=lambda message: self.progress.emit(f"{prefix}: {message}"),
                stop_event=self.stop_event,
            )
        except pipeline.ExtractionFailed as exc:
            self._status[index] = ("key_limit" if exc.key_limit else "extraction_failed", str(exc))
            if exc.key_limit:
                self.progress.emit(f"{prefix}: [停止] {pipeline.KEY_LIMIT_ABORT_MESSAGE}")
            self.file_failed.emit(source.label, f"Decision Items の抽出に失敗しました: {exc}")
            return None
        except OSError as exc:
            self._status[index] = ("failed", str(exc))
            self.file_failed.emit(source.label, f"枠組みを保存できませんでした: {exc}")
            return None
        return record

    def _run_one(
        self, source: SpecSource, requirements: str, index: int, total: int, saved=None
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
        try:
            pipeline.execute(
                self.client,
                experiment,
                self.generation_models,
                self._name,
                self.options,
                lambda message: self.progress.emit(f"{prefix}: {message}"),
                self.stop_event,
                saved_framework=saved,
            )
        except pipeline.ExtractionFailed as exc:
            status = "key_limit" if exc.key_limit else "extraction_failed"
            self._status[index] = (status, str(exc))
            if exc.key_limit:
                self.progress.emit(f"{prefix}: [停止] {pipeline.KEY_LIMIT_ABORT_MESSAGE}")
            self.file_failed.emit(source.label, f"Decision Items の抽出に失敗しました: {exc}")
            return None

        if experiment.aborted_reason:
            self._status[index] = ("partial_key_limit", experiment.aborted_reason)
        elif experiment.framework_status == "too_many_conditions":
            self._status[index] = ("too_many_conditions", experiment.framework_error)
        elif experiment.framework_status == "invalid_items":
            self._status[index] = ("invalid_items", experiment.framework_error)

        try:
            out_dir = file_manager.save_experiment(experiment, self.batch_dir)
            self.progress.emit(f"{prefix}: 保存しました -> {out_dir}")
        except Exception as exc:
            self.progress.emit(f"{prefix}: [警告] 保存に失敗しました: {exc}")

        return experiment
