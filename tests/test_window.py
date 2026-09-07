import ctypes
import ctypes.wintypes
import queue
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from copytree import window as window_module  # noqa: E402
from copytree.window import DropWindow  # noqa: E402


def _noop(*_args, **_kwargs):
    pass


def make_bare_window(**attrs):
    """构造未经 __init__ 的 DropWindow 实例（不创建真实 Tk），按需注入属性。"""
    app = DropWindow.__new__(DropWindow)
    app.actions = queue.Queue()
    app.root = mock.MagicMock()
    app.status_var = mock.MagicMock()
    app.log_text = mock.MagicMock()
    app.copy_button = mock.MagicMock()
    app.folder_list = mock.MagicMock()
    app._worker = None
    app._tray_started = False
    for name, value in attrs.items():
        setattr(app, name, value)
    return app


class WorkerExcludePatternsTests(unittest.TestCase):
    """拖拽窗口过滤模式必须应用 excludePatterns（评审 I-6），与右键 CLI 行为一致。"""

    def _opts(self, hide_git=True):
        return {
            "format": "text", "hide_git": hide_git, "gitignore": False,
            "source_only": False, "size": False, "time": False,
        }

    def _run_worker(self, app, opts):
        config = {
            "excludeDirs": [".git"], "excludeFiles": [],
            "excludePatterns": ["*.log", "dist/*"],
            "maxFiles": 2000, "maxItemsPerLevel": 200, "maxDepth": -1,
        }
        fake_result = mock.Mock(total_files=0, total_dirs=0, truncated=False)
        with mock.patch.object(window_module, "get_effective_config", return_value=config), \
             mock.patch.object(window_module, "scan_directory", return_value=fake_result) as scan_mock, \
             mock.patch.object(window_module, "build_tree_text", return_value=""), \
             mock.patch.object(window_module, "format_output", return_value=""):
            app._worker_main(["C:\\fake"], opts, do_copy=False, do_save=False)
        return scan_mock.call_args.kwargs

    def test_filter_mode_passes_exclude_patterns_from_config(self):
        app = DropWindow.__new__(DropWindow)
        app.actions = queue.Queue()
        kwargs = self._run_worker(app, self._opts(hide_git=True))
        self.assertEqual(kwargs["exclude_patterns"], {"*.log", "dist/*"})

    def test_non_filter_mode_keeps_exclude_patterns_off(self):
        app = DropWindow.__new__(DropWindow)
        app.actions = queue.Queue()
        kwargs = self._run_worker(app, self._opts(hide_git=False))
        self.assertIsNone(kwargs["exclude_patterns"])


class PollLoopTests(unittest.TestCase):
    """_poll 队列状态机特征测试（评审 I-7）：worker/托盘动作经队列回传的消费语义。"""

    def test_log_message_appends_log_only(self):
        app = make_bare_window()
        with mock.patch.object(app, "_append_log") as append_log:
            app.actions.put(("log", "进度一行"))
            app._poll()
        append_log.assert_called_once_with("进度一行")
        app.root.after.assert_called_once()

    def test_done_message_restores_button_and_sets_status(self):
        app = make_bare_window()
        app.actions.put(("done", "处理完成。"))
        app._poll()
        app.copy_button.config.assert_called_with(state="normal")
        app.status_var.set.assert_called_with("处理完成。")

    def test_tray_open_reiconsifies_window(self):
        app = make_bare_window()
        app.actions.put(("tray", "open"))
        app._poll()
        app.root.deiconify.assert_called_once()
        app.root.lift.assert_called_once()

    def test_tray_config_opens_config_file(self):
        app = make_bare_window()
        app.actions.put(("tray", "config"))
        with mock.patch.object(window_module, "open_config_file") as open_config:
            app._poll()
        open_config.assert_called_once()

    def test_tray_exit_quits_and_stops_after_chain(self):
        app = make_bare_window()
        app.actions.put(("tray", "exit"))
        with mock.patch.object(window_module, "stop_tray") as stop_tray:
            app._poll()
        stop_tray.assert_called_once()
        app.root.destroy.assert_called_once()
        # 退出后不再续订轮询，避免对已销毁窗口触发 after 回调
        app.root.after.assert_not_called()

    def test_empty_queue_keeps_polling_without_crash(self):
        app = make_bare_window()
        app._poll()
        app.root.after.assert_called_once_with(100, app._poll)

    def test_dead_worker_with_disabled_button_is_recovered(self):
        dead = threading.Thread(target=_noop)
        dead.start()
        dead.join()
        app = make_bare_window(_worker=dead)
        app.copy_button.__getitem__ = mock.Mock(return_value="disabled")
        app.actions.put(("log", "x"))
        app._poll()
        app.copy_button.config.assert_called_with(state="normal")


