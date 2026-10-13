"""TG 图片挑选下载器（tg-picker）

在图形界面里浏览某个群/频道的媒体缩略图，喜欢哪个就勾哪个，只下载选中的原图
——不用整个群聊全下，也不改动任何 Telegram 客户端数据。

原理：用你自己的 Telegram 账号走官方 API（Telethon），按需下载消息里的媒体。
受限保存（Restrict Saving Content / 禁止保存）的聊天同样可以下载：Telegram
服务器只对"转发"做硬拒绝，对"保存"只是请求客户端自律（协议原文 should），
所以走 API 读取并下载是可行的——这也是各类"受限内容保存机器人"的共同原理，
区别是本脚本完全跑在你自己电脑上，凭据不出本机。

首次使用需要在 https://my.telegram.org 申请 api_id / api_hash（免费）。
"""
from __future__ import annotations

import asyncio
import io
import os
import queue
import re
import threading
import tkinter as tk
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk
from telethon import TelegramClient, errors, functions
from telethon.tl.types import (
    DocumentAttributeFilename,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    MessageMediaDocument,
    MessageMediaPhoto,
)

import i18n
import ui_kit
import ui_style
from i18n import t
from ui_kit import FONT, human_size, unique_path
from ui_lang import LangMixin

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
SESSION_PATH = str(APP_DIR / "tg_picker_session")
DEFAULT_OUT = APP_DIR / "downloads"


def _app_title() -> str:
    """窗口标题（同时用作所有对话框的标题）。"""
    return t("app.online_title")

# 界面配色（深色，接近 Telegram）
BG = "#1e1e1e"
CARD = "#2b2b2b"
CARD_SEL = "#1f6feb"
CARD_HOVER = "#3a3a3a"
FG = "#e8e8e8"
SUB = "#9aa0a6"

THUMB = 150          # 缩略图边长
CELL_W = 168         # 单元格宽
CELL_H = 205         # 单元格高
SCAN_BATCH = 100     # 每次向服务器取多少条消息
SCAN_MAX_MSGS = 6000 # 单次"加载"最多扫描多少条消息，防失控
PUMP_MS = 80         # 消息队列轮询间隔（毫秒）
# 等待用户填写验证码/密码的上限（秒）。只是"界面已不可用"时的安全网：
# 没有它，后台协程会永久阻塞在 Event.wait() 上，busy 永不复位。
PROMPT_TIMEOUT = 300

IMAGE_EXT = {"image/jpeg": ".jpg", "image/png": ".png",
             "image/webp": ".webp", "image/gif": ".gif"}
VIDEO_EXT = {"video/mp4": ".mp4", "video/webm": ".webm",
             "video/quicktime": ".mov", "video/x-matroska": ".mkv"}


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
_ILLEGAL = '<>:"/\\|?*'


def sanitize(name: str, maxlen: int = 120) -> str:
    """把字符串净化成 Windows 可用的文件名片段。"""
    cleaned = "".join("_" if (c in _ILLEGAL or ord(c) < 32) else c for c in name)
    cleaned = cleaned.rstrip(" .")[:maxlen]
    return cleaned or "unnamed"


# human_size / unique_path 见 ui_kit（与缓存挑选器共用同一实现，避免两份逻辑漂移）


def classify(msg) -> str | None:
    """判断消息媒体类型：photo / video / file；非媒体或贴纸/音频返回 None。"""
    media = getattr(msg, "media", None)
    if media is None:
        return None
    if isinstance(media, MessageMediaPhoto):
        return "photo"
    if isinstance(media, MessageMediaDocument):
        doc = getattr(media, "document", None)
        if doc is None:
            return None
        attrs = getattr(doc, "attributes", []) or []
        if any(isinstance(a, DocumentAttributeSticker) for a in attrs):
            return None  # 贴纸不计入图片
        mime = (getattr(doc, "mime_type", "") or "").lower()
        if mime.startswith("image/"):
            return "photo"
        if mime.startswith("video/") or any(isinstance(a, DocumentAttributeVideo) for a in attrs):
            return "video"
        if mime.startswith("audio/"):
            return None
        return "file"
    return None


def original_name(msg) -> str | None:
    """取原始文件名（文档类才有）。"""
    doc = getattr(msg, "document", None)
    if doc is None:
        return None
    for a in getattr(doc, "attributes", []) or []:
        if isinstance(a, DocumentAttributeFilename):
            return a.file_name
    return None


def mime_of(msg) -> str:
    try:
        f = msg.file
        return (f.mime_type or "") if f else ""
    except Exception:
        return ""


def size_of(msg) -> int | None:
    try:
        f = msg.file
        return f.size if f else None
    except Exception:
        return None


def ext_for(item: MediaItem) -> str:
    """决定输出扩展名。"""
    if item.file_name:
        suffix = Path(item.file_name).suffix
        if suffix:
            return suffix
    mime = (item.mime or "").lower()
    if item.kind == "photo":
        return IMAGE_EXT.get(mime, ".jpg")
    if item.kind == "video":
        return VIDEO_EXT.get(mime, ".mp4")
    return ".bin"


@dataclass
class MediaItem:
    """一条可挑选的媒体。"""

    msg_id: int
    date: datetime
    kind: str
    size: int | None
    file_name: str | None
    mime: str
    msg: object
    selected: bool = False
    thumb: object = None          # PIL.Image
    photo: object = None          # ImageTk.PhotoImage（必须持有引用）
    cell: object = None           # 单元格 Frame
    img_label: object = None
    mark_label: object = None
    thumb_failed: bool = False    # 缩略图确实取不到（用于语言切换时刷新"无预览"）


