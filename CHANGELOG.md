# 变更记录

## 2026-10-15（重构：收敛两模式逐字重复的界面骨架方法）

### 重构 · 去重安全子集（`_handle` / `_divider` / `_update_empty_hint`）
- **现象（可维护性）**：缓存与 API 两个 GUI 各自逐字写了一遍 `_handle`（消息队列分发）、
  `_divider`（1px 分隔线）、`_update_empty_hint`（网格区空状态提示），任何改动都要两头同步、
  容易漂移。
- **修改**：把这三个**逐字相同**的方法收敛到既有的 `ui_lang.LangMixin`（两个界面都已继承它，
  并补齐类型声明）；有模式差异的 `set_busy` / `_pump` / `_on_wheel` / `on_open_dir` **刻意不抽**。
- **验证**：`ruff` / `mypy` 全绿、`pytest` **100 passed**（行为不变：空状态、消息分发、
  分隔线的既有断言与全部 GUI 用例通过）。

## 2026-10-15（修复：统一两模式进度条样式）

### 修复 · 进度条样式名与配色不一致（UI-2）
- **现象**：缓存模式的进度条是 `Slim.Horizontal.TProgressbar` 且显式覆盖了三色；
  API 模式用的是默认 `Horizontal.TProgressbar`、只设了 `background` / `troughcolor`
  ——clam 主题下两端会露出浅色 chrome，两模式观感不一致。
- **原因**：进度条样式由两个界面各自注册，样式名与覆盖项不同源。
- **修改**：把进度条样式收敛到 `ui_style`（`PROGRESS_STYLE` + `register_progressbar()`），
  两个模式注册并引用**同一个**样式名与配色（含 `bordercolor` / `lightcolor` / `darkcolor`
  与 disabled 档）；缓存侧取值不变、零视觉变化。
- **验证**：`ruff` / `mypy` 全绿、`pytest` **100 passed**（新增断言：两模式
  `pb.cget("style")` 相同且等于 `ui_style.PROGRESS_STYLE`）。

## 2026-10-15（修复：API 模式补齐视频角标与占位图）

### 修复 · API 视频单元格与缓存模式观感不一致（UI-1）
- **现象**：API 模式的视频卡片既不显示「视频」角标，封面取不到 / 尚未加载时也只显示
  「无预览」文字；而缓存模式的视频是"图右下角黑底「视频」角标 + 灰色占位图"，
  两模式并排看观感不一致。
- **原因**：视频角标与占位图**只在缓存模式实现**（Canvas 图元 `tag` / `tag_txt` 与
  `_video_placeholder`），API 的 `add_item_cell` / `_msg_thumb` 没有对应处理；文案键
  `cell.video_tag` 也因此只有缓存模式在用。
- **修改**：把角标的几何 / 配色与灰色占位图抽到**共用叶子模块 `ui_kit`**
  （`TAG_W/H/RADIUS/INSET/BG/FG/FONT_SIZE` 与 `video_placeholder()`），两模式同源引用
  （缓存模式数值不变、零视觉变化）；API 侧在 `add_item_cell` 新增角标图元、视频创建即显示
  占位图，`_msg_thumb` 在取不到封面时回退"占位图 + 角标"（不再显示「无预览」），
  语言切换时同步刷新角标文字。
- **验证**：`ruff` / `mypy` 全绿、`pytest` **99 passed**（新增一致性断言：两模式角标
  几何 / 配色同源、API 视频的占位图与角标状态正确；`cell.video_tag` 至此两模式都被使用）。

## 2026-10-15（修复：Shift 连选在列表变短后越界）

### 修复 · Shift 连选越界（缓存 / API 两模式）
- **现象**：扫描 / 加载一批媒体后，先点一条靠后的条目，再改「保存内容」筛选（或改「加载数量」
  后重新加载）使列表变短，随后 Shift+单击 → 抛 `IndexError: list index out of range`；
  该段落的连选只落一半、界面也不再刷新。
- **原因**：`last_click` 记的是"条目在**当时**列表里的下标"，但列表被筛选 / 排序 / 重扫换掉时
  **从不清它**；连选循环 `for i in range(lo, hi + 1): self.items[i]` 又没有上界钳制，
  锚点下标大于当前条数时即越界。这与先前修的缩略图「串图」是同一类"下标当身份"问题
  ——那处清了在途队列，却漏了 `last_click`。
- **修改**：① 列表重建处作废旧下标（缓存：`apply_filter_sort`、`_reset_grid_data`；
  API：`on_load`）；② 连选循环加 `hi = min(hi, len(self.items) - 1)` 兜底。
- **验证**：`ruff` / `mypy` 全绿、`pytest` **98 passed**（新增缓存 / API 两条回归用例，
  均为"列表变短 + 旧下标"复现，修复前会 `IndexError`）。

## 2026-10-15（打包：排除未使用的 PIL AVIF 插件，零损失瘦身）

### 打包 · 排除 PIL 的 AVIF 插件（发布包 −7.5MB）
- **现象**：Windows 发行包里的 `PIL/_avif.cp313-win_amd64.pyd` 达 **7.47 MB**，是全包（除内置
  ffmpeg 外）最大单项；而本工具实际处理与嗅探的图片格式只有 jpg / png / gif / webp / bmp，
  **全仓无 avif / heif / AvifImagePlugin 任何引用**——这 7.47 MB 纯属白占体积。
- **原因**：Pillow 默认随包携带 AVIF 编解码扩展，PyInstaller 会把它一并打进 onedir 产物；
  此前未对图片格式做任何裁剪。
- **修改**：`packaging/build_app.py` 的 EXCLUDE 加入 `PIL.AvifImagePlugin` 与 `PIL._avif`
  （后者是该插件的 C 扩展，一并排除以免误留二进制）。
- **验证**：`ruff` / `mypy` 全绿、`pytest` **96 passed**；用内置裁剪版 ffmpeg 重新打包实测：
  未压缩 **42.3 MB / 1020 文件**（排除前 49.8 MB）、zip **20.9 MB**（排除前 25.0 MB）；
  `_avif.pyd` 消失而 `_webp.pyd` 保留；体积前五为 tg-picker.exe 6.36 / python313.dll 5.83 /
  libcrypto-3.dll 4.99 / ffmpeg.exe 4.70 / _imaging 2.38（MB）。

## 2026-10-15（修复：API 缩略图卡片尺寸与缓存模式对齐）

### 修复 · API 模式缩略图卡片尺寸/间距与缓存模式不一致
- **现象**：API 模式的缩略图卡片比缓存模式大一圈、缩略图偏挤（左右留白明显更小），两模式并排看观感不一致。
- **原因**：此前两次改动修的都是网格 `relayout` 的**列数/居中**，而**卡片与缩略图本身各持一套尺寸**：
  API 为卡片 168x205、`THUMB=150`、图片直接居中无方框；缓存模式为卡片 164x196、`THUMB=140`、在 144 的
  图片方框内居中（顶部留 8px）。换算下来 API 卡片约大 4%、缩略图约大 7%、左右边距只有 8px（缓存 12px）、
  整体偏低约 9px——观感即"尺寸不对/太挤"。
- **修改**：把卡片几何常量收敛到 `ui_kit` 作为**单一来源**（`CARD_W/H`、`GAP`、`IMG_BOX`、`THUMB`、
  `CORNER`），两个模式同源引用（缓存模式数值不变、零视觉变化），杜绝再次漂移；`tg_picker.add_item_cell`
  改为缓存模式同款画法：单元格画布=卡片本身、图片在 144 方框内居中、信息行贴底 `CARD_H-20`、间隙由 grid
  的 padx/pady 提供。既定的网格居中（两侧对称 spacer 列）保持不变。
- **验证**：`ruff` / `mypy` 全绿；`pytest` **96 passed**（新增两模式几何同源断言）；实测图元坐标与缓存模式
  逐项相等（卡片 164x196、图片中心 82x80、信息行 y=176、徽标 146x18）。

## 2026-10-15（界面调整：顶栏清理 + 视图总览下移 + API 网格溢出与居中修复）

### 修复 · API 模式网格右侧溢出并居中对齐
- **现象**：API 模式的缩略图网格在某些窗口宽度下右侧**超出窗口、被裁掉**（缓存模式正常）；
  仅修正列数后，右侧又**空出一大块**。
- **原因**：① `relayout` 按 `(width-10)//CELL_W` 算列数，漏算了每个单元格 grid 的 `padx`（左右各 5px），
  窗口略宽时列数偏多、整行比画布宽十几像素；② 修正列数后仍剩一段取整余量（最多近一整格），而 Tk 的
  grid 默认**左对齐** ⇒ 余量全堆在右边。缓存模式是 Canvas 自绘、`CELL_W` 已含间隙且有 `_grid_x0` 居中，
  两处都不受影响。
- **修改**：按真实步距 `CELL_W + 2*CELL_PAD` 计算列数；并用**两侧对称的 spacer 列**（第 0 列与第 cols+1 列
  各 `weight=1`）把余量均分到左右，使整块卡片居中——与缓存模式同一视觉行为。列数变化时先把旧的右侧
  spacer 权重清零（记在 `_spacer_col`，兼容"置 `columns=0` 强制重排"这条路径），避免间距被撑歪。

### 调整 · 顶栏去掉「语言」二字
- **现象**：两个模式顶栏都多出一处冗余的「语言」提示文字（launcher 本就没有）。
- **修改**：去掉 `_build_lang_switch` 的 `with_label` 选项及其文案键 `lang.label`（中英同步）。

### 调整 · 视图总览下移到操作条，并补到 API 模式
- **现象**：缓存模式的「全部 x / 共 y」挤在顶栏、紧贴语言按钮；API 模式则完全没有这个缩略图数量统计。
- **修改**：两模式统一把它移到操作条第 2 行「已选 N 个」旁；API 模式补上同一项（沿用同一文案键
  `common.view_overview`，无筛选时显示数与总数相等）。文案键由 `cache.status.header` 改名而来。
- **验证**：`ruff` / `mypy` 全绿；`pytest` 全量 **95 passed**（新增：两模式概览标签同位置断言、API 居中
  spacer 列断言）。

## 2026-10-15（交付前审查修复·第四批）

### M6 · firstrun 为数据目录对话框另抄了一份配色（可维护性）
- **现象**：`firstrun.py` 的数据目录对话框本地又写了一套与 `ui_style` 同值的深灰配色常量
  （`BG` / `CARD` / `CARD_HOVER` / `ACCENT` / `FG` / `SUB`）；该对话框与启动器 `launcher`
  的「选择数据位置」面板本是同一件事的两处实现，配色一旦在 `ui_style` 里调整就会两头漂移。
- **修改**：删掉本地常量，改为从 `ui_style` 导入同一套色板（取值逐一相同，**外观零变化**、
  消除漂移）；并在模块 docstring 写明——统一入口 `launcher` 已在同一窗口内完成这一步，
  本模块仅在**直接运行** `tg_picker.py` / `cache_picker.py` 时兜底。
- **验证**：`ruff` / `mypy` 全绿；`pytest` 全量通过（`test_firstrun.py` 的对话框用例带 `gui`
  标记，本机有显示器、会真实建窗）。

### M7 · 界面字体写死为 Windows 专有字族（跨平台）
- **现象**：`ui_kit.FONT` 写死 `"Microsoft YaHei UI"`，而 release 会产出 Windows / macOS / Linux
  **三平台包**；非 Windows 上该字族不存在，Tk 会回退默认字体，观感与设计不符。
- **修改**：按平台各给一个存在率高、气质接近的 CJK 字族——Windows 雅黑 / macOS 苹方 /
  其余（Linux）Noto Sans CJK；若该平台仍缺此字体，Tk 自身再回退默认字体，不会报错。
- **验证**：`ruff` / `mypy` 全绿；`pytest` 全量通过（字体仅在 Tk 建窗时使用，逻辑无分支变化）。

### M9 · 发布不经过门禁、CI 只测一个 Python 版本（工程流程）
- **现象**：`release.yml` 打 tag 后直接构建三平台产物挂 Release，**完全不经过 CI 门禁**——门禁
  没过的提交照样能出包；而 `ci.yml` 只测 Python 3.13，与 `requires-python>=3.11` 声明的下限脱节。
- **修改**：`release.yml` 新增 `gate` 作业（跑与 CI 同款的 `ruff` / `mypy` / `pytest`），`build`
  以 `needs: gate` 依赖它，门禁不过则不发包；`ci.yml` 的矩阵补上
  `python-version: ["3.11", "3.12", "3.13"]`，让声明的下限真正被验证。
