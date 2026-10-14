"""与界面无关的通用工具与共享常量。

放在独立的**叶子模块**里（只依赖 ``i18n``，不依赖任何 GUI，也不依赖 tdata
解密模块），这样两个入口都能安全复用：

- ``cache_source`` / ``cache_picker`` 需要文件与数值格式化；
- ``tg_picker`` 刻意不与 ``cache_source`` 耦合（否则会被拖入 tdata 解密与
  ``_vendor`` 依赖链），所以工具函数不能塞进 ``cache_source``。

配色：两个 GUI 共用 ``ui_style`` 里的深色主题色板（原先各自写死、观感不一致，
已收敛为一处）；缩略图边长仍各自决定（缓存挑选器 140px、在线挑选器 150px）。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
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


def rounded_pts(x1: float, y1: float, x2: float, y2: float, r: int,
                seg: int = 6) -> list[float]:
    """生成圆角矩形轮廓的点列（四角按 ``seg`` 段采样圆弧），供 Canvas 多边形使用。

    Frame 与 Canvas 的直角 rectangle 都画不出圆角；两个挑选器的卡片因此都改用
    这个点列 + ``create_polygon(smooth=False)`` 绘制，圆角半径才真实、外观才一致。

    注意：早期版本每个角只给 2 个锚点、依赖 ``smooth=True`` 插值，实测视觉半径
    只有 3-4px（远小于设定值）；故这里按 ``seg`` 段采样圆弧、以 ``smooth=False``
    直接连线。
    """
    import math

    pts: list[float] = []
    corners = (
        (x2 - r, y1 + r, -math.pi / 2),   # 右上
        (x2 - r, y2 - r, 0.0),            # 右下
        (x1 + r, y2 - r, math.pi / 2),    # 左下
        (x1 + r, y1 + r, math.pi),        # 左上
    )
    for cx, cy, start in corners:
        for i in range(seg + 1):
            a = start + (math.pi / 2) * i / seg
            pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
    return pts


# ---------------------------------------------------------------------------
# 硬件检测与并发档位推荐（移植自上游 tg-scoop 的 hardware.py）
#
# 检测是**提示性质**：任何异常都按"未知"处理，永不阻断主流程；仅标准库。
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HardwareInfo:
    """检测结果；未知字段为 None。"""

    cores: int | None
    ram_gb: float | None


def _detect_ram_gb() -> float | None:
    """读取物理内存总量（GB），任何失败返回 None。"""
    try:
        if sys.platform == "win32":
            import ctypes

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return None
            return stat.ullTotalPhys / (1 << 30)
        if sys.platform == "darwin":
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            return int(out.stdout.strip()) / (1 << 30)
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / (1 << 20)  # kB -> GB
        return None
    except Exception:  # noqa: BLE001 —— 检测是提示性质，任何异常都按未知处理
        return None


def detect_hardware() -> HardwareInfo:
    """检测 CPU 核数与内存；失败字段为 None，永不抛异常。"""
    try:
        cores = os.cpu_count()
    except Exception:  # noqa: BLE001 —— 同上，检测失败按未知处理
        cores = None
    return HardwareInfo(cores=cores, ram_gb=_detect_ram_gb())


def recommended_workers(info: HardwareInfo) -> int:
    """推荐并发线程数：min(cores-1, 8)；低内存（<4GB）收敛到 2；保底 1。"""
    if not info.cores or info.cores <= 1:
        return 1
    workers = max(1, min(info.cores - 1, 8))
    if info.ram_gb is not None and info.ram_gb < 4:
        workers = min(2, workers)
    return workers


def gear_workers(key: str, info: HardwareInfo, recommended: int | None = None) -> int:
    """性能档位内部键 → 并发线程数（保守 1 / 均衡推荐 / 极速满核）。

    **低配钳制**（避免线程数过高导致运行异常）：核数未知 → 1；内存 <4GB 时
    「极速」收敛到推荐值，不跑满核。
    """
    rec = recommended if recommended is not None else recommended_workers(info)
    if key == "conservative":
        return 1
    if key == "turbo":
        if not info.cores:
            return 1
        if info.ram_gb is not None and info.ram_gb < 4:
            return max(1, rec)
        return max(1, info.cores)
    return max(1, rec)


# ---------------------------------------------------------------------------
# 系统集成：用文件管理器打开目录
# ---------------------------------------------------------------------------
def open_folder(path: Path) -> bool:
    """用系统文件管理器打开目录；成功返回 ``True``。

    失败（本机对 shell open 受限、无 ``xdg-open`` 等）返回 ``False``，
    由调用方决定兜底方式（缓存挑选器会回退为把绝对路径写入剪贴板）。

    打开目录是**有意的用户动作**，故直接交给系统外壳执行。
    """
    path = Path(path)
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# 日志：写入文件（带轮转），并在无控制台时接管 stdout / stderr
# ---------------------------------------------------------------------------
LOG_NAME = "tg_picker"              # 三个入口共用同一个日志器
_LOG_MAX_BYTES = 2 * 1024 * 1024    # 单个日志文件上限 2 MiB
_LOG_BACKUPS = 3                    # 保留的历史文件数


class _LogWriter:
    """把 ``print()`` / traceback 的输出喂给 logging（pythonw 无控制台时使用）。

    逐行转发：stdout 记为 INFO、stderr 记为 ERROR（traceback 走 stderr）。
    """

    def __init__(self, logger: logging.Logger, level: int) -> None:
        self._logger = logger
        self._level = level
        self._buf = ""

    def write(self, text: str) -> int:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line:
                self._logger.log(self._level, line)
        return len(text)

    def flush(self) -> None:
        if self._buf:
            self._logger.log(self._level, self._buf)
            self._buf = ""


def enable_file_log(log_path: Path) -> Path | None:
    """把日志写入文件（带轮转），并在无控制台时接管 ``stdout`` / ``stderr``。

    为什么放在 Python 侧、而不是让 ``.cmd`` 做 ``>log`` 重定向：

    - ``pythonw`` 启动的进程没有控制台，``sys.stdout`` / ``sys.stderr`` 都是 ``None``，
      任何 traceback 与 ``print(..., file=sys.stderr)`` 都会**无声丢失**；
    - 批处理的重定向依赖句柄继承与引号，在 Windows 上很容易出岔子——本项目刚因为
      ``.cmd`` 里的非 ASCII 踩过坑，这里不再让 shell 承担这类职责。

    只在**没有控制台**时接管（``sys.stderr is None``）；从终端用 ``python.exe`` 运行时
    保留控制台输出，方便开发调试。用 :class:`~logging.handlers.RotatingFileHandler`
    做轮转，避免日志无限增长。返回日志文件路径；未接管时返回 ``None``。
    """
    if sys.stderr is not None:
        return None
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(LOG_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # 同一进程内重复调用时不叠加 handler：先关掉旧文件再开新的
    for old in list(logger.handlers):
        logger.removeHandler(old)
        try:
            old.close()
        except Exception:
            pass
    handler = RotatingFileHandler(
        log_path, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS,
        encoding="utf-8", errors="replace",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)

    sys.stdout = _LogWriter(logger, logging.INFO)
    sys.stderr = _LogWriter(logger, logging.ERROR)
    return log_path


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

    **注意 ``mode`` 只在 POSIX 上构成访问控制**：Windows 的 ``os.chmod`` 只能切换
    只读位，无法限制其它用户读取。也就是说 Windows 下"凭据受 0600 保护"并不成立，
    实际防护取决于数据目录自身的 ACL（README「安全边界」已写明）。
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