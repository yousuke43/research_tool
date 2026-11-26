"""確認・承認用の共通DT枠組みの保存庫. Qt 非依存.

研究の手順:
    1. 「枠組みの作成のみ」で抽出 -> 共通DT枠組みを作り、ここに保存する (状態: 下書き)
    2. 「共通DT枠組み...」画面で条件項目・条件組合せを確認し、承認する
       (抽出項目を修正したい場合は、修正内容で新しい版を作る。元の版はそのまま残る)
    3. 「承認済みの枠組みで生成」で、承認済みの枠組みを全生成モデルへ渡す

確認時に、論理的に成立しない条件組合せを除外パターン (Y / N / -) で一括除外できる。
除外パターンと理由はレコードの exclusions に保存し、再編集・他の枠組みへの再利用ができる。
下書きの除外はその場で更新し、承認済みの枠組みの除外を変える場合は新しい版を作る
(承認済み・生成に使った内容は書き換えない)。

保存先:
    frameworks/<仕様名>/<framework_id>.json   枠組み本体 + 仕様・抽出の情報 (機械可読)
    frameworks/<仕様名>/<framework_id>.md     確認用 (仕様情報・条件一覧・条件組合せ表)

どの仕様の枠組みかは、仕様名 (source_name)・Excel の元情報 (番号・テーマ・解釈揺れ等)・
要求仕様本文の SHA-256 で対応付ける。要求仕様の本文が変わった場合、古い枠組みは使われない。
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from services import dt_framework
from utils import config_manager

STATUS_DRAFT = "draft"
STATUS_APPROVED = "approved"
STATUS_UNUSABLE = "unusable"  # 条件数が上限超過・抽出結果が不正など (承認できない)
STATUS_LABELS = {STATUS_DRAFT: "下書き (未確認)", STATUS_APPROVED: "承認済み", STATUS_UNUSABLE: "使用不可"}

MANUAL_SOURCE_NAME = "manual"

_lock = threading.Lock()


def frameworks_dir() -> Path:
    return config_manager.FRAMEWORKS_DIR


def requirements_hash(requirements: str) -> str:
    return hashlib.sha256((requirements or "").strip().encode("utf-8")).hexdigest()


@dataclass
class FrameworkRecord:
    framework_id: str
    created_at: str
    # --- どの仕様の枠組みか
    source_name: str
    source_label: str
    source_path: str
    source_meta: dict
    requirements: str
    requirements_sha256: str
    # --- 抽出 (モデル・生出力・トークン数・プロンプトなど)
    extraction: dict
    decision_items: str  # 枠組みの元になった抽出項目 (修正版では修正後の JSON)
    # --- 枠組み
    framework: dict | None = None  # CommonFramework.to_dict()。使用不可なら抽出項目のみ
    framework_error: str = ""
    max_conditions: int = 6
    # --- 確認
    status: str = STATUS_DRAFT
    approved_at: str = ""
    note: str = ""
    edited: bool = False  # 抽出項目を人手で修正した版か
    parent_id: str = ""  # 修正版の場合、元の枠組みの framework_id
    # 除外パターン ([{"pattern": "N Y -", "reason": "...", "conditions": {条件文: 値}}])
    exclusions: list = field(default_factory=list)
    # 抽出項目の修正で、条件が対応付けられず引き継げなかった除外パターンなど
    exclusion_warnings: list = field(default_factory=list)
    tool_version: dict = field(default_factory=dict)
    path: str = ""  # 保存したファイル (保存時に設定。JSON には書かない)

    # ------------------------------------------------------------------
    @property
    def usable(self) -> bool:
        return self.status != STATUS_UNUSABLE and bool(self.framework) and not self.framework_error

    @property
    def condition_count(self) -> int:
        return len((self.framework or {}).get("conditions", []))

    @property
    def rule_count(self) -> int:
        return len((self.framework or {}).get("rules", []))

    @property
    def excluded_count(self) -> int:
        return len((self.framework or {}).get("excluded_rules", []))

    @property
    def total_combinations(self) -> int:
        fw = self.framework or {}
        return int(fw.get("total_combinations") or (2 ** len(fw.get("conditions", [])) if fw.get("conditions") else 0))

    def decision_items_obj(self) -> dt_framework.DecisionItems:
        return dt_framework.parse_decision_items(self.decision_items)

    def exclusion_patterns(self) -> list[dt_framework.ExclusionPattern]:
        n = len(self.decision_items_obj().conditions)
        return [dt_framework.ExclusionPattern.from_dict(e, n) for e in self.exclusions]

    def common_framework(self) -> dt_framework.CommonFramework:
        return dt_framework.CommonFramework.from_dict(self.framework or {})

    def display_name(self) -> str:
        meta = self.source_meta or {}
        if meta.get("番号") or meta.get("テーマ"):
            parts = [str(meta.get("番号", "")), str(meta.get("テーマ", "")), f"揺れ{meta.get('解釈揺れ', '')}"]
            return " ".join(p for p in parts if p.strip())
        return self.source_name or self.source_label

    def source_summary(self) -> dict:
        """results.json の framework_source に記録する内容."""
        return {
            "framework_id": self.framework_id,
            "file": self.path,
            "created_at": self.created_at,
            "status": self.status,
            "approved_at": self.approved_at,
            "note": self.note,
            "edited": self.edited,
            "parent_id": self.parent_id,
            "exclusions": self.exclusions,
            "excluded_count": self.excluded_count,
            "total_combinations": self.total_combinations,
            "requirements_sha256": self.requirements_sha256,
            "extraction_model": self.extraction.get("model", ""),
        }

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("path", None)
        return data

    @classmethod
    def from_dict(cls, data: dict, path: str = "") -> "FrameworkRecord":
        known = {f for f in cls.__dataclass_fields__}  # noqa: SIM118
        record = cls(**{k: v for k, v in data.items() if k in known and k != "path"})
        record.path = path
        return record


# ------------------------------------------------------------------ 作成
def new_framework_id(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return f"{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def build_record(
    *,
    source_name: str,
    source_label: str,
    source_path: str,
    source_meta: dict,
    requirements: str,
    extraction: dict,
    decision_items: str,
    max_conditions: int,
    tool_version: dict | None = None,
    edited: bool = False,
    parent_id: str = "",
    exclusions: list[dict] | None = None,
) -> FrameworkRecord:
    """抽出項目から枠組みを作り、保存前のレコードにする (条件数超過などは使用不可として記録)."""
    record = FrameworkRecord(
        framework_id=new_framework_id(),
        created_at=datetime.now().isoformat(timespec="seconds"),
        source_name=source_name or MANUAL_SOURCE_NAME,
        source_label=source_label or source_name or MANUAL_SOURCE_NAME,
        source_path=source_path,
        source_meta=dict(source_meta or {}),
        requirements=requirements,
        requirements_sha256=requirements_hash(requirements),
        extraction=dict(extraction),
        decision_items=decision_items,
        max_conditions=max_conditions,
        tool_version=dict(tool_version or {}),
        edited=edited,
        parent_id=parent_id,
        exclusions=list(exclusions or []),
    )
    rebuild(record)
    return record


def rebuild(record: FrameworkRecord) -> None:
    """decision_items と exclusions から枠組みを作り直す (状態: 作れれば下書き、作れなければ使用不可)."""
    record.framework_error = ""
    items = None
    try:
        items = dt_framework.parse_decision_items(record.decision_items)
        patterns = [dt_framework.ExclusionPattern.from_dict(e, len(items.conditions)) for e in record.exclusions]
        framework = dt_framework.build_framework(items, record.max_conditions, exclusions=patterns)
        record.framework = framework.to_dict()
        record.exclusions = [p.to_dict(items.conditions) for p in patterns]
        if record.status == STATUS_UNUSABLE:
            record.status = STATUS_DRAFT
    except dt_framework.TooManyConditionsError as exc:
        record.framework = {
            **items.to_dict(),
            "condition_count": exc.condition_count,
            "total_combinations": 2 ** exc.condition_count,
            "remaining_rules": exc.rule_count,
        }
        record.framework_error = str(exc)
        record.status = STATUS_UNUSABLE
    except dt_framework.FrameworkError as exc:
        record.framework = items.to_dict() if items is not None else None
        record.framework_error = str(exc)
        record.status = STATUS_UNUSABLE
    if record.status == STATUS_UNUSABLE:
        record.approved_at = ""


def update_exclusions(
    record: FrameworkRecord, patterns: list[dt_framework.ExclusionPattern], max_conditions: int | None = None
) -> FrameworkRecord:
    """除外パターンを変更して保存し、保存したレコードを返す.

    - 下書き・使用不可: その場で更新する (まだ生成に使われていないため)
    - 承認済み: 元の版は変えず、新しい版 (下書き) を作る
    """
    conditions = record.decision_items_obj().conditions
    exclusions = [p.to_dict(conditions) for p in patterns]
    if record.status == STATUS_APPROVED:
        target = build_record(
            source_name=record.source_name,
            source_label=record.source_label,
            source_path=record.source_path,
            source_meta=record.source_meta,
            requirements=record.requirements,
            extraction=record.extraction,
            decision_items=record.decision_items,
            max_conditions=max_conditions or record.max_conditions,
            tool_version=record.tool_version,
            edited=record.edited,
            parent_id=record.framework_id,
            exclusions=exclusions,
        )
        target.note = record.note
    else:
        target = record
        target.exclusions = exclusions
        if max_conditions:
            target.max_conditions = max_conditions
        rebuild(target)
    save(target)
    return target


def derive_edited(parent: FrameworkRecord, decision_items: str, max_conditions: int | None = None) -> FrameworkRecord:
    """抽出項目を修正した新しい版を作る (元の版は変更しない)."""
    record = build_record(
        source_name=parent.source_name,
        source_label=parent.source_label,
        source_path=parent.source_path,
        source_meta=parent.source_meta,
        requirements=parent.requirements,
        extraction=parent.extraction,
        decision_items=decision_items,
        max_conditions=max_conditions or parent.max_conditions,
        tool_version=parent.tool_version,
        edited=True,
        parent_id=parent.framework_id,
    )
    # 除外パターンは条件文で対応付けて引き継ぐ (対応付けできないものは引き継がず、警告に残す)
    if parent.exclusions:
        try:
            new_conditions = record.decision_items_obj().conditions
        except dt_framework.FrameworkError:
            new_conditions = []
        mapped, dropped = dt_framework.remap_exclusions(parent.exclusions, new_conditions)
        record.exclusions = [p.to_dict(new_conditions) for p in mapped]
        record.exclusion_warnings = [
            f"除外パターン「{d.get('pattern', '')}」({d.get('reason', '')}) は条件が対応しないため引き継いでいません"
            for d in dropped
        ]
        rebuild(record)
    return record


def reusable_exclusion_sources(records: list[FrameworkRecord], conditions: list[str]) -> list[tuple[FrameworkRecord, int]]:
    """除外パターンを再利用できる他の枠組みと、対応付けできるパターン数 (多い順)."""
    result = []
    for r in records:
        if not r.exclusions:
            continue
        mapped, _ = dt_framework.remap_exclusions(r.exclusions, conditions)
        if mapped:
            result.append((r, len(mapped)))
    result.sort(key=lambda x: x[0].created_at, reverse=True)
    result.sort(key=lambda x: -x[1])  # 安定ソート: 対応数が多い順、同数なら新しい順
    return result


# ------------------------------------------------------------- 確認用 Markdown
def review_markdown(record: FrameworkRecord) -> str:
    meta = record.source_meta or {}
    lines = [
        f"# 共通DT枠組み: {record.display_name()}",
        "",
        f"- 状態: **{STATUS_LABELS.get(record.status, record.status)}**"
        + (f" ({record.approved_at})" if record.approved_at else ""),
        f"- framework_id: `{record.framework_id}`" + (f" (修正版。元: `{record.parent_id}`)" if record.edited else ""),
        f"- 仕様: {record.source_label}",
    ]
    for key, label in (("excel_file", "Excel"), ("sheet", "シート"), ("row", "行"), ("番号", "番号"),
                       ("区分", "区分"), ("テーマ", "テーマ"), ("解釈揺れ", "解釈揺れ"), ("揺れの内容", "揺れの内容")):
        if meta.get(key) not in (None, ""):
            lines.append(f"- {label}: {meta[key]}")
    lines += [
        f"- 要求仕様 SHA-256: `{record.requirements_sha256[:16]}…`",
        f"- 抽出モデル: {record.extraction.get('model', '')}",
        f"- 作成日時: {record.created_at}",
    ]
    if record.note:
        lines.append(f"- メモ: {record.note}")
    lines += ["", "## 要求仕様", "", record.requirements.strip(), ""]

    if record.framework_error:
        lines += ["## ⚠ 共通DTを作れません", "", record.framework_error, ""]
    fw = record.framework or {}
    if fw.get("conditions"):
        lines += ["## 条件項目", "", "| ID | 条件 |", "|---|---|"]
        for i, c in enumerate(fw["conditions"], start=1):
            text = c["text"] if isinstance(c, dict) else c
            lines.append(f"| C{i} | {text} |")
        lines.append("")
    if fw.get("actions"):
        lines += ["## 動作項目", "", "| 動作 | type | valueType |", "|---|---|---|"]
        for a in fw["actions"]:
            lines.append(f"| {a.get('name', '')} | {a.get('type', '')} | {a.get('valueType', '')} |")
        lines.append("")
    if record.exclusions:
        lines += ["## 除外パターン", "", "| # | パターン | 理由 |", "|---|---|---|"]
        for i, e in enumerate(record.exclusions, start=1):
            lines.append(f"| {i} | `{e.get('pattern', '')}` | {e.get('reason', '')} |")
        lines.append("")
    for warning in record.exclusion_warnings:
        lines.append(f"- ⚠ {warning}")
    if record.usable:
        cf = record.common_framework()
        lines += [
            f"## 条件組合せ ({len(cf.rules)} ルール"
            + (f" / 全 {cf.total_combinations} 通りから {len(cf.excluded_rules)} 通りを除外" if cf.excluded_rules else "")
            + ")",
            "",
            "生成モデルには、この条件組合せをそのまま渡します (動作部は各モデルが補完)。"
            + ("除外した組合せは渡しません。" if cf.excluded_rules else ""),
            "",
            dt_framework.to_markdown(cf),
        ]
        if cf.excluded_rules:
            limit = 200
            lines += ["", f"## 除外した組合せ ({len(cf.excluded_rules)} 通り。LLM には渡さない)", "",
                      "| ルール | " + " | ".join(f"C{i}" for i in range(1, len(cf.conditions) + 1)) + " | 一致したパターン |",
                      "|---|" + "---|" * (len(cf.conditions) + 1)]
            for r in cf.excluded_rules[:limit]:
                lines.append(f"| {r.rule_id} | " + " | ".join(r.values) + f" | {cf.exclusion_reason(r)} |")
            if len(cf.excluded_rules) > limit:
                lines.append(f"\n(ほか {len(cf.excluded_rules) - limit} 通り。framework.json を参照)")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------- 保存・読み込み
def _record_dir(record: FrameworkRecord) -> Path:
    from utils.file_manager import safe_component

    return frameworks_dir() / safe_component(record.source_name, MANUAL_SOURCE_NAME)


def save(record: FrameworkRecord) -> Path:
    directory = _record_dir(record)
    path = directory / f"{record.framework_id}.json"
    with _lock:
        directory.mkdir(parents=True, exist_ok=True)
        record.path = str(path)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        path.with_suffix(".md").write_text(review_markdown(record), encoding="utf-8")
    return path


def load(path: str | Path) -> FrameworkRecord:
    path = Path(path)
    return FrameworkRecord.from_dict(json.loads(path.read_text(encoding="utf-8")), str(path))


def list_records() -> list[FrameworkRecord]:
    """保存済みの枠組みを新しい順に返す (壊れたファイルは読み飛ばす)."""
    root = frameworks_dir()
    records = []
    if not root.exists():
        return records
    for path in root.glob("*/*.json"):
        try:
            records.append(load(path))
        except (OSError, ValueError, TypeError):
            continue
    records.sort(key=lambda r: r.created_at + r.framework_id, reverse=True)
    return records


def spec_sort_key(record: FrameworkRecord) -> tuple:
    """仕様の番号順に並べるためのキー (番号 → 仕様名。番号が無い仕様は最後)."""
    number = str((record.source_meta or {}).get("番号", "")).strip()
    if not number:
        digits = ""
        for ch in record.source_name or "":
            if not ch.isdigit():
                break
            digits += ch
        number = digits
    try:
        return (0, float(number), record.source_name)
    except ValueError:
        return (1, 0.0, record.source_name or record.source_label)


def set_status(record: FrameworkRecord, status: str) -> None:
    if status == STATUS_APPROVED and not record.usable:
        raise ValueError("この枠組みは使用できない状態のため承認できません。")
    record.status = status
    record.approved_at = datetime.now().isoformat(timespec="seconds") if status == STATUS_APPROVED else ""
    save(record)


def set_note(record: FrameworkRecord, note: str) -> None:
    record.note = note
    save(record)


def delete(record: FrameworkRecord) -> None:
    path = Path(record.path)
    for target in (path, path.with_suffix(".md")):
        try:
            target.unlink()
        except FileNotFoundError:
            pass


def find_approved(source_name: str, requirements: str, records: list[FrameworkRecord] | None = None) -> list[FrameworkRecord]:
    """仕様名と要求仕様本文が一致する承認済み枠組み (新しい順). 生成には先頭を使う."""
    sha = requirements_hash(requirements)
    records = list_records() if records is None else records
    return [
        r for r in records
        if r.source_name == (source_name or MANUAL_SOURCE_NAME)
        and r.requirements_sha256 == sha
        and r.status == STATUS_APPROVED
        and r.usable
    ]
