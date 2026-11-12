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
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from models.experiment import Experiment
from models.spec_source import SpecSource
from services.batch_runner import BatchWorker
from services.openrouter_client import OpenRouterClient
from utils import file_manager
from utils.config_manager import DATA_DIR


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

        self._build_ui()
        self._reload_files()

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
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_bar)
        layout.addWidget(QLabel("処理状況"))
        layout.addWidget(self.log_edit)
        layout.addLayout(bottom_buttons)

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
        self._set_status(f"実行中... (0/{len(sources)})")
        self._log(f"一括実行開始 ({len(sources)} 件)")

        self._thread = QThread(self)
        self._worker = BatchWorker(
            client=self.client,
            sources=sources,
            extraction_model=extraction,
            generation_models=generation,
            names=names,
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
        self.progress_bar.setValue(index - 1)
        self._set_status(f"実行中... ({index}/{total}) {filename}")

    def _on_file_finished(self, filename: str, experiment: object) -> None:
        exp = experiment if isinstance(experiment, Experiment) else None
        if exp is not None:
            self.completed_experiments.append(exp)
            ok = sum(1 for r in exp.results if r.ok)
            ng = len(exp.results) - ok
            self._log(f"{filename}: 完了 (成功 {ok} / 失敗 {ng})")
        self.progress_bar.setValue(self.progress_bar.value() + 1)

    def _on_file_failed(self, filename: str, message: str) -> None:
        self._log(f"{filename}: [エラー] {message}")
        self.progress_bar.setValue(self.progress_bar.value() + 1)

    def _on_batch_finished(self, experiments: list) -> None:
        self._set_status(f"完了 ({len(experiments)} / {self.progress_bar.maximum()} 件保存)")
        self._log("一括実行が完了しました")

    def _on_thread_finished(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        if self._thread is not None:
            self._thread.deleteLater()
            self._thread = None
        self.run_button.setEnabled(True)
        self.refresh_button.setEnabled(True)

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
