# tg-picker — an image picker that works around Telegram's "Restrict Saving Content"

> 🌐 Language / 语言: **English** | [中文](README.md)

Telegram groups and channels can enable **"Restrict Saving Content"**. When it is on,
the official clients hide the save and forward buttons, so photos and videos can be
viewed but not stored.

This tool offers **two** ways to get the content out. Both share the same workflow:
**browse a wall of thumbnails → pick what you want → save only the selected items**,
so you never have to export an entire chat.

| Method | How it works | Data source | Credentials | Launch |
|---|---|---|---|---|
| **① Cache picker** | Decrypts media already cached on this machine | Telegram `tdata` cache | **Not required** | `run_cache.cmd` |
| **② Online picker** | Downloads directly through your account API | Telegram servers | `api_id` / `api_hash` | `run.cmd` |

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
git clone <this repo>
cd tg-picker
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Then double-click whichever you need:

| I want to… | Double-click | Notes |
|---|---|---|
| Pick images I have **already viewed** in Telegram | `run_cache.cmd` | Auto-detects `tdata` on first run, or pick it manually; **no login of any kind** |
| Pick content that was **never cached** (older messages, etc.) | `run.cmd` | Requires `api_id` / `api_hash` first (see section 3) |

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

> If you only use the cache picker (method ①), you can drop `telethon` — just delete
> that line from `requirements.txt` and save about 13 MB.

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

## 2. Cache picker (recommended, `run_cache.cmd`)

Reads **Telegram Desktop's cache directly**. Open it and you immediately see the photos
and videos you have viewed in Telegram; only the ones you pick are saved — no need to
bulk-export with another tool first.

### Usage

1. Double-click **`run_cache.cmd`**
2. Confirm the `tdata folder` (the official install location and common portable
   locations are probed automatically; you can also click **Browse**); if you set a
   Telegram **passcode**, enter it
3. Click **Load cache** — about 15 seconds the first time (thumbnails are generated),
   then roughly 0.6 seconds on later launches
4. Pick from the thumbnail wall:
   - **Click** to select (turns blue with a check); click again to deselect
   - **Shift + click** to select a range
   - **Double-click** to decrypt and preview (opens in your system image viewer)
   - **Ctrl+A** selects all current results / **Esc** clears the selection
5. Choose the **Save to** folder (you can tick **Split by date**) → click **Save selected**

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

---

## 3. Online picker (method ②, `run.cmd`, credentials required)

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

1. Double-click `run.cmd`, enter api_id / api_hash / phone (`+8613800000000` format) →
   **Sign in / Connect**
2. Enter the code from the Telegram app in the **Code / password** row (two-step
   verification asks for the password as well)
3. Enter the group/channel (`@username`, a `t.me` link, an invite link, or a numeric ID)
   → **Load media**
4. Click to select / **double-click = download that one now and open it** →
   **Download selected**

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
| `cache_source.py` | Cache decryption and reading (TDEF, index, thumbnail cache, frame extraction) |
| `cache_picker.py` | **Cache picker** GUI (method ①) |
| `tg_picker.py` | Online picker GUI (method ②, Telethon) |
| `i18n.py` | Internationalization core: loads language resources, `t(key)` lookups, switches language and notifies the UI to refresh |
| `locales/zh.json`, `locales/en.json` | Language resource files (Chinese / English, identical key sets) |
| `_vendor/tg_scoop/` | Decryption core (verbatim copy of upstream tg-scoop, MIT, LICENSE included) |
| `run_cache.cmd` / `run.cmd` | The two launchers |
| `requirements.txt` | Dependency list |
| `LICENSE` | MIT license + third-party attribution + disclaimer |
| `CHANGELOG.md` | Change log (Chinese) |
| `README.en.md` | This English document |
| `_selftest_*.py` | Functional self-tests (can run against real data; `_selftest_i18n.py` needs no tdata) |
| `_smoketest_*.py` | GUI smoke tests |

**Generated at runtime, not tracked in version control**:

| Path | Notes |
|---|---|
| `cache_thumbs/` | Thumbnail cache (safe to delete; rebuilt automatically) |
| `cache_index.json` | Cache index (deleting it forces a full re-decrypt) |
| `preview/` | Temporary files for double-click previews (safe to delete) |
| `tmp_video/` | Temporary directory for video frame extraction (cleaned up automatically) |
| `cache_config.json` | Remembers the last paths and preferences |
| `language.json` | Language preference (shared by both tools) |
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
   .venv\Scripts\python.exe _selftest_i18n.py
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
- `run.cmd` / `run_cache.cmd` print their missing-virtualenv notice in both Chinese and
  English.