"""缓存挑选器 GUI 端到端测试：虚拟化渲染、滚动性能、选择操作、语言切换。

需要真实 tdata（``tdata_dir``）与显示器（标记 ``gui``）。
与旧脚本的关键差别：**用真实断言判定成败**（旧脚本只写日志、退出码恒为 0，
失败会被静默当成通过）。
"""
from __future__ import annotations

import io
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import cache_picker as cp
import i18n
import ui_kit

# 阈值：单次可视区刷新耗时上限（毫秒）与图元池上限
MAX_REFRESH_MS = 60
MAX_POOL = 120
MIN_VISIBLE_CELLS = 8


@pytest.mark.gui
def test_cache_gui_renders_scrolls_and_switches_language(tdata_dir: Path, tk_root):
    app = cp.CacheApp(tk_root)
    # 「打开即自动提取」已移除：显式触发读取；并固定类型为"全部"（不依赖本机配置）
    app.v_kind.set("all")
    app.on_load()

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


@pytest.mark.gui
def test_open_dir_falls_back_to_clipboard_when_unavailable(tk_root, tmp_path, monkeypatch):
    """打开目录失败时回退：把绝对路径写入剪贴板并在状态栏提示（不弹错、不崩）。"""
    app = cp.CacheApp(tk_root)
    out = tmp_path / "picked"
    out.mkdir()
    app.e_out.delete(0, "end")
    app.e_out.insert(0, str(out))

    def blocked(*_args, **_kwargs):
        raise OSError("shell open is blocked on this machine")

    monkeypatch.setattr(cp.os, "startfile", blocked, raising=False)
    monkeypatch.setattr(cp.subprocess, "Popen", blocked)

    app.on_open_dir()
    assert app.root.clipboard_get() == str(out), "未回退复制绝对路径"
    assert str(out) in app.l_status.cget("text"), "状态栏未提示已复制"
    app._on_close()


@pytest.mark.gui
def test_open_dir_warns_when_folder_missing(tk_root, tmp_path, monkeypatch):
    """目录不存在时提示先选目录，且不尝试打开。"""
    app = cp.CacheApp(tk_root)
    app.e_out.delete(0, "end")
    app.e_out.insert(0, str(tmp_path / "does-not-exist"))
    warns: list[str] = []
    monkeypatch.setattr(cp.messagebox, "showwarning",
                        lambda *a, **k: warns.append(str(a[-1])))
    copied: list[str] = []
    monkeypatch.setattr(app, "_copy_path_fallback", lambda p: copied.append(str(p)))
    app.on_open_dir()
    assert warns, "目录不存在时应弹提示"
    assert copied == [], "目录不存在时不应尝试打开或复制"
    app._on_close()


@pytest.mark.gui
def test_scan_requires_at_least_one_format(tk_root, tmp_path, monkeypatch):
    """一个输出类型都没勾时：提示且不发起扫描（不进入忙碌态）。"""
    app = cp.CacheApp(tk_root)
    app.e_tdata.delete(0, "end")
    app.e_tdata.insert(0, str(tmp_path))          # 保证通过路径校验，命中格式校验
    for var in app.v_formats.values():
        var.set(False)
    warns: list[str] = []
    monkeypatch.setattr(cp.messagebox, "showwarning",
                        lambda *a, **k: warns.append(str(a[-1])))
    app.on_load()
    assert warns, "未勾选任何输出类型时应提示"
    assert app.busy is False, "未勾选格式时不应进入忙碌态"
    app._on_close()


@pytest.mark.gui
def test_output_formats_default_all_and_gear_clamped(tk_root):
    """输出类型默认全勾（9 种格式）；档位线程数经低配钳制。"""
    app = cp.CacheApp(tk_root)
    assert tuple(app.v_formats) == cp.FORMATS
    assert all(v.get() for v in app.v_formats.values()), "输出类型应默认全勾"

    assert app._gear_workers("conservative") == 1
    assert app._gear_workers("balanced") == app.hw_rec
    # 模拟低配（内存 2GB / 32 核）：极速档被钳制到推荐值，而不是跑满 32 线程
    app.hw = ui_kit.HardwareInfo(cores=32, ram_gb=2.0)
    app.hw_rec = ui_kit.recommended_workers(app.hw)
    assert app._gear_workers("turbo") == app.hw_rec
    app._on_close()


