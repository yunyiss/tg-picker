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
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageFile

from i18n import t

# 缓存里可能存在"只缓存了一部分"的媒体，允许截断解码以尽量出图
ImageFile.LOAD_TRUNCATED_IMAGES = True

THUMB = 140
HEAD_BYTES = 64 * 1024          # 索引阶段嗅探时解密的前缀长度
MAX_DECRYPT_BYTES = 512 * 1024 * 1024

# 视频首帧提取：把解密后的明文写成临时文件，交给 ffmpeg 抽一帧。
# ffmpeg 由 imageio-ffmpeg 提供（自带静态二进制，无需系统安装）。
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
_NO_WINDOW_FLAGS = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0
)
_HAS_STARTUPINFO = hasattr(subprocess, "STARTUPINFO")


def _hidden_subprocess_kwargs() -> dict:
    """返回让子进程不弹控制台窗口的 subprocess 参数（仅 Windows 生效）。

    每次调用都新建 STARTUPINFO：抽帧由多线程并发发起，
    复用同一个对象不安全（同时被多个 CreateProcess 读取）。
    """
    kwargs: dict = {}
    if _NO_WINDOW_FLAGS:
        kwargs["creationflags"] = _NO_WINDOW_FLAGS
    if _HAS_STARTUPINFO:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = si
    return kwargs


def ffmpeg_exe() -> str | None:
    """返回可用的 ffmpeg 路径：优先 imageio-ffmpeg 自带的，其次系统 PATH。

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
                    im.thumbnail((THUMB, THUMB), Image.LANCZOS)
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
            im.thumbnail((THUMB, THUMB), Image.LANCZOS)
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
        """完整解密一个缓存文件。"""
        if path.stat().st_size > MAX_DECRYPT_BYTES:
            raise CacheError(t("cs.err_too_large", name=path.name))
        return self._decryptor.decrypt_file(path)

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
        rec = old.get(ent.rel)
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
    try:
        futures = {idx: pool.submit(work, idx) for idx in jobs}
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
                out = futures[idx].result()
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
