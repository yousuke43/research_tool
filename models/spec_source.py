"""一括実行の入力1件分 (仕様テキストファイル1つ、または Excel の1行)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SpecSource:
    # 出力フォルダ名のベース (outputs/<name>_<日時>/) と results.json の source_name
    name: str
    # 一覧・ログに表示する名前
    label: str
    path: Path
    # Excel 行の場合は読み込み済みの「内容」。テキストファイルの場合は None (実行時に読む)
    requirements: str | None = None
    # Excel 行の元情報 (シート名・行番号・解釈揺れ・揺れの内容など)。results.json に記録する
    meta: dict = field(default_factory=dict)

    def read_requirements(self) -> str:
        if self.requirements is not None:
            return self.requirements.strip()
        return self.path.read_text(encoding="utf-8").strip()
