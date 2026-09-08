import ctypes
import ctypes.wintypes
import os
import queue
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from copytree import theme  # noqa: E402
from copytree import window as window_module  # noqa: E402
from copytree.window import DropWindow  # noqa: E402


def _noop(*_args, **_kwargs):
    pass


def make_bare_window(**attrs):
    """构造未经 __init__ 的 DropWindow 实例（不创建真实 Tk），按需注入属性。

    新版布局的全部控件都以 mock 预置，既有测试与新增测试按需覆盖即可。
    """
    app = DropWindow.__new__(DropWindow)
    app.actions = queue.Queue()
    app.root = mock.MagicMock()
    app.status_var = mock.MagicMock()
    app.log_text = mock.MagicMock()
    app.copy_button = mock.MagicMock()
    # 布局 B：列表区是 ttk.Treeview（3 列：文件夹 / 文件数 / ✕）
    app.folder_tree = mock.MagicMock()
    app.folder_tree.get_children.return_value = tuple()
    app.folder_tree.item.return_value = tuple()
    # 结果卡（状态机）
    app.result_card = mock.MagicMock()
    app.result_status = mock.MagicMock()
    app.result_link = mock.MagicMock()
    app.save_button = mock.MagicMock()
    # 预览 / 抽屉 / 清空按钮
    app.preview_text = mock.MagicMock()
    app.latest_log_var = mock.MagicMock()
    app.clear_button = mock.MagicMock()
    # 设置菜单开关变量
    app.tray_var = mock.MagicMock()
    app.auto_copy_var = mock.MagicMock()
    app.auto_copy_var.get.return_value = False
    # 交互状态
    app._worker = None
    app._tray_started = False
    app._save_kind = "txt"
    app._copy_flash_active = False
    app._uninstall_armed = False
    app._uninstall_after_id = None
    app._uninstall_menu_index = 4
    app._settings_menu = None
    app._clear_armed = False
    app._clear_after_id = None
    app._log_expanded = False
    app._log_frame = mock.MagicMock()
    app.log_toggle_button = mock.MagicMock()
    app.drawer_frame = mock.MagicMock()
    # 扫描状态层（根因修复契约）
    app._scan_cache = {}
    app._scan_opts = None
    app._pending_scan_opts = None
    app._pending_auto_copy = False
    app.scan_button = mock.MagicMock()
    # 扫描相关选项变量（_scan_opt_snapshot/_copy_from_cache/_save_from_cache 读取；
    # get 返回值钉死为默认态，保证 seed 后 _cache_valid 的快照一致性可复现）
    for _name in ("hide_git_var", "gitignore_var", "source_only_var",
                  "size_var", "time_var"):
        _var = mock.MagicMock()
        _var.get.return_value = False
        setattr(app, _name, _var)
    app.format_var = mock.MagicMock()
    app.format_var.get.return_value = "树状文本"
    for name, value in attrs.items():
        setattr(app, name, value)
    return app


def _scan_opts(**overrides):
    opts = {
        "format": "text", "hide_git": False, "gitignore": False,
        "source_only": False, "size": False, "time": False,
    }
    opts.update(overrides)
    return opts


def _fake_scan_result(total_files=3, total_dirs=1, truncated=False):
    return mock.Mock(total_files=total_files, total_dirs=total_dirs, truncated=truncated)


class WorkerExcludePatternsTests(unittest.TestCase):
    """拖拽窗口过滤模式必须应用 excludePatterns（评审 I-6），与右键 CLI 行为一致。"""

    def _run_worker(self, app, opts):
        config = {
            "excludeDirs": [".git"], "excludeFiles": [],
            "excludePatterns": ["*.log", "dist/*"],
            "maxFiles": 2000, "maxItemsPerLevel": 200, "maxDepth": -1,
        }
        fake_result = _fake_scan_result(total_files=0, total_dirs=0)
        with mock.patch.object(window_module, "get_effective_config", return_value=config), \
             mock.patch.object(window_module, "scan_directory", return_value=fake_result) as scan_mock, \
             mock.patch.object(window_module, "build_tree_text", return_value=""), \
             mock.patch.object(window_module, "format_output", return_value=""):
            # 改写理由（旧→新）：worker 只扫描，do_copy/do_save 动作参数已随
            # 扫描状态层移除（复制/保存改由主线程消费缓存）
            app._worker_main(["C:\\fake"], opts)
        return scan_mock.call_args.kwargs

    def test_filter_mode_passes_exclude_patterns_from_config(self):
        app = DropWindow.__new__(DropWindow)
        app.actions = queue.Queue()
        kwargs = self._run_worker(app, _scan_opts(hide_git=True))
        self.assertEqual(kwargs["exclude_patterns"], {"*.log", "dist/*"})

    def test_non_filter_mode_keeps_exclude_patterns_off(self):
        # 改写理由（旧→新）：worker 重写后以空集合（falsy）表达关闭；
        # scanner 侧 `exclude_patterns or set()` 与 None 行为等价，
        # 仍保证非过滤模式不做 glob 排除（评审 I-6 的行为契约不变）
        app = DropWindow.__new__(DropWindow)
        app.actions = queue.Queue()
        kwargs = self._run_worker(app, _scan_opts(hide_git=False))
        self.assertFalse(kwargs["exclude_patterns"])


class PollLoopTests(unittest.TestCase):
    """_poll 队列状态机特征测试（评审 I-7）：worker/托盘动作经队列回传的消费语义。"""

    def test_log_message_appends_log_only(self):
        app = make_bare_window()
        with mock.patch.object(app, "_append_log") as append_log:
            app.actions.put(("log", "进度一行"))
            app._poll()
        append_log.assert_called_once_with("进度一行")
        app.root.after.assert_called_once()

    def test_done_message_restores_button_and_sets_status(self):
        # 兼容路径：纯字符串 payload 只恢复按钮与状态行，不触碰结果卡
        app = make_bare_window()
        app.actions.put(("done", "处理完成。"))
        app._poll()
        app.copy_button.config.assert_called_with(state="normal")
        app.status_var.set.assert_called_with("处理完成。")

    def test_count_message_fills_treeview_file_column(self):
        app = make_bare_window()
        app.folder_tree.get_children.return_value = ("i1",)
        app.folder_tree.item.return_value = (r"D:\x", "—", "✕")
        app.actions.put(("count", (r"D:\x", 1234)))
        app._poll()
        app.folder_tree.set.assert_called_once_with("i1", "files", "1,234")

    def test_preview_message_fills_preview_text(self):
        app = make_bare_window()
        app.actions.put(("preview", "a\nb"))
        app._poll()
        app.preview_text.insert.assert_called_once_with("1.0", "a\nb")
        app.preview_text.config.assert_called_with(state="disabled")

    def test_tray_open_reiconsifies_window(self):
        app = make_bare_window()
        app.actions.put(("tray", "open"))
        app._poll()
        app.root.deiconify.assert_called_once()
        app.root.lift.assert_called_once()

    def test_tray_config_opens_config_file(self):
        app = make_bare_window()
        app.actions.put(("tray", "config"))
        with mock.patch.object(window_module, "open_config_file") as open_config:
            app._poll()
        open_config.assert_called_once()

    def test_tray_exit_quits_and_stops_after_chain(self):
        app = make_bare_window()
        app.actions.put(("tray", "exit"))
        with mock.patch.object(window_module, "stop_tray") as stop_tray:
            app._poll()
        stop_tray.assert_called_once()
        app.root.destroy.assert_called_once()
        # 退出后不再续订轮询，避免对已销毁窗口触发 after 回调
        app.root.after.assert_not_called()

    def test_empty_queue_keeps_polling_without_crash(self):
        app = make_bare_window()
        app._poll()
        app.root.after.assert_called_once_with(100, app._poll)

    def test_dead_worker_with_disabled_button_is_recovered(self):
        dead = threading.Thread(target=_noop)
        dead.start()
        dead.join()
        app = make_bare_window(_worker=dead)
        app.copy_button.__getitem__ = mock.Mock(return_value="disabled")
        app.actions.put(("log", "x"))
        app._poll()
        app.copy_button.config.assert_called_with(state="normal")


