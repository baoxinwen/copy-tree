"""拖拽窗口。双击已安装的 copy-tree.exe（版本一致）时打开。

- 拖入一个或多个文件夹（原生 WM_DROPFILES，ctypes 子类化 Tk 窗口过程），
  按界面所选格式与过滤选项扫描并写入剪贴板；
- 「保存为 ▾」可把每个文件夹的结果保存为 txt / Markdown / JSON；
- 结果卡具备就绪态 ⇄ 操作结果态，任一文件夹截断时切换警告态；
- 设置菜单：关闭时驻留托盘、拖入后自动复制、打开配置、两步确认卸载；
- 视觉遵循 theme.py（墨林纸意 token）：本模块禁止出现裸色值。

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
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from loguru import logger

from . import theme
from .clipboard import copy_to_clipboard
from .config import get_effective_config, open_config_file, update_config_values
from .constants import (
    DEFAULT_OUTPUT_FILENAME_JSON,
    DEFAULT_OUTPUT_FILENAME_MD,
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

# 预览区只展示 tree_text 的前 N 行（与设计稿一致）
_PREVIEW_LINE_COUNT = 15

# 保存格式的文件名与内容形态：txt 沿用 tree_text（兼容不变），
# md/json 用对应格式的已格式化输出，保证文件内容与扩展名一致。
_SAVE_KINDS = ("txt", "md", "json")

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


def _fmt_count(n) -> str:
    """千分位计数：结果卡与文件数列共用。"""
    try:
        return f"{max(int(n), 0):,}"
    except (TypeError, ValueError):
        return "0"


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
        self.root.geometry("780x640")
        self.root.minsize(560, 520)

        self.actions: queue.Queue = queue.Queue()
        self._old_wndproc: ctypes.c_ssize_t | None = None
        self._new_wndproc_ref = None  # 持引用防止 GC 回收后回调地址失效
        self._worker: threading.Thread | None = None
        self._tray_started = False

        # 交互状态：保存格式 / 复制反馈 / 菜单两步确认 / 日志抽屉
        self._save_kind = "txt"
        self._copy_flash_active = False
        self._uninstall_armed = False
        self._uninstall_after_id = None
        self._clear_armed = False
        self._clear_after_id = None
        self._log_expanded = False

        # 扫描状态层：显式「扫描」→ 每文件夹结果缓存 → 复制/保存/预览只消费缓存
        self._scan_cache: dict[str, dict] = {}
        self._scan_opts: dict | None = None
        self._pending_scan_opts: dict | None = None
        self._pending_auto_copy = False

        self._build_ui()
        self._install_drag_drop()
        self._tint_titlebar()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll)

    # ── UI ──

    def _build_ui(self):
        config = get_effective_config()
        ui = _initial_ui_values(config)
        self.tray_var = tk.BooleanVar(value=bool(config.get("enableTray", False)))
        # 占位新配置项 auto_copy_on_drop：config.py 不落地该键，缺省 False，
        # 仅经设置菜单开关控制拖入后是否自动复制。
        self.auto_copy_var = tk.BooleanVar(value=bool(config.get("auto_copy_on_drop", False)))

        style = ttk.Style()
        theme.apply_theme(style)
        self._configure_styles(style)

        top = ttk.Frame(self.root, padding=theme.PAD_WINDOW)
        top.pack(fill="both", expand=True)

        # 安装状态横幅（机制原样保留），置于工具行上方
        self._build_status_banner(top)
        self._build_tool_row(top, ui)
        self._build_scan_row(top, ui)
        self._build_folder_table(top)
        self._build_preview(top)
        self._build_action_card(top)
        self._build_drawer(top)

        self._refresh_ready_card()

    def _configure_styles(self, style):
        """墨林纸意派生样式：颜色/字体/间距全部取自 theme 常量，禁止裸色值。"""
        # 分组小标签（字距加宽的弱提示）
        style.configure("GroupLabel.TLabel", background=theme.PAPER, foreground=theme.FAINT,
                        font=(theme.FONT_SANS, theme.SIZE_SMALL, "bold"))
        style.configure("FaintSmall.TLabel", background=theme.PAPER, foreground=theme.FAINT,
                        font=(theme.FONT_SANS, theme.SIZE_SMALL))
        style.configure("DrawerLog.TLabel", background=theme.PAPER, foreground=theme.MUTED,
                        font=(theme.FONT_MONO, theme.SIZE_SMALL))
        # 扫描范围条（表面底）
        style.configure("ScanRow.TFrame", background=theme.SURFACE)
        style.configure("ScanRowGroup.TLabel", background=theme.SURFACE, foreground=theme.FAINT,
                        font=(theme.FONT_SANS, theme.SIZE_SMALL, "bold"))
        style.configure("ScanRow.TCheckbutton", background=theme.SURFACE, foreground=theme.MUTED,
                        focusthickness=0)
        style.map("ScanRow.TCheckbutton", background=[("active", theme.SURFACE)])
        # 文件夹表（Treeview）
        style.configure("FolderTree.Treeview", background=theme.WHITE, fieldbackground=theme.WHITE,
                        foreground=theme.INK, rowheight=26, borderwidth=0, focusthickness=0)
        style.map("FolderTree.Treeview",
                  background=[("selected", theme.PINE)],
                  foreground=[("selected", theme.WHITE)])
        style.configure("FolderTree.Treeview.Heading", background=theme.WHITE, foreground=theme.FAINT,
                        relief="flat", borderwidth=0, padding=(theme.PAD_ROW, 3),
                        font=(theme.FONT_SANS, theme.SIZE_SMALL, "bold"))
        style.map("FolderTree.Treeview.Heading", background=[("active", theme.WHITE)])
        # 主 / 成功 / 危险 / 链接按钮
        style.configure("Primary.TButton", background=theme.PINE, foreground=theme.WHITE,
                        bordercolor=theme.PINE, focusthickness=0, padding=(theme.PAD_GROUP, 5),
                        font=(theme.FONT_SANS, theme.SIZE_BODY, "bold"))
        style.map("Primary.TButton",
                  background=[("active", theme.PINE_HOVER), ("pressed", theme.PINE_HOVER)],
                  foreground=[("active", theme.WHITE), ("pressed", theme.WHITE)])
        style.configure("Success.TButton", background=theme.SUCCESS, foreground=theme.WHITE,
                        bordercolor=theme.SUCCESS, focusthickness=0, padding=(theme.PAD_GROUP, 5))
        style.configure("DangerGhost.TButton", background=theme.WHITE, foreground=theme.DANGER,
                        bordercolor=theme.DANGER_LINE, focusthickness=0)
        style.map("DangerGhost.TButton",
                  background=[("active", theme.WHITE)],
                  bordercolor=[("active", theme.DANGER)])
        style.configure("DangerConfirm.TButton", background=theme.DANGER, foreground=theme.WHITE,
                        bordercolor=theme.DANGER, focusthickness=0)
        style.configure("Link.TButton", relief="flat", borderwidth=0, background=theme.PAPER,
                        foreground=theme.MUTED, focusthickness=0, padding=0)
        style.map("Link.TButton",
                  background=[("active", theme.PAPER)],
                  foreground=[("active", theme.PINE)])
        # 结果卡两态（正常 / 截断警告）
        style.configure("ResultCard.TFrame", background=theme.SURFACE)
        style.configure("ResultCard.TLabel", background=theme.SURFACE, foreground=theme.INK)
        style.configure("ResultCardWarn.TFrame", background=theme.WARN_BG,
                        bordercolor=theme.AMBER, relief="solid", borderwidth=1)
        style.configure("ResultCardWarn.TLabel", background=theme.WARN_BG, foreground=theme.WARN_TEXT)
        style.configure("ResultCardLink.TLabel", background=theme.WARN_BG, foreground=theme.WARN_TEXT,
                        font=(theme.FONT_SANS, theme.SIZE_SMALL, "underline"))
        # 两个 Menubutton（设置 / 保存为）
        style.configure("Settings.TMenubutton", background=theme.PAPER, foreground=theme.INK,
                        bordercolor=theme.BORDER, focusthickness=0)
        style.map("Settings.TMenubutton",
                  background=[("active", theme.PAPER)],
                  bordercolor=[("active", theme.PINE)])
        style.configure("Ghost.TMenubutton", background=theme.WHITE, foreground=theme.INK,
                        bordercolor=theme.BORDER, focusthickness=0, padding=(theme.PAD_ROW, 5))
        style.map("Ghost.TMenubutton",
                  background=[("active", theme.SURFACE)],
                  bordercolor=[("active", theme.PINE)])

    def _build_tool_row(self, parent, ui):
        """行 1：输出格式 + 内容选项 + 右侧设置菜单。"""
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(0, theme.PAD_ROW))
        ttk.Label(row, text="输出格式", style="GroupLabel.TLabel").pack(side="left")
        self.format_var = tk.StringVar(value=ui["format_label"])
        self.format_box = ttk.Combobox(
            row, textvariable=self.format_var, state="readonly",
            values=list(_FORMAT_LABELS), width=16,
        )
        self.format_box.pack(side="left", padx=(theme.PAD_ROW, theme.PAD_GROUP))
        self.size_var = tk.BooleanVar(value=ui["show_size"])
        self.time_var = tk.BooleanVar(value=ui["show_time"])
        ttk.Checkbutton(row, text="含大小", variable=self.size_var).pack(side="left")
        ttk.Checkbutton(row, text="含修改时间", variable=self.time_var).pack(
            side="left", padx=(theme.PAD_ROW, 0))
        self._build_settings_menu(row)

    def _build_settings_menu(self, parent):
        """「⚙ 设置 ▾」：两个开关 + 打开配置 + 卸载（菜单内两步确认）。

        卸载入口仅在打包运行（sys.frozen）时出现，与旧版底部按钮的显隐条件一致。
        """
        self.settings_button = ttk.Menubutton(
            parent, text="⚙ 设置 ▾", style="Settings.TMenubutton", direction="below")
        self._settings_menu = tk.Menu(self.settings_button, tearoff=0)
        self._settings_menu.add_checkbutton(
            label="关闭时驻留托盘", variable=self.tray_var, command=self._on_tray_toggle)
        self._settings_menu.add_checkbutton(label="拖入后自动复制", variable=self.auto_copy_var)
        self._settings_menu.add_separator()
        self._settings_menu.add_command(label="打开配置文件", command=lambda: open_config_file())
        if getattr(sys, "frozen", False):
            self._settings_menu.add_command(
                label="卸载 copy-tree…", command=self._on_uninstall_menu_selected)
            self._uninstall_menu_index = self._settings_menu.index("end")
        else:
            self._uninstall_menu_index = None
        self.settings_button.configure(menu=self._settings_menu)
        self.settings_button.pack(side="right")

    def _build_scan_row(self, parent, ui):
        """行 2：扫描范围过滤选项 + 右侧列表操作（表面底横条）。"""
        row = ttk.Frame(parent, style="ScanRow.TFrame", padding=(theme.PAD_ROW, 6))
        row.pack(fill="x")
        ttk.Label(row, text="扫描范围", style="ScanRowGroup.TLabel").pack(side="left")
        self.hide_git_var = tk.BooleanVar(value=True)
        self.gitignore_var = tk.BooleanVar(value=ui["gitignore"])
        self.source_only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="隐藏 .git 等目录", variable=self.hide_git_var,
                        style="ScanRow.TCheckbutton").pack(side="left", padx=(theme.PAD_GROUP, 0))
        ttk.Checkbutton(row, text="遵循 .gitignore", variable=self.gitignore_var,
                        style="ScanRow.TCheckbutton").pack(side="left", padx=(theme.PAD_ROW, 0))
        ttk.Checkbutton(row, text="仅源码文件", variable=self.source_only_var,
                        style="ScanRow.TCheckbutton",
                        command=self._refresh_ready_card).pack(side="left", padx=(theme.PAD_ROW, 0))
        ttk.Frame(row, style="ScanRow.TFrame").pack(side="left", fill="both", expand=True)
        separator = tk.Frame(row, bg=theme.BORDER, width=1, height=16)
        separator.pack(side="left", fill="y", padx=(0, theme.PAD_ROW))
        self.scan_button = ttk.Button(row, text="扫描", style="Primary.TButton",
                                      command=self._on_scan)
        self.scan_button.pack(side="left")
        ttk.Button(row, text="添加文件夹…", command=self._add_folder_dialog).pack(side="left", padx=(theme.PAD_ROW, 0))
        self.clear_button = ttk.Button(row, text="清空全部", style="DangerGhost.TButton",
                                       command=self._on_clear_clicked)
        self.clear_button.pack(side="left", padx=(theme.PAD_ROW, 0))

    def _build_folder_table(self, parent):
        """文件夹表：3 列（文件夹 / 文件数 / ✕），替代旧 Listbox。

        拖拽接线不受影响：_install_drag_drop 挂在顶层窗口 hwnd 上，不挂列表控件。
        """
        wrap = tk.Frame(parent, bg=theme.WHITE, highlightbackground=theme.LINE, highlightthickness=1)
        wrap.pack(fill="x")
        self.folder_tree = ttk.Treeview(
            wrap, columns=("path", "files", "del"), show="headings",
            selectmode="extended", height=4, style="FolderTree.Treeview",
        )
        self.folder_tree.heading("path", text="文件夹")
        self.folder_tree.heading("files", text="文件数")
        self.folder_tree.heading("del", text="")
        self.folder_tree.column("path", width=420, stretch=True)
        self.folder_tree.column("files", width=90, anchor="e", stretch=False)
        self.folder_tree.column("del", width=36, anchor="center", stretch=False)
        self.folder_tree.tag_configure("row", font=(theme.FONT_MONO, theme.SIZE_BODY),
                                       foreground=theme.INK)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.folder_tree.yview)
        self.folder_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.folder_tree.pack(side="left", fill="both", expand=True)
        self.folder_tree.bind("<Delete>", lambda _event: self._remove_selected())
        self.folder_tree.bind("<Button-1>", self._on_tree_click)
        self.folder_tree.bind("<<TreeviewSelect>>", self._on_folder_selected)

    def _build_preview(self, parent):
        """预览区（兼任拖放目标提示）：头部说明 + 只读等宽文本。

        设计稿的虚线边框 tkinter 无法呈现，按 brief 用 1px BORDER 实线替代。
        """
        box = tk.Frame(parent, bg=theme.PAPER, highlightbackground=theme.BORDER, highlightthickness=1)
        box.pack(fill="both", expand=True, pady=(theme.PAD_GROUP, 0))
        head = tk.Frame(box, bg=theme.PAPER)
        head.pack(fill="x")
        ttk.Label(head, text="输出预览 · 前 15 行", style="FaintSmall.TLabel").pack(
            side="left", padx=(theme.PAD_ROW, 0), pady=4)
        hint = tk.Frame(head, bg=theme.PAPER)
        hint.pack(side="right", padx=(0, theme.PAD_ROW))
        tk.Label(hint, text="＋", bg=theme.PAPER, fg=theme.AMBER,
                 font=(theme.FONT_SANS, theme.SIZE_BODY, "bold")).pack(side="left")
        tk.Label(hint, text=" 将文件夹拖到此处可随时添加", bg=theme.PAPER, fg=theme.MUTED,
                 font=(theme.FONT_SANS, theme.SIZE_SMALL)).pack(side="left")
        self.preview_text = tk.Text(
            box, height=6, state="disabled", wrap="none",
            bg=theme.PAPER, fg=theme.INK, insertbackground=theme.INK,
            relief="flat", highlightthickness=0, padx=10, pady=6,
            font=(theme.FONT_MONO, theme.SIZE_SMALL),
        )
        self.preview_text.pack(fill="both", expand=True)
        self.preview_text.tag_configure("dim", foreground=theme.FAINT)
        self.preview_text.config(state="normal")
        self.preview_text.insert("1.0", "将文件夹拖到此处，复制或保存后此处预览输出前 15 行。", "dim")
        self.preview_text.config(state="disabled")

    def _build_action_card(self, parent):
        """结果卡：状态文字 + 「保存为 ▾」+ 主按钮「复制到剪贴板」。"""
        self.result_card = ttk.Frame(parent, style="ResultCard.TFrame",
                                     padding=(theme.PAD_GROUP, theme.PAD_ROW + 2))
        self.result_card.pack(fill="x", pady=(theme.PAD_GROUP, 0))
        self.status_var = tk.StringVar(value="")
        self.result_status = ttk.Label(self.result_card, textvariable=self.status_var,
                                       style="ResultCard.TLabel")
        self.result_status.pack(side="left", fill="x", expand=True)
        self.result_link = ttk.Label(self.result_card, text="调整上限",
                                     style="ResultCardLink.TLabel", cursor="hand2")
        # ttk.Label 无 command 选项，链接点击用 Button-1 绑定
        self.result_link.bind("<Button-1>", lambda _event: self._open_limit_config())
        self._build_save_menu(self.result_card)
        self.copy_button = ttk.Button(self.result_card, text="复制到剪贴板",
                                      style="Primary.TButton", command=self._on_copy)
        self.save_button.pack(side="left", padx=(theme.PAD_ROW, 0))
        self.copy_button.pack(side="left", padx=(theme.PAD_ROW, 0))

    def _build_save_menu(self, parent):
        """「保存为 ▾」菜单：txt / Markdown / JSON 三项，分别写对应输出文件。"""
        self.save_button = ttk.Menubutton(parent, text="保存为 ▾",
                                          style="Ghost.TMenubutton", direction="above")
        self._save_menu = tk.Menu(self.save_button, tearoff=0)
        self._save_menu.add_command(
            label="txt 文本（directory_tree.txt）", command=lambda: self._on_save("txt"))
        self._save_menu.add_command(
            label="Markdown（directory_tree.md）", command=lambda: self._on_save("md"))
        self._save_menu.add_command(
            label="JSON（directory_tree.json）", command=lambda: self._on_save("json"))
        self.save_button.configure(menu=self._save_menu)

    def _build_drawer(self, parent):
        """底部抽屉：最近一条日志 + 「展开日志 ▾/▴」切换日志区显隐（默认折叠）。"""
        self.drawer_frame = ttk.Frame(parent)
        self.drawer_frame.pack(fill="x", pady=(theme.PAD_ROW, 0))
        self.latest_log_var = tk.StringVar(value="尚无日志")
        ttk.Label(self.drawer_frame, textvariable=self.latest_log_var,
                  style="DrawerLog.TLabel").pack(side="left")
        self.log_toggle_button = ttk.Button(self.drawer_frame, text="展开日志 ▾",
                                            style="Link.TButton", width=14,
                                            command=self._toggle_log)
        self.log_toggle_button.pack(side="right")
        # 日志区默认折叠：先创建不布局，展开时插到抽屉行之前
        self._log_frame = ttk.Frame(parent)
        log_scroll = ttk.Scrollbar(self._log_frame)
        log_scroll.pack(side="right", fill="y")
        self.log_text = tk.Text(
            self._log_frame, height=8, state="disabled", wrap="none",
            bg=theme.WHITE, fg=theme.INK, insertbackground=theme.INK,
            relief="flat", highlightthickness=1, highlightbackground=theme.LINE,
            font=(theme.FONT_MONO, theme.SIZE_SMALL), yscrollcommand=log_scroll.set,
        )
        self.log_text.pack(fill="both", expand=True)
        log_scroll.config(command=self.log_text.yview)

    def _build_status_banner(self, parent):
        """按安装状态在工具行上方渲染提示横幅；ok 态不创建任何控件。

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

    # ── 结果卡状态机 ──

    def _apply_card_style(self, warn: bool):
        """结果卡视觉：正常（表面底）⇄ 警告（警告底 + 强调文字）。"""
        if warn:
            self.result_card.configure(style="ResultCardWarn.TFrame")
            self.result_status.configure(style="ResultCardWarn.TLabel")
        else:
            self.result_card.configure(style="ResultCard.TFrame")
            self.result_status.configure(style="ResultCard.TLabel")

    def _refresh_ready_card(self):
        """就绪态门卫：缓存有效 → 启用复制/保存并显示待复制数；
        从未扫描或列表/选项变化 → 禁用并提示重新扫描。"""
        self.result_link.pack_forget()
        self._apply_card_style(warn=False)
        if not self._cache_valid():
            self.copy_button.config(state="disabled")
            self.copy_button.config(state="disabled")
            self.save_button.config(state="disabled")
            self.status_var.set(
                "请先执行扫描。" if self._scan_opts is None
                else "列表或选项已变化，请重新扫描。")
            return
        self._apply_card_style(warn=False)
        self.copy_button.config(state="normal")
        self.save_button.config(state="normal")
        self.status_var.set(
            f"共 {_fmt_count(self._sum_row_counts())} 个文件待复制，结果将写入剪贴板。")

    def _show_result_card(self, payload: dict):
        """操作结果态：copy/save 完成后由 _poll 调用；任一文件夹截断切警告态。"""
        if payload.get("truncated"):
            self.status_var.set(payload.get("warn_message") or payload.get("message", ""))
            self.result_link.pack(side="left", padx=(theme.PAD_ROW, 0), before=self.save_button)
            self._apply_card_style(warn=True)
        else:
            self.result_link.pack_forget()
            self._apply_card_style(warn=False)

    def _open_limit_config(self):
        """警告态的「调整上限」：打开配置文件调整 maxFiles。"""
        open_config_file()

    # ── 列表行操作（ttk.Treeview 等价于旧 folder_list.get/insert/delete）──

    def _iter_folder_paths(self):
        paths = []
        for iid in self.folder_tree.get_children():
            values = self.folder_tree.item(iid, "values")
            if values:
                paths.append(str(values[0]))
        return paths

    def _sum_row_counts(self) -> int:
        total = 0
        for iid in self.folder_tree.get_children():
            values = self.folder_tree.item(iid, "values")
            try:
                total += int(str(values[1]).replace(",", ""))
            except (TypeError, ValueError, IndexError):
                continue
        return total

    def _add_folder_row(self, path: str) -> bool:
        if path in self._iter_folder_paths():
            return False
        self.folder_tree.insert("", "end", values=(path, "—", "✕"), tags=("row",))
        return True

    def _remove_row(self, iid):
        self.folder_tree.delete(iid)
        self._refresh_ready_card()

    def _remove_selected(self):
        for iid in self.folder_tree.selection():
            self._remove_row(iid)

    def _on_tree_click(self, event):
        """点击第三列（✕）删除对应行；其余列保持原生选择行为。"""
        if self.folder_tree.identify_column(event.x) != "#3":
            return
        iid = self.folder_tree.identify_row(event.y)
        if iid:
            self._remove_row(iid)

    def _on_clear_clicked(self):
        """「清空全部」两步确认（设计稿交互态）：首点武装 2.5 秒，二点执行。"""
        if not self._clear_armed:
            self._clear_armed = True
            self.clear_button.config(text="确认清空？", style="DangerConfirm.TButton")
            self._clear_after_id = self.root.after(2500, self._disarm_clear)
            return
        self._disarm_clear()
        for iid in self.folder_tree.get_children():
            self.folder_tree.delete(iid)
        self._refresh_ready_card()

    def _disarm_clear(self):
        self._clear_armed = False
        self.clear_button.config(text="清空全部", style="DangerGhost.TButton")
        if self._clear_after_id is not None:
            self.root.after_cancel(self._clear_after_id)
            self._clear_after_id = None

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
        dropped_dirs = False
        for path in dropped:
            if os.path.isdir(path):
                self._add_folder_row(path)
                dropped_dirs = True
            else:
                self._append_log(f"[跳过] 不是文件夹：{path}")
        if dropped_dirs:
            self._refresh_ready_card()
            if self.auto_copy_var.get():
                # 拖入后自动复制：缓存有效直接复制，否则先扫描，scan_done 续跑复制
                self._start_scan_after_drop()

    # ── 动作 ──

    def _add_folder_dialog(self):
        path = filedialog.askdirectory(title="选择要扫描的文件夹")
        if path:
            path = os.path.normpath(path)
            if self._add_folder_row(path):
                self._refresh_ready_card()

    def _on_tray_toggle(self):
        update_config_values({"enableTray": bool(self.tray_var.get())})
        if self.tray_var.get():
            self._ensure_tray()

    def _on_uninstall_menu_selected(self):
        """菜单内两步确认：首点武装（3 秒不点弹回），二点进入既有卸载确认流程。"""
        if self._uninstall_armed:
            if self._uninstall_after_id is not None:
                self.root.after_cancel(self._uninstall_after_id)
                self._uninstall_after_id = None
            self._uninstall_armed = False
            self._settings_menu.entryconfig(self._uninstall_menu_index, label="卸载 copy-tree…")
            self._on_uninstall()
            return
        self._uninstall_armed = True
        self._settings_menu.entryconfig(self._uninstall_menu_index, label="⚠ 再点一次确认卸载")
        self._uninstall_after_id = self.root.after(3000, self._disarm_uninstall)

    def _disarm_uninstall(self):
        self._uninstall_after_id = None
        self._uninstall_armed = False
        if self._settings_menu is not None:
            self._settings_menu.entryconfig(self._uninstall_menu_index, label="卸载 copy-tree…")

    def _on_uninstall(self):
        if not messagebox.askyesno(
            "卸载 copy-tree",
            "将移除右键菜单、开始菜单快捷方式和本机安装副本。\n确定卸载吗？",
        ):
            return
        stop_tray()
        subprocess.Popen([sys.executable, "--uninstall"])
        self.root.destroy()

    def _flash_copy_button(self):
        """复制成功反馈：文字变「已复制 ✓」2 秒后还原，期间 _on_copy 防重入。"""
        self._copy_flash_active = True
        self.copy_button.config(text="已复制 ✓", style="Success.TButton")
        self.root.after(2000, self._reset_copy_button)

    def _reset_copy_button(self):
        self._copy_flash_active = False
        self.copy_button.config(text="复制到剪贴板", style="Primary.TButton")

    def _start_scan(self):
        folders = self._iter_folder_paths()
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
        self._pending_scan_opts = self._scan_opt_snapshot()
        self.scan_button.config(state="disabled")
        self.copy_button.config(state="disabled")
        self.save_button.config(state="disabled")
        self.status_var.set("扫描中…")
        self._worker = threading.Thread(
            target=self._worker_main, args=(folders, opts), daemon=True
        )
        self._worker.start()

    def _scan_opt_snapshot(self) -> dict:
        """影响扫描结果的选项快照：这些值变化即要求重新扫描。
        输出格式不在此列——格式只影响消费端（复制/保存）的现场格式化。"""
        return {
            "hide_git": self.hide_git_var.get(),
            "gitignore": self.gitignore_var.get(),
            "source_only": self.source_only_var.get(),
            "size": self.size_var.get(),
            "time": self.time_var.get(),
        }

    def _cache_valid(self) -> bool:
        if not self._scan_cache or self._scan_opts != self._scan_opt_snapshot():
            return False
        return set(self._iter_folder_paths()) == set(self._scan_cache)

    def _require_scan(self) -> bool:
        if self._cache_valid():
            return True
        self._refresh_ready_card()
        return False

    def _on_scan(self):
        """「扫描」按钮：显式触发扫描（唯一让缓存生效的入口）。"""
        self._start_scan()

    def _on_copy(self):
        """「复制到剪贴板」：防重入 + 缓存守卫，只消费缓存，不重新扫描。"""
        if self._copy_flash_active:
            return
        if not self._require_scan():
            return
        self._copy_from_cache()

    def _on_save(self, kind: str):
        """「保存为 ▾」：kind ∈ _SAVE_KINDS；缓存守卫通过后由主线程消费缓存。"""
        if kind not in _SAVE_KINDS:
            return
        if not self._require_scan():
            return
        self._save_kind = kind
        self._save_from_cache(kind)

    def _start_scan_after_drop(self):
        """拖入后自动复制：缓存有效直接复制；否则标记待复制并扫描，scan_done 续跑。"""
        if self._cache_valid():
            self._copy_from_cache()
            return
        self._pending_auto_copy = True
        self._start_scan()

    def _worker_main(self, folders, opts):
        """只做扫描：结果经消息队列回传主线程落入 _scan_cache，不直接复制/保存。"""
        total_files = 0
        any_truncated = False
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
                total_files += int(result.total_files)
                if result.truncated:
                    any_truncated = True

                # 表格文件数回填、扫描缓存、预览填充都经队列回 Tk 主线程执行
                self.actions.put(("count", (folder, int(result.total_files))))
                # payload 必须是 (path, data) 二元组：_poll 统一按 kind, payload 解包
                self.actions.put(("scanned", (folder, {
                    "tree_text": tree_text,
                    "result": result,
                    "files": int(result.total_files),
                    "truncated": bool(result.truncated),
                })))

                note = f"{result.total_files} 个文件，{result.total_dirs} 个文件夹"
                if result.truncated:
                    note += f"（已截断：{describe_truncation(result)}）"
                self.actions.put(("log", f"[完成] {folder} — {note}"))
            except Exception as e:  # 单个目录失败不影响其余目录
                logger.exception("窗口扫描失败 {}", folder)
                self.actions.put(("log", f"[失败] {folder} — {e}"))

        self.actions.put(("scan_done", {
            "files": total_files,
            "truncated": any_truncated,
        }))

    # ── 缓存消费：复制 / 保存 / 预览 ──

    def _copy_from_cache(self):
        fmt = _FORMAT_LABELS.get(self.format_var.get(), "text")
        outputs = []
        total_files = 0
        any_truncated = False
        for path in self._iter_folder_paths():
            entry = self._scan_cache.get(path)
            if entry is None:
                continue
            outputs.append(format_output(
                entry["tree_text"], fmt, result=entry["result"],
                show_size=self.size_var.get(), show_time=self.time_var.get(),
            ))
            total_files += entry["files"]
            any_truncated = any_truncated or entry["truncated"]
        combined = "\n\n".join(outputs)
        if not outputs or not copy_to_clipboard(combined):
            self._show_result_card({
                "action": "copy", "copy_ok": False, "truncated": any_truncated,
                "files": total_files, "message": "复制失败，请重试。", "warn_message": "",
            })
            return
        self.copy_button.config(text="已复制 ✓", style="Success.TButton")
        self._copy_flash_active = True
        self.root.after(2000, self._reset_copy_button)
        self._show_result_card({
            "action": "copy", "copy_ok": True, "truncated": any_truncated,
            "files": total_files,
            "message": f"已复制 {_fmt_count(total_files)} 个文件，结果已写入剪贴板。",
            "warn_message": f"已复制 {_fmt_count(total_files)} 个文件，超出上限，结果可能不完整",
        })
        self._append_log("")

    def _save_from_cache(self, kind: str):
        filename = {"txt": DEFAULT_OUTPUT_FILENAME_TXT,
                    "md": DEFAULT_OUTPUT_FILENAME_MD,
                    "json": DEFAULT_OUTPUT_FILENAME_JSON}[kind]
        saved_count = 0
        total_files = 0
        any_truncated = False
        for path in self._iter_folder_paths():
            entry = self._scan_cache.get(path)
            if entry is None:
                continue
            if kind == "txt":
                content = entry["tree_text"]  # txt 沿用 tree_text，行为与旧版完全一致
            else:
                # md/json 按保存类型对应格式现场格式化，保证文件内容与扩展名一致
                content = format_output(
                    entry["tree_text"], "markdown" if kind == "md" else "json",
                    result=entry["result"],
                    show_size=self.size_var.get(), show_time=self.time_var.get(),
                )
            save_path = os.path.join(path, filename)
            try:
                with open(save_path, "w", encoding="utf-8") as f:
                    f.write(content)
                saved_count += 1
                total_files += entry["files"]
                any_truncated = any_truncated or entry["truncated"]
                self._append_log(f"[保存] {save_path}")
            except OSError as e:
                self._append_log(f"[失败] 保存 {save_path}：{e}")
        if saved_count:
            self._show_result_card({
                "action": "save", "copy_ok": False, "truncated": any_truncated,
                "files": total_files,
                "message": f"已保存 {filename} 到 {saved_count} 个文件夹。",
                "warn_message": f"已保存 {filename}，超出上限，结果可能不完整",
            })
        else:
            self._show_result_card({
                "action": "save", "copy_ok": False, "truncated": False,
                "files": 0, "message": "保存失败，详见日志。", "warn_message": "",
            })
        self._append_log("")

    def _on_folder_selected(self, _event=None):
        """选中切换 → 预览跟随该文件夹的缓存结果；未扫描则提示先扫描。"""
        selection = self.folder_tree.selection()
        if not selection:
            return
        values = self.folder_tree.item(selection[0], "values")
        if not values:
            return
        path = str(values[0])
        entry = self._scan_cache.get(path)
        if entry is None:
            self.preview_text.config(state="normal")
            self.preview_text.delete("1.0", "end")
            self.preview_text.insert("1.0", f"{path}\n尚未扫描——点击「扫描」后此处显示该文件夹的目录树预览。")
            self.preview_text.config(state="disabled")
            return
        self._update_preview(entry["tree_text"])

    # ── 预览与日志 ──

    def _update_preview(self, tree_text: str):
        """把 tree_text 前 15 行写入只读预览区；超出时附省略提示行。"""
        lines = tree_text.splitlines()
        self.preview_text.config(state="normal")
        self.preview_text.delete("1.0", "end")
        if lines:
            self.preview_text.insert("1.0", "\n".join(lines[:_PREVIEW_LINE_COUNT]))
            if len(lines) > _PREVIEW_LINE_COUNT:
                self.preview_text.insert("end", f"\n… 仅预览前 {_PREVIEW_LINE_COUNT} 行", "dim")
        else:
            self.preview_text.insert("1.0", "（该文件夹没有可显示的内容）", "dim")
        self.preview_text.config(state="disabled")

    def _update_row_count(self, path: str, total: int):
        """扫描完成后按路径回填表格「文件数」列。"""
        for iid in self.folder_tree.get_children():
            values = self.folder_tree.item(iid, "values")
            if values and str(values[0]) == str(path):
                self.folder_tree.set(iid, "files", _fmt_count(total))
                return

    def _toggle_log(self):
        if self._log_expanded:
            self._log_frame.pack_forget()
            self.log_toggle_button.config(text="展开日志 ▾")
        else:
            self._log_frame.pack(fill="x", before=self.drawer_frame)
            self.log_toggle_button.config(text="收起日志 ▴")
        self._log_expanded = not self._log_expanded

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
                elif kind == "count":
                    path, total = payload
                    self._update_row_count(path, total)
                elif kind == "preview":
                    self._update_preview(payload)
                elif kind == "scanned":
                    path, data = payload
                    self._scan_cache[path] = data
                    if self.folder_tree.selection() and \
                            str(self.folder_tree.item(self.folder_tree.selection()[0], "values")[0]) == path:
                        self._update_preview(data["tree_text"])
                elif kind == "scan_done":
                    # 只在本次扫描确有选项快照时生效：seed/复用的既有缓存不被置为无效
                    if self._pending_scan_opts is not None:
                        self._scan_opts = self._pending_scan_opts
                        self._pending_scan_opts = None
                    self.scan_button.config(state="normal")
                    self._refresh_ready_card()
                    self._on_folder_selected()
                    if self._pending_auto_copy:
                        self._pending_auto_copy = False
                        if self._cache_valid():
                            self._copy_from_cache()
                elif kind == "done":
                    self.copy_button.config(state="normal")
                    if isinstance(payload, dict):
                        # 结果卡状态机：dict payload 携带截断/计数/动作信息
                        self.status_var.set(payload.get("message", ""))
                        self._show_result_card(payload)
                        if payload.get("copy_ok"):
                            self._flash_copy_button()
                    else:  # 兼容纯字符串完成消息
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
        if line:  # 空行仅作日志分隔，不覆盖抽屉行
            self.latest_log_var.set(f"{time.strftime('%H:%M')} {line}")

    # ── 视觉细节 ──

    def _tint_titlebar(self):
        """DWM 标题栏着成 PINE 色（Win11/部分 Win10）；失败静默，不影响功能。

        COLORREF 布局为 0x00BBGGRR，与 #RRGGBB 的字节序相反，需换算。
        """
        try:
            hwnd = user32.GetParent(self.root.winfo_id())
            if not hwnd:
                return
            r = int(theme.PINE[1:3], 16)
            g = int(theme.PINE[3:5], 16)
            b = int(theme.PINE[5:7], 16)
            color = ctypes.c_uint((b << 16) | (g << 8) | r)
            dwmapi = ctypes.windll.dwmapi
            dwmapi.DwmSetWindowAttribute.argtypes = [
                ctypes.wintypes.HWND, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
            dwmapi.DwmSetWindowAttribute.restype = ctypes.HRESULT
            dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), 4)  # DWMWA_CAPTION_COLOR
        except Exception:
            logger.debug("标题栏着色不可用（DwmSetWindowAttribute 失败或系统不支持）")

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
