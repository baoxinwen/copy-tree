"""拖拽窗口。双击已安装的 copy-tree.exe（版本一致）时打开。

- 拖入一个或多个文件夹（原生 WM_DROPFILES，ctypes 子类化 Tk 窗口过程），
  按界面所选格式与过滤选项扫描并写入剪贴板；
- 也可保存为 txt、打开配置文件、卸载 copy-tree；
- 可选"关闭时驻留托盘"（写入配置 enableTray，默认关闭）。

线程模型：扫描在后台线程执行，结果经线程安全队列回传，
由 Tk 主线程 after 轮询消费；托盘线程动作同样走队列。
"""

import ctypes
import ctypes.wintypes
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from loguru import logger

from .clipboard import copy_to_clipboard
from .config import get_effective_config, open_config_file, update_config_values
from .constants import (
    DEFAULT_OUTPUT_FILENAME_TXT,
    GENERATED_OUTPUT_FILENAMES,
    SOURCE_CODE_EXTENSIONS,
    SOURCE_CODE_FILENAMES,
    VERSION,
)
from .formatter import format_output
from .scanner import (
    build_tree_text,
    describe_truncation,
    normalize_path,
    scan_directory,
)
from .tray import start_tray, stop_tray

# ── 原生拖拽（WM_DROPFILES）──
user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32

# Python 3.13 的 ctypes.wintypes 不再导出 LRESULT，用等价的 c_ssize_t
_LRESULT = ctypes.c_ssize_t

user32.GetParent.restype = ctypes.wintypes.HWND
user32.GetParent.argtypes = [ctypes.wintypes.HWND]
user32.CallWindowProcW.restype = _LRESULT
user32.CallWindowProcW.argtypes = [
    ctypes.c_ssize_t, ctypes.wintypes.HWND, ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
]
shell32.DragAcceptFiles.restype = None
shell32.DragAcceptFiles.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.BOOL]
shell32.DragQueryFileW.restype = ctypes.wintypes.UINT
shell32.DragQueryFileW.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.UINT, ctypes.c_void_p, ctypes.wintypes.UINT]
shell32.DragFinish.restype = None
shell32.DragFinish.argtypes = [ctypes.wintypes.HANDLE]

WM_DROPFILES = 0x0233
GWLP_WNDPROC = -4

_FORMAT_LABELS = {
    "树状文本": "text",
    "Markdown 代码块": "markdown",
    "Markdown 列表": "markdown-list",
    "JSON": "json",
    "路径列表": "paths",
    "文件名列表": "names",
    "统计摘要": "summary",
}

_FORMAT_VALUE_TO_LABEL = {value: label for label, value in _FORMAT_LABELS.items()}

# 安装状态横幅的按钮规格：状态 → (按钮文本, 动作键)。
# 状态字符串与 __main__ 的 _INSTALL_STATE_* 常量保持契约一致——本模块禁止
# import __main__（它延迟导入 window，会循环导入），状态以普通字符串参数传入。
# downgrade 的按钮是"卸载"性质，走 uninstall 动作键（由 __main__ 装配注入）。
_BANNER_BUTTONS = {
    "not-installed": ("安装右键菜单", "not-installed"),
    "update": (f"更新到 v{VERSION}", "update"),
    "downgrade": ("卸载已装副本…", "uninstall"),
    "repair": ("重新安装", "repair"),
    "migrate": ("迁移到标准位置", "migrate"),
}


def _initial_ui_values(config: dict) -> dict:
    """UI 控件初始值取自配置，保证窗口入口与右键入口默认行为一致。"""
    return {
        "format_label": _FORMAT_VALUE_TO_LABEL.get(config.get("defaultFormat", ""), "树状文本"),
        "show_size": bool(config.get("showFileSize", False)),
        "show_time": bool(config.get("showFileTime", False)),
        "gitignore": bool(config.get("respectGitignore", False)),
    }


