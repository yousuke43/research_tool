"""枠組みの保存・確認・承認 (2段階実行) と解析用 CSV のテスト. API は使わない."""

from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import test_common_framework as tcf  # noqa: E402  (QApplication もここで作られる)

from PySide6.QtCore import QCoreApplication  # noqa: E402

from models.experiment import METHOD_COMMON_FRAMEWORK  # noqa: E402
from models.spec_source import SpecSource  # noqa: E402
from services import framework_store, pipeline, result_export  # noqa: E402
from services.batch_runner import MODE_FRAMEWORK_ONLY, MODE_FULL, MODE_USE_SAVED, BatchWorker  # noqa: E402
from utils import config_manager, file_manager  # noqa: E402

SPEC_META = {"番号": "01", "テーマ": "テーマパーク", "解釈揺れ": "あり", "区分": "変数追加版", "sheet": "Sheet2", "row": 3}


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as fp:
        return list(csv.DictReader(fp))


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
        self.sources = [
            SpecSource(name="01_テーマパーク_変数追加版_あり", label="book.xlsx / Sheet2 / 01_あり", path=Path("book.xlsx"),
                       requirements="テーマパークの料金仕様A", meta=dict(SPEC_META)),
            SpecSource(name="01_テーマパーク_変数追加版_なし", label="book.xlsx / Sheet2 / 01_なし", path=Path("book.xlsx"),
                       requirements="テーマパークの料金仕様B", meta={**SPEC_META, "解釈揺れ": "なし"}),
        ]

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_batch(self, mode, responder=None, sources=None):
        client = tcf.FakeClient(responder or (lambda m, p: tcf.TestFrameworkPipeline.responder(None, m, p)))
        worker = BatchWorker(
            client=client, sources=sources or self.sources, extraction_model="m/x",
            generation_models=["m/a", "m/b"], max_parallel_specs=1, mode=mode,
            options=pipeline.RunOptions(method=METHOD_COMMON_FRAMEWORK, reuse_cached_results=False),
        )
        finished, failed = [], []
        worker.finished.connect(lambda items: finished.extend(items))
        worker.file_failed.connect(lambda label, msg: failed.append((label, msg)))
        worker.run()
        QCoreApplication.processEvents()
        return worker, client, finished, failed


