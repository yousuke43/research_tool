"""解釈が分かれた箇所に対する「追加記述案」を LLM に作ってもらう処理 (Qt非依存).

流れ:
    差異の指定 (DifferenceSpec)
        - 自動: 比較タブで検出した「同じ条件の組み合わせなのに結果が違う」箇所
                (spec_from_comparison_group)
        - 手動: 任意のモデルの任意のルール列を起点に、利用者が解釈を書き足す
                (spec_from_rule) / 完全な自由入力
      -> 差異の文章 (format_difference)
         例: 条件が「A（成立）、B（不成立）」の時に、結果動作が「料金：4750」と「料金：4250」になる。
      -> 元の文書 + 差異の文章を prompts/additional_description.txt に埋め込んで LLM へ
      -> 返答 (「…」に対応した記述：…) を実行結果フォルダの additional_descriptions/ へ保存
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from models.decision_table import DecisionTable
from services import token_usage
from services.openrouter_client import ChatResult, OpenRouterClient
from services.table_comparison import ComparisonGroup, ComparisonResult
from utils.file_manager import load_prompt, render_prompt, safe_filename

PROMPT_NAME = "additional_description"
SAVE_SUBDIR = "additional_descriptions"

_CONDITION_VALUE_JA = {"Y": "成立", "N": "不成立"}
_ACTION_VALUE_JA = {"X": "実行する", "-": "実行しない"}


# ------------------------------------------------------------------ データ構造

@dataclass
class Interpretation:
    """分かれた解釈の1つ (= ある結果動作)."""

    outcome: str  # 例: "入場料金：4750"
    sources: list[str] = field(default_factory=list)  # この解釈をとったモデル名など (記録用)


@dataclass
class DifferenceSpec:
    """解釈が分かれている箇所 1件."""

    conditions: list[str] = field(default_factory=list)  # 例: ["来場人数が1人である（成立）", ...]
    interpretations: list[Interpretation] = field(default_factory=list)
    origin: str = "manual"  # "auto" (自動検出) / "manual" (手動指定)
    note: str = ""  # どこから作った差異か (例: "組合せ3", "GPT-4o の R2")


# ------------------------------------------------------------- 値の書き方

def describe_condition(label: str, value: str) -> str | None:
    """条件1つを文章用に整形する. 「-」(考慮不要) や空欄は None (文章に含めない)."""
    value = (value or "").strip()
    if not value or value == "-":
        return None
    return f"{label}（{_CONDITION_VALUE_JA.get(value.upper(), value)}）"


def describe_action(label: str, value: str) -> str:
    value = (value or "").strip()
    return f"{label}：{_ACTION_VALUE_JA.get(value.upper(), value) or '（空欄）'}"


def format_difference(spec: DifferenceSpec) -> str:
    """「条件が「」の時に結果動作が「」と「」になる」という文章を作る."""
    condition_text = "、".join(c for c in spec.conditions if c.strip()) or "（条件の指定なし）"
    outcomes = [i.outcome.strip() for i in spec.interpretations if i.outcome.strip()]
    outcome_text = "と".join(f"「{o}」" for o in outcomes) or "「」"
    return f"条件が「{condition_text}」の時に、結果動作が{outcome_text}になる。"


# ---------------------------------------------------------- 差異の組み立て

def spec_from_comparison_group(
    result: ComparisonResult,
    group: ComparisonGroup,
    titles: dict[str, str],
    group_number: int | None = None,
) -> DifferenceSpec:
    """比較タブの1組み合わせから差異を作る (自動検出 / 比較表での選択).

    食い違っている動作があればその動作だけ、無ければ全動作を結果として並べ、
    同じ結果になったモデルを1つの解釈にまとめる。
    """
    conditions = [
        text
        for g in result.condition_groups
        if (text := describe_condition(g.display_label, group.condition_display.get(g.group_id, "")))
    ]
    action_groups = [g for g in result.action_groups if g.group_id in group.conflicting_action_groups]
    if not action_groups:
        action_groups = list(result.action_groups)

    interpretations: dict[str, Interpretation] = {}
    for entry in group.entries:
        outcome = "、".join(
            describe_action(g.display_label, entry.action_values.get(g.group_id, "")) for g in action_groups
        )
        interp = interpretations.setdefault(outcome, Interpretation(outcome=outcome))
        interp.sources.append(f"{titles.get(entry.table_key, entry.table_key)}:{entry.rule_id}")

    label = f"組合せ{group_number}" if group_number is not None else "選択した組み合わせ"
    return DifferenceSpec(
        conditions=conditions,
        interpretations=list(interpretations.values()),
        origin="auto" if group.has_conflict else "manual",
        note=label,
    )


def spec_from_rule(table: DecisionTable, rule_index: int, title: str) -> DifferenceSpec:
    """1モデルの1ルール列から差異のひな形を作る (手動指定用).

    そのルールの結果を解釈1とし、解釈2は利用者が書き込むための空欄にしておく。
    """
    rule_id = table.rule_ids[rule_index]
    conditions = [
        text for row in table.conditions if (text := describe_condition(row.label, row.values[rule_index]))
    ]
    outcome = "、".join(describe_action(row.label, row.values[rule_index]) for row in table.actions)
    return DifferenceSpec(
        conditions=conditions,
        interpretations=[Interpretation(outcome=outcome, sources=[f"{title}:{rule_id}"]), Interpretation(outcome="")],
        origin="manual",
        note=f"{title} の {rule_id}",
    )


# ------------------------------------------------------------ LLM 呼び出し

def request_additional_description(
    client: OpenRouterClient, model_id: str, document: str, difference: str
) -> ChatResult:
    template = load_prompt(PROMPT_NAME)
    prompt = render_prompt(template, document=document, difference=difference)
    return client.chat(model_id, prompt, purpose=token_usage.PURPOSE_ADDITIONAL)


# ------------------------------------------------------------------ 保存

def save_additional_description(
    output_dir: str | Path,
    *,
    model_id: str,
    model_name: str,
    document: str,
    difference: str,
    spec: DifferenceSpec,
    chat_result: ChatResult,
    elapsed_sec: float,
) -> Path:
    """<実行結果フォルダ>/additional_descriptions/ に .md と .json を保存し、.md のパスを返す."""
    save_dir = Path(output_dir) / SAVE_SUBDIR
    save_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    base = f"{now.strftime('%Y%m%d_%H%M%S')}__{safe_filename(model_id)}"
    name = base
    n = 2
    while (save_dir / f"{name}.md").exists():
        name = f"{base}_{n}"
        n += 1

    markdown = (
        f"# 追加記述案\n\n"
        f"- 生成日時: {now.isoformat(timespec='seconds')}\n"
        f"- モデル: {model_name} (`{model_id}`)\n"
        f"- 差異の指定: {'自動検出' if spec.origin == 'auto' else '手動'}"
        f"{f' ({spec.note})' if spec.note else ''}\n\n"
        f"## 解釈が分かれている箇所\n\n{difference}\n\n"
    )
    sourced = [i for i in spec.interpretations if i.sources]
    if sourced:
        markdown += "".join(f"- 「{i.outcome}」: {', '.join(i.sources)}\n" for i in sourced) + "\n"
    markdown += f"## 追加記述案\n\n{chat_result.content}\n"
    (save_dir / f"{name}.md").write_text(markdown, encoding="utf-8")

    record = {
        "created_at": now.isoformat(timespec="seconds"),
        "model_id": model_id,
        "model_name": model_name,
        "elapsed_sec": round(elapsed_sec, 2),
        "prompt_tokens": chat_result.prompt_tokens,
        "completion_tokens": chat_result.completion_tokens,
        "total_tokens": chat_result.total_tokens,
        "difference": difference,
        "difference_spec": asdict(spec),
        "document": document,
        "response": chat_result.content,
    }
    (save_dir / f"{name}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return save_dir / f"{name}.md"
