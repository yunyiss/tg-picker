"""ui_kit 的单元测试：路径工具与「无控制台时的日志接管」。"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

import ui_kit


def _close_log_handlers() -> None:
    """关掉共用日志器上的所有 handler（释放日志文件，避免占用 tmp 目录）。"""
    logger = logging.getLogger(ui_kit.LOG_NAME)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()


@pytest.fixture(autouse=True)
def _clean_log_handlers():
    """每个用例前后都清空日志 handler（日志用例会往文件写并保持打开）。"""
    _close_log_handlers()
    yield
    _close_log_handlers()


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


def test_enable_file_log_rotates(tmp_path, monkeypatch):
    """日志超过单文件上限时轮转为 .1（避免日志无限增长）。"""
    monkeypatch.setattr(ui_kit.sys, "stdout", None)
    monkeypatch.setattr(ui_kit.sys, "stderr", None)
    monkeypatch.setattr(ui_kit, "_LOG_MAX_BYTES", 200)
    monkeypatch.setattr(ui_kit, "_LOG_BACKUPS", 2)
    log = ui_kit.enable_file_log(tmp_path / "logs" / "r.log")
    assert log is not None
    for _ in range(60):
        print("x" * 40)
    assert log.exists()
    assert (tmp_path / "logs" / "r.log.1").exists(), "超过上限后应轮转出 .1"


def test_detect_hardware_never_raises_and_recommends_sane_workers():
    """硬件检测是提示性质：不抛异常、字段可未知；推荐线程数落在 1..8。"""
    info = ui_kit.detect_hardware()
    assert info.cores is None or info.cores >= 1
    assert info.ram_gb is None or info.ram_gb > 0
    assert 1 <= ui_kit.recommended_workers(info) <= 8


def test_recommended_workers_rules():
    """搬运自上游 recommended_jobs 的规则：min(cores-1,8)、<4GB 收敛到 2、保底 1。"""
    H = ui_kit.HardwareInfo
    assert ui_kit.recommended_workers(H(cores=None, ram_gb=None)) == 1
    assert ui_kit.recommended_workers(H(cores=1, ram_gb=8.0)) == 1
    assert ui_kit.recommended_workers(H(cores=2, ram_gb=8.0)) == 1
    assert ui_kit.recommended_workers(H(cores=16, ram_gb=32.0)) == 8
    assert ui_kit.recommended_workers(H(cores=16, ram_gb=2.0)) == 2


def test_gear_workers_rules_and_low_spec_clamp():
    """档位线程数：保守 1 / 均衡推荐 / 极速满核；低配（<4GB、核数未知）被钳制。"""
    H = ui_kit.HardwareInfo
    hi = H(cores=16, ram_gb=32.0)
    assert ui_kit.gear_workers("conservative", hi) == 1
    assert ui_kit.gear_workers("balanced", hi) == 8      # min(16-1, 8)
    assert ui_kit.gear_workers("turbo", hi) == 16        # 满核
    # 低内存设备不允许"极速"跑满核
    lo = H(cores=16, ram_gb=2.0)
    assert ui_kit.gear_workers("turbo", lo) == ui_kit.recommended_workers(lo) == 2
    # 核数未知：任何档位都保底 1，避免线程数过高
    unk = H(cores=None, ram_gb=None)
    assert ui_kit.gear_workers("conservative", unk) == 1
    assert ui_kit.gear_workers("balanced", unk) == 1
    assert ui_kit.gear_workers("turbo", unk) == 1