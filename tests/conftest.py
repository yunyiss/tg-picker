"""pytest 公共装置。

- 把仓库根加入导入路径（取代原先每个测试脚本各写一遍 ``sys.path.insert``）；
- 需要真实 tdata 的用例用 :func:`tdata_dir` fixture，找不到即 **skip**（不算失败）；
- 需要显示器的 GUI 用例标记 ``gui``，并在创建 Tk 失败时自动 skip。
"""
from __future__ import annotations

import os
import sys
import tempfile
import tkinter as tk
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 测试期间把所有运行时产物（缩略图/索引/配置/语言/日志）写到临时目录，
# 绝不污染项目目录；必须在导入被测模块之前设置。
os.environ.setdefault("TG_PICKER_DATA_DIR", tempfile.mkdtemp(prefix="tgp-tests-"))


def _find_tdata() -> Path | None:
    """按 环境变量 TDATA_PATH → Windows 默认位置 → 常见盘符 的顺序找 tdata。"""
    env = os.environ.get("TDATA_PATH", "").strip()
    if env:
        p = Path(env)
        if p.is_dir():
            return p
    cands: list[Path] = []
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


@pytest.fixture(scope="session")
def tdata_dir() -> Path:
    """真实的 tdata 目录；找不到则跳过相关用例（可用 TDATA_PATH 指定）。"""
    found = _find_tdata()
    if found is None:
        pytest.skip("未找到 tdata 目录（可用环境变量 TDATA_PATH 指定）")
    return found


@pytest.fixture(scope="session")
def _tk_session_root():
    """整个测试会话只创建一个 Tk 解释器。

    同一进程里反复 ``tk.Tk()``（各自销毁再重建）会偶发 Tcl 初始化失败
    （"tk wasn't installed properly"，实为多次 interpreter 共存导致的报错），
    表现为 GUI 用例随机 skip。这里只建一次，用例改用 Toplevel。
    """
    try:
        root = tk.Tk()
    except tk.TclError as exc:          # 无 X/显示服务
        pytest.skip(f"当前环境没有可用的显示器：{exc}")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


@pytest.fixture()
def tk_root(_tk_session_root):
    """每个 GUI 用例一个独立的顶层窗口（Toplevel）。

    必须是真实映射的窗口：GUI 用例依赖真实画布尺寸来算列数与可见图元数，
    所以不能 withdraw。
    """
    win = tk.Toplevel(_tk_session_root)
    yield win
    try:
        win.destroy()
    except tk.TclError:
        pass