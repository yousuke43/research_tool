"""トークン累計 (services/token_usage.py) のテスト. API は呼ばない."""

from __future__ import annotations

import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services import token_usage
from services.openrouter_client import OpenRouterClient, OpenRouterError
from utils import config_manager, file_manager


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.outputs = self.tmp / "outputs"
        self.frameworks = self.tmp / "frameworks"
        self.patches = [
            mock.patch.object(file_manager, "OUTPUTS_DIR", self.outputs),
            mock.patch.object(config_manager, "FRAMEWORKS_DIR", self.frameworks),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def summary_rows(self) -> list[dict]:
        with (self.outputs / "token_usage" / "summary.csv").open(encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))


class TestClientRecordsCalls(Base):
    @staticmethod
    def response(content: str, usage: dict):
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": usage,
        }
        return response

    def test_each_api_call_is_recorded_and_summarized(self):
        client = OpenRouterClient(api_key="dummy")
        usage = {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140,
                 "completion_tokens_details": {"reasoning_tokens": 30}, "cost": 0.002}
        with mock.patch("services.openrouter_client.requests.post", return_value=self.response("ok", usage)):
            client.chat("m/a", "hi", purpose=token_usage.PURPOSE_EXTRACTION)
            client.chat("m/b", "hi", purpose=token_usage.PURPOSE_FRAMEWORK)
            client.chat("m/b", "hi", purpose=token_usage.PURPOSE_FRAMEWORK)

        calls = token_usage.load_calls()
        self.assertEqual([(c["model"], c["purpose"], c["source"]) for c in calls], [
            ("m/a", "extraction", "live"),
            ("m/b", "generation_common_framework", "live"),
            ("m/b", "generation_common_framework", "live"),
        ])
        self.assertEqual(calls[0]["reasoning_tokens"], "30")

        rows = self.summary_rows()
        overall = next(r for r in rows if r["集計区分"] == "全体")
        self.assertEqual((overall["回数"], overall["合計トークン"], overall["推論トークン"]), ("3", "420", "90"))
        self.assertAlmostEqual(float(overall["費用USD"]), 0.006)
        model_b = next(r for r in rows if r["集計区分"] == "モデル別" and r["モデル"] == "m/b")
        self.assertEqual(model_b["合計トークン"], "280")
        md = (self.outputs / "token_usage" / "summary.md").read_text(encoding="utf-8")
        self.assertIn("生成 (新方式: 動作結果補完)", md)
        self.assertIn("| 合計 | 3 | 300 | 120 | 90 | 420 |", md)

    def test_empty_response_is_still_recorded(self):
        client = OpenRouterClient(api_key="dummy")
        usage = {"prompt_tokens": 50, "completion_tokens": 8000, "total_tokens": 8050}
        response = self.response("", usage)
        response.json.return_value["choices"][0]["finish_reason"] = "length"
        with mock.patch("services.openrouter_client.requests.post", return_value=response):
            with self.assertRaises(OpenRouterError):
                client.chat("m/a", "hi", purpose=token_usage.PURPOSE_CONVENTIONAL)
        calls = token_usage.load_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["status"], calls[0]["total_tokens"]), ("empty_response", "8050"))


class TestBackfill(Base):
    def test_past_results_are_imported_once_without_duplicates(self):
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        # 従来方式: 抽出 1 + 生成 2 (うち 1 はキャッシュ再利用なので数えない)
        self.write_json(self.outputs / "run1" / "results.json", {
            "started_at": "2026-11-17T10:00:00", "extraction_model": "m/x", "decision_items": "A",
            "extraction_prompt_tokens": 10, "extraction_completion_tokens": 5, "extraction_total_tokens": 15,
            "results": [
                {"model_id": "m/a", **usage},
                {"model_id": "m/b", "reused_from_cache": True, **usage},
            ],
        })
        # 保存枠組みでの生成: 抽出は枠組みの写しなので数えない。バッチ 2 のうち 1 はキャッシュ
        self.write_json(self.outputs / "batch_1_共通DT" / "spec_1" / "results.json", {
            "started_at": "2026-11-18T10:00:00", "generation_method": "common_framework",
            "framework_source": {"framework_id": "f1"}, "extraction_model": "m/x", "decision_items": "B",
            "extraction_prompt_tokens": 10, "extraction_completion_tokens": 5, "extraction_total_tokens": 15,
            "results": [{"model_id": "m/a", **usage, "batches": [
                {**usage, "reused_from_cache": False}, {**usage, "reused_from_cache": True},
            ]}],
        })
        # 枠組み: 同じ抽出を持つ版が 2 つ (修正版・除外版は抽出を写す) → 1 回
        extraction = {"model": "m/x", "content": "B", **usage}
        self.write_json(self.frameworks / "spec" / "f1.json", {"created_at": "2026-11-18T09:00:00", "extraction": extraction})
        self.write_json(self.frameworks / "spec" / "f2.json", {"created_at": "2026-11-18T09:30:00", "extraction": extraction})
        self.write_json(self.outputs / "run1" / "additional_descriptions" / "a.json",
                        {"created_at": "2026-11-18T11:00:00", "model_id": "m/a", **usage})

        path = token_usage.write_summary()
        self.assertTrue(path.exists())
        calls = token_usage.load_calls()
        self.assertEqual(sorted((c["purpose"], c["model"]) for c in calls), sorted([
            ("extraction", "m/x"),                    # run1 の抽出
            ("generation_conventional", "m/a"),
            ("extraction", "m/x"),                    # 枠組み作成時の抽出 (版が 2 つでも 1 回)
            ("generation_common_framework", "m/a"),   # キャッシュでないバッチだけ
            ("additional_description", "m/a"),
        ]))
        self.assertTrue(all(c["source"] == "backfill" for c in calls))

        # 2 回目以降は取り込み直さない
        token_usage.record_call(purpose=token_usage.PURPOSE_EXTRACTION, model="m/x", total_tokens=7)
        self.assertEqual(len(token_usage.load_calls()), 6)
        overall = next(r for r in self.summary_rows() if r["集計区分"] == "全体")
        self.assertEqual(overall["合計トークン"], str(15 * 5 + 7))


if __name__ == "__main__":
    unittest.main()
