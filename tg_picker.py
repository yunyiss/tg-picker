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
from urllib.parse import unquote, urlsplit

from PIL import Image, ImageTk
from telethon import TelegramClient, errors, functions
from telethon.tl.types import (
    DocumentAttributeFilename,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    MessageMediaDocument,
    MessageMediaPhoto,
)

import app_paths
import firstrun
import i18n
import ui_kit
import ui_style
from i18n import t
from ui_kit import FONT, human_size, rounded_pts, unique_path
from ui_lang import LangMixin
from ui_style import (
    ACCENT,
    ACCENT_DIM,
    ACCENT_HOVER,
    BAR,
    BG,
    BORDER,
    BTN,
    CARD,
    CARD_HOVER,
    CARD_SEL,
    FG,
    FIELD,
    MUTED,
    PANEL,
    SEP,
    SUB,
)

APP_DIR = Path(__file__).resolve().parent
# 凭据/会话/默认导出目录统一落在「数据目录」（便携=程序旁 data/，否则用户目录）
DATA_DIR = app_paths.data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
SESSION_PATH = str(DATA_DIR / "tg_picker_session")
DEFAULT_OUT = app_paths.export_dir()   # 与缓存模式共用同一个默认导出目录


def _app_title() -> str:
    """窗口标题（同时用作所有对话框的标题）。"""
    return t("app.online_title")

# 界面配色（含卡片色 CARD / CARD_HOVER / CARD_SEL）全部来自 ui_style，
# 与缓存挑选器**共用同一套**，两模式的选中态视觉因此完全一致。

THUMB = 150          # 缩略图边长
CELL_W = 168         # 单元格宽
CELL_H = 205         # 单元格高
CORNER = 9           # 卡片圆角半径（与缓存模式一致）
CAPTION_H = 26       # 卡片底部信息行占高（图片区在其上方居中）
SCAN_BATCH = 100     # 每次向服务器取多少条消息
SCAN_MAX_MSGS = 6000 # 单次"加载"最多扫描多少条消息，防失控
PUMP_MS = 80         # 消息队列轮询间隔（毫秒）
# 等待用户填写验证码/密码的上限（秒）。只是"界面已不可用"时的安全网：
# 没有它，后台协程会永久阻塞在 Event.wait() 上，busy 永不复位。
PROMPT_TIMEOUT = 300
# 缩略图并发抓取路数：逐条串行 + 每条 sleep 时，一批 500 条要白等 25 秒。
# 改为 N 路并发，控速 sleep 放在并发名额**之外**，兼顾速度与服务端温和度。
THUMB_CONCURRENCY = 3

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
    cell: object = None           # 单元格 Canvas（自绘圆角卡片，与缓存模式同一套画法）
    card: object = None           # 卡片底：圆角多边形图元
    img: object = None            # 缩略图图元
    img_txt: object = None        # 占位文字（载入中 / 无预览）
    info: object = None           # 底部信息文字（日期 · 大小）
    badge_ring: object = None     # 选中徽标：外圈（内容区底色）
    badge: object = None          # 选中徽标：强调色圆底
    badge_txt: object = None      # 选中徽标：白色对勾
    thumb_failed: bool = False    # 缩略图确实取不到（用于语言切换时刷新"无预览"）


def item_name(it: MediaItem) -> str:
    """条目落盘用的文件名（**不含目录**）。

    双击确认与真正下载共用同一套命名，避免两处规则漂移。
    """
    date = it.date if isinstance(it.date, datetime) else datetime.now()
    if it.file_name:
        return f"{sanitize(Path(it.file_name).stem)}{ext_for(it)}"
    return f"{it.kind}_{date:%Y%m%d_%H%M%S}_{it.msg_id}{ext_for(it)}"


_PROXY_KINDS = {"socks5": "socks5", "socks4": "socks4", "http": "http", "https": "http"}


