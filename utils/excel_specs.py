"""data/ フォルダの Excel (.xlsx) から一括実行の対象行を読み込む.

1行 = 実験1回。列はヘッダー名で探すので、列の順番が変わっても動く。
必要な列がそろっていないシート (メモ用のシートなど) は読み飛ばす。

実行対象は「区分」が変数追加版で始まり、かつ「実行」列に〇が入っている行。
モデルに渡すのは「内容」列だけで、他の列は results.json に記録するだけ。
"""

from __future__ import annotations

from pathlib import Path

from models.spec_source import SpecSource
from utils.file_manager import safe_component

COL_NUMBER = "番号"
COL_CATEGORY = "区分"
COL_THEME = "テーマ"
COL_CONTENT = "内容"
COL_AMBIGUITY = "解釈揺れ"
COL_AMBIGUITY_DETAIL = "揺れの内容"
COL_RUN = "実行"

REQUIRED_COLUMNS = (COL_NUMBER, COL_CATEGORY, COL_THEME, COL_CONTENT, COL_AMBIGUITY, COL_RUN)
TARGET_CATEGORY = "変数追加版"
RUN_MARKS = {"〇", "○", "◯", "o", "O", "ｏ", "Ｏ"}

# ヘッダー行を探す範囲 (上にタイトル行などがあっても対応する)
_HEADER_SEARCH_ROWS = 10


def _text(value: object) -> str:
    # str.strip() は全角スペースも除去する ("　なし" -> "なし")
    return "" if value is None else str(value).strip()


def _find_header(rows: list[tuple]) -> tuple[int, dict[str, int]] | None:
    for index, row in enumerate(rows[:_HEADER_SEARCH_ROWS]):
        columns = {_text(v): i for i, v in enumerate(row) if _text(v)}
        if all(c in columns for c in REQUIRED_COLUMNS):
            return index, columns
    return None


def _folder_name(number: str, theme: str, ambiguity: str) -> str:
    if number.isdigit():
        number = f"{int(number):02d}"
    return safe_component(f"{number}_{theme}_{TARGET_CATEGORY}_{ambiguity or '不明'}")


def load_excel_specs(path: Path) -> tuple[list[SpecSource], list[str]]:
    """Excel から実行対象の行を読み込み、(対象行, 注意メッセージ) を返す."""
    import openpyxl  # 起動時間に影響しないよう使うときだけ読み込む

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    specs: list[SpecSource] = []
    notes: list[str] = []
    try:
        for ws in wb.worksheets:
            rows = list(ws.iter_rows(values_only=True))
            header = _find_header(rows)
            if header is None:
                continue
            header_index, columns = header

            def cell(row: tuple, name: str) -> str:
                i = columns.get(name)
                return _text(row[i]) if i is not None and i < len(row) else ""

            for offset, row in enumerate(rows[header_index + 1 :]):
                row_number = header_index + 2 + offset  # Excel 上の行番号 (1始まり)
                if not cell(row, COL_CATEGORY).startswith(TARGET_CATEGORY):
                    continue
                if cell(row, COL_RUN) not in RUN_MARKS:
                    continue

                number = cell(row, COL_NUMBER)
                theme = cell(row, COL_THEME)
                ambiguity = cell(row, COL_AMBIGUITY)
                content = cell(row, COL_CONTENT)
                name = _folder_name(number, theme, ambiguity)
                if not content:
                    notes.append(f"{ws.title} {row_number}行目 ({name}): 内容が空のため除外しました")
                    continue

                specs.append(
                    SpecSource(
                        name=name,
                        label=f"{path.name} / {ws.title} / {name}",
                        path=path,
                        requirements=content,
                        meta={
                            "excel_file": path.name,
                            "sheet": ws.title,
                            "row": row_number,
                            COL_NUMBER: number,
                            COL_CATEGORY: cell(row, COL_CATEGORY),
                            COL_THEME: theme,
                            COL_AMBIGUITY: ambiguity,
                            COL_AMBIGUITY_DETAIL: cell(row, COL_AMBIGUITY_DETAIL),
                        },
                    )
                )
    finally:
        wb.close()

    seen: dict[str, int] = {}
    for spec in specs:
        seen[spec.name] = seen.get(spec.name, 0) + 1
    for name, count in seen.items():
        if count > 1:
            notes.append(
                f"{path.name}: {name} が {count} 行あります "
                "(上書きはされず、実行日時で別フォルダになります)"
            )
    return specs, notes
