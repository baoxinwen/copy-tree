import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from copytree import __main__ as cli  # noqa: E402


class VersionOrderingTests(unittest.TestCase):
    """版本序值比较（评审 M-3）：降级场景不得复用"检测到新版"文案。"""

    def test_version_ordering(self):
        self.assertTrue(cli._is_version_newer("2.0", "1.9.9"))
        self.assertTrue(cli._is_version_newer("1.0.1", "1.0"))
        self.assertFalse(cli._is_version_newer("1.0", "1.0"))
        self.assertFalse(cli._is_version_newer("0.9", "1.0"))


class ChooseInstalledActionTests(unittest.TestCase):
    def test_downgrade_uses_honest_dialog(self):
        # 安装副本版本比当前文件新 → 属降级，弹窗必须如实说明而非"检测到新版"
        with mock.patch.object(cli, "get_installed_version", return_value="99.0"), \
             mock.patch.object(cli, "_same_path", return_value=True), \
             mock.patch.object(cli.os.path, "isfile", return_value=True), \
             mock.patch.object(cli, "_show_question_box", return_value=cli.IDYES) as box:
            action = cli._choose_installed_action("C:\\run\\copy-tree.exe", cli.INSTALL_EXE)

        self.assertEqual(action, cli._SETUP_ACTION_INSTALL)
        text = box.call_args[0][0]
        self.assertIn("降级", text)
        self.assertNotIn("检测到新版", text)

    def test_upgrade_keeps_update_dialog(self):
        with mock.patch.object(cli, "get_installed_version", return_value="0.0.1"), \
             mock.patch.object(cli, "_same_path", return_value=True), \
             mock.patch.object(cli.os.path, "isfile", return_value=True), \
             mock.patch.object(cli, "_show_question_box", return_value=cli.IDYES) as box:
            action = cli._choose_installed_action("C:\\run\\copy-tree.exe", cli.INSTALL_EXE)

        self.assertEqual(action, cli._SETUP_ACTION_INSTALL)
        text = box.call_args[0][0]
        self.assertIn("检测到新版", text)


class UninstallNoticeTests(unittest.TestCase):
    """卸载残留文件时提示必须如实（评审 M-4），不得笼统报"已卸载"。"""

    def test_notice_mentions_residual_when_install_dir_survives(self):
        with mock.patch.object(cli.os.path, "exists", return_value=True):
            notice = cli._uninstall_notice()
        self.assertIn("未能删除", notice)

    def test_notice_plain_when_nothing_survives(self):
        with mock.patch.object(cli.os.path, "exists", return_value=False):
            notice = cli._uninstall_notice()
        self.assertEqual(notice, cli.MSG_UNINSTALLED)


