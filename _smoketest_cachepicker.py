# -*- coding: utf-8 -*-
"""缓存版 GUI 测试：功能 + 滚动性能（图元复用、无撕裂）。

结果写入文件，避免管道缓冲。
用法：python _smoketest_cachepicker.py [日志文件]
"""
import sys
import time
import tkinter as tk
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import cache_picker as cp

LOG = Path(sys.argv[1]) if len(sys.argv) > 1 else _HERE / "_smoketest_cache_log.txt"
LOG.write_text("", encoding="utf-8")


def log(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")
        f.flush()


log("启动缓存版 GUI 测试（虚拟化渲染）")
root = tk.Tk()
app = cp.CacheApp(root)
state = {"phase": 0, "ticks": 0}

# 统计 refresh_view 的真实开销
timings: list[float] = []
_orig_refresh = app.refresh_view


def timed_refresh():
    t0 = time.perf_counter()
    _orig_refresh()
    timings.append((time.perf_counter() - t0) * 1000)


app.refresh_view = timed_refresh


def fail(msg):
    log("断言失败：" + msg)
    root.destroy()


def poll():
    state["ticks"] += 1
    if state["ticks"] > 1500:
        log("超时终止")
        root.destroy()
        return

    if state["phase"] == 0:
        if app.all_items:
            imgs = sum(1 for i in app.all_items if i.kind == "image")
            vids = sum(1 for i in app.all_items if i.kind == "video")
            log(f"[OK] 读取完成：{len(app.all_items)} 个媒体（图 {imgs} / 视频 {vids}）")
            log(f"[OK] 当前视图 {len(app.items)} 个，列数 {app.columns}")
            state["phase"] = 1
            root.after(600, poll)
        else:
            root.after(200, poll)
        return

    if state["phase"] == 1:
        ready = len(app.thumb_mem)
        if ready >= 12:
            log(f"[OK] 缩略图已载入 {ready} 张，图元池大小 {len(app.cell_pool)}")
            shown = sum(1 for c in app.cell_pool if c["slot"] is not None)
            log(f"[OK] 可见图元 {shown} 个")
            if len(app.cell_pool) > 120:
                return fail(f"图元池过大（{len(app.cell_pool)}），说明没有复用")
            if shown < 8:
                return fail(f"可见图元太少：{shown}")
            # 选 3 个
            for i in range(3):
                app.selected.add(str(app.items[i].entry.rel))
            app.update_count()
            log(f"[OK] 选中 3 个 → {app.l_count.cget('text')}")
            state["phase"] = 2
            root.after(300, poll)
        else:
            root.after(200, poll)
        return

    if state["phase"] == 2:
        # 模拟快速滚轮滚动 40 次，看耗时与图元池是否稳定
        timings.clear()
        pool_before = len(app.cell_pool)
        t0 = time.perf_counter()
        for i in range(40):
            app._scroll_units(3)
            app.refresh_view()          # 直接同步刷新以测量最坏情况
            root.update_idletasks()
        total_ms = (time.perf_counter() - t0) * 1000
        pool_after = len(app.cell_pool)
        avg = sum(timings) / max(len(timings), 1)
        worst = max(timings) if timings else 0
        log(f"[OK] 模拟 40 次滚轮：总耗时 {total_ms:.0f}ms，单次平均 {avg:.2f}ms，最慢 {worst:.2f}ms")
        log(f"[OK] 图元池：滚动前 {pool_before} → 滚动后 {pool_after}")
        if pool_after > pool_before * 2 + 20:
            return fail(f"滚动过程中图元池异常增长：{pool_before} → {pool_after}")
        if avg > 60:
            return fail(f"单次刷新过慢：{avg:.1f}ms（目标 <60ms）")
        # 滚回顶部
        app.canvas.yview_moveto(0)
        app.refresh_view()
        root.after(200, poll)
        state["phase"] = 3
        return

    if state["phase"] == 3:
        # 大跨度跳转：直接拖到底部
        app.canvas.yview_moveto(1.0)
        app.refresh_view()
        top_idx = min(app.cells_by_slot.keys()) if app.cells_by_slot else -1
        log(f"[OK] 拖到底部：可见首项索引 {top_idx}（共 {len(app.items)}）")
        if top_idx < len(app.items) * 0.5:
            return fail("拖到底部后仍显示前面的内容")
        # 切视频
        app.v_kind.set("video")
        app.on_filter_change()
        log(f"[OK] 切到视频 → {len(app.items)} 个")
        state["phase"] = 4
        root.after(500, poll)
        return

    if state["phase"] == 4:
        if app.items:
            app.on_select_all()
            log(f"[OK] 全选 → {app.l_count.cget('text')}")
            app.on_invert()
            log(f"[OK] 反选 → {app.l_count.cget('text')}")
            app.on_clear_sel()
            log(f"[OK] 清空 → {app.l_count.cget('text')}")
            log(f"[OK] 最终图元池大小 {len(app.cell_pool)}（复用生效）")
        log("缓存版 GUI 测试全部通过 ✅")
        root.destroy()
        return


root.after(400, poll)
root.mainloop()
log("窗口已关闭")