- **验证**：纯 YAML 改动，本地无法执行 Actions；已逐字段核对语法、矩阵展开与 `needs` 引用。

### F7 · 代理相关依赖缺版本上界
- **现象**：`requirements.txt` 里 `PySocks` / `python-socks` 只有下界、**没有上界**——其余依赖
  （Pillow / pycryptodome / imageio-ffmpeg / telethon）都带了上界，下一个大版本的破坏性改动
  会被无意引入。
- **修改**：补上 `PySocks>=1.7,<2`、`python-socks>=2.4,<3`，与其余依赖保持一致。
- **验证**：仅依赖声明改动；当前环境版本落在区间内，`pytest` 全量通过。

## 2026-10-14（交付前审查修复·第三批：缩略图并发 + 领域逻辑收敛）

### M4 · 在线模式缩略图抓取退化为串行（性能）
- **现象**：`Worker.load_thumbs` 逐条串行，且每条末尾 `await asyncio.sleep(0.05)` 温和控速；
  一批 500 条 ⇒ **25 秒纯等待**，期间吞吐只有 1 路。
- **修改**：抽出常量 `THUMB_CONCURRENCY = 3`；改为信号量限流的 N 路并发。关键在于**把控速 `sleep`
  放在信号量之外**——否则每条都占着并发名额睡，并发会退化成串行。整批 `asyncio.gather` 等齐后
  再返回，`busy` 复位的语义保持不变。
- **验证**：`ruff` / `mypy` / `pytest` 全绿（并发只影响耗时，不改结果与取消语义）。

### F5 · 领域逻辑从 GUI 模块收敛到 cache_source（可维护性）
- **现象**：`MediaItem` / `save_items` / `export_one` 属于「缓存媒体」的领域逻辑，却住在 GUI 模块
  `cache_picker.py` 里、与 Tk 代码混在一起；纯逻辑测试（保存/命名/去重）不得不 `import cache_picker`，
  连带拉起整个界面模块。
- **修改**：把三者搬到 `cache_source.py`（`_log` 与 `unique_path` 随之到位，逐条失败留痕逻辑原样保留）；
  `cache_picker` 改为 `from cache_source import MediaItem, export_one, save_items` 再导出——**所有调用点
  与既有测试（`cp.MediaItem` / `cp.save_items`）零改动**，`cache_picker` 里随之清掉 `logging` / `dataclass`
  / `unique_path` 等已无引用的导入。
- **验证**：`ruff` / `mypy`（6 files，覆盖范围不变）/ `pytest` 全绿。

### 验证
- `ruff` / `mypy`（6 files）全绿；`pytest` **94 passed**。〔注：`test_tdata_is_read_only` 依赖真实
  `tdata`，当 Telegram Desktop 正在运行并改写其 `binlog` 时会偶发误报，重跑即通过。〕

## 2026-10-14（交付前审查修复·第二批：发布合规 + 保存失败留痕 + 扫描限流）

### F4 · 发布包补齐第三方许可声明（合规）
- **现象**：`build_app.py` 只把 `locales` / `_vendor` / 裁剪版 ffmpeg 打进包——`_vendor` 里 tg_scoop 的
  MIT LICENSE 跟着走了，但**独立分发的 ffmpeg（LGPL 2.1+）没有任何声明**，对外挂 Release 时是实打实的合规缺口。
- **修改**：新增 `THIRD-PARTY-NOTICES.txt`（写明 ffmpeg 版本与许可、源码地址、以及"用仓库里的
  `packaging/build_ffmpeg.sh` 配方可重建同一份二进制"，并列出各运行时依赖的许可）；`build_app.py` 里
  `--add-data` 把 `LICENSE` 与它一并打进包；README 中/英的文件清单表各补一行。
- **验证**：**实跑了一次打包**（用占位文件充当 ffmpeg），确认产物里确实带上了 `LICENSE` /
  `THIRD-PARTY-NOTICES.txt` / `_vendor/tg_scoop/LICENSE` / `locales`（PyInstaller 6 在 onedir 下把
  它们放在 `_internal/`）；另加 `tests/test_packaging.py` 做源码级断言。构建产物已清理。

### F3 · 缓存模式逐条保存失败留痕
- **现象**：`save_items` 里是 `except Exception: fail += 1`，状态栏只给「成功 N / 失败 M」，
  **哪一条失败、为什么**（磁盘满 / 无权限 / 文件被占用）在界面与日志里都查不到。
- **修改**：逐条失败写进日志（三个入口共用的 logger → 数据目录 `logs/`）；有失败时状态栏用新键
  `cache.status.save_done_failed` 提示"（失败原因见日志文件）"，无失败时仍是原句（保持与 API 模式同句）。
- **验证**：新增用例用 `caplog` 断言"条目名 + 异常类型 + 原因"都写进了日志。

### M5 · 扫描在途任务限流（内存峰值）
- **现象**：`prepare` 里 `{idx: pool.submit(...) for idx in jobs}` 一次性提交全部待办，超大缓存会瞬间创建
  成千上万个 future——线程数虽受 `max_workers` 限制，但 future 与任务闭包本身就要占内存。
- **修改**：抽出 `_JobWindow` 滑动窗口：预填 `workers × 4` 个，之后**消费一个补一个**，在途数量恒定有界，
  并保证"下一个待消费的下标一定在窗口里"这条不变式。
- **验证**：新增直接盯住窗口性质的用例（桩线程池断言"预填只填满窗口""在途数 ≤ 窗口"）；
  并依赖既有的 `test_parallel_scan_matches_serial` 保证结果与串行**完全一致**。
- **备注**：这次改动一度让 `prepare` 撞上 ruff 的 `C901`（复杂度 18 > 15）；把窗口逻辑抽成小类后回到限值内
  ——这也正是"抽出去"的附带好处。

### 验证
- `ruff` / `mypy`（6 files）全绿；`pytest` **94 passed**（新增 4 条：打包结构 2 条、保存失败留痕、窗口性质）。

## 2026-10-14（交付前审查修复：串图竞态 + 凭据/日志隐私加固 + API 退出收尾）

### M1 · 缓存模式缩略图「串图」竞态（正确性）
- **现象**：缩略图结果按**列表下标**回传，而筛选 / 排序 / 搜索 / 重扫都会换掉 `self.items`。旧实现只清
  `thumb_mem`/`thumb_pending`，既不清 `photo_queue` 也不校验身份 ⇒ 在途的旧结果会贴到"现在位于同一下标的
  另一条媒体"上，并被 `thumb_mem`（同样以下标为键）长期记住——**显示的是错的图**。新旧列表等长时（例如只改排序）
  连越界判断都拦不住。
- **修改**：`_request_thumb` 结果里带上身份键 `rel`；`_drain_photos` 用 `str(items[idx].entry.rel) != rel` 复核，
  不一致**整条丢弃**（不写 `thumb_mem`、也不动 `thumb_pending`，校验放在 PIL→PhotoImage 转换之前）；
  新增 `_clear_photo_queue()`，在 `on_filter_change` / `_reset_grid_data` 换列表时清掉在途结果。
- **验证**：新增回归用例，**临时去掉身份校验可稳定复现失败**（旧结果贴到 idx0 的新条目上），加回后通过。

### 隐私加固（代理密码 / 会话文件）
- **代理密码曾按原文写进日志**：`online.log.proxy_set` 直接记用户输入的代理串，带 `user:pass` 的写法会把密码
  落进 `data/logs/tg_picker.log`。→ 新增 `mask_proxy()`，记录前掩码 userinfo（保留主机端口，便于排查连不上）。
- **会话文件从未收紧权限**：`config.json` 写入后是 `0o600`，但 Telethon 自建的 `tg_picker_session.session`
  （明文 SQLite、内含登录密钥，等同凭证）一直跟随 umask（POSIX 常见 `0644`）。→ 新增 `harden_session_file()`，
  **启动时与每次登录成功后**各收一次（含 SQLite 的 `-journal` / `-wal` 兄弟文件）。
- **文档补齐 Windows 上的真相**：`os.chmod` 在 Windows 上只能切换只读位、**不构成访问控制**，所以
  "凭据受 0600 保护"在 Windows 上并不成立。→ README 中/英与 `ui_kit.save_config` 的文档都写明这一点，
  并指出 Windows 下应依赖数据目录自身的 ACL。

### API 模式退出收尾（健壮性）
- **现象**：`_on_close` 只置取消位就销毁窗口，`loop.run_forever()` 永不停止、`TelegramClient` 也不
  `disconnect()`（仅靠 daemon 线程在进程退出时兜底，会话文件与套接字不是干净关闭）。
- **修改**：新增 `Worker.shutdown()`，把「`client.disconnect()` → `loop.stop()`」串成**一个协程**投递到事件
  循环：既不阻塞界面线程，又能保证"先断开、后停循环"的顺序。

### 验证
- 新增 5 条用例：`mask_proxy` 的掩码规则、登录日志**与该掩码后的渲染完全相等**（守住"接线处"，不只是测纯函数）、
  会话文件收权限（POSIX 上断言确为 `0600`）、启动与登录后各收一次（monkeypatch 拦到调用）、关闭后事件循环确实停止。
- 顺带修掉一处**用例间的数据目录污染**：前一条用例把 proxy 存进会话共享的临时数据目录，后一条构造窗口时会被回填，
  输入框变成拼接串导致用例结果"看执行顺序"。改为先清空字段，做到顺序无关。
- `ruff` / `mypy`（6 files）全绿；`pytest` **90 passed**。

## 2026-10-14（修复：CI 的 Linux 一路从建立起就一直红——mypy 报 Windows 专有属性）

### 现象
- GitHub Actions 的 CI **从第一次运行（`368530d`）起，ubuntu-latest 一路始终失败**，Windows 一路正常；
  失败步骤是 **mypy**，报 `cache_source.py:79: error: Module has no attribute "STARTUPINFO"`（另有 2 条同类）。

### 原因
- mypy 是按**当前平台**做类型检查的：`subprocess.STARTUPINFO` / `CREATE_NO_WINDOW` /
  `STARTF_USESHOWWINDOW` / `SW_HIDE` 在 typeshed 里**只在 win32 下定义**。
- `cache_source.py` 原来把"有没有这个属性"用 `hasattr` 存成模块常量、运行时再判断——
  mypy 无法据此缩小类型，于是在 Linux 上直接按"subprocess 模块没有该属性"报错。
  Windows 侧属性存在，所以本地与 Windows CI 一直是绿的。

### 修改
- `_hidden_subprocess_kwargs()` 改为 `sys.platform != "win32"` 提前返回：非 Windows 平台整个分支
  都会被 mypy 跳过（与 `ui_kit.open_folder` 里 `os.startfile` 的既有写法同一套路）。
  删除因此不再需要的 `_NO_WINDOW_FLAGS` / `_HAS_STARTUPINFO` 两个模块常量。
- Windows 运行时行为**不变**：仍传 `creationflags=CREATE_NO_WINDOW` + `STARTUPINFO(SW_HIDE)`，
  ffmpeg 子进程照旧不弹控制台窗口。

### 验证
- 本机复现 CI 平台：`mypy --platform linux` 修前 **3 errors** → 修后 **Success**；Windows 侧 `mypy` 仍 Success。
- 运行时自检：Windows 下 `_hidden_subprocess_kwargs()` 仍返回两项且取值正确。
- `ruff` 全绿；`pytest` **84 passed**（CI 门禁子集 `-m "not gui"` 47 passed）。

> 经验：本地（Windows）跑 mypy 看不到 Linux 侧的问题。改动涉及平台分支时，
> 顺手跑一次 `python -m mypy --platform linux` 即可提前发现（CI 的矩阵已能覆盖两个平台）。

## 2026-10-14（UI：压缩上下面板留白，让缩略图网格区更高）

### 背景
- 用户反馈：缩略图网格区被上下多个面板挤得太矮——1280×880 下只能看到约 1.8 行。

### 修改（**只收纵向留白**；缩略图尺寸、字号、配色一律不动）
- 两个模式各区的纵向内边距统一收紧：`12/14/10 → 8/6`；区内多行之间 `10 → 8`；
  分区标题下间距 `8 → 6`；网格区上下留白 `10 → 6`。
- API 模式日志区默认从 **7 行缩到 4 行**（仍可滚动）——单项收益最大的一笔（省 46px）。

### 效果（1280×880，实测取真实窗口高度）
- API 模式网格区高度 382 → **470 px**（+88，**+23%**）：可见约 1.8 行 → **2.2 行**
- 缓存模式 408 → **472 px**（+64，**+16%**）：约 2.0 行 → **2.3 行**
- 各区块占高变化——API：标题栏 47→39、登录区 119→107、群/频道区 78→72、操作条 88→82、
  状态栏 32→30、日志区 112→66；缓存：顶栏 51→39、设置区 187→167、筛选栏 82→72、
  状态栏 34→30、底部操作条 96→86

