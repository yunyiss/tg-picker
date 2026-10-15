# tg-picker 开发文档

> 🌐 语言 / Language：**中文** ｜ [English](DEVELOPMENT.en.md)
>
> 面向使用者的说明请看 [README.md](README.md)。本文档面向开发者与维护者：
> 目录与文件、解密内核的边界、多语言实现、测试与打包发布。

---

## 一、目录结构

```
tg-picker/
├─ run.cmd                        启动脚本（相对路径调用下面的入口）
├─ launcher.py                    统一启动入口：模式选择 → 对应模式
├─ cache_picker.py / tg_picker.py 两个模式（GUI）
├─ cache_source.py                缓存解密与索引（必须与 _vendor/ 同级）
├─ i18n.py                        翻译内核
├─ app_paths.py                   数据目录解析（便携 / 用户目录）
├─ firstrun.py                    首次启动的数据位置选择
├─ ui_kit.py / ui_lang.py / ui_style.py   共享工具、语言机制与样式构造
├─ locales/                       语言资源（zh.json / en.json）
├─ _vendor/tg_scoop/              解密内核（上游原样副本）
├─ tests/                         pytest 测试
└─ pyproject.toml / requirements*.txt
```

> 说明：本项目**刻意保持扁平布局**（入口脚本 + 模块），没有 `src/` 包。
> 原因是 `cache_source.py` 靠同级 `_vendor/` 定位解密内核、`i18n.py` 靠
> `HERE/locales` 定位资源、`.cmd` 靠 `%~dp0` 调用入口——搬进包会同时破坏这几处。

---

## 二、文件说明

| 文件 | 作用 |
|---|---|
| `cache_source.py` | 缓存解密与读取（TDEF、索引、缩略图缓存、抽帧、输出格式过滤） |
| `launcher.py` | **统一启动入口**：先展示模式选择界面，再进入对应模式 |
| `cache_picker.py` | **本地缓存模式** GUI（方法①） |
| `tg_picker.py` | **API 模式** GUI（方法②，Telethon） |
| `i18n.py` | 国际化内核：载入语言资源、`t(key)` 取词、切换语言并通知界面刷新 |
| `ui_kit.py` | 与界面无关的通用工具（`human_size` / `unique_path` / 配置读写 / 硬件检测、并发档位推荐与低配钳制）与共享常量 |
| `ui_lang.py` | 语言切换的界面机制（`LangMixin`）＋两模式逐字相同的界面骨架方法，三个界面共用 |
| `ui_style.py` | 共用色板 + ttk 样式构造：`register_common()`（容器/文字色阶/按钮/输入/复选）、`register_progressbar()`、分段按钮；差异项（滚动条配色）由各界面自定 |
| `app_paths.py` | 「数据目录」解析：便携（程序旁 `data/`）/ 用户目录，跨平台；供所有运行时产物定位 |
| `firstrun.py` | 首次启动的数据存放位置选择对话框（便携 / 用户目录） |
| `locales/zh.json`、`locales/en.json` | 语言资源文件（中文 / 英文，键集合一致） |
| `_vendor/tg_scoop/` | 解密内核（上游 tg-scoop 原样副本，MIT，含 LICENSE） |
| `run.cmd` | 启动脚本（调用 `launcher.py`） |
| `requirements.txt` / `requirements-dev.txt` | 运行依赖 / 开发测试依赖（pytest、ruff、mypy、PyInstaller） |
| `pyproject.toml` | 项目元数据与 ruff、mypy、pytest 配置（依赖只在上面的 txt 里声明） |
| `tests/` | pytest 测试套件（详见「六、运行测试」） |
| `LICENSE` | MIT 许可 + 第三方代码标注 + 免责声明 |
| `THIRD-PARTY-NOTICES.txt` | 发布包内第三方声明：内置裁剪版 ffmpeg 的 LGPL 许可、源码地址与重建配方（随包分发） |
| `CHANGELOG.md` | 变更记录（中文） |
| `README.md` / `README.en.md` | 面向使用者的说明（中 / 英） |
| `DEVELOPMENT.md` / `DEVELOPMENT.en.md` | 本文档（开发说明，中 / 英） |

---

## 三、解密内核的来源与边界

`_vendor/tg_scoop/` 是从上游**原样复制**的 4 个文件（`__init__.py`、
`exceptions.py`、`tdata_reader.py`、`cache_decryptor.py`），负责最不能写错的
密码学部分：`TDF$` 容器解析、LocalKey 派生（PBKDF2-HMAC-SHA512）、
AES-256-IGE 解本地密钥块、`TDEF` 容器解析与 AES-256-CTR 解密。