class TestTwoStageRun(Base):
    def test_framework_only_saves_drafts_without_generation(self):
        worker, client, finished, failed = self.run_batch(MODE_FRAMEWORK_ONLY)
        self.assertEqual(failed, [])
        self.assertTrue(all(tcf.is_extract(c[1]) for c in client.calls))  # 生成モデルは呼ばない
        self.assertEqual(len(client.calls), 2)
        self.assertFalse((self.tmp / "outputs").exists() and any((self.tmp / "outputs").glob("batch_*")))
        records = framework_store.list_records()
        self.assertEqual(len(records), 2)
        r = next(r for r in records if r.source_name.endswith("あり"))
        self.assertEqual(r.status, framework_store.STATUS_DRAFT)
        self.assertEqual(r.source_meta["番号"], "01")
        self.assertEqual(r.requirements, "テーマパークの料金仕様A")
        self.assertEqual(r.requirements_sha256, framework_store.requirements_hash("テーマパークの料金仕様A"))
        self.assertEqual(r.extraction["model"], "m/x")
        self.assertIn("extract", r.extraction["prompt"])
        self.assertEqual(r.rule_count, 8)
        # 仕様ごとのフォルダに JSON と確認用 Markdown
        path = Path(r.path)
        self.assertEqual(path.parent.name, "01_テーマパーク_変数追加版_あり")
        md = path.with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn("01 テーマパーク 揺れあり", md)
        self.assertIn("| 条件部 | 来場人数が1人である |", md)

    def test_use_saved_requires_approval(self):
        self.run_batch(MODE_FRAMEWORK_ONLY)
        worker, client, finished, failed = self.run_batch(MODE_USE_SAVED)
        self.assertEqual(client.calls, [])  # 未承認なので API は一切呼ばない
        self.assertEqual(finished, [])
        self.assertEqual(len(failed), 2)
        summary = json.loads((worker.batch_dir / "batch_summary.json").read_text(encoding="utf-8"))
        self.assertEqual({s["status"] for s in summary["specs"]}, {"no_approved_framework"})

    def test_use_saved_after_approval(self):
        self.run_batch(MODE_FRAMEWORK_ONLY)
        record = next(r for r in framework_store.list_records() if r.source_name.endswith("あり"))
        framework_store.set_status(record, framework_store.STATUS_APPROVED)
        framework_store.set_note(record, "C1とC2の同時成立は不可能")

        worker, client, finished, failed = self.run_batch(MODE_USE_SAVED)
        self.assertFalse(any(tcf.is_extract(c[1]) for c in client.calls))  # 抽出しない
        self.assertEqual({c[0] for c in client.calls}, {"m/a", "m/b"})
        self.assertEqual(len(finished), 1)  # 承認済みの「あり」だけ
        self.assertEqual(len(failed), 1)
        exp = finished[0]
        self.assertEqual(exp.framework_source["framework_id"], record.framework_id)
        self.assertEqual(exp.framework_source["note"], "C1とC2の同時成立は不可能")
        self.assertEqual(exp.extraction_model, "m/x")
        self.assertEqual(exp.framework, record.framework)
        self.assertTrue(all(r.ok for r in exp.results))

        saved = json.loads((Path(exp.output_dir) / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["framework_source"]["framework_id"], record.framework_id)
        self.assertIn("commit", saved["tool_version"])
        self.assertEqual(saved["source_meta"]["番号"], "01")
        # 解析用 CSV (仕様フォルダ・一括実行フォルダの両方)
        rules = read_csv(Path(exp.output_dir) / "rule_results.csv")
        self.assertEqual(len(rules), 8 * 2 * 1)  # ルール × モデル × 動作
        r6 = [r for r in rules if r["rule_id"] == "R6"]
        self.assertEqual({r["value"] for r in r6}, {"4000", "9999"})
        self.assertEqual({r["rule_category_label"] for r in r6}, {"動作結果の不一致"})
        self.assertEqual(r6[0]["framework_id"], record.framework_id)
        self.assertEqual(r6[0]["解釈揺れ"], "あり")
        batch_summary = read_csv(worker.batch_dir / "summary.csv")
        self.assertEqual([r["model_id"] for r in batch_summary], ["m/a", "m/b"])
        self.assertEqual(batch_summary[0]["rules_value_mismatch"], "1")
        self.assertEqual(len(read_csv(worker.batch_dir / "rule_results.csv")), 16)

    def test_latest_approved_is_used(self):
        self.run_batch(MODE_FRAMEWORK_ONLY)
        self.run_batch(MODE_FRAMEWORK_ONLY)
        records = [r for r in framework_store.list_records() if r.source_name.endswith("あり")]
        self.assertEqual(len(records), 2)
        for r in records:
            framework_store.set_status(r, framework_store.STATUS_APPROVED)
        found = framework_store.find_approved(records[0].source_name, "テーマパークの料金仕様A")
        self.assertEqual(found[0].framework_id, records[0].framework_id)  # 新しい順

    def test_changed_requirements_do_not_match(self):
        self.run_batch(MODE_FRAMEWORK_ONLY)
        record = framework_store.list_records()[0]
        framework_store.set_status(record, framework_store.STATUS_APPROVED)
        self.assertEqual(framework_store.find_approved(record.source_name, "書き換えた仕様"), [])
        self.assertEqual(len(framework_store.find_approved(record.source_name, record.requirements)), 1)

    def test_edited_version(self):
        self.run_batch(MODE_FRAMEWORK_ONLY, sources=self.sources[:1])
        parent = framework_store.list_records()[0]
        items = json.loads(tcf.dt_framework.extract_json_text(parent.decision_items))
        items["conditions"] = items["conditions"][:2]  # 条件を1つ削る
        child = framework_store.derive_edited(parent, json.dumps(items, ensure_ascii=False))
        framework_store.save(child)
        self.assertTrue(child.edited)
        self.assertEqual(child.parent_id, parent.framework_id)
        self.assertEqual(child.rule_count, 4)
        self.assertEqual(framework_store.load(parent.path).rule_count, 8)  # 元の版は変わらない
        framework_store.set_status(child, framework_store.STATUS_APPROVED)

        worker, client, finished, failed = self.run_batch(MODE_USE_SAVED, sources=self.sources[:1])
        exp = finished[0]
        self.assertTrue(exp.framework_source["edited"])
        self.assertEqual(len(exp.framework["rules"]), 4)
        self.assertNotIn("午前9時", exp.decision_items)

    def test_too_many_conditions_cannot_be_approved(self):
        many = json.dumps({"conditions": [f"条件{i}" for i in range(7)],
                           "actions": [{"name": "最終料金", "type": "value", "valueType": "int"}]}, ensure_ascii=False)
        self.run_batch(MODE_FRAMEWORK_ONLY, responder=lambda m, p: many, sources=self.sources[:1])
        record = framework_store.list_records()[0]
        self.assertEqual(record.status, framework_store.STATUS_UNUSABLE)
        self.assertIn("7 個", record.framework_error)
        self.assertEqual(len(record.framework["conditions"]), 7)  # 抽出結果は残る
        with self.assertRaises(ValueError):
            framework_store.set_status(record, framework_store.STATUS_APPROVED)

    def test_conventional_ignores_mode(self):
        client = tcf.FakeClient(lambda m, p: tcf.EXTRACTED_JSON if tcf.is_extract(p) else tcf.CONVENTIONAL_MD)
        worker = BatchWorker(client=client, sources=self.sources, extraction_model="m/x",
                             generation_models=["m/a"], max_parallel_specs=1, mode=MODE_FRAMEWORK_ONLY,
                             options=pipeline.RunOptions(method="conventional", reuse_cached_results=False))
        self.assertEqual(worker.mode, MODE_FULL)
        worker.run()
        self.assertEqual(framework_store.list_records(), [])
        rows = read_csv(worker.batch_dir / "rule_results.csv")
        self.assertEqual(len(rows), 2 * 2)  # 2仕様 × 2ルール
        self.assertEqual(rows[0]["conditions_text"], "来場人数が1人である=Y")


class TestUiReview(Base):
    def test_framework_dialog_approve_and_batch_dialog_marks(self):
        from PySide6.QtWidgets import QMessageBox

        from ui.batch_dialog import BatchDialog
        from ui.framework_dialog import FrameworkDialog
        from ui.history_dialog import HistoryDialog

        self.run_batch(MODE_FRAMEWORK_ONLY)
        dialog = FrameworkDialog({})
        self.assertEqual(dialog.tree.topLevelItemCount(), 2)
        dialog.tree.setCurrentItem(dialog.tree.topLevelItem(0))
        self.assertIn("条件組合せ (8 ルール)", dialog.review_view.text())
        self.assertTrue(dialog.approve_button.isEnabled())
        dialog.approve_button.click()
        approved = [r for r in framework_store.list_records() if r.status == framework_store.STATUS_APPROVED]
        self.assertEqual(len(approved), 1)
        self.assertFalse(dialog.approve_button.isEnabled())  # 承認後は選択が保たれ、取り消しのみ可能

        # 抽出項目を修正して新しい版
        items = json.loads(dialog.items_edit.toPlainText())
        items["conditions"].pop()
        dialog.items_edit.setPlainText(json.dumps(items, ensure_ascii=False))
        dialog.derive_button.click()
        self.assertEqual(dialog.tree.topLevelItemCount(), 3)
        self.assertIn("修正版", dialog.tree.currentItem().text(5))

        dialog.status_filter.setCurrentIndex(dialog.status_filter.findData(framework_store.STATUS_APPROVED))
        visible = [i for i in range(dialog.tree.topLevelItemCount()) if not dialog.tree.topLevelItem(i).isHidden()]
        self.assertEqual(len(visible), 1)

        with mock.patch("utils.file_manager.list_spec_sources", return_value=(self.sources, [])):
            batch = BatchDialog(None, {"generation_method": METHOD_COMMON_FRAMEWORK}, [])
            self.assertEqual(batch.mode_combo.currentData(), MODE_FRAMEWORK_ONLY)
            self.assertEqual(batch.run_button.text(), "枠組みを作成")
            batch.mode_combo.setCurrentIndex(batch.mode_combo.findData(MODE_USE_SAVED))
            texts = [batch.file_list.item(i).text() for i in range(batch.file_list.count())]
            self.assertEqual(sum("✔ 承認済み" in t for t in texts), 1)
            batch.method_combo.setCurrentIndex(batch.method_combo.findData("conventional"))
            self.assertFalse(batch.mode_combo.isEnabled())
            self.assertEqual(batch.run_button.text(), "実行")

        # 承認済みで生成 -> 履歴に表示・CSV 書き出し
        self.run_batch(MODE_USE_SAVED)
        history = HistoryDialog()
        self.assertEqual(history.run_list.count(), 1)
        history.method_filter.setCurrentIndex(history.method_filter.findData("conventional"))
        self.assertEqual(len(history._visible_runs()), 0)
        history.method_filter.setCurrentIndex(0)
        history.search_edit.setText("テーマパーク")
        self.assertEqual(len(history._visible_runs()), 1)
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.No):
            history._export_csv()
        exports = list((self.tmp / "outputs" / "exports").glob("export_*/rule_results.csv"))
        self.assertEqual(len(exports), 1)
        self.assertEqual(len(read_csv(exports[0])), 16)
        history.run_list.setCurrentRow(0)
        self.assertIn("使用した枠組み", history.summary_label.text())


