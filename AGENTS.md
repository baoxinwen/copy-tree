# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Build & Install

```bash
# Build (produces dist/copy-tree.exe and dist/copy-tree-cli.exe)
pip install -r requirements.txt
python -m PyInstaller copy-tree.spec --noconfirm

# Install/uninstall right-click menu
# Double-click dist/copy-tree.exe; confirm install, replacement, or uninstall.

# Reliable CLI stdout / redirection
dist/copy-tree-cli.exe --version
dist/copy-tree-cli.exe <folder>

# Tests
python -m unittest discover -s tests
python -m compileall src tests
```

Build creates two entry points from the same code: `copy-tree.exe` uses `console=False` (no CMD window for right-click/install/uninstall), while `copy-tree-cli.exe` uses `console=True` for reliable stdout, pipes, and redirection. `copy-tree.exe` CLI output is best-effort only because of the Windows GUI subsystem. PyInstaller entry point is `src/main.py` (not `src/copytree/__main__.py`). Icon is embedded from `src/copytree/icon.ico`.

Double-clicking `copy-tree.exe` (no args) manages installation: if not installed it asks for confirmation before installing, copies a stable GUI executable to `%LOCALAPPDATA%/copy-tree/copy-tree.exe`, optionally copies sibling `copy-tree-cli.exe`, then registers right-click commands and the Start Menu shortcut to that stable path (plus an「卸载 copy-tree」shortcut pointing at `--uninstall`). If already installed, decisions are version-first: the menu registry key records `Version`; when it equals the running build, double-click opens the drag-drop window (`window.py`) with no dialogs — uninstall lives in the window and the Start Menu uninstall shortcut; when it is older, double-click offers update/uninstall/cancel; when no version is recorded (installs from before versioning), fall back to whole-file byte compare: identical external files ask uninstall/keep, different external files ask update/uninstall/cancel, missing registered targets ask repair/uninstall/cancel, and legacy non-stable paths ask migrate/uninstall/cancel. Double-clicking `copy-tree-cli.exe` must not install, uninstall, or notify; with no args it prints CLI help whose usage name is `copy-tree-cli.exe`, waits for a key only when launched from Explorer, then exits with argument error. Terminal no-args, parser errors, or setup-arg misuse must not pause unless the parent process is Explorer. `copy-tree-cli.exe --version` prints `copy-tree-cli <version>`. All exits go through `_exit()` which calls `wait_notification()` before `sys.exit()` to ensure balloon notifications display.

## Architecture

**Execution modes** determined by executable and `__main__.py`:
- **GUI mode** (`copy-tree.exe` from Explorer/right-click/double-click or hidden `--notify`): setup/copy + tray balloon notification
- **CLI mode** (`copy-tree-cli.exe`, or best-effort stdout from `copy-tree.exe` when handles are available): clipboard + stdout/stderr

**Data flow**: CLI args → config merge (+ validation warnings) → `scanner.scan_directory()` → `build_tree_text()` → `formatter.format_output()` → clipboard (+ optional file save)

**Filtering logic** (in `__main__.py`, not scanner):
- Default (`copy-tree.exe path`): no filtering, shows everything
- `--filter`: applies `excludeDirs`/`excludeFiles` from config (filters `.git`, `node_modules`, etc.)
- `--exclude` extra items always apply regardless of `--filter`
- `--filter-ext` reads `filterExt` from config for extension-based filtering
- `--source-only` uses hardcoded `SOURCE_CODE_EXTENSIONS` plus `SOURCE_CODE_FILENAMES` (e.g. Dockerfile/Makefile) regardless of config
- `--gitignore` applies per-directory `.gitignore` rules via `gitignore.py`: cascading rule sets (inner dirs override outer), ignored dirs are never descended into; `respectGitignore` config turns it on by default; implies empty-dir pruning
- `excludePatterns` config adds fnmatch-style glob excludes matched against names and root-relative paths (case-insensitive), active only when filtering is on (`--filter`), same lifecycle as `excludeDirs`
- Generated output files (`directory_tree.txt`, `directory_tree.md`) are always excluded so saved trees are not included in the next scan
- Registry right-click commands append hidden `--notify` so copy actions force GUI notification mode instead of being mistaken for CLI output.

**Output formats**:
- `text`: raw tree text
- `markdown`: tree text wrapped in a Markdown code block
- `markdown-list`: nested Markdown bullet list rendered from `ScanResult`
- `json`: structured tree payload plus stats/truncation metadata
- `paths`: one absolute file path per line (long-path prefix stripped)
- `names`: one file name per line
- `summary`: one-line stats summary (files, directories, total size, truncation reason)

`VALID_FORMATS` is derived from `formatter._FORMATTERS`; config re-exports it and argparse choices must be kept in sync manually. `TreeEntry.size` is always populated on Windows (scandir cache, no extra syscall) so `summary` can total sizes even when `show_size` only controls display.

