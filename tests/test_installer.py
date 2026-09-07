import contextlib
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from copytree import __main__ as cli  # noqa: E402
from copytree import window as window_module  # noqa: E402

# Win32 MessageBoxW「取消」按钮标准值；winapi.py 只导出了 IDYES/IDNO
IDCANCEL = 2

_SOURCE_EXE = r"C:\run\copy-tree.exe"


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


def _assert_open_args_accepted(recorded):
    """契约校验：生产代码传给 open 的实参换 os.devnull 必须能被真实 open 打开。

    防止 mock 把 ValueError（closefd=False + 文件名之类参数错误）整个遮住，
    导致死代码照样绿灯。
    """
    for _, args, kwargs in recorded:
        with open(os.devnull, *args, **kwargs) as probe:
            if probe.closed:
                raise AssertionError("真实 open 打开的探针流不应立即关闭")


class AttachParentConsoleBackfillTests(unittest.TestCase):
    """_has_console() 命中但标准句柄无效时，须回填 CONOUT$/CONERR$ 流。

    GUI 子系统可执行文件即使附着了控制台，sys.stdout/sys.stderr 也可能为
    None；旧行为只置 _stdio_ready 不接流，下游（loguru stderr sink 等）拿
    到的仍是空流。GetStdHandle 在此分支不可达（_has_console 先短路），故
    不再打那颗死桩。
    """

    def setUp(self):
        cli._stdio_ready = False
        self.addCleanup(setattr, cli, "_stdio_ready", False)

    def test_console_with_invalid_handles_backfills_streams(self):
        # 契约式验证：记录传给 open 的实参，再用真实 open 证明参数组合可接受
        recorded = []

        def recording_open(file, *args, **kwargs):
            recorded.append((file, args, kwargs))
            return mock.Mock(name="stream")

        with mock.patch.object(cli.kernel32, "GetConsoleWindow", return_value=0x1001), \
             mock.patch.object(cli, "_launched_from_explorer", return_value=False), \
             mock.patch.object(cli.sys, "stdout", None), \
             mock.patch.object(cli.sys, "stderr", None), \
             mock.patch("builtins.open", side_effect=recording_open):
            result = cli._attach_parent_console()
            self.assertTrue(result)
            self.assertEqual([f for f, _, _ in recorded], ["CONOUT$", "CONERR$"])
        _assert_open_args_accepted(recorded)
        self.assertTrue(cli._stdio_ready)

    def test_console_with_valid_streams_leaves_them_untouched(self):
        # 已有流（如重定向管道）不得被 CONOUT$ 覆盖，保住重定向语义
        keeper_out, keeper_err = object(), object()
        with mock.patch.object(cli.kernel32, "GetConsoleWindow", return_value=0x1001), \
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


