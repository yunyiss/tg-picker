"""首次启动：让用户选择「数据目录」存放位置（便携 / 用户目录）。

面向"非安装版"的使用习惯：有些人用完就把工具删掉，所以首次启动先问清楚数据放哪，
并说明"删哪个目录即可清除全部痕迹"。选择会被记住（见 :mod:`app_paths`），下次不再询问。

统一入口 :mod:`launcher` 已把这一步做在**同一个窗口**内（选完数据位置即切回模式选择），
本模块仅在**直接运行 ``tg_picker.py`` / ``cache_picker.py``** 时兜底弹一个独立对话框。
"""
from __future__ import annotations

import tkinter as tk

import app_paths
import i18n
from i18n import t
from ui_kit import FONT
from ui_style import ACCENT, BG, CARD, CARD_HOVER, FG, SUB

# 配色与启动器、两个模式界面共用 ui_style 的同一套色板（原先此处另抄了一份，
# 有随 ui_style 调整而漂移的风险，故收敛掉）。


def ensure_choice(root: tk.Tk) -> None:
    """首次启动（尚无已保存选择）时弹窗询问并落盘；否则直接解析数据目录。"""
    if not app_paths.needs_choice():
        app_paths.ensure_resolved()
        return
    i18n.init()                    # 对话框文案按当前语言（默认中文）
    chosen = _ask(root)
    app_paths.set_mode(chosen or app_paths.PORTABLE, persist=True)


def _ask(root: tk.Tk) -> str | None:
    """弹出选择对话框；返回 ``app_paths.PORTABLE`` / ``app_paths.USERDIR``，
    用户直接关闭窗口则返回 ``None``（届时按便携处理）。"""
    result: dict[str, str | None] = {"mode": None}

    win = tk.Toplevel(root)
    win.title(t("firstrun.title"))
    win.configure(bg=BG)
    win.resizable(False, False)
    win.transient(root)
    win.protocol("WM_DELETE_WINDOW", win.destroy)

    tk.Label(win, text=t("firstrun.title"), bg=BG, fg=FG,
             font=(FONT, 13, "bold")).pack(anchor="w", padx=20, pady=(16, 4))
    tk.Label(win, text=t("firstrun.intro"), bg=BG, fg=SUB, font=(FONT, 9),
             wraplength=430, justify="left").pack(anchor="w", padx=20, pady=(0, 14))

    def choose(mode: str) -> None:
        result["mode"] = mode
        win.destroy()

    _option(win, t("firstrun.portable_title"), t("firstrun.portable_desc"), ACCENT,
            lambda: choose(app_paths.PORTABLE))
    _option(win, t("firstrun.userdir_title"), t("firstrun.userdir_desc"), CARD,
            lambda: choose(app_paths.USERDIR))

    win.update_idletasks()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    x = max(0, (sw - win.winfo_width()) // 2)
    y = max(0, (sh - win.winfo_height()) // 3)
    win.geometry(f"+{x}+{y}")

    try:
        win.wait_visibility()
        win.grab_set()
    except tk.TclError:
        pass
    root.wait_window(win)
    return result["mode"]


def _option(parent: tk.Widget, title: str, desc: str, accent: str,
            command) -> None:
    """一整块可点的选项卡片（标题 + 说明），悬停时提亮。"""
    row = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=accent,
                   highlightcolor=accent, cursor="hand2")
    row.pack(fill="x", padx=20, pady=(0, 12))

    title_lbl = tk.Label(row, text=title, bg=CARD, fg=FG, font=(FONT, 10, "bold"),
                         anchor="w", cursor="hand2")
    title_lbl.pack(fill="x", padx=14, pady=(10, 2))
    desc_lbl = tk.Label(row, text=desc, bg=CARD, fg=SUB, font=(FONT, 8),
                        anchor="w", justify="left", wraplength=400, cursor="hand2")
    desc_lbl.pack(fill="x", padx=14, pady=(0, 10))

    for w in (row, title_lbl, desc_lbl):
        w.bind("<Button-1>", lambda e: command())
        w.bind("<Enter>", lambda e: _set_bg((row, title_lbl, desc_lbl), CARD_HOVER))
        w.bind("<Leave>", lambda e: _set_bg((row, title_lbl, desc_lbl), CARD))


def _set_bg(widgets: tuple[tk.Widget, ...], color: str) -> None:
    for w in widgets:
        w.configure(bg=color)
