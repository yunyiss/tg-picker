# -*- coding: utf-8 -*-
"""GUI 冒烟测试：用真实目录加载缩略图墙，验证渲染/选择/统计，然后自动关闭。"""
import sys
import tkinter as tk
from pathlib import Path

sys.path.insert(0, r"D:\agent\dsh\tg-picker")
import gallery_picker as g

root = tk.Tk()
app = g.GalleryApp(root)
app.e_folder.delete(0, "end")
app.e_folder.insert(0, r"D:\agent\dsh\tg-scoop-output")

state = {"phase": 0, "ticks": 0}

def poll():
    state["ticks"] += 1
    if state["ticks"] > 400:      # 上限约 40 秒
        print("超时：缩略图未在预期时间内渲染完成")
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
