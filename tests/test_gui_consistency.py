"""两模式 UI 结构一致性自检（构建真实窗口后逐项比对）。

对应用户"两模式全流程走查、确认所有可统一的细节均已对齐"的要求：除模式特有的
条目外，操作条版式、进度/状态位置、分区标题、空状态提示在两模式下必须完全一致。
"""
from __future__ import annotations

import datetime
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

import pytest

import cache_picker as cp
import cache_source as cs
import i18n
import tg_picker as tp
import ui_kit
import ui_style

TEXTUAL_CLASSES = ("TButton", "TLabel", "TCheckbutton", "Checkbutton")


class _FakeMsg:
    """假消息对象：仅用于构造一条 MediaItem。"""

    media = None


def _side(widget) -> str:
    """控件在其父容器里的 pack 方向；未使用 pack 时返回空串。"""
    return str(widget.pack_info()["side"]) if widget.winfo_manager() == "pack" else ""


def _texts(frame, side: str) -> list[str]:
    """取该容器里指定 pack 方向、且 text 非空的子控件文案（创建顺序）。

    空文本控件（如空闲时留空的视图概览标签）不计入——否则会干扰"左侧控件顺序"断言。
    """
    out: list[str] = []
    for w in frame.winfo_children():
        if _side(w) != side or w.winfo_class() not in TEXTUAL_CLASSES:
            continue
        try:
            text = str(w.cget("text"))
        except tk.TclError:
            continue
        if text:
            out.append(text)
    return out


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

    # 第 2 行左侧：计数 + 全选 / 反选 / 清除选择（两模式同序）
    expect_sel = [i18n.t("common.count_none"), i18n.t("common.select_all"),
                  i18n.t("common.invert"), i18n.t("common.clear_sel")]
    assert _texts(c_row2, "left") == expect_sel
    assert _texts(o_row2, "left") == expect_sel

    # 视图概览标签：两模式同为 Status.TLabel、同宽，且与「已选」同处操作条第 2 行
    # （空闲时文本为空，故不计入上面的左侧文案序列）
    for app in (cache_app, online_app):
        assert str(app.l_overview.cget("style")) == "Status.TLabel"
        assert int(app.l_overview.cget("width")) == 18
        assert app.l_overview.master is app.l_count.master, "概览应与计数同一行"

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


def test_card_palette_is_shared():
    """两模式的卡片色板必须来自同一处——这是选中态视觉一致的前提。"""
    assert cp.CARD == tp.CARD == ui_style.CARD
    assert cp.CARD_HOVER == tp.CARD_HOVER == ui_style.CARD_HOVER
    assert cp.CARD_SEL == tp.CARD_SEL == ui_style.CARD_SEL
    assert cp.CORNER == tp.CORNER, "两模式卡片圆角半径应一致"


def test_card_geometry_is_shared():
    """两模式的卡片几何必须**同源**（尺寸 / 间隙 / 图片方框 / 缩略图 / 圆角）。

    此前 API 模式自持一套尺寸（卡片 168x205、缩略图 150、无图片方框），与缓存模式
    （164x196、140、144 方框）不一致，观感上 API 卡片偏大、缩略图偏挤。现统一由
    ui_kit 提供，本用例防止任何一侧再写回本地字面量造成漂移。
    """
    assert (cp.CARD_W, cp.CARD_H) == (tp.CARD_W, tp.CARD_H) \
        == (ui_kit.CARD_W, ui_kit.CARD_H)
    assert cp.IMG_BOX == tp.IMG_BOX == ui_kit.IMG_BOX
    assert cp.THUMB == tp.THUMB == ui_kit.THUMB
    assert cp.GAP == tp.GAP == ui_kit.GAP
    # API 的单元格画布即卡片本身；缓存模式的步距 = 卡片 + 间隙，两者步距一致
    assert (tp.CELL_W, tp.CELL_H) == (cp.CARD_W, cp.CARD_H)
    assert (cp.CELL_W, cp.CELL_H) == (cp.CARD_W + cp.GAP, cp.CARD_H + cp.GAP)


@pytest.mark.gui
def test_selected_count_label_matches(two_apps):
    """「已选 N 个」计数：两模式同款样式、同位置，且选中后同步更新。"""
    cache_app, online_app = two_apps
    for app in (cache_app, online_app):
        assert str(app.l_count.cget("style")) == "Count.TLabel"
        assert int(app.l_count.cget("width")) == 26
        assert str(app.l_count.cget("text")) == i18n.t("common.count_none")
    assert online_app.l_count.master is online_app.b_dl.master, "计数应与主按钮同一行"

    it = tp.MediaItem(msg_id=1, date=datetime.datetime(2026, 10, 10), kind="photo",
                      size=2048, file_name=None, mime="image/jpeg", msg=_FakeMsg())
    online_app.items.append(it)
    online_app.add_item_cell(it)
    assert online_app.l_count.cget("text") == i18n.t("common.count_none")
    assert it.cell.itemcget(it.badge, "state") == "hidden", "未选中不应显示徽标"
    assert it.cell.winfo_reqwidth() == tp.CELL_W, "单元格画布应保持固定尺寸"
    online_app.set_selected(it, True)
    online_app.update_count()
    assert str(online_app.l_count.cget("text")) == i18n.t(
        "common.count_selected", n=1, size=tp.human_size(2048))
    # 选中态视觉与缓存模式同一套：圆角卡片 + 强调色描边 + 淡蓝底 + 右上角圆形对勾徽标
    assert it.cell.itemcget(it.card, "outline") == ui_style.ACCENT
    assert it.cell.itemcget(it.card, "fill") == ui_style.CARD_SEL
    assert it.cell.itemcget(it.badge, "state") == "normal"
    assert it.cell.itemcget(it.badge_txt, "text") == "✓"
    # 卡片底是圆角多边形（不是直角矩形），与缓存模式同半径
    assert len(it.cell.coords(it.card)) > 8, "卡片底应为圆角多边形而非矩形"


