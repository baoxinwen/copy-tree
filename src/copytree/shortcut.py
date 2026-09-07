"""Start Menu 快捷方式创建与删除。通过 ctypes COM 调用 IShellLinkW。"""

import ctypes
import ctypes.wintypes
import os
from ctypes import HRESULT, POINTER, byref, c_void_p

from loguru import logger

from .constants import APP_ID, SHORTCUT_DIR, SHORTCUT_NAME, SHORTCUT_UNINSTALL_NAME

ole32 = ctypes.windll.ole32

ole32.IIDFromString.restype = HRESULT
ole32.IIDFromString.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
ole32.CoInitializeEx.restype = HRESULT
ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.wintypes.DWORD]
ole32.CoUninitialize.restype = None
ole32.CoUninitialize.argtypes = []
ole32.CoCreateInstance.restype = HRESULT
ole32.CoCreateInstance.argtypes = [
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.wintypes.DWORD,
    ctypes.c_void_p,
    ctypes.c_void_p,
]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", ctypes.c_ulong)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("wReserved1", ctypes.c_ushort),
        ("wReserved2", ctypes.c_ushort),
        ("wReserved3", ctypes.c_ushort),
        ("p", ctypes.c_void_p),
        # 对齐真实 PROPVARIANT 的 x64 尺寸（含 DECIMAL 联合的剩余部分），
        # 当前仅使用指针成员，但保持结构尺寸与系统一致更安全。
        ("__union_rest", ctypes.c_ubyte * 8),
    ]


# ── 接口 GUID ──
IID_IShellLinkW = GUID()
IID_IPersistFile = GUID()
IID_IPropertyStore = GUID()
CLSID_ShellLink = GUID()

ole32.IIDFromString("{000214F9-0000-0000-C000-000000000046}", byref(IID_IShellLinkW))
ole32.IIDFromString("{0000010B-0000-0000-C000-000000000046}", byref(IID_IPersistFile))
ole32.IIDFromString("{886D8EEB-8CF2-4444-8D02-CDBA1DBDCF99}", byref(IID_IPropertyStore))
ole32.IIDFromString("{00021401-0000-0000-C000-000000000046}", byref(CLSID_ShellLink))

# PKEY_AppUserModelID: {9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}, 5
PKEY_AppUserModelID = PROPERTYKEY()
ole32.IIDFromString("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}", byref(PKEY_AppUserModelID.fmtid))
PKEY_AppUserModelID.pid = 5

# ── COM vtable slot 偏移量 ──
# 以下偏移量基于 Windows 10 SDK (10.0.19041) 的 COM 接口定义。
# IShellLinkW vtable: IUnknown(3) + GetPath(3) GetIDList(4) SetIDList(5) GetDescription(6)
# SetDescription(7) GetWorkingDirectory(8) SetWorkingDirectory(9) GetArguments(10) SetArguments(11)
# GetHotkey(12) SetHotkey(13) GetShowCmd(14) SetShowCmd(15) GetIconLocation(16) SetIconLocation(17)
# SetRelativePath(18) Resolve(19) SetPath(20)。
# IPersistFile 是独立接口(通过 QueryInterface 获取): IUnknown(3) 后 Save 在 slot 6。
# IPropertyStore 是独立接口(通过 QueryInterface 获取): IUnknown(3) 后 SetValue 在 slot 6, Commit 在 slot 7。
# 若需支持其他 Windows 版本，应使用 comtypes 或 win32com 替代手动 vtable 调用。
_IUNKNOWN_QUERY_INTERFACE = 0
_IUNKNOWN_RELEASE = 2
_ISHELLLINKW_SET_PATH = 20
_ISHELLLINKW_SET_ARGUMENTS = 11
_IPERSISTFILE_SAVE = 6
_IPROPERTYSTORE_SET_VALUE = 6
_IPROPERTYSTORE_COMMIT = 7

# COM 线程模型常量
COINIT_APARTMENTTHREADED = 0x2
CLSCTX_INPROC_SERVER = 1
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106


# vtable 调用辅助
def _vtcall(obj_ptr, slot, *arg_types):
    """获取 COM 对象 vtable 中指定偏移的函数指针。"""
    vtable = ctypes.cast(obj_ptr, POINTER(POINTER(c_void_p))).contents
    return ctypes.cast(
        vtable[slot],
        ctypes.WINFUNCTYPE(HRESULT, c_void_p, *arg_types),
    )


