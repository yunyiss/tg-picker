"""统一启动入口（模式选择）。

把原先两个**独立入口**——在线挑选器（``tg_picker.py``，API 模式）与缓存
挑选器（``cache_picker.py``，本地缓存模式）——合并为一个启动器：双击启动
后先展示模式选择界面，用户选定后再进入该模式的完整功能流程。

实现要点（为什么能这样合并）
    两种模式的应用类（``tg_picker.App`` / ``cache_picker.CacheApp``）都接受
    一个**既有的 ``tk.Tk`` 根窗口**并在其中构建界面，因此这里复用同一个根
    窗口：先画出模式选择界面，选择后销毁这些控件，再把选中的模式构建进同一
    根窗口。这样避免了同进程出现多个 Tk 解释器（易触发 Tcl 初始化错误），
    也**完全不改动**任何一个模式的既有逻辑——只是把"谁来建窗口"这一步收敛。

    两个模式模块都**延迟导入**：只用本地缓存模式时不会拉起 telethon，只用
    API 模式时不会拉起 tdata 解密链，与原有两个入口的依赖边界保持一致。

语言切换的通用机制继承自 :class:`ui_lang.LangMixin`（与两个模式界面同一套）。
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

import i18n
import ui_kit
from i18n import t
from ui_kit import FONT
from ui_lang import LangMixin

HERE = Path(__file__).resolve().parent

# 视觉规范：沿用缓存挑选器的深灰配色，保证与两个模式界面观感一致。
BG = "#1b1b1f"            # 内容区
CARD = "#26262c"          # 模式卡片
CARD_HOVER = "#30303a"    # 卡片悬停
ACCENT = "#3b82f6"        # 强调色
FG = "#e8e8ec"            # 一级文字
SUB = "#9aa0a8"           # 二级文字
MUTED = "#7d838b"         # 三级文字
BTN = "#2e2e36"           # 段按钮底色


class Launcher(LangMixin):
    """模式选择窗口：一个根窗口，选定后装载对应模式的应用。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.app = None            # 选定后装载的模式应用实例（未选定时为 None）
        self.busy = False          # LangMixin._render_status 需要该属性
        i18n.init()
        self._init_lang_support()

        root.title(t("app.launcher_title"))
        root.geometry("760x460")
        root.minsize(680, 420)
        root.configure(bg=BG)

        self._build_style()
        # 先占好底部（side=bottom），中间的模式区再填满剩余空间
        self._build_header()
        self._build_footer()
        self._build_modes()

        self._enable_lang_support()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- i18n（差异部分；通用机制见 ui_lang.LangMixin）----------
    def _window_title(self) -> str:
        return t("app.launcher_title")

    def _idle_status(self) -> str:
        return t("launch.hint")

    def _build_style(self) -> None:
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=FG, font=(FONT, 9))
        st.configure("Title.TLabel", background=BG, foreground=FG,
                     font=(FONT, 17, "bold"))
        st.configure("Sub.TLabel", background=BG, foreground=SUB, font=(FONT, 10))
        st.configure("Hint.TLabel", background=BG, foreground=MUTED, font=(FONT, 8))
        # 模式卡片：**整块可点**，悬停时卡片与文字一起提亮
        st.configure("Card.TFrame", background=CARD)
        st.configure("CardHover.TFrame", background=CARD_HOVER)
        st.configure("CardTitle.TLabel", background=CARD, foreground=FG,
                     font=(FONT, 12, "bold"))
        st.configure("CardHoverTitle.TLabel", background=CARD_HOVER, foreground=FG,
                     font=(FONT, 12, "bold"))
        st.configure("CardDesc.TLabel", background=CARD, foreground=SUB, font=(FONT, 9))
        st.configure("CardHoverDesc.TLabel", background=CARD_HOVER, foreground=SUB,
                     font=(FONT, 9))
        st.configure("CardEnter.TLabel", background=CARD, foreground=ACCENT,
                     font=(FONT, 9, "bold"))
        st.configure("CardHoverEnter.TLabel", background=CARD_HOVER, foreground=ACCENT,
                     font=(FONT, 9, "bold"))
        # 语言切换分段按钮（与两个模式界面同一表达方式）
        st.configure("Seg.TButton", font=(FONT, 9), padding=(10, 3),
                     background=BTN, foreground=SUB,
                     borderwidth=0, relief="flat",
                     bordercolor=BTN, lightcolor=BTN, darkcolor=BTN)
        st.map("Seg.TButton",
               background=[("active", CARD_HOVER)], foreground=[("active", FG)])
        st.configure("SegOn.TButton", font=(FONT, 9, "bold"), padding=(10, 3),
                     background=ACCENT, foreground="#ffffff",
                     borderwidth=0, relief="flat",
                     bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT)
        st.map("SegOn.TButton", background=[("active", "#4b8ef7")])

    # ---------- 界面 ----------
    def _build_header(self) -> None:
        bar = ttk.Frame(self.root, padding=(20, 16, 20, 0))
        bar.pack(fill="x")
        self._build_lang_switch(bar)          # 语言切换置顶右侧
        self._T(ttk.Label(bar, style="Title.TLabel"),
                "app.launcher_title").pack(side="left")

        head = ttk.Frame(self.root, padding=(20, 8, 20, 0))
        head.pack(fill="x")
        self._T(ttk.Label(head, style="Sub.TLabel"), "launch.heading").pack(side="left")

    def _build_footer(self) -> None:
        bar = ttk.Frame(self.root, padding=(20, 0, 20, 14))
        bar.pack(fill="x", side="bottom")
        self.l_status = ttk.Label(bar, style="Hint.TLabel", text=self._idle_status())
        self.l_status.pack(side="left")

    def _build_modes(self) -> None:
        row = ttk.Frame(self.root, padding=(20, 18, 20, 0))
        row.pack(fill="both", expand=True)
        row.columnconfigure(0, weight=1, uniform="mode")
        row.columnconfigure(1, weight=1, uniform="mode")
        row.rowconfigure(0, weight=1)

        self._mode_card(row, 0, "api",
                        "launch.mode.api_title", "launch.mode.api_desc")
        self._mode_card(row, 1, "cache",
                        "launch.mode.cache_title", "launch.mode.cache_desc")

    def _mode_card(self, parent, col: int, mode: str,
                   title_key: str, desc_key: str) -> None:
        """一张**整体可点击**的模式卡片：标题 + 说明 + 进入提示。

        整张卡片（标题、说明、提示及周围留白）都能点，悬停时整块提亮并显示
        手型光标——避免"只有一小块按钮能点"的误解。
        """
        card = ttk.Frame(parent, style="Card.TFrame", padding=(20, 22))
        card.grid(row=0, column=col, sticky="nsew",
                  padx=(0, 10) if col == 0 else (10, 0))
        card.columnconfigure(0, weight=1)
        card.rowconfigure(1, weight=1)          # 说明占满中间，提示落到卡片底部

        title = self._T(ttk.Label(card, style="CardTitle.TLabel"), title_key)
        title.grid(row=0, column=0, sticky="w")
        desc = self._T(ttk.Label(card, style="CardDesc.TLabel",
                                 wraplength=286, justify="left"), desc_key)
        desc.grid(row=1, column=0, sticky="nw", pady=(10, 0))
        enter = self._T(ttk.Label(card, style="CardEnter.TLabel"), "launch.mode.enter")
        enter.grid(row=2, column=0, sticky="w", pady=(14, 0))

        # 整张卡片统一可点 + 悬停高亮（卡片底与各级文字同步换色）
        parts = {"card": card, "title": title, "desc": desc, "enter": enter}
        for w in parts.values():
            w.bind("<Button-1>", lambda e, m=mode: self.choose(m))
            w.bind("<Enter>", lambda e, p=parts: self._hover(p, True))
            w.bind("<Leave>", lambda e, p=parts: self._hover(p, False))
            try:
                w.configure(cursor="hand2")
            except tk.TclError:
                pass

    def _hover(self, parts: dict, on: bool) -> None:
        """切换整张卡片的悬停配色（卡片底 + 标题/说明/提示一起）。"""
        tag = "Hover" if on else ""
        parts["card"].configure(style=f"Card{tag}.TFrame")
        parts["title"].configure(style=f"Card{tag}Title.TLabel")
        parts["desc"].configure(style=f"Card{tag}Desc.TLabel")
        parts["enter"].configure(style=f"Card{tag}Enter.TLabel")

    # ---------- 选择模式 ----------
    def choose(self, mode: str) -> None:
        """销毁模式选择界面，把选定的模式构建进**同一根窗口**。

        ``mode`` 取 ``"api"``（在线挑选器）或 ``"cache"``（缓存挑选器）。
        """
        self._disable_lang_support()          # 选择界面即将销毁，先注销其刷新回调
        for child in self.root.winfo_children():
            child.destroy()
        for seq in ("<Control-a>", "<Escape>"):
            try:
                self.root.unbind(seq)
            except tk.TclError:
                pass

        if mode == "cache":
            import cache_picker
            self.app = cache_picker.CacheApp(self.root)
        else:
            import tg_picker
            self.app = tg_picker.App(self.root)

    def _on_close(self) -> None:
        """关闭窗口（尚未选择模式时）。"""
        self._disable_lang_support()
        self.root.destroy()


def main() -> None:
    """入口：建窗口并进入 Tk 主循环。"""
    ui_kit.enable_file_log(HERE / "logs" / "tg_picker.log")
    root = tk.Tk()
    Launcher(root)
    root.mainloop()


if __name__ == "__main__":
    main()