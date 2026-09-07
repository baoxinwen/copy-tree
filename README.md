<div align="center">

# copy-tree

**一键复制文件目录树到剪贴板**

[![Windows](https://img.shields.io/badge/platform-Windows-blue)](https://github.com)
[![Python](https://img.shields.io/badge/python-3.10+-green)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)

右键任意文件夹 → 选择 copy-tree → 目录树已复制到剪贴板

</div>

---

## 功能特性

- **右键菜单集成** — 18 项操作：一键复制顶层直出，格式 / 选项 / 保存三个级联分组
- **完整目录树** — 可选过滤 `.git`、`node_modules` 等目录
- **多种输出格式** — 纯文本、Markdown 代码块、Markdown 列表、JSON、路径列表、文件名列表、统计摘要
- **文件信息** — 可选显示文件大小和修改时间
- **后缀/源码筛选** — 自定义扩展名过滤，源码模式识别 `Dockerfile`、`Makefile` 等
- **glob / .gitignore 过滤** — `excludePatterns` 通配符排除；一键遵循 `.gitignore`
- **深度限制** — 控制显示层级
- **保存到文件** — 导出为 txt、Markdown 或 JSON
- **配置文件** — 自定义排除列表、默认格式、显示限制等
- **拖拽窗口** — 双击已装副本打开窗口，批量拖入文件夹复制；可选驻留托盘
- **双击管理** — 双击 exe 即可安装、更新、修复或卸载
- **轻依赖** — 运行时仅依赖 loguru，其余为 Python 标准库 + ctypes

## 右键菜单

```
📋 复制目录树                ← 顶层直出，一步复制
─────────────────────
📝 copy-tree 格式 ▸
   📝 复制为 Markdown
   📑 复制为 Markdown 列表
   🧾 复制为 JSON
   📄 复制路径列表
   🔤 复制文件名列表
   📊 复制统计摘要
─────────────────────
⚙️ copy-tree 选项 ▸
   📂 复制（隐藏 .git 等目录）
   🙈 复制（遵循 .gitignore）
   🏷️ 复制（仅指定后缀）
   🌲 复制（限 2 层）
   📏 复制（含文件大小）
   🕒 复制（含修改时间）
   📝 复制为 Markdown（含大小）
─────────────────────
💾 copy-tree 保存与配置 ▸
   💾 保存为 txt
   💾 保存为 Markdown
   💾 保存为 JSON
   🔧 打开配置文件
```

## 输出示例

```
📁 my-project/
├── 📁 src/
│   ├── main.py
│   └── 📁 utils/
│       ├── helper.js
│       └── style.css
├── image.png
└── package.json
```

## 安装

### 下载 exe（推荐）

从 [Releases](../../releases) 下载 `copy-tree.exe`，**双击运行**即可完成安装。

安装后自动复制到 `%LOCALAPPDATA%\copy-tree\`，右键菜单指向该稳定路径。再次双击已安装副本会打开拖拽窗口；用新版 exe 双击可更新，卸载入口在拖拽窗口和开始菜单的「卸载 copy-tree」快捷方式。

### 从源码构建

```bash
git clone https://github.com/baoxinwen/copy-tree.git
cd copy-tree
pip install -r requirements.txt
python -m PyInstaller copy-tree.spec --noconfirm
```

产物在 `dist/copy-tree.exe` 和 `dist/copy-tree-cli.exe`。

## 命令行用法

`copy-tree-cli.exe` 用于脚本和重定向：

```bash
copy-tree-cli.exe "C:\path\to\folder"                    # 基本用法
copy-tree-cli.exe "C:\path\to\folder" --filter            # 过滤 .git 等
copy-tree-cli.exe "C:\path\to\folder" --size               # 含文件大小
copy-tree-cli.exe "C:\path\to\folder" --format json        # JSON 格式
copy-tree-cli.exe "C:\path\to\folder" --format paths       # 路径列表
copy-tree-cli.exe "C:\path\to\folder" --format summary     # 统计摘要
copy-tree-cli.exe "C:\path\to\folder" --max-depth 2        # 限 2 层
copy-tree-cli.exe "C:\path\to\folder" --gitignore          # 遵循 .gitignore
copy-tree-cli.exe "C:\path\to\folder" --save               # 保存为 txt
copy-tree-cli.exe "C:\path\to\folder" --save-json          # 保存为 JSON
copy-tree-cli.exe "C:\path\to\folder" --no-clipboard       # 只输出不复制
copy-tree-cli.exe --check-config                           # 检查配置
copy-tree-cli.exe --version                                # 查看版本
```

## 配置文件

首次使用「打开配置文件」时自动生成 `%APPDATA%\copy-tree\copy-tree.json`。以下为示意（节选），实际生成的 `filterExt` 默认包含全部内置源码扩展名（`.py`、`.js`、`.md`、`.txt` 等 70+ 项）：

```json
{
  "excludeDirs": [".git", "node_modules", "__pycache__"],
  "excludeFiles": [".DS_Store", "Thumbs.db"],
  "excludePatterns": ["*.min.js", "dist/*"],
  "maxFiles": 2000,
  "maxItemsPerLevel": 200,
  "maxDepth": -1,
  "defaultFormat": "text",
  "showFileSize": false,
  "showFileTime": false,
  "respectGitignore": false,
  "enableTray": false,
  "filterExt": [".py", ".js", ".ts", ".html", ".css"]
}
```

| 字段 | 范围 | 说明 |
|------|------|------|
| `maxFiles` | -1 ~ 1000000 | 最大文件数，-1 不限制 |
| `maxItemsPerLevel` | 1 ~ 10000 | 单层级最大项数 |
| `maxDepth` | -1 ~ 100 | 显示深度，-1 不限制 |
| `excludePatterns` | 字符串列表 | 通配符排除，匹配名称或相对路径，仅在过滤模式下生效 |
| `showFileTime` | true/false | 默认显示修改时间 |
| `respectGitignore` | true/false | 扫描时遵循 `.gitignore` |
| `enableTray` | true/false | 拖拽窗口关闭时驻留托盘（默认 false，不常驻后台） |

## 拖拽窗口

双击已安装的 `copy-tree.exe`（版本一致时）会打开拖拽窗口：把一个或多个文件夹拖进窗口，按所选格式和过滤选项扫描并复制到剪贴板。窗口内还可以保存为 txt、打开配置文件或卸载 copy-tree。勾选「关闭时驻留托盘」后，关闭窗口仅最小化到系统托盘，双击托盘图标可再次打开。

## 日志

运行日志写入 `%APPDATA%\copy-tree\logs\copy-tree.log`：DEBUG 级全量记录，2MB 轮转、保留 5 份。
CLI 场景下 WARNING 及以上同时输出到 stderr；**stdout 始终只输出目录树数据**，脚本管道不受影响。
日志仅包含路径与统计信息，不记录文件内容或剪贴板文本。

## 项目结构

```
src/copytree/
├── __init__.py        # 版本号
├── __main__.py        # 入口：CLI 解析、GUI/CLI 模式分发
├── winapi.py          # Win32 API ctypes 声明集中管理
├── constants.py       # 常量：过滤列表、限制、UI 字符串
├── natural_sort.py    # 自然排序（file2 < file10）
├── config.py          # 配置文件加载与生成
├── scanner.py         # 目录扫描、过滤、树状文本生成
├── gitignore.py       # .gitignore 解析与匹配
├── formatter.py       # 输出格式化（文本 / Markdown / JSON / 路径 / 摘要）
├── clipboard.py       # Win32 剪贴板操作
├── notify.py          # 系统气泡通知
├── logging_setup.py   # 集中日志配置（loguru）
├── window.py          # 拖拽窗口（tkinter + WM_DROPFILES）
├── tray.py            # 可选托盘图标（默认关闭）
├── shortcut.py        # 开始菜单快捷方式（COM 接口）
└── registry.py        # 注册表右键菜单管理
```

## 许可证

[MIT License](LICENSE)
