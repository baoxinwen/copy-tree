"""copy-tree 入口：CLI 解析、GUI/CLI 模式分发。"""

import argparse
import ctypes
import ctypes.wintypes
import filecmp
import msvcrt
import os
import shutil
import sys

from loguru import logger

from .clipboard import copy_to_clipboard, get_last_failure_stage
from .config import (
    get_config_warnings,
    get_effective_config,
    update_config_values,
)
from .constants import (
    CONFIG_FILE,
    DEFAULT_OUTPUT_FILENAME_JSON,
    DEFAULT_OUTPUT_FILENAME_MD,
    DEFAULT_OUTPUT_FILENAME_TXT,
    GENERATED_OUTPUT_FILENAMES,
    INSTALL_CLI_EXE,
    INSTALL_DIR,
    INSTALL_EXE,
    MSG_INSTALLED,
    MSG_NOTIFY_FAIL,
    MSG_NOTIFY_SUCCESS,
    MSG_NOTIFY_SUCCESS_TRUNCATED,
    MSG_UNINSTALLED,
    MSG_UNINSTALLED_RESIDUE,
    SOURCE_CODE_EXTENSIONS,
    SOURCE_CODE_FILENAMES,
    VERSION,
)
from .formatter import format_output
from .logging_setup import setup_logging
from .notify import show_notification, wait_notification
from .registry import (
    get_installed_exe_path,
    get_installed_version,
    install,
    is_registered,
    uninstall,
)
from .scanner import build_tree_text, describe_truncation, normalize_path, scan_directory
from .winapi import (
    ATTACH_PARENT_PROCESS,
    FILE_TYPE_CHAR,
    FILE_TYPE_UNKNOWN,
    IDNO,
    IDYES,
    INVALID_HANDLE_VALUE,
    MB_ICONQUESTION,
    MB_YESNO,
    MB_YESNOCANCEL,
    MOVEFILE_DELAY_UNTIL_REBOOT,
    PROCESS_QUERY_LIMITED_INFORMATION,
    STD_ERROR_HANDLE,
    STD_OUTPUT_HANDLE,
    TH32CS_SNAPPROCESS,
    PROCESSENTRY32W,
    kernel32,
    user32,
)

_SETUP_ACTION_CANCEL = 0
_SETUP_ACTION_INSTALL = 1
_SETUP_ACTION_UNINSTALL = 2
_stdio_ready = False
_parent_process_name: str | None = None
_parent_process_names: list[str] | None = None
_force_notify_mode = False

_TERMINAL_PROCESS_NAMES = frozenset({
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "windowsterminal.exe",
    "wt.exe",
    "conhost.exe",
    "openconsole.exe",
})


class _ArgumentParser(argparse.ArgumentParser):
    def print_help(self, file=None):
        _print(self.format_help().rstrip("\n"))

    def _print_message(self, message, file=None):
        if message:
            _print_err(message.rstrip("\n"))

    def exit(self, status=0, message=None):
        if message:
            _print_err(message.rstrip("\n"))
        if status == 2:
            # argparse 解析错误默认退出码是 2；按规格归一为“参数错误=1”，
            # 退出码 2 保留给剪贴板写入失败。
            status = 1
        if status:
            _pause_if_double_clicked_cli()
        _exit(status)


def _has_console() -> bool:
    """检测当前进程是否拥有控制台。"""
    try:
        return kernel32.GetConsoleWindow() != 0
    except Exception:
        return False


def _is_cli_mode() -> bool:
    """检测是否有可用的 CLI 输出目标。"""
    if _force_notify_mode:
        return False
    if _launched_from_explorer():
        return False
    return _stdio_ready or _has_console()


def _attach_parent_console() -> bool:
    """附加到父进程控制台或继承的标准句柄。"""
    global _stdio_ready
    if _stdio_ready:
        return True
    if _force_notify_mode:
        return False
    if _launched_from_explorer():
        return False
    if _has_console():
        # GUI 子系统常见：控制台在而标准句柄未初始化，从控制台设备回填空缺
        # 流；已存在的流（如重定向管道）保持原样，不破坏重定向语义
        try:
            if sys.stdout is None:
                sys.stdout = open("CONOUT$", "w", encoding="utf-8", closefd=False)
            if sys.stderr is None:
                sys.stderr = open("CONERR$", "w", encoding="utf-8", closefd=False)
        except Exception:
            pass
        _stdio_ready = True
        return True
    stdout = _open_std_stream(STD_OUTPUT_HANDLE)
    stderr = _open_std_stream(STD_ERROR_HANDLE)
    if stdout or stderr:
        if stdout:
            sys.stdout = stdout
        if stderr:
            sys.stderr = stderr
        _stdio_ready = True
        return True
    try:
        if kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", closefd=False)
            sys.stderr = open("CONERR$", "w", encoding="utf-8", closefd=False)
            _stdio_ready = True
            return True
    except Exception:
        pass
    return False


def _launched_from_explorer() -> bool:
    """判断是否由资源管理器启动，用于强制走 GUI 通知模式。"""
    return "explorer.exe" in _get_parent_process_names()[:2]


def _get_parent_process_name() -> str:
    global _parent_process_name
    if _parent_process_name is not None:
        return _parent_process_name
    names = _get_parent_process_names()
    _parent_process_name = names[0] if names else ""
    return _parent_process_name


