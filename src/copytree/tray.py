"""可选的系统托盘图标。纯 ctypes 实现，运行在独立后台线程。

默认不启用（尊重"不常驻后台"原则）；拖拽窗口勾选"关闭时驻留托盘"后，
窗口关闭并不退出进程，托盘图标保留入口：左键双击重新打开窗口，
右键弹出菜单（打开 / 打开配置 / 退出）。
线程模型：托盘线程只做 Win32 消息泵，用户动作通过回调投递到线程安全队列，
由窗口主线程的轮询消费，绝不跨线程触碰 tkinter。
"""

import ctypes
import ctypes.wintypes
import threading

from loguru import logger

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32

# Python 3.13 的 ctypes.wintypes 不再导出 LRESULT，用等价的 c_ssize_t
_LRESULT = ctypes.c_ssize_t

user32.CreateWindowExW.restype = ctypes.wintypes.HWND
user32.CreateWindowExW.argtypes = [
    ctypes.wintypes.DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.wintypes.HWND, ctypes.wintypes.HMENU, ctypes.wintypes.HINSTANCE, ctypes.c_void_p,
]
user32.DefWindowProcW.restype = _LRESULT
user32.DefWindowProcW.argtypes = [
    ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
]
user32.RegisterClassW.restype = ctypes.wintypes.ATOM
user32.RegisterClassW.argtypes = [ctypes.c_void_p]
user32.DestroyWindow.restype = ctypes.wintypes.BOOL
user32.DestroyWindow.argtypes = [ctypes.wintypes.HWND]
user32.PostMessageW.restype = ctypes.wintypes.BOOL
user32.PostMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
user32.SetForegroundWindow.restype = ctypes.wintypes.BOOL
user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
user32.GetCursorPos.restype = ctypes.wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.c_void_p]
user32.LoadIconW.restype = ctypes.wintypes.HICON
user32.LoadIconW.argtypes = [ctypes.wintypes.HINSTANCE, ctypes.c_void_p]
user32.CreatePopupMenu.restype = ctypes.wintypes.HMENU
user32.CreatePopupMenu.argtypes = []
user32.AppendMenuW.restype = ctypes.wintypes.BOOL
user32.AppendMenuW.argtypes = [ctypes.wintypes.HMENU, ctypes.wintypes.UINT, ctypes.c_size_t, ctypes.c_wchar_p]
user32.TrackPopupMenu.restype = ctypes.wintypes.BOOL
user32.TrackPopupMenu.argtypes = [ctypes.wintypes.HMENU, ctypes.wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.wintypes.HWND, ctypes.c_void_p]
user32.DestroyMenu.restype = ctypes.wintypes.BOOL
user32.DestroyMenu.argtypes = [ctypes.wintypes.HMENU]
user32.RegisterWindowMessageW.restype = ctypes.wintypes.UINT
user32.RegisterWindowMessageW.argtypes = [ctypes.c_wchar_p]
user32.DestroyIcon.restype = ctypes.wintypes.BOOL
user32.DestroyIcon.argtypes = [ctypes.wintypes.HICON]
kernel32.GetModuleHandleW.restype = ctypes.wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
user32.GetMessageW.restype = ctypes.wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.c_void_p, ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.UINT]
user32.TranslateMessage.restype = ctypes.wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.c_void_p]
user32.DispatchMessageW.restype = _LRESULT
user32.DispatchMessageW.argtypes = [ctypes.c_void_p]

shell32.Shell_NotifyIconW.restype = ctypes.wintypes.BOOL
shell32.Shell_NotifyIconW.argtypes = [ctypes.wintypes.DWORD, ctypes.c_void_p]
shell32.ExtractIconExW.restype = ctypes.wintypes.UINT
shell32.ExtractIconExW.argtypes = [ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.UINT]

NIM_ADD = 0
NIM_DELETE = 2
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
WM_APP_TRAY_CALLBACK = 0x8001  # WM_APP + 1
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WM_NULL = 0x0000
TPM_RIGHTBUTTON = 0x0002
IDI_APPLICATION = 32512

_MENU_OPEN = 1
_MENU_CONFIG = 2
_MENU_EXIT = 3


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("hWnd", ctypes.wintypes.HWND),
        ("uID", ctypes.wintypes.UINT),
        ("uFlags", ctypes.wintypes.UINT),
        ("uCallbackMessage", ctypes.wintypes.UINT),
        ("hIcon", ctypes.wintypes.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.wintypes.DWORD),
        ("dwStateMask", ctypes.wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", ctypes.wintypes.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.wintypes.DWORD),
    ]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.wintypes.HINSTANCE),
        ("hIcon", ctypes.wintypes.HICON),
        ("hCursor", ctypes.wintypes.HANDLE),
        ("hbrBackground", ctypes.wintypes.HBRUSH),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.wintypes.LONG), ("y", ctypes.wintypes.LONG)]


_WNDPROC = ctypes.WINFUNCTYPE(
    _LRESULT,
    ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
)

_tray_lock = threading.Lock()
_tray_hwnd: int | None = None
_tray_thread = None


