"""在线挑选器 GUI 冒烟测试（不联网、不登录）。

覆盖：窗口构建、队列各分支、单元格创建与选中、语言切换、验证码回填。
另有 1 条无需显示器的用例，覆盖 Prompter 等待输入时不阻塞事件循环。
"""
from __future__ import annotations

import asyncio
import datetime
import os
import queue
import stat
import threading
import time

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
    assert app.b_dl.cget("text") == "Save selected (1)", app.b_dl.cget("text")
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


@pytest.mark.gui
def test_online_open_dir_falls_back_to_clipboard(tk_root, tmp_path, monkeypatch):
    """打开目录失败时回退：把绝对路径写入剪贴板（不弹错、不崩）。"""
    app = tp.App(tk_root)
    out = tmp_path / "picked"
    out.mkdir()
    app.e_out.delete(0, "end")
    app.e_out.insert(0, str(out))
    monkeypatch.setattr(tp.ui_kit, "open_folder", lambda p: False)
    app.on_open_dir()
    assert app.root.clipboard_get() == str(out), "未回退复制绝对路径"
    app._on_close()


@pytest.mark.gui
def test_online_open_dir_warns_when_folder_missing(tk_root, tmp_path, monkeypatch):
    """目录不存在时提示先选目录，且不尝试打开。"""
    app = tp.App(tk_root)
    app.e_out.delete(0, "end")
    app.e_out.insert(0, str(tmp_path / "does-not-exist"))
    warns: list[str] = []
    monkeypatch.setattr(tp.messagebox, "showwarning",
                        lambda *a, **k: warns.append(str(a[-1])))
    opened: list[str] = []
    monkeypatch.setattr(tp.ui_kit, "open_folder",
                        lambda p: (opened.append(str(p)), True)[1])
    app.on_open_dir()
    assert warns, "目录不存在时应弹提示"
    assert opened == [], "目录不存在时不应尝试打开"
    app._on_close()


def test_parse_proxy_variants():
    """代理字符串解析：socks5 / http / 带账号密码 / 省略 scheme / 非法。"""
    assert tp.parse_proxy("") is None
    assert tp.parse_proxy("   ") is None
    assert tp.parse_proxy("127.0.0.1:7897") == ("socks5", "127.0.0.1", 7897)
    assert tp.parse_proxy("socks5://127.0.0.1:7897") == ("socks5", "127.0.0.1", 7897)
    assert tp.parse_proxy("http://127.0.0.1:10809") == ("http", "127.0.0.1", 10809)
    assert tp.parse_proxy("socks5://u:p@127.0.0.1:7897") == (
        "socks5", "127.0.0.1", 7897, True, "u", "p")
    assert tp.parse_proxy("not-a-proxy") is None, "缺端口应视为非法"
    assert tp.parse_proxy("ftp://127.0.0.1:21") is None, "不支持的协议应视为非法"


@pytest.mark.gui
def test_online_login_rejects_bad_proxy(tk_root, monkeypatch):
    """代理格式非法时应提示，且不提交登录任务。"""
    app = tp.App(tk_root)
    app.e_api_id.insert(0, "123")
    app.e_api_hash.insert(0, "x" * 32)
    app.e_phone.insert(0, "+8613800000000")
    app.e_proxy.insert(0, "not-a-proxy")
    warns: list[str] = []
    monkeypatch.setattr(tp.messagebox, "showwarning",
                        lambda *a, **k: warns.append(str(a[-1])))
    submitted: list[str] = []
    app.worker.submit = lambda coro, label=None: (submitted.append(label), coro.close())
    app.on_login()
    assert warns, "非法代理应弹提示"
    assert submitted == [], "非法代理不应提交登录"
    app._on_close()


@pytest.mark.gui
def test_online_proxy_row_is_compact(tk_root):
    """代理行只留紧凑的代理框 + 小字提示（登录按钮已归到凭据行）。"""
    app = tp.App(tk_root)
    assert app.e_proxy.winfo_exists()
    assert int(app.e_proxy.cget("width")) <= 30, "代理输入框不应过宽"
    assert app.e_proxy.master is not app.b_login.master, "登录按钮应归到凭据行"
    texts = []

    def _walk(w):
        for c in w.winfo_children():
            if str(c.winfo_class()) == "TLabel":
                texts.append(str(c.cget("text")))
            _walk(c)

    _walk(app._login_frame)
    assert i18n.t("online.login.proxy_hint") in texts, "代理行应有小字提示"
    app._on_close()


@pytest.mark.gui
def test_online_login_button_on_credentials_row(tk_root):
    """登录按钮与登录状态应与手机号（凭据行）同一行。"""
    app = tp.App(tk_root)
    row1 = app.e_api_id.master
    assert app.e_phone.master is row1
    assert app.b_login.master is row1, "登录按钮应与手机号同一行"
    assert app.l_login.master is row1, "登录状态应与按钮同一行"
    app._on_close()


@pytest.mark.gui
def test_online_credentials_row_has_no_big_gap(tk_root):
    """回归：两行各自独立子栅格——代理行很长的提示文案不应把凭据行撑出空白。

    旧实现里两行共用一个栅格，代理提示横跨 api_hash 所在的列，把该列撑得很宽，
    `api_id` 与 `api_hash` 之间因此空出一大块。
    """
    app = tp.App(tk_root)
    tk_root.update()                     # 需要真实几何
    row1 = app.e_api_id.master
    labels = {str(w.cget("text")): w for w in row1.winfo_children()
              if str(w.winfo_class()) == "TLabel"}
    lab_hash = labels[i18n.t("online.login.api_hash")]
    gap = lab_hash.winfo_x() - (app.e_api_id.winfo_x() + app.e_api_id.winfo_width())
    assert 0 <= gap < 40, f"api_id 与 api_hash 之间不应有大片空白（实际 {gap}px）"
    app._on_close()


