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


class ComputeInstallStateTests(unittest.TestCase):
    """_compute_install_state 六态矩阵（Task 3）：纯计算，无弹窗、无副作用。

    判定规则逐分支平移自原 _choose_installed_action，另有 F5 拍板：
    同版本（或字节相同）一律 ok，不再触发任何询问分支。
    """

    def compute(self, *, installed_path="", version="", menu_registered=False,
                stable=True, install_exe_exists=True, files_match=True,
                running_version="1.1.0"):
        """统一桩式：installed_path 为注册表解析出的目标，stable 为其与
        INSTALL_EXE 同路径，menu_registered 为菜单键是否残留。"""
        with mock.patch.object(cli, "get_installed_exe_path", return_value=installed_path), \
             mock.patch.object(cli, "get_installed_version", return_value=version), \
             mock.patch.object(cli, "is_registered", return_value=menu_registered), \
             mock.patch.object(cli, "_same_path", return_value=stable), \
             mock.patch.object(cli.os.path, "isfile", return_value=install_exe_exists), \
             mock.patch.object(cli, "_files_match", return_value=files_match), \
             mock.patch.object(cli, "VERSION", running_version):
            return cli._compute_install_state(r"C:\run\copy-tree.exe")

    def test_state_update_when_running_newer(self):
        state, info = self.compute(installed_path=cli.INSTALL_EXE, version="1.0.0")
        self.assertEqual(state, cli._INSTALL_STATE_UPDATE)
        self.assertEqual(info["installed_version"], "1.0.0")

    def test_state_ok_when_same_version_registered(self):
        # purity 锁：状态计算不得弹窗；若未来回归引入弹窗，
        # 先在 patch 阶段挂起/报错，而不是卡住测试进程
        with mock.patch.object(cli, "_show_question_box") as question_box:
            state, info = self.compute(installed_path=cli.INSTALL_EXE, version="1.1.0")
        self.assertEqual(state, cli._INSTALL_STATE_OK)
        self.assertEqual(info["installed_exe_path"], cli.INSTALL_EXE)
        self.assertEqual(info["installed_version"], "1.1.0")
        question_box.assert_not_called()

    def test_state_downgrade_when_running_older(self):
        state, info = self.compute(installed_path=cli.INSTALL_EXE, version="99.0")
        self.assertEqual(state, cli._INSTALL_STATE_DOWNGRADE)
        self.assertEqual(info["installed_version"], "99.0")

    def test_state_repair_when_target_missing(self):
        state, _ = self.compute(installed_path=cli.INSTALL_EXE, version="1.0.0",
                                install_exe_exists=False)
        self.assertEqual(state, cli._INSTALL_STATE_REPAIR)

    def test_state_repair_when_menu_orphan(self):
        # 菜单键在但命令解析不出主程序路径：残留菜单按修复处理
        state, info = self.compute(installed_path="", menu_registered=True)
        self.assertEqual(state, cli._INSTALL_STATE_REPAIR)
        self.assertEqual(info["installed_exe_path"], "")

    def test_state_migrate_when_legacy_path(self):
        legacy = r"D:\legacy\copy-tree.exe"
        state, info = self.compute(installed_path=legacy, stable=False)
        self.assertEqual(state, cli._INSTALL_STATE_MIGRATE)
        self.assertEqual(info["installed_exe_path"], legacy)

    def test_state_not_installed_when_no_registry(self):
        state, info = self.compute(installed_path="", menu_registered=False)
        self.assertEqual(state, cli._INSTALL_STATE_NOT_INSTALLED)
        # 契约：未安装时两个键仍存在且为空串（不得为 None），Task 5 直接透传
        self.assertEqual(info, {"installed_exe_path": "", "installed_version": ""})

    def test_same_version_byte_differs_is_ok(self):
        # F5 拍板：同版本不做任何字节比对，字节差异也视为正常。
        # 刻意不走 self.compute：辅助方法内部还会 patch _files_match，
        # 嵌套后外层 mock 被遮蔽，assert_not_called 恒真、锁失效；
        # 这里单层 patch + 直接调用，保证检查作用于被测函数执行期。
        with mock.patch.object(cli, "get_installed_exe_path", return_value=cli.INSTALL_EXE), \
             mock.patch.object(cli, "get_installed_version", return_value="1.1.0"), \
             mock.patch.object(cli, "is_registered", return_value=True), \
             mock.patch.object(cli, "_same_path", return_value=True), \
             mock.patch.object(cli.os.path, "isfile", return_value=True), \
             mock.patch.object(cli, "VERSION", "1.1.0"), \
             mock.patch.object(cli, "_files_match", return_value=False) as files_match:
            state, info = cli._compute_install_state(r"C:\run\copy-tree.exe")
        self.assertEqual(state, cli._INSTALL_STATE_OK)
        self.assertEqual(info["installed_version"], "1.1.0")
        files_match.assert_not_called()

    def test_no_version_record_same_bytes_is_ok(self):
        # 旧版安装未登记版本：回退字节比对，相同 → ok（原"卸载/保留"询问退役）
        state, info = self.compute(installed_path=cli.INSTALL_EXE, version="",
                                   files_match=True)
        self.assertEqual(state, cli._INSTALL_STATE_OK)
        self.assertEqual(info["installed_version"], "")

    def test_no_version_record_bytes_differ_is_update(self):
        state, _ = self.compute(installed_path=cli.INSTALL_EXE, version="",
                                files_match=False)
        self.assertEqual(state, cli._INSTALL_STATE_UPDATE)

    def test_no_version_record_target_missing_is_repair(self):
        state, _ = self.compute(installed_path=cli.INSTALL_EXE, version="",
                                install_exe_exists=False)
        self.assertEqual(state, cli._INSTALL_STATE_REPAIR)

    def test_legacy_target_missing_is_repair(self):
        # 非标准路径且旧文件已消失：无处可迁移，只能修复
        state, _ = self.compute(installed_path=r"D:\legacy\copy-tree.exe",
                                stable=False, install_exe_exists=False)
        self.assertEqual(state, cli._INSTALL_STATE_REPAIR)

    def test_version_with_non_numeric_segment_is_update(self):
        # 极值输入：非数字段按 _version_key 语义取 0，"1.0.0rc" < "1.1.0" → update
        state, _ = self.compute(installed_path=cli.INSTALL_EXE, version="1.0.0rc")
        self.assertEqual(state, cli._INSTALL_STATE_UPDATE)

    # 说明：原 test_empty_version_string_behaves_as_unrecorded 与
    # test_no_version_record_same_bytes_is_ok 输入完全重复（空版本串为假值、
    # 走字节比对路径），评审后删除；空版本语义由该用例继续锁定。

    def test_missing_target_check_oserror_propagates(self):
        # 依赖失败边界：isfile 抛 OSError 时现状冒泡，锁定不加吞咽
        with mock.patch.object(cli, "get_installed_exe_path", return_value=cli.INSTALL_EXE), \
             mock.patch.object(cli, "get_installed_version", return_value="1.0.0"), \
             mock.patch.object(cli, "_same_path", return_value=True), \
             mock.patch.object(cli, "VERSION", "1.1.0"), \
             mock.patch.object(cli.os.path, "isfile", side_effect=OSError("disk error")):
            self.assertRaises(OSError, cli._compute_install_state, r"C:\run\copy-tree.exe")

    def test_info_dict_contract_exact_keys(self):
        # 契约：info 恰好两个键，长路径/版本原样透传，供窗口展示
        state, info = self.compute(installed_path=cli.INSTALL_EXE, version="1.0.0")
        self.assertEqual(
            info,
            {"installed_exe_path": cli.INSTALL_EXE, "installed_version": "1.0.0"},
        )
        self.assertIn(state, ("update", "ok", "downgrade", "repair", "migrate",
                              "not-installed"))


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


