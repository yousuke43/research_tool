"""解析用 CSV の書き出し. Qt 非依存.

results.json (1仕様分) から、Excel / pandas でそのまま集計できる表を作る。
文字コードは Excel で文字化けしないよう UTF-8 (BOM 付き)。

    summary.csv       1行 = 仕様 × 生成モデル。状態・エラー種別・時間・トークン数・不一致件数など
    rule_results.csv  1行 = 仕様 × ルール × 生成モデル × 動作項目 (ロング形式)。
                      新方式では判定区分 (完全一致 / 不一致 など) も入る。
                      除外した組合せ (LLM に渡していない) は model_id 空欄・判定区分 excluded の行として残す

保存先:
    各仕様フォルダ          <仕様フォルダ>/summary.csv, rule_results.csv
    一括実行フォルダ        batch_<日時>/summary.csv, rule_results.csv (その一括実行の全仕様分)
    履歴画面からの書き出し  outputs/exports/export_<日時>/ (表示中の全結果)
"""

from __future__ import annotations

import csv
from pathlib import Path

from models.experiment import METHOD_COMMON_FRAMEWORK
from services.dt_framework import format_value, is_not_applicable
from services.framework_comparison import CATEGORY_EXCLUDED, CATEGORY_LABELS, comparison_for_record

SUMMARY_FILE = "summary.csv"
RULE_RESULTS_FILE = "rule_results.csv"

_META_COLUMNS = ("番号", "区分", "テーマ", "解釈揺れ", "揺れの内容")


def _base_columns(record: dict, run_dir: str = "") -> dict:
    meta = record.get("source_meta") or {}
    fw_source = record.get("framework_source") or {}
    path = Path(run_dir or record.get("output_dir") or "")
    return {
        "batch": path.parent.name if path.parent.name.startswith("batch_") else "",
        "folder": path.name,
        "started_at": record.get("started_at", ""),
        "method": record.get("generation_method") or "conventional",
        "source_name": record.get("source_name", ""),
        **{col: meta.get(col, "") for col in _META_COLUMNS},
        "extraction_model": record.get("extraction_model", ""),
        "framework_id": fw_source.get("framework_id", ""),
        "framework_edited": fw_source.get("edited", "") if fw_source else "",
    }


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


def summary_rows(record: dict, run_dir: str = "") -> list[dict]:
    base = _base_columns(record, run_dir)
    framework = record.get("framework") or {}
    comparison = comparison_for_record(record) if record.get("generation_method") == METHOD_COMMON_FRAMEWORK else {}
    counts = comparison.get("counts") or {}
    partial = set(comparison.get("partial_models") or [])
    rows = []
    results = record.get("results") or []
    if not results:  # 生成していない (条件数超過など) 場合も1行残す
        rows.append({**base, "model_id": "", "status": record.get("framework_status") or "not_generated",
                     "error": record.get("framework_error", "")})
        return rows
    for r in results:
        validated = r.get("validated_results") or {}
        na = sum(1 for acts in validated.values() for v in acts.values() if is_not_applicable(v))
        row = {
            **base,
            "model_id": r.get("model_id", ""),
            "model_name": r.get("model_name", ""),
            "status": r.get("status", ""),
            "error_kind": r.get("error_kind", ""),
            "error": _first_line(r.get("error", "")),
            "elapsed_sec": r.get("elapsed_sec", ""),
            "prompt_tokens": r.get("prompt_tokens", ""),
            "completion_tokens": r.get("completion_tokens", ""),
            "reasoning_tokens": r.get("reasoning_tokens", ""),
            "total_tokens": r.get("total_tokens", ""),
            "reused_from_cache": r.get("reused_from_cache", ""),
            "condition_count": len(framework.get("conditions", [])) if framework else "",
            "rule_count": len(framework.get("rules", [])) if framework else "",
            "total_combinations": framework.get("total_combinations", "") if framework else "",
            "excluded_count": len(framework.get("excluded_rules", [])) if framework else "",
            "exclusion_patterns": " / ".join(
                f"{e.get('pattern', '')} ({e.get('reason', '')})" for e in framework.get("exclusions", [])
            ) if framework else "",
            "validation_errors": len(r.get("validation_errors") or []),
            "validation_warnings": len(r.get("validation_warnings") or []),
            "compared": (r.get("status") == "ok" or r.get("model_id") in partial) if comparison else "",
            "not_applicable_rules": na if validated else "",
        }
        for key, label in CATEGORY_LABELS.items():
            row[f"rules_{key}"] = counts.get(key, "") if comparison else ""
        rows.append(row)
    return rows


