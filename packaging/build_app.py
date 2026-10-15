#!/usr/bin/env python3
"""用 PyInstaller 把工具打成 **onedir** 发布包（内置自带的裁剪版 ffmpeg）。

    python packaging/build_app.py <ffmpeg 可执行文件路径> [--dist <输出目录>]

为什么 onedir 而非 onefile：onefile 每次启动都要把内容自解压到临时目录，
冷启动明显变慢；onedir 是"一个文件夹（小 exe + 依赖）"，启动快、体积相同。

ffmpeg 由 ``packaging/build_ffmpeg.sh`` 构建（见该脚本里的配方）。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 只在 API 模式/缓存模式下按需导入，必须显式声明，否则 PyInstaller 静态分析会漏
HIDDEN = [
    "cache_picker", "tg_picker", "cache_source",
    "ui_kit", "ui_lang", "ui_style", "i18n", "app_paths", "firstrun",
    "Crypto", "Crypto.Cipher.AES",
]

# 开发/构建期模块，发布包一律排除（不进用户包）
EXCLUDE = [
    "imageio_ffmpeg",                       # 改用自带的裁剪版 ffmpeg
    "pytest", "_pytest", "pluggy", "iniconfig", "pygments",
    "mypy", "mypyc", "ast_serialize", "librt", "pathspec",
    "ruff", "pip", "setuptools", "wheel",
    "PyInstaller", "pefile", "altgraph", "pywin32ctypes",
    # 图片格式裁剪：本工具只处理 jpg/png/gif/webp/bmp，**不含 AVIF**（全仓无
    # avif/heif/AvifImagePlugin 引用）。排除后省约 7.5MB（实测 _avif.pyd 7.47MB），
    # 零功能损失。PIL._avif 是该插件的 C 扩展，一并排除以免误留二进制。
    "PIL.AvifImagePlugin", "PIL._avif",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ffmpeg", help="裁剪版 ffmpeg 可执行文件路径")
    ap.add_argument("--dist", default=str(ROOT / "dist"),
                    help="输出目录（默认 <仓库>/dist）")
    args = ap.parse_args()

    ff = Path(args.ffmpeg).resolve()
    if not ff.is_file():
        print(f"找不到 ffmpeg：{ff}（先跑 packaging/build_ffmpeg.sh）", file=sys.stderr)
        return 1

    sep = ";" if sys.platform == "win32" else ":"
    specpath = ROOT / "build"
    specpath.mkdir(parents=True, exist_ok=True)

    # 说明：设置了 --specpath 后，相对路径会以 spec 文件所在目录为基准解析，因此下面
    # 的源路径一律用绝对路径。另：--noconsole 在 Windows 上是不弹控制台；在 macOS 上
    # 它等价 --windowed，会产出 dist/tg-picker.app（打包成 zip 的步骤按两种名字兼容）。
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onedir", "--noconsole", "--name", "tg-picker",
        "--distpath", args.dist, "--workpath", str(specpath / "pyi"),
        "--specpath", str(specpath),
        "--add-data", f"{ROOT / 'locales'}{sep}locales",
        "--add-data", f"{ROOT / '_vendor'}{sep}_vendor",
        "--add-data", f"{ff}{sep}.",
        # 许可与第三方声明随包分发：内置的裁剪版 ffmpeg 是 LGPL，对外发布时必须
        # 同时给出许可证文本与"如何获取/重建对应源码"的说明（见 THIRD-PARTY-NOTICES.txt）
        "--add-data", f"{ROOT / 'LICENSE'}{sep}.",
        "--add-data", f"{ROOT / 'THIRD-PARTY-NOTICES.txt'}{sep}.",
    ]
    for h in HIDDEN:
        cmd += ["--hidden-import", h]
    for m in EXCLUDE:
        cmd += ["--exclude-module", m]
    cmd.append(str(ROOT / "launcher.py"))

    print(">> " + " ".join(cmd))
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
