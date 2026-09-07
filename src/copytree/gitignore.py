"""精简版 .gitignore 解析与匹配。纯 stdlib，供扫描器按目录级联应用规则。

支持的语义（与 git 行为对齐的常用子集）：
- 空行与 # 注释行忽略；``\\#``/``\\!`` 转义字面量；
- ``!`` 前缀反选（最后一条命中的规则生效）；
- 尾部 ``/`` 表示仅匹配目录；
- 含内部 ``/`` 的模式锚定到所在 .gitignore 目录，否则匹配任意层级的路径段；
- ``**`` 跨路径段通配，``*``/``?``/``[...]`` 为单段通配；
- 父目录被忽略时其子树不再进入（由扫描器剪枝保证），
  与 git 一致：无法重新包含已被忽略目录下的内容。
匹配统一不区分大小写（Windows 惯例），与 git 的区分大小写略有差异。
"""

import os
import re

_GITIGNORE_NAME = ".gitignore"


class IgnoreRule:
    """一条编译后的 gitignore 规则。"""

    __slots__ = ("negate", "dir_only", "regex")

    def __init__(self, negate: bool, dir_only: bool, regex: re.Pattern):
        self.negate = negate
        self.dir_only = dir_only
        self.regex = regex

    def matches(self, rel_path: str, is_dir: bool) -> bool:
        if self.dir_only and not is_dir:
            return False
        return self.regex.match(rel_path) is not None


class GitignoreRuleSet:
    """单个 .gitignore 文件的解析结果。"""

    __slots__ = ("rules",)

    def __init__(self, base_dir: str):
        self.rules: list[IgnoreRule] = []
        path = os.path.join(base_dir, _GITIGNORE_NAME)
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                text = f.read()
        except (OSError, ValueError):
            return
        for raw_line in text.splitlines():
            rule = self._parse_line(raw_line)
            if rule is not None:
                self.rules.append(rule)

    @staticmethod
    def _parse_line(raw_line: str) -> IgnoreRule | None:
        line = raw_line.rstrip()
        if not line or line.startswith("#"):
            return None
        negate = line.startswith("!")
        if negate:
            line = line[1:]
        # 首个字符的转义字面量（\# \!）
        if len(line) >= 2 and line[0] == "\\" and line[1] in "#!":
            line = line[1:]

        dir_only = line.endswith("/")
        if dir_only:
            line = line.rstrip("/")

        anchored = "/" in line
        if anchored and line.startswith("/"):
            line = line.lstrip("/")

        body = _translate_glob(line)
        if not body:
            return None
        if anchored:
            # match() 只从路径开头尝试，未锚定模式用 (?:.*/)? 表达任意前缀层级
            regex_src = f"^(?:{body})(?:/|$)"
        else:
            regex_src = f"^(?:.*/)?(?:{body})(?:/|$)"
        try:
            regex = re.compile(regex_src, re.IGNORECASE)
        except re.error:
            return None
        return IgnoreRule(negate=negate, dir_only=dir_only, regex=regex)

    def match(self, rel_path: str, is_dir: bool) -> bool | None:
        """返回该文件是否被此规则集命中；True=忽略，False=反选，None=无匹配。"""
        result: bool | None = None
        for rule in self.rules:
            if rule.matches(rel_path, is_dir):
                result = not rule.negate
        return result


class GitignoreStack:
    """按目录层级叠加的规则集；进入目录时压入该目录的 .gitignore，离开时弹出。

    匹配时从根到当前目录依次求值，内层规则集的结果覆盖外层（最后一条命中规则生效）。
    每层规则集以自身 .gitignore 所在目录为基准求值（与 git 一致）：扫描方在压栈时
    提供该目录相对扫描根的段序列，求值时剥离同名前缀再匹配锚定规则。
    """

    def __init__(self):
        self._stack: list[tuple[GitignoreRuleSet, tuple[str, ...]]] = []

    def push_dir(self, abs_dir: str, base_segments: tuple[str, ...] = ()) -> None:
        """进入目录。base_segments 为该目录相对扫描根的目录段（空表示扫描根本身）。"""
        self._stack.append((GitignoreRuleSet(abs_dir), tuple(base_segments)))

    def pop_dir(self) -> None:
        if self._stack:
            self._stack.pop()

    def is_ignored(self, rel_path: str, is_dir: bool) -> bool:
        result = False
        parts = rel_path.split("/")
        for rule_set, base_segments in self._stack:
            if base_segments and len(parts) > len(base_segments):
                base_rel = "/".join(parts[len(base_segments):])
            else:
                base_rel = rel_path
            matched = rule_set.match(base_rel, is_dir)
            if matched is not None:
                result = matched
        return result


def _translate_glob(pattern: str) -> str:
    """把 gitignore 通配符翻译为正则片段；翻译失败（空段/坏语法）返回空串。"""
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "/":
            # 相邻斜杠或首尾斜杠（此处尾部已剥离）不合法，跳过多余斜杠
            if out and out[-1] != "/":
                out.append("/")
            i += 1
        elif ch == "*":
            if pattern[i : i + 2] == "**":
                # `**/`（两侧均为段边界）按 git 语义翻译为“跨零个或多个目录段”：
                # `**/foo` 命中根级 foo，`a/**/b` 命中 a/b；
                # 其余位置保持 .*（结尾 `/**` 即“目录内全部内容”）
                if pattern[i + 2 : i + 3] == "/" and (not out or out[-1] == "/"):
                    out.append("(?:[^/]*/)*")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
                    while i < n and pattern[i] == "*":  # 容忍 ***/*** 合并
                        i += 1
            else:
                out.append("[^/]*")
                i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape(ch))
                i += 1
            else:
                content = pattern[i + 1 : end]
                if content.startswith("!"):
                    content = "^" + content[1:]
                out.append(f"[{content}]")
                i = end + 1
        elif ch == "\\" and i + 1 < n:
            out.append(re.escape(pattern[i + 1]))
            i += 2
        else:
            out.append(re.escape(ch))
            i += 1
    src = "".join(out)
    # 折叠连续斜杠，避免 "a//b" 产生 "//" 正则片段语义漂移
    while "//" in src:
        src = src.replace("//", "/")
    if src in ("", "/"):
        return ""
    return src
