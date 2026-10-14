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
import logging
import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
import traceback
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import app_paths
import cache_source as cs
import firstrun
import i18n
import ui_kit
import ui_style
from i18n import t
from ui_kit import (
    FONT,
    detect_hardware,
    gear_workers,
    human_size,
    recommended_workers,
    rounded_pts,
    unique_path,
)
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

HERE = Path(__file__).resolve().parent
# 运行时产物统一落在「数据目录」（便携=程序旁 data/，否则用户目录；见 app_paths）
DATA_DIR = app_paths.data_dir()
CONFIG_PATH = DATA_DIR / "cache_config.json"
THUMB_DIR = DATA_DIR / "cache_thumbs"
INDEX_PATH = DATA_DIR / "cache_index.json"
VIDEO_TMP_DIR = DATA_DIR / "tmp_video"   # 视频抽帧的临时目录（用完即删）
EXPORT_DIR = app_paths.export_dir()      # 默认导出目录：与 API 模式共用同一个
THUMB = cs.THUMB

# 日志：三个入口共用同一个 logger（输出落到数据目录的 logs/，见 ui_kit.enable_file_log）。
# 界面只报"成功 N / 失败 M"，逐条失败的原因必须留痕，否则用户无从排查。
_log = logging.getLogger(ui_kit.LOG_NAME)


def _app_title() -> str:
    """窗口标题（同时用作所有对话框的标题）。"""
    return t("app.cache_title")


# 排序：**内部键**与显示文案分离（显示文案随语言变化，逻辑只看键）。
# 早先直接把中文文案当键并写进 cache_config.json，故保留旧值迁移表。
SORT_KEYS = ("date_desc", "date_asc", "size_desc", "size_asc", "source")
_LEGACY_SORT = {
    "日期 新→旧": "date_desc",
    "日期 旧→新": "date_asc",
    "大小 大→小": "size_desc",
    "大小 小→大": "size_asc",
    "来源目录": "source",
}

# 导出内容（内部键，文案走 kind.*）：与显示文案分离，逻辑只看键。
KIND_VALUES = ("all", "image", "video")

# 输出类型（可单独勾选的**扫描格式**）：内部值即扩展名（含点），与
# ``cache_source.sniff()`` 的返回值一一对应；显示名去掉前导点。
# 与上游 tg-scoop 的 9 种 MediaType 一致（未勾选的格式在扫描时被早停跳过）。
FORMATS = (
    ".mp4", ".webm", ".avi", ".mkv", ".mov",     # 视频
    ".jpg", ".png", ".gif", ".webp",             # 图片
)

# 性能档位（内部键，文案走 gear.*）：控制后台缩略图工作线程数。
# 移植自上游 tg-scoop 的三档：保守=串行、均衡=推荐、极速=满核。
GEAR_KEYS = ("conservative", "balanced", "turbo")

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
# 色板（含卡片色 CARD / CARD_HOVER / CARD_SEL）全部来自 ui_style，两个 GUI 同一套。

# 字体见 ui_kit（两个挑选器共用）

# 网格几何：CARD_* 是卡片实际绘制尺寸，CELL_* 是含间隙的步距。
# 两者分开，卡片之间才会露出 BG 形成缝隙。
#
# 卡片内部纵向分配（CARD_H 必须容纳）；与 _place_cell 里的 box_y 偏移保持一致：
#   上边距 8 + 图片方框 IMG_BOX + 间隙 6 + 信息行 ~17 + 下边距 6
CARD_W, CARD_H = 164, 196
GAP = 10
CELL_W, CELL_H = CARD_W + GAP, CARD_H + GAP
PAD = 10
IMG_BOX = 144             # 卡片内图片方框边长
CORNER = 9                # 卡片圆角半径
MAX_THUMBS_IN_MEM = 600   # 内存中保留的缩略图上限
PHOTO_BATCH_PER_TICK = 6  # 每个 UI 周期最多转换几张（避免卡顿）
PUMP_MS = 40              # 消息队列轮询间隔（毫秒）
assert 8 + IMG_BOX + 6 + 17 + 6 <= CARD_H, "CARD_H 容不下图片与信息行"


@dataclass
class MediaItem:
    """网格中的一条**缓存**媒体（对应一个 `Tdata` 文件）。

    与在线挑选器的 ``tg_picker.MediaItem`` 是同一概念、字段不同：
    这里的数据来自已解密的本地缓存，故只包一层 :class:`cache_source.CacheEntry`。
    """

    entry: cs.CacheEntry
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = cs.default_cache_name(self.entry)

    @property
    def kind(self) -> str:
        """媒体类型：``image`` / ``video``。"""
        return self.entry.kind

    @property
    def size(self) -> int:
        """明文大小（字节）；未知时回退到加密文件大小。"""
        return self.entry.size or self.entry.enc_size

    @property
    def mtime(self) -> float:
        """缓存文件的修改时间（用作"拍摄/接收时间"的近似）。"""
        return self.entry.mtime


# ---------------------------------------------------------------------------
# 读取与保存（纯函数，便于自检）
# ---------------------------------------------------------------------------
def load_cache(tdata: Path, passcode: str, progress_cb=None, cancel_event=None,
               exts: tuple[str, ...] | None = None, workers: int = 1):
    """读取并解析缓存，返回 (条目, 统计)。

    ``exts`` 为可选的输出格式过滤（扩展名集合，None=不过滤），只在嗅探命中后
    按格式早停；``workers`` 为读盘/解密的并发线程数（来自界面「性能档位」）。
    两者均见 :func:`cache_source.prepare`。
    """
    return cs.prepare(
        tdata=tdata,
        passcode=passcode,
        index_path=INDEX_PATH,
        thumb_dir=THUMB_DIR,
        progress_cb=progress_cb,
        cancel_event=cancel_event,
        exts=exts,
        workers=workers,
    )