class AttachConsoleStreamTests(unittest.TestCase):
    """AttachConsole 分支接流契约（修复 v1.0.0 起 closefd=False 致 open 必抛的死代码）。

    AttachConsole 成功后必须真实接通 CONOUT$/CONERR$；任一 open 失败时不得
    误置 _stdio_ready（时序约束：置 True 只能跟在成功的 open 之后）。
    """

    def setUp(self):
        cli._stdio_ready = False
        self.addCleanup(setattr, cli, "_stdio_ready", False)

    def test_attach_console_success_opens_streams_with_valid_args(self):
        recorded = []

        def recording_open(file, *args, **kwargs):
            recorded.append((file, args, kwargs))
            return mock.Mock(name="stream")

        with mock.patch.object(cli.kernel32, "GetConsoleWindow", return_value=0), \
             mock.patch.object(cli.kernel32, "GetStdHandle", return_value=0), \
             mock.patch.object(cli.kernel32, "AttachConsole", return_value=1), \
             mock.patch.object(cli, "_launched_from_explorer", return_value=False), \
             mock.patch.object(cli.sys, "stdout", None), \
             mock.patch.object(cli.sys, "stderr", None), \
             mock.patch("builtins.open", side_effect=recording_open):
            result = cli._attach_parent_console()
            self.assertTrue(result)
            self.assertEqual([f for f, _, _ in recorded], ["CONOUT$", "CONERR$"])
        _assert_open_args_accepted(recorded)
        self.assertTrue(cli._stdio_ready)

    def test_attach_console_open_failure_keeps_stdio_not_ready(self):
        # 异常被吞后不得误报就绪：AttachConsole 成功但流打不开 → 返回 False
        with mock.patch.object(cli.kernel32, "GetConsoleWindow", return_value=0), \
             mock.patch.object(cli.kernel32, "GetStdHandle", return_value=0), \
             mock.patch.object(cli.kernel32, "AttachConsole", return_value=1), \
             mock.patch.object(cli, "_launched_from_explorer", return_value=False), \
             mock.patch.object(cli.sys, "stdout", None), \
             mock.patch.object(cli.sys, "stderr", None), \
             mock.patch("builtins.open", side_effect=OSError("no console device")):
            result = cli._attach_parent_console()
            out_during = cli.sys.stdout
        self.assertFalse(result)
        self.assertFalse(cli._stdio_ready)
        self.assertIsNone(out_during)