def start_tray(on_open, on_config, on_exit) -> bool:
    """启动托盘线程。on_open/on_config/on_exit 在托盘线程被调用，须自行保证线程安全。

    返回 True 表示图标已实际注册成功；类注册/建窗/NIM_ADD 失败或超时返回
    False，调用方据此决定是否保留可见窗口，避免出现无托盘图标的驻留进程。
    """
    global _tray_thread
    with _tray_lock:
        if _tray_thread is not None and _tray_thread.is_alive():
            return True
        started = threading.Event()
        result = {"ok": False}
        thread = threading.Thread(
            target=_tray_main,
            args=(on_open, on_config, on_exit, started, result),
            daemon=True,
        )
        _tray_thread = thread
    thread.start()
    # 不能持锁等待：托盘线程初始化中途要短暂获取 _tray_lock 登记窗口句柄
    ok = started.wait(timeout=5.0) and result["ok"]
    if not ok:
        logger.warning("托盘图标初始化失败或超时")
    return ok


def stop_tray() -> None:
    """请求托盘线程移除图标并退出（幂等）。"""
    with _tray_lock:
        hwnd = _tray_hwnd
    if hwnd:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def _tray_main(on_open, on_config, on_exit, started: threading.Event, result: dict):
    global _tray_hwnd
    hwnd = None
    try:
        # Explorer 重启会广播 TaskbarCreated 并回收全部托盘图标，需重新挂载
        state = {
            "taskbar_msg": user32.RegisterWindowMessageW("TaskbarCreated"),
            "nid": None,
            "hicon": None,
        }
        wndproc_ref = _WNDPROC(_make_wndproc(on_open, on_config, on_exit, state))
        class_name = "CopyTreeTrayWnd"
        wc = _WNDCLASSW()
        wc.lpfnWndProc = ctypes.cast(wndproc_ref, ctypes.c_void_p).value
        wc.lpszClassName = class_name
        wc.hInstance = kernel32.GetModuleHandleW(None)
        if not user32.RegisterClassW(ctypes.byref(wc)):
            started.set()
            return

        hwnd = user32.CreateWindowExW(
            0, class_name, "", 0,
            0, 0, 0, 0, None, None, wc.hInstance, None,
        )
        if not hwnd:
            started.set()
            return
        with _tray_lock:
            _tray_hwnd = hwnd

        hicon = _load_icon()
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_APP_TRAY_CALLBACK
        nid.hIcon = hicon
        nid.szTip = "copy-tree"
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            logger.warning("托盘图标注册失败")
            started.set()
            return

        # 图标挂载成功后才允许 TaskbarCreated 重挂与 WM_CLOSE 销毁
        state["nid"] = nid
        state["hicon"] = hicon
        result["ok"] = True
        started.set()
        msg = ctypes.create_string_buffer(48)  # sizeof(MSG) on x64
        # GetMessageW 返回 0 表示 WM_QUIT，-1 表示错误
        while user32.GetMessageW(msg, None, 0, 0) > 0:
            user32.TranslateMessage(msg)
            user32.DispatchMessageW(msg)
    except Exception:
        logger.exception("托盘线程异常退出")
        started.set()
    finally:
        with _tray_lock:
            _tray_hwnd = None


def _make_wndproc(on_open, on_config, on_exit, state):
    def _proc(hwnd, msg, wparam, lparam):
        if msg == WM_APP_TRAY_CALLBACK:
            if lparam == WM_LBUTTONDBLCLK:
                on_open()
            elif lparam == WM_RBUTTONUP:
                _show_menu(hwnd, on_open, on_config, on_exit)
            return 0
        if msg == WM_COMMAND:
            cmd = wparam & 0xFFFF
            if cmd == _MENU_OPEN:
                on_open()
            elif cmd == _MENU_CONFIG:
                on_config()
            elif cmd == _MENU_EXIT:
                on_exit()
            return 0
        if state["taskbar_msg"] and msg == state["taskbar_msg"]:
            # Explorer 重启后系统回收图标并广播 TaskbarCreated：重新挂载
            if state["nid"] is not None:
                shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(state["nid"]))
            return 0
        if msg == WM_CLOSE:
            nid = state["nid"]
            if nid is None:
                nid = _NOTIFYICONDATAW()
                nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
                nid.hWnd = hwnd
                nid.uID = 1
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            hicon = state["hicon"]
            if hicon is not None:
                user32.DestroyIcon(hicon)
                state["hicon"] = None
            user32.DestroyWindow(hwnd)
            return 0
        if msg == 0x0002:  # WM_DESTROY
            from ctypes import windll
            windll.user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    return _proc


def _show_menu(hwnd, on_open, on_config, on_exit):
    menu = user32.CreatePopupMenu()
    if not menu:
        return
    MF_STRING = 0x0
    user32.AppendMenuW(menu, MF_STRING, _MENU_OPEN, "打开 copy-tree")
    user32.AppendMenuW(menu, MF_STRING, _MENU_CONFIG, "打开配置文件")
    user32.AppendMenuW(menu, MF_STRING, _MENU_EXIT, "退出")
    pt = _POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    # TrackPopupMenu 前必须 SetForegroundWindow，否则点击菜单外无法关闭
    user32.SetForegroundWindow(hwnd)
    user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON, pt.x, pt.y, 0, hwnd, None)
    user32.PostMessageW(hwnd, WM_NULL, 0, 0)
    user32.DestroyMenu(menu)


def _load_icon():
    """优先取主程序内嵌图标（frozen exe），失败回退系统默认图标。"""
    import sys

    try:
        exe_path = sys.executable if getattr(sys, "frozen", False) else ""
        if exe_path and exe_path.lower().endswith(".exe"):
            hicon_large = ctypes.wintypes.HICON()
            if shell32.ExtractIconExW(exe_path, 0, ctypes.byref(hicon_large), None, 1):
                return hicon_large
    except Exception:
        pass
    return user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
