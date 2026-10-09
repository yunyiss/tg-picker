# -*- coding: utf-8 -*-
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
import json
import os
import queue
import re
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import tkinter as tk
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

APP_NAME = "TG 图片挑选下载器"
APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
SESSION_PATH = str(APP_DIR / "tg_picker_session")
DEFAULT_OUT = APP_DIR / "downloads"

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

IMAGE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
VIDEO_EXT = {"video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov", "video/x-matroska": ".mkv"}


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
_ILLEGAL = '<>:"/\\|?*'


def sanitize(name: str, maxlen: int = 120) -> str:
    """把字符串净化成 Windows 可用的文件名片段。"""
    cleaned = "".join("_" if (c in _ILLEGAL or ord(c) < 32) else c for c in name)
    cleaned = cleaned.rstrip(" .")[:maxlen]
    return cleaned or "unnamed"


def human_size(n: int | None) -> str:
    """把字节数格式化成可读文本。"""
    if not n:
        return "-"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def unique_path(path: Path) -> Path:
    """目标已存在时追加 (1)(2)…，绝不覆盖。"""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 10000):
        cand = path.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
    raise RuntimeError(f"命名冲突过多: {path}")


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


def ext_for(item: "MediaItem") -> str:
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


# --------------------------------------------------------------------------
# 后台线程：Telethon 事件循环 + 与 GUI 的队列通信
# --------------------------------------------------------------------------
class Prompter:
    """让后台协程能向 GUI 索要输入（验证码/两步密码）。"""

    def __init__(self, ui_q: "queue.Queue") -> None:
        self.ui_q = ui_q
        self._event = threading.Event()
        self._value: str | None = None

    def ask(self, kind: str, text: str) -> str | None:
        """后台线程调用：发请求并阻塞等待 GUI 回填。"""
        self._event.clear()
        self._value = None
        self.ui_q.put(("prompt", kind, text))
        self._event.wait()
        return self._value

    def provide(self, value: str | None) -> None:
        """GUI 线程调用：回填结果并唤醒后台。"""
        self._value = value
        self._event.set()