@pytest.mark.gui
def test_api_thumb_items_switch(tk_root):
    """缩略图到位 / 取不到时，Canvas 图元正确切换（替换原先的 Label 逻辑）。"""
    from PIL import Image

    i18n.set_language("zh")
    app = tp.App(tk_root)
    it = tp.MediaItem(msg_id=7, date=datetime.datetime(2026, 10, 10), kind="photo",
                      size=100, file_name=None, mime="image/jpeg", msg=_FakeMsg())
    app.items.append(it)
    app.add_item_cell(it)
    app.by_id[it.msg_id] = it

    app._msg_thumb(("thumb", it.msg_id, Image.new("RGB", (60, 40), "red")))
    assert it.cell.itemcget(it.img, "state") == "normal"
    assert it.cell.itemcget(it.img_txt, "state") == "hidden"
    assert it.photo is not None, "必须持有 PhotoImage 引用"

    app._msg_thumb(("thumb", it.msg_id, None))
    assert it.thumb_failed is True
    assert it.cell.itemcget(it.img, "state") == "hidden"
    assert it.cell.itemcget(it.img_txt, "state") == "normal"
    assert it.cell.itemcget(it.img_txt, "text") == i18n.t("cell.no_preview")
    app._on_close()


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


@pytest.mark.gui
def test_online_grid_centered_with_spacer_columns(tk_root, monkeypatch):
    """API 网格靠两侧对称 spacer 列居中（与缓存模式一致），不留右侧大块空位。

    仅修正列数只解决"溢出"，取整余量还会全堆在右边；这里断言两侧各有一个 weight=1
    的空列，且列数变小后旧的右侧 spacer 权重被清零（否则会撑歪单元格间距）。
    """
    i18n.set_language("zh")
    app = tp.App(tk_root)
    try:
        app.columns = 0
        monkeypatch.setattr(app.canvas, "winfo_width", lambda: 1200)
        app.relayout()
        wide = app.columns
        assert wide >= 1
        assert int(app.inner.grid_columnconfigure(0)["weight"]) == 1, "左侧应有 spacer 列"
        assert int(app.inner.grid_columnconfigure(wide + 1)["weight"]) == 1, "右侧应有 spacer 列"

        monkeypatch.setattr(app.canvas, "winfo_width", lambda: 700)
        app.relayout()
        narrow = app.columns
        assert narrow < wide, "画布变窄后列数应减少"
        assert int(app.inner.grid_columnconfigure(wide + 1)["weight"]) == 0, \
            "旧的右侧 spacer 权重应被清零"
        assert int(app.inner.grid_columnconfigure(narrow + 1)["weight"]) == 1
    finally:
        app._on_close()


def _cache_item(rel: str) -> cp.MediaItem:
    """构造一条最小的缓存媒体（只为校验"下标→身份"用，不碰磁盘）。"""
    return cp.MediaItem(entry=cs.CacheEntry(
        cache_path=Path("dummy"), rel=rel, label="cache", mtime=1_700_000_000.0,
        enc_size=100, kind="image", ext=".jpg", size=100, thumb_key=rel))


@pytest.mark.gui
def test_cache_shift_click_after_list_shrinks_does_not_crash(tk_root):
    """回归（N6）：列表变短后 ``last_click`` 仍指向旧下标时，Shift 连选不得越界。

    ``last_click`` 记的是"条目在**当时**列表里的下标"，而筛选 / 排序 / 重扫都会换掉
    ``self.items``（条数还可能变少）。旧实现直接 ``range(lo, hi+1)`` 取
    ``self.items[i]``，越界会抛 ``IndexError``（修复前本用例可稳定复现）。
    """
    i18n.set_language("zh")
    app = cp.CacheApp(tk_root)
    app.items = [_cache_item(f"r{i}") for i in range(5)]
    app.all_items = list(app.items)
    app.columns = 5
    app._grid_x0 = 10
    app.last_click = 999                       # 遗留自更长的列表
    app._on_click(SimpleNamespace(x=10, y=30, state=0x0001))    # 修复前：IndexError
    assert all(str(it.entry.rel) in app.selected for it in app.items), "应选中所点条目"

    app.on_filter_change()                     # 换列表（筛选 / 排序）
    assert app.last_click is None, "列表重建后 last_click 应失效"
    app._on_close()


@pytest.mark.gui
def test_online_shift_click_after_reload_does_not_crash(tk_root, monkeypatch):
    """回归（N6）：API 模式重新加载（旧下标作废）后，Shift 连选不得越界。"""
    i18n.set_language("zh")
    app = tp.App(tk_root)
    items = [tp.MediaItem(msg_id=i, date=datetime.datetime(2026, 10, 10), kind="photo",
                          size=1, file_name=None, mime="image/jpeg", msg=_FakeMsg())
             for i in range(5)]
    for it in items:
        app.add_item_cell(it)
    app.items = list(items)
    app.last_click = 999                       # 遗留自上一批（条数更多）
    app.on_cell_click(SimpleNamespace(state=0x0001), items[0])   # 修复前：IndexError
    assert items[0].selected

    monkeypatch.setattr(app, "_do_load", lambda more: None)
    app.on_load()                              # 重新加载：旧下标必须作废
    assert app.last_click is None, "重新加载后 last_click 应失效"
    app._on_close()
