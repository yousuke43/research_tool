"""共通DT枠組み (新方式) の中核ロジック. Qt にも API にも依存しない純粋な処理.

新方式の流れ:
    1. 抽出モデルの出力 (Decision Items の JSON) を解析する          -> parse_decision_items()
    2. 条件項目の Y/N 全組合せ (2^n 通り) をプログラムで生成する      -> build_framework()
       - LLM は使わない。論理的に成立しない組合せも残す。'-' による統合はしない。
       - 条件列の順序とルールID (R1, R2, ...) はここで固定する。
    3. 生成モデルに枠組みを渡し、各ルールの動作結果だけを JSON で返させる
       (プロンプト部品: rule_table_markdown(), output_example_json())
    4. 返ってきた JSON を検証する                                     -> validate_fill_response()
    5. 枠組みに動作結果を埋め込み、従来と同じ Markdown DT にする        -> to_markdown()

条件数が多い場合は split_batches() でルールを複数バッチに分けて生成し、
merge_batch_values() で統合する (バッチ間でもルールIDと条件組合せは固定)。

将来、全組合せ以外の組合せ削減方式を追加する場合は COMBINATION_STRATEGIES に
関数を登録し、build_framework(strategy=...) で選べるようにしてある。

条件組合せの一括除外 (ExclusionPattern):
    各条件に Y / N / - (どちらでもよい) を指定したパターンに一致する組合せを、
    LLM に渡す前に除外する (論理的に成立しない組合せを人が確認して取り除く用途)。
    - ルールIDは全組合せでの番号のまま (除外しても R1, R2, R4, R8 … と番号を振り直さない)。
      除外パターンを変えてもIDの意味が変わらないので、版や仕様をまたいで比較できる。
    - 除外した組合せは LLM に一切渡さない。LLM が「適用不可」と判断した組合せとは区別して記録する。
"""

from __future__ import annotations

import itertools
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable

FRAMEWORK_METHOD = "common_dt_framework"
FRAMEWORK_VERSION = "1.0"
STRATEGY_FULL = "full_combination"
STRATEGY_EXCLUSION = "full_combination_with_exclusions"

# 除外の編集のために列挙できる条件数の上限 (2^12 = 4096 通り)。
# LLM に渡すルール数の上限は別 (2^max_conditions。既定 64)。
ENUMERATION_MAX_CONDITIONS = 12
PATTERN_ANY = "-"

NOT_APPLICABLE = "適用不可"
ACTION_DO = "X"
ACTION_DONT = "-"

TYPE_VALUE = "value"
TYPE_ACTION = "action"
VALUE_TYPES = ("int", "float", "string")


class FrameworkError(ValueError):
    """枠組みを作れない (抽出結果が不正など)."""


class TooManyConditionsError(FrameworkError):
    """LLM に渡すルール数が上限 (2^max_conditions) を超えている."""

    def __init__(self, condition_count: int, max_conditions: int, rule_count: int | None = None) -> None:
        total = 2 ** condition_count
        rule_count = total if rule_count is None else rule_count
        if rule_count == total:
            message = (
                f"条件数が {condition_count} 個あり、自動生成の上限 ({max_conditions} 個) を超えています "
                f"({total} 通り)。共通DTの生成は行いません "
                "(上限は「モデル設定...」の生成設定で変更できます。"
                "「共通DT枠組み...」で成立しない組合せを除外して上限以下にすることもできます)。"
            )
        else:
            message = (
                f"除外後も {rule_count} ルール ({total} 通り中) 残っており、上限 ({2 ** max_conditions} ルール = "
                f"条件 {max_conditions} 個分) を超えています。さらに除外するか、上限を変更してください。"
            )
        super().__init__(message)
        self.condition_count = condition_count
        self.max_conditions = max_conditions
        self.rule_count = rule_count


# ------------------------------------------------------------- データ構造

@dataclass(frozen=True)
class ActionItem:
    name: str
    type: str = TYPE_VALUE  # "value" / "action"
    value_type: str = ""  # type="value" のとき "int" / "float" / "string"

    def to_dict(self) -> dict:
        data: dict = {"name": self.name, "type": self.type}
        if self.type == TYPE_VALUE:
            data["valueType"] = self.value_type
        return data


