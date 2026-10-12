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
import sys
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
from gallery_picker import human_size, unique_path

APP_NAME = "Telegram 缓存挑选器"
HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "cache_config.json"
THUMB_DIR = HERE / "cache_thumbs"
INDEX_PATH = HERE / "cache_index.json"
PREVIEW_DIR = HERE / "preview"
VIDEO_TMP_DIR = HERE / "tmp_video"   # 视频抽帧的临时目录（用完即删）
THUMB = cs.THUMB

# ---------------------------------------------------------------------------
# 视觉规范
#
# 层次：内容区(BG) → 面板(PANEL) → 卡片(CARD)，全部靠明度差分层。
#
# 教训（曾被视觉评审判定"乱"）：clam 主题会自绘浅色 chrome——输入框描边、
# 单选/复选指示器、滚动条箭头都是**图片**，只设 background/foreground 压不住。
# 因此：
#   - 输入框必须显式给 bordercolor/lightcolor/darkcolor
#   - 单选/复选在 clam 下没有可配色选项（indicator 由图片绘制），
#     一律改用**分段按钮**（Seg.TButton / SegOn.TButton）表达选中态
#   - 滚动条/进度条同样要补 lightcolor/darkcolor/bordercolor
# ---------------------------------------------------------------------------
BG = "#1b1b1f"            # 内容区（最深）
PANEL = "#232329"         # 顶栏 / 底栏
BAR = "#1a1a20"           # 筛选栏（比面板明显更暗，形成可感知的一档）
CARD = "#26262c"          # 卡片
CARD_HOVER = "#30303a"    # 悬停
CARD_SEL = "#24344d"      # 选中：淡蓝底
ACCENT = "#3b82f6"        # 强调色
ACCENT_HOVER = "#4b8ef7"
ACCENT_DIM = "#2b4a77"    # 强调色的弱化版（次级选中态）
FIELD = "#31313a"         # 输入框底色（比面板亮，保证"看得出是输入框"）
FIELD_HOVER = "#35353f"
BORDER = "#4a4c53"        # 输入框描边（对 FIELD 约 2.1:1，可见但不刺眼）
BORDER_FOCUS = ACCENT
BTN = "#2e2e36"           # 次级按钮底色
FG = "#e8e8ec"            # 一级文字
SUB = "#9aa0a8"           # 二级文字
MUTED = "#7d838b"         # 三级文字（对 PANEL 约 3.6:1，可读）
SEP = "#3a3a44"           # 分隔线（对 PANEL 约 1.6:1，肉眼可辨）

FONT = "Microsoft YaHei UI"