class AttachParentConsoleFallbackTests(unittest.TestCase):
    """_attach_parent_console 无控制台场景特征测试（此前零覆盖）。

    锁定现状：无控制台窗口、标准句柄无效、AttachConsole 失败时，
    必须安静地返回 False 且不触碰任何标准流。
    """

    def setUp(self):
        cli._stdio_ready = False
        self.addCleanup(setattr, cli, "_stdio_ready", False)

    def test_no_console_and_no_handles_returns_false(self):
        # 控制台 API 全部返回失败：GetConsoleWindow=0、句柄无效、AttachConsole=0
        with mock.patch.object(cli.kernel32, "GetConsoleWindow", return_value=0), \
             mock.patch.object(cli.kernel32, "GetStdHandle", return_value=cli.INVALID_HANDLE_VALUE), \
             mock.patch.object(cli.kernel32, "AttachConsole", return_value=0), \
             mock.patch.object(cli, "_launched_from_explorer", return_value=False):
            result = cli._attach_parent_console()

        self.assertFalse(result)
        self.assertFalse(cli._stdio_ready)

    def test_zero_handle_is_also_invalid(self):
        # 空输入边界：句柄值 0 同样视为无效，不得尝试打开流
        with mock.patch.object(cli.kernel32, "GetConsoleWindow", return_value=0), \
             mock.patch.object(cli.kernel32, "GetStdHandle", return_value=0), \
             mock.patch.object(cli.kernel32, "AttachConsole", return_value=0), \
             mock.patch.object(cli, "_launched_from_explorer", return_value=False):
            result = cli._attach_parent_console()

        self.assertFalse(result)
        self.assertFalse(cli._stdio_ready)


