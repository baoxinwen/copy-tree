import json
import os
import re
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from copytree.formatter import format_output  # noqa: E402
from copytree.scanner import build_tree_text, scan_directory  # noqa: E402

from loguru import logger as _quiet_logger  # noqa: E402

_quiet_logger.remove()  # 保持测试输出干净


class FormatterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("test_runtime_formatter")
        if self.tmp.exists():
            shutil.rmtree(self.tmp)
        self.tmp.mkdir()

    def tearDown(self):
        if self.tmp.exists():
            shutil.rmtree(self.tmp)

    def test_markdown_list_output_uses_nested_bullets(self):
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "main.py").write_text("print('ok')", encoding="utf-8")

        result = scan_directory(str(self.tmp), max_files=-1)
        output = format_output(build_tree_text(result), "markdown-list", result=result)

        self.assertIn("- \U0001F4C1 test_runtime_formatter/", output)
        self.assertIn("  - \U0001F4C1 src/", output)
        self.assertIn("    - main.py", output)

    def test_json_output_contains_tree_and_stats(self):
        (self.tmp / "a.txt").write_text("a", encoding="utf-8")

        result = scan_directory(str(self.tmp), max_files=-1, show_size=True)
        output = format_output(
            build_tree_text(result, show_size=True),
            "json",
            result=result,
            show_size=True,
        )
        payload = json.loads(output)

        self.assertEqual(payload["stats"]["displayedFiles"], 1)
        self.assertEqual(payload["stats"]["scannedFiles"], 1)
        self.assertTrue(payload["stats"]["fileCountsAreComplete"])
        self.assertNotIn("matchedFiles", payload["stats"])
        self.assertEqual(payload["root"]["children"][0]["name"], "a.txt")
        self.assertEqual(payload["root"]["children"][0]["sizeBytes"], 1)

    def test_json_path_strips_long_path_prefix(self):
        base = str(self.tmp.resolve())
        seg = "L" * 210
        long_dir = os.path.join(base, seg)
        prefixed = "\\\\?\\" + long_dir
        os.makedirs(prefixed, exist_ok=True)
        with open(prefixed + "\\f.txt", "w", encoding="utf-8") as f:
            f.write("x")

        result = scan_directory(long_dir, max_files=-1)
        output = format_output(build_tree_text(result), "json", result=result)
        payload = json.loads(output)
        child = payload["root"]["children"][0]
        self.assertFalse(child["path"].startswith("\\\\?\\"))
        self.assertTrue(child["path"].endswith("f.txt"))

    def test_names_format_outputs_real_name_for_long_names(self):
        # 回归（评审 I-2）：names 契约是"每行一个文件名"，供脚本按名定位文件，
        # 必须与 paths/JSON 一致输出真实名，不能是 display 截断名。
        long_name = "f" * (80 + 9) + ".txt"
        (self.tmp / long_name).write_text("x", encoding="utf-8")

        result = scan_directory(str(self.tmp), max_files=-1)
        names_output = format_output(build_tree_text(result), "names", result=result)
        paths_output = format_output(build_tree_text(result), "paths", result=result)

        self.assertEqual(names_output, long_name)
        self.assertTrue(paths_output.endswith(long_name))

    def test_json_truncation_omits_hidden_by_max_files(self):
        for i in range(3):
            (self.tmp / f"{i}.txt").write_text("x", encoding="utf-8")

        result = scan_directory(str(self.tmp), max_files=1, max_items_per_level=2)
        output = format_output(build_tree_text(result), "json", result=result)
        payload = json.loads(output)

        self.assertTrue(payload["stats"]["truncated"])
        self.assertIn("truncation", payload)
        self.assertNotIn("hiddenByMaxFiles", payload["truncation"])
        self.assertIn("truncatedLevels", payload["truncation"])
        self.assertIn("hiddenItemsByLevel", payload["truncation"])

    def test_json_modified_time_has_timezone_offset(self):
        (self.tmp / "a.txt").write_text("x", encoding="utf-8")

        result = scan_directory(str(self.tmp), max_files=-1, show_time=True)
        output = format_output(
            build_tree_text(result, show_time=True),
            "json",
            result=result,
            show_time=True,
        )
        payload = json.loads(output)
        child = payload["root"]["children"][0]
        self.assertIn("modifiedTime", child)
        self.assertRegex(child["modifiedTime"], r"[+-]\d{2}:\d{2}$")

    def test_json_omits_size_bytes_when_unknown(self):
        (self.tmp / "a.txt").write_text("a", encoding="utf-8")

        result = scan_directory(str(self.tmp), max_files=-1, show_size=True)
        result.root.children[0].size = None  # 模拟 stat 读取失败
        output = format_output(
            build_tree_text(result, show_size=True),
            "json",
            result=result,
            show_size=True,
        )
        payload = json.loads(output)

        self.assertNotIn("sizeBytes", payload["root"]["children"][0])


if __name__ == "__main__":
    unittest.main()
