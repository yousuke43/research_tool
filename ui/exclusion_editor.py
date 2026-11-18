"""共通DTの条件組合せを一括除外する編集画面 (「共通DT枠組み...」ダイアログのタブ).

- 各条件に Y / N / - (どちらでもよい) を指定した除外パターンを複数登録・編集・削除する
- 除外前後の組合せ数と、どの組合せが除外されるかをプレビューする
- 除外パターンと理由を枠組みに保存する (他の枠組みに保存したパターンも読み込める)

プレビューは QTableView + モデルで描画するので、組合せが数千通りあっても重くならない。
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from services import dt_framework, framework_store
from services.dt_framework import PATTERN_ANY, ExclusionPattern, FrameworkRule
from services.framework_store import STATUS_APPROVED, FrameworkRecord

PATTERN_CHOICES = (PATTERN_ANY, "Y", "N")
_EXCLUDED_BG = QColor("#cfd6e3")
_INVALID_BG = QColor("#ffd3d3")

CAUTION = (
    "論理的に同時に成立しない組合せ (条件間の包含・排他。例:「5回以上」が Y なのに「3回以上」が N) だけを除外してください。"
    "割引の併用可否など、解釈によって結果が変わりうる組合せ (研究対象の解釈差異) は除外しないでください。"
    "除外した組合せは LLM に渡さず、結果では「除外」として「適用不可」と区別して記録します。"
)

FILTER_ALL, FILTER_EXCLUDED, FILTER_KEPT = "all", "excluded", "kept"


class _PreviewModel(QAbstractTableModel):
    """全組合せのプレビュー (1行 = 1組合せ)."""

    def __init__(self) -> None:
        super().__init__()
        self._conditions: list[str] = []
        self._rows: list[tuple[FrameworkRule, tuple[int, ...]]] = []

    def set_rows(self, conditions: list[str], rows: list[tuple[FrameworkRule, tuple[int, ...]]]) -> None:
        self.beginResetModel()
        self._conditions = conditions
        self._rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._conditions) + 2

    def rule_at(self, row: int) -> FrameworkRule | None:
        return self._rows[row][0] if 0 <= row < len(self._rows) else None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        rule, hits = self._rows[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            if col == 0:
                return rule.rule_id
            if col <= len(self._conditions):
                return rule.values[col - 1]
            return "除外 (パターン " + ", ".join(f"#{i + 1}" for i in hits) + ")" if hits else "残す (LLMに渡す)"
        if role == Qt.BackgroundRole and hits:
            return QBrush(_EXCLUDED_BG)
        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignCenter)
        if role == Qt.ToolTipRole and 1 <= col <= len(self._conditions):
            return self._conditions[col - 1]
        return None

    def headerData(self, section: int, orientation, role: int = Qt.DisplayRole):  # noqa: N802
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            if section == 0:
                return "ルール"
            if section <= len(self._conditions):
                return f"C{section}"
            return "状態"
        if role == Qt.ToolTipRole and 1 <= section <= len(self._conditions):
            return self._conditions[section - 1]
        return None


class ExclusionEditor(QWidget):
    saved = Signal(str)  # 保存したレコードの framework_id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._record: FrameworkRecord | None = None
        self._all_records: list[FrameworkRecord] = []
        self._conditions: list[str] = []
        self._all_rules: list[FrameworkRule] = []
        self._max_conditions = 6
        self._dirty = False
        self._build_ui()
        self.setEnabled(False)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        caution = QLabel(CAUTION)
        caution.setWordWrap(True)
        caution.setStyleSheet("color: #8a4b00;")
        self.legend_label = QLabel("")
        self.legend_label.setWordWrap(True)
        self.legend_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        # --- 除外パターン
        self.pattern_table = QTableWidget()
        self.pattern_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.pattern_table.itemChanged.connect(self._on_reason_changed)
        self.add_button = QPushButton("パターンを追加")
        self.add_button.clicked.connect(lambda: self._add_pattern(None, ""))
        self.delete_button = QPushButton("選択したパターンを削除")
        self.delete_button.clicked.connect(self._delete_selected)
        self.quick_edit = QLineEdit()
        self.quick_edit.setPlaceholderText("例: N Y -")
        self.quick_edit.setMaximumWidth(140)
        self.quick_reason = QLineEdit()
        self.quick_reason.setPlaceholderText("理由 (例: 5回以上なら必ず3回以上)")
        self.quick_button = QPushButton("文字で追加")
        self.quick_button.clicked.connect(self._add_from_text)
        self.from_rule_button = QPushButton("プレビューで選択した組合せから作成")
        self.from_rule_button.setToolTip("プレビューで選択した組合せの Y/N をそのままパターンにします (あとで一部を - に変更できます)。")
        self.from_rule_button.clicked.connect(self._add_from_selected_rule)

        buttons = QHBoxLayout()
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.delete_button)
        buttons.addWidget(self.from_rule_button)
        buttons.addStretch(1)
        quick = QHBoxLayout()
        quick.addWidget(QLabel("パターン:"))
        quick.addWidget(self.quick_edit)
        quick.addWidget(self.quick_reason, 1)
        quick.addWidget(self.quick_button)

        self.reuse_combo = QComboBox()
        self.reuse_combo.setMinimumWidth(320)
        self.reuse_button = QPushButton("このパターンを読み込む")
        self.reuse_button.clicked.connect(self._import_from_other)
        reuse = QHBoxLayout()
        reuse.addWidget(QLabel("他の枠組みの除外を再利用:"))
        reuse.addWidget(self.reuse_combo, 1)
        reuse.addWidget(self.reuse_button)

        pattern_box = QWidget()
        pattern_layout = QVBoxLayout(pattern_box)
        pattern_layout.setContentsMargins(0, 0, 0, 0)
        pattern_layout.addWidget(QLabel("除外パターン (いずれかに一致した組合せを除外。- はどちらでもよい)"))
        pattern_layout.addWidget(self.pattern_table, 1)
        pattern_layout.addLayout(buttons)
        pattern_layout.addLayout(quick)
        pattern_layout.addLayout(reuse)

        # --- プレビュー
        self.count_label = QLabel("")
        self.filter_combo = QComboBox()
        self.filter_combo.addItem("すべての組合せ", FILTER_ALL)
        self.filter_combo.addItem("除外対象のみ", FILTER_EXCLUDED)
        self.filter_combo.addItem("残る組合せのみ", FILTER_KEPT)
        self.filter_combo.currentIndexChanged.connect(self._refresh_preview)
        self.preview_model = _PreviewModel()
        self.preview_view = QTableView()
        self.preview_view.setModel(self.preview_model)
        self.preview_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.preview_view.verticalHeader().setVisible(False)
        self.preview_view.verticalHeader().setDefaultSectionSize(20)
        # 全行を走査する ResizeToContents は数千行で重いので、列幅は固定にする
        self.preview_view.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.preview_view.horizontalHeader().setStretchLastSection(True)
        preview_top = QHBoxLayout()
        preview_top.addWidget(self.count_label, 1)
        preview_top.addWidget(self.filter_combo)
        preview_box = QWidget()
        preview_layout = QVBoxLayout(preview_box)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.addLayout(preview_top)
        preview_layout.addWidget(self.preview_view, 1)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(pattern_box)
        splitter.addWidget(preview_box)
        splitter.setSizes([260, 320])

        self.save_button = QPushButton("除外を保存")
        self.save_button.clicked.connect(self.save)
        self.revert_button = QPushButton("編集を取り消す")
        self.revert_button.clicked.connect(lambda: self.set_record(self._record, self._all_records, self._max_conditions))
        self.save_note = QLabel("")
        self.save_note.setStyleSheet("color: #555;")
        save_row = QHBoxLayout()
        save_row.addWidget(self.save_note, 1)
        save_row.addWidget(self.revert_button)
        save_row.addWidget(self.save_button)

        layout = QVBoxLayout(self)
        layout.addWidget(caution)
        layout.addWidget(self.legend_label)
        layout.addWidget(splitter, 1)
        layout.addLayout(save_row)

    # ------------------------------------------------------------- 読み込み
    def set_record(self, record: FrameworkRecord | None, all_records: list[FrameworkRecord], max_conditions: int = 6) -> None:
        self._record = record
        self._all_records = all_records
        self._max_conditions = max_conditions
        self.pattern_table.blockSignals(True)
        self.pattern_table.clear()
        self.pattern_table.setRowCount(0)
        self.pattern_table.blockSignals(False)
        self._conditions, self._all_rules = [], []
        if record is None:
            self.setEnabled(False)
            self.preview_model.set_rows([], [])
            return
        try:
            self._conditions = record.decision_items_obj().conditions
            self._all_rules = dt_framework.all_rules(len(self._conditions))
            patterns = record.exclusion_patterns()
        except dt_framework.FrameworkError as exc:
            self.setEnabled(False)
            self.legend_label.setText(f"この枠組みでは除外を編集できません: {exc}")
            self.preview_model.set_rows([], [])
            return
        self.setEnabled(True)
        self.legend_label.setText("　".join(f"<b>C{i}</b>: {c}" for i, c in enumerate(self._conditions, start=1)))
        headers = ["#"] + [f"C{i}" for i in range(1, len(self._conditions) + 1)] + ["理由", "一致数"]
        self.pattern_table.setColumnCount(len(headers))
        self.pattern_table.setHorizontalHeaderLabels(headers)
        for i, text in enumerate(self._conditions, start=1):
            self.pattern_table.horizontalHeaderItem(i).setToolTip(text)
        self.pattern_table.horizontalHeader().setSectionResizeMode(len(headers) - 2, QHeaderView.Stretch)
        for p in patterns:
            self._add_pattern(p.values, p.reason, mark_dirty=False)
        self._fill_reuse_combo()
        self._dirty = False
        self.save_note.setText(
            "承認済みの枠組みです。保存すると新しい版 (下書き) を作ります (元の版は変わりません)。"
            if record.status == STATUS_APPROVED else "下書きの枠組みです。保存するとこの枠組みを更新します。"
        )
        self._refresh_preview()

    def _fill_reuse_combo(self) -> None:
        self.reuse_combo.clear()
        others = [r for r in self._all_records if self._record is None or r.framework_id != self._record.framework_id]
        for record, count in framework_store.reusable_exclusion_sources(others, self._conditions):
            self.reuse_combo.addItem(
                f"{record.display_name()} ({record.framework_id}) — {count} パターン", record.framework_id
            )
        self.reuse_combo.setEnabled(self.reuse_combo.count() > 0)
        self.reuse_button.setEnabled(self.reuse_combo.count() > 0)
        if not self.reuse_combo.count():
            self.reuse_combo.addItem("(条件が対応する除外パターンを持つ枠組みはありません)")

    # ------------------------------------------------------------- パターン編集
    def _add_pattern(self, values: tuple[str, ...] | None, reason: str, mark_dirty: bool = True) -> None:
        n = len(self._conditions)
        values = values or tuple(PATTERN_ANY for _ in range(n))
        row = self.pattern_table.rowCount()
        self.pattern_table.blockSignals(True)
        self.pattern_table.insertRow(row)
        number = QTableWidgetItem(str(row + 1))
        number.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        self.pattern_table.setItem(row, 0, number)
        for ci in range(n):
            combo = QComboBox()
            combo.addItems(PATTERN_CHOICES)
            combo.setCurrentText(values[ci])
            combo.currentIndexChanged.connect(self._on_pattern_changed)
            self.pattern_table.setCellWidget(row, 1 + ci, combo)
        self.pattern_table.setItem(row, n + 1, QTableWidgetItem(reason))
        count = QTableWidgetItem("")
        count.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        self.pattern_table.setItem(row, n + 2, count)
        self.pattern_table.blockSignals(False)
        self.pattern_table.resizeColumnsToContents()
        if mark_dirty:
            self._dirty = True
            self._refresh_preview()

    def _delete_selected(self) -> None:
        rows = sorted({i.row() for i in self.pattern_table.selectedIndexes()}, reverse=True)
        if not rows:
            QMessageBox.information(self, "削除", "削除するパターンの行を選択してください。")
            return
        for row in rows:
            self.pattern_table.removeRow(row)
        for row in range(self.pattern_table.rowCount()):
            self.pattern_table.item(row, 0).setText(str(row + 1))
        self._dirty = True
        self._refresh_preview()

    def _add_from_text(self) -> None:
        try:
            values = dt_framework.parse_pattern(self.quick_edit.text(), len(self._conditions))
        except dt_framework.FrameworkError as exc:
            QMessageBox.warning(self, "パターンが不正です", str(exc))
            return
        self._add_pattern(values, self.quick_reason.text().strip())
        self.quick_edit.clear()
        self.quick_reason.clear()

    def _add_from_selected_rule(self) -> None:
        rows = sorted({i.row() for i in self.preview_view.selectionModel().selectedRows()})
        if not rows:
            QMessageBox.information(self, "パターンの作成", "プレビューで組合せ (行) を選択してください。")
            return
        for row in rows:
            rule = self.preview_model.rule_at(row)
            if rule is not None:
                self._add_pattern(rule.values, "")

    def _import_from_other(self) -> None:
        framework_id = self.reuse_combo.currentData()
        source = next((r for r in self._all_records if r.framework_id == framework_id), None)
        if source is None:
            return
        mapped, dropped = dt_framework.remap_exclusions(source.exclusions, self._conditions)
        existing = {p.values for p in self._current_patterns()[0]}
        added = 0
        for p in mapped:
            if p.values not in existing:
                self._add_pattern(p.values, p.reason)
                added += 1
        message = f"{added} パターンを読み込みました (重複 {len(mapped) - added} 件は省略)。"
        if dropped:
            message += f"\n条件が対応しないため {len(dropped)} パターンは読み込んでいません。"
        QMessageBox.information(self, "除外パターンの再利用", message)

    def _on_pattern_changed(self) -> None:
        self._dirty = True
        self._refresh_preview()

    def _on_reason_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == len(self._conditions) + 1:
            self._dirty = True

    def _current_patterns(self) -> tuple[list[ExclusionPattern], list[int]]:
        """(有効なパターン, 無効な行番号). すべて - の行は無効 (全組合せを除外してしまうため)."""
        n = len(self._conditions)
        patterns, invalid = [], []
        for row in range(self.pattern_table.rowCount()):
            values = tuple(self.pattern_table.cellWidget(row, 1 + ci).currentText() for ci in range(n))
            reason_item = self.pattern_table.item(row, n + 1)
            reason = reason_item.text().strip() if reason_item else ""
            if all(v == PATTERN_ANY for v in values):
                invalid.append(row)
                continue
            patterns.append(ExclusionPattern(values, reason))
        return patterns, invalid

    # ------------------------------------------------------------- プレビュー
    def _refresh_preview(self) -> None:
        if not self._conditions:
            return
        n = len(self._conditions)
        patterns, invalid = self._current_patterns()
        # パターンの行番号 -> 有効パターンの番号 (無効な行を飛ばす)
        valid_rows = [r for r in range(self.pattern_table.rowCount()) if r not in invalid]
        hits_by_rule = []
        for rule in self._all_rules:
            hits = tuple(valid_rows[i] for i, p in enumerate(patterns) if p.matches(rule.values))
            hits_by_rule.append((rule, hits))
        counts = dt_framework.pattern_match_counts(self._all_rules, patterns)

        self.pattern_table.blockSignals(True)
        for row in range(self.pattern_table.rowCount()):
            item = self.pattern_table.item(row, n + 2)
            if row in invalid:
                item.setText("無効 (すべて -)")
                item.setBackground(QBrush(_INVALID_BG))
            else:
                item.setText(str(counts[valid_rows.index(row)]))
                item.setBackground(QBrush())
        self.pattern_table.blockSignals(False)

        total = len(self._all_rules)
        excluded = sum(1 for _, hits in hits_by_rule if hits)
        kept = total - excluded
        limit = 2 ** self._max_conditions
        text = f"全 {total} 通り → 除外 {excluded} 通り → <b>残り {kept} ルール</b> (LLMに渡す上限 {limit})"
        color = "#1a7a1a"
        if kept == 0:
            text += "　⚠ 全組合せが除外されています"
            color = "#b00020"
        elif kept > limit:
            text += "　⚠ 上限を超えています"
            color = "#b00020"
        if invalid:
            text += f"　⚠ 無効なパターン {len(invalid)} 件 (保存時に除きます)"
        self.count_label.setText(text)
        self.count_label.setStyleSheet(f"color: {color};")

        mode = self.filter_combo.currentData()
        if mode == FILTER_EXCLUDED:
            hits_by_rule = [x for x in hits_by_rule if x[1]]
        elif mode == FILTER_KEPT:
            hits_by_rule = [x for x in hits_by_rule if not x[1]]
        self.preview_model.set_rows(self._conditions, hits_by_rule)
        header = self.preview_view.horizontalHeader()
        header.resizeSection(0, 64)
        for col in range(1, len(self._conditions) + 1):
            header.resizeSection(col, 44)

    # ------------------------------------------------------------------ 保存
    def has_unsaved_changes(self) -> bool:
        return self._dirty

    def save(self) -> FrameworkRecord | None:
        if self._record is None:
            return None
        patterns, invalid = self._current_patterns()
        missing = [i + 1 for i, p in enumerate(patterns) if not p.reason]
        if missing:
            QMessageBox.warning(
                self, "理由が未入力です",
                "除外の理由を入力してください (研究の記録として保存します)。\n"
                f"理由が空のパターン: {', '.join('#' + str(m) for m in missing)}",
            )
            return None
        if len({p.values for p in patterns}) != len(patterns):
            QMessageBox.warning(self, "重複", "同じ除外パターンが複数あります。重複を削除してください。")
            return None
        kept, _ = dt_framework.apply_exclusions(self._all_rules, patterns)
        if not kept:
            QMessageBox.warning(self, "保存できません", "全組合せが除外されています。")
            return None
        if self._record.status == STATUS_APPROVED:
            answer = QMessageBox.question(
                self, "新しい版の作成",
                "この枠組みは承認済みです。除外を変更した新しい版 (下書き) を作ります。\n"
                "生成に使うには、新しい版を確認して承認してください。よろしいですか?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            )
            if answer != QMessageBox.Yes:
                return None
        try:
            saved = framework_store.update_exclusions(self._record, patterns, self._max_conditions)
        except OSError as exc:
            QMessageBox.warning(self, "保存できません", str(exc))
            return None
        self._dirty = False
        if saved.framework_error:
            QMessageBox.warning(self, "枠組みを作れません", saved.framework_error)
        self.saved.emit(saved.framework_id)
        return saved
