import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from copytree import registry  # noqa: E402


class InstallFailureTests(unittest.TestCase):
    """install() 失败路径语义（评审 I-4）。

    不变量（registry.install 快捷方式分支注释明示）：安装失败绝不能调用
    uninstall()——更新场景下会把既有完好安装连根拆除。本测试守护注册表
    写失败分支遵守同一不变量。
    """

    def test_menu_write_failure_does_not_uninstall_existing_install(self):
        fd, exe_path = tempfile.mkstemp(suffix=".exe")
        os.close(fd)
        self.addCleanup(os.remove, exe_path)

        winreg_mock = mock.MagicMock()
        winreg_mock.SetValueEx.side_effect = OSError("模拟注册表写入失败")
        with mock.patch.object(registry, "winreg", winreg_mock), \
             mock.patch.object(registry, "create_start_menu_shortcut", return_value=True), \
             mock.patch.object(registry, "_delete_key_recursive"), \
             mock.patch.object(registry, "uninstall") as mock_uninstall:
            ok = registry.install(exe_path)

        self.assertFalse(ok)
        mock_uninstall.assert_not_called()


class LeafCommandTests(unittest.TestCase):
    """右键菜单命令构造（AGENTS 硬约束守护，评审 I-7）。"""

    def test_leaf_command_quotes_exe_and_injects_notify(self):
        captured = {}

        def fake_set_value(key, value_name, reserved, value_type, data):
            if value_name == "":
                captured["default"] = data
            return None

        winreg_mock = mock.MagicMock()
        winreg_mock.SetValueEx.side_effect = fake_set_value
        with mock.patch.object(registry, "winreg", winreg_mock):
            registry._write_leaf_command(
                r"Software\Classes\Directory\shell\CopyTree",
                "📋 复制目录树", "", r"C:\Program Files\copy-tree\copy-tree.exe", r'"%1\."',
            )

        self.assertEqual(captured["default"], r'"C:\Program Files\copy-tree\copy-tree.exe" "%1\." --notify')


if __name__ == "__main__":
    unittest.main()