@pytest.mark.gui
def test_scan_uses_gear_workers(tk_root, tmp_path, monkeypatch):
    """「性能档位」应真正作用于**扫描**：传给 load_cache 的 workers 与档位一致。"""
    seen: dict = {}

    def fake_load(tdata, passcode, **kw):
        seen.update(kw)
        return [], {"total": 0, "ok": 0, "reused": 0, "skipped": 0, "failed": 0}

    monkeypatch.setattr(cp, "load_cache", fake_load)
    app = cp.CacheApp(tk_root)
    app.e_tdata.delete(0, "end")
    app.e_tdata.insert(0, str(tmp_path))
    app.v_gear.set("conservative")
    app.cb_gear.set(app._gear_label("conservative"))
    app.on_load()

    deadline = time.time() + 10
    while "workers" not in seen and time.time() < deadline:
        app.root.update()
        time.sleep(0.02)
    assert seen.get("workers") == 1, f"保守档应传 1 个扫描线程，实际 {seen}"
    app._on_close()


@pytest.mark.gui
def test_select_all_box_is_tristate_in_sync(tk_root):
    """「全选」三态联动：默认全选 → 取消一个变半选 → 全取消变未选 → 点选一键全勾。"""
    app = cp.CacheApp(tk_root)
    assert app.v_all.get() == "1", "默认应全选"

    app.v_formats[".webm"].set(False)
    assert app.v_all.get() == "", "取消一个后应为半选"
    assert sum(1 for v in app.v_formats.values() if v.get()) == len(cp.FORMATS) - 1

    for var in app.v_formats.values():
        var.set(False)
    assert app.v_all.get() == "0", "全部取消后应为未选"

    app.v_all.set("1")        # 模拟点击：意图按各格式当前状态决定，与控件改写后的变量无关
    app.on_toggle_all()
    assert all(v.get() for v in app.v_formats.values()), "点全选应勾上所有格式"
    assert app.v_all.get() == "1"

    app.v_all.set("0")        # 再点一次：一键取消
    app.on_toggle_all()
    assert not any(v.get() for v in app.v_formats.values()), "再点应取消所有格式"
    assert app.v_all.get() == "0"
    app._on_close()


@pytest.mark.gui
def test_gear_change_does_not_strand_pending_thumbnails(tk_root):
    """改「性能档位」时，已排队但尚未执行的缩略图任务不能被永久丢弃。

    回归测试（P0-1）：旧实现在重建线程池时用 ``cancel_futures=True`` 取消排队
    任务，而被取消的任务**不会投递结果**，其下标会永久留在 ``thumb_pending``；
    ``_thumb_for`` 见到 ``idx in thumb_pending`` 就不再重新请求，于是这些格子
    一直停在占位图/空白，除非重扫或清缓存。这里用单线程（保守档）制造"排队"，
    再模拟改档位，断言排队中的那条最终仍能拿到缩略图。
    """
    app = cp.CacheApp(tk_root)
    app.v_gear.set("conservative")
    app._apply_gear()                       # 单线程：便于让第二个任务进入排队

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (30, 60, 90)).save(buf, "PNG")
    png = buf.getvalue()

    started = threading.Event()
    gate = threading.Event()

    class SlowReader:
        """解密被闸门挡住，用来把第二个任务稳定地卡在"排队"状态。"""

        def decrypt(self, path: str) -> bytes:
            started.set()
            gate.wait(5)
            return png

    app.reader = SlowReader()

    def make_item(rel: str) -> cp.MediaItem:
        entry = SimpleNamespace(
            rel=rel, label="cache", mtime=1_700_000_000.0, enc_size=100,
            kind="image", ext=".jpg", size=100, thumb_key="",
            cache_path=Path("dummy"),
        )
        return cp.MediaItem(entry=entry)

    app.items = [make_item("a"), make_item("b")]
    app.all_items = list(app.items)
    app.root.update()                       # 让画布拿到真实尺寸

    app._request_thumb(0, app.items[0])
    assert started.wait(5), "首个解码任务未开始，无法制造排队"
    app._request_thumb(1, app.items[1])     # 单线程下进入排队
    assert 1 in app.thumb_pending

    app._apply_gear()                       # 改档位：重建线程池（修复点）

    gate.set()                              # 放行，让新旧任务都能跑完
    deadline = time.time() + 10
    while time.time() < deadline and not {0, 1} <= set(app.thumb_mem):
        app._drain_photos()
        app.root.update()
        time.sleep(0.02)

    assert {0, 1} <= set(app.thumb_mem), (
        f"改档位后缩略图卡死：thumbs={sorted(app.thumb_mem)} "
        f"pending={sorted(app.thumb_pending)} failed={sorted(app.thumb_failed)}"
    )
    app._on_close()