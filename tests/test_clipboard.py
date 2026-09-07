"""剪贴板写入契约的特征测试（评审 I-7 延后项）。

特征测试定义：锁定当前真实行为，对现有代码应全绿；变红即行为回归。
Win32 桩打在 clipboard 模块自有的 kernel32/user32 WinDLL 实例上，
不影响其他模块。GlobalLock 返回真实可写内存地址，使 memmove 编码
行为可被真实断言（而非仅验证 mock 调用）。
"""

import ctypes
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from copytree import clipboard  # noqa: E402

_FAKE_HANDLE = 0x1234


def stub_clipboard(test: unittest.TestCase, **overrides):
    """桩住 clipboard 模块的 Win32 依赖；返回 (mocks, buf)。

    overrides 按函数名替换默认返回值；buf 为 GlobalLock 指向的
    真实可写内存，供断言实际写入的字节。
    """
    buf = ctypes.create_string_buffer(2 * 1024 * 1024 + 16)
    specs = [
        (clipboard.kernel32, "GlobalAlloc", _FAKE_HANDLE),
        (clipboard.kernel32, "GlobalLock", ctypes.addressof(buf)),
        (clipboard.kernel32, "GlobalUnlock", 1),
        (clipboard.kernel32, "GlobalFree", None),
        (clipboard.user32, "OpenClipboard", 1),
        (clipboard.user32, "EmptyClipboard", 1),
        (clipboard.user32, "SetClipboardData", 0x5678),
        (clipboard.user32, "CloseClipboard", 1),
    ]
    mocks = {}
    for target, name, default in specs:
        patcher = mock.patch.object(target, name, return_value=overrides.get(name, default))
        mocks[name] = patcher.start()
        test.addCleanup(patcher.stop)
    sleep_patcher = mock.patch.object(clipboard.time, "sleep", return_value=None)
    mocks["sleep"] = sleep_patcher.start()
    test.addCleanup(sleep_patcher.stop)
    return mocks, buf


class CopySuccessTests(unittest.TestCase):
    def test_success_encodes_utf16_with_terminator(self):
        mocks, buf = stub_clipboard(self)
        self.assertTrue(clipboard.copy_to_clipboard("Aü", max_retries=1))
        expected = "Aü".encode("utf-16-le") + b"\x00\x00"
        self.assertEqual(buf.raw[: len(expected)], expected)
        mocks["SetClipboardData"].assert_called_once_with(
            clipboard.CF_UNICODETEXT, _FAKE_HANDLE
        )

    def test_empty_text_is_valid_input(self):
        mocks, buf = stub_clipboard(self)
        self.assertTrue(clipboard.copy_to_clipboard("", max_retries=1))
        # 空串也要写入 UTF-16 终止符：按 2 字节分配
        self.assertEqual(mocks["GlobalAlloc"].call_args[0][1], 2)
        self.assertEqual(buf.raw[:2], b"\x00\x00")

    def test_one_megabyte_text_allocates_full_buffer(self):
        text = "x" * (1024 * 1024)
        mocks, _buf = stub_clipboard(self)
        self.assertTrue(clipboard.copy_to_clipboard(text, max_retries=1))
        self.assertEqual(mocks["GlobalAlloc"].call_args[0][1], 2 * (1024 * 1024) + 2)

    def test_emoji_encoded_as_utf16_surrogate_pair(self):
        mocks, buf = stub_clipboard(self)
        self.assertTrue(clipboard.copy_to_clipboard("📁", max_retries=1))
        expected = "📁".encode("utf-16-le") + b"\x00\x00"
        self.assertEqual(buf.raw[: len(expected)], expected)
        self.assertEqual(expected, b"\x3d\xd8\xc1\xdc\x00\x00")  # D83D DCC1 + 终止符

    def test_empty_clipboard_precedes_set_clipboard_data(self):
        mocks, _buf = stub_clipboard(self)
        order = []
        mocks["EmptyClipboard"].side_effect = lambda: (order.append("empty"), 1)[1]
        mocks["SetClipboardData"].side_effect = lambda *a: (order.append("set"), 0x5678)[1]
        self.assertTrue(clipboard.copy_to_clipboard("x", max_retries=1))
        self.assertEqual(order, ["empty", "set"])


class FailureStageTests(unittest.TestCase):
    def test_global_alloc_failure_reports_memory_stage(self):
        mocks, _buf = stub_clipboard(self, GlobalAlloc=None)
        self.assertFalse(clipboard.copy_to_clipboard("x", max_retries=1))
        self.assertEqual(clipboard.get_last_failure_stage(), "memory")
        # 分配失败：无内存可释放，也不该碰剪贴板
        mocks["GlobalFree"].assert_not_called()
        mocks["OpenClipboard"].assert_not_called()

    def test_global_lock_failure_frees_memory(self):
        mocks, _buf = stub_clipboard(self, GlobalLock=None)
        self.assertFalse(clipboard.copy_to_clipboard("x", max_retries=1))
        self.assertEqual(clipboard.get_last_failure_stage(), "memory")
        mocks["GlobalFree"].assert_called_once_with(_FAKE_HANDLE)

    def test_open_clipboard_failure_frees_memory_and_reports_busy(self):
        mocks, _buf = stub_clipboard(self, OpenClipboard=0)
        self.assertFalse(clipboard.copy_to_clipboard("x", max_retries=1))
        self.assertEqual(clipboard.get_last_failure_stage(), "busy")
        mocks["GlobalFree"].assert_called_once_with(_FAKE_HANDLE)
        # 剪贴板未打开成功时不得清空他人剪贴板
        mocks["SetClipboardData"].assert_not_called()

    def test_set_clipboard_data_failure_closes_clipboard_and_frees(self):
        mocks, _buf = stub_clipboard(self, SetClipboardData=None)
        self.assertFalse(clipboard.copy_to_clipboard("x", max_retries=1))
        self.assertEqual(clipboard.get_last_failure_stage(), "busy")
        mocks["GlobalFree"].assert_called_once_with(_FAKE_HANDLE)
        # 失败路径也要释放剪贴板锁
        mocks["CloseClipboard"].assert_called_once()


class RetryTests(unittest.TestCase):
    def test_busy_clipboard_retries_until_success(self):
        mocks, _buf = stub_clipboard(self)
        # 前两次被剪贴板管理器占用（并发争用），第三次成功
        mocks["OpenClipboard"].side_effect = [0, 0, 1]
        self.assertTrue(clipboard.copy_to_clipboard("x", max_retries=3))
        self.assertEqual(mocks["OpenClipboard"].call_count, 3)
        # 指数退避间隔：0.15s / 0.45s
        self.assertEqual(
            [call.args[0] for call in mocks["sleep"].call_args_list],
            [0.15, 0.45],
        )

    def test_permanent_busy_gives_up_after_max_retries(self):
        mocks, _buf = stub_clipboard(self, OpenClipboard=0)
        self.assertFalse(clipboard.copy_to_clipboard("x", max_retries=3))
        self.assertEqual(mocks["OpenClipboard"].call_count, 3)
        self.assertEqual(mocks["GlobalFree"].call_count, 3)  # 每次尝试都释放内存
        self.assertEqual(clipboard.get_last_failure_stage(), "busy")


if __name__ == "__main__":
    unittest.main()
