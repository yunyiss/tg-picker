# tg-picker — development documentation

> 🌐 Language / 语言: **English** | [中文](DEVELOPMENT.md)
>
> For user-facing documentation see [README.en.md](README.en.md). This document is for
> developers and maintainers: layout and files, the decryption core's boundaries, the
> i18n implementation, testing, and packaging/release.

---

## 1. Layout

```
tg-picker/
├─ run.cmd                        launcher (calls the entry point below by relative path)
├─ launcher.py                    unified launcher: mode selection → chosen mode
├─ cache_picker.py / tg_picker.py the two modes (GUI)
├─ cache_source.py                cache decryption and indexing (must stay next to _vendor/)
├─ i18n.py                        translation core
├─ app_paths.py                    data-directory resolution (portable / user directory)
├─ firstrun.py                     first-run data-location chooser
├─ ui_kit.py / ui_lang.py / ui_style.py   shared helpers, language mechanism, style builders
├─ locales/                       language resources (zh.json / en.json)
├─ _vendor/tg_scoop/              decryption core (verbatim upstream copy)
├─ tests/                         pytest suite
└─ pyproject.toml / requirements*.txt
```

> The project deliberately keeps a **flat layout** (entry scripts + modules) with no
> `src/` package: `cache_source.py` locates the decryption core via a sibling
> `_vendor/`, `i18n.py` locates resources via `HERE/locales`, and the `.cmd` launcher
> invokes the entry point via `%~dp0`. Moving modules into a package would break all
> three at once.

---

## 2. Files

| File | Purpose |
|---|---|
| `cache_source.py` | Cache decryption and reading (TDEF, index, thumbnail cache, frame extraction, output-format filter) |
| `launcher.py` | **Unified launcher**: shows the mode-selection screen, then enters the chosen mode |
| `cache_picker.py` | **Local cache mode** GUI (method ①) |
| `tg_picker.py` | **API mode** GUI (method ②, Telethon) |
| `i18n.py` | Internationalization core: loads language resources, `t(key)` lookups, switches language and notifies the UI to refresh |
| `ui_kit.py` | GUI-independent helpers (`human_size` / `unique_path` / config I/O / hardware detection, concurrency tier recommendation and low-spec clamping) and shared constants |
| `ui_lang.py` | Language-switching UI mechanism (`LangMixin`) plus the UI-skeleton methods the two modes share verbatim; used by all three screens |
| `ui_style.py` | Shared palette + ttk style builders: `register_common()` (containers / text scale / buttons / inputs / checkboxes), `register_progressbar()`, segmented buttons; differences (scrollbar colours) are per-screen |
| `app_paths.py` | "Data directory" resolution: portable (a `data/` folder next to the program) / user directory, cross-platform; locates all runtime artifacts |
| `firstrun.py` | First-run dialog to choose where data is stored (portable / user directory) |
| `locales/zh.json`, `locales/en.json` | Language resource files (Chinese / English, identical key sets) |
| `_vendor/tg_scoop/` | Decryption core (verbatim copy of upstream tg-scoop, MIT, LICENSE included) |
| `run.cmd` | Launcher script (calls `launcher.py`) |
| `requirements.txt` / `requirements-dev.txt` | Runtime / development dependencies (pytest, ruff, mypy, PyInstaller) |
| `pyproject.toml` | Project metadata plus ruff, mypy and pytest configuration (dependencies live only in the .txt files) |
| `tests/` | pytest test suite (see section 6) |
| `LICENSE` | MIT license + third-party attribution + disclaimer |
| `THIRD-PARTY-NOTICES.txt` | Third-party notices shipped inside the release: the bundled trimmed FFmpeg's LGPL license, source URL and rebuild recipe |
| `CHANGELOG.md` | Change log (Chinese) |
| `README.md` / `README.en.md` | User-facing documentation (Chinese / English) |
| `DEVELOPMENT.md` / `DEVELOPMENT.en.md` | This document (development docs, Chinese / English) |

---

## 3. Where the decryption core comes from, and its boundaries

`_vendor/tg_scoop/` contains four files **copied verbatim** from the upstream project
(`__init__.py`, `exceptions.py`, `tdata_reader.py`, `cache_decryptor.py`). They handle
the part that must not be wrong: parsing the `TDF$` container, deriving the LocalKey
(PBKDF2-HMAC-SHA512), decrypting the local key block with AES-256-IGE, parsing the
`TDEF` container, and decrypting with AES-256-CTR.