def save_items(items, out_dir: Path, tdata: Path, passcode: str,
               by_date: bool = False, progress_cb=None,
               cancel_event=None, reader: cs.CacheReader | None = None):
    """解密选中项并写出到 out_dir。返回 (成功, 失败)。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if reader is None:
        reader = cs.get_reader(tdata, passcode)
    ok = fail = 0
    total = len(items)
    for i, it in enumerate(items, 1):
        if cancel_event is not None and cancel_event.is_set():
            break
        try:
            export_one(it, out_dir, tdata, passcode, by_date=by_date, reader=reader)
            ok += 1
        except Exception as exc:
            fail += 1
            # 逐条留痕：状态栏只有汇总数字，这里记下"是哪条、为什么失败"
            _log.warning("保存失败：%s：%s: %s",
                         it.name, exc.__class__.__name__, exc)
        if progress_cb is not None:
            progress_cb(i, total, it.name)
    return ok, fail


def export_one(it: MediaItem, out_dir: Path, tdata: Path, passcode: str,
               by_date: bool = False, reader: cs.CacheReader | None = None) -> Path:
    """把一条缓存媒体解密并导出到 ``out_dir``，返回落盘路径。

    重名时走 :func:`unique_path` 追加序号，绝不覆盖已有文件；落盘后把时间戳
    对齐到缓存文件的修改时间，方便按日期整理。``by_date=True`` 时再按
    ``YYYY-MM-DD`` 分子目录（与「保存选中」同一套规则）。
    """
    out_dir = Path(out_dir)
    target_dir = out_dir
    if by_date:
        target_dir = out_dir / datetime.fromtimestamp(it.mtime).strftime("%Y-%m-%d")
    target_dir.mkdir(parents=True, exist_ok=True)
    if reader is None:
        reader = cs.get_reader(tdata, passcode)
    dest = unique_path(target_dir / it.name)
    dest.write_bytes(reader.decrypt(it.entry.cache_path))
    try:
        os.utime(dest, (it.mtime, it.mtime))
    except OSError:
        pass
    return dest


def cache_artifacts() -> list[Path]:
    """本工具产生的、**可安全删除且会自动重建**的缓存路径。

    只包含缩略图缓存、索引、抽帧临时目录——用户自己保存出来的媒体绝不在此列。
    """
    return [THUMB_DIR, INDEX_PATH, VIDEO_TMP_DIR]


def cache_bytes(paths: list[Path] | None = None) -> int:
    """统计上述缓存占用的字节数（目录递归累加）。"""
    total = 0
    for p in (cache_artifacts() if paths is None else paths):
        p = Path(p)
        try:
            if p.is_dir():
                total += sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
            elif p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def clean_cache() -> int:
    """删除缓存并返回释放的字节数；单个路径删失败不影响其它。"""
    freed = cache_bytes()
    for p in cache_artifacts():
        p = Path(p)
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.is_file():
                p.unlink()
        except OSError:
            continue
    return freed


def load_config() -> dict:
    """读取本工具的本地配置（路径固定为 cache_config.json）。"""
    return ui_kit.load_config(CONFIG_PATH)


def save_config(data: dict) -> None:
    """把配置合并写回 cache_config.json（失败只告警，不影响运行）。"""
    ui_kit.save_config(CONFIG_PATH, data)


# ---------------------------------------------------------------------------
# GUI —— Canvas 虚拟化网格
# ---------------------------------------------------------------------------
class CacheApp(LangMixin):
    """缓存挑选器主窗口（Canvas 图元虚拟化网格）。

    语言切换的通用机制继承自 :class:`ui_lang.LangMixin`，本类只补差异部分。
    """

    _lang_box_style = "Panel.TFrame"     # 语言按钮所在容器的 ttk 样式

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.ui_q: queue.Queue = queue.Queue()
        self.pool: concurrent.futures.ThreadPoolExecutor | None = None

        # 硬件检测（提示性质，失败按未知处理）：决定性能档位的推荐值与标签文案
        self.hw = detect_hardware()
        self.hw_cores = self.hw.cores or 1
        self.hw_rec = recommended_workers(self.hw)

        self.all_items: list[MediaItem] = []
        self.items: list[MediaItem] = []
        self.selected: set[str] = set()
        self.columns = 1
        self.last_click: int | None = None
        self.busy = False
        self.cancel = threading.Event()
        self.tdata: Path | None = None
        self.reader: cs.CacheReader | None = None

        # 缩略图
        self.thumb_mem: OrderedDict[int, ImageTk.PhotoImage] = OrderedDict()
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
        self._pump_job = None
        self._exporting = False       # 单条导出的重入保护

        # i18n：先定语言再建界面（首次绘制即用正确语言）；登记表先建、回调后注册。
        i18n.init()
        self._init_lang_support()

        root.title(_app_title())
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
        self.update_count()          # 让计数/主按钮/顶栏概览在首次也在正确语言下

        self._enable_lang_support()

        self.root.bind("<Control-a>", lambda e: (self.on_select_all(), "break")[1])
        self.root.bind("<Escape>", lambda e: self.on_clear_sel())
        self._pump_job = self.root.after(60, self._pump)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # 刻意**不**自动读取缓存：解密/出缩略图是本工具最重的一步，
        # 交给用户点「读取缓存」触发（延迟提取，避免打开即全量扫描）。

    # ------------------------- 界面 -------------------------
    def _build_style(self) -> None:
        """统一的 ttk 样式（按控件类别拆到下面的小函数，便于定位与修改）。

        关键点：clam 主题的输入框描边、滚动条、进度条都由**自绘图片**构成，
        只设 background/foreground 会留下浅色边缘（视觉上像贴了白胶带）。
        必须连 bordercolor / lightcolor / darkcolor 一起覆盖。
        """
        st = ttk.Style()
        ui_style.use_clam(st)
        self._style_containers(st)
        self._style_labels(st)
        self._style_buttons(st)
        self._style_inputs(st)
        self._style_checkbuttons(st)
        self._style_progressbars(st)
        self._style_scrollbars(st)

    @staticmethod
    def _style_containers(st: ttk.Style) -> None:
        """容器：内容区 / 面板 / 筛选栏 / 分隔线，靠明度差分档。"""
        st.configure("TFrame", background=BG)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("Bar.TFrame", background=BAR)
        st.configure("Sep.TFrame", background=SEP)

    @staticmethod
    def _style_labels(st: ttk.Style) -> None:
        """文字三级色阶（不再出现多种杂灰）。"""
        st.configure("TLabel", background=PANEL, foreground=FG, font=(FONT, 9))
        st.configure("Title.TLabel", background=PANEL, foreground=FG,
                     font=(FONT, 12, "bold"))
        st.configure("Section.TLabel", background=PANEL, foreground=FG,
                     font=(FONT, 11, "bold"))
        st.configure("Field.TLabel", background=PANEL, foreground=SUB, font=(FONT, 9))
        st.configure("Hint.TLabel", background=PANEL, foreground=MUTED, font=(FONT, 8))
        st.configure("Status.TLabel", background=PANEL, foreground=SUB, font=(FONT, 8))
        st.configure("Count.TLabel", background=PANEL, foreground=ACCENT,
                     font=(FONT, 10, "bold"))
        st.configure("BarStatus.TLabel", background=BAR, foreground=SUB, font=(FONT, 8))
        # 网格区空状态提示（底为内容区 BG，故不随 PANEL）
        st.configure("Empty.TLabel", background=BG, foreground=SUB, font=(FONT, 10))

    @staticmethod
    def _style_buttons(st: ttk.Style) -> None:
        """按钮：统一高度，压掉 clam 的立体边框；含主按钮与分段按钮。"""
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
        ui_style.segmented_buttons(
            st, base_bg=BTN, base_fg=SUB, hover_bg="#383842", hover_fg=FG,
            accent=ACCENT, accent_hover=ACCENT_HOVER, padding=(14, 5))

    def _style_inputs(self, st: ttk.Style) -> None:
        """输入框与下拉框：显式压掉 clam 的浅色描边。"""
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

    @staticmethod
    def _style_checkbuttons(st: ttk.Style) -> None:
        """复选框。

        关键：clam 的 Checkbutton.indicator 用的是
        upperbordercolor / lowerbordercolor（不是 bordercolor/lightcolor/darkcolor）
        ——之前只设 bordercolor 完全没生效，浅色斜面边框因此留了下来。
        """
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

    @staticmethod
    def _style_progressbars(st: ttk.Style) -> None:
        """进度条：细轨，轨道比背景更暗以便区分。"""
        st.configure("Slim.Horizontal.TProgressbar", background=ACCENT,
                     troughcolor="#14141a", borderwidth=0, thickness=6,
                     bordercolor="#14141a", lightcolor=ACCENT, darkcolor=ACCENT)
        st.map("Slim.Horizontal.TProgressbar", background=[("disabled", "#2b2b33")])

    @staticmethod
    def _style_scrollbars(st: ttk.Style) -> None:
        """滚动条：轨道独立底色 + 深色滑块，压掉 clam 的亮箭头。"""
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

    # ---------- i18n（差异部分；通用机制见 ui_lang.LangMixin）----------
    def _window_title(self) -> str:
        return t("app.cache_title")

    def _idle_status(self) -> str:
        return t("cache.status.default", n=self.thumbs_ready)

    def _retranslate_dynamic(self) -> None:
        """重算缓存挑选器特有的动态文案。

        通用部分（静态登记、状态栏重放、标题、语言按钮）由 LangMixin.retranslate
        统一处理，这里只补：排序/类型/档位下拉、Canvas 角标、计数/概览。
        """
        # 排序下拉：先写 values 再回显（readonly 下先 set 会被复位）
        self.cb_sort.configure(values=[t(f"sort.{k}") for k in SORT_KEYS])
        self.cb_sort.set(t(f"sort.{self.v_sort.get()}"))
        # 输出类型 / 性能档位下拉：同样先写 values 再回显（档位文案含线程数）
        self.cb_kind.configure(values=[t(f"kind.{k}") for k in KIND_VALUES])
        self.cb_kind.set(t(f"kind.{self.v_kind.get()}"))
        self.cb_gear.configure(values=self._gear_labels())
        self.cb_gear.set(self._gear_label(self.v_gear.get()))
        # 网格里的视频角标是 Canvas 图元，文案只在新建时设过，需显式重设
        for cell in self.cell_pool:
            if cell.get("tag_txt") is not None:
                self._canvas_cfg(cell, "tag_txt", text=t("cell.video_tag"))
        self.update_count()                # 计数 + 主按钮 + 顶栏概览

    def _build_header(self) -> None:
        # padding 左缘与设置区/底栏标签对齐（同为 16）
        bar = ttk.Frame(self.root, style="Panel.TFrame", padding=(16, 8, 16, 4))
        bar.pack(fill="x", side="top")
        # 语言切换放最右（带「语言」标签，与 API 模式一致），其左是视图概览
        self._build_lang_switch(bar, with_label=True)
        self.l_header_stat = ttk.Label(bar, text="", style="Status.TLabel")
        self.l_header_stat.pack(side="right")
        self._T(ttk.Label(bar, style="Title.TLabel"), "app.cache_title").pack(side="left")
        self._divider()

    # ---------- 数据源设置 ----------
    def _build_setup(self) -> None:
        wrap = ttk.Frame(self.root, style="Panel.TFrame", padding=(16, 8, 16, 6))
        wrap.pack(fill="x", side="top")
        # 分区标题：与 API 模式的「① 登录」「② 选择聊天与筛选」同款、同编号风格
        self._T(ttk.Label(wrap, style="Section.TLabel"), "cache.setup.title").pack(
            anchor="w", pady=(0, 6))

        # 第一行：数据源 + 主操作（统一 8px 间距节奏）
        r1 = ttk.Frame(wrap, style="Panel.TFrame")
        r1.pack(fill="x")
        self._T(ttk.Label(r1, style="Field.TLabel", width=14),
                "cache.setup.tdata_label").pack(side="left")
        self.e_tdata = ttk.Entry(r1)
        self.e_tdata.pack(side="left", fill="x", expand=True, padx=(8, 8))
        self._T(ttk.Button(r1, command=self.on_browse), "common.browse").pack(side="left")
        self.b_scan = self._T(
            ttk.Button(r1, style="Accent.TButton", command=self.on_load),
            "cache.setup.scan_btn")
        self.b_scan.pack(side="left", padx=(8, 0))

        # 第二行：可选密码 + 只读提示（提示只在此处出现一次）
        r2 = ttk.Frame(wrap, style="Panel.TFrame")
        r2.pack(fill="x", pady=(8, 0))
        self._T(ttk.Label(r2, style="Field.TLabel", width=14),
                "cache.setup.passcode_label").pack(side="left")
        self.e_pass = ttk.Entry(r2, width=16, show="*")
        self.e_pass.pack(side="left", padx=(8, 8))
        self.b_reload = self._T(ttk.Button(r2, command=self.on_reload),
                                "cache.setup.rescan_btn")
        self.b_reload.pack(side="left")
        self.b_clean = self._T(ttk.Button(r2, command=self.on_clean_cache),
                               "cache.setup.clean_btn")
        self.b_clean.pack(side="left", padx=(8, 0))
        self._T(ttk.Label(r2, style="Hint.TLabel"), "cache.setup.hint").pack(
            side="left", padx=(12, 0))

        # 第三行：输出类型（**扫描格式**）——9 个勾选框，默认全勾（与上游一致）。
        # 未勾选的格式在扫描时被早停跳过，不参与解密/缩略图，也不会出现在网格里。
        r3 = ttk.Frame(wrap, style="Panel.TFrame")
        r3.pack(fill="x", pady=(8, 0))
        self._T(ttk.Label(r3, style="Field.TLabel", width=14),
                "cache.setup.formats_label").pack(side="left", anchor="n")
        # 「全选」框：三态显示（全选 "1" / 半选 "" / 全不选 "0"），随各格式勾选实时同步。
        # ttk::checkbutton 在 Tk 8.6 不支持三态，故这一个用经典 tk.Checkbutton
        # （配色与相邻的 ttk 复选框对齐，视觉上保持一致）。
        self.v_all = tk.StringVar(value="1")
        self.cb_all = self._T(
            tk.Checkbutton(r3, variable=self.v_all, onvalue="1", offvalue="0",
                           tristatevalue="", command=self.on_toggle_all,
                           bg=PANEL, fg=FG, activebackground=PANEL,
                           activeforeground=FG, selectcolor=FIELD,
                           font=(FONT, 9), highlightthickness=0, bd=0,
                           relief="flat"),
            "common.select_all")
        self.cb_all.pack(side="left", anchor="n", padx=(0, 14))
        grid = ttk.Frame(r3, style="Panel.TFrame")
        grid.pack(side="left")
        self.v_formats: dict[str, tk.BooleanVar] = {}
        for i, ext in enumerate(FORMATS):
            var = tk.BooleanVar(value=True)
            ttk.Checkbutton(grid, text=ext.lstrip("."), variable=var,
                            style="TCheckbutton").grid(
                row=i // 5, column=i % 5, sticky="w", padx=(0, 12), pady=1)
            self.v_formats[ext] = var
            var.trace_add("write", self._sync_all_state)   # 单个变更 → 同步全选框
        self._divider()

    # ---------- 筛选栏 ----------
    def _build_toolbar(self) -> None:
        wrap = ttk.Frame(self.root, style="Panel.TFrame", padding=(16, 6, 16, 6))
        wrap.pack(fill="x", side="top")
        # 分区标题：与 API 模式同款（Section.TLabel / ① ② 编号）
        self._T(ttk.Label(wrap, style="Section.TLabel"),
                "cache.toolbar.title").pack(anchor="w", pady=(0, 6))

        row = ttk.Frame(wrap, style="Panel.TFrame")
        row.pack(fill="x")

        # 导出内容：下拉（内部键 all/image/video，文案随语言变化）。
        # 决定**预览显示与导出**的范围（扫描范围由上面的「输出类型」复选决定）。
        self._T(ttk.Label(row, style="Sub.TLabel"),
                "cache.toolbar.export_label").pack(side="left")
        self.v_kind = tk.StringVar(value="all")
        self.cb_kind = ttk.Combobox(row, width=10, state="readonly",
                                    values=[t(f"kind.{k}") for k in KIND_VALUES])
        self.cb_kind.set(t("kind.all"))
        self.cb_kind.pack(side="left", padx=(8, 20))
        self.cb_kind.bind("<<ComboboxSelected>>", lambda e: self.on_kind_change())

        self._T(ttk.Label(row, style="Sub.TLabel"),
                "cache.toolbar.sort_label").pack(side="left")
        # 排序用**内部键**驱动逻辑，下拉里显示的是随语言变化的文案
        self.v_sort = tk.StringVar(value="date_desc")
        self.cb_sort = ttk.Combobox(
            row, width=18, state="readonly",
            values=[t(f"sort.{k}") for k in SORT_KEYS],
        )
        self.cb_sort.set(t("sort.date_desc"))
        self.cb_sort.pack(side="left", padx=(8, 20))
        self.cb_sort.bind("<<ComboboxSelected>>", lambda e: self.on_sort_change())

        # 性能档位：下拉，控制后台缩略图工作线程数（内部键 conservative/balanced/turbo）
        self._T(ttk.Label(row, style="Sub.TLabel"),
                "cache.toolbar.gear_label").pack(side="left")
        self.v_gear = tk.StringVar(value="balanced")
        self.cb_gear = ttk.Combobox(row, width=16, state="readonly",
                                    values=self._gear_labels())
        self.cb_gear.set(self._gear_label("balanced"))
        self.cb_gear.pack(side="left", padx=(8, 0))
        self.cb_gear.bind("<<ComboboxSelected>>", lambda e: self.on_gear_change())

        # 搜索靠右，与筛选区分开
        self._T(ttk.Button(row, command=self.on_clear_search),
                "cache.toolbar.clear_search").pack(side="right")
        self.e_search = ttk.Entry(row, width=22)
        self.e_search.pack(side="right", padx=(8, 8))
        self.e_search.bind("<KeyRelease>", self._on_search_key)
        self._T(ttk.Label(row, style="Sub.TLabel"),
                "cache.toolbar.search_label").pack(side="right")

    # ---------- 性能档位 ----------
    def _gear_workers(self, key: str) -> int:
        """档位内部键 → 后台工作线程数（保守 1 / 均衡 推荐 / 极速 满核）。

        低配钳制（核数未知 → 1；内存 <4GB 时极速不跑满核）见 ``ui_kit.gear_workers``，
        避免低配置设备因线程设置过高出现运行异常。
        """
        return gear_workers(key, self.hw, self.hw_rec)

    def _gear_label(self, key: str) -> str:
        """档位内部键 → 当前语言下的下拉文案（含线程数）。"""
        return t(f"gear.{key}", n=self._gear_workers(key))

    def _gear_labels(self) -> list[str]:
        """三档的下拉文案列表，顺序与 GEAR_KEYS 一致。"""
        return [self._gear_label(k) for k in GEAR_KEYS]

    def on_gear_change(self) -> None:
        """性能档位下拉：记住选择，空闲时立即按新线程数重建工作线程池。"""
        idx = self.cb_gear.current()
        if 0 <= idx < len(GEAR_KEYS):
            self.v_gear.set(GEAR_KEYS[idx])
        save_config({"gear": self.v_gear.get()})
        if not self.busy:
            self._apply_gear()

    def _apply_gear(self) -> None:
        """按当前档位重建后台工作线程池（旧的整体关停，已排队的取消）。

        关键：被取消的排队任务**不会投递结果**，其下标会永久留在
        ``thumb_pending`` 里；而 ``_thumb_for`` 见到 ``idx in thumb_pending``
        就不再重新请求——这些格子会一直停在占位图/空白。因此在关停旧池后，
        统一释放旧池占用的待载下标，并按当前可见区在新池上重新请求
        （可见格子立即重发，不可见的等滚到时再发）。
        """
        old = self.pool
        self.pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._gear_workers(self.v_gear.get()))
        if old is not None:
            try:
                old.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            # 旧池的排队任务此刻已取消、运行中的也会自行投递结果；
            # 无论哪种，其下标都由下面的 refresh_view 在当前新池上重新认领。
            if self.thumb_pending:
                self.thumb_pending.clear()
                self.refresh_view()

    def on_sort_change(self) -> None:
        """下拉选择 → 映射回内部键（文案随语言变，逻辑只看键）。"""
        idx = self.cb_sort.current()
        if 0 <= idx < len(SORT_KEYS):
            self.v_sort.set(SORT_KEYS[idx])
        elif self.v_sort.get() not in SORT_KEYS:
            self.v_sort.set("date_desc")
        self.on_filter_change()

    def on_kind_change(self) -> None:
        """导出内容下拉：切换预览显示与导出的范围（数据已扫过时即时生效）。

        扫描范围由上方「输出类型」的格式勾选决定，与本下拉解耦。
        """
        idx = self.cb_kind.current()
        if 0 <= idx < len(KIND_VALUES):
            self.v_kind.set(KIND_VALUES[idx])
        self.on_filter_change()

    # ---------- 输出类型（扫描格式）----------
    def _sync_all_state(self, *_args) -> None:
        """按各格式的勾选情况，把「全选」框同步为 全选 / 半选 / 全不选。

        由每个格式的 BooleanVar 的 write 追踪调用，因此**实时生效**，无需重启任务。
        """
        values = [var.get() for var in self.v_formats.values()]
        if all(values):
            self.v_all.set("1")
        elif any(values):
            self.v_all.set("")       # 半选（部分勾选）
        else:
            self.v_all.set("0")

    def on_toggle_all(self) -> None:
        """点「全选」：一键勾选/取消勾选所有扫描类型。

        意图按**各格式当前是否已全选**决定（不依赖控件点击后被 Tk 改写的变量值），
        因此三态只是"显示"，点击始终是干脆的切换。
        """
        target = not all(var.get() for var in self.v_formats.values())
        for var in self.v_formats.values():
            var.set(target)
        self._sync_all_state()

    # ---------- 底部操作条 ----------
    def _build_bottombar(self) -> None:
        wrap = ttk.Frame(self.root, style="Panel.TFrame", padding=(16, 8, 16, 8))
        wrap.pack(fill="x", side="bottom")

        # 第一行：保存目标（与 API 模式操作条第 1 行同一版式）
        r1 = ttk.Frame(wrap, style="Panel.TFrame")
        r1.pack(fill="x")
        self._T(ttk.Label(r1, style="Field.TLabel", width=14),
                "cache.bottom.out_label").pack(side="left")
        self.e_out = ttk.Entry(r1)
        self.e_out.pack(side="left", fill="x", expand=True, padx=(8, 8))
        self._T(ttk.Button(r1, command=self.on_pick_out), "common.browse").pack(
            side="left", padx=(0, 8))
        # 打开目录：用系统文件管理器打开「保存到」目录（失败时回退复制路径）
        self.b_open = self._T(ttk.Button(r1, command=self.on_open_dir),
                              "common.open_dir")
        self.b_open.pack(side="left")

        # 第二行：计数 + 选择操作（左）｜按日期分文件夹 + 主按钮 + 取消（右）
        r2 = ttk.Frame(wrap, style="Panel.TFrame")
        r2.pack(fill="x", pady=(8, 0))
        # l_count / b_save 的文本由 update_count() 拥有，故用 t() 设初值但不登记
        self.l_count = ttk.Label(r2, text=t("common.count_none"),
                                 style="Count.TLabel", width=26)
        self.l_count.pack(side="left", padx=(0, 12))
        self._T(ttk.Button(r2, command=self.on_select_all), "common.select_all").pack(
            side="left", padx=(0, 8))
        self._T(ttk.Button(r2, command=self.on_invert), "common.invert").pack(
            side="left", padx=(0, 8))
        self._T(ttk.Button(r2, command=self.on_clear_sel), "common.clear_sel").pack(side="left")
        # 右侧：取消 / 主按钮 / 本模式特有的「按日期分文件夹」（自右向左 pack）
        self.b_cancel = self._T(ttk.Button(r2, command=self.on_cancel, state="disabled"),
                                "common.cancel")
        self.b_cancel.pack(side="right")
        self.b_save = ttk.Button(r2, text=t("cache.bottom.save_none"), style="Accent.TButton",
                                 command=self.on_save)
        self.b_save.pack(side="right", padx=(0, 8))
        self.v_bydate = tk.BooleanVar(value=False)
        self._T(ttk.Checkbutton(r2, variable=self.v_bydate),
                "cache.bottom.bydate").pack(side="right", padx=(0, 16))

    def _build_grid(self) -> None:
        wrap = ttk.Frame(self.root, padding=(16, 6, 8, 6))
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

        # 空状态：无内容时在网格区居中显示的内联提示（与 API 模式同一风格）
        self.l_empty = self._T(
            ttk.Label(self.canvas, style="Empty.TLabel", justify="center"),
            "cache.empty_hint")
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

    def _build_statusbar(self) -> None:
        # 状态栏底色与 API 模式一致（BAR）；状态在右、进度在左
        wrap = ttk.Frame(self.root, style="Bar.TFrame", padding=(16, 5, 16, 5))
        wrap.pack(fill="x", side="bottom")
        self.l_status = ttk.Label(wrap, text=t("common.ready"), style="BarStatus.TLabel")
        self.l_status.pack(side="right")
        # 进度条只在任务进行时出现，常态不占位（避免"像个空输入框"）
        self.pb = ttk.Progressbar(wrap, mode="determinate", maximum=100,
                                  style="Slim.Horizontal.TProgressbar")
        self._pb_visible = False

    def _show_progress(self, value: float | None = None) -> None:
        """显示进度条（并可选设值）。"""
        if not self._pb_visible:
            self.pb.pack(side="left", fill="x", expand=True, padx=(0, 10))
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

    @staticmethod
    def _default_tdata() -> str:
        """探测 tdata 默认位置（跨平台，不硬编码盘符）。

        顺序：Telegram 官方默认路径 → 便携版常见位置（程序旁 / 各盘根）。
        找不到就返回空字符串，由用户手动选择。

        不依赖实例状态，故为静态方法——这样无 GUI 也能直接测试它。
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

    @staticmethod
    def _migrate_sort(value: str) -> str:
        """旧配置里存的是中文排序文案；迁移为内部键（已是键则原样返回）。"""
        if value in SORT_KEYS:
            return value
        return _LEGACY_SORT.get(value, "date_desc")

    def _load_config(self) -> None:
        cfg = load_config()
        tdata = cfg.get("tdata") or ""
        if not tdata:
            tdata = self._default_tdata()
        self.e_tdata.delete(0, "end")
        self.e_tdata.insert(0, tdata)

        out = cfg.get("out_dir") or str(EXPORT_DIR)
        self.e_out.delete(0, "end")
        self.e_out.insert(0, out)
        if cfg.get("kind") in KIND_VALUES:
            self.v_kind.set(cfg["kind"])
        if cfg.get("sort"):
            self.v_sort.set(self._migrate_sort(str(cfg["sort"])))
        # 输出类型（扫描格式）：按保存的列表回填；缺失/为空则维持默认全勾
        saved_fmts = cfg.get("formats")
        if isinstance(saved_fmts, (list, tuple)):
            keep = [e for e in FORMATS if e in saved_fmts]
            if keep:
                for ext, var in self.v_formats.items():
                    var.set(ext in keep)
        # 性能档位：**启动时按硬件检测结果自动匹配**——无有效保存值时用推荐档
        # （均衡=推荐线程数）；保存值仍经 ui_kit.gear_workers 的低配钳制，
        # 避免低配设备因线程设置过高出现运行异常。
        if cfg.get("gear") in GEAR_KEYS:
            self.v_gear.set(cfg["gear"])
        # 下拉回显当前键对应的文案（语言切换时由 retranslate 重设）
        self.cb_kind.set(t(f"kind.{self.v_kind.get()}"))
        self.cb_sort.set(t(f"sort.{self.v_sort.get()}"))
        self.cb_gear.set(self._gear_label(self.v_gear.get()))
        self._apply_gear()           # 先建好工作线程池（此前为 None），供缩略图加载用
        if cfg.get("by_date") is not None:
            self.v_bydate.set(bool(cfg["by_date"]))

    # ------------------------- 读取 -------------------------
    def on_browse(self) -> None:
        """选择 tdata 目录并回填到输入框。"""
        chosen = filedialog.askdirectory(initialdir=self.e_tdata.get() or str(HERE))
        if chosen:
            self.e_tdata.delete(0, "end")
            self.e_tdata.insert(0, chosen)

    def on_load(self) -> None:
        """开始扫描：**用户显式触发**，按勾选的「输出类型」限定格式后后台解密/索引。

        不再在打开时自动读取——全量解密与出缩略图是最重的一步，交由用户点击触发；
        只处理已勾选的格式（未勾选的格式嗅探后早停，不付出解密/缩略图开销）。
        """
        if self.busy:
            return
        tdata = Path(self.e_tdata.get().strip())
        if not tdata.is_dir():
            messagebox.showwarning(_app_title(), t("cache.dialog.no_tdata"))
            return
        selected = tuple(ext for ext, var in self.v_formats.items() if var.get())
        if not selected:
            messagebox.showwarning(_app_title(), t("cache.dialog.need_format"))
            return
        self.busy = True
        self.cancel.clear()
        self.set_busy(True)
        self._reset_grid_data()
        self._apply_gear()           # 档位若在忙碌时改过，这里生效
        passcode = self.e_pass.get()
        exts = None if len(selected) == len(FORMATS) else selected   # 全勾=不过滤
        workers = self._gear_workers(self.v_gear.get())   # 性能档位真正作用于扫描
        t0 = time.time()

        def work():
            try:
                entries, stats = load_cache(
                    tdata, passcode,
                    progress_cb=lambda i, tot: self.ui_q.put(("load_progress", i, tot)),
                    cancel_event=self.cancel,
                    exts=exts,
                    workers=workers,
                )
                self.ui_q.put(("load_done", tdata, entries, stats, passcode, time.time() - t0))
            except cs.PasscodeRequiredError as exc:
                # 只传语言键，由主线程渲染，避免工作线程在切换语言瞬间取到半新状态
                self.ui_q.put(("load_error", "cache.dialog.need_passcode_title", str(exc)))
            except cs.CacheError as exc:
                self.ui_q.put(("load_error", "cache.dialog.load_failed_title", str(exc)))
            except Exception as exc:  # pragma: no cover
                self.ui_q.put(("load_error", "cache.dialog.load_failed_title",
                               f"{exc.__class__.__name__}: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    def on_reload(self) -> None:
        """丢弃索引并重新扫描。

        注意：必须先判 busy 再删索引——否则读取过程中点「重新扫描」会把索引
        删掉却因 on_load 提前返回而不重扫，索引凭空丢失。
        """
        if self.busy:
            return
        try:
            INDEX_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        self.on_load()

    def on_clean_cache(self) -> None:
        """清理本地缓存（缩略图 / 索引 / 抽帧临时文件），回收磁盘空间。

        只删这些**会自动重建**的产物，绝不碰 tdata、也不碰你保存出来的媒体。
        清理后当前视图的缩略图会按需重新生成，所以只作废内存缓存、不清空已读到的数据。
        """
        if self.busy:
            return
        size = cache_bytes()
        if size <= 0:
            messagebox.showinfo(_app_title(), t("cache.dialog.clean_nothing"))
            return
        if not messagebox.askyesno(
            _app_title(), t("cache.dialog.confirm_clean", size=human_size(size))
        ):
            return
        freed = clean_cache()
        # 顺序很关键：先隐藏图元（解除 Canvas 对 Tk 图像的引用）再丢弃内存缩略图，
        # 反了会留下悬空引用，之后 itemconfigure 会持续报 image doesn't exist。
        for cell in self.cell_pool:
            self._hide_cell(cell)
        self.cells_by_slot.clear()
        self.thumb_mem.clear()
        self.thumb_pending.clear()
        self.thumb_failed.clear()
        self.thumbs_ready = 0
        self._text_w_cache.clear()
        self.refresh_view()          # 可见区按需重新生成缩略图
        self.set_status("cache.status.cleaned", size=human_size(freed))

    def _reset_grid_data(self) -> None:
        """清空网格与缩略图内存（不删磁盘缓存）。"""
        self.all_items = []
        self.items = []
        self.selected.clear()
        self.thumb_mem.clear()
        self.thumb_pending.clear()
        self.thumb_failed.clear()
        self._clear_photo_queue()        # 在途结果属于旧列表，直接丢弃
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
        """筛选/排序变化后重建视图：清空选中与缩略图缓存并回到顶部。"""
        self.apply_filter_sort()
        self.selected.clear()
        self.thumb_mem.clear()
        self.thumb_pending.clear()
        self.thumb_failed.clear()
        self._clear_photo_queue()        # 列表已换，在途结果按下标回传不再有效
        self.hover_slot = None
        self._last_scrollregion = None
        self.canvas.yview_moveto(0)
        self.refresh_view()
        self.update_count()

    def apply_filter_sort(self) -> None:
        """按类型、搜索词、排序**内部键**生成当前视图列表 self.items。

        排序只看 SORT_KEYS 里的键，与下拉框显示的语言文案无关。
        """
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

        sort = self.v_sort.get()
        if sort == "date_desc":
            items.sort(key=lambda x: x.mtime, reverse=True)
        elif sort == "date_asc":
            items.sort(key=lambda x: x.mtime)
        elif sort == "size_desc":
            items.sort(key=lambda x: x.size, reverse=True)
        elif sort == "size_asc":
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
        self._update_empty_hint()
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
            0, 0, anchor="center", text=t("cell.video_tag"), fill="#e8e8ec",
            font=(FONT, 8), state="hidden")
        # 选中徽标：内容区底色描边圈 + 强调色圆底 + 对勾。
        # 外圈是必要的——缩略图可能是亮色/同色系，纯强调色圆底在图上会糊掉。
        cell["badge_ring"] = self.canvas.create_oval(
            0, 0, 0, 0, fill=BG, outline="", state="hidden")
        cell["badge"] = self.canvas.create_oval(
            0, 0, 0, 0, fill=ACCENT, outline="", state="hidden")
        cell["badge_txt"] = self.canvas.create_text(
            0, 0, anchor="center", text="✓", fill="#ffffff",
            font=(FONT, 11, "bold"), state="hidden")
        cell["info"] = self.canvas.create_text(
            0, 0, anchor="nw", text="", fill=SUB, font=(FONT, 9))
        cell["photo"] = None

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
        for key in ("rect", "img", "tag", "tag_txt", "badge_ring", "badge",
                    "badge_txt", "info"):
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
        for key in ("rect", "tag", "tag_txt", "badge_ring", "badge", "badge_txt", "info"):
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
                *rounded_pts(x, y, x + CARD_W, y + CARD_H, CORNER))

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
                                    *rounded_pts(tx, ty, tx + tag_w,
                                                 ty + tag_h, 4))
                self._canvas_cfg(cell, "tag_txt", state="normal")
                self._canvas_coords(cell, "tag_txt",
                                    tx + tag_w // 2, ty + tag_h // 2)
            else:
                self._canvas_cfg(cell, "tag", state="hidden")
                self._canvas_cfg(cell, "tag_txt", state="hidden")

            # 选中徽标：仅选中时出现（右上角圆底对勾 + 一圈底色描边圈）
            if is_sel:
                r, ring = 11, 3
                cx = x + CARD_W - r - 7
                cy = y + r + 7
                self._canvas_cfg(cell, "badge_ring", state="normal")
                self._canvas_coords(cell, "badge_ring",
                                    cx - r - ring, cy - r - ring,
                                    cx + r + ring, cy + r + ring)
                self._canvas_cfg(cell, "badge", state="normal")
                self._canvas_coords(cell, "badge", cx - r, cy - r, cx + r, cy + r)
                self._canvas_cfg(cell, "badge_txt", state="normal")
                self._canvas_coords(cell, "badge_txt", cx, cy)
            else:
                for key in ("badge_ring", "badge", "badge_txt"):
                    self._canvas_cfg(cell, key, state="hidden")

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
    def _thumb_for(self, idx: int, it: MediaItem) -> ImageTk.PhotoImage | None:
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
            return self._video_placeholder() if it.kind == "video" else None

        if idx not in self.thumb_pending:
            self._request_thumb(idx, it)
        # 生成中：视频先给占位图（有内容比空着好），图片留空
        return self._video_placeholder() if it.kind == "video" else None

    def _video_placeholder(self) -> ImageTk.PhotoImage | None:
        """视频的灰色占位图（生成中或提帧失败时使用），只生成一次并复用。"""
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

    def _request_thumb(self, idx: int, it: MediaItem) -> None:
        """后台线程：优先读磁盘缩略图缓存；缺失则解密媒体并生成。

        视频需要整体解密后交给 ffmpeg 抽帧（缓存分片可能不完整，
        抽帧失败会回退到占位图）。
        """
        self.thumb_pending.add(idx)
        tp = cs.entry_thumb_path(THUMB_DIR, it.entry.thumb_key) if it.entry.thumb_key else None
        reader = self.reader
        kind = it.kind
        cache_path = it.entry.cache_path
        # 身份键：结果回传时用它校验。下标只代表"条目在**当时**列表里的位置"，
        # 列表一旦被筛选/排序/重扫换掉，同一个下标可能已经是另一条媒体。
        rel = str(it.entry.rel)

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
                self.photo_queue.put((idx, rel, img, kind))
            except Exception:
                self.photo_queue.put((idx, rel, None, kind))

        self.pool.submit(work)

    def _clear_photo_queue(self) -> None:
        """丢弃在途的缩略图结果。

        列表被筛选/排序/重扫换掉之后，旧请求的结果已无意义（而且按下标回传，
        贴上去就是"串图"）。早丢弃可以少做几次 PIL→PhotoImage 的转换，
        也避免在途的图对象占着内存。
        """
        while True:
            try:
                self.photo_queue.get_nowait()
            except queue.Empty:
                return

    def _drain_photos(self) -> bool:
        """主线程把后台读到的 PIL 图转成 PhotoImage（每周期限量）。

        结果按下标回传，因此**必须用身份键复核**：下标相同不等于同一条媒体
        （筛选、排序、重扫都会换掉 `self.items`）。不一致就整条丢弃——既不写
        `thumb_mem`（否则错的图会被长期记住），也不动 `thumb_pending`（那是
        新请求的pending）。
        """
        changed = False
        for _ in range(PHOTO_BATCH_PER_TICK):
            try:
                queued = self.photo_queue.get_nowait()
            except queue.Empty:
                break
            idx, rel, img, kind = queued
            if idx >= len(self.items) or str(self.items[idx].entry.rel) != rel:
                continue
            self.thumb_pending.discard(idx)
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
        # 落在卡片之间的间隙里不算命中（含卡片右下边缘那条像素）
        if (gx % CELL_W) >= CARD_W or (gy % CELL_H) >= CARD_H:
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
        self.export_item(self.items[idx])

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
                "cache.status.hover",
                name=it.name, size=human_size(it.size),
                time=f"{datetime.fromtimestamp(it.mtime):%Y-%m-%d %H:%M}",
                label=it.entry.label,
            )
        else:
            self.set_status("cache.status.default", n=self.thumbs_ready)

    def _on_leave(self, event) -> None:
        if self.hover_slot is not None:
            prev = self.hover_slot
            self.hover_slot = None
            cell = self.cells_by_slot.get(prev)
            if cell is not None:
                self._place_cell(cell, prev, prev % self.columns, prev // self.columns)
        self.set_status("cache.status.default", n=self.thumbs_ready)

    def export_item(self, it: MediaItem) -> None:
        """双击：询问确认后把这一条**导出**到「保存到」目录。

        刻意不调用系统默认程序打开文件：实测某些机器的 shell 环境会让
        ShellExecute 崩溃或长时间卡住（见 CHANGELOG）。导出完成后只在状态栏
        给出完整路径，自行到该目录查看即可。

        同一时刻只允许一个导出在跑（连点会造成重复解密与并发写盘）。
        """
        if self._exporting:
            return
        out = self.e_out.get().strip()
        if not out:
            messagebox.showwarning(_app_title(), t("cache.dialog.no_out_dir"))
            return
        if not messagebox.askyesno(
            _app_title(), t("cache.dialog.confirm_export", out=out, name=it.name)
        ):
            return
        self._exporting = True
        tdata = Path(self.e_tdata.get().strip())
        passcode = self.e_pass.get()
        self.set_status("cache.status.exporting", name=it.name)

        def work():
            dest = None
            err = None
            try:
                dest = export_one(it, Path(out), tdata, passcode, reader=self.reader)
            except Exception as exc:
                err = f"{exc.__class__.__name__}: {exc}"
            finally:
                self._exporting = False

            self.ui_q.put(("export", str(dest) if dest else None, err))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------- 选择 -------------------------
    def on_select_all(self) -> None:
        """选中当前筛选结果里的全部条目（不改变筛选条件）。"""
        for it in self.items:
            self.selected.add(str(it.entry.rel))
        self.update_count()
        self.refresh_view()

    def on_invert(self) -> None:
        """反选：当前筛选结果里已选的取消、未选的选中。"""
        for it in self.items:
            rel = str(it.entry.rel)
            if rel in self.selected:
                self.selected.discard(rel)
            else:
                self.selected.add(rel)
        self.update_count()
        self.refresh_view()

    def on_clear_sel(self) -> None:
        """清空全部选中项。"""
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
            self.l_count.configure(
                text=t("common.count_selected", n=n, size=human_size(total)))
            self.b_save.configure(text=t("cache.bottom.save_n", n=n))
        else:
            self.l_count.configure(text=t("common.count_none"))
            self.b_save.configure(text=t("cache.bottom.save_none"))
        self._update_header()

    def _update_header(self) -> None:
        """顶栏右侧显示当前视图概览。"""
        if not self.all_items:
            self.l_header_stat.configure(text="")
            return
        self.l_header_stat.configure(
            text=t("cache.status.header", kind=t(f"kind.{self.v_kind.get()}"),
                   shown=len(self.items), total=len(self.all_items))
        )

    # ------------------------- 保存 -------------------------
    def on_pick_out(self) -> None:
        """选择保存目标目录并回填到输入框。"""
        chosen = filedialog.askdirectory(initialdir=self.e_out.get() or str(HERE))
        if chosen:
            self.e_out.delete(0, "end")
            self.e_out.insert(0, chosen)

    def on_open_dir(self) -> None:
        """用系统文件管理器打开「保存到」目录（移植自上游的打开目录逻辑）。

        路径不存在时提示先选目录；启动外部程序失败（本机可能对 shell open 受限）
        时不弹错，回退为把**绝对路径写入剪贴板**并在状态栏提示，保证不崩、可用。
        """
        path = Path(self.e_out.get().strip() or str(EXPORT_DIR))
        if not path.is_dir():
            messagebox.showwarning(_app_title(), t("cache.dialog.no_out_dir"))
            return
        if ui_kit.open_folder(path):
            self.set_status("cache.status.opened_dir", path=path)
        else:
            self._copy_path_fallback(path)

    def _copy_path_fallback(self, path: Path) -> None:
        """打开目录失败时的兜底：把绝对路径写入剪贴板并提示。"""
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(str(path))
        except tk.TclError:
            pass
        self.set_status("cache.status.path_copied", path=path)

    def on_cancel(self) -> None:
        """请求取消当前的扫描 / 保存（复用已有 ``self.cancel`` 事件）。

        取消机制（``load_cache`` / ``save_items`` 的 ``cancel_event``）一直存在，
        此前只是没有界面入口；这里补上，与 API 模式的「取消」按钮对齐。
        """
        self.cancel.set()
        self.set_status("common.cancel_req")

    def on_save(self) -> None:
        """保存选中项：二次确认后在后台线程解密写出到输出目录。"""
        if self.busy:
            return
        if not self.selected:
            messagebox.showinfo(_app_title(), t("cache.dialog.nothing_selected"))
            return
        out = self.e_out.get().strip()
        if not out:
            messagebox.showwarning(_app_title(), t("cache.dialog.no_out_dir"))
            return
        tdata = Path(self.e_tdata.get().strip())
        by_rel = {str(it.entry.rel): it for it in self.all_items}
        items = [by_rel[r] for r in self.selected if r in by_rel]
        if not items:
            return
        total_size = sum(it.size for it in items)
        if not messagebox.askyesno(
            _app_title(),
            t("cache.dialog.confirm_save", n=len(items), size=human_size(total_size),
              out=out,
              by_date=t("cache.dialog.confirm_save_bydate") if self.v_bydate.get() else ""),
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
                reader = self.reader or cs.get_reader(tdata, passcode)
                ok, fail = save_items(
                    items, Path(out), tdata, passcode, by_date=by_date,
                    progress_cb=lambda d, tot, n: self.ui_q.put(("save_progress", d, tot, n)),
                    cancel_event=self.cancel, reader=reader,
                )
                self.ui_q.put(("save_done", Path(out), ok, fail))
            except Exception as exc:  # pragma: no cover
                # 只报错即可：load_error 分支已复位 busy，再补一条 save_done
                # 会导致连弹两个对话框（且后者用成功文案）。
                self.ui_q.put(("load_error", "cache.dialog.save_failed_title",
                               f"{exc.__class__.__name__}: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------- 主循环 -------------------------
    def _pump(self) -> None:
        """定期排空消息队列并推进缩略图加载。

        单条消息处理失败绝不能中断轮询本身——否则 after 链断开，界面会永久假死、
        busy 卡住、队列持续堆积。
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
        try:
            if self._drain_photos():
                self.refresh_view()
        except Exception:
            traceback.print_exc()
        self._pump_job = self.root.after(PUMP_MS, self._pump)

    def _handle(self, msg: tuple) -> None:
        """按消息类型分发到 ``self._msg_<类型>``；未定义的类型直接忽略。

        消息由后台线程经 ``ui_q`` 投递，统一在这里（主线程）消费。
        """
        handler = getattr(self, f"_msg_{msg[0]}", None)
        if handler is not None:
            handler(msg)

    def _msg_load_progress(self, msg: tuple) -> None:
        """索引进度：更新进度条与状态栏（文案按当前语言渲染，随语言切换重放）。"""
        _, i, total = msg
        self._show_progress((i / total * 100) if total else 0)
        self.set_status("cs.progress_parse", i=i, total=total)

    def _msg_load_done(self, msg: tuple) -> None:
        """缓存读取完成：装载条目、刷新视图与计数、记住 tdata 路径。"""
        _, tdata, entries, stats, passcode, elapsed = msg
        self.tdata = tdata
        self.busy = False
        self.set_busy(False)
        self._hide_progress()
        self.all_items = [MediaItem(entry=e) for e in entries]
        self.reader = cs.get_reader(tdata, passcode)
        self.apply_filter_sort()
        self.refresh_view()
        self.update_count()          # 同步顶栏概览与计数
        reused = stats["reused"]
        self.set_status(
            "cache.status.load_done",
            ok=stats["ok"],
            reused=t("cache.status.reused", n=reused) if reused else "",
            skipped=stats["skipped"], failed=stats["failed"],
            elapsed=f"{elapsed:.1f}",
        )
        save_config({"tdata": str(tdata)})
        if not entries:
            # 不再弹窗：网格区已有内联空态提示，这里只补"怎样让缓存里有内容"的指引
            self.set_status("cache.status.no_media")

    def _msg_load_error(self, msg: tuple) -> None:
        """读取/保存失败：弹错误框并复位忙碌态（标题走语言键）。"""
        _, title_key, detail = msg
        self.busy = False
        self.set_busy(False)
        self._hide_progress()
        messagebox.showerror(_app_title(), f"{t(title_key)}\n\n{detail}")
        self.set_status_error(title_key, detail)

    def _msg_export(self, msg: tuple) -> None:
        """单条导出结果：成功只在状态栏给出完整路径，失败弹错误框。"""
        _, path, err = msg
        if err:
            messagebox.showerror(_app_title(), t("cache.dialog.export_failed", err=err))
            self.set_status("cache.status.export_failed")
        else:
            self.set_status("cache.status.export_done", path=path)

    def _msg_save_progress(self, msg: tuple) -> None:
        """保存进度：更新进度条与状态栏（状态栏显示当前文件名）。"""
        _, done, total, name = msg
        self._show_progress((done / total * 100) if total else 0)
        self.set_status("cache.status.saving", done=done, total=total, name=name)

    def _msg_save_done(self, msg: tuple) -> None:
        """保存完成：提示结果并记住本次保存选项。"""
        _, out, ok, fail = msg
        self.busy = False
        self.set_busy(False)
        self._hide_progress()
        # 有失败时提示去看日志文件（逐条原因写在里面）
        key = "cache.status.save_done_failed" if fail else "cache.status.save_done"
        self.set_status(key, ok=ok, fail=fail)
        save_config({"out_dir": str(out), "kind": self.v_kind.get(),
                     "sort": self.v_sort.get(), "by_date": bool(self.v_bydate.get()),
                     "formats": [e for e, v in self.v_formats.items() if v.get()]})

    # ------------------------- 状态栏 -------------------------
    def set_status_error(self, key: str, detail: str) -> None:
        """错误状态：标题也是语言键，切换语言时一并重算。

        ``set_status`` / ``_render_status`` 由 LangMixin 提供。
        """
        self._status_fn = (lambda k=key, d=detail: t("cache.status.err", title=t(k), detail=d))
        self.l_status.configure(text=self._status_fn())

    def set_busy(self, busy: bool) -> None:
        """切换忙碌态：禁用会引发重入的几个按钮。"""
        state = "disabled" if busy else "normal"
        self.b_save.configure(state=state)
        self.b_scan.configure(state=state)
        self.b_reload.configure(state=state)
        self.b_clean.configure(state=state)
        # 取消按钮与上面相反：仅在任务进行时可用
        self.b_cancel.configure(state="normal" if busy else "disabled")

    def _on_close(self) -> None:
        """关闭窗口：停掉后台任务与所有 after 定时任务，避免销毁后回调报错。"""
        self.cancel.set()
        self._disable_lang_support()
        for job in (self._pump_job, self._refresh_job, self._search_job):
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
        if self.pool is not None:
            try:
                self.pool.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        self.root.destroy()


def main() -> None:
    """入口：建窗口并进入 Tk 主循环。"""
    root = tk.Tk()
    firstrun.ensure_choice(root)          # 首次启动先确定数据目录
    ui_kit.enable_file_log(app_paths.data_dir() / "logs" / "cache_picker.log")
    CacheApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