# --------------------------------------------------------------------------
# 后台线程：Telethon 事件循环 + 与 GUI 的队列通信
# --------------------------------------------------------------------------
class Prompter:
    """让后台协程能向 GUI 索要输入（验证码/两步密码）。"""

    def __init__(self, ui_q: queue.Queue) -> None:
        self.ui_q = ui_q
        self._event = threading.Event()
        self._value: str | None = None

    async def ask(self, kind: str, key: str, **kw) -> str | None:
        """后台协程调用：发请求并**等待** GUI 回填；超时或界面已关闭返回 None。

        传的是**语言键 + 参数**而不是成文文案，这样界面切换语言时能按
        新语言重渲染这条待填写的提示。

        等待放在**独立线程**里（``asyncio.to_thread``）：若直接在事件循环线程上
        ``Event.wait``，等待期间整个循环都会被占住——取消信号、其它协程都无法
        推进。仍必须带超时：界面已销毁（或用户一直不提交）而事件永不 set 时，
        不能让它永久挂住。
        """
        self._event.clear()
        self._value = None
        self.ui_q.put(("prompt", kind, key, kw))
        return await asyncio.to_thread(self._wait_value)

    def _wait_value(self) -> str | None:
        """在独立线程里阻塞等待 GUI 回填；超时返回 None。"""
        if not self._event.wait(timeout=PROMPT_TIMEOUT):
            return None
        return self._value

    def provide(self, value: str | None) -> None:
        """GUI 线程调用：回填结果并唤醒后台。"""
        self._value = value
        self._event.set()