def _get_parent_process_names() -> list[str]:
    """返回当前进程向上若干级父进程名，兼容 PyInstaller onefile 包装进程。"""
    global _parent_process_names
    if _parent_process_names is not None:
        return list(_parent_process_names)

    names: list[str] = []
    pid = os.getpid()
    for _ in range(3):
        parent_pid = _get_parent_process_id(pid)
        if not parent_pid or parent_pid == pid:
            break
        name = _get_process_image_name(parent_pid)
        if name:
            names.append(name)
        pid = parent_pid
    _parent_process_names = names
    return list(_parent_process_names)


def _get_process_image_name(pid: int) -> str:
    try:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = ctypes.wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return os.path.basename(buffer.value).lower()
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        pass
    return ""


def _get_parent_process_id(pid: int) -> int:
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    snapshot_value = getattr(snapshot, "value", snapshot)
    if not snapshot_value or snapshot_value == INVALID_HANDLE_VALUE:
        return 0
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return 0
        while True:
            if int(entry.th32ProcessID) == int(pid):
                return int(entry.th32ParentProcessID)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                return 0
    finally:
        kernel32.CloseHandle(snapshot)


def _open_std_stream(handle_id: int):
    """打开继承的 stdout/stderr 句柄，支持重定向和测试捕获。"""
    try:
        handle = kernel32.GetStdHandle(handle_id)
        handle_value = _handle_value(handle)
        if not _is_valid_handle_value(handle_value):
            return None
        file_type = kernel32.GetFileType(handle)
        if file_type == FILE_TYPE_UNKNOWN:
            return None
        fd = msvcrt.open_osfhandle(handle_value, os.O_TEXT)
        return open(fd, "w", encoding="utf-8", buffering=1, closefd=False)
    except Exception:
        return None


def _handle_value(handle) -> int:
    return getattr(handle, "value", handle)


def _is_valid_handle_value(handle_value: int | None) -> bool:
    return bool(handle_value) and handle_value != INVALID_HANDLE_VALUE


def _build_arg_parser() -> argparse.ArgumentParser:
    cli_executable = _is_cli_executable()
    parser = _ArgumentParser(
        prog=_program_usage_name(),
        description=(
            "copy-tree-cli：输出目录树到 stdout，可选写入剪贴板"
            if cli_executable
            else "copy-tree：右键菜单安装/卸载与复制通知入口"
        ),
    )
    parser.add_argument("path", nargs="?", help="目标文件夹路径")
    parser.add_argument("--size", action="store_true", help="显示文件大小")
    parser.add_argument("--time", action="store_true", help="显示修改时间")
    parser.add_argument(
        "--format",
        choices=["text", "markdown", "markdown-list", "json", "paths", "names", "summary"],
        dest="fmt",
        help="输出格式：text、markdown、markdown-list、json、paths、names 或 summary",
    )
    parser.add_argument(
        "--filter", action="store_true", dest="apply_filter",
        help="过滤配置文件中指定的目录和文件"
    )
    parser.add_argument(
        "--exclude", action="append", default=[],
        help="额外排除的目录或文件名（可多次使用）",
    )
    parser.add_argument(
        "--source-only", action="store_true", help="仅显示源码文件（内置列表）"
    )
    parser.add_argument(
        "--filter-ext", action="store_true", dest="filter_ext",
        help="仅显示配置文件中 filterExt 指定后缀的文件"
    )
    parser.add_argument(
        "--gitignore", action="store_true", dest="gitignore",
        help="遵循目录中的 .gitignore 规则过滤"
    )
    parser.add_argument(
        "--save", action="store_true", help="保存到目标目录下的 directory_tree.txt"
    )
    parser.add_argument(
        "--save-md", action="store_true", dest="save_md",
        help="保存到目标目录下的 directory_tree.md"
    )
    parser.add_argument(
        "--save-json", action="store_true", dest="save_json",
        help="保存到目标目录下的 directory_tree.json"
    )
    parser.add_argument(
        "--max-depth", type=int, default=None, help="限制显示深度（0 = 仅根目录）"
    )
    parser.add_argument(
        "--no-clipboard",
        "--stdout-only",
        action="store_true",
        dest="no_clipboard",
        help="仅输出到 stdout，不写入剪贴板（CLI 脚本场景）",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="检查配置文件并输出校验结果，不扫描目录",
    )
    if cli_executable:
        setup_help = argparse.SUPPRESS
    else:
        setup_help = None
    parser.add_argument(
        "--install",
        action="store_true",
        help=setup_help or "安装：注册右键菜单",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help=setup_help or "卸载：清除右键菜单",
    )
    parser.add_argument(
        "--config",
        action="store_true",
        help=setup_help or "打开配置文件进行编辑",
    )
    parser.add_argument(
        "--version", action="store_true", help="显示版本号"
    )
    if not cli_executable:
        # --notify 是右键菜单注入给 copy-tree.exe 的隐藏参数；
        # CLI 入口不注册它，避免脚本误用后被静默吞掉 stdout。
        parser.add_argument(
            "--notify",
            action="store_true",
            help=argparse.SUPPRESS,
        )
    return parser