class Worker:
    """在独立线程里跑 asyncio，GUI 通过 submit() 投递协程。"""

    def __init__(self, ui_q: "queue.Queue") -> None:
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

    def submit(self, coro, label: str = "任务") -> None:
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def _done(f):
            try:
                f.result()
            except Exception as exc:  # 理论上协程内部已捕获，这里是兜底
                self.ui_q.put(("log", f"[{label}] 异常: {exc.__class__.__name__}: {exc}"))
                self.ui_q.put(("log", traceback.format_exc()))
                self.ui_q.put(("busy", False))

        fut.add_done_callback(_done)

    def post(self, *msg) -> None:
        self.ui_q.put(msg)

    # ----- 登录 -----
    async def login(self, api_id: int, api_hash: str, phone: str) -> None:
        try:
            self.post("log", f"正在连接 Telegram（api_id={api_id}）…")
            self.client = TelegramClient(SESSION_PATH, api_id, api_hash)
            await self.client.connect()

            if not await self.client.is_user_authorized():
                self.post("log", "该会话尚未登录，正在请求验证码…")
                sent = await self.client.send_code_request(phone)
                code = self.prompter.ask("code", f"验证码已发送到 {phone}，请在下方输入：")
                if not code:
                    self.post("log", "已取消登录。")
                    self.post("busy", False)
                    return
                try:
                    await self.client.sign_in(
                        phone=phone, code=code.strip(), phone_code_hash=sent.phone_code_hash
                    )
                except errors.SessionPasswordNeededError:
                    pw = self.prompter.ask("password", "该账号开启了两步验证，请输入密码：")
                    if not pw:
                        self.post("log", "已取消登录。")
                        self.post("busy", False)
                        return
                    await self.client.sign_in(password=pw)

            me = await self.client.get_me()
            who = " ".join(x for x in [getattr(me, "first_name", None), getattr(me, "last_name", None)] if x)
            self.post("log", f"登录成功：{who or '（无名）'} @{getattr(me, 'username', None) or '-'}")
            save_config({"api_id": api_id, "api_hash": api_hash, "phone": phone})
            self.post("logged_in", who)
        except errors.PhoneCodeInvalidError:
            self.post("log", "验证码错误，请重新点击登录。")
        except errors.PhoneCodeExpiredError:
            self.post("log", "验证码已过期，请重新点击登录。")
        except errors.PasswordHashInvalidError:
            self.post("log", "两步验证密码错误，请重新登录。")
        except Exception as exc:
            self.post("log", f"登录失败：{exc.__class__.__name__}: {exc}")
        finally:
            self.post("busy", False)

    # ----- 解析聊天 -----
    async def _resolve(self, chat: str):
        raw = (chat or "").strip()
        if not raw:
            raise ValueError("请填写群/频道")
        # 从链接里抠出标识
        m = re.search(r"(?:t\.me|telegram\.me)/(?:joinchat/|\+)?([^/?#\s]+)", raw, re.I)
        invite = None
        if m:
            token = m.group(1)
            if (("+" in raw) or ("joinchat" in raw.lower())) and re.fullmatch(r"[A-Za-z0-9_-]{10,}", token):
                invite = token
            else:
                raw = token
        if raw.startswith("@"):
            raw = raw[1:]
        if invite:
            self.post("log", f"检测到邀请链接，尝试加入会话…")
            try:
                res = await self.client(functions.messages.ImportChatInviteRequest(hash=invite))
                return res.chats[0] if getattr(res, "chats", None) else await self.client.get_entity(raw)
            except errors.UserAlreadyParticipantError:
                # 已在群里：用 messages.CheckChatInvite 拿 chat_id
                info = await self.client(functions.messages.CheckChatInviteRequest(hash=invite))
                chat_obj = getattr(info, "chat", None)
                if chat_obj is not None:
                    return chat_obj
                raise
        if re.fullmatch(r"-?\d+", raw):
            return await self.client.get_entity(int(raw))
        return await self.client.get_entity(raw)

    # ----- 加载媒体列表 -----
    async def load_media(self, chat: str, target: int, kinds: set[str], offset_id: int | None) -> None:
        """持续翻页直到收集到 target 个目标类型媒体（或消息耗尽）。"""
        try:
            if self.client is None or not await self.client.is_user_authorized():
                self.post("log", "请先登录。")
                self.post("busy", False)
                return
            self.cancel.clear()
            entity = await self._resolve(chat)
            self.post("log", f"已定位会话：{getattr(entity, 'title', None) or getattr(entity, 'username', None) or entity.id}")

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
                self.post("log", f"扫描 {scanned} 条消息，已找到 {len(items)} 个媒体…")
                if last_id is None:
                    break  # 本页没有可用消息，终止
                cursor = last_id
                if len(batch) < SCAN_BATCH:
                    break  # 已经到底

            if not items:
                self.post("log", "该范围内没有符合筛选条件的媒体。")
            else:
                self.post("log", f"本批共 {len(items)} 个媒体，开始生成缩略图…")
            self.post("media_loaded", items, cursor, len(items) < target)
            await self.load_thumbs(items)
        except errors.UsernameNotOccupiedError:
            self.post("log", "找不到该用户名。")
        except errors.ChannelPrivateError:
            self.post("log", "无法访问该会话（私密或你未加入）。")
        except errors.ChatAdminRequiredError:
            self.post("log", "需要管理员权限才能读取该会话。")
        except ValueError as exc:
            self.post("log", str(exc))
        except Exception as exc:
            self.post("log", f"加载失败：{exc.__class__.__name__}: {exc}")
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
                self.post("log", f"触发限速，等待 {exc.seconds} 秒后重试…")
                await asyncio.sleep(exc.seconds + 1)
        raise RuntimeError("unreachable")

    async def download_items(self, items: list[MediaItem], out_dir: Path) -> None:
        try:
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            self.cancel.clear()
            total = len(items)
            ok = fail = 0
            self.post("log", f"开始下载 {total} 个文件到 {out_dir}")
            for idx, it in enumerate(items, 1):
                if self.cancel.is_set():
                    self.post("log", f"已取消：完成 {ok} 个，剩余 {total - idx + 1} 个未下载。")
                    break
                self.post("progress", idx - 1, total)
                try:
                    path = await self._download_one(it, out_dir)
                    ok += 1
                    self.post("log", f"[{idx}/{total}] ✓ {path.name}")
                except errors.FloodWaitError as exc:
                    fail += 1
                    self.post("log", f"[{idx}/{total}] ✗ 限速未恢复（等 {exc.seconds}s），跳过")
                except Exception as exc:
                    fail += 1
                    self.post("log", f"[{idx}/{total}] ✗ {exc.__class__.__name__}: {exc}")
                self.post("progress", idx, total)
                await asyncio.sleep(0.35)  # 温和控速，避免触发限流
            self.post("log", f"下载结束：成功 {ok}，失败 {fail}。")
            self.post("download_done", out_dir, ok, fail)
        except Exception as exc:
            self.post("log", f"下载中断：{exc.__class__.__name__}: {exc}")
        finally:
            self.post("busy", False)

    async def download_single(self, it: MediaItem, out_dir: Path, open_after: bool = True) -> None:
        """双击预览：下载原图并调用系统默认程序打开。"""
        try:
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            path = await self._download_one(it, out_dir)
            self.post("log", f"已下载：{path}")
            if open_after:
                self.post("open_file", str(path))
        except Exception as exc:
            self.post("log", f"下载失败：{exc.__class__.__name__}: {exc}")
        finally:
            self.post("busy", False)