class DropWindow:
    def __init__(self, install_state="ok", install_info=None, install_actions=None):
        # 安装状态上下文（与 __main__ 的 _INSTALL_STATE_* 字符串契约一致）：
        # install_actions 值为无参 callable，返回 bool 表示成功，成功后横幅自毁；
        # _install_actions 是同一 dict 的运行时别名，_on_install_action 按此读取
        self.install_state = install_state
        self.install_info = install_info
        self.install_actions = install_actions if install_actions is not None else {}
        self._install_actions = self.install_actions
        self._banner_frame = None

        self.root = tk.Tk()
        self.root.title(f"copy-tree v{VERSION} — 拖入文件夹即可复制目录树")
        self.root.minsize(520, 460)

        self.actions: queue.Queue = queue.Queue()
        self._old_wndproc: ctypes.c_ssize_t | None = None
        self._new_wndproc_ref = None  # 持引用防止 GC 回收后回调地址失效
        self._worker: threading.Thread | None = None
        self._tray_started = False

        self._build_ui()
        self._install_drag_drop()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll)

    # ── UI ──

    def _build_ui(self):
        config = get_effective_config()
        ui = _initial_ui_values(config)

        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="both", expand=True)

        self._build_status_banner(top)

        list_frame = ttk.Frame(top)
        list_frame.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(list_frame)
        scrollbar.pack(side="right", fill="y")
        self.folder_list = tk.Listbox(list_frame, height=6, selectmode="extended", yscrollcommand=scrollbar.set)
        self.folder_list.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.folder_list.yview)

        list_buttons = ttk.Frame(top)
        list_buttons.pack(fill="x", pady=(4, 8))
        ttk.Button(list_buttons, text="添加文件夹…", command=self._add_folder_dialog).pack(side="left")
        ttk.Button(list_buttons, text="移除选中", command=self._remove_selected).pack(side="left", padx=4)
        ttk.Button(list_buttons, text="清空", command=lambda: self.folder_list.delete(0, "end")).pack(side="left")

        options = ttk.LabelFrame(top, text="选项", padding=8)
        options.pack(fill="x")
        first_row = ttk.Frame(options)
        first_row.pack(fill="x")
        ttk.Label(first_row, text="格式：").pack(side="left")
        self.format_var = tk.StringVar(value=ui["format_label"])
        self.format_box = ttk.Combobox(
            first_row, textvariable=self.format_var, state="readonly",
            values=list(_FORMAT_LABELS), width=16,
        )
        self.format_box.pack(side="left", padx=(0, 12))
        self.size_var = tk.BooleanVar(value=ui["show_size"])
        self.time_var = tk.BooleanVar(value=ui["show_time"])
        ttk.Checkbutton(first_row, text="含大小", variable=self.size_var).pack(side="left")
        ttk.Checkbutton(first_row, text="含修改时间", variable=self.time_var).pack(side="left", padx=(8, 0))

        second_row = ttk.Frame(options)
        second_row.pack(fill="x", pady=(6, 0))
        self.hide_git_var = tk.BooleanVar(value=True)
        self.gitignore_var = tk.BooleanVar(value=ui["gitignore"])
        self.source_only_var = tk.BooleanVar(value=False)
        self.tray_var = tk.BooleanVar(value=bool(config.get("enableTray", False)))
        ttk.Checkbutton(second_row, text="隐藏 .git 等目录", variable=self.hide_git_var).pack(side="left")
        ttk.Checkbutton(second_row, text="遵循 .gitignore", variable=self.gitignore_var).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(second_row, text="仅源码文件", variable=self.source_only_var).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(second_row, text="关闭时驻留托盘", variable=self.tray_var, command=self._on_tray_toggle).pack(side="left", padx=(8, 0))

        buttons = ttk.Frame(top)
        buttons.pack(fill="x", pady=8)
        self.copy_button = ttk.Button(buttons, text="复制到剪贴板", command=self._on_copy)
        self.copy_button.pack(side="left")
        ttk.Button(buttons, text="保存为 txt", command=self._on_save_txt).pack(side="left", padx=4)
        ttk.Button(buttons, text="打开配置文件", command=lambda: open_config_file()).pack(side="left", padx=4)
        if getattr(sys, "frozen", False):
            ttk.Button(buttons, text="卸载 copy-tree…", command=self._on_uninstall).pack(side="right")

        log_frame = ttk.LabelFrame(top, text="结果", padding=4)
        log_frame.pack(fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_frame)
        log_scroll.pack(side="right", fill="y")
        self.log_text = tk.Text(log_frame, height=8, state="disabled", wrap="none", yscrollcommand=log_scroll.set)
        self.log_text.pack(fill="both", expand=True)
        log_scroll.config(command=self.log_text.yview)

        self.status_var = tk.StringVar(value="就绪。拖入文件夹或点击「添加文件夹」开始。")
        ttk.Label(self.root, textvariable=self.status_var, relief="sunken", anchor="w", padding=(6, 2)).pack(fill="x", side="bottom")

    def _build_status_banner(self, parent):
        """按安装状态在列表区上方渲染提示横幅；ok 态不创建任何控件。

        install_actions 未注入对应动作键时不渲染按钮（只提示）。
        """
        if self.install_state == "ok":
            return
        info = self.install_info or {}
        if self.install_state == "not-installed":
            text = "尚未安装右键菜单，安装后可右键文件夹一键复制目录树。"
        elif self.install_state == "update":
            text = f"检测到新版本 v{VERSION}，已安装副本为旧版 v{info.get('installed_version', '?')}。"
        elif self.install_state == "downgrade":
            text = f"本机已安装更新版本 v{info.get('installed_version', '?')}（当前运行 v{VERSION}），右键菜单不受影响。"
        elif self.install_state == "repair":
            text = "安装副本丢失或损坏，重新安装后右键菜单恢复可用。"
        elif self.install_state == "migrate":
            text = "检测到旧安装路径，迁移到标准位置后右键菜单不再依赖下载目录。"
        else:  # 未知状态不渲染，避免空白横幅
            return
        self._banner_frame = ttk.Frame(parent)
        self._banner_frame.pack(fill="x")
        ttk.Label(self._banner_frame, text=text).pack(side="left")
        button_spec = _BANNER_BUTTONS.get(self.install_state)
        if button_spec is not None:
            button_text, action_key = button_spec
            if action_key in self._install_actions:
                ttk.Button(
                    self._banner_frame, text=button_text,
                    command=lambda key=action_key: self._on_install_action(key),
                ).pack(side="left", padx=(8, 0))

    def _on_install_action(self, key: str):
        """执行注入的安装动作；成功后横幅自毁，失败保留横幅供重试。

        卸载类回调可能不返回（进程即将退出），无需特殊处理。
        """
        action = self._install_actions.get(key)
        if action is None:
            return
        if action() and self._banner_frame is not None:
            self._banner_frame.destroy()
            self._banner_frame = None

    # ── 原生拖拽 ──

    def _install_drag_drop(self):
        """子类化 Tk 顶层窗口过程以接收 WM_DROPFILES（tkinter 原生不支持）。"""
        self.root.update_idletasks()
        hwnd = user32.GetParent(self.root.winfo_id())
        if not hwnd:
            logger.warning("未能取得顶层窗口句柄，拖拽不可用（仍可用按钮添加）")
            return

        def _proc(h_wnd, msg, wparam, lparam):
            if msg == WM_DROPFILES:
                self._handle_drop(wparam)
                return 0
            return user32.CallWindowProcW(self._old_wndproc, h_wnd, msg, wparam, lparam)

        self._new_wndproc_ref = _WNDPROC_TYPE(_proc)
        if hasattr(user32, "SetWindowLongPtrW"):
            user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
            user32.SetWindowLongPtrW.argtypes = [ctypes.wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            self._old_wndproc = user32.SetWindowLongPtrW(
                hwnd, GWLP_WNDPROC, ctypes.cast(self._new_wndproc_ref, ctypes.c_void_p).value
            )
        else:  # 32 位 Python 回退
            user32.SetWindowLongW.restype = ctypes.c_ssize_t
            user32.SetWindowLongW.argtypes = [ctypes.wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            self._old_wndproc = user32.SetWindowLongW(
                hwnd, GWLP_WNDPROC, ctypes.cast(self._new_wndproc_ref, ctypes.c_void_p).value
            )
        shell32.DragAcceptFiles(hwnd, True)

    def _handle_drop(self, hdrop_param):
        hdrop = ctypes.wintypes.HANDLE(hdrop_param)
        dropped = []
        count = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
        for i in range(count):
            length = shell32.DragQueryFileW(hdrop, i, None, 0)
            buf = ctypes.create_unicode_buffer(length + 1)
            shell32.DragQueryFileW(hdrop, i, buf, length + 1)
            dropped.append(buf.value)
        shell32.DragFinish(hdrop)
        for path in dropped:
            if os.path.isdir(path):
                if path not in self.folder_list.get(0, "end"):
                    self.folder_list.insert("end", path)
            else:
                self._append_log(f"[跳过] 不是文件夹：{path}")

    # ── 动作 ──

    def _add_folder_dialog(self):
        path = filedialog.askdirectory(title="选择要扫描的文件夹")
        if path:
            path = os.path.normpath(path)
            if path not in self.folder_list.get(0, "end"):
                self.folder_list.insert("end", path)

    def _remove_selected(self):
        for index in reversed(self.folder_list.curselection()):
            self.folder_list.delete(index)

    def _on_tray_toggle(self):
        update_config_values({"enableTray": bool(self.tray_var.get())})
        if self.tray_var.get():
            self._ensure_tray()

    def _on_copy(self):
        self._run_action(copy=True, save=False)

    def _on_save_txt(self):
        self._run_action(copy=False, save=True)

    def _on_uninstall(self):
        if not messagebox.askyesno(
            "卸载 copy-tree",
            "将移除右键菜单、开始菜单快捷方式和本机安装副本。\n确定卸载吗？",
        ):
            return
        stop_tray()
        subprocess.Popen([sys.executable, "--uninstall"])
        self.root.destroy()

    def _run_action(self, copy: bool, save: bool):
        folders = list(self.folder_list.get(0, "end"))
        if not folders:
            self.status_var.set("请先添加要扫描的文件夹。")
            return
        if self._worker is not None and self._worker.is_alive():
            self.status_var.set("正在处理上一批任务…")
            return

        opts = {
            "format": _FORMAT_LABELS.get(self.format_var.get(), "text"),
            "hide_git": self.hide_git_var.get(),
            "gitignore": self.gitignore_var.get(),
            "source_only": self.source_only_var.get(),
            "size": self.size_var.get(),
            "time": self.time_var.get(),
        }
        self.copy_button.config(state="disabled")
        self.status_var.set("扫描中…")
        self._worker = threading.Thread(
            target=self._worker_main, args=(folders, opts, copy, save), daemon=True
        )
        self._worker.start()

    def _worker_main(self, folders, opts, do_copy, do_save):
        fmt = opts["format"]
        outputs = []
        for folder in folders:
            try:
                config = get_effective_config()
                exclude_dirs: set = set()
                exclude_files: set = set()
                exclude_patterns: set = set()
                prune = False
                if opts["hide_git"]:
                    exclude_dirs = set(config.get("excludeDirs") or [])
                    exclude_files = set(config.get("excludeFiles") or [])
                    # excludePatterns 与 excludeDirs 同生命周期：仅过滤模式生效（与右键 CLI 一致）
                    exclude_patterns = set(config.get("excludePatterns") or [])
                    prune = True
                include_ext = include_names = None
                if opts["source_only"]:
                    include_ext = SOURCE_CODE_EXTENSIONS
                    include_names = SOURCE_CODE_FILENAMES
                    prune = True
                exclude_files |= GENERATED_OUTPUT_FILENAMES

                max_depth = config.get("maxDepth", -1)
                result = scan_directory(
                    path=folder,
                    exclude_dirs=exclude_dirs or None,
                    exclude_files=exclude_files or None,
                    exclude_patterns=exclude_patterns or None,
                    max_files=config.get("maxFiles", 2000),
                    max_items_per_level=config.get("maxItemsPerLevel", 200),
                    show_size=opts["size"],
                    show_time=opts["time"],
                    max_depth=None if max_depth == -1 else max_depth,
                    include_ext=include_ext,
                    include_names=include_names,
                    prune_empty_dirs=prune or opts["gitignore"],
                    respect_gitignore=opts["gitignore"],
                )
                tree_text = build_tree_text(result, show_size=opts["size"], show_time=opts["time"])
                output = format_output(
                    tree_text, fmt, result=result, show_size=opts["size"], show_time=opts["time"]
                )
                outputs.append(output)

                note = f"{result.total_files} 个文件，{result.total_dirs} 个文件夹"
                if result.truncated:
                    note += f"（已截断：{describe_truncation(result)}）"
                self.actions.put(("log", f"[完成] {folder} — {note}"))

                if do_save:
                    save_path = os.path.join(folder, DEFAULT_OUTPUT_FILENAME_TXT)
                    try:
                        with open(save_path, "w", encoding="utf-8") as f:
                            f.write(tree_text)
                        self.actions.put(("log", f"[保存] {save_path}"))
                    except OSError as e:
                        self.actions.put(("log", f"[失败] 保存 {save_path}：{e}"))
            except Exception as e:  # 单个目录失败不影响其余目录
                logger.exception("窗口扫描失败 {}", folder)
                self.actions.put(("log", f"[失败] {folder} — {e}"))

        if do_copy and outputs:
            combined = "\n\n".join(outputs)
            if copy_to_clipboard(combined):
                self.actions.put(("done", f"已复制 {len(outputs)} 个目录树（{fmt} 格式，{len(combined)} 字符）"))
            else:
                self.actions.put(("log", "[失败] 剪贴板写入失败，请重试"))
                self.actions.put(("done", "复制失败"))
        else:
            self.actions.put(("done", "处理完成。" if not do_copy else "没有可复制的内容。"))

    # ── 托盘与消息泵 ──

    def _ensure_tray(self) -> bool:
        if self._tray_started:
            return True
        ok = start_tray(
            on_open=lambda: self.actions.put(("tray", "open")),
            on_config=lambda: self.actions.put(("tray", "config")),
            on_exit=lambda: self.actions.put(("tray", "exit")),
        )
        # 成功后同进程内不重复启动；失败保留重试机会（下次关闭再试）
        self._tray_started = ok
        return ok

    def _on_close(self):
        if self.tray_var.get():
            if self._ensure_tray():
                self.root.withdraw()
                self.status_var.set("已驻留托盘：双击托盘图标重新打开。")
            else:
                # 托盘不可用时隐藏窗口会让进程对用户完全不可见：保持窗口打开
                self.status_var.set(
                    "托盘不可用，窗口保持打开；可取消勾选「关闭时驻留托盘」后再关闭退出。"
                )
        else:
            self._quit_app()

    def _quit_app(self):
        stop_tray()
        self.root.destroy()

    def _poll(self):
        try:
            while True:
                kind, payload = self.actions.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "done":
                    self.copy_button.config(state="normal")
                    self.status_var.set(payload)
                    self._append_log("")
                elif kind == "tray":
                    if payload == "open":
                        self.root.deiconify()
                        self.root.lift()
                    elif payload == "config":
                        open_config_file()
                    elif payload == "exit":
                        self._quit_app()
                        return
        except queue.Empty:
            pass
        if self._worker is not None and not self._worker.is_alive() and self.copy_button["state"] == "disabled":
            # 兜底：任务意外结束时恢复按钮
            self.copy_button.config(state="normal")
        self.root.after(100, self._poll)

    def _append_log(self, line: str):
        self.log_text.config(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def run(self):
        self.root.mainloop()


_WNDPROC_TYPE = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,
    ctypes.wintypes.HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
)


def run_drop_window(install_state="ok", install_info=None, install_actions=None) -> None:
    """打开拖拽窗口并阻塞至窗口关闭。仅在 GUI 主程序中使用。"""
    app = DropWindow(
        install_state=install_state,
        install_info=install_info,
        install_actions=install_actions,
    )
    try:
        app.run()
    finally:
        stop_tray()
