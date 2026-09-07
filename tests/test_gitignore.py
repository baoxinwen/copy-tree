import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from copytree.gitignore import GitignoreRuleSet, GitignoreStack, _translate_glob  # noqa: E402


def make_ruleset(text: str) -> GitignoreRuleSet:
    """构造规则集：文本经由真实解析路径（打开文件被 mock 掉）。"""
    from unittest import mock

    with mock.patch("builtins.open", mock.mock_open(read_data=text)):
        return GitignoreRuleSet("ignored-path")


class TranslateGlobTests(unittest.TestCase):
    def test_star_stays_within_segment(self):
        self.assertEqual(_translate_glob("*.log"), r"[^/]*\.log")

    def test_double_star_slash_matches_zero_or_more_dirs(self):
        # git 语义：`a/**/b` 中 `/**/` 可匹配零个目录，即 a/b 也命中
        self.assertEqual(_translate_glob("a/**/b"), r"a/(?:[^/]*/)*b")
        self.assertEqual(_translate_glob("**/foo"), r"(?:[^/]*/)*foo")

    def test_trailing_double_star_keeps_cross_segment(self):
        # 结尾 `/**` 表示“目录内全部内容”，保持跨段通配
        self.assertEqual(_translate_glob("a/**"), r"a/.*")

    def test_question_mark(self):
        self.assertEqual(_translate_glob("file?.txt"), r"file[^/]\.txt")

    def test_regex_special_chars_escaped(self):
        self.assertEqual(_translate_glob("a+b.txt"), r"a\+b\.txt")

    def test_empty_after_strip(self):
        self.assertEqual(_translate_glob("/"), "")


class RuleSetTests(unittest.TestCase):
    def test_comment_and_blank_ignored(self):
        rs = make_ruleset("# 注释\n\n   \n*.log\n")
        self.assertEqual(len(rs.rules), 1)

    def test_unanchored_matches_any_level(self):
        rs = make_ruleset("*.log\n")
        self.assertIs(rs.match("a.log", False), True)
        self.assertIs(rs.match("x/y/a.log", False), True)
        self.assertIs(rs.match("a.logx", False), None)

    def test_negation(self):
        rs = make_ruleset("*.log\n!important.log\n")
        self.assertIs(rs.match("a.log", False), True)
        self.assertIs(rs.match("important.log", False), False)

    def test_anchored_with_inner_slash(self):
        rs = make_ruleset("/build\n")
        self.assertIs(rs.match("build/out.bin", True), True)
        self.assertIs(rs.match("lib/build/x", True), None)

    def test_dir_only(self):
        rs = make_ruleset("temp/\n")
        self.assertIs(rs.match("temp", True), True)
        self.assertIs(rs.match("a/temp", True), True)
        self.assertIs(rs.match("temp", False), None)

    def test_double_star_pattern(self):
        rs = make_ruleset("docs/**/*.md\n")
        self.assertIs(rs.match("docs/guide/index.md", False), True)
        self.assertIs(rs.match("docs/a/b/c.md", False), True)
        self.assertIs(rs.match("docs/readme.txt", False), None)

    def test_leading_double_star_slash_matches_root_level(self):
        # git：`**/foo` 匹配任意层级（含根级）；`**/*.log` 过滤根级日志
        rs = make_ruleset("**/*.log\n")
        self.assertIs(rs.match("a.log", False), True)
        self.assertIs(rs.match("x/y/a.log", False), True)

    def test_double_star_slash_bare_name_matches_root(self):
        rs = make_ruleset("**/foo\n")
        self.assertIs(rs.match("foo", True), True)
        self.assertIs(rs.match("x/foo", True), True)

    def test_double_star_slash_between_segments_matches_zero_dirs(self):
        # git：`a/**/b` 等价匹配 a/b（零个目录）、a/x/b、a/x/y/b
        rs = make_ruleset("a/**/b\n")
        self.assertIs(rs.match("a/b", True), True)
        self.assertIs(rs.match("a/x/b", True), True)
        self.assertIs(rs.match("a/x/y/b", True), True)
        self.assertIs(rs.match("a/bc", True), None)

    def test_bare_name_matches_file_and_dir(self):
        rs = make_ruleset("node_modules\n")
        self.assertIs(rs.match("x/node_modules", True), True)
        self.assertIs(rs.match("x/node_modules", False), True)

    def test_dir_only_rule_does_not_match_file(self):
        rs = make_ruleset("node_modules/\n")
        self.assertIs(rs.match("x/node_modules", True), True)
        self.assertIs(rs.match("x/node_modules", False), None)

    def test_last_matching_rule_wins(self):
        rs = make_ruleset("*.log\n!important.log\nreally.log\n")
        self.assertIs(rs.match("really.log", False), True)
        self.assertIs(rs.match("important.log", False), False)


