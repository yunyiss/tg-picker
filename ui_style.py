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