def main():
    global _force_notify_mode
    # --help 和参数错误会在 parse_args 内部经 _ArgumentParser.exit 进入 _exit 记日志，
    # 日志初始化必须先于参数解析；--notify 是右键菜单注入的隐藏参数，
    # 直接扫 argv 判定，避免模式检测依赖解析结果。
    _force_notify_mode = "--notify" in sys.argv[1:]
    if not _force_notify_mode:
        _attach_parent_console()
    setup_logging(enable_stderr=_is_cli_mode())
    logger.info(
        "启动 mode={} notify_force={} argv={}",
        "CLI" if _is_cli_mode() else "GUI",
        _force_notify_mode,
        " ".join(sys.argv),
    )
    parser = _build_arg_parser()
    args = parser.parse_args()

    if _is_cli_executable() and (args.install or args.uninstall or args.config):
        logger.warning(
            "CLI 入口收到安装类参数 install={} uninstall={} config={}，已拒绝",
            args.install, args.uninstall, args.config,
        )
        _print_err("安装、卸载和打开配置请使用 copy-tree.exe。copy-tree-cli.exe 仅用于命令行复制/输出。")
        _pause_if_double_clicked_cli()
        _exit(1)

    if args.version:
        _print(f"{_program_display_name()} {VERSION}")
        _exit(0)

    if _is_no_args_mode(args):
        _handle_no_args(parser)
        return

    if args.install:
        _handle_install()
        return

    if args.uninstall:
        _handle_uninstall()
        return

    if args.config:
        _handle_open_config()
        return

    if args.check_config:
        _check_config()
        _exit(0 if not get_config_warnings() else 1)

    _handle_directory_scan(args, parser)


def _is_no_args_mode(args) -> bool:
    return (
        not args.path
        and not args.install
        and not args.uninstall
        and not args.config
        and not args.version
        and not args.check_config
    )


def _handle_no_args(parser):
    if _is_gui_executable():
        _manage_install_from_gui()
        _exit(0)
    parser.print_help()
    _pause_if_double_clicked_cli()
    _exit(1)


def _handle_install():
    exe_path = _get_exe_path()
    if _install_from_source(exe_path):
        _report_setup_status(MSG_INSTALLED)
    else:
        _report_setup_status("安装失败")
        _exit(3)
    _exit(0)


def _handle_uninstall():
    installed_exe_path = get_installed_exe_path()
    if uninstall():
        _cleanup_installed_files(installed_exe_path)
        _report_setup_status(_uninstall_notice())
    else:
        _report_setup_status("卸载失败")
        _exit(3)
    _exit(0)


def _handle_open_config():
    from .config import open_config_file

    if not open_config_file():
        _notify("无法打开配置文件")
        _exit(3)
    _exit(0)


def _handle_directory_scan(args, parser):
    if not args.path:
        parser.print_help()
        _pause_if_double_clicked_cli()
        _exit(1)

    target = os.path.abspath(args.path)
    scan_target = normalize_path(target)
    if not os.path.isdir(scan_target):
        logger.warning("目标目录无效: {}", target)
        if _is_cli_mode():
            _print_err(f"'{target}' 不是有效的目录")
        else:
            _notify(f"目录无效：{target}")
        _exit(1)

    config, config_warnings, show_size, show_time = _merge_scan_config(args)
    logger.info(
        "扫描 {} maxFiles={} maxItemsPerLevel={} maxDepth={} filter={} source_only={} filter_ext={} exclude={}",
        target, config["maxFiles"], config["maxItemsPerLevel"],
        args.max_depth if args.max_depth is not None else config.get("maxDepth"),
        args.apply_filter, args.source_only, args.filter_ext,
        list(args.exclude) or "无",
    )
    result = _execute_scan(scan_target, config, args, show_size, show_time)
    output, save_display_path = _format_and_save(
        scan_target, target, result, config, args, show_size, show_time
    )
    _copy_and_output(result, output, args, config_warnings, save_display_path)


def _merge_scan_config(args):
    cli_overrides = {}
    if args.fmt:
        cli_overrides["defaultFormat"] = args.fmt

    config = get_effective_config(cli_overrides or None)
    config_warnings = get_config_warnings()
    show_size = args.size or config.get("showFileSize", False)
    show_time = args.time or config.get("showFileTime", False)
    return config, config_warnings, show_size, show_time


def _execute_scan(target, config, args, show_size, show_time):
    exclude_dirs, exclude_files = _build_exclude_sets(args, config)
    # excludePatterns 与 excludeDirs 同生命周期：仅在主动启用过滤时生效
    exclude_patterns = set(config.get("excludePatterns") or []) if args.apply_filter else set()
    max_depth = _resolve_max_depth(args, config)
    include_ext, include_names = _resolve_include_filters(args, config)
    respect_gitignore = args.gitignore or config.get("respectGitignore", False)
    prune_empty_dirs = (
        args.apply_filter
        or bool(args.exclude)
        or args.source_only
        or args.filter_ext
        or respect_gitignore
    )

    return scan_directory(
        path=target,
        exclude_dirs=exclude_dirs,
        exclude_files=exclude_files,
        exclude_patterns=exclude_patterns or None,
        max_files=config["maxFiles"],
        max_items_per_level=config["maxItemsPerLevel"],
        show_size=show_size,
        show_time=show_time,
        max_depth=max_depth,
        include_ext=include_ext,
        include_names=include_names,
        prune_empty_dirs=prune_empty_dirs,
        respect_gitignore=respect_gitignore,
    )


