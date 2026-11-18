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
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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
from ui.comparison_tabs import add_comparison_tabs, add_framework_tabs
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
        self.export_button = QPushButton("表示中の結果をCSVに書き出す")
        self.export_button.setToolTip(
            "一覧に表示されている (絞り込み後の) 実行結果をまとめて、解析用の CSV を書き出します。\n"
            "summary.csv: 仕様×モデルごとの状態・時間・トークン数・不一致件数\n"
            "rule_results.csv: 仕様×ルール×モデル×動作ごとの値と判定 (ロング形式)"
        )
        self.export_button.clicked.connect(self._export_csv)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("仕様名・番号・テーマ・モデル・フォルダ名で絞り込み")
        self.search_edit.textChanged.connect(self._apply_filter)
        self.method_filter = QComboBox()
        self.method_filter.addItem("すべての方式", "")
        self.method_filter.addItem("従来方式", "conventional")
        self.method_filter.addItem("新方式 (共通DT)", "common_framework")
        self.method_filter.currentIndexChanged.connect(self._apply_filter)
        filter_row = QHBoxLayout()
        filter_row.addWidget(self.search_edit, 1)
        filter_row.addWidget(self.method_filter)

        left_buttons = QHBoxLayout()
        left_buttons.addWidget(self.refresh_button)
        left_buttons.addWidget(self.open_folder_button)

        left_box = QVBoxLayout()
        left_box.addWidget(QLabel("実行履歴 (新しい順)"))
        left_box.addLayout(filter_row)
        left_box.addWidget(self.run_list, 1)
        left_box.addLayout(left_buttons)
        left_box.addWidget(self.export_button)
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
            if run.get("generation_method") == "common_framework":
                label += "  [共通DT]"
            if run.get("source_name"):
                label += f"  [{run['source_name']}]"
            if run.get("batch_name"):
                label += f"  (一括: {run['batch_name']})"
            if run.get("extraction_model_name"):
                label += f"  抽出:{run['extraction_model_name']}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, run)
            item.setToolTip(run.get("requirements_preview", ""))
            self.run_list.addItem(item)
        self._apply_filter()

        if not runs:
            self.summary_label.setText("outputs/ にまだ実験結果がありません。")
        else:
            self.summary_label.setText(f"{len(runs)} 件の実行結果があります。左の一覧から選択してください。")

    def _apply_filter(self) -> None:
        if not hasattr(self, "search_edit"):
            return
        keyword = self.search_edit.text().strip().lower()
        method = self.method_filter.currentData()
        for i in range(self.run_list.count()):
            item = self.run_list.item(i)
            run = item.data(Qt.UserRole)
            meta = " ".join(str(v) for v in (run.get("source_meta") or {}).values())
            text = " ".join([item.text(), run.get("folder_name", ""), meta,
                             " ".join(run.get("generation_models", [])), run.get("framework_id", "")]).lower()
            hidden = (bool(keyword) and keyword not in text) or (
                bool(method) and run.get("generation_method", "conventional") != method
            )
            item.setHidden(hidden)

    def _visible_runs(self) -> list[dict]:
        return [
            self.run_list.item(i).data(Qt.UserRole)
            for i in range(self.run_list.count())
            if not self.run_list.item(i).isHidden()
        ]

    def _export_csv(self) -> None:
        from datetime import datetime

        from services.result_export import export_records

        runs = self._visible_runs()
        if not runs:
            QMessageBox.information(self, "CSV 書き出し", "書き出す実行結果がありません。")
            return
        records = []
        for run in runs:
            try:
                records.append((file_manager.load_experiment_record(run["dir"]), run["dir"]))
            except (OSError, ValueError):
                continue
        dest = file_manager.OUTPUTS_DIR / "exports" / f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        try:
            n_summary, n_rules = export_records(dest, records)
        except OSError as exc:
            QMessageBox.warning(self, "CSV 書き出し", f"書き出せませんでした: {exc}")
            return
        answer = QMessageBox.question(
            self, "CSV 書き出し",
            f"{len(records)} 件の実行結果を書き出しました。\n{dest}\n"
            f"summary.csv: {n_summary} 行 / rule_results.csv: {n_rules} 行\n\nフォルダを開きますか?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            self._open_path(dest)

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
        method = record.get("generation_method") or "conventional"
        summary += "  /  方式: " + ("新方式 (共通DT枠組み)" if method == "common_framework" else "従来方式")
        summary += f"\n成功 {ok} / 失敗 {total - ok}"
        if record.get("aborted_reason"):
            summary += f"  /  ⚠ {record['aborted_reason']}"
        if record.get("framework_error"):
            summary += f"  /  ⚠ {record['framework_error']}"
        fw_source = record.get("framework_source") or {}
        if fw_source:
            summary += (
                f"\n使用した枠組み: {fw_source.get('framework_id', '')}"
                + (f" (承認 {fw_source['approved_at'].replace('T', ' ')})" if fw_source.get("approved_at") else "")
                + (" ・修正版" if fw_source.get("edited") else "")
                + (f" ・メモ: {fw_source['note']}" if fw_source.get("note") else "")
            )
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
                + ("  /  保存済みの成功結果を再利用" if result.get("reused_from_cache") else "")
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
        add_framework_tabs(self.result_tabs, record)

    # ------------------------------------------------------------ フォルダを開く
    def _open_selected_folder(self) -> None:
        run = self._selected_run()
        if run is None:
            QMessageBox.information(self, "出力フォルダを開く", "一覧から実行結果を選択してください。")
            return
        self._open_path(Path(run["dir"]))

    def _open_path(self, target: Path) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(str(target))  # noqa: S606 (Windows 標準の方法)
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except OSError as exc:
            QMessageBox.warning(self, "フォルダを開けません", str(exc))