Everything **outside** that core is implemented by this project: cache traversal and
indexing, type sniffing, thumbnail generation (including ffmpeg frame extraction), and
all of the GUI and interaction logic.

Load order (see `cache_source.find_decryptor_src()`):
`TG_SCOOP_SRC` environment variable → **`_vendor/` (default)** → sibling `tg-scoop/src`
(development fallback).

---

## 4. Languages (i18n) implementation

### Language resource files

All user-visible strings live in these two files, keyed with **dot-separated
namespaces** (e.g. `cache.setup.browse`):

```
locales/zh.json     Chinese (the default language; the complete key set)
locales/en.json     English (key set strictly identical to zh.json)
```

Values may contain named placeholders, formatted by `t()`. Both files use the **same
key** with different text:

`locales/zh.json`

```json
{ "common.count_selected": "已选 {n} 个　{size}" }
```

`locales/en.json`

```json
{ "common.count_selected": "Selected {n} · {size}" }
```

All string lookups in code go through `i18n.t("key", name=value)`:

```python
from i18n import t
self.b_save.configure(text=t("cache.bottom.save_n", n=3))   # → Save selected (3)
```

`t()` never raises: if the key is missing in the current language it falls back to the
default language (Chinese), and if it is missing there too it returns the key itself.

### Adding a language (Japanese, for example)

No business code changes are needed — just three steps:

1. **Copy** `locales/zh.json` to `locales/ja.json` and translate the **values**
   (do **not** change the keys);
2. Register the language code and display name in `LANGUAGES` at the top of `i18n.py`:

   ```python
   LANGUAGES = {"zh": "中文", "en": "English", "ja": "日本語"}
   ```

3. Run the self-test to confirm nothing is untranslated and no placeholder is broken:

   ```cmd
   .venv\Scripts\python.exe -m pytest tests/test_i18n.py
   ```

The self-test verifies that the two resource files have identical key sets, that every
value is non-empty and uses identical `{}` placeholders, that every `t("...")` key used
in the source has resources, that switching language notifies the UI, and — in
**English mode** — walks the widget tree of both interfaces (including combo boxes and
canvas text) to confirm no Chinese characters remain.

### Notes

- No third-party localization dependency is used (no gettext / `.po` / `.mo`), only
  JSON plus the standard library.
- Internal identifiers are **never translated**: media kind values
  `image/video/photo/file`, the cache filename prefix `tg_`, file extensions, the
  `YYYY-MM-DD` date-subfolder format, and so on — this keeps existing configuration and
  tests compatible.
- Legacy sort values stored as Chinese text in `cache_config.json` (e.g.
  `"日期 新→旧"`) are migrated to internal keys (`date_desc`) automatically on read, so
  no manual config editing is required.
- `run.cmd` prints its missing-virtualenv notice in plain ASCII (English) — `.cmd`
  files are parsed using the system code page, so non-ASCII there causes trouble.

---

## 5. Installing dependencies

```cmd
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt        :: tool only
.venv\Scripts\pip install -r requirements-dev.txt    :: plus tests and lint (includes the above)
```

---

## 6. Running the tests

```cmd
.venv\Scripts\python.exe -m pytest                   :: everything
.venv\Scripts\python.exe -m pytest -m "not gui"      :: skip the display-dependent GUI tests
.venv\Scripts\python.exe -m pytest -m gui            :: GUI tests only
.venv\Scripts\python.exe -m ruff check .             :: static checks
.venv\Scripts\python.exe -m mypy                     :: type checks (6 pure-logic modules)
```

> For debugging you can also run **`python launcher.py`** directly: launching from a terminal keeps
> console output; only `run.cmd` (which uses `pythonw`) redirects stdout/stderr to `logs/tg_picker.log`.

