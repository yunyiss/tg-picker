"""在线挑选器 GUI 冒烟测试（不联网、不登录）。

覆盖：窗口构建、队列各分支、单元格创建与选中、语言切换、验证码回填。
另有 1 条无需显示器的用例，覆盖 Prompter 等待输入时不阻塞事件循环。
"""
from __future__ import annotations

import asyncio
import datetime
import queue
import threading

import pytest

import i18n
import tg_picker as tp


class _FakeMsg:
    """假消息对象：仅用于构造一条 MediaItem。"""

    media = None


@pytest.mark.gui
def test_online_gui_builds_and_handles_messages(tk_root):
    app = tp.App(tk_root)

    # 队列分支：日志 / 忙碌态
    app.ui_q.put(("log", "冒烟测试：日志分支 OK"))
    app.ui_q.put(("busy", True))
    app.ui_q.put(("busy", False))

    # 单元格：创建 + 选中切换 + 计数
    item = tp.MediaItem(msg_id=999,
                        date=datetime.datetime(2026, 10, 10, 1, 2, 3),
                        kind="photo", size=12345, file_name=None,
                        mime="image/jpeg", msg=_FakeMsg())
    app.by_id[item.msg_id] = item
    app.items.append(item)
    app.add_item_cell(item)
    app.columns = 0
    app.relayout()

    app.toggle_item(item)
    assert item.selected is True
    assert "1" in app.b_dl.cget("text"), app.b_dl.cget("text")

    # 语言切换：只调 set_language（不调会写偏好的 on_lang_change）
    i18n.set_language("en")
    assert app.b_dl.cget("text") == "Download selected (1)", app.b_dl.cget("text")
    assert app.l_login.cget("text") == i18n.t("online.login.not_logged")
    i18n.set_language("zh")
    assert app.b_dl.cget("text") == i18n.t("online.download_btn", n=1)

    app.toggle_item(item)
    assert item.selected is False

    # prompt 分支：后台线程索要验证码 → 主线程回填
    result: dict[str, str | None] = {}

    def ask_later() -> None:
        result["v"] = asyncio.run_coroutine_threadsafe(
            app.worker.prompter.ask("code", "online.prompt.code",
                                   phone="+8613800000000"),
            app.worker.loop,
        ).result(timeout=10)

    def answer() -> None:
        app.e_prompt.insert(0, "12345")
        app.on_prompt_submit()

    threading.Thread(target=ask_later, daemon=True).start()
    tk_root.after(600, answer)
    tk_root.after(20_000, tk_root.quit)          # 兜底，避免测试挂死
    tk_root.after(2500, tk_root.quit)
    tk_root.mainloop()

    assert result.get("v") == "12345", result
    app._on_close()


def test_prompter_ask_does_not_block_event_loop():
    """回归（P0-2）：等待验证码/密码时，事件循环仍能推进其它协程。

    旧实现直接在事件循环线程上 ``Event.wait``，等待期间整个循环被占住，
    取消信号与其它协程都无法推进。这里在独立线程跑一个事件循环，发起 ask 后
    调度一个"心跳"协程——若循环被阻塞，心跳在 provide 之前永远不会执行。
    """
    ui_q: queue.Queue = queue.Queue()
    prompter = tp.Prompter(ui_q)
    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    try:
        fut = asyncio.run_coroutine_threadsafe(
            prompter.ask("code", "online.prompt.code", phone="+8613800000000"), loop)
        msg = ui_q.get(timeout=5)
        assert msg[0] == "prompt", msg

        beat = threading.Event()

        async def beat_once() -> None:
            beat.set()

        asyncio.run_coroutine_threadsafe(beat_once(), loop)
        assert beat.wait(2), "等待输入时事件循环被阻塞（其它协程无法运行）"

        prompter.provide("12345")
        assert fut.result(timeout=5) == "12345"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        runner.join(timeout=5)


@pytest.mark.gui
def test_online_double_click_confirms_before_download(tk_root, monkeypatch):
    """回归：双击缩略图必须**先弹确认**；取消则不下单，确认才提交下载。"""
    app = tp.App(tk_root)
    item = tp.MediaItem(msg_id=1, date=datetime.datetime(2026, 10, 10, 1, 2, 3),
                        kind="photo", size=1, file_name="pic.jpg",
                        mime="image/jpeg", msg=_FakeMsg())

    submitted: list[str] = []

    def fake_submit(coro, label=None):
        submitted.append(label)
        coro.close()                      # 避免 "coroutine was never awaited"

    app.worker.submit = fake_submit

    state = {"ret": False}
    messages: list[str] = []

    def fake_ask(title, message, **kw):
        messages.append(str(message))
        return state["ret"]

    monkeypatch.setattr(tp.messagebox, "askyesno", fake_ask)

    # 取消：不发起下载
    app.preview_item(item)
    assert messages, "双击应先弹确认对话框"
    assert str(tp.DEFAULT_OUT) in messages[0], "确认框里应显示目标目录"
    assert submitted == [], "取消确认后不应发起下载"

    # 确认：发起下载
    state["ret"] = True
    app.preview_item(item)
    assert submitted == [i18n.t("online.task.preview")], submitted
    app._on_close()