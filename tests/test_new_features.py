import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from copytree import config  # noqa: E402
from copytree.notify import _truncate_utf16  # noqa: E402
from copytree.scanner import describe_truncation, scan_directory  # noqa: E402

from loguru import logger as _quiet_logger  # noqa: E402

_quiet_logger.remove()


class TruncateUtf16Tests(unittest.TestCase):
    def test_ascii_unchanged(self):
        self.assertEqual(_truncate_utf16("abc", 10), "abc")

    def test_truncates_by_utf16_units(self):
        # emoji 每字符占 2 个 UTF-16 单元
        text = "📁" * 200
        out = _truncate_utf16(text, 255)
        self.assertLessEqual(len(out.encode("utf-16-le")) // 2, 255)

    def test_never_splits_surrogate_pair(self):
        out = _truncate_utf16("a📁b", 2)  # "a📁" 占 3 单元 > 2
        self.assertEqual(out, "a")

    def test_exact_boundary(self):
        out = _truncate_utf16("a📁", 3)
        self.assertEqual(out, "a📁")


class ConfigNewKeysTests(unittest.TestCase):
    def test_defaults_contain_new_keys(self):
        for key in ("excludePatterns", "showFileTime", "respectGitignore", "enableTray"):
            self.assertIn(key, config._DEFAULTS)

    def test_invalid_list_type_warns(self):
        warnings = self._load_with({"excludePatterns": "not-a-list"})
        self.assertTrue(any("excludePatterns" in w for w in warnings))

    def test_invalid_bool_warns(self):
        for key in ("showFileTime", "respectGitignore", "enableTray"):
            warnings = self._load_with({key: "yes"})
            self.assertTrue(any(key in w for w in warnings))

    def test_valid_values_accepted(self):
        values = {
            "excludePatterns": ["*.min.js", "dist/*"],
            "showFileTime": True,
            "respectGitignore": True,
            "enableTray": False,
        }
        warnings = self._load_with(values, capture_config=True)
        self.assertEqual(warnings, [])
        for key, expected in values.items():
            self.assertEqual(self._config[key], expected)

    def _load_with(self, user_config, capture_config=False):
        config._CONFIG_WARNINGS = []
        fake = mock.mock_open(read_data=json.dumps(user_config))
        with mock.patch("builtins.open", fake):
            loaded = config.load_config()
        if capture_config:
            self._config = loaded
        return config.get_config_warnings()

    def setUp(self):
        self._config = {}


class TruncationAccountingTests(unittest.TestCase):
    def test_describe_reports_unscanned_items(self):
        # 评审 M-9：原实现无任何断言（不崩溃即过）。max_files=1 时第二个
        # 子目录未递归，describe 必须报告有未显示项。
        import os
        import shutil
        import tempfile

        root = tempfile.mkdtemp(prefix="ct_unscanned_")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        for d in ("a", "b"):
            os.makedirs(os.path.join(root, d))
            with open(os.path.join(root, d, "1.txt"), "w", encoding="utf-8") as f:
                f.write("x")

        result = scan_directory(root, max_files=1)
        self.assertGreaterEqual(result.unscanned_items, 1)
        text = describe_truncation(result)
        self.assertIn("另有", text)
        self.assertIn("未显示", text)

    def test_unscanned_items_surface_in_message(self):
        import copytree.scanner as scanner

        result = scanner.ScanResult(root=scanner.TreeEntry(name="x", is_dir=True))
        result.truncated = True
        result.scan_stopped = True
        result.max_files_limit = 2000
        result.unscanned_items = 3
        text = describe_truncation(result)
        self.assertIn("另有 3 项未显示", text)


if __name__ == "__main__":
    unittest.main()
