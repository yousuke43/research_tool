"""条件組合せの一括除外のテスト. API は使わない."""

from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import test_common_framework as tcf  # noqa: E402  (QApplication もここで作られる)

from PySide6.QtCore import QCoreApplication  # noqa: E402

from models.experiment import METHOD_COMMON_FRAMEWORK  # noqa: E402
from models.spec_source import SpecSource  # noqa: E402
from services import dt_framework as d  # noqa: E402
from services import framework_store, pipeline  # noqa: E402
from services.batch_runner import MODE_FRAMEWORK_ONLY, MODE_USE_SAVED, BatchWorker  # noqa: E402
from utils import config_manager, file_manager  # noqa: E402

VISITS = ["来場回数3回以上", "来場回数5回以上", "来場回数10回以上"]
VISITS_JSON = json.dumps(
    {"conditions": VISITS, "actions": [{"name": "最終料金", "type": "value", "valueType": "int"}]}, ensure_ascii=False
)
SPEC_PATTERNS = [
    d.ExclusionPattern(("N", "Y", "-"), "5回以上なら必ず3回以上"),
    d.ExclusionPattern(("-", "N", "Y"), "10回以上なら必ず5回以上"),
]


def items():
    return d.parse_decision_items(VISITS_JSON)


class TestPatternLogic(unittest.TestCase):
    def test_spec_example_8_to_4(self):
        fw = d.build_framework(items(), exclusions=SPEC_PATTERNS)
        self.assertEqual(fw.total_combinations, 8)
        self.assertEqual([(r.rule_id, "".join(r.values)) for r in fw.rules],
                         [("R1", "YYY"), ("R2", "YYN"), ("R4", "YNN"), ("R8", "NNN")])
        self.assertEqual([r.rule_id for r in fw.excluded_rules], ["R3", "R5", "R6", "R7"])
        self.assertIn("10回以上なら必ず5回以上", fw.exclusion_reason(fw.excluded_rules[0]))  # R3 = Y N Y
        self.assertEqual(fw.strategy, d.STRATEGY_EXCLUSION)
        # 保存・復元してもルールIDと除外理由が保たれる
        again = d.CommonFramework.from_dict(json.loads(json.dumps(fw.to_dict(), ensure_ascii=False)))
        self.assertEqual(again.rules, fw.rules)
        self.assertEqual(again.excluded_rules, fw.excluded_rules)
        self.assertEqual(again.exclusions, fw.exclusions)
        # 完成DT (Markdown) には残したルールだけ
        md = d.to_markdown(fw)
        self.assertIn("| R1 | R2 | R4 | R8 |", md)
        self.assertNotIn("R3", md)

    def test_no_exclusion_is_unchanged(self):
        fw = d.build_framework(items())
        self.assertEqual(len(fw.rules), 8)
        self.assertEqual(fw.excluded_rules, [])
        self.assertEqual(fw.strategy, d.STRATEGY_FULL)

    def test_parse_pattern(self):
        self.assertEqual(d.parse_pattern("N Y -", 3), ("N", "Y", "-"))
        self.assertEqual(d.parse_pattern("ny-", 3), ("N", "Y", "-"))
        self.assertEqual(d.parse_pattern("Ｎ,Ｙ,－", 3), ("N", "Y", "-"))
        for bad in ("N Y", "N Y - -", "N X -", "- - -"):
            with self.assertRaises(d.FrameworkError):
                d.parse_pattern(bad, 3)

    def test_overlapping_patterns_and_counts(self):
        rules = d.all_rules(3)
        patterns = SPEC_PATTERNS + [d.ExclusionPattern(("N", "Y", "Y"), "重複")]
        kept, excluded = d.apply_exclusions(rules, patterns)
        self.assertEqual(len(kept), 4)
        r5 = next(r for r in excluded if r.rule_id == "R5")  # N Y Y は #1 と #3 の両方に一致
        self.assertEqual(r5.pattern_indexes, (0, 2))
        self.assertEqual(d.pattern_match_counts(rules, patterns), [2, 2, 1])

    def test_all_excluded_and_limits(self):
        with self.assertRaises(d.FrameworkError):
            d.build_framework(items(), exclusions=[d.ExclusionPattern(("Y", "-", "-"), ""), d.ExclusionPattern(("N", "-", "-"), "")])
        seven = d.DecisionItems([f"条件{i}" for i in range(7)], [d.ActionItem("x", "action")])
        with self.assertRaises(d.TooManyConditionsError):
            d.build_framework(seven, 6)  # 除外なし: 128 > 64
        half = d.ExclusionPattern(("N",) + ("-",) * 6, "理由")
        self.assertEqual(len(d.build_framework(seven, 6, exclusions=[half]).rules), 64)  # 除外で上限以下
        with self.assertRaises(d.TooManyConditionsError) as ctx:
            d.build_framework(seven, 5, exclusions=[half])
        self.assertIn("除外後も 64 ルール", str(ctx.exception))
        thirteen = d.DecisionItems([f"条件{i}" for i in range(13)], [d.ActionItem("x", "action")])
        with self.assertRaises(d.FrameworkError):
            d.build_framework(thirteen, 6, exclusions=[d.ExclusionPattern(("N",) + ("-",) * 12, "")])

    def test_prompt_and_validation_with_gaps(self):
        fw = d.build_framework(items(), exclusions=SPEC_PATTERNS)
        table = d.rule_table_markdown(fw)
        self.assertIn("ルールIDは連番ではありません", table)
        self.assertNotIn("| R3 |", table)
        self.assertIn("表にある 4 件", d.output_example_json(fw))
        ok = {"results": [{"rule_id": rid, "actions": {"最終料金": 1000}} for rid in fw.rule_ids]}
        self.assertTrue(d.validate_fill_data(fw, ok).ok)
        bad = {"results": ok["results"] + [{"rule_id": "R3", "actions": {"最終料金": 1000}}]}
        report = d.validate_fill_data(fw, bad)
        self.assertFalse(report.ok)
        self.assertIn("除外したルールID R3", report.errors[0])
        # 除外のない枠組みのプロンプトは従来と同じ文言
        self.assertIn("R1 〜 R8", d.rule_table_markdown(d.build_framework(items())))

    def test_remap_exclusions(self):
        saved = [p.to_dict(VISITS) for p in SPEC_PATTERNS]
        # 条件の順番が違っても条件文で対応付ける
        mapped, dropped = d.remap_exclusions(saved, ["来場回数10回以上", "来場回数3回以上", "来場回数5回以上", "会員である"])
        self.assertEqual([m.values for m in mapped], [("-", "N", "Y", "-"), ("Y", "-", "N", "-")])
        self.assertEqual(dropped, [])
        # Y/N を指定している条件がなければ対応付けない
        mapped, dropped = d.remap_exclusions(saved, ["来場回数3回以上", "来場回数5回以上"])
        self.assertEqual(len(mapped), 1)
        self.assertEqual(len(dropped), 1)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [
            mock.patch.object(file_manager, "OUTPUTS_DIR", self.tmp / "outputs"),
            mock.patch.object(config_manager, "CACHE_DIR", self.tmp / "outputs" / "_cache"),
            mock.patch.object(config_manager, "FRAMEWORKS_DIR", self.tmp / "frameworks"),
            mock.patch.object(config_manager, "SETTINGS_PATH", self.tmp / "settings.json"),
        ]
        for p in self.patches:
            p.start()
        self.source = SpecSource(name="99_来場回数", label="99_来場回数", path=Path("x.xlsx"),
                                 requirements="来場回数に応じた割引", meta={"番号": "99"})

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def responder(self, model, prompt):
        if tcf.is_extract(prompt):
            return VISITS_JSON
        ids = tcf.rule_ids_in_prompt(prompt)
        # モデル a は R8 を「適用不可」と判断する (除外とは別物として記録されることを確認する)
        return tcf.fill_json(ids, lambda rid: d.NOT_APPLICABLE if (model == "m/a" and rid == "R8") else 1000)

    def run_batch(self, mode):
        client = tcf.FakeClient(self.responder)
        worker = BatchWorker(client=client, sources=[self.source], extraction_model="m/x",
                             generation_models=["m/a", "m/b"], max_parallel_specs=1, mode=mode,
                             options=pipeline.RunOptions(method=METHOD_COMMON_FRAMEWORK, reuse_cached_results=False))
        finished = []
        worker.finished.connect(lambda xs: finished.extend(xs))
        worker.run()
        QCoreApplication.processEvents()
        return worker, client, finished