class Worker:
    """在独立线程里跑 asyncio，GUI 通过 submit() 投递协程。"""

    def __init__(self, ui_q: queue.Queue) -> None:
        self.ui_q = ui_q
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.client: TelegramClient | None = None
        self.prompter = Prompter(ui_q)
        self.cancel = threading.Event()

    # ----- 基础设施 -----
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro, label: str | None = None) -> None:
        """把协程投递到后台事件循环；未捕获的异常兜底记入日志并复位忙碌态。"""
        label = label or t("online.task.default")
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def _done(f):
            try:
                f.result()
            except Exception as exc:  # 理论上协程内部已捕获，这里是兜底
                self.ui_q.put(("log", t("online.log.submit_err", label=label,
                                        name=exc.__class__.__name__, exc=exc)))
                self.ui_q.put(("log", traceback.format_exc()))
                self.ui_q.put(("busy", False))

        fut.add_done_callback(_done)

    def post(self, *msg) -> None:
        """从后台线程向 GUI 投递一条消息（消息名对应 App._msg_<名>）。"""
        self.ui_q.put(msg)

    # ----- 登录 -----
    async def login(self, api_id: int, api_hash: str, phone: str) -> None:
        """连接并登录；需要验证码/两步密码时经 Prompter 向界面索要。

        全程响应取消：收到取消（或等待输入超时）即中止并复位忙碌态。
        """
        try:
            self.cancel.clear()          # 上一次的取消不应影响本次登录
            self.post("log", t("online.log.connecting", api_id=api_id))
            self.client = TelegramClient(SESSION_PATH, api_id, api_hash)
            await self.client.connect()

            if self.cancel.is_set():
                self.post("log", t("online.log.cancelled"))
                return

            if not await self.client.is_user_authorized():
                self.post("log", t("online.log.requesting_code"))
                sent = await self.client.send_code_request(phone)
                if self.cancel.is_set():
                    self.post("log", t("online.log.cancelled"))
                    return
                code = await self.prompter.ask("code", "online.prompt.code", phone=phone)
                if not code:
                    self.post("log", t("online.log.cancelled"))
                    return
                try:
                    await self.client.sign_in(
                        phone=phone, code=code.strip(), phone_code_hash=sent.phone_code_hash
                    )
                except errors.SessionPasswordNeededError:
                    pw = await self.prompter.ask("password", "online.prompt.password")
                    if not pw:
                        self.post("log", t("online.log.cancelled"))
                        return
                    await self.client.sign_in(password=pw)

            if self.cancel.is_set():
                self.post("log", t("online.log.cancelled"))
                return

            me = await self.client.get_me()
            who = " ".join(
                x for x in [getattr(me, "first_name", None),
                            getattr(me, "last_name", None)] if x
            )
            self.post("log", t("online.log.login_ok", who=who or t("online.log.unnamed"),
                               username=getattr(me, "username", None) or "-"))
            save_config({"api_id": api_id, "api_hash": api_hash, "phone": phone})
            self.post("logged_in", who)
        except errors.PhoneCodeInvalidError:
            self.post("log", t("online.log.code_invalid"))
        except errors.PhoneCodeExpiredError:
            self.post("log", t("online.log.code_expired"))
        except errors.PasswordHashInvalidError:
            self.post("log", t("online.log.password_invalid"))
        except Exception as exc:
            self.post("log", t("online.log.login_failed",
                               name=exc.__class__.__name__, exc=exc))
        finally:
            self.post("busy", False)

    # ----- 解析聊天 -----
    async def _resolve(self, chat: str):
        """把用户输入的群/频道标识（@用户名、t.me 链接、邀请链接、数字 ID）解析为实体。"""
        raw = (chat or "").strip()
        if not raw:
            raise ValueError(t("online.log.need_chat"))
        # 从链接里抠出标识
        m = re.search(r"(?:t\.me|telegram\.me)/(?:joinchat/|\+)?([^/?#\s]+)", raw, re.I)
        invite = None
        if m:
            token = m.group(1)
            is_invite = ("+" in raw) or ("joinchat" in raw.lower())
            if is_invite and re.fullmatch(r"[A-Za-z0-9_-]{10,}", token):
                invite = token
            else:
                raw = token
        if raw.startswith("@"):
            raw = raw[1:]
        if invite:
            self.post("log", t("online.log.invite"))
            try:
                res = await self.client(functions.messages.ImportChatInviteRequest(hash=invite))
            except errors.UserAlreadyParticipantError:
                # 已在群里：用 messages.CheckChatInvite 拿 chat_id
                info = await self.client(functions.messages.CheckChatInviteRequest(hash=invite))
                chat_obj = getattr(info, "chat", None)
                if chat_obj is None:
                    raise
                return chat_obj
            chats = getattr(res, "chats", None)
            if not chats:
                # 注意：此时 raw 仍是完整邀请链接，拿它去 get_entity 必然失败，
                # 所以直接报错并提示改用 @用户名 / 数字 ID。
                raise ValueError(t("online.log.invite_failed"))
            return chats[0]
        if re.fullmatch(r"-?\d+", raw):
            return await self.client.get_entity(int(raw))
        return await self.client.get_entity(raw)

    # ----- 加载媒体列表 -----
    async def load_media(self, chat: str, target: int, kinds: set[str],
                         offset_id: int | None) -> None:
        """持续翻页直到收集到 target 个目标类型媒体（或消息耗尽）。"""
        try:
            if self.client is None or not await self.client.is_user_authorized():
                self.post("log", t("online.log.need_login"))
                self.post("busy", False)
                return
            self.cancel.clear()
            entity = await self._resolve(chat)
            self.post("log", t("online.log.entity",
                               title=getattr(entity, "title", None)
                               or getattr(entity, "username", None) or entity.id))

            items: list[MediaItem] = []
            seen: set[int] = set()
            cursor = offset_id or 0
            scanned = 0
            while len(items) < target and scanned < SCAN_MAX_MSGS and not self.cancel.is_set():
                batch = await self.client.get_messages(entity, limit=SCAN_BATCH, offset_id=cursor)
                if not batch:
                    break
                scanned += len(batch)
                last_id = None
                for msg in batch:
                    if msg is None:
                        continue
                    last_id = msg.id if last_id is None else min(last_id, msg.id)
                    if msg.id in seen:
                        continue
                    seen.add(msg.id)
                    kind = classify(msg)
                    if kind is None or kind not in kinds:
                        continue
                    fname = original_name(msg)
                    items.append(
                        MediaItem(
                            msg_id=msg.id,
                            date=getattr(msg, "date", datetime.now()),
                            kind=kind,
                            size=size_of(msg),
                            file_name=fname,
                            mime=mime_of(msg),
                            msg=msg,
                        )
                    )
                    if len(items) >= target:
                        break
                self.post("log", t("online.log.scanning", scanned=scanned, found=len(items)))
                if last_id is None:
                    break  # 本页没有可用消息，终止
                cursor = last_id
                if len(batch) < SCAN_BATCH:
                    break  # 已经到底

            if not items:
                self.post("log", t("online.log.no_match"))
            else:
                self.post("log", t("online.log.batch", n=len(items)))
            self.post("media_loaded", items, cursor, len(items) < target)
            await self.load_thumbs(items)
        except errors.UsernameNotOccupiedError:
            self.post("log", t("online.log.no_such_user"))
        except errors.ChannelPrivateError:
            self.post("log", t("online.log.private"))
        except errors.ChatAdminRequiredError:
            self.post("log", t("online.log.admin_required"))
        except ValueError as exc:
            self.post("log", str(exc))
        except Exception as exc:
            self.post("log", t("online.log.load_failed",
                               name=exc.__class__.__name__, exc=exc))
        finally:
            self.post("busy", False)

    # ----- 缩略图 -----
    async def _thumb_bytes(self, it: MediaItem) -> bytes:
        """按媒体类型挑合适的缩略图尺寸，多种策略依次尝试。

        照片优先取较小的尺寸（网格里够用且快），视频/文件取内嵌封面图；
        任一策略失败就退回整文件，最后仍失败则返回空字节。
        """
        if it.kind == "photo":
            strategies: list[dict] = [{"thumb": 1}, {"thumb": 0}, {}]
        else:
            strategies = [{"thumb": -1}, {}]
        for kw in strategies:
            try:
                buf = io.BytesIO()
                got = await self.client.download_media(it.msg, file=buf, **kw)
                data = buf.getvalue() if got is not None else b""
                if data:
                    return data
            except Exception:
                continue
        return b""

    async def load_thumbs(self, items: list[MediaItem]) -> None:
        """逐条拉取缩略图并投递给界面；取不到的投 None（界面显示"无预览"）。"""
        for it in items:
            if self.cancel.is_set():
                break
            try:
                data = await self._thumb_bytes(it)
                if not data:
                    raise ValueError("无缩略图数据")
                img = Image.open(io.BytesIO(data))
                img = img.convert("RGB")
                img.thumbnail((THUMB, THUMB), Image.LANCZOS)
                self.post("thumb", it.msg_id, img)
            except Exception:
                self.post("thumb", it.msg_id, None)
            await asyncio.sleep(0.05)  # 温和控速

    # ----- 下载 -----
    async def _download_one(self, it: MediaItem, out_dir: Path) -> Path:
        """下载一个媒体原件到 out_dir，返回路径。带 FloodWait 重试。"""
        date = it.date if isinstance(it.date, datetime) else datetime.now()
        if it.file_name:
            stem = sanitize(Path(it.file_name).stem)
            name = f"{stem}{ext_for(it)}"
        else:
            name = f"{it.kind}_{date:%Y%m%d_%H%M%S}_{it.msg_id}{ext_for(it)}"
        path = unique_path(out_dir / sanitize(name, 150))

        for attempt in (1, 2):
            try:
                with open(path, "wb") as f:
                    await self.client.download_media(it.msg, file=f)
                try:
                    ts = date.timestamp()
                    os.utime(path, (ts, ts))
                except OSError:
                    pass
                return path
            except errors.FloodWaitError as exc:
                if attempt == 2:
                    raise
                self.post("log", t("online.log.rate_wait", seconds=exc.seconds))
                await asyncio.sleep(exc.seconds + 1)
        raise RuntimeError("unreachable")

    async def download_items(self, items: list[MediaItem], out_dir: Path) -> None:
        """批量下载原件到 out_dir；逐个报进度，结束投递 download_done。"""
        try:
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            self.cancel.clear()
            total = len(items)
            ok = fail = 0
            self.post("log", t("online.log.dl_start", n=total, out=out_dir))
            for idx, it in enumerate(items, 1):
                if self.cancel.is_set():
                    self.post("log", t("online.log.dl_cancel", ok=ok, left=total - idx + 1))
                    break
                self.post("progress", idx - 1, total)
                try:
                    path = await self._download_one(it, out_dir)
                    ok += 1
                    self.post("log", t("online.log.dl_item_ok",
                                       idx=idx, total=total, name=path.name))
                except errors.FloodWaitError as exc:
                    fail += 1
                    self.post("log", t("online.log.dl_item_flood",
                                       idx=idx, total=total, seconds=exc.seconds))
                except Exception as exc:
                    fail += 1
                    self.post("log", t("online.log.dl_item_err", idx=idx, total=total,
                                       name=exc.__class__.__name__, exc=exc))
                self.post("progress", idx, total)
                await asyncio.sleep(0.35)  # 温和控速，避免触发限流
            self.post("log", t("online.log.dl_done", ok=ok, fail=fail))
            self.post("download_done", out_dir, ok, fail)
        except Exception as exc:
            self.post("log", t("online.log.dl_interrupted",
                               name=exc.__class__.__name__, exc=exc))
        finally:
            self.post("busy", False)

    async def download_single(self, it: MediaItem, out_dir: Path) -> None:
        """双击预览：把这一条原件下载到输出目录。"""
        try:
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            path = await self._download_one(it, out_dir)
            self.post("log", t("online.log.downloaded", path=path))
        except Exception as exc:
            self.post("log", t("online.log.dl_single_failed",
                               name=exc.__class__.__name__, exc=exc))
        finally:
            self.post("busy", False)


