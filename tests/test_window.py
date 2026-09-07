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


if __name__ == "__main__":
    unittest.main()
