"""ui_kit 的单元测试：路径工具（不依赖真实 Tk 与剪贴板）。"""
from __future__ import annotations

from pathlib import Path

import ui_kit


def test_unique_path_adds_suffix(tmp_path: Path):
    """重名时追加 (1)(2)…，绝不覆盖已有文件。"""
    target = tmp_path / "a.jpg"
    assert ui_kit.unique_path(target) == target
    target.write_bytes(b"x")
    assert ui_kit.unique_path(target).name == "a (1).jpg"