class DecisionTreeTests(unittest.TestCase):
    """_choose_installed_action 七分支特征测试（评审 I-7：安装决策树零覆盖）。

    锁定现状：各前置状态到弹窗/动作的路由语义，防止后续改动无声漂移。
    """

    def decide(self, *, version="", same_stable=False, same_running=False,
               install_exe_exists=False, files_match=False,
               confirm=True, box_result=None):
        box_result = cli.IDYES if box_result is None else box_result
        with mock.patch.object(cli, "get_installed_version", return_value=version), \
             mock.patch.object(cli, "_same_path", side_effect=[same_stable, same_running]), \
             mock.patch.object(cli.os.path, "isfile", return_value=install_exe_exists), \
             mock.patch.object(cli, "_files_match", return_value=files_match), \
             mock.patch.object(cli, "_confirm_uninstall", return_value=confirm), \
             mock.patch.object(cli, "_show_question_box", return_value=box_result) as box:
            action = cli._choose_installed_action(r"C:\run\copy-tree.exe", cli.INSTALL_EXE)
        return action, box

    def test_same_version_reports_uptodate_without_dialog(self):
        action, box = self.decide(version=cli.VERSION, same_stable=True)
        self.assertEqual(action, cli._SETUP_ACTION_UPTODATE)
        box.assert_not_called()

    def test_registered_and_running_without_version_asks_uninstall(self):
        action, _ = self.decide(same_stable=True, same_running=True, confirm=True)
        self.assertEqual(action, cli._SETUP_ACTION_UNINSTALL)
        action, _ = self.decide(same_stable=True, same_running=True, confirm=False)
        self.assertEqual(action, cli._SETUP_ACTION_CANCEL)

    def test_registered_files_match_offers_uninstall_or_keep(self):
        action, _ = self.decide(same_stable=True, install_exe_exists=True,
                                files_match=True, box_result=cli.IDNO)
        self.assertEqual(action, cli._SETUP_ACTION_CANCEL)
        action, _ = self.decide(same_stable=True, install_exe_exists=True,
                                files_match=True, box_result=cli.IDYES)
        self.assertEqual(action, cli._SETUP_ACTION_UNINSTALL)

    def test_registered_files_differ_falls_back_to_update_dialog(self):
        # 空版本号（旧版安装）时回退 filecmp 比对：不同 → 更新弹窗
        action, _ = self.decide(same_stable=True, install_exe_exists=True,
                                files_match=False, box_result=cli.IDYES)
        self.assertEqual(action, cli._SETUP_ACTION_INSTALL)

    def test_registered_but_target_missing_routes_to_repair(self):
        action, _ = self.decide(same_stable=True, install_exe_exists=False,
                                box_result=cli.IDYES)
        self.assertEqual(action, cli._SETUP_ACTION_INSTALL)

    def test_unregistered_with_existing_install_routes_to_migrate(self):
        action, box = self.decide(same_stable=False, install_exe_exists=True,
                                  box_result=cli.IDYES)
        self.assertEqual(action, cli._SETUP_ACTION_INSTALL)
        self.assertIn("旧安装路径", box.call_args[0][0])

    def test_nothing_registered_and_missing_routes_to_repair(self):
        # 空输入边界：installed_exe_path 为空串时不迁移，直接走修复
        with mock.patch.object(cli, "get_installed_version", return_value=""), \
             mock.patch.object(cli, "_same_path", side_effect=[False, False]), \
             mock.patch.object(cli.os.path, "isfile", return_value=False), \
             mock.patch.object(cli, "_show_question_box", return_value=cli.IDYES) as box:
            action = cli._choose_installed_action(r"C:\run\copy-tree.exe", "")
        self.assertEqual(action, cli._SETUP_ACTION_INSTALL)
        self.assertIn("修复", box.call_args[0][0])

    def test_dialog_cancel_is_preserved_everywhere(self):
        for kwargs in (
            {"same_stable": True, "same_running": True, "confirm": False},
            {"same_stable": True, "install_exe_exists": True, "files_match": True,
             "box_result": 2},
            {"same_stable": False, "install_exe_exists": True, "box_result": 2},
        ):
            action, _ = self.decide(**kwargs)
            self.assertEqual(action, cli._SETUP_ACTION_CANCEL)


class FilesMatchTests(unittest.TestCase):
    """_files_match 真实文件行为（filecmp shallow=False）。"""

    def setUp(self):
        import tempfile

        self.dir = tempfile.mkdtemp(prefix="ct_filesmatch_")
        self.addCleanup(_cleanup_dir, self.dir)

    def _write(self, name, content):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_identical_files_match(self):
        left = self._write("a.exe", "same-content")
        right = self._write("b.exe", "same-content")
        self.assertTrue(cli._files_match(left, right))

    def test_different_files_do_not_match(self):
        left = self._write("a.exe", "content-1")
        right = self._write("b.exe", "content-2")
        self.assertFalse(cli._files_match(left, right))

    def test_missing_file_does_not_match(self):
        left = self._write("a.exe", "content")
        self.assertFalse(cli._files_match(left, os.path.join(self.dir, "missing.exe")))
        # 空路径边界：视为文件不存在
        self.assertFalse(cli._files_match("", os.path.join(self.dir, "x.exe")))


def _cleanup_dir(path):
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
