"""モデルが出力した Markdown デシジョンテーブルを構造化データへ変換する.

想定する入力形式 (prompts/generate.txt の出力形式) :

    | 区分 | 記述 | R1 | R2 | R3 |
    |---|---|---|---|---|
    | 条件部 | 条件1 | Y | Y | N |
    | 動作部 | 動作1 | X | - | X |

区分列の文字列に「条件」「動作」が含まれるかどうかで行を分類するため、
モデルごとに多少表記が揺れても (「条件部」「条件」など) 壊れにくい。
"""

from __future__ import annotations

import re

from models.decision_table import (
    ROW_CATEGORY_ACTION,
    ROW_CATEGORY_CONDITION,
    DecisionRow,
    DecisionTable,
)


class DecisionTableParseError(ValueError):
    """Markdown からデシジョンテーブルを抽出できなかったことを表す."""


_CONDITION_KEYWORD = "条件"
_ACTION_KEYWORD = "動作"
_SEPARATOR_CELL_RE = re.compile(r"^:?-{1,}:?$")


def _classify_category(raw_category: str) -> str | None:
    text = raw_category.strip()
    if _CONDITION_KEYWORD in text:
        return ROW_CATEGORY_CONDITION
    if _ACTION_KEYWORD in text:
        return ROW_CATEGORY_ACTION
    return None


def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    non_empty = [c for c in cells if c != ""]
    return bool(non_empty) and all(_SEPARATOR_CELL_RE.match(c) for c in non_empty)


def parse_decision_table(markdown: str) -> DecisionTable:
    """Markdown 文字列から DecisionTable を組み立てる.

    表の体裁が多少崩れていても (区切り行の欠落、セル数の過不足など)
    できる限り解釈し、本当に表として読めない場合のみ例外を送出する。
    """
    table_lines = [line for line in (markdown or "").splitlines() if line.strip().startswith("|")]
    if len(table_lines) < 2:
        raise DecisionTableParseError("Markdown 表が見つかりませんでした。")

    header_cells = _split_row(table_lines[0])
    if len(header_cells) < 3:
        raise DecisionTableParseError("表のヘッダーにルール列 (R1, R2, ...) が見つかりませんでした。")
    rule_ids = header_cells[2:]

    rows: list[DecisionRow] = []
    for line in table_lines[1:]:
        cells = _split_row(line)
        if _is_separator_row(cells):
            continue
        if len(cells) < 2:
            continue
        category = _classify_category(cells[0])
        if category is None:
            continue
        label = cells[1]
        values = cells[2:]
        if len(values) < len(rule_ids):
            values = values + [""] * (len(rule_ids) - len(values))
        rows.append(DecisionRow(category=category, label=label, values=values[: len(rule_ids)]))

    if not rows:
        raise DecisionTableParseError("条件部・動作部の行が見つかりませんでした。")

    return DecisionTable(rule_ids=rule_ids, rows=rows)
