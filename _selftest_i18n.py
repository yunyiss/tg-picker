# -*- coding: utf-8 -*-
"""国际化（i18n）自检：键完整性、占位符一致、源码覆盖、切换行为、界面无遗漏。

无需 tdata、无需联网；断言失败即非零退出。

用法：python _selftest_i18n.py
"""
import re
import string
import sys
import tkinter as tk
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import i18n  # noqa: E402

SOURCES = ["cache_picker.py", "tg_picker.py", "cache_source.py"]
# 键名形如 ns.sub.sub：小写字母开头、点分
KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$")
# 源码里的 t("key") 调用
T_CALL_RE = re.compile(r"\bt\(\s*[\"']([A-Za-z][\w.]*)[\"']")
# CJK 及全角标点
CJK_RE = re.compile(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]")


def load(lang: str) -> dict:
    import json
    return json.loads((HERE / "locales" / f"{lang}.json").read_text(encoding="utf-8"))


ZH = load("zh")
EN = load("en")
LANG_NAMES = set(i18n.available().values())     # 语言按钮自身文案（如"中文"）


def placeholders(text: str) -> set:
    """取 str.format 占位符名集合；非法模板返回空集。"""
    try:
        return {name for _, name, _, _ in string.Formatter().parse(text) if name}
    except ValueError:
        return set()


def check_catalogs() -> None:
    print("=== 1) 两份语言的键集合是否一致 ===")
    missing_in_en = sorted(set(ZH) - set(EN))
    extra_in_en = sorted(set(EN) - set(ZH))
    assert not missing_in_en, f"en.json 缺少键：{missing_in_en}"
    assert not extra_in_en, f"en.json 多出键：{extra_in_en}"
    print(f"键集合一致，共 {len(ZH)} 条 ✓")

    print("=== 2) 取值非空 + 占位符一致 ===")
    for lang, cat in (("zh", ZH), ("en", EN)):
        for k, v in cat.items():
            assert isinstance(v, str) and v.strip(), f"{lang}.json 键 {k} 取值为空"
    for k in ZH:
        assert placeholders(ZH[k]) == placeholders(EN[k]), (
            f"键 {k} 占位符不一致：zh={sorted(placeholders(ZH[k]))} "
            f"en={sorted(placeholders(EN[k]))}")
    print("全部取值非空，占位符逐条一致 ✓")


def check_source_coverage() -> None:
    print("=== 3) 源码覆盖：字面量键都存在，且资源键都被用到 ===")
    text = "\n".join((HERE / s).read_text(encoding="utf-8") for s in SOURCES)
    # 3a) t("...") 里的键必须在两份资源里
    for key in sorted(set(T_CALL_RE.findall(text))):
        assert KEY_RE.match(key), f"疑似键名的字面量不合规范：{key}"
        assert key in ZH, f"源码用了 t(\"{key}\")，但 zh.json 没有该键"
        assert key in EN, f"源码用了 t(\"{key}\")，但 en.json 没有该键"
    # 3b) 资源里每个键都应在源码中以字面量出现（含经 _T(...) 传入的键）。
    #     sort.*/kind.* 是运行时拼接的动态键，字面量扫描抓不到，由 3c 单独校验。
    dynamic_prefix = ("sort.", "kind.")
    unused = []
    for key in ZH:
        if key.startswith(dynamic_prefix):
            continue
        if f'"{key}"' not in text and f"'{key}'" not in text:
            unused.append(key)
    assert not unused, f"资源键未被任何源码引用（疑似拼写错误或废弃）：{unused}"
    # 3c) 动态拼接的键单独枚举校验
    kind_keys = ("image", "video", "file", "all")
    dyn = [f"sort.{k}" for k in __import__("cache_picker").SORT_KEYS]
    dyn += [f"kind.{k}" for k in kind_keys]
    for key in dyn:
        assert key in ZH and key in EN, f"动态键缺少资源：{key}"
    print(f"t() 字面量键全部有资源；资源键全部被引用；动态键 {len(dyn)} 条齐备 ✓")


def check_switch() -> None:
    print("=== 4) 切换行为 ===")
    calls = []
    i18n.on_change(lambda: calls.append(1))
    base = len(calls)

    i18n.set_language("en")
    assert len(calls) == base + 1, f"切到 en 应通知一次，实际 {len(calls) - base}"
    for key in ("app.cache_title", "common.browse", "cache.status.save_done"):
        assert i18n.t(key) == EN[key], f"en 模式下 {key} 未取到英文资源"
        assert i18n.t(key) != ZH[key], f"en 模式下 {key} 与中文相同（未翻译？）"
    assert i18n.t("cache.bottom.count_selected", n=3, size="1.5 MB") == \
        EN["cache.bottom.count_selected"].format(n=3, size="1.5 MB")

    i18n.set_language("zh")
    assert len(calls) == base + 2, "切回 zh 应再通知一次"
    assert i18n.t("app.cache_title") == ZH["app.cache_title"]

    # 未知语言码回退默认，且不抛错
    assert i18n.set_language("xx") == i18n.DEFAULT
    i18n.set_language("zh")
    assert i18n.t("不存在的键.不存在") == "不存在的键.不存在"
    print("切换通知、取值、未知码回退、缺失键兜底均正常 ✓")


def walk_texts(widget, out: list) -> None:
    """收集控件树上的可见文字（text 选项 + Combobox 的 values）。"""
    try:
        val = widget.cget("text")
        if val:
            out.append(str(val))
    except Exception:
        pass
    try:
        vals = widget.cget("values")
        if vals:
            out.extend(str(v) for v in vals)
    except Exception:
        pass
    for child in widget.winfo_children():
        walk_texts(child, out)


def scan_app(app, extra: list) -> list:
    """返回英文模式下仍含 CJK 的文字（语言按钮自身文案除外）。"""
    texts = []
    walk_texts(app.root, texts)
    texts.extend(extra)
    bad = []
    for s in texts:
        if s in LANG_NAMES:
            continue
        if CJK_RE.search(s):
            bad.append(s)
    return bad


def check_widgets() -> None:
    print("=== 5) 英文模式下界面文案无中文残留 ===")
    import cache_picker as cp
    import tg_picker as tp

    # --- 缓存挑选器 ---
    root = tk.Tk()
    app = cp.CacheApp(root)
    app.e_tdata.delete(0, "end")          # 避免本机路径干扰文本扫描
    app.e_out.delete(0, "end")
    i18n.set_language("en")
    root.update_idletasks()
    extra = []
    # Canvas 图元不在控件树里：手工造一张卡片，取视频角标文案
    cell = app._acquire_cell()
    extra.append(app.canvas.itemcget(cell["tag_txt"], "text"))
    bad = scan_app(app, extra)
    assert not bad, f"缓存挑选器在英文模式下仍有中文：{bad}"
    print(f"  缓存挑选器：控件树 + Combobox + Canvas 角标 全部为英文 ✓")
    root.destroy()

    # --- 在线挑选器 ---
    root = tk.Tk()
    app2 = tp.App(root)
    app2.e_out.delete(0, "end")           # 输出目录里是路径，不含界面文案
    i18n.set_language("en")
    root.update_idletasks()
    bad2 = scan_app(app2, [])
    assert not bad2, f"在线挑选器在英文模式下仍有中文：{bad2}"
    print("  在线挑选器：控件树 + Combobox 全部为英文 ✓")
    root.destroy()

    i18n.set_language("zh")


def main() -> None:
    check_catalogs()
    check_source_coverage()
    check_switch()
    check_widgets()
    print("\ni18n 自检全部通过 ✅")


if __name__ == "__main__":
    main()