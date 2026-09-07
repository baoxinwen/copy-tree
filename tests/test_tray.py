import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from copytree import tray  # noqa: E402
from copytree import window as window_module  # noqa: E402
from copytree.window import DropWindow  # noqa: E402


def _noop(*_args, **_kwargs):
    pass


class StartTrayResultTests(unittest.TestCase):
    """托盘初始化失败必须如实上报（评审 I-5）。

    背景：窗口关闭驻留托盘时会先 start_tray 再 withdraw 主窗口；若托盘
    实际没建起来却返回 True，进程将变得完全不可见。
    """

    def test_register_class_failure_returns_false(self):
        with mock.patch.object(tray, "_tray_thread", None), \
             mock.patch.object(tray.user32, "RegisterClassW", return_value=0):
            ok = tray.start_tray(_noop, _noop, _noop)
        self.assertFalse(ok)

    def test_shell_notify_failure_returns_false(self):
        created = {"hwnd": False}

        def fake_create_window(*_args, **_kwargs):
            created["hwnd"] = True
            return 0x1234

        with mock.patch.object(tray, "_tray_thread", None), \
             mock.patch.object(tray.user32, "RegisterClassW", return_value=1), \
             mock.patch.object(tray.user32, "CreateWindowExW", side_effect=fake_create_window), \
             mock.patch.object(tray.shell32, "Shell_NotifyIconW", return_value=0), \
             mock.patch.object(tray, "_load_icon", return_value=None):
            ok = tray.start_tray(_noop, _noop, _noop)
        self.assertTrue(created["hwnd"])
        self.assertFalse(ok)


class DropWindowCloseTests(unittest.TestCase):
    """托盘不可用时窗口不得隐藏（评审 I-5），成功路径行为不变。"""

    def _make_window(self):
        app = DropWindow.__new__(DropWindow)
        app._tray_started = False
        app.tray_var = mock.Mock()
        app.tray_var.get.return_value = True
        app.root = mock.Mock()
        app.status_var = mock.Mock()
        return app

    def test_close_keeps_window_when_tray_unavailable(self):
        app = self._make_window()
        with mock.patch.object(window_module, "start_tray", return_value=False):
            app._on_close()
        app.root.withdraw.assert_not_called()
        status_text = app.status_var.set.call_args[0][0]
        self.assertIn("托盘不可用", status_text)

    def test_close_withdraws_when_tray_ready(self):
        app = self._make_window()
        with mock.patch.object(window_module, "start_tray", return_value=True):
            app._on_close()
        app.root.withdraw.assert_called_once()

    def test_failed_tray_can_retry_on_next_close(self):
        app = self._make_window()
        with mock.patch.object(window_module, "start_tray", return_value=False):
            app._on_close()
        self.assertFalse(app._tray_started)
        with mock.patch.object(window_module, "start_tray", return_value=True):
            app._on_close()
        self.assertTrue(app._tray_started)
        self.assertEqual(app.root.withdraw.call_count, 1)


class WndProcTaskbarTests(unittest.TestCase):
    """Explorer 重启广播 TaskbarCreated 后重新挂载图标（评审 I-5）；退出销毁 HICON（评审 M-6）。"""

    def _state(self, taskbar_msg=0x8FAC):
        return {"taskbar_msg": taskbar_msg, "nid": None, "hicon": None}

    def test_readds_icon_on_taskbar_created(self):
        state = self._state()
        state["nid"] = tray._NOTIFYICONDATAW()
        proc = tray._make_wndproc(_noop, _noop, _noop, state)
        with mock.patch.object(tray.shell32, "Shell_NotifyIconW") as notify_mock:
            ret = proc(0x1234, state["taskbar_msg"], 0, 0)
        self.assertEqual(ret, 0)
        notify_mock.assert_called_once()
        self.assertEqual(notify_mock.call_args[0][0], tray.NIM_ADD)

    def test_ignores_taskbar_broadcast_when_icon_never_added(self):
        state = self._state()
        proc = tray._make_wndproc(_noop, _noop, _noop, state)
        with mock.patch.object(tray.shell32, "Shell_NotifyIconW") as notify_mock:
            proc(0x1234, state["taskbar_msg"], 0, 0)
        notify_mock.assert_not_called()

    def test_destroy_icon_on_close(self):
        state = self._state()
        state["nid"] = tray._NOTIFYICONDATAW()
        hicon = mock.Mock()
        state["hicon"] = hicon
        proc = tray._make_wndproc(_noop, _noop, _noop, state)
        with mock.patch.object(tray.shell32, "Shell_NotifyIconW"), \
             mock.patch.object(tray.user32, "DestroyWindow") as destroy_mock, \
             mock.patch.object(tray.user32, "DestroyIcon") as destroy_icon_mock:
            proc(0x1234, tray.WM_CLOSE, 0, 0)
        destroy_icon_mock.assert_called_once_with(hicon)
        self.assertIsNone(state["hicon"])
        destroy_mock.assert_called_once()