class ManageInstallFromGuiTests(unittest.TestCase):
    """no-args 主流程重接线（Task 5）：算状态 → 可选首次引导 → 带状态开窗。

    桩式遵循 Task 3 教训：所有依赖在一个 ExitStack 里单层并行 patch，
    断言用的 mock 与注入被测函数的是同一实例，严禁嵌套 patch 遮蔽。
    """

    SOURCE = _SOURCE_EXE

    def run_manage(self, *, state, info=None, dismissed=False, confirm_result=True,
                   install_ok=True, update_ok=True, notify_mode=False):
        """统一桩式：执行 cli._manage_install_from_gui()，返回全部 mock 供断言。"""
        if info is None:
            info = {"installed_exe_path": "", "installed_version": ""}
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        ns = SimpleNamespace()
        ns.info = info
        ns.exe = stack.enter_context(mock.patch.object(
            cli, "_get_exe_path", return_value=self.SOURCE))
        ns.compute = stack.enter_context(mock.patch.object(
            cli, "_compute_install_state", return_value=(state, info)))
        ns.config = stack.enter_context(mock.patch.object(
            cli, "get_effective_config",
            return_value={"installPromptDismissed": dismissed}))
        ns.confirm = stack.enter_context(mock.patch.object(
            cli, "_confirm_install", return_value=confirm_result))
        ns.install = stack.enter_context(mock.patch.object(
            cli, "_install_from_source", return_value=install_ok))
        ns.notify = stack.enter_context(mock.patch.object(cli, "_notify"))
        ns.update = stack.enter_context(mock.patch.object(
            cli, "update_config_values", return_value=update_ok))
        ns.open_window = stack.enter_context(mock.patch.object(
            cli, "_open_drop_window", return_value=None))
        if notify_mode:
            self.addCleanup(setattr, cli, "_force_notify_mode", False)
            cli._force_notify_mode = True
        cli._manage_install_from_gui()
        return ns

    def test_no_args_opens_window_with_computed_state(self):
        # 任意状态都开窗且参数原样透传；非 not-installed 态不得弹首次引导
        states = [
            (cli._INSTALL_STATE_NOT_INSTALLED,
             {"installed_exe_path": "", "installed_version": ""}),
            (cli._INSTALL_STATE_OK,
             {"installed_exe_path": cli.INSTALL_EXE, "installed_version": "1.1.0"}),
            (cli._INSTALL_STATE_UPDATE,
             {"installed_exe_path": cli.INSTALL_EXE, "installed_version": "1.0.0"}),
            (cli._INSTALL_STATE_DOWNGRADE,
             {"installed_exe_path": cli.INSTALL_EXE, "installed_version": "99.0"}),
            (cli._INSTALL_STATE_REPAIR,
             {"installed_exe_path": cli.INSTALL_EXE, "installed_version": ""}),
            (cli._INSTALL_STATE_MIGRATE,
             {"installed_exe_path": r"D:\legacy\copy-tree.exe",
              "installed_version": "1.0.0"}),
        ]
        for state, info in states:
            with self.subTest(state=state):
                ns = self.run_manage(state=state, info=info, dismissed=True)
                ns.open_window.assert_called_once_with(state, info)
                if state != cli._INSTALL_STATE_NOT_INSTALLED:
                    ns.confirm.assert_not_called()

    def test_first_run_prompt_shown_when_not_installed_and_not_dismissed(self):
        ns = self.run_manage(state=cli._INSTALL_STATE_NOT_INSTALLED, dismissed=False)
        ns.confirm.assert_called_once_with()
        ns.compute.assert_called_once_with(self.SOURCE)

    def test_prompt_accept_installs_then_opens_ok_window(self):
        ns = self.run_manage(state=cli._INSTALL_STATE_NOT_INSTALLED,
                             confirm_result=True, install_ok=True)
        ns.install.assert_called_once_with(self.SOURCE)
        ns.notify.assert_called_once_with(cli.MSG_INSTALLED)
        ns.open_window.assert_called_once_with(
            cli._INSTALL_STATE_OK,
            {"installed_exe_path": cli.INSTALL_EXE, "installed_version": cli.VERSION},
        )
        ns.update.assert_not_called()

    def test_prompt_accept_install_failure_keeps_not_installed_window(self):
        # 安装失败：通知失败但 state 保持 not-installed（横幅可重试），
        # 且不写 dismiss 标记（下次双击仍可再次询问）
        ns = self.run_manage(state=cli._INSTALL_STATE_NOT_INSTALLED,
                             confirm_result=True, install_ok=False)
        ns.notify.assert_called_once_with("安装失败")
        ns.open_window.assert_called_once_with(
            cli._INSTALL_STATE_NOT_INSTALLED,
            {"installed_exe_path": "", "installed_version": ""},
        )
        ns.update.assert_not_called()

    def test_prompt_decline_persists_flag_and_still_opens_window(self):
        ns = self.run_manage(state=cli._INSTALL_STATE_NOT_INSTALLED,
                             confirm_result=False)
        ns.update.assert_called_once_with({"installPromptDismissed": True})
        ns.install.assert_not_called()
        ns.notify.assert_not_called()
        ns.open_window.assert_called_once_with(
            cli._INSTALL_STATE_NOT_INSTALLED,
            {"installed_exe_path": "", "installed_version": ""},
        )

    def test_prompt_decline_write_failure_logs_warning(self):
        # 标记写失败（update 返回 False）时行为不变：仍开窗，
        # 但必须留一条警告日志便于排查「下次双击又问了一次」
        with mock.patch.object(cli.logger, "warning") as warn:
            ns = self.run_manage(state=cli._INSTALL_STATE_NOT_INSTALLED,
                                 confirm_result=False, update_ok=False)
        ns.update.assert_called_once_with({"installPromptDismissed": True})
        ns.open_window.assert_called_once_with(
            cli._INSTALL_STATE_NOT_INSTALLED,
            {"installed_exe_path": "", "installed_version": ""},
        )
        warn.assert_called_once()

    def test_prompt_skipped_when_dismissed(self):
        ns = self.run_manage(state=cli._INSTALL_STATE_NOT_INSTALLED, dismissed=True)
        ns.confirm.assert_not_called()
        ns.install.assert_not_called()
        ns.update.assert_not_called()
        ns.open_window.assert_called_once_with(
            cli._INSTALL_STATE_NOT_INSTALLED,
            {"installed_exe_path": "", "installed_version": ""},
        )

    def test_notify_mode_bypasses_window(self):
        # 回归红线：--notify 全程不开窗——_manage_install_from_gui 直接返回，
        # 不算状态、不弹引导、不开窗（调用点锁定在 _handle_no_args）
        ns = self.run_manage(state=cli._INSTALL_STATE_OK, notify_mode=True)
        ns.compute.assert_not_called()
        ns.confirm.assert_not_called()
        ns.open_window.assert_not_called()