class StackCascadeTests(unittest.TestCase):
    def test_inner_ruleset_overrides_outer(self):
        import os
        import tempfile

        root = tempfile.mkdtemp()
        self.addCleanup(_cleanup, root)
        sub = os.path.join(root, "sub")
        os.makedirs(sub)
        with open(os.path.join(root, ".gitignore"), "w", encoding="utf-8") as f:
            f.write("*.log\n")
        with open(os.path.join(sub, ".gitignore"), "w", encoding="utf-8") as f:
            f.write("!keep.log\n")

        stack = GitignoreStack()
        stack.push_dir(root)
        self.assertIs(stack.is_ignored("a.log", False), True)
        self.assertIs(stack.is_ignored("sub/keep.log", False), True)
        stack.push_dir(sub)
        self.assertIs(stack.is_ignored("sub/keep.log", False), False)
        stack.pop_dir()
        self.assertIs(stack.is_ignored("sub/keep.log", False), True)

    def test_missing_gitignore_is_empty_ruleset(self):
        import os
        import tempfile

        root = tempfile.mkdtemp()
        self.addCleanup(_cleanup, root)
        stack = GitignoreStack()
        stack.push_dir(root)  # 目录里没有 .gitignore
        self.assertIs(stack.is_ignored("anything", False), False)


class ScanIntegrationTests(unittest.TestCase):
    """gitignore → scanner 全链路集成测试（真实临时目录，不经 mock）。

    历史缺陷（评审 C-1/I-1）都发生在“解析正确但求值契约错位”的集成路径上，
    规则集单元测试因直接构造规则对象而全部绕过，故此处必须走 scan_directory。
    """

    def _make_tree(self, root: str, files: dict):
        import os

        for rel, content in files.items():
            path = os.path.join(root, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

    def _scan_names(self, root: str) -> list:
        from copytree.scanner import scan_directory

        result = scan_directory(root, respect_gitignore=True)
        names = []

        def walk(entry):
            for child in entry.children or []:
                names.append(child.name)
                walk(child)

        walk(result.root)
        return names

    def test_root_double_star_log_ignores_root_level(self):
        import os
        import tempfile

        root = tempfile.mkdtemp()
        self.addCleanup(_cleanup, root)
        self._make_tree(root, {
            ".gitignore": "**/*.log\n",
            "a.log": "x",
            "sub/b.log": "x",
            "keep.txt": "x",
        })
        names = self._scan_names(root)
        self.assertNotIn("a.log", names)
        self.assertNotIn("b.log", names)
        self.assertIn("keep.txt", names)

    def test_nested_gitignore_anchored_rule(self):
        import os
        import tempfile

        root = tempfile.mkdtemp()
        self.addCleanup(_cleanup, root)
        self._make_tree(root, {
            os.path.join("sub", ".gitignore").replace(os.sep, "/"): "docs/build\n",
            "sub/docs/build/secret.txt": "x",
            "sub/keep.txt": "x",
        })
        names = self._scan_names(root)
        self.assertNotIn("secret.txt", names)
        self.assertIn("keep.txt", names)

    def test_nested_gitignore_root_anchored_rule(self):
        import os
        import tempfile

        root = tempfile.mkdtemp()
        self.addCleanup(_cleanup, root)
        self._make_tree(root, {
            "sub/.gitignore": "/tmpdir\n",
            "sub/tmpdir/secret.txt": "x",
            "sub/keep.txt": "x",
        })
        names = self._scan_names(root)
        self.assertNotIn("secret.txt", names)
        self.assertIn("keep.txt", names)

    def test_negation_in_nested_gitignore_still_works(self):
        # 对照组：非锚定否定经前缀吸收恰好正确，不得因锚定修复而回退
        import os
        import tempfile

        root = tempfile.mkdtemp()
        self.addCleanup(_cleanup, root)
        self._make_tree(root, {
            ".gitignore": "*.log\n",
            "sub/.gitignore": "!keep.log\n",
            "sub/keep.log": "x",
            "sub/other.log": "x",
        })
        names = self._scan_names(root)
        self.assertIn("keep.log", names)
        self.assertNotIn("other.log", names)


def _cleanup(path: str):
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
