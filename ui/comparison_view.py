"""複数モデルのデシジョンテーブルを比較するタブ.

「同じ条件の組み合わせに対して、モデルごとに動作の結果が食い違っていないか」を
一覧できるようにする。実際の計算は services/table_comparison.py (Qt非依存) が行い、
このウィジェットは結果の描画とユーザー操作 (表記ゆれの手動統合) だけを担当する。

比較できない2パターンへの対応:
    1. 条件・動作の記述が完全に同じ (空白・改行の差異のみ) で、組み合わせの結果が違う
       -> 自動で対応付けて比較する (services.table_comparison の正規化処理)。
    2. 条件・動作の記述がテーブルごとに微妙に違い、自動では対応付けられない
       -> 「表記ゆれの解決」パネルで該当項目を一覧し、利用者が同じ項目として
          手動統合できるようにする。統合後は1と同じロジックで再比較する。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from models.decision_table import ROW_CATEGORY_ACTION, ROW_CATEGORY_CONDITION, DecisionTable, TableSource
from services.decision_table_parser import DecisionTableParseError, parse_decision_table
from services.table_comparison import (
    AliasGroup,
    ComparisonGroup,
    ComparisonResult,
    LabelAlignment,
    LabelGroup,
    LabelKey,
    align_labels,
    compare_tables,
)

_CATEGORY_LABEL_JA = {ROW_CATEGORY_CONDITION: "条件", ROW_CATEGORY_ACTION: "動作"}

_CONFLICT_COLOR = QColor("#ffd3d3")  # 同じ組み合わせなのに動作の結果が違う
_MISSING_COLOR = QColor("#eeeeee")  # そのモデルはこの組み合わせのルールを生成していない
_SPACER_COLOR = QColor("#dddddd")  # 組み合わせ同士の区切り
_GROUP_HEADER_COLOR = QColor("#f4f6fa")  # 縦表示の「組合せ」「条件」セル (組み合わせ内で共通の値)

ORIENTATION_HORIZONTAL = "horizontal"
ORIENTATION_VERTICAL = "vertical"


class ComparisonView(QWidget):
    """TableSource のリストを受け取り、比較結果タブとして表示するウィジェット."""

    def __init__(
        self,
        sources: list[TableSource],
        parent: QWidget | None = None,
        document: str = "",
        output_dir: str = "",
        orientation: str = ORIENTATION_HORIZONTAL,
    ) -> None:
        super().__init__(parent)
        self._sources = sources
        # horizontal: 列 = 組み合わせ×モデル (横に並べる) / vertical: 行 = 組み合わせ×モデル (縦に並べる)
        self._orientation = orientation
        # 縦表示で、各列がどの組み合わせか (番号, 組み合わせ)
        self._col_groups: list[tuple[int, ComparisonGroup]] = []
        self._document = document  # 追加記述案の作成で LLM に渡す元の文書 (Requirements)
        self._output_dir = output_dir  # 追加記述案の保存先となる実行結果フォルダ
        self._columns: list[_Column] = []
        self._titles: dict[str, str] = {s.key: s.title for s in sources}
        self._tables: dict[str, DecisionTable] = {}
        self._parse_errors: dict[str, str] = {}
        self._manual_aliases: list[AliasGroup] = []
        self._alignment: LabelAlignment | None = None
        self._result: ComparisonResult | None = None

        self._build_ui()
        self._parse_sources()
        self._refresh()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.notice_label = QLabel()
        self.notice_label.setWordWrap(True)
        self.notice_label.setStyleSheet("color: #b00020;")
        self.notice_label.hide()

        self.resolution_group = QGroupBox("表記ゆれの解決 (条件・動作の表現がテーブルごとに違うため自動対応付けできない項目)")
        self.unresolved_list = QListWidget()
        self.unresolved_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.merge_button = QPushButton("選択した項目を同じ項目として統合する")
        self.merge_button.clicked.connect(self._on_merge_selected)
        self.reset_alias_button = QPushButton("統合をすべて解除")
        self.reset_alias_button.clicked.connect(self._on_reset_aliases)
        resolution_buttons = QHBoxLayout()
        resolution_buttons.addWidget(self.merge_button)
        resolution_buttons.addWidget(self.reset_alias_button)
        resolution_buttons.addStretch(1)
        resolution_layout = QVBoxLayout(self.resolution_group)
        resolution_layout.addWidget(QLabel("同じ条件・動作を指しているものを複数選択してください (Ctrl+クリックで複数選択可)。"))
        resolution_layout.addWidget(self.unresolved_list, 1)
        resolution_layout.addLayout(resolution_buttons)
        self.resolution_group.hide()

        self.diff_only_check = QCheckBox("結果が異なる組み合わせのみ表示")
        self.diff_only_check.toggled.connect(self._render_table)
        self.summary_label = QLabel()

        filter_bar = QHBoxLayout()
        filter_bar.addWidget(self.diff_only_check)
        filter_bar.addWidget(self.summary_label, 1)

        legend = QLabel(
            f'<span style="background-color:{_CONFLICT_COLOR.name()};">&nbsp;&nbsp;</span> 結果が食い違う　'
            f'<span style="background-color:{_MISSING_COLOR.name()};">&nbsp;&nbsp;</span> そのモデルに該当ルールなし'
        )

        self.table = QTableWidget()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(False)

        self.additional_button = QPushButton("追加記述案を作成...")
        self.additional_button.setToolTip(
            "解釈が分かれている箇所 (自動検出 / 比較表で選択した組み合わせ / 手動指定) について、\n"
            "それぞれの解釈に一意に定まるような追加記述案を LLM に作成させます。\n"
            "比較表で組み合わせの列を選択してから押すと、その組み合わせが選ばれた状態で開きます。"
        )
        self.additional_button.clicked.connect(self._open_additional_dialog)
        self.additional_button.setEnabled(bool(self._document.strip()))
        if not self._document.strip():
            self.additional_button.setToolTip("元の文書 (Requirements) が無いため使用できません。")
        top_bar = QHBoxLayout()
        top_bar.addStretch(1)
        top_bar.addWidget(self.additional_button)

        layout = QVBoxLayout(self)
        layout.addLayout(top_bar)
        layout.addWidget(self.notice_label)
        layout.addWidget(self.resolution_group)
        layout.addLayout(filter_bar)
        layout.addWidget(legend)
        layout.addWidget(self.table, 1)

    # --------------------------------------------------------------- 解析
    def _parse_sources(self) -> None:
        for source in self._sources:
            if not source.ok:
                continue
            try:
                self._tables[source.key] = parse_decision_table(source.content)
            except DecisionTableParseError as exc:
                self._parse_errors[source.key] = str(exc)

    def _excluded_notice(self) -> str:
        failed = [s.title for s in self._sources if not s.ok]
        unparsable = [self._titles[key] for key in self._parse_errors]
        parts = []
        if failed:
            parts.append(f"生成失敗のため比較対象外: {', '.join(failed)}")
        if unparsable:
            parts.append(f"表として解析できなかったため比較対象外: {', '.join(unparsable)}")
        return " / ".join(parts)

    # ------------------------------------------------------------- 再計算
    def _refresh(self) -> None:
        """エイリアスの変更などをもとに、対応付けと比較結果を再計算して描画する."""
        notice = self._excluded_notice()
        if notice:
            self.notice_label.setText(notice)
            self.notice_label.show()
        else:
            self.notice_label.hide()

        if len(self._tables) < 2:
            self._alignment = None
            self._result = None
            self.resolution_group.hide()
            self.diff_only_check.setEnabled(False)
            self.summary_label.setText("")
            self._clear_table()
            self.table.setRowCount(0)
            self.table.setColumnCount(0)
            self.notice_label.setText(
                (notice + "\n" if notice else "")
                + f"比較するには正常なデシジョンテーブルが2つ以上必要です (現在 {len(self._tables)} 件)。"
            )
            self.notice_label.show()
            return

        self.diff_only_check.setEnabled(True)
        self._alignment = align_labels(self._tables, self._manual_aliases)
        self._result = compare_tables(self._tables, self._alignment)
        self._render_unresolved()
        self._render_table()

    # ------------------------------------------------------- 表記ゆれパネル
    def _render_unresolved(self) -> None:
        self.unresolved_list.clear()
        unresolved = self._alignment.incomplete_groups(ROW_CATEGORY_CONDITION) + self._alignment.incomplete_groups(
            ROW_CATEGORY_ACTION
        )
        if not unresolved:
            self.resolution_group.hide()
            return

        for group in unresolved:
            table_names = "、".join(self._titles.get(k, k) for k in group.members)
            category_ja = _CATEGORY_LABEL_JA[group.category]
            item = QListWidgetItem(f"[{category_ja}] 「{group.display_label}」 — {table_names} のみに存在")
            item.setData(Qt.UserRole, group)
            self.unresolved_list.addItem(item)
        self.resolution_group.show()

    def _on_merge_selected(self) -> None:
        groups: list[LabelGroup] = [item.data(Qt.UserRole) for item in self.unresolved_list.selectedItems()]
        if len(groups) < 2:
            QMessageBox.information(self, "統合", "統合する項目を2つ以上選択してください。")
            return
        if len({g.category for g in groups}) > 1:
            QMessageBox.warning(self, "統合できません", "条件どうし、動作どうしでのみ統合できます。")
            return

        default_name = groups[0].display_label
        name, accepted = QInputDialog.getText(self, "統合後の表示名", "この項目の表示名:", text=default_name)
        if not accepted or not name.strip():
            return

        members = frozenset(
            LabelKey(table_key, group.category, raw_label)
            for group in groups
            for table_key, raw_label in group.members.items()
        )
        self._manual_aliases.append(AliasGroup(members=members, display_label=name.strip()))
        self._refresh()

    def _on_reset_aliases(self) -> None:
        if not self._manual_aliases:
            return
        self._manual_aliases.clear()
        self._refresh()

    # ------------------------------------------------------- 追加記述案
    def _selected_comparison_groups(self) -> list[tuple[int, ComparisonGroup]]:
        """比較表で選択されているセルが属する組み合わせを (番号, 組み合わせ) で返す."""
        selected: list[tuple[int, ComparisonGroup]] = []
        seen: set[int] = set()
        if self._orientation == ORIENTATION_VERTICAL:
            for index in sorted(self.table.selectedIndexes(), key=lambda i: i.column()):
                picked = self._col_groups[index.column()] if index.column() < len(self._col_groups) else None
                if picked is not None and picked[0] not in seen:
                    seen.add(picked[0])
                    selected.append(picked)
            return selected
        for index in sorted(self.table.selectedIndexes(), key=lambda i: i.column()):
            if index.column() >= len(self._columns):
                continue
            column = self._columns[index.column()]
            if column.is_spacer or column.comparison_group is None or column.group_number in seen:
                continue
            seen.add(column.group_number)
            selected.append((column.group_number, column.comparison_group))
        return selected

    def _open_additional_dialog(self) -> None:
        from ui.additional_description_dialog import AdditionalDescriptionDialog

        dialog = AdditionalDescriptionDialog(
            document=self._document,
            output_dir=self._output_dir,
            tables=self._tables,
            titles=self._titles,
            comparison=self._result,
            selected_groups=self._selected_comparison_groups(),
            parent=self,
        )
        dialog.exec()

    # ----------------------------------------------------------- 比較表
    def _render_table(self) -> None:
        if self._result is None or self._alignment is None:
            return
        result = self._result
        table_keys = self._alignment.table_keys

        self._clear_table()

        total = len(result.groups)
        conflicts = result.conflict_count
        summary = f"モデル数: {len(table_keys)}　組み合わせ数: {total}　うち結果が異なる組み合わせ: {conflicts}"
        self.summary_label.setText(summary)
        self.summary_label.setStyleSheet("color: #b00020;" if conflicts else "color: #1a7a1a;")

        if not result.condition_groups or not result.action_groups:
            self.table.setRowCount(0)
            self.table.setColumnCount(0)
            missing = "条件" if not result.condition_groups else "動作"
            self.notice_label.setText(
                f"⚠ 全テーブルに共通する{missing}が見つからないため比較できません。"
                "上の「表記ゆれの解決」で対応する項目を統合してください。"
            )
            self.notice_label.show()
            return

        numbered_groups = list(enumerate(result.groups, start=1))
        if self.diff_only_check.isChecked():
            numbered_groups = [(n, g) for n, g in numbered_groups if g.has_conflict]

        if self._orientation == ORIENTATION_VERTICAL:
            self._populate_vertical(result, table_keys, numbered_groups)
        else:
            self._populate_table(result, table_keys, numbered_groups)

    def _clear_table(self) -> None:
        self.table.clear()
        self.table.clearSpans()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self._columns = []
        self._col_groups = []

    def _populate_vertical(
        self,
        result: ComparisonResult,
        table_keys: list[str],
        numbered_groups: list[tuple[int, ComparisonGroup]],
    ) -> None:
        """縦表示: 通常のデシジョンテーブルと同じく条件を行に並べ、1列 = 1組み合わせにする.

        条件の行は1つずつ (組み合わせ内で共通の値)。動作 (結果) の行だけを
        モデルの数だけ縦に並べるので、同じ列を上から下へ見ればモデル間の違いがわかる。

            [条件] A               | Y | N | ...
            [条件] B               | N | N | ...
            -------------------------------------
            [動作] 料金 / モデル1   | 800 | ...
            [動作] 料金 / モデル2   | 700 | ...   <- 食い違いは赤
        """
        # 行の組み立て: ("condition", group) / ("action", group, table_key) / ("spacer",)
        row_plan: list[tuple] = [("condition", g) for g in result.condition_groups]
        for act_group in result.action_groups:
            row_plan.append(("spacer",))
            row_plan.extend(("action", act_group, key) for key in table_keys)

        self.table.verticalHeader().setVisible(True)
        self.table.setRowCount(len(row_plan))
        self.table.setColumnCount(len(numbered_groups))
        self.table.setHorizontalHeaderLabels(
            [f"組合せ{n}" + (" ⚠" if g.has_conflict else "") for n, g in numbered_groups]
        )
        vertical_labels = []
        for plan in row_plan:
            if plan[0] == "condition":
                vertical_labels.append(f"[条件] {plan[1].display_label}")
            elif plan[0] == "action":
                vertical_labels.append(f"[動作] {plan[1].display_label}  /  {self._titles.get(plan[2], plan[2])}")
            else:
                vertical_labels.append("")
        self.table.setVerticalHeaderLabels(vertical_labels)
        self._col_groups = list(numbered_groups)

        for col, (number, group) in enumerate(numbered_groups):
            header_item = self.table.horizontalHeaderItem(col)
            if group.has_conflict:
                header_item.setToolTip("この組み合わせはモデルによって結果が異なります。")

            for row, plan in enumerate(row_plan):
                if plan[0] == "spacer":
                    item = QTableWidgetItem("")
                    item.setFlags(Qt.ItemIsEnabled)
                    item.setBackground(QBrush(_SPACER_COLOR))
                elif plan[0] == "condition":
                    item = self._plain_item(group.condition_display.get(plan[1].group_id, ""))
                    item.setBackground(QBrush(_GROUP_HEADER_COLOR))
                else:
                    item = self._action_item(group, plan[1], plan[2])
                self.table.setItem(row, col, item)

        self.table.resizeColumnsToContents()
        for row, plan in enumerate(row_plan):
            if plan[0] == "spacer":
                self.table.setRowHeight(row, 6)
        self.table.horizontalHeader().setMinimumHeight(30)

    def _action_item(self, group: ComparisonGroup, act_group: LabelGroup, table_key: str) -> QTableWidgetItem:
        """縦表示の動作セル (あるモデル・ある組み合わせの結果)."""
        title = self._titles.get(table_key, table_key)
        entries = [e for e in group.entries if e.table_key == table_key]
        if not entries:
            item = self._plain_item("—")
            item.setBackground(QBrush(_MISSING_COLOR))
            item.setToolTip(f"{title} はこの組み合わせのルールを生成していません。")
            return item

        # 同じモデルの複数ルールが同じ組み合わせに入る場合 (表記ゆれ未解決など) は並べて表示する
        values = [e.action_values.get(act_group.group_id, "") for e in entries]
        item = self._plain_item(" / ".join(values))
        item.setToolTip(f"{title} の {', '.join(e.rule_id for e in entries)}")
        if act_group.group_id in group.conflicting_action_groups:
            item.setBackground(QBrush(_CONFLICT_COLOR))
        return item

    @staticmethod
    def _plain_item(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignCenter)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        return item

    def _populate_table(
        self,
        result: ComparisonResult,
        table_keys: list[str],
        numbered_groups: list[tuple[int, ComparisonGroup]],
    ) -> None:
        self.table.verticalHeader().setVisible(True)
        columns = _build_columns(table_keys, numbered_groups)
        self._columns = columns
        row_specs = [(ROW_CATEGORY_CONDITION, g) for g in result.condition_groups] + [
            (ROW_CATEGORY_ACTION, g) for g in result.action_groups
        ]

        self.table.setRowCount(len(row_specs))
        self.table.setColumnCount(len(columns))
        self.table.setVerticalHeaderLabels(
            [f"[{_CATEGORY_LABEL_JA[cat]}] {g.display_label}" for cat, g in row_specs]
        )
        self.table.setHorizontalHeaderLabels([col.header_text(self._titles) for col in columns])

        for row_index, (category, row_group) in enumerate(row_specs):
            col_index = 0
            while col_index < len(columns):
                column = columns[col_index]
                if column.is_spacer:
                    self._set_spacer_cell(row_index, col_index)
                    col_index += 1
                    continue

                if category == ROW_CATEGORY_CONDITION:
                    span = _span_length(columns, col_index)
                    value = column.comparison_group.condition_display.get(row_group.group_id, "")
                    rule_tip = "、".join(
                        f"{self._titles.get(e.table_key, e.table_key)}:{e.rule_id}"
                        for e in column.comparison_group.entries
                    )
                    self._set_condition_cell(row_index, col_index, span, value, rule_tip)
                    col_index += span
                else:
                    self._set_action_cell(row_index, col_index, column, row_group)
                    col_index += 1

        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setMinimumHeight(44)

    def _set_spacer_cell(self, row: int, col: int) -> None:
        item = QTableWidgetItem("")
        item.setFlags(Qt.ItemIsEnabled)
        item.setBackground(QBrush(_SPACER_COLOR))
        self.table.setItem(row, col, item)
        self.table.setColumnWidth(col, 6)

    def _set_condition_cell(self, row: int, col: int, span: int, value: str, tooltip: str = "") -> None:
        item = QTableWidgetItem(value)
        item.setTextAlignment(Qt.AlignCenter)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        if tooltip:
            item.setToolTip(tooltip)
        self.table.setItem(row, col, item)
        if span > 1:
            self.table.setSpan(row, col, 1, span)

    def _set_action_cell(self, row: int, col: int, column: "_Column", action_group: LabelGroup) -> None:
        group = column.comparison_group
        entry = group.entry_for_table(column.table_key)
        item = QTableWidgetItem()
        item.setTextAlignment(Qt.AlignCenter)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)

        if entry is None:
            item.setText("—")
            item.setBackground(QBrush(_MISSING_COLOR))
            item.setToolTip(f"{self._titles.get(column.table_key, column.table_key)} はこの組み合わせのルールを生成していません。")
        else:
            item.setText(entry.action_values.get(action_group.group_id, ""))
            item.setToolTip(f"{self._titles.get(column.table_key, column.table_key)} の {entry.rule_id}")
            if action_group.group_id in group.conflicting_action_groups:
                item.setBackground(QBrush(_CONFLICT_COLOR))
        self.table.setItem(row, col, item)


# ------------------------------------------------------------- 列の組み立て

@dataclass(frozen=True)
class _Column:
    is_spacer: bool
    group_number: int = 0
    comparison_group: ComparisonGroup | None = None
    table_key: str = ""

    def header_text(self, titles: dict[str, str]) -> str:
        if self.is_spacer:
            return ""
        return f"組合せ{self.group_number}\n{titles.get(self.table_key, self.table_key)}"


def _build_columns(table_keys: list[str], numbered_groups: list[tuple[int, ComparisonGroup]]) -> list[_Column]:
    columns: list[_Column] = []
    for number, group in numbered_groups:
        if columns:
            columns.append(_Column(is_spacer=True))
        for table_key in table_keys:
            columns.append(_Column(is_spacer=False, group_number=number, comparison_group=group, table_key=table_key))
    return columns


def _span_length(columns: list[_Column], start: int) -> int:
    """start 列から始まる、同じ組み合わせに属する (スペーサーでない) 列数を数える."""
    group = columns[start].comparison_group
    length = 0
    index = start
    while index < len(columns) and not columns[index].is_spacer and columns[index].comparison_group is group:
        length += 1
        index += 1
    return length