### 验证
- `ruff` / `mypy`（6 files）全绿；`pytest` **84 passed**

## 2026-10-14（UI：API 模式改为自绘圆角卡片，与缓存模式完全同款）

### 现象
- 缓存模式的卡片是**圆角矩形**、选中徽标是**圆形**；API 模式是**直角卡片**、徽标是**方形色块**。
  上一步只统一了配色与描边，形状没跟上（用户截图对比后指出）。

### 修改
- API 模式的单元格由 `tk.Frame` 改为**每个单元格一个固定尺寸的小 `tk.Canvas`**，
  卡片底改用**圆角多边形**绘制：与缓存模式同一套——复用 `ui_kit.rounded_pts`、圆角半径同为 **9px**。
  为此把缓存模式内部的圆角点列生成抽到 `ui_kit.rounded_pts`，两模式共用同一处实现（含"每角只给 2 个
  锚点会导致视觉半径只剩 3-4px"的历史教训注释）。
- 选中徽标改为与缓存模式**同形状、同位置、同尺寸**：底色外圈 + 强调色圆底 + 白勾
  （原先 API 侧是 Label 做的方形色块）。
- 缩略图 / 占位文字 / 信息行由内嵌 Label 改为 **Canvas 图元**（内嵌控件会盖住徽标、破坏圆角）；
  信息行同时对齐缓存模式的格式与字号（`MM-DD HH:MM  ·  大小`，字号 9）。
- 顺带修掉一处交互缺陷：滚轮落在卡片上时原本**滚动失效**（画布的 `bind_all` 会在指针进入子控件时被解绑），
  现在单元格自身也绑定滚轮，网格区任意位置都能滚动（与缓存模式一致）。

### 验证
- `tests/test_gui_consistency.py` 新增/加强 3 条：卡片底为圆角多边形且半径与缓存模式一致、
  选中徽标的形状/位置/状态两模式一致、缩略图与「无预览」占位图元正确切换
- `ruff` / `mypy`（6 files）全绿；`pytest` **84 passed**

## 2026-10-14（UI：选中态统一，API 模式补齐「已选 N 个」计数）

### 现象
- 两模式选中态视觉不一致、观感也一般：缓存模式是暗蓝底 + 细描边（选中不够醒目）；
  API 模式是 `#1f6feb` 亮蓝**铺满整卡**，把缩略图画面压掉，看着很重。
- 计数提示只有缓存模式有（底栏「已选 N 个　大小」），API 模式缺这一项。

### 修改
- **卡片色板收敛**：`CARD` / `CARD_HOVER` / `CARD_SEL` 移入 `ui_style`，两模式**共用同一套**
  （原 API 侧 `#2b2b2b` / `#1f6feb` 与缓存侧 `#26262c` / `#24344d` 各写各的）；
  选中底色统一为 **`#2a3c5c`**——够醒目，又不至于压掉缩略图本身。
- **选中态统一为同一套视觉**：淡蓝底 + **2px 强调色描边** + **右上角对勾徽标**
  - 缓存模式：徽标外加一圈**内容区底色描边圈**（缩略图是亮色/同色系时不再糊掉），对勾字号 10→11
  - API 模式：描边由"与底色相同"改为**强调色**（原先 `highlightbackground=底色` 等于没有描边）；
    徽标从左上角移到**右上角**，并改为**强调色小徽标 + 白勾**（原先是白勾直接落在亮蓝底上）
- **API 模式补齐计数**：操作条第 2 行最左新增**「已选 N 个」计数标签**（`Count.TLabel`、宽 26，
  与缓存模式同款、同位置），由 `update_count()` 与主按钮文案一并刷新；
  缓存模式原先的 `update_button()` 一并改名 `update_count()`（两处命名对齐）
- 计数文案收敛为共用键 `common.count_none` / `common.count_selected`
  （原 `cache.bottom.count_none` / `count_selected` 删除），两模式**同一键、同一句式**

### 验证
- `tests/test_gui_consistency.py` 新增 2 条并收紧 1 条：两模式卡片色板同源、
  计数标签的样式/位置/文案一致且选中后同步更新、操作条第 2 行左侧顺序两模式完全相同（含计数）
- `ruff` / `mypy`（6 files）全绿；`pytest` **83 passed**
- README（中/英）i18n 示例键同步为 `common.count_selected`

## 2026-10-14（UI：两模式（本地缓存 / API）文案、交互、版式与空状态全面统一）

### 背景
- 工具由同一个启动器进入两个并列模式：**底层数据流不同是合理的**；但用户可感知层
  （按钮措辞、弹窗文案、状态反馈位置、选择/取消交互、分区标题、操作条版式、空状态）
  仍有明显差异，用起来像"两个独立工具"。
- 目标：**除"模式切换标识 + 模式特有功能"外，用户感知层完全一致**。

### 修改
- **文案统一**（只改取值、键名基本不动）
  - 主按钮统一「保存选中（{n}）」；术语中的「下载 / 导出」一律改为「保存」
  - 完成反馈 / 进行中 / 未选中提示 / 单条确认弹窗 / 未选输出目录 等句式在两模式下取值**完全相同**
  - 窗口标题统一为「TG 图片挑选器 · 本地缓存模式」「… · API 模式」
  - 共用键收敛：「清除选择」两侧统一 `common.clear_sel`（删除 `common.clear`）；新增 `common.invert`、`common.cancel_req`
- **交互补齐**
  - API 模式新增 **Shift 连选** 与 **「反选」**（语义与缓存模式一致：按被点条目的当前选中态决定整段选/取消）
  - 缓存模式新增 **「取消」按钮**（运行期可用、空闲禁用，点击置位取消事件并给出反馈）
- **版式统一**
  - 操作条统一为**两行**：第 1 行「保存到 + 输入框 + 浏览 + 打开目录」；
    第 2 行「选择操作（左）+ 主按钮 / 取消（右）」；缓存模式特有的「按日期分文件夹」放在主按钮左侧
  - 状态栏底色统一 **BAR**；**状态在右、进度在左**（缓存模式此前相反，且进度条改为按需出现的整宽细条）
  - 缓存模式筛选栏改回面板底，并补编号分区标题「① 缓存与设置」「② 筛选与选择」，
    与 API 模式的「① 登录」「② 选择聊天与筛选」**同款**（`Section.TLabel`）
  - API 模式顶部补**模式标题标签**与区域分隔线（`Sep.TFrame`）；表单字段标签统一为次级色（`Sub.TLabel`）；
    主框架与网格区**水平内边距统一 16px**（原 API 侧为 12px）
- **空状态统一**：两模式在**网格区居中**显示同款内联提示（新样式 `Empty.TLabel`）；
  缓存模式不再弹「没有解析出媒体」对话框，改由状态栏给出"先在 Telegram Desktop 里查看/播放再重扫"的指引
  （原 `cache.dialog.no_media` 键移除，新增 `cache.empty_hint` / `online.empty_hint` / `cache.status.no_media`）
- 清理因上述调整而失效的样式定义（`BarField.TLabel` / `BarSection.TLabel` / `Bar.TCheckbutton`）

### 明确保留的差异（属模式特有，未统一）
- API 模式保留**日志文本区**（登录成功/失败、限速、逐条明细必须可见）；缓存模式不加 UI 日志区
  （已有日志**文件** + 状态栏，无逐条明细，UI 日志冗余）
- 类型筛选语义不同：缓存 = **保存范围**下拉（含"全部"）；API = **从服务器加载哪些类型**（可多选）
- 各模式特有输入与动作：tdata / 锁定密码 / 输出类型 / 性能档位 / 搜索 / 重新扫描 / 清理缓存；
  登录 / 代理 / 群频道 / 加载数量 / 加载更早

### 验证
- `tests/test_i18n.py` 新增 `test_cross_mode_wording_is_identical`：6 组同功能文案在中/英两语言下取值必须相同
- 新增 `tests/test_gui_consistency.py`（6 条，构建真实窗口后比对）：操作条两行的控件顺序与文案、
  进度条与状态标签的左右位、①② 分区标题的数量与样式、空态提示的显示/隐藏、
  API 模式 Shift 连选的区间语义、缓存模式取消按钮的状态与取消事件
- `ruff` / `mypy`（6 files）全绿；`pytest` **81 passed**
- README（中/英）同步：按钮名（「导出内容」→「保存内容」、Download selected → Save selected）与"双击保存"等描述更新

## 2026-10-14（UI：API 模式对齐缓存样式，登录/代理区重排）

### 现象
- API 模式仍是旧样式：按钮保留 clam 的立体边框、输入框带浅色描边、标签底色与面板不一致；
  登录 + 代理挤在一条拉满整行的横条里，占空间且不像常规软件。
- 凭据行与代理行**共用一个栅格**：代理提示文案很长、横跨 `api_hash` 所在列并把它撑宽，
  于是 `api_id` 与 `api_hash` 之间空出一大块（截图可见）。

### 修改
- **色板收敛**：`ui_style` 新增共用的深色主题色板，`cache_picker` 与 `tg_picker` 改为从它导入
  （原先各自写死色值、观感不一致）。
- **`tg_picker._build_style` 重写**为与缓存挑选器同一套：内容区 BG / 面板 PANEL / 状态栏 BAR；文字三级色阶；
  按钮压掉立体边框并统一内边距；输入框 / 下拉框 / 复选 / 滚动条显式覆盖
  `bordercolor`/`lightcolor`/`darkcolor`（复选另加 `upper/lowerbordercolor`）；补下拉列表 Listbox 深色。
- **登录区重排**为「标题 + 两行**独立子栅格**」：第 1 行 凭据（api_id / api_hash / 手机号）
  + 登录按钮 + 登录状态；第 2 行 代理（短输入框 + 小字提示）。两行各自 `grid`、列宽互不牵连，
  每行末尾留弹性空列吸收多余宽度；行内间距 14px。
- 语言切换条 / 登录 / 加载 / 操作 / 状态区分别套上 PANEL / BAR 面板底，标签底色随所在面板。

### 验证
- 自检：凭据行需求宽度 781px（最小窗口 1020px 下不裁切）、`api_id`→`api_hash` 间距 14px、
  代理输入框 182px、无零尺寸 / 偏移控件、17 个 ttk.Label 无底色错配。
- `test_gui_online` 新增 3 条：代理行紧凑、登录按钮与手机号同行、`api_id`→`api_hash` 间距 < 40px。
- `ruff` / `mypy`（6 files）全绿；`pytest` **73 passed**。

## 2026-10-14（功能：API 模式支持代理设置）

### 背景
- Telegram 在部分网络下无法直连；Telethon **不读系统 / 代理软件的全局设置**，此前只能靠 TUN 才能用 API 模式。

### 修改
- 登录表单新增**「代理」输入行**（留空 = 直连）；支持 `host:port`（默认按 socks5）、`socks5://host:port`、
  `http://user:pass@host:port` 等写法，新增 `parse_proxy()` 解析。
- 来源优先级：环境变量 `TG_PICKER_PROXY` > 配置里的 `proxy`；登录后自动记住、下次启动回填。
- `Worker.login` 增加 `proxy` 参数并传给 `TelegramClient(proxy=...)`；设置了代理时日志会显示「使用代理：…」。
- 新增语言键 `online.login.proxy`、`online.login.proxy_hint`、`online.dialog.bad_proxy`、`online.log.proxy_set`（中英同步）。
- `requirements.txt` 增加 `PySocks`（socks5/4）与 `python-socks`（http）——代理链路所需。

### 验证
- `test_gui_online` 新增 2 条：`parse_proxy` 各写法与非法输入；非法代理时提示且不提交登录。
- **端到端实测**：`parse_proxy("127.0.0.1:7897")` → `('socks5','127.0.0.1',7897)`，用本机会话经该代理连接
  `is_user_authorized=True`（**未开 TUN**）。
- `ruff` / `mypy`（6 files）全绿；`pytest` **69 passed**。

## 2026-10-14（功能：保存路径增加「打开文件夹」，并抽出公共实现）

### 现象
- API 模式「保存到」一行只有「浏览」，**没有打开目录的入口**；缓存模式有此功能。

### 修改
- 新增 `ui_kit.open_folder()`（跨平台：Windows `startfile` / macOS `open` / Linux `xdg-open`），返回是否成功；
  缓存模式的 `on_open_dir` 改为调用它（行为不变：失败仍回退为复制路径到剪贴板）。
