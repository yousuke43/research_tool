"""使ったトークンの累計 (実行ごとではなく、ツール全体の合計).

outputs/token_usage/
    calls.csv     実際に API を呼んだ1回ごとの記録 (追記のみ)
    summary.md    全体・用途別・モデル別・モデル×用途・日別の集計 (読む用)
    summary.csv   同じ集計 (Excel / pandas 用、ロング形式)

- OpenRouterClient.chat() が応答を受け取るたびに record_call() で記録し、集計を書き直す
- 成功済みの結果を再利用した (キャッシュ) 場合は API を呼んでいないので記録しない
- calls.csv がまだ無いときは、保存済みの結果 (outputs/ の results.json・追加記述案、frameworks/) から
  過去の分を1回だけ取り込む (source = backfill)。同じ抽出を複数の版で共有している場合などは重複を除く
"""

from __future__ import annotations

import csv
import json
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from utils import config_manager, file_manager

SUBDIR = "token_usage"
CALLS_FILE = "calls.csv"
SUMMARY_MD = "summary.md"
SUMMARY_CSV = "summary.csv"

PURPOSE_EXTRACTION = "extraction"
PURPOSE_CONVENTIONAL = "generation_conventional"
PURPOSE_FRAMEWORK = "generation_common_framework"
PURPOSE_ADDITIONAL = "additional_description"
PURPOSE_OTHER = "other"
PURPOSE_LABELS = {
    PURPOSE_EXTRACTION: "抽出",
    PURPOSE_CONVENTIONAL: "生成 (従来方式)",
    PURPOSE_FRAMEWORK: "生成 (新方式: 動作結果補完)",
    PURPOSE_ADDITIONAL: "追加記述案",
    PURPOSE_OTHER: "その他",
}

SOURCE_LIVE = "live"
SOURCE_BACKFILL = "backfill"

COLUMNS = [
    "timestamp", "purpose", "model", "prompt_tokens", "completion_tokens", "reasoning_tokens",
    "total_tokens", "cost", "elapsed_sec", "finish_reason", "status", "source", "note",
]
_NUMBERS = ("prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens")

_lock = threading.Lock()


def usage_dir() -> Path:
    return file_manager.OUTPUTS_DIR / SUBDIR


def calls_path() -> Path:
    return usage_dir() / CALLS_FILE


# ------------------------------------------------------------------ 記録
def record_call(
    *,
    purpose: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    reasoning_tokens: int = 0,
    total_tokens: int = 0,
    cost: float | None = None,
    elapsed_sec: float = 0.0,
    finish_reason: str = "",
    status: str = "ok",
) -> None:
    """API 呼び出し1回分を記録し、集計ファイルを書き直す. 失敗しても例外は出さない (実験を止めない)."""
    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "purpose": purpose or PURPOSE_OTHER,
        "model": model,
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "reasoning_tokens": int(reasoning_tokens or 0),
        "total_tokens": int(total_tokens or 0),
        "cost": "" if cost is None else cost,
        "elapsed_sec": round(float(elapsed_sec or 0.0), 2),
        "finish_reason": finish_reason,
        "status": status,
        "source": SOURCE_LIVE,
        "note": "",
    }
    try:
        with _lock:
            _ensure_ledger()
            _append([row])
            _write_summary_unlocked()
    except OSError:
        pass


def _append(rows: list[dict]) -> None:
    path = calls_path()
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        if new:
            writer.writeheader()
        writer.writerows(rows)


def _ensure_ledger() -> None:
    if calls_path().exists():
        return
    rows = backfill_rows()
    _append(rows)  # 0 件でもヘッダーだけ書く (取り込みは1回だけ)


def load_calls() -> list[dict]:
    path = calls_path()
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ------------------------------------------------------- 過去分の取り込み
def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _load_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _row(timestamp: str, purpose: str, model: str, usage: dict, note: str) -> dict:
    return {
        "timestamp": str(timestamp or "")[:19],
        "purpose": purpose,
        "model": model or "",
        "prompt_tokens": _int(usage.get("prompt_tokens")),
        "completion_tokens": _int(usage.get("completion_tokens")),
        "reasoning_tokens": _int(usage.get("reasoning_tokens")),
        "total_tokens": _int(usage.get("total_tokens")),
        "cost": "",
        "elapsed_sec": usage.get("elapsed_sec", "") or "",
        "finish_reason": usage.get("finish_reason", "") or "",
        "status": "ok",
        "source": SOURCE_BACKFILL,
        "note": note,
    }