class MainFlagDispatchTests(unittest.TestCase):
    """回归红线：--install/--uninstall 参数通路行为不变（Task 5 只重接 no-args）。"""

    def run_main(self, argv):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(
            cli.sys, "argv", ["copy-tree.exe"] + argv))
        # 非 frozen 环境默认按 CLI 入口处理；显式钉成 GUI exe 才能走到安装分发
        stack.enter_context(mock.patch.object(
            cli, "_is_cli_executable", return_value=False))
        stack.enter_context(mock.patch.object(
            cli, "_attach_parent_console", return_value=False))
        stack.enter_context(mock.patch.object(
            cli, "_launched_from_explorer", return_value=False))
        stack.enter_context(mock.patch.object(cli, "setup_logging"))
        stack.enter_context(mock.patch.object(cli, "_pause_if_double_clicked_cli"))
        ns = SimpleNamespace()
        ns.exit = stack.enter_context(mock.patch.object(cli, "_exit"))
        ns.install = stack.enter_context(mock.patch.object(cli, "_handle_install"))
        ns.uninstall = stack.enter_context(mock.patch.object(cli, "_handle_uninstall"))
        cli.main()
        return ns

    def test_install_flag_dispatches_to_install_handler(self):
        ns = self.run_main(["--install"])
        ns.install.assert_called_once_with()
        ns.uninstall.assert_not_called()
        ns.exit.assert_not_called()

    def test_uninstall_flag_dispatches_to_uninstall_handler(self):
        ns = self.run_main(["--uninstall"])
        ns.uninstall.assert_called_once_with()
        ns.install.assert_not_called()
        ns.exit.assert_not_called()


