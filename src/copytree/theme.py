"""墨林纸意设计 token —— 拖拽窗口的唯一风格来源。

分层约定：
- 原语/语义色：本文件的常量，window.py 只允许引用这里的角色色，
  禁止出现裸色值（改风格 = 改本文件）；
- 字体：FONT_SANS 用于界面正文与控件，FONT_MONO 用于文件路径/数字/日志；
- 圆角与阴影：tkinter 的 ttk 控件不支持，仅在 Canvas 自绘元素上生效，
  因此不提供 shadow token，圆角常量供自绘时引用；
- apply_theme(style)：统一切换 ttk 基主题并铺底色，幂等。
"""

# ── 语义色 ──
PINE = "#1F3D2B"        # 主色：标题栏、主按钮、选中态
PINE_HOVER = "#2C5540"  # 主色悬停
PAPER = "#FAF9F6"       # 窗口画布
SURFACE = "#F1EFE8"     # 分区底（扫描条、结果卡、侧区）
AMBER = "#E8A33D"       # 品牌点睛：标题装饰符、拖放高亮、警告边
INK = "#22301F"         # 主文字（非纯黑）
MUTED = "#7A776D"       # 次要文字
FAINT = "#A8A49A"       # 弱提示文字
LINE = "#E3E0D6"        # 分隔线
BORDER = "#D8D4C8"      # 控件描边
DANGER = "#8C3A2B"      # 危险动作（卸载、清空确认）
DANGER_LINE = "#E5CFC5" # 危险动作描边
WARN_BG = "#FBF3DB"     # 截断警告底
WARN_TEXT = "#7A5A00"   # 截断警告文字
SUCCESS = "#346538"     # 复制成功反馈
WHITE = "#FFFFFF"

# ── 字体 ──
FONT_SANS = "Segoe UI"
FONT_MONO = "Cascadia Mono"

# ── 字号（磅） ──
SIZE_BODY = 10
SIZE_SMALL = 9
SIZE_TITLE = 10

# ── 间距（像素）──
PAD_WINDOW = 16
PAD_GROUP = 12
PAD_ROW = 8

# ── 圆角（像素，仅 Canvas 自绘元素可用） ──
RADIUS_SM = 6
RADIUS_MD = 8

# apply_theme 只铺一次底；重复调用直接返回，避免 ttk 主题反复重载。
_applied = False


def apply_theme(style) -> None:
    """对传入的 ttk.Style 应用墨林纸意底色体系。幂等。"""
    global _applied
    if _applied:
        return
    _applied = True
    style.theme_use("clam")
    style.configure("TFrame", background=PAPER)
    style.configure("TLabel", background=PAPER, foreground=INK)
    style.configure("TButton", background=WHITE, foreground=INK,
                    bordercolor=BORDER, focusthickness=0, padding=(10, 4))
    style.map("TButton",
              background=[("active", SURFACE), ("pressed", SURFACE)],
              bordercolor=[("active", PINE)])
    style.configure("TCheckbutton", background=PAPER, foreground=MUTED,
                    focusthickness=0)
    style.map("TCheckbutton",
              background=[("active", PAPER)],
              indicatorcolor=[("selected", PINE)])
