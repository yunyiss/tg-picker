"""firstrun 测试：首次启动的「数据目录」选择（对话框与落盘）。

改模块级状态的用例用 autouse fixture 前后复位，避免影响其它用例。
"""
from __future__ import annotations

import tkinter as tk

import pytest

import app_paths
import firstrun


@pytest.fixture(autouse=True)
def _reset_state():
    old = (app_paths._MODE, app_paths._DIR)
    app_paths._MODE = None
    app_paths._DIR = None
    yield
    app_paths._MODE, app_paths._DIR = old


@pytest.mark.gui
def test_ensure_choice_persists_selection(tmp_path, monkeypatch, tk_root):
    """首次启动：用户的选择被落盘，数据目录据此解析。"""
    monkeypatch.delenv("TG_PICKER_DATA_DIR", raising=False)
    monkeypatch.setattr(app_paths, "program_dir", lambda: tmp_path)
    monkeypatch.setattr(app_paths, "user_data_dir", lambda: tmp_path / "ud")
    monkeypatch.setattr(firstrun, "_ask", lambda root: app_paths.USERDIR)

    firstrun.ensure_choice(tk_root)
    assert app_paths.saved_mode() == app_paths.USERDIR
    assert app_paths.data_dir() == tmp_path / "ud"


@pytest.mark.gui
def test_ensure_choice_defaults_to_portable_when_closed(tmp_path, monkeypatch, tk_root):
    """对话框被关闭（返回 None）时按便携处理。"""
    monkeypatch.delenv("TG_PICKER_DATA_DIR", raising=False)
    monkeypatch.setattr(app_paths, "program_dir", lambda: tmp_path)
    monkeypatch.setattr(firstrun, "_ask", lambda root: None)

    firstrun.ensure_choice(tk_root)
    assert app_paths.saved_mode() == app_paths.PORTABLE
    assert app_paths.data_dir() == tmp_path / "data"


@pytest.mark.gui
def test_ensure_choice_skips_dialog_when_choice_exists(tmp_path, monkeypatch, tk_root):
    """已有保存的选择时不再弹窗。"""
    monkeypatch.delenv("TG_PICKER_DATA_DIR", raising=False)
    monkeypatch.setattr(app_paths, "program_dir", lambda: tmp_path)
    (tmp_path / "app_location.json").write_text('{"mode": "portable"}', encoding="utf-8")
    asked: list[int] = []
    monkeypatch.setattr(firstrun, "_ask", lambda root: asked.append(1))

    firstrun.ensure_choice(tk_root)
    assert asked == [], "已有选择时不应再询问"
    assert app_paths.data_dir() == tmp_path / "data"


@pytest.mark.gui
def test_ask_builds_dialog_and_returns_none_on_close(tk_root):
    """_ask 能建出模态对话框；被直接关闭时返回 None。"""
    def close_soon() -> None:
        for w in tk_root.winfo_children():
            if isinstance(w, tk.Toplevel):
                w.destroy()

    tk_root.after(300, close_soon)
    assert firstrun._ask(tk_root) is None
