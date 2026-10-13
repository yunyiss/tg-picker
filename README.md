# tg-picker —— 绕过 Telegram「禁止保存」的图片挑选器

> 🌐 语言 / Language：**中文** ｜ [English](README.en.md)

Telegram 的群组/频道可以开启 **「禁止保存」**（Restrict Saving Content），
开启后官方客户端会隐藏保存与转发按钮，图片视频只能看、存不下来。

本工具提供**两种**把内容存下来的方法，共同点是：
**缩略图墙浏览 → 点选想要的 → 只保存选中的**，不必整群全量导出。

| 模式 | 原理 | 数据来源 | 需要凭据 |
|---|---|---|---|
| **① 本地缓存模式** | 解密本机已缓存的媒体 | Telegram `tdata` 缓存 | **不需要** |
| **② API 模式** | 用账号 API 直接下载 | Telegram 服务器 | 需要 api_id / api_hash |

> 两种模式共用一个启动入口：双击 `run.cmd`，先在**模式选择界面**选一个，再进入对应流程。

> ### 为什么这两种能work？
> Telegram 官方协议对"禁止保存"的处理是**分离**的：服务器**硬拒绝转发**
> （错误码 `CHAT_FORWARDS_RESTRICTED`），但对"保存"只是**请求客户端自律**
> （协议原文用词是 *should*）。也就是说：**媒体字节本来就完整送到了你设备上**，
> 官方客户端只是"自愿"把保存按钮藏了。
>
> 两种方法都不做转发，因此不触发服务器拒绝：
> - **①** 解密已落盘的加密缓存（没破解服务器，只是把本机文件还原）
> - **②** 用你自己的账号读消息并下载（等同你自己在客户端里能做的操作）

---

## 快速开始

```cmd
git clone https://github.com/yunyiss/tg-picker.git
cd tg-picker
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

然后双击 **`run.cmd`** 启动，在模式选择界面选一个：

| 我想…… | 选择模式 | 说明 |
|---|---|---|
| 挑选我**已经在 Telegram 里看过**的图 | **本地缓存模式** | 首次自动探测 `tdata`，也可手动选；**无需登录任何东西** |
| 挑**没缓存过**的内容（历史消息等） | **API 模式** | 需先申请 api_id / api_hash（见第三节） |

> **要求**：Python 3.11+（实测于 3.13）；桌面环境需有 Tk（Windows 官方安装包自带）。

---

## 一、环境与依赖

| 依赖 | 用途 | 装后体积 |
|---|---|---|
| `Pillow` | 解码/缩放图片、GUI 显示 | ~15 MB |
| `pycryptodome` | AES 解密 tdata 缓存 | ~7 MB |
| `imageio-ffmpeg` | 抽视频首帧（自带静态 ffmpeg，**无需系统装 ffmpeg**） | ~84 MB |
| `telethon` | 仅在线挑选器（方法②）需要 | ~13 MB |

```cmd
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

> 只用本地缓存模式（方法①）的话，可以不要 `telethon`——把 `requirements.txt`
> 里那一行删掉即可，省约 13 MB。统一入口**按需延迟加载**：选本地缓存模式时
> 不会拉起 `telethon`，选 API 模式时不会拉起 tdata 解密链。

## 一·补、解密内核的来源与边界

`_vendor/tg_scoop/` 是从上游**原样复制**的 4 个文件（`__init__.py`、
`exceptions.py`、`tdata_reader.py`、`cache_decryptor.py`），负责最不能写错的
密码学部分：`TDF$` 容器解析、LocalKey 派生（PBKDF2-HMAC-SHA512）、
AES-256-IGE 解本地密钥块、`TDEF` 容器解析与 AES-256-CTR 解密。

本项目自己实现的是内核**之外**的全部部分：缓存遍历与索引、类型嗅探、
缩略图生成（含 ffmpeg 抽帧）、以及全部 GUI 与交互逻辑。

加载优先级（见 `cache_source.find_decryptor_src()`）：
`TG_SCOOP_SRC` 环境变量 → **`_vendor/`（默认）** → 同级 `tg-scoop/src`（开发兜底）。

---

## 二、本地缓存模式（推荐）

