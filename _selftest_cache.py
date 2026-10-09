# -*- coding: utf-8 -*-
"""缓存读取自检：验证 TDEF 解密、类型嗅探、缩略图生成。"""
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, r"D:\agent\dsh\tg-picker")
import cache_source as cs

print("=== 1) 定位解密模块 ===")
src = cs.find_decryptor_src()
print("tg-scoop 模块目录:", src)
assert src, "找不到 tg-scoop 解密模块"

print("=== 2) 派生 LocalKey（只读 tdata）===")
t0 = time.time()
reader = cs.CacheReader(Path(r"D:\Telegram\tdata"))
print(f"LocalKey 派生成功，耗时 {time.time()-t0:.2f}s，无密码")
roots = reader.cache_roots()
print("缓存根目录:", [str(x) for x in roots])
assert roots, "没有找到缓存目录"

print("=== 3) 枚举缓存文件 ===")
t0 = time.time()
entries = reader.entries()
print(f"候选缓存文件 {len(entries)} 个（耗时 {time.time()-t0:.2f}s）")
assert entries, "没枚举到缓存文件"

print("=== 4) 抽样解密 + 类型嗅探 + 缩略图 ===")
counter = Counter()
ok = fail = nonmedia = thumbs = 0
sample_names = []
t0 = time.time()
for ent in entries[:400]:
    try:
        data = reader.decrypt(ent.cache_path)
    except Exception:
        fail += 1
        continue
    hit = cs.sniff(data)
    if hit is None:
        nonmedia += 1
        continue
    kind, ext = hit
    counter[f"{kind}{ext}"] += 1
    ok += 1
    if ok <= 5:
        img = cs.make_thumb(data, kind)
        if img is not None:
            thumbs += 1
            size_kb = len(data) // 1024
            sample_names.append(f"{kind}{ext} {size_kb}KB -> {img.size}")
print(f"媒体 {ok} 个，非媒体 {nonmedia} 个，解密失败 {fail} 个（耗时 {time.time()-t0:.2f}s）")
print("类型分布:", dict(counter))
print("缩略图样例:", sample_names)
assert ok > 0, "一个媒体都没解出来！"
assert thumbs > 0, "缩略图一张都没生成！"

print("=== 5) 命名与路径工具 ===")
e0 = [e for e in entries][0]
e0.thumb_key = "abcdef1234567890"
e0.ext = ".jpg"
name = cs.default_cache_name(e0)
print("默认文件名:", name)
assert name.startswith("tg_") and name.endswith(".jpg")

print("=== 6) 索引读写 ===")
import tempfile
with tempfile.TemporaryDirectory() as td:
    ip = Path(td) / "idx.json"
    cs.save_index(ip, Path(r"D:\Telegram\tdata"), {"x": {"kind": "image", "ext": ".jpg", "size": 1, "mtime": 1.0}})
    got = cs.load_index(ip, Path(r"D:\Telegram\tdata"))
    assert got.get("x", {}).get("kind") == "image", got
    # tdata 变了应失效
    got2 = cs.load_index(ip, Path(r"D:\Other\tdata"))
    assert got2 == {}, "tdata 路径变化后索引应失效"
print("索引读写 + tdata 变更失效 ✓")

print("\n缓存读取自检全部通过 ✅")
