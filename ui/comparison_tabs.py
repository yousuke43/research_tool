"""結果画面 (実行直後 / 過去の結果) に比較系のタブを追加する共通処理.

タブの並び (左から):
    比較 (縦)            <- 結果を開いたときに最初に表示する
    テーブル一覧 (縦並び)
    Extracted Decision Items / 各モデル   (呼び出し側が先に追加しておく)
    比較 (横)            <- 従来の比較タブ
"""

from __future__ import annotations

from PySide6.QtWidgets import QTabWidget

from models.decision_table import TableSource
from ui.comparison_view import ORIENTATION_HORIZONTAL, ORIENTATION_VERTICAL, ComparisonView
from ui.stacked_tables_view import StackedTablesView


def add_comparison_tabs(tabs: QTabWidget, sources: list[TableSource], document: str, output_dir: str) -> None:
    """比較(縦)・テーブル一覧を先頭に、比較(横)を末尾に追加し、比較(縦)を選択状態にする."""
    if not sources:
        return
    tabs.addTab(
        ComparisonView(sources, document=document, output_dir=output_dir, orientation=ORIENTATION_HORIZONTAL),
        "比較 (横)",
    )
    tabs.insertTab(
        0,
        ComparisonView(sources, document=document, output_dir=output_dir, orientation=ORIENTATION_VERTICAL),
        "比較 (縦)",
    )
    tabs.insertTab(1, StackedTablesView(sources), "テーブル一覧 (縦並び)")
    tabs.setCurrentIndex(0)
