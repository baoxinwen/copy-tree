"""注册表安装/卸载操作。顶层直出 + 多个单层级联子菜单。

重要（实测结论，勿回退）：Windows 11 24H2/25H2 的经典右键菜单**无法展开二级
静态级联**——无论 SubCommands 空串枚举、显式列表、路径形式还是
ExtendedSubCommandsKey 自指，教科书式最小样例同样失效。因此所有级联只允许
一层：常用复制直出为顶层叶节点，其余功能拆分为多个单层（一级）级联。
"""

import os
import winreg

from loguru import logger

from .constants import APP_ID, SHORTCUT_UNINSTALL_NAME, VERSION
from .shortcut import create_start_menu_shortcut, remove_start_menu_shortcut

# 两个注册位置（%1 为文件夹本体，%V 为空白处）
_MENU_LOCATIONS = [
    r"Software\Classes\Directory\shell",            # 右键点击文件夹
    r"Software\Classes\Directory\Background\shell",  # 空白处右键
]

# 每个位置下的顶层键名
_KEY_MAIN = "CopyTree"        # 一键复制（顶层叶节点，最高频动作直达）
_KEY_FORMATS = "CopyTreeFmt"  # 其他输出格式（单层级联）
_KEY_OPTIONS = "CopyTreeOpt"  # 扫描选项（单层级联）
_KEY_SAVE = "CopyTreeSave"    # 保存与配置（单层级联）

_ALL_TOP_KEYS = [_KEY_MAIN, _KEY_FORMATS, _KEY_OPTIONS, _KEY_SAVE]

# 级联子项：(注册表子键名, 菜单文字, 命令行参数后缀, 是否在此项前加分隔线)
_FORMAT_ITEMS = [
    ("fmt1md",      "📝 复制为 Markdown",       "--format markdown",         False),
    ("fmt2mdlist",  "📑 复制为 Markdown 列表",   "--format markdown-list",    False),
    ("fmt3json",    "🧾 复制为 JSON",            "--format json",             False),
    ("fmt4paths",   "📄 复制路径列表",            "--format paths",            True),
    ("fmt5names",   "🔤 复制文件名列表",          "--format names",            False),
    ("fmt6summary", "📊 复制统计摘要",            "--format summary",          False),
]

_OPTION_ITEMS = [
    ("opt1filter", "📂 复制（隐藏 .git 等目录）",  "--filter",                  False),
    ("opt2git",    "🙈 复制（遵循 .gitignore）",   "--filter --gitignore",      False),
    ("opt3ext",    "🏷️ 复制（仅指定后缀）",       "--filter-ext",              False),
    ("opt4depth",  "🌲 复制（限 2 层）",           "--max-depth 2",             False),
    ("opt5size",   "📏 复制（含文件大小）",        "--size",                    False),
    ("opt6time",   "🕒 复制（含修改时间）",        "--time",                    False),
    ("opt7mdsize", "📝 复制为 Markdown（含大小）", "--format markdown --size",  False),
]

_SAVE_ITEMS = [
    ("save1txt",    "💾 保存为 txt",       "--save --no-clipboard",     False),
    ("save2md",     "💾 保存为 Markdown",   "--save-md --no-clipboard",  False),
    ("save3json",   "💾 保存为 JSON",       "--save-json --no-clipboard", False),
    ("save4config", "🔧 打开配置文件",       "--config",                  True),
]


def install(exe_path: str) -> bool:
    """注册右键菜单、创建快捷方式、注册 AUMID。"""
    exe_path = os.path.abspath(exe_path)
    if not os.path.isfile(exe_path):
        return False

    try:
        if not create_start_menu_shortcut(exe_path):
            # 快捷方式是第一个写入动作：此时注册表尚未被改动，
            # 直接失败返回即可。绝不能调用 uninstall()，
            # 否则更新场景下会把既有完好安装误删。
            return False
        # 「卸载 copy-tree」快捷方式是双击不再弹卸载询问后的常驻卸载入口，
        # 创建失败不影响安装
        if not create_start_menu_shortcut(
            exe_path, arguments="--uninstall", shortcut_name=SHORTCUT_UNINSTALL_NAME
        ):
            logger.warning("卸载快捷方式创建失败（不影响安装）")
        _register_aumid(exe_path)
        for location in _MENU_LOCATIONS:
            is_background = "Background" in location
            arg_param = r'"%V\."' if is_background else r'"%1\."'
            _write_menu(location, exe_path, arg_param)
        return True
    except OSError:
        # 注册表/AUMID 写失败只报安装失败，绝不能调用 uninstall()——
        # 与上方快捷方式分支同一不变量：更新场景下 uninstall 会把既有
        # 完好安装连根拆除。残缺状态由 is_registered() 识别，下次双击
        # 走修复流程。
        logger.exception("注册右键菜单失败")
        return False


