"""新方式 (共通DT枠組み) の比較タブ「共通DT比較」.

全モデルの条件部は共通なので、1行 = 1ルール (同じルールID) として各モデルの動作結果を並べ、
完全一致 / 意味的一致 / 動作結果の不一致 / 適用可否の不一致 / 生成失敗・形式不正 を色分けする。
計算は services/framework_comparison.py (Qt非依存)。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from services.dt_framework import CommonFramework, format_value
from services.framework_comparison import (
    CATEGORY_APPLICABILITY_MISMATCH,
    CATEGORY_EXACT,
    CATEGORY_EXCLUDED,
    CATEGORY_FAILED,
    CATEGORY_LABELS,
    CATEGORY_SEMANTIC,
    CATEGORY_VALUE_MISMATCH,
    ModelFill,
    compare_fills,
)

_COLORS = {
    CATEGORY_EXACT: QColor("#ffffff"),
    CATEGORY_SEMANTIC: QColor("#e3f4e1"),
    CATEGORY_VALUE_MISMATCH: QColor("#ffd3d3"),
    CATEGORY_APPLICABILITY_MISMATCH: QColor("#ffe2b8"),
    CATEGORY_FAILED: QColor("#e4e4e4"),
    CATEGORY_EXCLUDED: QColor("#cfd6e3"),
}


class FrameworkComparisonView(QWidget):
    def __init__(self, framework: CommonFramework, fills: list[ModelFill], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._framework = framework
        self._fills = fills
        self._titles = {f.key: f.title or f.key for f in fills}
        self._result = compare_fills(framework, fills)

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.diff_only_check = QCheckBox("不一致 (動作結果・適用可否) のルールのみ表示")
        self.diff_only_check.toggled.connect(self._render)
        self.show_excluded_check = QCheckBox("除外した組合せも表示 (LLMには渡していない)")
        self.show_excluded_check.toggled.connect(self._render)
        self.show_excluded_check.setVisible(bool(framework.excluded_rules))

        legend = QLabel(
            "　".join(
                f'<span style="background-color:{_COLORS[c].name()};">&nbsp;&nbsp;&nbsp;</span> {label}'
                for c, label in CATEGORY_LABELS.items()
            )
        )
        note = QLabel(
            "条件部は全モデル共通 (プログラムで生成した全組合せ。除外パターンがある場合は除外後)。"
            "同じルールIDの動作結果を比較しています。「除外」は人が除外した組合せ、"
            "「適用不可」は LLM が成立しないと判断した組合せです。"
            "不一致が解釈差異か計算ミスかは自動では判定しません。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #555;")

        self.table = QTableWidget()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)

        top = QHBoxLayout()
        top.addWidget(self.diff_only_check)
        top.addWidget(self.show_excluded_check)
        top.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self.summary_label)
        layout.addWidget(note)
        layout.addLayout(top)
        layout.addWidget(legend)
        layout.addWidget(self.table, 1)
        self._render()

    def _render(self) -> None:
        result = self._result
        counts = result.counts()
        parts = [f"{CATEGORY_LABELS[c]}: {n}" for c, n in counts.items()]
        fw = self._framework
        text = f"ルール数: {len(result.rules)}"
        if fw.excluded_rules:
            text += f" (全 {fw.total_combinations} 通りから {len(fw.excluded_rules)} 通りを除外)"
        text += "　" + "　".join(parts)
        if result.partial_models:
            text += "\n形式不正 (型が合わない値など) だが、返ってきた値で比較に含めたモデル: " + "、".join(
                self._titles.get(k, k) for k in result.partial_models
            ) + "　(⚠ = 項目の型と合わない値)"
        if result.failed_models:
            text += "\n値を取り出せないため比較対象外: " + "、".join(self._titles.get(k, k) for k in result.failed_models)
        self.summary_label.setText(text)
        differences = counts[CATEGORY_VALUE_MISMATCH] + counts[CATEGORY_APPLICABILITY_MISMATCH]
        self.summary_label.setStyleSheet("color: #b00020;" if differences or result.failed_models else "color: #1a7a1a;")

        rules: list = list(result.rules)
        if self.diff_only_check.isChecked():
            rules = [r for r in rules if r.is_difference]
        if self.show_excluded_check.isChecked() and not self.diff_only_check.isChecked():
            rules += result.excluded
            rules.sort(key=lambda r: int(self._rule_id(r)[1:]) if self._rule_id(r)[1:].isdigit() else 0)
        valid_keys = [k for k in result.model_keys if k not in result.failed_models]
        cond_headers = [f"C{i}" for i in range(1, len(fw.conditions) + 1)]
        action_headers = [
            (action.name, key) for action in fw.actions for key in valid_keys
        ]
        headers = ["ルール"] + cond_headers + [f"{a}\n{self._titles.get(k, k)}" for a, k in action_headers] + ["判定"]

        self.table.clear()
        self.table.setRowCount(len(rules))
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        for i, text in enumerate(fw.conditions, start=1):
            self.table.horizontalHeaderItem(i).setToolTip(text)
        self.table.verticalHeader().setVisible(False)

        for row, rule in enumerate(rules):
            if isinstance(rule, dict):  # 除外した組合せ
                self._set_excluded_row(row, rule, len(headers))
                continue
            self.table.setItem(row, 0, self._item(rule.rule_id))
            for ci, value in enumerate(rule.conditions):
                self.table.setItem(row, 1 + ci, self._item(value, tooltip=fw.conditions[ci]))
            comparisons = {a.action: a for a in rule.actions}
            for col, (action_name, key) in enumerate(action_headers, start=1 + len(cond_headers)):
                comparison = comparisons[action_name]
                title = self._titles.get(key, key)
                if key not in comparison.values:
                    item = self._item("(なし)", tooltip=f"{title} / このルールの値がありません (形式不正)")
                    item.setBackground(QBrush(_COLORS[CATEGORY_FAILED]))
                    self.table.setItem(row, col, item)
                    continue
                text = format_value(comparison.values.get(key))
                tooltip = f"{title} / {CATEGORY_LABELS[comparison.category]}"
                if key in comparison.type_mismatch:
                    text += " ⚠"
                    tooltip += " / 項目の型と合わない値 (返ってきたまま比較しています)"
                item = self._item(text)
                item.setBackground(QBrush(_COLORS[comparison.category]))
                item.setToolTip(tooltip)
                self.table.setItem(row, col, item)
            verdict = self._item(CATEGORY_LABELS[rule.category])
            verdict.setBackground(QBrush(_COLORS[rule.category]))
            self.table.setItem(row, len(headers) - 1, verdict)

        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setMinimumHeight(40)

    @staticmethod
    def _rule_id(rule) -> str:
        return rule["rule_id"] if isinstance(rule, dict) else rule.rule_id

    def _set_excluded_row(self, row: int, rule: dict, column_count: int) -> None:
        brush = QBrush(_COLORS[CATEGORY_EXCLUDED])
        cells = [rule["rule_id"], *rule["conditions"]]
        for col in range(column_count):
            text = cells[col] if col < len(cells) else ""
            if col == column_count - 1:
                text = CATEGORY_LABELS[CATEGORY_EXCLUDED]
            item = self._item(text, tooltip=f"除外: {rule['reason']}")
            item.setBackground(brush)
            self.table.setItem(row, col, item)

    @staticmethod
    def _item(text: str, tooltip: str = "") -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignCenter)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        if tooltip:
            item.setToolTip(tooltip)
        return item