本项目自己实现的是内核**之外**的全部部分：缓存遍历与索引、类型嗅探、
缩略图生成（含 ffmpeg 抽帧）、以及全部 GUI 与交互逻辑。

加载优先级（见 `cache_source.find_decryptor_src()`）：
`TG_SCOOP_SRC` 环境变量 → **`_vendor/`（默认）** → 同级 `tg-scoop/src`（开发兜底）。

---

## 四、多语言（i18n）实现

### 语言资源文件

用户可见文案全部集中在这两个文件里，键名用**点号命名空间**（如 `cache.setup.browse`）：

```
locales/zh.json     中文（默认语言，键最全）
locales/en.json     英文（键集合与 zh.json 严格一致）
```

取值可含命名占位符，由 `t()` 做格式化。两个文件用的是**同一个键**、不同语言的值：

`locales/zh.json`

```json
{ "common.count_selected": "已选 {n} 个　{size}" }
```

`locales/en.json`

```json
{ "common.count_selected": "Selected {n} · {size}" }
```

代码里取词统一走 `i18n.t("键名", 参数=值)`：

```python
from i18n import t
self.b_save.configure(text=t("cache.bottom.save_n", n=3))   # → 保存选中（3）
```

`t()` 永远不会抛错：当前语言缺该键时回退默认语言（中文），再缺则原样返回键名。

### 新增一种语言（例如日语）

无需改动任何业务代码，三步即可：

1. **复制** `locales/zh.json` 为 `locales/ja.json`，逐条翻译**值**（**键名不要改**）；
2. 在 `i18n.py` 顶部的 `LANGUAGES` 里登记语言码与显示名：

   ```python
   LANGUAGES = {"zh": "中文", "en": "English", "ja": "日本語"}
   ```

3. 运行自检确认没有漏翻或占位符写错：

   ```cmd
   .venv\Scripts\python.exe -m pytest tests/test_i18n.py
   ```

自检会校验：两份资源键集合一致、每条取值的 `{}` 占位符一致且非空、源码里所有
`t("...")` 的键都有资源、切换语言时界面通知正常，并在**英文模式下遍历两个界面的
控件树（含下拉框与画布文字）**，确认没有任何中文残留。

### 说明

- 未引入任何第三方本地化依赖（不用 gettext / `.po` / `.mo`），纯 JSON + 标准库。
- 内部标识**不参与翻译**：媒体类型取值 `image/video/photo/file`、缓存文件名前缀
  `tg_`、扩展名、按日期分目录的 `YYYY-MM-DD` 格式等，保证既有配置与测试都兼容。
- 旧版 `cache_config.json` 里以中文文本保存的排序项（如 `"日期 新→旧"`）会在读取时
  自动迁移成内部键（`date_desc`），无需手工改配置。
- `run.cmd` 在虚拟环境缺失时的提示为纯 ASCII（英文）——`.cmd` 按系统代码页解析，
  用非 ASCII 会踩坑。

---

## 五、安装依赖

```cmd
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt        :: 只跑工具
.venv\Scripts\pip install -r requirements-dev.txt    :: 加上测试与 lint（含上面这份）
```

---

## 六、运行测试

```cmd
.venv\Scripts\python.exe -m pytest                   :: 全部用例
.venv\Scripts\python.exe -m pytest -m "not gui"      :: 跳过需要显示器的 GUI 用例
.venv\Scripts\python.exe -m pytest -m gui            :: 只跑 GUI 用例
.venv\Scripts\python.exe -m ruff check .             :: 静态检查
.venv\Scripts\python.exe -m mypy                     :: 类型检查（6 个纯逻辑模块）
```

> 调试时也可直接 **`python launcher.py`**：从终端启动会保留控制台输出；只有用 `run.cmd`（`pythonw`）
> 启动时才会把 stdout/stderr 接管到 `logs/tg_picker.log`。

