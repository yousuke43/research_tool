"""共通DTの条件組合せを一括除外する編集画面 (「共通DT枠組み...」ダイアログのタブ).

左: パターンの入力フォーム (条件文を見ながら Y / N / - をボタンで選び、理由を書いて「追加」) と登録済みの一覧
右: 全組合せのプレビュー (除外される組合せ・フォームのパターンに一致する組合せを色で表示)

- 理由はフォームで入力してから追加するので、表のセル編集や日本語入力の未確定で理由が消えることはない
- 一覧の行を選ぶとフォームに読み込まれ、直して「選択中のパターンを更新」できる
- プレビューは QTableView + モデルで描画するので、組合せが数千通りあっても重くならない
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
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

PATTERN_CHOICES = ("Y", "N", PATTERN_ANY)
_EXCLUDED_BG = QColor("#cfd6e3")
_FORM_MATCH_BG = QColor("#fff1b8")
_BOTH_BG = QColor("#e3d3a0")
_MISSING_BG = QColor("#ffd3d3")
_ERROR_STYLE = "color: #b00020;"
_OK_STYLE = "color: #1a7a1a;"

CAUTION = (
    "除外するのは、論理的に同時に成立しない組合せ (例:「5回以上」が Y なのに「3回以上」が N) だけにしてください。"
    "割引の併用可否など、解釈で結果が変わりうる組合せは研究対象なので除外しないでください。"
    "除外した組合せは LLM に渡さず、結果では「除外」として「適用不可」と区別して記録します。"
)

FILTER_ALL, FILTER_EXCLUDED, FILTER_KEPT = "all", "excluded", "kept"


def _commit_input_method() -> None:
    """日本語入力の未確定文字を確定させる (未確定のままだと text() に入らない)."""
    method = QGuiApplication.inputMethod()
    if method is not None:
        method.commit()


class _PreviewModel(QAbstractTableModel):
    """全組合せのプレビュー (1行 = 1組合せ)."""

    def __init__(self) -> None:
        super().__init__()
        self._conditions: list[str] = []
        self._rows: list[tuple[FrameworkRule, tuple[int, ...]]] = []
        self._form: ExclusionPattern | None = None

    def set_rows(self, conditions: list[str], rows: list[tuple[FrameworkRule, tuple[int, ...]]]) -> None:
        self.beginResetModel()
        self._conditions = conditions
        self._rows = rows
        self.endResetModel()

    def set_form_pattern(self, pattern: ExclusionPattern | None) -> None:
        self._form = pattern
        if self._rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._rows) - 1, self.columnCount() - 1))

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
        if role == Qt.BackgroundRole:
            in_form = self._form is not None and self._form.matches(rule.values)
            if hits and in_form:
                return QBrush(_BOTH_BG)
            if in_form:
                return QBrush(_FORM_MATCH_BG)
            if hits:
                return QBrush(_EXCLUDED_BG)
            return None
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
        self._patterns: list[ExclusionPattern] = []
        self._max_conditions = 6
        self._dirty = False
        self._form_touched = False  # フォームに入力したがまだ追加・更新していない
        self._editing_row: int | None = None  # 一覧から読み込んで編集中の行
        self._value_groups: list[QButtonGroup] = []
        self._syncing = False
        font = self.font()
        font.setPointSizeF(font.pointSizeF() + 1)
        self.setFont(font)
        self._build_ui()
        self.setEnabled(False)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        caution = QLabel(CAUTION)
        caution.setWordWrap(True)
        caution.setStyleSheet("color: #8a4b00;")

        # --- ① フォーム
        self.conditions_box = QWidget()
        self.conditions_grid = QGridLayout(self.conditions_box)
        self.conditions_grid.setContentsMargins(4, 4, 4, 4)
        self.conditions_grid.setVerticalSpacing(6)
        self.conditions_grid.setColumnStretch(1, 1)
        conditions_scroll = QScrollArea()
        conditions_scroll.setWidgetResizable(True)
        conditions_scroll.setFrameShape(QFrame.NoFrame)
        conditions_scroll.setWidget(self.conditions_box)
        conditions_scroll.setMinimumHeight(120)

        self.pattern_text = QLineEdit()
        self.pattern_text.setPlaceholderText("例: N Y -")
        self.pattern_text.setToolTip("Y / N / - を条件の順に並べて入力すると、上のボタンに反映されます。")
        self.pattern_text.textEdited.connect(self._on_pattern_text_edited)
        self.form_match_label = QLabel("")
        self.form_match_label.setWordWrap(True)
        text_row = QHBoxLayout()
        text_row.addWidget(QLabel("文字で入力:"))
        text_row.addWidget(self.pattern_text)
        text_row.addWidget(self.form_match_label, 1)

        self.reason_edit = QLineEdit()
        self.reason_edit.setPlaceholderText("例: 5回以上なら必ず3回以上 (研究の記録として保存します)")
        self.reason_edit.setMinimumHeight(30)
        self.reason_edit.textEdited.connect(self._on_form_touched)
        self.reason_edit.returnPressed.connect(self._on_enter_in_reason)
        reason_row = QHBoxLayout()
        reason_label = QLabel("理由 (必須):")
        reason_label.setStyleSheet("font-weight: bold;")
        reason_row.addWidget(reason_label)
        reason_row.addWidget(self.reason_edit, 1)

        self.add_button = QPushButton("＋ 追加")
        self.add_button.setMinimumHeight(32)
        self.add_button.setDefault(False)
        self.add_button.setStyleSheet("font-weight: bold;")
        self.add_button.clicked.connect(self.add_from_form)
        self.update_button = QPushButton("選択中のパターンを更新")
        self.update_button.setMinimumHeight(32)
        self.update_button.clicked.connect(self.update_from_form)
        self.clear_button = QPushButton("フォームをクリア")
        self.clear_button.setMinimumHeight(32)
        self.clear_button.clicked.connect(self._clear_form)
        form_buttons = QHBoxLayout()
        form_buttons.addWidget(self.add_button)
        form_buttons.addWidget(self.update_button)
        form_buttons.addStretch(1)
        form_buttons.addWidget(self.clear_button)
        self.form_message = QLabel("")
        self.form_message.setWordWrap(True)

        form_box = QGroupBox("① 除外する組合せのパターンを作る (各条件の Y / N / -  を選ぶ。- はどちらでもよい)")
        form_layout = QVBoxLayout(form_box)
        form_layout.addWidget(conditions_scroll, 1)
        form_layout.addLayout(text_row)
        form_layout.addLayout(reason_row)
        form_layout.addLayout(form_buttons)
        form_layout.addWidget(self.form_message)

        # --- ② 登録済み
        self.pattern_table = QTableWidget(0, 4)
        self.pattern_table.setHorizontalHeaderLabels(["#", "パターン", "理由", "一致数"])
        self.pattern_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.pattern_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.pattern_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.pattern_table.verticalHeader().setVisible(False)
        self.pattern_table.verticalHeader().setDefaultSectionSize(28)
        header = self.pattern_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.pattern_table.itemSelectionChanged.connect(self._on_list_selection_changed)
        self.delete_button = QPushButton("選択したパターンを削除")
        self.delete_button.clicked.connect(self._delete_selected)
        self.reuse_combo = QComboBox()
        self.reuse_combo.setMinimumWidth(200)
        self.reuse_button = QPushButton("読み込む")
        self.reuse_button.clicked.connect(self._import_from_other)
        list_buttons = QHBoxLayout()
        list_buttons.addWidget(self.delete_button)
        list_buttons.addStretch(1)
        reuse = QHBoxLayout()
        reuse.addWidget(QLabel("他の枠組みの除外を再利用:"))
        reuse.addWidget(self.reuse_combo, 1)
        reuse.addWidget(self.reuse_button)

        list_box = QGroupBox("② 登録済みのパターン (行を選ぶとフォームに読み込んで修正できます)")
        list_layout = QVBoxLayout(list_box)
        list_layout.addWidget(self.pattern_table, 1)
        list_layout.addLayout(list_buttons)
        list_layout.addLayout(reuse)

        left = QSplitter(Qt.Vertical)
        left.addWidget(form_box)
        left.addWidget(list_box)
        left.setSizes([360, 260])

        # --- プレビュー
        self.count_label = QLabel("")
        self.count_label.setWordWrap(True)
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
        self.preview_view.verticalHeader().setDefaultSectionSize(24)
        # 全行を走査する ResizeToContents は数千行で重いので、列幅は固定にする
        self.preview_view.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.preview_view.horizontalHeader().setStretchLastSection(True)
        self.preview_view.doubleClicked.connect(lambda _index: self._load_selected_rules_into_form())
        self.from_rule_button = QPushButton("選択した組合せをフォームに入れる")
        self.from_rule_button.setToolTip(
            "1行選択: その Y/N をフォームに入れます (一部を - に変えてから追加できます)。行のダブルクリックでも同じです。\n"
            "複数行選択: フォームの理由で、選んだ組合せをそれぞれパターンとして追加します。"
        )
        self.from_rule_button.clicked.connect(self._load_selected_rules_into_form)
        legend = QLabel(
            f"<span style='background:{_EXCLUDED_BG.name()}'>&nbsp;除外&nbsp;</span>　"
            f"<span style='background:{_FORM_MATCH_BG.name()}'>&nbsp;フォームのパターンに一致&nbsp;</span>"
        )
        preview_top = QHBoxLayout()
        preview_top.addWidget(legend)
        preview_top.addStretch(1)
        preview_top.addWidget(self.filter_combo)
        preview_box = QGroupBox("プレビュー (全組合せ)")
        preview_layout = QVBoxLayout(preview_box)
        preview_layout.addWidget(self.count_label)
        preview_layout.addLayout(preview_top)
        preview_layout.addWidget(self.preview_view, 1)
        preview_layout.addWidget(self.from_rule_button)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(preview_box)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        # --- 保存
        self.save_button = QPushButton("除外を保存")
        self.save_button.setMinimumHeight(32)
        self.save_button.clicked.connect(self.save)
        self.revert_button = QPushButton("編集を取り消す")
        self.revert_button.setMinimumHeight(32)
        self.revert_button.clicked.connect(lambda: self.set_record(self._record, self._all_records, self._max_conditions))
        self.save_note = QLabel("")
        self.save_note.setWordWrap(True)
        self.save_note.setStyleSheet("color: #555;")
        save_row = QHBoxLayout()
        save_row.addWidget(self.save_note, 1)
        save_row.addWidget(self.revert_button)
        save_row.addWidget(self.save_button)

        layout = QVBoxLayout(self)
        layout.addWidget(caution)
        layout.addWidget(splitter, 1)
        layout.addLayout(save_row)

    def _build_condition_rows(self) -> None:
        while self.conditions_grid.count():
            item = self.conditions_grid.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._value_groups = []
        bold = QFont(self.font())
        bold.setBold(True)
        for i, text in enumerate(self._conditions):
            cid = QLabel(f"C{i + 1}")
            cid.setFont(bold)
            label = QLabel(text)
            label.setWordWrap(True)
            group = QButtonGroup(self)
            group.setExclusive(True)
            buttons = QHBoxLayout()
            buttons.setSpacing(2)
            for value in PATTERN_CHOICES:
                button = QPushButton(value)
                button.setCheckable(True)
                button.setFixedSize(44, 28)
                button.setToolTip({"Y": "成立する", "N": "成立しない", PATTERN_ANY: "どちらでもよい"}[value])
                button.setStyleSheet("QPushButton:checked { background: #3b6fd8; color: white; font-weight: bold; }")
                group.addButton(button)
                buttons.addWidget(button)
            group.buttons()[-1].setChecked(True)  # 初期値は -
            group.buttonClicked.connect(self._on_value_button_clicked)
            holder = QWidget()
            holder.setLayout(buttons)
            buttons.setContentsMargins(0, 0, 0, 0)
            self.conditions_grid.addWidget(cid, i, 0, Qt.AlignTop)
            self.conditions_grid.addWidget(label, i, 1)
            self.conditions_grid.addWidget(holder, i, 2, Qt.AlignTop)
            self._value_groups.append(group)
        self.conditions_grid.setRowStretch(len(self._conditions), 1)

    # ------------------------------------------------------------- 読み込み
    def set_record(self, record: FrameworkRecord | None, all_records: list[FrameworkRecord], max_conditions: int = 6) -> None:
        self._record = record
        self._all_records = all_records
        self._max_conditions = max_conditions
        self._conditions, self._all_rules, self._patterns = [], [], []
        self._dirty = False
        if record is None:
            self.setEnabled(False)
            self._build_condition_rows()
            self._render_list()
            self.preview_model.set_rows([], [])
            return
        try:
            self._conditions = record.decision_items_obj().conditions
            self._all_rules = dt_framework.all_rules(len(self._conditions))
            self._patterns = list(record.exclusion_patterns())
        except dt_framework.FrameworkError as exc:
            self.setEnabled(False)
            self._conditions, self._all_rules, self._patterns = [], [], []
            self._build_condition_rows()
            self._render_list()
            self.save_note.setText(f"この枠組みでは除外を編集できません: {exc}")
            self.preview_model.set_rows([], [])
            return
        self.setEnabled(True)
        self._build_condition_rows()
        self._fill_reuse_combo()
        self.save_note.setText(
            "承認済みの枠組みです。保存すると新しい版 (下書き) を作ります (元の版は変わりません)。"
            if record.status == STATUS_APPROVED else "下書きの枠組みです。保存するとこの枠組みを更新します。"
        )
        self._clear_form()
        self._render_list()
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

    # ------------------------------------------------------------- フォーム
    def form_values(self) -> tuple[str, ...]:
        return tuple(g.checkedButton().text() if g.checkedButton() else PATTERN_ANY for g in self._value_groups)

    def set_form_values(self, values: tuple[str, ...]) -> None:
        for group, value in zip(self._value_groups, values):
            for button in group.buttons():
                if button.text() == value:
                    button.setChecked(True)
        self._sync_pattern_text()
        self._update_form_feedback()

    def _form_pattern(self) -> ExclusionPattern | None:
        values = self.form_values()
        if not values or all(v == PATTERN_ANY for v in values):
            return None
        return ExclusionPattern(values, self.reason_edit.text().strip())

    def _sync_pattern_text(self) -> None:
        self._syncing = True
        self.pattern_text.setText(" ".join(self.form_values()))
        self._syncing = False

    def _on_value_button_clicked(self, _button=None) -> None:
        self._sync_pattern_text()
        self._on_form_touched()

    def _on_pattern_text_edited(self, text: str) -> None:
        if self._syncing:
            return
        try:
            values = dt_framework.parse_pattern(text, len(self._conditions))
        except dt_framework.FrameworkError:
            self.form_match_label.setText("(Y / N / - を条件の数だけ入力)")
            self.form_match_label.setStyleSheet("color: #777;")
            return
        for group, value in zip(self._value_groups, values):
            for button in group.buttons():
                if button.text() == value:
                    button.setChecked(True)
        self._on_form_touched()

    def _on_form_touched(self, *_args) -> None:
        self._form_touched = True
        self._set_form_message("")
        self._update_form_feedback()

    def _update_form_feedback(self) -> None:
        pattern = self._form_pattern()
        self.preview_model.set_form_pattern(pattern)
        if pattern is None:
            self.form_match_label.setText("(すべて - のパターンは使えません)")
            self.form_match_label.setStyleSheet("color: #777;")
        else:
            hits = [r.rule_id for r in self._all_rules if pattern.matches(r.values)]
            shown = ", ".join(hits[:8]) + (" …" if len(hits) > 8 else "")
            self.form_match_label.setText(f"一致する組合せ: {len(hits)} 通り ({shown})")
            self.form_match_label.setStyleSheet("color: #7a5c00;")
        self.update_button.setEnabled(self._editing_row is not None)

    def _set_form_message(self, text: str, error: bool = True) -> None:
        self.form_message.setText(text)
        self.form_message.setStyleSheet(_ERROR_STYLE if error else _OK_STYLE)

    def _mark_reason_missing(self, missing: bool) -> None:
        self.reason_edit.setStyleSheet("QLineEdit { border: 2px solid #b00020; }" if missing else "")

    def _clear_form(self) -> None:
        self._editing_row = None
        self.pattern_table.blockSignals(True)
        self.pattern_table.clearSelection()
        self.pattern_table.blockSignals(False)
        self.set_form_values(tuple(PATTERN_ANY for _ in self._conditions))
        self.reason_edit.clear()
        self._mark_reason_missing(False)
        self._form_touched = False
        self._set_form_message("")
        self._update_form_feedback()

    def _validate_form(self, ignore_row: int | None = None) -> ExclusionPattern | None:
        _commit_input_method()
        pattern = self._form_pattern()
        if pattern is None:
            self._set_form_message("Y か N の条件を1つ以上選んでください (すべて - だと全組合せが除外されます)。")
            return None
        if not pattern.reason:
            self._mark_reason_missing(True)
            self.reason_edit.setFocus()
            self._set_form_message("理由を入力してください。")
            return None
        self._mark_reason_missing(False)
        for row, existing in enumerate(self._patterns):
            if row != ignore_row and existing.values == pattern.values:
                self._set_form_message(f"同じパターンが #{row + 1} に登録済みです。")
                return None
        return pattern

    def add_from_form(self) -> bool:
        pattern = self._validate_form()
        if pattern is None:
            return False
        self._patterns.append(pattern)
        self._after_patterns_changed()
        self._clear_form()
        self._set_form_message(f"#{len(self._patterns)} として追加しました (まだ保存していません)。", error=False)
        return True

    def update_from_form(self) -> bool:
        row = self._editing_row
        if row is None or not (0 <= row < len(self._patterns)):
            return False
        pattern = self._validate_form(ignore_row=row)
        if pattern is None:
            return False
        self._patterns[row] = pattern
        self._after_patterns_changed()
        self._clear_form()
        self._set_form_message(f"#{row + 1} を更新しました (まだ保存していません)。", error=False)
        return True

    def _on_enter_in_reason(self) -> None:
        if self._editing_row is not None:
            self.update_from_form()
        else:
            self.add_from_form()

    # ------------------------------------------------------------- 一覧
    def _render_list(self) -> None:
        counts = dt_framework.pattern_match_counts(self._all_rules, self._patterns) if self._all_rules else []
        self.pattern_table.blockSignals(True)
        self.pattern_table.setRowCount(len(self._patterns))
        for row, pattern in enumerate(self._patterns):
            cells = [
                str(row + 1),
                " ".join(pattern.values),
                pattern.reason or "(理由が未入力です)",
                str(counts[row]) if row < len(counts) else "",
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col != 2:
                    item.setTextAlignment(Qt.AlignCenter)
                if col == 1:
                    item.setToolTip("\n".join(
                        f"C{i + 1} {c}: {v}" for i, (c, v) in enumerate(zip(self._conditions, pattern.values))
                    ))
                if not pattern.reason:
                    item.setBackground(QBrush(_MISSING_BG))
                self.pattern_table.setItem(row, col, item)
        self.pattern_table.blockSignals(False)

    def _after_patterns_changed(self) -> None:
        self._dirty = True
        self._render_list()
        self._refresh_preview()

    def _on_list_selection_changed(self) -> None:
        rows = sorted({i.row() for i in self.pattern_table.selectedIndexes()})
        if len(rows) != 1:
            return
        row = rows[0]
        pattern = self._patterns[row]
        self._editing_row = row
        self.set_form_values(pattern.values)
        self.reason_edit.setText(pattern.reason)
        self._mark_reason_missing(not pattern.reason)
        self._form_touched = False
        self._set_form_message(f"#{row + 1} を読み込みました。直して「選択中のパターンを更新」を押してください。", error=False)
        self._update_form_feedback()

    def _delete_selected(self) -> None:
        rows = sorted({i.row() for i in self.pattern_table.selectedIndexes()}, reverse=True)
        if not rows:
            self._set_form_message("削除するパターンを②の一覧で選択してください。")
            return
        for row in rows:
            del self._patterns[row]
        self._after_patterns_changed()
        self._clear_form()

    def _load_selected_rules_into_form(self) -> None:
        rows = sorted({i.row() for i in self.preview_view.selectionModel().selectedRows()})
        rules = [r for r in (self.preview_model.rule_at(row) for row in rows) if r is not None]
        if not rules:
            self._set_form_message("プレビューで組合せ (行) を選択してください。")
            return
        if len(rules) == 1:
            reason = self.reason_edit.text()
            self._clear_form()
            self.set_form_values(rules[0].values)
            self.reason_edit.setText(reason)
            self._form_touched = True
            self._set_form_message(f"{rules[0].rule_id} の組合せを入れました。理由を書いて「追加」してください。", error=False)
            self.reason_edit.setFocus()
            return
        _commit_input_method()
        reason = self.reason_edit.text().strip()
        if not reason:
            self._mark_reason_missing(True)
            self.reason_edit.setFocus()
            self._set_form_message(f"{len(rules)} 行を選択中です。先に理由を入力してから押すと、同じ理由でまとめて追加します。")
            return
        existing = {p.values for p in self._patterns}
        added = 0
        for rule in rules:
            if rule.values not in existing:
                self._patterns.append(ExclusionPattern(rule.values, reason))
                existing.add(rule.values)
                added += 1
        self._after_patterns_changed()
        self._clear_form()
        self._set_form_message(f"{added} パターンを追加しました (重複 {len(rules) - added} 件は省略。まだ保存していません)。", error=False)

    def _import_from_other(self) -> None:
        framework_id = self.reuse_combo.currentData()
        source = next((r for r in self._all_records if r.framework_id == framework_id), None)
        if source is None:
            return
        mapped, dropped = dt_framework.remap_exclusions(source.exclusions, self._conditions)
        existing = {p.values for p in self._patterns}
        added = 0
        for p in mapped:
            if p.values not in existing:
                self._patterns.append(p)
                existing.add(p.values)
                added += 1
        if added:
            self._after_patterns_changed()
        message = f"{added} パターンを読み込みました (重複 {len(mapped) - added} 件は省略。まだ保存していません)。"
        if dropped:
            message += f" 条件が対応しないため {len(dropped)} パターンは読み込んでいません。"
        self._set_form_message(message, error=bool(dropped))

    # ------------------------------------------------------------- プレビュー
    def _refresh_preview(self) -> None:
        if not self._conditions:
            self.count_label.setText("")
            return
        hits_by_rule = []
        for rule in self._all_rules:
            hits = tuple(i for i, p in enumerate(self._patterns) if p.matches(rule.values))
            hits_by_rule.append((rule, hits))

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
        if self._dirty:
            text += "　<span style='color:#b00020'>(未保存の変更あり)</span>"
        self.count_label.setText(text)
        self.count_label.setStyleSheet(f"color: {color};")

        mode = self.filter_combo.currentData()
        if mode == FILTER_EXCLUDED:
            hits_by_rule = [x for x in hits_by_rule if x[1]]
        elif mode == FILTER_KEPT:
            hits_by_rule = [x for x in hits_by_rule if not x[1]]
        self.preview_model.set_rows(self._conditions, hits_by_rule)
        header = self.preview_view.horizontalHeader()
        header.resizeSection(0, 56)
        for col in range(1, len(self._conditions) + 1):
            header.resizeSection(col, 36)

    # ------------------------------------------------------------------ 保存
    def has_unsaved_changes(self) -> bool:
        return self._dirty

    def current_patterns(self) -> list[ExclusionPattern]:
        return list(self._patterns)

    def save(self) -> FrameworkRecord | None:
        if self._record is None:
            return None
        _commit_input_method()
        if self._form_touched and self._form_pattern() is not None:
            verb = "更新" if self._editing_row is not None else "追加"
            answer = QMessageBox.question(
                self, "フォームの内容",
                f"①のフォームに、まだ{verb}していないパターンがあります。{verb}してから保存しますか?\n"
                "(「いいえ」ならフォームの内容は保存しません)",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel, QMessageBox.Yes,
            )
            if answer == QMessageBox.Cancel:
                return None
            if answer == QMessageBox.Yes:
                done = self.update_from_form() if self._editing_row is not None else self.add_from_form()
                if not done:
                    return None
        patterns = self._patterns
        missing = [i for i, p in enumerate(patterns) if not p.reason]
        if missing:
            self.pattern_table.selectRow(missing[0])  # フォームに読み込まれる
            self._mark_reason_missing(True)
            self.reason_edit.setFocus()
            QMessageBox.warning(
                self, "理由が未入力です",
                f"理由が空のパターンがあります: {', '.join('#' + str(m + 1) for m in missing)}\n"
                f"#{missing[0] + 1} をフォームに読み込みました。理由を書いて「選択中のパターンを更新」してから保存してください。",
            )
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
            saved = framework_store.update_exclusions(self._record, list(patterns), self._max_conditions)
        except OSError as exc:
            QMessageBox.warning(self, "保存できません", str(exc))
            return None
        self._dirty = False
        self._form_touched = False
        if saved.framework_error:
            QMessageBox.warning(self, "枠組みを作れません", saved.framework_error)
        self.saved.emit(saved.framework_id)
        return saved