@dataclass(frozen=True)
class DecisionItems:
    conditions: list[str]
    actions: list[ActionItem]

    def to_dict(self) -> dict:
        return {"conditions": list(self.conditions), "actions": [a.to_dict() for a in self.actions]}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


@dataclass(frozen=True)
class FrameworkRule:
    rule_id: str
    values: tuple[str, ...]  # 条件ごとの "Y" / "N" (conditions と同じ順序)


@dataclass(frozen=True)
class ExclusionPattern:
    """除外パターン. values は条件ごとの "Y" / "N" / "-" (conditions と同じ順序)."""

    values: tuple[str, ...]
    reason: str = ""

    @property
    def text(self) -> str:
        return " ".join(self.values)

    def matches(self, rule_values: tuple[str, ...]) -> bool:
        return all(p == PATTERN_ANY or p == v for p, v in zip(self.values, rule_values))

    def to_dict(self, conditions: list[str] | None = None) -> dict:
        data: dict = {"pattern": self.text, "reason": self.reason}
        if conditions is not None:
            # 条件文も残しておく (条件の並びが違う別の枠組みへ再利用するときの対応付けに使う)
            data["conditions"] = {c: v for c, v in zip(conditions, self.values)}
        return data

    @classmethod
    def from_dict(cls, data: dict, condition_count: int) -> "ExclusionPattern":
        return cls(parse_pattern(str(data.get("pattern", "")), condition_count), str(data.get("reason", "")))


@dataclass(frozen=True)
class ExcludedRule:
    rule_id: str
    values: tuple[str, ...]
    pattern_indexes: tuple[int, ...]  # 一致した除外パターンの番号 (0 始まり)


