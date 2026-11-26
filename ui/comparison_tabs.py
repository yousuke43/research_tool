"""結果画面 (実行直後 / 過去の結果) に比較系のタブを追加する共通処理.

タブの並び (左から):
    比較 (縦)            <- 結果を開いたときに最初に表示する
    テーブル一覧 (縦並び)
    Extracted Decision Items / 各モデル   (呼び出し側が先に追加しておく)
    比較 (横)            <- 従来の比較タブ

新方式 (共通DT枠組み) の結果では、さらに先頭に
    共通DT比較 / 共通DT枠組み
を追加する (add_framework_tabs)。
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
    horizontal = ComparisonView(sources, document=document, output_dir=output_dir, orientation=ORIENTATION_HORIZONTAL)
    vertical = ComparisonView(sources, document=document, output_dir=output_dir, orientation=ORIENTATION_VERTICAL)
    # 「比較対象から外す」は縦・横のどちらで操作しても両方に反映する
    vertical.excluded_changed.connect(horizontal.set_excluded)
    horizontal.excluded_changed.connect(vertical.set_excluded)

    tabs.addTab(horizontal, "比較 (横)")
    tabs.insertTab(0, vertical, "比較 (縦)")
    tabs.insertTab(1, StackedTablesView(sources), "テーブル一覧 (縦並び)")
    tabs.setCurrentIndex(0)


def add_framework_tabs(tabs: QTabWidget, record: dict) -> None:
    """新方式 (共通DT枠組み) の結果に「共通DT比較」「共通DT枠組み」タブを追加する.

    record は results.json の内容 (Experiment.to_dict() と同じ形式)。従来方式の結果では何もしない。
    """
    from models.experiment import METHOD_COMMON_FRAMEWORK
    from services.dt_framework import CommonFramework, to_markdown
    from services.framework_comparison import fills_from_records
    from ui.framework_comparison_view import FrameworkComparisonView
    from ui.result_view import ResultView

    if record.get("generation_method") != METHOD_COMMON_FRAMEWORK:
        return
    if record.get("framework_status") != "ok" or not record.get("framework"):
        message = record.get("framework_error") or "共通DTの枠組みが作成されていません。"
        view = ResultView(f"# 共通DTを生成していません\n\n{message}\n", caption="新方式 (共通DT枠組み)")
        tabs.insertTab(0, view, "⚠ 共通DT")
        tabs.setCurrentIndex(0)
        return

    framework = CommonFramework.from_dict(record["framework"])
    settings = record.get("generation_settings") or {}
    tabs.insertTab(0, FrameworkComparisonView(framework, fills_from_records(record.get("results", []), framework)), "共通DT比較")
    caption = (
        f"方式: {framework.method} v{framework.version} ({framework.strategy})  /  "
        f"条件 {len(framework.conditions)} 個・{len(framework.rules)} ルール"
        + (f" (全 {framework.total_combinations} 通りから {len(framework.excluded_rules)} 通りを除外)"
           if framework.excluded_rules else "")
        + (f"  /  バッチサイズ {settings.get('framework_batch_size')}" if settings.get("framework_batch_size") else "")
    )
    tabs.insertTab(1, ResultView(to_markdown(framework), caption=caption), "共通DT枠組み")
    tabs.setCurrentIndex(0)