class ResultCardStateTests(unittest.TestCase):
    """结果卡状态机：就绪态（默认/列表增删后）⇄ 操作结果态（copy/save 完成后）。"""

    def _done_payload(self, truncated=False, copy_ok=True):
        return {
            "action": "copy", "copy_ok": copy_ok, "truncated": truncated,
            "files": 2000 if truncated else 128,
            "message": "已复制 128 个文件，结果已写入剪贴板。",
            "warn_message": "已复制 2,000 个文件，超出上限，结果可能不完整",
        }

    def test_ready_state_lists_present_shows_total(self):
        # 改写理由（旧→新）：新契约下「共 N 个文件待复制」仅在缓存有效时出现——
        # 先 seed 缓存与选项快照再刷新结果卡（原测试未扫描即期待待复制态）
        app = make_bare_window()
        app.folder_tree.get_children.return_value = ("i1", "i2")
        app.folder_tree.item.return_value = (r"D:\a", "128", "✕")
        app._scan_cache[r"D:\a"] = {
            "tree_text": "", "result": _fake_scan_result(),
            "files": 128, "truncated": False}
        app._scan_opts = app._scan_opt_snapshot()
        app._refresh_ready_card()
        app.result_card.configure.assert_called_with(style="ResultCard.TFrame")
        app.result_link.pack_forget.assert_called_once()
        text = app.status_var.set.call_args[0][0]
        # 两行各 128 → 就绪态汇总所有行的文件数
        self.assertIn("256", text)
        self.assertIn("待复制", text)

    def test_ready_state_empty_list_shows_guidance(self):
        # 改写理由（旧→新）：新契约合并「空列表」与「未扫描」态——初始统一提示
        # 「请先执行扫描」（ScanStateTests.test_initial_buttons_disabled_with_scan_hint
        # 契约），旧「就绪。拖入…」文案不再出现
        app = make_bare_window()  # get_children -> ()
        app._refresh_ready_card()
        text = app.status_var.set.call_args[0][0]
        self.assertIn("请先执行扫描", text)
        app.result_card.configure.assert_called_with(style="ResultCard.TFrame")
        app.copy_button.config.assert_called_with(state="disabled")
        app.save_button.config.assert_called_with(state="disabled")

    def test_clean_done_keeps_card_normal(self):
        app = make_bare_window()
        app.actions.put(("done", self._done_payload(truncated=False)))
        with mock.patch.object(app, "_append_log"):
            app._poll()
        app.result_card.configure.assert_called_with(style="ResultCard.TFrame")
        app.result_link.pack_forget.assert_called_once()
        self.assertIn("已复制", app.status_var.set.call_args[0][0])

    def test_truncated_done_switches_card_to_warn(self):
        app = make_bare_window()
        app.actions.put(("done", self._done_payload(truncated=True)))
        with mock.patch.object(app, "_append_log"):
            app._poll()
        app.result_card.configure.assert_called_with(style="ResultCardWarn.TFrame")
        app.result_status.configure.assert_called_with(style="ResultCardWarn.TLabel")
        app.result_link.pack.assert_called_once()
        self.assertIn("超出上限", app.status_var.set.call_args[0][0])
        self.assertIn("可能不完整", app.status_var.set.call_args[0][0])

    def test_adjust_limit_link_opens_config_file(self):
        app = make_bare_window()
        with mock.patch.object(window_module, "open_config_file") as open_config:
            app._open_limit_config()
        open_config.assert_called_once_with()

    def test_list_removal_returns_card_to_ready(self):
        # 改写理由（旧→新）：新契约下删除行 → 缓存失效，结果卡回到
        # 「请重新扫描」门卫态并禁用复制（旧断言「待复制」仅在缓存有效时出现）
        app = make_bare_window()
        paths = {"i0": r"D:\a", "i1": r"D:\b"}

        def item(iid, column=None):
            if column == "values":
                return (paths[iid], "—", "✕")
            return tuple()

        app.folder_tree.item.side_effect = item
        app.folder_tree.get_children.return_value = ("i0", "i1")
        app._scan_cache = {
            p: {"tree_text": p, "result": _fake_scan_result(),
                "files": 1, "truncated": False}
            for p in (r"D:\a", r"D:\b")
        }
        app._scan_opts = app._scan_opt_snapshot()
        self.assertTrue(app._cache_valid())  # 前置：删除前缓存有效
        app.folder_tree.get_children.return_value = ("i0",)  # 删除 i1 后仅剩 D:\a
        app._remove_row("i1")
        app.folder_tree.delete.assert_called_once_with("i1")
        text = app.status_var.set.call_args[0][0]
        self.assertIn("重新扫描", text)
        app.result_link.pack_forget.assert_called_once()
        app.copy_button.config.assert_called_with(state="disabled")


class CopyFeedbackTests(unittest.TestCase):
    """复制按钮反馈：成功后「已复制 ✓」2 秒还原，反馈期间防重入。"""

    def test_copy_ok_done_flashes_button_for_2s(self):
        app = make_bare_window()
        app.actions.put(("done", {"action": "copy", "copy_ok": True, "truncated": False,
                                  "files": 1, "message": "已复制 1 个文件，结果已写入剪贴板。",
                                  "warn_message": ""}))
        with mock.patch.object(app, "_append_log"):
            app._poll()
        app.copy_button.config.assert_any_call(text="已复制 ✓", style="Success.TButton")
        app.root.after.assert_any_call(2000, app._reset_copy_button)
        self.assertTrue(app._copy_flash_active)

    def test_reentry_blocked_while_flash_active(self):
        # 改写理由（旧→新）：`_run_action` 已随扫描状态层移除，复制入口改为
        # `_on_copy` 直接触发 `_copy_from_cache`；闪灯防重入语义不变
        app = make_bare_window()
        app._copy_flash_active = True
        with mock.patch.object(app, "_copy_from_cache") as copy_cache:
            app._on_copy()
        copy_cache.assert_not_called()

    def test_copy_allowed_when_flash_inactive(self):
        # 改写理由（旧→新）：断言对象从 _run_action(copy=True, save=False)
        # 改为 _copy_from_cache()——复制不再带动作参数进 worker，而是主线程消费缓存
        app = make_bare_window()
        with mock.patch.object(app, "_copy_from_cache") as copy_cache, \
             mock.patch.object(app, "_cache_valid", return_value=True):
            app._on_copy()
        copy_cache.assert_called_once_with()

    def test_reset_restores_button_text_and_style(self):
        app = make_bare_window()
        app._copy_flash_active = True
        app._reset_copy_button()
        app.copy_button.config.assert_called_once_with(text="复制到剪贴板", style="Primary.TButton")
        self.assertFalse(app._copy_flash_active)


