"""与界面无关的通用工具与共享常量。

放在独立的**叶子模块**里（只依赖 ``i18n``，不依赖任何 GUI，也不依赖 tdata
解密模块），这样两个入口都能安全复用：

- ``cache_source`` / ``cache_picker`` 需要文件与数值格式化；
- ``tg_picker`` 刻意不与 ``cache_source`` 耦合（否则会被拖入 tdata 解密与
  ``_vendor`` 依赖链），所以工具函数不能塞进 ``cache_source``。

注意：两个 GUI 的**配色与缩略图边长故意不统一**（缓存挑选器用 ``#1b1b1f`` 深灰系
+ 140px，在线挑选器用 ``#1e1e1e`` + 150px），这里只共享确实相同的东西（字体）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from i18n import t

# 界面字体：两个 GUI 原先把同一字面量各写一遍，这里收敛为一处
FONT = "Microsoft YaHei UI"


def human_size(n: int | None) -> str:
    """把字节数格式化成可读文本（如 ``1.5 MB``）；falsy 值返回 ``-``。"""
    if not n:
        return "-"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def unique_path(path: Path) -> Path:
    """目标已存在时追加 ``(1)``/``(2)``… 后缀，绝不覆盖已有文件。"""
    path = Path(path)
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 100000):
        cand = path.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
    raise RuntimeError(t("common.err_name_conflict", name=path.name))


def open_path(path: str | Path) -> subprocess.Popen:
    """用系统默认程序打开文件或目录 —— **在独立的一次性子进程里执行**。

    为什么不在本进程里直接 ``os.startfile``：

    - 实测在某些机器的 shell 环境下，这一调用会以 ``ucrtbase.dll`` 的
      ``__fastfail``（异常码 0xC0000409）**直接终止整个进程**（第三方 shell 扩展 /
      杀软钩子 / Store 应用激活），Python 侧捕获不到，也不会有 traceback；
    - 它还是**同步**调用，ShellExecute 被拖住时 Tk 主线程会一直“无响应”。

    隔离到子进程后，崩溃与卡顿都不会波及界面。返回 ``Popen``，调用方若要判断
    “到底起来了没有”，可在**后台线程**里 ``wait(timeout=...)``，切勿阻塞界面。
    """
    target = str(path)
    # 子进程里再决定用哪种方式打开（与主进程同样的平台判断）
    code = (
        "import os,subprocess,sys\n"
        "p = sys.argv[1]\n"
        "if sys.platform == 'win32':\n"
        "    os.startfile(p)\n"
        "elif sys.platform == 'darwin':\n"
        "    subprocess.run(['open', p])\n"
        "else:\n"
        "    subprocess.run(['xdg-open', p])\n"
    )
    kwargs: dict = {}
    if sys.platform == "win32":
        # 不能弹控制台窗口：GUI 由 pythonw 启动，子进程若继承控制台会闪出黑窗
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        if hasattr(subprocess, "STARTUPINFO"):
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
            kwargs["startupinfo"] = si
    return subprocess.Popen([sys.executable, "-c", code, target], **kwargs)


def load_config(path: Path) -> dict:
    """读取 JSON 配置。

    文件不存在时静默返回空 dict；文件损坏时告警到 stderr 后同样返回空 dict
    ——配置不可用不应让整个工具起不来。
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"[config] 读取 {path} 失败：{exc}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def save_config(path: Path, data: dict, merge: bool = True,
                mode: int | None = None) -> None:
    """写回 JSON 配置。

    ``merge=True``（默认）时先读旧内容再合并，避免覆盖其它键；``mode`` 非空时
    写后设置文件权限（在线挑选器的配置含凭据，用 ``0o600``）。写入失败只告警，
    不影响正在进行的操作。
    """
    path = Path(path)
    try:
        merged = load_config(path) if merge else {}
        merged.update(data)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        if mode is not None:
            path.chmod(mode)
    except Exception as exc:
        print(f"[config] 写入 {path} 失败：{exc}", file=sys.stderr)