def create_start_menu_shortcut(
    exe_path: str,
    arguments: str = "",
    shortcut_name: str = SHORTCUT_NAME,
) -> bool:
    """在 Start Menu 创建快捷方式并设置 AppUserModelID，可附带启动参数。"""
    shortcut_path = os.path.join(SHORTCUT_DIR, shortcut_name)
    os.makedirs(SHORTCUT_DIR, exist_ok=True)

    coinit_hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if coinit_hr < 0 and coinit_hr != RPC_E_CHANGED_MODE:  # FAILED(hr)；套间已被他处初始化时仍可继续
        logger.error("CoInitializeEx 失败 hr=0x{:08X}", coinit_hr & 0xFFFFFFFF)
        return False
    initialized_here = coinit_hr >= 0  # 仅本次调用真正初始化成功时才需要配对 CoUninitialize
    try:
        # CoCreateInstance -> IShellLinkW
        ptr = c_void_p()
        hr = ole32.CoCreateInstance(
            byref(CLSID_ShellLink), None, CLSCTX_INPROC_SERVER,
            byref(IID_IShellLinkW), byref(ptr)
        )
        if hr != 0 or not ptr.value:
            return False

        set_path_hr = _vtcall(ptr, _ISHELLLINKW_SET_PATH, ctypes.c_wchar_p)(ptr.value, exe_path)
        if set_path_hr != 0:
            _vtcall(ptr, _IUNKNOWN_RELEASE)(ptr.value)
            return False

        if arguments:
            args_hr = _vtcall(ptr, _ISHELLLINKW_SET_ARGUMENTS, ctypes.c_wchar_p)(ptr.value, arguments)
            if args_hr != 0:
                _vtcall(ptr, _IUNKNOWN_RELEASE)(ptr.value)
                return False

        _try_set_app_user_model_id(ptr)

        try:
            _save_shortcut(ptr, shortcut_path)
        finally:
            _vtcall(ptr, _IUNKNOWN_RELEASE)(ptr.value)
        logger.info("开始菜单快捷方式已创建 {}", shortcut_path)
        return True
    except Exception:
        logger.exception("创建快捷方式失败 path={}", exe_path)
        return False
    finally:
        if initialized_here:
            ole32.CoUninitialize()


def _try_set_app_user_model_id(shell_link_ptr):
    """设置 AppUserModelID（用于通知），失败时静默忽略。"""
    ps_ptr = c_void_p()
    try:
        _vtcall(shell_link_ptr, _IUNKNOWN_QUERY_INTERFACE, POINTER(GUID), POINTER(c_void_p))(
            shell_link_ptr.value, byref(IID_IPropertyStore), byref(ps_ptr)
        )
    except OSError:
        return
    if not ps_ptr.value:
        return
    try:
        pv, _pv_text = _make_lpWSTR(APP_ID)
        _vtcall(ps_ptr, _IPROPERTYSTORE_SET_VALUE, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))(
            ps_ptr.value, byref(PKEY_AppUserModelID), byref(pv)
        )
        _vtcall(ps_ptr, _IPROPERTYSTORE_COMMIT)(ps_ptr.value)
    except OSError:
        pass
    finally:
        _vtcall(ps_ptr, _IUNKNOWN_RELEASE)(ps_ptr.value)


def _save_shortcut(shell_link_ptr, shortcut_path: str):
    """通过 IPersistFile 保存快捷方式文件。失败时抛出 OSError。"""
    pf_ptr = c_void_p()
    hr = _vtcall(shell_link_ptr, _IUNKNOWN_QUERY_INTERFACE, POINTER(GUID), POINTER(c_void_p))(
        shell_link_ptr.value, byref(IID_IPersistFile), byref(pf_ptr)
    )
    if hr != 0 or not pf_ptr.value:
        raise OSError(f"IPersistFile QueryInterface failed: 0x{hr & 0xFFFFFFFF:08X}")
    try:
        save_hr = _vtcall(pf_ptr, _IPERSISTFILE_SAVE, ctypes.c_wchar_p, ctypes.c_int)(
            pf_ptr.value, shortcut_path, 1
        )
        if save_hr != 0:
            raise OSError(f"IPersistFile::Save failed: 0x{save_hr & 0xFFFFFFFF:08X}")
    finally:
        _vtcall(pf_ptr, _IUNKNOWN_RELEASE)(pf_ptr.value)


def remove_start_menu_shortcut() -> bool:
    """删除 Start Menu 快捷方式（含「卸载 copy-tree」）。"""
    ok = True
    for name in (SHORTCUT_NAME, SHORTCUT_UNINSTALL_NAME):
        shortcut_path = os.path.join(SHORTCUT_DIR, name)
        try:
            if os.path.exists(shortcut_path):
                os.remove(shortcut_path)
        except OSError:
            logger.exception("删除快捷方式失败 {}", shortcut_path)
            ok = False
    return ok


def _make_lpWSTR(value: str) -> tuple[PROPVARIANT, ctypes.Array]:
    """构造一个 VT_LPWSTR (31) PROPVARIANT。简化版：只设置 vt 和指针。"""
    wstr = ctypes.create_unicode_buffer(value)
    pv = PROPVARIANT()
    pv.vt = 31  # VT_LPWSTR
    pv.p = ctypes.cast(wstr, ctypes.c_void_p)
    return pv, wstr
