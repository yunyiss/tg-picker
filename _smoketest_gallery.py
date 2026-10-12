# -*- coding: utf-8 -*-
"""GUI 冒烟测试：加载缩略图墙，验证渲染/选择/统计，然后自动关闭。

数据源取命令行参数或环境变量 GALLERY_SRC；都没有则用程序目录下的
downloads/，再不存在就临时生成样本，保证随时可跑。
"""
import os
import sys
import tempfile
import tkinter as tk
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import gallery_picker as g

_tmp_ctx = None
if len(sys.argv) > 1:
    _SRC = Path(sys.argv[1])
elif os.environ.get("GALLERY_SRC", "").strip():
    _SRC = Path(os.environ["GALLERY_SRC"].strip())
elif (_HERE / "downloads").is_dir():
    _SRC = _HERE / "downloads"
else:
    _tmp_ctx = tempfile.TemporaryDirectory()
    _SRC = Path(_tmp_ctx.name) / "sample"
    _SRC.mkdir(parents=True, exist_ok=True)
    from PIL import Image as _Im
    for i in range(8):
        _Im.new("RGB", (80 + i * 6, 60), (40 + i * 15, 90, 150)).save(
            _SRC / f"s{i:02d}.jpg", "JPEG")
    # 放两个假 mp4：只需扩展名正确即可走到"视频"分支（缩略图用占位图）
    for i in range(2):
        (_SRC / f"clip{i:02d}.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)

root = tk.Tk()
app = g.GalleryApp(root)
# GalleryApp.__init__ 会在 200ms 后自动扫描默认目录；先取消它，
# 改为直接扫描测试数据源，避免扫到空目录导致等待超时。
app._cancel_auto_scan()
app.e_folder.delete(0, "end")
app.e_folder.insert(0, str(_SRC))
print(f"测试数据源: {_SRC}")
app.folder = _SRC
app.start_scan(_SRC)

state = {"phase": 0, "ticks": 0}

def poll():
    state["ticks"] += 1
    if state["ticks"] > 400:      # 上限约 40 秒
        print(f"超时：阶段 {state['phase']} 未完成"
              f"（all_items={len(app.all_items)} rendered={app.rendered} "
              f"items={len(app.items)} thumbs={app.thumbs_ready}）")
        root.destroy()
        return

    if state["phase"] == 0:
        # 等扫描完成
        if app.all_items:
            print(f"[OK] 扫描完成：{len(app.all_items)} 个文件（图片+视频全收），列表 {len(app.items)} 个")
            # 选前 3 个
            for i in range(min(3, app.rendered)):
                mf = app.cells.get(i)
                if mf:
                    app.selected.add(str(mf.path))
                    app._paint_selected(mf, True)
            app.update_count()
            print(f"[OK] 选中 3 个 → 计数标签：{app.l_count.cget('text')}")
            state["phase"] = 1
            root.after(200, poll)
            return
    elif state["phase"] == 1:
        ready = app.thumbs_ready
        if ready >= min(12, app.rendered) or ready >= 12:
            print(f"[OK] 已渲染 {app.rendered} 格，缩略图完成 {ready} 张")
            # 验证真的有 PhotoImage 挂上去
            shown = sum(1 for m in app.cells.values() if m.photo is not None)
            print(f"[OK] 实际显示图片的格子：{shown} / {len(app.cells)}")
            assert shown > 0, "没有任何缩略图显示出来"
            # 测试筛选切换
            app.v_kind.set("video")
            app.on_filter_change()
            print(f"[OK] 切到视频筛选 → {len(app.items)} 个视频")
            state["phase"] = 2
            root.after(500, poll)
            return
        else:
            root.after(100, poll)
            return
    elif state["phase"] == 2:
        if app.rendered > 0:
            print(f"[OK] 视频视图渲染 {app.rendered} 格")
            # 测试清空选择
            app.on_clear_sel()
            print(f"[OK] 清除选择 → {app.l_count.cget('text')}")
            # 测试全选
            app.on_select_all()
            print(f"[OK] 全选已加载 → {app.l_count.cget('text')}")
            app.on_invert()
            print(f"[OK] 反选 → {app.l_count.cget('text')}")
            print("\nGUI 冒烟测试全部通过 ✅")
            root.destroy()
            return
        root.after(100, poll)
        return

root.after(300, poll)
root.mainloop()

if _tmp_ctx is not None:
    _tmp_ctx.cleanup()