def is_registered() -> bool:
    """检查右键菜单各顶层键是否都存在（不校验命令值内容）。"""
    return all(
        _key_exists(location + "\\" + key)
        for location in _MENU_LOCATIONS
        for key in _ALL_TOP_KEYS
    )


def get_registered_command() -> str:
    """返回主菜单项注册的原始命令串，未注册时为空字符串。"""
    return _read_command(_MENU_LOCATIONS[0] + "\\" + _KEY_MAIN + r"\command")


def get_installed_exe_path() -> str:
    """返回当前右键菜单命令指向的主程序路径，未安装或无法解析时返回空字符串。"""
    command = get_registered_command()
    return _extract_quoted_exe(command)


def get_installed_version() -> str:
    """返回已注册右键菜单记录的版本号，未注册或未记录时为空字符串。"""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _MENU_LOCATIONS[0] + "\\" + _KEY_MAIN, 0, winreg.KEY_READ
        )
    except (FileNotFoundError, OSError):
        return ""
    try:
        value, _value_type = winreg.QueryValueEx(key, "Version")
    except OSError:
        return ""
    finally:
        winreg.CloseKey(key)
    return value if isinstance(value, str) else ""


def uninstall() -> bool:
    """清除所有注册表项、删除快捷方式。"""
    for location in _MENU_LOCATIONS:
        for key in _ALL_TOP_KEYS:
            _delete_key_recursive(winreg.HKEY_CURRENT_USER, location + "\\" + key)

    # 清理旧的单独注册项（兼容旧版本升级）
    for old_key in [
        r"Software\Classes\Directory\shell\CopyTree.WithSize",
        r"Software\Classes\Directory\Background\shell\CopyTree.WithSize",
    ]:
        _delete_key_recursive(winreg.HKEY_CURRENT_USER, old_key)

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{APP_ID}")
    except (FileNotFoundError, OSError):
        pass

    remove_start_menu_shortcut()
    leftovers = [
        location + "\\" + key
        for location in _MENU_LOCATIONS
        for key in _ALL_TOP_KEYS
        if _key_exists(location + "\\" + key)
    ]
    if leftovers:
        logger.error("卸载后菜单键仍残留: {}", leftovers)
        return False
    logger.info("卸载完成：菜单键、快捷方式与 AUMID 已清除")
    return True


def _write_menu(location: str, exe_path: str, arg_param: str):
    """在一个注册位置写入全部顶层键：直出叶节点 + 三个单层级联。"""
    # 先清理该位置的全部旧顶层键，避免残留
    for key in _ALL_TOP_KEYS:
        _delete_key_recursive(winreg.HKEY_CURRENT_USER, location + "\\" + key)

    # 1. 一键复制：顶层叶节点，最高频动作一步直达
    main_path = location + "\\" + _KEY_MAIN
    _write_leaf_command(main_path, "📋 复制目录树", "", exe_path, arg_param)
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, main_path, 0, winreg.KEY_WRITE)
    winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, f'"{exe_path}",0')
    # 记录版本号，双击已装副本时按版本判断升级/就绪，免去全量字节比对
    winreg.SetValueEx(key, "Version", 0, winreg.REG_SZ, VERSION)
    winreg.CloseKey(key)

    # 2-4. 单层级联（Win11 24H2+ 无法展开二级级联，故全部平铺为一级）
    _write_cascade(location, _KEY_FORMATS, "📝 copy-tree 格式", _FORMAT_ITEMS, exe_path, arg_param)
    _write_cascade(location, _KEY_OPTIONS, "⚙️ copy-tree 选项", _OPTION_ITEMS, exe_path, arg_param)
    _write_cascade(location, _KEY_SAVE, "💾 copy-tree 保存与配置", _SAVE_ITEMS, exe_path, arg_param)