def _build_exclude_sets(args, config):
    if args.apply_filter:
        exclude_dirs = set(config["excludeDirs"])
        exclude_files = set(config["excludeFiles"])
    else:
        exclude_dirs = set()
        exclude_files = set()
    for name in args.exclude:
        exclude_dirs.add(name)
        exclude_files.add(name)
    exclude_files.update(GENERATED_OUTPUT_FILENAMES)
    return exclude_dirs, exclude_files


def _resolve_max_depth(args, config):
    max_depth = args.max_depth if args.max_depth is not None else config.get("maxDepth", -1)
    if max_depth < -1:
        max_depth = 0
    return None if max_depth == -1 else max_depth


def _resolve_include_filters(args, config):
    include_ext = None
    include_names = None
    if args.source_only:
        include_ext = SOURCE_CODE_EXTENSIONS
        include_names = SOURCE_CODE_FILENAMES
    elif args.filter_ext:
        include_ext = set(config.get("filterExt", []))
    return include_ext, include_names


def _format_and_save(target, display_target, result, config, args, show_size, show_time):
    tree_text = build_tree_text(result, show_size=show_size, show_time=show_time)
    default_format = config.get("defaultFormat", "text")
    output = format_output(
        tree_text,
        default_format,
        result=result,
        show_size=show_size,
        show_time=show_time,
    )

    # 保存内容与当前格式相同时直接复用，避免格式化两遍
    md_content = output if default_format == "markdown" else None
    json_content = output if default_format == "json" else None

    save_display_path = ""
    if args.save or args.save_md or args.save_json:
        saved_names = _save_to_file(
            target, tree_text, result, args, show_size, show_time, md_content, json_content
        )
        save_display_path = "、".join(
            os.path.join(display_target, name) for name in saved_names
        )

    return output, save_display_path


def _save_to_file(
    target: str,
    tree_text: str,
    result,
    args,
    show_size: bool,
    show_time: bool,
    md_content: str | None = None,
    json_content: str | None = None,
) -> list[str]:
    """按开关分别保存 txt / Markdown / JSON，返回实际写出的文件名列表；失败时提示并退出码 3。"""
    def _write(filename: str, content: str):
        save_path = os.path.join(target, filename)
        try:
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            logger.error("保存失败 {} -> {}: {}", filename, save_path, e)
            if not _is_cli_mode():
                _notify(f"保存失败：{e}")
            else:
                _print_err(f"保存失败：{e}")
            _exit(3)
        logger.info("已保存 {}", save_path)
        saved.append(filename)

    saved: list[str] = []
    if args.save:
        _write(DEFAULT_OUTPUT_FILENAME_TXT, tree_text)
    if args.save_md:
        _write(
            DEFAULT_OUTPUT_FILENAME_MD,
            md_content
            if md_content is not None
            else format_output(
                tree_text, "markdown", result=result,
                show_size=show_size, show_time=show_time,
            ),
        )
    if args.save_json:
        _write(
            DEFAULT_OUTPUT_FILENAME_JSON,
            json_content
            if json_content is not None
            else format_output(
                tree_text, "json", result=result,
                show_size=show_size, show_time=show_time,
            ),
        )
    return saved


def _copy_and_output(result, output, args, config_warnings, save_display_path=""):
    copied_to_clipboard = not args.no_clipboard
    if copied_to_clipboard and not copy_to_clipboard(output):
        reason = "内存不足，无法写入剪贴板" if get_last_failure_stage() == "memory" else "剪贴板被占用"
        logger.error("剪贴板写入失败（重试后仍失败），输出长度 {} 字符", len(output))
        if not _is_cli_mode():
            _notify(MSG_NOTIFY_FAIL.format(error=reason))
        else:
            _print_err(f"复制失败：{reason}")
        _exit(2)

    if not _is_cli_mode():
        _notify_result(result, copied_to_clipboard, config_warnings, save_display_path)
    else:
        _attach_parent_console()
        _print_config_warnings(config_warnings)
        _print(output)

    logger.info(
        "复制完成 files={} dirs={} truncated={} clipboard={} saved={}",
        result.total_files, result.total_dirs, result.truncated,
        copied_to_clipboard, save_display_path or "无",
    )
    _exit(0)


def _notify_result(result, copied_to_clipboard, config_warnings, save_display_path=""):
    if result.truncated:
        msg = MSG_NOTIFY_SUCCESS_TRUNCATED.format(
            files=result.total_files,
            dirs=result.total_dirs,
            reason=describe_truncation(result),
        )
    else:
        msg = MSG_NOTIFY_SUCCESS.format(
            files=result.total_files, dirs=result.total_dirs
        )
    if not copied_to_clipboard:
        if save_display_path:
            msg = f"已保存目录树：{save_display_path}"
        else:
            msg = f"已生成目录树但未写入剪贴板：{result.total_files} 个文件，{result.total_dirs} 个文件夹"
        if result.truncated:
            msg = f"{msg}（已截断：{describe_truncation(result)}）"
    msg = _append_config_warning_summary(msg, config_warnings)
    _notify(msg)