@pytest.mark.gui
def test_online_login_passes_parsed_proxy(tk_root, monkeypatch):
    """填了代理时，登录任务应带上解析后的 proxy 参数（保持原功能逻辑）。"""
    app = tp.App(tk_root)
    # 同上：清掉启动时回填的值，避免依赖"本用例排在第几个"
    for entry in (app.e_api_id, app.e_api_hash, app.e_phone, app.e_proxy):
        entry.delete(0, "end")
    app.e_api_id.insert(0, "123")
    app.e_api_hash.insert(0, "y" * 32)
    app.e_phone.insert(0, "+8613800000000")
    app.e_proxy.insert(0, "127.0.0.1:7897")
    captured = {}

    def fake_login(api_id, api_hash, phone, proxy=None):
        captured.update(api_id=api_id, api_hash=api_hash, phone=phone, proxy=proxy)

        async def _noop() -> None:
            return None

        return _noop()

    app.worker.login = fake_login
    app.worker.submit = lambda coro, label=None: coro.close()
    app.on_login()
    assert captured["proxy"] == ("socks5", "127.0.0.1", 7897)
    assert captured["api_id"] == 123
    assert captured["phone"] == "+8613800000000"
    app._on_close()


def test_mask_proxy_hides_credentials():
    """带 user:pass 的代理串在显示 / 记日志前必须掩码（否则密码会落到日志文件里）。"""
    assert tp.mask_proxy("http://user:pass@127.0.0.1:8080") == \
        "http://***:***@127.0.0.1:8080"
    assert tp.mask_proxy("user:pass@1.2.3.4:1080") == "***:***@1.2.3.4:1080"
    assert tp.mask_proxy("socks5://127.0.0.1:7897") == "socks5://127.0.0.1:7897"
    assert tp.mask_proxy("") == ""


def test_harden_session_file_permissions(tmp_path, monkeypatch):
    """会话文件等同登录凭证：写入后应把权限收到 0600（POSIX 可验证；Windows 为 no-op）。"""
    monkeypatch.setattr(tp, "SESSION_PATH", str(tmp_path / "tg_picker_session"))
    tp.harden_session_file()                      # 文件不存在时不应报错

    sess = tmp_path / "tg_picker_session.session"
    sess.write_text("x", encoding="utf-8")
    sess.chmod(0o644)
    tp.harden_session_file()
    assert sess.exists(), "收权限不应影响文件本身"
    if os.name == "posix":
        assert stat.S_IMODE(sess.stat().st_mode) == 0o600, "会话文件权限应收到 0600"


@pytest.mark.gui
def test_api_close_stops_worker_loop(tk_root):
    """回归：关闭窗口应断开连接并停掉后台事件循环（旧实现 loop 永不停止）。"""
    app = tp.App(tk_root)
    assert app.worker.loop.is_running()
    app._on_close()
    deadline = time.time() + 5
    while time.time() < deadline and app.worker.loop.is_running():
        time.sleep(0.05)
    assert not app.worker.loop.is_running(), "关闭后后台事件循环应停止"


@pytest.mark.gui
def test_login_log_line_uses_masked_proxy(tk_root):
    """回归：登录时那条「使用代理」日志必须渲染**掩码后**的串。

    旧实现把用户输入的原文写进日志 ⇒ 带 user:pass 的代理会把密码落进
    `data/logs/tg_picker.log`。这里断言日志与该为掩码后的渲染**完全相等**。
    """
    raw = "http://alice:s3cret@127.0.0.1:8080"
    app = tp.App(tk_root)
    # 先清空：会话共享同一个临时数据目录，前面的用例可能把代理存进 config.json，
    # 启动时会被回填到输入框，直接 insert 会拼成一个无效串（用例就变成"看执行顺序"）
    for entry in (app.e_api_id, app.e_api_hash, app.e_phone, app.e_proxy):
        entry.delete(0, "end")
    app.e_api_id.insert(0, "123")
    app.e_api_hash.insert(0, "y" * 32)
    app.e_phone.insert(0, "+8613800000000")
    app.e_proxy.insert(0, raw)
    logged: list[str] = []
    app.log = logged.append                      # 拦下界面日志（= 写进日志文件的内容）
    app.worker.login = lambda *a, **kw: asyncio.sleep(0)
    app.worker.submit = lambda coro, label=None: coro.close()
    app.on_login()

    assert logged == [i18n.t("online.log.proxy_set", proxy=tp.mask_proxy(raw))]
    assert "s3cret" not in logged[0] and "alice" not in logged[0], "凭据不得进日志"
    assert "127.0.0.1:8080" in logged[0], "主机端口应当保留（便于排查连不上）"
    app._on_close()


@pytest.mark.gui
def test_session_file_is_hardened_at_startup_and_after_login(tk_root, monkeypatch):
    """回归：会话文件权限既要在启动时收，也要在每次登录成功后收。

    （`harden_session_file` 自身的行为由 `test_harden_session_file_permissions`
    覆盖；这里只断言"接线"没漏——漏接就等于没加固。）
    """
    calls: list[int] = []
    monkeypatch.setattr(tp, "harden_session_file", lambda: calls.append(1))
    app = tp.App(tk_root)
    assert calls, "启动时应收紧一次会话文件权限"
    before = len(calls)
    app._msg_logged_in(("logged_in", "someone"))
    assert len(calls) == before + 1, "登录成功后应再收紧一次"
    app._on_close()