def _write_cascade(
    location: str,
    key_name: str,
    label: str,
    items: list,
    exe_path: str,
    arg_param: str,
):
    """写入一个单层级联容器：MUIVerb + 空 SubCommands + shell 子键。

    关键约束（实测）：容器**不能写默认值**——默认值与 MUIVerb 并存时
    子菜单无法展开；只写 MUIVerb 即可正常显示与展开。
    """
    base_path = location + "\\" + key_name
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, base_path, 0, winreg.KEY_WRITE)
    winreg.SetValueEx(key, "MUIVerb", 0, winreg.REG_SZ, label)
    winreg.SetValueEx(key, "SubCommands", 0, winreg.REG_SZ, "")
    winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, f'"{exe_path}",0')
    winreg.CloseKey(key)

    # 容器需要空的 command 子键，避免点击容器时执行命令
    _write_empty_command(base_path + r"\command")

    for subkey_name, text, args, separator in items:
        item_path = base_path + r"\shell" + "\\" + subkey_name
        _write_leaf_command(item_path, text, args, exe_path, arg_param)
        if separator:
            item_key = winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER, item_path, 0, winreg.KEY_SET_VALUE
            )
            winreg.SetValueEx(item_key, "CommandFlags", 0, winreg.REG_DWORD, 0x20)
            winreg.CloseKey(item_key)


def _write_leaf_command(
    item_path: str, text: str, args: str, exe_path: str, arg_param: str
):
    """写入一个叶子菜单项及其命令；命令统一注入 --notify 强制通知模式。"""
    item_key = winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, item_path, 0, winreg.KEY_WRITE
    )
    winreg.SetValueEx(item_key, "", 0, winreg.REG_SZ, text)
    winreg.SetValueEx(item_key, "Icon", 0, winreg.REG_SZ, f'"{exe_path}",0')
    winreg.CloseKey(item_key)

    cmd_key = winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, item_path + r"\command", 0, winreg.KEY_WRITE
    )
    cmd_str = f'"{exe_path}" {arg_param} --notify'
    if args:
        cmd_str += f" {args}"
    winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, cmd_str)
    winreg.CloseKey(cmd_key)


def _write_empty_command(key_path: str):
    """写入默认值为空串的 command 子键。"""
    cmd_key = winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE
    )
    # 清掉旧版布局可能写入的默认命令值，避免残留指向过期 exe
    winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, "")
    winreg.CloseKey(cmd_key)


def _key_exists(key_path: str) -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ)
    except (FileNotFoundError, OSError):
        return False
    winreg.CloseKey(key)
    return True


def _read_command(key_path: str) -> str:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ)
    except (FileNotFoundError, OSError):
        return ""
    try:
        value, _value_type = winreg.QueryValueEx(key, "")
    except OSError:
        value = ""
    finally:
        winreg.CloseKey(key)
    return value if isinstance(value, str) else ""


def _extract_quoted_exe(command: str) -> str:
    command = command.strip()
    if not command.startswith('"'):
        return ""
    end = command.find('"', 1)
    if end == -1:
        return ""
    return command[1:end]


def _delete_key_recursive(root: int, key_path: str):
    """递归删除注册表键及其所有子键。"""
    try:
        key = winreg.OpenKey(root, key_path, 0, winreg.KEY_READ)
    except FileNotFoundError:
        return
    except OSError:
        return

    # 先收集所有子键名
    subkeys = []
    try:
        i = 0
        while True:
            name = winreg.EnumKey(key, i)
            subkeys.append(name)
            i += 1
    except OSError:
        pass
    winreg.CloseKey(key)

    # 递归删除子键
    for name in subkeys:
        _delete_key_recursive(root, key_path + "\\" + name)

    # 删除自身
    try:
        winreg.DeleteKey(root, key_path)
    except (FileNotFoundError, OSError):
        pass


def _register_aumid(exe_path: str):
    """在注册表中注册 AppUserModelID（通知所需）。"""
    key_path = rf"Software\Classes\AppUserModelId\{APP_ID}"
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE)
    winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "copy-tree")
    winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, exe_path)
    winreg.CloseKey(key)
