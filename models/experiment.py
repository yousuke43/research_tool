"""実験1回分のデータ構造."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

METHOD_CONVENTIONAL = "conventional"
METHOD_COMMON_FRAMEWORK = "common_framework"


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
    # --- 以下は追加項目 (従来方式でも記録する)
    method: str = METHOD_CONVENTIONAL
    # "" (成功) / "api_error" / "key_limit" / "invalid_output" / "skipped"
    error_kind: str = ""
    reasoning_tokens: int = 0
    finish_reason: str = ""
    reused_from_cache: bool = False
    # --- 新方式 (共通DT枠組み) のみ
    raw_response: str = ""  # モデルが返した生の JSON (バッチが複数ある場合は batches を参照)
    batches: list[dict] = field(default_factory=list)
    validated_results: dict = field(default_factory=dict)  # rule_id -> {動作名: 正規化した値}
    raw_results: dict = field(default_factory=dict)  # rule_id -> {動作名: 返ってきた値}
    validation_errors: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict:
        data = {
            "model_id": self.model_id,
            "model_name": self.model_name,
            "method": self.method,
            "status": "ok" if self.ok else "error",
            "error_kind": self.error_kind,
            "elapsed_sec": round(self.elapsed_sec, 2),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "finish_reason": self.finish_reason,
            "reused_from_cache": self.reused_from_cache,
            "output_file": self.output_file,
            "content": self.content,
            "error": self.error,
        }
        if self.method == METHOD_COMMON_FRAMEWORK:
            data.update(
                {
                    "raw_response": self.raw_response,
                    "validated_results": self.validated_results,
                    "raw_results": self.raw_results,
                    "validation_errors": self.validation_errors,
                    "validation_warnings": self.validation_warnings,
                    "batches": self.batches,
                }
            )
        return data


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
    # --- 生成方式と再現用の情報
    generation_method: str = METHOD_CONVENTIONAL
    generation_settings: dict = field(default_factory=dict)  # max_tokens・バッチサイズ・上限条件数など
    prompts: dict = field(default_factory=dict)  # 使用したプロンプトテンプレート (名前 -> 本文)
    extraction_reused_from_cache: bool = False
    extraction_reasoning_tokens: int = 0
    # --- 新方式 (共通DT枠組み) のみ
    framework: dict | None = None  # CommonFramework.to_dict()
    # "ok" / "too_many_conditions" / "invalid_items" / "" (従来方式)
    framework_status: str = ""
    framework_error: str = ""
    framework_comparison: dict | None = None
    # 一括実行の中止理由 (APIキー上限超過など)。空なら中止なし
    aborted_reason: str = ""
    # 保存済み (確認・承認済み) の枠組みを使った場合の出所 (framework_id・ファイル・承認日時など)
    framework_source: dict | None = None
    # 実行したツールのバージョン (git コミット・未コミット変更の有無)
    tool_version: dict = field(default_factory=dict)

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
            "extraction_reasoning_tokens": self.extraction_reasoning_tokens,
            "extraction_reused_from_cache": self.extraction_reused_from_cache,
            "generation_method": self.generation_method,
            "generation_settings": self.generation_settings,
            "prompts": self.prompts,
            "framework_status": self.framework_status,
            "framework_error": self.framework_error,
            "framework": self.framework,
            "framework_comparison": self.framework_comparison,
            "aborted_reason": self.aborted_reason,
            "framework_source": self.framework_source,
            "tool_version": self.tool_version,
            "generation_models": [r.model_id for r in self.results],
            "results": [r.to_dict() for r in self.results],
            "output_dir": self.output_dir,
        }
