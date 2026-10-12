# -*- coding: utf-8 -*-
"""缓存读取自检：验证 TDEF 解密、类型嗅探、缩略图生成。

需要一份真实的 tdata 才能跑；路径取自命令行参数或环境变量 TDATA_PATH，
都没有就尝试常见默认位置，仍找不到则以退出码 2 跳过（不是失败）。
"""
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cache_source as cs


def _resolve_tdata() -> Path | None:
    """按 参数 → 环境变量 → 常见路径 的顺序找 tdata。"""
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    env = os.environ.get("TDATA_PATH", "").strip()
    if env:
        return Path(env)
    cands = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            cands.append(Path(appdata) / "Telegram Desktop" / "tdata")
    elif sys.platform == "darwin":
        cands.append(Path.home() / "Library" / "Application Support"
                     / "Telegram Desktop" / "tdata")
    else:
        cands.append(Path.home() / ".local" / "share" / "TelegramDesktop" / "tdata")
    for drive in ("C:", "D:", "E:", "F:"):
        cands.append(Path(f"{drive}/Telegram/tdata"))
    for c in cands:
        try:
            if c.is_dir():
                return c
        except OSError:
            continue
    return None


TDATA = _resolve_tdata()
if TDATA is None:
    print("跳过：未找到 tdata 目录。")
    print("用法：python _selftest_cache.py [tdata路径]   或设置 TDATA_PATH")
    sys.exit(2)

print(f"使用 tdata: {TDATA}")

print("=== 1) 定位解密模块 ===")
src = cs.find_decryptor_src()
print("解密模块目录:", src)
assert src, "找不到解密模块（应存在 _vendor/tg_scoop）"

print("=== 2) 派生 LocalKey（只读 tdata）===")
t0 = time.time()
reader = cs.CacheReader(TDATA)
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
    cs.save_index(ip, TDATA, {"x": {"kind": "image", "ext": ".jpg", "size": 1, "mtime": 1.0}})
    got = cs.load_index(ip, TDATA)
    assert got.get("x", {}).get("kind") == "image", got
    # tdata 变了应失效
    got2 = cs.load_index(ip, Path(td) / "别的tdata")
    assert got2 == {}, "tdata 路径变化后索引应失效"
print("索引读写 + tdata 变更失效 ✓")

print("\n缓存读取自检全部通过 ✅")
