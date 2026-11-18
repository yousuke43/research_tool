"""data/ フォルダの仕様ファイルをまとめて実行するダイアログ.

フォルダ内の *.txt と *.xlsx の実行対象行を一覧表示し、選択したものをまとめて
(抽出 -> 各生成モデルで生成 -> 保存) 実行する。
モデル設定はメイン画面で選択済みのものをそのまま使う。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime

from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from models.experiment import Experiment
from models.spec_source import SpecSource
from services import framework_store
from services.batch_runner import (
    DEFAULT_PARALLEL_SPECS,
    MAX_PARALLEL_SPECS,
    MODE_FRAMEWORK_ONLY,
    MODE_FULL,
    MODE_USE_SAVED,
    RUN_MODES,
    BatchWorker,
)
from services.openrouter_client import OpenRouterClient
from services.pipeline import RunOptions
from utils import file_manager
from utils.config_manager import DATA_DIR, DEFAULT_SETTINGS, GENERATION_METHODS, METHOD_COMMON_FRAMEWORK


class BatchDialog(QDialog):
    def __init__(
        self,
        client: OpenRouterClient,
        settings: dict,
        models: list[dict],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("一括実行 (dataフォルダ)")
        self.resize(760, 640)

        self.client = client
        self.settings = dict(settings)
        self.models = list(models or [])
        self.completed_experiments: list[Experiment] = []
        self._sources: list[SpecSource] = []

        self._thread: QThread | None = None
        self._worker: BatchWorker | None = None
        self._running: list[str] = []  # 実行中の仕様 (並列実行なので複数)
        self._done_count = 0

        self._build_ui()
        self._reload_files()
        self._on_method_or_mode_changed()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.info_label = QLabel(
            f"対象フォルダ: {DATA_DIR}\n"
            "このフォルダの .txt ファイル (1ファイル = 1件) と、.xlsx の\n"
            "「区分」が変数追加版で「実行」に〇が入っている行 (1行 = 1件) を仕様として読み込みます。"
        )
        self.info_label.setWordWrap(True)

        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QAbstractItemView.NoSelection)

        self.refresh_button = QPushButton("一覧を更新")
        self.refresh_button.clicked.connect(self._reload_files)
        self.select_all_button = QPushButton("すべて選択")
        self.select_all_button.clicked.connect(lambda: self._set_all_checked(True))
        self.select_none_button = QPushButton("選択解除")
        self.select_none_button.clicked.connect(lambda: self._set_all_checked(False))
        self.open_data_button = QPushButton("dataフォルダを開く")
        self.open_data_button.clicked.connect(self._open_data_dir)

        list_buttons = QHBoxLayout()
        list_buttons.addWidget(self.refresh_button)
        list_buttons.addWidget(self.select_all_button)
        list_buttons.addWidget(self.select_none_button)
        list_buttons.addStretch(1)
        list_buttons.addWidget(self.open_data_button)

        self.model_label = QLabel("")
        self.model_label.setWordWrap(True)
        self.model_label.setStyleSheet("color: #555;")
        self._update_model_label()

        self.status_label = QLabel("待機中")
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)

        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setFixedHeight(200)

        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, MAX_PARALLEL_SPECS)
        self.parallel_spin.setValue(DEFAULT_PARALLEL_SPECS)
        self.parallel_spin.setToolTip(
            "仕様を何件同時に実行するか。各仕様の中でも生成モデルは並列に実行されるため、\n"
            "同時に送るリクエスト数は「この値 x 生成モデル数 (最大4)」になります。\n"
            "OpenRouter のレート制限 (HTTP 429) でエラーが出る場合は値を下げてください。"
        )
        parallel_row = QHBoxLayout()
        parallel_row.addWidget(QLabel("同時に実行する仕様の数:"))
        parallel_row.addWidget(self.parallel_spin)
        parallel_row.addWidget(QLabel("(1 にすると従来どおり1件ずつ順番に実行)"))
        parallel_row.addStretch(1)

        self.method_combo = QComboBox()
        for key, label in GENERATION_METHODS.items():
            self.method_combo.addItem(label, key)
        self.method_combo.setCurrentIndex(max(0, self.method_combo.findData(self.settings.get("generation_method"))))
        self.reuse_check = QCheckBox("成功済みの結果を再利用 (失敗した処理だけ再実行)")
        self.reuse_check.setChecked(bool(self.settings.get("reuse_cached_results", DEFAULT_SETTINGS["reuse_cached_results"])))
        self.reuse_check.setToolTip(
            "モデル・要求仕様・プロンプト・生成方式・抽出項目がすべて同じ場合だけ、以前に成功した\n"
            "抽出・生成結果を再利用し、API へのリクエストを省略します。"
        )
        method_row = QHBoxLayout()
        method_row.addWidget(QLabel("生成方式:"))
        method_row.addWidget(self.method_combo)
        method_row.addWidget(self.reuse_check)
        method_row.addStretch(1)

        # 新方式の実行モード (枠組みを確認してから生成する2段階の手順)
        self.mode_combo = QComboBox()
        for key, label in RUN_MODES.items():
            self.mode_combo.addItem(label, key)
        self.mode_combo.setToolTip(
            "① 枠組みの作成のみ: 抽出して共通DT枠組みを frameworks/ に保存します (生成モデルは使いません)。\n"
            "   「枠組みを確認...」で条件項目・条件組合せを確認し、承認してください。\n"
            "② 承認済みの保存枠組みで生成: 各仕様の最新の承認済み枠組みを全生成モデルへ渡します (抽出はしません)。\n"
            "   承認済みの枠組みがない仕様は実行しません。"
        )
        self.framework_button = QPushButton("枠組みを確認...")
        self.framework_button.clicked.connect(self._open_framework_dialog)
        mode_row = QHBoxLayout()
        self.mode_label = QLabel("実行モード:")
        mode_row.addWidget(self.mode_label)
        mode_row.addWidget(self.mode_combo)
        mode_row.addWidget(self.framework_button)
        mode_row.addStretch(1)
        self.method_combo.currentIndexChanged.connect(self._on_method_or_mode_changed)
        self.mode_combo.currentIndexChanged.connect(self._on_method_or_mode_changed)

        self.run_button = QPushButton("実行")
        self.run_button.setMinimumHeight(32)
        self.run_button.clicked.connect(self._on_run)
        self.close_button = QPushButton("閉じる")
        self.close_button.clicked.connect(self.close)

        bottom_buttons = QHBoxLayout()
        bottom_buttons.addWidget(self.run_button)
        bottom_buttons.addStretch(1)
        bottom_buttons.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.info_label)
        layout.addLayout(list_buttons)
        layout.addWidget(self.file_list, 1)
        layout.addWidget(self.model_label)
        layout.addLayout(method_row)
        layout.addLayout(mode_row)
        layout.addLayout(parallel_row)
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_bar)
        layout.addWidget(QLabel("処理状況"))
        layout.addWidget(self.log_edit)
        layout.addLayout(bottom_buttons)

    # ---------------------------------------------------------- 実行モード
    def _is_framework_method(self) -> bool:
        return self.method_combo.currentData() == METHOD_COMMON_FRAMEWORK

    def _current_mode(self) -> str:
        return self.mode_combo.currentData() if self._is_framework_method() else MODE_FULL

    def _on_method_or_mode_changed(self) -> None:
        framework = self._is_framework_method()
        for widget in (self.mode_label, self.mode_combo, self.framework_button):
            widget.setEnabled(framework and self._thread is None)
        mode = self._current_mode()
        self.run_button.setText({
            MODE_FRAMEWORK_ONLY: "枠組みを作成",
            MODE_USE_SAVED: "承認済み枠組みで生成",
        }.get(mode, "実行"))
        self._update_framework_marks()

    def _update_framework_marks(self) -> None:
        """② のとき、各仕様に承認済みの枠組みがあるかを一覧に表示する."""
        show = self._current_mode() in (MODE_USE_SAVED, MODE_FRAMEWORK_ONLY)
        records = framework_store.list_records() if show else []
        for i in range(self.file_list.count()):
            item = self.file_list.item(i)
            source = self._sources[item.data(Qt.UserRole)]
            text = source.label
            if show:
                try:
                    approved = framework_store.find_approved(source.name, source.read_requirements(), records)
                except OSError:
                    approved = []
                drafts = [r for r in records if r.source_name == source.name and r.status != framework_store.STATUS_APPROVED]
                if approved:
                    text += f"　✔ 承認済み枠組みあり ({approved[0].framework_id})"
                elif drafts:
                    text += "　… 未承認の枠組みあり"
                else:
                    text += "　(枠組みなし)"
            item.setText(text)

    def _open_framework_dialog(self) -> None:
        from ui.framework_dialog import FrameworkDialog

        FrameworkDialog(self.settings, self).exec()
        self._update_framework_marks()

    def _update_model_label(self) -> None:
        extraction = self.settings.get("extraction_model", "")
        generation = self.settings.get("generation_models", [])
        if not extraction or not generation:
            self.model_label.setText(
                "⚠ 抽出モデル・生成モデルが未設定です。メイン画面の「モデル設定...」から選択してください。"
            )
        else:
            self.model_label.setText(
                f"抽出モデル: {extraction}\n生成モデル ({len(generation)}): {', '.join(generation)}"
            )

    # -------------------------------------------------------------- 一覧表示
    def _reload_files(self) -> None:
        checked = {
            self.file_list.item(i).data(Qt.UserRole + 1)
            for i in range(self.file_list.count())
            if self.file_list.item(i).checkState() == Qt.Checked
        }
        self.file_list.clear()
        self._sources, notes = file_manager.list_spec_sources()
        for index, source in enumerate(self._sources):
            item = QListWidgetItem(source.label)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setData(Qt.UserRole, index)
            item.setData(Qt.UserRole + 1, source.label)
            item.setCheckState(Qt.Checked if source.label in checked or not checked else Qt.Unchecked)
            if source.requirements:
                item.setToolTip(source.requirements[:300])
            self.file_list.addItem(item)

        for note in notes:
            self._log(f"[注意] {note}")
        if hasattr(self, "mode_combo"):
            self._update_framework_marks()

        if not self._sources:
            self.status_label.setText(
                f"{DATA_DIR} に実行対象が見つかりません。仕様ファイルを置いてから更新してください。"
            )
        else:
            self.status_label.setText(f"{len(self._sources)} 件の仕様を検出しました")

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for i in range(self.file_list.count()):
            self.file_list.item(i).setCheckState(state)

    def _checked_sources(self) -> list[SpecSource]:
        return [
            self._sources[self.file_list.item(i).data(Qt.UserRole)]
            for i in range(self.file_list.count())
            if self.file_list.item(i).checkState() == Qt.Checked
        ]

    def _open_data_dir(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(str(DATA_DIR))  # noqa: S606 (Windows 標準の方法)
            else:
                subprocess.Popen(["xdg-open", str(DATA_DIR)])
        except OSError as exc:
            QMessageBox.warning(self, "フォルダを開けません", str(exc))

    # -------------------------------------------------------------------- ログ
    def _log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_edit.appendPlainText(f"[{stamp}] {message}")

    # ------------------------------------------------------------------ 実行
    def _on_run(self) -> None:
        if self._thread is not None:
            return

        extraction = self.settings.get("extraction_model", "")
        generation = list(self.settings.get("generation_models", []))
        if not extraction or not generation:
            QMessageBox.warning(
                self, "モデル未設定", "抽出モデルと生成モデルを設定してください。"
            )
            return

        sources = self._checked_sources()
        if not sources:
            QMessageBox.warning(self, "一括実行", "実行する仕様を選択してください。")
            return

        names = {m.get("id", ""): m.get("name", "") for m in self.models}
        names = {k: v for k, v in names.items() if k and v}

        self.completed_experiments = []
        self.log_edit.clear()
        self.progress_bar.setRange(0, len(sources))
        self.progress_bar.setValue(0)
        self.run_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.parallel_spin.setEnabled(False)
        self.method_combo.setEnabled(False)
        self.reuse_check.setEnabled(False)
        mode = self._current_mode()
        for widget in (self.mode_combo, self.framework_button):
            widget.setEnabled(False)
        options = RunOptions.from_settings(
            {**self.settings, "reuse_cached_results": self.reuse_check.isChecked()},
            method=self.method_combo.currentData(),
        )
        self._running = []
        self._done_count = 0
        self._set_status(f"実行中... (完了 0/{len(sources)})")
        self._log(
            f"一括実行開始 ({len(sources)} 件、同時実行 {self.parallel_spin.value()} 件、"
            f"方式: {self.method_combo.currentText()}、"
            + (f"モード: {RUN_MODES[mode]}、" if self._is_framework_method() else "")
            + f"再利用: {'あり' if options.reuse_cached_results else 'なし'})"
        )

        self._thread = QThread(self)
        self._worker = BatchWorker(
            client=self.client,
            sources=sources,
            extraction_model=extraction,
            generation_models=generation,
            names=names,
            max_parallel_specs=self.parallel_spin.value(),
            options=options,
            mode=mode,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._log)
        self._worker.file_started.connect(self._on_file_started)
        self._worker.file_finished.connect(self._on_file_finished)
        self._worker.file_failed.connect(self._on_file_failed)
        self._worker.finished.connect(self._on_batch_finished)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

    def _set_status(self, message: str) -> None:
        self.status_label.setText(message)

    def _on_file_started(self, filename: str, index: int, total: int) -> None:
        self._running.append(filename)
        self._update_running_status()

    def _mark_done(self, filename: str) -> None:
        if filename in self._running:
            self._running.remove(filename)
        self._done_count += 1
        self.progress_bar.setValue(self._done_count)
        self._update_running_status()

    def _update_running_status(self) -> None:
        total = self.progress_bar.maximum()
        running = "、".join(self._running) if self._running else "-"
        self._set_status(f"実行中... (完了 {self._done_count}/{total})  処理中 {len(self._running)} 件: {running}")

    def _on_file_finished(self, filename: str, experiment: object) -> None:
        if isinstance(experiment, framework_store.FrameworkRecord):
            record = experiment
            state = (f"条件 {record.condition_count} 個 / {record.rule_count} ルール" if record.usable
                     else f"使用不可: {record.framework_error}")
            self._log(f"{filename}: 枠組みを保存 ({state})")
            self._mark_done(filename)
            return
        exp = experiment if isinstance(experiment, Experiment) else None
        if exp is not None:
            self.completed_experiments.append(exp)
            ok = sum(1 for r in exp.results if r.ok)
            ng = len(exp.results) - ok
            self._log(f"{filename}: 完了 (成功 {ok} / 失敗 {ng})")
        self._mark_done(filename)

    def _on_file_failed(self, filename: str, message: str) -> None:
        self._log(f"{filename}: [エラー] {message}")
        self._mark_done(filename)

    def _on_batch_finished(self, experiments: list) -> None:
        reason = self._worker.abort_reason if self._worker is not None else ""
        if reason:
            self._set_status(f"停止 ({len(experiments)} / {self.progress_bar.maximum()} 件保存): {reason}")
            self._log("一括実行を停止しました (APIキー上限超過)")
            QMessageBox.warning(
                self,
                "一括実行を停止しました",
                f"{reason}。\n\n成功した結果は保存済みです。上限を確認してから再実行すると、"
                "「成功済みの結果を再利用」が有効な場合は失敗・未実行の処理だけが実行されます。",
            )
            return
        self._set_status(f"完了 ({len(experiments)} / {self.progress_bar.maximum()} 件保存)")
        self._log("一括実行が完了しました")
        if experiments and all(isinstance(e, framework_store.FrameworkRecord) for e in experiments):
            answer = QMessageBox.question(
                self, "枠組みの確認",
                f"枠組みを {len(experiments)} 件保存しました (未承認)。\n今すぐ確認・承認しますか?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            )
            if answer == QMessageBox.Yes:
                self._open_framework_dialog()

    def _on_thread_finished(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        if self._thread is not None:
            self._thread.deleteLater()
            self._thread = None
        self.run_button.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.parallel_spin.setEnabled(True)
        self.method_combo.setEnabled(True)
        self.reuse_check.setEnabled(True)
        self._on_method_or_mode_changed()

    # ------------------------------------------------------------------ 終了
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt のシグネチャ)
        if self._thread is not None and self._thread.isRunning():
            answer = QMessageBox.question(
                self,
                "終了確認",
                "一括実行中です。中断して閉じますか?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            if self._worker is not None:
                self._worker.request_stop()
            self._thread.quit()
            self._thread.wait(5000)
        super().closeEvent(event)
