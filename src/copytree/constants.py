"""copy-tree 常量定义：默认过滤列表、限制、UI 字符串、注册表路径。"""

import os

from . import __version__

# ── 版本 ──
VERSION = __version__

# ── AppUserModelID ──
APP_ID = "CopyTree.CopyTree"

# ── 默认过滤目录（大小写不敏感，精确匹配）──
DEFAULT_EXCLUDE_DIRS = frozenset({
    ".git", ".svn", ".hg",
    "node_modules", "__pycache__",
    ".vs", ".vscode", ".idea",
    "dist", "build", "out", "bin", "obj",
    ".next", ".nuxt", ".cache",
    "vendor", "target", ".gradle",
})

# ── 默认过滤文件（大小写不敏感，精确匹配）──
DEFAULT_EXCLUDE_FILES = frozenset({
    ".DS_Store", "Thumbs.db", "desktop.ini",
})

# ── 限制 ──
MAX_FILES = 2000
MAX_ITEMS_PER_LEVEL = 200
MAX_NAME_LENGTH = 80

# ── 源码文件扩展名（用于「仅源码文件」菜单项）──
SOURCE_CODE_EXTENSIONS = frozenset({
    ".py", ".pyw",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".kt", ".kts", ".scala",
    ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx",
    ".cs", ".fs", ".vb",
    ".go", ".rs", ".swift", ".dart",
    ".rb", ".php", ".pl", ".r", ".R",
    ".lua", ".vim", ".el", ".clj", ".ex", ".exs",
    ".sql", ".sh", ".bash", ".zsh", ".fish", ".ps1",
    ".bat", ".cmd",
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".vue", ".svelte",
    ".json", ".yaml", ".yml", ".toml", ".xml", ".ini", ".cfg",
    ".md", ".rst", ".txt", ".tex",
    ".cmake", ".makefile", ".dockerfile",
    ".proto", ".graphql", ".graphqls",
    ".wasm",
})

# ── 无扩展名但常见的源码/构建配置文件 ──
SOURCE_CODE_FILENAMES = frozenset({
    "dockerfile", "makefile", "gnumakefile",
    "cmakelists.txt", "justfile", "rakefile",
    "gemfile", "podfile", "procfile", "vagrantfile",
})

# ── 默认输出文件名 ──
DEFAULT_OUTPUT_FILENAME_TXT = "directory_tree.txt"
DEFAULT_OUTPUT_FILENAME_MD = "directory_tree.md"
DEFAULT_OUTPUT_FILENAME_JSON = "directory_tree.json"
GENERATED_OUTPUT_FILENAMES = frozenset({
    DEFAULT_OUTPUT_FILENAME_TXT,
    DEFAULT_OUTPUT_FILENAME_MD,
    DEFAULT_OUTPUT_FILENAME_JSON,
})

# ── 树状符号 ──
BRANCH = "\u251C\u2500\u2500 "     # ├──
LAST = "\u2514\u2500\u2500 "       # └──
PIPE = "\u2502   "                  # │
SPACE = "    "                      #     (4 spaces)
FOLDER_PREFIX = "\U0001F4C1 "      # 📁
LOCK_PREFIX = "\U0001F512 "        # 🔒

# ── 文件属性（Windows）──
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
# Junction 的 reparse tag（不是 symlink，显示为文件条目且不递归）
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003

# ── 快捷方式路径 ──
INSTALL_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA", ""),
    "copy-tree",
)
INSTALL_EXE = os.path.join(INSTALL_DIR, "copy-tree.exe")
INSTALL_CLI_EXE = os.path.join(INSTALL_DIR, "copy-tree-cli.exe")
SHORTCUT_DIR = os.path.join(
    os.environ.get("APPDATA", ""),
    "Microsoft", "Windows", "Start Menu", "Programs",
)
SHORTCUT_NAME = "copy-tree.lnk"
SHORTCUT_UNINSTALL_NAME = "卸载 copy-tree.lnk"

# ── 配置文件路径 ──
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", ""), "copy-tree")
CONFIG_FILE = os.path.join(CONFIG_DIR, "copy-tree.json")

# ── 日志路径 ──
LOG_DIR = os.path.join(CONFIG_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "copy-tree.log")

# ── UI 字符串 ──
MSG_INSTALLED = "copy-tree 已就绪，右键文件夹即可使用"
MSG_UNINSTALLED = "copy-tree 已卸载"
MSG_UNINSTALLED_RESIDUE = "copy-tree 已卸载（部分文件被占用未能删除，可稍后手动删除安装目录）"
MSG_NOTIFY_SUCCESS = "已复制目录树：{files} 个文件，{dirs} 个文件夹"
MSG_NOTIFY_SUCCESS_TRUNCATED = "已复制目录树：{files} 个文件，{dirs} 个文件夹（已截断：{reason}）"
MSG_NOTIFY_FAIL = "复制失败：{error}"
MSG_NO_ACCESS = "无访问权限"
MSG_TRUNCATED_TAIL = "... 输出已截断：{details}（当前显示 {shown_files} 个文件，{total_dirs} 个文件夹）"
MSG_TRUNCATED_LEVEL = "(还有 {count} 项未显示)"
MSG_TRUNCATED_DEPTH = "(目录层级过深，后续未扫描)"
MSG_SIZE_UNKNOWN = "未知"
