import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from copytree.constants import (  # noqa: E402
    GENERATED_OUTPUT_FILENAMES,
    SOURCE_CODE_EXTENSIONS,
    SOURCE_CODE_FILENAMES,
)
from copytree.scanner import build_tree_text, describe_truncation, normalize_path, _root_display_name, scan_directory  # noqa: E402

from loguru import logger as _quiet_logger  # noqa: E402

_quiet_logger.remove()  # 保持测试输出干净


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("test_runtime_scanner")
        if self.tmp.exists():
            shutil.rmtree(self.tmp)
        self.tmp.mkdir()

    def tearDown(self):
        if self.tmp.exists():
            shutil.rmtree(self.tmp)

    def test_max_files_minus_one_is_unlimited(self):
        (self.tmp / "a.txt").write_text("a", encoding="utf-8")
        (self.tmp / "b.txt").write_text("b", encoding="utf-8")

        result = scan_directory(str(self.tmp), max_files=-1)

        self.assertFalse(result.truncated)
        self.assertEqual(result.total_files, 2)
        self.assertEqual(result.total_files_actual, 2)

    def test_max_files_truncation_reports_hidden_files(self):
        (self.tmp / "a.txt").write_text("a", encoding="utf-8")
        (self.tmp / "b.txt").write_text("b", encoding="utf-8")

        result = scan_directory(str(self.tmp), max_files=1)

        self.assertTrue(result.truncated)
        self.assertTrue(result.scan_stopped)
        self.assertEqual(result.total_files, 1)
        self.assertIn("达到 maxFiles=1", describe_truncation(result))

    def test_max_files_hides_unscanned_sibling_directories(self):
        for name in ("a", "b", "c"):
            child = self.tmp / name
            child.mkdir()
            (child / f"{name}.txt").write_text(name, encoding="utf-8")

        result = scan_directory(str(self.tmp), max_files=1)
        tree_text = build_tree_text(result)

        self.assertIn("a.txt", tree_text)
        self.assertNotIn("📁 b/", tree_text)
        self.assertNotIn("📁 c/", tree_text)
        self.assertTrue(result.scan_stopped)

    def test_level_truncation_reports_hidden_items(self):
        for i in range(3):
            (self.tmp / f"{i}.txt").write_text("x", encoding="utf-8")

        result = scan_directory(str(self.tmp), max_files=-1, max_items_per_level=2)
        tree_text = build_tree_text(result)

        self.assertTrue(result.truncated)
        self.assertEqual(result.truncated_levels, 1)
        self.assertEqual(result.truncated_items, 2)
        self.assertIn("maxItemsPerLevel", describe_truncation(result))
        self.assertIn("输出已截断", tree_text)
        self.assertIn("└── (还有 2 项未显示)", tree_text)

    def test_long_directory_display_name_keeps_real_path_for_recursion(self):
        long_name = "a" * 90
        child_dir = self.tmp / long_name
        child_dir.mkdir()
        (child_dir / "inside.txt").write_text("x", encoding="utf-8")

        result = scan_directory(str(self.tmp), max_files=-1)

        self.assertEqual(result.total_files, 1)
        self.assertEqual(result.total_dirs, 1)
        self.assertEqual(result.root.children[0].children[0].name, "inside.txt")

    def test_source_filter_includes_extensionless_source_filenames(self):
        (self.tmp / "Dockerfile").write_text("FROM scratch", encoding="utf-8")
        (self.tmp / "Makefile").write_text("all:", encoding="utf-8")
        (self.tmp / "photo.png").write_text("x", encoding="utf-8")

        result = scan_directory(
            str(self.tmp),
            include_ext=SOURCE_CODE_EXTENSIONS,
            include_names=SOURCE_CODE_FILENAMES,
        )

        names = {child.name for child in result.root.children}
        self.assertEqual(names, {"Dockerfile", "Makefile"})

    def test_empty_extension_filter_matches_no_files(self):
        (self.tmp / "a.py").write_text("x", encoding="utf-8")

        result = scan_directory(str(self.tmp), include_ext=set())

        self.assertEqual(result.total_files, 0)
        self.assertEqual(result.root.children, [])

    def test_prune_empty_dirs_after_active_filtering(self):
        child_dir = self.tmp / "logs"
        child_dir.mkdir()
        (child_dir / "debug.log").write_text("x", encoding="utf-8")

        result = scan_directory(
            str(self.tmp),
            exclude_files={"debug.log"},
            prune_empty_dirs=True,
        )

        self.assertEqual(result.root.children, [])

    def test_prune_keeps_directories_that_were_empty_before_filtering(self):
        empty_dir = self.tmp / "empty"
        empty_dir.mkdir()
        logs_dir = self.tmp / "logs"
        logs_dir.mkdir()
        (logs_dir / "debug.log").write_text("x", encoding="utf-8")

        result = scan_directory(
            str(self.tmp),
            exclude_files={"debug.log"},
            prune_empty_dirs=True,
        )

        self.assertEqual([child.name for child in result.root.children], ["empty"])

    def test_generated_output_names_can_be_excluded(self):
        (self.tmp / "directory_tree.txt").write_text("old", encoding="utf-8")
        (self.tmp / "keep.txt").write_text("keep", encoding="utf-8")

        result = scan_directory(
            str(self.tmp),
            exclude_files=set(GENERATED_OUTPUT_FILENAMES),
            max_files=-1,
        )

        names = {child.name for child in result.root.children}
        self.assertEqual(names, {"keep.txt"})

    def test_long_unc_path_uses_unc_prefix(self):
        path = "\\\\server\\share\\" + ("a" * 260)

        normalized = normalize_path(path)

        self.assertTrue(normalized.startswith("\\\\?\\UNC\\server\\share\\"))

    def test_long_relative_path_is_made_absolute_before_prefix(self):
        path = "a" * 260

        normalized = normalize_path(path)

        self.assertTrue(normalized.startswith("\\\\?\\"))
        self.assertTrue(Path(normalized[4:]).is_absolute())

    def test_drive_root_display_name_is_not_empty(self):
        self.assertEqual(_root_display_name("C:\\"), "C:")

    def test_extreme_depth_is_truncated_before_recursion_limit(self):
        current = self.tmp
        for i in range(15):
            current = current / f"d{i}"
            current.mkdir()

        with mock.patch("copytree.scanner.sys.getrecursionlimit", return_value=25):
            result = scan_directory(str(self.tmp), max_files=-1)

        tree_text = build_tree_text(result)
        self.assertTrue(result.depth_limited)
        self.assertIn("目录层级过深", describe_truncation(result))
        self.assertIn("目录层级过深，后续未扫描", tree_text)

    def test_negative_max_depth_behaves_like_zero(self):
        (self.tmp / "a").mkdir()
        (self.tmp / "a" / "b.txt").write_text("x", encoding="utf-8")

        result_neg = scan_directory(str(self.tmp), max_files=-1, max_depth=-2)
        result_zero = scan_directory(str(self.tmp), max_files=-1, max_depth=0)

        self.assertEqual(result_neg.total_files, 0)
        self.assertEqual(result_neg.total_dirs, 0)
        self.assertEqual(result_neg.root.children, [])
        self.assertFalse(result_neg.truncated)
        self.assertEqual(result_neg.total_files, result_zero.total_files)
        self.assertEqual(result_neg.total_dirs, result_zero.total_dirs)

    def test_generic_oserror_marks_directory_locked(self):
        real_scandir = os.scandir
        calls = {"n": 0}

        def flaky(path):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_scandir(path)
            raise OSError(5, "generic failure")

        (self.tmp / "x").mkdir()

        with mock.patch("copytree.scanner.os.scandir", flaky):
            result = scan_directory(str(self.tmp), max_files=-1)

        self.assertEqual(result.total_dirs, 1)
        self.assertTrue(result.root.children[0].access_denied)

    def test_unscanned_items_count_all_remaining_sibling_dirs(self):
        # 回归（评审 M-1）：maxFiles 早停发生在子目录递归中时，首个未递归
        # 目录之后的兄弟目录也必须计入 unscanned_items，尾部提示不得低估。
        root = self.tmp
        a = root / "a"
        a.mkdir()
        for i in range(3):
            (a / f"f{i}.txt").write_text("x", encoding="utf-8")
        for d in ("b", "c"):
            (root / d).mkdir()
            (root / d / "inner.txt").write_text("x", encoding="utf-8")

        result = scan_directory(str(root), max_files=2)

        self.assertTrue(result.scan_stopped)
        self.assertEqual(result.unscanned_items, 2)

    def test_exclude_patterns_match_real_names_for_long_dirs(self):
        # 回归（评审 M-2）：目录栈必须存真实名——display 截断只影响展示，
        # 否则超过 MAX_NAME_LENGTH 的目录名会让 rel_path 匹配静默失效。
        long_dir = "d" * (80 + 10)
        target = self.tmp / long_dir
        target.mkdir()
        (target / "x.log").write_text("x", encoding="utf-8")

        result = scan_directory(
            str(self.tmp), exclude_patterns={long_dir + "/*.log"}
        )

        self.assertEqual(result.total_files, 0)

    def test_depth_guard_scales_with_recursion_limit(self):
        # 回归：阈值必须按“每层约 2 个栈帧”折算，否则默认递归限制下
        # 保护不会先于 RecursionError 触发（历史缺陷，~500 层即崩溃）。
        # 建目录走 \\?\ 前缀（评审 I-8）：不依赖系统 LongPathsEnabled 策略，
        # 默认配置的 Windows 上同样可跑，消除环境依赖假红。
        import os

        current = os.path.abspath(str(self.tmp))
        for i in range(120):
            current = os.path.join(current, f"d{i}")
            os.mkdir("\\\\?\\" + current if os.name == "nt" else current)

        with mock.patch("copytree.scanner.sys.getrecursionlimit", return_value=300):
            result = scan_directory(str(self.tmp), max_files=-1)

        self.assertTrue(result.depth_limited)


if __name__ == "__main__":
    unittest.main()
