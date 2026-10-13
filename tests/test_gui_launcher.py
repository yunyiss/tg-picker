"""统一启动入口（模式选择）的 GUI 冒烟测试。

覆盖：模式选择界面构建、语言切换实时刷新、选择后进入对应模式的应用。
不联网；缓存模式不需要真实 tdata（仅构建界面，不触发读取）。
"""
from __future__ import annotations

import pytest

import i18n
import launcher


def _texts(widget, out: list[str]) -> list[str]:
    """收集控件树上的可见文字（text 选项）。"""
    try:
        value = widget.cget("text")
        if value:
            out.append(str(value))
    except Exception:
        pass
    for child in widget.winfo_children():
        _texts(child, out)
    return out


def _find_by_text(widget, text: str):
    """按可见文字查找控件（用于定位卡片里的说明标签）。"""
    try:
        if str(widget.cget("text")) == text:
            return widget
    except Exception:
        pass
    for child in widget.winfo_children():
        found = _find_by_text(child, text)
        if found is not None:
            return found
    return None


@pytest.mark.gui
def test_launcher_shows_modes_and_switches_language(tk_root):
    win = launcher.Launcher(tk_root)
    assert win.app is None, "未选择模式前不应装载任何应用"

    i18n.set_language("zh")
    tk_root.update_idletasks()
    texts = _texts(tk_root, [])
    assert i18n.t("launch.mode.api_title") in texts, texts
    assert i18n.t("launch.mode.cache_title") in texts, texts

    i18n.set_language("en")
    tk_root.update_idletasks()
    texts = _texts(tk_root, [])
    assert "API mode" in texts, texts
    assert "Local cache mode" in texts, texts

    i18n.set_language("zh")
    win._on_close()


@pytest.mark.gui
def test_launcher_enters_cache_mode(tk_root):
    import cache_picker as cp

    win = launcher.Launcher(tk_root)
    win.choose("cache")
    assert isinstance(win.app, cp.CacheApp), "选择缓存模式后应装载 CacheApp"
    win.app._on_close()


@pytest.mark.gui
def test_launcher_enters_api_mode(tk_root):
    import tg_picker as tp

    win = launcher.Launcher(tk_root)
    win.choose("api")
    assert isinstance(win.app, tp.App), "选择 API 模式后应装载 App"
    win.app._on_close()


@pytest.mark.gui
def test_launcher_whole_card_is_clickable(tk_root):
    """整张卡片（含说明文字）都可点，且悬停样式存在。"""
    import cache_picker as cp

    i18n.set_language("zh")
    win = launcher.Launcher(tk_root)
    tk_root.update_idletasks()

    desc = _find_by_text(tk_root, i18n.t("launch.mode.cache_desc"))
    assert desc is not None, "找不到缓存模式的说明文字"
    assert desc.bind("<Button-1>"), "说明文字上未绑定点击：卡片不是整块可点"

    # 悬停样式必须已定义，否则 configure(style=...) 会抛 TclError
    desc.event_generate("<Enter>")
    tk_root.update_idletasks()
    desc.event_generate("<Leave>")
    tk_root.update_idletasks()

    # 点说明文字即进入缓存模式（而非只有一小块按钮能点）
    desc.event_generate("<Button-1>")
    assert isinstance(win.app, cp.CacheApp), "点说明文字应进入缓存模式"
    win.app._on_close()