class TestStoreAndGeneration(Base):
    def test_draft_updates_in_place_approved_creates_new_version(self):
        self.run_batch(MODE_FRAMEWORK_ONLY)
        draft = framework_store.list_records()[0]
        updated = framework_store.update_exclusions(draft, SPEC_PATTERNS)
        self.assertEqual(updated.framework_id, draft.framework_id)  # 下書きはその場で更新
        reloaded = framework_store.load(draft.path)
        self.assertEqual(reloaded.rule_count, 4)
        self.assertEqual(reloaded.excluded_count, 4)
        self.assertEqual([e["reason"] for e in reloaded.exclusions], ["5回以上なら必ず3回以上", "10回以上なら必ず5回以上"])
        self.assertEqual(reloaded.exclusions[0]["conditions"], {VISITS[0]: "N", VISITS[1]: "Y", VISITS[2]: "-"})
        self.assertIn("除外した組合せ (4 通り", Path(draft.path).with_suffix(".md").read_text(encoding="utf-8"))

        framework_store.set_status(reloaded, framework_store.STATUS_APPROVED)
        newer = framework_store.update_exclusions(reloaded, SPEC_PATTERNS[:1])
        self.assertNotEqual(newer.framework_id, reloaded.framework_id)  # 承認済みは新しい版
        self.assertEqual(newer.parent_id, reloaded.framework_id)
        self.assertEqual(newer.status, framework_store.STATUS_DRAFT)
        self.assertEqual(newer.rule_count, 6)
        original = framework_store.load(reloaded.path)
        self.assertEqual((original.status, original.rule_count), (framework_store.STATUS_APPROVED, 4))  # 元は不変

    def test_generation_uses_only_kept_rules_and_distinguishes_excluded(self):
        self.run_batch(MODE_FRAMEWORK_ONLY)
        record = framework_store.update_exclusions(framework_store.list_records()[0], SPEC_PATTERNS)
        framework_store.set_status(record, framework_store.STATUS_APPROVED)

        worker, client, finished = self.run_batch(MODE_USE_SAVED)
        prompts = [c[1] for c in client.calls]
        self.assertEqual(len(prompts), 2)
        for prompt in prompts:  # 全モデルに同じ4ルールだけを渡す
            self.assertEqual(tcf.rule_ids_in_prompt(prompt), ["R1", "R2", "R4", "R8"])
        self.assertEqual(prompts[0], prompts[1])
        exp = finished[0]
        self.assertTrue(all(r.ok for r in exp.results))
        self.assertEqual(exp.framework_source["excluded_count"], 4)
        self.assertEqual(exp.generation_settings["framework_strategy"], d.STRATEGY_EXCLUSION)
        counts = exp.framework_comparison["counts"]
        self.assertEqual(counts["excluded"], 4)  # 人が除外
        self.assertEqual(counts["applicability_mismatch"], 1)  # R8: LLM の「適用不可」(a) と 1000 (b)

        with (Path(exp.output_dir) / "rule_results.csv").open(encoding="utf-8-sig") as fp:
            rows = list(csv.DictReader(fp))
        excluded_rows = [r for r in rows if r["rule_category"] == "excluded"]
        self.assertEqual(sorted({r["rule_id"] for r in excluded_rows}), ["R3", "R5", "R6", "R7"])
        self.assertTrue(all(r["model_id"] == "" and r["value"] == "" for r in excluded_rows))
        self.assertIn("必ず", excluded_rows[0]["exclusion_reason"])
        na_rows = [r for r in rows if r["not_applicable"] == "True"]
        self.assertEqual([(r["rule_id"], r["model_id"]) for r in na_rows], [("R8", "m/a")])
        self.assertNotEqual(na_rows[0]["rule_category"], "excluded")
        self.assertEqual([r["rule_id"] for r in rows][:2], ["R1", "R1"])  # ルール番号順
        with (Path(exp.output_dir) / "summary.csv").open(encoding="utf-8-sig") as fp:
            summary = list(csv.DictReader(fp))
        self.assertEqual((summary[0]["total_combinations"], summary[0]["excluded_count"]), ("8", "4"))
        self.assertEqual(summary[0]["rules_excluded"], "4")

    def test_too_many_conditions_can_be_rescued_by_exclusion(self):
        seven = json.dumps({"conditions": [f"条件{i}" for i in range(7)],
                            "actions": [{"name": "最終料金", "type": "value", "valueType": "int"}]}, ensure_ascii=False)
        record = framework_store.build_record(
            source_name="s", source_label="s", source_path="", source_meta={}, requirements="r",
            extraction={"model": "m/x"}, decision_items=seven, max_conditions=6)
        framework_store.save(record)
        self.assertEqual(record.status, framework_store.STATUS_UNUSABLE)
        fixed = framework_store.update_exclusions(record, [d.ExclusionPattern(("N",) + ("-",) * 6, "条件0は常に成立")])
        self.assertEqual(fixed.status, framework_store.STATUS_DRAFT)
        self.assertEqual(fixed.rule_count, 64)
        self.assertTrue(fixed.usable)

    def test_items_edit_carries_exclusions(self):
        self.run_batch(MODE_FRAMEWORK_ONLY)
        parent = framework_store.update_exclusions(framework_store.list_records()[0], SPEC_PATTERNS)
        edited = json.loads(VISITS_JSON)
        edited["conditions"] = ["来場回数3回以上", "来場回数5回以上", "会員である"]  # 10回以上を削除
        child = framework_store.derive_edited(parent, json.dumps(edited, ensure_ascii=False))
        self.assertEqual([e["pattern"] for e in child.exclusions], ["N Y -"])
        self.assertEqual(len(child.exclusion_warnings), 1)
        self.assertEqual(child.rule_count, 6)

    def test_reusable_sources(self):
        self.run_batch(MODE_FRAMEWORK_ONLY)
        self.run_batch(MODE_FRAMEWORK_ONLY)
        newer, older = framework_store.list_records()
        framework_store.update_exclusions(older, SPEC_PATTERNS)
        sources = framework_store.reusable_exclusion_sources(framework_store.list_records(), VISITS)
        self.assertEqual([(r.framework_id, n) for r, n in sources], [(older.framework_id, 2)])