class SaveMenuTests(unittest.TestCase):
    """「保存为 ▾」菜单：三项分别触发 _on_save('txt'/'md'/'json')。"""

    def _build(self):
        app = DropWindow.__new__(DropWindow)
        parent = mock.MagicMock()
        with mock.patch.object(window_module, "ttk") as ttk_mock, \
             mock.patch.object(window_module, "tk") as tk_mock:
            app._build_save_menu(parent)
        menu = tk_mock.Menu.return_value
        button = ttk_mock.Menubutton.return_value
        return app, menu, button

    def test_menu_has_three_kind_entries(self):
        _app, menu, button = self._build()
        labels = [c.kwargs["label"] for c in menu.add_command.call_args_list]
        self.assertEqual(len(labels), 3)
        self.assertTrue(any("txt" in label for label in labels))
        self.assertTrue(any("Markdown" in label or "md" in label for label in labels))
        self.assertTrue(any("JSON" in label for label in labels))
        button.configure.assert_called_once_with(menu=menu)

    def test_entries_trigger_on_save_with_matching_kind(self):
        app, menu, _button = self._build()
        commands = [c.kwargs["command"] for c in menu.add_command.call_args_list]
        with mock.patch.object(app, "_on_save") as on_save:
            for command, kind in zip(commands, ("txt", "md", "json")):
                command()
                on_save.assert_called_with(kind)

    def test_on_save_sets_kind_and_runs_save_action(self):
        # 改写理由（旧→新）：保存不再经 `_run_action` 进 worker——新契约是
        # 缓存有效时 `_on_save` 记录 kind 并由主线程 `_save_from_cache(kind)` 消费缓存
        app = make_bare_window()
        app.folder_tree.get_children.return_value = ("i1",)
        app.folder_tree.item.return_value = (r"C:\x", "1", "✕")
        app._scan_cache[r"C:\x"] = {
            "tree_text": "T", "result": _fake_scan_result(),
            "files": 1, "truncated": False}
        app._scan_opts = app._scan_opt_snapshot()
        with mock.patch.object(app, "_save_from_cache") as save:
            app._on_save("json")
        self.assertEqual(app._save_kind, "json")
        save.assert_called_once_with("json")

    def test_on_save_rejects_unknown_kind(self):
        # 改写理由（旧→新）：拒绝语义不变，断言对象改为 `_save_from_cache` 不被调用
        app = make_bare_window()
        with mock.patch.object(app, "_save_from_cache") as save:
            app._on_save("xml")
        save.assert_not_called()
        self.assertEqual(app._save_kind, "txt")  # 未知 kind 不改变当前保存格式


class SaveKindWorkerTests(unittest.TestCase):
    """保存格式（缓存消费等价测试）。

    改写理由（旧→新）：扫描状态层把 worker 保存流程整体移入主线程
    `_save_from_cache(kind)`——worker 只扫描。原「worker 收 save_kind 写文件 /
    发 done 消息」的断言改为缓存消费等价断言：txt 沿用 tree_text 写 _TXT
    （兼容不变）；md/json 现场按对应格式 format_output 后写 _MD/_JSON；
    完成消息改为 `_show_result_card` 收到的 payload；count/preview 改为
    worker 发布 count + scanned + scan_done 的扫描流契约。
    """

    def _run_save(self, save_kind, truncated=False):
        app = make_bare_window()
        app.folder_tree.get_children.return_value = ("i1",)
        app.folder_tree.item.return_value = (r"C:\fake", "1", "✕")
        app._scan_cache[r"C:\fake"] = {
            "tree_text": "L1\nL2\nL3",
            "result": _fake_scan_result(total_files=3, truncated=truncated),
            "files": 3,
            "truncated": truncated,
        }
        app._scan_opts = app._scan_opt_snapshot()
        with mock.patch.object(window_module, "format_output", return_value="OUT") as fmt_mock, \
             mock.patch("builtins.open", mock.mock_open()) as open_mock, \
             mock.patch.object(app, "_show_result_card") as card:
            app._save_from_cache(save_kind)
        return open_mock, fmt_mock, card

    def test_txt_save_writes_tree_text_to_txt_filename(self):
        open_mock, fmt_mock, _card = self._run_save("txt")
        expected = os.path.join("C:\\fake", "directory_tree.txt")
        open_mock.assert_called_once_with(expected, "w", encoding="utf-8")
        open_mock().write.assert_called_once_with("L1\nL2\nL3")
        fmt_mock.assert_not_called()  # txt 沿用 tree_text，不做二次格式化（兼容不变）

    def test_md_save_writes_markdown_formatted_content(self):
        open_mock, fmt_mock, _card = self._run_save("md")
        expected = os.path.join("C:\\fake", "directory_tree.md")
        open_mock.assert_called_once_with(expected, "w", encoding="utf-8")
        self.assertEqual(fmt_mock.call_args.args[1], "markdown")
        open_mock().write.assert_called_once_with("OUT")

    def test_json_save_writes_json_formatted_content(self):
        open_mock, fmt_mock, _card = self._run_save("json")
        expected = os.path.join("C:\\fake", "directory_tree.json")
        open_mock.assert_called_once_with(expected, "w", encoding="utf-8")
        self.assertEqual(fmt_mock.call_args.args[1], "json")
        open_mock().write.assert_called_once_with("OUT")

    def test_worker_publishes_count_and_scanned_messages(self):
        app = DropWindow.__new__(DropWindow)
        app.actions = queue.Queue()
        fake_result = _fake_scan_result(total_files=3)
        config = {"maxFiles": 2000, "maxItemsPerLevel": 200, "maxDepth": -1}
        with mock.patch.object(window_module, "get_effective_config", return_value=config), \
             mock.patch.object(window_module, "scan_directory", return_value=fake_result), \
             mock.patch.object(window_module, "build_tree_text", return_value="L1\nL2\nL3"):
            app._worker_main(["C:\\fake"], _scan_opts())
        messages = []
        while not app.actions.empty():
            messages.append(app.actions.get_nowait())
        kinds = [kind for kind, _payload in messages]
        self.assertIn("count", kinds)
        self.assertIn("scanned", kinds)
        self.assertIn("scan_done", kinds)
        count_payload = dict(p for k, p in messages if k == "count")
        self.assertEqual(count_payload["C:\\fake"], 3)
        path, data = [p for k, p in messages if k == "scanned"][0]
        self.assertEqual(path, "C:\\fake")
        self.assertEqual(data["tree_text"], "L1\nL2\nL3")
        self.assertEqual(data["files"], 3)

    def test_save_payload_carries_result_state(self):
        _open, _fmt, card = self._run_save("txt")
        payload = card.call_args.args[0]
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["action"], "save")
        self.assertFalse(payload["copy_ok"])
        self.assertFalse(payload["truncated"])
        self.assertEqual(payload["files"], 3)
        self.assertIn("已保存", payload["message"])

    def test_truncated_save_marks_payload(self):
        _open, _fmt, card = self._run_save("txt", truncated=True)
        payload = card.call_args.args[0]
        self.assertTrue(payload["truncated"])
        self.assertTrue(payload["warn_message"])