def parse_proxy(text: str) -> tuple | None:
    """把代理字符串解析成 Telethon 的 ``proxy`` 参数；无法解析返回 ``None``。

    支持 ``host:port``（默认按 socks5）、``socks5://host:port``、
    ``http://user:pass@host:port`` 等写法。
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        u = urlsplit(text if "://" in text else f"socks5://{text}")
        kind = _PROXY_KINDS.get(u.scheme.lower())
        if kind is None or not u.hostname or not u.port:
            return None
    except ValueError:
        return None
    if u.username:
        return (kind, u.hostname, u.port, True,
                unquote(u.username), unquote(u.password or ""))
    return (kind, u.hostname, u.port)


def mask_proxy(text: str) -> str:
    """把代理串里的账号密码掩码后再显示 / 记日志（凭据不该进日志文件）。

    ``http://user:pass@host:port`` → ``http://***:***@host:port``；
    ``user:pass@host:port`` → ``***:***@host:port``；不含凭据的原样返回。
    """
    text = (text or "").strip()
    creds, at, host = text.rpartition("@")
    if not at:
        return text
    scheme = creds.split("://", 1)[0] + "://" if "://" in creds else ""
    return f"{scheme}***:***@{host}"


def resolve_proxy_text() -> str:
    """代理来源优先级：环境变量 ``TG_PICKER_PROXY`` > 配置里的 ``proxy``。"""
    env = os.environ.get("TG_PICKER_PROXY", "").strip()
    if env:
        return env
    value = load_config().get("proxy")
    return value.strip() if isinstance(value, str) else ""


def harden_session_file() -> None:
    """把 Telethon 会话文件的权限收紧到「仅本人可读写」。

    该文件是明文 SQLite、内含登录密钥，等同登录凭证；而它由 Telethon 自己创建、
    不设权限，POSIX 上会跟随 umask（常见 0644 ⇒ 同机其它用户可读即可接管账号）。
    因此启动时与登录成功后各收一次权限（含 SQLite 的 ``-journal`` / ``-wal`` 兄弟文件）。

    **只在 POSIX 上真正生效**：Windows 的 ``os.chmod`` 只能切换只读位、不构成访问
    控制，Windows 下的保护取决于数据目录自身的 ACL（见 README「安全边界」）。
    文件不存在或收权限失败一律忽略——这是加固，不该影响使用。
    """
    for suffix in ("", "-journal", "-wal"):
        path = Path(f"{SESSION_PATH}.session{suffix}")
        try:
            if path.exists():
                path.chmod(0o600)
        except OSError:
            pass


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

    def shutdown(self) -> None:
        """关闭窗口时收尾：断开 Telegram 连接（顺带 flush 会话文件）并停掉后台循环。

        以前只置取消位就销毁窗口：``loop.run_forever()`` 永不停止、客户端也不
        ``disconnect()``，会话文件与套接字都不是干净关闭（仅靠 daemon 线程在进程
        退出时兜底）。这里把「断开 → 停循环」串成**一个协程**，既不阻塞界面线程，
        又能保证顺序（先断开、后停循环）。
        """
        async def _shutdown() -> None:
            if self.client is not None:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
            self.loop.stop()

        try:
            asyncio.run_coroutine_threadsafe(_shutdown(), self.loop)
        except Exception:
            pass

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
    async def login(self, api_id: int, api_hash: str, phone: str,
                    proxy: tuple | None = None) -> None:
        """连接并登录；需要验证码/两步密码时经 Prompter 向界面索要。

        ``proxy`` 为 Telethon 的代理参数（见 :func:`parse_proxy`）；``None`` 表示直连。
        全程响应取消：收到取消（或等待输入超时）即中止并复位忙碌态。
        """
        try:
            self.cancel.clear()          # 上一次的取消不应影响本次登录
            self.post("log", t("online.log.connecting", api_id=api_id))
            self.client = TelegramClient(SESSION_PATH, api_id, api_hash, proxy=proxy)
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
        """并发拉取缩略图并投递给界面；取不到的投 None（界面显示"无预览"）。

        信号量限制并发路数（``THUMB_CONCURRENCY``），每条仍保留温和节流，但
        ``sleep`` 放在信号量**之外**——否则每条都要占着名额睡，并发会退化成串行。
        整批 ``gather`` 等齐后再返回，与调用方（``busy`` 复位）的语义保持一致。
        """
        sem = asyncio.Semaphore(THUMB_CONCURRENCY)

        async def one(it: MediaItem) -> None:
            if self.cancel.is_set():
                return
            async with sem:
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
            await asyncio.sleep(0.05)  # 温和控速（不占并发名额）

        await asyncio.gather(*(one(it) for it in items))

    # ----- 下载 -----
    async def _download_one(self, it: MediaItem, out_dir: Path) -> Path:
        """下载一个媒体原件到 out_dir，返回路径。带 FloodWait 重试。"""
        date = it.date if isinstance(it.date, datetime) else datetime.now()
        path = unique_path(out_dir / sanitize(item_name(it), 150))

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
                name = item_name(it)
                self.post("progress", idx - 1, total, name)
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
                self.post("progress", idx, total, name)
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

    _lang_box_style = "Panel.TFrame"     # 语言按钮所在容器的 ttk 样式（面板底）

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
        self.last_click: int | None = None   # 上次单击的条目下标（供 Shift 连选）

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
        harden_session_file()               # 旧版本遗留的松权限会话文件，启动时一并收紧

        self._enable_lang_support()

        self._pump_job = self.root.after(80, self._pump)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 样式与布局 ----------
    def _build_style(self) -> None:
        """ttk 样式：与缓存挑选器**同一套**深色主题（面板 / 文字三级色阶 / 扁平控件）。

        clam 的输入框描边、滚动条、进度条、复选框指示块都由**自绘图片**构成，
        只设 ``background`` 会留下浅色边缘；因此输入框 / 复选 / 滚动条都要连
        ``bordercolor`` / ``lightcolor`` / ``darkcolor`` 一起覆盖（复选还要
        ``upperbordercolor`` / ``lowerbordercolor``）。
        """
        st = ttk.Style()
        ui_style.use_clam(st)
        # 容器：内容区 BG / 面板 PANEL / 状态栏 BAR / 分隔线 SEP
        st.configure("TFrame", background=BG)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("Bar.TFrame", background=BAR)
        st.configure("Sep.TFrame", background=SEP)
        # 文字三级色阶（标签底随所在面板）；标题/分区标题与缓存模式同款
        st.configure("TLabel", background=PANEL, foreground=FG, font=(FONT, 9))
        st.configure("Title.TLabel", background=PANEL, foreground=FG,
                     font=(FONT, 12, "bold"))
        st.configure("Section.TLabel", background=PANEL, foreground=FG,
                     font=(FONT, 11, "bold"))
        st.configure("Sub.TLabel", background=PANEL, foreground=SUB, font=(FONT, 9))
        st.configure("Status.TLabel", background=PANEL, foreground=SUB, font=(FONT, 8))
        st.configure("Hint.TLabel", background=PANEL, foreground=MUTED, font=(FONT, 8))
        st.configure("BarStatus.TLabel", background=BAR, foreground=SUB, font=(FONT, 8))
        st.configure("Count.TLabel", background=PANEL, foreground=ACCENT,
                     font=(FONT, 10, "bold"))
        # 网格区空状态提示（底为内容区 BG，故不随 PANEL）
        st.configure("Empty.TLabel", background=BG, foreground=SUB, font=(FONT, 10))
        # 按钮：统一高度、压掉 clam 的立体边框
        st.configure("TButton", font=(FONT, 9), padding=(12, 6),
                     background=BTN, foreground=FG, borderwidth=0, focusthickness=0,
                     relief="flat", bordercolor=BTN, lightcolor=BTN, darkcolor=BTN)
        st.map("TButton",
               background=[("pressed", "#3a3a44"), ("active", "#383842"),
                           ("disabled", PANEL)],
               foreground=[("disabled", MUTED)],
               bordercolor=[("active", BTN)], lightcolor=[("active", BTN)],
               darkcolor=[("active", BTN)])
        st.configure("Accent.TButton", font=(FONT, 10, "bold"), padding=(18, 6),
                     background=ACCENT, foreground="#ffffff", borderwidth=0,
                     relief="flat", bordercolor=ACCENT, lightcolor=ACCENT,
                     darkcolor=ACCENT)
        st.map("Accent.TButton",
               background=[("pressed", "#2f6fd8"), ("active", ACCENT_HOVER),
                           ("disabled", ACCENT_DIM)],
               foreground=[("disabled", "#93a7c4")],
               bordercolor=[("active", ACCENT_HOVER)],
               lightcolor=[("active", ACCENT_HOVER)],
               darkcolor=[("active", ACCENT_HOVER)])
        # 输入框 / 下拉框：显式压掉 clam 的浅色描边
        st.configure("TEntry", fieldbackground=FIELD, foreground=FG, insertcolor=FG,
                     borderwidth=1, padding=5, relief="flat",
                     bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        st.map("TEntry",
               fieldbackground=[("disabled", PANEL), ("focus", FIELD)],
               foreground=[("disabled", MUTED)],
               bordercolor=[("focus", ACCENT)],
               lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)])
        st.configure("TCombobox", fieldbackground=FIELD, background=FIELD,
                     foreground=FG, arrowcolor=SUB, borderwidth=1, padding=4,
                     relief="flat", bordercolor=BORDER, lightcolor=BORDER,
                     darkcolor=BORDER)
        st.map("TCombobox",
               fieldbackground=[("readonly", FIELD)], foreground=[("readonly", FG)],
               bordercolor=[("focus", ACCENT)],
               lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)])
        # 下拉列表本身（Tk 的 Listbox）也要跟着深色
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        # 复选框（clam 的 indicator 用 upper/lowerbordercolor 才生效）
        st.configure("TCheckbutton", background=PANEL, foreground=FG, font=(FONT, 9),
                     focusthickness=0, focuscolor=PANEL, indicatorbackground=FIELD,
                     indicatorforeground=ACCENT, upperbordercolor=BORDER,
                     lowerbordercolor=BORDER, indicatorsize=13)
        st.map("TCheckbutton",
               background=[("active", PANEL)], foreground=[("active", FG)],
               indicatorbackground=[("selected", ACCENT), ("!selected", FIELD)],
               indicatorforeground=[("selected", "#ffffff")],
               upperbordercolor=[("selected", ACCENT), ("!selected", BORDER)],
               lowerbordercolor=[("selected", ACCENT), ("!selected", BORDER)])
        # 进度条 / 滚动条：细轨 + 深槽
        st.configure("Horizontal.TProgressbar", background=ACCENT,
                     troughcolor="#14141a", borderwidth=0, thickness=6)
        st.configure("Vertical.TScrollbar", background=BTN, troughcolor=BG,
                     bordercolor=BG, arrowcolor=SUB, borderwidth=0,
                     lightcolor=BTN, darkcolor=BTN)
        st.map("Vertical.TScrollbar",
               background=[("active", "#383842"), ("pressed", "#3a3a44")])
        # 分段按钮：语言切换用（与缓存挑选器一致的表达方式）
        ui_style.segmented_buttons(
            st, base_bg=BTN, base_fg=SUB, hover_bg="#383842", hover_fg=FG,
            accent=ACCENT, accent_hover=ACCENT_HOVER, padding=(14, 5))

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
        self.update_count()                       # 「已选 N 个」+「保存选中（n）」
        self.l_login.configure(text=self._login_text())
        self._render_prompt()                      # 待填写的验证码/密码提示
        for it in self.items:                      # 单元格里"无预览"占位
            if it.thumb_failed and it.cell is not None:
                it.cell.itemconfigure(it.img_txt, text=t("cell.no_preview"))

    def _divider(self) -> None:
        """一条 1px 分隔线，用于划分区域（与缓存模式同款）。"""
        ttk.Frame(self.root, style="Sep.TFrame", height=1).pack(fill="x", side="top")

    def _build_langbar(self) -> None:
        """顶部标题栏：左侧模式标题，右侧语言切换（与缓存模式同一版式）。"""
        bar = ttk.Frame(self.root, padding=(16, 8, 16, 4), style="Panel.TFrame")
        bar.pack(fill="x")
        self._build_lang_switch(bar, with_label=True)
        self._T(ttk.Label(bar, style="Title.TLabel"), "app.online_title").pack(side="left")
        self._divider()

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
        """登录 / 代理表单：两行紧凑栅格，且两行各自独立（列宽互不牵连）。

        第 1 行 = 凭据（api_id / api_hash / 手机号）+ 登录按钮 + 登录状态；
        第 2 行 = 代理（短输入框 + 小字提示）。
        两行各用一个子栅格，是因为第 2 行的提示文案很长——若与第 1 行共列，
        会把 `api_hash` 所在列一起撑宽，凭空留出一块空白。每行末尾留弹性空列
        吸收多余宽度，窄窗口下也不挤压控件。
        """
        f = ttk.Frame(self.root, padding=(16, 6, 16, 4), style="Panel.TFrame")
        f.pack(fill="x")
        self._login_frame = f            # 验证码行要插在它下面
        self._T(ttk.Label(f, style="Section.TLabel"), "online.login.title").pack(
            anchor="w", pady=(0, 6))

        # 第 1 行：凭据 + 登录按钮 + 登录状态。**独立子栅格**：不与代理行共列，
        # 避免被下面那行的长提示撑开而留下大空白。
        r1 = ttk.Frame(f, style="Panel.TFrame")
        r1.pack(fill="x")
        self._T(ttk.Label(r1, style="Sub.TLabel"),
                "online.login.api_id").grid(row=0, column=0, sticky="w")
        self.e_api_id = ttk.Entry(r1, width=12)
        self.e_api_id.grid(row=0, column=1, sticky="w", padx=(6, 14))
        self._T(ttk.Label(r1, style="Sub.TLabel"),
                "online.login.api_hash").grid(row=0, column=2, sticky="w")
        self.e_api_hash = ttk.Entry(r1, width=30)
        self.e_api_hash.grid(row=0, column=3, sticky="w", padx=(6, 14))
        self._T(ttk.Label(r1, style="Sub.TLabel"),
                "online.login.phone").grid(row=0, column=4, sticky="w")
        self.e_phone = ttk.Entry(r1, width=15)
        self.e_phone.grid(row=0, column=5, sticky="w", padx=(6, 14))
        self.b_login = self._T(ttk.Button(r1, command=self.on_login), "online.login.btn")
        self.b_login.grid(row=0, column=6, sticky="w", padx=(0, 10))
        self.l_login = ttk.Label(r1, text=self._login_text(), style="Status.TLabel")
        self.l_login.grid(row=0, column=7, sticky="w")
        r1.columnconfigure(8, weight=1)   # 弹性空列：吸收多余宽度，内容左对齐

        # 第 2 行：代理（紧凑）+ 小字提示（同样独立，互不影响列宽）
        r2 = ttk.Frame(f, style="Panel.TFrame")
        r2.pack(fill="x", pady=(6, 0))
        self._T(ttk.Label(r2, style="Sub.TLabel"),
                "online.login.proxy").grid(row=0, column=0, sticky="w")
        self.e_proxy = ttk.Entry(r2, width=24)
        self.e_proxy.grid(row=0, column=1, sticky="w", padx=(6, 8))
        self._T(ttk.Label(r2, style="Hint.TLabel"), "online.login.proxy_hint").grid(
            row=0, column=2, sticky="w")
        r2.columnconfigure(3, weight=1)

        # 验证码/密码输入行（默认隐藏，收到后台请求时显示）
        self.pf = ttk.Frame(self.root, padding=(16, 0, 16, 4), style="Panel.TFrame")
        self._T(ttk.Label(self.pf, style="Sub.TLabel"),
                "online.login.code_label").grid(row=0, column=0, sticky="w")
        self.e_prompt = ttk.Entry(self.pf, width=28, show="")
        self.e_prompt.grid(row=0, column=1, sticky="w", padx=(4, 8))
        self._T(ttk.Button(self.pf, command=self.on_prompt_submit),
                "common.submit").grid(row=0, column=2)
        self.l_prompt = ttk.Label(self.pf, text="", style="Status.TLabel")
        self.l_prompt.grid(row=0, column=3, sticky="w", padx=(10, 0))
        self.e_prompt.bind("<Return>", lambda e: self.on_prompt_submit())
        self.prompt_kind = None
        self._divider()

    def _build_chat(self) -> None:
        f = ttk.Frame(self.root, padding=(16, 6, 16, 6), style="Panel.TFrame")
        f.pack(fill="x")
        self._T(ttk.Label(f, style="Section.TLabel"), "online.chat.title").grid(
            row=0, column=0, columnspan=10, sticky="w", pady=(0, 6))
        self._T(ttk.Label(f, style="Sub.TLabel"),
                "online.chat.chat_label").grid(row=1, column=0, sticky="w")
        self.e_chat = ttk.Entry(f, width=42)
        self.e_chat.grid(row=1, column=1, columnspan=2, sticky="we", padx=(4, 12))
        self._T(ttk.Label(f, style="Hint.TLabel"), "online.chat.chat_hint").grid(
            row=1, column=3, sticky="w", padx=(0, 16))
        self._T(ttk.Label(f, style="Sub.TLabel"),
                "online.chat.count_label").grid(row=1, column=4, sticky="w")
        self.cb_count = ttk.Combobox(f, width=6, state="readonly",
                                     values=("30", "60", "100", "200", "500"))
        self.cb_count.set("60")
        self.cb_count.grid(row=1, column=5, sticky="w", padx=(4, 16))
        self._T(ttk.Label(f, style="Sub.TLabel"),
                "online.chat.kind_label").grid(row=1, column=6, sticky="w")
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
        wrap = ttk.Frame(self.root, padding=(16, 6, 8, 6))
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        vbar = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y", padx=(6, 0))
        self.inner = tk.Frame(self.canvas, bg=BG)
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._on_wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

        # 空状态：无内容时在网格区居中显示的内联提示（与缓存模式同一风格）
        self.l_empty = self._T(
            ttk.Label(self.canvas, style="Empty.TLabel", justify="center"),
            "online.empty_hint")
        self._empty_shown = False
        self._update_empty_hint()

    def _update_empty_hint(self) -> None:
        """无内容时显示空状态提示，有内容时隐藏（两种模式同一交互）。"""
        show = not self.items
        if show == self._empty_shown:
            return
        if show:
            self.l_empty.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self.l_empty.place_forget()
        self._empty_shown = show

    def _build_actions(self) -> None:
        """底部操作区：两行版式，与缓存模式完全一致。

        第 1 行 = 保存到 + 输入框 + 浏览 + 打开目录；
        第 2 行 = 全选/反选/清除选择（左）+ 主按钮/取消（右）。
        """
        f = ttk.Frame(self.root, padding=(16, 6, 16, 6), style="Panel.TFrame")
        f.pack(fill="x")

        # 第 1 行：保存目标
        r1 = ttk.Frame(f, style="Panel.TFrame")
        r1.pack(fill="x")
        self._T(ttk.Label(r1, style="Sub.TLabel"),
                "online.actions.out_label").pack(side="left")
        self.e_out = ttk.Entry(r1, width=58)
        self.e_out.insert(0, str(DEFAULT_OUT))
        self.e_out.pack(side="left", fill="x", expand=True, padx=(8, 8))
        self._T(ttk.Button(r1, command=self.on_pick_dir), "common.browse").pack(
            side="left", padx=(0, 8))
        self._T(ttk.Button(r1, command=self.on_open_dir), "common.open_dir").pack(side="left")

        # 第 2 行：计数 + 选择操作（左）+ 主按钮 / 取消（右）
        r2 = ttk.Frame(f, style="Panel.TFrame")
        r2.pack(fill="x", pady=(8, 0))
        # l_count / b_dl 的文本由 update_count() 拥有，故用 t() 设初值但不登记
        self.l_count = ttk.Label(r2, text=t("common.count_none"),
                                 style="Count.TLabel", width=26)
        self.l_count.pack(side="left", padx=(0, 12))
        self._T(ttk.Button(r2, command=lambda: self.set_all(True)), "common.select_all").pack(
            side="left", padx=(0, 8))
        self._T(ttk.Button(r2, command=self.invert_all), "common.invert").pack(
            side="left", padx=(0, 8))
        self._T(ttk.Button(r2, command=lambda: self.set_all(False)), "common.clear_sel").pack(
            side="left")
        self.b_cancel = self._T(ttk.Button(r2, command=self.on_cancel, state="disabled"),
                                "common.cancel")
        self.b_cancel.pack(side="right")
        # b_dl 的文本由 update_count() 拥有，故用 t() 设初值但不登记
        self.b_dl = ttk.Button(r2, text=t("online.download_btn", n=0), style="Accent.TButton",
                               command=self.on_download)
        self.b_dl.pack(side="right", padx=(0, 8))

        # 进度条在左、状态在右（与缓存模式一致）
        f2 = ttk.Frame(self.root, padding=(16, 4, 16, 6), style="Bar.TFrame")
        f2.pack(fill="x")
        self.l_status = ttk.Label(f2, text=t("common.ready"), style="BarStatus.TLabel")
        self.l_status.pack(side="right")
        self.pb = ttk.Progressbar(f2, mode="determinate", maximum=100)
        self.pb.pack(side="left", fill="x", expand=True, padx=(0, 10))

    def _build_log(self) -> None:
        f = ttk.Frame(self.root, padding=(16, 0, 16, 6))
        f.pack(fill="both")
        # 日志区只占必要高度（默认 4 行，可滚动）——把纵向空间尽量让给缩略图网格
        self.txt = tk.Text(f, height=4, bg="#14141a", fg=SUB, insertbackground=FG,
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
        proxy = resolve_proxy_text()
        if proxy:
            self.e_proxy.insert(0, proxy)

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
        proxy_text = self.e_proxy.get().strip()
        proxy = parse_proxy(proxy_text)
        if proxy_text and proxy is None:
            messagebox.showwarning(_app_title(), t("online.dialog.bad_proxy"))
            return
        save_config({"proxy": proxy_text})       # 记住代理设置，下次启动回填
        if proxy:
            # 掩码后再记：带 user:pass 的代理串不该落进日志文件
            self.log(t("online.log.proxy_set", proxy=mask_proxy(proxy_text)))
        self.set_busy(True)
        self.worker.submit(self.worker.login(api_id, api_hash, phone, proxy=proxy),
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
        self.update_count()
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
        self.log(t("common.cancel_req"))

    def on_pick_dir(self) -> None:
        """选择下载输出目录并回填到输入框。"""
        chosen = filedialog.askdirectory(initialdir=self.e_out.get() or str(DEFAULT_OUT))
        if chosen:
            self.e_out.delete(0, "end")
            self.e_out.insert(0, chosen)

    def on_open_dir(self) -> None:
        """用系统文件管理器打开「保存到」目录（失败则把路径复制到剪贴板）。"""
        path = Path(self.e_out.get().strip() or str(DEFAULT_OUT))
        if not path.is_dir():
            messagebox.showwarning(_app_title(), t("online.dialog.no_out_dir"))
            return
        if ui_kit.open_folder(path):
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(str(path))
        except tk.TclError:
            pass
        self.log(t("online.log.path_copied", path=path))

    # ---------- 缩略图网格 ----------
    def add_item_cell(self, it: MediaItem) -> None:
        """为一条媒体创建一个单元格（自绘圆角卡片 + 缩略图 + 信息行 + 对勾徽标）。

        与缓存模式**同一套画法**：卡片是 Canvas 上的**圆角多边形**（Frame 只能画
        直角），因此圆角、底色、描边、圆形徽标、信息行格式两边完全一致。
        每个单元格是一个固定尺寸的小 Canvas，沿用原有的 grid 布局与滚动结构。
        """
        cell = tk.Canvas(self.inner, width=CELL_W, height=CELL_H, bg=BG,
                         highlightthickness=0, bd=0)
        # 卡片底：圆角多边形（内缩 1px，2px 描边才不会被画布边缘裁掉）
        it.card = cell.create_polygon(0, 0, 0, 0, fill=CARD, outline=CARD,
                                      width=2, smooth=False)
        cell.coords(it.card, *rounded_pts(1, 1, CELL_W - 1, CELL_H - 1, CORNER))

        img_cy = (CELL_H - CAPTION_H) // 2
        it.img = cell.create_image(CELL_W // 2, img_cy, anchor="center")
        it.img_txt = cell.create_text(CELL_W // 2, img_cy, anchor="center",
                                      text=t("cell.loading"), fill=SUB,
                                      font=(FONT, 9))
        # 信息行：与缓存模式同格式、同字号（"MM-DD HH:MM  ·  大小"）
        it.info = cell.create_text(
            CELL_W // 2, CELL_H - 13, anchor="center", fill=SUB, font=(FONT, 9),
            text=f"{it.date:%m-%d %H:%M}  ·  {human_size(it.size)}")
        # 选中徽标：右上角「底色外圈 + 强调色圆底 + 白勾」，与缓存模式同位置同形状
        r, ring = 11, 3
        cx, cy = CELL_W - r - 7, r + 7
        it.badge_ring = cell.create_oval(cx - r - ring, cy - r - ring,
                                         cx + r + ring, cy + r + ring,
                                         fill=BG, outline="", state="hidden")
        it.badge = cell.create_oval(cx - r, cy - r, cx + r, cy + r,
                                    fill=ACCENT, outline="", state="hidden")
        it.badge_txt = cell.create_text(cx, cy, anchor="center", text="✓",
                                        fill="#ffffff", font=(FONT, 11, "bold"),
                                        state="hidden")
        it.cell = cell
        cell.bind("<Button-1>", lambda e, i=it: self.on_cell_click(e, i))
        cell.bind("<Double-Button-1>", lambda e, i=it: self.preview_item(i))
        cell.bind("<Enter>", lambda e, i=it: self.hover_item(i, True))
        cell.bind("<Leave>", lambda e, i=it: self.hover_item(i, False))
        # 滚轮落在卡片上时也能滚动（画布仅在指针进入空白区时才 bind_all）
        cell.bind("<MouseWheel>", self._on_wheel)

    def _paint_cell(self, it: MediaItem, fill: str, outline: str, *,
                    badge: bool = False) -> None:
        """给卡片底统一上色并切换徽标（悬停 / 选中 / 常态共用，避免漏改）。"""
        it.cell.itemconfigure(it.card, fill=fill, outline=outline)
        state = "normal" if badge else "hidden"
        for item in (it.badge_ring, it.badge, it.badge_txt):
            it.cell.itemconfigure(item, state=state)

    def hover_item(self, it: MediaItem, on: bool) -> None:
        """鼠标进出单元格时切换底色（已选中项保持选中底色）。"""
        if it.selected:
            return
        edge = CARD_HOVER if on else CARD
        self._paint_cell(it, edge, edge)

    def set_selected(self, it: MediaItem, value: bool) -> None:
        """设置单条选中态并刷新其外观（单击 / 连选 / 全选 / 反选共用）。

        与缓存模式**同一套视觉**：圆角卡片铺淡蓝底 + 强调色描边 + 右上角圆形对勾
        徽标；缩略图本身保持中性底，不用亮色铺满整卡把画面压掉。
        """
        if it.selected == value:
            return
        it.selected = value
        self._paint_cell(it, CARD_SEL if value else CARD,
                         ACCENT if value else CARD, badge=value)

    def toggle_item(self, it: MediaItem) -> None:
        """单击切换该媒体的选中态并刷新计数。"""
        self.set_selected(it, not it.selected)
        self.update_count()

    def on_cell_click(self, event, it: MediaItem) -> None:
        """单元格单击：Shift+单击按当前顺序**连选**（与缓存模式同一套交互）。

        与缓存模式一致：以被点条目的"当前是否已选"决定整段是选中还是取消，
        再更新"上次单击位置"供下一次 Shift 连选使用。
        """
        idx = self.items.index(it)
        if event.state & 0x0001 and self.last_click is not None:
            lo, hi = sorted((self.last_click, idx))
            turn_on = not it.selected
            for i in range(lo, hi + 1):
                self.set_selected(self.items[i], turn_on)
            self.update_count()
        else:
            self.toggle_item(it)
        self.last_click = idx

    def preview_item(self, it: MediaItem) -> None:
        """双击：**先确认**再下载该原件（避免误触直接落盘）。"""
        out = self.e_out.get().strip() or str(DEFAULT_OUT)
        if not messagebox.askyesno(
            _app_title(), t("online.dialog.confirm_save", out=out, name=item_name(it))
        ):
            return
        self.set_busy(True)
        self.worker.submit(self.worker.download_single(it, Path(out)),
                           label=t("online.task.preview"))

    def set_all(self, value: bool) -> None:
        """全选或清空选择（对应"全选" / "清除选择"按钮）。"""
        for it in self.items:
            self.set_selected(it, value)
        self.update_count()

    def invert_all(self) -> None:
        """反选：对当前已加载的条目取反（与缓存模式一致）。"""
        for it in self.items:
            self.set_selected(it, not it.selected)
        self.update_count()

    def update_count(self) -> None:
        """刷新「已选」计数标签与主按钮文案（选择变化与语言切换时都会重算）。

        与缓存模式同一套措辞（共用键 common.count_none / count_selected）。
        """
        picked = [it for it in self.items if it.selected]
        n = len(picked)
        if n:
            size = sum(it.size or 0 for it in picked)
            self.l_count.configure(
                text=t("common.count_selected", n=n, size=human_size(size)))
        else:
            self.l_count.configure(text=t("common.count_none"))
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
        """登录成功：记住显示名、刷新登录状态标签，并收一次会话文件权限。"""
        self.logged_in = True
        self.who = msg[1] or ""
        self.l_login.configure(text=self._login_text())
        harden_session_file()

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
        self._update_empty_hint()
        if items:
            self.set_status("online.status.loaded", n=len(items))

    def _msg_thumb(self, msg: tuple) -> None:
        """单张缩略图到位（或确认取不到）：替换格子内容并推进进度。"""
        _, mid, img = msg
        it = self.by_id.get(mid)
        self._thumb_done += 1
        if it is not None and it.cell is not None:
            if img is None:
                it.thumb_failed = True
                it.cell.itemconfigure(it.img, image="", state="hidden")
                it.cell.itemconfigure(it.img_txt, text=t("cell.no_preview"),
                                      state="normal")
            else:
                it.thumb = img
                it.photo = ImageTk.PhotoImage(img)
                it.cell.itemconfigure(it.img, image=it.photo, state="normal")
                it.cell.itemconfigure(it.img_txt, state="hidden")
        if self._thumb_total:
            self.set_status("online.status.thumbs",
                            done=self._thumb_done, total=self._thumb_total)
            if self._thumb_done >= self._thumb_total:
                self.set_status("online.status.ready_hint")

    def _msg_progress(self, msg: tuple) -> None:
        """下载进度：更新进度条与状态栏。"""
        _, done, total, name = msg
        self.pb.configure(value=(done / total * 100) if total else 0)
        self.set_status("online.status.downloading", done=done, total=total, name=name)

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
        """关闭窗口：取消后台任务、收尾 Telegram 连接与事件循环，再销毁窗口。"""
        try:
            self.worker.cancel.set()
            self.worker.prompter.provide(None)
        except Exception:
            pass
        self.worker.shutdown()               # 断开连接 + 停掉后台事件循环（不阻塞界面）
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
    root = tk.Tk()
    firstrun.ensure_choice(root)          # 首次启动先确定数据目录
    ui_kit.enable_file_log(app_paths.data_dir() / "logs" / "tg_picker.log")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