| 测试文件 | 覆盖面 | 依赖 |
|---|---|---|
| `tests/test_i18n.py` | 两份资源键一致、占位符一致、源码键全覆盖、切换行为；英文模式遍历界面确认无中文残留 | 无 tdata；最后两项需显示器 |
| `tests/test_cache_source.py` | 定位解密内核、派生 LocalKey、枚举/解密/嗅探/缩略图、索引读写与失效、tdata 只读 | 需真实 tdata |
| `tests/test_cache_picker_logic.py` | 读取缓存、输出类型过滤、保存（含重名不覆盖）、按日期分目录、单条导出、清理缓存（只删缓存产物）、索引复用率 | 需真实 tdata |
| `tests/test_ui_kit.py` | 路径工具（重名加序号、绝不覆盖）；无控制台日志接管；硬件检测与并发档位推荐 | 无 |
| `tests/test_gui_online.py` | API 模式 GUI：建窗、队列分支、单元格选中、语言切换、验证码回填 | 需显示器（不联网） |
| `tests/test_gui_cache.py` | 本地缓存模式 GUI：虚拟化渲染、滚动性能、选择操作、语言切换、打开目录（含失败回退） | 需真实 tdata + 显示器 |
| `tests/test_gui_launcher.py` | 统一入口：模式选择界面构建、语言切换、选后进入对应模式 | 需显示器（不联网） |
| `tests/test_gui_consistency.py` | 两模式 UI 结构一致性：操作条版式、进度/状态位置、分区标题、空状态；卡片几何/色板/样式同源；Shift 连选与视频角标 | 需显示器 |
| `tests/test_app_paths.py` | 数据目录解析（便携/用户目录/环境变量覆盖）、marker 落盘、便携不可写回退、两模式默认导出目录一致 | 无 |
| `tests/test_firstrun.py` | 首次启动的数据位置选择（对话框与落盘、已有选择不再询问） | 需显示器 |
| `tests/test_packaging.py` | 打包脚本结构自检：随包携带 LICENSE / 第三方声明，且声明覆盖内置 ffmpeg 的 LGPL | 无 |

- 找不到 tdata 时相关用例会**跳过**（`skip`，不算失败）；可用环境变量
  `TDATA_PATH` 指定：`set TDATA_PATH=D:\Telegram\tdata`。
- 没有显示器（如 Linux 无 X）时 GUI 用例同样会跳过。
- 测试**不会**改写你的语言偏好（只调 `i18n.set_language`，不调会写
  `language.json` 的 `on_lang_change`），也不会写入 tdata。

---

## 七、打包与发布

发布形态是**独立可执行包**：各平台各自构建一次（当前为 **Windows / Linux**；macOS 待实机
验证后再加入），内置随包分发的裁剪版 ffmpeg，用户下载解压即用（无需 Python / 无需安装任何依赖）。构建脚本在 `packaging/`：

```cmd
:: 1) 构建"只解码"的最小 ffmpeg（约 5MB；需要 gcc + nasm + make）
bash packaging/build_ffmpeg.sh ffmpeg-bin
:: 2) 打成 onedir（默认输出 dist/tg-picker）
python packaging/build_app.py ffmpeg-bin/ffmpeg.exe --dist dist
```

- Windows 上第 1 步建议在 **Git Bash + MinGW-w64**（`gcc`/`nasm`/`mingw32-make`）下执行。
  注意：Git Bash 里的 `find` 会被 Windows 自带的 `find.exe` 抢占、报 `FIND: Parameter format not correct`；
  先 `export PATH="/usr/bin:/bin:$PATH"` 再跑即可（CI 用 msys2 shell，不受此影响）。
- 打 tag（`v*`）会触发 `.github/workflows/release.yml`：当前为 `ubuntu` / `windows`
  两个平台各构建一次，打成 zip 并附到 GitHub Release（macOS 待实机验证后再加回 matrix）。
- **二进制不进仓库**：仓库只存构建脚本与那份 `configure` 配方（`packaging/build_ffmpeg.sh`）；
  `dist/`、`build/` 均已加入 `.gitignore`。

---

## 八、代码风格

- `ruff` 只启用确定性检查（`E/F/I/UP/B`，外加圈复杂度 `C90≤15` 与命名 `N`），**不做**格式化重排；`_vendor/` 不参与检查。
- `mypy` 只覆盖**纯逻辑**模块（`cache_source` 与共享叶子模块 `ui_kit` / `ui_lang` / `ui_style` / `i18n` / `app_paths`）；两个 GUI 大文件暂不纳入。
- 换行符由 `.gitattributes` 约束：源码与配置 LF、`.cmd`/`.bat` 为 CRLF。
  若在引入这些约定之前克隆过仓库，执行一次归一即可：

  ```cmd
  git add --renormalize .
  git commit -m "chore: 统一换行符为 LF"
  ```

---

## 九、致谢

- 解密内核来自 **[GarrettFynn/tg-scoop](https://github.com/GarrettFynn/tg-scoop)**
  （MIT）——它把 Telegram Desktop 的 `tdata` 密钥派生与 `TDEF` 缓存解密
  整理成了可复用的实现。本项目直接复用了这四个文件
  （见 `_vendor/README.md`）；本项目自己实现的是缓存遍历与索引、
  类型嗅探、缩略图与抽帧、以及全部 GUI 与交互逻辑。
- 上游把该算法写清楚，是本工具能存在的前提。
