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

失敗・形式不正のモデルが一部だけの場合は、残りのモデルで比較したうえで
failed_models に記録する。不一致が解釈差異か計算ミスかはここでは判断しない。
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from services.dt_framework import NOT_APPLICABLE, CommonFramework, is_not_applicable

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


@dataclass(frozen=True)
class ActionComparison:
    action: str
    category: str
    values: dict[str, Any]  # model key -> 正規化済みの値


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
            "counts": self.counts(),
            "rules": [
                {
                    "rule_id": r.rule_id,
                    "category": r.category,
                    "actions": {a.action: {"category": a.category, "values": a.values} for a in r.actions},
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
    valid = [f for f in fills if f.ok]
    failed = [f.key for f in fills if not f.ok]
    rules: list[RuleComparison] = []
    for rule in framework.rules:
        action_results = []
        for action in framework.actions:
            raw = [f.raw_values.get(rule.rule_id, {}).get(action.name) for f in valid]
            category = classify_values(raw)
            values = {f.key: f.values.get(rule.rule_id, {}).get(action.name) for f in valid}
            action_results.append(ActionComparison(action.name, category, values))
        category = max((a.category for a in action_results), key=_SEVERITY.__getitem__, default=CATEGORY_FAILED)
        rules.append(RuleComparison(rule.rule_id, rule.values, category, action_results, failed))
    excluded = [
        {"rule_id": r.rule_id, "conditions": r.values, "reason": framework.exclusion_reason(r)}
        for r in framework.excluded_rules
    ]
    return FrameworkComparison(model_keys=model_keys, failed_models=failed, rules=rules, excluded=excluded)


def fills_from_records(results: list[dict]) -> list[ModelFill]:
    """results.json の results (新方式) から ModelFill を作る."""
    fills = []
    for i, r in enumerate(results):
        fills.append(
            ModelFill(
                key=r.get("model_id") or str(i),
                title=r.get("model_name") or r.get("model_id", ""),
                ok=r.get("status") == "ok",
                values=r.get("validated_results") or {},
                raw_values=r.get("raw_results") or {},
                error=r.get("error", ""),
            )
        )
    return fills


__all__ = [
    "CATEGORY_LABELS",
    "NOT_APPLICABLE",
    "ModelFill",
    "FrameworkComparison",
    "RuleComparison",
    "classify_values",
    "compare_fills",
    "fills_from_records",
]