class StartTrayGuardTests(unittest.TestCase):
    """托盘线程已在运行时重复启动是幂等的（评审 I-7：并发重复）。"""

    def test_start_tray_returns_true_when_thread_alive(self):
        gate = threading.Event()
        alive = threading.Thread(target=gate.wait)
        alive.start()
        # addCleanup 是 LIFO：先注册 join、后注册 set，保证 set 先执行再 join
        self.addCleanup(alive.join)
        self.addCleanup(gate.set)
        with mock.patch.object(tray, "_tray_thread", alive), \
             mock.patch.object(tray.threading, "Thread") as thread_ctor:
            ok = tray.start_tray(_noop, _noop, _noop)
        self.assertTrue(ok)
        thread_ctor.assert_not_called()


class StopTrayTests(unittest.TestCase):
    def test_stop_tray_posts_close_to_registered_window(self):
        with mock.patch.object(tray, "_tray_hwnd", 0x1234), \
             mock.patch.object(tray.user32, "PostMessageW") as post:
            tray.stop_tray()
        post.assert_called_once_with(0x1234, tray.WM_CLOSE, 0, 0)

    def test_stop_tray_without_window_is_noop(self):
        with mock.patch.object(tray, "_tray_hwnd", None), \
             mock.patch.object(tray.user32, "PostMessageW") as post:
            tray.stop_tray()
        post.assert_not_called()


class ShowMenuTests(unittest.TestCase):
    """托盘右键菜单构造与资源释放（评审 I-7）。"""

    def test_menu_builds_three_items_and_destroys_menu(self):
        appends = []

        def fake_append(menu, flags, item_id, text):
            appends.append(item_id)
            return 1

        with mock.patch.object(tray.user32, "CreatePopupMenu", return_value=0x55), \
             mock.patch.object(tray.user32, "AppendMenuW", side_effect=fake_append), \
             mock.patch.object(tray.user32, "GetCursorPos", return_value=1), \
             mock.patch.object(tray.user32, "SetForegroundWindow", return_value=1), \
             mock.patch.object(tray.user32, "TrackPopupMenu", return_value=1), \
             mock.patch.object(tray.user32, "PostMessageW", return_value=1), \
             mock.patch.object(tray.user32, "DestroyMenu") as destroy:
            tray._show_menu(0x1234, _noop, _noop, _noop)
        self.assertEqual(appends, [tray._MENU_OPEN, tray._MENU_CONFIG, tray._MENU_EXIT])
        destroy.assert_called_once_with(0x55)

    def test_menu_creation_failure_is_silent(self):
        with mock.patch.object(tray.user32, "CreatePopupMenu", return_value=0), \
             mock.patch.object(tray.user32, "AppendMenuW") as append:
            tray._show_menu(0x1234, _noop, _noop, _noop)
        append.assert_not_called()


class WndProcUnknownInputTests(unittest.TestCase):
    """未知菜单命令 / 未知回调参数不得触发任何动作（评审 I-7：非法输入）。"""

    def test_unknown_command_id_triggers_nothing(self):
        state = {"taskbar_msg": 0x8FAC, "nid": None, "hicon": None}
        on_open, on_config, on_exit = mock.Mock(), mock.Mock(), mock.Mock()
        proc = tray._make_wndproc(on_open, on_config, on_exit, state)
        ret = proc(0x1234, tray.WM_COMMAND, 0x9999, 0)
        self.assertEqual(ret, 0)
        on_open.assert_not_called()
        on_config.assert_not_called()
        on_exit.assert_not_called()

    def test_unknown_callback_lparam_triggers_nothing(self):
        state = {"taskbar_msg": 0x8FAC, "nid": None, "hicon": None}
        on_open, on_config, on_exit = mock.Mock(), mock.Mock(), mock.Mock()
        proc = tray._make_wndproc(on_open, on_config, on_exit, state)
        ret = proc(0x1234, tray.WM_APP_TRAY_CALLBACK, 0, 0x0201)  # 非 DBLCLK/非 RBUTTONUP
        self.assertEqual(ret, 0)
        on_open.assert_not_called()
        on_config.assert_not_called()
        on_exit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