- API 模式「保存到」一行新增**「打开文件夹」按钮**（`App.on_open_dir`）；失败时把路径复制到剪贴板并在日志区提示。
- 「打开目录」按钮文案收敛为共用键 `common.open_dir`（同一按钮同一文案），删除重复键 `cache.bottom.open_dir`；
  新增 `online.dialog.no_out_dir`、`online.log.path_copied`（中英同步）。

### 验证
- `test_gui_online` 新增 2 条：打开失败回退复制路径；目录不存在时提示且不尝试打开。
- `test_gui_cache` 的打开目录用例改为对 `ui_kit.open_folder` 打桩（与实现解耦）。
- `ruff` / `mypy`（6 files）全绿；`pytest` **67 passed**。

## 2026-10-14（修复：API 模式双击缩略图改为「先确认再下载」）

### 现象
- API 模式双击缩略图会**直接下载落盘**，没有任何确认，容易误触。

### 修改
- `App.preview_item` 先弹确认框（显示**目标目录**与**文件名**），确认后才提交下载；文案与缓存模式对齐
  （新增语言键 `online.dialog.confirm_save`，中英同步）。
- 抽出 `item_name()`：确认框展示的文件名与真正下载所用命名**共用同一套规则**，避免两处漂移。

### 验证
- `test_gui_online` 新增 1 条：取消确认不发起下载、确认后才提交，并校验确认框内包含目标目录。
- `ruff` / `mypy`（6 files）全绿；`pytest` **65 passed**。

## 2026-10-14（修复：统一 API / 缓存模式的默认保存目录）

### 现象
- API 模式与缓存模式的默认保存目录不一致（前者 `data/downloads`，后者 `程序目录/<本地化名>`）。

### 修改
- 默认导出目录统一为**数据目录下的 `exports/`**：新增 `app_paths.EXPORT_SUBDIR` 与
  `app_paths.export_dir()`；`tg_picker.DEFAULT_OUT` 与 `cache_picker.EXPORT_DIR` 改指同一目录。
- 目录名固定 `exports`、不再走 i18n（原缓存默认名随语言变化，中英文会生成不同文件夹）；
  删除随之不再使用的语言键 `cache.default_out_dir`（中英同步）。
- 仍优先使用配置里记住的 `out_dir`：用户上次手动选过的目录不受影响。

### 验证
- `test_app_paths` 新增 2 条：`export_dir()` 位于数据目录下且名为 `exports`；两个模式的默认导出目录必须一致。
- `ruff` / `mypy`（6 files）全绿；`pytest` **64 passed**。

## 2026-10-14（调整：数据位置选择并入启动器窗口 + 位置提示）

### 修改
- 「数据存放位置」**不再单独弹窗**：首次启动时**在模式选择窗口内**先显示位置选择
  （两张整体可点的卡片：便携 / 用户目录），选完后**同一个窗口**变回正常的模式选择
  （`Launcher` 中部面板在两者间切换）。单独开发者入口（直接跑 `cache_picker.py` /
  `tg_picker.py`）仍用 `firstrun` 的小对话框兜底。
- 模式选择底部新增**数据（缓存 / 配置）位置提示**（`launch.data_dir`），随语言切换重放；
  选数据位置时同步刷新。
- 新增语言键 `launch.data_dir`、`firstrun.enter`（中英同步）。

### 验证
- `test_gui_launcher` 新增 2 条：位置提示存在；首次启动同窗口先选位置、选完变回模式选择。
- `ruff` / `mypy`（6 files）全绿；`pytest` **62 passed**。

## 2026-10-14（发布准备：CI 打包流水线）

### 修改
- 新增 `packaging/build_ffmpeg.sh`：把"只解码"最小 ffmpeg 的 **configure 配方**固化为构建脚本
  （跨平台：Windows 走 MSYS2/MinGW，macOS/Linux 各用自家工具链；用 gzip 解压，不依赖 xz）。
- 新增 `packaging/build_app.py`：用 PyInstaller 打成 **onedir** 发布包并内置该 ffmpeg；
  按需导入的模块显式声明、开发期模块一律排除。
- 新增 `.github/workflows/release.yml`：打 tag（`v*`）或手动触发时，`ubuntu`/`macos`/`windows`
  各构建一次、打成 zip 并附到 GitHub Release。**仓库只存脚本与配方，二进制不入库。**
- `requirements-dev.txt` 增加 `pyinstaller`（本地打包用）；README（中/英）增加"打包与发布"小节。

### 验证（本机 Windows 实跑）
- `build_ffmpeg.sh`：下载 7.1 源码 → configure → 编译，产出 **4.7 MB** `ffmpeg.exe`。
- `build_app.py`：onedir **49.7 MB**，`ffmpeg.exe` / `locales/` / `_vendor/` 均在包内。
- 冻结件启动验证：预置"便携"后启动，程序旁自动生成 `data/logs/`，启动器窗口正常、日志无报错。
- `ruff` / `mypy` / `pytest`（60 passed）不受影响。
- 说明：`release.yml` 无法在本地执行，首次打 tag 时由 CI 验证。

## 2026-10-14（发布准备：首次启动选择数据存放位置）

### 修改
- 新增 `firstrun.py`：首次启动（尚无保存选择时）弹出对话框，让用户选**便携**（程序旁
  `data/`，删程序即删数据）或**用户目录**（`%APPDATA%` 等，移动/升级不丢配置）；选择落盘到
  程序旁的 `app_location.json`，下次不再询问。三个入口（`launcher` / `cache_picker` /
  `tg_picker` 的 `main`）统一先调用它。
- 新增语言键 `firstrun.*`（6 个，中英同步）；`test_i18n` 的源码覆盖面纳入 `firstrun.py`。
- README（中/英）同步：文件清单加入 `app_paths` / `firstrun`；"运行时产物"表改述为**数据目录**，
  并说明便携 / 用户目录与首次选择。

### 验证
- 新增 `tests/test_firstrun.py`（4 条）：选择落盘、关闭默认便携、已有选择不再询问、对话框可建可关。
- `ruff` / `mypy`（6 files）全绿；`pytest` **60 passed**。

## 2026-10-14（发布准备：运行时产物统一收敛到「数据目录」）

### 背景
原先缩略图/索引/配置/语言偏好/日志/会话/默认导出目录都硬编码在项目根（`HERE/…`）。
发布成独立可执行包后，这些会写进程序内部目录，既不便清理，也不符合"用完即删不留痕"。

### 修改
- 新增 `app_paths.py`：统一解析"数据目录"——**便携**（`<程序目录>/data`，删程序即删数据）
  或**用户目录**（Windows `%APPDATA%/tg-picker`、macOS `~/Library/Application Support/tg-picker`、
  Linux `~/.local/share/tg-picker`）；选择记录在程序目录旁的 `app_location.json`；便携不可写时
  自动回退用户目录；测试/CI 可用环境变量 `TG_PICKER_DATA_DIR` 覆盖。
- `i18n`（language.json）、`cache_picker`（thumbs / index / config / tmp_video）、
  `tg_picker`（config / session / downloads）、三个入口的日志路径，**全部改走数据目录**。
- `tests/conftest.py` 把测试期数据目录指向临时目录（不污染仓库）；`.gitignore` 忽略
  `data/` 与 `app_location.json`；`mypy` 覆盖 `app_paths.py`。
- 首次启动的"位置选择"对话框留待下一步。

### 验证
- 新增 `tests/test_app_paths.py`（7 条）：模式落盘、便携回退、环境变量覆盖、无选择时需询问等。
- `ruff` / `mypy`（6 files）全绿；`pytest` **56 passed**。

## 2026-10-14（发布准备：ffmpeg 支持「随工具分发」）

### 背景
目标发布形态是"独立可执行包"：用户下载即用、不装任何系统依赖。为此工具需要能定位
**随自己分发**的 ffmpeg（自包含的裁剪版），而不是只认 imageio-ffmpeg 或系统 PATH。

### 修改
`cache_source.ffmpeg_exe()` 查找顺序改为：imageio-ffmpeg（开发）→ **自带 ffmpeg**
（`_bundled_ffmpeg()`：优先 `sys._MEIPASS`，其次 exe 所在目录）→ 系统 PATH（兜底）。
开发环境行为不变（imageio 优先）。

### 验证
- `ruff` / `mypy` 全绿；`pytest` 通过。
- 打包实测：裁剪版 ffmpeg（**4.7 MB**，完整版 83.6 MB）随包分发后，在屏蔽 imageio 的
  情况下，工具自身代码路径能找到并驱动它抽帧（mp4 / mkv / webm 均通过）。

## 2026-10-13（重构：收敛三个界面重复的 ttk 样式代码）

### 背景
三个界面（`launcher` / `cache_picker` / `tg_picker`）各自重写了一遍
`theme_use("clam")` 与分段按钮（`Seg` / `SegOn`）的样式构造，且都带着"clam 会
自绘浅色 chrome、必须显式覆盖 bordercolor/lightcolor/darkcolor"这条教训——
一处踩坑要改三遍。

### 修改
- 新增 `ui_style.py`：`use_clam(st)` 与 `segmented_buttons(...)`。只共享**构造
  逻辑**，颜色仍由各界面传入（与 `ui_kit` 共享字体、`ui_lang` 共享语言机制同一
  思路，不合并刻意不同的配色）。
- 三个界面改为调用它，配色逐项对照原值传入，**视觉不变**。
- mypy 覆盖范围加入 `ui_style.py`；README（中/英）文件清单同步补该模块。

### 验证
- `ruff` / `mypy`（5 files）全绿；`pytest` **49 passed**（含 GUI：三个界面均能
  构建、语言切换正常）。

## 2026-10-13（工程化：引入 mypy 类型检查）

### 修改
- 新增 `[tool.mypy]` 配置：覆盖"纯逻辑"模块（`cache_source` / `ui_kit` /
  `ui_lang` / `i18n`），开启 `check_untyped_defs`，并 `ignore_missing_imports`
  （Pillow / telethon 无类型存根）。两个 GUI 模块暂不纳入——Tk 控件对象与回调
  注解收益低、噪声大——配置里已注明，待其属性补齐标注后再扩大范围。
- 修正被 mypy 抓出的问题：`Image.LANCZOS` → `Image.Resampling.LANCZOS`
  （Pillow 存根不再暴露旧常量）；`load_index` 取出的记录用 `or {}` 消除 None
  联集；`LangMixin` 声明子类提供的 `root` / `busy` / `l_status` 属性；语言按钮
  改用 `functools.partial`（默认参 lambda 的类型无法推断）。
- `requirements-dev.txt` 增加 `mypy`；CI 增加 `python -m mypy` 步骤。

### 验证
- `mypy`：**Success: no issues found in 4 source files**；`ruff` 全绿；
  `pytest` **49 passed**。

## 2026-10-13（工程化：ruff 扩展复杂度与命名规则）

### 修改
- `select` 增加 `C90`（圈复杂度，上限 15）与 `N`（pep8-naming）。
- 随之修正 `cache_source`：`PasscodeRequired` → `PasscodeRequiredError`（N818）；
  `_import_decryptor()` 解包出的类别名改为小写（N806）。调用方
  `cache_picker` 的 `except cs.PasscodeRequired` 同步更名。
- 对三处**刻意或天然**的情况做窄范围豁免，并在配置里注明理由：`tests/*` 的
  C901/N806、`tg_picker.py` 的 C901（异步 + 分页编排分支天然多）、
  `ui_lang.py` 的 N802（`_T` 注册器刻意大写，与 `t()` 呼应）。

### 验证
- `ruff check .` 在新规则下**全绿**；`pytest` **49 passed**。

## 2026-10-13（工程化：日志改用 logging 并做轮转）

### 背景
`enable_file_log` 直接把 `sys.stdout/stderr` 接到一个普通文件（`open(..., "a")`），
日志只增不减，长期运行会无限增长。

### 修改
改用标准库 `logging`：共用日志器 `tg_picker` + `RotatingFileHandler`（单文件
2 MiB、保留 3 份）。无控制台（`pythonw`）时仍接管 `sys.stdout` / `sys.stderr`
（经逐行 writer 转成 INFO / ERROR 记录），有控制台时保持原样不接管。

### 验证
- 原用例 `test_enable_file_log_redirects_stdout_and_stderr` 仍通过（stdout/stderr
  内容确实落进了日志文件）；新增 `test_enable_file_log_rotates` 断言超过上限会
  轮转出 `.1`。测试用 autouse fixture 关闭 handler，避免占用临时目录。
- `ruff` 全绿；`pytest` 通过。

## 2026-10-13（重构：领域层不再拼装界面文案）

