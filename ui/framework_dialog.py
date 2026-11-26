"""共通DT枠組みの確認・承認ダイアログ.

「① 枠組みの作成のみ」で保存した枠組み (frameworks/) を一覧し、
条件項目・動作項目・条件組合せを確認して承認する。承認済みの枠組みだけが
「② 承認済みの保存枠組みで生成」で生成モデルへ渡される。

抽出項目に誤りがある場合は、修正した内容で「新しい版」を作る (元の版は残る。
修正版には edited / parent_id が記録され、生成結果の results.json にも残る)。

「条件組合せの除外」タブで、論理的に成立しない組合せを除外パターン (Y / N / -) で一括除外できる
(ui/exclusion_editor.py)。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from services import dt_framework, framework_store
from services.framework_store import (
    STATUS_APPROVED,
    STATUS_DRAFT,
    STATUS_LABELS,
    STATUS_UNUSABLE,
    FrameworkRecord,
)
from ui.exclusion_editor import ExclusionEditor
from ui.result_view import ResultView
from utils import file_manager
from utils.config_manager import DEFAULT_SETTINGS

COLUMNS = ["仕様", "状態", "条件 / ルール", "抽出モデル", "作成日時", "備考"]
_STATUS_COLORS = {STATUS_APPROVED: "#1a7a1a", STATUS_DRAFT: "#9a6700", STATUS_UNUSABLE: "#b00020"}


class FrameworkDialog(QDialog):
    def __init__(self, settings: dict | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("共通DT枠組みの確認・承認")
        self.resize(1440, 900)
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.settings = dict(settings or {})
        self._records: list[FrameworkRecord] = []
        self._current_specs: dict[str, str] = {}  # 仕様名 -> 現在の要求仕様の SHA-256
        self._shown_id = ""  # 詳細に表示中の framework_id (未保存の除外編集の確認用)
        self._build_ui()
        self.reload()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        help_label = QLabel(
            "手順: 一括実行で「① 枠組みの作成のみ」を実行 → ここで条件項目・条件組合せを確認して「承認する」→ "
            "一括実行で「② 承認済みの保存枠組みで生成」。生成には各仕様の最新の承認済み枠組みが使われます。"
        )
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: #555;")

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("仕様名・テーマ・抽出モデルで絞り込み")
        self.filter_edit.textChanged.connect(self._apply_filter)
        self.status_filter = QComboBox()
        self.status_filter.addItem("すべての状態", "")
        for key, label in STATUS_LABELS.items():
            self.status_filter.addItem(label, key)
        self.status_filter.currentIndexChanged.connect(self._apply_filter)
        self.refresh_button = QPushButton("一覧を更新")
        self.refresh_button.clicked.connect(self.reload)
        self.open_folder_button = QPushButton("保存フォルダを開く")
        self.open_folder_button.clicked.connect(self._open_folder)
        self.hide_list_button = QPushButton("一覧を隠す")
        self.hide_list_button.setCheckable(True)
        self.hide_list_button.setToolTip("左の一覧を隠して、右側 (除外の編集など) を広く使います。")
        self.hide_list_button.toggled.connect(self._toggle_list)

        top = QHBoxLayout()
        top.addWidget(self.filter_edit, 1)
        top.addWidget(self.status_filter)
        top.addWidget(self.refresh_button)
        top.addWidget(self.open_folder_button)
        top.addWidget(self.hide_list_button)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setRootIsDecorated(False)
        self.tree.currentItemChanged.connect(self._on_selection_changed)

        # --- 右側: 詳細
        self.header_label = QLabel("左の一覧から枠組みを選択してください。")
        self.header_label.setWordWrap(True)
        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #b00020;")

        self.review_view = ResultView("", caption="生成モデルに渡す内容 (条件項目・動作項目・条件組合せ)")
        self.items_edit = QPlainTextEdit()
        self.items_edit.setPlaceholderText("抽出項目 (JSON)")
        self.derive_button = QPushButton("修正内容で新しい版を作成")
        self.derive_button.setToolTip(
            "上の JSON を修正して押すと、修正後の抽出項目から枠組みを作り直し、新しい版 (下書き) として保存します。\n"
            "元の版は変更されません。修正版であることは生成結果にも記録されます。"
        )
        self.derive_button.clicked.connect(self._on_derive)
        items_tab = QWidget()
        items_layout = QVBoxLayout(items_tab)
        items_layout.addWidget(QLabel("抽出項目 (conditions / actions)。修正する場合はここを編集してください。"))
        items_layout.addWidget(self.items_edit, 1)
        items_layout.addWidget(self.derive_button, 0, Qt.AlignRight)
        self.raw_view = ResultView("", caption="抽出モデルの出力 (そのまま)")
        self.exclusion_editor = ExclusionEditor()
        self.exclusion_editor.saved.connect(self._on_exclusions_saved)

        self.detail_tabs = QTabWidget()
        self.detail_tabs.addTab(self.review_view, "確認")
        self.detail_tabs.addTab(self.exclusion_editor, "条件組合せの除外")
        self.detail_tabs.addTab(items_tab, "抽出項目 (修正)")
        self.detail_tabs.addTab(self.raw_view, "抽出モデルの生出力")

        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("メモ (確認時の気づき・修正理由など)")
        self.note_button = QPushButton("メモを保存")
        self.note_button.clicked.connect(self._on_save_note)
        note_row = QHBoxLayout()
        note_row.addWidget(QLabel("メモ:"))
        note_row.addWidget(self.note_edit, 1)
        note_row.addWidget(self.note_button)

        self.approve_button = QPushButton("承認する")
        self.approve_button.clicked.connect(lambda: self._set_status(STATUS_APPROVED))
        self.unapprove_button = QPushButton("承認を取り消す")
        self.unapprove_button.clicked.connect(lambda: self._set_status(STATUS_DRAFT))
        self.delete_button = QPushButton("削除...")
        self.delete_button.clicked.connect(self._on_delete)
        action_row = QHBoxLayout()
        action_row.addWidget(self.approve_button)
        action_row.addWidget(self.unapprove_button)
        action_row.addStretch(1)
        action_row.addWidget(self.delete_button)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(self.header_label)
        right_layout.addWidget(self.warning_label)
        right_layout.addWidget(self.detail_tabs, 1)
        right_layout.addLayout(note_row)
        right_layout.addLayout(action_row)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.tree)
        splitter.addWidget(right)
        splitter.setSizes([420, 1020])
        self.main_splitter = splitter

        self.close_button = QPushButton("閉じる")
        self.close_button.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(help_label)
        layout.addLayout(top)
        layout.addWidget(splitter, 1)
        layout.addLayout(bottom)
        self._set_detail_enabled(False)

    def _toggle_list(self, hidden: bool) -> None:
        self.tree.setVisible(not hidden)
        self.hide_list_button.setText("一覧を表示" if hidden else "一覧を隠す")

    # ------------------------------------------------------------- 一覧
    def _load_current_specs(self) -> None:
        """data/ の現在の仕様の本文ハッシュ (枠組み作成後に仕様が変わっていないかの確認用)."""
        self._current_specs = {}
        try:
            sources, _notes = file_manager.list_spec_sources()
        except Exception:  # noqa: BLE001 (確認用の補助情報なので失敗しても続ける)
            return
        for source in sources:
            try:
                self._current_specs[source.name] = framework_store.requirements_hash(source.read_requirements())
            except OSError:
                continue

    def reload(self) -> None:
        selected = self._selected_record()
        selected_id = selected.framework_id if selected else ""
        self._records = framework_store.list_records()
        self._load_current_specs()

        approved_count: dict[tuple[str, str], int] = {}
        for r in self._records:
            if r.status == STATUS_APPROVED:
                key = (r.source_name, r.requirements_sha256)
                approved_count[key] = approved_count.get(key, 0) + 1

        self.tree.blockSignals(True)  # 作り直しの途中で選択変更の処理を走らせない
        self.tree.clear()
        restore = None
        # 仕様の番号順 (同じ仕様の中は新しい版が上。list_records() の新しい順を保つ)
        for record in sorted(self._records, key=framework_store.spec_sort_key):
            notes = []
            if record.edited:
                notes.append("修正版")
            current = self._current_specs.get(record.source_name)
            if current and current != record.requirements_sha256:
                notes.append("⚠ 現在の仕様と本文が異なる")
            if record.status == STATUS_APPROVED and approved_count.get((record.source_name, record.requirements_sha256), 0) > 1:
                notes.append("承認済みが複数 (最新を使用)")
            if record.note:
                notes.append(f"メモ: {record.note}")
            if record.usable:
                size = f"{record.condition_count} / {record.rule_count}"
                if record.excluded_count:
                    size += f" (全{record.total_combinations}・除外{record.excluded_count})"
            else:
                size = "-"
            item = QTreeWidgetItem([
                record.display_name(),
                STATUS_LABELS.get(record.status, record.status),
                size,
                record.extraction.get("model", ""),
                record.created_at.replace("T", " "),
                " / ".join(notes),
            ])
            item.setData(0, Qt.UserRole, record.framework_id)
            item.setToolTip(0, f"{record.source_label}\n{record.path}")
            item.setForeground(1, QBrush(QColor(_STATUS_COLORS.get(record.status, "#000"))))
            self.tree.addTopLevelItem(item)
            if record.framework_id == selected_id:
                restore = item
        self.tree.blockSignals(False)
        for col in range(len(COLUMNS) - 1):
            self.tree.resizeColumnToContents(col)
        self._apply_filter()
        if restore is not None:
            self.tree.blockSignals(True)
            self.tree.setCurrentItem(restore)
            self.tree.blockSignals(False)
        self._on_selection_changed()
        if not self._records:
            self.header_label.setText(
                "保存済みの枠組みがありません。一括実行で生成方式「新方式」・"
                "実行モード「① 枠組みの作成のみ」を実行してください。"
            )

    def _apply_filter(self) -> None:
        keyword = self.filter_edit.text().strip().lower()
        status = self.status_filter.currentData()
        by_id = {r.framework_id: r for r in self._records}
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            record = by_id.get(item.data(0, Qt.UserRole))
            text = " ".join(item.text(c) for c in range(len(COLUMNS))).lower()
            if record is not None:
                text += " " + record.source_label.lower()
            hidden = (bool(keyword) and keyword not in text) or (bool(status) and record is not None and record.status != status)
            item.setHidden(hidden)

    def _selected_record(self) -> FrameworkRecord | None:
        item = self.tree.currentItem() if hasattr(self, "tree") else None
        if item is None:
            return None
        framework_id = item.data(0, Qt.UserRole)
        return next((r for r in self._records if r.framework_id == framework_id), None)

    # ------------------------------------------------------------- 詳細
    def _set_detail_enabled(self, enabled: bool) -> None:
        for widget in (self.note_edit, self.note_button, self.approve_button, self.unapprove_button,
                       self.delete_button, self.derive_button, self.items_edit):
            widget.setEnabled(enabled)

    def _on_selection_changed(self, current=None, previous=None) -> None:
        record = self._selected_record()
        if (
            self.exclusion_editor.has_unsaved_changes()
            and previous is not None
            and (record is None or record.framework_id != self._shown_id)
        ):
            answer = QMessageBox.question(
                self, "未保存の変更", "除外パターンの編集が保存されていません。破棄して別の枠組みを表示しますか?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                self.tree.blockSignals(True)
                self.tree.setCurrentItem(previous)
                self.tree.blockSignals(False)
                return
        if record is None:
            self._set_detail_enabled(False)
            self.exclusion_editor.set_record(None, self._records)
            self._shown_id = ""
            return
        keep_edits = self.exclusion_editor.has_unsaved_changes() and record.framework_id == self._shown_id
        self._shown_id = record.framework_id
        if not keep_edits:  # 同じ枠組みの再表示 (メモ保存など) では編集中の除外を残す
            max_conditions = int(self.settings.get("framework_max_conditions", DEFAULT_SETTINGS["framework_max_conditions"]))
            self.exclusion_editor.set_record(record, self._records, max_conditions)
        self._set_detail_enabled(True)
        self.header_label.setText(
            f"<b>{record.display_name()}</b>　{STATUS_LABELS.get(record.status, record.status)}"
            f"{'（' + record.approved_at.replace('T', ' ') + ' 承認）' if record.approved_at else ''}<br>"
            f"{record.source_label}<br>framework_id: {record.framework_id}"
            + (f"（修正版。元: {record.parent_id}）" if record.edited else "")
        )
        warnings = []
        if record.framework_error:
            warnings.append(record.framework_error)
        warnings.extend(record.exclusion_warnings)
        current = self._current_specs.get(record.source_name)
        if current and current != record.requirements_sha256:
            warnings.append(
                "この枠組みを作成した後に、data/ の仕様の本文が変更されています。"
                "この枠組みは現在の仕様の生成には使われません (作り直してください)。"
            )
        self.warning_label.setText("\n".join(warnings))
        self.review_view.set_text(framework_store.review_markdown(record))
        self.items_edit.setPlainText(self._pretty_items(record.decision_items))
        self.raw_view.set_text(f"```\n{record.extraction.get('content', '')}\n```\n")
        self.note_edit.setText(record.note)
        self.approve_button.setEnabled(record.usable and record.status != STATUS_APPROVED)
        self.unapprove_button.setEnabled(record.status == STATUS_APPROVED)

    @staticmethod
    def _pretty_items(text: str) -> str:
        try:
            return json.dumps(dt_framework.load_json(text), ensure_ascii=False, indent=2)
        except ValueError:
            return text

    # ------------------------------------------------------------- 操作
    def _set_status(self, status: str) -> None:
        record = self._selected_record()
        if record is None:
            return
        if status == STATUS_APPROVED and self.exclusion_editor.has_unsaved_changes():
            QMessageBox.warning(
                self, "未保存の除外", "除外パターンの編集が保存されていません。「除外を保存」してから承認してください。"
            )
            return
        try:
            framework_store.set_status(record, status)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "変更できません", str(exc))
            return
        self.reload()

    def _on_exclusions_saved(self, framework_id: str) -> None:
        self._shown_id = ""
        self.reload()
        self._select(framework_id)
        self.detail_tabs.setCurrentWidget(self.exclusion_editor)

    def _select(self, framework_id: str) -> None:
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.data(0, Qt.UserRole) == framework_id:
                self.tree.setCurrentItem(item)
                return

    def _on_save_note(self) -> None:
        record = self._selected_record()
        if record is None:
            return
        try:
            framework_store.set_note(record, self.note_edit.text().strip())
        except OSError as exc:
            QMessageBox.warning(self, "保存できません", str(exc))
            return
        self.reload()

    def _on_derive(self) -> None:
        record = self._selected_record()
        if record is None:
            return
        text = self.items_edit.toPlainText()
        try:
            dt_framework.parse_decision_items(text)
        except dt_framework.FrameworkError as exc:
            QMessageBox.warning(self, "抽出項目が不正です", str(exc))
            return
        if self._pretty_items(text) == self._pretty_items(record.decision_items):
            QMessageBox.information(self, "新しい版", "抽出項目が変更されていません。")
            return
        max_conditions = int(self.settings.get("framework_max_conditions", DEFAULT_SETTINGS["framework_max_conditions"]))
        new_record = framework_store.derive_edited(record, text, max_conditions)
        try:
            framework_store.save(new_record)
        except OSError as exc:
            QMessageBox.warning(self, "保存できません", str(exc))
            return
        if new_record.framework_error:
            QMessageBox.warning(self, "枠組みを作れません", new_record.framework_error)
        if new_record.exclusion_warnings:
            QMessageBox.information(self, "除外パターン", "\n".join(new_record.exclusion_warnings))
        self.reload()
        self._select(new_record.framework_id)

    def _on_delete(self) -> None:
        record = self._selected_record()
        if record is None:
            return
        answer = QMessageBox.question(
            self, "削除の確認",
            f"この枠組みを削除しますか?\n{record.display_name()} ({record.framework_id})\n\n"
            "この枠組みを使った生成結果 (outputs/) は削除されません。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        framework_store.delete(record)
        self.reload()

    def _open_folder(self) -> None:
        record = self._selected_record()
        target = os.path.dirname(record.path) if record and record.path else str(framework_store.frameworks_dir())
        os.makedirs(target, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(target)  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", target])
        except OSError as exc:
            QMessageBox.warning(self, "フォルダを開けません", str(exc))
