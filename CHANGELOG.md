# Changelog

本文件记录 copy-tree 的全部重要变更。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [1.2.0] - 2026-09-08

墨林纸意界面：拖拽窗口全面重设计，扫描成为独立步骤。

### Added

- 界面全面重设计（墨林纸意风格）：松林绿标题栏、纸白画布、表格化文件夹列表、输出预览区兼任拖放目标
- 「扫描」成为独立步骤：先扫描看预览与文件数，再复制/保存；扫描前复制与保存按钮禁用
- 输出预览：复制前即可查看目录树前 15 行，随选中文件夹切换
- 保存格式扩展：txt / Markdown / JSON 三种格式可选导出
- 「拖入后自动复制」设置项：拖入文件夹后自动完成扫描与复制
- 复制成功反馈：按钮本体变为「已复制 ✓」2 秒

### Changed

- 切换选中的文件夹时，输出预览跟随切换
- 列表或扫描选项变化后，复制/保存按钮禁用并提示重新扫描，避免拿到过期结果
- 结果卡区分就绪态与截断警告态：截断时明确提示「结果可能不完整」并提供「调整上限」入口

### Fixed

- 扫描进行中或选项刚变化时，不再可能把过期的扫描结果复制出去

## [1.1.0] - 2026-09-07

纯绿色版：双击即用，安装引导收敛进窗口。

### Added

- 拖拽窗口按安装状态渲染横幅与操作按钮：未安装 / 有更新 / 版本回退 / 需修复 / 需迁移五种状态一目了然，安装、更新、修复、迁移、卸载全部在窗口内一键完成
- 首次启动引导：未安装时弹一次安装询问，选择"暂不"后不再打扰（配置 `installPromptDismissed`），随时可在窗口点「安装右键菜单」补装

### Changed

- 双击即用：双击 copy-tree.exe 不再弹出安装/更新/修复对话框链，任何状态下都直接打开拖拽窗口；安装决策全部改为窗口内用户主动触发
- 同版本副本双击不再比对字节、不再弹卸载/保留询问，视为正常直接开窗

### Fixed

- 修复从浏览器"打开"等非资源管理器场景启动时，因 `sys.stderr` 为空导致的启动崩溃（`TypeError: Cannot log to objects of type 'NoneType'`）
- 有控制台但标准流未接通时，`_attach_parent_console` 现在会接通 `CONOUT$`/`CONERR$`，CLI 模式 stderr 日志恢复可用

## [1.0.0] - 2026-09-01

首个稳定版。

### Added

- 右键菜单集成：「📋 复制目录树」顶层直出，格式 / 选项 / 保存与配置三个单层级联，共 18 项操作
- 输出格式 7 种：树状文本、Markdown 代码块、Markdown 列表、JSON、路径列表、文件名列表、统计摘要
- 保存到文件：`--save` / `--save-md` / `--save-json` 可组合，导出 `directory_tree.txt` / `.md` / `.json`
- `.gitignore` 支持：按目录级联规则过滤，内层覆盖外层（`--gitignore` 或配置 `respectGitignore`）
- `excludePatterns` 通配排除：匹配文件名或根相对路径，仅在过滤模式生效
- 拖拽窗口：双击已安装副本打开，批量拖入文件夹复制；可选关闭后驻留系统托盘（`enableTray`，默认关闭）
- 集中日志：`%APPDATA%\copy-tree\logs\copy-tree.log`（DEBUG 起记、2MB 轮转保留 5 份）；stdout 始终只输出目录树数据，脚本管道不受污染
- 安装管理：稳定副本位于 `%LOCALAPPDATA%\copy-tree`，按注册表记录的版本号判断更新；提供修复入口、开始菜单「卸载 copy-tree」快捷方式
- 独立命令行入口 `copy-tree-cli.exe`：支持 stdout、管道与重定向；参数错误退出码 1，剪贴板写入失败退出码 2
- 配置文件 `%APPDATA%\copy-tree\copy-tree.json`：中文注释自动生成，兼容 UTF-8 BOM，非法值告警并回退默认
- 文件信息与截断控制：文件大小 / 修改时间展示，`maxFiles` / `maxItemsPerLevel` / `maxDepth` 截断并在尾部汇总说明
- exe 内嵌 VS_VERSIONINFO 版本资源；GitHub Actions 以 Python 3.10 / 3.12 双版本测试后自动构建发布（附 SHA256SUMS.txt）