@dataclass
class CommonFramework:
    """全生成モデルに共通して渡す DT の枠組み (条件部のみ確定、動作部は未入力).

    rules は LLM に渡すルール (除外後)。excluded_rules は除外したルール (LLM には渡さない)。
    """

    items: DecisionItems
    rules: list[FrameworkRule]
    strategy: str = STRATEGY_FULL
    method: str = FRAMEWORK_METHOD
    version: str = FRAMEWORK_VERSION
    exclusions: list[ExclusionPattern] = field(default_factory=list)
    excluded_rules: list[ExcludedRule] = field(default_factory=list)

    @property
    def total_combinations(self) -> int:
        return len(self.rules) + len(self.excluded_rules)

    def exclusion_reason(self, rule: ExcludedRule) -> str:
        return " / ".join(
            f"#{i + 1} {self.exclusions[i].text}" + (f" ({self.exclusions[i].reason})" if self.exclusions[i].reason else "")
            for i in rule.pattern_indexes
            if i < len(self.exclusions)
        )

    @property
    def conditions(self) -> list[str]:
        return self.items.conditions

    @property
    def actions(self) -> list[ActionItem]:
        return self.items.actions

    @property
    def rule_ids(self) -> list[str]:
        return [r.rule_id for r in self.rules]

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "version": self.version,
            "strategy": self.strategy,
            "conditions": [
                {"id": f"C{i}", "text": text} for i, text in enumerate(self.conditions, start=1)
            ],
            "actions": [a.to_dict() for a in self.actions],
            "rule_count": len(self.rules),
            "total_combinations": self.total_combinations,
            "excluded_count": len(self.excluded_rules),
            "rules": [
                {"rule_id": r.rule_id, "conditions": dict(zip(self._condition_ids(), r.values))}
                for r in self.rules
            ],
            "exclusions": [e.to_dict(self.conditions) for e in self.exclusions],
            "excluded_rules": [
                {
                    "rule_id": r.rule_id,
                    "conditions": dict(zip(self._condition_ids(), r.values)),
                    "patterns": [i + 1 for i in r.pattern_indexes],
                    "reason": self.exclusion_reason(r),
                }
                for r in self.excluded_rules
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CommonFramework":
        conditions = [c["text"] if isinstance(c, dict) else str(c) for c in data.get("conditions", [])]
        actions = [_action_from_obj(a) for a in data.get("actions", [])]
        ids = [f"C{i}" for i in range(1, len(conditions) + 1)]
        rules = [
            FrameworkRule(r["rule_id"], tuple(r["conditions"][cid] for cid in ids))
            for r in data.get("rules", [])
        ]
        exclusions = [ExclusionPattern.from_dict(e, len(conditions)) for e in data.get("exclusions", [])]
        excluded = [
            ExcludedRule(
                r["rule_id"],
                tuple(r["conditions"][cid] for cid in ids),
                tuple(int(i) - 1 for i in r.get("patterns", [])),
            )
            for r in data.get("excluded_rules", [])
        ]
        return cls(
            items=DecisionItems(conditions, actions),
            rules=rules,
            strategy=data.get("strategy", STRATEGY_FULL),
            method=data.get("method", FRAMEWORK_METHOD),
            version=data.get("version", FRAMEWORK_VERSION),
            exclusions=exclusions,
            excluded_rules=excluded,
        )

    def _condition_ids(self) -> list[str]:
        return [f"C{i}" for i in range(1, len(self.conditions) + 1)]


# ------------------------------------------------------- 抽出結果の解析

_FENCE_RE = re.compile(r"```[a-zA-Z]*\s*\n(.*?)\n\s*```", re.DOTALL)


def extract_json_text(text: str) -> str:
    """LLM 出力から JSON 部分を取り出す (```json 囲み・前後の説明文があっても可)."""
    text = (text or "").strip()
    for block in _FENCE_RE.findall(text):
        block = block.strip()
        if block.startswith("{") or block.startswith("["):
            return block
    if text.startswith("{") or text.startswith("["):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def load_json(text: str) -> Any:
    """extract_json_text() + json.loads(). 失敗時は ValueError (メッセージ付き)."""
    candidate = extract_json_text(text)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON の解析に失敗しました: {exc}") from exc


def _action_from_obj(obj: Any) -> ActionItem:
    if isinstance(obj, str):
        return ActionItem(name=obj.strip(), type=TYPE_ACTION)
    if not isinstance(obj, dict):
        raise FrameworkError(f"動作項目の形式が不正です: {obj!r}")
    name = str(obj.get("name") or "").strip()
    if not name:
        raise FrameworkError(f"動作項目に name がありません: {obj!r}")
    kind = str(obj.get("type") or TYPE_VALUE).strip()
    if kind not in (TYPE_VALUE, TYPE_ACTION):
        raise FrameworkError(f"動作項目「{name}」の type が不正です: {kind!r}")
    value_type = ""
    if kind == TYPE_VALUE:
        value_type = str(obj.get("valueType") or "string").strip()
        if value_type not in VALUE_TYPES:
            raise FrameworkError(f"動作項目「{name}」の valueType が不正です: {value_type!r}")
    return ActionItem(name=name, type=kind, value_type=value_type)


def parse_decision_items(text: str) -> DecisionItems:
    """抽出モデルの出力 (prompts/extract.txt の JSON) を DecisionItems にする."""
    try:
        data = load_json(text)
    except ValueError as exc:
        raise FrameworkError(f"抽出結果 (Decision Items) を JSON として読めません: {exc}") from exc
    if not isinstance(data, dict):
        raise FrameworkError("抽出結果の JSON がオブジェクトではありません。")

    raw_conditions = data.get("conditions")
    if not isinstance(raw_conditions, list):
        raise FrameworkError("抽出結果に conditions (配列) がありません。")
    conditions: list[str] = []
    for c in raw_conditions:
        if isinstance(c, dict):
            c = c.get("name") or c.get("text") or c.get("description") or ""
        c = str(c).strip()
        if c:
            conditions.append(c)

    raw_actions = data.get("actions")
    if not isinstance(raw_actions, list):
        raise FrameworkError("抽出結果に actions (配列) がありません。")
    actions = [_action_from_obj(a) for a in raw_actions]

    if not conditions:
        raise FrameworkError("条件項目が1つも抽出されていません。")
    if not actions:
        raise FrameworkError("動作項目が1つも抽出されていません。")
    names = [a.name for a in actions]
    duplicated = sorted({n for n in names if names.count(n) > 1})
    if duplicated:
        raise FrameworkError(f"動作項目名が重複しています: {', '.join(duplicated)}")
    return DecisionItems(conditions=conditions, actions=actions)


# ------------------------------------------------------ 条件組合せの生成

def full_combinations(condition_count: int) -> list[tuple[str, ...]]:
    """Y/N の全組合せ. R1 = 全て Y、最後が全て N (例の表と同じ並び)."""
    return list(itertools.product(("Y", "N"), repeat=condition_count))


# 組合せ生成方式の登録先 (将来の削減方式はここに追加する)
COMBINATION_STRATEGIES: dict[str, Callable[[int], list[tuple[str, ...]]]] = {
    STRATEGY_FULL: full_combinations,
}


def all_rules(condition_count: int, strategy: str = STRATEGY_FULL) -> list[FrameworkRule]:
    """除外前の全ルール (ルールIDはここで確定する)."""
    if condition_count > ENUMERATION_MAX_CONDITIONS:
        raise FrameworkError(
            f"条件数が {condition_count} 個あり、組合せ ({2 ** condition_count} 通り) を列挙できる上限 "
            f"({ENUMERATION_MAX_CONDITIONS} 個) を超えています。抽出項目を見直してください。"
        )
    generator = COMBINATION_STRATEGIES.get(strategy)
    if generator is None:
        raise FrameworkError(f"未対応の組合せ生成方式です: {strategy}")
    return [FrameworkRule(f"R{i}", tuple(values)) for i, values in enumerate(generator(condition_count), start=1)]


def parse_pattern(text: str, condition_count: int) -> tuple[str, ...]:
    """'N Y -' / 'NY-' / 'N,Y,-' などを ('N', 'Y', '-') にする. 不正なら FrameworkError."""
    normalized = unicodedata.normalize("NFKC", text or "").upper()
    chars = [c for c in normalized if not c.isspace() and c not in ",、|/"]
    chars = [PATTERN_ANY if c in ("-", "ー", "−", "―", "*", "?") else c for c in chars]
    if len(chars) != condition_count:
        raise FrameworkError(f"除外パターン「{text}」の長さが条件数 ({condition_count}) と一致しません。")
    bad = sorted({c for c in chars if c not in ("Y", "N", PATTERN_ANY)})
    if bad:
        raise FrameworkError(f"除外パターン「{text}」に使えない文字があります: {' '.join(bad)} (Y / N / - のみ)")
    if all(c == PATTERN_ANY for c in chars):
        raise FrameworkError("すべてが「-」のパターンは全組合せを除外してしまうため使えません。")
    return tuple(chars)


def apply_exclusions(
    rules: list[FrameworkRule], patterns: list[ExclusionPattern]
) -> tuple[list[FrameworkRule], list[ExcludedRule]]:
    """パターンに1つでも一致したルールを除外する. (残すルール, 除外したルール) を返す."""
    kept: list[FrameworkRule] = []
    excluded: list[ExcludedRule] = []
    for rule in rules:
        hits = tuple(i for i, p in enumerate(patterns) if p.matches(rule.values))
        if hits:
            excluded.append(ExcludedRule(rule.rule_id, rule.values, hits))
        else:
            kept.append(rule)
    return kept, excluded


def pattern_match_counts(rules: list[FrameworkRule], patterns: list[ExclusionPattern]) -> list[int]:
    """各パターンに一致する組合せ数 (他のパターンと重複していても数える)."""
    return [sum(1 for r in rules if p.matches(r.values)) for p in patterns]


def remap_exclusions(
    patterns: list[dict], new_conditions: list[str]
) -> tuple[list[ExclusionPattern], list[dict]]:
    """保存済みの除外パターン (条件文つき) を、別の条件リストへ対応付ける.

    パターン中で Y / N を指定している条件が新しい条件リストにすべて存在する場合だけ移す
    (新しい条件側にしかない条件は「-」)。対応付けできないパターンは除いて返す。
    """
    mapped: list[ExclusionPattern] = []
    dropped: list[dict] = []
    for data in patterns:
        by_text = data.get("conditions") or {}
        specified = {c: v for c, v in by_text.items() if v != PATTERN_ANY}
        if not by_text or any(c not in new_conditions for c in specified):
            dropped.append(data)
            continue
        values = tuple(specified.get(c, PATTERN_ANY) for c in new_conditions)
        if all(v == PATTERN_ANY for v in values):
            dropped.append(data)
            continue
        mapped.append(ExclusionPattern(values, str(data.get("reason", ""))))
    return mapped, dropped


def build_framework(
    items: DecisionItems,
    max_conditions: int = 6,
    strategy: str = STRATEGY_FULL,
    exclusions: list[ExclusionPattern] | None = None,
) -> CommonFramework:
    """抽出結果から共通DTの枠組みを作る.

    除外パターンがあれば一致する組合せを除く。LLM に渡すルール数が 2^max_conditions を
    超える場合は TooManyConditionsError (除外なしなら、条件数が max_conditions を超える場合と同じ)。
    """
    n = len(items.conditions)
    exclusions = list(exclusions or [])
    if not exclusions and n > max_conditions:
        raise TooManyConditionsError(n, max_conditions)
    for pattern in exclusions:
        if len(pattern.values) != n:
            raise FrameworkError(f"除外パターン「{pattern.text}」の長さが条件数 ({n}) と一致しません。")
    rules, excluded = apply_exclusions(all_rules(n, strategy), exclusions)
    if not rules:
        raise FrameworkError("除外パターンによって全ての組合せが除外されています。")
    if len(rules) > 2 ** max_conditions:
        raise TooManyConditionsError(n, max_conditions, len(rules))
    return CommonFramework(
        items=items,
        rules=rules,
        strategy=STRATEGY_EXCLUSION if exclusions else strategy,
        exclusions=exclusions,
        excluded_rules=excluded,
    )


def split_batches(rules: list[FrameworkRule], batch_size: int) -> list[list[FrameworkRule]]:
    """ルールを batch_size 件ずつに分ける (順序・ルールIDはそのまま)."""
    size = max(1, int(batch_size))
    return [rules[i : i + size] for i in range(0, len(rules), size)]


# --------------------------------------------------------- プロンプト部品

def condition_legend_markdown(framework: CommonFramework) -> str:
    lines = ["| ID | 条件 |", "|---|---|"]
    lines += [f"| C{i} | {_cell(text)} |" for i, text in enumerate(framework.conditions, start=1)]
    return "\n".join(lines)


def rule_table_markdown(framework: CommonFramework, rules: list[FrameworkRule] | None = None) -> str:
    """生成モデルに渡す条件組合せの表 (1行 = 1ルール)."""
    rules = framework.rules if rules is None else rules
    ids = [f"C{i}" for i in range(1, len(framework.conditions) + 1)]
    lines = [
        "条件の一覧:",
        "",
        condition_legend_markdown(framework),
        "",
        _rules_heading(rules),
        "",
        "| ルール | " + " | ".join(ids) + " |",
        "|---|" + "---|" * len(ids),
    ]
    lines += [f"| {r.rule_id} | " + " | ".join(r.values) + " |" for r in rules]
    return "\n".join(lines)


def _is_contiguous(rules: list[FrameworkRule]) -> bool:
    try:
        numbers = [int(r.rule_id[1:]) for r in rules]
    except ValueError:
        return False
    return numbers == list(range(numbers[0], numbers[0] + len(numbers))) if numbers else True


def _rules_heading(rules: list[FrameworkRule]) -> str:
    if not rules:
        return "判断するルール: なし"
    if _is_contiguous(rules):
        return f"判断するルール ({len(rules)} 件: {rules[0].rule_id} 〜 {rules[-1].rule_id}):"
    return f"判断するルール ({len(rules)} 件。ルールIDは連番ではありません。表にあるルールIDだけを出力してください):"


def _example_value(action: ActionItem) -> Any:
    if action.type == TYPE_ACTION:
        return f"{ACTION_DO} または {ACTION_DONT}"
    return {"int": "<整数>", "float": "<数値>", "string": "<文字列>"}[action.value_type]


def output_example_json(framework: CommonFramework, rules: list[FrameworkRule] | None = None) -> str:
    rules = framework.rules if rules is None else rules
    sample_ids = [r.rule_id for r in rules[:2]] or ["R1"]
    example = {
        "results": [
            {"rule_id": rid, "actions": {a.name: _example_value(a) for a in framework.actions}}
            for rid in sample_ids
        ]
    }
    text = json.dumps(example, ensure_ascii=False, indent=2)
    # 数値の項目は引用符なしで示す (文字列で返されるのを防ぐ)
    text = text.replace('"<整数>"', "<整数>").replace('"<数値>"', "<数値>")
    text = text.replace(f'"{ACTION_DO} または {ACTION_DONT}"', f'"{ACTION_DO}" または "{ACTION_DONT}"')
    if len(rules) > 2:
        if _is_contiguous(rules):
            text += f"\n(このように {rules[0].rule_id} 〜 {rules[-1].rule_id} の {len(rules)} 件すべてを出力する)"
        else:
            text += f"\n(このように、表にある {len(rules)} 件のルールIDすべてについて出力する)"
    return text


def estimate_output_tokens(framework: CommonFramework, rules: list[FrameworkRule] | None = None) -> int:
    """回答 JSON 本体のおおよそのトークン数 (推論トークンは含まない。設定の目安用)."""
    rules = framework.rules if rules is None else rules
    per_action = sum(len(a.name) + 12 for a in framework.actions)
    return int(math.ceil(len(rules) * (20 + per_action) * 1.2)) + 50


# ------------------------------------------------------------ JSON 検証

@dataclass
class ValidationReport:
    values: dict[str, dict[str, Any]] = field(default_factory=dict)  # rule_id -> {動作名: 正規化した値}
    raw_values: dict[str, dict[str, Any]] = field(default_factory=dict)  # rule_id -> {動作名: 返ってきた値そのまま}
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


_INT_STR_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_STR_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")


def _nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip()


def is_not_applicable(value: Any) -> bool:
    return isinstance(value, str) and _nfkc(value) == NOT_APPLICABLE


def normalize_action_value(action: ActionItem, value: Any) -> tuple[Any, str | None, str | None]:
    """(正規化した値, エラー, 警告) を返す. エラーがあれば値は使わない."""
    if value is None:
        return None, "値が null です", None
    if is_not_applicable(value):
        return NOT_APPLICABLE, None, None

    if action.type == TYPE_ACTION:
        if isinstance(value, bool):
            return (ACTION_DO if value else ACTION_DONT), None, f"真偽値 {value} を {'X' if value else '-'} として扱いました"
        if isinstance(value, str):
            text = _nfkc(value)
            if text.upper() == ACTION_DO:
                return ACTION_DO, None, None
            if text in ("-", "ー", "−", "―", "‐"):
                return ACTION_DONT, None, None
        return None, f"action 型には X / - / {NOT_APPLICABLE} のみ使えます (値: {value!r})", None

    vt = action.value_type
    if isinstance(value, bool):
        return None, f"{vt} 型に真偽値が返されました (値: {value!r})", None
    if vt == "int":
        if isinstance(value, int):
            return value, None, None
        if isinstance(value, float):
            if value.is_integer():
                return int(value), None, None
            return None, f"int 型に小数が返されました (値: {value!r})", None
        if isinstance(value, str):
            text = _nfkc(value).replace(",", "")
            if _INT_STR_RE.match(text):
                return int(text), None, f"文字列 {value!r} を整数に変換しました"
        return None, f"int 型ではありません (値: {value!r})", None
    if vt == "float":
        if isinstance(value, (int, float)):
            return value, None, None
        if isinstance(value, str):
            text = _nfkc(value).replace(",", "")
            if _FLOAT_STR_RE.match(text):
                number = float(text)
                return number, None, f"文字列 {value!r} を数値に変換しました"
        return None, f"float 型ではありません (値: {value!r})", None
    # string
    if isinstance(value, str):
        if not value.strip():
            return None, "空文字が返されました", None
        return value.strip(), None, None
    if isinstance(value, (int, float)):
        return str(value), None, f"数値 {value!r} を文字列として扱いました"
    return None, f"string 型ではありません (値: {value!r})", None


def validate_fill_data(
    framework: CommonFramework, data: Any, expected_rule_ids: list[str] | None = None
) -> ValidationReport:
    """生成モデルの JSON (解析済み) を検証し、正規化した動作結果を返す."""
    report = ValidationReport()
    expected = list(expected_rule_ids if expected_rule_ids is not None else framework.rule_ids)
    expected_set = set(expected)
    excluded_ids = {r.rule_id for r in framework.excluded_rules}
    action_by_name = {a.name: a for a in framework.actions}

    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        report.errors.append('JSON に "results" 配列がありません。')
        return report

    seen: set[str] = set()
    for index, entry in enumerate(data["results"]):
        if not isinstance(entry, dict):
            report.errors.append(f"results[{index}] がオブジェクトではありません。")
            continue
        rule_id = str(entry.get("rule_id") or "").strip()
        if not rule_id:
            report.errors.append(f"results[{index}] に rule_id がありません。")
            continue
        if rule_id in seen:
            report.errors.append(f"ルールID {rule_id} が重複しています。")
            continue
        seen.add(rule_id)
        if rule_id not in expected_set:
            if rule_id in excluded_ids:
                report.errors.append(f"除外したルールID {rule_id} が含まれています (渡していないルール)。")
            else:
                report.errors.append(f"未定義のルールID {rule_id} が含まれています。")
            continue

        actions = entry.get("actions")
        if not isinstance(actions, dict):
            report.errors.append(f"{rule_id}: actions がオブジェクトではありません。")
            continue
        unknown = [name for name in actions if name not in action_by_name]
        if unknown:
            report.errors.append(f"{rule_id}: 未定義の動作項目があります: {', '.join(map(str, unknown))}")
        normalized: dict[str, Any] = {}
        raw: dict[str, Any] = {}
        for name, action in action_by_name.items():
            if name not in actions:
                report.errors.append(f"{rule_id}: 動作項目「{name}」がありません。")
                continue
            value, error, warning = normalize_action_value(action, actions[name])
            if error:
                report.errors.append(f"{rule_id}「{name}」: {error}")
                continue
            if warning:
                report.warnings.append(f"{rule_id}「{name}」: {warning}")
            normalized[name] = value
            raw[name] = actions[name]
        report.values[rule_id] = normalized
        report.raw_values[rule_id] = raw

    missing = [rid for rid in expected if rid not in seen]
    if missing:
        shown = ", ".join(missing[:20]) + (" ..." if len(missing) > 20 else "")
        report.errors.append(f"ルールID が不足しています ({len(missing)} 件): {shown}")
    return report


def validate_fill_response(
    framework: CommonFramework, text: str, expected_rule_ids: list[str] | None = None
) -> ValidationReport:
    """生成モデルの生の応答テキストを解析・検証する."""
    try:
        data = load_json(text)
    except ValueError as exc:
        return ValidationReport(errors=[str(exc)])
    return validate_fill_data(framework, data, expected_rule_ids)


def merge_batch_values(reports: list[ValidationReport]) -> tuple[dict, dict]:
    """バッチごとの検証結果を1つにまとめる (values, raw_values)."""
    values: dict[str, dict] = {}
    raw: dict[str, dict] = {}
    for report in reports:
        values.update(report.values)
        raw.update(report.raw_values)
    return values, raw


# ------------------------------------------------------ Markdown DT の完成

def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def to_markdown(framework: CommonFramework, values: dict[str, dict[str, Any]] | None = None) -> str:
    """枠組みに動作結果を埋め込んだ Markdown DT (従来方式と同じ形式).

    values が None のときは動作部を空欄にした枠組みだけを出力する。
    """
    values = values or {}
    rule_ids = framework.rule_ids
    lines = [
        "| 区分 | 記述 | " + " | ".join(rule_ids) + " |",
        "|---|---|" + "---|" * len(rule_ids),
    ]
    for ci, condition in enumerate(framework.conditions):
        cells = [r.values[ci] for r in framework.rules]
        lines.append(f"| 条件部 | {_cell(condition)} | " + " | ".join(cells) + " |")
    for action in framework.actions:
        cells = [_cell(format_value(values.get(rid, {}).get(action.name))) for rid in rule_ids]
        lines.append(f"| 動作部 | {_cell(action.name)} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