### 背景
`cache_source.prepare()` 的进度回调把**已格式化好的**文案一并传给界面
（`progress_cb(done, total, t("cs.progress_parse", ...))`），领域层因此依赖
i18n；而且这条进度在后续切换语言时无法重渲染（界面只能原样显示）。

### 修改
- `prepare()` 的 `progress_cb` 改为**只回调数字** `progress_cb(done, total)`；
  面向用户的进度文案由界面按当前语言渲染。
- `cache_picker` 进度消息改为只带 `(i, total)`，状态栏走
  `set_status("cs.progress_parse", ...)`——**纳入可重放**，切换语言即时刷新。
- `ui_lang.LangMixin.set_status_raw()` 随之失去唯一调用方，一并删除（避免死代码）。

### 验证
- 新增用例 `test_prepare_progress_callback_passes_counts_only`：断言进度回调
  只会以 `(done, total)` 两个参数触发。
- `ruff` 全绿；`pytest` 通过（含 GUI：进度状态栏按语言键渲染）。

## 2026-10-13（工程化：新增 GitHub Actions CI）

### 修改
- 新增 `.github/workflows/ci.yml`：push / PR 时在 `ubuntu-latest` 与
  `windows-latest` 上运行 `ruff check .` 与 `pytest -m "not gui"`；Linux 侧额外
  安装 `python3-tk`（`tests/conftest.py` 顶层 `import tkinter` 需要它）。
  需要真实 tdata 的用例因找不到 tdata 自动 skip，GUI 用例按标记排除——
  CI 保证的是"纯逻辑 + 单测"这条线不退化。

### 验证
- 本地以同一选择执行 `pytest -m "not gui"`：**32 passed / 15 deselected**；
  `ruff check .` 全绿。

## 2026-10-13（优化：复用 CacheReader，避免重复派生 LocalKey）

### 背景
LocalKey 的派生（PBKDF2-HMAC-SHA512）开销不小。一次扫描里它被请求了两次：
`cache_source.prepare()` 内部新建一个 `CacheReader`，扫描完成后
`CacheApp._msg_load_done()` 又新建一个——平白多跑一遍 PBKDF2。

### 修改
新增 `cache_source.get_reader(tdata, passcode)`：按 `(tdata, passcode)` 复用同一个
`CacheReader`，**只保留最近一个**（避免内存里同时驻留多份密钥）。`prepare()`、
`CacheApp` 的扫描回填/保存/单条导出，以及 `save_items`/`export_one` 的默认路径
统一改走它。

### 验证
- 新增用例 `test_get_reader_reuses_same_instance`：统计 `CacheReader` 构造次数，
  断言同一 `(tdata, passcode)` 只派生一次、且返回同一实例。
- `ruff` 全绿；`pytest` 通过。

## 2026-10-13（修复：等待验证码/密码不再阻塞事件循环）

### 现象
API 模式登录时若弹出「验证码 / 两步密码」输入行，在用户填写（或超时）之前，
「取消」按钮与所有其它后台协程都不再推进——界面看似卡住。

### 根因
`Prompter.ask()` 直接在 asyncio 事件循环线程上 `Event.wait()`。该方法由协程
（`Worker.login`）调用，于是等待期间整个事件循环被占住，`on_cancel` 置位的
取消信号、其它协程都无法被调度（此前只能用 `PROMPT_TIMEOUT` 兜底）。

### 修改
`Prompter.ask()` 改为 `async`，用 `asyncio.to_thread()` 把阻塞等待放到**独立
线程**；事件循环保持可调度，取消信号与其它协程照常推进，超时安全网保留。
`login()` 的两处调用改为 `await`。

### 验证
- 新增无需显示器的回归用例 `test_prompter_ask_does_not_block_event_loop`：
  发起 `ask` 后在**同一事件循环**调度一个心跳协程，断言它在 `provide` 之前就
  能执行。**已确认把等待改回"直接阻塞循环"时该用例失败**，修复后通过。
- 更新 `test_gui_online` 的验证码回填分支为 await 语义（走
  `run_coroutine_threadsafe`）。
- `ruff` 全绿；`pytest` 通过。

## 2026-10-13（修复：切换「性能档位」会卡死排队中的缩略图）

### 现象
扫描完成后、缩略图仍在按需生成时切换「性能档位」，部分格子会**永久停在
占位图/空白**，须重新扫描或清理缓存才恢复。

### 根因
`_apply_gear()` 重建工作线程池时用 `cancel_futures=True` 关停旧池，被取消的
排队任务**不会投递结果**（`_request_thumb` 的 `work()` 根本没机会执行），其
下标却仍留在 `thumb_pending` 里；而 `_thumb_for` 见到 `idx in thumb_pending`
就直接返回占位图、**不再重新请求**——于是这些下标永久失联。

### 修改
`_apply_gear()` 关停旧池后，若仍有待载下标，统一 `thumb_pending.clear()` 并按
当前可见区 `refresh_view()`，由**新池**重新认领（可见格子立即重发，不可见的
滚到时再发）。运行中的旧任务仍会自行投递结果，重复投递同一张图无副作用。

### 验证
- 新增回归用例 `test_gear_change_does_not_strand_pending_thumbnails`：用单线程
  档位制造"排队"，改档位后断言排队中的那条最终仍拿到缩略图。**已确认在旧实现
  下该用例失败**（`thumbs=[0] pending=[1]`，即下标 1 失联），修复后通过。
- `ruff` 全绿；`pytest` **45 passed**（新用例 +1）。

## 2026-10-13（扫描并行化：让「性能档位」真正作用于扫描）

### 背景
「性能档位」此前只决定 GUI **按需**出缩略图的线程池大小，而**扫描本身是串行的**——
档位对最耗时的首次扫描完全不起作用（档位显示 8 线程，冷扫描仍约 12.6s）。

### 修改
- `cache_source.prepare()` 新增 ``workers`` 参数：零 IO 的索引复用/格式过滤仍在主线程直接
  判定；真正耗时的**读盘、解密、缩略图生成**交给线程池（Pillow/Crypto 会释放 GIL，能并行）。
  结果按**原顺序**汇总，因此 ``workers=1`` 与旧版逐项串行的**输出完全一致**（确定性红线）。
- 拆出 `_process_one()` 纯工作函数：只读写属于自己的数据（自己的条目与缩略图文件），
  不触碰共享状态，可在工作线程调用；取消时不再领取新结果，且不写索引。
- 链路打通：`cache_picker.load_cache(..., workers=)`；`on_load()` 按当前档位传线程数。

### 验证
- 新增回归用例：并发(8) 与串行(1) 的**条目顺序、类型/大小、统计逐项一致**；以及
  「档位 → 扫描线程数」的界面接线用例。
- 实测（3055 条媒体，冷扫描）：**12.57s（1 线程）→ 4.79s（8 线程），提速 2.6x**；
  暖扫描（全复用）不受影响，仍约 1.2s；并发下条目顺序与串行完全一致。
- `ruff` 全绿；`pytest` **44 passed**（30 非 GUI + 14 GUI，含真实 tdata）。

## 2026-10-13（输出类型：新增三态「全选」；修复筛选导致「全部扫描」变慢）

### 新增
- **「全选」复选框**（在「输出类型」格式区左侧）：默认全选；点击一键勾选/取消勾选全部格式；
  单个格式勾选变化时**实时**同步其 全选 / 半选 / 全不选 三态（由每个格式变量的 write 追踪
  驱动，无需重启任务）。
  - Tk 8.6 的 `ttk::checkbutton` **不支持**三态，故这一个用经典 `tk.Checkbutton`
    （`tristatevalue=""` 表示半选），配色与相邻的 ttk 复选框对齐。
  - 点击意图按"各格式当前是否已全选"决定，而非控件改写后的变量值，因此三态只作**显示**，
    点击始终是干脆的全勾/全取消。

### 修复：筛选后再「全部扫描」的性能退化
- **现象**（实测，3055 条媒体）：普通「全部扫描」（索引已热）原本约 0.4–1.2s；但**先按格式
  筛选、再「全部扫描」**会退到 1.84s（筛掉的是视频），甚至 **7.57s ≈ 冷扫描**（筛掉的是
  图片）。
- **根因**：按格式筛选时索引只写入选中的格式，**其它格式的记录被丢弃**；下次全扫这些条目
  没有索引可复用，图片必须重新解密并重新生成缩略图。
  - 用隔离实验确认：把"扫描后清理缩略图"关掉后该场景仍为 7.57s，故主因是**索引收缩**，
    而不是缩略图清理。
- **修复（性能优先，经用户确认）**：
  - 筛选扫描**不再丢弃**其它格式**仍然有效**（mtime 一致）的索引记录——本次结果仍只含所选
    格式，但切回该格式/「全部」可直接复用；
  - **移除**扫描热路径上的缩略图清理（它只额外贡献约 0.2s，却会删掉本可复用的缩略图）；
    空间回收改由「清理缓存」按钮承担。
- **效果**：video-only → 全部扫描由 7.57s 变为 **1.21s**，且 `reused=3060`（**完全复用**，
  与热扫描同量级）。

### 验证
- `ruff` 全绿；`pytest` **42 passed**（29 非 GUI + 13 GUI，含真实 tdata）。
- 新增/更新用例：「全选」三态联动；筛选后**索引记录保留**、切回「全部」完全复用（回归测试）。
- 实测复核：full(热)→full 复用 3060/3060；jpg-only/video-only 各自只输出所选格式（2112 / 557）
  且索引保持 3060 条。

## 2026-10-13（本地缓存模式：按格式勾选 / 导出内容 / 开始扫描按钮 / 档位自动匹配）

### 背景
承接上一条。原「类型」是「全部/图片/视频」下拉，粒度不足以满足"**不扫描 webm 等特定格式**"
的需求；且进入页面后没有醒目的扫描触发按钮（「读取缓存」易被读成"读取路径设置"）。

### 修改
- **输出类型改为按格式勾选**：新增 9 个格式勾选框（`mp4/webm/avi/mkv/mov/jpg/png/gif/webp`，
  默认全勾），未勾选的格式在扫描时**早停**（不解密、不出缩略图、不入索引）。为让 **mkv 与
  webm 能被分别勾选**，`cache_source.sniff()` 补齐 EBML 的 DocType 判定（含 `matroska` →
  `.mkv`，都找不到默认 `.mkv`）。`prepare()` 的过滤参数由 `kinds` 改为按扩展名的 `exts`
  （**复用路径同样生效**）。
- **导出内容下拉**：原「类型 图片/视频」控件改为「**导出内容**」（全部/图片/视频），保留
  原有下拉交互，只决定**预览显示与导出**范围——扫描范围由格式勾选决定，二者解耦。
- **「开始扫描」按钮**：原「读取缓存」更名为「开始扫描」，语义明确直接；「重新扫描」保留为
  "丢弃索引后重扫"。一个格式都没勾时给出提示且不发起扫描。
- **性能档位自动匹配 + 低配钳制**：启动时按 `detect_hardware()` 结果**自动匹配推荐档位**；
  新增 `ui_kit.gear_workers()`：核数未知 → 1、内存 <4GB 时「极速」收敛到推荐值，避免低配
  设备因线程设置过高出现运行异常。格式与档位选择一并写入 `cache_config.json`。

### 验证
- `ruff` 全绿；`pytest` **40 passed**（28 非 GUI + 12 GUI，含真实 tdata）。
- 新增/更新用例：EBML 嗅探区分 webm/mkv、按格式过滤（含"排除 webm"）、未勾选任何格式时提示
  且不进入忙碌态、档位低配钳制规则。
- 实机核对：默认 9 格式全勾；只勾 mp4/jpg/png → 扫出 2616 条（.jpg 2112 / .mp4 504，**无 webm**）；
  32 核 / 15.7GB → 均衡档 8 线程（= 推荐值）。

## 2026-10-13（本地缓存模式：移植上游的输出类型 / 性能档位 / 打开目录，移除打开即自动提取）

### 背景
上游 tg-scoop 的 GUI 有三块可复用逻辑：输出类型过滤（`MediaType` + `allowed_types` 早停）、
性能档位（硬件检测 + 并发数推荐）、打开目录（`os.startfile` + 失败兜底）。本项目原先会在
打开本地缓存模式时**自动全量读取缓存**（`root.after(250, on_load)`），既慢又与文档「需点
『读取缓存』」不符。本次把三者移植过来，并移除自动读取。

### 修改
- **移除打开即自动提取**：删掉自动 `on_load`；解密/出缩略图改由用户点「读取缓存」触发
  （实测：启动建窗 0.18s、0 条自动提取；按需读取 0.58s / 3060 条）。
