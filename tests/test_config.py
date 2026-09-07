import sys
import json
import shutil
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from copytree import config as config_module  # noqa: E402
from copytree.config import (  # noqa: E402
    _merge,
    get_config_warnings,
    get_effective_config,
    load_config,
    update_config_values,
)

from loguru import logger as _quiet_logger  # noqa: E402

_quiet_logger.remove()  # 保持测试输出干净


class ConfigTests(unittest.TestCase):
    def test_rejects_non_string_list_items(self):
        config = {"excludeDirs": ["build"]}
        _merge(config, {"excludeDirs": ["dist", 1]}, "excludeDirs", list)
        self.assertEqual(config["excludeDirs"], ["build"])

    def test_rejects_invalid_integer_ranges(self):
        config = {"maxFiles": 2000, "maxItemsPerLevel": 200, "maxDepth": -1}

        _merge(config, {"maxFiles": -2}, "maxFiles", int)
        _merge(config, {"maxItemsPerLevel": 0}, "maxItemsPerLevel", int)
        _merge(config, {"maxDepth": -2}, "maxDepth", int)

        self.assertEqual(config["maxFiles"], 2000)
        self.assertEqual(config["maxItemsPerLevel"], 200)
        self.assertEqual(config["maxDepth"], -1)

    def test_rejects_invalid_default_format(self):
        config = {"defaultFormat": "text"}
        _merge(config, {"defaultFormat": "html"}, "defaultFormat", str)
        self.assertEqual(config["defaultFormat"], "text")

    def test_accepts_new_default_formats(self):
        config = {"defaultFormat": "text"}
        _merge(config, {"defaultFormat": "json"}, "defaultFormat", str)
        self.assertEqual(config["defaultFormat"], "json")

        _merge(config, {"defaultFormat": "markdown-list"}, "defaultFormat", str)
        self.assertEqual(config["defaultFormat"], "markdown-list")

    def test_load_config_records_validation_warnings(self):
        original_config_file = config_module.CONFIG_FILE
        tmp = Path("test_runtime_config")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir()
        try:
            config_file = tmp / "copy-tree.json"
            config_file.write_text(
                '{"maxFiles": -2, "showFileSize": "yes", "unknown": true}',
                encoding="utf-8",
            )
            config_module.CONFIG_FILE = str(config_file)
            try:
                config = load_config()
                warnings = get_config_warnings()
            finally:
                config_module.CONFIG_FILE = original_config_file
        finally:
            if tmp.exists():
                shutil.rmtree(tmp)

        self.assertEqual(config["maxFiles"], 2000)
        self.assertFalse(config["showFileSize"])
        self.assertTrue(any("maxFiles" in warning for warning in warnings))
        self.assertTrue(any("showFileSize" in warning for warning in warnings))
        self.assertTrue(any("未知配置项 unknown" in warning for warning in warnings))

    def test_filter_ext_requires_dot_prefix(self):
        from copytree.constants import SOURCE_CODE_EXTENSIONS

        original_config_file = config_module.CONFIG_FILE
        tmp = Path("test_runtime_config_ext")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir()
        try:
            config_file = tmp / "copy-tree.json"
            config_file.write_text(
                '{"filterExt": [".py", "js"]}',
                encoding="utf-8",
            )
            config_module.CONFIG_FILE = str(config_file)
            try:
                config = load_config()
                warnings = get_config_warnings()
            finally:
                config_module.CONFIG_FILE = original_config_file
        finally:
            if tmp.exists():
                shutil.rmtree(tmp)

        self.assertEqual(config["filterExt"], sorted(SOURCE_CODE_EXTENSIONS))
        self.assertTrue(any("filterExt" in warning for warning in warnings))

    def test_install_prompt_dismissed_defaults_false(self):
        original_config_file = config_module.CONFIG_FILE
        tmp = Path("test_runtime_config_prompt")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir()
        try:
            config_module.CONFIG_FILE = str(tmp / "copy-tree.json")
            try:
                cfg = get_effective_config({})
            finally:
                config_module.CONFIG_FILE = original_config_file
        finally:
            if tmp.exists():
                shutil.rmtree(tmp)

        self.assertIs(False, cfg["installPromptDismissed"])

    def test_install_prompt_dismissed_invalid_type_falls_back(self):
        original_config_file = config_module.CONFIG_FILE
        tmp = Path("test_runtime_config_prompt_invalid")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir()
        try:
            config_file = tmp / "copy-tree.json"
            config_file.write_text(
                '{"installPromptDismissed": "yes"}',
                encoding="utf-8",
            )
            config_module.CONFIG_FILE = str(config_file)
            try:
                cfg = get_effective_config({})
                warnings = get_config_warnings()
            finally:
                config_module.CONFIG_FILE = original_config_file
        finally:
            if tmp.exists():
                shutil.rmtree(tmp)

        self.assertIs(False, cfg["installPromptDismissed"])
        self.assertTrue(any("installPromptDismissed" in warning for warning in warnings))

    def test_update_config_values_persists_dismiss_flag(self):
        original_config_file = config_module.CONFIG_FILE
        original_config_dir = config_module.CONFIG_DIR
        tmp = Path("test_runtime_config_prompt_update")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir()
        try:
            config_file = tmp / "copy-tree.json"
            config_module.CONFIG_FILE = str(config_file)
            config_module.CONFIG_DIR = str(tmp)
            try:
                updated = update_config_values({"installPromptDismissed": True})
                doc = json.loads(config_file.read_text("utf-8-sig"))
            finally:
                config_module.CONFIG_FILE = original_config_file
                config_module.CONFIG_DIR = original_config_dir
        finally:
            if tmp.exists():
                shutil.rmtree(tmp)

        self.assertTrue(updated)
        self.assertTrue(doc["installPromptDismissed"])
        self.assertIn("__installPromptDismissed说明", doc)


if __name__ == "__main__":
    unittest.main()
