"""两模式 UI 结构一致性自检（构建真实窗口后逐项比对）。

对应用户"两模式全流程走查、确认所有可统一的细节均已对齐"的要求：除模式特有的
条目外，操作条版式、进度/状态位置、分区标题、空状态提示在两模式下必须完全一致。
"""
from __future__ import annotations

import datetime
import tkinter as tk
from types import SimpleNamespace

import pytest

import cache_picker as cp
import i18n
import tg_picker as tp

TEXTUAL_CLASSES = ("TButton", "TLabel", "TCheckbutton", "Checkbutton")


class _FakeMsg:
    """假消息对象：仅用于构造一条 MediaItem。"""

    media = None


def _side(widget) -> str:
    """控件在其父容器里的 pack 方向；未使用 pack 时返回空串。"""
    return str(widget.pack_info()["side"]) if widget.winfo_manager() == "pack" else ""


def _texts(frame, side: str) -> list[str]:
    """取该容器里指定 pack 方向、且带 text 选项的子控件文案（创建顺序）。"""
    return [
        str(w.cget("text"))
        for w in frame.winfo_children()
        if _side(w) == side and w.winfo_class() in TEXTUAL_CLASSES
    ]


def _buttons(frame, side: str) -> list:
    return [w for w in frame.winfo_children()
            if w.winfo_class() == "TButton" and _side(w) == side]


def _section_titles(widget, out: list[str]) -> None:
    """递归收集分区标题（style 为 Section.TLabel 的标签）。"""
    for child in widget.winfo_children():
        if child.winfo_class() == "TLabel":
            try:
                if str(child.cget("style")) == "Section.TLabel":
                    out.append(str(child.cget("text")))
            except tk.TclError:
                pass
        _section_titles(child, out)


@pytest.fixture()
def two_apps(tk_root):
    """并排建两个模式窗口（各自独立顶层窗口），退出时统一关闭。"""
    i18n.set_language("zh")                 # 断言文案前固定语言
    other = tk.Toplevel(tk_root.master)
    cache_app = cp.CacheApp(tk_root)
    online_app = tp.App(other)
    yield cache_app, online_app
    for app in (cache_app, online_app):
        try:
            app._on_close()
        except tk.TclError:
            pass


@pytest.mark.gui
def test_action_bar_layout_matches_between_modes(two_apps):
    """操作条版式一致：第 1 行=保存到；第 2 行=选择操作（左）+ 主按钮/取消（右）。"""
    cache_app, online_app = two_apps
    c_row1, c_row2 = cache_app.e_out.master, cache_app.b_save.master
    o_row1, o_row2 = online_app.e_out.master, online_app.b_dl.master

    # 第 1 行：浏览 / 打开目录，顺序与文案两模式完全一致
    expect_row1 = [i18n.t("common.browse"), i18n.t("common.open_dir")]
    assert _texts(c_row1, "left")[-2:] == expect_row1
    assert _texts(o_row1, "left")[-2:] == expect_row1

    # 第 2 行左侧：全选 / 反选 / 清除选择（缓存模式前面多一个计数标签，属模式差异）
    expect_sel = [i18n.t("common.select_all"), i18n.t("common.invert"),
                  i18n.t("common.clear_sel")]
    assert _texts(c_row2, "left")[-3:] == expect_sel
    assert _texts(o_row2, "left") == expect_sel

    # 第 2 行右侧：最右为「取消」，其左为主按钮（强调样式）
    for row in (c_row2, o_row2):
        right = _buttons(row, "right")
        assert str(right[0].cget("text")) == i18n.t("common.cancel"), "最右应为取消"
        assert str(right[1].cget("style")) == "Accent.TButton", "取消左侧应是主按钮"


@pytest.mark.gui
def test_progress_and_status_positions_match(two_apps):
    """进度条在左、状态在右，且两者同属一个状态栏容器。"""
    cache_app, online_app = two_apps
    cache_app._show_progress()               # 缓存模式进度条按需出现
    try:
        for app in (cache_app, online_app):
            assert app.pb.master is app.l_status.master, "进度条与状态应同一容器"
            assert _side(app.l_status) == "right"
            assert _side(app.pb) == "left"
    finally:
        cache_app._hide_progress()


@pytest.mark.gui
def test_section_titles_match(two_apps):
    """两模式都有 ①② 两个同款（Section.TLabel）分区标题。"""
    cache_app, online_app = two_apps
    for app in (cache_app, online_app):
        titles: list[str] = []
        _section_titles(app.root, titles)
        assert len(titles) == 2, f"分区标题数量不符：{titles}"
        assert titles[0].startswith("①") and titles[1].startswith("②"), titles


@pytest.mark.gui
def test_empty_hint_matches(two_apps):
    """空状态提示：两模式都存在、同为 Empty.TLabel、居中显示；有内容即隐藏。"""
    cache_app, online_app = two_apps
    for app in (cache_app, online_app):
        assert str(app.l_empty.cget("style")) == "Empty.TLabel"
        assert app.l_empty.winfo_manager() == "place", "无内容时应显示空态提示"
    cache_app.items.append(object())
    cache_app._update_empty_hint()
    assert cache_app.l_empty.winfo_manager() == "", "有内容后应隐藏空态提示"
    cache_app.items.pop()
    cache_app._update_empty_hint()
    assert cache_app.l_empty.winfo_manager() == "place"


@pytest.mark.gui
def test_online_shift_click_selects_range(tk_root):
    """Shift+单击按当前顺序连选，语义与缓存模式一致。"""
    i18n.set_language("zh")
    app = tp.App(tk_root)
    items = []
    for i in range(3):
        it = tp.MediaItem(msg_id=100 + i, date=datetime.datetime(2026, 10, 10),
                          kind="photo", size=1000, file_name=None,
                          mime="image/jpeg", msg=_FakeMsg())
        app.items.append(it)
        app.add_item_cell(it)
        items.append(it)
    click = SimpleNamespace(state=0)
    shift_click = SimpleNamespace(state=0x0001)

    app.on_cell_click(click, items[0])                    # 单击 → 选中第 1 条
    assert items[0].selected and not items[1].selected
    app.on_cell_click(shift_click, items[2])              # Shift 连选 → 0..2 全选
    assert all(it.selected for it in items)
    app.on_cell_click(shift_click, items[1])              # Shift 点已选项 → 该段取消
    assert items[0].selected and not items[1].selected and not items[2].selected
    app._on_close()


@pytest.mark.gui
def test_cache_cancel_button_state_and_event(tk_root):
    """缓存模式取消按钮：空闲禁用、运行期可用，点击置位取消事件并给出反馈。"""
    i18n.set_language("zh")
    app = cp.CacheApp(tk_root)
    assert str(app.b_cancel.cget("state")) == "disabled"
    app.set_busy(True)
    assert str(app.b_cancel.cget("state")) == "normal"
    app.on_cancel()
    assert app.cancel.is_set(), "点击取消应置位取消事件"
    assert app.l_status.cget("text") == i18n.t("common.cancel_req")
    app.set_busy(False)
    assert str(app.b_cancel.cget("state")) == "disabled"
    app._on_close()