- **输出类型下拉菜单**：原「类型」分段按钮改为只读下拉（全部/图片/视频，内部键
  `all/image/video`）；所选类型既是显示筛选，也是「读取缓存」的**提取范围**。
  `cache_source.prepare()` 新增 `kinds` 参数：命中集合外类型嗅探后**早停**（不解密、
  不出缩略图、不落索引）；**索引复用路径同样生效**，否则过滤会被旧索引绕过。
- **性能档位**：新增下拉（保守 1 线程 / 均衡推荐 / 极速满核）控制后台缩略图工作线程数；
  `ui_kit` 新增 `detect_hardware()`（CPU 核数 + 内存，失败按未知、永不抛）与
  `recommended_workers()`（`min(cores-1,8)`、<4GB 收敛到 2、保底 1）给出推荐值；切换档位
  即按新线程数重建线程池，选择写入 `cache_config.json`。
- **打开目录**：底栏新增按钮，用系统文件管理器打开「保存到」目录；路径不存在时提示，
  启动外部程序失败（本机对 shell open 受限）时**不崩**——把绝对路径写入剪贴板并提示。

### 验证
- `ruff` 全绿；`pytest` **36 passed**（26 非 GUI + 10 GUI，含真实 tdata）。
- 新增/更新用例：输出类型过滤（含索引复用路径）、硬件检测与档位推荐规则、打开目录失败
  回退到剪贴板、目录不存在时提示且不尝试打开；缓存 GUI 用例改为显式触发读取。
- 两份语言资源各新增 7 个键（`gear.*` ×3、`cache.toolbar.gear_label`、`cache.bottom.open_dir`、
  `cache.status.opened_dir` / `path_copied`），键集合一致；`test_i18n` 动态键前缀加入 `gear.`。
- 实机核对：类型=视频 → 只读出 557 条视频；档位 均衡 8 / 极速 32(=核数) / 保守 1。

## 2026-10-13（模式卡片改为整块可点）

### 背景
模式选择界面的两张卡片里只有**模式名那一小块**能点，说明文字与卡片留白都不可点，
用户第一眼会以为整张卡片都能点，实际点了没反应。

### 修改
- `launcher.py` 的卡片改为**整体可点击**：卡片底、标题、说明文字（及周围留白）
  统一绑定 `<Button-1>`，点任意处即进入该模式。
- 悬停时**整张卡片提亮**（卡片底色 + 标题/说明同步换色）并显示手型光标，
  明确传达"整块可点"。
- 每张卡片底部增加「点击进入 →」提示（新键 `launch.mode.enter`，两份资源同步）。
- 删除只覆盖单行的 `Mode.TButton` 样式（已被整块卡片取代）。

### 验证
- `tests/test_gui_launcher.py` 新增用例：断言说明文字上已绑定点击、悬停样式存在、
  点说明文字能进入缓存模式。
- 两份语言资源各新增 1 个键，键集合保持一致；`ruff` 全绿、`pytest` 通过。

## 2026-10-13（合并为统一启动入口：先选模式）

### 修改
- 原先两个独立入口（`run_cache.cmd` → `cache_picker.py`；`run.cmd` → `tg_picker.py`）
  合并为**单一启动入口**：新增 `launcher.py`，双击 `run.cmd` 后先展示**模式选择界面**
  （「本地缓存模式」/「API 模式」两张卡片），选定后进入对应模式的完整功能流程。
- 删除 `run_cache.cmd`；`run.cmd` 改为调用 `launcher.py`（仍为 pythonw 无窗口启动）。

### 实现要点
- 复用**同一个 Tk 根窗口**：`tg_picker.App` / `cache_picker.CacheApp` 本就接受一个
  既有的 root 并在其中构建界面，因此选择后销毁选择界面、把该模式构建进同一 root——
  避免同进程出现多个 Tk 解释器（易触发 Tcl 初始化错误），且**不改动**任何模式既有逻辑。
- 两个模式模块**延迟导入**：只用本地缓存模式不会拉起 telethon，只用 API 模式不会
  拉起 tdata 解密链（与原有两个入口的依赖边界保持一致）。
- 模式选择界面复用 `ui_lang.LangMixin`，与两个模式界面共享同一套双语实时切换机制。
- 两个模式的 `main()` 与 `if __name__ == "__main__"` 保留，仍可单独开发/调试。

### 验证
- 新增 `tests/test_gui_launcher.py`（3 条）：模式选择界面构建、英文模式无中文残留、
  选择后分别装载 `CacheApp` / `App`。
- `tests/test_i18n.py` 的 `SOURCES` 纳入 `launcher.py`，键覆盖自检覆盖到新入口。
- 两份语言资源各新增 7 个键（`app.launcher_title` + `launch.*`），键集合保持一致。
- `ruff` 全绿；`pytest` 通过（23 非 GUI + 7 GUI，含真实 tdata 的缓存模式用例）。
- `run.cmd` 仍为纯 ASCII / CRLF / 无 BOM（逐字节确认）。

## 2026-10-13（启动改为无控制台，报错写入日志文件）

### 修改
- 两个 `.cmd` 改用 `pythonw.exe` 无窗口启动（`start ""`）：双击后**不再出现控制台窗口**。
- 新增 `ui_kit.enable_file_log()`：`pythonw` 进程没有控制台，`sys.stdout` / `sys.stderr`
  都是 `None`，任何 traceback 与 `print(..., file=sys.stderr)` 都会**无声丢失**；现在由
  程序在启动时把它们接到 `logs\cache_picker.log` / `logs\tg_picker.log`（仅在没有控制台
  时接管；从终端用 `python.exe` 运行仍保留控制台输出，便于开发）。日志目录已入
  `.gitignore`。
- **没有**在 `.cmd` 里做 `>log` 重定向：批处理的重定向依赖句柄继承与引号，在 Windows
  上容易出岔子——本项目刚因为 `.cmd` 里的非 ASCII 踩过坑，这类职责改由 Python 侧承担。
- 新增 2 条单元测试覆盖 `enable_file_log`（有/无控制台两种情况）。

### 验证
`ruff` 全绿、`pytest` 通过；两个 `.cmd` 仍为纯 ASCII / CRLF / 无 BOM（逐字节确认）。

## 2026-10-13（修复 .cmd 中文注释导致的解析错误）

### 现象
`run_cache.cmd` 启动时，控制台开头报：
`'<乱码>' 不是内部或外部命令，也不是可运行的程序或批处理文件。`

### 根因
两个 `.cmd` 存为 UTF-8（无 BOM），而 cmd.exe 按**系统 OEM 代码页**（中文 Windows 为
GBK/936）解析批处理。`rem` 行里的中文被按 GBK 误解码后字节错位，吞掉了下一行的
`rem` 前缀，使那行被当作命令执行。此前 `run_cache.cmd` 用 `pythonw` + `start`，
窗口一闪即退、报错看不见；改为保留控制台后暴露出来（即该问题一直存在）。

### 修改
- `run_cache.cmd` / `run.cmd` 内容改为**纯 ASCII**（英文注释与提示），彻底消除对
  代码页的依赖；仍保持 CRLF、无 BOM。
- 代价：`.venv` 缺失时的提示由中英双语改为纯英文——该提示只在环境没装好时出现，
  正常情况下看不到。

### 验证
逐字节确认：两个文件 `non_ascii=0`、无 BOM、全部 CRLF。本环境禁止拉起 `cmd.exe`
（工具侧拦截），最终需人工双击确认。

## 2026-10-13（统一两个启动脚本与窗口尺寸）

### 修改
- `run_cache.cmd` 由 `pythonw.exe` 改为 `python.exe`，与 `run.cmd` 行为一致：双击后
  **保留控制台窗口**，便于查看日志与报错（此前它用 `pythonw` + `start`，双击时黑窗
  一闪即退，用户看不到任何输出）。`.venv` 缺失的提示与失败时 `pause` 也保持一致。
- 两个 GUI 窗口尺寸统一为 **1280×880**（最小 1020×680）：在线挑选器原本是
  1180×820（最小 940×620）。

### 说明
- 改用 `python.exe` 不会重新引入「加载视频缩略图时闪控制台窗口」的老问题——
  ffmpeg 子进程早已带 `CREATE_NO_WINDOW` / `STARTUPINFO(SW_HIDE)`。

## 2026-10-13（移除「打开 / 复制路径」功能）

### 背景
「打开目录」的四种实现方式在用户机器上全部失败（见下一条记录），曾改为「复制路径」
兜底；用户明确不需要这个功能——「不用单独给个复制按钮，把打开路径这个功能也删掉」。

### 修改
- 删除 `ui_kit.copy_path()`，以及 `tests/test_ui_kit.py` 中 3 条对应用例
  （该文件只保留 `unique_path` 用例）；
- 缓存挑选器：底栏移除「复制路径」按钮与 `on_copy_path()`；「保存完成」不再询问是否
  复制路径，只在状态栏显示结果；
- 在线挑选器：移除双击后的复制路径投递、`_msg_copy_path()` 与「下载完成」询问；
  `download_single()` 去掉仅为此存在的 `open_after` 参数；
- 删除语言键 `common.copy_path` / `cache.status.path_copied` /
  `online.status.path_copied` / `cache.dialog.save_done_ask` / `online.dialog.done_ask`，
  并把 `online.status.ready_hint` 改为「双击下载原件」（两份资源各 **138** 键）。

至此「启动外部程序 / 写系统剪贴板」这两条代码路径在项目里彻底消失。

## 2026-10-13（修复「打开目录」不可用 + 新增「清理缓存」按钮）

### 修复
「打开目录」打不开。为了修它先后试了三种方式，最终结论是**目录必须在本进程内
直接调 `os.startfile`**：

1. 隔离到子进程 + `CREATE_NO_WINDOW` / `STARTUPINFO(SW_HIDE)`：这两个标志让子进程
   失去窗口站/桌面关联，其内部的 `ShellExecute`（`os.startfile` 的实现）直接失败
   ——无论传绝对路径还是相对路径都打不开；
2. 去掉标志、改为直接 spawn `explorer.exe <目录>`：在用户机器上 explorer 自己就
   起不来，报 `0xc0000142`（进程初始化失败）；
3. 进程内直接 `os.startfile`：这条从项目一开始就在用，且**目录打开从未与那次原生
   崩溃相关**（0xC0000409 只发生在把**媒体文件**交给播放器/看图程序时）。

**最终决定：放弃「打开」，改为「复制路径」**（用户授权：不行这个功能就不要了）。

用户实机验证第 3 种方式（进程内 `os.startfile`）同样失败，报 `[WinError 5] 拒绝访问`
——即这台环境对「shell open」整体受限，再换第 5 种调用方式也不会更好。继续试错已无
性价比，于是：

- 新增 `ui_kit.copy_path(path, widget, root=None)`：把**绝对路径**写入剪贴板并返回该
  路径，完全不依赖任何外部程序与 shell，不会崩、不会被拦；调用方在状态栏/日志提示
  「已复制到剪贴板」，用户粘贴到资源管理器地址栏即可到达目标；
- **删除** `open_dir` / `open_file` 以及随之而来的隔离子进程、stderr 回收、
  `_open_detached` / `_msg_open_failed` 与相关语言键——「启动外部程序」这条代码路径
  在项目里彻底消失，这一类故障（0xC0000409 崩溃、WinError 5、0xc0000142）从此没有
  发生的入口；
- 缓存挑选器底栏按钮、在线挑选器的「下载完成」询问与双击项，全部改为复制路径；
- `tests/test_ui_kit.py` 改写：以假控件断言复制的是**绝对路径**、相对路径按 `root`
  解析、目标不存在也能复制（这三点正是它比「打开」可靠的原因）。

### 新增
「清理缓存」按钮（在「重新扫描」旁边）：

- 清掉**缩略图缓存 / 索引 / 抽帧临时目录**，并显示释放了多少空间；
- 只删这些**会自动重建**的产物——不碰 tdata，也不碰你导出的媒体；
- 清理后可见区的缩略图按需重新生成。实现上有个顺序要点：先隐藏 Canvas 图元
  （解除对 Tk 图像的引用）再丢弃内存中的缩略图，反了会留下悬空引用
  （历史上就是这条导致 `image "pyimage…" doesn't exist` 刷屏）；
- 忙碌时按钮禁用（`set_busy` 一并管理）。

### 验证
新增用例 `test_clean_cache_removes_only_cache_artifacts`：造出缩略图/索引/抽帧目录
与一个「用户导出的媒体」，断言前者被删、释放字节数正确、后者**原样保留**。
`ruff` 全绿；`pytest` 通过。

