"""三个界面共用的 ttk 样式构造。

三个界面（``launcher`` / ``cache_picker`` / ``tg_picker``）的配色**故意不同**，
因此这里只共享**构造逻辑**（颜色由调用方传入），不共享色值——与 ``ui_kit``
共享字体、``ui_lang`` 共享语言机制是同一思路。

要点（本项目踩过的坑）：clam 主题会自绘浅色 chrome，只设 ``background`` /
``foreground`` 压不住；下面这些样式都显式覆盖了 ``bordercolor`` /
``lightcolor`` / ``darkcolor``。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ui_kit import FONT


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
