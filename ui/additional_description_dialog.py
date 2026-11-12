"""解釈が分かれた箇所に対する「追加記述案」を作成するダイアログ.

1. 差異を指定する
     - 自動検出: 比較タブで「同じ条件の組み合わせなのに結果が違う」と判定された箇所
     - 比較表で選択: 比較タブの表で選択していた組み合わせ
     - 手動: 任意のモデルの任意のルール列をひな形にして、別の解釈を書き足す / 自由入力
2. 条件・解釈 (結果動作) を必要に応じて編集する -> 差異の文章が自動で組み立てられる
   (差異の文章そのものを直接書き換えてもよい)
3. 「追加記述案を生成」で LLM に問い合わせ、結果を実行結果フォルダの
   additional_descriptions/ に保存する

計算・保存は services/additional_description_service.py が行い、ここは画面だけを担当する。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from models.decision_table import DecisionTable
from services import additional_description_service as ads
from services import model_service
from services.openrouter_client import OpenRouterClient
from services.table_comparison import ComparisonGroup, ComparisonResult
from utils import config_manager


class _GenerateWorker(QObject):
    finished = Signal(object, float)  # ChatResult, 所要時間
    failed = Signal(str)

    def __init__(self, client: OpenRouterClient, model_id: str, document: str, difference: str) -> None:
        super().__init__()
        self.client = client
        self.model_id = model_id
        self.document = document
        self.difference = difference

    def run(self) -> None:
        started = time.perf_counter()
        try:
            result = ads.request_additional_description(self.client, self.model_id, self.document, self.difference)
        except Exception as exc:  # 通信エラー・プロンプト不備などはすべて画面に出す
            self.failed.emit(str(exc))
            return
        self.finished.emit(result, time.perf_counter() - started)


class AdditionalDescriptionDialog(QDialog):
    def __init__(
        self,
        document: str,
        output_dir: str,
        tables: dict[str, DecisionTable],
        titles: dict[str, str],
        comparison: ComparisonResult | None = None,
        selected_groups: list[tuple[int, ComparisonGroup]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("追加記述案の作成")
        self.resize(1000, 780)

        self._document = document
        self._output_dir = output_dir
        self._tables = tables
        self._titles = titles
        self._comparison = comparison
        self._current_spec = ads.DifferenceSpec()
        self._updating = False

        config_manager.load_env()
        self._client = OpenRouterClient(config_manager.get_api_key())
        self._models = model_service.load_cached_models()
        self._thread: QThread | None = None
        self._worker: _GenerateWorker | None = None
        self._last_saved: Path | None = None

        self._build_ui()
        self._populate_candidates(selected_groups or [])
        self._on_candidate_changed()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        # --- 差異の選択
        self.candidate_combo = QComboBox()
        self.candidate_combo.currentIndexChanged.connect(self._on_candidate_changed)
        candidate_row = QHBoxLayout()
        candidate_row.addWidget(QLabel("差異の指定:"))
        candidate_row.addWidget(self.candidate_combo, 1)

        self.condition_edit = QPlainTextEdit()
        self.condition_edit.setPlaceholderText("条件を1行に1つずつ (例: 来場人数が1人である（成立）)")
        self.condition_edit.textChanged.connect(self._on_fields_changed)

        self.interp_table = QTableWidget(0, 2)
        self.interp_table.setHorizontalHeaderLabels(["結果動作 (解釈)", "その解釈をとったモデル・ルール (記録用)"])
        self.interp_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.interp_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.interp_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.interp_table.itemChanged.connect(self._on_fields_changed)
        add_button = QPushButton("解釈を追加")
        add_button.clicked.connect(self._add_interpretation_row)
        remove_button = QPushButton("選択した解釈を削除")
        remove_button.clicked.connect(self._remove_interpretation_rows)
        interp_buttons = QHBoxLayout()
        interp_buttons.addWidget(add_button)
        interp_buttons.addWidget(remove_button)
        interp_buttons.addStretch(1)

        self.difference_edit = QPlainTextEdit()
        self.difference_edit.setPlaceholderText("条件が「…」の時に、結果動作が「…」と「…」になる。")
        rebuild_button = QPushButton("条件・解釈から文章を作り直す")
        rebuild_button.clicked.connect(self._rebuild_difference)
        diff_header = QHBoxLayout()
        diff_header.addWidget(QLabel("LLM に渡す差異の文章 (直接編集可):"))
        diff_header.addStretch(1)
        diff_header.addWidget(rebuild_button)

        spec_group = QGroupBox("1. 解釈が分かれている箇所")
        spec_layout = QVBoxLayout(spec_group)
        spec_layout.addLayout(candidate_row)
        spec_layout.addWidget(QLabel("条件:"))
        spec_layout.addWidget(self.condition_edit, 2)
        spec_layout.addWidget(QLabel("分かれた解釈 (結果動作は直接編集できます):"))
        spec_layout.addWidget(self.interp_table, 2)
        spec_layout.addLayout(interp_buttons)
        spec_layout.addLayout(diff_header)
        spec_layout.addWidget(self.difference_edit, 1)

        # --- 生成
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self._populate_models()
        self.generate_button = QPushButton("追加記述案を生成")
        self.generate_button.clicked.connect(self._on_generate)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        gen_row = QHBoxLayout()
        gen_row.addWidget(QLabel("モデル:"))
        gen_row.addWidget(self.model_combo, 1)
        gen_row.addWidget(self.generate_button)

        self.result_edit = QPlainTextEdit()
        self.result_edit.setReadOnly(True)
        self.result_edit.setPlaceholderText("ここに追加記述案が表示されます。")
        self.open_folder_button = QPushButton("保存先フォルダを開く")
        self.open_folder_button.clicked.connect(self._open_save_dir)
        result_buttons = QHBoxLayout()
        result_buttons.addWidget(self.status_label, 1)
        result_buttons.addWidget(self.open_folder_button)

        gen_group = QGroupBox("2. 追加記述案")
        gen_layout = QVBoxLayout(gen_group)
        gen_layout.addLayout(gen_row)
        gen_layout.addWidget(self.result_edit, 1)
        gen_layout.addLayout(result_buttons)

        save_note = QLabel(f"保存先: {self._save_dir()}")
        save_note.setStyleSheet("color: #555;")
        save_note.setTextInteractionFlags(Qt.TextSelectableByMouse)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(spec_group)
        splitter.addWidget(gen_group)
        splitter.setSizes([460, 320])

        layout = QVBoxLayout(self)
        layout.addWidget(save_note)
        layout.addWidget(splitter, 1)

    def _populate_models(self) -> None:
        settings = config_manager.load_settings()
        preferred = [settings.get("extraction_model", "")] + list(settings.get("generation_models", []))
        seen: set[str] = set()
        for model_id in preferred + [m.get("id", "") for m in self._models]:
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            name = model_service.display_name(self._models, model_id)
            self.model_combo.addItem(model_id if name == model_id else f"{name}  [{model_id}]", model_id)
        self.model_combo.setCurrentIndex(0 if self.model_combo.count() else -1)

    # ------------------------------------------------------------ 差異の候補
    def _populate_candidates(self, selected_groups: list[tuple[int, ComparisonGroup]]) -> None:
        """コンボボックスに差異の候補を並べる. itemData は DifferenceSpec を返す関数."""
        combo = self.candidate_combo
        comparison = self._comparison

        if comparison is not None:
            for number, group in selected_groups:
                combo.addItem(
                    f"比較表で選択: 組合せ{number}",
                    lambda g=group, n=number: ads.spec_from_comparison_group(comparison, g, self._titles, n),
                )
            for number, group in enumerate(comparison.groups, start=1):
                if not group.has_conflict:
                    continue
                spec = ads.spec_from_comparison_group(comparison, group, self._titles, number)
                combo.addItem(
                    f"自動検出: 組合せ{number}  {'、'.join(spec.conditions)[:60]}",
                    lambda g=group, n=number: ads.spec_from_comparison_group(comparison, g, self._titles, n),
                )

        for key, table in self._tables.items():
            title = self._titles.get(key, key)
            for index, rule_id in enumerate(table.rule_ids):
                combo.addItem(
                    f"手動: {title} の {rule_id} をひな形にする",
                    lambda t=table, i=index, ti=title: ads.spec_from_rule(t, i, ti),
                )

        combo.addItem(
            "手動: 自由入力 (空欄から)",
            lambda: ads.DifferenceSpec(interpretations=[ads.Interpretation(""), ads.Interpretation("")]),
        )

    def _on_candidate_changed(self) -> None:
        factory = self.candidate_combo.currentData()
        if factory is None:
            return
        self._current_spec = factory()
        self._load_spec_into_fields(self._current_spec)

    def _load_spec_into_fields(self, spec: ads.DifferenceSpec) -> None:
        self._updating = True
        try:
            self.condition_edit.setPlainText("\n".join(spec.conditions))
            self.interp_table.setRowCount(0)
            for interp in spec.interpretations:
                self._append_interpretation(interp.outcome, ", ".join(interp.sources))
        finally:
            self._updating = False
        self._rebuild_difference()

    def _append_interpretation(self, outcome: str, sources: str) -> None:
        row = self.interp_table.rowCount()
        self.interp_table.insertRow(row)
        self.interp_table.setItem(row, 0, QTableWidgetItem(outcome))
        self.interp_table.setItem(row, 1, QTableWidgetItem(sources))

    def _add_interpretation_row(self) -> None:
        self._append_interpretation("", "")
        self.interp_table.editItem(self.interp_table.item(self.interp_table.rowCount() - 1, 0))

    def _remove_interpretation_rows(self) -> None:
        rows = sorted({i.row() for i in self.interp_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.interp_table.removeRow(row)
        self._on_fields_changed()

    def _spec_from_fields(self) -> ads.DifferenceSpec:
        conditions = [line.strip() for line in self.condition_edit.toPlainText().splitlines() if line.strip()]
        interpretations = []
        for row in range(self.interp_table.rowCount()):
            outcome_item = self.interp_table.item(row, 0)
            source_item = self.interp_table.item(row, 1)
            outcome = outcome_item.text().strip() if outcome_item else ""
            sources_text = source_item.text().strip() if source_item else ""
            sources = [s.strip() for s in sources_text.split(",") if s.strip()]
            if outcome:
                interpretations.append(ads.Interpretation(outcome=outcome, sources=sources))
        return ads.DifferenceSpec(
            conditions=conditions,
            interpretations=interpretations,
            origin=self._current_spec.origin,
            note=self._current_spec.note,
        )

    def _on_fields_changed(self, *_args) -> None:
        if not self._updating:
            self._rebuild_difference()

    def _rebuild_difference(self) -> None:
        self.difference_edit.setPlainText(ads.format_difference(self._spec_from_fields()))

    # ------------------------------------------------------------------ 生成
    def _selected_model(self) -> tuple[str, str]:
        text = self.model_combo.currentText().strip()
        index = self.model_combo.findText(text)
        model_id = self.model_combo.itemData(index) if index >= 0 else text
        return model_id, model_service.display_name(self._models, model_id)

    def _on_generate(self) -> None:
        model_id, _ = self._selected_model()
        difference = self.difference_edit.toPlainText().strip()
        spec = self._spec_from_fields()
        if not model_id:
            QMessageBox.warning(self, "追加記述案", "モデルを選択してください。")
            return
        if len(spec.interpretations) < 2:
            answer = QMessageBox.question(
                self,
                "追加記述案",
                "解釈 (結果動作) が2つ未満です。このまま生成しますか?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        if not difference:
            QMessageBox.warning(self, "追加記述案", "差異の文章が空です。")
            return

        self._pending = (model_id, difference, spec)
        self.generate_button.setEnabled(False)
        self.status_label.setStyleSheet("")
        self.status_label.setText(f"生成中... ({model_id})")
        self.result_edit.clear()

        self._thread = QThread(self)
        self._worker = _GenerateWorker(self._client, model_id, self._document, difference)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_generated)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

    def _on_generated(self, chat_result, elapsed: float) -> None:
        model_id, difference, spec = self._pending
        self.result_edit.setPlainText(chat_result.content)
        try:
            path = ads.save_additional_description(
                self._save_dir().parent,
                model_id=model_id,
                model_name=model_service.display_name(self._models, model_id),
                document=self._document,
                difference=difference,
                spec=spec,
                chat_result=chat_result,
                elapsed_sec=elapsed,
            )
        except OSError as exc:
            self.status_label.setStyleSheet("color: #b00020;")
            self.status_label.setText(f"生成しましたが保存に失敗しました: {exc}")
            return
        self._last_saved = path
        self.status_label.setStyleSheet("color: #1a7a1a;")
        self.status_label.setText(f"完了 ({elapsed:.1f}秒)  保存: {path.name}")

    def _on_failed(self, message: str) -> None:
        self.status_label.setStyleSheet("color: #b00020;")
        self.status_label.setText(f"生成に失敗しました: {message}")

    def _on_thread_finished(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        if self._thread is not None:
            self._thread.deleteLater()
            self._thread = None
        self.generate_button.setEnabled(True)

    # ------------------------------------------------------------ 保存先
    def _save_dir(self) -> Path:
        base = Path(self._output_dir) if self._output_dir else config_manager.OUTPUTS_DIR
        return base / ads.SAVE_SUBDIR

    def _open_save_dir(self) -> None:
        target = self._save_dir()
        target.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(str(target))  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except OSError as exc:
            QMessageBox.warning(self, "フォルダを開けません", str(exc))

    def reject(self) -> None:  # 閉じる (Esc / ×) 時
        if self._thread is not None and self._thread.isRunning():
            QMessageBox.information(self, "追加記述案", "生成中です。完了するまでお待ちください。")
            return
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._thread is not None and self._thread.isRunning():
            QMessageBox.information(self, "追加記述案", "生成中です。完了するまでお待ちください。")
            event.ignore()
            return
        super().closeEvent(event)