| Test file | Coverage | Requirements |
|---|---|---|
| `tests/test_i18n.py` | identical key sets, identical placeholders, every source key has resources, switching behaviour; walks both UIs in English mode to prove no Chinese remains | no tdata; the last two need a display |
| `tests/test_cache_source.py` | decryptor discovery, LocalKey derivation, enumerate/decrypt/sniff/thumbnail, index round-trip and invalidation, tdata read-only | real tdata |
| `tests/test_cache_picker_logic.py` | loading the cache, output-type filter, saving (including no-overwrite on name clash), date subfolders, single-item export, cache cleaning (artifacts only), index reuse rate | real tdata |
| `tests/test_ui_kit.py` | path helpers (numeric suffix, never overwrite); console-less log capture; hardware detection and concurrency tier recommendation | none |
| `tests/test_gui_online.py` | API mode GUI: window build, queue branches, cell selection, language switch, code prompt round-trip | display (no network) |
| `tests/test_gui_cache.py` | local cache mode GUI: virtualized rendering, scroll performance, selection actions, language switch, open folder (including fallback) | real tdata + display |
| `tests/test_gui_launcher.py` | unified launcher: mode-selection build, language switch, entering the chosen mode | display (no network) |
| `tests/test_gui_consistency.py` | two-mode UI structure parity: action-bar layout, progress/status positions, section titles, empty state; shared card geometry / palette / styles; shift-click range and video badge | display |
| `tests/test_app_paths.py` | data-directory resolution (portable / user dir / env override), marker persistence, portable fallback when unwritable, both modes share one default export dir | none |
| `tests/test_firstrun.py` | first-run data-location choice (dialog and persistence; no prompt when a choice already exists) | display |
| `tests/test_packaging.py` | packaging-script self-check: LICENSE / third-party notices are bundled and cover the bundled FFmpeg LGPL | none |

- When no tdata is found those tests are **skipped** (not failed); point at one with the
  `TDATA_PATH` environment variable, e.g. `set TDATA_PATH=D:\Telegram\tdata`.
- GUI tests are likewise skipped when there is no display (for example Linux without X).
- The tests **never** rewrite your language preference (they only call
  `i18n.set_language`, never the preference-writing `on_lang_change`) and never write
  into tdata.

---

## 7. Packaging & release

The release artifact is a **standalone bundle**: built once per platform (currently
**Windows / Linux**; macOS will be added once it can be verified on real hardware) with
the slimmed ffmpeg bundled inside, so users just download, unzip and run (no Python, no
dependency installation). Build scripts live in `packaging/`:

```cmd
:: 1) build the decode-only minimal ffmpeg (~5MB; needs gcc + nasm + make)
bash packaging/build_ffmpeg.sh ffmpeg-bin
:: 2) build the onedir bundle (defaults to dist/tg-picker)
python packaging/build_app.py ffmpeg-bin/ffmpeg.exe --dist dist
```

- On Windows, run step 1 under **Git Bash + MinGW-w64** (`gcc` / `nasm` / `mingw32-make`).
  Note: inside Git Bash, `find` is shadowed by Windows' `find.exe` (`FIND: Parameter format not
  correct`); run `export PATH="/usr/bin:/bin:$PATH"` first (CI uses an msys2 shell and is unaffected).
- Pushing a tag (`v*`) triggers `.github/workflows/release.yml`, which currently builds on
  `ubuntu` / `windows`, zips each bundle and attaches them to a GitHub Release (macOS is
  held back until it can be verified on real hardware).
- **Binaries are not committed**: the repo holds only the build scripts and the `configure`
  recipe (`packaging/build_ffmpeg.sh`); `dist/` and `build/` are git-ignored.

---

## 8. Code style

- `ruff` runs only deterministic checks (`E/F/I/UP/B`, plus McCabe complexity `C90 <= 15` and
  naming `N`) and performs **no** reformatting; `_vendor/` is excluded.
- `mypy` covers only the **pure-logic** modules (`cache_source` and the shared leaf modules
  `ui_kit` / `ui_lang` / `ui_style` / `i18n` / `app_paths`); the two large GUI files are not
  included yet.
- Line endings are enforced by `.gitattributes`: source and config are LF, `.cmd`/`.bat`
  are CRLF. If you cloned the repository before these rules existed, normalise once:

  ```cmd
  git add --renormalize .
  git commit -m "chore: normalize line endings to LF"
  ```

---

## 9. Credits

- The decryption core comes from **[GarrettFynn/tg-scoop](https://github.com/GarrettFynn/tg-scoop)**
  (MIT) — it turned Telegram Desktop's `tdata` key derivation and `TDEF` cache
  decryption into a reusable implementation. This project reuses those four files
  (see `_vendor/README.md`); everything this project implements itself is cache
  traversal and indexing, type sniffing, thumbnails and frame extraction, and all of
  the GUI and interaction logic.
- Upstream writing that algorithm down clearly is what made this tool possible.
