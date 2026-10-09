# -*- coding: utf-8 -*-
"""本地图片挑选器（gallery-picker）

不联网、不需要 api_id、不需要登录。直接扫描本地文件夹里的图片/视频，
以缩略图墙呈现，你点选想要的那些，一键复制/移动到指定文件夹。

典型用法：
    1) 用 tg-scoop 把 Telegram 缓存解密到某个输出目录（一次性）
    2) 用本工具浏览那个目录，点选喜欢的图片，保存到归档目录

特性：
    - 缩略图墙 + 无限滚动（几千张也不卡）
    - 单击选中 / Shift 单击范围选 / Ctrl+A 全选 / 双击打开
    - 复制 或 移动（移动可顺便把源目录整理干净）
    - 可选"按日期分文件夹"
    - 缩略图磁盘缓存，第二次打开秒开
    - 类型筛选 / 排序 / 文件名搜索
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import queue
import shutil
import threading
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageTk

APP_NAME = "本地图片挑选器"
HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "gallery_config.json"
THUMB_DIR = HERE / "thumbs"

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".jfif", ".tif", ".tiff", ".avif"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".wmv", ".flv"}

THUMB = 140            # 缩略图边长
CELL_W, CELL_H = 164, 196
BATCH = 48             # 每批渲染多少格
THUMB_WORKERS = 4

# 深色配色
BG = "#1e1e1e"
CARD = "#2b2b2b"
CARD_SEL = "#1f6feb"
CARD_HOVER = "#3a3a3a"
FG = "#e8e8e8"
SUB = "#9aa0a6"


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def human_size(n: int | None) -> str:
    """字节数 → 可读文本。"""
    if not n:
        return "-"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def unique_path(path: Path) -> Path:
    """目标已存在则追加 (1)(2)…，绝不覆盖。"""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 100000):
        cand = path.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
    raise RuntimeError(f"命名冲突过多：{path}")


@dataclass
class MediaFile:
    """一个可挑选的媒体文件。"""

    path: Path
    size: int
    mtime: float
    kind: str  # image / video

    # GUI 相关的挂载点（不属于数据本身）
    cell: object = None
    img_label: object = None
    mark_label: object = None
    info_label: object = None
    photo: object = None


# ---------------------------------------------------------------------------
# 扫描与缩略图（纯函数，便于单测）
# ---------------------------------------------------------------------------
def scan_folder(root: Path, want_images: bool = True, want_videos: bool = True) -> list[MediaFile]:
    """递归扫描文件夹，返回媒体文件列表（跳过缩略图缓存目录自身）。"""
    root = Path(root)
    out: list[MediaFile] = []
    thumb_resolved = THUMB_DIR.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        keep = []
        for d in dirnames:
            try:
                if Path(dirpath, d).resolve() == thumb_resolved:
                    continue
            except OSError:
                pass
            keep.append(d)
        dirnames[:] = keep
        for fn in filenames:
            ext = Path(fn).suffix.lower()
            if ext in IMAGE_EXT:
                if not want_images:
                    continue
                kind = "image"
            elif ext in VIDEO_EXT:
                if not want_videos:
                    continue
                kind = "video"
            else:
                continue
            p = Path(dirpath, fn)
            try:
                st = p.stat()
            except OSError:
                continue
            out.append(MediaFile(path=p, size=st.st_size, mtime=st.st_mtime, kind=kind))
    return out


def thumb_key(mf: MediaFile) -> str:
    """缩略图缓存键：路径 + 大小 + 修改时间。"""
    raw = f"{mf.path}|{mf.size}|{int(mf.mtime)}"
    return hashlib.md5(raw.encode("utf-8", "replace")).hexdigest()


def thumb_path(key: str) -> Path:
    """缩略图缓存落盘路径（两级散列，避免单目录文件过多）。"""
    return THUMB_DIR / key[:2] / f"{key}.jpg"


def make_placeholder(text: str = "VIDEO") -> Image.Image:
    """视频/损坏文件的占位图。"""
    im = Image.new("RGB", (THUMB, THUMB), (58, 58, 58))
    d = ImageDraw.Draw(im)
    cx, cy = THUMB // 2, THUMB // 2 - 6
    d.rectangle([cx - 30, cy - 20, cx + 30, cy + 20], outline=(150, 150, 150), width=2)
    d.polygon([(cx - 12, cy - 11), (cx - 12, cy + 11), (cx + 12, cy)], fill=(190, 190, 190))
    try:
        d.text((8, THUMB - 20), text, fill=(200, 200, 200))
    except Exception:
        pass
    return im


def build_thumb(mf: MediaFile, key: str) -> Image.Image | None:
    """生成（或读取缓存）缩略图；在工作线程中调用。

    返回 PIL.Image；视频或损坏文件返回占位图。
    """
    tp = thumb_path(key)
    if tp.exists():
        try:
            with Image.open(tp) as im:
                im.load()
                return im.convert("RGB")
        except Exception:
            try:
                tp.unlink()
            except OSError:
                pass

    if mf.kind != "image":
        return make_placeholder("VIDEO")

    try:
        with Image.open(mf.path) as im:
            try:
                im.draft("RGB", (THUMB * 2, THUMB * 2))  # JPEG 快速降采样
            except Exception:
                pass
            im = im.convert("RGB")
            im.thumbnail((THUMB, THUMB), Image.LANCZOS)
            try:
                tp.parent.mkdir(parents=True, exist_ok=True)
                tmp = tp.with_suffix(".tmp")
                im.save(tmp, "JPEG", quality=85)
                os.replace(tmp, tp)
            except OSError:
                pass
            return im
    except Exception:
        return make_placeholder("?")


def save_files(
    items: list[MediaFile],
    out_dir: Path,
    move: bool = False,
    by_date: bool = False,
    progress_cb=None,
    cancel_event: threading.Event | None = None,
) -> tuple[int, int, int]:
    """把选中的文件复制/移动到 out_dir。

    Returns:
        (成功, 失败, 跳过)
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ok = fail = skip = 0
    total = len(items)
    for i, mf in enumerate(items, 1):
        if cancel_event is not None and cancel_event.is_set():
            break
        try:
            target_dir = out_dir
            if by_date:
                target_dir = out_dir / datetime.fromtimestamp(mf.mtime).strftime("%Y-%m-%d")
                target_dir.mkdir(parents=True, exist_ok=True)
            dest = unique_path(target_dir / mf.path.name)
            try:
                if mf.path.resolve() == dest.resolve():
                    skip += 1
                    continue
            except OSError:
                pass
            if move:
                shutil.move(str(mf.path), str(dest))
            else:
                shutil.copy2(mf.path, dest)
            ok += 1
        except Exception:
            fail += 1
        if progress_cb is not None:
            progress_cb(i, total, mf.path.name)
    return ok, fail, skip


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
    except Exception:
        pass


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class GalleryApp:
    # 子类可调：允许"移动"保存（缓存模式必须禁止——绝不动 tdata）
    ALLOW_MOVE = True
    # 筛选控件在工具栏中的起始列（子类若把数据源行加宽，需要右移）
    FILTER_COL0 = 5

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.ui_q: queue.Queue = queue.Queue()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=THUMB_WORKERS)

        self.all_items: list[MediaFile] = []   # 扫描结果全集
        self.items: list[MediaFile] = []       # 过滤+排序后
        self.selected: set[str] = set()        # 选中项的路径字符串
        self.cells: dict[int, MediaFile] = {}  # 已渲染：索引 → 项
        self.rendered = 0
        self.columns = 0
        self.last_click: int | None = None
        self.gen = 0                           # 世代号：过滤/重扫后作废旧任务
        self.requested: set[int] = set()
        self.thumbs_ready = 0
        self.scanning = False
        self.saving = False
        self.save_cancel = threading.Event()
        self.folder: Path | None = None

        root.title(APP_NAME)
        root.geometry("1240x860")
        root.minsize(980, 640)
        root.configure(bg=BG)

        self._build_style()
        self._build_toolbar()
        self._build_grid()
        self._build_actionbar()
        self._build_statusbar()

        self._load_config()
        self._bind_keys()
        self.root.after(80, self._pump)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 自动加载上次的目录（或默认目录）
        default = self.folder
        if default and default.is_dir():
            self.root.after(200, lambda: self.start_scan(default))

    # ------------------------- 界面构建 -------------------------
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

    def _build_toolbar(self) -> None:
        f = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        f.pack(fill="x")
        self._build_source_row(f)
        self._build_filter_row(f)
        f.columnconfigure(1, weight=1)

    def _build_source_row(self, f: ttk.Frame) -> None:
        """数据源行：默认是"图片文件夹"。子类可覆盖（如改成 tdata 路径）。"""
        ttk.Label(f, text="图片文件夹").grid(row=0, column=0, sticky="w")
        self.e_folder = ttk.Entry(f, width=52)
        self.e_folder.grid(row=0, column=1, sticky="we", padx=(4, 4))
        ttk.Button(f, text="浏览…", command=self.on_browse).grid(row=0, column=2, padx=(0, 4))
        self.b_scan = ttk.Button(f, text="重新扫描", command=self.on_rescan)
        self.b_scan.grid(row=0, column=3, padx=(0, 10))
        ttk.Button(f, text="打开", command=self.on_open_folder).grid(row=0, column=4)

    def _build_filter_row(self, f: ttk.Frame) -> None:
        """筛选行：类型 / 排序 / 搜索。"""
        c = self.FILTER_COL0
        ttk.Label(f, text="类型").grid(row=0, column=c, padx=(14, 2))
        self.v_kind = tk.StringVar(value="image")
        ttk.Radiobutton(f, text="图片", value="image", variable=self.v_kind,
                        command=self.on_filter_change).grid(row=0, column=c + 1)
        ttk.Radiobutton(f, text="视频", value="video", variable=self.v_kind,
                        command=self.on_filter_change).grid(row=0, column=c + 2)
        ttk.Radiobutton(f, text="全部", value="all", variable=self.v_kind,
                        command=self.on_filter_change).grid(row=0, column=c + 3)

        ttk.Label(f, text="排序").grid(row=0, column=c + 4, padx=(14, 2))
        self.cb_sort = ttk.Combobox(
            f, width=12, state="readonly",
            values=("日期 新→旧", "日期 旧→新", "大小 大→小", "大小 小→大", "文件名"),
        )
        self.cb_sort.set("日期 新→旧")
        self.cb_sort.grid(row=0, column=c + 5)
        self.cb_sort.bind("<<ComboboxSelected>>", lambda e: self.on_filter_change())

        ttk.Label(f, text="搜索").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.e_search = ttk.Entry(f, width=30)
        self.e_search.grid(row=1, column=1, sticky="w", padx=(4, 4), pady=(6, 0))
        self.e_search.bind("<Return>", lambda e: self.on_filter_change())
        ttk.Button(f, text="应用筛选", command=self.on_filter_change).grid(
            row=1, column=2, sticky="w", pady=(6, 0)
        )
        self.l_hint = ttk.Label(f, text="提示：单击选中 · Shift+单击范围选 · 双击打开 · Ctrl+A 全选已加载", style="Sub.TLabel")
        self.l_hint.grid(row=1, column=3, columnspan=8, sticky="w", padx=(10, 0), pady=(6, 0))

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
        self._resize_job = None

    def _build_actionbar(self) -> None:
        f = ttk.Frame(self.root, padding=(12, 4, 12, 4))
        f.pack(fill="x")

        self.l_count = ttk.Label(f, text="已选 0 个（0 B）", style="H.TLabel")
        self.l_count.grid(row=0, column=0, sticky="w", padx=(0, 12))
        ttk.Button(f, text="全选已加载", command=self.on_select_all).grid(row=0, column=1, padx=(0, 4))
        ttk.Button(f, text="反选", command=self.on_invert).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(f, text="清除选择", command=self.on_clear_sel).grid(row=0, column=3, padx=(0, 14))

        ttk.Label(f, text="保存方式").grid(row=0, column=4, padx=(0, 2))
        self.v_move = tk.BooleanVar(value=False)
        ttk.Radiobutton(f, text="复制", value=False, variable=self.v_move).grid(row=0, column=5)
        ttk.Radiobutton(f, text="移动（源文件会被移走）", value=True, variable=self.v_move).grid(row=0, column=6, padx=(0, 14))

        self.v_bydate = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="按日期分文件夹", variable=self.v_bydate).grid(row=0, column=7, padx=(0, 14))

        ttk.Label(f, text="保存到").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.e_out = ttk.Entry(f, width=56)
        self.e_out.grid(row=1, column=1, columnspan=4, sticky="we", padx=(4, 4), pady=(6, 0))
        ttk.Button(f, text="浏览…", command=self.on_pick_out).grid(row=1, column=5, pady=(6, 0))
        ttk.Button(f, text="打开输出目录", command=self.on_open_out).grid(row=1, column=6, pady=(6, 0))
        self.b_save = ttk.Button(f, text="保存选中 →", style="Accent.TButton", command=self.on_save)
        self.b_save.grid(row=1, column=7, columnspan=2, sticky="w", padx=(10, 0), pady=(6, 0))

        f.columnconfigure(4, weight=1)

    def _build_statusbar(self) -> None:
        f = ttk.Frame(self.root, padding=(12, 0, 12, 10))
        f.pack(fill="x")
        self.pb = ttk.Progressbar(f, mode="determinate", maximum=100)
        self.pb.pack(side="left", fill="x", expand=True)
        self.l_status = ttk.Label(f, text="就绪", style="Sub.TLabel")
        self.l_status.pack(side="left", padx=(10, 0))

    def _bind_keys(self) -> None:
        self.root.bind("<Control-a>", lambda e: (self.on_select_all(), "break")[1])
        self.root.bind("<Escape>", lambda e: self.on_clear_sel())

    def _load_config(self) -> None:
        cfg = load_config()
        default_folder = cfg.get("folder") or ""
        if not default_folder:
            cand = Path(r"D:\agent\dsh\tg-scoop-output")
            default_folder = str(cand if cand.is_dir() else HERE)
        self.e_folder.delete(0, "end")
        self.e_folder.insert(0, default_folder)
        self.folder = Path(default_folder)

        out = cfg.get("out_dir") or ""
        if not out:
            out = str(Path(default_folder).parent / "挑选保存" if default_folder else HERE / "挑选保存")
        self.e_out.delete(0, "end")
        self.e_out.insert(0, out)

        if cfg.get("sort"):
            self.cb_sort.set(cfg["sort"])
        if cfg.get("kind"):
            self.v_kind.set(cfg["kind"])
        if cfg.get("by_date") is not None:
            self.v_bydate.set(bool(cfg["by_date"]))

    # ------------------------- 扫描 -------------------------
    def on_browse(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.e_folder.get() or str(HERE))
        if chosen:
            self.e_folder.delete(0, "end")
            self.e_folder.insert(0, chosen)
            self.start_scan(Path(chosen))

    def on_rescan(self) -> None:
        folder = Path(self.e_folder.get().strip())
        if not folder.is_dir():
            messagebox.showwarning(APP_NAME, "文件夹不存在，请重新选择")
            return
        self.start_scan(folder)

    def start_scan(self, folder: Path) -> None:
        """扫描整个文件夹（图片+视频都收进来）。

        类型筛选交给 apply_filter_sort 在前端做，这样切换"图片/视频/全部"
        无需重新扫描——只读元数据，切换是瞬时的。
        """
        if self.scanning:
            return
        self.scanning = True
        self.gen += 1
        self.set_status(f"正在扫描 {folder} …")
        self.b_scan.configure(state="disabled")
        self._clear_grid()
        self.all_items = []
        self.items = []
        self.selected.clear()
        self.update_count()

        def work():
            try:
                found = scan_folder(folder, want_images=True, want_videos=True)
                self.ui_q.put(("scan_done", folder, found))
            except Exception as exc:  # pragma: no cover
                self.ui_q.put(("status", f"扫描失败：{exc.__class__.__name__}: {exc}"))
                self.ui_q.put(("scan_failed",))

        threading.Thread(target=work, daemon=True).start()

    def on_filter_change(self) -> None:
        """类型/排序/搜索变化：只在前端重新过滤排序，不重新扫描磁盘。"""
        self.gen += 1
        self._clear_grid()
        self.apply_filter_sort()
        self.render_more(first=True)
        self._autofill()
        if not self.items:
            self.set_status(
                f"当前条件下没有匹配项（该目录共 {len(self.all_items)} 个媒体文件）"
            )

    def apply_filter_sort(self) -> None:
        kind = self.v_kind.get()
        if kind == "image":
            items = [m for m in self.all_items if m.kind == "image"]
        elif kind == "video":
            items = [m for m in self.all_items if m.kind == "video"]
        else:
            items = list(self.all_items)

        kw = self.e_search.get().strip().lower()
        if kw:
            items = [m for m in items if kw in m.path.name.lower()]

        sort = self.cb_sort.get()
        if sort == "日期 新→旧":
            items.sort(key=lambda m: m.mtime, reverse=True)
        elif sort == "日期 旧→新":
            items.sort(key=lambda m: m.mtime)
        elif sort == "大小 大→小":
            items.sort(key=lambda m: m.size, reverse=True)
        elif sort == "大小 小→大":
            items.sort(key=lambda m: m.size)
        else:
            items.sort(key=lambda m: m.path.name.lower())

        self.items = items

    # ------------------------- 网格渲染 -------------------------
    def _clear_grid(self) -> None:
        for w in self.inner.winfo_children():
            w.destroy()
        self.cells.clear()
        self.requested.clear()
        self.rendered = 0
        self.columns = 0
        self.thumbs_ready = 0
        self.last_click = None
        self.canvas.configure(scrollregion=(0, 0, 0, 0))

    def _calc_columns(self) -> int:
        width = max(self.canvas.winfo_width(), 320)
        return max(1, (width - 8) // CELL_W)

    def render_more(self, first: bool = False) -> None:
        """渲染下一批格子。"""
        if self.rendered >= len(self.items):
            return
        cols = self._calc_columns()
        self.columns = cols
        end = min(self.rendered + (BATCH * 2 if first else BATCH), len(self.items))
        for idx in range(self.rendered, end):
            mf = self.items[idx]
            self._create_cell(idx, mf, cols)
        self.rendered = end
        self.root.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.set_status(
            f"共 {len(self.items)} 个 · 已加载 {self.rendered} 个 · 已选 {len(self.selected)} 个"
            f" · 缩略图 {self.thumbs_ready}/{self.rendered}"
        )

    def _create_cell(self, idx: int, mf: MediaFile, cols: int) -> None:
        cell = tk.Frame(self.inner, bg=CARD, width=CELL_W, height=CELL_H,
                        highlightthickness=2, highlightbackground=CARD)
        cell.grid_propagate(False)
        cell.grid(row=idx // cols, column=idx % cols, padx=5, pady=5)

        img_label = tk.Label(cell, bg=CARD, fg=SUB, text="载入中…", font=("Microsoft YaHei UI", 8))
        img_label.pack(expand=True)
        info_label = tk.Label(
            cell, bg=CARD, fg=SUB, font=("Microsoft YaHei UI", 7),
            text=f"{datetime.fromtimestamp(mf.mtime):%m-%d %H:%M}  {human_size(mf.size)}",
        )
        info_label.pack(side="bottom", pady=(0, 3))
        mark = tk.Label(cell, bg=CARD, fg="#ffffff", text="", font=("Microsoft YaHei UI", 10, "bold"))
        mark.place(x=6, y=4)

        mf.cell, mf.img_label, mf.info_label, mf.mark_label = cell, img_label, info_label, mark
        self.cells[idx] = mf

        for w in (cell, img_label, info_label, mark):
            w.bind("<Button-1>", lambda e, i=idx: self.on_cell_click(i, e))
            w.bind("<Double-Button-1>", lambda e, i=idx: self.on_cell_open(i))
            w.bind("<Enter>", lambda e, i=idx: self.on_cell_hover(i, True))
            w.bind("<Leave>", lambda e, i=idx: self.on_cell_hover(i, False))

        if str(mf.path) in self.selected:
            self._paint_selected(mf, True)

        self._request_thumb(idx, mf)

    def _request_thumb(self, idx: int, mf: MediaFile) -> None:
        if idx in self.requested:
            return
        self.requested.add(idx)
        key = thumb_key(mf)
        gen = self.gen

        def work():
            try:
                img = build_thumb(mf, key)
            except Exception:
                img = None
            self.ui_q.put(("thumb", gen, idx, img))

        self.pool.submit(work)

    def _paint_selected(self, mf: MediaFile, is_sel: bool) -> None:
        color = CARD_SEL if is_sel else CARD
        for w in (mf.cell, mf.img_label, mf.info_label, mf.mark_label):
            if w is not None:
                w.configure(bg=color)
        if mf.cell is not None:
            mf.cell.configure(highlightbackground=color)
        if mf.mark_label is not None:
            mf.mark_label.configure(text="✓" if is_sel else "")

    def on_cell_hover(self, idx: int, entering: bool) -> None:
        mf = self.cells.get(idx)
        if mf is None or str(mf.path) in self.selected:
            return
        color = CARD_HOVER if entering else CARD
        for w in (mf.cell, mf.img_label, mf.info_label, mf.mark_label):
            if w is not None:
                w.configure(bg=color)
        if entering:
            self.set_status(f"{mf.path.name}  ·  {human_size(mf.size)}  ·  "
                            f"{datetime.fromtimestamp(mf.mtime):%Y-%m-%d %H:%M}")
            self.root.after(1500, lambda: self.set_status(self._default_status()))

    # ------------------------- 选择 -------------------------
    def on_cell_click(self, idx: int, event) -> None:
        mf = self.cells.get(idx)
        if mf is None:
            return
        shift = bool(event.state & 0x0001)
        if shift and self.last_click is not None:
            lo, hi = sorted((self.last_click, idx))
            toggle_to = str(mf.path) not in self.selected
            for i in range(lo, hi + 1):
                m = self.cells.get(i)
                if m is None:
                    continue
                if toggle_to:
                    self.selected.add(str(m.path))
                    self._paint_selected(m, True)
                else:
                    self.selected.discard(str(m.path))
                    self._paint_selected(m, False)
        else:
            p = str(mf.path)
            if p in self.selected:
                self.selected.discard(p)
                self._paint_selected(mf, False)
            else:
                self.selected.add(p)
                self._paint_selected(mf, True)
        self.last_click = idx
        self.update_count()

    def on_cell_open(self, idx: int) -> None:
        mf = self.cells.get(idx)
        if mf is None:
            return
        try:
            os.startfile(str(mf.path))
        except Exception as exc:
            messagebox.showwarning(APP_NAME, f"无法打开：{exc}")

    def on_select_all(self) -> None:
        for i, mf in self.cells.items():
            self.selected.add(str(mf.path))
            self._paint_selected(mf, True)
        self.update_count()

    def on_invert(self) -> None:
        for i, mf in self.cells.items():
            p = str(mf.path)
            if p in self.selected:
                self.selected.discard(p)
                self._paint_selected(mf, False)
            else:
                self.selected.add(p)
                self._paint_selected(mf, True)
        self.update_count()

    def on_clear_sel(self) -> None:
        self.selected.clear()
        for mf in self.cells.values():
            self._paint_selected(mf, False)
        self.update_count()

    def update_count(self) -> None:
        total = 0
        for p in self.selected:
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
        self.l_count.configure(text=f"已选 {len(self.selected)} 个（{human_size(total)}）")

    def _default_status(self) -> str:
        return (f"共 {len(self.items)} 个 · 已加载 {self.rendered} 个 · 已选 {len(self.selected)} 个"
                f" · 缩略图 {self.thumbs_ready}/{self.rendered}")

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

    def on_open_folder(self) -> None:
        f = self.e_folder.get().strip()
        if f and Path(f).is_dir():
            os.startfile(f)

    def on_save(self) -> None:
        if self.saving:
            return
        if not self.selected:
            messagebox.showinfo(APP_NAME, "还没有选中任何文件\n\n在缩略图上单击即可选中（变蓝打勾）")
            return
        out = self.e_out.get().strip()
        if not out:
            messagebox.showwarning(APP_NAME, "请先选择保存到的目录")
            return

        items = []
        for p in sorted(self.selected):
            try:
                st = os.stat(p)
            except OSError:
                continue
            kind = "video" if Path(p).suffix.lower() in VIDEO_EXT else "image"
            items.append(MediaFile(path=Path(p), size=st.st_size, mtime=st.st_mtime, kind=kind))
        if not items:
            messagebox.showwarning(APP_NAME, "选中的文件都不存在了")
            return

        move = bool(self.v_move.get())
        by_date = bool(self.v_bydate.get())
        action = "移动" if move else "复制"
        total_size = sum(m.size for m in items)
        if not messagebox.askyesno(
            APP_NAME,
            f"将{action} {len(items)} 个文件（{human_size(total_size)}）到：\n{out}\n\n"
            + ("源文件会被移走！\n" if move else "")
            + ("（按日期分文件夹）" if by_date else ""),
        ):
            return

        self.saving = True
        self.save_cancel.clear()
        self.set_busy(True)
        self.pb.configure(value=0)

        def work():
            try:
                ok, fail, skip = save_files(
                    items, Path(out), move=move, by_date=by_date,
                    progress_cb=lambda d, t, name: self.ui_q.put(("save_progress", d, t, name)),
                    cancel_event=self.save_cancel,
                )
                self.ui_q.put(("save_done", Path(out), ok, fail, skip, move))
            except Exception as exc:  # pragma: no cover
                self.ui_q.put(("status", f"保存失败：{exc.__class__.__name__}: {exc}"))
                self.ui_q.put(("save_done", Path(out), 0, len(items), 0, move))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------- 事件与队列 -------------------------
    def _on_canvas_resize(self, event) -> None:
        self.canvas.itemconfigure(self.win, width=event.width)
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(150, self._on_resized)

    def _on_resized(self) -> None:
        cols = self._calc_columns()
        if cols != self.columns and self.cells:
            self.columns = cols
            for idx, mf in self.cells.items():
                if mf.cell is not None:
                    mf.cell.grid(row=idx // cols, column=idx % cols, padx=5, pady=5)
            self.root.update_idletasks()
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._autofill()

    def _on_wheel(self, event) -> None:
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        try:
            _, bottom = self.canvas.yview()
        except Exception:
            return
        if bottom > 0.9:
            self.render_more()
            self._autofill()

    def _autofill(self) -> None:
        """内容不足一屏时继续补渲染。"""
        guard = 0
        while self.rendered < len(self.items) and guard < 40:
            need = self.canvas.winfo_height() + 2 * CELL_H
            if self.inner.winfo_reqheight() >= need:
                break
            self.render_more()
            guard += 1

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
        if kind == "status":
            self.set_status(msg[1])
        elif kind == "scan_done":
            _, folder, found = msg
            self.folder = folder
            self.scanning = False
            self.b_scan.configure(state="normal")
            self.all_items = found
            self.apply_filter_sort()
            self.render_more(first=True)
            self._autofill()
            if not found:
                messagebox.showinfo(APP_NAME, "这个文件夹里没有找到图片/视频")
            save_config({"folder": str(folder)})
        elif kind == "scan_failed":
            self.scanning = False
            self.b_scan.configure(state="normal")
        elif kind == "thumb":
            _, gen, idx, img = msg
            if gen != self.gen:
                return
            mf = self.cells.get(idx)
            if mf is None:
                return
            if img is None:
                mf.img_label.configure(text="无法预览", image="")
            else:
                mf.photo = ImageTk.PhotoImage(img)
                mf.img_label.configure(image=mf.photo, text="")
            self.thumbs_ready += 1
            if self.thumbs_ready % 8 == 0 or self.thumbs_ready == self.rendered:
                self.set_status(self._default_status())
        elif kind == "save_progress":
            _, done, total, name = msg
            self.pb.configure(value=(done / total * 100) if total else 0)
            self.set_status(f"保存中 {done}/{total} · {name}")
        elif kind == "save_done":
            _, out, ok, fail, skip, move = msg
            self.saving = False
            self.set_busy(False)
            self.pb.configure(value=100)
            word = "移动" if move else "复制"
            self.set_status(f"完成：{word}成功 {ok}，失败 {fail}，跳过 {skip}")

            if move:
                # 移动后源文件已不在，清掉选中与网格，重新扫描
                self.selected.clear()
                self.update_count()
                self.start_scan(self.folder) if self.folder else None
            if messagebox.askyesno(
                APP_NAME,
                f"{word}完成：成功 {ok}，失败 {fail}，跳过 {skip}。\n\n是否打开输出目录？",
            ):
                try:
                    os.startfile(str(out))
                except Exception as exc:
                    messagebox.showwarning(APP_NAME, f"打开失败：{exc}")
            save_config({"out_dir": str(out), "sort": self.cb_sort.get(),
                         "kind": self.v_kind.get(), "by_date": bool(self.v_bydate.get())})

    def set_status(self, text: str) -> None:
        self.l_status.configure(text=text)

    def set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.b_save.configure(state=state)
        self.b_scan.configure(state="disabled" if (busy or self.scanning) else "normal")

    def _on_close(self) -> None:
        self.save_cancel.set()
        try:
            self.pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    GalleryApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