class OpenDropWindowTests(unittest.TestCase):
    """_open_drop_window（Task 5）：回调装配 + 三参透传 + tkinter 初始化失败兜底。

    回调键契约取窗口侧实际查找的并集：window._BANNER_BUTTONS 按状态查
    not-installed/update/repair/migrate/uninstall（downgrade 固定绑 uninstall），
    另附协调口径中的 install 键作别名，多余键窗口永不读取。
    """

    SOURCE = _SOURCE_EXE
    LEGACY_INFO = {"installed_exe_path": r"D:\legacy\copy-tree.exe",
                   "installed_version": "1.0.0"}

    def open_window(self, state="update", info=None, run_side_effect=None,
                    install_ok=None, migrate_choice=None, patch_uninstall=False,
                    expect_failure=False):
        if info is None:
            info = dict(self.LEGACY_INFO)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        ns = SimpleNamespace()
        ns.info = info
        stack.enter_context(mock.patch.object(
            cli, "_get_exe_path", return_value=self.SOURCE))
        ns.run = stack.enter_context(mock.patch.object(
            window_module, "run_drop_window", side_effect=run_side_effect))
        ns.install = ns.migrate = ns.uninstall = None
        if install_ok is not None:
            ns.install = stack.enter_context(mock.patch.object(
                cli, "_install_from_source", return_value=install_ok))
        if migrate_choice is not None:
            ns.migrate = stack.enter_context(mock.patch.object(
                cli, "_choose_migrate_or_uninstall", return_value=migrate_choice))
        if patch_uninstall:
            ns.uninstall = stack.enter_context(mock.patch.object(
                cli, "_uninstall_from_gui"))
        if expect_failure:
            ns.report = stack.enter_context(mock.patch.object(
                cli, "_report_setup_status"))
            ns.exit = stack.enter_context(mock.patch.object(cli, "_exit"))
        cli._open_drop_window(state, info)
        ns.actions = ns.run.call_args.kwargs["install_actions"]
        return ns

    def test_forwards_state_info_and_action_dict(self):
        info = dict(self.LEGACY_INFO)
        ns = self.open_window("repair", info=info)
        ns.run.assert_called_once()
        self.assertEqual(ns.run.call_args.kwargs["install_state"], "repair")
        self.assertIs(ns.run.call_args.kwargs["install_info"], info)
        expected_keys = {"install", "update", "repair", "migrate", "uninstall",
                         "not-installed"}
        self.assertTrue(expected_keys.issubset(ns.actions))
        for key in expected_keys:
            self.assertTrue(callable(ns.actions[key]), key)

    def test_install_family_keys_call_install_from_source(self):
        # install/update/repair/not-installed 四个键都路由到 _install_from_source
        for key in ("install", "update", "repair", "not-installed"):
            with self.subTest(key=key):
                ns = self.open_window(install_ok=True)
                self.assertIs(ns.actions[key](), True)
                ns.install.assert_called_once_with(self.SOURCE)

    def test_install_action_failure_returns_false(self):
        # 失败返回 False：窗口横幅保留供重试
        ns = self.open_window(install_ok=False)
        self.assertIs(ns.actions["update"](), False)
        ns.install.assert_called_once_with(self.SOURCE)

    def test_migrate_action_confirmed_then_installs(self):
        ns = self.open_window(migrate_choice=cli._SETUP_ACTION_INSTALL,
                              install_ok=True)
        self.assertIs(ns.actions["migrate"](), True)
        ns.migrate.assert_called_once_with(
            self.LEGACY_INFO["installed_exe_path"], cli.INSTALL_EXE)
        ns.install.assert_called_once_with(self.SOURCE)

    def test_migrate_action_cancelled_returns_false_without_install(self):
        # 取消不动安装：回调返回 False，横幅保留
        ns = self.open_window(migrate_choice=cli._SETUP_ACTION_CANCEL,
                              install_ok=True)
        self.assertIs(ns.actions["migrate"](), False)
        ns.install.assert_not_called()

    def test_migrate_action_uninstall_choice_runs_uninstall(self):
        # 「否：卸载 copy-tree」接回现有卸载流程（与 uninstall 键回调同路，
        # 正常完成返回 True；失败时内部 _exit(3) 自然不返回），且不触发安装
        ns = self.open_window(migrate_choice=cli._SETUP_ACTION_UNINSTALL,
                              install_ok=True, patch_uninstall=True)
        self.assertIs(ns.actions["migrate"](), True)
        ns.migrate.assert_called_once_with(
            self.LEGACY_INFO["installed_exe_path"], cli.INSTALL_EXE)
        ns.uninstall.assert_called_once_with(self.LEGACY_INFO["installed_exe_path"])
        ns.install.assert_not_called()

    def test_uninstall_action_uses_existing_uninstall_path(self):
        # 卸载回调复用现有流程（成功后返回 True；失败时内部 _exit(3) 不返回）
        ns = self.open_window(patch_uninstall=True)
        self.assertIs(ns.actions["uninstall"](), True)
        ns.uninstall.assert_called_once_with(self.LEGACY_INFO["installed_exe_path"])

    def test_window_init_failure_reports_and_exits_3(self):
        # tkinter 初始化失败（tk.Tk() 抛异常等）：报告状态并按失败惯例 _exit(3)
        ns = self.open_window(run_side_effect=RuntimeError("tk init failed"),
                              expect_failure=True)
        ns.report.assert_called_once()
        self.assertIn("无法打开窗口", ns.report.call_args.args[0])
        ns.exit.assert_called_once_with(3)