class TestExportRows(unittest.TestCase):
    def test_not_generated_record_still_has_summary_row(self):
        record = {"generation_method": METHOD_COMMON_FRAMEWORK, "framework_status": "too_many_conditions",
                  "framework_error": "条件数が多い", "results": [], "source_meta": SPEC_META}
        rows = result_export.summary_rows(record)
        self.assertEqual(rows[0]["status"], "too_many_conditions")
        self.assertEqual(rows[0]["番号"], "01")
        self.assertEqual(result_export.rule_rows(record), [])


if __name__ == "__main__":
    unittest.main()


class TestSpecOrder(unittest.TestCase):
    def test_records_are_sorted_by_spec_number(self):
        def rec(name, number, created):
            r = framework_store.build_record(
                source_name=name, source_label=name, source_path="", source_meta={"番号": number} if number else {},
                requirements="r", extraction={"model": "m/x"},
                decision_items='{"conditions": ["a"], "actions": [{"name": "x", "type": "action"}]}', max_conditions=6)
            r.created_at = created
            return r

        records = [  # list_records() と同じ新しい順
            rec("10_ホテル_あり", "10", "2026-11-20T10:05:00"),
            rec("2_映画_なし", "2", "2026-11-20T10:04:00"),
            rec("manual", "", "2026-11-20T10:03:00"),
            rec("2_映画_あり", "2", "2026-11-20T10:02:00"),
            rec("10_ホテル_あり", "10", "2026-11-20T10:01:00"),
        ]
        ordered = sorted(records, key=framework_store.spec_sort_key)
        self.assertEqual([(r.source_name, r.created_at[-5:]) for r in ordered], [
            ("2_映画_あり", "02:00"), ("2_映画_なし", "04:00"),
            ("10_ホテル_あり", "05:00"), ("10_ホテル_あり", "01:00"),  # 同じ仕様は新しい版が上
            ("manual", "03:00"),
        ])
