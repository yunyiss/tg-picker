"""缓存挑选器 GUI 端到端测试：虚拟化渲染、滚动性能、选择操作、语言切换。

需要真实 tdata（``tdata_dir``）与显示器（标记 ``gui``）。
与旧脚本的关键差别：**用真实断言判定成败**（旧脚本只写日志、退出码恒为 0，
失败会被静默当成通过）。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

import cache_picker as cp
import i18n

# 阈值：单次可视区刷新耗时上限（毫秒）与图元池上限
MAX_REFRESH_MS = 60
MAX_POOL = 120
MIN_VISIBLE_CELLS = 8


@pytest.mark.gui
def test_cache_gui_renders_scrolls_and_switches_language(tdata_dir: Path, tk_root):
    app = cp.CacheApp(tk_root)

    problems: list[str] = []
    rec: dict[str, object] = {}
    timings: list[float] = []
    original_refresh = app.refresh_view

    def timed_refresh() -> None:
        t0 = time.perf_counter()
        original_refresh()
        timings.append((time.perf_counter() - t0) * 1000)

    app.refresh_view = timed_refresh           # 统计真实刷新开销
    state = {"phase": 0, "ticks": 0}

    def poll() -> None:
        state["ticks"] += 1
        if state["ticks"] > 1500:
            problems.append("超时：数据/缩略图未在预期内就绪")
            tk_root.quit()
            return

        if state["phase"] == 0:                # 等缓存读完
            if not app.all_items:
                tk_root.after(200, poll)
                return
            rec["total"] = len(app.all_items)
            rec["images"] = sum(1 for i in app.all_items if i.kind == "image")
            rec["videos"] = sum(1 for i in app.all_items if i.kind == "video")
            rec["view"] = len(app.items)
            rec["columns"] = app.columns
            state["phase"] = 1
            tk_root.after(600, poll)
            return

        if state["phase"] == 1:                # 等缩略图铺开
            if len(app.thumb_mem) < 12:
                tk_root.after(200, poll)
                return
            rec["pool"] = len(app.cell_pool)
            rec["visible"] = sum(1 for c in app.cell_pool if c["slot"] is not None)
            for i in range(min(3, len(app.items))):
                app.selected.add(str(app.items[i].entry.rel))
            app.update_count()
            rec["count_zh"] = app.l_count.cget("text")
            state["phase"] = 2
            tk_root.after(300, poll)
            return

        if state["phase"] == 2:                # 模拟快速滚轮
            timings.clear()
            pool_before = len(app.cell_pool)
            t0 = time.perf_counter()
            for _ in range(40):
                app._scroll_units(3)
                app.refresh_view()
                tk_root.update_idletasks()
            rec["scroll_ms"] = (time.perf_counter() - t0) * 1000
            rec["pool_before"] = pool_before
            rec["pool_after"] = len(app.cell_pool)
            app.canvas.yview_moveto(0)
            app.refresh_view()
            state["phase"] = 3
            tk_root.after(200, poll)
            return

        if state["phase"] == 3:                # 大跨度跳到底部 + 切视频
            app.canvas.yview_moveto(1.0)
            app.refresh_view()
            rec["top_idx"] = min(app.cells_by_slot) if app.cells_by_slot else -1
            rec["items_len"] = len(app.items)
            app.v_kind.set("video")
            app.on_filter_change()
            rec["videos_filtered"] = len(app.items)
            state["phase"] = 4
            tk_root.after(500, poll)
            return

        if state["phase"] == 4:                # 选择操作
            if app.items:
                app.on_select_all()
                rec["all_sel"] = app.l_count.cget("text")
                app.on_invert()
                rec["inverted"] = app.l_count.cget("text")
                app.on_clear_sel()
                rec["cleared"] = app.l_count.cget("text")
            rec["pool_final"] = len(app.cell_pool)
            state["phase"] = 5
            tk_root.after(300, poll)
            return

        if state["phase"] == 5:                # 语言切换
            # 先回中文，使起点确定（不依赖本机已保存的语言偏好）
            i18n.set_language("zh")
            rec["count_none_zh"] = i18n.t("cache.bottom.count_none")
            rec["sort_key_before"] = app.v_sort.get()
            rec["count_zh_before"] = app.l_count.cget("text")

            i18n.set_language("en")
            # 期望值必须在"英文模式当下"取，否则断言时语言已切回中文
            rec["expected_sort_en"] = [i18n.t(f"sort.{k}") for k in cp.SORT_KEYS]
            rec["expected_count_none_en"] = i18n.t("cache.bottom.count_none")
            rec["expected_save_none_en"] = i18n.t("cache.bottom.save_none")
            rec["sort_values_en"] = list(app.cb_sort["values"])
            rec["sort_key_after"] = app.v_sort.get()
            rec["count_en"] = app.l_count.cget("text")
            rec["save_btn_en"] = app.b_save.cget("text")
            app.apply_filter_sort()            # 英文下排序逻辑仍可用
            rec["items_after_en_sort"] = len(app.items)

            i18n.set_language("zh")
            rec["count_back_zh"] = app.l_count.cget("text")
            tk_root.quit()
            return

    tk_root.after(400, poll)
    tk_root.after(120_000, tk_root.quit)             # 兜底，避免测试挂死
    tk_root.mainloop()
    app._on_close()

    # ---- 断言 ----
    assert not problems, problems

    assert rec["total"] > 0, "没有读到任何媒体"
    assert rec["images"] > 0, "样本里没有图片"

    # 虚拟化：图元池必须复用而非随内容增长
    assert rec["pool"] <= MAX_POOL, f"图元池过大（{rec['pool']}），说明没有复用"
    assert rec["visible"] >= MIN_VISIBLE_CELLS, f"可见图元太少：{rec['visible']}"

    avg_ms = sum(timings) / max(len(timings), 1)
    assert avg_ms < MAX_REFRESH_MS, f"单次刷新过慢：{avg_ms:.1f}ms（目标 <{MAX_REFRESH_MS}ms）"
    assert rec["pool_after"] <= rec["pool_before"] * 2 + 20, (
        f"滚动过程中图元池异常增长：{rec['pool_before']} → {rec['pool_after']}"
    )

    # 拖到底部后不应还显示前面的内容
    assert rec["top_idx"] >= rec["items_len"] * 0.5, "拖到底部后仍显示前面的内容"
    assert rec["videos_filtered"] > 0, "切换到视频后没有内容"

    # 选择操作
    assert rec["all_sel"] != rec["count_none_zh"], "全选后计数仍显示未选择"
    assert rec["cleared"] == rec["count_none_zh"], "清空后计数应显示未选择"

    # 语言切换：文案换语言，但排序**内部键**不变、排序逻辑仍可用
    assert rec["sort_values_en"] == rec["expected_sort_en"], rec["sort_values_en"]
    assert rec["sort_key_after"] == rec["sort_key_before"], "切换语言不应改变排序内部键"
    assert rec["count_en"] == rec["expected_count_none_en"], rec["count_en"]
    assert rec["count_en"] != rec["count_zh_before"], "英文计数与中文相同（未翻译？）"
    assert rec["save_btn_en"] == rec["expected_save_none_en"], rec["save_btn_en"]
    assert rec["items_after_en_sort"] == rec["videos_filtered"], "英文下排序后条数应不变"
    assert rec["count_back_zh"] == rec["count_none_zh"], rec["count_back_zh"]