class PreviewTests(unittest.TestCase):
    """预览区：每次扫描完成填充 tree_text 前 15 行，超出时带省略提示。"""

    def test_preview_filled_with_first_15_lines_and_marker(self):
        app = make_bare_window()
        lines = [f"line{i}" for i in range(20)]
        app._update_preview("\n".join(lines))
        first = app.preview_text.insert.call_args_list[0]
        self.assertEqual(first.args, ("1.0", "\n".join(lines[:15])))
        marker = app.preview_text.insert.call_args_list[1]
        self.assertEqual(marker.args[1], "\n… 仅预览前 15 行")
        app.preview_text.config.assert_called_with(state="disabled")

    def test_preview_short_text_inserted_without_marker(self):
        app = make_bare_window()
        app._update_preview("a\nb\nc")
        app.preview_text.insert.assert_called_once_with("1.0", "a\nb\nc")

    def test_preview_empty_text_shows_placeholder(self):
        app = make_bare_window()
        app._update_preview("")
        content = app.preview_text.insert.call_args.args[1]
        self.assertIn("没有可显示", content)


class SettingsMenuTests(unittest.TestCase):
    """设置菜单：托盘驻留、拖入自动复制两个开关 + 打开配置 + 两步确认卸载。"""

    def _build(self, frozen=True):
        app = DropWindow.__new__(DropWindow)
        app.tray_var = mock.MagicMock()
        app.auto_copy_var = mock.MagicMock()
        app._uninstall_armed = False
        app._uninstall_after_id = None
        app._settings_menu = None
        app._uninstall_menu_index = None
        parent = mock.MagicMock()
        with mock.patch.object(window_module.sys, "frozen", frozen, create=True), \
             mock.patch.object(window_module, "ttk") as ttk_mock, \
             mock.patch.object(window_module, "tk") as tk_mock:
            app._build_settings_menu(parent)
        return app, ttk_mock, tk_mock

    def test_menu_contains_four_entries_when_frozen(self):
        _app, ttk_mock, tk_mock = self._build(frozen=True)
        menu = tk_mock.Menu.return_value
        ttk_mock.Menubutton.assert_called_once()
        self.assertIn("设置", ttk_mock.Menubutton.call_args.kwargs["text"])
        # 菜单挂到设置 Menubutton 上，tearoff=0
        tk_mock.Menu.assert_called_once_with(ttk_mock.Menubutton.return_value, tearoff=0)
        check_labels = [c.kwargs["label"] for c in menu.add_checkbutton.call_args_list]
        self.assertEqual(check_labels, ["关闭时驻留托盘", "拖入后自动复制"])
        menu.add_separator.assert_called_once()
        cmd_labels = [c.kwargs["label"] for c in menu.add_command.call_args_list]
        self.assertEqual(cmd_labels, ["打开配置文件", "卸载 copy-tree…"])

    def test_checkbuttons_bound_to_tray_and_auto_copy_vars(self):
        app, _ttk, tk_mock = self._build(frozen=True)
        menu = tk_mock.Menu.return_value
        first, second = menu.add_checkbutton.call_args_list
        self.assertEqual(first.kwargs["variable"], app.tray_var)
        self.assertEqual(first.kwargs["command"], app._on_tray_toggle)
        self.assertEqual(second.kwargs["variable"], app.auto_copy_var)
        self.assertNotIn("command", second.kwargs)

    def test_uninstall_entry_absent_when_not_frozen(self):
        _app, _ttk, tk_mock = self._build(frozen=False)
        menu = tk_mock.Menu.return_value
        cmd_labels = [c.kwargs["label"] for c in menu.add_command.call_args_list]
        self.assertEqual(cmd_labels, ["打开配置文件"])


class UninstallConfirmTests(unittest.TestCase):
    """卸载两步确认：首点武装（3 秒回弹），二点进入既有卸载确认流程。"""

    def test_first_click_arms_and_schedules_revert(self):
        app = make_bare_window()
        app._settings_menu = mock.MagicMock()
        with mock.patch.object(app, "_on_uninstall") as uninstall:
            app._on_uninstall_menu_selected()
        app._settings_menu.entryconfig.assert_called_once_with(
            app._uninstall_menu_index, label="⚠ 再点一次确认卸载")
        app.root.after.assert_called_once_with(3000, app._disarm_uninstall)
        uninstall.assert_not_called()

    def test_second_click_invokes_existing_uninstall_flow(self):
        app = make_bare_window()
        app._settings_menu = mock.MagicMock()
        app._uninstall_armed = True
        app._uninstall_after_id = 55
        with mock.patch.object(app, "_on_uninstall") as uninstall:
            app._on_uninstall_menu_selected()
        app.root.after_cancel.assert_called_once_with(55)
        app._settings_menu.entryconfig.assert_called_with(
            app._uninstall_menu_index, label="卸载 copy-tree…")
        uninstall.assert_called_once_with()

    def test_timeout_disarms_and_next_click_rearms(self):
        app = make_bare_window()
        app._settings_menu = mock.MagicMock()
        with mock.patch.object(app, "_on_uninstall") as uninstall:
            app._on_uninstall_menu_selected()
            app._disarm_uninstall()
            app._settings_menu.entryconfig.assert_called_with(
                app._uninstall_menu_index, label="卸载 copy-tree…")
            self.assertFalse(app._uninstall_armed)
            app._on_uninstall_menu_selected()
            labels = [c.kwargs["label"] for c in app._settings_menu.entryconfig.call_args_list]
            self.assertEqual(labels[-1], "⚠ 再点一次确认卸载")
            uninstall.assert_not_called()


