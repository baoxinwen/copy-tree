"""Win32 剪贴板写入，ctypes 实现。"""

import ctypes
import ctypes.wintypes
import time

from loguru import logger

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# 剪贴板重试配置：指数退避，总等待约 0.6s，
# 容忍剪贴板管理器（剪贴板历史、Ditto 等）短时持锁
_CLIPBOARD_MAX_RETRIES = 3
_CLIPBOARD_RETRY_DELAYS = (0.15, 0.45)

# 最近一次写入失败的阶段："memory"（分配/锁定共享内存）或 "busy"（打开/写入剪贴板）
_failure_stage = "busy"

# 设置正确的函数签名（64 位下必须显式设置返回类型）
kernel32.GlobalAlloc.restype = ctypes.wintypes.HGLOBAL
kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = ctypes.wintypes.BOOL
kernel32.GlobalUnlock.argtypes = [ctypes.wintypes.HGLOBAL]
kernel32.GlobalFree.restype = ctypes.wintypes.HGLOBAL
kernel32.GlobalFree.argtypes = [ctypes.wintypes.HGLOBAL]

user32.OpenClipboard.restype = ctypes.wintypes.BOOL
user32.OpenClipboard.argtypes = [ctypes.wintypes.HWND]
user32.CloseClipboard.restype = ctypes.wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.EmptyClipboard.restype = ctypes.wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.SetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]


def copy_to_clipboard(text: str, max_retries: int = _CLIPBOARD_MAX_RETRIES) -> bool:
    """将 Unicode 文本写入系统剪贴板。失败时按指数退避重试。"""
    global _failure_stage
    for attempt in range(1, max_retries + 1):
        if _write_clipboard(text):
            logger.debug("剪贴板写入成功 attempt={} chars={}", attempt, len(text))
            return True
        logger.debug(
            "剪贴板写入失败 attempt={}/{} stage={} winError={}",
            attempt, max_retries, _failure_stage, ctypes.get_last_error(),
        )
        if attempt < max_retries:
            time.sleep(_CLIPBOARD_RETRY_DELAYS[min(attempt - 1, len(_CLIPBOARD_RETRY_DELAYS) - 1)])
    logger.error("剪贴板写入最终失败，共尝试 {} 次，stage={}", max_retries, _failure_stage)
    return False


def get_last_failure_stage() -> str:
    """返回最近一次剪贴板写入失败的阶段："memory" 或 "busy"。"""
    return _failure_stage


def _write_clipboard(text: str) -> bool:
    global _failure_stage
    data = text.encode("utf-16-le") + b"\x00\x00"
    h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if h_mem is None:
        _failure_stage = "memory"
        return False

    ptr = kernel32.GlobalLock(h_mem)
    if ptr is None:
        _failure_stage = "memory"
        kernel32.GlobalFree(h_mem)
        return False

    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(h_mem)

    if not user32.OpenClipboard(0):
        _failure_stage = "busy"
        kernel32.GlobalFree(h_mem)
        return False

    # EmptyClipboard must precede SetClipboardData (Windows convention).
    # If SetClipboardData fails, the prior clipboard contents are already
    # cleared and cannot be restored; this is expected behavior.
    user32.EmptyClipboard()
    result = user32.SetClipboardData(CF_UNICODETEXT, h_mem)
    user32.CloseClipboard()

    if result is None:
        _failure_stage = "busy"
        kernel32.GlobalFree(h_mem)
        return False

    return True