class InstallDialogCharacterizationTests(unittest.TestCase):
    """弹窗函数特征测试——锁定按钮 ID → 返回值映射。

    只测映射不测文案；_show_question_box 打桩后 MessageBoxW 永不被真实调用。
    生产路径现仅消费 _confirm_install 与 _choose_migrate_or_uninstall；
    其余五个暂无生产调用点，按 spec 6.2 作为特征测试保留，防映射漂移。
    """

    def ask(self, func, button_id, *args):
        with mock.patch.object(cli, "_show_question_box", return_value=button_id) as box:
            result = func(*args)
        box.assert_called_once()
        return result

    def test_confirm_install_mapping(self):
        self.assertIs(self.ask(cli._confirm_install, cli.IDYES), True)
        self.assertIs(self.ask(cli._confirm_install, cli.IDNO), False)

    def test_confirm_uninstall_mapping(self):
        self.assertIs(self.ask(cli._confirm_uninstall, cli.IDYES), True)
        self.assertIs(self.ask(cli._confirm_uninstall, cli.IDNO), False)

    def test_choose_uninstall_or_keep_mapping(self):
        path = r"C:\installed\copy-tree.exe"
        self.assertEqual(
            self.ask(cli._choose_uninstall_or_keep, cli.IDYES, path),
            cli._SETUP_ACTION_UNINSTALL)
        self.assertEqual(
            self.ask(cli._choose_uninstall_or_keep, cli.IDNO, path),
            cli._SETUP_ACTION_CANCEL)

    def test_choose_update_or_uninstall_mapping(self):
        args = (r"C:\run\copy-tree.exe", r"C:\installed\copy-tree.exe", "1.0.0")
        self.assertEqual(
            self.ask(cli._choose_update_or_uninstall, cli.IDYES, *args),
            cli._SETUP_ACTION_INSTALL)
        self.assertEqual(
            self.ask(cli._choose_update_or_uninstall, cli.IDNO, *args),
            cli._SETUP_ACTION_UNINSTALL)
        self.assertEqual(
            self.ask(cli._choose_update_or_uninstall, IDCANCEL, *args),
            cli._SETUP_ACTION_CANCEL)

    def test_choose_downgrade_or_uninstall_mapping(self):
        args = (r"C:\run\copy-tree.exe", r"C:\installed\copy-tree.exe", "99.0")
        self.assertEqual(
            self.ask(cli._choose_downgrade_or_uninstall, cli.IDYES, *args),
            cli._SETUP_ACTION_INSTALL)
        self.assertEqual(
            self.ask(cli._choose_downgrade_or_uninstall, cli.IDNO, *args),
            cli._SETUP_ACTION_UNINSTALL)
        self.assertEqual(
            self.ask(cli._choose_downgrade_or_uninstall, IDCANCEL, *args),
            cli._SETUP_ACTION_CANCEL)

    def test_choose_migrate_or_uninstall_mapping(self):
        args = (r"D:\legacy\copy-tree.exe", r"C:\installed\copy-tree.exe")
        self.assertEqual(
            self.ask(cli._choose_migrate_or_uninstall, cli.IDYES, *args),
            cli._SETUP_ACTION_INSTALL)
        self.assertEqual(
            self.ask(cli._choose_migrate_or_uninstall, cli.IDNO, *args),
            cli._SETUP_ACTION_UNINSTALL)
        self.assertEqual(
            self.ask(cli._choose_migrate_or_uninstall, IDCANCEL, *args),
            cli._SETUP_ACTION_CANCEL)

    def test_choose_repair_or_uninstall_mapping(self):
        self.assertEqual(
            self.ask(cli._choose_repair_or_uninstall, cli.IDYES, r"C:\gone\copy-tree.exe"),
            cli._SETUP_ACTION_INSTALL)
        self.assertEqual(
            self.ask(cli._choose_repair_or_uninstall, cli.IDNO, r"C:\gone\copy-tree.exe"),
            cli._SETUP_ACTION_UNINSTALL)
        self.assertEqual(
            self.ask(cli._choose_repair_or_uninstall, IDCANCEL, r"C:\gone\copy-tree.exe"),
            cli._SETUP_ACTION_CANCEL)


if __name__ == "__main__":
    unittest.main()