class AttachParentConsoleBackfillTests(unittest.TestCase):
    """_has_console() 命中但标准句柄无效时，须回填 CONOUT$/CONERR$ 流。

    GUI 子系统可执行文件即使附着了控制台，sys.stdout/sys.stderr 也可能为
    None；旧行为只置 _stdio_ready 不接流，下游（loguru stderr sink 等）拿
    到的仍是空流。
    """

    def setUp(self):
        cli._stdio_ready = False
        self.addCleanup(setattr, cli, "_stdio_ready", False)

    def test_console_with_invalid_handles_backfills_streams(self):
        fake_out, fake_err = mock.Mock(name="stdout"), mock.Mock(name="stderr")
        with mock.patch.object(cli.kernel32, "GetConsoleWindow", return_value=0x1001), \
             mock.patch.object(cli.kernel32, "GetStdHandle", return_value=0), \
             mock.patch.object(cli, "_launched_from_explorer", return_value=False), \
             mock.patch.object(cli.sys, "stdout", None), \
             mock.patch.object(cli.sys, "stderr", None), \
             mock.patch("builtins.open", side_effect=[fake_out, fake_err]) as open_mock:
            result = cli._attach_parent_console()
            self.assertTrue(result)
            self.assertIs(cli.sys.stdout, fake_out)
            self.assertIs(cli.sys.stderr, fake_err)
        opened = [c.args[0] for c in open_mock.call_args_list]
        self.assertEqual(opened, ["CONOUT$", "CONERR$"])
        self.assertTrue(cli._stdio_ready)

    def test_console_with_valid_streams_leaves_them_untouched(self):
        # 已有流（如重定向管道）不得被 CONOUT$ 覆盖，保住重定向语义
        keeper_out, keeper_err = object(), object()
        with mock.patch.object(cli.kernel32, "GetConsoleWindow", return_value=0x1001), \
             mock.patch.object(cli.kernel32, "GetStdHandle", return_value=0), \
             mock.patch.object(cli, "_launched_from_explorer", return_value=False), \
             mock.patch.object(cli.sys, "stdout", keeper_out), \
             mock.patch.object(cli.sys, "stderr", keeper_err), \
             mock.patch("builtins.open") as open_mock:
            result = cli._attach_parent_console()
            self.assertTrue(result)
            self.assertIs(cli.sys.stdout, keeper_out)
            self.assertIs(cli.sys.stderr, keeper_err)
        open_mock.assert_not_called()
        self.assertTrue(cli._stdio_ready)

    def test_backfill_open_failure_does_not_raise(self):
        # 依赖失败边界：控制台设备打不开时仍不抛异常，控制台在即视为就绪
        with mock.patch.object(cli.kernel32, "GetConsoleWindow", return_value=0x1001), \
             mock.patch.object(cli.kernel32, "GetStdHandle", return_value=0), \
             mock.patch.object(cli, "_launched_from_explorer", return_value=False), \
             mock.patch.object(cli.sys, "stdout", None), \
             mock.patch.object(cli.sys, "stderr", None), \
             mock.patch("builtins.open", side_effect=OSError("no console device")):
            result = cli._attach_parent_console()
            out_during = cli.sys.stdout
            err_during = cli.sys.stderr
        self.assertTrue(result)
        self.assertIsNone(out_during)
        self.assertIsNone(err_during)
        self.assertTrue(cli._stdio_ready)


if __name__ == "__main__":
    unittest.main()
