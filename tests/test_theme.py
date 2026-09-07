import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from copytree import theme  # noqa: E402


class TokenContractTests(unittest.TestCase):
    """token 契约：关键色值与角色名是 window.py 与测试共同依赖的稳定接口。"""

    def test_semantic_roles_present(self):
        for role in (
            "PINE", "PINE_HOVER", "PAPER", "SURFACE", "AMBER", "INK", "MUTED",
            "FAINT", "LINE", "BORDER", "DANGER", "DANGER_LINE",
            "WARN_BG", "WARN_TEXT", "SUCCESS", "WHITE",
        ):
            self.assertTrue(hasattr(theme, role), f"缺少语义色 {role}")

    def test_core_values_match_design(self):
        self.assertEqual(theme.PINE, "#1F3D2B")
        self.assertEqual(theme.PAPER, "#FAF9F6")
        self.assertEqual(theme.AMBER, "#E8A33D")
        self.assertEqual(theme.SURFACE, "#F1EFE8")

    def test_fonts_declared(self):
        self.assertIn("Segoe UI", theme.FONT_SANS)
        self.assertIn("Cascadia Mono", theme.FONT_MONO)


class ApplyThemeTests(unittest.TestCase):
    def setUp(self):
        theme._applied = False  # 复位幂等标志（同 logging_setup._configured 惯例）

    def test_apply_theme_uses_styleable_base_and_configures_surfaces(self):
        style = mock.Mock()
        theme.apply_theme(style)
        style.theme_use.assert_called_once_with("clam")
        configured = {call.args[0]: call.kwargs for call in style.configure.call_args_list}
        self.assertIn("TFrame", configured)
        self.assertIn("TLabel", configured)
        self.assertIn("TButton", configured)
        self.assertIn("TCheckbutton", configured)
        self.assertIn("background", configured["TFrame"])
        self.assertEqual(configured["TFrame"]["background"], theme.PAPER)

    def test_apply_theme_is_idempotent(self):
        style = mock.Mock()
        theme.apply_theme(style)
        theme.apply_theme(style)
        self.assertEqual(style.theme_use.call_count, 1)


if __name__ == "__main__":
    unittest.main()
