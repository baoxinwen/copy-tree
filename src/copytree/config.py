"""配置文件加载。从 %APPDATA%/copy-tree/copy-tree.json 读取，缺失或格式错误时使用默认值。"""

import json
import os

from loguru import logger

from .constants import (
    CONFIG_DIR,
    CONFIG_FILE,
    DEFAULT_EXCLUDE_DIRS,
    DEFAULT_EXCLUDE_FILES,
    MAX_FILES,
    MAX_ITEMS_PER_LEVEL,
    SOURCE_CODE_EXTENSIONS,
)
from .formatter import VALID_FORMATS

_DEFAULTS = {
    "excludeDirs": list(DEFAULT_EXCLUDE_DIRS),
    "excludeFiles": list(DEFAULT_EXCLUDE_FILES),
    "excludePatterns": [],
    "maxFiles": MAX_FILES,
    "maxItemsPerLevel": MAX_ITEMS_PER_LEVEL,
    "maxDepth": -1,
    "defaultFormat": "text",
    "showFileSize": False,
    "showFileTime": False,
    "respectGitignore": False,
    "enableTray": False,
    "installPromptDismissed": False,
    "filterExt": sorted(SOURCE_CODE_EXTENSIONS),
}

VALID_FORMATS  # 由 formatter._FORMATTERS 派生，此处仅再导出保持原引用路径可用
_CONFIG_WARNINGS: list[str] = []  # 非线程安全，当前仅单线程使用

_COMMENTS = {
    "__说明": "这是 copy-tree 的配置文件。修改后保存，下次使用右键菜单时生效。删除此文件可恢复默认设置。",
    "__excludeDirs说明": "要排除的目录名列表。精确匹配目录名，大小写不敏感。例如想排除 logs 目录，就在列表里加上 \"logs\"。",
    "__excludeFiles说明": "要排除的文件名列表。精确匹配文件名，大小写不敏感。",
    "__excludePatterns说明": "按通配符模式排除文件或目录，匹配名称或相对路径，大小写不敏感。例如 [\"*.min.js\", \"dist/*\", \"*.log\"]。与 excludeDirs 一样，仅在启用过滤（如「隐藏 .git 等目录」）时生效。",
    "__maxFiles说明": "最大显示文件总数。超过此数量会截断并在末尾提示。设为 -1 表示不限制。",
    "__maxItemsPerLevel说明": "同一层级（同一个文件夹内）最大显示项数。超过此数量会在该层级截断。",
    "__maxDepth说明": "默认显示深度。-1 表示不限制（显示全部层级），0 表示仅显示根目录，2 表示只显示 2 层。右键菜单有快捷选项。",
    "__defaultFormat说明": "默认输出格式。可选：\"text\"（纯文本）、\"markdown\"（Markdown 代码块）、\"markdown-list\"（Markdown 列表）、\"json\"（结构化 JSON）、\"paths\"（路径列表）、\"names\"（文件名列表）、\"summary\"（统计摘要）。",
    "__showFileSize说明": "是否默认显示文件大小。true 显示，false 不显示。右键菜单有专门的「含大小」选项。",
    "__showFileTime说明": "是否默认显示修改时间。true 显示，false 不显示。右键菜单有专门的「含修改时间」选项。",
    "__respectGitignore说明": "扫描时是否遵循目录中的 .gitignore 规则过滤。true 遵循，false 忽略。右键菜单有专门的「遵循 .gitignore」选项。",
    "__enableTray说明": "拖拽窗口关闭时是否驻留系统托盘（不常驻后台的默认选择是 false）。",
    "__installPromptDismissed说明": "首次使用引导中用户选择“暂不安装”后，是否不再弹出安装询问（true 后仍可随时在拖拽窗口安装）。",
    "__filterExt说明": "按后缀筛选文件的扩展名列表。用于右键菜单「仅指定后缀文件」功能。可自定义，例如只看图片就填 [\".png\", \".jpg\", \".svg\"]。",
}