def backfill_rows() -> list[dict]:
    """保存済みの結果から、過去に実際に API を呼んだ分を組み立てる.

    - キャッシュ再利用 (reused_from_cache) と、保存枠組みを使った生成の抽出 (枠組み作成時の抽出の写し) は数えない
    - 同じ抽出 (モデル・出力・トークン数が同じ) は1回として数える (枠組みの版ごとに写しがあるため)
    """
    rows: list[dict] = []
    seen_extractions: set[tuple] = set()

    def add_extraction(timestamp: str, model: str, content: str, usage: dict, note: str) -> None:
        if _int(usage.get("total_tokens")) <= 0 and _int(usage.get("prompt_tokens")) <= 0:
            return
        key = (model, content, _int(usage.get("prompt_tokens")), _int(usage.get("completion_tokens")))
        if key in seen_extractions:
            return
        seen_extractions.add(key)
        rows.append(_row(timestamp, PURPOSE_EXTRACTION, model, usage, note))

    outputs = file_manager.OUTPUTS_DIR
    if outputs.exists():
        result_files = list(outputs.glob("*/results.json")) + list(outputs.glob("*/*/results.json"))
        for path in sorted(result_files):
            if SUBDIR in path.parts or "_cache" in path.parts:
                continue
            data = _load_json(path)
            if data is None:
                continue
            note = str(path.parent.relative_to(outputs))
            started = data.get("started_at", "")
            if not data.get("extraction_reused_from_cache") and not data.get("framework_source"):
                add_extraction(
                    started, data.get("extraction_model", ""), data.get("decision_items", ""),
                    {
                        "prompt_tokens": data.get("extraction_prompt_tokens"),
                        "completion_tokens": data.get("extraction_completion_tokens"),
                        "reasoning_tokens": data.get("extraction_reasoning_tokens"),
                        "total_tokens": data.get("extraction_total_tokens"),
                        "elapsed_sec": data.get("extraction_elapsed_sec"),
                    },
                    note,
                )
            method = data.get("generation_method") or "conventional"
            purpose = PURPOSE_FRAMEWORK if method == "common_framework" else PURPOSE_CONVENTIONAL
            for result in data.get("results") or []:
                if not isinstance(result, dict):
                    continue
                model = result.get("model_id", "")
                batches = result.get("batches") or []
                if batches:
                    for batch in batches:
                        if isinstance(batch, dict) and not batch.get("reused_from_cache") and _int(batch.get("total_tokens")) > 0:
                            rows.append(_row(started, purpose, model, batch, note))
                elif not result.get("reused_from_cache") and _int(result.get("total_tokens")) > 0:
                    rows.append(_row(started, purpose, model, result, note))
        for path in sorted(outputs.glob("**/additional_descriptions/*.json")):
            data = _load_json(path)
            if data and _int(data.get("total_tokens")) > 0:
                rows.append(_row(data.get("created_at", ""), PURPOSE_ADDITIONAL, data.get("model_id", ""), data,
                                 str(path.relative_to(outputs))))

    frameworks = config_manager.FRAMEWORKS_DIR
    if frameworks.exists():
        for path in sorted(frameworks.glob("*/*.json")):
            data = _load_json(path)
            extraction = (data or {}).get("extraction") or {}
            if not isinstance(extraction, dict) or extraction.get("reused_from_cache"):
                continue
            add_extraction(
                data.get("created_at", ""), extraction.get("model", ""), extraction.get("content", ""), extraction,
                f"frameworks/{path.parent.name}/{path.name}",
            )
    rows.sort(key=lambda r: r["timestamp"])
    return rows


# ------------------------------------------------------------------ 集計
def _totals() -> dict:
    return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
            "total_tokens": 0, "cost": 0.0, "cost_known": 0}


def _add(bucket: dict, row: dict) -> None:
    bucket["calls"] += 1
    for name in _NUMBERS:
        bucket[name] += _int(row.get(name))
    cost = row.get("cost")
    if cost not in ("", None):
        try:
            bucket["cost"] += float(cost)
            bucket["cost_known"] += 1
        except ValueError:
            pass


def summarize(rows: list[dict]) -> dict:
    overall = _totals()
    by_purpose: dict[str, dict] = defaultdict(_totals)
    by_model: dict[str, dict] = defaultdict(_totals)
    by_model_purpose: dict[tuple[str, str], dict] = defaultdict(_totals)
    by_day: dict[str, dict] = defaultdict(_totals)
    by_source: dict[str, dict] = defaultdict(_totals)
    for row in rows:
        purpose = row.get("purpose") or PURPOSE_OTHER
        model = row.get("model") or "(不明)"
        _add(overall, row)
        _add(by_purpose[purpose], row)
        _add(by_model[model], row)
        _add(by_model_purpose[(model, purpose)], row)
        _add(by_day[(row.get("timestamp") or "")[:10] or "(不明)"], row)
        _add(by_source[row.get("source") or SOURCE_LIVE], row)
    return {
        "overall": overall,
        "by_purpose": dict(by_purpose),
        "by_model": dict(by_model),
        "by_model_purpose": dict(by_model_purpose),
        "by_day": dict(by_day),
        "by_source": dict(by_source),
        "first": min((r.get("timestamp") or "" for r in rows), default=""),
        "last": max((r.get("timestamp") or "" for r in rows), default=""),
    }


def _cost_text(bucket: dict) -> str:
    if not bucket["cost_known"]:
        return "-"
    text = f"${bucket['cost']:.4f}"
    return text if bucket["cost_known"] == bucket["calls"] else f"{text} ({bucket['cost_known']}/{bucket['calls']} 回分)"


