"""三个界面共用的 ttk 样式构造。

这里同时提供**共用的深色主题色板**（``BG`` / ``PANEL`` / ``FG`` …）与
**构造逻辑**（``use_clam`` / ``segmented_buttons``）：三个界面原先各自写死色值、
观感不一致，现已收敛为一处。缩略图边长等几何仍由各界面自行决定。

要点（本项目踩过的坑）：clam 主题会自绘浅色 chrome，只设 ``background`` /
``foreground`` 压不住；下面这些样式都显式覆盖了 ``bordercolor`` /
``lightcolor`` / ``darkcolor``。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ui_kit import FONT

# ---------------------------------------------------------------------------
# 深色主题色板（三个界面共用，统一观感、避免各处数值漂移）
# ---------------------------------------------------------------------------
BG = "#1b1b1f"            # 内容区（最深）
PANEL = "#232329"         # 面板（顶栏 / 表单区 / 底栏）
BAR = "#1a1a20"           # 状态栏（比面板明显更暗，形成可感知的一档）
SEP = "#3a3a44"           # 分隔线
FIELD = "#31313a"         # 输入框底色（比面板亮，保证"看得出是输入框"）
BORDER = "#4a4c53"        # 输入框描边（对 FIELD 约 2.1:1，可见但不刺眼）
BTN = "#2e2e36"           # 次级按钮底色
FG = "#e8e8ec"            # 一级文字
SUB = "#9aa0a8"           # 二级文字
MUTED = "#7d838b"         # 三级文字（对 PANEL 约 3.6:1，可读）
ACCENT = "#3b82f6"        # 强调色
ACCENT_HOVER = "#4b8ef7"
ACCENT_DIM = "#2b4a77"    # 强调色的弱化版（禁用 / 次级选中态）

# 网格卡片（两个挑选器共用）：靠明度分档；选中 = 深蓝底 + 强调色描边 + 对勾徽标
CARD = "#26262c"          # 卡片常态
CARD_HOVER = "#30303a"    # 悬停
CARD_SEL = "#2a3c5c"      # 选中（深蓝底：够醒目，又不压掉缩略图本身）


def use_clam(st: ttk.Style) -> None:
    """切到 ``clam`` 主题（本项目所有自绘样式的基准；失败则沿用当前主题）。"""
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass


def segmented_buttons(
    st: ttk.Style,
    *,
    base_bg: str,
    base_fg: str,
    hover_bg: str,
    hover_fg: str,
    accent: str,
    accent_hover: str,
    padding: tuple[int, int],
) -> None:
    """配置语言切换用的分段按钮：``Seg.TButton``（未选）/ ``SegOn.TButton``（选中）。

    未选态为平底灰字、选中态为强调色实底白字——用于替代 clam 下无法配色的单选钮。
    """
    st.configure("Seg.TButton", font=(FONT, 9), padding=padding,
                 background=base_bg, foreground=base_fg,
                 borderwidth=0, relief="flat",
                 bordercolor=base_bg, lightcolor=base_bg, darkcolor=base_bg)
    st.map("Seg.TButton",
           background=[("active", hover_bg)], foreground=[("active", hover_fg)])
    st.configure("SegOn.TButton", font=(FONT, 9, "bold"), padding=padding,
                 background=accent, foreground="#ffffff",
                 borderwidth=0, relief="flat",
                 bordercolor=accent, lightcolor=accent, darkcolor=accent)
    st.map("SegOn.TButton", background=[("active", accent_hover)])


# ---------------------------------------------------------------------------
# 进度条（两个模式**共用一份定义**，避免样式名 / 配色漂移——UI-2）
#
# clam 主题的进度条也由自绘图片构成，只设 ``background`` 会在两端留下浅色 chrome；
# 因此连 ``bordercolor`` / ``lightcolor`` / ``darkcolor`` 一起覆盖，并给禁用态一档暗色。
# ---------------------------------------------------------------------------
PROGRESS_STYLE = "Slim.Horizontal.TProgressbar"


def register_progressbar(st: ttk.Style) -> str:
    """注册共用的细轨进度条样式，返回样式名（两个模式都引用**这一个**名字）。"""
    st.configure(PROGRESS_STYLE, background=ACCENT, troughcolor="#14141a",
                 borderwidth=0, thickness=6,
                 bordercolor="#14141a", lightcolor=ACCENT, darkcolor=ACCENT)
    st.map(PROGRESS_STYLE, background=[("disabled", "#2b2b33")])
    return PROGRESS_STYLE


# ---------------------------------------------------------------------------
# 两个模式**共用**的 ttk 样式（原先两个界面各自注册一份、逐字重复）。
#
# 差异项由各界面另行声明：滚动条配色（两模式不同）。
# ---------------------------------------------------------------------------
def register_common(st: ttk.Style) -> None:
    """注册两个挑选器共用的容器 / 文字色阶 / 按钮 / 输入框 / 复选样式。

    内容与原两处实现逐项相同（缓存侧零视觉变化）；``Sub.TLabel`` 是二级文字色阶，
    此前缓存模式引用了它却**未注册**，会静默回退成 ``TLabel`` 的一级文字色——
    统一注册后两模式的次级标签颜色随之对齐。
    """
    # 容器：内容区 / 面板 / 状态栏 / 分隔线，靠明度差分档
    st.configure("TFrame", background=BG)
    st.configure("Panel.TFrame", background=PANEL)
    st.configure("Bar.TFrame", background=BAR)
    st.configure("Sep.TFrame", background=SEP)
    # 文字三级色阶（不再出现多种杂灰）
    st.configure("TLabel", background=PANEL, foreground=FG, font=(FONT, 9))
    st.configure("Title.TLabel", background=PANEL, foreground=FG,
                 font=(FONT, 12, "bold"))
    st.configure("Section.TLabel", background=PANEL, foreground=FG,
                 font=(FONT, 11, "bold"))
    st.configure("Sub.TLabel", background=PANEL, foreground=SUB, font=(FONT, 9))
    st.configure("Hint.TLabel", background=PANEL, foreground=MUTED, font=(FONT, 8))
    st.configure("Status.TLabel", background=PANEL, foreground=SUB, font=(FONT, 8))
    st.configure("Count.TLabel", background=PANEL, foreground=ACCENT,
                 font=(FONT, 10, "bold"))
    st.configure("BarStatus.TLabel", background=BAR, foreground=SUB, font=(FONT, 8))
    # 网格区空状态提示（底为内容区 BG，故不随 PANEL）
    st.configure("Empty.TLabel", background=BG, foreground=SUB, font=(FONT, 10))
    # 按钮：统一高度，压掉 clam 的立体边框
    st.configure("TButton", font=(FONT, 9), padding=(12, 6),
                 background=BTN, foreground=FG,
                 borderwidth=0, focusthickness=0, relief="flat",
                 bordercolor=BTN, lightcolor=BTN, darkcolor=BTN)
    st.map("TButton",
           background=[("pressed", "#3a3a44"), ("active", "#383842"),
                       ("disabled", PANEL)],
           foreground=[("disabled", MUTED)],
           bordercolor=[("active", BTN)], lightcolor=[("active", BTN)],
           darkcolor=[("active", BTN)])
    # 主按钮
    st.configure("Accent.TButton", font=(FONT, 10, "bold"), padding=(18, 6),
                 background=ACCENT, foreground="#ffffff",
                 borderwidth=0, relief="flat",
                 bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT)
    st.map("Accent.TButton",
           background=[("pressed", "#2f6fd8"), ("active", ACCENT_HOVER),
                       ("disabled", ACCENT_DIM)],
           foreground=[("disabled", "#93a7c4")],
           bordercolor=[("active", ACCENT_HOVER)],
           lightcolor=[("active", ACCENT_HOVER)],
           darkcolor=[("active", ACCENT_HOVER)])
    # 输入框 / 下拉框：显式压掉 clam 的浅色描边
    st.configure("TEntry", fieldbackground=FIELD, foreground=FG, insertcolor=FG,
                 borderwidth=1, padding=5, relief="flat",
                 bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
    st.map("TEntry",
           fieldbackground=[("disabled", PANEL), ("focus", FIELD)],
           foreground=[("disabled", MUTED)],
           bordercolor=[("focus", ACCENT)],
           lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)])
    st.configure("TCombobox", fieldbackground=FIELD, background=FIELD,
                 foreground=FG, arrowcolor=SUB, borderwidth=1, padding=4,
                 relief="flat", bordercolor=BORDER,
                 lightcolor=BORDER, darkcolor=BORDER)
    st.map("TCombobox",
           fieldbackground=[("readonly", FIELD)],
           foreground=[("readonly", FG)],
           bordercolor=[("focus", ACCENT)],
           lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)])
    # 复选框（clam 的 indicator 用 upper/lowerbordercolor 才生效）
    st.configure("TCheckbutton", background=PANEL, foreground=FG,
                 font=(FONT, 9), focusthickness=0, focuscolor=PANEL,
                 indicatorbackground=FIELD, indicatorforeground=ACCENT,
                 upperbordercolor=BORDER, lowerbordercolor=BORDER,
                 indicatorsize=13)
    st.map("TCheckbutton",
           background=[("active", PANEL)],
           foreground=[("active", FG)],
           indicatorbackground=[("selected", ACCENT), ("!selected", FIELD)],
           indicatorforeground=[("selected", "#ffffff")],
           upperbordercolor=[("selected", ACCENT), ("!selected", BORDER)],
           lowerbordercolor=[("selected", ACCENT), ("!selected", BORDER)])


def combobox_listbox_colors(root: tk.Misc) -> None:
    """让下拉列表（Tk 的 Listbox）也跟随深色主题（``option_add`` 需要 root）。"""
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