class ClearConfirmTests(unittest.TestCase):
    """「清空全部」两步确认（设计稿交互态）：首点武装 2.5 秒，二点清空。"""

    def test_first_click_arms_without_clearing(self):
        app = make_bare_window()
        app._on_clear_clicked()
        app.clear_button.config.assert_called_with(text="确认清空？", style="DangerConfirm.TButton")
        app.folder_tree.delete.assert_not_called()
        app.root.after.assert_called_once_with(2500, app._disarm_clear)

    def test_second_click_clears_rows_and_returns_ready(self):
        # 改写理由（旧→新）：新契约下「待复制」须以有效缓存为前提；清空列表后
        # 缓存必然失效，结果卡回到「请先执行扫描」门卫态（旧断言不再成立）
        app = make_bare_window()
        app.folder_tree.get_children.return_value = ("i1", "i2")
        app._clear_armed = True
        app._clear_after_id = 77
        app._on_clear_clicked()
        self.assertEqual(app.folder_tree.delete.call_count, 2)
        app.root.after_cancel.assert_called_once_with(77)
        app.clear_button.config.assert_called_with(text="清空全部", style="DangerGhost.TButton")
        app.result_link.pack_forget.assert_called_once()
        self.assertIn("请先执行扫描", app.status_var.set.call_args[0][0])
        app.copy_button.config.assert_called_with(state="disabled")

    def test_timeout_disarms_clear(self):
        app = make_bare_window()
        app._on_clear_clicked()
        app._disarm_clear()
        self.assertFalse(app._clear_armed)
        app.clear_button.config.assert_called_with(text="清空全部", style="DangerGhost.TButton")
        app.folder_tree.delete.assert_not_called()


class AutoCopyOnDropTests(unittest.TestCase):
    """「拖入后自动复制」。

    改写理由（旧→新）：扫描状态层落地后 drop 不再直接 `_on_copy`——
    `_handle_drop` 改经 `_start_scan_after_drop`：缓存有效 → 直接
    `_copy_from_cache`（原「开启时拖入触发一次复制」的等价实现）；
    缓存无效 → `_pending_auto_copy=True` + `_start_scan`，scan_done 续跑。
    「auto 关闭永不触发」「纯文件拖入不触发」的原意图原样保留。
    """

    def _drop(self, paths, auto_copy, cache_valid_seed=False):
        app = make_bare_window()
        app.auto_copy_var.get.return_value = auto_copy
        if cache_valid_seed:
            app.folder_tree.get_children.return_value = ("i1",)
            app.folder_tree.item.return_value = (r"D:\data", "1", "✕")
            app._scan_cache[r"D:\data"] = {
                "tree_text": "T", "result": _fake_scan_result(),
                "files": 1, "truncated": False}
            app._scan_opts = app._scan_opt_snapshot()
        with mock.patch.object(window_module.shell32, "DragQueryFileW",
                               side_effect=HandleDropTests._drop_paths_static(paths)), \
             mock.patch.object(window_module.shell32, "DragAcceptFiles"), \
             mock.patch.object(window_module.os.path, "isdir",
                               side_effect=lambda p: not p.endswith(".txt")), \
             mock.patch.object(app, "_copy_from_cache") as copy_cache, \
             mock.patch.object(app, "_start_scan") as start_scan:
            app._handle_drop(0xABC)
        return app, copy_cache, start_scan

    def test_enabled_cache_valid_copies_from_cache_after_drop(self):
        _app, copy_cache, start_scan = self._drop([r"D:\data"], auto_copy=True,
                                                  cache_valid_seed=True)
        copy_cache.assert_called_once_with()
        start_scan.assert_not_called()

    def test_enabled_cache_invalid_defers_copy_to_scan(self):
        app, copy_cache, start_scan = self._drop([r"D:\data"], auto_copy=True)
        copy_cache.assert_not_called()
        start_scan.assert_called_once_with()
        self.assertTrue(app._pending_auto_copy)

    def test_disabled_copy_never_triggers(self):
        app, copy_cache, start_scan = self._drop([r"D:\data"], auto_copy=False)
        copy_cache.assert_not_called()
        start_scan.assert_not_called()
        self.assertFalse(app._pending_auto_copy)

    def test_non_folder_drop_never_triggers(self):
        _app, copy_cache, start_scan = self._drop([r"D:\notes.txt"], auto_copy=True)
        copy_cache.assert_not_called()
        start_scan.assert_not_called()


class HandleDropTests(unittest.TestCase):
    """WM_DROPFILES 两段式 DragQueryFileW 协议特征测试（评审 I-7）。

    列表控件已由 Listbox 换为 ttk.Treeview（布局 B）：断言从 folder_list.insert
    改为 folder_tree.insert 的等价行插入（values 首列为路径）。
    """

    @staticmethod
    def _drop_paths_static(paths):
        def fake_query(_hdrop, index, buf, _size):
            if index == 0xFFFFFFFF:  # 计数查询
                return len(paths)
            if buf is None:  # 长度查询（不含终止符）
                return len(paths[index])
            buf.value = paths[index]  # 内容查询
            return len(paths[index])
        return fake_query

    def _drop_paths(self, paths):
        return self._drop_paths_static(paths)

    def _run_drop(self, paths):
        app = make_bare_window()

        def fake_isdir(path):
            return not path.endswith(".txt")

        with mock.patch.object(window_module.shell32, "DragQueryFileW",
                               side_effect=self._drop_paths(paths)), \
             mock.patch.object(window_module.shell32, "DragAcceptFiles"), \
             mock.patch.object(window_module.os.path, "isdir", side_effect=fake_isdir):
            app._handle_drop(0xABC)
        return app

    def test_empty_drop_finishes_without_insert(self):
        app = self._run_drop([])
        app.folder_tree.insert.assert_not_called()

    def test_folder_and_non_folder_are_separated(self):
        app = make_bare_window()
        with mock.patch.object(window_module.shell32, "DragQueryFileW",
                               side_effect=self._drop_paths([r"D:\资料\项目", r"D:\notes.txt"])), \
             mock.patch.object(window_module.shell32, "DragAcceptFiles"), \
             mock.patch.object(window_module.os.path, "isdir",
                               side_effect=lambda p: not p.endswith(".txt")), \
             mock.patch.object(app, "_append_log") as append_log:
            app._handle_drop(0xABC)
        app.folder_tree.insert.assert_called_once()
        values = app.folder_tree.insert.call_args.kwargs["values"]
        self.assertEqual(values[0], r"D:\资料\项目")
        append_log.assert_called_once()
        self.assertIn("不是文件夹", append_log.call_args[0][0])

    def test_duplicate_folder_is_not_inserted_twice(self):
        app = make_bare_window()
        app.folder_tree.get_children.return_value = ("i1",)
        app.folder_tree.item.return_value = (r"D:\already", "—", "✕")

        def fake_query(_hdrop, index, buf, _size):
            if index == 0xFFFFFFFF:
                return 1
            if buf is None:
                return len(r"D:\already")
            buf.value = r"D:\already"
            return len(r"D:\already")

        with mock.patch.object(window_module.shell32, "DragQueryFileW", side_effect=fake_query), \
             mock.patch.object(window_module.shell32, "DragAcceptFiles"), \
             mock.patch.object(window_module.os.path, "isdir", return_value=True):
            app._handle_drop(0xABC)
        app.folder_tree.insert.assert_not_called()

    def test_many_dropped_folders_all_inserted(self):
        paths = [rf"D:\case{i}" for i in range(50)]
        app = self._run_drop(paths)
        self.assertEqual(app.folder_tree.insert.call_count, 50)

    def test_delete_key_removes_selection(self):
        app = make_bare_window()
        app.folder_tree.selection.return_value = ("i1", "i2")
        app._remove_selected()
        self.assertEqual(app.folder_tree.delete.call_count, 2)

    def test_tree_click_on_del_column_removes_row(self):
        app = make_bare_window()
        app.folder_tree.identify_column.return_value = "#3"
        app.folder_tree.identify_row.return_value = "i1"
        app._on_tree_click(mock.Mock(x=100, y=10))
        app.folder_tree.delete.assert_called_once_with("i1")

    def test_tree_click_on_other_column_keeps_row(self):
        app = make_bare_window()
        app.folder_tree.identify_column.return_value = "#1"
        app._on_tree_click(mock.Mock(x=10, y=10))
        app.folder_tree.delete.assert_not_called()