def load_config() -> dict:
    """加载配置文件，返回有效配置字典，并记录无效配置警告。"""
    global _CONFIG_WARNINGS
    _CONFIG_WARNINGS = []
    config = dict(_DEFAULTS)

    try:
        # utf-8-sig 兼容记事本保存时写入的 BOM，避免带 BOM 配置被静默回退默认值
        with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
            user = json.load(f)
    except FileNotFoundError:
        logger.debug("配置文件不存在，使用内置默认值 {}", CONFIG_FILE)
        return config
    except json.JSONDecodeError as e:
        _CONFIG_WARNINGS.append(f"配置文件不是有效 JSON（第 {e.lineno} 行第 {e.colno} 列），已使用默认配置")
        logger.warning("配置文件不是有效 JSON（第 {} 行第 {} 列），已使用默认配置", e.lineno, e.colno)
        return config
    except (OSError, ValueError) as e:
        _CONFIG_WARNINGS.append(f"无法读取配置文件，已使用默认配置：{e}")
        logger.warning("无法读取配置文件，已使用默认配置: {}", e)
        return config

    if not isinstance(user, dict):
        _CONFIG_WARNINGS.append("配置文件根对象必须是 JSON 对象，已使用默认配置")
        logger.warning("配置文件根对象必须是 JSON 对象，已使用默认配置")
        return config

    schema = {
        "excludeDirs": list,
        "excludeFiles": list,
        "excludePatterns": list,
        "maxFiles": int,
        "maxItemsPerLevel": int,
        "maxDepth": int,
        "defaultFormat": str,
        "showFileSize": bool,
        "showFileTime": bool,
        "respectGitignore": bool,
        "enableTray": bool,
        "installPromptDismissed": bool,
        "filterExt": list,
    }
    for key, expected_type in schema.items():
        if key in user and not _merge(config, user, key, expected_type):
            warning = _validation_warning(key, user[key])
            _CONFIG_WARNINGS.append(warning)
            logger.warning("配置项无效: {}", warning)

    unknown_keys = sorted(k for k in user if not k.startswith("__") and k not in schema)
    for key in unknown_keys:
        _CONFIG_WARNINGS.append(f"未知配置项 {key} 已忽略")

    filter_ext = config.get("filterExt")
    if isinstance(filter_ext, list):
        invalid_exts = [item for item in filter_ext if not item.startswith(".")]
        if invalid_exts:
            config["filterExt"] = sorted(SOURCE_CODE_EXTENSIONS)
            preview = ", ".join(repr(item) for item in invalid_exts[:5])
            if len(invalid_exts) > 5:
                preview += " 等"
            _CONFIG_WARNINGS.append(
                f"filterExt 含不以 . 开头的扩展名：{preview}，已恢复默认列表"
            )

    return config


def get_config_warnings() -> list[str]:
    """返回最近一次 load_config()/get_effective_config() 产生的配置校验警告。"""
    return list(_CONFIG_WARNINGS)


def get_effective_config(cli_overrides: dict | None = None) -> dict:
    """合并：默认值 < 配置文件 < CLI 覆盖。返回最终配置。"""
    config = load_config()
    if cli_overrides:
        for key, value in cli_overrides.items():
            if value is not None:
                config[key] = value
    return config


def _merge(config: dict, user: dict, key: str, expected_type: type):
    if key not in user:
        return True

    value = user[key]
    if expected_type is list:
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            config[key] = value
            return True
        return False

    if expected_type is int:
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        if key == "maxFiles" and not (-1 <= value <= 1_000_000):
            return False
        if key == "maxItemsPerLevel" and not (1 <= value <= 10_000):
            return False
        if key == "maxDepth" and not (-1 <= value <= 100):
            return False
        config[key] = value
        return True

    if expected_type is bool:
        if isinstance(value, bool):
            config[key] = value
            return True
        return False

    if expected_type is str:
        if isinstance(value, str):
            if key == "defaultFormat" and value not in VALID_FORMATS:
                return False
            config[key] = value
            return True
        return False

    return False


