# tg-picker — an image picker that works around Telegram's "Restrict Saving Content"

> 🌐 Language / 语言: **English** | [中文](README.md)

Telegram groups and channels can enable **"Restrict Saving Content"**. When it is on,
the official clients hide the save and forward buttons, so photos and videos can be
viewed but not stored.

This tool offers **two** ways to get the content out. Both share the same workflow:
**browse a wall of thumbnails → pick what you want → save only the selected items**,
so you never have to export an entire chat.

| Mode | How it works | Data source | Credentials |
|---|---|---|---|
| **① Local cache mode** | Decrypts media already cached on this machine | Telegram `tdata` cache | **Not required** |
| **② API mode** | Downloads directly through your account API | Telegram servers | `api_id` / `api_hash` |

> Both modes share one launcher: double-click `run.cmd`, pick a mode on the
> **mode-selection screen**, then enter that mode's flow.

> ### Why do these work?
> Telegram's protocol treats the two operations separately: the server **hard-refuses
> forwarding** (error `CHAT_FORWARDS_RESTRICTED`), but for saving it only **asks the
> client to restrain itself** (the protocol wording is *should*). In other words, **the
> media bytes already reach your device in full** — the official client merely chooses
> to hide the save button.
>
> Neither method forwards anything, so neither triggers a server-side refusal:
> - **①** decrypts the encrypted cache already written to disk (no server was broken
>   into; local files are simply restored)
> - **②** reads messages and downloads with your own account (equivalent to what you
>   can do in the client yourself)

---

## Quick start

```cmd
git clone https://github.com/yunyiss/tg-picker.git
cd tg-picker
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Then double-click **`run.cmd`** and pick a mode on the selection screen:

| I want to… | Pick mode | Notes |
|---|---|---|
| Pick images I have **already viewed** in Telegram | **Local cache mode** | Auto-detects `tdata` on first run, or pick it manually; **no login of any kind** |
| Pick content that was **never cached** (older messages, etc.) | **API mode** | Requires `api_id` / `api_hash` first (see section 3) |

> **Requirements**: Python 3.11+ (tested on 3.13); a desktop environment with Tk
> (bundled with the official Windows installer).

---

## 1. Environment and dependencies

| Dependency | Purpose | Installed size |
|---|---|---|
| `Pillow` | Decode/scale images, GUI display | ~15 MB |
| `pycryptodome` | AES decryption of the `tdata` cache | ~7 MB |
| `imageio-ffmpeg` | Extract the first video frame (bundles a static ffmpeg, **no system ffmpeg needed**) | ~84 MB |
| `telethon` | Only needed by the online picker (method ②) | ~13 MB |

```cmd
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

> If you only use local cache mode (method ①), you can drop `telethon` — just delete
> that line from `requirements.txt` and save about 13 MB. The unified launcher imports
> **lazily**: local cache mode never pulls in `telethon`, and API mode never pulls in
> the tdata decryption chain.

## 1b. Where the decryption core comes from, and its boundaries

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

## 2. Local cache mode (recommended)

Reads **Telegram Desktop's cache directly**. Open it and you immediately see the photos
and videos you have viewed in Telegram; only the ones you pick are saved — no need to
bulk-export with another tool first.

### Usage

1. Double-click **`run.cmd`** and click **"Local cache mode"** on the selection screen
2. Confirm the `tdata folder` (the official install location and common portable
   locations are probed automatically; you can also click **Browse**); if you set a
   Telegram **passcode**, enter it
3. **Output types** — all 9 formats are checked by default (mp4 / webm / avi / mkv / mov /
   jpg / png / gif / webp); uncheck the ones you don't want, and unchecked formats are
   **never scanned** (no decryption, no thumbnails). The **Export** dropdown
   (All / Images / Videos) sets what is previewed and exported. The **Performance** gear
   (Conservative 1 thread / Balanced recommended / Turbo all cores) sets the background
   thumbnail worker count and is auto-matched to this machine's hardware on launch
   (low-spec devices are clamped so it never over-threads)
4. Click **Start scan** — nothing is scanned automatically on launch (so it never
   decrypts everything on startup); about 15 seconds the first time (thumbnails are
   generated), then roughly 0.6 seconds on later launches (incremental index reuse)
5. Pick from the thumbnail wall:
   - **Click** to select (turns blue with a check); click again to deselect
   - **Shift + click** to select a range
   - **Double-click** to **export** that item into the "Save to" folder (it asks for
     confirmation first; the full path then appears in the status bar)
   - **Ctrl+A** selects all current results / **Esc** clears the selection
6. Choose the **Save to** folder (you can click **Open folder** to open it in the file
   manager — falling back to copying the path to the clipboard if that fails; and tick
   **Split by date**) → click **Save selected**

### Safety boundaries

- **Read-only on `tdata`**: never modifies or deletes a single byte (verified: file
  modification times are unchanged before and after reading)
