"""実験1回分のデータ構造."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class GenerationResult:
    """1つの生成モデルの結果 (成功でも失敗でも1件作る)."""

    model_id: str
    model_name: str = ""
    content: str = ""
    error: str = ""
    elapsed_sec: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    output_file: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "model_name": self.model_name,
            "status": "ok" if self.ok else "error",
            "elapsed_sec": round(self.elapsed_sec, 2),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "output_file": self.output_file,
            "content": self.content,
            "error": self.error,
        }


@dataclass
class Experiment:
    """Requirements -> Decision Items -> 各モデルの Decision Table."""

    requirements: str
    extraction_model: str
    extraction_model_name: str = ""
    decision_items: str = ""
    decision_items_file: str = ""
    extraction_elapsed_sec: float = 0.0
    extraction_prompt_tokens: int = 0
    extraction_completion_tokens: int = 0
    extraction_total_tokens: int = 0
    results: list[GenerationResult] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None
    output_dir: str = ""
    # dataフォルダ一括実行の場合、元の仕様ファイル名 (拡張子なし) とパス。
    # 手動入力 (Requirements 欄への直接入力) の場合は空文字のまま。
    source_name: str = ""
    source_path: str = ""
    # Excel の行から実行した場合の元情報 (シート名・行番号・番号・テーマ・解釈揺れなど)
    source_meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": self.finished_at.isoformat(timespec="seconds")
            if self.finished_at
            else "",
            "source_name": self.source_name,
            "source_path": self.source_path,
            "source_meta": self.source_meta,
            "requirements": self.requirements,
            "extraction_model": self.extraction_model,
            "extraction_model_name": self.extraction_model_name,
            "extraction_elapsed_sec": round(self.extraction_elapsed_sec, 2),
            "extraction_prompt_tokens": self.extraction_prompt_tokens,
            "extraction_completion_tokens": self.extraction_completion_tokens,
            "extraction_total_tokens": self.extraction_total_tokens,
            "decision_items": self.decision_items,
            "decision_items_file": self.decision_items_file,
            "generation_models": [r.model_id for r in self.results],
            "results": [r.to_dict() for r in self.results],
            "output_dir": self.output_dir,
        }