class HandleDropTests(unittest.TestCase):
    """WM_DROPFILES 两段式 DragQueryFileW 协议特征测试（评审 I-7）。"""

    def _drop_paths(self, paths):
        def fake_query(_hdrop, index, buf, _size):
            if index == 0xFFFFFFFF:  # 计数查询
                return len(paths)
            if buf is None:  # 长度查询（不含终止符）
                return len(paths[index])
            buf.value = paths[index]  # 内容查询
            return len(paths[index])

        return fake_query

    def _run_drop(self, paths):
        app = make_bare_window()
        app.folder_list.get.return_value = tuple()

        def fake_isdir(path):
            return not path.endswith(".txt")

        with mock.patch.object(window_module.shell32, "DragQueryFileW",
                               side_effect=self._drop_paths(paths)), \
             mock.patch.object(window_module.shell32, "DragAcceptFiles"), \
             mock.patch.object(window_module.os.path, "isdir", side_effect=fake_isdir):
            app._handle_drop(0xABC)
        return app

    def test_empty_drop_finishes_without_insert(self):
        app = self._run_drop([])
        app.folder_list.insert.assert_not_called()

    def test_folder_and_non_folder_are_separated(self):
        app = make_bare_window()
        app.folder_list.get.return_value = tuple()
        with mock.patch.object(window_module.shell32, "DragQueryFileW",
                               side_effect=self._drop_paths([r"D:\资料\项目", r"D:\notes.txt"])), \
             mock.patch.object(window_module.os.path, "isdir",
                               side_effect=lambda p: not p.endswith(".txt")), \
             mock.patch.object(app, "_append_log") as append_log:
            app._handle_drop(0xABC)
        app.folder_list.insert.assert_called_once_with("end", r"D:\资料\项目")
        append_log.assert_called_once()
        self.assertIn("不是文件夹", append_log.call_args[0][0])

    def test_duplicate_folder_is_not_inserted_twice(self):
        app = make_bare_window()
        app.folder_list.get.return_value = (r"D:\already",)

        def fake_query(_hdrop, index, buf, _size):
            if index == 0xFFFFFFFF:
                return 1
            if buf is None:
                return len(r"D:\already")
            buf.value = r"D:\already"
            return len(r"D:\already")

        with mock.patch.object(window_module.shell32, "DragQueryFileW", side_effect=fake_query), \
             mock.patch.object(window_module.os.path, "isdir", return_value=True):
            app._handle_drop(0xABC)
        app.folder_list.insert.assert_not_called()

    def test_many_dropped_folders_all_inserted(self):
        paths = [rf"D:\case{i}" for i in range(50)]
        app = self._run_drop(paths)
        self.assertEqual(app.folder_list.insert.call_count, 50)


class RunActionGuardTests(unittest.TestCase):
    def test_run_action_ignored_while_worker_alive(self):
        gate = threading.Event()
        alive = threading.Thread(target=gate.wait)
        alive.start()
        # addCleanup 是 LIFO：先注册 join、后注册 set，保证 set 先执行再 join
        self.addCleanup(alive.join)
        self.addCleanup(gate.set)
        app = make_bare_window(_worker=alive)
        app.folder_list.get.return_value = ["C:\\fake"]
        with mock.patch.object(window_module.threading, "Thread") as thread_ctor:
            app._run_action(copy=True, save=False)
        thread_ctor.assert_not_called()
        app.status_var.set.assert_called_once()
        self.assertIn("上一批", app.status_var.set.call_args[0][0])