**直接读取 Telegram Desktop 的缓存**，打开就能看到你在 Telegram 里看过的图片/视频，
挑中哪个才保存哪个——不需要先用别的工具批量导出。

### 使用

1. 双击 **`run.cmd`**，在模式选择界面点 **「本地缓存模式」**
2. 确认 `tdata 目录`（会自动探测官方安装位置与便携版常见位置，也可点「浏览」手动选择）；
   若设过 Telegram 的**锁定密码**则填上
3. 点 **「读取缓存」**——首次约 15 秒（生成缩略图），之后启动约 0.6 秒
4. 在缩略图墙上挑选：
   - **单击** 选中（变蓝打勾），再点取消
   - **Shift + 单击** 范围连选
   - **双击** 把这一条**导出**到「保存到」目录（会先弹窗确认；导出后状态栏给出完整路径）
   - **Ctrl+A** 全选当前筛选结果 / **Esc** 清空
5. 选好「保存到」目录（可勾**按日期分文件夹**）→ 点 **「保存选中」**

### 安全边界

- **只读 `tdata`**：绝不修改、绝不删除其中任何字节（已验证读取前后文件修改时间不变）
- **保存前不写出任何文件**；保存只写入你指定的输出目录
- 界面里**没有"移动"选项**，避免误动缓存
- 解密内核为 [tg-scoop](https://github.com/GarrettFynn/tg-scoop)（MIT）的
  内置副本，已从本机缓存成功解出 3019 个媒体、失败 0 个

### 提示

- 缓存里只有**你完整看过/播放过**的媒体；只瞥一眼的可能不完整，会显示"无预览"
- 想看最新内容，建议**先退出 Telegram** 再点「重新扫描」，避免读到写入中的文件
- 缓存文件变多/变小后，点「重新扫描」（会丢弃索引，重新解密一遍）
- 缩略图缓存变大占地方时，点 **「清理缓存」**：只删缩略图/索引/抽帧临时文件
  （会显示释放了多少空间），下次自动重建；**你的 tdata 与已导出的媒体不受影响**

---

## 三、API 模式（需要凭据）

通过 Telegram API 按**群/频道**在线浏览缩略图并挑选下载。
适合**缓存里没有**的内容（例如很久以前的消息、你没在客户端里翻到过的媒体）。

### 申请 api_id / api_hash（一次，免费）

1. 打开 https://my.telegram.org ，用手机号登录（**验证码发到 Telegram App 里**，不是短信）
2. 进 **API development tools** → 填 App title / Short name（5–32 位字母数字）→ 提交
3. 记下 **api_id**（数字）与 **api_hash**（32 位字符串）

> 若提交报 `ERROR`：先关掉广告拦截插件或用无痕窗口、把 URL 填 `https://example.com`、
> Platform 选 Desktop、Short name 保证 5 位以上纯字母数字，再提交。

### 使用

1. 双击 `run.cmd`，在模式选择界面点 **「API 模式」**；填 api_id / api_hash / 手机号（`+8613800000000` 格式）→「登录 / 连接」
2. 在 Telegram App 里收验证码，填到弹出的「验证码/密码」行（两步验证会再问一次密码）
3. 填群/频道（`@用户名`、`t.me` 链接、邀请链接、数字 ID）→「加载媒体」
4. 单击选中 / **双击 = 立刻下载这一张**（下载路径见下方日志）→「下载选中」

### 注意

- 私密频道需要你的账号**已是成员**；邀请链接会自动尝试加入
- 内置温和控速与 FloodWait 重试，仍建议不要短时间狂拉数千条
- `tg_picker.session` 等同于登录凭证，**不要拷贝给别人**；api_hash 也不要外发

---

## 四、文件说明

| 文件 | 作用 |
|---|---|
| `cache_source.py` | 缓存解密与读取（TDEF、索引、缩略图缓存、抽帧） |
| `launcher.py` | **统一启动入口**：先展示模式选择界面，再进入对应模式 |
| `cache_picker.py` | **本地缓存模式** GUI（方法①） |
| `tg_picker.py` | **API 模式** GUI（方法②，Telethon） |
| `i18n.py` | 国际化内核：载入语言资源、`t(key)` 取词、切换语言并通知界面刷新 |
| `ui_kit.py` | 与界面无关的通用工具（`human_size` / `unique_path` / 配置读写）与共享常量 |
| `ui_lang.py` | 语言切换的界面机制（`LangMixin`），三个界面共用 |
| `locales/zh.json`、`locales/en.json` | 语言资源文件（中文 / 英文，键集合一致） |
| `_vendor/tg_scoop/` | 解密内核（上游 tg-scoop 原样副本，MIT，含 LICENSE） |
| `run.cmd` | 启动脚本（调用 `launcher.py`） |
| `requirements.txt` / `requirements-dev.txt` | 运行依赖 / 开发测试依赖（pytest、ruff） |
| `pyproject.toml` | 项目元数据与 ruff、pytest 配置（依赖只在上面的 txt 里声明） |
| `tests/` | pytest 测试套件（详见「八、开发与测试」） |
| `LICENSE` | MIT 许可 + 第三方代码标注 + 免责声明 |
| `CHANGELOG.md` | 变更记录（中文） |
| `README.en.md` | 本说明的英文版 |

**运行时自动生成、不入版本库**：

| 路径 | 说明 |
|---|---|
| `cache_thumbs/` | 缩略图缓存（可删，会自动重建） |
| `cache_index.json` | 缓存索引（删掉后会重新解密一遍） |
| `tmp_video/` | 视频抽帧临时目录（自动清理） |
| `cache_config.json` | 记住上次的路径与偏好 |
| `language.json` | 语言偏好（两个工具共享） |
| `logs/` | 启动与报错日志（无控制台启动时由程序写入，打不开工具时先看这里） |
| `tg_picker.session`、`config.json` | 在线挑选器的会话与凭据（**勿外传**） |

---

## 五、致谢

- 解密内核来自 **[GarrettFynn/tg-scoop](https://github.com/GarrettFynn/tg-scoop)**
  （MIT）——它把 Telegram Desktop 的 `tdata` 密钥派生与 `TDEF` 缓存解密
  整理成了可复用的实现。本项目直接复用了这四个文件
  （见 `_vendor/README.md`）；本项目自己实现的是缓存遍历与索引、
  类型嗅探、缩略图与抽帧、以及全部 GUI 与交互逻辑。
- 上游把该算法写清楚，是本工具能存在的前提。

---

## 六、合规提醒

本工具只做**你自己有权访问的内容**的本地读取与保存：不破解服务器、
不绕过 Telegram 的服务端限制、不托管内容、不做转发。
缓存挑选器只提取**已经缓存到你自己硬盘上**的数据；
在线挑选器只用你自己的账号读取你本就能看到的内容。
请遵守所在地区法律与内容所有者设定的规则。

---

## 七、多语言（i18n）

两个界面都内置**中文 / 英文**双语，**默认中文**。右上角有 `中文 ｜ English`
分段按钮，点击后**整个界面立即切换**（按钮、标签、排序下拉、状态栏、各类对话框
都会实时更新），不需要重启。

### 语言偏好存在哪

语言偏好保存在项目根目录的 **`language.json`**：

```json
{ "lang": "zh" }
```

它是两个工具**共享**的——在缓存挑选器里切到英文，再打开在线挑选器也是英文。
该文件属运行时本地配置，已加入 `.gitignore`，不进版本库；删除后回到默认中文。

### 语言资源文件

用户可见文案全部集中在这两个文件里，键名用**点号命名空间**（如 `cache.setup.browse`）：

```
locales/zh.json     中文（默认语言，键最全）
locales/en.json     英文（键集合与 zh.json 严格一致）
```

取值可含命名占位符，由 `t()` 做格式化。两个文件用的是**同一个键**、不同语言的值：

`locales/zh.json`

```json
{ "cache.bottom.count_selected": "已选 {n} 个　{size}" }
```

`locales/en.json`

```json
{ "cache.bottom.count_selected": "Selected {n} · {size}" }
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

## 八、开发与测试

### 目录结构

```
tg-picker/
├─ run.cmd                        启动脚本（相对路径调用下面的入口）
├─ launcher.py                    统一启动入口：模式选择 → 对应模式
├─ cache_picker.py / tg_picker.py 两个模式（GUI）
├─ cache_source.py                缓存解密与索引（必须与 _vendor/ 同级）
├─ i18n.py                        翻译内核
├─ ui_kit.py / ui_lang.py         共享工具与语言切换界面机制
├─ locales/                       语言资源（zh.json / en.json）
├─ _vendor/tg_scoop/              解密内核（上游原样副本）
├─ tests/                         pytest 测试
└─ pyproject.toml / requirements*.txt
```

> 说明：本项目**刻意保持扁平布局**（入口脚本 + 模块），没有 `src/` 包。
> 原因是 `cache_source.py` 靠同级 `_vendor/` 定位解密内核、`i18n.py` 靠
> `HERE/locales` 定位资源、`.cmd` 靠 `%~dp0` 调用入口——搬进包会同时破坏这几处。

### 安装依赖

```cmd
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt        :: 只跑工具
.venv\Scripts\pip install -r requirements-dev.txt    :: 加上测试与 lint（含上面这份）
```

### 运行测试

```cmd
.venv\Scripts\python.exe -m pytest                   :: 全部用例
.venv\Scripts\python.exe -m pytest -m "not gui"      :: 跳过需要显示器的 GUI 用例
.venv\Scripts\python.exe -m pytest -m gui            :: 只跑 GUI 用例
.venv\Scripts\python.exe -m ruff check .             :: 静态检查
```

| 测试文件 | 覆盖面 | 依赖 |
|---|---|---|
| `tests/test_i18n.py` | 两份资源键一致、占位符一致、源码键全覆盖、切换行为；英文模式遍历界面确认无中文残留 | 无 tdata；最后两项需显示器 |
| `tests/test_cache_source.py` | 定位解密内核、派生 LocalKey、枚举/解密/嗅探/缩略图、索引读写与失效、tdata 只读 | 需真实 tdata |
| `tests/test_cache_picker_logic.py` | 读取缓存、保存（含重名不覆盖）、按日期分目录、单条导出、清理缓存（只删缓存产物）、索引复用率 | 需真实 tdata |
| `tests/test_ui_kit.py` | 路径工具：重名加序号、绝不覆盖已有文件 | 无 |
| `tests/test_gui_online.py` | API 模式 GUI：建窗、队列分支、单元格选中、语言切换、验证码回填 | 需显示器（不联网） |
| `tests/test_gui_cache.py` | 本地缓存模式 GUI：虚拟化渲染、滚动性能、选择操作、语言切换 | 需真实 tdata + 显示器 |
| `tests/test_gui_launcher.py` | 统一入口：模式选择界面构建、语言切换、选后进入对应模式 | 需显示器（不联网） |

- 找不到 tdata 时相关用例会**跳过**（`skip`，不算失败）；可用环境变量
  `TDATA_PATH` 指定：`set TDATA_PATH=D:\Telegram\tdata`。
- 没有显示器（如 Linux 无 X）时 GUI 用例同样会跳过。
- 测试**不会**改写你的语言偏好（只调 `i18n.set_language`，不调会写
  `language.json` 的 `on_lang_change`），也不会写入 tdata。

### 代码风格

- `ruff` 只启用确定性检查（`E/F/I/UP/B`），**不做**格式化重排；`_vendor/` 不参与检查。
- 换行符由 `.gitattributes` 约束：源码与配置 LF、`.cmd`/`.bat` 为 CRLF。
  若在引入这些约定之前克隆过仓库，执行一次归一即可：

  ```cmd
  git add --renormalize .
  git commit -m "chore: 统一换行符为 LF"
  ```

---

## 九、许可证

本项目采用 **MIT License**，全文见 [LICENSE](LICENSE)。

- 内嵌的解密内核 `_vendor/tg_scoop/` 来自
  [GarrettFynn/tg-scoop](https://github.com/GarrettFynn/tg-scoop)（MIT），
  其版权声明与许可文本随附于 `_vendor/tg_scoop/LICENSE`；
- [LICENSE](LICENSE) 中另含第三方代码标注与免责声明。