# --------------------------------------------------------------------------
# 配置读写
# --------------------------------------------------------------------------
def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(data: dict) -> None:
    try:
        merged = load_config()
        merged.update(data)
        CONFIG_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class App:
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

        root.title(APP_NAME)
        root.geometry("1180x820")
        root.minsize(940, 620)
        root.configure(bg=BG)

        self._build_style()
        self._build_login()
        self._build_chat()
        self._build_grid()
        self._build_actions()
        self._build_log()
        self._load_config_into_form()

        self.root.after(80, self._pump)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 样式与布局 ----------
    def _build_style(self) -> None:
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Sub.TLabel", background=BG, foreground=SUB, font=("Microsoft YaHei UI", 8))
        st.configure("H.TLabel", background=BG, foreground="#8ab4f8", font=("Microsoft YaHei UI", 10, "bold"))
        st.configure("TButton", font=("Microsoft YaHei UI", 9))
        st.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"))
        st.configure("TEntry", fieldbackground="#2b2b2b", foreground=FG, insertcolor=FG)
        st.configure("TCheckbutton", background=BG, foreground=FG)
        st.configure("TCombobox", fieldbackground="#2b2b2b", foreground=FG)
        st.configure("Horizontal.TProgressbar", background="#1f6feb", troughcolor="#2b2b2b")

    def _build_login(self) -> None:
        f = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        f.pack(fill="x")
        ttk.Label(f, text="① 登录（首次需在 my.telegram.org 申请 api_id / api_hash）", style="H.TLabel").grid(
            row=0, column=0, columnspan=8, sticky="w", pady=(0, 6)
        )
        ttk.Label(f, text="api_id").grid(row=1, column=0, sticky="w")
        self.e_api_id = ttk.Entry(f, width=12)
        self.e_api_id.grid(row=1, column=1, sticky="w", padx=(4, 12))
        ttk.Label(f, text="api_hash").grid(row=1, column=2, sticky="w")
        self.e_api_hash = ttk.Entry(f, width=34)
        self.e_api_hash.grid(row=1, column=3, sticky="w", padx=(4, 12))
        ttk.Label(f, text="手机号").grid(row=1, column=4, sticky="w")
        self.e_phone = ttk.Entry(f, width=18)
        self.e_phone.grid(row=1, column=5, sticky="w", padx=(4, 12))
        self.b_login = ttk.Button(f, text="登录 / 连接", command=self.on_login)
        self.b_login.grid(row=1, column=6, sticky="w")
        self.l_login = ttk.Label(f, text="未登录", style="Sub.TLabel")
        self.l_login.grid(row=1, column=7, sticky="w", padx=(10, 0))

        # 验证码/密码输入行（默认隐藏，收到后台请求时显示）
        self.pf = ttk.Frame(self.root, padding=(12, 0, 12, 4))
        ttk.Label(self.pf, text="验证码/密码").grid(row=0, column=0, sticky="w")
        self.e_prompt = ttk.Entry(self.pf, width=28, show="")
        self.e_prompt.grid(row=0, column=1, sticky="w", padx=(4, 8))
        ttk.Button(self.pf, text="提交", command=self.on_prompt_submit).grid(row=0, column=2)
        self.l_prompt = ttk.Label(self.pf, text="", style="Sub.TLabel")
        self.l_prompt.grid(row=0, column=3, sticky="w", padx=(10, 0))
        self.e_prompt.bind("<Return>", lambda e: self.on_prompt_submit())
        self.prompt_kind = None

    def _build_chat(self) -> None:
        f = ttk.Frame(self.root, padding=(12, 6, 12, 4))
        f.pack(fill="x")
        ttk.Label(f, text="② 选择聊天与筛选", style="H.TLabel").grid(row=0, column=0, columnspan=10, sticky="w", pady=(0, 6))
        ttk.Label(f, text="群/频道").grid(row=1, column=0, sticky="w")
        self.e_chat = ttk.Entry(f, width=42)
        self.e_chat.grid(row=1, column=1, columnspan=2, sticky="we", padx=(4, 12))
        ttk.Label(f, text="（@用户名 / t.me 链接 / 数字ID）", style="Sub.TLabel").grid(row=1, column=3, sticky="w", padx=(0, 16))
        ttk.Label(f, text="加载数量").grid(row=1, column=4, sticky="w")
        self.cb_count = ttk.Combobox(f, width=6, values=("30", "60", "100", "200", "500"), state="readonly")
        self.cb_count.set("60")
        self.cb_count.grid(row=1, column=5, sticky="w", padx=(4, 16))
        ttk.Label(f, text="类型").grid(row=1, column=6, sticky="w")
        self.v_photo = tk.BooleanVar(value=True)
        self.v_video = tk.BooleanVar(value=False)
        self.v_file = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="图片", variable=self.v_photo).grid(row=1, column=7, sticky="w")
        ttk.Checkbutton(f, text="视频", variable=self.v_video).grid(row=1, column=8, sticky="w")
        ttk.Checkbutton(f, text="文件", variable=self.v_file).grid(row=1, column=9, sticky="w")
        self.b_load = ttk.Button(f, text="加载媒体", command=self.on_load)
        self.b_load.grid(row=1, column=10, sticky="w", padx=(12, 4))
        self.b_more = ttk.Button(f, text="加载更早", command=self.on_load_more, state="disabled")
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
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._on_wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _build_actions(self) -> None:
        f = ttk.Frame(self.root, padding=(12, 4, 12, 4))
        f.pack(fill="x")
        ttk.Label(f, text="输出目录").grid(row=0, column=0, sticky="w")
        self.e_out = ttk.Entry(f, width=58)
        self.e_out.insert(0, str(DEFAULT_OUT))
        self.e_out.grid(row=0, column=1, sticky="we", padx=(4, 6))
        ttk.Button(f, text="浏览…", command=self.on_pick_dir).grid(row=0, column=2, padx=(0, 12))
        ttk.Button(f, text="全选", command=lambda: self.set_all(True)).grid(row=0, column=3, padx=(0, 4))
        ttk.Button(f, text="清除选择", command=lambda: self.set_all(False)).grid(row=0, column=4, padx=(0, 12))
        self.b_dl = ttk.Button(f, text="下载选中（0）", style="Accent.TButton", command=self.on_download)
        self.b_dl.grid(row=0, column=5, padx=(0, 8))
        self.b_cancel = ttk.Button(f, text="取消", command=self.on_cancel, state="disabled")
        self.b_cancel.grid(row=0, column=6)
        f.columnconfigure(1, weight=1)

        f2 = ttk.Frame(self.root, padding=(12, 0, 12, 6))
        f2.pack(fill="x")
        self.pb = ttk.Progressbar(f2, mode="determinate", maximum=100)
        self.pb.pack(side="left", fill="x", expand=True)
        self.l_status = ttk.Label(f2, text="就绪", style="Sub.TLabel")
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
        cfg = load_config()
        if cfg.get("api_id"):
            self.e_api_id.insert(0, str(cfg["api_id"]))
        if cfg.get("api_hash"):
            self.e_api_hash.insert(0, cfg["api_hash"])
        if cfg.get("phone"):
            self.e_phone.insert(0, cfg["phone"])

    def on_login(self) -> None:
        try:
            api_id = int(self.e_api_id.get().strip())
        except ValueError:
            messagebox.showwarning(APP_NAME, "api_id 必须是数字")
            return
        api_hash = self.e_api_hash.get().strip()
        phone = self.e_phone.get().strip()
        if not api_hash or not phone:
            messagebox.showwarning(APP_NAME, "请填写 api_hash 和手机号（含国家码，如 +8613800000000）")
            return
        self.set_busy(True)
        self.worker.submit(self.worker.login(api_id, api_hash, phone), label="登录")

    def on_prompt_submit(self) -> None:
        if self.prompt_kind is None:
            return
        value = self.e_prompt.get()
        self.e_prompt.delete(0, "end")
        self.pf.pack_forget()
        self.prompt_kind = None
        self.worker.prompter.provide(value)

    def on_load(self) -> None:
        self.items.clear()
        self.by_id.clear()
        for w in self.inner.winfo_children():
            w.destroy()
        self.next_offset = 0
        self.b_more.configure(state="disabled")
        self.update_button()
        self._do_load(more=False)

    def on_load_more(self) -> None:
        if not self.items:
            self._do_load(more=False)
            return
        self._do_load(more=True)

    def _do_load(self, more: bool) -> None:
        chat = self.e_chat.get().strip()
        kinds = set()
        if self.v_photo.get():
            kinds.add("photo")
        if self.v_video.get():
            kinds.add("video")
        if self.v_file.get():
            kinds.add("file")
        if not kinds:
            messagebox.showwarning(APP_NAME, "请至少勾选一种类型")
            return
        try:
            target = int(self.cb_count.get())
        except ValueError:
            target = 60
        offset = self.next_offset if more else 0
        self.set_busy(True)
        self.worker.submit(
            self.worker.load_media(chat, target, kinds, offset), label="加载媒体"
        )

    def on_download(self) -> None:
        sel = [it for it in self.items if it.selected]
        if not sel:
            messagebox.showinfo(APP_NAME, "还没有选中任何文件（在缩略图上单击即可选中）")
            return
        out = self.e_out.get().strip() or str(DEFAULT_OUT)
        self.set_busy(True)
        self.worker.submit(self.worker.download_items(sel, Path(out)), label="下载")

    def on_cancel(self) -> None:
        self.worker.cancel.set()
        self.log("已请求取消…")

    def on_pick_dir(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.e_out.get() or str(APP_DIR))
        if chosen:
            self.e_out.delete(0, "end")
            self.e_out.insert(0, chosen)

    # ---------- 缩略图网格 ----------
    def add_item_cell(self, it: MediaItem) -> None:
        cell = tk.Frame(self.inner, bg=CARD, width=CELL_W, height=CELL_H, bd=0,
                        highlightthickness=2, highlightbackground=CARD)
        cell.grid_propagate(False)
        cell.pack_propagate(False)
        img_label = tk.Label(cell, bg=CARD, fg=SUB, text="…", font=("Microsoft YaHei UI", 9))
        img_label.pack(expand=True)
        info = tk.Label(
            cell,
            bg=CARD,
            fg=SUB,
            font=("Microsoft YaHei UI", 7),
            text=f"{it.date:%m-%d %H:%M}  {human_size(it.size)}",
        )
        info.pack(side="bottom", pady=(0, 3))
        mark = tk.Label(cell, bg=CARD, fg="#ffffff", text="", font=("Microsoft YaHei UI", 10, "bold"))
        mark.place(x=6, y=4)
        it.cell, it.img_label, it.mark_label = cell, img_label, mark
        for w in (cell, img_label, info, mark):
            w.bind("<Button-1>", lambda e, i=it: self.toggle_item(i))
            w.bind("<Double-Button-1>", lambda e, i=it: self.preview_item(i))
            w.bind("<Enter>", lambda e, i=it: self.hover_item(i, True))
            w.bind("<Leave>", lambda e, i=it: self.hover_item(i, False))

    def hover_item(self, it: MediaItem, on: bool) -> None:
        if it.selected:
            return
        color = CARD_HOVER if on else CARD
        for w in (it.cell, it.img_label, it.mark_label):
            w.configure(bg=color)

    def toggle_item(self, it: MediaItem) -> None:
        it.selected = not it.selected
        color = CARD_SEL if it.selected else CARD
        it.cell.configure(bg=color, highlightbackground=color)
        it.img_label.configure(bg=color)
        it.mark_label.configure(bg=color, text="✓" if it.selected else "")
        self.update_button()

    def preview_item(self, it: MediaItem) -> None:
        """双击：下载该原件并打开。"""
        out = self.e_out.get().strip() or str(DEFAULT_OUT)
        self.set_busy(True)
        self.worker.submit(self.worker.download_single(it, Path(out)), label="预览下载")

    def set_all(self, value: bool) -> None:
        for it in self.items:
            if it.selected != value:
                it.selected = value
                color = CARD_SEL if value else CARD
                it.cell.configure(bg=color, highlightbackground=color)
                it.img_label.configure(bg=color)
                it.mark_label.configure(bg=color, text="✓" if value else "")
        self.update_button()

    def update_button(self) -> None:
        n = sum(1 for it in self.items if it.selected)
        self.b_dl.configure(text=f"下载选中（{n}）")

    def relayout(self) -> None:
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
        try:
            while True:
                msg = self.ui_q.get_nowait()
                self._handle(msg)
        except queue.Empty:
            pass
        self.root.after(80, self._pump)

    def _handle(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "log":
            self.log(msg[1])
        elif kind == "prompt":
            _, pkind, text = msg
            self.prompt_kind = pkind
            self.l_prompt.configure(text=text)
            self.e_prompt.configure(show="*" if pkind == "password" else "")
            self.pf.pack(fill="x", after=self.root.winfo_children()[0])
            self.e_prompt.focus_set()
        elif kind == "logged_in":
            self.logged_in = True
            self.l_login.configure(text=f"已登录：{msg[1] or '当前账号'}")
        elif kind == "busy":
            self.set_busy(bool(msg[1]))
        elif kind == "media_loaded":
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
                self.status(f"加载 {len(items)} 个媒体，缩略图生成中…")
        elif kind == "thumb":
            _, mid, img = msg
            it = self.by_id.get(mid)
            self._thumb_done += 1
            if it is not None and it.img_label is not None:
                if img is None:
                    it.img_label.configure(text="无预览", image="")
                else:
                    it.thumb = img
                    it.photo = ImageTk.PhotoImage(img)
                    it.img_label.configure(image=it.photo, text="")
            if self._thumb_total:
                self.status(f"缩略图 {self._thumb_done}/{self._thumb_total}")
                if self._thumb_done >= self._thumb_total:
                    self.status("就绪：单击选中，双击下载并打开，然后点「下载选中」")
        elif kind == "progress":
            _, done, total = msg
            pct = (done / total * 100) if total else 0
            self.pb.configure(value=pct)
            self.status(f"下载中 {done}/{total}")
        elif kind == "download_done":
            _, out, ok, fail = msg
            self.pb.configure(value=100)
            self.status(f"完成：成功 {ok}，失败 {fail}")
            if messagebox.askyesno(APP_NAME, f"下载完成：成功 {ok}，失败 {fail}。\n\n是否打开输出目录？"):
                try:
                    os.startfile(str(out))
                except Exception as exc:
                    self.log(f"打开目录失败：{exc}")
        elif kind == "open_file":
            try:
                os.startfile(msg[1])
            except Exception as exc:
                self.log(f"打开文件失败：{exc}")

    # ---------- 小工具 ----------
    def log(self, text: str) -> None:
        self.txt.configure(state="normal")
        self.txt.insert("end", text + "\n")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def status(self, text: str) -> None:
        self.l_status.configure(text=text)

    def set_busy(self, busy: bool) -> None:
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
        try:
            self.worker.cancel.set()
        except Exception:
            pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
