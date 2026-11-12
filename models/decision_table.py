"""Decision Table (Markdown) の構造化表現.

比較機能 (services/decision_table_parser.py, services/table_comparison.py,
ui/comparison_view.py) で共通して使うデータ構造をここにまとめる。
"""

from __future__ import annotations

from dataclasses import dataclass, field

ROW_CATEGORY_CONDITION = "condition"
ROW_CATEGORY_ACTION = "action"


@dataclass(frozen=True)
class DecisionRow:
    """デシジョンテーブルの1行 (条件部 or 動作部の1項目)."""

    category: str  # ROW_CATEGORY_CONDITION / ROW_CATEGORY_ACTION
    label: str  # 記述 (原文そのまま。表記ゆれ判定にも使う)
    values: list[str]  # 各ルール列のセル値 (rule_ids と同じ順序・同じ長さ)


@dataclass
class DecisionTable:
    """1モデル分のデシジョンテーブル (Markdown をパースした結果)."""

    rule_ids: list[str] = field(default_factory=list)  # 例: ["R1", "R2", ...]
    rows: list[DecisionRow] = field(default_factory=list)

    @property
    def conditions(self) -> list[DecisionRow]:
        return [r for r in self.rows if r.category == ROW_CATEGORY_CONDITION]

    @property
    def actions(self) -> list[DecisionRow]:
        return [r for r in self.rows if r.category == ROW_CATEGORY_ACTION]


@dataclass(frozen=True)
class TableSource:
    """比較対象の入力1件 (呼び出し側の Experiment / 履歴レコードから変換する)."""

    key: str  # テーブル間で一意な内部識別子 (基本は model_id)
    title: str  # 画面表示用の名前 (モデル名など)
    content: str  # モデルが出力した Markdown
    ok: bool = True  # False の場合は生成失敗 (比較対象外) を表す
