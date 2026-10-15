"""直接读取 Telegram Desktop 的 tdata 缓存（TDEF 加密媒体）。

不落地中间文件：按需解密 → 嗅探类型 → 生成缩略图缓存 → 记录索引。
只有用户真正挑选保存时，才把明文写到你指定的目录。

性能要点：
    - 索引阶段只解密每个文件的前 64 KiB 用于类型嗅探（视频可能几十 MB，
      不必为了知道"这是 mp4"而整体解密）；图片体积小，仍完整解密以便出图。
    - 缩略图与索引落盘缓存，重复启动直接复用。

解密实现复用 tg-scoop（GarrettFynn/tg-scoop，MIT）里经过实测的
tdata 密钥派生与 TDEF 解密代码——同一套逻辑已经从这套 tdata 里
成功解出 3019 个媒体文件，正确性有实测背书。

安全边界：**只读 tdata**，绝不修改、绝不删除其中的任何字节。
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageFile

from i18n import t
from ui_kit import LOG_NAME, THUMB, unique_path

# 缓存里可能存在"只缓存了一部分"的媒体，允许截断解码以尽量出图
ImageFile.LOAD_TRUNCATED_IMAGES = True

# 日志：三个入口共用同一个 logger（输出落到数据目录的 logs/，见 ui_kit.enable_file_log）。
# 界面只报"成功 N / 失败 M"，逐条失败的原因必须留痕，否则用户无从排查。
_log = logging.getLogger(LOG_NAME)

# THUMB（缩略图边长上限）与卡片几何同源，见 ui_kit——两个模式共用一套数值。
HEAD_BYTES = 64 * 1024          # 索引阶段嗅探时解密的前缀长度
MAX_DECRYPT_BYTES = 512 * 1024 * 1024

# 视频首帧提取：把解密后的明文写成临时文件，交给 ffmpeg 抽一帧。
# ffmpeg 的查找顺序见 ffmpeg_exe()：imageio-ffmpeg（开发）→ 自带的 ffmpeg
# （随工具分发的裁剪版）→ 系统 PATH（兜底）。自包含发布时自带二进制，用户无需安装。
VIDEO_THUMB_TIMEOUT = 30        # 单个视频抽帧超时（秒）
VIDEO_SEEK = "0.1"              # 抽帧位置：略微避开可能全黑的开头

_FFMPEG_CACHE: str | None = None


# ---------------------------------------------------------------------------
# 通用小工具
#
# human_size / unique_path 已收敛到 ui_kit（两个入口共用，避免实现漂移），
# 本模块不再重复定义。
# ---------------------------------------------------------------------------


# Windows：GUI 通过 pythonw.exe（无控制台）启动时，每次调用 ffmpeg.exe
# 这类控制台程序，系统都会为它**新建一个控制台窗口**并闪一下。缩略图是
# 边滚动边生成的，于是表现为"加载时不断弹窗"。必须显式要求不创建窗口。
def _hidden_subprocess_kwargs() -> dict:
    """返回让子进程不弹控制台窗口的 subprocess 参数（仅 Windows 生效）。

    用 ``sys.platform`` 做分支（而不是把 ``hasattr`` 的结果存成模块常量）：
    ``STARTUPINFO`` / ``CREATE_NO_WINDOW`` 这些属性在 typeshed 里只在 win32
    下定义，写成"运行时才判断"的形式会让 mypy 在 Linux/macOS 上按当前平台
    报 ``Module has no attribute``（CI 的 Linux 一路因此一直红）。写成平台
    分支后，非 Windows 平台整个分支都会被 mypy 跳过——与 ``ui_kit.open_folder``
    里 ``os.startfile`` 的处理方式一致。

    每次调用都新建 STARTUPINFO：抽帧由多线程并发发起，
    复用同一个对象不安全（同时被多个 CreateProcess 读取）。
    """
    if sys.platform != "win32":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": si}


def _bundled_ffmpeg() -> str:
    """查找**随工具分发**的 ffmpeg（自包含发布时放在程序目录/冻结资源目录里）。

    发布形态是"独立可执行包"：ffmpeg 与程序一起分发，用户无需另装。
    在源码运行时这里通常找不到（返回空串），交由后续的 PATH 兜底。
    """
    names = ("ffmpeg.exe",) if sys.platform == "win32" else ("ffmpeg",)
    dirs: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)   # PyInstaller 冻结资源目录
    if meipass:
        dirs.append(Path(meipass))
    exe = getattr(sys, "executable", "") or ""
    if exe:
        dirs.append(Path(exe).resolve().parent)
    dirs.append(Path(__file__).resolve().parent)
    for d in dirs:
        for n in names:
            p = d / n
            try:
                if p.is_file():
                    return str(p)
            except OSError:
                continue
    return ""


def ffmpeg_exe() -> str | None:
    """返回可用的 ffmpeg 路径，查找顺序：

    1. imageio-ffmpeg 自带的（开发环境）；
    2. **随工具分发**的 ffmpeg（自包含发布，见 :func:`_bundled_ffmpeg`）；
    3. 系统 PATH（兜底）。

    结果进程内缓存，避免每个视频都重复探测。
    """
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE is not None:
        return _FFMPEG_CACHE or None
    exe = ""
    try:
        import imageio_ffmpeg

        candidate = imageio_ffmpeg.get_ffmpeg_exe()
        if candidate and os.path.isfile(candidate):
            exe = candidate
    except Exception:
        exe = ""
    if not exe:
        exe = _bundled_ffmpeg()
    if not exe:
        exe = shutil.which("ffmpeg") or ""
    _FFMPEG_CACHE = exe
    return exe or None


def make_video_thumb(data: bytes, temp_dir: Path | None = None) -> Image.Image | None:
    """用 ffmpeg 从视频明文抽一帧并缩放，返回缩略图；失败返回 None。

    **必须落临时文件**：MP4 的 moov（索引）常位于文件尾部，而 TDEF 解密后
    的明文是完整字节流；若改用管道（pipe:0）输入，ffmpeg 无法回退 seek，
    会报 "partial file / unspecified pixel format" 而抽帧失败。写盘后
    ffmpeg 可以正常 seek，成功率显著提高。

    临时文件用完立即删除；写入位置优先 temp_dir（便于统一清理），
    默认用系统临时目录。
    """
    exe = ffmpeg_exe()
    if not exe or not data:
        return None

    tmp_path: str | None = None
    try:
        if temp_dir is not None:
            try:
                Path(temp_dir).mkdir(parents=True, exist_ok=True)
                fd, tmp_path = tempfile.mkstemp(suffix=".mp4", prefix="vid-", dir=str(temp_dir))
            except OSError:
                fd, tmp_path = tempfile.mkstemp(suffix=".mp4", prefix="vid-")
        else:
            fd, tmp_path = tempfile.mkstemp(suffix=".mp4", prefix="vid-")
        with os.fdopen(fd, "wb") as f:
            f.write(data)

        # 先按偏移抽帧（避开可能全黑的开头），失败再试首帧
        for seek in (VIDEO_SEEK, None):
            cmd = [exe, "-loglevel", "error"]
            if seek is not None:
                cmd += ["-ss", seek]
            cmd += [
                "-i", tmp_path,
                "-frames:v", "1",
                "-vf", f"scale={THUMB * 2}:-2:flags=bilinear",
                "-f", "image2pipe",
                "-vcodec", "mjpeg",
                "pipe:1",
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=VIDEO_THUMB_TIMEOUT,
                    # 关键：否则 pythonw 下每个视频都会闪出一个控制台窗口
                    **_hidden_subprocess_kwargs(),
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if proc.returncode != 0 or not proc.stdout:
                continue
            try:
                with Image.open(io.BytesIO(proc.stdout)) as im:
                    im = im.convert("RGB")
                    im.thumbnail((THUMB, THUMB), Image.Resampling.LANCZOS)
                    return im
            except Exception:
                continue
        return None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


class CacheError(RuntimeError):
    """缓存读取过程中的可预期错误。"""


class PasscodeRequiredError(CacheError):
    """该 tdata 设有本地锁定密码。"""


class MediaTooLargeError(CacheError):
    """单个媒体明文超过 ``MAX_DECRYPT_BYTES``，无法整份读进内存。

    单列为独立类型（而非又一个裸 ``CacheError``），是为了让调用方能把"体积过大"
    与其它解密失败区分开、给出针对性的提示（见 README / LOG-1）。
    """


# ---------------------------------------------------------------------------
# 定位解密模块
#
# 默认使用随项目分发的 _vendor/tg_scoop（自包含，不依赖外部目录）；
# 若该副本缺失，才回退到外部的 tg-scoop 检出或 TG_SCOOP_SRC。
# _vendor 内的代码来自 GarrettFynn/tg-scoop（MIT），详见 _vendor/tg_scoop/LICENSE。
# ---------------------------------------------------------------------------
def find_decryptor_src() -> Path | None:
    """返回包含 tg_scoop 包的目录（供 sys.path 使用），找不到返回 None。

    优先级：
        1. TG_SCOOP_SRC 环境变量（便于开发时指向外部检出）
        2. 内置副本 _vendor/       ← 正常路径，保证自包含
        3. 同级 tg-scoop 检出的 src/  （旧布局，仅作开发兜底）
    """
    here = Path(__file__).resolve().parent
    candidates: list[Path] = []
    env = os.environ.get("TG_SCOOP_SRC", "").strip()
    if env:
        candidates.append(Path(env))
    candidates += [
        here / "_vendor",
        here.parent / "tg-scoop" / "src",
    ]
    for c in candidates:
        try:
            if (c / "tg_scoop" / "cache_decryptor.py").is_file():
                return c
        except OSError:
            continue
    return None


@dataclass
class CacheEntry:
    """一条缓存媒体（对应一个 TDEF 文件）。"""

    cache_path: Path
    rel: str
    label: str          # cache / media_cache
    mtime: float
    enc_size: int
    kind: str = ""
    ext: str = ""
    size: int = 0
    thumb_key: str = ""


def _import_decryptor():
    """导入解密模块（内置副本优先）。"""
    src = find_decryptor_src()
    if src is None:
        raise CacheError(t("cs.err_no_decryptor"))
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        from tg_scoop.cache_decryptor import (
            TDEF_DATA_OFFSET,
            CacheDecryptor,
            iter_cache_files,
        )
        from tg_scoop.exceptions import PasswordRequiredError
        from tg_scoop.tdata_reader import TdataReader
    except Exception as exc:  # pragma: no cover
        raise CacheError(t("cs.err_import", exc=exc)) from exc
    return (
        TdataReader,
        CacheDecryptor,
        iter_cache_files,
        PasswordRequiredError,
        TDEF_DATA_OFFSET,
    )


# ---------------------------------------------------------------------------
# 明文类型嗅探
# ---------------------------------------------------------------------------
def sniff(data: bytes) -> tuple[str, str] | None:
    """按文件头判断明文媒体类型，返回 (kind, ext)；无法识别返回 None。"""
    if len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "image", ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image", ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image", ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image", ".webp"
    if data[:2] == b"BM":
        return "image", ".bmp"
    if data[4:8] == b"ftyp":
        return ("video", ".mov") if data[8:12] == b"qt  " else ("video", ".mp4")
    if data[:4] == b"\x1a\x45\xdf\xa3":
        # EBML 容器：webm 与 matroska(mkv) 靠文件头前段的 DocType 区分；
        # 都找不到时默认 MKV——这样两种格式才能被分别勾选/过滤（与上游一致）。
        window = data[:4096]
        if b"webm" in window:
            return "video", ".webm"
        return "video", ".mkv"
    if data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        return "video", ".avi"
    return None


def make_thumb(data: bytes, kind: str) -> Image.Image | None:
    """由明文生成缩略图；视频或解码失败返回 None。"""
    if kind != "image":
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            im.thumbnail((THUMB, THUMB), Image.Resampling.LANCZOS)
            return im
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 缓存读取器
# ---------------------------------------------------------------------------
class CacheReader:
    """持有一份 LocalKey，按需解密 tdata 缓存。只读。"""

    def __init__(self, tdata: Path, passcode: str = "") -> None:
        """派生 LocalKey 并准备解密器；tdata 不存在或需密码时抛 CacheError。"""
        tdata = Path(tdata)
        if not tdata.is_dir():
            raise CacheError(t("cs.err_tdata_missing", tdata=tdata))
        (
            tdata_reader_cls,
            cache_decryptor_cls,
            iter_cache_files,
            password_required_error,
            tdef_offset,
        ) = _import_decryptor()
        self.tdata = tdata
        self._iter_cache_files = iter_cache_files
        self._tdef_offset = tdef_offset
        self._reader = tdata_reader_cls(tdata)
        try:
            local_key = self._reader.read_local_key(passcode)
        except password_required_error as exc:
            raise PasscodeRequiredError(t("cs.err_passcode")) from exc
        self._decryptor = cache_decryptor_cls(local_key)

    def cache_roots(self) -> list[Path]:
        """返回存在的缓存根目录。"""
        base = self.tdata / "user_data"
        roots = []
        for name in ("cache", "media_cache"):
            d = base / name
            if d.is_dir():
                roots.append(d)
        return roots

    def entries(self) -> list[CacheEntry]:
        """列出全部候选缓存文件（跳过 version/binlog 等索引文件）。"""
        out: list[CacheEntry] = []
        for root in self.cache_roots():
            try:
                files = list(self._iter_cache_files(root))
            except Exception:
                continue
            for p in files:
                try:
                    st = p.stat()
                except OSError:
                    continue
                try:
                    rel = str(p.relative_to(self.tdata))
                except ValueError:
                    rel = p.name
                out.append(
                    CacheEntry(
                        cache_path=p, rel=rel, label=root.name,
                        mtime=st.st_mtime, enc_size=st.st_size,
                    )
                )
        return out

    def decrypt(self, path: Path) -> bytes:
        """完整解密一个缓存文件（整份读入内存；导出请改用 :meth:`iter_decrypted`）。"""
        if path.stat().st_size > MAX_DECRYPT_BYTES:
            raise MediaTooLargeError(t("cs.err_too_large", name=path.name))
        return self._decryptor.decrypt_file(path)

    def iter_decrypted(self, path: Path,
                       chunk_size: int = 1 << 20) -> Iterator[bytes]:
        """**分块**解密一个缓存文件，逐块产出明文（内存峰值 ≈ ``chunk_size``）。

        供导出**流式**写出，避免把整份明文驻留内存。与 :meth:`decrypt` 同口径：
        超过 ``MAX_DECRYPT_BYTES`` 抛 :class:`MediaTooLargeError`；输出与整份解密
        **逐字节一致**（同一 TDEF/CTR 实现，实测 120 个缓存文件完全一致）。
        """
        if path.stat().st_size > MAX_DECRYPT_BYTES:
            raise MediaTooLargeError(t("cs.err_too_large", name=path.name))
        return self._decryptor.decrypt_file_iter(path, chunk_size=chunk_size)

    def read_head_and_size(self, path: Path, head_bytes: int = HEAD_BYTES) -> tuple[bytes, int]:
        """只解密文件前缀并推算明文总长度（用于快速嗅探）。

        TDEF 容器固定开销 116 字节（magic 4 + salt 64 + 校验块 48），
        因此明文长度 = 文件长度 - 116，无需整体解密。

        Returns:
            (明文前缀, 明文总长度)
        """
        stat = path.stat()
        plain_size = max(0, stat.st_size - self._tdef_offset)
        gen = self._decryptor.decrypt_file_iter(path, chunk_size=head_bytes)
        head = next(gen, b"")
        return head, plain_size


_READER_CACHE: dict[tuple[str, str], CacheReader] = {}


def get_reader(tdata: Path, passcode: str = "") -> CacheReader:
    """按 ``(tdata, passcode)`` 复用同一个 :class:`CacheReader`。

    LocalKey 的派生（PBKDF2-HMAC-SHA512）开销不小，而一次会话里会被请求多次
    （扫描 ``prepare``、保存、扫描完成后回填 GUI 持有的 reader）；不缓存就会
    重复派生。只保留**最近一个**，避免在内存里同时驻留多份密钥。
    """
    key = (str(Path(tdata)), passcode)
    cached = _READER_CACHE.get(key)
    if cached is not None:
        return cached
    reader = CacheReader(tdata, passcode)
    _READER_CACHE.clear()
    _READER_CACHE[key] = reader
    return reader


# ---------------------------------------------------------------------------
# 索引与缩略图
# ---------------------------------------------------------------------------
def load_index(index_path: Path, tdata: Path) -> dict:
    """读取索引；tdata 路径变化则视为失效。"""
    try:
        data = json.loads(Path(index_path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    if str(data.get("tdata", "")) != str(tdata):
        return {}
    return data.get("entries", {}) or {}


def save_index(index_path: Path, tdata: Path, entries: dict) -> None:
    """把索引写成 JSON；写失败只忽略（只是缓存，丢了会重新解密）。"""
    try:
        Path(index_path).parent.mkdir(parents=True, exist_ok=True)
        Path(index_path).write_text(
            json.dumps({"version": 1, "tdata": str(tdata), "entries": entries},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def entry_thumb_path(thumb_dir: Path, key: str) -> Path:
    """缩略图的落盘路径：按哈希前两位分子目录，避免单目录文件过多。"""
    return Path(thumb_dir) / key[:2] / f"{key}.jpg"


def _process_one(ent: CacheEntry, reader: CacheReader, thumb_dir: Path,
                 key: str, exts: tuple[str, ...] | None) -> dict:
    """处理单个需要读盘的缓存文件：嗅探 → （图片）解密出缩略图。

    只读写**属于自己的**数据（``ent`` 与自己的缩略图文件），不触碰任何共享状态；
    结果由主线程按原顺序汇总，因此并发下输出与串行逐字节一致（确定性红线）。
    可在工作线程中调用。
    """
    try:
        head, plain_size = reader.read_head_and_size(ent.cache_path)
    except Exception:
        return {"status": "failed"}
    hit = sniff(head)
    if hit is None:
        return {"status": "skipped"}
    if exts is not None and hit[1] not in exts:
        return {"status": "filtered"}
    kind, ext = hit
    if kind == "image":
        # 图片体积小：完整解密，得到精确大小并出缩略图
        try:
            data = reader.decrypt(ent.cache_path)
            size = len(data)
            img = make_thumb(data, kind)
        except Exception:
            return {"status": "failed"}
        tp = entry_thumb_path(thumb_dir, key)
        try:
            tp.parent.mkdir(parents=True, exist_ok=True)
            if img is not None:
                img.save(tp, "JPEG", quality=85)
            elif tp.exists():
                tp.unlink()
        except OSError:
            pass
    else:
        # 视频：不整体解密，长度由容器开销推算；缩略图用界面占位图
        size = plain_size
    return {"status": "ok", "kind": kind, "ext": ext, "size": size}


class _JobWindow:
    """把待办任务**限流**地交给线程池：在途数量恒定不超过 ``size``。

    以前是"一次性 submit 全部 jobs"，超大缓存会瞬间创建成千上万个 future；线程数
    虽然受 ``max_workers`` 限制，但 future 与任务闭包本身就要占内存。这里改成滑动
    窗口：预填 ``size`` 个，之后**消费一个补一个**，因此不变式始终成立——下一个待
    消费的下标一定在窗口里。
    """

    def __init__(self, pool: concurrent.futures.ThreadPoolExecutor,
                 work, jobs: list[int], size: int) -> None:
        self._pool = pool
        self._work = work
        self._jobs = iter(jobs)
        self._size = max(1, size)
        self._pending: dict[int, concurrent.futures.Future] = {}

    def fill(self) -> None:
        """预填窗口（待办不足就少填几个）。"""
        while len(self._pending) < self._size and self._spawn():
            pass

    def take(self, idx: int) -> concurrent.futures.Future:
        """取出该下标的 future 并立刻补一个新的进窗口（先补再等结果，线程池不空转）。"""
        fut = self._pending.pop(idx)
        self._spawn()
        return fut

    def _spawn(self) -> bool:
        """提交下一个待办；已无待办返回 False。"""
        nxt = next(self._jobs, None)
        if nxt is None:
            return False
        self._pending[nxt] = self._pool.submit(self._work, nxt)
        return True


def prepare(
    tdata: Path,
    passcode: str,
    index_path: Path,
    thumb_dir: Path,
    progress_cb=None,
    cancel_event=None,
    exts: tuple[str, ...] | None = None,
    workers: int = 1,
) -> tuple[list[CacheEntry], dict]:
    """遍历缓存：嗅探类型 → 需要时出缩略图 → 复用/更新索引。

    ``exts`` 为可选的**输出格式过滤**（扩展名集合，含点，如 ``(".jpg", ".png")``；
    移植自上游 tg-scoop 的 allowed_types，但按**具体格式**而非大类）：
    None 表示不过滤；给定集合时，嗅探命中但不在集合内的格式**早停**（不解密、
    不出缩略图、不进本次结果），只计入 skipped——这样只想扫某几种格式时不会为
    其它格式付出解密与缩略图开销。旧索引里**仍然有效**（mtime 一致）的其它格式
    记录会原样保留，便于切回时直接复用（不因筛选而丢缓存、拖慢下次全扫）。

    ``workers`` 为读盘/解密阶段的并发线程数（来自界面的「性能档位」）：
    索引复用等零 IO 的条目仍在主线程直接判定，真正耗时的解密与缩略图生成交给
    线程池（Pillow/Crypto 会释放 GIL，因此能并行）；结果按**原顺序**汇总，
    ``workers=1`` 与旧版逐项串行的输出完全一致。

    ``progress_cb`` 每处理一项回调一次 ``progress_cb(done, total)``——**只给数字**，
    面向用户的进度文案由调用方（界面）按当前语言渲染，本层不拼装文案。

    返回 (有效条目, 统计)。
    """
    reader = get_reader(tdata, passcode)
    old = load_index(index_path, tdata)
    entries = reader.entries()
    total = len(entries)
    new_index: dict = {}
    result: list[CacheEntry] = []
    stats = {"total": total, "ok": 0, "reused": 0, "skipped": 0, "failed": 0}

    # ---- 1) 串行决策（零 IO 或仅一次 stat）：复用 / 过滤的直接定案 ----
    keys: list[str] = []
    reused: dict[int, dict] = {}      # 下标 -> 旧记录（直接复用）
    carried: dict[int, dict] = {}     # 下标 -> 旧记录（未选格式：保留但不输出）
    jobs: list[int] = []
    for idx, ent in enumerate(entries):
        key = hashlib.md5(ent.rel.encode("utf-8", "replace")).hexdigest()
        keys.append(key)
        rec = old.get(ent.rel) or {}
        rec_kind = rec.get("kind") if rec else None
        rec_ext = rec.get("ext") if rec else None
        rec_fresh = bool(rec) and abs(float(rec.get("mtime", -1)) - ent.mtime) < 1e-6
        # 复用路径上的格式过滤：记录仍有效，故保留到新索引，便于切回时复用
        if exts is not None and rec_fresh and rec_ext is not None and rec_ext not in exts:
            carried[idx] = rec
            continue
        # 复用：索引有记录 + 文件未变 + （图片需缩略图仍在；视频无需缩略图）
        thumb_ok = rec_kind != "image" or entry_thumb_path(thumb_dir, key).exists()
        if rec_fresh and thumb_ok:
            reused[idx] = rec
            continue
        jobs.append(idx)

    # ---- 2) 并行读盘/解密（保序汇总；workers=1 即等价串行）----
    def work(idx: int) -> dict:
        return _process_one(entries[idx], reader, thumb_dir, keys[idx], exts)

    done = 0
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers))
    # 在途任务限流（滑动窗口，见 _JobWindow）：窗口取 workers 的 4 倍，既压住内存峰值，
    # 又留足排队深度让线程池不至于饿着。
    window = _JobWindow(pool, work, jobs, max(1, workers) * 4)
    try:
        window.fill()
        for idx, ent in enumerate(entries):
            if cancel_event is not None and cancel_event.is_set():
                break
            key = keys[idx]
            if idx in reused:
                rec = reused[idx]
                ent.kind = rec.get("kind", "")
                ent.ext = rec.get("ext", "")
                ent.size = int(rec.get("size", 0))
                ent.thumb_key = key
                new_index[ent.rel] = rec
                result.append(ent)
                stats["ok"] += 1
                stats["reused"] += 1
            elif idx in carried:
                new_index[ent.rel] = carried[idx]
                stats["skipped"] += 1
            else:
                out = window.take(idx).result()
                status = out["status"]
                if status == "failed":
                    stats["failed"] += 1
                elif status == "skipped" or status == "filtered":
                    stats["skipped"] += 1
                else:
                    ent.kind, ent.ext = out["kind"], out["ext"]
                    ent.size = out["size"]
                    ent.thumb_key = key
                    new_index[ent.rel] = {
                        "kind": ent.kind, "ext": ent.ext, "size": ent.size,
                        "mtime": ent.mtime, "label": ent.label,
                    }
                    result.append(ent)
                    stats["ok"] += 1
            done += 1
            if progress_cb is not None:
                progress_cb(done, total)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)

    if cancel_event is not None and cancel_event.is_set():
        # 被取消：不写索引，避免把"扫到一半"的结果落盘（下次启动会重新解密）
        return result, stats
    save_index(index_path, tdata, new_index)
    return result, stats


def default_cache_name(ent: CacheEntry) -> str:
    """缓存媒体的可读文件名：tg_日期_时间_哈希8位.扩展名。"""
    when = datetime.fromtimestamp(ent.mtime)
    short = (ent.thumb_key or hashlib.md5(ent.rel.encode()).hexdigest())[:8]
    return f"tg_{when:%Y%m%d_%H%M%S}_{short}{ent.ext or '.bin'}"


# ---------------------------------------------------------------------------
# 挑选项与保存（领域逻辑，从 GUI 模块 cache_picker 收敛到此，便于自检与复用）
# ---------------------------------------------------------------------------
@dataclass
class MediaItem:
    """网格中的一条**缓存**媒体（对应一个 `Tdata` 文件）。

    与在线挑选器的 ``tg_picker.MediaItem`` 是同一概念、字段不同：
    这里的数据来自已解密的本地缓存，故只包一层 :class:`CacheEntry`。
    """

    entry: CacheEntry
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = default_cache_name(self.entry)

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


def save_items(items, out_dir: Path, tdata: Path, passcode: str,
               by_date: bool = False, progress_cb=None,
               cancel_event=None, reader: CacheReader | None = None):
    """解密选中项并写出到 out_dir。返回 (成功, 失败)。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if reader is None:
        reader = get_reader(tdata, passcode)
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
               by_date: bool = False, reader: CacheReader | None = None) -> Path:
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
        reader = get_reader(tdata, passcode)
    dest = unique_path(target_dir / it.name)
    # 分块流式写出（避免把整份明文读进内存）；输出与整份解密逐字节一致。
    with open(dest, "wb") as f:
        for chunk in reader.iter_decrypted(it.entry.cache_path):
            f.write(chunk)
    try:
        os.utime(dest, (it.mtime, it.mtime))
    except OSError:
        pass
    return dest