- **Nothing is written before you save**; saving only writes to the output folder you choose
- There is **no "move" option** in the UI, so the cache cannot be disturbed by accident
- The decryption core is a bundled copy of [tg-scoop](https://github.com/GarrettFynn/tg-scoop)
  (MIT); it has successfully decoded 3,019 media items from a real local cache with 0 failures

### Tips

- The cache only holds media you have **viewed or played in full**; items you only
  glanced at may be incomplete and will show "No preview"
- To see the newest content, quit Telegram first and then click **Rescan**, so
  in-progress writes are not read
- If cache files grow or shrink, click **Rescan** (it discards the index and decrypts
  everything again)
- When the thumbnail cache grows too large, click **Clear cache**: it deletes only the
  thumbnails, index and frame temp files (and reports how much was freed); they are
  rebuilt automatically. **Your tdata and any media you exported are untouched.**

---

## 3. API mode (credentials required)

Browses thumbnails for a **group/channel** over the Telegram API and downloads your
picks. Useful for content that is **not in the cache** (very old messages, media you
never scrolled past in the client).

### Getting `api_id` / `api_hash` (one-time, free)

1. Open https://my.telegram.org and sign in with your phone number (the code arrives
   **inside the Telegram app**, not by SMS)
2. Go to **API development tools** → fill in App title / Short name (5–32 alphanumeric
   characters) → submit
3. Note down **api_id** (a number) and **api_hash** (a 32-character string)

> If submission returns `ERROR`: disable ad blockers or use a private window, set the
> URL to `https://example.com`, choose Desktop as the platform, and make sure the Short
> name is at least 5 alphanumeric characters before submitting again.

### Usage

1. Double-click `run.cmd` and click **"API mode"** on the selection screen; enter
   api_id / api_hash / phone (`+8613800000000` format) → **Sign in / Connect**
2. Enter the code from the Telegram app in the **Code / password** row (two-step
   verification asks for the password as well)
3. Enter the group/channel (`@username`, a `t.me` link, an invite link, or a numeric ID)
   → **Load media**
4. Click to select / **double-click = download that one now** (the path appears in the
   log below) → **Download selected**

### Notes

- Private channels require your account to **already be a member**; invite links are
  followed automatically
- Gentle rate limiting and FloodWait retries are built in, but avoid pulling thousands
  of messages in a burst
- `tg_picker.session` is equivalent to your login credentials — **do not share it**;
  keep `api_hash` private too

---

## 4. Files

| File | Purpose |
|---|---|
| `cache_source.py` | Cache decryption and reading (TDEF, index, thumbnail cache, frame extraction, output-format filter) |
| `launcher.py` | **Unified launcher**: shows the mode-selection screen, then enters the chosen mode |
| `cache_picker.py` | **Local cache mode** GUI (method ①) |
| `tg_picker.py` | **API mode** GUI (method ②, Telethon) |
| `i18n.py` | Internationalization core: loads language resources, `t(key)` lookups, switches language and notifies the UI to refresh |
| `ui_kit.py` | GUI-independent helpers (`human_size` / `unique_path` / config I/O / hardware detection, concurrency tier recommendation and low-spec clamping) and shared constants |
| `ui_lang.py` | Language-switching UI mechanism (`LangMixin`), shared by all three screens |
| `locales/zh.json`, `locales/en.json` | Language resource files (Chinese / English, identical key sets) |
| `_vendor/tg_scoop/` | Decryption core (verbatim copy of upstream tg-scoop, MIT, LICENSE included) |
| `run.cmd` | Launcher script (calls `launcher.py`) |
| `requirements.txt` / `requirements-dev.txt` | Runtime / development dependencies (pytest, ruff) |
| `pyproject.toml` | Project metadata plus ruff and pytest configuration (dependencies live only in the .txt files) |
| `tests/` | pytest test suite (see section 8) |
| `LICENSE` | MIT license + third-party attribution + disclaimer |
| `CHANGELOG.md` | Change log (Chinese) |
| `README.en.md` | This English document |

**Generated at runtime, not tracked in version control**:

| Path | Notes |
|---|---|
| `cache_thumbs/` | Thumbnail cache (safe to delete; rebuilt automatically) |
| `cache_index.json` | Cache index (deleting it forces a full re-decrypt) |
| `tmp_video/` | Temporary directory for video frame extraction (cleaned up automatically) |
| `cache_config.json` | Remembers the last paths and preferences |
| `language.json` | Language preference (shared by both tools) |
| `logs/` | Startup and error log (written by the app when it runs without a console — check here first if a tool will not start) |
| `tg_picker.session`, `config.json` | Session and credentials for the online picker (**do not share**) |

---

## 5. Credits

