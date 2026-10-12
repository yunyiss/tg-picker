"""ui_kit 的单元测试：路径工具与「无控制台时的日志接管」。"""
from __future__ import annotations

from pathlib import Path

import ui_kit


def test_unique_path_adds_suffix(tmp_path: Path):
    """重名时追加 (1)(2)…，绝不覆盖已有文件。"""
    target = tmp_path / "a.jpg"
    assert ui_kit.unique_path(target) == target
    target.write_bytes(b"x")
    assert ui_kit.unique_path(target).name == "a (1).jpg"


def test_enable_file_log_skips_when_console_exists(tmp_path, monkeypatch):
    """有控制台（python.exe）时不接管，也不建日志文件。"""
    monkeypatch.setattr(ui_kit.sys, "stderr", object())
    assert ui_kit.enable_file_log(tmp_path / "logs" / "a.log") is None
    assert not (tmp_path / "logs").exists()


def test_enable_file_log_redirects_stdout_and_stderr(tmp_path, monkeypatch):
    """pythonw（无控制台，stderr 为 None）时把 stdout/stderr 接到日志文件。"""
    monkeypatch.setattr(ui_kit.sys, "stdout", None)
    monkeypatch.setattr(ui_kit.sys, "stderr", None)
    log = ui_kit.enable_file_log(tmp_path / "logs" / "a.log")
    assert log == tmp_path / "logs" / "a.log"
    print("走 stdout 的一行")
    ui_kit.sys.stderr.write("走 stderr 的一行\n")
    assert log.exists()
    text = log.read_text(encoding="utf-8")
    assert "走 stdout 的一行" in text
    assert "走 stderr 的一行" in text