def _append_config_warning_summary(msg: str, warnings: list[str]) -> str:
    if not warnings:
        return msg
    if len(warnings) == 1:
        return f"{msg}；配置警告：{warnings[0]}"
    return f"{msg}；配置有 {len(warnings)} 项警告，已使用有效值/默认值"


def _print_config_warnings(warnings: list[str]):
    for warning in warnings:
        _print_err(warning, "WARN")


def _check_config():
    get_effective_config()
    warnings = get_config_warnings()
    if not warnings:
        logger.info("配置检查通过 {}", CONFIG_FILE)
        _print(f"配置检查通过：{CONFIG_FILE}")
        return
    logger.warning("配置检查发现 {} 项警告", len(warnings))
    _print(f"配置检查发现 {len(warnings)} 项警告：")
    for warning in warnings:
        _print(f"- {warning}")


def _manage_install_from_gui():
    if _force_notify_mode:
        # --notify 是右键复制流程注入的隐藏参数：该模式只允许气泡通知，
        # 绝不进入安装管理/开窗流程（回归红线）
        logger.debug("--notify 通知模式：跳过安装管理，不开窗")
        return
    source_exe_path = _get_exe_path()
    state, info = _compute_install_state(source_exe_path)
    logger.debug(
        "安装管理入口 source='{}' state='{}' registered_target='{}'",
        source_exe_path, state, info["installed_exe_path"],
    )

    if (
        state == _INSTALL_STATE_NOT_INSTALLED
        and not get_effective_config().get("installPromptDismissed")
    ):
        # 首次引导只在真正未安装且用户未拒绝过时出现一次；
        # 拒绝后写标记，之后每次双击都安静地直接开窗
        if _confirm_install():
            if _install_from_source(source_exe_path):
                _notify(MSG_INSTALLED)
                state = _INSTALL_STATE_OK
                info = {
                    "installed_exe_path": INSTALL_EXE,
                    "installed_version": VERSION,
                }
            else:
                # state 保持 not-installed：窗口横幅仍提供重试入口
                _notify("安装失败")
        else:
            update_config_values({"installPromptDismissed": True})
    _open_drop_window(state, info)


def _open_drop_window(install_state: str, install_info: dict) -> None:
    """装配安装动作回调并按状态打开拖拽窗口；阻塞至窗口关闭。

    回调契约与 window.DropWindow 一致：无参 callable，返回 bool 表示成功，
    成功(True)后横幅自毁，False 保留供重试；用户在弹窗里取消同样算 False。
    键取两侧契约的并集：window._BANNER_BUTTONS 按状态查
    not-installed/update/repair/migrate/uninstall（downgrade 固定绑 uninstall），
    另附 install 键作别名——窗口不会读取多余键。
    """
    source_exe_path = _get_exe_path()
    installed_exe_path = install_info.get("installed_exe_path", "") if install_info else ""

    def _install_action() -> bool:
        return _install_from_source(source_exe_path)

    def _migrate_action() -> bool:
        # 迁移会改写安装位置，用户点按钮后仍确认一次（用户主动触发的弹窗）；
        # 取消或选择卸载都不动安装，横幅保留可重试
        if _choose_migrate_or_uninstall(installed_exe_path, INSTALL_EXE) != _SETUP_ACTION_INSTALL:
            return False
        return _install_from_source(source_exe_path)

    def _uninstall_action() -> bool:
        # 复用现有卸载流程：失败时内部直接 _exit(3)，可能不返回
        _uninstall_from_gui(installed_exe_path)
        return True

    actions = {
        "install": _install_action,
        "update": _install_action,
        "repair": _install_action,
        "not-installed": _install_action,
        "migrate": _migrate_action,
        "uninstall": _uninstall_action,
    }
    try:
        # 延迟导入：window 依赖 tkinter，缺失/损坏时走下面的兜底而不是崩在 import
        from .window import run_drop_window

        run_drop_window(
            install_state=install_state,
            install_info=install_info,
            install_actions=actions,
        )
    except Exception as e:
        logger.exception("拖拽窗口打开失败 state='{}'", install_state)
        _report_setup_status(f"无法打开窗口：{e}")
        _exit(3)


