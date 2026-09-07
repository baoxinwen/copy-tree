"""Start Menu 快捷方式 COM 协议的特征测试（评审 I-7 延后项）。

在内存中伪造 IShellLinkW / IPropertyStore / IPersistFile COM 对象
（手工 vtable + WINFUNCTYPE 回调），让 shortcut._vtcall 走真实的
slot 寻址与调用路径——锁定 AGENTS 记录的血泪教训区域（vtable 偏移、
Release 配对、套间初始化语义）。对现有代码应全绿；变红即协议回归。
"""

import ctypes
from ctypes import HRESULT, POINTER, c_void_p, byref
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from copytree import shortcut  # noqa: E402
from copytree.shortcut import GUID, PROPVARIANT, PROPERTYKEY  # noqa: E402

E_NOINTERFACE = -2147467262


class FakeInterface:
    """内存中的假 COM 对象：对象内存首字段指向手工 vtable。"""

    def __init__(self, slots):
        self.releases = 0
        self._keep = list(slots.values())
        size = max(slots) + 1
        vtable = (ctypes.c_void_p * size)()
        for index, callback in slots.items():
            vtable[index] = ctypes.cast(callback, ctypes.c_void_p)
        obj = ctypes.create_string_buffer(ctypes.sizeof(ctypes.c_void_p))
        vtable_addr = ctypes.c_void_p(ctypes.addressof(vtable))
        ctypes.memmove(obj, byref(vtable_addr), ctypes.sizeof(ctypes.c_void_p))
        self.ptr = c_void_p(ctypes.addressof(obj))
        self._keep.extend([vtable, obj])

    def _release_slot(self):
        proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p)

        def release(_this):
            self.releases += 1
            return 0

        callback = proto(release)
        self._keep.append(callback)
        return callback

    def _query_interface_slot(self, targets):
        proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(GUID), POINTER(c_void_p))

        def query_interface(_this, riid, out):
            target = targets.get(bytes(riid.contents))
            if target is None:
                return E_NOINTERFACE
            out[0] = target
            return 0

        callback = proto(query_interface)
        self._keep.append(callback)
        return callback


class FakeShellLink(FakeInterface):
    def __init__(self, set_path_hr=0, set_args_hr=0):
        self._keep = []
        self.set_path_calls = []
        self.set_args_calls = []
        self.set_path_hr = set_path_hr
        self.set_args_hr = set_args_hr
        self.qi_targets = {}
        super().__init__({
            0: self._query_interface_slot(self.qi_targets),
            2: self._release_slot(),
            11: self._set_arguments_slot(),
            20: self._set_path_slot(),
        })

    def _set_path_slot(self):
        proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, ctypes.c_wchar_p)

        def set_path(_this, path):
            self.set_path_calls.append(path)
            return self.set_path_hr

        callback = proto(set_path)
        self._keep.append(callback)
        return callback

    def _set_arguments_slot(self):
        proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, ctypes.c_wchar_p)

        def set_arguments(_this, args):
            self.set_args_calls.append(args)
            return self.set_args_hr

        callback = proto(set_arguments)
        self._keep.append(callback)
        return callback


class FakePropertyStore(FakeInterface):
    def __init__(self):
        self._keep = []
        self.set_values = []
        self.commits = 0
        super().__init__({
            0: self._query_interface_slot({}),
            2: self._release_slot(),
            6: self._set_value_slot(),
            7: self._commit_slot(),
        })

    def _set_value_slot(self):
        proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))

        def set_value(_this, key, pv):
            self.set_values.append((key.contents.pid, pv.contents.vt))
            return 0

        callback = proto(set_value)
        self._keep.append(callback)
        return callback

    def _commit_slot(self):
        proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p)

        def commit(_this):
            self.commits += 1
            return 0

        callback = proto(commit)
        self._keep.append(callback)
        return callback


class FakePersistFile(FakeInterface):
    def __init__(self, save_hr=0):
        self._keep = []
        self.save_calls = []
        self.save_hr = save_hr
        super().__init__({
            0: self._query_interface_slot({}),
            2: self._release_slot(),
            6: self._save_slot(),
        })

    def _save_slot(self):
        proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, ctypes.c_wchar_p, ctypes.c_int)

        def save(_this, path, _flags):
            self.save_calls.append(path)
            return self.save_hr

        callback = proto(save)
        self._keep.append(callback)
        return callback


