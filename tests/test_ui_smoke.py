"""画面の簡易動作確認 (オフスクリーン描画・API 不使用).

新方式・従来方式の結果表示、設定ダイアログ・一括実行ダイアログ・履歴ダイアログが
例外なく組み立てられることを確認する。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from models.experiment import METHOD_COMMON_FRAMEWORK, METHOD_CONVENTIONAL, Experiment  # noqa: E402
from services import pipeline  # noqa: E402
from tests import test_common_framework as tcf  # noqa: E402
from utils import config_manager, file_manager  # noqa: E402

_APP = QApplication.instance() or QApplication([])


class TestUiSmoke(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [
            mock.patch.object(file_manager, "OUTPUTS_DIR", self.tmp / "outputs"),
            mock.patch.object(config_manager, "CACHE_DIR", self.tmp / "outputs" / "_cache"),
            mock.patch.object(config_manager, "SETTINGS_PATH", self.tmp / "settings.json"),
            mock.patch.object(config_manager, "CONFIG_DIR", self.tmp),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _experiment(self, method):
        if method == METHOD_COMMON_FRAMEWORK:
            client = tcf.FakeClient(lambda m, p: tcf.TestFrameworkPipeline.responder(None, m, p))
        else:
            client = tcf.FakeClient(lambda m, p: tcf.EXTRACTED_JSON if tcf.is_extract(p) else tcf.CONVENTIONAL_MD)
        exp = Experiment(requirements="仕様", extraction_model="m/x")
        pipeline.execute(client, exp, ["m/a", "m/b"], lambda m: m, pipeline.RunOptions(method=method),
                         lambda msg: None, threading.Event())
        file_manager.save_experiment(exp)
        return exp

    def test_main_window_and_dialogs(self):
        from ui.batch_dialog import BatchDialog
        from ui.history_dialog import HistoryDialog
        from ui.main_window import MainWindow
        from ui.settings_dialog import SettingsDialog

        with mock.patch.object(MainWindow, "_start_model_fetch"):
            window = MainWindow()
        # 方式の切り替え (設定は一時ファイルへ保存される)
        window.method_combo.setCurrentIndex(window.method_combo.findData(METHOD_COMMON_FRAMEWORK))
        self.assertEqual(config_manager.load_settings()["generation_method"], METHOD_COMMON_FRAMEWORK)

        fw_exp = self._experiment(METHOD_COMMON_FRAMEWORK)
        window._show_results(fw_exp)
        titles = [window.result_tabs.tabText(i) for i in range(window.result_tabs.count())]
        self.assertEqual(titles[:4], ["共通DT比較", "共通DT枠組み", "比較 (縦)", "テーブル一覧 (縦並び)"])
        self.assertIn("比較 (横)", titles)
        view = window.result_tabs.widget(0)
        self.assertEqual(view.table.rowCount(), 8)
        view.diff_only_check.setChecked(True)
        self.assertEqual(view.table.rowCount(), 1)

        conv_exp = self._experiment(METHOD_CONVENTIONAL)
        window._show_results(conv_exp)
        titles = [window.result_tabs.tabText(i) for i in range(window.result_tabs.count())]
        self.assertEqual(titles[0], "比較 (縦)")
        self.assertNotIn("共通DT比較", titles)

        settings_dialog = SettingsDialog(window.client, window.settings, [], window)
        selected = settings_dialog.selected_settings()
        self.assertEqual(selected["framework_max_conditions"], 6)
        self.assertIn("framework_max_tokens", selected)

        batch = BatchDialog(window.client, window.settings, [], window)
        self.assertEqual(batch.method_combo.currentData(), METHOD_COMMON_FRAMEWORK)

        history = HistoryDialog(window)
        self.assertEqual(history.run_list.count(), 2)
        labels = [history.run_list.item(i).text() for i in range(history.run_list.count())]
        self.assertEqual(sum("[共通DT]" in label for label in labels), 1)
        for i in range(history.run_list.count()):
            history.run_list.setCurrentRow(i)
            self.assertGreater(history.result_tabs.count(), 0)

    def test_too_many_conditions_tab(self):
        from ui.comparison_tabs import add_framework_tabs
        from PySide6.QtWidgets import QTabWidget

        tabs = QTabWidget()
        add_framework_tabs(tabs, {"generation_method": METHOD_COMMON_FRAMEWORK, "framework_status": "too_many_conditions",
                                  "framework_error": "条件数が 7 個あり"})
        self.assertEqual(tabs.tabText(0), "⚠ 共通DT")


if __name__ == "__main__":
    unittest.main()