def _version_key(version: str) -> tuple:
    """版本号转可比较的整数元组；非数字段按 0 处理，长度不齐按位比较。"""
    parts = []
    for segment in version.split("."):
        digits = "".join(ch for ch in segment if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _is_version_newer(left: str, right: str) -> bool:
    """left 版本是否比 right 更新（按点分数字序值比较，非纯字符串相等）。"""
    return _version_key(left) > _version_key(right)


_INSTALL_STATE_NOT_INSTALLED = "not-installed"
_INSTALL_STATE_OK = "ok"
_INSTALL_STATE_UPDATE = "update"
_INSTALL_STATE_DOWNGRADE = "downgrade"
_INSTALL_STATE_REPAIR = "repair"
_INSTALL_STATE_MIGRATE = "migrate"


def _compute_install_state(source_exe_path: str) -> tuple[str, dict]:
    """双击安装决策的六态纯计算：只判定，不弹窗、不写注册表、不改文件。

    返回 (状态, 上下文)；上下文固定含 installed_exe_path / installed_version
    两个键（未安装时为空串，不用 None），供调用方（弹窗流程或拖拽窗口）直接使用。
    """
    installed_exe_path = get_installed_exe_path()
    installed_version = get_installed_version()

    if not installed_exe_path:
        # 菜单键已注册但命令解析不出主程序路径：按残留菜单提供修复，而不是当成未安装
        state = (
            _INSTALL_STATE_REPAIR if is_registered() else _INSTALL_STATE_NOT_INSTALLED
        )
        return state, {"installed_exe_path": "", "installed_version": installed_version}

    registered_stable_copy = _same_path(installed_exe_path, INSTALL_EXE)

    if installed_version and registered_stable_copy:
        # 优先按注册表版本判断，免去每次双击的全量字节比对；
        # 同版本即视为正常，不再做任何字节比对（F5）
        if installed_version == VERSION:
            state = _INSTALL_STATE_OK
        elif not os.path.isfile(INSTALL_EXE):
            state = _INSTALL_STATE_REPAIR
        elif _is_version_newer(installed_version, VERSION):
            # 安装副本比当前文件新：属降级
            state = _INSTALL_STATE_DOWNGRADE
        else:
            state = _INSTALL_STATE_UPDATE
    elif registered_stable_copy:
        # 注册表无版本值（旧版安装后未重装）时退回字节比对
        if os.path.isfile(INSTALL_EXE) and _files_match(source_exe_path, INSTALL_EXE):
            state = _INSTALL_STATE_OK
        elif os.path.isfile(INSTALL_EXE):
            state = _INSTALL_STATE_UPDATE
        else:
            state = _INSTALL_STATE_REPAIR
    elif os.path.isfile(installed_exe_path):
        state = _INSTALL_STATE_MIGRATE
    else:
        state = _INSTALL_STATE_REPAIR

    return state, {
        "installed_exe_path": installed_exe_path,
        "installed_version": installed_version,
    }


def _uninstall_from_gui(installed_exe_path: str):
    if uninstall():
        logger.info("GUI 卸载完成 registered_target='{}'", installed_exe_path or "(未解析)")
        _cleanup_installed_files(installed_exe_path)
        _notify(_uninstall_notice())
    else:
        logger.error("GUI 卸载失败：注册表键仍残留")
        _notify("卸载失败")
        _exit(3)


def _uninstall_notice() -> str:
    """卸载提示。运行中 exe 的自删在标准权限下必然残留（MOVEFILE_DELAY_UNTIL_REBOOT
    需要管理员写 HKLM），此时如实说明，避免"已卸载"与残留状态相矛盾。"""
    if os.path.exists(INSTALL_EXE) or os.path.exists(INSTALL_DIR):
        return MSG_UNINSTALLED_RESIDUE
    return MSG_UNINSTALLED


def _install_from_source(source_exe_path: str) -> bool:
    target_exe_path = _prepare_installed_files(source_exe_path)
    if not target_exe_path:
        logger.error("安装中止：稳定副本准备失败 source='{}'", source_exe_path)
        return False
    result = install(target_exe_path)
    logger.info("注册右键菜单 target='{}' result={}", target_exe_path, "成功" if result else "失败")
    return result


def _prepare_installed_files(source_exe_path: str) -> str:
    """准备稳定安装副本，避免右键菜单依赖下载目录里的 exe。

    先复制到同目录暂存文件、全部就绪后再原子替换；
    任一步失败只清理暂存文件并保留旧稳定副本，
    避免更新半途失败把本来可用的右键菜单变成悬空注册。
    """
    source_exe_path = os.path.abspath(source_exe_path)
    if not getattr(sys, "frozen", False):
        return source_exe_path
    if not source_exe_path or not os.path.isfile(source_exe_path):
        return ""
    staged_gui = ""
    staged_cli = ""
    try:
        os.makedirs(INSTALL_DIR, exist_ok=True)
        staged_gui = _stage_file(source_exe_path, INSTALL_EXE)
        source_cli_path = os.path.join(os.path.dirname(source_exe_path), "copy-tree-cli.exe")
        if os.path.isfile(source_cli_path):
            staged_cli = _stage_file(source_cli_path, INSTALL_CLI_EXE)
        elif os.path.exists(INSTALL_CLI_EXE):
            _delete_or_schedule_file(INSTALL_CLI_EXE)
        logger.debug(
            "暂存完成 gui='{}' cli='{}'",
            staged_gui or "(同文件跳过)", staged_cli or "(无需更新)",
        )
        if staged_gui:
            os.replace(staged_gui, INSTALL_EXE)
            staged_gui = ""
        if staged_cli:
            os.replace(staged_cli, INSTALL_CLI_EXE)
            staged_cli = ""
        return INSTALL_EXE
    except (OSError, shutil.Error):
        # 只清理尚未替换成功的暂存文件；已就位的旧稳定副本保持不动
        logger.error("准备稳定安装副本失败，保留旧副本 gui_staged='{}' cli_staged='{}'",
                     staged_gui or "-", staged_cli or "-")
        _delete_or_schedule_file(staged_gui)
        _delete_or_schedule_file(staged_cli)
        return ""


def _stage_file(source_path: str, target_path: str) -> str:
    """把源文件复制到目标同目录的暂存名，返回暂存路径；同一文件时返回空串表示无需替换。"""
    if _same_path(source_path, target_path):
        return ""
    staged_path = target_path + ".new"
    shutil.copy2(source_path, staged_path)
    return staged_path


def _files_match(left: str, right: str) -> bool:
    try:
        return (
            os.path.isfile(left)
            and os.path.isfile(right)
            and filecmp.cmp(left, right, shallow=False)
        )
    except OSError:
        return False


def _cleanup_installed_files(installed_exe_path: str):
    """卸载时只清理 copy-tree 自己的稳定安装副本，不碰用户原始下载文件。"""
    paths = [INSTALL_CLI_EXE, INSTALL_EXE]
    installed_exe_path = os.path.abspath(installed_exe_path) if installed_exe_path else ""
    if installed_exe_path and _is_inside_install_dir(installed_exe_path):
        paths.insert(0, installed_exe_path)
    seen = set()
    for path in paths:
        normalized = os.path.normcase(os.path.abspath(path))
        if normalized in seen:
            continue
        seen.add(normalized)
        _delete_or_schedule_file(path)
    try:
        os.rmdir(INSTALL_DIR)
    except OSError:
        pass


def _delete_or_schedule_file(path: str) -> bool:
    if not path or not os.path.exists(path):
        return True
    try:
        os.remove(path)
        return True
    except OSError:
        try:
            return bool(kernel32.MoveFileExW(path, None, MOVEFILE_DELAY_UNTIL_REBOOT))
        except OSError:
            return False


def _is_inside_install_dir(path: str) -> bool:
    install_dir = os.path.normcase(os.path.abspath(INSTALL_DIR))
    candidate = os.path.normcase(os.path.abspath(path))
    return candidate == install_dir or candidate.startswith(install_dir + os.sep)


def _same_path(left: str, right: str) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _show_question_box(text: str, buttons: int) -> int:
    """统一的问题对话框入口；返回 IDYES / IDNO / IDCANCEL。"""
    return user32.MessageBoxW(None, text, "copy-tree", buttons | MB_ICONQUESTION)


def _confirm_install() -> bool:
    result = _show_question_box(
        f"是否安装 copy-tree 右键菜单？\n\n程序会安装到：\n{INSTALL_EXE}",
        MB_YESNO,
    )
    return result == IDYES


def _choose_uninstall_or_keep(installed_path: str) -> int:
    result = _show_question_box(
        f"copy-tree 已安装，右键菜单可直接使用。\n\n安装位置：\n{installed_path}\n\n是否卸载 copy-tree？",
        MB_YESNO,
    )
    return _SETUP_ACTION_UNINSTALL if result == IDYES else _SETUP_ACTION_CANCEL


def _choose_update_or_uninstall(source_path: str, target_path: str, installed_version: str = "") -> int:
    version_note = f"（当前 v{installed_version} → 新版 v{VERSION}）" if installed_version else ""
    result = _show_question_box(
        f"检测到新版 copy-tree{version_note}。\n\n请选择操作：\n是：使用当前文件更新安装副本\n否：卸载 copy-tree\n取消：不做更改\n\n当前文件：\n{source_path}\n\n安装位置：\n{target_path}",
        MB_YESNOCANCEL,
    )
    if result == IDYES:
        return _SETUP_ACTION_INSTALL
    if result == IDNO:
        return _SETUP_ACTION_UNINSTALL
    return _SETUP_ACTION_CANCEL


def _choose_downgrade_or_uninstall(source_path: str, target_path: str, installed_version: str) -> int:
    result = _show_question_box(
        f"已安装副本（v{installed_version}）比当前文件（v{VERSION}）更新。\n\n请选择操作：\n是：仍使用当前文件覆盖安装（降级）\n否：卸载 copy-tree\n取消：不做更改\n\n当前文件：\n{source_path}\n\n安装位置：\n{target_path}",
        MB_YESNOCANCEL,
    )
    if result == IDYES:
        return _SETUP_ACTION_INSTALL
    if result == IDNO:
        return _SETUP_ACTION_UNINSTALL
    return _SETUP_ACTION_CANCEL


def _choose_migrate_or_uninstall(old_path: str, new_path: str) -> int:
    result = _show_question_box(
        f"检测到 copy-tree 使用旧安装路径：\n{old_path}\n\n请选择操作：\n是：迁移到稳定安装位置\n否：卸载 copy-tree\n取消：不做更改\n\n新的安装位置：\n{new_path}",
        MB_YESNOCANCEL,
    )
    if result == IDYES:
        return _SETUP_ACTION_INSTALL
    if result == IDNO:
        return _SETUP_ACTION_UNINSTALL
    return _SETUP_ACTION_CANCEL


def _choose_repair_or_uninstall(registered_path: str) -> int:
    result = _show_question_box(
        f"copy-tree 的右键菜单已注册，但目标程序不存在或不可用：\n{registered_path}\n\n请选择操作：\n是：使用当前文件修复安装\n否：卸载残留的右键菜单\n取消：不做更改\n\n新的安装位置：\n{INSTALL_EXE}",
        MB_YESNOCANCEL,
    )
    if result == IDYES:
        return _SETUP_ACTION_INSTALL
    if result == IDNO:
        return _SETUP_ACTION_UNINSTALL
    return _SETUP_ACTION_CANCEL


def _confirm_uninstall() -> bool:
    result = _show_question_box(
        "copy-tree 已安装。是否卸载右键菜单并移除本机安装副本？",
        MB_YESNO,
    )
    return result == IDYES


def _report_setup_status(msg: str):
    logger.info("安装状态反馈: {}", msg)
    if _is_cli_executable():
        _print(msg)
    else:
        _notify(msg)


def _is_gui_executable() -> bool:
    return getattr(sys, "frozen", False) and not _is_cli_executable()


def _is_cli_executable() -> bool:
    if not getattr(sys, "frozen", False):
        return True
    return os.path.basename(sys.executable).lower() == "copy-tree-cli.exe"


def _program_display_name() -> str:
    if getattr(sys, "frozen", False):
        name = os.path.splitext(os.path.basename(sys.executable))[0]
        if name.lower() == "copy-tree-cli":
            return "copy-tree-cli"
        return "copy-tree"
    return "copy-tree-cli" if _is_cli_executable() else "copy-tree"


def _program_usage_name() -> str:
    name = _program_display_name()
    return f"{name}.exe" if getattr(sys, "frozen", False) else name


def _pause_if_double_clicked_cli():
    if not _is_cli_executable():
        return
    if not _stdout_is_console():
        return
    if not (_launched_from_explorer() or _has_private_console()):
        return
    _print("\n按任意键退出...")
    try:
        msvcrt.getwch()
    except OSError:
        pass


def _has_private_console() -> bool:
    if not getattr(sys, "frozen", False):
        return False
    if _launched_from_terminal():
        return False
    count = _console_process_count()
    return 0 < count <= 2


def _launched_from_terminal() -> bool:
    return any(name in _TERMINAL_PROCESS_NAMES for name in _get_parent_process_names()[:3])


def _console_process_count() -> int:
    try:
        process_ids = (ctypes.wintypes.DWORD * 8)()
        return int(kernel32.GetConsoleProcessList(process_ids, len(process_ids)))
    except Exception:
        return 0


def _stdout_is_console() -> bool:
    handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    handle_value = _handle_value(handle)
    return (
        _is_valid_handle_value(handle_value)
        and kernel32.GetFileType(handle) == FILE_TYPE_CHAR
    )


def _get_exe_path() -> str:
    """获取当前 exe 的完整路径。"""
    if getattr(sys, "frozen", False):
        exe_path = sys.executable
        if os.path.basename(exe_path).lower() == "copy-tree-cli.exe":
            gui_exe = os.path.join(os.path.dirname(exe_path), "copy-tree.exe")
            if os.path.isfile(gui_exe):
                return gui_exe
        return exe_path
    return os.path.abspath(sys.argv[0])


def _print(msg: str):
    """安全输出到 stdout。"""
    _write_native(STD_OUTPUT_HANDLE, msg)


def _print_err(msg: str, level: str = "ERROR"):
    """安全输出到 stderr，带严重性级别标识。"""
    _write_native(STD_ERROR_HANDLE, f"[{level}] {msg}")


def _write_native(handle_id: int, msg: str) -> bool:
    """直接写入 Windows 标准句柄；无窗口 exe 场景下为 best-effort。"""
    try:
        text = msg if msg.endswith("\n") else msg + "\n"
        handle = _get_output_handle(handle_id)
        if handle is None:
            return _write_os_fd(handle_id, text)
        file_type = kernel32.GetFileType(handle)
        if file_type == FILE_TYPE_CHAR:
            wide_buffer = ctypes.create_unicode_buffer(text)
            written = ctypes.wintypes.DWORD(0)
            if kernel32.WriteConsoleW(
                handle,
                wide_buffer,
                len(wide_buffer) - 1,
                ctypes.byref(written),
                None,
            ):
                return True
        data = text.encode("utf-8", errors="replace")
        buffer = ctypes.create_string_buffer(data)
        written = ctypes.wintypes.DWORD(0)
        if kernel32.WriteFile(handle, buffer, len(data), ctypes.byref(written), None):
            return True
        return _write_os_fd(handle_id, text)
    except Exception:
        return False


def _get_output_handle(handle_id: int):
    handle = kernel32.GetStdHandle(handle_id)
    handle_value = _handle_value(handle)
    if _is_valid_handle_value(handle_value) and kernel32.GetFileType(handle) != FILE_TYPE_UNKNOWN:
        return handle
    _attach_parent_console()
    handle = kernel32.GetStdHandle(handle_id)
    handle_value = _handle_value(handle)
    if _is_valid_handle_value(handle_value) and kernel32.GetFileType(handle) != FILE_TYPE_UNKNOWN:
        return handle
    return None


def _write_os_fd(handle_id: int, text: str) -> bool:
    fd = 1 if handle_id == STD_OUTPUT_HANDLE else 2
    try:
        os.write(fd, text.encode("utf-8", errors="replace"))
        return True
    except OSError:
        return False


def _notify(msg: str):
    """显示系统通知。"""
    if not show_notification("copy-tree", msg):
        _print_err(f"通知失败：{msg}")


def _exit(code: int = 0):
    """等待通知完成后退出进程。所有退出路径的唯一出口，便于统一审计。"""
    wait_notification()
    if code:
        logger.warning("进程退出 code={}", code)
    else:
        logger.info("进程正常退出")
    sys.exit(code)


if __name__ == "__main__":
    main()
