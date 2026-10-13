"""i18n 自检：资源键完整性、占位符一致、源码覆盖、切换行为、界面无中文残留。

不需要 tdata，也不需要联网。前四项可直接运行；最后一项需要显示器（标记 gui）。
"""
from __future__ import annotations

import json
import re
import string
from pathlib import Path

import pytest

import i18n

HERE = Path(__file__).resolve().parent.parent
SOURCES = ["launcher.py", "cache_picker.py", "tg_picker.py", "cache_source.py",
           "ui_kit.py", "ui_lang.py"]
# 键名形如 ns.sub.sub：小写字母开头、点分
KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$")
# 源码里的 t("key") 调用
T_CALL_RE = re.compile(r"\bt\(\s*[\"']([A-Za-z][\w.]*)[\"']")
# CJK 及全角标点
CJK_RE = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")
# 运行时拼接的动态键，字面量扫描抓不到，单独枚举校验
DYNAMIC_PREFIX = ("sort.", "kind.", "gear.")
KIND_KEYS = ("image", "video", "file", "all")


def _load(lang: str) -> dict:
    return json.loads((HERE / "locales" / f"{lang}.json").read_text(encoding="utf-8"))


ZH = _load("zh")
EN = _load("en")
# 语言按钮自身文案（如"中文"）在英文模式下仍然是中文，属预期
LANG_NAMES = set(i18n.available().values())


def _placeholders(text: str) -> set[str]:
    """取 str.format 占位符名集合；非法模板返回空集。"""
    try:
        return {name for _, name, _, _ in string.Formatter().parse(text) if name}
    except ValueError:
        return set()


def test_catalog_keys_match():
    """两份语言的键集合必须完全一致。"""
    assert set(ZH) == set(EN), (
        f"en.json 缺少：{sorted(set(ZH) - set(EN))}；"
        f"en.json 多出：{sorted(set(EN) - set(ZH))}"
    )


def test_catalog_values_non_empty_and_placeholders_match():
    """取值非空，且同一键在两语言里的 {} 占位符必须一致。"""
    for lang, cat in (("zh", ZH), ("en", EN)):
        for key, value in cat.items():
            assert isinstance(value, str) and value.strip(), f"{lang}.json 的 {key} 取值为空"
    for key in ZH:
        assert _placeholders(ZH[key]) == _placeholders(EN[key]), (
            f"键 {key} 占位符不一致：zh={sorted(_placeholders(ZH[key]))} "
            f"en={sorted(_placeholders(EN[key]))}"
        )


def test_source_coverage():
    """源码里用到的键都有资源，且资源里的键都被源码引用。"""
    text = "\n".join((HERE / s).read_text(encoding="utf-8") for s in SOURCES)
    # t("...") 里的键必须在两份资源里
    for key in sorted(set(T_CALL_RE.findall(text))):
        assert KEY_RE.match(key), f"疑似键名的字面量不合规范：{key}"
        assert key in ZH, f'源码用了 t("{key}")，但 zh.json 没有该键'
        assert key in EN, f'源码用了 t("{key}")，但 en.json 没有该键'
    # 资源里每个键都应被源码引用（动态键由下面的枚举覆盖）
    unused = [
        key for key in ZH
        if not key.startswith(DYNAMIC_PREFIX)
        and f'"{key}"' not in text and f"'{key}'" not in text
    ]
    assert not unused, f"资源键未被任何源码引用（疑似拼写错误或废弃）：{unused}"
    # 动态拼接键
    import cache_picker as cp
    for key in ([f"sort.{k}" for k in cp.SORT_KEYS]
                + [f"kind.{k}" for k in KIND_KEYS]
                + [f"gear.{k}" for k in cp.GEAR_KEYS]):
        assert key in ZH and key in EN, f"动态键缺少资源：{key}"


def test_switch_behavior():
    """切换语言：通知次数、取值、未知码回退、缺失键兜底。"""
    calls: list[int] = []
    i18n.on_change(lambda: calls.append(1))
    base = len(calls)

    i18n.set_language("en")
    assert len(calls) == base + 1, f"切到 en 应通知一次，实际 {len(calls) - base} 次"
    for key in ("app.cache_title", "common.browse", "cache.status.save_done"):
        assert i18n.t(key) == EN[key], f"en 模式下 {key} 未取到英文资源"
        assert i18n.t(key) != ZH[key], f"en 模式下 {key} 与中文相同（未翻译？）"
    assert i18n.t("cache.bottom.count_selected", n=3, size="1.5 MB") == \
        EN["cache.bottom.count_selected"].format(n=3, size="1.5 MB")

    i18n.set_language("zh")
    assert len(calls) == base + 2, "切回 zh 应再通知一次"
    assert i18n.t("app.cache_title") == ZH["app.cache_title"]

    assert i18n.set_language("xx") == i18n.DEFAULT        # 未知语言码回退默认
    i18n.set_language("zh")
    assert i18n.t("不存在的键.不存在") == "不存在的键.不存在"   # 缺失键兜底为键名


def _walk_texts(widget, out: list[str]) -> None:
    """收集控件树上的可见文字（text 选项 + Combobox 的 values）。"""
    try:
        value = widget.cget("text")
        if value:
            out.append(str(value))
    except Exception:
        pass
    try:
        values = widget.cget("values")
        if values:
            out.extend(str(v) for v in values)
    except Exception:
        pass
    for child in widget.winfo_children():
        _walk_texts(child, out)


def _collect_texts(app, extra: list[str]) -> list[str]:
    texts: list[str] = []
    _walk_texts(app.root, texts)
    texts.extend(extra)
    return texts


@pytest.mark.gui
def test_cache_app_has_no_chinese_in_english_mode(tk_root):
    """英文模式下，缓存挑选器的控件树 + 下拉框 + Canvas 角标不得残留中文。"""
    import cache_picker as cp

    app = cp.CacheApp(tk_root)
    app.e_tdata.delete(0, "end")       # 避免本机路径干扰文本扫描
    app.e_out.delete(0, "end")
    i18n.set_language("en")
    tk_root.update_idletasks()
    # Canvas 图元不在控件树里：手工造一张卡片，取视频角标文案
    cell = app._acquire_cell()
    extra = [app.canvas.itemcget(cell["tag_txt"], "text")]

    bad = [s for s in _collect_texts(app, extra) if s not in LANG_NAMES and CJK_RE.search(s)]
    app._on_close()
    assert not bad, f"缓存挑选器在英文模式下仍有中文：{bad}"


@pytest.mark.gui
def test_online_app_has_no_chinese_in_english_mode(tk_root):
    """英文模式下，在线挑选器的控件树 + 下拉框不得残留中文。"""
    import tg_picker as tp

    app = tp.App(tk_root)
    app.e_out.delete(0, "end")         # 输出目录里是路径，不含界面文案
    i18n.set_language("en")
    tk_root.update_idletasks()

    bad = [s for s in _collect_texts(app, []) if s not in LANG_NAMES and CJK_RE.search(s)]
    app._on_close()
    assert not bad, f"在线挑选器在英文模式下仍有中文：{bad}"