def _validation_warning(key: str, _value) -> str:
    if key in ("excludeDirs", "excludeFiles", "excludePatterns", "filterExt"):
        return f"{key} 必须是字符串列表，当前值已忽略"
    if key == "maxFiles":
        return "maxFiles 必须是 -1 到 1000000 之间的整数，当前值已忽略"
    if key == "maxItemsPerLevel":
        return "maxItemsPerLevel 必须是 1 到 10000 之间的整数，当前值已忽略"
    if key == "maxDepth":
        return "maxDepth 必须是 -1 到 100 之间的整数，当前值已忽略"
    if key == "defaultFormat":
        allowed = ", ".join(VALID_FORMATS)
        return f"defaultFormat 必须是以下之一：{allowed}，当前值已忽略"
    if key == "showFileSize":
        return "showFileSize 必须是 true 或 false，当前值已忽略"
    if key == "showFileTime":
        return "showFileTime 必须是 true 或 false，当前值已忽略"
    if key == "respectGitignore":
        return "respectGitignore 必须是 true 或 false，当前值已忽略"
    if key == "enableTray":
        return "enableTray 必须是 true 或 false，当前值已忽略"
    if key == "installPromptDismissed":
        return "installPromptDismissed 必须是 true 或 false，当前值已忽略"
    return f"{key} 的值无效，当前值已忽略"


def ensure_config_file() -> str:
    """确保配置文件存在（带详细注释），返回文件路径。"""
    if not os.path.isfile(CONFIG_FILE):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        doc = {}
        doc["__说明"] = _COMMENTS["__说明"]
        doc["excludeDirs"] = sorted(DEFAULT_EXCLUDE_DIRS)
        doc["__excludeDirs说明"] = _COMMENTS["__excludeDirs说明"]
        doc["excludeFiles"] = sorted(DEFAULT_EXCLUDE_FILES)
        doc["__excludeFiles说明"] = _COMMENTS["__excludeFiles说明"]
        doc["excludePatterns"] = []
        doc["__excludePatterns说明"] = _COMMENTS["__excludePatterns说明"]
        doc["maxFiles"] = MAX_FILES
        doc["__maxFiles说明"] = _COMMENTS["__maxFiles说明"]
        doc["maxItemsPerLevel"] = MAX_ITEMS_PER_LEVEL
        doc["__maxItemsPerLevel说明"] = _COMMENTS["__maxItemsPerLevel说明"]
        doc["maxDepth"] = -1
        doc["__maxDepth说明"] = _COMMENTS["__maxDepth说明"]
        doc["defaultFormat"] = "text"
        doc["__defaultFormat说明"] = _COMMENTS["__defaultFormat说明"]
        doc["showFileSize"] = False
        doc["__showFileSize说明"] = _COMMENTS["__showFileSize说明"]
        doc["showFileTime"] = False
        doc["__showFileTime说明"] = _COMMENTS["__showFileTime说明"]
        doc["respectGitignore"] = False
        doc["__respectGitignore说明"] = _COMMENTS["__respectGitignore说明"]
        doc["enableTray"] = False
        doc["__enableTray说明"] = _COMMENTS["__enableTray说明"]
        doc["installPromptDismissed"] = False
        doc["__installPromptDismissed说明"] = _COMMENTS["__installPromptDismissed说明"]
        doc["filterExt"] = sorted(SOURCE_CODE_EXTENSIONS)
        doc["__filterExt说明"] = _COMMENTS["__filterExt说明"]
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
    return CONFIG_FILE


def update_config_values(values: dict) -> bool:
    """更新配置文件中的若干键（保留注释键与未涉及项）；文件不存在时先创建默认骨架。

    原子写入：先写临时文件再替换，避免写一半崩溃损坏配置。
    """
    try:
        ensure_config_file()
        with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
            doc = json.load(f)
        if not isinstance(doc, dict):
            return False
        doc.update(values)
        tmp_path = CONFIG_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, CONFIG_FILE)
        logger.info("配置已更新 keys={}", sorted(values))
        return True
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("配置更新失败: {}", e)
        return False


def open_config_file() -> bool:
    """创建默认配置文件（如不存在）并用记事本打开。"""
    try:
        path = ensure_config_file()
        os.startfile(path)
        return True
    except OSError:
        return False