class TrayToggleTests(unittest.TestCase):
    def test_toggle_on_persists_config_and_starts_tray(self):
        app = make_bare_window()
        app.tray_var = mock.Mock()
        app.tray_var.get.return_value = True
        with mock.patch.object(window_module, "update_config_values") as update, \
             mock.patch.object(app, "_ensure_tray") as ensure:
            app._on_tray_toggle()
        update.assert_called_once_with({"enableTray": True})
        ensure.assert_called_once()

    def test_toggle_off_persists_config_without_starting_tray(self):
        app = make_bare_window()
        app.tray_var = mock.Mock()
        app.tray_var.get.return_value = False
        with mock.patch.object(window_module, "update_config_values") as update, \
             mock.patch.object(app, "_ensure_tray") as ensure:
            app._on_tray_toggle()
        update.assert_called_once_with({"enableTray": False})
        ensure.assert_not_called()
    """UI 初始值应取自配置（评审 M-7），使窗口入口与右键入口默认行为一致。"""

    def test_values_come_from_config(self):
        config = {
            "defaultFormat": "json", "showFileSize": True,
            "showFileTime": True, "respectGitignore": True,
        }
        values = window_module._initial_ui_values(config)
        self.assertEqual(values["format_label"], "JSON")
        self.assertTrue(values["show_size"])
        self.assertTrue(values["show_time"])
        self.assertTrue(values["gitignore"])

    def test_unknown_format_falls_back_to_text_label(self):
        values = window_module._initial_ui_values({"defaultFormat": "no-such"})
        self.assertEqual(values["format_label"], "树状文本")

    def test_empty_config_uses_defaults(self):
        values = window_module._initial_ui_values({})
        self.assertEqual(values["format_label"], "树状文本")
        self.assertFalse(values["show_size"])
        self.assertFalse(values["show_time"])
        self.assertFalse(values["gitignore"])


