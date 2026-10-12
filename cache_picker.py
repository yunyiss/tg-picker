# -*- coding: utf-8 -*-
"""Telegram 缓存挑选器（cache-picker）

直接读取 Telegram Desktop 的 tdata 缓存，边看边挑边保存——不需要先批量导出。

**渲染方式（性能关键）**：
    不使用 Tk 子控件铺网格（几百个 Widget 滚动会掉帧、撕裂），而是用 Canvas
    原生图元 + 可视区虚拟化：只为屏幕上可见的那几行准备图元，滚动时复用、
    仅更新坐标与图像。因此滚轮快速滚动是平滑的，也不会出现像素块错乱。

    - 缩略图在后台线程从磁盘缓存载入，主线程分批转成 PhotoImage
    - 内存中的缩略图数量有上限，超出后淘汰不可见项，避免占用过大

安全边界：**只读 tdata**，绝不修改/删除其中任何文件；保存只写你指定的输出目录。
解密复用 tg-scoop（GarrettFynn/tg-scoop，MIT）。
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import queue
import threading
import time
import tkinter as tk
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import cache_source as cs
from gallery_picker import (
    BG,
    CARD,
    CARD_HOVER,
    CARD_SEL,
    FG,
    SUB,
    human_size,
    unique_path,
)

APP_NAME = "Telegram 缓存挑选器"
HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "cache_config.json"
THUMB_DIR = HERE / "cache_thumbs"
INDEX_PATH = HERE / "cache_index.json"
PREVIEW_DIR = HERE / "preview"
VIDEO_TMP_DIR = HERE / "tmp_video"   # 视频抽帧的临时目录（用完即删）
THUMB = cs.THUMB

# 网格几何
CELL_W, CELL_H = 164, 196
PAD = 6
MAX_THUMBS_IN_MEM = 600      # 内存中保留的缩略图上限
PHOTO_BATCH_PER_TICK = 6     # 每个 UI 周期最多转换几张（避免卡顿）


@dataclass
class Item:
    """网格中的一个缓存媒体。"""

    entry: cs.CacheEntry
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = cs.default_cache_name(self.entry)

    @property
    def kind(self) -> str:
        return self.entry.kind

    @property
    def size(self) -> int:
        return self.entry.size or self.entry.enc_size

    @property
    def mtime(self) -> float:
        return self.entry.mtime


# ---------------------------------------------------------------------------
# 读取与保存（纯函数，便于自检）
# ---------------------------------------------------------------------------
def load_cache(tdata: Path, passcode: str, progress_cb=None, cancel_event=None):
    """读取并解析缓存，返回 (条目, 统计)。"""
    return cs.prepare(
        tdata=tdata,
        passcode=passcode,
        index_path=INDEX_PATH,
        thumb_dir=THUMB_DIR,
        progress_cb=progress_cb,
        cancel_event=cancel_event,
    )


def save_items(items, out_dir: Path, tdata: Path, passcode: str,
               by_date: bool = False, progress_cb=None,
               cancel_event=None, reader: cs.CacheReader | None = None):
    """解密选中项并写出到 out_dir。返回 (成功, 失败)。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if reader is None:
        reader = cs.CacheReader(tdata, passcode)
    ok = fail = 0
    total = len(items)
    for i, it in enumerate(items, 1):
        if cancel_event is not None and cancel_event.is_set():
            break
        try:
            data = reader.decrypt(it.entry.cache_path)
            target_dir = out_dir
            if by_date:
                target_dir = out_dir / datetime.fromtimestamp(it.mtime).strftime("%Y-%m-%d")
                target_dir.mkdir(parents=True, exist_ok=True)
            dest = unique_path(target_dir / it.name)
            dest.write_bytes(data)
            try:
                os.utime(dest, (it.mtime, it.mtime))
            except OSError:
                pass
            ok += 1
        except Exception:
            fail += 1
        if progress_cb is not None:
            progress_cb(i, total, it.name)
    return ok, fail


def open_preview(it: Item, tdata: Path, passcode: str,
                 reader: cs.CacheReader | None = None) -> Path:
    """解密到 preview 目录并返回路径（供双击打开）。"""
    if reader is None:
        reader = cs.CacheReader(tdata, passcode)
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    dest = PREVIEW_DIR / it.name
    dest.write_bytes(reader.decrypt(it.entry.cache_path))
    return dest


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(data: dict) -> None:
    try:
        merged = load_config()
        merged.update(data)
        CONFIG_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# GUI —— Canvas 虚拟化网格