## 2026-10-13（调整：双击预览改为「导出到保存目录」）

### 背景
双击预览依赖**系统默认程序**打开文件，而那一步正是崩溃/卡死的高发点（见下一条记录
的排查）。用户的思路是：不要让工具去做这件麻烦事——双击时问一句「是否导出」，
确认后直接把文件导到本地，自己去查看即可。

### 修改
- 双击改为：先弹窗确认（对话框里显示目标目录与文件名）→ 确认后解密**导出到
  「保存到」目录**（与「保存选中」同一去处、同一套重名加后缀规则），完成后只在
  状态栏给出完整路径；**不再调用系统默认程序打开**。要看图/播放时点底栏「打开目录」。
- 新增 `export_one()`，并让 `save_items()` 复用它——「单条导出」与「批量保存」从此
  共用同一份「解密 → 去重命名 → 落盘 → 对齐时间戳」实现，避免两套逻辑漂移。
- 移除不再需要的 `open_preview()` 与 `PREVIEW_DIR`：不再产生 `preview/` 目录，
  `.gitignore` 中的对应规则一并删除。
- 「保存到」的默认目录由中文 `挑选保存` 改为英文 `picked`，中英界面统一，避开中文路径
  可能带来的问题（**已保存过 `out_dir` 的配置不受影响**）。
- i18n 键同步调整：新增 `cache.dialog.confirm_export` / `cache.dialog.export_failed` /
  `cache.status.exporting|export_done|export_failed`，删除 preview 系列键。

### 验证
- `ruff check .` 全绿；`pytest` 通过（原 `open_preview` 用例改写为 `export_one`，
  覆盖「导出的是媒体」与「重名不覆盖」）。
- 双击导出后的实际观感需在用户机器上确认（本环境 ShellExecute/弹窗受沙箱限制）。

## 2026-10-13（修复：双击预览导致崩溃 / 卡死）

### 现象
双击部分图片或视频时，工具窗口先「无响应」，随后整个进程消失。

### 定位（实测证据）
- **Windows 事件日志**：`pythonw.exe` 崩溃，故障模块 `ucrtbase.dll`，异常码
  `0xC0000409`（CRT `__fastfail`），两条记录的偏移完全相同 → 原生层致命错误，
  Python 捕获不到、也不会有 traceback。
- **排除数据路径**：按预览的代码路径处理全部 **3056** 个缓存媒体（解密 + 写盘 +
  视频抽帧），退出码 0、无崩溃 → 与文件内容/大小/解密/ffmpeg 无关。这条同时否定了
  最初「大文件撑爆内存」的猜测（本机视频最大仅 5.8 MB、图片最大 0.5 MB）。
- **排除 GUI 侧**：驱动真实 GUI 连续预览 60 个（图片/视频交替），仅把
  `os.startfile` 换成空实现 → 退出码 0、无崩溃。
- 两次实验与真实崩溃的**唯一差异**就是 `os.startfile()` 真正执行的那一步。
- 另注：本开发环境里 ShellExecute 被拦截（`PermissionError WinError 5`），
  因此真实调用无法在此复现；主进程与新增子进程调用的异常行为完全一致。

### 修改
- 新增 `ui_kit.open_path()`：把「用系统默认程序打开」**隔离到一次性子进程**里执行
  （Windows 用 `os.startfile`，macOS 用 `open`，Linux 用 `xdg-open`，并隐藏子进程
  控制台）。这样即使 shell 扩展/杀软钩子把进程搞崩、或 ShellExecute 被长时间拖住，
  界面都毫发无损；同时消除了「卡住时 Tk 主线程无响应」的问题。
- `cache_picker` / `tg_picker` 里全部 `os.startfile` 调用改走它；启动失败（例如该
  类型没有关联程序）会在后台线程里被发现并照旧提示。
- 删除因此不再被引用的 i18n 键 `cache.dialog.cannot_open`，新增
  `cache.dialog.open_unsupported`。

### 验证
- `ruff check .` 全绿；`pytest` **23 passed**。
- `pythonw` 启动回归通过；`open_path` 的子进程管道与失败检测已单独验证。
- **真实「打开文件」行为需在用户机器上双击确认**（本环境 ShellExecute 被拦截，
  无法自动验证）——与 `.cmd` 换行符那次同理，凡涉及系统 shell 的改动都要人工确认。

## 2026-10-13（修复：双击启动脚本无效）

### 背景
双击 `run_cache.cmd` 只弹出一个命令行窗口后就挂住、应用不启动。

根因：该文件是 **LF 换行**（Unix 风格），而 `.cmd` 需要 CRLF。cmd.exe 解析带括号的
多行 `if (...)` 块时会一直往下找闭合括号，把后面的 `start` 行一并吞掉，因此永远走不到
启动应用那一步；而缓存挑选器用 `pythonw` 启动（无控制台），失败是完全静默的。

### 修改
- `run_cache.cmd` / `run.cmd` 换行符归一为 CRLF（无 BOM）。
- 两者改用 `%~dp0` 为基准的绝对路径（`set "PYW=%~dp0.venv\Scripts\pythonw.exe"`）
  并用引号包裹调用，不再依赖当前工作目录。

### 验证
用户手动双击确认可正常打开。

> 说明：这个问题**无法在本项目的开发环境里自动验证**——该环境禁止拉起 `cmd.exe`，
> 先前两次用 `Start-Process run_cache.cmd` 的尝试都因这条限制而失效（并非脚本本身报错）。
> 之后涉及 `.cmd` 的改动都需要人工双击确认。

## 2026-10-12（工程化重构：修缺陷、去重复、规范化）

### 背景
功能已完整、测试通过，但作为「可直接交付」的项目仍有欠缺。经逐行审查（两个 GUI + 领域层）、
项目级规范审查与 AST 静态检查，发现三类问题：**真实缺陷**（不是风格问题）、**重复实现与
命名不一致**、**项目级规范缺失**（无 pyproject/ruff/pytest 配置、无 .gitattributes，
换行符混乱；测试散在根目录且 `_smoketest_cachepicker.py` 退出码恒为 0，失败会被当成通过）。

### 修改（缺陷修复，不改变产品行为）
- **读取中点「重新扫描」会白删索引**：`on_reload` 先删索引再调 `on_load`，而后者在 busy 时
  提前返回 → 索引丢失却不重扫。改为先判 busy。
- **保存失败会连弹两个对话框**：失败路径同时投递 `load_error` 与 `save_done`，后者还用成功
  文案。现在只报错一次。
- **在线挑选器可能永久死锁**：`Prompter.ask` 在 asyncio 事件循环线程里做无超时阻塞等待，
  用户不填验证码就永不苏醒、busy 永不复位。改为带 `PROMPT_TIMEOUT` 超时，
  且登录全程响应「取消」（此前取消按钮对登录完全无效）。
- **两个 GUI 的轮询会永久停摆**：`_pump` 把 `after()` 排在循环之后且无保护，任一回调抛异常
  即断掉 after 链、界面假死。改为单条消息隔离异常 + 兜底重排。
- 其余：`prepare()` 被取消时不再写半成品索引；`_on_close` 取消所有 after 任务并注销语言监听；
  预览文件重名不再覆盖；卡片右下边缘像素不再被判成间隙（点击失效）；预览加重入保护；
  邀请链接解析失败不再把完整 URL 丢给 `get_entity`（必然失败）。

### 修改（结构）
- 新增 `ui_kit.py`（叶子模块）：`human_size` / `unique_path` 原先在 `cache_source.py` 与
  `tg_picker.py` 各有一份且行为不一致，现统一；`load_config` / `save_config` 也由两个 GUI
  各写一份改为共用（读取失败不再静默吞掉，改为告警一次）；共享字体常量。
  **刻意不合并**两套配色与缩略图边长（会改变视觉输出）。
- 新增 `ui_lang.py`（`LangMixin`）：两个 GUI 曾各自重写一遍的 i18n 界面机制（`_T` 登记、
  `retranslate`、语言按钮、可重放状态栏）收敛为一份，差异留给子类钩子。
- `cache_picker.Item` 更名为 `MediaItem`（与在线挑选器同一概念对齐）。
- 拆分超长函数（逐行搬运、不改逻辑）：`_build_style` 140 行拆为 7 个按控件类别的样式函数；
  两个 `_handle` 拆为按消息类型分发的 `_msg_*`。补齐缺失 docstring 与类型标注，
  修正「注释称上边距 7、实现是 8」这类不一致。
- 测试迁入 `tests/` 并改写为 pytest（见下）。

### 修改（工程规范）
- 新增 `pyproject.toml`（项目元数据 + ruff/pytest 配置；依赖只在 requirements 里声明，
  避免两处漂移）、`requirements-dev.txt`、`.editorconfig`、`.gitattributes`
  （源码 LF、`.cmd` CRLF）。
- `requirements.txt` 补版本上界。
- 根目录 5 个 `_selftest_*.py` / `_smoketest_*.py` 脚本迁移为 `tests/` 下的 pytest 用例；
  统一由 `tests/conftest.py` 处理导入路径、tdata 与显示器缺失时的 skip。
  **修掉假阳性**：缓存 GUI 测试原先只写日志、退出码恒为 0，现改为真实断言。
  同时移除一个会偶发失败的耗时断言（只断言索引复用率）。

### 验证
- `python -m pytest`：**23 passed**（真实 tdata：读取 3000+ 媒体、保存/重名不覆盖/按日期
  分目录/预览/索引复用率、tdata 只读；两个 GUI 端到端含滚动性能与语言切换；
  i18n 键一致性与英文模式无中文残留）。
- `python -m ruff check .`：全部通过。
- **ruff 抓到一处重构引入的真实缺陷**：`cache_picker.py` 被误删 `import sys`，而
  `_default_tdata()` 仍在使用它——平时都有已保存的 tdata 路径，走不到该分支，测试不会暴露。
  已修复，并把它改为静态方法 + 补一条无需 GUI 的回归测试。

## 2026-10-12（中英双语国际化）

### 背景
界面此前是纯中文硬编码：两个 GUI 与 `cache_source.py` 里零散分布着约 140 条用户可见
文案，没有语言资源文件，也没有任何字符串集中机制——非中文用户无法使用，改动文案
需要在多处同步。`cache_config.json` 里虽有一个 `lang` 键，但从未被任何代码读取。

### 修改
- 新增 `i18n.py`：轻量翻译内核（无第三方依赖）。语言资源放 `locales/<lang>.json`，
  取词统一走 `t(key, **kw)`；当前语言缺失时回退默认语言（中文），再缺则原样返回键名，
  **永不抛错**。`set_language()` 切换后通知已注册的监听者，两个 GUI 据此实时刷新。
  语言字典"先在局部构造完再单次赋值发布"，后台线程随时取词都不会读到半更新状态。
- 新增 `locales/zh.json` 与 `locales/en.json`，各 **142 个键**，键集合与占位符严格一致；
  命名空间为 `app / lang / common / kind / sort / cell / cache / online / cs`。
- 两个 GUI 各实现 `retranslate()`：构建期用 `_T(widget, key)` 登记静态文案，切换语言时
  统一重设；运行时文本改由对应函数重算。头部新增 `中文 ｜ English` 分段按钮。
  语言偏好写入两工具**共享**的 `language.json`（已入 `.gitignore`）。
- `cache_source.py` 的错误文案与索引进度改走 `t()`；内部标识（`human_size` 单位、
  文件名前缀 `tg_`、`sniff` 的 kind/ext）保持不译。
- **排序键解耦**（本次最大隐患）：原先下拉框直接把中文文案当 key 并持久化到
  `cache_config.json`。现引入内部键 `date_desc/date_asc/size_desc/size_asc/source`，
  显示文案随语言变化、逻辑只看键；`_load_config` 会把旧的中文值自动迁移为内部键。
- 修掉若干"切换语言后不刷新"的缺口：类型分段按钮文案（原来只切样式不切文字）、
  `tg_picker` 的登录状态标签（原先丢弃了用户名，无法重建）、待填写的验证码/密码提示
  （`Prompter.ask` 改为传**语言键+参数**而非成文文案）、以及状态栏（改为记录
  key+参数，切换语言时按新语言重放）。后台线程只投递语言键，由主线程渲染。
- 英文文案比中文长，相应放宽了 `l_count`、三处字段标签、排序下拉的宽度，避免截断。
- `run.cmd` / `run_cache.cmd` 的虚拟环境缺失提示改为中英同行。