- The decryption core comes from **[GarrettFynn/tg-scoop](https://github.com/GarrettFynn/tg-scoop)**
  (MIT) — it turned Telegram Desktop's `tdata` key derivation and `TDEF` cache
  decryption into a reusable implementation. This project reuses those four files
  (see `_vendor/README.md`); everything this project implements itself is cache
  traversal and indexing, type sniffing, thumbnails and frame extraction, and all of
  the GUI and interaction logic.
- Upstream writing that algorithm down clearly is what made this tool possible.

---

## 6. Compliance

This tool only reads and saves content **you are entitled to access**, locally: it does
not break into servers, bypass Telegram's server-side restrictions, host content, or
perform any forwarding. The cache picker extracts only data **already cached on your own
disk**; the online picker uses your own account to read content you can already see.
Please follow the laws of your jurisdiction and the rules set by content owners.

---

## 7. Languages (i18n)

Both interfaces ship with **Chinese / English** support and **default to Chinese**.
A `中文 ｜ English` segmented button in the top-right corner switches the **entire
interface instantly** (buttons, labels, the sort dropdown, the status bar, and every
dialog update live) with no restart required.

### Where the preference is stored

The language preference lives in **`language.json`** in the project root:

```json
{ "lang": "zh" }
```

It is **shared** by both tools — switch to English in the cache picker and the online
picker opens in English too. The file is local runtime configuration, is listed in
`.gitignore`, and is not committed; delete it to fall back to Chinese.

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
{ "cache.bottom.count_selected": "已选 {n} 个　{size}" }
```

`locales/en.json`

```json
{ "cache.bottom.count_selected": "Selected {n} · {size}" }
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

## 8. Development and testing

### Layout

```
tg-picker/
├─ run.cmd                        launcher (calls the entry point below by relative path)
├─ launcher.py                    unified launcher: mode selection → chosen mode
├─ cache_picker.py / tg_picker.py the two modes (GUI)
├─ cache_source.py                cache decryption and indexing (must stay next to _vendor/)
├─ i18n.py                        translation core
├─ ui_kit.py / ui_lang.py         shared helpers and the language-switching UI mechanism
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

### Installing dependencies

```cmd
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt        :: tool only
.venv\Scripts\pip install -r requirements-dev.txt    :: plus tests and lint (includes the above)
```

### Running the tests

```cmd
.venv\Scripts\python.exe -m pytest                   :: everything
.venv\Scripts\python.exe -m pytest -m "not gui"      :: skip the display-dependent GUI tests
.venv\Scripts\python.exe -m pytest -m gui            :: GUI tests only
.venv\Scripts\python.exe -m ruff check .             :: static checks
```

| Test file | Coverage | Requirements |
|---|---|---|
| `tests/test_i18n.py` | identical key sets, identical placeholders, every source key has resources, switching behaviour; walks both UIs in English mode to prove no Chinese remains | no tdata; the last two need a display |
| `tests/test_cache_source.py` | decryptor discovery, LocalKey derivation, enumerate/decrypt/sniff/thumbnail, index round-trip and invalidation, tdata read-only | real tdata |
| `tests/test_cache_picker_logic.py` | loading the cache, output-type filter, saving (including no-overwrite on name clash), date subfolders, single-item export, cache cleaning (artifacts only), index reuse rate | real tdata |
| `tests/test_ui_kit.py` | path helpers (numeric suffix, never overwrite); console-less log capture; hardware detection and concurrency tier recommendation | none |
| `tests/test_gui_online.py` | API mode GUI: window build, queue branches, cell selection, language switch, code prompt round-trip | display (no network) |
| `tests/test_gui_cache.py` | local cache mode GUI: virtualized rendering, scroll performance, selection actions, language switch, open folder (including fallback) | real tdata + display |
| `tests/test_gui_launcher.py` | unified launcher: mode-selection build, language switch, entering the chosen mode | display (no network) |

- When no tdata is found those tests are **skipped** (not failed); point at one with the
  `TDATA_PATH` environment variable, e.g. `set TDATA_PATH=D:\Telegram\tdata`.
- GUI tests are likewise skipped when there is no display (for example Linux without X).
- The tests **never** rewrite your language preference (they only call
  `i18n.set_language`, never the preference-writing `on_lang_change`) and never write
  into tdata.

### Code style

- `ruff` runs only deterministic checks (`E/F/I/UP/B`) and performs **no** reformatting;
  `_vendor/` is excluded.
- Line endings are enforced by `.gitattributes`: source and config are LF, `.cmd`/`.bat`
  are CRLF. If you cloned the repository before these rules existed, normalise once:

  ```cmd
  git add --renormalize .
  git commit -m "chore: normalize line endings to LF"
  ```

---

## 9. License

This project is released under the **MIT License** — see [LICENSE](LICENSE).

- The bundled decryption core `_vendor/tg_scoop/` comes from
  [GarrettFynn/tg-scoop](https://github.com/GarrettFynn/tg-scoop) (MIT); its copyright
  notice and license text are kept in `_vendor/tg_scoop/LICENSE`.
- [LICENSE](LICENSE) also contains third-party attribution and a disclaimer.