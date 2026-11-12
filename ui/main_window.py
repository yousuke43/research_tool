"""メインウィンドウ.

画面の流れ:
    モデル設定 -> Requirements 入力 -> 実行 -> 処理状況 -> 結果表示

実際の処理 (抽出・生成・保存) は services/experiment_runner.py が行う。
このファイルは「画面の組み立て」と「ワーカーとの接続」だけを担当する。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from models.decision_table import TableSource
from models.experiment import Experiment
from services import model_service
from services.experiment_runner import ExperimentWorker
from services.model_service import ModelFetchWorker
from services.openrouter_client import OpenRouterClient
from ui.batch_dialog import BatchDialog
from ui.comparison_view import ComparisonView
from ui.history_dialog import HistoryDialog
from ui.result_view import ResultView
from ui.settings_dialog import SettingsDialog
from utils import config_manager
from utils.config_manager import OUTPUTS_DIR


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Decision Table Experiment Tool")
        self.resize(1100, 800)

        config_manager.load_env()
        self.settings = config_manager.load_settings()
        self.client = OpenRouterClient(config_manager.get_api_key())
        self.models = model_service.load_cached_models()
        self.last_experiment: Experiment | None = None

        self._run_thread: QThread | None = None
        self._run_worker: ExperimentWorker | None = None
        self._fetch_thread: QThread | None = None
        self._fetch_worker: ModelFetchWorker | None = None

        self._build_ui()
        self._update_model_summary()
        self._check_api_key()
        # 起動時にモデル一覧を裏で更新する (失敗してもキャッシュで動作する)
        self._start_model_fetch()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        # --- モデル設定バー
        self.model_label = QLabel("")
        self.model_label.setWordWrap(True)
        self.settings_button = QPushButton("モデル設定...")
        self.settings_button.clicked.connect(self._open_settings)
        self.open_output_button = QPushButton("出力フォルダを開く")
        self.open_output_button.clicked.connect(self._open_output_dir)
        self.batch_button = QPushButton("一括実行 (dataフォルダ)...")
        self.batch_button.clicked.connect(self._open_batch_dialog)
        self.history_button = QPushButton("過去の結果を見る...")
        self.history_button.clicked.connect(self._open_history_dialog)

        settings_bar = QHBoxLayout()
        settings_bar.addWidget(self.model_label, 1)
        settings_bar.addWidget(self.settings_button)
        settings_bar.addWidget(self.batch_button)
        settings_bar.addWidget(self.history_button)
        settings_bar.addWidget(self.open_output_button)

        # --- Requirements
        self.requirements_edit = QPlainTextEdit()
        self.requirements_edit.setPlaceholderText("要求仕様をここに入力してください。")
        requirements_box = QGroupBox("Requirements")
        requirements_layout = QVBoxLayout(requirements_box)
        requirements_layout.addWidget(self.requirements_edit)

        # --- 実行 + 処理状況
        self.run_button = QPushButton("実行")
        self.run_button.setMinimumHeight(36)
        self.run_button.setMinimumWidth(140)
        self.run_button.clicked.connect(self._on_run)

        self.status_label = QLabel("待機中")
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedWidth(200)

        run_bar = QHBoxLayout()
        run_bar.addWidget(self.run_button)
        run_bar.addWidget(self.status_label, 1)
        run_bar.addWidget(self.progress_bar)

        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setFixedHeight(110)

        # --- 結果
        self.result_tabs = QTabWidget()
        self.result_tabs.setDocumentMode(True)
        self._show_placeholder()
        result_box = QGroupBox("Results")
        result_layout = QVBoxLayout(result_box)
        result_layout.addWidget(self.result_tabs)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(requirements_box)
        splitter.addWidget(result_box)
        splitter.setSizes([280, 520])

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(settings_bar)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        layout.addWidget(line)
        layout.addWidget(splitter, 1)
        layout.addLayout(run_bar)
        layout.addWidget(QLabel("処理状況"))
        layout.addWidget(self.log_edit)
        self.setCentralWidget(central)

        self.statusBar().showMessage("準備完了")

    def _show_placeholder(self) -> None:
        self.result_tabs.clear()
        placeholder = QLabel(
            "Requirements を入力して「実行」を押すと、\n"
            "Decision Items の抽出と各モデルの Decision Table 生成をまとめて行います。"
        )
        placeholder.setAlignment(Qt.AlignCenter)
        placeholder.setStyleSheet("color: #666;")
        self.result_tabs.addTab(placeholder, "結果")

    # --------------------------------------------------------------- ログ
    def _log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_edit.appendPlainText(f"[{stamp}] {message}")

    def _set_status(self, message: str) -> None:
        self.status_label.setText(message)

    # ----------------------------------------------------------- モデル設定
    def _check_api_key(self) -> None:
        if not config_manager.get_api_key():
            self._log(
                "OPENROUTER_API_KEY が未設定です。"
                ".env に OPENROUTER_API_KEY=... を設定してください。"
            )
            self._set_status("APIキー未設定")

    def _update_model_summary(self) -> None:
        extraction = self.settings.get("extraction_model", "")
        generation = self.settings.get("generation_models", [])

        if not extraction and not generation:
            self.model_label.setText(
                "モデル未設定です。「モデル設定...」からモデルを選択してください。"
            )
            self.model_label.setStyleSheet("color: #b00020;")
            return

        extraction_text = (
            f"{model_service.display_name(self.models, extraction)} ({extraction})"
            if extraction
            else "(未選択)"
        )
        generation_text = ", ".join(generation) if generation else "(未選択)"
        text = (
            f"抽出モデル: {extraction_text}\n"
            f"生成モデル ({len(generation)}): {generation_text}"
        )

        missing = model_service.find_missing(
            ([extraction] if extraction else []) + list(generation), self.models
        )
        if missing:
            text += "\n⚠ OpenRouter 上に見つからないモデル: " + ", ".join(missing)
            self.model_label.setStyleSheet("color: #b00020;")
        else:
            self.model_label.setStyleSheet("")
        self.model_label.setText(text)

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.client, self.settings, self.models, self)
        accepted = dialog.exec() == SettingsDialog.Accepted
        # ダイアログ内で一覧を更新している場合があるので取り込む
        self.models = dialog.models
        if accepted:
            self.settings = dialog.selected_settings()
            try:
                config_manager.save_settings(self.settings)
                self._log(f"モデル設定を保存しました: {config_manager.SETTINGS_PATH}")
            except OSError as exc:
                QMessageBox.warning(self, "保存に失敗", f"設定を保存できませんでした:\n{exc}")
        self._update_model_summary()

    # ------------------------------------------------------------ 一括実行
    def _open_batch_dialog(self) -> None:
        if not config_manager.get_api_key():
            QMessageBox.critical(
                self,
                "APIキー未設定",
                "OPENROUTER_API_KEY が設定されていません。\n"
                f"{config_manager.ROOT_DIR / '.env'} に\n"
                "OPENROUTER_API_KEY=xxxxx\n"
                "を設定してからアプリを再起動してください。",
            )
            return
        dialog = BatchDialog(self.client, self.settings, self.models, self)
        dialog.exec()

    # ------------------------------------------------------------ 過去の結果
    def _open_history_dialog(self) -> None:
        dialog = HistoryDialog(self)
        dialog.exec()

    # --------------------------------------------------- モデル一覧の裏取得
    def _start_model_fetch(self) -> None:
        if self._fetch_thread is not None:
            return
        self._fetch_thread = QThread(self)
        self._fetch_worker = ModelFetchWorker(self.client)
        self._fetch_worker.moveToThread(self._fetch_thread)
        self._fetch_thread.started.connect(self._fetch_worker.run)
        self._fetch_worker.finished.connect(self._on_models_fetched)
        self._fetch_worker.failed.connect(self._on_models_fetch_failed)
        self._fetch_worker.finished.connect(self._fetch_thread.quit)
        self._fetch_worker.failed.connect(self._fetch_thread.quit)
        self._fetch_thread.finished.connect(self._on_fetch_thread_finished)
        self._fetch_thread.start()
        self._log("モデル一覧を取得中...")

    def _on_models_fetched(self, models: list) -> None:
        self.models = models
        self._log(f"モデル一覧を取得しました ({len(models)} モデル)")
        self._update_model_summary()

    def _on_models_fetch_failed(self, message: str) -> None:
        # 取得に失敗してもアプリは動かし続ける (キャッシュがあればそれを使う)
        self._log(f"[警告] {message}")
        if self.models:
            self._log(f"キャッシュのモデル一覧を使用します ({len(self.models)} モデル)")
        else:
            self._log("モデル一覧がありません。「モデル設定...」から再取得してください。")

    def _on_fetch_thread_finished(self) -> None:
        if self._fetch_worker is not None:
            self._fetch_worker.deleteLater()
            self._fetch_worker = None
        if self._fetch_thread is not None:
            self._fetch_thread.deleteLater()
            self._fetch_thread = None

    # ------------------------------------------------------------------ 実行
    def _on_run(self) -> None:
        if self._run_thread is not None:
            return

        requirements = self.requirements_edit.toPlainText().strip()
        if not requirements:
            QMessageBox.warning(self, "実行", "Requirements を入力してください。")
            return

        if not config_manager.get_api_key():
            QMessageBox.critical(
                self,
                "APIキー未設定",
                "OPENROUTER_API_KEY が設定されていません。\n"
                f"{config_manager.ROOT_DIR / '.env'} に\n"
                "OPENROUTER_API_KEY=xxxxx\n"
                "を設定してからアプリを再起動してください。",
            )
            return

        extraction = self.settings.get("extraction_model", "")
        generation = list(self.settings.get("generation_models", []))
        if not extraction or not generation:
            QMessageBox.warning(
                self,
                "モデル未設定",
                "抽出モデルと生成モデルを設定してください。\n"
                "「モデル設定...」から選択できます。",
            )
            return

        names = {m.get("id", ""): m.get("name", "") for m in self.models}
        names = {k: v for k, v in names.items() if k and v}

        self.log_edit.clear()
        self.result_tabs.clear()
        self._show_placeholder()
        self.run_button.setEnabled(False)
        self.settings_button.setEnabled(False)
        self.progress_bar.setRange(0, 0)  # 不定 (処理中)
        self._set_status("実行中...")
        self._log(
            f"実験開始 (抽出: {extraction} / 生成: {len(generation)} モデル)"
        )

        self._run_thread = QThread(self)
        self._run_worker = ExperimentWorker(
            client=self.client,
            requirements=requirements,
            extraction_model=extraction,
            generation_models=generation,
            names=names,
        )
        self._run_worker.moveToThread(self._run_thread)
        self._run_thread.started.connect(self._run_worker.run)
        self._run_worker.progress.connect(self._on_progress)
        self._run_worker.finished.connect(self._on_experiment_finished)
        self._run_worker.failed.connect(self._on_experiment_failed)
        self._run_worker.finished.connect(self._run_thread.quit)
        self._run_worker.failed.connect(self._run_thread.quit)
        self._run_thread.finished.connect(self._on_run_thread_finished)
        self._run_thread.start()

    def _on_progress(self, message: str) -> None:
        self._set_status(message)
        self._log(message)

    def _on_experiment_finished(self, experiment: Experiment) -> None:
        self.last_experiment = experiment
        self._show_results(experiment)
        ok = sum(1 for r in experiment.results if r.ok)
        ng = len(experiment.results) - ok
        summary = f"完了 (成功 {ok} / 失敗 {ng})"
        self._set_status(summary)
        if experiment.output_dir:
            self.statusBar().showMessage(f"{summary}  保存先: {experiment.output_dir}")
        else:
            self.statusBar().showMessage(summary)

    def _on_experiment_failed(self, message: str) -> None:
        self._set_status("エラー")
        self._log(message.replace("\n", " "))
        QMessageBox.critical(self, "実行に失敗しました", message)

    def _on_run_thread_finished(self) -> None:
        if self._run_worker is not None:
            self._run_worker.deleteLater()
            self._run_worker = None
        if self._run_thread is not None:
            self._run_thread.deleteLater()
            self._run_thread = None
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        self.run_button.setEnabled(True)
        self.settings_button.setEnabled(True)

    # -------------------------------------------------------------- 結果表示
    def _show_results(self, experiment: Experiment) -> None:
        self.result_tabs.clear()

        items_view = ResultView(
            experiment.decision_items,
            caption=f"抽出モデル: {experiment.extraction_model}",
        )
        self.result_tabs.addTab(items_view, "Extracted Decision Items")

        for result in experiment.results:
            if result.ok:
                view = ResultView(
                    result.content,
                    caption=f"{result.model_id}  ({result.elapsed_sec:.1f}秒)",
                )
                title = result.model_name or result.model_id
            else:
                view = ResultView(
                    f"# 生成失敗\n\n**model:** `{result.model_id}`\n\n"
                    f"```\nAPI Error: {result.error}\n```\n",
                    caption=f"{result.model_id}  (生成失敗)",
                )
                title = f"⚠ {result.model_name or result.model_id}"
            index = self.result_tabs.addTab(view, title)
            self.result_tabs.setTabToolTip(index, result.model_id)

        if experiment.results:
            sources = [
                TableSource(
                    key=result.model_id,
                    title=result.model_name or result.model_id,
                    content=result.content,
                    ok=result.ok,
                )
                for result in experiment.results
            ]
            self.result_tabs.addTab(
                ComparisonView(sources, document=experiment.requirements, output_dir=experiment.output_dir),
                "比較",
            )

        self.result_tabs.setCurrentIndex(0)

    # ------------------------------------------------------------ 出力フォルダ
    def _open_output_dir(self) -> None:
        target = Path(self.last_experiment.output_dir) if (
            self.last_experiment and self.last_experiment.output_dir
        ) else OUTPUTS_DIR
        target.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(str(target))  # noqa: S606 (Windows 標準の方法)
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except OSError as exc:
            QMessageBox.warning(self, "フォルダを開けません", str(exc))

    # ------------------------------------------------------------------ 終了
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt のシグネチャ)
        if self._run_thread is not None and self._run_thread.isRunning():
            answer = QMessageBox.question(
                self,
                "終了確認",
                "実行中です。終了しますか?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        for thread in (self._run_thread, self._fetch_thread):
            if thread is not None:
                thread.quit()
                thread.wait(3000)
        super().closeEvent(event)
