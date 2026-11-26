"""新方式 (共通DT枠組み) の結果をモデル間で比較する. Qt 非依存.

全モデルの条件部は共通 (同じルールID = 同じ条件組合せ) なので、
同じルールIDの動作結果どうしを比べるだけでよい。

判定区分 (1ルール・1動作ごと。ルール全体は最も重い区分):
    exact                 完全一致     : 返ってきた値がそのまま同じ
    semantic              意味的一致   : 表記は違うが同じ値 (3750 と "3,750"、3750 と 3750.0 など)
    value_mismatch        動作結果の不一致
    applicability_mismatch 適用可否の不一致 : あるモデルは「適用不可」、別のモデルは値を返した
    failed                生成失敗・形式不正 : 比較できる (検証に通った) モデルが2つ未満
    excluded              除外 (LLM未使用) : 除外パターンで除外し、LLM に渡していない組合せ
                          (LLM が「適用不可」と判断した組合せとは区別する)

値を1つも取り出せなかったモデル (API エラー・JSON として読めない等) は比較から外し failed_models に記録する。
形式不正でも値を取り出せたモデル (型が合わない値・一部のルールの欠落など) は、返ってきた値で比較に含め
partial_models に記録する。型が合わない値は type_mismatch に印を付ける。
不一致が解釈差異か計算ミスかはここでは判断しない。
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from services.dt_framework import (
    NOT_APPLICABLE,
    CommonFramework,
    is_not_applicable,
    normalize_action_value,
    validate_fill_response,
)

CATEGORY_EXACT = "exact"
CATEGORY_SEMANTIC = "semantic"
CATEGORY_VALUE_MISMATCH = "value_mismatch"
CATEGORY_APPLICABILITY_MISMATCH = "applicability_mismatch"
CATEGORY_FAILED = "failed"
CATEGORY_EXCLUDED = "excluded"

CATEGORY_LABELS = {
    CATEGORY_EXACT: "完全一致",
    CATEGORY_SEMANTIC: "意味的一致",
    CATEGORY_VALUE_MISMATCH: "動作結果の不一致",
    CATEGORY_APPLICABILITY_MISMATCH: "適用可否の不一致",
    CATEGORY_FAILED: "生成失敗・形式不正",
    CATEGORY_EXCLUDED: "除外 (LLM未使用)",
}
# ルール全体の区分を決めるときの重さ (大きいほど優先)
_SEVERITY = {
    CATEGORY_EXACT: 0,
    CATEGORY_SEMANTIC: 1,
    CATEGORY_VALUE_MISMATCH: 2,
    CATEGORY_APPLICABILITY_MISMATCH: 3,
    CATEGORY_FAILED: 4,
}


@dataclass(frozen=True)
class ModelFill:
    """1モデル分の補完結果 (検証済み)."""

    key: str
    title: str
    ok: bool
    values: dict[str, dict[str, Any]] = field(default_factory=dict)  # 正規化済み
    raw_values: dict[str, dict[str, Any]] = field(default_factory=dict)  # 返ってきたまま
    error: str = ""

    @property
    def comparable(self) -> bool:
        """比較に使える値が1つでもあるか (形式不正でも値を取り出せていれば比較する)."""
        return any(self.values.values())


@dataclass(frozen=True)
class ActionComparison:
    action: str
    category: str
    values: dict[str, Any]  # model key -> 正規化済みの値 (値が無いモデルは含まない)
    type_mismatch: tuple[str, ...] = ()  # 型が合わない値を返したモデル


@dataclass(frozen=True)
class RuleComparison:
    rule_id: str
    conditions: tuple[str, ...]
    category: str
    actions: list[ActionComparison]
    failed_models: list[str]

    @property
    def is_difference(self) -> bool:
        return self.category in (CATEGORY_VALUE_MISMATCH, CATEGORY_APPLICABILITY_MISMATCH)


@dataclass
class FrameworkComparison:
    model_keys: list[str]
    failed_models: list[str]
    rules: list[RuleComparison]
    # 形式不正だが、取り出せた値で比較に含めたモデル
    partial_models: list[str] = field(default_factory=list)
    # 除外した組合せ ({"rule_id", "conditions": (Y/N...), "reason"})。比較対象ではない
    excluded: list[dict] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        result = {c: 0 for c in CATEGORY_LABELS}
        for rule in self.rules:
            result[rule.category] += 1
        result[CATEGORY_EXCLUDED] = len(self.excluded)
        return result

    def to_dict(self) -> dict:
        return {
            "model_keys": self.model_keys,
            "failed_models": self.failed_models,
            "partial_models": self.partial_models,
            "counts": self.counts(),
            "rules": [
                {
                    "rule_id": r.rule_id,
                    "category": r.category,
                    "actions": {
                        a.action: {"category": a.category, "values": a.values, "type_mismatch": list(a.type_mismatch)}
                        for a in r.actions
                    },
                    "failed_models": r.failed_models,
                }
                for r in self.rules
            ],
            "excluded_rules": [{"rule_id": e["rule_id"], "reason": e["reason"]} for e in self.excluded],
        }


def semantic_key(value: Any) -> Any:
    """意味的一致の判定用. 数値は数値として、文字列は全角半角・空白・カンマ・末尾の「円」を無視する."""
    if is_not_applicable(value):
        return ("NA",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("num", float(value))
    text = unicodedata.normalize("NFKC", str(value))
    text = "".join(text.split()).replace(",", "")
    if text.endswith("円"):
        text = text[:-1]
    try:
        return ("num", float(text))
    except ValueError:
        return ("str", text.upper() if text in ("x", "X") else text)


def _exact_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def classify_values(raw_values: list[Any]) -> str:
    """同じルール・同じ動作の、複数モデルの値を区分に分類する."""
    if len(raw_values) < 2:
        return CATEGORY_FAILED
    na_flags = {is_not_applicable(v) for v in raw_values}
    if len(na_flags) > 1:
        return CATEGORY_APPLICABILITY_MISMATCH
    if len({_exact_key(v) for v in raw_values}) == 1:
        return CATEGORY_EXACT
    if len({semantic_key(v) for v in raw_values}) == 1:
        return CATEGORY_SEMANTIC
    return CATEGORY_VALUE_MISMATCH


def compare_fills(framework: CommonFramework, fills: list[ModelFill]) -> FrameworkComparison:
    model_keys = [f.key for f in fills]
    usable = [f for f in fills if f.ok or f.comparable]
    failed = [f.key for f in fills if f not in usable]
    partial = [f.key for f in usable if not f.ok]
    rules: list[RuleComparison] = []
    for rule in framework.rules:
        action_results = []
        missing: set[str] = set()
        for action in framework.actions:
            present = [f for f in usable if action.name in f.values.get(rule.rule_id, {})]
            missing.update(f.key for f in usable if f not in present)
            raw = [
                f.raw_values.get(rule.rule_id, {}).get(action.name, f.values[rule.rule_id][action.name])
                for f in present
            ]
            values = {f.key: f.values[rule.rule_id][action.name] for f in present}
            mismatch = tuple(
                f.key for f, value in zip(present, raw) if normalize_action_value(action, value)[1] is not None
            )
            action_results.append(ActionComparison(action.name, classify_values(raw), values, mismatch))
        category = max((a.category for a in action_results), key=_SEVERITY.__getitem__, default=CATEGORY_FAILED)
        rule_failed = failed + [k for k in model_keys if k in missing]
        rules.append(RuleComparison(rule.rule_id, rule.values, category, action_results, rule_failed))
    excluded = [
        {"rule_id": r.rule_id, "conditions": r.values, "reason": framework.exclusion_reason(r)}
        for r in framework.excluded_rules
    ]
    return FrameworkComparison(
        model_keys=model_keys, failed_models=failed, rules=rules, partial_models=partial, excluded=excluded
    )


def _revalidate(framework: CommonFramework, record: dict) -> tuple[dict, dict] | None:
    """形式不正のモデルの生の応答を検証し直し、取り出せる値を返す (以前の版で保存した結果用)."""
    chunks = [(b.get("raw_response", ""), b.get("rule_ids") or None) for b in record.get("batches") or []]
    if not chunks and record.get("raw_response"):
        chunks = [(record["raw_response"], None)]
    values: dict = {}
    raw: dict = {}
    for text, rule_ids in chunks:
        if not text:
            continue
        report = validate_fill_response(framework, text, rule_ids)
        values.update(report.values)
        raw.update(report.raw_values)
    return (values, raw) if values else None


def fills_from_records(results: list[dict], framework: CommonFramework | None = None) -> list[ModelFill]:
    """results.json の results (新方式) から ModelFill を作る.

    framework を渡すと、形式不正のモデルは生の応答から値を取り出し直す
    (型が合わない値を比較に使わなかった以前の版の結果でも比較できるようにする)。
    """
    fills = []
    for i, r in enumerate(results):
        ok = r.get("status") == "ok"
        values = r.get("validated_results") or {}
        raw_values = r.get("raw_results") or {}
        if not ok and framework is not None:
            redone = _revalidate(framework, r)
            if redone is not None:
                values, raw_values = redone
        fills.append(
            ModelFill(
                key=r.get("model_id") or str(i),
                title=r.get("model_name") or r.get("model_id", ""),
                ok=ok,
                values=values,
                raw_values=raw_values,
                error=r.get("error", ""),
            )
        )
    return fills


def comparison_for_record(record: dict) -> dict:
    """results.json の内容から比較結果 (to_dict の形) を作り直す. 作れなければ保存済みの値を返す."""
    framework_data = record.get("framework")
    if record.get("framework_status") != "ok" or not framework_data or not record.get("results"):
        return record.get("framework_comparison") or {}
    try:
        framework = CommonFramework.from_dict(framework_data)
    except (KeyError, TypeError, ValueError):
        return record.get("framework_comparison") or {}
    return compare_fills(framework, fills_from_records(record["results"], framework)).to_dict()


__all__ = [
    "CATEGORY_LABELS",
    "NOT_APPLICABLE",
    "ModelFill",
    "FrameworkComparison",
    "RuleComparison",
    "classify_values",
    "compare_fills",
    "comparison_for_record",
    "fills_from_records",
]
