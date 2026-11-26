"""型が合わない値を返したモデル (形式不正) も、返ってきた値で比較に含めるテスト. API は呼ばない."""

from __future__ import annotations

import json
import unittest

from services import dt_framework as d
from services import result_export
from services.framework_comparison import (
    CATEGORY_EXACT,
    CATEGORY_VALUE_MISMATCH,
    compare_fills,
    comparison_for_record,
    fills_from_records,
)

ITEMS = json.dumps({
    "conditions": ["会員である", "来場回数が5回以上である"],
    "actions": [{"name": "最終料金", "type": "value", "valueType": "int"}, {"name": "記念品を渡す", "type": "action"}],
}, ensure_ascii=False)


def fill(values: dict[str, tuple]) -> str:
    return json.dumps({"results": [
        {"rule_id": rid, "actions": {"最終料金": fee, "記念品を渡す": gift}} for rid, (fee, gift) in values.items()
    ]}, ensure_ascii=False)


class TestPartialComparison(unittest.TestCase):
    def setUp(self):
        self.fw = d.build_framework(d.parse_decision_items(ITEMS))
        good = {"R1": (4000, "X"), "R2": (4500, "-"), "R3": (4500, "X"), "R4": (5000, "-")}
        formula = {"R1": ("0.9P-500", "X"), "R2": ("0.9P", "-"), "R3": ("P-500", "X"), "R4": ("P", "-")}
        self.responses = {
            "m/good": fill(good),
            "m/good2": fill(good),
            "m/formula": fill(formula),
            "m/refuse": json.dumps({"error": "P が不明なので整数にできません"}, ensure_ascii=False),
        }

    def record(self) -> dict:
        """以前の版で保存した results.json の形 (形式不正のモデルは型不正の値が validated_results に無い)."""
        results = []
        for model, text in self.responses.items():
            report = d.validate_fill_response(self.fw, text, self.fw.rule_ids)
            ok = report.ok
            results.append({
                "model_id": model, "model_name": model, "status": "ok" if ok else "error",
                "validated_results": report.values if ok else {}, "raw_results": report.raw_values if ok else {},
                "raw_response": text, "validation_errors": report.errors,
                "batches": [{"rule_ids": self.fw.rule_ids, "raw_response": text}],
            })
        return {"generation_method": "common_framework", "framework_status": "ok",
                "framework": self.fw.to_dict(), "results": results}

    def test_type_mismatch_values_are_kept_but_model_is_still_invalid(self):
        report = d.validate_fill_response(self.fw, self.responses["m/formula"], self.fw.rule_ids)
        self.assertFalse(report.ok)
        self.assertIn("int 型ではありません", report.errors[0])
        self.assertEqual(report.values["R2"], {"最終料金": "0.9P", "記念品を渡す": "-"})

    def test_invalid_model_with_values_is_compared(self):
        record = self.record()
        fills = fills_from_records(record["results"], self.fw)
        result = compare_fills(self.fw, fills)
        self.assertEqual(result.partial_models, ["m/formula"])
        self.assertEqual(result.failed_models, ["m/refuse"])  # 値を1つも取り出せない
        r2 = next(r for r in result.rules if r.rule_id == "R2")
        fee = next(a for a in r2.actions if a.action == "最終料金")
        gift = next(a for a in r2.actions if a.action == "記念品を渡す")
        self.assertEqual(fee.values["m/formula"], "0.9P")
        self.assertEqual(fee.type_mismatch, ("m/formula",))
        self.assertEqual(fee.category, CATEGORY_VALUE_MISMATCH)
        self.assertEqual(gift.category, CATEGORY_EXACT)  # 型が正しい項目はそのまま一致

        saved = comparison_for_record(record)
        self.assertEqual(saved["partial_models"], ["m/formula"])
        self.assertEqual(saved["rules"][1]["actions"]["最終料金"]["type_mismatch"], ["m/formula"])

    def test_without_framework_old_behavior_is_kept(self):
        # 枠組みを渡さなければ保存済みの validated_results だけを使う (形式不正のモデルは値なし → 比較対象外)
        result = compare_fills(self.fw, fills_from_records(self.record()["results"]))
        self.assertEqual(sorted(result.failed_models), ["m/formula", "m/refuse"])

    def test_missing_rule_is_reported_per_rule(self):
        self.responses["m/formula"] = fill({"R1": ("0.9P-500", "X")})  # R2 以降が欠落
        result = compare_fills(self.fw, fills_from_records(self.record()["results"], self.fw))
        r1, r2 = result.rules[0], result.rules[1]
        self.assertIn("m/formula", r1.actions[0].values)
        self.assertNotIn("m/formula", r1.failed_models)
        self.assertNotIn("m/formula", r2.actions[0].values)
        self.assertIn("m/formula", r2.failed_models)

    def test_csv_includes_values_of_partially_valid_model(self):
        rows = result_export.rule_rows(self.record())
        row = next(r for r in rows if r["model_id"] == "m/formula" and r["rule_id"] == "R2" and r["action"] == "最終料金")
        self.assertEqual((row["value"], row["type_mismatch"], row["model_status"]), ("0.9P", True, "error"))
        self.assertEqual(row["action_category"], CATEGORY_VALUE_MISMATCH)
        refuse = next(r for r in rows if r["model_id"] == "m/refuse" and r["rule_id"] == "R2")
        self.assertEqual(refuse["value"], "")
        summary = {r["model_id"]: r for r in result_export.summary_rows(self.record())}
        self.assertEqual((summary["m/formula"]["compared"], summary["m/refuse"]["compared"]), (True, False))


if __name__ == "__main__":
    unittest.main()
