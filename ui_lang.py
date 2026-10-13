"""语言切换的界面机制（两个 GUI 共用）。

缓存挑选器与在线挑选器的控件构建方式完全不同（前者是 Canvas 图元虚拟化，
后者是真实的 Frame 网格），但**文案例登记与重刷逻辑**曾经逐字重复了一遍。
这里用 :class:`LangMixin` 收敛公共部分，差异留给子类的两个钩子：

- :meth:`LangMixin._retranslate_dynamic` —— 语言切换时需要重算的**动态**文本
  （如"已选 N 个"、Canvas 角标、占据位符的单元格文字）；
- :meth:`LangMixin._idle_status` —— 空闲时状态栏显示的文案。

另一条约定：状态栏内容以 ``(语言键, 参数)`` 记录并可**按新语言原样重放**，
这样停留在"保存中 3/10"这类动态文本上时切换语言也能正确刷新。
"""
from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from functools import partial
from tkinter import ttk
from typing import Any

import i18n
from i18n import t


class LangMixin:
    """给 Tk 应用混入双语支持。

    子类需要：

    1. ``self.root`` 已存在（窗口）；
    2. 构建界面**之前**调用 :meth:`_init_lang_support`；
    3. 构建界面**之后**调用 :meth:`_enable_lang_support`；
    4. 覆写 :meth:`_retranslate_dynamic`、:meth:`_idle_status`、:meth:`_window_title`。

    登记静态文案请用 :meth:`_T`；文本由运行时函数拥有的控件（计数标签、主按钮、
    状态栏等）**不要**登记，改在 :meth:`_retranslate_dynamic` 里重算，否则两边
    会互相覆盖。
    """

    # 子类的界面/运行态提供的属性；在此声明，供类型检查识别（不产生运行时副作用）
    root: tk.Tk
    busy: bool
    l_status: ttk.Label

    # ---- 生命周期 ----
    def _init_lang_support(self) -> None:
        """初始化文案登记表（须在构建界面之前调用）。"""
        self._tr_widgets: list[tuple[Any, str, dict]] = []
        self._lang_btns: dict[str, ttk.Button] = {}
        self._status_fn: Callable[[], str] | None = None

    def _enable_lang_support(self) -> None:
        """注册语言切换回调（须在构建界面之后调用）。"""
        i18n.on_change(self.retranslate)

    def _disable_lang_support(self) -> None:
        """注销语言切换回调（窗口关闭时调用，避免残留监听者）。"""
        i18n.off_change(self.retranslate)

    # ---- 子类钩子 ----
    def _retranslate_dynamic(self) -> None:
        """语言切换时重算动态文本。默认无操作。"""

    def _idle_status(self) -> str:
        """空闲时状态栏文案（默认通用"就绪"）。"""
        return t("common.ready")

    def _window_title(self) -> str:
        """窗口标题。"""
        return t("app.cache_title")

    # ---- 静态文案登记 ----
    def _T(self, widget, key: str, **kw):
        """给控件设置 i18n 文案并登记，切换语言时统一重刷；返回该控件。"""
        widget.configure(text=t(key, **kw))
        self._tr_widgets.append((widget, key, kw))
        return widget

    # ---- 语言切换按钮 ----
    def _build_lang_switch(self, parent, with_label: bool = False) -> ttk.Frame:
        """在 ``parent`` 右侧创建语言切换分段按钮。

        按钮文案固定为语言自身的显示名（如 ``中文`` / ``English``），不随当前
        语言变化；选中态用 ``SegOn.TButton`` 表达。
        """
        box = ttk.Frame(parent, style=getattr(self, "_lang_box_style", "TFrame"))
        box.pack(side="right", padx=(0, 8) if with_label else (0, 12))
        if with_label:
            self._T(ttk.Label(box, style="Sub.TLabel"), "lang.label").pack(
                side="left", padx=(0, 6))
        for code, label in i18n.available().items():
            btn = ttk.Button(box, text=label, style="Seg.TButton", width=8,
                             command=partial(self.on_lang_change, code))
            btn.pack(side="left", padx=(0, 4))
            self._lang_btns[code] = btn
        self._sync_lang_buttons()
        return box

    def _sync_lang_buttons(self) -> None:
        """同步语言分段按钮的选中样式。"""
        cur = i18n.get_language()
        for code, btn in self._lang_btns.items():
            btn.configure(style="SegOn.TButton" if code == cur else "Seg.TButton")

    def on_lang_change(self, code: str) -> None:
        """切换语言：触发全局刷新并记住偏好。"""
        if code == i18n.get_language():
            return
        i18n.set_language(code)
        i18n.save_language(code)

    # ---- 刷新 ----
    def retranslate(self) -> None:
        """语言切换后重刷窗口全部文案（静态登记 + 子类动态部分 + 状态栏 + 标题）。"""
        for widget, key, kw in self._tr_widgets:
            try:
                widget.configure(text=t(key, **kw))
            except tk.TclError:
                pass          # 控件已销毁（窗口正在关闭）
        self._retranslate_dynamic()
        self._render_status()
        try:
            self.root.title(self._window_title())
        except tk.TclError:
            pass
        self._sync_lang_buttons()

    # ---- 状态栏（可重放）----
    def set_status(self, key: str, **kw) -> None:
        """设置状态栏文案（记下 key+参数，切换语言时按新语言重放）。"""
        self._status_fn = (lambda k=key, a=dict(kw): t(k, **a))
        self.l_status.configure(text=self._status_fn())

    def _render_status(self) -> None:
        """按当前语言重放状态栏；无重放内容且空闲时回到空闲文案。"""
        if self._status_fn is not None:
            self.l_status.configure(text=self._status_fn())
        elif not self.busy:
            self.l_status.configure(text=self._idle_status())