# 网格几何：CARD_* 是卡片实际绘制尺寸，CELL_* 是含间隙的步距。
# 两者分开，卡片之间才会露出 BG 形成缝隙。
#
# 卡片内部纵向分配（CARD_H 必须容纳）：
#   上边距 7 + 图片方框 IMG_BOX + 间隙 6 + 信息行 ~17 + 下边距 6
CARD_W, CARD_H = 164, 196
GAP = 10
CELL_W, CELL_H = CARD_W + GAP, CARD_H + GAP
PAD = 10
IMG_BOX = 144             # 卡片内图片方框边长
CORNER = 9                # 卡片圆角半径
MAX_THUMBS_IN_MEM = 600   # 内存中保留的缩略图上限
PHOTO_BATCH_PER_TICK = 6  # 每个 UI 周期最多转换几张（避免卡顿）
assert 7 + IMG_BOX + 6 + 17 + 6 <= CARD_H, "CARD_H 容不下图片与信息行"


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
        self._grid_x0 = PAD       # 网格水平居中偏移
        self._text_w_cache: dict[str, int] = {}   # 文字宽度测量缓存

        # 忽略程序自身触发的滚动回调，避免递归刷新
        self._suppress = False
        self._refresh_job = None
        self._search_job = None

        root.title(APP_NAME)
        root.geometry("1280x880")
        root.minsize(1020, 680)
        root.configure(bg=BG)

        self._build_style()
        self._build_header()
        self._build_setup()
        self._build_toolbar()
        # 先占好底部（side=bottom），网格再填满中间剩余空间
        self._build_statusbar()
        self._build_bottombar()
        self._build_grid()
        self._load_config()

        self.root.bind("<Control-a>", lambda e: (self.on_select_all(), "break")[1])
        self.root.bind("<Escape>", lambda e: self.on_clear_sel())
        self.root.after(60, self._pump)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(250, self.on_load)

    # ------------------------- 界面 -------------------------
    def _build_style(self) -> None:
        """统一的 ttk 样式。

        关键点：clam 主题的输入框描边、滚动条、进度条都由**自绘图片**构成，
        只设 background/foreground 会留下浅色边缘（视觉上像贴了白胶带）。
        必须连 bordercolor / lightcolor / darkcolor 一起覆盖。
        """
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass

        # ---- 容器 ----
        st.configure("TFrame", background=BG)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("Bar.TFrame", background=BAR)
        st.configure("Sep.TFrame", background=SEP)

        # ---- 文字三级色阶（不再出现多种杂灰）----
        st.configure("TLabel", background=PANEL, foreground=FG, font=(FONT, 9))
        st.configure("Title.TLabel", background=PANEL, foreground=FG,
                     font=(FONT, 12, "bold"))
        st.configure("Field.TLabel", background=PANEL, foreground=SUB, font=(FONT, 9))
        st.configure("Hint.TLabel", background=PANEL, foreground=MUTED, font=(FONT, 8))
        st.configure("Status.TLabel", background=PANEL, foreground=SUB, font=(FONT, 8))
        st.configure("Count.TLabel", background=PANEL, foreground=ACCENT,
                     font=(FONT, 10, "bold"))
        # 筛选栏上的文字（背景是 BAR 而非 PANEL）
        st.configure("BarField.TLabel", background=BAR, foreground=SUB, font=(FONT, 9))

        # ---- 按钮：统一高度，压掉 clam 的立体边框 ----
        st.configure("TButton", font=(FONT, 9), padding=(12, 6),
                     background=BTN, foreground=FG,
                     borderwidth=0, focusthickness=0, relief="flat",
                     bordercolor=BTN, lightcolor=BTN, darkcolor=BTN)
        st.map("TButton",
               background=[("pressed", "#3a3a44"), ("active", "#383842"),
                           ("disabled", PANEL)],
               foreground=[("disabled", MUTED)],
               bordercolor=[("active", BTN)], lightcolor=[("active", BTN)],
               darkcolor=[("active", BTN)])

        # 主按钮
        st.configure("Accent.TButton", font=(FONT, 10, "bold"), padding=(18, 6),
                     background=ACCENT, foreground="#ffffff",
                     borderwidth=0, relief="flat",
                     bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT)
        st.map("Accent.TButton",
               background=[("pressed", "#2f6fd8"), ("active", ACCENT_HOVER),
                           ("disabled", ACCENT_DIM)],
               foreground=[("disabled", "#93a7c4")],
               bordercolor=[("active", ACCENT_HOVER)],
               lightcolor=[("active", ACCENT_HOVER)],
               darkcolor=[("active", ACCENT_HOVER)])

        # 分段按钮（替代 clam 无法配色的单选钮）：未选=平底，选中=蓝色实底
        st.configure("Seg.TButton", font=(FONT, 9), padding=(14, 5),
                     background=BTN, foreground=SUB,
                     borderwidth=0, relief="flat",
                     bordercolor=BTN, lightcolor=BTN, darkcolor=BTN)
        st.map("Seg.TButton",
               background=[("active", "#383842")], foreground=[("active", FG)])
        st.configure("SegOn.TButton", font=(FONT, 9, "bold"), padding=(14, 5),
                     background=ACCENT, foreground="#ffffff",
                     borderwidth=0, relief="flat",
                     bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT)
        st.map("SegOn.TButton", background=[("active", ACCENT_HOVER)])

        # ---- 输入框：显式压掉浅色描边 ----
        st.configure("TEntry", fieldbackground=FIELD, foreground=FG,
                     insertcolor=FG, borderwidth=1, padding=5, relief="flat",
                     bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        st.map("TEntry",
               fieldbackground=[("disabled", PANEL), ("focus", FIELD)],
               foreground=[("disabled", MUTED)],
               bordercolor=[("focus", ACCENT)],
               lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)])

        st.configure("TCombobox", fieldbackground=FIELD, background=FIELD,
                     foreground=FG, arrowcolor=SUB, borderwidth=1, padding=4,
                     relief="flat", bordercolor=BORDER,
                     lightcolor=BORDER, darkcolor=BORDER)
        st.map("TCombobox",
               fieldbackground=[("readonly", FIELD)],
               foreground=[("readonly", FG)],
               bordercolor=[("focus", ACCENT)],
               lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)])
        # 下拉列表本身（Tk 的 Listbox）也要跟着深色
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

        # ---- 复选框 ----
        # 关键：clam 的 Checkbutton.indicator 用的是
        # upperbordercolor / lowerbordercolor（不是 bordercolor/lightcolor/darkcolor）
        # ——之前只设 bordercolor 完全没生效，浅色斜面边框因此留了下来。
        st.configure("TCheckbutton", background=PANEL, foreground=FG,
                     font=(FONT, 9), focusthickness=0, focuscolor=PANEL,
                     indicatorbackground=FIELD, indicatorforeground=ACCENT,
                     upperbordercolor=BORDER, lowerbordercolor=BORDER,
                     indicatorsize=13)
        st.map("TCheckbutton",
               background=[("active", PANEL)],
               foreground=[("active", FG)],
               indicatorbackground=[("selected", ACCENT), ("!selected", FIELD)],
               indicatorforeground=[("selected", "#ffffff")],
               upperbordercolor=[("selected", ACCENT), ("!selected", BORDER)],
               lowerbordercolor=[("selected", ACCENT), ("!selected", BORDER)])
        st.configure("Bar.TCheckbutton", background=BAR, foreground=FG,
                     font=(FONT, 9), focusthickness=0, focuscolor=BAR,
                     indicatorbackground=FIELD, indicatorforeground=ACCENT,
                     upperbordercolor=BORDER, lowerbordercolor=BORDER,
                     indicatorsize=13)
        st.map("Bar.TCheckbutton",
               background=[("active", BAR)],
               foreground=[("active", FG)],
               indicatorbackground=[("selected", ACCENT), ("!selected", FIELD)],
               indicatorforeground=[("selected", "#ffffff")],
               upperbordercolor=[("selected", ACCENT), ("!selected", BORDER)],
               lowerbordercolor=[("selected", ACCENT), ("!selected", BORDER)])

        # ---- 进度条：细轨，轨道比背景更暗以便区分 ----
        st.configure("Slim.Horizontal.TProgressbar", background=ACCENT,
                     troughcolor="#14141a", borderwidth=0, thickness=6,
                     bordercolor="#14141a", lightcolor=ACCENT, darkcolor=ACCENT)
        st.map("Slim.Horizontal.TProgressbar", background=[("disabled", "#2b2b33")])

        # ---- 滚动条：轨道独立底色 + 深色滑块，压掉 clam 亮箭头 ----
        st.configure("Vertical.TScrollbar", background="#3d3d47",
                     troughcolor="#15151a", borderwidth=0, arrowsize=12,
                     bordercolor="#15151a",
                     lightcolor="#3d3d47", darkcolor="#3d3d47",
                     arrowcolor=SUB, relief="flat")
        st.map("Vertical.TScrollbar",
               background=[("active", "#4d4d59"), ("pressed", ACCENT)],
               lightcolor=[("active", "#4d4d59")],
               darkcolor=[("active", "#4d4d59")],
               arrowcolor=[("active", FG)])

    # ---------- 顶部标题栏 ----------
    def _divider(self) -> None:
        """一条 1px 分隔线，用于划分区域。"""
        ttk.Frame(self.root, style="Sep.TFrame", height=1).pack(fill="x", side="top")

    def _build_header(self) -> None:
        # padding 左缘与设置区/底栏标签对齐（同为 16）
        bar = ttk.Frame(self.root, style="Panel.TFrame", padding=(16, 12, 16, 12))
        bar.pack(fill="x", side="top")
        ttk.Label(bar, text="Telegram 缓存挑选器", style="Title.TLabel").pack(side="left")
        self.l_header_stat = ttk.Label(bar, text="", style="Status.TLabel")
        self.l_header_stat.pack(side="right")
        self._divider()

    # ---------- 数据源设置 ----------
    def _build_setup(self) -> None:
        wrap = ttk.Frame(self.root, style="Panel.TFrame", padding=(16, 14, 16, 14))
        wrap.pack(fill="x", side="top")

        # 第一行：数据源 + 主操作（统一 8px 间距节奏）
        r1 = ttk.Frame(wrap, style="Panel.TFrame")
        r1.pack(fill="x")
        ttk.Label(r1, text="tdata 目录", style="Field.TLabel", width=10).pack(side="left")
        self.e_tdata = ttk.Entry(r1)
        self.e_tdata.pack(side="left", fill="x", expand=True, padx=(8, 8))
        ttk.Button(r1, text="浏览…", command=self.on_browse).pack(side="left")
        self.b_load = ttk.Button(r1, text="读取缓存", style="Accent.TButton",
                                 command=self.on_load)
        self.b_load.pack(side="left", padx=(8, 0))

        # 第二行：可选密码 + 只读提示（提示只在此处出现一次）
        r2 = ttk.Frame(wrap, style="Panel.TFrame")
        r2.pack(fill="x", pady=(10, 0))
        ttk.Label(r2, text="锁定密码", style="Field.TLabel", width=10).pack(side="left")
        self.e_pass = ttk.Entry(r2, width=16, show="*")
        self.e_pass.pack(side="left", padx=(8, 8))
        self.b_reload = ttk.Button(r2, text="重新扫描", command=self.on_reload)
        self.b_reload.pack(side="left")
        ttk.Label(r2, text="没设过就留空　·　原 tdata 只读，不会被改动",
                  style="Hint.TLabel").pack(side="left", padx=(12, 0))
        self._divider()

    # ---------- 筛选栏（独立一档背景）----------
    def _build_toolbar(self) -> None:
        wrap = ttk.Frame(self.root, style="Bar.TFrame", padding=(16, 10, 16, 10))
        wrap.pack(fill="x", side="top")

        row = ttk.Frame(wrap, style="Bar.TFrame")
        row.pack(fill="x")

        # 类型：分段按钮（clam 单选钮无法配色，故用按钮表达选中态）
        ttk.Label(row, text="类型", style="BarField.TLabel").pack(side="left")
        self.v_kind = tk.StringVar(value="image")
        self._kind_btns: dict[str, ttk.Button] = {}
        kind_box = ttk.Frame(row, style="Bar.TFrame")
        kind_box.pack(side="left", padx=(8, 20))
        for txt, val in (("图片", "image"), ("视频", "video"), ("全部", "all")):
            b = ttk.Button(kind_box, text=txt, style="Seg.TButton",
                           command=lambda v=val: self.on_kind_change(v))
            b.pack(side="left", padx=(0, 6))
            self._kind_btns[val] = b
        self._sync_kind_buttons()

        ttk.Label(row, text="排序", style="BarField.TLabel").pack(side="left")
        self.cb_sort = ttk.Combobox(
            row, width=11, state="readonly",
            values=("日期 新→旧", "日期 旧→新", "大小 大→小", "大小 小→大", "来源目录"),
        )
        self.cb_sort.set("日期 新→旧")
        self.cb_sort.pack(side="left", padx=(8, 20))
        self.cb_sort.bind("<<ComboboxSelected>>", lambda e: self.on_filter_change())

        # 搜索靠右，与筛选区分开
        ttk.Button(row, text="✕", command=self.on_clear_search).pack(side="right")
        self.e_search = ttk.Entry(row, width=22)
        self.e_search.pack(side="right", padx=(8, 8))
        self.e_search.bind("<KeyRelease>", self._on_search_key)
        ttk.Label(row, text="搜索", style="BarField.TLabel").pack(side="right")

    def _sync_kind_buttons(self) -> None:
        """同步类型分段按钮的选中样式。"""
        cur = self.v_kind.get()
        for val, btn in self._kind_btns.items():
            btn.configure(style="SegOn.TButton" if val == cur else "Seg.TButton")

    def on_kind_change(self, val: str) -> None:
        if self.v_kind.get() == val:
            return
        self.v_kind.set(val)
        self._sync_kind_buttons()
        self.on_filter_change()

    # ---------- 底部操作条 ----------
    def _build_bottombar(self) -> None:
        ttk.Frame(self.root, style="Sep.TFrame", height=1).pack(fill="x", side="bottom")
        wrap = ttk.Frame(self.root, style="Panel.TFrame", padding=(16, 12, 16, 12))
        wrap.pack(fill="x", side="bottom")

        # 第一行：选择操作 + 保存设置
        r1 = ttk.Frame(wrap, style="Panel.TFrame")
        r1.pack(fill="x")
        self.l_count = ttk.Label(r1, text="未选择", style="Count.TLabel", width=18)
        self.l_count.pack(side="left", padx=(0, 12))
        ttk.Button(r1, text="全选", command=self.on_select_all).pack(side="left", padx=(0, 8))
        ttk.Button(r1, text="反选", command=self.on_invert).pack(side="left", padx=(0, 8))
        ttk.Button(r1, text="清除", command=self.on_clear_sel).pack(side="left")

        self.v_bydate = tk.BooleanVar(value=False)
        ttk.Checkbutton(r1, text="按日期分文件夹", variable=self.v_bydate).pack(side="right")

        # 第二行：保存目标 + 主按钮（左边缘与上一区对齐）
        r2 = ttk.Frame(wrap, style="Panel.TFrame")
        r2.pack(fill="x", pady=(10, 0))
        ttk.Label(r2, text="保存到", style="Field.TLabel", width=10).pack(side="left")
        self.e_out = ttk.Entry(r2)
        self.e_out.pack(side="left", fill="x", expand=True, padx=(8, 8))
        ttk.Button(r2, text="浏览…", command=self.on_pick_out).pack(side="left", padx=(0, 8))
        ttk.Button(r2, text="打开目录", command=self.on_open_out).pack(side="left")
        self.b_save = ttk.Button(r2, text="保存选中", style="Accent.TButton",
                                 command=self.on_save)
        self.b_save.pack(side="left", padx=(12, 0))

    def _build_grid(self) -> None:
        wrap = ttk.Frame(self.root, padding=(14, 10, 8, 10))
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        vbar = ttk.Scrollbar(wrap, orient="vertical", command=self._on_scrollbar)
        self.canvas.configure(yscrollcommand=vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y", padx=(6, 0))

        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-4>", lambda e: self._scroll_units(-3))
        self.canvas.bind("<Button-5>", lambda e: self._scroll_units(3))

    def _build_statusbar(self) -> None:
        wrap = ttk.Frame(self.root, style="Panel.TFrame", padding=(16, 7, 16, 7))
        wrap.pack(fill="x", side="bottom")
        self.l_status = ttk.Label(wrap, text="就绪", style="Status.TLabel")
        self.l_status.pack(side="left")
        # 进度条只在任务进行时出现，常态不占位（避免"像个空输入框"）
        self.pb = ttk.Progressbar(wrap, mode="determinate", maximum=100,
                                  style="Slim.Horizontal.TProgressbar", length=200)
        self._pb_visible = False

    def _show_progress(self, value: float | None = None) -> None:
        """显示进度条（并可选设值）。"""
        if not self._pb_visible:
            self.pb.pack(side="right")
            self._pb_visible = True
        if value is not None:
            self.pb.configure(value=value)

    def _hide_progress(self) -> None:
        """隐藏进度条，回到干净的状态栏。"""
        if self._pb_visible:
            self.pb.pack_forget()
            self._pb_visible = False

    def _on_search_key(self, event) -> None:
        """搜索框输入防抖：停顿 300ms 再过滤，避免每个字符都重建网格。"""
        if event.keysym in ("Return", "Escape"):
            return
        if self._search_job is not None:
            self.root.after_cancel(self._search_job)
        self._search_job = self.root.after(300, self._apply_search)

    def _apply_search(self) -> None:
        self._search_job = None
        self.on_filter_change()

    def on_clear_search(self) -> None:
        self.e_search.delete(0, "end")
        self.on_filter_change()

    def _default_tdata(self) -> str:
        """探测 tdata 默认位置（跨平台，不硬编码盘符）。

        顺序：Telegram 官方默认路径 → 便携版常见位置（程序旁 / 各盘根）。
        找不到就返回空字符串，由用户手动选择。
        """
        cands: list[Path] = []
        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA")
            if appdata:
                cands.append(Path(appdata) / "Telegram Desktop" / "tdata")
        elif sys.platform == "darwin":
            cands.append(Path.home() / "Library" / "Application Support"
                         / "Telegram Desktop" / "tdata")
        else:
            cands.append(Path.home() / ".local" / "share" / "TelegramDesktop" / "tdata")
        # 便携版：Telegram.exe 旁边的 tdata（可能在任何盘）
        for anchor in (HERE, HERE.parent):
            cands.append(anchor / "tdata")
        for drive in ("C:", "D:", "E:", "F:"):
            cands.append(Path(f"{drive}/Telegram/tdata"))
        for c in cands:
            try:
                if c.is_dir():
                    return str(c)
            except OSError:
                continue
        return ""

    def _load_config(self) -> None:
        cfg = load_config()
        tdata = cfg.get("tdata") or ""
        if not tdata:
            tdata = self._default_tdata()
        self.e_tdata.delete(0, "end")
        self.e_tdata.insert(0, tdata)

        out = cfg.get("out_dir") or str(HERE / "挑选保存")
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
        """按当前滚动位置渲染可视区（复用图元，按需载入缩略图）。

        网格整体水平居中，消除右侧留白不均匀的问题；卡片之间保留
        GAP 间隙（CARD_* 与 CELL_* 分离），滚动时露出内容区底色形成分格。
        """
        if not self.items:
            for cell in self.cell_pool:
                self._hide_cell(cell)
            self.cells_by_slot.clear()
            return

        cw = max(self.canvas.winfo_width(), 200)
        ch = max(self.canvas.winfo_height(), 200)
        cols = max(1, (cw - PAD) // CELL_W)
        self.columns = cols
        # 居中偏移：把取整浪费的余量平均分到两侧
        self._grid_x0 = max(PAD, (cw - cols * CELL_W) // 2)

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

    def _new_cell_items(self, cell: dict) -> None:
        """（重新）创建一张卡片的全部 Canvas 图元。

        卡片底用 **smooth 多边形**绘制以实现圆角（Canvas 的 rectangle 只能
        画直角），与右上角圆形对勾徽标的形状语言一致；全部为 Canvas 图元，
        滚动时可循环复用。
        """
        cell["rect"] = self.canvas.create_polygon(
            0, 0, 0, 0, fill=CARD, outline=CARD, width=2, smooth=False)
        cell["img"] = self.canvas.create_image(0, 0, anchor="nw")
        # 视频角标：贴在图右下角（黑底 + "视频"），四角用真实圆弧
        cell["tag"] = self.canvas.create_polygon(
            0, 0, 0, 0, fill="#0d0d10", outline="", state="hidden", smooth=False)
        cell["tag_txt"] = self.canvas.create_text(
            0, 0, anchor="center", text="视频", fill="#e8e8ec",
            font=(FONT, 8), state="hidden")
        # 选中徽标：圆底 + 对勾
        cell["badge"] = self.canvas.create_oval(
            0, 0, 0, 0, fill=ACCENT, outline="", state="hidden")
        cell["badge_txt"] = self.canvas.create_text(
            0, 0, anchor="center", text="✓", fill="#ffffff",
            font=(FONT, 10, "bold"), state="hidden")
        cell["info"] = self.canvas.create_text(
            0, 0, anchor="nw", text="", fill=SUB, font=(FONT, 9))
        cell["photo"] = None

    @staticmethod
    def _rounded_pts(x1: int, y1: int, x2: int, y2: int, r: int,
                     seg: int = 6) -> list[float]:
        """生成圆角矩形的点列（四角用真实圆弧采样）。

        早期版本每个角只给 2 个锚点、依赖 smooth=True 插值，实测视觉半径
        只有 3-4px（远小于设定值）。这里改为按 seg 段采样圆弧、以
        smooth=False 直接连线，圆角半径才与参数一致。
        """
        import math

        pts: list[float] = []
        corners = (
            (x2 - r, y1 + r, -math.pi / 2),   # 右上
            (x2 - r, y2 - r, 0.0),            # 右下
            (x1 + r, y2 - r, math.pi / 2),    # 左下
            (x1 + r, y1 + r, math.pi),        # 左上
        )
        for cx, cy, start in corners:
            for i in range(seg + 1):
                a = start + (math.pi / 2) * i / seg
                pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
        return pts

    def _acquire_cell(self) -> dict:
        """取一个空闲图元组；池子为空则新建一组 Canvas 图元。"""
        for cell in self.cell_pool:
            if cell["slot"] is None:
                return cell
        cell: dict = {"slot": None}
        self._new_cell_items(cell)
        self.cell_pool.append(cell)
        return cell

    def _text_w(self, cell: dict, key: str) -> int:
        """测量图元当前文字的像素宽度（带缓存，避免每帧查询 Tcl）。

        Canvas 的 bbox() 会触发一次 Tcl 往返，滚动时每格都调用会拖慢渲染，
        因此按"文字内容"缓存结果——同一批文件名/大小的组合高度重复。
        """
        try:
            text = self.canvas.itemcget(cell[key], "text")
        except tk.TclError:
            return CARD_W
        cached = self._text_w_cache.get(text)
        if cached is not None:
            return cached
        try:
            x1, _, x2, _ = self.canvas.bbox(cell[key])
            w = max(1, int(x2 - x1))
        except (tk.TclError, TypeError):
            w = CARD_W
        # 简单上限，避免缓存无限增长（文字组合数量级远小于此）
        if len(self._text_w_cache) > 4000:
            self._text_w_cache.clear()
        self._text_w_cache[text] = w
        return w

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
        """删除并重建该卡片的全部图元（用于从失效图像中恢复）。"""
        for key in ("rect", "img", "tag", "tag_txt", "badge", "badge_txt", "info"):
            try:
                self.canvas.delete(cell[key])
            except (tk.TclError, KeyError):
                pass
        self._new_cell_items(cell)

    def _hide_cell(self, cell: dict) -> None:
        cell["slot"] = None
        # 先解除图像引用，再丢弃 Python 侧引用——否则 Tk 销毁图像后，
        # Canvas 图元会留下悬空引用，下次复用时 itemconfigure 直接抛错。
        self._canvas_cfg(cell, "img", image="", state="hidden")
        for key in ("rect", "tag", "tag_txt", "badge", "badge_txt", "info"):
            self._canvas_cfg(cell, key, state="hidden")
        cell["photo"] = None

    def _place_cell(self, cell: dict, idx: int, col: int, row: int) -> None:
        """把第 idx 项画到 (col,row) 格；复用已有图元，只改坐标与图像。

        坐标基于 CARD_W/CARD_H（不含间隙），因此卡片之间会自然露出
        内容区底色，形成清晰的分格；网格整体按 _grid_x0 水平居中。
        """
        it = self.items[idx]
        x = self._grid_x0 + col * CELL_W
        y = PAD + row * CELL_H
        rel = str(it.entry.rel)
        is_sel = rel in self.selected
        is_hover = (self.hover_slot == idx) and not is_sel

        # 卡片底色：选中=淡蓝底+蓝描边；悬停=略亮；常态=卡片色（描边同底色）
        if is_sel:
            fill, outline = CARD_SEL, ACCENT
        elif is_hover:
            fill, outline = CARD_HOVER, CARD_HOVER
        else:
            fill, outline = CARD, CARD

        # 图片区在卡片内水平居中；下方留出信息行
        box_x = x + (CARD_W - IMG_BOX) // 2
        box_y = y + 8

        self._suppress = True
        try:
            # 选中态画圆角描边（与圆形对勾徽标形状语言一致）；
            # 常态用同色描边以占位，视觉上就是一块干净的圆角卡片。
            self._canvas_cfg(cell, "rect", state="normal", fill=fill, outline=outline)
            self._canvas_coords(
                cell, "rect",
                *self._rounded_pts(x, y, x + CARD_W, y + CARD_H, CORNER))

            # 缩略图：在 IMG_BOX 方框内居中
            photo = self._thumb_for(idx, it)
            img_w = img_h = 0
            if photo is not None:
                self._canvas_cfg(cell, "img", state="normal", image=photo)
                pw, ph = photo.width(), photo.height()
                img_w, img_h = pw, ph
                ix = box_x + (IMG_BOX - pw) // 2
                iy = box_y + (IMG_BOX - ph) // 2
                self._canvas_coords(cell, "img", ix, iy)
                cell["photo"] = photo
            else:
                self._canvas_cfg(cell, "img", image="", state="hidden")
                cell["photo"] = None

            # 视频角标：贴在**实际图像**的右下角（而非容器底边，
            # 否则 letterbox 缩略图会让角标悬空在卡片底色上）
            if it.kind == "video" and photo is not None:
                tag_w, tag_h = 34, 18
                anchor_x = box_x + (IMG_BOX - img_w) // 2
                anchor_y = box_y + (IMG_BOX - img_h) // 2
                tx = anchor_x + img_w - tag_w - 3
                ty = anchor_y + img_h - tag_h - 3
                self._canvas_cfg(cell, "tag", state="normal")
                self._canvas_coords(cell, "tag",
                                    *self._rounded_pts(tx, ty, tx + tag_w,
                                                       ty + tag_h, 4))
                self._canvas_cfg(cell, "tag_txt", state="normal")
                self._canvas_coords(cell, "tag_txt",
                                    tx + tag_w // 2, ty + tag_h // 2)
            else:
                self._canvas_cfg(cell, "tag", state="hidden")
                self._canvas_cfg(cell, "tag_txt", state="hidden")

            # 选中徽标：仅选中时出现（右上角圆底对勾）
            if is_sel:
                r = 11
                cx = x + CARD_W - r - 7
                cy = y + r + 7
                self._canvas_cfg(cell, "badge", state="normal")
                self._canvas_coords(cell, "badge", cx - r, cy - r, cx + r, cy + r)
                self._canvas_cfg(cell, "badge_txt", state="normal")
                self._canvas_coords(cell, "badge_txt", cx, cy)
            else:
                self._canvas_cfg(cell, "badge", state="hidden")
                self._canvas_cfg(cell, "badge_txt", state="hidden")

            # 底部信息：日期 时间 · 大小（水平居中，列尾自然对齐）
            # 统一用二级文字色：选中时也不提级，避免同一信息出现两种层级。
            label = (f"{datetime.fromtimestamp(it.mtime):%m-%d %H:%M}"
                     f"  ·  {human_size(it.size)}")
            self._canvas_cfg(cell, "info", state="normal", text=label, fill=SUB)
            self._canvas_coords(cell, "info",
                                x + (CARD_W - self._text_w(cell, "info")) // 2,
                                y + CARD_H - 20)
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
        """把画布坐标映射到项目下标；落在间隙或网格外返回 None。"""
        if not self.items:
            return None
        cx = self.canvas.canvasx(x)
        cy = self.canvas.canvasy(y)
        gx = cx - self._grid_x0
        gy = cy - PAD
        if gx < 0 or gy < 0:
            return None
        col = int(gx // CELL_W)
        row = int(gy // CELL_H)
        if col < 0 or col >= self.columns:
            return None
        # 落在卡片之间的间隙里不算命中
        if (gx % CELL_W) > CARD_W or (gy % CELL_H) > CARD_H:
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
        """更新"已选"计数：底栏计数 + 主按钮文案 + 顶栏概览。

        只统计当前筛选列表里可见的选中项，避免"看不到却被选中"造成的困惑。
        """
        by_rel = {str(it.entry.rel): it for it in self.items}
        total = 0
        n = 0
        for rel in self.selected:
            it = by_rel.get(rel)
            if it is not None:
                total += it.size
                n += 1
        if n:
            self.l_count.configure(text=f"已选 {n} 个　{human_size(total)}")
            self.b_save.configure(text=f"保存选中（{n}）")
        else:
            self.l_count.configure(text="未选择")
            self.b_save.configure(text="保存选中")
        self._update_header()

    def _update_header(self) -> None:
        """顶栏右侧显示当前视图概览。"""
        if not self.all_items:
            self.l_header_stat.configure(text="")
            return
        kind_cn = {"image": "图片", "video": "视频", "all": "全部"}.get(self.v_kind.get(), "")
        self.l_header_stat.configure(
            text=f"{kind_cn} {len(self.items)} / 共 {len(self.all_items)}"
        )

    def _default_status(self) -> str:
        return (f"缩略图已载入 {self.thumbs_ready}　·　"
                f"单击选中，Shift+单击连选，双击预览")

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
        self._show_progress(0)
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
            self._show_progress((i / total * 100) if total else 0)
            self.set_status(txt)
        elif kind == "load_done":
            _, tdata, entries, stats, passcode, elapsed = msg
            self.tdata = tdata
            self.busy = False
            self.set_busy(False)
            self._hide_progress()
            self.all_items = [Item(entry=e) for e in entries]
            self.reader = cs.CacheReader(tdata, passcode)
            self.apply_filter_sort()
            self.refresh_view()
            self.update_count()          # 同步顶栏概览与计数
            reused = stats["reused"]
            self.set_status(
                f"读取完成：媒体 {stats['ok']} 个"
                + (f"（复用 {reused}）" if reused else "")
                + f"　·　跳过 {stats['skipped']}　·　失败 {stats['failed']}"
                + f"　·　耗时 {elapsed:.1f}s"
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
            self._hide_progress()
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
            self._show_progress((d / t * 100) if t else 0)
            self.set_status(f"保存中 {d}/{t} · {n}")
        elif kind == "save_done":
            _, out, ok, fail = msg
            self.busy = False
            self.set_busy(False)
            self._hide_progress()
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
