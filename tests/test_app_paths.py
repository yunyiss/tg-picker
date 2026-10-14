"""app_paths 单元测试：数据目录解析、模式落盘、便携不可写回退、环境变量覆盖。

这些用例会改模块级状态（``_MODE`` / ``_DIR``），用 autouse fixture 前后复位，
避免影响其它用例（它们依赖 conftest 设的 ``TG_PICKER_DATA_DIR``）。
"""
from __future__ import annotations

import pytest

import app_paths


@pytest.fixture(autouse=True)
def _reset_state():
    old = (app_paths._MODE, app_paths._DIR)
    app_paths._MODE = None
    app_paths._DIR = None
    yield
    app_paths._MODE, app_paths._DIR = old


def test_user_data_dir_is_absolute():
    """用户数据目录必须是绝对路径（跨平台三选一）。"""
    assert app_paths.user_data_dir().is_absolute()


def test_portable_mode_writes_marker_and_uses_program_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("TG_PICKER_DATA_DIR", raising=False)
    monkeypatch.setattr(app_paths, "program_dir", lambda: tmp_path)

    d = app_paths.set_mode(app_paths.PORTABLE)
    assert d == tmp_path / "data"
    assert app_paths.marker_path() == tmp_path / "app_location.json"
    assert app_paths.marker_path().is_file(), "选择应落盘到程序目录旁的 marker"
    assert app_paths.saved_mode() == app_paths.PORTABLE
    assert app_paths.is_portable() is True


def test_userdir_mode_uses_user_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("TG_PICKER_DATA_DIR", raising=False)
    monkeypatch.setattr(app_paths, "program_dir", lambda: tmp_path)
    monkeypatch.setattr(app_paths, "user_data_dir", lambda: tmp_path / "ud")

    d = app_paths.set_mode(app_paths.USERDIR)
    assert d == tmp_path / "ud"
    assert app_paths.saved_mode() == app_paths.USERDIR
    assert app_paths.is_portable() is False


def test_portable_falls_back_when_program_dir_not_writable(tmp_path, monkeypatch):
    """程序目录不可写时，便携模式自动退回用户目录（不报错）。"""
    monkeypatch.delenv("TG_PICKER_DATA_DIR", raising=False)
    monkeypatch.setattr(app_paths, "program_dir", lambda: tmp_path)
    monkeypatch.setattr(app_paths, "user_data_dir", lambda: tmp_path / "ud")
    monkeypatch.setattr(app_paths, "_writable", lambda p: False)

    assert app_paths.set_mode(app_paths.PORTABLE) == tmp_path / "ud"


def test_env_override_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("TG_PICKER_DATA_DIR", str(tmp_path / "envd"))
    assert app_paths.data_dir() == tmp_path / "envd"
    assert app_paths.needs_choice() is False, "有环境变量覆盖时不应再询问"


def test_needs_choice_true_without_marker(tmp_path, monkeypatch):
    monkeypatch.delenv("TG_PICKER_DATA_DIR", raising=False)
    monkeypatch.setattr(app_paths, "program_dir", lambda: tmp_path)
    assert app_paths.needs_choice() is True


def test_saved_mode_ignores_invalid_content(tmp_path, monkeypatch):
    monkeypatch.delenv("TG_PICKER_DATA_DIR", raising=False)
    monkeypatch.setattr(app_paths, "program_dir", lambda: tmp_path)
    (tmp_path / "app_location.json").write_text("not json", encoding="utf-8")
    assert app_paths.saved_mode() is None


def test_export_dir_helper_lives_under_data_dir(tmp_path, monkeypatch):
    """export_dir() = 数据目录 / exports（固定名，不走 i18n）。"""
    monkeypatch.setenv("TG_PICKER_DATA_DIR", str(tmp_path / "d"))
    assert app_paths.export_dir() == tmp_path / "d" / app_paths.EXPORT_SUBDIR


def test_both_modes_share_default_export_dir():
    """API 与缓存模式解析出的默认导出目录必须**同一个**。"""
    import cache_picker as cp
    import tg_picker as tp

    assert cp.EXPORT_DIR == tp.DEFAULT_OUT, "两个模式的默认保存目录必须一致"
    assert cp.EXPORT_DIR.name == app_paths.EXPORT_SUBDIR
    assert cp.EXPORT_DIR.parent == app_paths.data_dir()
