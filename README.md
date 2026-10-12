# tg-picker —— 图片挑选器合集

三个独立的图形界面工具，共同点是：**缩略图墙浏览 → 点选想要的 → 只保存选中的**，
不用把整个群聊或整个缓存全量导出。

| 工具 | 数据来源 | 是否需要联网/凭据 | 启动 |
|---|---|---|---|
| **缓存挑选器** | 直读 Telegram `tdata` 缓存 | 不需要（纯本地） | `run_cache.cmd` |
| **文件夹挑选器** | 本地已导出的图片文件夹 | 不需要 | `run_gallery.cmd` |
| **在线挑选器** | Telegram API 按群/频道浏览 | 需要 api_id / api_hash | `run.cmd` |

> **本项目自包含**：解密所需的内核已随项目分发在 `_vendor/tg_scoop/`
> （来自 [GarrettFynn/tg-scoop](https://github.com/GarrettFynn/tg-scoop)，MIT），
> 因此**不依赖任何外部目录**，整个文件夹拷到哪都能跑。
> 出处说明见 [`_vendor/README.md`](_vendor/README.md)。

---

## 快速开始

```cmd
git clone <本仓库地址>
cd tg-picker
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

然后按需要双击对应脚本（或按上表选择）：

| 我想…… | 双击 | 说明 |
|---|---|---|
| 浏览并挑选 **Telegram 缓存里**的图 | `run_cache.cmd` | 首次会自动探测 `tdata` 位置，也可手动选 |
| 挑选一个**本地文件夹**里的图 | `run_gallery.cmd` | |
| 按**群/频道在线**浏览并下载 | `run.cmd` | 需先申请 api_id / api_hash（见第四节） |

> **要求**：Python 3.11+（仅缓存版实测于 3.13）；
> 桌面环境需有 Tk（Windows 官方安装包自带）。

---

## 一、环境与依赖

依赖很轻，只有 4 个直接依赖（另有 3 个是 telethon 的传递依赖）：

| 依赖 | 用途 | 装后体积 |
|---|---|---|
| `Pillow` | 解码/缩放图片、GUI 显示 | ~15 MB |
| `pycryptodome` | AES 解密 tdata 缓存 | ~7 MB |
| `imageio-ffmpeg` | 抽视频首帧（自带静态 ffmpeg，**无需系统装 ffmpeg**） | ~84 MB |
| `telethon` | 仅在线挑选器需要 | ~13 MB |

重装依赖：

```cmd
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

> 只要缓存挑选器、不要在线挑图的话，可以只装前三个
> （把 `telethon` 那行从 `requirements.txt` 删掉即可）。

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

## 二、缓存挑选器（推荐，`run_cache.cmd`）

**直接读取 Telegram Desktop 的缓存**，打开就能看到你在 Telegram 里看过的图片/视频，
挑中哪个才保存哪个——不需要先用别的工具批量导出。

### 使用

1. 双击 **`run_cache.cmd`**
2. 确认 `tdata 目录`（会自动探测官方安装位置与便携版常见位置，也可点「浏览」手动选择）；
   若设过 Telegram 的**锁定密码**则填上
3. 点 **「读取缓存」**——首次约 15 秒（生成缩略图），之后启动约 0.6 秒
4. 在缩略图墙上挑选：
   - **单击** 选中（变蓝打勾），再点取消
   - **Shift + 单击** 范围连选
   - **双击** 解密预览（用系统看图程序打开）
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

---

## 三、文件夹挑选器（`run_gallery.cmd`）

用于浏览**已经导出到本地**的图片/视频文件夹。

1. 双击 `run_gallery.cmd`
2. 选「图片文件夹」→ 点「重新扫描」
3. 挑选后选择保存方式：
   - **复制**（默认，源文件保留）
   - **移动**（源文件被移走，适合挑完清理）
   - **按日期分文件夹**

---

## 四、在线挑选器（`run.cmd`，需要凭据）

通过 Telegram API 按**群/频道**在线浏览缩略图并挑选下载，适合"缓存里没有、
但服务器上有"的内容。

### 申请 api_id / api_hash（一次，免费）

1. 打开 https://my.telegram.org ，用手机号登录（**验证码发到 Telegram App 里**，不是短信）
2. 进 **API development tools** → 填 App title / Short name（5–32 位字母数字）→ 提交
3. 记下 **api_id**（数字）与 **api_hash**（32 位字符串）

> 若提交报 `ERROR`：先关掉广告拦截插件或用无痕窗口、把 URL 填 `https://example.com`、
> Platform 选 Desktop、Short name 保证 5 位以上纯字母数字，再提交。

### 使用

1. 双击 `run.cmd`，填 api_id / api_hash / 手机号（`+8613800000000` 格式）→「登录 / 连接」
2. 在 Telegram App 里收验证码，填到弹出的「验证码/密码」行（两步验证会再问一次密码）
3. 填群/频道（`@用户名`、`t.me` 链接、邀请链接、数字 ID）→「加载媒体」
4. 单击选中 / **双击 = 立刻下载这一张并打开** →「下载选中」

### 注意

- 私密频道需要你的账号**已是成员**；邀请链接会自动尝试加入
- 内置温和控速与 FloodWait 重试，仍建议不要短时间狂拉数千条
- `tg_picker.session` 等同于登录凭证，**不要拷贝给别人**；api_hash 也不要外发

---

## 五、文件说明

| 文件 | 作用 |
|---|---|
| `cache_source.py` | 缓存解密与读取（TDEF、索引、缩略图缓存） |
| `cache_picker.py` | 缓存挑选器 GUI |
| `gallery_picker.py` | 文件夹挑选器 GUI |
| `tg_picker.py` | 在线挑选器 GUI（Telethon） |
| `_vendor/tg_scoop/` | 解密内核（上游 tg-scoop 原样副本，MIT，含 LICENSE） |
| `run_cache.cmd` / `run_gallery.cmd` / `run.cmd` | 三个启动脚本 |
| `requirements.txt` | 依赖清单 |
| `CHANGELOG.md` | 变更记录 |
| `_selftest_*.py` | 功能自检（可在真实数据上跑） |
| `_smoketest_*.py` | GUI 冒烟测试 |

**运行时自动生成、不入版本库**：

| 路径 | 说明 |
|---|---|
| `cache_thumbs/`、`thumbs/` | 缩略图缓存（可删，会自动重建） |
| `cache_index.json` | 缓存索引（删掉后会重新解密一遍） |
| `preview/` | 双击预览的临时文件（可删） |
| `cache_config.json`、`gallery_config.json` | 记住上次的路径与偏好 |
| `tg_picker.session`、`config.json` | 在线挑选器的会话与凭据（**勿外传**） |

---

## 六、合规提醒

本工具集只做**你自己有权访问的内容**的本地读取与保存：不破解服务器、
不绕过 Telegram 的服务端限制、不托管内容、不做转发。
缓存挑选器只提取**已经缓存到你自己硬盘上**的数据。
请遵守所在地区法律与内容所有者设定的规则。
