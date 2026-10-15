"""缓存挑选器端到端自检：读取真实缓存、保存、按日期分目录、预览、索引复用。

需要真实 tdata（``tdata_dir`` fixture，缺失即 skip）。写出的文件一律落在
``tmp_path``，不触碰用户的其它目录。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import cache_picker as cp
import cache_source as cs


def test_default_tdata_probe_needs_no_gui():
    """``_default_tdata`` 只做路径探测，必须能在无 GUI 下调用并返回字符串。

    回归测试：该函数依赖模块级的 ``import sys``，重构时曾被误删导致 NameError
    （因为平时都有已保存的 tdata 路径，走不到这条分支，测试不会暴露）。
    """
    result = cp.CacheApp._default_tdata()
    assert isinstance(result, str)


def _images(entries, count: int):
    items = [cp.MediaItem(entry=e) for e in entries]
    imgs = [i for i in items if i.kind == "image"]
    return imgs[:count]


def test_load_cache_decodes_media(tdata_dir: Path):
    """读取真实缓存：应解出媒体且零失败。"""
    entries, stats = cp.load_cache(tdata_dir, "")
    assert stats["total"] > 0, "一个缓存文件都没枚举到"
    assert stats["ok"] > 0, "没解析出任何媒体"
    assert stats["failed"] == 0, f"有解密失败：{stats['failed']}"

    items = [cp.MediaItem(entry=e) for e in entries]
    assert any(i.kind == "image" for i in items), "没有图片"
    # 文件名规则：tg_ 前缀
    assert all(i.name.startswith("tg_") for i in items[:5])


def test_load_cache_filters_by_format(tdata_dir: Path):
    """输出格式过滤（按**具体格式**早停）：只解出所选格式，未勾选的被计 skipped。"""
    img_exts = (".jpg", ".png", ".gif", ".webp")
    entries, stats = cp.load_cache(tdata_dir, "", exts=img_exts)
    assert stats["failed"] == 0
    items = [cp.MediaItem(entry=e) for e in entries]
    assert items, "只扫图片格式后仍应有条目"
    assert all(i.entry.ext in img_exts for i in items), "过滤后混入了未勾选的格式"
    assert all(i.kind == "image" for i in items)
    # 与不过滤相比，跳过的条目不会更少（其它格式被计入 skipped）
    _, stats_all = cp.load_cache(tdata_dir, "")
    assert stats["skipped"] >= stats_all["skipped"]


def test_load_cache_excludes_webm(tdata_dir: Path):
    """需求的直接场景：不勾选 webm 时，webm 不被扫描识别（也不会混进结果）。"""
    entries, _ = cp.load_cache(tdata_dir, "", exts=(".mp4", ".jpg", ".png"))
    items = [cp.MediaItem(entry=e) for e in entries]
    assert all(i.entry.ext != ".webm" for i in items), "未勾选 webm 却仍被扫出来"


def test_filtered_scan_keeps_index_for_reuse(tdata_dir: Path, tmp_path: Path, monkeypatch):
    """筛选扫描只输出所选格式，但**保留**其它格式仍有效的索引记录。

    回归测试（性能）：曾经在筛选时丢弃其它格式的索引记录，导致切回「全部」时
    整批重新解密（视频筛选后回全扫需重生成 2000+ 缩略图，≈ 冷扫描）。
    """
    idx = tmp_path / "idx.json"
    thumbs = tmp_path / "thumbs"
    monkeypatch.setattr(cp, "INDEX_PATH", idx)
    monkeypatch.setattr(cp, "THUMB_DIR", thumbs)

    cp.load_cache(tdata_dir, "")                       # 全量：写入完整索引
    full_n = len(json.loads(idx.read_text(encoding="utf-8"))["entries"])
    assert full_n > 0

    entries, stats = cp.load_cache(tdata_dir, "", exts=(".jpg",))
    assert all(cp.MediaItem(entry=e).entry.ext == ".jpg" for e in entries), \
        "本次输出应只有所选格式"
    kept_n = len(json.loads(idx.read_text(encoding="utf-8"))["entries"])
    assert kept_n == full_n, "其它格式的索引记录应被保留（否则切回全部要重解密）"

    # 切回「全部」：应当**全部复用**，不重新解密
    _, stats_full = cp.load_cache(tdata_dir, "")
    assert stats_full["reused"] == full_n, \
        f"切回全部应完全复用索引，实际复用 {stats_full['reused']}/{full_n}"
    assert stats_full["failed"] == 0


def test_parallel_scan_matches_serial(tdata_dir: Path, tmp_path: Path, monkeypatch):
    """并发扫描（workers=8）与串行（workers=1）的条目顺序与统计必须逐项一致。

    回归测试（确定性红线）：扫描的读盘/解密改为并发后，输出不得因调度而漂移。
    """
    def run(workers: int, tag: str):
        monkeypatch.setattr(cp, "INDEX_PATH", tmp_path / f"idx-{tag}.json")
        monkeypatch.setattr(cp, "THUMB_DIR", tmp_path / f"thumbs-{tag}")
        entries, stats = cp.load_cache(tdata_dir, "", workers=workers)
        return entries, stats

    e1, s1 = run(1, "serial")
    e8, s8 = run(8, "parallel")

    assert len(e1) == len(e8) > 0, (len(e1), len(e8))
    assert [e.rel for e in e1] == [e.rel for e in e8], "并发下条目顺序应与串行一致"
    assert [(e.kind, e.ext, e.size) for e in e1] == \
        [(e.kind, e.ext, e.size) for e in e8], "并发下类型/大小应与串行一致"
    assert s1 == s8, f"并发统计应与串行一致：{s1} vs {s8}"


def test_thumbnails_are_cached_on_disk(tdata_dir: Path):
    """图片缩略图应落盘（重复启动才可能秒开）。"""
    entries, _ = cp.load_cache(tdata_dir, "")
    items = [cp.MediaItem(entry=e) for e in entries]
    have = sum(
        1 for it in items[:80]
        if cs.entry_thumb_path(cp.THUMB_DIR, it.entry.thumb_key).exists()
    )
    assert have > 0, "缩略图一个都没生成"


def test_save_writes_valid_media_without_overwrite(tdata_dir: Path, tmp_path: Path):
    """保存选中项：写出的确实是媒体；重名自动加后缀而不覆盖。"""
    entries, _ = cp.load_cache(tdata_dir, "")
    picked = _images(entries, 5)
    assert picked, "样本里没有图片可用"

    out = tmp_path / "saved"
    ok, fail = cp.save_items(picked, out, tdata_dir, "")
    assert (ok, fail) == (5, 0), (ok, fail)

    for it in picked:
        f = out / it.name
        assert f.exists(), f"没写出 {it.name}"
        assert cs.sniff(f.read_bytes()[:12]) is not None, f"{it.name} 写出的不是媒体"

    # 再存一次同名文件：应加后缀，总数变 6
    ok2, _fail2 = cp.save_items(picked[:1], out, tdata_dir, "")
    assert ok2 == 1
    assert len(list(out.glob("*"))) == 6, "重名应加后缀而不是覆盖"


def test_save_by_date_creates_dated_subfolders(tdata_dir: Path, tmp_path: Path):
    """勾选"按日期分文件夹"时，输出目录下应生成 YYYY-MM-DD 子目录。"""
    entries, _ = cp.load_cache(tdata_dir, "")
    picked = _images(entries, 6)
    assert picked, "样本里没有图片可用"

    out = tmp_path / "bydate"
    ok, fail = cp.save_items(picked, out, tdata_dir, "", by_date=True)
    assert (ok, fail) == (6, 0), (ok, fail)

    subs = [p.name for p in out.iterdir() if p.is_dir()]
    assert subs and all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) for s in subs), subs


def test_export_one_streams_identical_bytes(tdata_dir: Path, tmp_path: Path):
    """LOG-2：导出改为分块流式写出后，落盘内容仍与整份解密**逐字节一致**。"""
    entries, _ = cp.load_cache(tdata_dir, "")
    picked = _images(entries, 1)
    assert picked, "样本里没有图片可用"
    it = picked[0]
    dest = cp.export_one(it, tmp_path / "stream", tdata_dir, "")
    reader = cs.get_reader(tdata_dir, "")
    assert dest.read_bytes() == reader.decrypt(it.entry.cache_path), "流式导出字节应与整份一致"


def test_export_one_decrypts_to_file(tdata_dir: Path, tmp_path: Path):
    """双击导出：应解密成非空文件，且重名加后缀而不覆盖。"""
    entries, _ = cp.load_cache(tdata_dir, "")
    picked = _images(entries, 1)
    assert picked, "样本里没有图片可用"

    out = tmp_path / "out"
    dest = cp.export_one(picked[0], out, tdata_dir, "")
    assert dest.exists() and dest.stat().st_size > 0
    assert cs.sniff(dest.read_bytes()[:12]) is not None, "导出的不是媒体"

    again = cp.export_one(picked[0], out, tdata_dir, "")
    assert again != dest, "重名应加后缀而不是覆盖"
    assert dest.exists(), "原文件不应被覆盖"


def test_clean_cache_removes_only_cache_artifacts(tmp_path, monkeypatch):
    """清理缓存：删掉缩略图/索引/抽帧目录并返回释放空间，且不碰用户导出的媒体。"""
    thumb = tmp_path / "thumbs"
    (thumb / "ab").mkdir(parents=True)
    (thumb / "ab" / "x.jpg").write_bytes(b"a" * 100)
    index = tmp_path / "idx.json"
    index.write_bytes(b"b" * 50)
    video = tmp_path / "tmp_video"
    video.mkdir()
    (video / "t.mp4").write_bytes(b"c" * 10)
    keep = tmp_path / "picked"                      # 用户导出的媒体
    keep.mkdir()
    (keep / "keep.jpg").write_bytes(b"d" * 999)

    monkeypatch.setattr(cp, "cache_artifacts", lambda: [thumb, index, video])
    assert cp.cache_bytes() == 160
    assert cp.clean_cache() == 160
    assert not thumb.exists() and not index.exists() and not video.exists()
    assert keep.exists() and (keep / "keep.jpg").exists(), "用户导出的媒体绝不能被清掉"


def test_index_reuse_is_high(tdata_dir: Path):
    """第二次读取应几乎全部复用索引（缓存索引的意义所在）。

    只断言复用比例，不断言耗时——耗时断言在负载波动下会偶发失败。
    """
    _entries1, stats1 = cp.load_cache(tdata_dir, "")
    _entries2, stats2 = cp.load_cache(tdata_dir, "")
    assert stats2["ok"] > 0
    assert stats2["reused"] > stats2["ok"] * 0.8, (
        f"索引复用率过低：{stats2['reused']}/{stats2['ok']}"
    )
    # Telegram 可能在后台持续写缓存，允许两次总量小幅增长
    assert stats2["ok"] >= stats1["ok"] - 50, "两次结果差异过大"