class RunActionGuardTests(unittest.TestCase):
    """扫描守卫（改写理由：`_run_action` 被扫描状态层移除，worker 只扫描，
    复制/保存改在主线程消费缓存；空列表提示与 worker 存活忽略两条守卫语义
    原样平移到 `_start_scan`）。"""

    def test_start_scan_ignored_while_worker_alive(self):
        gate = threading.Event()
        alive = threading.Thread(target=gate.wait)
        alive.start()
        # addCleanup 是 LIFO：先注册 join、后注册 set，保证 set 先执行再 join
        self.addCleanup(alive.join)
        self.addCleanup(gate.set)
        app = make_bare_window(_worker=alive)
        app.folder_tree.get_children.return_value = ("i1",)
        app.folder_tree.item.return_value = (r"C:\fake", "—", "✕")
        with mock.patch.object(window_module.threading, "Thread") as thread_ctor:
            app._start_scan()
        thread_ctor.assert_not_called()
        app.status_var.set.assert_called_once()
        self.assertIn("上一批", app.status_var.set.call_args[0][0])

    def test_empty_list_scan_shows_hint_without_worker(self):
        app = make_bare_window()  # 列表为空
        with mock.patch.object(window_module.threading, "Thread") as thread_ctor:
            app._start_scan()
        thread_ctor.assert_not_called()
        app.status_var.set.assert_called_once_with("请先添加要扫描的文件夹。")

    def test_worker_thread_receives_only_folders_and_opts(self):
        """改写理由：旧契约「worker 线程参数末位是 save_kind」随保存流程移入
        `_save_from_cache`（主线程消费缓存）而不再成立；等价新契约是
        `_start_scan` 给线程的位置参数恰为 (folders, opts)，不携带任何
        复制/保存动作参数——worker 职责收敛为纯扫描。"""
        app = make_bare_window()
        app.folder_tree.get_children.return_value = ("i1",)
        app.folder_tree.item.return_value = (r"C:\fake", "—", "✕")
        app._save_kind = "md"
        started = {}

        def fake_thread(target, args, daemon):
            started["target"] = target
            started["args"] = args
            return mock.MagicMock()

        with mock.patch.object(window_module.threading, "Thread", side_effect=fake_thread):
            app._start_scan()
        self.assertEqual(started["target"], app._worker_main)
        folders, opts = started["args"]
        self.assertEqual(folders, [r"C:\fake"])
        self.assertEqual(
            set(opts), {"format", "hide_git", "gitignore", "source_only", "size", "time"})
        self.assertNotIn("save_kind", opts)


class TrayToggleTests(unittest.TestCase):
    def test_toggle_on_persists_config_and_starts_tray(self):
        app = make_bare_window()
        app.tray_var = mock.Mock()
        app.tray_var.get.return_value = True
        with mock.patch.object(window_module, "update_config_values") as update, \
             mock.patch.object(app, "_ensure_tray") as ensure:
            app._on_tray_toggle()
        update.assert_called_once_with({"enableTray": True})
        ensure.assert_called_once()

    def test_toggle_off_persists_config_without_starting_tray(self):
        app = make_bare_window()
        app.tray_var = mock.Mock()
        app.tray_var.get.return_value = False
        with mock.patch.object(window_module, "update_config_values") as update, \
             mock.patch.object(app, "_ensure_tray") as ensure:
            app._on_tray_toggle()
        update.assert_called_once_with({"enableTray": False})
        ensure.assert_not_called()


class InitialUiValuesTests(unittest.TestCase):
    """UI 初始值应取自配置（评审 M-7），使窗口入口与右键入口默认行为一致。"""

    def test_values_come_from_config(self):
        config = {
            "defaultFormat": "json", "showFileSize": True,
            "showFileTime": True, "respectGitignore": True,
        }
        values = window_module._initial_ui_values(config)
        self.assertEqual(values["format_label"], "JSON")
        self.assertTrue(values["show_size"])
        self.assertTrue(values["show_time"])
        self.assertTrue(values["gitignore"])

    def test_unknown_format_falls_back_to_text_label(self):
        values = window_module._initial_ui_values({"defaultFormat": "no-such"})
        self.assertEqual(values["format_label"], "树状文本")

    def test_empty_config_uses_defaults(self):
        values = window_module._initial_ui_values({})
        self.assertEqual(values["format_label"], "树状文本")
        self.assertFalse(values["show_size"])
        self.assertFalse(values["show_time"])
        self.assertFalse(values["gitignore"])


class LogDrawerTests(unittest.TestCase):
    """底部抽屉：最近一条日志 + 展开日志 ▾/▴ 切换 log_text 显隐。"""

    def test_append_log_updates_drawer_line_with_stamp(self):
        app = make_bare_window()
        app._append_log("[完成] x")
        app.latest_log_var.set.assert_called_once()
        self.assertIn("[完成] x", app.latest_log_var.set.call_args[0][0])

    def test_append_log_blank_line_keeps_drawer_text(self):
        app = make_bare_window()
        app._append_log("")
        app.latest_log_var.set.assert_not_called()

    def test_toggle_expands_then_collapses_log_area(self):
        app = make_bare_window()
        app._toggle_log()
        app._log_frame.pack.assert_called_once_with(fill="x", before=app.drawer_frame)
        app.log_toggle_button.config.assert_called_with(text="收起日志 ▴")
        self.assertTrue(app._log_expanded)
        app._toggle_log()
        app._log_frame.pack_forget.assert_called_once()
        app.log_toggle_button.config.assert_called_with(text="展开日志 ▾")
        self.assertFalse(app._log_expanded)


