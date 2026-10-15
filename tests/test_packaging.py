"""打包脚本的结构自检（不需要显示器，也不用真的跑 PyInstaller）。

发布包是"独立可执行包"，里面**内置了裁剪版 ffmpeg（LGPL）**——所以"随包带上
LICENSE 与第三方声明"不是可选项，而是对外分发的前提。这里用源码级断言把它钉住：
`packaging/build_app.py` 里必须有这两个 ``--add-data``，且声明文件确实存在并写明
了 ffmpeg 的许可与源码获取方式。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_build_bundles_license_and_third_party_notices():
    """回归：发布包必须随附 LICENSE 与第三方声明（内置 ffmpeg 是 LGPL）。"""
    script = (ROOT / "packaging" / "build_app.py").read_text(encoding="utf-8")
    assert "'LICENSE'" in script, "build_app.py 应把 LICENSE 打进包里"
    assert "'THIRD-PARTY-NOTICES.txt'" in script, "build_app.py 应把第三方声明打进包里"


def test_third_party_notices_covers_bundled_ffmpeg():
    """声明文件要真的能支撑合规：写明 ffmpeg 的许可、源码与重建方式。"""
    notice = (ROOT / "THIRD-PARTY-NOTICES.txt").read_text(encoding="utf-8")
    assert "LGPL" in notice, "应写明 ffmpeg 的 LGPL 许可"
    assert "ffmpeg.org" in notice, "应给出 ffmpeg 源码获取地址"
    assert "build_ffmpeg.sh" in notice, "应说明如何用仓库里的配方重建同一份二进制"
    assert (ROOT / "LICENSE").is_file(), "仓库根应有 LICENSE（会随包分发）"


def test_build_collects_pillow_tk_bridge():
    """回归（Linux/mac 打包后缩略图全不显示）：必须显式收集 PIL._tkinter_finder。

    Pillow 的 ``ImageTk`` 是在 ``PhotoImage()`` 里**惰性**导入这个模块的；PyInstaller
    的 hook 在 Linux 上不会自动带上它，于是打包后每次 PIL→Tk 转换都抛
    ``ModuleNotFoundError``、被上层静默吞掉 → 缩略图与视频占位全部显示不出来
    （Windows 因 hook 差异会顺带收集，故未暴露）。这里用源码级断言把它钉住。
    """
    script = (ROOT / "packaging" / "build_app.py").read_text(encoding="utf-8")
    assert '"PIL._tkinter_finder"' in script, "build_app.py 必须显式收集 PIL._tkinter_finder"
