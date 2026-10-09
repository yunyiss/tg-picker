# -*- coding: utf-8 -*-
"""功能自检：扫描真实目录、生成缩略图、复制/移动、按日期分文件夹。"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r"D:\agent\dsh\tg-picker")
import gallery_picker as g

SRC = Path(r"D:\agent\dsh\tg-scoop-output")

print("=== 1) 扫描真实目录 ===")
items = g.scan_folder(SRC)
print(f"扫描到 {len(items)} 个媒体文件")
assert len(items) > 0, "没扫到文件"
imgs = [m for m in items if m.kind == "image"]
vids = [m for m in items if m.kind == "video"]
print(f"  图片 {len(imgs)}，视频 {len(vids)}")

# 只扫图片
only_img = g.scan_folder(SRC, want_images=True, want_videos=False)
assert all(m.kind == "image" for m in only_img)
assert len(only_img) == len(imgs)
print(f"  仅图片模式：{len(only_img)} 个 ✓")

print("=== 2) 缩略图生成 + 缓存命中 ===")
sample = imgs[0]
key = g.thumb_key(sample)
t1 = g.build_thumb(sample, key)
assert t1 is not None and t1.size[0] <= g.THUMB and t1.size[1] <= g.THUMB, t1.size
print(f"  首次生成 {sample.path.name} → {t1.size}")
assert g.thumb_path(key).exists(), "缓存文件没写盘"
t2 = g.build_thumb(sample, key)
assert t2 is not None and t2.size == t1.size
print(f"  二次读取（走缓存）→ {t2.size} ✓")

# 视频占位图
if vids:
    tv = g.build_thumb(vids[0], g.thumb_key(vids[0]))
    assert tv is not None
    print(f"  视频占位图 → {tv.size} ✓")

# 损坏文件不应崩
bad = Path(tempfile.gettempdir()) / "gallery_bad_test.jpg"
bad.write_bytes(b"not an image at all")
tb = g.build_thumb(g.MediaFile(path=bad, size=20, mtime=0, kind="image"), "badtest000")
assert tb is not None
print(f"  损坏图片容错 → {tb.size} ✓")
bad.unlink(missing_ok=True)

print("=== 3) 复制保存（不破坏源文件）===")
with tempfile.TemporaryDirectory() as td:
    out = Path(td) / "saved"
    picked = imgs[:5]
    ok, fail, skip = g.save_files(picked, out, move=False)
    assert (ok, fail, skip) == (5, 0, 0), (ok, fail, skip)
    for m in picked:
        assert (out / m.path.name).exists(), m.path.name
        assert m.path.exists(), "复制模式下源文件必须还在"
    print(f"  复制 5 个 → 成功 {ok} 失败 {fail} 跳过 {skip}，源文件未动 ✓")

    # 重名不覆盖
    again = out / picked[0].path.name
    before = again.stat().st_size
    ok2, fail2, _ = g.save_files([picked[0]], out, move=False)
    assert ok2 == 1 and fail2 == 0
    cands = list(out.glob(f"{picked[0].path.stem} (*){picked[0].path.suffix}"))
    assert cands, "重名应生成 (1) 后缀"
    assert again.stat().st_size == before, "原文件被覆盖了！"
    print(f"  重名处理 → 生成 {cands[0].name}，原文件未被覆盖 ✓")

print("=== 4) 按日期分文件夹 ===")
with tempfile.TemporaryDirectory() as td:
    out = Path(td) / "bydate"
    ok, fail, skip = g.save_files(imgs[:4], out, move=False, by_date=True)
    assert fail == 0, fail
    subs = [p for p in out.iterdir() if p.is_dir()]
    assert subs, "应创建日期子目录"
    import re
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name) for p in subs), [p.name for p in subs]
    print(f"  按日期分目录 → {[p.name for p in subs]} ✓")

print("=== 5) 移动模式（源文件被移走）===")
with tempfile.TemporaryDirectory() as td:
    sandbox = Path(td) / "src"
    sandbox.mkdir()
    # 拷几个样本进沙箱，避免真的动 tg-scoop-output
    moved_src = []
    for m in imgs[:3]:
        dst = sandbox / m.path.name
        shutil.copy2(m.path, dst)
        st = dst.stat()
        moved_src.append(g.MediaFile(path=dst, size=st.st_size, mtime=st.st_mtime, kind="image"))
    out = Path(td) / "moved"
    ok, fail, skip = g.save_files(moved_src, out, move=True)
    assert (ok, fail, skip) == (3, 0, 0), (ok, fail, skip)
    for m in moved_src:
        assert not m.path.exists(), "移动模式下源文件应消失"
        assert (out / m.path.name).exists()
    print(f"  移动 3 个 → 成功 {ok}，源文件已移走、目标已就位 ✓")

print("=== 6) 工具函数 ===")
assert g.human_size(0) == "-"
assert g.human_size(1024) == "1.0 KB"
assert g.unique_path(Path(tempfile.gettempdir()) / "绝不会存在的文件_zzz.jpg").name.endswith(".jpg")
print("  human_size / unique_path ✓")

print("\n全部功能自检通过 ✅")
