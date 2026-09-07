"""Win32 API ctypes 声明集中管理。"""

import ctypes
import ctypes.wintypes

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32

# ── 进程快照 ──
TH32CS_SNAPPROCESS = 0x00000002


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.wintypes.DWORD),
        ("cntUsage", ctypes.wintypes.DWORD),
        ("th32ProcessID", ctypes.wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.wintypes.DWORD),
        ("cntThreads", ctypes.wintypes.DWORD),
        ("th32ParentProcessID", ctypes.wintypes.DWORD),
        ("pcPriClassBase", ctypes.wintypes.LONG),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("szExeFile", ctypes.wintypes.WCHAR * 260),
    ]


# ── 常量 ──
ATTACH_PARENT_PROCESS = 0xFFFFFFFF
STD_OUTPUT_HANDLE = 0xFFFFFFF5
STD_ERROR_HANDLE = 0xFFFFFFF4
# 64 位: 0xFFFFFFFFFFFFFFFF, 32 位: 0xFFFFFFFF
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
FILE_TYPE_UNKNOWN = 0
FILE_TYPE_CHAR = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MOVEFILE_DELAY_UNTIL_REBOOT = 0x00000004
MB_YESNO = 0x00000004
MB_YESNOCANCEL = 0x00000003
MB_ICONQUESTION = 0x00000020
IDYES = 6
IDNO = 7

# ── kernel32 函数签名 ──
kernel32.GetConsoleWindow.restype = ctypes.wintypes.HWND
kernel32.GetConsoleWindow.argtypes = []
kernel32.GetConsoleProcessList.restype = ctypes.wintypes.DWORD
kernel32.GetConsoleProcessList.argtypes = [
    ctypes.POINTER(ctypes.wintypes.DWORD),
    ctypes.wintypes.DWORD,
]
kernel32.GetStdHandle.restype = ctypes.wintypes.HANDLE
kernel32.GetStdHandle.argtypes = [ctypes.wintypes.DWORD]
kernel32.GetFileType.restype = ctypes.wintypes.DWORD
kernel32.GetFileType.argtypes = [ctypes.wintypes.HANDLE]
kernel32.AttachConsole.restype = ctypes.wintypes.BOOL
kernel32.AttachConsole.argtypes = [ctypes.wintypes.DWORD]
kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
kernel32.OpenProcess.argtypes = [
    ctypes.wintypes.DWORD,
    ctypes.wintypes.BOOL,
    ctypes.wintypes.DWORD,
]
kernel32.QueryFullProcessImageNameW.restype = ctypes.wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    ctypes.wintypes.HANDLE,
    ctypes.wintypes.DWORD,
    ctypes.wintypes.LPWSTR,
    ctypes.POINTER(ctypes.wintypes.DWORD),
]
kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
kernel32.CreateToolhelp32Snapshot.restype = ctypes.wintypes.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [
    ctypes.wintypes.DWORD,
    ctypes.wintypes.DWORD,
]
kernel32.Process32FirstW.restype = ctypes.wintypes.BOOL
kernel32.Process32FirstW.argtypes = [
    ctypes.wintypes.HANDLE,
    ctypes.POINTER(PROCESSENTRY32W),
]
kernel32.Process32NextW.restype = ctypes.wintypes.BOOL
kernel32.Process32NextW.argtypes = [
    ctypes.wintypes.HANDLE,
    ctypes.POINTER(PROCESSENTRY32W),
]
kernel32.MoveFileExW.restype = ctypes.wintypes.BOOL
kernel32.MoveFileExW.argtypes = [
    ctypes.wintypes.LPCWSTR,
    ctypes.wintypes.LPCWSTR,
    ctypes.wintypes.DWORD,
]
kernel32.WriteConsoleW.restype = ctypes.wintypes.BOOL
kernel32.WriteConsoleW.argtypes = [
    ctypes.wintypes.HANDLE,
    ctypes.wintypes.LPCWSTR,
    ctypes.wintypes.DWORD,
    ctypes.POINTER(ctypes.wintypes.DWORD),
    ctypes.wintypes.LPVOID,
]
kernel32.WriteFile.restype = ctypes.wintypes.BOOL
kernel32.WriteFile.argtypes = [
    ctypes.wintypes.HANDLE,
    ctypes.c_void_p,
    ctypes.wintypes.DWORD,
    ctypes.POINTER(ctypes.wintypes.DWORD),
    ctypes.wintypes.LPVOID,
]

# ── user32 函数签名 ──
user32.MessageBoxW.restype = ctypes.c_int
user32.MessageBoxW.argtypes = [
    ctypes.wintypes.HWND,
    ctypes.wintypes.LPCWSTR,
    ctypes.wintypes.LPCWSTR,
    ctypes.wintypes.UINT,
]