**Key module dependencies**:
- `scanner.py` is the core — `_ScanContext` with filtering/truncation counters, performance-first `maxFiles` early stop, long child-path normalization, depth guard, tree rendering, include-name filtering, long display-name-safe recursion, and empty-dir pruning for active filter modes
- `config.py` loads `%APPDATA%/copy-tree/copy-tree.json`; generates annotated defaults with `__X说明` keys on first open; includes `filterExt`; records validation warnings for invalid JSON/types/ranges/unknown keys; `update_config_values()` patches keys atomically (temp file + `os.replace`) preserving annotation keys
- `gitignore.py` parses `.gitignore` into compiled rule sets (`GitignoreRuleSet`); `GitignoreStack` overlays one rule set per directory while scanning — inner rules override outer, ignored dirs are never descended into; matching is case-insensitive, supports `!` negation, trailing `/` dir-only, inner `/` anchoring, `**` crossing segments
- `formatter.py` formats text, Markdown code blocks, Markdown lists, JSON, path lists, name lists, and stats summaries; JSON uses real names from `TreeEntry.path` when available so long display-name truncation does not corrupt machine output, and stats use `scannedFiles`/`fileCountsAreComplete`
- `window.py` is the tkinter drag-drop window opened on same-version double-click; native drops subclass the Tk toplevel HWND for `WM_DROPFILES` via ctypes (`SetWindowLongPtrW`); scans run on a worker thread and results flow back through a thread-safe queue polled by `root.after` — never touch tkinter from other threads
- `tray.py` is the optional tray icon (config `enableTray`, default off) with its own Win32 message-pump thread; actions are marshalled to the Tk thread via the shared queue; `start_tray` must not hold `_tray_lock` while waiting on the ready event (the tray thread briefly acquires it)
- `__main__.py` prepares the stable installed copy under `%LOCALAPPDATA%/copy-tree`, manages install/replace/uninstall prompts, and uses native `WriteConsoleW`/`WriteFile` plus CRT fd fallback for best-effort `copy-tree.exe` output without changing the no-window GUI requirement; reliable CLI output belongs to `copy-tree-cli.exe`. CLI-only affordances include `--no-clipboard`/`--stdout-only` for scripts and `--check-config` for config validation without scanning.
- `registry.py` manages cascading right-click submenu via `MUIVerb` + `SubCommands` + nested `shell\` subkeys; cleans old subkeys before writing to avoid duplicates; `CommandFlags=0x20` for menu separators; exposes installed-path detection for GUI replacement prompts
- `notify.py` tracks notification thread via `_notify_thread` global; `wait_notification()` joins before process exit
- `clipboard.py`, `shortcut.py` are independent Win32 interop modules using ctypes
- `logging_setup.py` centralizes loguru configuration — file sink at `%APPDATA%/copy-tree/logs/copy-tree.log` (DEBUG+, 2MB rotation, 5 retained), stderr sink WARNING+ only in CLI mode; stdout is never a log sink because it carries tree data; every exit funnels through `_exit()` which logs the code. Modules must not add their own sinks
- `tests/` contains local stdlib `unittest` coverage for scanner/config/formatter/gitignore edge cases plus characterization suites for clipboard (Win32 stubs on the module's own WinDLL instances), window (`DropWindow.__new__` + mocks, queue `_poll` state machine, WM_DROPFILES two-call protocol), tray (start/stop guards, wndproc), shortcut (in-memory fake COM objects with hand-built vtables), registry install failure semantics, and the `__main__` install decision tree — IS tracked in git; `.github/workflows/build.yml` runs the compile check (`python -m compileall src tests`), the unittest suite, then build/release on Windows and uploads both exe files plus `SHA256SUMS.txt`. Only test *artifacts* (`test_runtime*/`, coverage output) are git-ignored

**Right-click menu**: `registry.py` writes 4 top-level keys per location (`Directory\shell` + `Directory\Background\shell`): the `CopyTree` leaf (📋 复制目录树, one-click copy; carries `Version` for `get_installed_version()` and its `command` is read by `get_registered_command()`/`get_installed_exe_path()`), plus three SINGLE-LEVEL cascades — `CopyTreeFmt` (📝 格式), `CopyTreeOpt` (⚙️ 选项), `CopyTreeSave` (💾 保存与配置). Hard constraints learned through live testing on Win11 25H2 (26200): **depth-2 static cascades do not expand at all** on 24H2+ regardless of write pattern (SubCommands enum/explicit/path-form/ExtendedSubCommandsKey all fail on a textbook-minimal sample), so never nest a cascade inside a cascade; and a cascade container **must NOT set the default value alongside MUIVerb** — default+MUIVerb together silently breaks expansion, MUIVerb-only works. `_write_leaf_command()` injects `--notify` into every command.

**Config precedence**: defaults < config file < CLI args. Merged in `get_effective_config()`.

## Platform Constraints

- Windows only — uses `winreg`, `ctypes.windll`, `os.startfile`, Win32 clipboard/shell APIs
- All Win32 ctypes calls must set explicit `restype`/`argtypes` for 64-bit pointer safety (see `clipboard.py`)
- Notifications use `shell32.Shell_NotifyIconW` (not `user32`), daemon thread with join-on-exit
- Registry keys go under `HKCU` only — no admin rights needed
- Long paths (>248 chars) get `\\?\` prefix, and UNC paths get `\\?\UNC\`, in `scanner._normalize_path()`

## Config file

Located at `%APPDATA%/copy-tree/copy-tree.json`. Fields: `excludeDirs`, `excludeFiles`, `excludePatterns` (fnmatch globs, filter-mode only), `maxFiles`, `maxItemsPerLevel`, `maxDepth` (-1=unlimited), `defaultFormat` (text|markdown|markdown-list|json|paths|names|summary), `showFileSize`, `showFileTime`, `respectGitignore`, `enableTray`, `filterExt` (list of extensions for `--filter-ext`). Invalid JSON/value types/ranges/unknown keys are ignored, defaults are kept, and warnings are surfaced via stderr in CLI mode or notification summary in GUI mode; files with a UTF-8 BOM parse fine (`utf-8-sig`). Generated with Chinese annotation keys (`__X说明`) on first `--config` open.
