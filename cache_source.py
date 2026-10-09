# -*- coding: utf-8 -*-
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

import hashlib
import io
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageFile

# 缓存里可能存在"只缓存了一部分"的媒体，允许截断解码以尽量出图
ImageFile.LOAD_TRUNCATED_IMAGES = True

THUMB = 140
HEAD_BYTES = 64 * 1024          # 索引阶段嗅探时解密的前缀长度
MAX_DECRYPT_BYTES = 512 * 1024 * 1024


class CacheError(RuntimeError):
    """缓存读取过程中的可预期错误。"""


class PasscodeRequired(CacheError):
    """该 tdata 设有本地锁定密码。"""


# ---------------------------------------------------------------------------
# 定位 tg-scoop 的解密模块
# ---------------------------------------------------------------------------
def find_decryptor_src() -> Path | None:
    """返回包含 tg_scoop 包的目录（供 sys.path 使用），找不到返回 None。"""
    here = Path(__file__).resolve().parent
    candidates: list[Path] = []
    env = os.environ.get("TG_SCOOP_SRC", "").strip()
    if env:
        candidates.append(Path(env))
    candidates += [
        here / "_vendor",
        here.parent / "tg-scoop" / "src",
        Path(r"D:\agent\dsh\tg-scoop\src"),
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
    """导入 tg-scoop 的 TdataReader / CacheDecryptor / iter_cache_files。"""
    src = find_decryptor_src()
    if src is None:
        raise CacheError(
            "找不到 tg-scoop 的解密模块。\n\n"
            "请确认 D:\\agent\\dsh\\tg-scoop\\src 存在，"
            "或设置环境变量 TG_SCOOP_SRC 指向该目录。"
        )
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
        raise CacheError(f"导入 tg-scoop 解密模块失败：{exc}") from exc
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
        return "video", ".webm"
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
        tdata = Path(tdata)
        if not tdata.is_dir():
            raise CacheError(f"tdata 目录不存在：{tdata}")
        (
            TdataReader,
            CacheDecryptor,
            iter_cache_files,
            PasswordRequiredError,
            tdef_offset,
        ) = _import_decryptor()
        self.tdata = tdata
        self._iter_cache_files = iter_cache_files
        self._tdef_offset = tdef_offset
        self._reader = TdataReader(tdata)
        try:
            local_key = self._reader.read_local_key(passcode)
        except PasswordRequiredError as exc:
            raise PasscodeRequired(
                "该 tdata 设有本地锁定密码（Telegram 设置→隐私与安全→锁定密码），"
                "请填写密码后重试"
            ) from exc
        self._decryptor = CacheDecryptor(local_key)

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
            raise CacheError(f"文件过大，已跳过：{path.name}")
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
    return Path(thumb_dir) / key[:2] / f"{key}.jpg"


def prepare(
    tdata: Path,
    passcode: str,
    index_path: Path,
    thumb_dir: Path,
    progress_cb=None,
    cancel_event=None,
) -> tuple[list[CacheEntry], dict]:
    """遍历缓存：嗅探类型 → 需要时出缩略图 → 复用/更新索引。

    返回 (有效条目, 统计)。
    """
    reader = CacheReader(tdata, passcode)
    old = load_index(index_path, tdata)
    entries = reader.entries()
    total = len(entries)
    new_index: dict = {}
    result: list[CacheEntry] = []
    stats = {"total": total, "ok": 0, "reused": 0, "skipped": 0, "failed": 0}

    for i, ent in enumerate(entries, 1):
        if cancel_event is not None and cancel_event.is_set():
            break
        if progress_cb is not None:
            progress_cb(i, total, f"解析缓存 {i}/{total}")

        rec = old.get(ent.rel)
        key = hashlib.md5(ent.rel.encode("utf-8", "replace")).hexdigest()
        tp = entry_thumb_path(thumb_dir, key)

        # 复用：索引有记录 + 文件未变 + （图片需缩略图仍在；视频无需缩略图）
        rec_kind = rec.get("kind") if rec else None
        thumb_ok = tp.exists() if rec_kind == "image" else True
        if rec and abs(float(rec.get("mtime", -1)) - ent.mtime) < 1e-6 and thumb_ok:
            ent.kind = rec.get("kind", "")
            ent.ext = rec.get("ext", "")
            ent.size = int(rec.get("size", 0))
            ent.thumb_key = key
            new_index[ent.rel] = rec
            result.append(ent)
            stats["ok"] += 1
            stats["reused"] += 1
            continue

        # 快速嗅探：只解密前缀即可判断类型
        try:
            head, plain_size = reader.read_head_and_size(ent.cache_path)
        except Exception:
            stats["failed"] += 1
            continue
        hit = sniff(head)
        if hit is None:
            stats["skipped"] += 1
            continue

        kind, ext = hit
        ent.kind, ent.ext = kind, ext

        if kind == "image":
            # 图片体积小：完整解密，得到精确大小并出缩略图
            try:
                data = reader.decrypt(ent.cache_path)
                ent.size = len(data)
                img = make_thumb(data, kind)
            except Exception:
                stats["failed"] += 1
                continue
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
            ent.size = plain_size

        ent.thumb_key = key
        new_index[ent.rel] = {
            "kind": kind, "ext": ext, "size": ent.size,
            "mtime": ent.mtime, "label": ent.label,
        }
        result.append(ent)
        stats["ok"] += 1

    save_index(index_path, tdata, new_index)
    return result, stats


def default_cache_name(ent: CacheEntry) -> str:
    """缓存媒体的可读文件名：tg_日期_时间_哈希8位.扩展名。"""
    when = datetime.fromtimestamp(ent.mtime)
    short = (ent.thumb_key or hashlib.md5(ent.rel.encode()).hexdigest())[:8]
    return f"tg_{when:%Y%m%d_%H%M%S}_{short}{ent.ext or '.bin'}"