class TestEditorUi(Base):
    def test_editor_preview_and_save(self):
        from PySide6.QtWidgets import QMessageBox

        from ui.exclusion_editor import FILTER_EXCLUDED, ExclusionEditor
        from ui.framework_dialog import FrameworkDialog

        self.run_batch(MODE_FRAMEWORK_ONLY)
        dialog = FrameworkDialog({})
        dialog.tree.setCurrentItem(dialog.tree.topLevelItem(0))
        editor: ExclusionEditor = dialog.exclusion_editor
        self.assertIn("全 8 通り → 除外 0 通り", editor.count_label.text())
        editor.quick_edit.setText("N Y -")
        editor.quick_reason.setText("5回以上なら必ず3回以上")
        editor.quick_button.click()
        editor.quick_edit.setText("- N Y")
        editor.quick_button.click()  # 理由なし
        self.assertIn("除外 4 通り", editor.count_label.text())
        self.assertIn("残り 4 ルール", editor.count_label.text())
        self.assertEqual(editor.pattern_table.item(0, 5).text(), "2")  # 一致数
        editor.filter_combo.setCurrentIndex(editor.filter_combo.findData(FILTER_EXCLUDED))
        self.assertEqual(editor.preview_model.rowCount(), 4)
        self.assertEqual(editor.preview_model.rule_at(0).rule_id, "R3")

        with mock.patch.object(QMessageBox, "warning") as warning:
            self.assertIsNone(editor.save())  # 理由が空なので保存しない
            self.assertIn("#2", warning.call_args.args[2])
            dialog.approve_button.click()  # 未保存の除外があるので承認しない
        self.assertEqual(framework_store.list_records()[0].status, framework_store.STATUS_DRAFT)

        editor.pattern_table.item(1, 4).setText("10回以上なら必ず5回以上")
        saved = editor.save()
        self.assertIsNotNone(saved)
        self.assertEqual(framework_store.load(saved.path).rule_count, 4)
        self.assertIn("3 / 4 (全8・除外4)", dialog.tree.currentItem().text(2))
        self.assertFalse(editor.has_unsaved_changes())
        dialog.approve_button.click()
        self.assertEqual(framework_store.list_records()[0].status, framework_store.STATUS_APPROVED)

        # 全て - の行は無効として扱う
        editor.add_button.click()
        self.assertIn("無効なパターン 1 件", editor.count_label.text())

    def test_large_preview_is_fast(self):
        from ui.exclusion_editor import ExclusionEditor

        twelve = json.dumps({"conditions": [f"条件{i}" for i in range(12)],
                             "actions": [{"name": "x", "type": "action"}]}, ensure_ascii=False)
        record = framework_store.build_record(
            source_name="big", source_label="big", source_path="", source_meta={}, requirements="r",
            extraction={"model": "m/x"}, decision_items=twelve, max_conditions=6)
        editor = ExclusionEditor()
        started = time.perf_counter()
        editor.set_record(record, [record])
        for i in range(6):
            editor._add_pattern(tuple("Y" if j == i else ("N" if j == i + 1 else "-") for j in range(12)), "r")
        elapsed = time.perf_counter() - started
        self.assertEqual(editor.preview_model.rowCount(), 4096)
        self.assertLess(elapsed, 3.0)
        self.assertIn("全 4096 通り", editor.count_label.text())


if __name__ == "__main__":
    unittest.main()