def _md_table(title: str, key_headers: list[str], items: list[tuple[list[str], dict]]) -> list[str]:
    headers = key_headers + ["回数", "入力", "出力", "うち推論", "合計", "費用 (OpenRouter)"]
    lines = [f"## {title}", "", "| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for keys, b in items:
        cells = keys + [f"{b['calls']:,}", f"{b['prompt_tokens']:,}", f"{b['completion_tokens']:,}",
                        f"{b['reasoning_tokens']:,}", f"{b['total_tokens']:,}", _cost_text(b)]
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    return lines + [""]


def _by_total(items: dict) -> list:
    return sorted(items.items(), key=lambda kv: -kv[1]["total_tokens"])


def render_markdown(summary: dict) -> str:
    label = lambda p: PURPOSE_LABELS.get(p, p)  # noqa: E731
    source_label = {SOURCE_LIVE: "実行時に記録", SOURCE_BACKFILL: "過去の保存結果から取り込み"}
    lines = [
        "# トークン使用量の累計",
        "",
        f"- 更新: {datetime.now().isoformat(timespec='seconds')}",
        f"- 期間: {summary['first'] or '-'} 〜 {summary['last'] or '-'}",
        "- 実際に API を呼んだ分だけを数えます (成功済みの結果を再利用した分は含みません)。",
        "- 入力 = prompt_tokens、出力 = completion_tokens (推論トークンを含む)、合計 = total_tokens。",
        "- 1回ごとの記録は calls.csv、Excel 用の集計は summary.csv にあります。",
        "",
    ]
    lines += _md_table("全体", ["区分"], [(["合計"], summary["overall"])])
    lines += _md_table("用途別", ["用途"], [([label(k)], v) for k, v in _by_total(summary["by_purpose"])])
    lines += _md_table("モデル別", ["モデル"], [([k], v) for k, v in _by_total(summary["by_model"])])
    lines += _md_table(
        "モデル × 用途", ["モデル", "用途"],
        [([m, label(p)], v) for (m, p), v in sorted(summary["by_model_purpose"].items(), key=lambda kv: (kv[0][0], kv[0][1]))],
    )
    lines += _md_table("日別", ["日付"], [([k], v) for k, v in sorted(summary["by_day"].items())])
    lines += _md_table("記録の出どころ", ["出どころ"], [([source_label.get(k, k)], v) for k, v in _by_total(summary["by_source"])])
    return "\n".join(lines)


def _csv_rows(summary: dict) -> list[dict]:
    rows = []

    def add(kind: str, bucket: dict, purpose: str = "", model: str = "", day: str = "") -> None:
        rows.append({
            "集計区分": kind, "用途": PURPOSE_LABELS.get(purpose, purpose), "モデル": model, "日付": day,
            "回数": bucket["calls"], "入力トークン": bucket["prompt_tokens"], "出力トークン": bucket["completion_tokens"],
            "推論トークン": bucket["reasoning_tokens"], "合計トークン": bucket["total_tokens"],
            "費用USD": round(bucket["cost"], 6) if bucket["cost_known"] else "",
        })

    add("全体", summary["overall"])
    for purpose, b in _by_total(summary["by_purpose"]):
        add("用途別", b, purpose=purpose)
    for model, b in _by_total(summary["by_model"]):
        add("モデル別", b, model=model)
    for (model, purpose), b in sorted(summary["by_model_purpose"].items()):
        add("モデル×用途", b, purpose=purpose, model=model)
    for day, b in sorted(summary["by_day"].items()):
        add("日別", b, day=day)
    return rows


def _write_summary_unlocked() -> Path:
    summary = summarize(load_calls())
    directory = usage_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / SUMMARY_MD).write_text(render_markdown(summary) + "\n", encoding="utf-8")
    rows = _csv_rows(summary)
    with (directory / SUMMARY_CSV).open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return directory / SUMMARY_MD


def write_summary() -> Path:
    """集計ファイルを書き直す (calls.csv が無ければ過去分を取り込んで作る)."""
    with _lock:
        _ensure_ledger()
        return _write_summary_unlocked()


def rebuild_from_saved() -> Path:
    """calls.csv を保存済みの結果から作り直す (古いコードのまま動いていたアプリの実行分を取り込む用).

    実行時の記録 (live) にしか無い情報 (費用・空応答だった呼び出し) は失われるので、
    元の calls.csv は calls_<日時>.bak.csv として残す。
    """
    with _lock:
        path = calls_path()
        if path.exists():
            path.rename(path.with_name(f"calls_{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak.csv"))
        _ensure_ledger()
        return _write_summary_unlocked()


if __name__ == "__main__":
    # python -m services.token_usage            集計だけ作り直す
    # python -m services.token_usage --rebuild  calls.csv を保存済みの結果から作り直す
    import sys

    print(rebuild_from_saved() if "--rebuild" in sys.argv[1:] else write_summary())
