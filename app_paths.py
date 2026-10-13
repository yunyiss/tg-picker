"""统一的「数据目录」解析：便携模式 / 用户目录模式（跨平台）。

分工：
    - **只读资源**（``locales/``、``_vendor/``、自带的 ffmpeg）随程序本身走；
    - **运行时产物**（缩略图、索引、配置、语言偏好、日志、会话、默认导出目录）
      全部落在**数据目录**下——这样用户删掉工具目录即可清除全部痕迹。

两种模式（首次启动由用户选定，见 ``firstrun``）：
    - **便携**：``<程序目录>/data``（默认推荐；删程序即删数据，适合"用完即删"）；
    - **用户目录**：Windows ``%APPDATA%/tg-picker``、macOS
      ``~/Library/Application Support/tg-picker``、Linux ``~/.local/share/tg-picker``
      （移动/升级程序不丢配置）。

选择记录在**程序目录**下的 ``app_location.json``（随手删程序即清除）；若程序目录不可写，
便携模式自动退回用户目录。测试/CI 可用环境变量 ``TG_PICKER_DATA_DIR`` 直接指定。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_NAME = "tg-picker"
MARKER_NAME = "app_location.json"
PORTABLE_SUBDIR = "data"
ENV_VAR = "TG_PICKER_DATA_DIR"

PORTABLE = "portable"
USERDIR = "userdir"

_MODE: str | None = None        # "portable" / "userdir" / "env"；None=未解析
_DIR: Path | None = None


def program_dir() -> Path:
    """程序所在目录：源码运行时=本项目根；冻结（PyInstaller）时=exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def portable_dir() -> Path:
    """便携模式的数据目录（程序旁的 ``data/``）。"""
    return program_dir() / PORTABLE_SUBDIR


def user_data_dir() -> Path:
    """跨平台的用户数据目录。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def marker_path() -> Path:
    """记录选择的文件（放在程序目录内，删程序即清除）。"""
    return program_dir() / MARKER_NAME


def _writable(parent: Path) -> bool:
    """判断某目录是否可写（写一个探针文件再删）。"""
    try:
        parent.mkdir(parents=True, exist_ok=True)
        probe = parent / ".tgp_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def saved_mode() -> str | None:
    """读取已保存的选择；无/不可读/非法则返回 None。"""
    try:
        data = json.loads(marker_path().read_text(encoding="utf-8"))
    except Exception:
        return None
    mode = str(data.get("mode", "")) if isinstance(data, dict) else ""
    return mode if mode in (PORTABLE, USERDIR) else None


def _effective_dir(mode: str) -> Path:
    """按模式给出实际目录；便携不可写时退回用户目录。"""
    if mode == PORTABLE and _writable(program_dir()):
        return portable_dir()
    return user_data_dir()


def set_mode(mode: str, persist: bool = True) -> Path:
    """设定模式（可选落盘），返回实际生效的数据目录。"""
    global _MODE, _DIR
    mode = mode if mode in (PORTABLE, USERDIR) else PORTABLE
    _MODE = mode
    _DIR = _effective_dir(mode)
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if persist:
        try:
            marker_path().write_text(
                json.dumps({"mode": mode}, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError:
            pass
    return _DIR


def ensure_resolved(default: str = PORTABLE) -> Path:
    """启动时解析数据目录：环境变量 > 已保存选择 > 默认（便携）。"""
    global _MODE, _DIR
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        _MODE = "env"
        _DIR = Path(env)
        try:
            _DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return _DIR
    return set_mode(saved_mode() or default, persist=False)


def data_dir() -> Path:
    """返回当前数据目录（尚未解析时按默认解析一次）。"""
    if _DIR is None:
        ensure_resolved()
    assert _DIR is not None
    return _DIR


def needs_choice() -> bool:
    """是否需要在首次启动时询问（无环境变量覆盖、且尚无保存的选择）。"""
    if os.environ.get(ENV_VAR, "").strip():
        return False
    return saved_mode() is None


def is_portable() -> bool:
    """当前是否真正落在便携目录（便携模式且可写）。"""
    return _MODE == PORTABLE and data_dir() == portable_dir()
