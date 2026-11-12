"""複数の DecisionTable を比較するロジック (UI非依存).

用語:
    テーブル (table)    : 1モデル分の DecisionTable
    ラベル (label)      : 条件部・動作部の「記述」セルの文字列
    正規化 (normalize)  : 空白・改行の差異を無視して比較するための前処理
    ラベルグループ       : 「同じ項目」として扱われるラベルの集合。
                          正規化した文字列が完全一致すれば自動で同じグループになる
                          (= 比較ケースの前者)。表記が異なり自動では一致しない場合は、
                          利用者が指定したエイリアスで手動統合する (= 比較ケースの後者)。
    組み合わせ (combination) : 「対応付けられた条件」がとる値の組 (条件部の1ルール分)。
                              全テーブルを横断し、同じ組み合わせを持つルール同士を
                              まとめて動作部の値を比較する。

この層は Qt に依存しない純粋なロジックなので、ユニットテストしやすく、
UI 側 (ui/comparison_view.py) は計算結果を描画するだけにする。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from models.decision_table import (
    ROW_CATEGORY_ACTION,
    ROW_CATEGORY_CONDITION,
    DecisionRow,
    DecisionTable,
)

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """比較用に空白・改行の差異を無視したテキストへ変換する."""
    return _WHITESPACE_RE.sub("", text or "")


# --------------------------------------------------------------------- ラベル

@dataclass(frozen=True)
class LabelKey:
    """特定テーブル内の1ラベル (条件 or 動作の記述) を一意に指す."""

    table_key: str
    category: str
    raw_label: str


@dataclass(frozen=True)
class AliasGroup:
    """利用者が「同じ項目として扱う」と手動指定したラベルの集合 (比較ケースの後者用)."""

    members: frozenset[LabelKey]
    display_label: str


@dataclass
class LabelGroup:
    """同一項目として扱われるラベルの集合 (正規化一致、または手動エイリアスの結果)."""

    group_id: str
    category: str
    display_label: str
    members: dict[str, str] = field(default_factory=dict)  # table_key -> raw_label

    def is_complete(self, table_keys: list[str]) -> bool:
        """全テーブルにこの項目が存在する (= 比較に使える) かどうか."""
        return all(key in self.members for key in table_keys)


@dataclass
class LabelAlignment:
    """全テーブルのラベルをグループ化した結果."""

    table_keys: list[str]
    groups: list[LabelGroup]

    def groups_by_category(self, category: str) -> list[LabelGroup]:
        return [g for g in self.groups if g.category == category]

    def complete_groups(self, category: str) -> list[LabelGroup]:
        return [g for g in self.groups_by_category(category) if g.is_complete(self.table_keys)]

    def incomplete_groups(self, category: str) -> list[LabelGroup]:
        return [g for g in self.groups_by_category(category) if not g.is_complete(self.table_keys)]


class _UnionFind:
    """手動エイリアスによるグループ統合を扱うための単純な Union-Find."""

    def __init__(self) -> None:
        self._parent: dict[LabelKey, LabelKey] = {}

    def add(self, item: LabelKey) -> None:
        self._parent.setdefault(item, item)

    def find(self, item: LabelKey) -> LabelKey:
        self.add(item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: LabelKey, b: LabelKey) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self._parent[root_b] = root_a


def align_labels(
    tables: dict[str, DecisionTable],
    manual_aliases: list[AliasGroup] | tuple[AliasGroup, ...] = (),
) -> LabelAlignment:
    """全テーブルの条件・動作ラベルをグループ化する.

    1. まず正規化後のテキストが完全一致するラベル同士を自動でまとめる (前者のケース)。
    2. 次に manual_aliases で指定されたラベル同士を追加で統合する (後者のケース)。

    tables と manual_aliases だけから毎回ゼロから計算する純粋関数にしてあるので、
    エイリアスを増減させても一貫した結果になる (呼び出し側で状態を気にしなくてよい)。
    """
    table_keys = list(tables.keys())
    union_find = _UnionFind()
    all_keys: list[LabelKey] = []
    first_seen_by_normalized: dict[tuple[str, str], LabelKey] = {}

    for table_key in table_keys:
        for row in tables[table_key].rows:
            label_key = LabelKey(table_key, row.category, row.label)
            union_find.add(label_key)
            all_keys.append(label_key)

            normalized = (row.category, normalize_text(row.label))
            seen = first_seen_by_normalized.get(normalized)
            if seen is None:
                first_seen_by_normalized[normalized] = label_key
            else:
                union_find.union(seen, label_key)

    alias_display_by_root: dict[LabelKey, str] = {}
    for alias in manual_aliases:
        members = list(alias.members)
        if not members:
            continue
        for member in members:
            union_find.add(member)
        for member in members[1:]:
            union_find.union(members[0], member)
        alias_display_by_root[union_find.find(members[0])] = alias.display_label

    groups_by_root: dict[LabelKey, LabelGroup] = {}
    root_order: list[LabelKey] = []
    group_counter = 0

    for label_key in all_keys:
        root = union_find.find(label_key)
        group = groups_by_root.get(root)
        if group is None:
            group_counter += 1
            display_label = alias_display_by_root.get(root, label_key.raw_label)
            group = LabelGroup(
                group_id=f"{label_key.category}-{group_counter}",
                category=label_key.category,
                display_label=display_label,
            )
            groups_by_root[root] = group
            root_order.append(root)
        group.members.setdefault(label_key.table_key, label_key.raw_label)

    return LabelAlignment(table_keys=table_keys, groups=[groups_by_root[root] for root in root_order])


# ----------------------------------------------------------------- 比較本体

@dataclass(frozen=True)
class ComparisonRuleEntry:
    """1テーブルの1ルール列 (= ある条件の組み合わせ1つ) を表す."""

    table_key: str
    rule_id: str
    condition_values: dict[str, str]  # group_id -> 正規化済みの値
    action_values: dict[str, str]  # group_id -> 正規化済みの値


@dataclass(frozen=True)
class ComparisonGroup:
    """同じ条件の組み合わせを持つ、各テーブルのルールをまとめたもの."""

    key: tuple[str, ...]
    condition_display: dict[str, str]  # group_id -> 表示用の値
    entries: list[ComparisonRuleEntry]
    conflicting_action_groups: frozenset[str]  # 動作の値がテーブル間で食い違う group_id

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicting_action_groups)

    def entry_for_table(self, table_key: str) -> ComparisonRuleEntry | None:
        return next((e for e in self.entries if e.table_key == table_key), None)


@dataclass(frozen=True)
class ComparisonResult:
    alignment: LabelAlignment
    condition_groups: list[LabelGroup]  # 比較に使った (= 全テーブルに存在する) 条件
    action_groups: list[LabelGroup]  # 比較に使った (= 全テーブルに存在する) 動作
    groups: list[ComparisonGroup]  # 条件の組み合わせごとの比較結果

    @property
    def conflict_count(self) -> int:
        return sum(1 for g in self.groups if g.has_conflict)


def _cell_value(
    group: LabelGroup, table_key: str, row_by_label: dict[str, DecisionRow], rule_index: int
) -> str:
    """あるテーブルにおける、あるラベルグループ・ルール列のセル値 (正規化済み) を取り出す.

    テーブルがそのグループに属するラベル (行) を持たない場合は空文字を返す。
    """
    raw_label = group.members.get(table_key)
    row = row_by_label.get(raw_label) if raw_label is not None else None
    return normalize_text(row.values[rule_index]) if row is not None else ""


def compare_tables(tables: dict[str, DecisionTable], alignment: LabelAlignment) -> ComparisonResult:
    """ラベルの対応付け結果をもとに、条件の組み合わせ単位で動作の値を比較する."""
    table_keys = alignment.table_keys
    condition_groups = alignment.complete_groups(ROW_CATEGORY_CONDITION)
    action_groups = alignment.complete_groups(ROW_CATEGORY_ACTION)

    entries_by_key: dict[tuple[str, ...], list[ComparisonRuleEntry]] = {}
    key_order: list[tuple[str, ...]] = []

    for table_key in table_keys:
        table = tables.get(table_key)
        if table is None:
            continue
        row_by_label = {row.label: row for row in table.rows}

        for rule_index, rule_id in enumerate(table.rule_ids):
            condition_values = {
                g.group_id: _cell_value(g, table_key, row_by_label, rule_index) for g in condition_groups
            }
            action_values = {
                g.group_id: _cell_value(g, table_key, row_by_label, rule_index) for g in action_groups
            }
            combination_key = tuple(condition_values[g.group_id] for g in condition_groups)

            entry = ComparisonRuleEntry(
                table_key=table_key,
                rule_id=rule_id,
                condition_values=condition_values,
                action_values=action_values,
            )
            if combination_key not in entries_by_key:
                entries_by_key[combination_key] = []
                key_order.append(combination_key)
            entries_by_key[combination_key].append(entry)

    groups: list[ComparisonGroup] = []
    for combination_key in key_order:
        entries = entries_by_key[combination_key]
        conflicting = frozenset(
            g.group_id
            for g in action_groups
            if len({e.action_values[g.group_id] for e in entries}) > 1
        )
        condition_display = {
            g.group_id: next((e.condition_values[g.group_id] for e in entries), "")
            for g in condition_groups
        }
        groups.append(
            ComparisonGroup(
                key=combination_key,
                condition_display=condition_display,
                entries=entries,
                conflicting_action_groups=conflicting,
            )
        )

    return ComparisonResult(
        alignment=alignment,
        condition_groups=condition_groups,
        action_groups=action_groups,
        groups=groups,
    )
