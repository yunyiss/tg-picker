# -*- coding: utf-8 -*-
"""GUI 冒烟测试：构建窗口、跑一轮队列泵，然后自动关闭。不联网、不登录。"""
import sys
import tkinter as tk

sys.path.insert(0, r"D:\agent\dsh\tg-picker")
import tg_picker as t

root = tk.Tk()
app = t.App(root)

# 模拟后台投递各类消息，验证 GUI 处理分支不抛异常
app.ui_q.put(("log", "冒烟测试：日志分支 OK"))
app.ui_q.put(("busy", True))
app.ui_q.put(("busy", False))

# 构造一个假媒体项，验证单元格创建 + 选中切换 + 计数
import datetime
class FakeMsg:
    media = None
it = t.MediaItem(msg_id=999, date=datetime.datetime(2026, 10, 10, 1, 2, 3),
                 kind="photo", size=12345, file_name=None, mime="image/jpeg", msg=FakeMsg())
app.by_id[it.msg_id] = it
app.items.append(it)
app.add_item_cell(it)
app.columns = 0
app.relayout()
app.toggle_item(it)
assert it.selected is True
assert "1" in app.b_dl.cget("text"), app.b_dl.cget("text")
app.toggle_item(it)
assert it.selected is False

# 验证 prompt 分支能显示并回填
import threading
res = {}
def ask_later():
    res["v"] = app.worker.prompter.ask("code", "测试验证码：")
threading.Thread(target=ask_later, daemon=True).start()
root.after(600, lambda: (app.e_prompt.insert(0, "12345"), app.on_prompt_submit()))

def finish():
    print("prompter 回填:", res.get("v"))
    assert res.get("v") == "12345", res
    print("GUI 冒烟测试通过：窗口构建、单元格、选中、prompt 分支均正常")
    root.destroy()

root.after(1800, finish)
root.mainloop()
