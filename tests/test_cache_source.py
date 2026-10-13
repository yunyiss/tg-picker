"""cache_source 自检：定位解密内核、派生 LocalKey、枚举/解密/嗅探、索引读写。

除索引与命名外都需要真实 tdata（用 ``tdata_dir`` fixture，缺失即 skip）。
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

import cache_source as cs


def test_find_decryptor_src_points_to_vendor():
    """解密内核应能定位到项目内置的 _vendor（保证自包含，不依赖外部目录）。"""
    src = cs.find_decryptor_src()
    assert src is not None, "找不到解密模块（应存在 _vendor/tg_scoop）"
    assert (src / "tg_scoop" / "cache_decryptor.py").is_file()


def test_default_cache_name_format():
    """默认文件名形如 tg_日期_时间_哈希8位.扩展名，且不依赖真实 tdata。"""
    entry = cs.CacheEntry(
        cache_path=Path("dummy"), rel="1/0/0s", label="cache",
        mtime=datetime(2026, 10, 10, 1, 50, 25).timestamp(),
        enc_size=1, kind="image", ext=".jpg", size=1,
        thumb_key="abcdef1234567890",
    )
    name = cs.default_cache_name(entry)
    assert re.fullmatch(r"tg_\d{8}_\d{6}_abcdef12\.jpg", name), name


def test_sniff_recognizes_media_types():
    """类型嗅探按文件头判断，不依赖真实数据。"""
    assert cs.sniff(b"\xff\xd8\xff\xe0" + b"\x00" * 8) == ("image", ".jpg")
    assert cs.sniff(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8) == ("image", ".png")
    assert cs.sniff(b"\x00\x00\x00\x18ftypmp42") == ("video", ".mp4")
    assert cs.sniff(b"\x00\x00\x00\x18ftypqt  ") == ("video", ".mov")
    assert cs.sniff(b"RIFF\x00\x00\x00\x00AVI ") == ("video", ".avi")
    # EBML 容器：webm / mkv 靠 DocType 区分（缺失时默认 mkv），这样才能分别勾选
    assert cs.sniff(b"\x1a\x45\xdf\xa3" + b"\x00" * 20 + b"webm") == ("video", ".webm")
    assert cs.sniff(b"\x1a\x45\xdf\xa3" + b"\x00" * 20 + b"matroska") == ("video", ".mkv")
    assert cs.sniff(b"\x1a\x45\xdf\xa3" + b"\x00" * 40) == ("video", ".mkv")
    assert cs.sniff(b"not-a-media!") is None


def test_local_key_and_cache_roots(tdata_dir: Path):
    """能派生 LocalKey 并找到缓存根目录（只读，不修改 tdata）。"""
    reader = cs.CacheReader(tdata_dir)
    roots = reader.cache_roots()
    assert roots, "没有找到缓存目录"
    assert all(r.is_dir() for r in roots)


def test_enumerate_decrypt_sniff_and_thumb(tdata_dir: Path):
    """枚举缓存文件，抽样解密 + 嗅探 + 生成缩略图（图片应能出图）。"""
    reader = cs.CacheReader(tdata_dir)
    entries = reader.entries()
    assert entries, "没枚举到缓存文件"

    ok = fail = thumbs = 0
    for ent in entries[:400]:
        try:
            data = reader.decrypt(ent.cache_path)
        except Exception:
            fail += 1
            continue
        hit = cs.sniff(data)
        if hit is None:
            continue
        kind, _ext = hit
        ok += 1
        if thumbs == 0 and kind == "image":
            if cs.make_thumb(data, kind) is not None:
                thumbs += 1

    assert ok > 0, "一个媒体都没解出来"
    assert fail == 0, f"有解密失败：{fail}"
    assert thumbs > 0, "缩略图一张都没生成"


def test_index_roundtrip_and_invalidation(tdata_dir: Path, tmp_path: Path):
    """索引读写正常；tdata 路径变化后索引应失效。"""
    index_path = tmp_path / "idx.json"
    entries = {"x": {"kind": "image", "ext": ".jpg", "size": 1, "mtime": 1.0}}

    cs.save_index(index_path, tdata_dir, entries)
    got = cs.load_index(index_path, tdata_dir)
    assert got.get("x", {}).get("kind") == "image", got

    other = tmp_path / "别的tdata"
    assert cs.load_index(index_path, other) == {}, "tdata 路径变化后索引应失效"


def test_corrupt_index_falls_back_to_empty(tmp_path: Path):
    """索引文件损坏时应安全回退为空 dict，而不是抛错。"""
    bad = tmp_path / "bad.json"
    bad.write_text("{ 这不是合法 JSON", encoding="utf-8")
    assert cs.load_index(bad, tmp_path) == {}


def test_tdata_is_read_only(tdata_dir: Path, tmp_path: Path):
    """读取缓存不得改动 tdata 内任何文件（用 binlog 的 mtime 校验）。

    索引与缩略图一律写到 tmp_path，**绝不**落到 tdata 里面。
    """
    binlog = tdata_dir / "user_data" / "cache" / "1" / "binlog"
    if not binlog.exists():
        pytest.skip("该 tdata 没有 binlog，无法校验只读性")
    before = binlog.stat().st_mtime
    cs.prepare(
        tdata_dir, "",
        index_path=tmp_path / "readonly_idx.json",
        thumb_dir=tmp_path / "readonly_thumbs",
    )
    assert binlog.stat().st_mtime == before, "binlog 被改动了（应当只读）"


def test_get_reader_reuses_same_instance(tdata_dir: Path, monkeypatch):
    """同一 ``(tdata, passcode)`` 复用同一个 CacheReader（避免重复派生 LocalKey）。

    回归：扫描内部（``prepare``）与 GUI 扫描完成后回填 reader 曾各自新建一次
    ``CacheReader``，使 PBKDF2 的 LocalKey 派生白跑两遍。
    """
    calls: list[tuple[str, str]] = []
    real = cs.CacheReader

    def counting(tdata, passcode=""):
        calls.append((str(tdata), passcode))
        return real(tdata, passcode)

    monkeypatch.setattr(cs, "CacheReader", counting)
    monkeypatch.setattr(cs, "_READER_CACHE", {})

    r1 = cs.get_reader(tdata_dir, "")
    r2 = cs.get_reader(tdata_dir, "")
    assert r1 is r2, "同一 (tdata, passcode) 应复用同一个 reader"
    assert len(calls) == 1, f"LocalKey 应只派生一次，实际 {len(calls)} 次：{calls}"