"""過去に保存した実験結果 (outputs/ 配下) を一覧・閲覧するダイアログ.

results.json を読み込み、実行直後の結果画面と同じタブ構成
(Extracted Decision Items + モデルごと) で再表示する。
一括実行で溜まった結果を見比べるときの分析用。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from models.decision_table import TableSource
from ui.comparison_tabs import add_comparison_tabs
from ui.result_view import ResultView
from utils import file_manager


def _format_tokens(record: dict, prefix: str = "") -> str:
    prompt = record.get(f"{prefix}prompt_tokens")
    completion = record.get(f"{prefix}completion_tokens")
    total = record.get(f"{prefix}total_tokens")
    if not any([prompt, completion, total]):
        return ""
    return f"  /  トークン: 入力{prompt or 0} + 出力{completion or 0} = 計{total or 0}"


class HistoryDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("過去の実行結果")
        self.resize(1100, 760)
        self._build_ui()
        self._reload_runs()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.run_list = QListWidget()
        self.run_list.currentItemChanged.connect(self._on_selection_changed)

        self.refresh_button = QPushButton("一覧を更新")
        self.refresh_button.clicked.connect(self._reload_runs)
        self.open_folder_button = QPushButton("出力フォルダを開く")
        self.open_folder_button.clicked.connect(self._open_selected_folder)

        left_buttons = QHBoxLayout()
        left_buttons.addWidget(self.refresh_button)
        left_buttons.addWidget(self.open_folder_button)

        left_box = QVBoxLayout()
        left_box.addWidget(QLabel("実行履歴 (新しい順)"))
        left_box.addWidget(self.run_list, 1)
        left_box.addLayout(left_buttons)
        left_widget = QWidget()
        left_widget.setLayout(left_box)

        self.summary_label = QLabel("左の一覧から実行結果を選択してください。")
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("color: #555;")

        self.result_tabs = QTabWidget()
        self.result_tabs.setDocumentMode(True)

        right_box = QVBoxLayout()
        right_box.addWidget(self.summary_label)
        right_box.addWidget(self.result_tabs, 1)
        right_widget = QWidget()
        right_widget.setLayout(right_box)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setSizes([320, 780])

        layout = QVBoxLayout(self)
        layout.addWidget(splitter, 1)

    # -------------------------------------------------------------- 一覧表示
    def _reload_runs(self) -> None:
        self.run_list.clear()
        runs = file_manager.list_output_runs()
        for run in runs:
            label = run["started_at"] or run["folder_name"]
            if run.get("source_name"):
                label += f"  [{run['source_name']}]"
            if run.get("extraction_model_name"):
                label += f"  抽出:{run['extraction_model_name']}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, run)
            item.setToolTip(run.get("requirements_preview", ""))
            self.run_list.addItem(item)

        if not runs:
            self.summary_label.setText("outputs/ にまだ実験結果がありません。")
        else:
            self.summary_label.setText(f"{len(runs)} 件の実行結果があります。左の一覧から選択してください。")

    def _selected_run(self) -> dict | None:
        item = self.run_list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def _on_selection_changed(self) -> None:
        run = self._selected_run()
        self.result_tabs.clear()
        if run is None:
            return

        try:
            record = file_manager.load_experiment_record(run["dir"])
        except (OSError, ValueError) as exc:
            self.summary_label.setText(f"読み込みに失敗しました: {exc}")
            return

        self._show_record(run, record)

    def _show_record(self, run: dict, record: dict) -> None:
        ok = sum(1 for r in record.get("results", []) if r.get("status") == "ok")
        total = len(record.get("results", []))
        summary = (
            f"フォルダ: {run['dir']}\n"
            f"実行日時: {record.get('started_at', '-')}"
        )
        if record.get("source_name"):
            summary += f"  /  仕様ファイル: {record['source_name']}"
        summary += f"\n成功 {ok} / 失敗 {total - ok}"
        self.summary_label.setText(summary)

        extraction_caption = (
            f"抽出モデル: {record.get('extraction_model', '')}"
            f"  ({record.get('extraction_elapsed_sec', 0):.1f}秒)"
            f"{_format_tokens(record, 'extraction_')}"
        )
        items_view = ResultView(record.get("decision_items", ""), caption=extraction_caption)
        self.result_tabs.addTab(items_view, "Extracted Decision Items")

        for result in record.get("results", []):
            ok_flag = result.get("status") == "ok"
            caption = (
                f"{result.get('model_id', '')}  ({result.get('elapsed_sec', 0):.1f}秒)"
                f"{_format_tokens(result)}"
            )
            if ok_flag:
                view = ResultView(result.get("content", ""), caption=caption)
                title = result.get("model_name") or result.get("model_id", "")
            else:
                view = ResultView(
                    f"# 生成失敗\n\n**model:** `{result.get('model_id', '')}`\n\n"
                    f"```\nAPI Error: {result.get('error', '')}\n```\n",
                    caption=caption + "  (生成失敗)",
                )
                title = f"⚠ {result.get('model_name') or result.get('model_id', '')}"
            index = self.result_tabs.addTab(view, title)
            self.result_tabs.setTabToolTip(index, result.get("model_id", ""))

        results = record.get("results", [])
        if results:
            sources = [
                TableSource(
                    key=r.get("model_id") or str(i),
                    title=r.get("model_name") or r.get("model_id", ""),
                    content=r.get("content", ""),
                    ok=r.get("status") == "ok",
                )
                for i, r in enumerate(results)
            ]
            add_comparison_tabs(self.result_tabs, sources, record.get("requirements", ""), run["dir"])
        elif self.result_tabs.count():
            self.result_tabs.setCurrentIndex(0)

    # ------------------------------------------------------------ フォルダを開く
    def _open_selected_folder(self) -> None:
        run = self._selected_run()
        if run is None:
            QMessageBox.information(self, "出力フォルダを開く", "一覧から実行結果を選択してください。")
            return
        target = Path(run["dir"])
        try:
            if sys.platform == "win32":
                os.startfile(str(target))  # noqa: S606 (Windows 標準の方法)
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except OSError as exc:
            QMessageBox.warning(self, "フォルダを開けません", str(exc))