class TintTitlebarTests(unittest.TestCase):
    """DWM 标题栏着色（可选加分）：PINE 入 COLORREF，失败静默。"""

    def test_tint_sets_dwm_caption_color(self):
        app = make_bare_window()
        app.root.winfo_id.return_value = 42
        fake_windll = mock.MagicMock()
        with mock.patch.object(window_module.user32, "GetParent", return_value=0x100), \
             mock.patch.object(window_module.ctypes, "windll", fake_windll):
            app._tint_titlebar()
        call = fake_windll.dwmapi.DwmSetWindowAttribute.call_args
        self.assertEqual(call.args[0], 0x100)
        self.assertEqual(call.args[1], 35)  # DWMWA_CAPTION_COLOR
        self.assertEqual(call.args[3], 4)
        r = int(theme.PINE[1:3], 16)
        g = int(theme.PINE[3:5], 16)
        b = int(theme.PINE[5:7], 16)
        self.assertEqual(call.args[2]._obj.value, (b << 16) | (g << 8) | r)  # 0x00BBGGRR

    def test_tint_failure_is_silent(self):
        app = make_bare_window()
        app.root.winfo_id.return_value = 42
        with mock.patch.object(window_module.user32, "GetParent",
                               side_effect=RuntimeError("no hwnd")), \
             mock.patch.object(window_module.ctypes, "windll", mock.MagicMock()):
            app._tint_titlebar()  # 不应抛出