def _framework_rule_rows(record: dict, base: dict) -> list[dict]:
    framework = record.get("framework") or {}
    comparison = comparison_for_record(record)
    cond_texts = [c["text"] if isinstance(c, dict) else str(c) for c in framework.get("conditions", [])]
    cond_ids = [f"C{i}" for i in range(1, len(cond_texts) + 1)]
    rule_cmp = {r["rule_id"]: r for r in comparison.get("rules", [])}
    rows = []
    for rule in framework.get("rules", []):
        rid = rule["rule_id"]
        values = [rule["conditions"].get(cid, "") for cid in cond_ids]
        cmp = rule_cmp.get(rid, {})
        for result in record.get("results") or []:
            model_id = result.get("model_id", "")
            validated = {
                name: act.get("values", {}).get(model_id)
                for name, act in (cmp.get("actions") or {}).items()
                if model_id in act.get("values", {})
            } or (result.get("validated_results") or {}).get(rid, {})
            raw = (result.get("raw_results") or {}).get(rid, {})
            for action in framework.get("actions", []):
                name = action.get("name", "")
                act_cmp = (cmp.get("actions") or {}).get(name, {})
                rows.append({
                    **base,
                    "rule_id": rid,
                    "condition_values": "".join(values),
                    "conditions": "; ".join(f"{cid}={v}" for cid, v in zip(cond_ids, values)),
                    "conditions_text": "; ".join(f"{t}={v}" for t, v in zip(cond_texts, values)),
                    "model_id": result.get("model_id", ""),
                    "model_status": result.get("status", ""),
                    "action": name,
                    "value": format_value(validated.get(name)) if name in validated else "",
                    "type_mismatch": model_id in (act_cmp.get("type_mismatch") or []),
                    "raw_value": "" if name not in raw else str(raw.get(name)),
                    "not_applicable": is_not_applicable(validated.get(name)) if name in validated else "",
                    "action_category": act_cmp.get("category", ""),
                    "action_category_label": CATEGORY_LABELS.get(act_cmp.get("category", ""), ""),
                    "rule_category": cmp.get("category", ""),
                    "rule_category_label": CATEGORY_LABELS.get(cmp.get("category", ""), ""),
                    "exclusion_reason": "",
                })
    # 除外した組合せ: LLM には渡していないので値は空。「適用不可」とは別の区分
    for rule in framework.get("excluded_rules", []):
        values = [rule["conditions"].get(cid, "") for cid in cond_ids]
        for action in framework.get("actions", []):
            rows.append({
                **base,
                "rule_id": rule["rule_id"],
                "condition_values": "".join(values),
                "conditions": "; ".join(f"{cid}={v}" for cid, v in zip(cond_ids, values)),
                "conditions_text": "; ".join(f"{t}={v}" for t, v in zip(cond_texts, values)),
                "model_id": "",
                "model_status": "",
                "action": action.get("name", ""),
                "value": "",
                "raw_value": "",
                "not_applicable": "",
                "action_category": CATEGORY_EXCLUDED,
                "action_category_label": CATEGORY_LABELS[CATEGORY_EXCLUDED],
                "rule_category": CATEGORY_EXCLUDED,
                "rule_category_label": CATEGORY_LABELS[CATEGORY_EXCLUDED],
                "exclusion_reason": rule.get("reason", ""),
            })
    rows.sort(key=lambda r: int(r["rule_id"][1:]) if r["rule_id"][1:].isdigit() else 0)
    return rows


def _conventional_rule_rows(record: dict, base: dict) -> list[dict]:
    from services.decision_table_parser import DecisionTableParseError, parse_decision_table

    rows = []
    for result in record.get("results") or []:
        if result.get("status") != "ok":
            continue
        try:
            table = parse_decision_table(result.get("content", ""))
        except DecisionTableParseError:
            continue
        for index, rid in enumerate(table.rule_ids):
            conds = [(row.label, row.values[index]) for row in table.conditions]
            for action in table.actions:
                rows.append({
                    **base,
                    "rule_id": rid,
                    "condition_values": "".join(v for _, v in conds),
                    "conditions": "",
                    "conditions_text": "; ".join(f"{label}={v}" for label, v in conds),
                    "model_id": result.get("model_id", ""),
                    "model_status": "ok",
                    "action": action.label,
                    "value": action.values[index],
                    "raw_value": action.values[index],
                })
    return rows


def rule_rows(record: dict, run_dir: str = "") -> list[dict]:
    base = _base_columns(record, run_dir)
    if record.get("generation_method") == METHOD_COMMON_FRAMEWORK:
        return _framework_rule_rows(record, base)
    return _conventional_rule_rows(record, base)


def write_csv(path: Path, rows: list[dict]) -> None:
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    with Path(path).open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=columns or ["(no data)"])
        writer.writeheader()
        writer.writerows(rows)


def export_records(dest_dir: Path, records: list[tuple[dict, str]]) -> tuple[int, int]:
    """(results.json の内容, フォルダ) のリストを dest_dir の CSV 2つにまとめる. (行数, 行数) を返す."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    summaries, rules = [], []
    for record, run_dir in records:
        summaries.extend(summary_rows(record, run_dir))
        rules.extend(rule_rows(record, run_dir))
    write_csv(dest_dir / SUMMARY_FILE, summaries)
    write_csv(dest_dir / RULE_RESULTS_FILE, rules)
    return len(summaries), len(rules)
