"""输出格式化：纯文本、Markdown、Markdown 列表、JSON、路径/文件名列表和统计摘要。"""

import datetime
import json
import os

from loguru import logger

from .constants import FOLDER_PREFIX, LOCK_PREFIX, MSG_NO_ACCESS
from .scanner import (
    ScanResult,
    TreeEntry,
    _format_size,
    build_suffix,
    describe_truncation,
    strip_long_prefix,
)

def format_text(tree_text: str) -> str:
    """返回原始树状文本。"""
    return tree_text


def format_markdown(tree_text: str) -> str:
    """将树状文本包裹在 Markdown 代码块中。"""
    return f"```\n{tree_text}\n```"


def format_markdown_list(
    result: ScanResult, show_size: bool = False, show_time: bool = False
) -> str:
    """生成 Markdown 无序列表。"""
    lines: list[str] = []
    _render_markdown_entry(result.root, 0, lines, show_size, show_time)
    if result.truncated:
        lines.append(f"  - ... 输出已截断：{describe_truncation(result)}")
    return "\n".join(lines)


def format_json(
    result: ScanResult, show_size: bool = False, show_time: bool = False
) -> str:
    """生成结构化 JSON，便于脚本和其他工具消费。"""
    payload = {
        "root": _entry_to_dict(result.root, show_size, show_time),
        "stats": {
            "displayedFiles": result.total_files,
            "displayedDirectories": result.total_dirs,
            "scannedFiles": result.total_files_actual,
            "fileCountsAreComplete": not result.truncated,
            "truncated": result.truncated,
            "scanStopped": result.scan_stopped,
        },
    }
    if result.truncated:
        payload["truncation"] = {
            "summary": describe_truncation(result),
            "truncatedLevels": result.truncated_levels,
            "hiddenItemsByLevel": result.truncated_items,
            "maxFilesLimit": result.max_files_limit,
            "depthLimited": result.depth_limited,
        }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def format_paths(result: ScanResult) -> str:
    """每行一个文件绝对路径（剥离长路径前缀），供脚本批量使用。"""
    return "\n".join(
        strip_long_prefix(entry.path) for entry in _iter_files(result.root) if entry.path
    )


def format_names(result: ScanResult) -> str:
    """每行一个文件名。与 paths/JSON 一致输出真实名（entry.name 是 display 截断名）。"""
    return "\n".join(_entry_name(entry) for entry in _iter_files(result.root))


def format_summary(result: ScanResult) -> str:
    """一段统计摘要：文件数、目录数、总大小，截断时附加说明。"""
    text = f"{result.root.name}：{result.total_files} 个文件，{result.total_dirs} 个文件夹"
    total_size = _total_size(result.root)
    if total_size:
        text += f"，总大小 {_format_size(total_size)}"
    if result.truncated:
        text += f"（已截断：{describe_truncation(result)}）"
    return text


def _iter_files(entry: TreeEntry):
    """深度优先遍历出所有文件条目，跳过截断标记与无权限目录。"""
    if entry.is_marker:
        return
    if entry.is_dir:
        if entry.access_denied:
            return
        for child in entry.children:
            yield from _iter_files(child)
    else:
        yield entry


def _total_size(root: TreeEntry) -> int:
    """统计显示范围内文件大小总和（字节）；大小未知（st 读取失败）的文件不计。"""
    total = 0
    stack = list(root.children)
    while stack:
        entry = stack.pop()
        if entry.is_marker or entry.access_denied:
            continue
        if entry.is_dir:
            stack.extend(entry.children)
        elif entry.size:
            total += entry.size
    return total


def _format_text_wrapper(tree_text: str, **kwargs) -> str:
    return format_text(tree_text)


def _format_markdown_wrapper(tree_text: str, **kwargs) -> str:
    return format_markdown(tree_text)


def _format_markdown_list_wrapper(tree_text: str, result=None, show_size=False, show_time=False, **kwargs) -> str:
    if result is None:
        return tree_text
    return format_markdown_list(result, show_size, show_time)


def _format_json_wrapper(tree_text: str, result=None, show_size=False, show_time=False, **kwargs) -> str:
    if result is None:
        return tree_text
    return format_json(result, show_size, show_time)


def _format_paths_wrapper(tree_text: str, result=None, **kwargs) -> str:
    if result is None:
        return tree_text
    return format_paths(result)


def _format_names_wrapper(tree_text: str, result=None, **kwargs) -> str:
    if result is None:
        return tree_text
    return format_names(result)


def _format_summary_wrapper(tree_text: str, result=None, **kwargs) -> str:
    if result is None:
        return tree_text
    return format_summary(result)


_FORMATTERS = {
    "text": _format_text_wrapper,
    "markdown": _format_markdown_wrapper,
    "markdown-list": _format_markdown_list_wrapper,
    "json": _format_json_wrapper,
    "paths": _format_paths_wrapper,
    "names": _format_names_wrapper,
    "summary": _format_summary_wrapper,
}

# 合法格式由分发表派生，避免多处手工同步
VALID_FORMATS = tuple(_FORMATTERS)


def format_output(
    tree_text: str,
    fmt: str,
    result: ScanResult | None = None,
    show_size: bool = False,
    show_time: bool = False,
) -> str:
    """根据格式类型分发格式化。未知格式回退 text 并告警。"""
    formatter = _FORMATTERS.get(fmt)
    if formatter is None:
        logger.warning("未知输出格式 '{}'，回退为 text", fmt)
        formatter = _format_text_wrapper
    return formatter(tree_text, result=result, show_size=show_size, show_time=show_time)


def _render_markdown_entry(
    entry: TreeEntry,
    indent: int,
    lines: list[str],
    show_size: bool,
    show_time: bool,
):
    prefix = "  " * indent + "- "
    if entry.is_marker:
        lines.append(prefix + entry.name)
        return

    suffix = build_suffix(entry, show_size, show_time)
    if entry.is_dir:
        if entry.access_denied:
            lines.append(f"{prefix}{LOCK_PREFIX}{entry.name}/ ({MSG_NO_ACCESS})")
            return
        lines.append(f"{prefix}{FOLDER_PREFIX}{entry.name}/{suffix}")
        for child in entry.children:
            _render_markdown_entry(child, indent + 1, lines, show_size, show_time)
        return

    lines.append(f"{prefix}{entry.name}{suffix}")


def _entry_to_dict(entry: TreeEntry, show_size: bool, show_time: bool) -> dict:
    if entry.is_marker:
        return {"type": "truncation", "message": entry.name}

    item = {
        "name": _entry_name(entry),
        "type": "directory" if entry.is_dir else "file",
    }
    if entry.path:
        item["path"] = strip_long_prefix(entry.path)
    if entry.access_denied:
        item["accessDenied"] = True
    if show_time and entry.mtime is not None:
        item["modifiedTime"] = _format_json_time(entry.mtime)
    if not entry.is_dir and show_size and entry.size is not None:
        item["sizeBytes"] = entry.size
    if entry.is_dir:
        item["children"] = [
            _entry_to_dict(child, show_size, show_time) for child in entry.children
        ]
    return item


def _entry_name(entry: TreeEntry) -> str:
    if not entry.path:
        return entry.name
    name = os.path.basename(entry.path.rstrip("/\\"))
    return name or entry.name


def _format_json_time(timestamp: float) -> str:
    return datetime.datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")