### 验证
- 新增 `_selftest_i18n.py`（无需 tdata、无需联网）：校验两份资源键集合一致、取值非空且
  占位符逐条一致、源码中 `t("...")` 字面量键全部有资源、资源键全部被引用、动态拼接键
  （`sort.*`/`kind.*`）齐备、切换语言的通知与回退行为正确；并在**英文模式下**遍历两个界面的
  控件树（含 Combobox 取值与 Canvas 角标文字），确认**无中文残留**。
- 在原有测试中追加语言切换步骤（只调 `set_language`，不写用户语言偏好）：
  `_smoketest_gui.py` 通过；`_smoketest_cachepicker.py` 在真实数据上通过（切英文后排序下拉、
  计数、主按钮全部更新，且排序内部键不变、排序逻辑仍可用，再切回中文）。
- 功能回归：`_selftest_cache.py`、`_selftest_cachepicker.py` 全部通过（读取 3056 个媒体、
  保存 5/5、按日期分目录、预览、索引复用 100%、tdata 只读未变）。

## 2026-10-12（聚焦定位：移除文件夹挑选器）

### 背景
产品定位明确为「**绕过 Telegram 的禁止保存限制**」。原先还有一个
"文件夹挑选器"（浏览本地已导出图片文件夹），它与该定位无关，且属于
"先整批导出、再挑一遍"的多余步骤——缓存挑选器已能直接读缓存边看边挑，
不需要中间那一步导出。

### 修改
- 移除 `gallery_picker.py`（955 行）、`run_gallery.cmd`、
  `_selftest_gallery.py`、`_smoketest_gallery.py`
- `cache_picker.py` 原先从 `gallery_picker` 借用 `human_size` / `unique_path`
  两个通用函数，已把这两个函数移入 `cache_source.py`，解除耦合
- 清理 gallery 专用产物：`thumbs/` 缓存目录、`gallery_config.json`
- 清理 `.gitignore` 中 gallery 相关条目
- README 重写开篇：说明「禁止保存」的原理（服务器拒绝**转发**、
  但保存只是**请求客户端自律**），并把两种方法（缓存 / 在线）并列；
  新增「致谢」章节专门感谢上游 tg-scoop 把解密算法写清楚

### 验证
移除后回归：`_selftest_cache.py`、`_selftest_cachepicker.py`、
`_smoketest_gui.py`、`_smoketest_cachepicker.py` 从 `C:\` 运行全部通过。

## 2026-10-12（自包含：内置解密内核）

### 背景
此前 `cache_source.py` 依赖一个**外部目录**（tg-scoop 源码检出）提供解密模块，
导致「删掉那个目录就打不开工具」，也无法把 `tg-picker` 整体拷到别处运行。

### 修改
- 新增 `_vendor/tg_scoop/`，从上游 [GarrettFynn/tg-scoop](https://github.com/GarrettFynn/tg-scoop)
  （MIT）复制**依赖闭包内的 4 个文件**：`__init__.py`、`exceptions.py`、
  `tdata_reader.py`、`cache_decryptor.py`，并一并带上上游 `LICENSE`。
  未复制上游的 CLI/GUI/提取编排/MTProto 等与本工具无关的模块。
- `find_decryptor_src()` 的优先级明确为：
  `TG_SCOOP_SRC` 环境变量 → **`_vendor/`（默认）** → 同级 `tg-scoop/src`（开发兜底）。
- 缺失模块时的报错文案改为指向 `_vendor`，不再引导用户去找外部目录。
- 新增 `_vendor/README.md` 记录出处、许可、复制清单与"勿直接改上游副本"的约定。

### 验证（真正的独立性测试）
把外部 tg-scoop 检出目录**临时改名使其不可见**，在此前提下：
- 功能自检：读取 3034 个媒体、保存 5/5、按日期分目录、预览、索引复用 100%、
  **tdata 只读未变** —— 全部通过。
- GUI 端到端：载入 3034 个媒体（图 2477 / 视频 557）、缩略图生成 28 张
  失败 0、选中并保存成功写出文件，stderr 无输出。
- 解析确认 `find_decryptor_src()` 指向项目内的 `_vendor`。

确认无需外部目录后，已删除该检出目录（释放约 21 MB）。

## 2026-10-10（UI 重设计）

### 背景
界面被判定"乱"。经两轮独立视觉评审（含像素级测量），确认根因**不是配色方案，
而是 ttk clam 主题的自绘 chrome 从未被覆盖**，加上若干间距/对齐不统一。

### 根因（逐条实测）
| 现象 | 实测证据 |
|---|---|
| 输入框有亮灰白描边 | `#d3d0ca`，对面板底 10.2:1，像贴了白胶带 |
| 单选钮是白色实心圆盘 | clam 的 `Radiobutton.indicator` **没有任何可配置项**，`indicatorcolor` 被静默忽略 |
| 复选框是白色方块 | 同上；且其可配置项是 `upperbordercolor/lowerbordercolor`，**不是** `bordercolor` |
| 滚动条亮槽+亮箭头 | 只设了 `background/troughcolor`，未覆盖 `lightcolor/darkcolor/bordercolor` |
| 卡片之间无间隙、连成一片 | `CELL_W=168` 而矩形画到 `x+166` 且 `width=2`，恰好把 2px 缝填满 |
| 网格右侧留白不均匀 | 右余 61px（大窗）/ 133px（最小窗），左贴齐右空 |
| 进度条像空输入框 | 178×26，与输入框同色同尺寸且常驻 |
| 设置区与筛选栏分不开 | 两者同为 `#232329` 且**无任何分隔线**（全区只有标题下一条） |
| 文字有 6 套杂灰 | 副标题、提示、概览、标签、状态各用不同灰值 |

### 修改
- **压掉 clam chrome**：输入框补 `bordercolor/lightcolor/darkcolor`；
  复选框改用正确的 `upperbordercolor/lowerbordercolor`；滚动条与进度条补
  `lightcolor/darkcolor/bordercolor`。
- **单选钮改分段按钮**：clam 下无法给 indicator 配色，改用
  `Seg.TButton`（深灰底灰字）+ `SegOn.TButton`（蓝实底白字），选中态一眼可辨。
- **卡片几何分离**：新增 `CARD_W/CARD_H`（实际绘制）与 `CELL_W/CELL_H`（含
  `GAP=10` 步距），卡间真正露出内容区底色。
- **圆角改为真实圆弧**：原先每角 2 锚点 + `smooth=True`，实测视觉半径只有 3-4px；
  改为按 6 段采样圆弧、`smooth=False`，半径与 `CORNER=9` 一致。
- **网格水平居中**：新增 `_grid_x0`，右余从 61px 降到 18px、最小窗从 133px 降到 60px。
- **视频角标贴合图像**：原先锚在**容器**底边，letterbox 缩略图会让角标悬空在
  卡片底色上；改为锚在实际图像 bbox 内（已结构验证 6/6 全在图内）。
- **进度条改为按需显示**：新增 `_show_progress/_hide_progress`，空闲时不占位。
- **建立视觉体系**：文字收敛为三级色阶（`FG/SUB/MUTED`）；筛选栏独立底色
  `BAR`；分隔线 `SEP` 提到可感知的 1.6:1。
- **其他**：统一按钮间距节奏（标签固定宽度 10 使各输入框左缘对齐）；
  卡片信息行字号 8→9；选中卡信息行不再单独提级（消除同信息两种层级）。

### 验证
- 功能回归：缓存读取 3021 个媒体、保存 5/5、按日期分目录、预览、索引复用
  100%、**tdata 只读未变**——全部通过。
- 像素自检：输入框描边实测 `#4a4c53`（对比度 1.12:1 → 1.82:1）；
  复选框未选中边框 `#4a4c53`、选中 `#3b82f6`（近白像素已消除）。
- 结构验证：6 个视频角标 bbox 全部落在图像 bbox 内。
- 布局验证：1280×880 与 1020×680 下均无截断/重叠/塌陷，左右余量对称。

## 2026-10-10（修复：ffmpeg 闪窗）

### 修复：加载视频缩略图时不断弹出控制台窗口
- 现象：界面用 `pythonw.exe`（无控制台）启动，而 ffmpeg 是控制台程序，
  Windows 每次调用都会为它**新建一个控制台窗口**。缩略图是边滚动边生成的，
  每个视频还要试两种抽帧策略，于是表现为"一直在弹窗"，非常违和。
- 修复：给 ffmpeg 子进程传 `CREATE_NO_WINDOW` 与 `STARTUPINFO(SW_HIDE)`
  （`cache_source._hidden_subprocess_kwargs()`）。
  抽帧由多线程并发发起，故每次调用都新建 `STARTUPINFO`，不复用共享对象。
- 实证（在 pythonw 下 A/B 对比，枚举子进程顶层窗口并判可见性）：

  | 组别 | 观察到的可见窗口数 |
  |---|---|
  | 旧行为 | 1（即弹窗） |
  | 修复后 | **0** |

  说明：先前用 `python.exe` 测试从未复现——因为 python.exe 自带控制台，
  ffmpeg 继承它而不新建窗口。必须用 `pythonw` 才能复现。

## 2026-10-10（视频缩略图）

### 新增：视频显示真实首帧
- 此前视频格只显示灰色播放按钮占位图，看不到内容。现接入 ffmpeg 抽取首帧：
  - 依赖 `imageio-ffmpeg`（自带静态 ffmpeg 二进制，无需系统安装）
  - `cache_source.make_video_thumb()`：抽帧并缩放为缩略图
  - 缩略图同样落盘缓存，之后启动直接复用
- GUI 中图片与视频统一走后台生成；未完成时视频临时显示占位图，
  生成完成后自动替换为真实画面。

### 关键实现细节：抽帧必须落临时文件
最初走 `pipe:0` 管道输入，实测 12 个样本中 5 个失败，ffmpeg 报
`partial file` / `unspecified pixel format`。原因是 **MP4 的 moov（索引）
常位于文件尾部**，管道无法回退 seek。
改为写入临时文件后再抽帧（用完即删），并加"按偏移抽帧失败则回退首帧"：
- 修复前：12 个样本成功 7 个（58%）
- 修复后：25 个样本 **成功 25 个（100%）**，平均 0.06 秒/个

## 2026-10-10

### 新增：直读 Telegram 缓存的挑选器
- `cache_source.py`：读取 `tdata` 本地密钥（只读），解密 TDEF 加密缓存，
  嗅探媒体类型并生成缩略图缓存与索引。
  - 索引阶段只解密文件前 64 KiB 用于识别类型，视频不再整体解密
  - 缩略图与索引落盘，重复启动直接复用（实测 15.4s → 0.6s）
- `cache_picker.py`：缓存版 GUI。打开即看缓存里的图，点选后保存；
  **保存前不写出任何文件**，原 tdata 一个字节都不改动。
- `run_cache.cmd`：双击启动（无控制台窗口）。

### 修复：缩略图不显示 / 滚动撕裂（两个 bug）
1. 隐藏格子时先丢弃了图片引用，Tk 随即销毁图像，而 Canvas 图元仍指向它，
   导致后续 `itemconfigure` 持续报 `image "pyimage..." doesn't exist`，
   表现为**缩略图全部消失**。改为先解除图元引用再丢引用。
2. `_canvas_cfg` / `_canvas_coords` 调用时漏传图元键名，抛 `TypeError`。
   已统一签名，并加入失效图元自动重建，滚动不再中断。

### 优化：渲染从"子控件网格"改为"Canvas 虚拟化"
- 早期实现为每张图创建一个 Tk 子控件（几百个），Tk 无双缓冲，
  快速滚轮会撕裂出像素块；且滚轮处理每滚一下新建一批控件，非常卡。
- 现在只渲染屏幕内可见的格子并循环复用图元：
  - 单次刷新 **6.45ms**（早期 8.28ms，且报错 3039 行 → 0 行）
  - 图元池稳定在 49 个，不随内容增长
  - 滚动事件 16ms 节流、hover 仅重绘受影响的两格
  - 缩略图后台线程加载，内存中最多保留 600 张

## 更早（同一会话内）

### 两种挑选器
- `tg_picker.py`：Telethon 版，按群/频道在线浏览缩略图并挑选下载
  （需要 `api_id` / `api_hash`）。

> 曾短暂存在 `gallery_picker.py`（浏览本地已导出文件夹的挑选器），
> 因与产品定位（绕过 Telegram 保存限制）无关、且"先导出再挑"属于多余步骤，
> 已于 2026-10-12 移除。

### 缓存解密可行性验证
- 复用 tg-scoop 的解密实现，从本机一份真实 `tdata` 成功解出 3019 个媒体
  （图片 2464 / 视频 557），解密失败 0 个；并验证读取前后 tdata 文件
  修改时间未变，确认**只读**。