# --------------------------------------------------------------------------
# 配置读写
# --------------------------------------------------------------------------
def load_config() -> dict:
    """读取本工具的本地配置（路径固定为 config.json）。"""
    return ui_kit.load_config(CONFIG_PATH)


def save_config(data: dict) -> None:
    """把配置合并写回 config.json。

    配置里含 api_hash 等凭据，写后把权限收紧到 0o600（仅当前用户可读写）。
    """
    ui_kit.save_config(CONFIG_PATH, data, mode=0o600)


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class App(LangMixin):
    """在线挑选器主窗口（Frame 网格 + Canvas 滚动）。

    语言切换的通用机制继承自 :class:`ui_lang.LangMixin`，本类只补差异部分。
    """

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.ui_q: queue.Queue = queue.Queue()
        self.worker = Worker(self.ui_q)
        self.items: list[MediaItem] = []
        self.by_id: dict[int, MediaItem] = {}
        self.logged_in = False
        self.busy = False
        self.next_offset = 0
        self.exhausted = False
        self.columns = 0
        self._thumb_done = 0
        self._thumb_total = 0
        self._resize_job = None
        self._pump_job = None
        self.who = ""                 # 已登录显示名（供语言切换时重建标签）
        self._prompt_spec = None      # 待填写的提示（语言键+参数），切换语言时重渲染

        # i18n：先定语言再建界面（首次绘制即用正确语言）；登记表先建、回调后注册。
        i18n.init()
        self._init_lang_support()

        root.title(_app_title())
        root.geometry("1280x880")
        root.minsize(1020, 680)
        root.configure(bg=BG)

        self._build_style()
        self._build_langbar()
        self._build_login()
        self._build_chat()
        self._build_grid()
        self._build_actions()
        self._build_log()
        self._load_config_into_form()

        self._enable_lang_support()

        self._pump_job = self.root.after(80, self._pump)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 样式与布局 ----------
    def _build_style(self) -> None:
        st = ttk.Style()
        ui_style.use_clam(st)
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Sub.TLabel", background=BG, foreground=SUB, font=(FONT, 8))
        st.configure("H.TLabel", background=BG, foreground="#8ab4f8", font=(FONT, 10, "bold"))
        st.configure("TButton", font=(FONT, 9))
        st.configure("Accent.TButton", font=(FONT, 10, "bold"))
        st.configure("TEntry", fieldbackground="#2b2b2b", foreground=FG, insertcolor=FG)
        st.configure("TCheckbutton", background=BG, foreground=FG)
        st.configure("TCombobox", fieldbackground="#2b2b2b", foreground=FG)
        st.configure("Horizontal.TProgressbar", background="#1f6feb", troughcolor="#2b2b2b")
        # 分段按钮：语言切换用（与缓存挑选器一致的表达方式）
        ui_style.segmented_buttons(
            st, base_bg="#2b2b2b", base_fg=SUB, hover_bg="#3a3a3a", hover_fg=FG,
            accent="#1f6feb", accent_hover="#2b7bf5", padding=(10, 3))

    # ---------- i18n（差异部分；通用机制见 ui_lang.LangMixin）----------
    def _window_title(self) -> str:
        return t("app.online_title")

    def _idle_status(self) -> str:
        return t("common.ready")

    def _retranslate_dynamic(self) -> None:
        """重算在线挑选器特有的动态文案。

        通用部分（静态登记、状态栏重放、标题、语言按钮）由
        LangMixin.retranslate 统一处理，这里只补：主按钮计数、登录状态、
        待填写的验证码/密码提示、取不到缩略图的格子占位。
        """
        self.update_button()                       # "下载选中（n）"
        self.l_login.configure(text=self._login_text())
        self._render_prompt()                      # 待填写的验证码/密码提示
        for it in self.items:                      # 单元格里"无预览"占位
            if it.thumb_failed and it.img_label is not None:
                it.img_label.configure(text=t("cell.no_preview"))

    def _build_langbar(self) -> None:
        """顶部语言切换条（置顶右侧）。"""
        bar = ttk.Frame(self.root, padding=(12, 6, 12, 0))
        bar.pack(fill="x")
        self._build_lang_switch(bar, with_label=True)

    def _login_text(self) -> str:
        """登录状态标签：未登录 / 已登录：<名字>。"""
        if not self.logged_in:
            return t("online.login.not_logged")
        return t("online.logged_in", who=self.who or t("online.current_account"))

    def _render_prompt(self) -> None:
        """按当前语言重渲染待填写的提示（若该行正在显示）。"""
        if self._prompt_spec is None:
            return
        key, kw = self._prompt_spec
        self.l_prompt.configure(text=t(key, **kw))

    def _build_login(self) -> None:
        f = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        f.pack(fill="x")
        self._login_frame = f            # 验证码行要插在它下面
        self._T(ttk.Label(f, style="H.TLabel"), "online.login.title").grid(
            row=0, column=0, columnspan=8, sticky="w", pady=(0, 6)
        )
        self._T(ttk.Label(f), "online.login.api_id").grid(row=1, column=0, sticky="w")
        self.e_api_id = ttk.Entry(f, width=12)
        self.e_api_id.grid(row=1, column=1, sticky="w", padx=(4, 12))
        self._T(ttk.Label(f), "online.login.api_hash").grid(row=1, column=2, sticky="w")
        self.e_api_hash = ttk.Entry(f, width=34)
        self.e_api_hash.grid(row=1, column=3, sticky="w", padx=(4, 12))
        self._T(ttk.Label(f), "online.login.phone").grid(row=1, column=4, sticky="w")
        self.e_phone = ttk.Entry(f, width=18)
        self.e_phone.grid(row=1, column=5, sticky="w", padx=(4, 12))
        self.b_login = self._T(ttk.Button(f, command=self.on_login), "online.login.btn")
        self.b_login.grid(row=1, column=6, sticky="w")
        self.l_login = ttk.Label(f, text=self._login_text(), style="Sub.TLabel")
        self.l_login.grid(row=1, column=7, sticky="w", padx=(10, 0))

        # 验证码/密码输入行（默认隐藏，收到后台请求时显示）
        self.pf = ttk.Frame(self.root, padding=(12, 0, 12, 4))
        self._T(ttk.Label(self.pf), "online.login.code_label").grid(row=0, column=0, sticky="w")
        self.e_prompt = ttk.Entry(self.pf, width=28, show="")
        self.e_prompt.grid(row=0, column=1, sticky="w", padx=(4, 8))
        self._T(ttk.Button(self.pf, command=self.on_prompt_submit),
                "common.submit").grid(row=0, column=2)
        self.l_prompt = ttk.Label(self.pf, text="", style="Sub.TLabel")
        self.l_prompt.grid(row=0, column=3, sticky="w", padx=(10, 0))
        self.e_prompt.bind("<Return>", lambda e: self.on_prompt_submit())
        self.prompt_kind = None

    def _build_chat(self) -> None:
        f = ttk.Frame(self.root, padding=(12, 6, 12, 4))
        f.pack(fill="x")
        self._T(ttk.Label(f, style="H.TLabel"), "online.chat.title").grid(
            row=0, column=0, columnspan=10, sticky="w", pady=(0, 6))
        self._T(ttk.Label(f), "online.chat.chat_label").grid(row=1, column=0, sticky="w")
        self.e_chat = ttk.Entry(f, width=42)
        self.e_chat.grid(row=1, column=1, columnspan=2, sticky="we", padx=(4, 12))
        self._T(ttk.Label(f, style="Sub.TLabel"), "online.chat.chat_hint").grid(
            row=1, column=3, sticky="w", padx=(0, 16))
        self._T(ttk.Label(f), "online.chat.count_label").grid(row=1, column=4, sticky="w")
        self.cb_count = ttk.Combobox(f, width=6, state="readonly",
                                     values=("30", "60", "100", "200", "500"))
        self.cb_count.set("60")
        self.cb_count.grid(row=1, column=5, sticky="w", padx=(4, 16))
        self._T(ttk.Label(f), "online.chat.kind_label").grid(row=1, column=6, sticky="w")
        self.v_photo = tk.BooleanVar(value=True)
        self.v_video = tk.BooleanVar(value=False)
        self.v_file = tk.BooleanVar(value=False)
        self._T(ttk.Checkbutton(f, variable=self.v_photo),
                "kind.image").grid(row=1, column=7, sticky="w")
        self._T(ttk.Checkbutton(f, variable=self.v_video),
                "kind.video").grid(row=1, column=8, sticky="w")
        self._T(ttk.Checkbutton(f, variable=self.v_file),
                "kind.file").grid(row=1, column=9, sticky="w")
        self.b_load = self._T(ttk.Button(f, command=self.on_load), "online.chat.load_btn")
        self.b_load.grid(row=1, column=10, sticky="w", padx=(12, 4))
        self.b_more = self._T(ttk.Button(f, command=self.on_load_more, state="disabled"),
                              "online.chat.more_btn")
        self.b_more.grid(row=1, column=11, sticky="w")
        f.columnconfigure(1, weight=1)

    def _build_grid(self) -> None:
        wrap = ttk.Frame(self.root, padding=(12, 6, 12, 4))
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        vbar = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")
        self.inner = tk.Frame(self.canvas, bg=BG)
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._on_wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _build_actions(self) -> None:
        f = ttk.Frame(self.root, padding=(12, 4, 12, 4))
        f.pack(fill="x")
        self._T(ttk.Label(f), "online.actions.out_label").grid(row=0, column=0, sticky="w")
        self.e_out = ttk.Entry(f, width=58)
        self.e_out.insert(0, str(DEFAULT_OUT))
        self.e_out.grid(row=0, column=1, sticky="we", padx=(4, 6))
        self._T(ttk.Button(f, command=self.on_pick_dir), "common.browse").grid(
            row=0, column=2, padx=(0, 12))
        self._T(ttk.Button(f, command=lambda: self.set_all(True)), "common.select_all").grid(
            row=0, column=3, padx=(0, 4))
        self._T(ttk.Button(f, command=lambda: self.set_all(False)), "common.clear_sel").grid(
            row=0, column=4, padx=(0, 12))
        # b_dl 的文本由 update_button() 拥有，故用 t() 设初值但不登记
        self.b_dl = ttk.Button(f, text=t("online.download_btn", n=0), style="Accent.TButton",
                               command=self.on_download)
        self.b_dl.grid(row=0, column=5, padx=(0, 8))
        self.b_cancel = self._T(ttk.Button(f, command=self.on_cancel, state="disabled"),
                                "common.cancel")
        self.b_cancel.grid(row=0, column=6)
        f.columnconfigure(1, weight=1)

        f2 = ttk.Frame(self.root, padding=(12, 0, 12, 6))
        f2.pack(fill="x")
        self.pb = ttk.Progressbar(f2, mode="determinate", maximum=100)
        self.pb.pack(side="left", fill="x", expand=True)
        self.l_status = ttk.Label(f2, text=t("common.ready"), style="Sub.TLabel")
        self.l_status.pack(side="left", padx=(10, 0))

    def _build_log(self) -> None:
        f = ttk.Frame(self.root, padding=(12, 0, 12, 10))
        f.pack(fill="both")
        self.txt = tk.Text(f, height=7, bg="#141414", fg="#c8c8c8", insertbackground=FG,
                           relief="flat", font=("Consolas", 9), wrap="none")
        sb = ttk.Scrollbar(f, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set, state="disabled")
        self.txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    # ---------- 事件 ----------
    def _load_config_into_form(self) -> None:
        """把上次的凭据回填到登录表单（首次为空）。"""
        cfg = load_config()
        if cfg.get("api_id"):
            self.e_api_id.insert(0, str(cfg["api_id"]))
        if cfg.get("api_hash"):
            self.e_api_hash.insert(0, cfg["api_hash"])
        if cfg.get("phone"):
            self.e_phone.insert(0, cfg["phone"])

    def on_login(self) -> None:
        """校验凭据格式后提交登录任务（首次会走验证码流程）。"""
        try:
            api_id = int(self.e_api_id.get().strip())
        except ValueError:
            messagebox.showwarning(_app_title(), t("online.dialog.api_id_num"))
            return
        api_hash = self.e_api_hash.get().strip()
        phone = self.e_phone.get().strip()
        if not api_hash or not phone:
            messagebox.showwarning(_app_title(), t("online.dialog.need_hash_phone"))
            return
        self.set_busy(True)
        self.worker.submit(self.worker.login(api_id, api_hash, phone),
                           label=t("online.task.login"))

    def on_prompt_submit(self) -> None:
        """把验证码/密码回填给等待中的后台协程，并收起输入行。"""
        if self.prompt_kind is None:
            return
        value = self.e_prompt.get()
        self.e_prompt.delete(0, "end")
        self.pf.pack_forget()
        self.prompt_kind = None
        self._prompt_spec = None
        self.worker.prompter.provide(value)

    def on_load(self) -> None:
        """清空当前结果并从头加载一批媒体。"""
        self.items.clear()
        self.by_id.clear()
        for w in self.inner.winfo_children():
            w.destroy()
        self.next_offset = 0
        self.b_more.configure(state="disabled")
        self.update_button()
        self._do_load(more=False)

    def on_load_more(self) -> None:
        """继续加载更早的媒体（沿用当前筛选条件与分页游标）。"""
        if not self.items:
            self._do_load(more=False)
            return
        self._do_load(more=True)

    def _do_load(self, more: bool) -> None:
        """收集筛选条件并提交加载任务；``more=True`` 时从上次游标继续。"""
        chat = self.e_chat.get().strip()
        kinds = set()
        if self.v_photo.get():
            kinds.add("photo")
        if self.v_video.get():
            kinds.add("video")
        if self.v_file.get():
            kinds.add("file")
        if not kinds:
            messagebox.showwarning(_app_title(), t("online.dialog.need_kind"))
            return
        try:
            target = int(self.cb_count.get())
        except ValueError:
            target = 60
        offset = self.next_offset if more else 0
        self.set_busy(True)
        self.worker.submit(
            self.worker.load_media(chat, target, kinds, offset), label=t("online.task.load")
        )

    def on_download(self) -> None:
        """下载全部已选中的媒体到输出目录。"""
        sel = [it for it in self.items if it.selected]
        if not sel:
            messagebox.showinfo(_app_title(), t("online.dialog.nothing_selected"))
            return
        out = self.e_out.get().strip() or str(DEFAULT_OUT)
        self.set_busy(True)
        self.worker.submit(self.worker.download_items(sel, Path(out)),
                           label=t("online.task.download"))

    def on_cancel(self) -> None:
        """请求取消当前任务。若正卡在等待验证码/密码，同时唤醒后台协程。"""
        self.worker.cancel.set()
        self.worker.prompter.provide(None)
        self.log(t("online.log.cancel_req"))

    def on_pick_dir(self) -> None:
        """选择下载输出目录并回填到输入框。"""
        chosen = filedialog.askdirectory(initialdir=self.e_out.get() or str(APP_DIR))
        if chosen:
            self.e_out.delete(0, "end")
            self.e_out.insert(0, chosen)

    # ---------- 缩略图网格 ----------
    def add_item_cell(self, it: MediaItem) -> None:
        """为一条媒体创建网格单元格（图片位 + 信息行 + 选中标记）并绑定事件。"""
        cell = tk.Frame(self.inner, bg=CARD, width=CELL_W, height=CELL_H, bd=0,
                        highlightthickness=2, highlightbackground=CARD)
        cell.grid_propagate(False)
        cell.pack_propagate(False)
        img_label = tk.Label(cell, bg=CARD, fg=SUB, text=t("cell.loading"),
                             font=(FONT, 9))
        img_label.pack(expand=True)
        info = tk.Label(
            cell,
            bg=CARD,
            fg=SUB,
            font=(FONT, 7),
            text=f"{it.date:%m-%d %H:%M}  {human_size(it.size)}",
        )
        info.pack(side="bottom", pady=(0, 3))
        mark = tk.Label(cell, bg=CARD, fg="#ffffff", text="", font=(FONT, 10, "bold"))
        mark.place(x=6, y=4)
        it.cell, it.img_label, it.mark_label = cell, img_label, mark
        for w in (cell, img_label, info, mark):
            w.bind("<Button-1>", lambda e, i=it: self.toggle_item(i))
            w.bind("<Double-Button-1>", lambda e, i=it: self.preview_item(i))
            w.bind("<Enter>", lambda e, i=it: self.hover_item(i, True))
            w.bind("<Leave>", lambda e, i=it: self.hover_item(i, False))

    def hover_item(self, it: MediaItem, on: bool) -> None:
        """鼠标进出单元格时切换底色（已选中项保持选中底色）。"""
        if it.selected:
            return
        color = CARD_HOVER if on else CARD
        for w in (it.cell, it.img_label, it.mark_label):
            w.configure(bg=color)

    def toggle_item(self, it: MediaItem) -> None:
        """单击切换该媒体的选中态并刷新计数。"""
        it.selected = not it.selected
        color = CARD_SEL if it.selected else CARD
        it.cell.configure(bg=color, highlightbackground=color)
        it.img_label.configure(bg=color)
        it.mark_label.configure(bg=color, text="✓" if it.selected else "")
        self.update_button()

    def preview_item(self, it: MediaItem) -> None:
        """双击：下载该原件。"""
        out = self.e_out.get().strip() or str(DEFAULT_OUT)
        self.set_busy(True)
        self.worker.submit(self.worker.download_single(it, Path(out)),
                           label=t("online.task.preview"))

    def set_all(self, value: bool) -> None:
        """全选或清空选择（对应"全选" / "清除选择"按钮）。"""
        for it in self.items:
            if it.selected != value:
                it.selected = value
                color = CARD_SEL if value else CARD
                it.cell.configure(bg=color, highlightbackground=color)
                it.img_label.configure(bg=color)
                it.mark_label.configure(bg=color, text="✓" if value else "")
        self.update_button()

    def update_button(self) -> None:
        """刷新主按钮上的选中计数（语言切换时也会重算）。"""
        n = sum(1 for it in self.items if it.selected)
        self.b_dl.configure(text=t("online.download_btn", n=n))

    def relayout(self) -> None:
        """按画布宽度重排列数；列数未变时直接返回，避免无谓重排。"""
        width = max(self.canvas.winfo_width(), 320)
        cols = max(1, (width - 10) // CELL_W)
        if cols == self.columns:
            return
        self.columns = cols
        for idx, it in enumerate(self.items):
            if it.cell is not None:
                it.cell.grid(row=idx // cols, column=idx % cols, padx=5, pady=5)

    def _on_canvas_resize(self, event) -> None:
        self.canvas.itemconfigure(self.win, width=event.width)
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(120, self.relayout)

    def _on_wheel(self, event) -> None:
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # ---------- 队列轮询 ----------
    def _pump(self) -> None:
        """定期排空消息队列。

        单条消息处理失败绝不能中断轮询本身——否则 after 链断开，界面会永久假死。
        """
        while True:
            try:
                msg = self.ui_q.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle(msg)
            except Exception:
                traceback.print_exc()
        self._pump_job = self.root.after(PUMP_MS, self._pump)

    def _handle(self, msg: tuple) -> None:
        """按消息类型分发到 ``self._msg_<类型>``；未定义的类型直接忽略。

        消息由 Worker 线程经 ``ui_q`` 投递，统一在这里（主线程）消费。
        """
        handler = getattr(self, f"_msg_{msg[0]}", None)
        if handler is not None:
            handler(msg)

    def _msg_log(self, msg: tuple) -> None:
        """后台日志：原样追加到日志框。"""
        self.log(msg[1])

    def _msg_prompt(self, msg: tuple) -> None:
        """后台索要验证码/密码：显示输入行并聚焦。"""
        _, pkind, key, kw = msg
        self.prompt_kind = pkind
        self._prompt_spec = (key, kw)          # 供切换语言时重渲染
        self.l_prompt.configure(text=t(key, **kw))
        self.e_prompt.configure(show="*" if pkind == "password" else "")
        self.pf.pack(fill="x", after=self._login_frame)
        self.e_prompt.focus_set()

    def _msg_logged_in(self, msg: tuple) -> None:
        """登录成功：记住显示名并刷新登录状态标签。"""
        self.logged_in = True
        self.who = msg[1] or ""
        self.l_login.configure(text=self._login_text())

    def _msg_busy(self, msg: tuple) -> None:
        """忙碌态变更：统一由 set_busy 处理按钮可用性。"""
        self.set_busy(bool(msg[1]))

    def _msg_media_loaded(self, msg: tuple) -> None:
        """新一批媒体：建格子、重排、更新分页游标与进度基准。"""
        _, items, cursor, exhausted = msg
        self.next_offset = cursor or 0
        self.exhausted = bool(exhausted)
        for it in items:
            self.items.append(it)
            self.by_id[it.msg_id] = it
            self.add_item_cell(it)
        self.columns = 0
        self.relayout()
        self._thumb_total = len(items)
        self._thumb_done = 0
        self.b_more.configure(state="disabled" if exhausted else "normal")
        if items:
            self.set_status("online.status.loaded", n=len(items))

    def _msg_thumb(self, msg: tuple) -> None:
        """单张缩略图到位（或确认取不到）：替换格子内容并推进进度。"""
        _, mid, img = msg
        it = self.by_id.get(mid)
        self._thumb_done += 1
        if it is not None and it.img_label is not None:
            if img is None:
                it.thumb_failed = True
                it.img_label.configure(text=t("cell.no_preview"), image="")
            else:
                it.thumb = img
                it.photo = ImageTk.PhotoImage(img)
                it.img_label.configure(image=it.photo, text="")
        if self._thumb_total:
            self.set_status("online.status.thumbs",
                            done=self._thumb_done, total=self._thumb_total)
            if self._thumb_done >= self._thumb_total:
                self.set_status("online.status.ready_hint")

    def _msg_progress(self, msg: tuple) -> None:
        """下载进度：更新进度条与状态栏。"""
        _, done, total = msg
        self.pb.configure(value=(done / total * 100) if total else 0)
        self.set_status("online.status.downloading", done=done, total=total)

    def _msg_download_done(self, msg: tuple) -> None:
        """下载完成：提示结果。"""
        _, _out, ok, fail = msg
        self.pb.configure(value=100)
        self.set_status("online.status.done", ok=ok, fail=fail)

    # ---------- 小工具 ----------
    def log(self, text: str) -> None:
        """向日志框追加一行并滚到底部（日志框常态只读）。"""
        self.txt.configure(state="normal")
        self.txt.insert("end", text + "\n")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def set_busy(self, busy: bool) -> None:
        """切换忙碌态：统一控制按钮可用性、取消按钮与进度条。"""
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.b_login, self.b_load, self.b_dl):
            b.configure(state=state)
        more_state = "disabled" if (busy or self.exhausted or not self.items) else "normal"
        self.b_more.configure(state=more_state)
        self.b_cancel.configure(state="normal" if busy else "disabled")
        if not busy:
            self.pb.configure(value=0)

    def _on_close(self) -> None:
        """关闭窗口：取消后台任务、唤醒可能正阻塞等待输入的协程，再销毁窗口。"""
        try:
            self.worker.cancel.set()
            self.worker.prompter.provide(None)
        except Exception:
            pass
        self._disable_lang_support()
        for job in (self._pump_job, self._resize_job):
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
        self.root.destroy()


def main() -> None:
    """入口：建窗口并进入 Tk 主循环。"""
    ui_kit.enable_file_log(APP_DIR / "logs" / "tg_picker.log")
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
