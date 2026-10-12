# -*- coding: utf-8 -*-
"""缓存版挑选器功能自检：读取真实 tdata 缓存、保存、预览、索引复用。

tdata 路径取自命令行参数或环境变量 TDATA_PATH，找不到则退出码 2 跳过。
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cache_picker as cp
import cache_source as cs


def _resolve_tdata() -> Path | None:
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
    print("用法：python _selftest_cachepicker.py [tdata路径]   或设置 TDATA_PATH")
    sys.exit(2)

print(f"使用 tdata: {TDATA}")

print("=== 1) 读取真实缓存（首次，需解密）===")
t0 = time.time()
entries, stats = cp.load_cache(TDATA, "")
elapsed = time.time() - t0
print(f"总计 {stats['total']} 个缓存文件")
print(f"媒体 {stats['ok']} 个（复用 {stats['reused']}），非媒体 {stats['skipped']}，失败 {stats['failed']}")
print(f"耗时 {elapsed:.1f}s")
assert stats["ok"] > 0, "没解析出任何媒体"
assert stats["failed"] == 0, f"有解密失败：{stats['failed']}"

items = [cp.Item(entry=e) for e in entries]
imgs = [i for i in items if i.kind == "image"]
vids = [i for i in items if i.kind == "video"]
print(f"图片 {len(imgs)}，视频 {len(vids)}")
print("样例:", [(i.name, i.entry.label) for i in items[:3]])
assert imgs, "没有图片"

print("=== 2) 缩略图是否已落盘 ===")
have = 0
for it in items[:80]:
    tp = cs.entry_thumb_path(cp.THUMB_DIR, it.entry.thumb_key)
    if tp.exists():
        have += 1
assert have > 0, "缩略图一个都没生成"
print(f"前 80 个中 {have} 个有缩略图文件 ✓")

print("=== 3) 保存选中项（解密写出）===")
with tempfile.TemporaryDirectory() as td:
    out = Path(td) / "saved"
    picked = imgs[:5]
    ok, fail = cp.save_items(picked, out, TDATA, "")
    assert (ok, fail) == (5, 0), (ok, fail)
    for it in picked:
        f = out / it.name
        assert f.exists(), f"没写出 {it.name}"
        # 校验写出的确实是合法媒体
        head = f.read_bytes()[:12]
        assert cs.sniff(head) is not None, f"{it.name} 写出的不是媒体：{head[:8].hex()}"
    sizes = [(out / it.name).stat().st_size for it in picked]
    print(f"保存 5 个 → 成功 {ok} 失败 {fail}，大小 {sizes} ✓")

    # 重名不覆盖
    ok2, fail2 = cp.save_items(picked[:1], out, TDATA, "")
    assert ok2 == 1, (ok2, fail2)
    assert len(list(out.glob("*"))) == 6, "重名应加后缀而不是覆盖"
    print("重名自动加后缀、不覆盖 ✓")

print("=== 4) 按日期分文件夹 ===")
with tempfile.TemporaryDirectory() as td:
    out = Path(td) / "bydate"
    ok, fail = cp.save_items(imgs[:6], out, TDATA, "", by_date=True)
    assert fail == 0 and ok == 6, (ok, fail)
    subs = [p.name for p in out.iterdir() if p.is_dir()]
    import re
    assert subs and all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) for s in subs), subs
    print(f"按日期分目录 → {subs} ✓")

print("=== 5) 预览解密 ===")
import shutil
pv = cp.open_preview(imgs[0], TDATA, "")
assert Path(pv).exists() and Path(pv).stat().st_size > 0
print(f"预览文件 {Path(pv).name} 大小 {Path(pv).stat().st_size} ✓")
shutil.rmtree(cp.PREVIEW_DIR, ignore_errors=True)

print("=== 6) 索引复用（第二次应几乎全走缓存）===")
t0 = time.time()
entries2, stats2 = cp.load_cache(TDATA, "")
elapsed2 = time.time() - t0
print(f"第二次：媒体 {stats2['ok']}（复用 {stats2['reused']}），耗时 {elapsed2:.2f}s")
# 注意：Telegram 可能在后台运行并持续写入缓存，两次总数允许小幅增长，
# 但复用数必须覆盖绝大多数（这里断言复用占比 > 80%）。
assert stats2["reused"] > stats2["ok"] * 0.8, \
    f"索引复用率过低：{stats2['reused']}/{stats2['ok']}"
assert stats2["ok"] >= stats["ok"] - 50, "两次结果差异过大"
assert elapsed2 < elapsed, "第二次没有更快"
print(f"复用率 {stats2['reused']/stats2['ok']*100:.1f}%，加速比 {elapsed/max(elapsed2,0.01):.1f}x ✓")

print("=== 7) 只读验证：确认 tdata 未被修改 ===")
kd = TDATA / "user_data" / "cache" / "1" / "binlog"
before = kd.stat().st_mtime if kd.exists() else None
cp.load_cache(TDATA, "")
after = kd.stat().st_mtime if kd.exists() else None
assert before == after, "binlog 被改动了！"
print("tdata 内文件修改时间未变，确认只读 ✓")

print("\n缓存版功能自检全部通过 ✅")
