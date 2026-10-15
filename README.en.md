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
   jpg / png / gif / webp). The **Select all** box on the left toggles them all at once and
   turns to the **partial** state when only some are checked; unchecked formats are
   **never scanned** (no decryption, no thumbnails, not in this run's results) — scan jpg
   only and only jpg comes out. Index records and thumbnails already known for other
   formats are **kept**, so switching back to All never re-scans (still sub-second); click
   **Clear cache** to reclaim that disk space.
   The **Save content** dropdown (All / Images / Videos) sets what is previewed and saved.
   The **Performance** gear (Conservative 1 thread / Balanced recommended / Turbo all
   cores) sets the **concurrency for both the scan and the thumbnails** and is auto-matched
   to this machine's hardware on launch (low-spec devices are clamped so it never
   over-threads); a cold scan measured 12.6s → 4.8s
4. Click **Start scan** — nothing is scanned automatically on launch (so it never
   decrypts everything on startup); about 5–13 seconds the first time (thumbnails are
   generated, depending on the gear and machine), then roughly 0.6–1.2 seconds on later
   launches (incremental index reuse)
5. Pick from the thumbnail wall:
   - **Click** to select (turns blue with a check); click again to deselect
   - **Shift + click** to select a range
   - **Double-click** to **save** that item into the "Save to" folder (it asks for
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
  (MIT); it has successfully decoded several thousand media items from a real local cache with 0 failures

### Tips

- The cache only holds media you have **viewed or played in full**; items you only
  glanced at may be incomplete and will show "No preview"
- To see the newest content, quit Telegram first and then click **Rescan**, so
  in-progress writes are not read
- If cache files grow or shrink, click **Rescan** (it discards the index and decrypts
  everything again)
- When the thumbnail cache grows too large, click **Clear cache**: it deletes only the
  thumbnails, index and frame temp files (and reports how much was freed); they are
  rebuilt automatically. **Your tdata and any media you saved are untouched.**

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
4. Click to select / **double-click = confirm, then save that one** (the path appears in the
   log below) → **Save selected**

### Notes

- Private channels require your account to **already be a member**; invite links are
  followed automatically
- Gentle rate limiting and FloodWait retries are built in, but avoid pulling thousands
  of messages in a burst
- `tg_picker_session.session` is equivalent to your login credentials — **do not share it**;
  keep `api_hash` private too. Both files (plus the phone number and proxy string in
  `config.json`) are stored **unencrypted** in the data directory: on POSIX their
  permissions are tightened to `0600` after writing (the session file is hardened at
  startup and after every successful sign-in). **On Windows `os.chmod` only toggles the
  read-only bit and cannot restrict other users**, so there you rely on the data
  directory's own ACL (e.g. choose the user directory, or restrict it to your account).

---

## 4. Data directory and runtime artifacts

**Runtime artifacts: written to the "data directory" (not tracked in version control)**:

On first run you choose where data is stored (delete that folder to clean everything up):

- **Portable** (recommended): a `data/` folder next to the program — deleting the program folder
  removes everything;
- **User directory**: Windows `%APPDATA%\tg-picker`, macOS `~/Library/Application Support/tg-picker`,
  Linux `~/.local/share/tg-picker` — config survives moving/upgrading the program.

| Path (inside the data directory) | Notes |
|---|---|
| `cache_thumbs/` | Thumbnail cache (safe to delete; rebuilt automatically) |
| `cache_index.json` | Cache index (deleting it forces a full re-decrypt) |
| `tmp_video/` | Temporary directory for video frame extraction (cleaned up automatically) |
| `cache_config.json` | Remembers the last paths and preferences |
| `language.json` | Language preference (shared by both tools) |
| `logs/` | Startup and error log (written by the app when it runs without a console — check here first if a tool will not start) |
| `tg_picker_session.session`, `config.json` | Session and credentials for the online picker (**do not share**) |

> The choice is recorded in `app_location.json` next to the program; if the program folder is not
> writable, portable mode falls back to the user directory automatically.

---

## 5. Compliance

This tool only reads and saves content **you are entitled to access**, locally: it does
not break into servers, bypass Telegram's server-side restrictions, host content, or
perform any forwarding. The cache picker extracts only data **already cached on your own
disk**; the online picker uses your own account to read content you can already see.
Please follow the laws of your jurisdiction and the rules set by content owners.

---

## 6. Languages (i18n)

Both interfaces ship with **Chinese / English** support and **default to Chinese**.
A `中文 ｜ English` segmented button in the top-right corner switches the **entire
interface instantly** (buttons, labels, the sort dropdown, the status bar, and every
dialog update live) with no restart required.

### Where the preference is stored

The language preference lives in **`language.json`** inside the **data directory**
(see section 4; in portable mode that is the `data/` folder next to the program):

```json
{ "lang": "zh" }
```

It is **shared** by both tools — switch to English in the cache picker and the online
picker opens in English too. The file is local runtime configuration, is listed in
`.gitignore`, and is not committed; delete it to fall back to Chinese.

---

## 7. Developer documentation

For **developer-facing** content — layout, file-by-file notes, where the decryption core
comes from and its boundaries, the i18n implementation (resource-file format / adding a
language), testing, packaging and release, and code style — see
[DEVELOPMENT.en.md](DEVELOPMENT.en.md) (Chinese: [DEVELOPMENT.md](DEVELOPMENT.md)).

---

## 8. License

This project is released under the **MIT License** — see [LICENSE](LICENSE).

- The bundled decryption core `_vendor/tg_scoop/` comes from
  [GarrettFynn/tg-scoop](https://github.com/GarrettFynn/tg-scoop) (MIT); its copyright
  notice and license text are kept in `_vendor/tg_scoop/LICENSE`.
- [LICENSE](LICENSE) also contains third-party attribution and a disclaimer.
