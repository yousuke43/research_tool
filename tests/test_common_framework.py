"""共通DT枠組み (新方式) と関連機能のテスト. API は使わない (モッククライアント).

実行方法 (プロジェクト直下で):
    .\\.venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(ROOT))

from models.experiment import METHOD_COMMON_FRAMEWORK, METHOD_CONVENTIONAL  # noqa: E402
from models.spec_source import SpecSource  # noqa: E402
from services import dt_framework, framework_comparison, pipeline  # noqa: E402
from services.decision_table_parser import parse_decision_table  # noqa: E402
from services.dt_framework import (  # noqa: E402
    NOT_APPLICABLE,
    ActionItem,
    DecisionItems,
    build_framework,
    parse_decision_items,
    validate_fill_response,
)
from services.openrouter_client import (  # noqa: E402
    ChatResult,
    KeyLimitError,
    OpenRouterClient,
    OpenRouterError,
    is_key_limit_error,
    make_api_error,
)
from services.table_comparison import align_labels, compare_tables  # noqa: E402
from utils import config_manager, file_manager  # noqa: E402
from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

# 画面のテスト (test_ui_smoke) と同じプロセスで動くよう QApplication を作る (描画はしない)
_APP = QCoreApplication.instance() or QApplication([])

EXTRACTED_JSON = """```json
{
  "conditions": [
    "来場人数が1人である",
    "来場人数が10人以上である",
    "午前9時までに来場した"
  ],
  "actions": [
    {"name": "最終料金", "type": "value", "valueType": "int"}
  ]
}
```"""

CONVENTIONAL_MD = """| 区分 | 記述 | R1 | R2 |
|---|---|---|---|
| 条件部 | 来場人数が1人である | Y | N |
| 動作部 | 最終料金 | 5000 | 4000 |
"""


def fill_json(rule_ids, value_for):
    return json.dumps(
        {"results": [{"rule_id": rid, "actions": {"最終料金": value_for(rid)}} for rid in rule_ids]},
        ensure_ascii=False,
    )


def price_for(framework, base=5000):
    """R1 等、条件1と条件2が同時に Y のルールは適用不可. それ以外は適当な料金."""
    rules = {r.rule_id: r.values for r in framework.rules}

    def value(rid):
        v = rules[rid]
        if v[0] == "Y" and v[1] == "Y":
            return NOT_APPLICABLE
        return base - (500 if v[2] == "Y" else 0) - (1000 if v[1] == "Y" else 0)

    return value


class FakeClient(OpenRouterClient):
    """プロンプト内容に応じて応答を返すモッククライアント. 呼び出しを記録する."""

    def __init__(self, responder):
        super().__init__(api_key="dummy")
        self.responder = responder
        self.calls: list[tuple[str, str, dict]] = []
        self._lock = threading.Lock()

    def chat(self, model, prompt, temperature=None, max_tokens=None, response_format=None, purpose=""):
        with self._lock:
            self.calls.append((model, prompt, {"max_tokens": max_tokens}))
        content = self.responder(model, prompt)
        if isinstance(content, Exception):
            raise content
        return ChatResult(content=content, prompt_tokens=100, completion_tokens=50, total_tokens=150)


def is_extract(prompt: str) -> bool:
    return "抽出する専門家" in prompt


def rule_ids_in_prompt(prompt: str) -> list[str]:
    ids = []
    for line in prompt.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and cells[0].startswith("R") and cells[0][1:].isdigit() and len(cells) > 1 and cells[1] in ("Y", "N"):
            ids.append(cells[0])
    return ids


class TempOutputsMixin:
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [
            mock.patch.object(file_manager, "OUTPUTS_DIR", self.tmp / "outputs"),
            mock.patch.object(config_manager, "CACHE_DIR", self.tmp / "outputs" / "_cache"),
            mock.patch.object(config_manager, "FRAMEWORKS_DIR", self.tmp / "frameworks"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------- テスト1・2
class TestCombinationGeneration(unittest.TestCase):
    def test_four_conditions_give_16_unique_rules(self):
        items = DecisionItems(["A", "B", "C", "D"], [ActionItem("最終料金", "value", "int")])
        fw = build_framework(items, max_conditions=6)
        self.assertEqual(len(fw.rules), 16)
        combos = [r.values for r in fw.rules]
        self.assertEqual(len(set(combos)), 16)  # 重複なし
        import itertools

        self.assertEqual(set(combos), set(itertools.product("YN", repeat=4)))  # 漏れなし
        self.assertEqual(fw.rule_ids, [f"R{i}" for i in range(1, 17)])
        self.assertEqual(combos[0], ("Y", "Y", "Y", "Y"))
        self.assertEqual(combos[-1], ("N", "N", "N", "N"))
        self.assertTrue(all("-" not in c for c in combos))  # '-' による統合なし

    def test_contradictory_combination_is_kept(self):
        fw = build_framework(parse_decision_items(EXTRACTED_JSON))
        self.assertEqual(len(fw.rules), 8)
        both = [r for r in fw.rules if r.values[0] == "Y" and r.values[1] == "Y"]
        self.assertEqual(len(both), 2)  # 「1人」と「10人以上」が同時に Y でも残る
        self.assertEqual(both[0].rule_id, "R1")

    def test_too_many_conditions(self):
        items = DecisionItems([f"C{i}" for i in range(7)], [ActionItem("x", "action")])
        with self.assertRaises(dt_framework.TooManyConditionsError):
            build_framework(items, max_conditions=6)
        self.assertEqual(len(build_framework(items, max_conditions=7).rules), 128)  # 上限は変更可能

    def test_framework_roundtrip_and_batches(self):
        fw = build_framework(parse_decision_items(EXTRACTED_JSON))
        again = dt_framework.CommonFramework.from_dict(json.loads(json.dumps(fw.to_dict())))
        self.assertEqual(again.rules, fw.rules)
        batches = dt_framework.split_batches(fw.rules, 3)
        self.assertEqual([len(b) for b in batches], [3, 3, 2])
        self.assertEqual([r.rule_id for b in batches for r in b], fw.rule_ids)


# ---------------------------------------------------------------- テスト3
class TestFill(unittest.TestCase):
    def setUp(self):
        self.fw = build_framework(parse_decision_items(EXTRACTED_JSON))

    def test_fill_only_action_part(self):
        text = "```json\n" + fill_json(self.fw.rule_ids, price_for(self.fw)) + "\n```"
        report = validate_fill_response(self.fw, text)
        self.assertTrue(report.ok, report.errors)
        md = dt_framework.to_markdown(self.fw, report.values)
        table = parse_decision_table(md)
        self.assertEqual(table.rule_ids, self.fw.rule_ids)
        # 条件部は枠組みそのまま
        for ci, row in enumerate(table.conditions):
            self.assertEqual(row.label, self.fw.conditions[ci])
            self.assertEqual(row.values, [r.values[ci] for r in self.fw.rules])
        # 動作部だけが埋まる
        prices = table.actions[0].values
        self.assertEqual(prices[0], NOT_APPLICABLE)  # R1 = Y,Y,Y
        self.assertEqual(prices[1], NOT_APPLICABLE)  # R2 = Y,Y,N
        self.assertEqual(prices[2], "4500")  # R3 = Y,N,Y
        self.assertEqual(prices[4], "3500")  # R5 = N,Y,Y
        self.assertEqual(prices[7], "5000")  # R8 = N,N,N


# ---------------------------------------------------------------- テスト4
class TestInvalidJson(unittest.TestCase):
    def setUp(self):
        self.fw = build_framework(parse_decision_items(EXTRACTED_JSON))
        self.ok_results = json.loads(fill_json(self.fw.rule_ids, price_for(self.fw)))["results"]

    def check(self, results, expected_fragment):
        report = validate_fill_response(self.fw, json.dumps({"results": results}, ensure_ascii=False))
        self.assertFalse(report.ok)
        self.assertTrue(any(expected_fragment in e for e in report.errors), report.errors)

    def test_missing_rule(self):
        self.check(self.ok_results[:-1], "不足")

    def test_duplicate_rule(self):
        self.check(self.ok_results + [self.ok_results[0]], "重複")

    def test_undefined_rule(self):
        self.check(self.ok_results + [{"rule_id": "R99", "actions": {"最終料金": 1}}], "未定義のルールID")

    def test_missing_action(self):
        broken = [dict(r) for r in self.ok_results]
        broken[3] = {"rule_id": broken[3]["rule_id"], "actions": {}}
        self.check(broken, "がありません")

    def test_unknown_action(self):
        broken = [dict(r) for r in self.ok_results]
        broken[3] = {"rule_id": broken[3]["rule_id"], "actions": {"最終料金": 1, "割引額": 2}}
        self.check(broken, "未定義の動作項目")

    def test_wrong_type(self):
        broken = [dict(r) for r in self.ok_results]
        broken[4] = {"rule_id": broken[4]["rule_id"], "actions": {"最終料金": 4500.5}}
        self.check(broken, "int")
        broken[4] = {"rule_id": broken[4]["rule_id"], "actions": {"最終料金": True}}
        self.check(broken, "真偽値")

    def test_invalid_value(self):
        broken = [dict(r) for r in self.ok_results]
        broken[4] = {"rule_id": broken[4]["rule_id"], "actions": {"最終料金": "不明"}}
        self.check(broken, "int 型ではありません")
        broken[4] = {"rule_id": broken[4]["rule_id"], "actions": {"最終料金": None}}
        self.check(broken, "null")

    def test_unparsable_json(self):
        report = validate_fill_response(self.fw, '{"results": [ {"rule_id": "R1", ')
        self.assertFalse(report.ok)
        self.assertIn("JSON の解析に失敗", report.errors[0])

    def test_numeric_string_is_converted_with_warning(self):
        fixed = [dict(r) for r in self.ok_results]
        fixed[4] = {"rule_id": fixed[4]["rule_id"], "actions": {"最終料金": "4,500"}}
        report = validate_fill_response(self.fw, json.dumps({"results": fixed}, ensure_ascii=False))
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.values["R5"]["最終料金"], 4500)
        self.assertTrue(report.warnings)

    def test_action_type_values(self):
        items = DecisionItems(["A"], [ActionItem("通知する", "action")])
        fw = build_framework(items)
        good = {"results": [{"rule_id": "R1", "actions": {"通知する": "X"}}, {"rule_id": "R2", "actions": {"通知する": "-"}}]}
        self.assertTrue(dt_framework.validate_fill_data(fw, good).ok)
        bad = {"results": [{"rule_id": "R1", "actions": {"通知する": "する"}}, {"rule_id": "R2", "actions": {"通知する": "-"}}]}
        self.assertFalse(dt_framework.validate_fill_data(fw, bad).ok)

    def test_invalid_output_is_not_treated_as_success(self):
        """形式不正のモデルは error になり、Markdown DT (content) を持たない."""
        fw = self.fw

        def responder(model, prompt):
            return fill_json(fw.rule_ids[:-1], price_for(fw))  # 1ルール欠落

        from services import step_runner

        with mock.patch.object(config_manager, "CACHE_DIR", Path(tempfile.mkdtemp())):
            result = step_runner.run_framework_generation(FakeClient(responder), "m/a", "A", "spec", fw)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "invalid_output")
        self.assertEqual(result.content, "")
        self.assertTrue(result.validation_errors)
        self.assertTrue(result.batches[0]["raw_response"])  # 生の応答は残る


# ---------------------------------------------------------------- テスト5
class TestComparison(unittest.TestCase):
    def setUp(self):
        self.fw = build_framework(parse_decision_items(EXTRACTED_JSON))

    def fill(self, key, value_for, ok=True):
        report = validate_fill_response(self.fw, fill_json(self.fw.rule_ids, value_for))
        return framework_comparison.ModelFill(key, key, ok, report.values, report.raw_values)

    def test_different_prices_detected_per_rule(self):
        base = price_for(self.fw)
        a = self.fill("A", base)
        b = self.fill("B", lambda rid: 9999 if rid == "R6" else base(rid))  # 料金が違う
        c = self.fill("C", lambda rid: NOT_APPLICABLE if rid == "R8" else ("4,500" if rid == "R3" else base(rid)))
        result = framework_comparison.compare_fills(self.fw, [a, b, c])
        by_id = {r.rule_id: r for r in result.rules}
        self.assertEqual(by_id["R6"].category, "value_mismatch")
        self.assertEqual(by_id["R6"].actions[0].values, {"A": 4000, "B": 9999, "C": 4000})
        self.assertEqual(by_id["R8"].category, "applicability_mismatch")
        self.assertEqual(by_id["R3"].category, "semantic")  # 4500 と "4,500"
        self.assertEqual(by_id["R1"].category, "exact")
        self.assertEqual(by_id["R7"].category, "exact")
        counts = result.counts()
        self.assertEqual(counts["semantic"], 1)
        self.assertEqual(counts["value_mismatch"], 1)
        self.assertEqual(counts["applicability_mismatch"], 1)

    def test_semantic_match(self):
        self.assertEqual(framework_comparison.classify_values([3750, "3,750円"]), "semantic")
        self.assertEqual(framework_comparison.classify_values([3750, 3750.0]), "semantic")
        self.assertEqual(framework_comparison.classify_values([3750, 3750]), "exact")
        self.assertEqual(framework_comparison.classify_values([3750, NOT_APPLICABLE]), "applicability_mismatch")
        self.assertEqual(framework_comparison.classify_values([3750, 4000]), "value_mismatch")

    def test_failed_models(self):
        base = price_for(self.fw)
        a = self.fill("A", base)
        bad = framework_comparison.ModelFill("B", "B", False, error="JSON 不正")
        result = framework_comparison.compare_fills(self.fw, [a, bad])
        self.assertEqual(result.failed_models, ["B"])
        self.assertTrue(all(r.category == "failed" for r in result.rules))

    def test_existing_comparison_reused_on_completed_markdown(self):
        """完成した Markdown DT は従来の比較機能でもルール単位で比較できる."""
        base = price_for(self.fw)
        md_a = dt_framework.to_markdown(self.fw, validate_fill_response(self.fw, fill_json(self.fw.rule_ids, base)).values)
        other = lambda rid: 9999 if rid == "R6" else base(rid)  # noqa: E731
        md_b = dt_framework.to_markdown(self.fw, validate_fill_response(self.fw, fill_json(self.fw.rule_ids, other)).values)
        tables = {"A": parse_decision_table(md_a), "B": parse_decision_table(md_b)}
        result = compare_tables(tables, align_labels(tables))
        self.assertEqual(len(result.groups), 8)  # 組合せの漏れ・重複がないので 8 グループ
        self.assertEqual(result.conflict_count, 1)


# ---------------------------------------------------- パイプライン (新方式)
class TestFrameworkPipeline(TempOutputsMixin, unittest.TestCase):
    def responder(self, model, prompt):
        if is_extract(prompt):
            return EXTRACTED_JSON
        ids = rule_ids_in_prompt(prompt)
        fw = build_framework(parse_decision_items(EXTRACTED_JSON))
        base = price_for(fw)
        if model == "m/b":
            return fill_json(ids, lambda rid: 9999 if rid == "R6" else base(rid))
        return fill_json(ids, base)

    def run_pipeline(self, client, options, models=("m/a", "m/b")):
        from models.experiment import Experiment

        exp = Experiment(requirements="テーマパークの料金仕様", extraction_model="m/x")
        pipeline.execute(client, exp, list(models), lambda m: m, options, lambda msg: None, threading.Event())
        return exp

    def test_same_framework_to_all_models_and_saved(self):
        client = FakeClient(self.responder)
        options = pipeline.RunOptions(method=METHOD_COMMON_FRAMEWORK, framework_batch_size=3, framework_max_tokens=12345)
        exp = self.run_pipeline(client, options)
        self.assertEqual(exp.framework_status, "ok")
        self.assertTrue(all(r.ok for r in exp.results), [r.error for r in exp.results])
        # 全モデルに同じ枠組み (同じプロンプト) が渡されている
        gen_calls = [c for c in client.calls if not is_extract(c[1])]
        by_model = {}
        for model, prompt, opts in gen_calls:
            by_model.setdefault(model, []).append(prompt)
            self.assertEqual(opts["max_tokens"], 12345)
        self.assertEqual(sorted(by_model["m/a"]), sorted(by_model["m/b"]))
        self.assertEqual(len(by_model["m/a"]), 3)  # 8 ルール / バッチ3件 = 3バッチ
        self.assertEqual(exp.framework_comparison["counts"]["value_mismatch"], 1)

        out = file_manager.save_experiment(exp)
        self.assertTrue(out.name.endswith(file_manager.FRAMEWORK_DIR_SUFFIX))
        record = json.loads((out / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(record["generation_method"], METHOD_COMMON_FRAMEWORK)
        self.assertEqual(record["generation_settings"]["framework_version"], dt_framework.FRAMEWORK_VERSION)
        self.assertIn("generate_framework", record["prompts"])
        self.assertEqual(len(record["framework"]["rules"]), 8)
        self.assertEqual(len(record["results"][0]["batches"]), 3)
        self.assertIn("R1", record["results"][0]["validated_results"])
        self.assertTrue((out / "framework.json").exists())
        self.assertTrue((out / "framework.md").exists())
        self.assertTrue((out / "raw_responses" / "m_a_batch1.txt").exists())
        self.assertIn("| 条件部 | 来場人数が1人である |", (out / "m_a.md").read_text(encoding="utf-8"))

    def test_too_many_conditions_saves_extraction_only(self):
        many = json.dumps({"conditions": [f"条件{i}" for i in range(7)], "actions": [{"name": "最終料金", "type": "value", "valueType": "int"}]}, ensure_ascii=False)
        client = FakeClient(lambda m, p: many if is_extract(p) else self.fail("生成は呼ばれないはず"))
        exp = self.run_pipeline(client, pipeline.RunOptions(method=METHOD_COMMON_FRAMEWORK))
        self.assertEqual(exp.framework_status, "too_many_conditions")
        self.assertEqual(exp.results, [])
        self.assertEqual(len(client.calls), 1)
        out = file_manager.save_experiment(exp)
        record = json.loads((out / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(record["framework"]["condition_count"], 7)
        self.assertTrue(record["decision_items"])

    def test_reuse_successful_results_and_rerun_failed_only(self):
        state = {"fail_b": True}

        def responder(model, prompt):
            if model == "m/b" and not is_extract(prompt) and state["fail_b"]:
                return OpenRouterError("HTTP 500: server error", 500)
            return self.responder(model, prompt)

        options = pipeline.RunOptions(method=METHOD_COMMON_FRAMEWORK, reuse_cached_results=True)
        client = FakeClient(responder)
        first = self.run_pipeline(client, options)
        self.assertEqual([r.ok for r in first.results], [True, False])

        state["fail_b"] = False
        client2 = FakeClient(responder)
        second = self.run_pipeline(client2, options)
        self.assertEqual([r.ok for r in second.results], [True, True])
        self.assertTrue(second.extraction_reused_from_cache)
        self.assertTrue(second.results[0].reused_from_cache)
        self.assertFalse(second.results[1].reused_from_cache)
        self.assertEqual({c[0] for c in client2.calls}, {"m/b"})  # 失敗したモデルだけ再実行

        # プロンプト (生成方式) が違えば再利用しない
        client3 = FakeClient(lambda m, p: EXTRACTED_JSON if is_extract(p) else CONVENTIONAL_MD)
        third = self.run_pipeline(client3, pipeline.RunOptions(method=METHOD_CONVENTIONAL, reuse_cached_results=True))
        self.assertTrue(third.extraction_reused_from_cache)  # 抽出は同じ条件なので再利用
        self.assertFalse(any(r.reused_from_cache for r in third.results))
        self.assertEqual(len(client3.calls), 2)

        # 要求仕様が違えば抽出も再利用しない
        from models.experiment import Experiment

        client4 = FakeClient(self.responder)
        exp = Experiment(requirements="別の仕様", extraction_model="m/x")
        pipeline.execute(client4, exp, ["m/a"], lambda m: m, options, lambda msg: None, threading.Event())
        self.assertFalse(exp.extraction_reused_from_cache)

    def test_reuse_disabled(self):
        options = pipeline.RunOptions(method=METHOD_COMMON_FRAMEWORK, reuse_cached_results=False)
        self.run_pipeline(FakeClient(self.responder), options)
        client = FakeClient(self.responder)
        self.run_pipeline(client, options)
        self.assertEqual(len(client.calls), 3)  # 抽出1 + 生成2


# ---------------------------------------------------------------- テスト6
class TestConventionalCompatibility(TempOutputsMixin, unittest.TestCase):
    def test_conventional_pipeline(self):
        from models.experiment import Experiment

        client = FakeClient(lambda m, p: EXTRACTED_JSON if is_extract(p) else "```markdown\n" + CONVENTIONAL_MD + "```")
        exp = Experiment(requirements="仕様", extraction_model="m/x")
        options = pipeline.RunOptions(method=METHOD_CONVENTIONAL)
        pipeline.execute(client, exp, ["m/a", "m/b"], lambda m: m, options, lambda msg: None, threading.Event())
        self.assertTrue(all(r.ok for r in exp.results))
        self.assertEqual(exp.framework, None)
        self.assertTrue(any("Markdown形式のデシジョンテーブル" in c[1] for c in client.calls))
        self.assertTrue(all(c[2]["max_tokens"] is None for c in client.calls))  # 従来どおり上限を送らない
        out = file_manager.save_experiment(exp)
        self.assertFalse(out.name.endswith(file_manager.FRAMEWORK_DIR_SUFFIX))
        record = json.loads((out / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(record["generation_method"], METHOD_CONVENTIONAL)
        for key in ("content", "status", "elapsed_sec", "prompt_tokens", "output_file"):
            self.assertIn(key, record["results"][0])  # 既存のキーは維持
        self.assertNotIn("validated_results", record["results"][0])
        self.assertFalse((out / "framework.json").exists())
        table = parse_decision_table((out / "m_a.md").read_text(encoding="utf-8"))
        self.assertEqual(table.rule_ids, ["R1", "R2"])

    def test_old_settings_file_still_loads(self):
        tmp = self.tmp / "settings.json"
        tmp.write_text(json.dumps({"extraction_model": "a", "generation_models": ["b"]}), encoding="utf-8")
        with mock.patch.object(config_manager, "SETTINGS_PATH", tmp):
            settings = config_manager.load_settings()
        self.assertEqual(settings["generation_method"], METHOD_CONVENTIONAL)
        self.assertEqual(settings["framework_max_conditions"], 6)


# ---------------------------------------------------------------- テスト7
class TestKeyLimit(TempOutputsMixin, unittest.TestCase):
    def test_detection(self):
        self.assertTrue(is_key_limit_error(403, "HTTP 403: Key limit exceeded (total limit)"))
        self.assertTrue(is_key_limit_error(403, "HTTP 403: total limit exceeded"))
        self.assertTrue(is_key_limit_error(402, "HTTP 402: Insufficient credits"))
        self.assertTrue(is_key_limit_error(402, ""))
        self.assertFalse(is_key_limit_error(403, "HTTP 403: Your input was flagged by moderation"))
        self.assertFalse(is_key_limit_error(403, "HTTP 403: model not allowed"))
        self.assertFalse(is_key_limit_error(429, "HTTP 429: Rate limit exceeded"))
        self.assertFalse(is_key_limit_error(500, "HTTP 500: Internal error"))
        self.assertIsInstance(make_api_error(403, "Key limit exceeded"), KeyLimitError)
        self.assertNotIsInstance(make_api_error(403, "Forbidden"), KeyLimitError)

    def test_client_raises_key_limit_from_http_response(self):
        response = mock.Mock(status_code=403, text="")
        response.json.return_value = {"error": {"message": "Key limit exceeded", "code": 403}}
        client = OpenRouterClient(api_key="dummy")
        with mock.patch("services.openrouter_client.requests.post", return_value=response):
            with self.assertRaises(KeyLimitError):
                client.chat("m/a", "hi")
        response.json.return_value = {"error": {"message": "Forbidden by policy", "code": 403}}
        with mock.patch("services.openrouter_client.requests.post", return_value=response):
            with self.assertRaises(OpenRouterError) as ctx:
                client.chat("m/a", "hi")
            self.assertNotIsInstance(ctx.exception, KeyLimitError)

    def test_max_tokens_is_not_fixed(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}
        client = OpenRouterClient(api_key="dummy")
        with mock.patch("services.openrouter_client.requests.post", return_value=response) as post:
            client.chat("m/a", "hi")
            self.assertNotIn("max_tokens", post.call_args.kwargs["json"])
            client.chat("m/a", "hi", max_tokens=8000)
            self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 8000)

    def _run_batch(self, responder, sources, method=METHOD_COMMON_FRAMEWORK, parallel=1):
        from services.batch_runner import BatchWorker

        client = FakeClient(responder)
        worker = BatchWorker(
            client=client, sources=sources, extraction_model="m/x", generation_models=["m/a", "m/b"],
            max_parallel_specs=parallel, options=pipeline.RunOptions(method=method, reuse_cached_results=False),
        )
        finished, failed, logs = [], [], []
        worker.finished.connect(lambda exps: finished.extend(exps))
        worker.file_failed.connect(lambda label, msg: failed.append((label, msg)))
        worker.progress.connect(logs.append)
        worker.run()
        # ワーカースレッドから emit されたシグナルはキュー経由で届くので処理する
        QCoreApplication.processEvents()
        return worker, client, finished, failed, logs

    def test_batch_stops_after_key_limit(self):
        sources = [SpecSource(name=f"spec{i}", label=f"spec{i}", path=Path(f"spec{i}.txt"), requirements=f"仕様{i}") for i in range(1, 6)]
        good = TestFrameworkPipeline.responder

        def responder(model, prompt):
            if "仕様2" in prompt and model == "m/b":
                import time

                time.sleep(0.3)  # m/a (進行中のジョブ) が先に終わるようにする
                return KeyLimitError("HTTP 403: Key limit exceeded", 403)
            return good(None, model, prompt)

        worker, client, finished, failed, logs = self._run_batch(responder, sources)
        self.assertTrue(worker.stop_event.is_set())
        # 仕様3以降にはリクエストを送らない
        self.assertFalse(any("仕様3" in c[1] or "仕様4" in c[1] or "仕様5" in c[1] for c in client.calls))
        self.assertEqual([e.source_name for e in finished], ["spec1", "spec2"])  # 進行中の仕様2も保存
        spec2 = finished[1]
        self.assertTrue(spec2.aborted_reason)
        self.assertEqual({r.model_id: r.ok for r in spec2.results}, {"m/a": True, "m/b": False})
        self.assertEqual(spec2.results[1].error_kind, "key_limit")
        summary = json.loads((worker.batch_dir / "batch_summary.json").read_text(encoding="utf-8"))
        statuses = [s["status"] for s in summary["specs"]]
        self.assertEqual(statuses, ["done", "partial_key_limit", "skipped_key_limit", "skipped_key_limit", "skipped_key_limit"])
        self.assertTrue(summary["aborted_reason"])
        self.assertTrue(worker.batch_dir.name.endswith(file_manager.FRAMEWORK_DIR_SUFFIX))
        self.assertTrue(any("[停止]" in line for line in logs))

    def test_batch_stops_when_extraction_hits_limit(self):
        sources = [SpecSource(name=f"s{i}", label=f"s{i}", path=Path(f"s{i}.txt"), requirements=f"仕様{i}") for i in range(1, 4)]

        def responder(model, prompt):
            if is_extract(prompt) and "仕様1" in prompt:
                return make_api_error(402, "HTTP 402: Insufficient credits")
            return CONVENTIONAL_MD

        worker, client, finished, failed, logs = self._run_batch(responder, sources, method=METHOD_CONVENTIONAL)
        self.assertEqual(len(client.calls), 1)  # 最初の抽出だけ送信して停止
        self.assertEqual(finished, [])
        self.assertEqual(len(failed), 3)

    def test_generic_403_does_not_stop_batch(self):
        sources = [SpecSource(name=f"s{i}", label=f"s{i}", path=Path(f"s{i}.txt"), requirements=f"仕様{i}") for i in range(1, 4)]

        def responder(model, prompt):
            if is_extract(prompt):
                return EXTRACTED_JSON
            if model == "m/b":
                return make_api_error(403, "HTTP 403: moderation flagged")
            return CONVENTIONAL_MD

        worker, client, finished, failed, logs = self._run_batch(responder, sources, method=METHOD_CONVENTIONAL)
        self.assertFalse(worker.stop_event.is_set())
        self.assertEqual(len(finished), 3)


if __name__ == "__main__":
    unittest.main()
