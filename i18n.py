"""轻量国际化（i18n）。

语言资源放在 ``locales/<lang>.json``，使用**点号命名空间的扁平键**
（如 ``"cache.setup.browse"``）。默认语言为中文（zh）；切换语言时会通知
已注册的监听者，界面据此实时刷新。

新增一种语言：
    1. 复制 ``locales/zh.json`` 为 ``locales/<code>.json``，逐条翻译（**键不要改**）；
    2. 在下面的 ``LANGUAGES`` 里登记 ``<code>`` 与它的显示名；
    3. 运行 ``_selftest_i18n.py`` 校验键集合与占位符一致。

本模块不依赖任何第三方库。

线程安全：语言资源字典与当前语言都是"先在局部构造/读好、再单次赋值发布"，
因此后台线程随时调用 :func:`t` 都不会读到半更新状态；``t()`` 本身只做字典
查找，永不抛错（缺失时逐级兜底，最后退回键名本身）。
"""
from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import app_paths

HERE = Path(__file__).resolve().parent
LOCALES_DIR = HERE / "locales"


def _config_path() -> Path:
    """语言偏好文件（在数据目录下，随数据目录走：便携=程序旁 data/，否则用户目录）。"""
    return app_paths.data_dir() / "language.json"


DEFAULT = "zh"
# 语言码 → 切换按钮上的显示名（显示名本身不随语言变化）
LANGUAGES: dict[str, str] = {"zh": "中文", "en": "English"}

_catalog: dict[str, dict[str, str]] = {}          # lang -> {key: text}
_current: str = DEFAULT
_listeners: list[Callable[[], None]] = []
_warned: set[str] = set()


def available() -> dict[str, str]:
    """返回 {语言码: 显示名}。"""
    return dict(LANGUAGES)


def get_language() -> str:
    """返回当前语言码。"""
    return _current


def normalize(lang: str) -> str:
    """把未知/空语言码规范化为已登记的语言（回退默认）。"""
    code = (lang or "").strip().lower()
    return code if code in LANGUAGES else DEFAULT


def _load_catalog(lang: str) -> dict[str, str]:
    """读取某个语言的资源文件；失败返回空 dict（只告警一次）。"""
    path = LOCALES_DIR / f"{lang}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        if lang not in _warned:
            _warned.add(lang)
            print(f"[i18n] 无法载入语言资源 {path}: {exc}", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _ensure(lang: str) -> None:
    """确保某语言已载入（整体构造后单次赋值发布）。"""
    if lang not in _catalog:
        _catalog[lang] = _load_catalog(lang)


def set_language(lang: str, notify: bool = True) -> str:
    """切换语言并（可选）通知监听者；返回生效后的语言码。"""
    global _current
    code = normalize(lang)
    _ensure(DEFAULT)
    if code != DEFAULT:
        _ensure(code)
    # 目标语言字典已就绪，再单次发布，避免并发读到半更新状态
    _current = code
    if notify:
        for cb in list(_listeners):
            try:
                cb()
            except Exception:
                pass
    return code


def init() -> str:
    """启动时调用：读共享语言偏好并据此设定语言（**不触发通知**）。

    应在构建界面之前调用，这样首次绘制的就是正确语言。
    """
    lang = ""
    try:
        cfg = json.loads(_config_path().read_text(encoding="utf-8"))
        if isinstance(cfg, dict):
            lang = str(cfg.get("lang", ""))
    except Exception:
        lang = ""
    return set_language(lang, notify=False)


def save_language(lang: str) -> None:
    """把语言偏好写入共享配置文件（失败静默忽略）。"""
    try:
        _config_path().write_text(
            json.dumps({"lang": normalize(lang)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def on_change(cb: Callable[[], None]) -> None:
    """注册语言切换回调（界面借此实时刷新）。重复注册同一回调只保留一份。"""
    if cb not in _listeners:
        _listeners.append(cb)


def off_change(cb: Callable[[], None]) -> None:
    """注销语言切换回调。

    窗口关闭时应调用，否则已销毁的界面仍留在监听表里（其 retranslate 会不断
    抛 TclError 并被 :func:`set_language` 吞掉，属于隐性泄漏）。
    """
    try:
        _listeners.remove(cb)
    except ValueError:
        pass


def t(key: str, **kwargs) -> str:
    """取当前语言下的文案。

    查找顺序：当前语言 → 默认语言(zh) → ``key`` 本身。**永不抛错**。
    传入 ``kwargs`` 时做 ``str.format``；格式化失败则原样返回模板。
    """
    text = _catalog.get(_current, {}).get(key)
    if text is None:
        text = _catalog.get(DEFAULT, {}).get(key)
    if text is None:
        return key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text
    return text