class InstallBannerTests(unittest.TestCase):
    """安装状态横幅（Task 4）：ok 态零控件；非 ok 态渲染文案与 0~1 个注入回调按钮。

    状态字符串与 __main__ 的 _INSTALL_STATE_* 常量保持契约一致（window 禁止
    import __main__，避免循环导入），测试直接用字面量。
    回调契约：_install_actions 值为无参 callable，返回 bool 表示成功。
    """

    def _build_banner(self, install_state, install_info=None, install_actions=None):
        """在 __new__ 实例上跑 _build_status_banner，ttk 整体替换为 mock 以捕获控件参数。"""
        app = DropWindow.__new__(DropWindow)
        app.install_state = install_state
        app.install_info = install_info
        actions = {} if install_actions is None else install_actions
        # 与 __init__ 一致：install_actions 与运行时别名 _install_actions 指向同一 dict
        app.install_actions = actions
        app._install_actions = actions
        app._banner_frame = None
        parent = mock.Mock()
        with mock.patch.object(window_module, "ttk") as ttk_mock:
            app._build_status_banner(parent)
        return app, ttk_mock, parent

    def test_banner_hidden_when_state_ok(self):
        app, ttk_mock, _parent = self._build_banner("ok")
        ttk_mock.Frame.assert_not_called()
        ttk_mock.Label.assert_not_called()
        ttk_mock.Button.assert_not_called()
        self.assertIsNone(app._banner_frame)

    def test_banner_shows_update_button_with_versions(self):
        actions = {"update": mock.Mock(return_value=True)}
        app, ttk_mock, parent = self._build_banner(
            "update", install_info={"installed_version": "1.0.0"}, install_actions=actions)
        frame = ttk_mock.Frame.return_value
        ttk_mock.Frame.assert_called_once_with(parent)
        self.assertIs(app._banner_frame, frame)
        # 横幅先于列表区占满一行
        frame.pack.assert_called_once_with(fill="x")
        label_text = ttk_mock.Label.call_args.kwargs["text"]
        self.assertIn("v1.0.0", label_text)
        self.assertIn(f"v{window_module.VERSION}", label_text)
        button_kwargs = ttk_mock.Button.call_args.kwargs
        self.assertEqual(button_kwargs["text"], f"更新到 v{window_module.VERSION}")
        self.assertTrue(callable(button_kwargs["command"]))

    def test_banner_shows_install_button_when_not_installed(self):
        actions = {"not-installed": mock.Mock(return_value=True)}
        app, ttk_mock, _parent = self._build_banner("not-installed", install_actions=actions)
        self.assertIsNotNone(app._banner_frame)
        label_text = ttk_mock.Label.call_args.kwargs["text"]
        self.assertIn("尚未安装右键菜单", label_text)
        self.assertEqual(ttk_mock.Button.call_args.kwargs["text"], "安装右键菜单")

    def test_downgrade_button_maps_to_uninstall_action(self):
        action = mock.Mock(return_value=True)
        app, ttk_mock, _parent = self._build_banner(
            "downgrade", install_info={"installed_version": "9.9.9"},
            install_actions={"uninstall": action})
        label_text = ttk_mock.Label.call_args.kwargs["text"]
        self.assertIn("v9.9.9", label_text)
        self.assertIn(f"v{window_module.VERSION}", label_text)
        self.assertEqual(ttk_mock.Button.call_args.kwargs["text"], "卸载已装副本…")
        ttk_mock.Button.call_args.kwargs["command"]()
        action.assert_called_once_with()

    def test_repair_and_migrate_states_render_their_buttons(self):
        cases = (("repair", "重新安装"), ("migrate", "迁移到标准位置"))
        for state, button_text in cases:
            with self.subTest(state=state):
                action = mock.Mock(return_value=True)
                _app, ttk_mock, _parent = self._build_banner(
                    state, install_actions={state: action})
                self.assertEqual(ttk_mock.Button.call_args.kwargs["text"], button_text)
                ttk_mock.Button.call_args.kwargs["command"]()
                action.assert_called_once_with()

    def test_banner_without_injected_action_renders_label_only(self):
        # install_actions 缺省为 {}：只提示，不渲染无回调可走的按钮
        app, ttk_mock, _parent = self._build_banner("not-installed", install_actions={})
        self.assertIsNotNone(app._banner_frame)
        ttk_mock.Label.assert_called_once()
        ttk_mock.Button.assert_not_called()

    def test_update_button_invokes_injected_action(self):
        action = mock.Mock(return_value=True)
        app, ttk_mock, _parent = self._build_banner("update", install_actions={"update": action})
        command = ttk_mock.Button.call_args.kwargs["command"]
        command()
        action.assert_called_once_with()

    def test_banner_destroyed_after_action_success(self):
        app = DropWindow.__new__(DropWindow)
        app._install_actions = {"update": mock.Mock(return_value=True)}
        banner = mock.Mock()
        app._banner_frame = banner
        app._on_install_action("update")
        banner.destroy.assert_called_once_with()
        self.assertIsNone(app._banner_frame)

    def test_banner_kept_after_action_failure(self):
        app = DropWindow.__new__(DropWindow)
        action = mock.Mock(return_value=False)
        app._install_actions = {"update": action}
        banner = mock.Mock()
        app._banner_frame = banner
        app._on_install_action("update")
        banner.destroy.assert_not_called()
        self.assertIs(banner, app._banner_frame)
        # 失败后横幅保留，可重试
        action.return_value = True
        app._on_install_action("update")
        self.assertEqual(action.call_count, 2)
        banner.destroy.assert_called_once_with()

    def test_missing_action_key_is_noop(self):
        app = DropWindow.__new__(DropWindow)
        app._install_actions = {}
        banner = mock.Mock()
        app._banner_frame = banner
        app._on_install_action("update")  # 不应抛异常
        banner.destroy.assert_not_called()
        self.assertIs(banner, app._banner_frame)

    def test_run_drop_window_forwards_params(self):
        info = {"installed_version": "1.0.0"}
        actions = {"update": lambda: True}
        with mock.patch.object(window_module, "DropWindow") as window_ctor, \
             mock.patch.object(window_module, "stop_tray") as stop_tray:
            window_module.run_drop_window("update", info, actions)
        window_ctor.assert_called_once_with(
            install_state="update", install_info=info, install_actions=actions)
        window_ctor.return_value.run.assert_called_once_with()
        stop_tray.assert_called_once()


if __name__ == "__main__":
    unittest.main()