# ---------------------------------------------------------------------------
class CacheApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.ui_q: queue.Queue = queue.Queue()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)

        self.all_items: list[Item] = []
        self.items: list[Item] = []
        self.selected: set[str] = set()
        self.columns = 1
        self.last_click: int | None = None
        self.busy = False
        self.cancel = threading.Event()
        self.tdata: Path | None = None
        self.reader: cs.CacheReader | None = None

        # 缩略图
        self.thumb_mem: "OrderedDict[int, ImageTk.PhotoImage]" = OrderedDict()
        self.thumb_pending: set[int] = set()
        self.thumb_failed: set[int] = set()   # 载入失败的项，避免无限重试
        self.photo_queue: queue.Queue = queue.Queue()
        self.video_ph: ImageTk.PhotoImage | None = None
        self.thumbs_ready = 0

        # Canvas 图元池
        self.cell_pool: list[dict] = []
        self.cells_by_slot: dict[int, dict] = {}
        self.hover_slot: int | None = None
        self._last_scrollregion: tuple | None = None

        # 忽略程序自身触发的滚动回调，避免递归刷新
        self._suppress = False
        self._refresh_job = None

        root.title(APP_NAME)
        root.geometry("1260x880")
        root.minsize(1000, 660)
        root.configure(bg=BG)

        self._build_style()
        self._build_source_row()
        self._build_actionbar()
        self._build_grid()
        self._build_statusbar()
        self._load_config()

        self.root.bind("<Control-a>", lambda e: (self.on_select_all(), "break")[1])
        self.root.bind("<Escape>", lambda e: self.on_clear_sel())
        self.root.after(60, self._pump)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(250, self.on_load)

    # ------------------------- 界面 -------------------------
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
        st.configure("TCombobox", fieldbackground="#2b2b2b", foreground=FG)
        st.configure("TRadiobutton", background=BG, foreground=FG, font=("Microsoft YaHei UI", 9))
        st.configure("TCheckbutton", background=BG, foreground=FG, font=("Microsoft YaHei UI", 9))
        st.configure("Horizontal.TProgressbar", background="#1f6feb", troughcolor="#2b2b2b")

    def _build_source_row(self) -> None:
        f = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        f.pack(fill="x")

        ttk.Label(f, text="tdata 目录").grid(row=0, column=0, sticky="w")
        self.e_tdata = ttk.Entry(f, width=42)
        self.e_tdata.grid(row=0, column=1, sticky="we", padx=(4, 4))
        ttk.Button(f, text="浏览…", command=self.on_browse).grid(row=0, column=2, padx=(0, 6))

        ttk.Label(f, text="锁定密码").grid(row=0, column=3, sticky="w")
        self.e_pass = ttk.Entry(f, width=12, show="*")
        self.e_pass.grid(row=0, column=4, sticky="w", padx=(4, 6))
        self.b_load = ttk.Button(f, text="读取缓存", command=self.on_load)
        self.b_load.grid(row=0, column=5, padx=(0, 6))
        self.b_reload = ttk.Button(f, text="重新扫描", command=self.on_reload)
        self.b_reload.grid(row=0, column=6)

        ttk.Label(f, text="类型").grid(row=0, column=7, padx=(16, 2))
        self.v_kind = tk.StringVar(value="image")
        for i, (txt, val) in enumerate((("图片", "image"), ("视频", "video"), ("全部", "all"))):
            ttk.Radiobutton(f, text=txt, value=val, variable=self.v_kind,
                            command=self.on_filter_change).grid(row=0, column=8 + i)

        ttk.Label(f, text="排序").grid(row=0, column=11, padx=(16, 2))
        self.cb_sort = ttk.Combobox(
            f, width=12, state="readonly",
            values=("日期 新→旧", "日期 旧→新", "大小 大→小", "大小 小→大", "来源目录"),
        )
        self.cb_sort.set("日期 新→旧")
        self.cb_sort.grid(row=0, column=12)
        self.cb_sort.bind("<<ComboboxSelected>>", lambda e: self.on_filter_change())

        ttk.Label(f, text="搜索").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.e_search = ttk.Entry(f, width=26)
        self.e_search.grid(row=1, column=1, sticky="w", padx=(4, 4), pady=(6, 0))
        self.e_search.bind("<Return>", lambda e: self.on_filter_change())
        ttk.Button(f, text="应用筛选", command=self.on_filter_change).grid(
            row=1, column=2, sticky="w", pady=(6, 0))
        self.l_hint = ttk.Label(
            f,
            text="单击选中 · Shift+单击范围选 · 双击预览 · Ctrl+A 全选 · Esc 清空　（保存前不写出任何文件）",
            style="Sub.TLabel",
        )
        self.l_hint.grid(row=1, column=3, columnspan=10, sticky="w", padx=(10, 0), pady=(6, 0))
        f.columnconfigure(1, weight=1)

    def _build_actionbar(self) -> None:
        f = ttk.Frame(self.root, padding=(12, 4, 12, 4))
        f.pack(fill="x")

        self.l_count = ttk.Label(f, text="已选 0 个（0 B）", style="H.TLabel")
        self.l_count.grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Button(f, text="全选（当前筛选）", command=self.on_select_all).grid(row=0, column=1, padx=(0, 4))
        ttk.Button(f, text="反选", command=self.on_invert).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(f, text="清除选择", command=self.on_clear_sel).grid(row=0, column=3, padx=(0, 16))

        self.v_bydate = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="按日期分文件夹", variable=self.v_bydate).grid(row=0, column=4, padx=(0, 16))
        ttk.Label(f, text="（原 tdata 只读，不会被改动）", style="Sub.TLabel").grid(row=0, column=5)

        ttk.Label(f, text="保存到").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.e_out = ttk.Entry(f, width=50)
        self.e_out.grid(row=1, column=1, columnspan=3, sticky="we", padx=(4, 4), pady=(6, 0))
        ttk.Button(f, text="浏览…", command=self.on_pick_out).grid(row=1, column=4, sticky="w", pady=(6, 0))
        ttk.Button(f, text="打开目录", command=self.on_open_out).grid(row=1, column=5, sticky="w", pady=(6, 0))
        self.b_save = ttk.Button(f, text="保存选中 →", style="Accent.TButton", command=self.on_save)
        self.b_save.grid(row=1, column=6, sticky="w", padx=(12, 0), pady=(6, 0))
        f.columnconfigure(1, weight=1)

    def _build_grid(self) -> None:
        wrap = ttk.Frame(self.root, padding=(12, 6, 12, 4))
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        vbar = ttk.Scrollbar(wrap, orient="vertical", command=self._on_scrollbar)
        self.canvas.configure(yscrollcommand=vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")

        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        # Windows 滚轮
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        # Linux
        self.canvas.bind("<Button-4>", lambda e: self._scroll_units(-3))
        self.canvas.bind("<Button-5>", lambda e: self._scroll_units(3))

    def _build_statusbar(self) -> None:
        f = ttk.Frame(self.root, padding=(12, 0, 12, 10))
        f.pack(fill="x")
        self.pb = ttk.Progressbar(f, mode="determinate", maximum=100)
        self.pb.pack(side="left", fill="x", expand=True)
        self.l_status = ttk.Label(f, text="就绪", style="Sub.TLabel")
        self.l_status.pack(side="left", padx=(10, 0))

    def _load_config(self) -> None:
        cfg = load_config()
        tdata = cfg.get("tdata") or ""
        if not tdata:
            cand = Path(r"D:\Telegram\tdata")
            tdata = str(cand if cand.is_dir() else "")
        self.e_tdata.delete(0, "end")
        self.e_tdata.insert(0, tdata)

        out = cfg.get("out_dir") or str(HERE.parent / "挑选保存")
        self.e_out.delete(0, "end")
        self.e_out.insert(0, out)
        if cfg.get("kind"):
            self.v_kind.set(cfg["kind"])
        if cfg.get("sort"):
            self.cb_sort.set(cfg["sort"])
        if cfg.get("by_date") is not None:
            self.v_bydate.set(bool(cfg["by_date"]))

    # ------------------------- 读取 -------------------------
    def on_browse(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.e_tdata.get() or str(HERE))
        if chosen:
            self.e_tdata.delete(0, "end")
            self.e_tdata.insert(0, chosen)

    def on_load(self) -> None:
        if self.busy:
            return
        tdata = Path(self.e_tdata.get().strip())
        if not tdata.is_dir():
            messagebox.showwarning(APP_NAME, "tdata 目录不存在，请检查路径")
            return
        self.busy = True
        self.cancel.clear()
        self.set_busy(True)
        self._reset_grid_data()
        passcode = self.e_pass.get()
        t0 = time.time()

        def work():
            try:
                entries, stats = load_cache(
                    tdata, passcode,
                    progress_cb=lambda i, t, txt: self.ui_q.put(("load_progress", i, t, txt)),
                    cancel_event=self.cancel,
                )
                self.ui_q.put(("load_done", tdata, entries, stats, passcode, time.time() - t0))
            except cs.PasscodeRequired as exc:
                self.ui_q.put(("load_error", "需要密码", str(exc)))
            except cs.CacheError as exc:
                self.ui_q.put(("load_error", "读取失败", str(exc)))
            except Exception as exc:  # pragma: no cover
                self.ui_q.put(("load_error", "读取失败", f"{exc.__class__.__name__}: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    def on_reload(self) -> None:
        try:
            INDEX_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        self.on_load()

    def _reset_grid_data(self) -> None:
        """清空网格与缩略图内存（不删磁盘缓存）。"""
        self.all_items = []
        self.items = []
        self.selected.clear()
        self.thumb_mem.clear()
        self.thumb_pending.clear()
        self.thumb_failed.clear()
        self.cells_by_slot.clear()
        self.thumbs_ready = 0
        self.hover_slot = None
        self._last_scrollregion = None
        for cell in self.cell_pool:
            self._hide_cell(cell)
        self.canvas.configure(scrollregion=(0, 0, 0, 0))
        self.canvas.yview_moveto(0)
        self.update_count()

    # ------------------------- 筛选 -------------------------
    def on_filter_change(self) -> None:
        self.apply_filter_sort()
        self.selected.clear()
        self.thumb_mem.clear()
        self.thumb_pending.clear()
        self.thumb_failed.clear()
        self.hover_slot = None
        self._last_scrollregion = None
        self.canvas.yview_moveto(0)
        self.refresh_view()
        self.update_count()

    def apply_filter_sort(self) -> None:
        kind = self.v_kind.get()
        if kind == "image":
            items = [it for it in self.all_items if it.kind == "image"]
        elif kind == "video":
            items = [it for it in self.all_items if it.kind == "video"]
        else:
            items = list(self.all_items)

        kw = self.e_search.get().strip().lower()
        if kw:
            items = [it for it in items if kw in it.name.lower() or kw in it.entry.rel.lower()]

        sort = self.cb_sort.get()
        if sort == "日期 新→旧":
            items.sort(key=lambda x: x.mtime, reverse=True)
        elif sort == "日期 旧→新":
            items.sort(key=lambda x: x.mtime)
        elif sort == "大小 大→小":
            items.sort(key=lambda x: x.size, reverse=True)
        elif sort == "大小 小→大":
            items.sort(key=lambda x: x.size)
        else:
            items.sort(key=lambda x: (x.entry.label, x.entry.rel))
        self.items = items

    # ------------------------- 虚拟化渲染 -------------------------
    def _on_canvas_configure(self, event) -> None:
        self._schedule_refresh()

    def _on_scrollbar(self, *args) -> None:
        """滚动条拖动：yview 之后刷新可视区。"""
        self.canvas.yview(*args)
        self._schedule_refresh()

    def _on_wheel(self, event) -> None:
        delta = -1 * int(event.delta / 120) if event.delta else 0
        if delta:
            self._scroll_units(delta * 3)

    def _scroll_units(self, units: int) -> None:
        self.canvas.yview_scroll(units, "units")
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        """合并高频滚动事件，每个 UI 周期最多刷一次。"""
        if self._suppress:
            return
        if self._refresh_job is not None:
            return
        self._refresh_job = self.root.after(16, self._do_refresh)

    def _do_refresh(self) -> None:
        self._refresh_job = None
        self.refresh_view()

    def refresh_view(self) -> None:
        """按当前滚动位置渲染可视区（复用图元，按需载入缩略图）。"""
        if not self.items:
            for cell in self.cell_pool:
                self._hide_cell(cell)
            self.cells_by_slot.clear()
            return

        cw = max(self.canvas.winfo_width(), 200)
        ch = max(self.canvas.winfo_height(), 200)
        cols = max(1, (cw - PAD) // CELL_W)
        self.columns = cols

        rows = (len(self.items) + cols - 1) // cols
        total_h = rows * CELL_H + PAD * 2

        # 仅在尺寸真正变化时改 scrollregion（避免每帧触发 <Configure> 回调）
        region = (0, 0, cw, total_h)
        if region != self._last_scrollregion:
            self._suppress = True
            try:
                self.canvas.configure(scrollregion=region)
            finally:
                self._suppress = False
            self._last_scrollregion = region

        top = self.canvas.canvasy(0)
        first_row = max(0, int((top - PAD) // CELL_H) - 1)
        last_row = min(rows - 1, int((top + ch - PAD) // CELL_H) + 1)

        needed: dict[int, tuple[int, int]] = {}
        for r in range(first_row, last_row + 1):
            for c in range(cols):
                idx = r * cols + c
                if idx < len(self.items):
                    needed[idx] = (c, r)

        # 回收不再可见的图元
        for slot in list(self.cells_by_slot.keys()):
            if slot not in needed:
                cell = self.cells_by_slot.pop(slot)
                self._hide_cell(cell)

        # 放置/更新可见图元
        for idx, (c, r) in needed.items():
            cell = self.cells_by_slot.get(idx)
            if cell is None:
                cell = self._acquire_cell()
                self.cells_by_slot[idx] = cell
            self._place_cell(cell, idx, c, r)

    def _acquire_cell(self) -> dict:
        """取一个空闲图元组；池子为空则新建一组 Canvas 图元。"""
        for cell in self.cell_pool:
            if cell["slot"] is None:
                return cell
        cell = {
            "slot": None,
            "rect": self.canvas.create_rectangle(0, 0, 0, 0, fill=CARD, outline=CARD, width=2),
            "img": self.canvas.create_image(0, 0, anchor="nw"),
            "mark": self.canvas.create_text(0, 0, anchor="nw", text="", fill="#ffffff",
                                            font=("Microsoft YaHei UI", 11, "bold")),
            "info": self.canvas.create_text(0, 0, anchor="nw", text="", fill=SUB,
                                            font=("Microsoft YaHei UI", 7)),
            "photo": None,
        }
        self.cell_pool.append(cell)
        return cell

    def _canvas_cfg(self, cell: dict, key: str, **kw) -> bool:
        """安全地配置 Canvas 图元。

        若图元引用的图像已被 Tk 销毁（引用计数归零），itemconfigure 会抛
        TclError；此时重建该单元格的图元后重试一次。这样任何残留的失效
        引用都能自愈，不会让滚动中断。
        """
        try:
            self.canvas.itemconfigure(cell[key], **kw)
            return True
        except tk.TclError:
            self._rebuild_cell(cell)
            try:
                self.canvas.itemconfigure(cell[key], **kw)
                return True
            except tk.TclError:
                return False

    def _canvas_coords(self, cell: dict, key: str, *args) -> bool:
        try:
            self.canvas.coords(cell[key], *args)
            return True
        except tk.TclError:
            self._rebuild_cell(cell)
            try:
                self.canvas.coords(cell[key], *args)
                return True
            except tk.TclError:
                return False

    def _rebuild_cell(self, cell: dict) -> None:
        """删除并重建该单元格的 4 个 Canvas 图元（用于从失效图像中恢复）。"""
        for key in ("rect", "img", "mark", "info"):
            try:
                self.canvas.delete(cell[key])
            except tk.TclError:
                pass
        cell["photo"] = None
        cell["rect"] = self.canvas.create_rectangle(0, 0, 0, 0, fill=CARD, outline=CARD, width=2)
        cell["img"] = self.canvas.create_image(0, 0, anchor="nw")
        cell["mark"] = self.canvas.create_text(0, 0, anchor="nw", text="", fill="#ffffff",
                                               font=("Microsoft YaHei UI", 11, "bold"))
        cell["info"] = self.canvas.create_text(0, 0, anchor="nw", text="", fill=SUB,
                                               font=("Microsoft YaHei UI", 7))

    def _hide_cell(self, cell: dict) -> None:
        cell["slot"] = None
        # 先解除图像引用，再丢弃 Python 侧引用——否则 Tk 销毁图像后，
        # Canvas 图元会留下悬空引用，下次复用时 itemconfigure 直接抛错。
        self._canvas_cfg(cell, "img", image="", state="hidden")
        for key in ("rect", "mark", "info"):
            self._canvas_cfg(cell, key, state="hidden")
        cell["photo"] = None

    def _place_cell(self, cell: dict, idx: int, col: int, row: int) -> None:
        it = self.items[idx]
        x = PAD + col * CELL_W
        y = PAD + row * CELL_H
        rel = str(it.entry.rel)
        is_sel = rel in self.selected
        is_hover = (self.hover_slot == idx) and not is_sel

        self._suppress = True
        try:
            self._canvas_cfg(cell, "rect", state="normal",
                             fill=CARD_SEL if is_sel else (CARD_HOVER if is_hover else CARD),
                             outline=CARD_SEL if is_sel else (CARD_HOVER if is_hover else CARD))
            self._canvas_coords(cell, "rect", x, y, x + CELL_W - 2, y + CELL_H - 2)

            # 缩略图
            photo = self._thumb_for(idx, it)
            if photo is not None:
                self._canvas_cfg(cell, "img", state="normal", image=photo)
                pw, ph = photo.width(), photo.height()
                self._canvas_coords(cell, "img",
                                    x + (CELL_W - 2 - pw) // 2,
                                    y + 6 + max(0, (THUMB - ph) // 2))
                cell["photo"] = photo
            else:
                self._canvas_cfg(cell, "img", image="", state="hidden")
                cell["photo"] = None

            self._canvas_cfg(
                cell, "mark", state="normal",
                text="✓" if is_sel else "",
                fill="#ffffff" if is_sel else CARD,
                font=("Microsoft YaHei UI", 11, "bold"),
            )
            self._canvas_coords(cell, "mark", x + 8, y + 6)

            label = "VIDEO" if it.kind == "video" else ""
            self._canvas_cfg(
                cell, "info", state="normal",
                text=f"{label}  {datetime.fromtimestamp(it.mtime):%m-%d %H:%M}  {human_size(it.size)}".strip(),
                fill="#ffffff" if is_sel else SUB,
            )
            self._canvas_coords(cell, "info", x + 8, y + CELL_H - 26)
        finally:
            self._suppress = False

        cell["slot"] = idx

    # ------------------------- 缩略图 -------------------------
    def _thumb_for(self, idx: int, it: Item) -> ImageTk.PhotoImage | None:
        """取缩略图 PhotoImage。

        图片与视频都走后台生成（视频用 ffmpeg 抽首帧）；尚未生成完成的
        返回占位图（视频）或 None（图片），后续刷新会自动替换为真实画面。
        """
        photo = self.thumb_mem.get(idx)
        if photo is not None:
            self.thumb_mem.move_to_end(idx)
            return photo

        if idx in self.thumb_failed:
            # 确实提不出图：视频显示占位图，图片留空
            return self._placeholder_for(it) if it.kind == "video" else None

        if idx not in self.thumb_pending:
            self._request_thumb(idx, it)
        # 生成中：视频先给占位图（有内容比空着好），图片留空
        return self._placeholder_for(it) if it.kind == "video" else None

    def _placeholder_for(self, it: Item) -> ImageTk.PhotoImage | None:
        """视频的灰色占位图（生成中或提帧失败时使用）。"""
        if self.video_ph is None:
            self.video_ph = self._make_video_placeholder()
        return self.video_ph

    def _make_video_placeholder(self) -> ImageTk.PhotoImage | None:
        try:
            im = Image.new("RGB", (THUMB, THUMB), (58, 58, 58))
            from PIL import ImageDraw
            d = ImageDraw.Draw(im)
            cx, cy = THUMB // 2, THUMB // 2
            d.rectangle([cx - 30, cy - 22, cx + 30, cy + 22], outline=(150, 150, 150), width=2)
            d.polygon([(cx - 11, cy - 12), (cx - 11, cy + 12), (cx + 13, cy)], fill=(190, 190, 190))
            return ImageTk.PhotoImage(im)
        except Exception:
            return None

    def _request_thumb(self, idx: int, it: Item) -> None:
        """后台线程：优先读磁盘缩略图缓存；缺失则解密媒体并生成。

        视频需要整体解密后交给 ffmpeg 抽帧（缓存分片可能不完整，
        抽帧失败会回退到占位图）。
        """
        self.thumb_pending.add(idx)
        tp = cs.entry_thumb_path(THUMB_DIR, it.entry.thumb_key) if it.entry.thumb_key else None
        reader = self.reader
        kind = it.kind
        cache_path = it.entry.cache_path

        def work():
            try:
                img = None
                if tp is not None and tp.exists():
                    with Image.open(tp) as im:
                        im.load()
                        img = im.convert("RGB")
                elif reader is not None:
                    data = reader.decrypt(cache_path)
                    if kind == "image":
                        img = cs.make_thumb(data, kind)
                    else:
                        # 视频抽帧需落临时文件（MP4 的 moov 常在尾部，
                        # 管道输入无法 seek），临时文件由 ffmpeg 抽完即删。
                        img = cs.make_video_thumb(data, temp_dir=VIDEO_TMP_DIR)
                    if img is not None and tp is not None:
                        try:
                            tp.parent.mkdir(parents=True, exist_ok=True)
                            img.save(tp, "JPEG", quality=85)
                        except OSError:
                            pass
                self.photo_queue.put((idx, img, kind))
            except Exception:
                self.photo_queue.put((idx, None, kind))

        self.pool.submit(work)

    def _drain_photos(self) -> bool:
        """主线程把后台读到的 PIL 图转成 PhotoImage（每周期限量）。"""
        changed = False
        for _ in range(PHOTO_BATCH_PER_TICK):
            try:
                item = self.photo_queue.get_nowait()
            except queue.Empty:
                break
            idx, img, kind = item
            self.thumb_pending.discard(idx)
            if idx >= len(self.items):
                continue
            if img is None:
                # 提取失败：视频仍显示占位图，图片留空
                self.thumb_failed.add(idx)
                continue
            try:
                self.thumb_mem[idx] = ImageTk.PhotoImage(img)
                self.thumb_mem.move_to_end(idx)
                self.thumbs_ready += 1
                changed = True
            except Exception:
                self.thumb_failed.add(idx)
        if changed:
            self._evict_thumbs()
        return changed

    def _evict_thumbs(self) -> None:
        """超出上限时淘汰不可见缩略图，避免内存膨胀。"""
        if len(self.thumb_mem) <= MAX_THUMBS_IN_MEM:
            return
        visible = set(self.cells_by_slot.keys())
        for key in list(self.thumb_mem.keys()):
            if len(self.thumb_mem) <= MAX_THUMBS_IN_MEM:
                break
            if key not in visible:
                del self.thumb_mem[key]

    # ------------------------- 交互 -------------------------
    def _slot_at(self, x: int, y: int) -> int | None:
        if not self.items:
            return None
        cx = self.canvas.canvasx(x)
        cy = self.canvas.canvasy(y)
        if cx < PAD or cy < PAD:
            return None
        col = int((cx - PAD) // CELL_W)
        row = int((cy - PAD) // CELL_H)
        if col < 0 or col >= self.columns:
            return None
        idx = row * self.columns + col
        return idx if 0 <= idx < len(self.items) else None

    def _on_click(self, event) -> None:
        idx = self._slot_at(event.x, event.y)
        if idx is None:
            return
        it = self.items[idx]
        rel = str(it.entry.rel)
        shift = bool(event.state & 0x0001)
        if shift and self.last_click is not None:
            lo, hi = sorted((self.last_click, idx))
            turn_on = rel not in self.selected
            for i in range(lo, hi + 1):
                r = str(self.items[i].entry.rel)
                if turn_on:
                    self.selected.add(r)
                else:
                    self.selected.discard(r)
        else:
            if rel in self.selected:
                self.selected.discard(rel)
            else:
                self.selected.add(rel)
        self.last_click = idx
        self.update_count()
        self.refresh_view()

    def _on_double_click(self, event) -> None:
        idx = self._slot_at(event.x, event.y)
        if idx is None:
            return
        self.on_preview(self.items[idx])

    def _on_motion(self, event) -> None:
        idx = self._slot_at(event.x, event.y)
        if idx == self.hover_slot:
            return
        prev, self.hover_slot = self.hover_slot, idx
        # 只重绘受影响的两格，避免每次鼠标移动都整屏刷新
        for target in (prev, idx):
            if target is None:
                continue
            cell = self.cells_by_slot.get(target)
            if cell is None:
                continue
            col = target % self.columns
            row = target // self.columns
            self._place_cell(cell, target, col, row)
        if idx is not None and idx < len(self.items):
            it = self.items[idx]
            self.set_status(
                f"{it.name}　·　{human_size(it.size)}　·　"
                f"{datetime.fromtimestamp(it.mtime):%Y-%m-%d %H:%M}　·　来源 {it.entry.label}"
            )
        else:
            self.set_status(self._default_status())

    def _on_leave(self, event) -> None:
        if self.hover_slot is not None:
            prev = self.hover_slot
            self.hover_slot = None
            cell = self.cells_by_slot.get(prev)
            if cell is not None:
                self._place_cell(cell, prev, prev % self.columns, prev // self.columns)
        self.set_status(self._default_status())

    def on_preview(self, it: Item) -> None:
        tdata = Path(self.e_tdata.get().strip())
        passcode = self.e_pass.get()
        self.set_status(f"正在解密预览 {it.name} …")

        def work():
            try:
                path = open_preview(it, tdata, passcode, self.reader)
                self.ui_q.put(("preview", str(path), None))
            except Exception as exc:
                self.ui_q.put(("preview", None, f"{exc.__class__.__name__}: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------- 选择 -------------------------
    def on_select_all(self) -> None:
        for it in self.items:
            self.selected.add(str(it.entry.rel))
        self.update_count()
        self.refresh_view()

    def on_invert(self) -> None:
        for it in self.items:
            rel = str(it.entry.rel)
            if rel in self.selected:
                self.selected.discard(rel)
            else:
                self.selected.add(rel)
        self.update_count()
        self.refresh_view()

    def on_clear_sel(self) -> None:
        self.selected.clear()
        self.update_count()
        self.refresh_view()

    def update_count(self) -> None:
        by_rel = {str(it.entry.rel): it for it in self.all_items}
        total = 0
        for rel in self.selected:
            it = by_rel.get(rel)
            if it is not None:
                total += it.size
        self.l_count.configure(text=f"已选 {len(self.selected)} 个（{human_size(total)}）")
        self.b_save.configure(text=f"保存选中（{len(self.selected)}）→")

    def _default_status(self) -> str:
        return (f"共 {len(self.items)} 个 · 已选 {len(self.selected)} 个 · "
                f"缩略图已载入 {self.thumbs_ready}")

    # ------------------------- 保存 -------------------------
    def on_pick_out(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.e_out.get() or str(HERE))
        if chosen:
            self.e_out.delete(0, "end")
            self.e_out.insert(0, chosen)

    def on_open_out(self) -> None:
        out = self.e_out.get().strip()
        if out and Path(out).is_dir():
            os.startfile(out)
        else:
            messagebox.showinfo(APP_NAME, "输出目录还不存在，保存后会自动创建")

    def on_save(self) -> None:
        if self.busy:
            return
        if not self.selected:
            messagebox.showinfo(APP_NAME, "还没有选中任何内容\n\n在缩略图上单击即可选中（变蓝打勾）")
            return
        out = self.e_out.get().strip()
        if not out:
            messagebox.showwarning(APP_NAME, "请先选择保存到的目录")
            return
        tdata = Path(self.e_tdata.get().strip())
        by_rel = {str(it.entry.rel): it for it in self.all_items}
        items = [by_rel[r] for r in self.selected if r in by_rel]
        if not items:
            return
        total_size = sum(it.size for it in items)
        if not messagebox.askyesno(
            APP_NAME,
            f"将保存 {len(items)} 个文件（约 {human_size(total_size)}）到：\n{out}\n\n"
            + ("（按日期分文件夹）\n" if self.v_bydate.get() else "")
            + "原 tdata 缓存不会被改动。",
        ):
            return

        self.busy = True
        self.set_busy(True)
        self.pb.configure(value=0)
        passcode = self.e_pass.get()
        by_date = bool(self.v_bydate.get())
        self.cancel.clear()

        def work():
            try:
                reader = self.reader or cs.CacheReader(tdata, passcode)
                ok, fail = save_items(
                    items, Path(out), tdata, passcode, by_date=by_date,
                    progress_cb=lambda d, t, n: self.ui_q.put(("save_progress", d, t, n)),
                    cancel_event=self.cancel, reader=reader,
                )
                self.ui_q.put(("save_done", Path(out), ok, fail))
            except Exception as exc:  # pragma: no cover
                self.ui_q.put(("load_error", "保存失败", f"{exc.__class__.__name__}: {exc}"))
                self.ui_q.put(("save_done", Path(out), 0, len(items)))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------- 主循环 -------------------------
    def _pump(self) -> None:
        try:
            while True:
                self._handle(self.ui_q.get_nowait())
        except queue.Empty:
            pass
        if self._drain_photos():
            self.refresh_view()
        self.root.after(40, self._pump)

    def _handle(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "load_progress":
            _, i, total, txt = msg
            self.pb.configure(value=(i / total * 100) if total else 0)
            self.set_status(txt)
        elif kind == "load_done":
            _, tdata, entries, stats, passcode, elapsed = msg
            self.tdata = tdata
            self.busy = False
            self.set_busy(False)
            self.pb.configure(value=100)
            self.all_items = [Item(entry=e) for e in entries]
            self.reader = cs.CacheReader(tdata, passcode)
            self.apply_filter_sort()
            self.refresh_view()
            self.set_status(
                f"读取完成：媒体 {stats['ok']} 个（复用 {stats['reused']}）· "
                f"非媒体 {stats['skipped']} · 失败 {stats['failed']} · "
                f"共 {stats['total']} 个缓存文件 · 耗时 {elapsed:.1f}s"
            )
            save_config({"tdata": str(tdata)})
            if not entries:
                messagebox.showinfo(
                    APP_NAME,
                    "没有解析出可用的媒体。\n\n"
                    "请先在 Telegram Desktop 里查看/播放你想保存的内容，再回来点“重新扫描”。",
                )
        elif kind == "load_error":
            _, title, detail = msg
            self.busy = False
            self.set_busy(False)
            self.pb.configure(value=0)
            messagebox.showerror(APP_NAME, f"{title}\n\n{detail}")
            self.set_status(f"{title}：{detail}")
        elif kind == "preview":
            _, path, err = msg
            if err:
                messagebox.showerror(APP_NAME, f"预览失败：{err}")
                self.set_status("预览失败")
            else:
                try:
                    os.startfile(path)
                    self.set_status(f"已解密预览：{Path(path).name}")
                except Exception as exc:
                    messagebox.showerror(APP_NAME, f"无法打开：{exc}")
        elif kind == "save_progress":
            _, d, t, n = msg
            self.pb.configure(value=(d / t * 100) if t else 0)
            self.set_status(f"保存中 {d}/{t} · {n}")
        elif kind == "save_done":
            _, out, ok, fail = msg
            self.busy = False
            self.set_busy(False)
            self.pb.configure(value=100)
            self.set_status(f"保存完成：成功 {ok}，失败 {fail}")
            if messagebox.askyesno(APP_NAME, f"保存完成：成功 {ok}，失败 {fail}。\n\n是否打开输出目录？"):
                try:
                    os.startfile(str(out))
                except Exception as exc:
                    messagebox.showwarning(APP_NAME, f"打开失败：{exc}")
            save_config({"out_dir": str(out), "kind": self.v_kind.get(),
                         "sort": self.cb_sort.get(), "by_date": bool(self.v_bydate.get())})

    def set_status(self, text: str) -> None:
        self.l_status.configure(text=text)

    def set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.b_save.configure(state=state)
        self.b_load.configure(state=state)
        self.b_reload.configure(state=state)

    def _on_close(self) -> None:
        self.cancel.set()
        try:
            self.pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    CacheApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
