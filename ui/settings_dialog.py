"""モデル設定ダイアログ.

OpenRouter から取得したモデル一覧を表示し、
 - 抽出モデル: 1つ
 - 生成モデル: 複数 (チェックボックス)
を選択する。あわせて生成設定 (新方式の条件数上限・バッチサイズ・出力上限 max_tokens、
成功結果の再利用) も設定する。選択内容は呼び出し側 (MainWindow) が settings.json へ保存する。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from services import model_service
from services.model_service import ModelFetchWorker
from services.openrouter_client import OpenRouterClient
from utils.config_manager import DEFAULT_SETTINGS

COLUMNS = ["表示名", "Model ID", "コンテキスト"]


def _context_text(model: dict) -> str:
    length = model.get("context_length") or 0
    return f"{length:,}" if length else "-"


class SettingsDialog(QDialog):
    def __init__(
        self,
        client: OpenRouterClient,
        settings: dict,
        models: list[dict],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("モデル設定")
        self.resize(1000, 640)

        self.client = client
        self.models = list(models or [])
        self._settings = dict(settings)
        self._thread: QThread | None = None
        self._worker: ModelFetchWorker | None = None

        self._build_ui()
        self._populate()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("モデル名 / Model ID で絞り込み")
        self.search_edit.textChanged.connect(self._apply_filter)

        self.refresh_button = QPushButton("モデル一覧を更新")
        self.refresh_button.clicked.connect(self._refresh_models)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #555;")

        top = QHBoxLayout()
        top.addWidget(self.search_edit, 1)
        top.addWidget(self.refresh_button)

        self.extract_tree = QTreeWidget()
        self.extract_tree.setHeaderLabels(COLUMNS)
        self.extract_tree.setRootIsDecorated(False)
        self.extract_tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.extract_tree.itemSelectionChanged.connect(self._update_selection_label)

        self.generate_tree = QTreeWidget()
        self.generate_tree.setHeaderLabels(COLUMNS)
        self.generate_tree.setRootIsDecorated(False)
        self.generate_tree.itemChanged.connect(self._update_selection_label)

        extract_box = QGroupBox("抽出モデル (Decision Items / 1つ選択)")
        extract_layout = QVBoxLayout(extract_box)
        extract_layout.addWidget(self.extract_tree)

        generate_box = QGroupBox("生成モデル (Decision Table / 複数選択)")
        generate_layout = QVBoxLayout(generate_box)
        generate_layout.addWidget(self.generate_tree)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(extract_box)
        splitter.addWidget(generate_box)
        splitter.setSizes([480, 480])

        self.selection_label = QLabel("")
        self.selection_label.setWordWrap(True)
        self.warning_label = QLabel("")
        self.warning_label.setStyleSheet("color: #b00020;")
        self.warning_label.setWordWrap(True)

        generation_box = self._build_generation_settings()

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.status_label)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.selection_label)
        layout.addWidget(self.warning_label)
        layout.addWidget(generation_box)
        layout.addWidget(self.buttons)

    def _setting(self, key: str):
        return self._settings.get(key, DEFAULT_SETTINGS[key])

    def _spin(self, key: str, low: int, high: int, step: int, tooltip: str, special: str = "") -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setSingleStep(step)
        spin.setValue(int(self._setting(key)))
        spin.setToolTip(tooltip)
        if special:
            spin.setSpecialValueText(special)
        return spin

    def _build_generation_settings(self) -> QGroupBox:
        box = QGroupBox("生成設定")
        self.max_conditions_spin = self._spin(
            "framework_max_conditions", 1, 12, 1,
            "新方式で全組合せ (2^n 通り) を自動生成する条件数の上限。\n"
            "これを超える場合は警告を出し、共通DTの生成は行いません (抽出結果は保存します)。",
        )
        self.batch_size_spin = self._spin(
            "framework_batch_size", 1, 4096, 8,
            "新方式で1回のリクエストに含めるルール数。ルール数がこれを超えると複数バッチに分けて生成し、\n"
            "最後に統合します (ルールIDと条件組合せはバッチ間でも固定)。",
        )
        self.framework_tokens_spin = self._spin(
            "framework_max_tokens", 0, 1_000_000, 1000,
            "新方式の出力上限 (max_tokens)。推論 (reasoning) トークンも含まれるモデルがあるため、\n"
            "小さくしすぎると途中で切れます。0 = 指定しない (モデルの既定値)。",
            special="指定しない",
        )
        self.conventional_tokens_spin = self._spin(
            "conventional_max_tokens", 0, 1_000_000, 1000,
            "従来方式の出力上限 (max_tokens)。0 = 指定しない (これまでどおり)。",
            special="指定しない",
        )
        self.extraction_tokens_spin = self._spin(
            "extraction_max_tokens", 0, 1_000_000, 1000,
            "抽出の出力上限 (max_tokens)。0 = 指定しない (これまでどおり)。",
            special="指定しない",
        )
        self.reuse_check = QCheckBox("成功済みの抽出・生成結果を再利用する (失敗した処理だけ再実行)")
        self.reuse_check.setChecked(bool(self._setting("reuse_cached_results")))
        self.reuse_check.setToolTip(
            "モデル・要求仕様・プロンプト・生成方式・抽出項目 (実際に送るプロンプト全文) がすべて同じ場合だけ、\n"
            "以前に成功した結果を再利用します。同じ条件で再サンプリングしたい場合はチェックを外してください。"
        )

        form = QFormLayout(box)
        form.addRow("新方式: 全組合せを生成する条件数の上限", self.max_conditions_spin)
        form.addRow("新方式: 1リクエストあたりのルール数 (バッチサイズ)", self.batch_size_spin)
        form.addRow("新方式: 出力上限 max_tokens", self.framework_tokens_spin)
        form.addRow("従来方式: 出力上限 max_tokens", self.conventional_tokens_spin)
        form.addRow("抽出: 出力上限 max_tokens", self.extraction_tokens_spin)
        form.addRow(self.reuse_check)
        return box

    # ------------------------------------------------------------- 一覧表示
    def _populate(self) -> None:
        extraction_model = self._settings.get("extraction_model", "")
        generation_models = set(self._settings.get("generation_models", []))

        self.extract_tree.blockSignals(True)
        self.generate_tree.blockSignals(True)
        self.extract_tree.clear()
        self.generate_tree.clear()

        selected_item = None
        for model in self.models:
            values = [model.get("name", ""), model.get("id", ""), _context_text(model)]

            item = QTreeWidgetItem(values)
            self.extract_tree.addTopLevelItem(item)
            if model.get("id") == extraction_model:
                selected_item = item

            check_item = QTreeWidgetItem(values)
            check_item.setFlags(check_item.flags() | Qt.ItemIsUserCheckable)
            check_item.setCheckState(
                0, Qt.Checked if model.get("id") in generation_models else Qt.Unchecked
            )
            self.generate_tree.addTopLevelItem(check_item)

        for tree in (self.extract_tree, self.generate_tree):
            tree.resizeColumnToContents(0)
            tree.resizeColumnToContents(1)

        self.extract_tree.blockSignals(False)
        self.generate_tree.blockSignals(False)

        if selected_item is not None:
            self.extract_tree.setCurrentItem(selected_item)
            self.extract_tree.scrollToItem(selected_item)

        if self.models:
            self.status_label.setText(f"{len(self.models)} モデルを表示中")
        else:
            self.status_label.setText(
                "モデル一覧が未取得です。「モデル一覧を更新」を押してください。"
            )

        self._apply_filter(self.search_edit.text())
        self._update_selection_label()

    def _apply_filter(self, text: str) -> None:
        keyword = text.strip().lower()
        for tree in (self.extract_tree, self.generate_tree):
            for i in range(tree.topLevelItemCount()):
                item = tree.topLevelItem(i)
                haystack = f"{item.text(0)} {item.text(1)}".lower()
                item.setHidden(bool(keyword) and keyword not in haystack)

    def _update_selection_label(self) -> None:
        extraction = self._current_extraction_model()
        generation = self._checked_generation_models()
        generation_text = ", ".join(generation) if generation else "(未選択)"
        self.selection_label.setText(
            f"抽出モデル: {extraction or '(未選択)'}\n"
            f"生成モデル: {len(generation)} 件  {generation_text}"
        )

        missing = model_service.find_missing(
            ([extraction] if extraction else []) + generation, self.models
        )
        if missing:
            self.warning_label.setText(
                "⚠ OpenRouter 上に見つからないモデル: " + ", ".join(missing)
            )
        else:
            self.warning_label.setText("")

    # ------------------------------------------------------------ 一覧の更新
    def _refresh_models(self) -> None:
        if self._thread is not None:
            return
        self.refresh_button.setEnabled(False)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(False)
        self.status_label.setText("モデル一覧を取得中...")

        self._thread = QThread(self)
        self._worker = ModelFetchWorker(self.client)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_models_fetched)
        self._worker.failed.connect(self._on_fetch_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

    def _on_models_fetched(self, models: list) -> None:
        # 現在の選択状態を保ったまま一覧を差し替える
        self._settings = self.selected_settings()
        self.models = models
        self._populate()
        self.status_label.setText(f"{len(models)} モデルを取得しました")

    def _on_fetch_failed(self, message: str) -> None:
        self.status_label.setText("モデル一覧の取得に失敗しました")
        QMessageBox.warning(self, "モデル一覧の取得に失敗", message)

    def _on_thread_finished(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        if self._thread is not None:
            self._thread.deleteLater()
            self._thread = None
        self.refresh_button.setEnabled(True)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(True)

    # ------------------------------------------------------------ 選択の取得
    def _current_extraction_model(self) -> str:
        item = self.extract_tree.currentItem()
        return item.text(1) if item is not None else ""

    def _checked_generation_models(self) -> list[str]:
        models = []
        for i in range(self.generate_tree.topLevelItemCount()):
            item = self.generate_tree.topLevelItem(i)
            if item.checkState(0) == Qt.Checked:
                models.append(item.text(1))
        return models

    def selected_settings(self) -> dict:
        return {
            "extraction_model": self._current_extraction_model(),
            "generation_models": self._checked_generation_models(),
            "framework_max_conditions": self.max_conditions_spin.value(),
            "framework_batch_size": self.batch_size_spin.value(),
            "framework_max_tokens": self.framework_tokens_spin.value(),
            "conventional_max_tokens": self.conventional_tokens_spin.value(),
            "extraction_max_tokens": self.extraction_tokens_spin.value(),
            "reuse_cached_results": self.reuse_check.isChecked(),
        }

    def accept(self) -> None:
        settings = self.selected_settings()
        if not settings["extraction_model"]:
            QMessageBox.warning(self, "モデル設定", "抽出モデルを1つ選択してください。")
            return
        if not settings["generation_models"]:
            QMessageBox.warning(self, "モデル設定", "生成モデルを1つ以上選択してください。")
            return
        super().accept()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt のシグネチャ)
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
        super().closeEvent(event)
