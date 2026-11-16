"""各モデルのデシジョンテーブルを、加工せずにそのまま縦に並べるタブ (人手での見比べ用).

比較タブのような対応付け・組み合わせ単位の再構成は行わず、モデルが出力した表を
上から順に表示するだけ。列幅 (区分・記述・各ルール列) をすべての表でそろえるので、
上下にスクロールしながら見比べやすい。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from models.decision_table import ROW_CATEGORY_CONDITION, DecisionTable, TableSource
from services.decision_table_parser import DecisionTableParseError, parse_decision_table

_CONDITION_COLOR = QColor("#f4f6fa")
_ACTION_COLOR = QColor("#fbf7ee")


class StackedTablesView(QWidget):
    def __init__(self, sources: list[TableSource], parent: QWidget | None = None) -> None:
        super().__init__(parent)

        parsed: list[tuple[TableSource, DecisionTable | None, str]] = []
        for source in sources:
            if not source.ok:
                parsed.append((source, None, "生成失敗"))
                continue
            try:
                parsed.append((source, parse_decision_table(source.content), ""))
            except DecisionTableParseError as exc:
                parsed.append((source, None, f"表として解析できませんでした: {exc}"))

        container = QWidget()
        box = QVBoxLayout(container)
        box.setSpacing(6)

        tables: list[QTableWidget] = []
        for source, table, error in parsed:
            title = QLabel(source.title)
            title_font = QFont(title.font())
            title_font.setBold(True)
            title_font.setPointSize(title_font.pointSize() + 1)
            title.setFont(title_font)
            title.setToolTip(source.key)
            box.addWidget(title)

            if table is None:
                note = QLabel(f"⚠ {error}")
                note.setStyleSheet("color: #b00020;")
                box.addWidget(note)
                if source.content.strip():
                    raw = QPlainTextEdit(source.content)
                    raw.setReadOnly(True)
                    raw.setMaximumHeight(160)
                    box.addWidget(raw)
            else:
                widget = _build_table_widget(table)
                tables.append(widget)
                box.addWidget(widget)

            line = QFrame()
            line.setFrameShape(QFrame.HLine)
            line.setStyleSheet("color: #ccc;")
            box.addWidget(line)
        box.addStretch(1)

        _align_column_widths(tables)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)

        hint = QLabel(
            "各モデルの Decision Table をそのまま縦に並べています (列幅はすべての表でそろえています)。"
            "　R1, R2… の見出しをつかんでドラッグすると、列を並び替えられます。"
        )
        hint.setStyleSheet("color: #555;")
        hint.setWordWrap(True)
        reset_button = QPushButton("R列の並びを元に戻す")
        reset_button.clicked.connect(lambda: [_reset_column_order(t) for t in tables])
        top_bar = QHBoxLayout()
        top_bar.addWidget(hint, 1)
        top_bar.addWidget(reset_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(top_bar)
        layout.addWidget(scroll, 1)


def _build_table_widget(table: DecisionTable) -> QTableWidget:
    widget = QTableWidget(len(table.rows), 2 + len(table.rule_ids))
    widget.setHorizontalHeaderLabels(["区分", "記述"] + table.rule_ids)
    widget.verticalHeader().setVisible(False)
    widget.setEditTriggers(QAbstractItemView.NoEditTriggers)
    widget.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    # 表自身はスクロールさせず、外側の QScrollArea でまとめてスクロールする
    widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    widget.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    for row_index, row in enumerate(table.rows):
        is_condition = row.category == ROW_CATEGORY_CONDITION
        color = QBrush(_CONDITION_COLOR if is_condition else _ACTION_COLOR)
        cells = ["条件部" if is_condition else "動作部", row.label] + list(row.values)
        for col, text in enumerate(cells):
            item = QTableWidgetItem(text)
            item.setBackground(color)
            if col != 1:
                item.setTextAlignment(Qt.AlignCenter)
            widget.setItem(row_index, col, item)

    widget.resizeColumnsToContents()
    widget.resizeRowsToContents()
    _make_rule_columns_movable(widget)
    return widget


_FIXED_COLUMNS = 2  # 「区分」「記述」は動かさない


def _make_rule_columns_movable(widget: QTableWidget) -> None:
    """R1, R2, ... の列見出しをつかんでドラッグし、並び替えられるようにする.

    QHeaderView には「一部の列だけ固定」の機能がないので、区分・記述の列が動いた
    (または R列が区分・記述より左へ入った) 場合は、移動を取り消して元に戻す。
    """
    header = widget.horizontalHeader()
    header.setSectionsMovable(True)
    header.setToolTip("R列の見出しをつかんでドラッグすると、列の並びを入れ替えられます。")

    def on_moved(logical: int, old_visual: int, new_visual: int) -> None:
        if logical < _FIXED_COLUMNS or new_visual < _FIXED_COLUMNS:
            header.blockSignals(True)
            header.moveSection(new_visual, old_visual)
            header.blockSignals(False)

    header.sectionMoved.connect(on_moved)


def _reset_column_order(widget: QTableWidget) -> None:
    header = widget.horizontalHeader()
    header.blockSignals(True)
    for logical in range(widget.columnCount()):
        header.moveSection(header.visualIndex(logical), logical)
    header.blockSignals(False)


def _align_column_widths(tables: list[QTableWidget]) -> None:
    """全表で「区分」「記述」列とルール列の幅をそろえ、各表の高さを中身に合わせる."""
    if not tables:
        return
    category_width = max(t.columnWidth(0) for t in tables)
    label_width = min(max(t.columnWidth(1) for t in tables), 520)
    rule_width = max(
        (t.columnWidth(c) for t in tables for c in range(2, t.columnCount())),
        default=48,
    )
    rule_width = max(rule_width, 48)
    for t in tables:
        t.setColumnWidth(0, category_width)
        t.setColumnWidth(1, label_width)
        for c in range(2, t.columnCount()):
            t.setColumnWidth(c, rule_width)
        t.resizeRowsToContents()
        header = t.horizontalHeader()
        height = max(header.height(), header.sizeHint().height()) + sum(t.rowHeight(r) for r in range(t.rowCount()))
        width = sum(t.columnWidth(c) for c in range(t.columnCount()))
        t.setFixedSize(width + 2 * t.frameWidth(), height + 2 * t.frameWidth())