class ShortcutProtocolTests(unittest.TestCase):
    def _wire(self, shell_link, **ole32_overrides):
        """桩住 ole32 入口，把 CoCreateInstance 指向假 ShellLink。

        CoCreateInstance 用带原型的 ctypes 函数指针替换（而非 Mock）：
        出参 byref(ptr) 经真实指针语义写入，与 COM 调用约定一致。
        """
        self.shortcut_dir = tempfile.mkdtemp(prefix="ct_shortcut_")
        self.addCleanup(_cleanup, self.shortcut_dir)
        self.co_create_calls = 0

        proto = ctypes.WINFUNCTYPE(
            HRESULT, POINTER(GUID), c_void_p, ctypes.c_uint32, POINTER(GUID), POINTER(c_void_p)
        )

        def fake_co_create_instance(_clsid, _outer, _ctx, _riid, out):
            self.co_create_calls += 1
            hr = ole32_overrides.get("co_create_hr", 0)
            if hr:
                return hr
            out[0] = shell_link.ptr.value
            return 0

        self._fake_co_create = proto(fake_co_create_instance)  # 持引用防 GC
        patchers = [
            mock.patch.object(shortcut.ole32, "CoInitializeEx",
                              return_value=ole32_overrides.get("coinit_hr", 0)),
            mock.patch.object(shortcut.ole32, "CoCreateInstance", new=self._fake_co_create),
            mock.patch.object(shortcut.ole32, "CoUninitialize"),
            mock.patch.object(shortcut, "SHORTCUT_DIR", self.shortcut_dir),
        ]
        mocks = {}
        for patcher in patchers:
            mocks[patcher.attribute] = patcher.start()
            self.addCleanup(patcher.stop)
        self.shell_link = shell_link
        return mocks

    def test_success_sets_path_args_appid_and_saves(self):
        ps = FakePropertyStore()
        pf = FakePersistFile()
        shell = FakeShellLink()
        shell.qi_targets.update({
            bytes(shortcut.IID_IPropertyStore): ps.ptr.value,
            bytes(shortcut.IID_IPersistFile): pf.ptr.value,
        })
        mocks = self._wire(shell)
        ok = shortcut.create_start_menu_shortcut(
            r"C:\app\copy-tree.exe", "--uninstall", "卸载 copy-tree.lnk"
        )
        self.assertTrue(ok)
        self.assertEqual(shell.set_path_calls, [r"C:\app\copy-tree.exe"])
        self.assertEqual(shell.set_args_calls, ["--uninstall"])
        # AppUserModelID：PKEY pid=5、VT_LPWSTR(31)
        self.assertEqual(ps.set_values, [(5, 31)])
        self.assertEqual(ps.commits, 1)
        self.assertEqual(pf.save_calls, [os.path.join(self.shortcut_dir, "卸载 copy-tree.lnk")])
        # 三个接口各 Release 恰好一次；本次真实初始化过 COM 需配对 CoUninitialize
        self.assertEqual((shell.releases, ps.releases, pf.releases), (1, 1, 1))
        mocks["CoUninitialize"].assert_called_once()

    def test_set_path_failure_releases_and_fails(self):
        ps = FakePropertyStore()
        pf = FakePersistFile()
        shell = FakeShellLink(set_path_hr=1)
        shell.qi_targets.update({
            bytes(shortcut.IID_IPropertyStore): ps.ptr.value,
            bytes(shortcut.IID_IPersistFile): pf.ptr.value,
        })
        self._wire(shell)
        self.assertFalse(shortcut.create_start_menu_shortcut(r"C:\app\copy-tree.exe"))
        self.assertEqual(shell.releases, 1)
        self.assertEqual(pf.save_calls, [])

    def test_coinitialize_hard_failure_aborts(self):
        shell = FakeShellLink()
        mocks = self._wire(shell, coinit_hr=-2147418113)
        self.assertFalse(shortcut.create_start_menu_shortcut(r"C:\app\copy-tree.exe"))
        self.assertEqual(self.co_create_calls, 0)
        mocks["CoUninitialize"].assert_not_called()

    def test_changed_mode_continues_without_uninitialize(self):
        # 并发场景：COM 套间已被他处初始化（RPC_E_CHANGED_MODE）时仍继续，
        # 但因本次未真正初始化，不得配对 CoUninitialize
        shell = FakeShellLink()
        mocks = self._wire(shell, coinit_hr=shortcut.RPC_E_CHANGED_MODE, co_create_hr=1)
        self.assertFalse(shortcut.create_start_menu_shortcut(r"C:\app\copy-tree.exe"))
        self.assertEqual(self.co_create_calls, 1)
        mocks["CoUninitialize"].assert_not_called()

    def test_save_failure_releases_all_and_reports_false(self):
        ps = FakePropertyStore()
        pf = FakePersistFile(save_hr=1)
        shell = FakeShellLink()
        shell.qi_targets.update({
            bytes(shortcut.IID_IPropertyStore): ps.ptr.value,
            bytes(shortcut.IID_IPersistFile): pf.ptr.value,
        })
        self._wire(shell)
        self.assertFalse(shortcut.create_start_menu_shortcut(r"C:\app\copy-tree.exe"))
        self.assertEqual((shell.releases, ps.releases, pf.releases), (1, 1, 1))


class RemoveShortcutTests(unittest.TestCase):
    def test_removes_both_shortcuts_and_is_idempotent(self):
        directory = tempfile.mkdtemp(prefix="ct_shortcut_rm_")
        self.addCleanup(_cleanup, directory)
        for name in (shortcut.SHORTCUT_NAME, shortcut.SHORTCUT_UNINSTALL_NAME):
            with open(os.path.join(directory, name), "w", encoding="utf-8") as f:
                f.write("")
        with mock.patch.object(shortcut, "SHORTCUT_DIR", directory):
            self.assertTrue(shortcut.remove_start_menu_shortcut())
            self.assertEqual(os.listdir(directory), [])
            # 幂等：文件已不存在时再次删除仍成功
            self.assertTrue(shortcut.remove_start_menu_shortcut())

    def test_removal_failure_reports_false(self):
        directory = tempfile.mkdtemp(prefix="ct_shortcut_rm2_")
        self.addCleanup(_cleanup, directory)
        with mock.patch.object(shortcut, "SHORTCUT_DIR", directory), \
             mock.patch.object(shortcut.os, "remove", side_effect=OSError("被占用")):
            with open(os.path.join(directory, shortcut.SHORTCUT_NAME), "w", encoding="utf-8") as f:
                f.write("")
            self.assertFalse(shortcut.remove_start_menu_shortcut())


class MakeLpwstrTests(unittest.TestCase):
    def test_builds_vt_lpwstr_propvariant(self):
        pv, keeper = shortcut._make_lpWSTR("CopyTree.CopyTree")
        self.assertEqual(pv.vt, 31)
        self.assertTrue(pv.p)
        self.assertTrue(keeper)

    def test_empty_value_is_accepted(self):
        pv, _keeper = shortcut._make_lpWSTR("")
        self.assertEqual(pv.vt, 31)


def _cleanup(path):
    import shutil

    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
