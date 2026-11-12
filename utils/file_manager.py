"""ファイル入出力.

- prompts/*.txt の読み込みとプレースホルダ置換
- 1回の実験結果を outputs/<日時>/ へ保存
- data/ フォルダの仕様 (テキストファイル / Excel の行) 一覧取得、outputs/ 配下の過去実験の読み込み
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from models.experiment import Experiment
from models.spec_source import SpecSource
from utils.config_manager import DATA_DIR, OUTPUTS_DIR, PROMPTS_DIR


def load_prompt(name: str) -> str:
    """prompts/<name>.txt を読み込む."""
    path = PROMPTS_DIR / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"プロンプトファイルが見つかりません: {path}")
    return path.read_text(encoding="utf-8")


def render_prompt(template: str, **values: str) -> str:
    """テンプレート中の {key} を値で置換する.

    プロンプトに JSON 例などの波括弧が含まれても壊れないよう、
    str.format ではなく単純置換を使う。
    """
    text = template
    for key, value in values.items():
        placeholder = "{" + key + "}"
        if placeholder not in text:
            raise ValueError(
                f"プロンプトに {placeholder} が含まれていません。"
                f"prompts フォルダのテンプレートを確認してください。"
            )
        text = text.replace(placeholder, value)
    return text


def safe_filename(model_id: str) -> str:
    """'openai/gpt-4o' のようなモデルIDをファイル名に使える形へ変換する."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model_id).strip("_") or "model"


_WINDOWS_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_component(name: str, fallback: str = "untitled") -> str:
    """仕様ファイル名などをフォルダ/ファイル名に使える形へ変換する.

    モデルIDと違い日本語のファイル名もそのまま残したいので、
    Windows で使えない記号だけを置換する (safe_filename とは用途が異なる)。
    """
    cleaned = _WINDOWS_INVALID_CHARS.sub("_", name or "").strip(" .")
    return cleaned or fallback


_OUTER_FENCE = re.compile(r"\A\s*```[ \t]*(?:markdown|md)?[ \t]*\n(.*?)\n[ \t]*```\s*\Z", re.DOTALL | re.IGNORECASE)


def unwrap_code_fence(text: str) -> str:
    """出力全体が ```markdown ... ``` で囲まれている場合だけ、その囲みを外す.

    モデルが表をコードブロックに入れて返すとプレビューで表として描画されないため。
    途中にコードブロックを含むだけの出力はそのまま返す。
    """
    match = _OUTER_FENCE.match(text or "")
    return match.group(1) if match else (text or "")


def save_experiment(experiment: Experiment) -> Path:
    """実験結果を保存し、そのフォルダパスを返す.

    - 手動実行 (Requirements 欄への直接入力): outputs/<YYYYMMDD_HHMMSS>/
    - dataフォルダ一括実行 (experiment.source_name あり):
      outputs/<仕様ファイル名>_<YYYYMMDD_HHMMSS>/
      (Excel の行の場合は outputs/<番号>_<テーマ>_変数追加版_<解釈揺れ>_<YYYYMMDD_HHMMSS>/)
    """
    stamp = experiment.started_at.strftime("%Y%m%d_%H%M%S")
    if experiment.source_name:
        folder_base = f"{safe_component(experiment.source_name)}_{stamp}"
    else:
        folder_base = stamp
    out_dir = OUTPUTS_DIR / folder_base
    suffix = 2
    while out_dir.exists():  # 同じ秒に2回実行した場合
        out_dir = OUTPUTS_DIR / f"{folder_base}_{suffix}"
        suffix += 1
    out_dir.mkdir(parents=True)

    (out_dir / "input.txt").write_text(experiment.requirements, encoding="utf-8")

    # ファイル名だけでどの抽出モデルの出力かわかるようにする
    items_name = f"decision_items__{safe_filename(experiment.extraction_model)}.txt"
    (out_dir / items_name).write_text(experiment.decision_items, encoding="utf-8")
    experiment.decision_items_file = items_name

    used_names: set[str] = set()
    for result in experiment.results:
        base = safe_filename(result.model_id)
        name = base
        n = 2
        while name in used_names:
            name = f"{base}_{n}"
            n += 1
        used_names.add(name)

        if result.ok:
            # .md はプレビューで表が見えるよう囲みを外す (生の出力は results.json に残る)
            body = unwrap_code_fence(result.content)
        else:
            body = f"# 生成失敗: {result.model_id}\n\nAPI Error: {result.error}\n"
        (out_dir / f"{name}.md").write_text(body, encoding="utf-8")
        result.output_file = f"{name}.md"

    experiment.output_dir = str(out_dir)
    (out_dir / "results.json").write_text(
        json.dumps(experiment.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return out_dir


def list_spec_files() -> list[Path]:
    """data/ フォルダ直下の仕様テキストファイル (*.txt) を名前順で返す."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(DATA_DIR.glob("*.txt"), key=lambda p: p.name)


def list_spec_sources() -> tuple[list[SpecSource], list[str]]:
    """data/ フォルダの一括実行対象を (対象一覧, 注意メッセージ) で返す.

    *.txt は1ファイル = 1件、*.xlsx は実行対象の1行 = 1件として扱う。
    """
    from utils.excel_specs import load_excel_specs  # excel_specs が本モジュールを import するため

    sources = [SpecSource(name=p.stem, label=p.name, path=p) for p in list_spec_files()]
    notes: list[str] = []
    for path in sorted(DATA_DIR.glob("*.xlsx"), key=lambda p: p.name):
        if path.name.startswith("~$"):  # Excel で開いている間にできる一時ファイル
            continue
        try:
            specs, excel_notes = load_excel_specs(path)
        except Exception as exc:
            notes.append(f"{path.name}: 読み込めませんでした ({exc})")
            continue
        sources.extend(specs)
        notes.extend(excel_notes)
    return sources, notes


def list_output_runs() -> list[dict]:
    """outputs/ 以下の results.json を新しい順に列挙する (履歴一覧表示用)."""
    runs: list[dict] = []
    if not OUTPUTS_DIR.exists():
        return runs
    for results_path in OUTPUTS_DIR.glob("*/results.json"):
        try:
            data = json.loads(results_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        requirements = data.get("requirements") or ""
        runs.append(
            {
                "dir": str(results_path.parent),
                "folder_name": results_path.parent.name,
                "started_at": data.get("started_at", ""),
                "source_name": data.get("source_name", ""),
                "extraction_model_name": data.get("extraction_model_name", ""),
                "requirements_preview": requirements[:60].replace("\n", " "),
                "model_count": len(data.get("results", [])),
            }
        )
    runs.sort(key=lambda r: r["started_at"], reverse=True)
    return runs


def load_experiment_record(output_dir: str | Path) -> dict:
    """指定フォルダの results.json を読み込む (過去実験の再表示用)."""
    path = Path(output_dir) / "results.json"
    return json.loads(path.read_text(encoding="utf-8"))