class InstallBannerTests(unittest.TestCase):
    """安装状态横幅（Task 4）：ok 态零控件；非 ok 态渲染文案与 0~1 个注入回调按钮。

    状态字符串与 __main__ 的 _INSTALL_STATE_* 常量保持契约一致（window 禁止
    import __main__，避免循环导入），测试直接用字面量。
    回调契约：_install_actions 值为无参 callable，返回 bool 表示成功。
    """

    def _build_banner(self, install_state, install_info=None, install_actions=None):
        """在 __new__ 实例上跑 _build_status_banner，ttk 整体替换为 mock 以捕获控件参数。"""
        app = DropWindow.__new__(DropWindow)
        app.install_state = install_state
        app.install_info = install_info
        actions = {} if install_actions is None else install_actions
        # 与 __init__ 一致：install_actions 与运行时别名 _install_actions 指向同一 dict
        app.install_actions = actions
        app._install_actions = actions
        app._banner_frame = None
        parent = mock.Mock()
        with mock.patch.object(window_module, "ttk") as ttk_mock:
            app._build_status_banner(parent)
        return app, ttk_mock, parent

    def test_banner_hidden_when_state_ok(self):
        app, ttk_mock, _parent = self._build_banner("ok")
        ttk_mock.Frame.assert_not_called()
        ttk_mock.Label.assert_not_called()
        ttk_mock.Button.assert_not_called()
        self.assertIsNone(app._banner_frame)

    def test_banner_shows_update_button_with_versions(self):
        actions = {"update": mock.Mock(return_value=True)}
        app, ttk_mock, parent = self._build_banner(
            "update", install_info={"installed_version": "1.0.0"}, install_actions=actions)
        frame = ttk_mock.Frame.return_value
        ttk_mock.Frame.assert_called_once_with(parent)
        self.assertIs(app._banner_frame, frame)
        # 横幅先于列表区占满一行
        frame.pack.assert_called_once_with(fill="x")
        label_text = ttk_mock.Label.call_args.kwargs["text"]
        self.assertIn("v1.0.0", label_text)
        self.assertIn(f"v{window_module.VERSION}", label_text)
        button_kwargs = ttk_mock.Button.call_args.kwargs
        self.assertEqual(button_kwargs["text"], f"更新到 v{window_module.VERSION}")
        self.assertTrue(callable(button_kwargs["command"]))

    def test_banner_shows_install_button_when_not_installed(self):
        actions = {"not-installed": mock.Mock(return_value=True)}
        app, ttk_mock, _parent = self._build_banner("not-installed", install_actions=actions)
        self.assertIsNotNone(app._banner_frame)
        label_text = ttk_mock.Label.call_args.kwargs["text"]
        self.assertIn("尚未安装右键菜单", label_text)
        self.assertEqual(ttk_mock.Button.call_args.kwargs["text"], "安装右键菜单")

    def test_downgrade_button_maps_to_uninstall_action(self):
        action = mock.Mock(return_value=True)
        app, ttk_mock, _parent = self._build_banner(
            "downgrade", install_info={"installed_version": "9.9.9"},
            install_actions={"uninstall": action})
        label_text = ttk_mock.Label.call_args.kwargs["text"]
        self.assertIn("v9.9.9", label_text)
        self.assertIn(f"v{window_module.VERSION}", label_text)
        self.assertEqual(ttk_mock.Button.call_args.kwargs["text"], "卸载已装副本…")
        ttk_mock.Button.call_args.kwargs["command"]()
        action.assert_called_once_with()

    def test_repair_and_migrate_states_render_their_buttons(self):
        cases = (("repair", "重新安装"), ("migrate", "迁移到标准位置"))
        for state, button_text in cases:
            with self.subTest(state=state):
                action = mock.Mock(return_value=True)
                _app, ttk_mock, _parent = self._build_banner(
                    state, install_actions={state: action})
                self.assertEqual(ttk_mock.Button.call_args.kwargs["text"], button_text)
                ttk_mock.Button.call_args.kwargs["command"]()
                action.assert_called_once_with()

    def test_banner_without_injected_action_renders_label_only(self):
        # install_actions 缺省为 {}：只提示，不渲染无回调可走的按钮
        app, ttk_mock, _parent = self._build_banner("not-installed", install_actions={})
        self.assertIsNotNone(app._banner_frame)
        ttk_mock.Label.assert_called_once()
        ttk_mock.Button.assert_not_called()

    def test_update_button_invokes_injected_action(self):
        action = mock.Mock(return_value=True)
        app, ttk_mock, _parent = self._build_banner("update", install_actions={"update": action})
        command = ttk_mock.Button.call_args.kwargs["command"]
        command()
        action.assert_called_once_with()

    def test_banner_destroyed_after_action_success(self):
        app = DropWindow.__new__(DropWindow)
        app._install_actions = {"update": mock.Mock(return_value=True)}
        banner = mock.Mock()
        app._banner_frame = banner
        app._on_install_action("update")
        banner.destroy.assert_called_once_with()
        self.assertIsNone(app._banner_frame)

    def test_banner_kept_after_action_failure(self):
        app = DropWindow.__new__(DropWindow)
        action = mock.Mock(return_value=False)
        app._install_actions = {"update": action}
        banner = mock.Mock()
        app._banner_frame = banner
        app._on_install_action("update")
        banner.destroy.assert_not_called()
        self.assertIs(banner, app._banner_frame)
        # 失败后横幅保留，可重试
        action.return_value = True
        app._on_install_action("update")
        self.assertEqual(action.call_count, 2)
        banner.destroy.assert_called_once_with()

    def test_missing_action_key_is_noop(self):
        app = DropWindow.__new__(DropWindow)
        app._install_actions = {}
        banner = mock.Mock()
        app._banner_frame = banner
        app._on_install_action("update")  # 不应抛异常
        banner.destroy.assert_not_called()
        self.assertIs(banner, app._banner_frame)

    def test_run_drop_window_forwards_params(self):
        info = {"installed_version": "1.0.0"}
        actions = {"update": lambda: True}
        with mock.patch.object(window_module, "DropWindow") as window_ctor, \
             mock.patch.object(window_module, "stop_tray") as stop_tray:
            window_module.run_drop_window("update", info, actions)
        window_ctor.assert_called_once_with(
            install_state="update", install_info=info, install_actions=actions)
        window_ctor.return_value.run.assert_called_once_with()
        stop_tray.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class ScanStateTests(unittest.TestCase):
    """扫描状态层（根因修复契约）：
    显式「扫描」→ 结果缓存（每文件夹）→ 选中切换预览 → 复制/保存只消费缓存。
    列表或扫描相关选项变化 → 缓存失效，复制/保存禁用，直到重新扫描。"""

    def _make(self, folders=(("C:\\a", "128"), ("C:\\b", "3,412"))):
        app = make_bare_window()
        rows = [(f"i{i}", p, n) for i, (p, n) in enumerate(folders)]

        def item(iid, column=None):
            for r in rows:
                if r[0] == iid and column == "values":
                    return (r[1], r[2], "✕")
            return tuple()

        app.folder_tree.get_children.return_value = tuple(r[0] for r in rows)
        app.folder_tree.item.side_effect = item
        app.folder_tree.selection.return_value = (rows[0][0],) if rows else tuple()

        def flag(value=False):
            v = mock.Mock()
            v.get.return_value = value
            return v

        app.hide_git_var = flag()
        app.gitignore_var = flag()
        app.source_only_var = flag()
        app.size_var = flag()
        app.time_var = flag()
        app.format_var = flag("Markdown 代码块")
        app._scan_cache = {}
        app._scan_opts = None
        app._pending_scan_opts = None
        app._pending_auto_copy = False
        return app, rows

    def _seed(self, app, texts):
        for path, text in texts.items():
            app._scan_cache[path] = {
                "tree_text": text,
                "result": _fake_scan_result(total_files=10),
                "files": 10,
                "truncated": False,
            }
        app._scan_opts = app._scan_opt_snapshot()

    def _config(self):
        return {"excludeDirs": [], "excludeFiles": [], "excludePatterns": [],
                "maxFiles": 2000, "maxItemsPerLevel": 200, "maxDepth": -1}

    # ── 就绪门卫 ──

    def test_initial_buttons_disabled_with_scan_hint(self):
        app, _ = self._make(folders=())
        app._refresh_ready_card()
        app.copy_button.config.assert_called_with(state="disabled")
        app.save_button.config.assert_called_with(state="disabled")
        status = app.status_var.set.call_args[0][0]
        self.assertIn("请先执行扫描", status)

    def test_scan_fills_cache_and_enables_buttons(self):
        app, _ = self._make()
        app._on_scan()
        app._worker.join(timeout=5)
        app._poll()
        self.assertEqual(set(app._scan_cache), {"C:\\a", "C:\\b"})
        app.copy_button.config.assert_called_with(state="normal")
        app.save_button.config.assert_called_with(state="normal")

    def test_scan_button_wiring_calls_start(self):
        app, _ = self._make()
        with mock.patch.object(app, "_start_scan") as start:
            app._on_scan()
        start.assert_called_once_with()

    # ── 选中 → 预览 ──

    def test_selection_switch_updates_preview_from_cache(self):
        app, _ = self._make()
        self._seed(app, {"C:\\a": "TREE-A", "C:\\b": "TREE-B"})
        app.folder_tree.selection.return_value = ("i1",)
        app._on_folder_selected()
        inserted = "".join(str(c.args[1]) for c in app.preview_text.insert.call_args_list)
        self.assertIn("TREE-B", inserted)
        app.folder_tree.selection.return_value = ("i0",)
        app._on_folder_selected()
        inserted = "".join(str(c.args[1]) for c in app.preview_text.insert.call_args_list)
        self.assertIn("TREE-A", inserted)

    def test_unscanned_selection_shows_hint(self):
        app, _ = self._make()
        self._seed(app, {"C:\\a": "TREE-A"})  # C:\b 未扫描
        app.folder_tree.selection.return_value = ("i1",)
        app._on_folder_selected()
        inserted = "".join(str(c.args[1]) for c in app.preview_text.insert.call_args_list)
        self.assertIn("尚未扫描", inserted)

    # ── 复制/保存消费缓存 ──

    def test_copy_consumes_cache_without_rescan(self):
        app, _ = self._make()
        self._seed(app, {"C:\\a": "TA", "C:\\b": "TB"})
        with mock.patch.object(window_module, "copy_to_clipboard", return_value=True) as clip, \
             mock.patch.object(window_module, "format_output", return_value="FMT") as fmt:
            app._on_copy()
        clip.assert_called_once_with("FMT\n\nFMT")
        self.assertEqual(fmt.call_args.args[1], "markdown")
        self.assertIsNone(app._worker)

    def test_save_from_cache_writes_selected_format(self):
        import shutil
        import tempfile
        app, _ = self._make()
        tmp = Path(tempfile.mkdtemp(prefix="scan_state_"))
        target = str(tmp / "one")
        Path(target).mkdir()
        app.folder_tree.get_children.return_value = ("i0",)
        app.folder_tree.item.side_effect = (
            lambda iid, column=None: (target, "10", "✕") if column == "values" else tuple())
        self._seed(app, {target: "RAWTREE"})
        with mock.patch.object(window_module, "format_output", return_value="# MD") as fmt:
            app._on_save("md")
        written = (Path(target) / "directory_tree.md").read_text(encoding="utf-8")
        self.assertEqual(written, "# MD")
        fmt.assert_called_once()
        shutil.rmtree(tmp, ignore_errors=True)

    # ── 失效 ──

    def test_options_change_invalidates_cache(self):
        app, _ = self._make()
        self._seed(app, {"C:\\a": "TA", "C:\\b": "TB"})
        self.assertTrue(app._cache_valid())
        app.size_var.get.return_value = True
        self.assertFalse(app._cache_valid())
        app._refresh_ready_card()
        app.copy_button.config.assert_called_with(state="disabled")

    def test_list_change_invalidates_cache(self):
        app, _ = self._make()
        self._seed(app, {"C:\\a": "TA", "C:\\b": "TB"})
        app.folder_tree.get_children.return_value = ("i0",)  # 删除一行后
        app._refresh_ready_card()
        self.assertFalse(app._cache_valid())
        app.copy_button.config.assert_called_with(state="disabled")

    # ── 自动复制链 ──

    def test_drop_with_auto_copy_starts_scan_when_cache_invalid(self):
        app, _ = self._make()
        app.auto_copy_var.get.return_value = True
        app._pending_auto_copy = True
        with mock.patch.object(app, "_start_scan") as start:
            app._start_scan_after_drop()
        start.assert_called_once_with()
        self.assertTrue(app._pending_auto_copy)

    def test_scan_done_runs_pending_auto_copy(self):
        app, _ = self._make()
        self._seed(app, {"C:\\a": "TA", "C:\\b": "TB"})
        app._pending_auto_copy = True
        with mock.patch.object(window_module, "copy_to_clipboard", return_value=True) as clip, \
             mock.patch.object(window_module, "format_output", return_value="FMT"):
            app.actions.put(("scan_done", {"files": 20, "truncated": False}))
            app._poll()
        clip.assert_called_once()
        self.assertFalse(app._pending_auto_copy)
