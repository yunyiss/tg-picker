# 内置解密模块（vendored）

本目录下的 `tg_scoop/` 是从 **[GarrettFynn/tg-scoop](https://github.com/GarrettFynn/tg-scoop)**
复制的解密内核，用于让 tg-picker **自包含**——不再依赖外部 `tg-scoop` 检出目录。

## 来源

| 项目 | 说明 |
|---|---|
| 上游 | https://github.com/GarrettFynn/tg-scoop |
| 许可 | MIT（见 `tg_scoop/LICENSE`，版权归 tg-scoop contributors） |
| 复制时间 | 2026-10-10 |
| 上游版本 | 0.1.0 |

## 复制了哪些文件（仅依赖闭包内的 4 个）

| 文件 | 作用 |
|---|---|
| `tg_scoop/__init__.py` | 包入口与异常导出 |
| `tg_scoop/exceptions.py` | 异常体系 |
| `tg_scoop/tdata_reader.py` | TDF$ 解析、LocalKey 派生（PBKDF2-HMAC-SHA512）、AES-256-IGE 解本地密钥块 |
| `tg_scoop/cache_decryptor.py` | TDEF 容器解析、AES-256-CTR 解密、SHA-256 校验 |

**未复制**上游的 CLI / GUI / 提取编排 / MTProto 等模块——它们与本工具的
用途重叠或无关。这 4 个文件只依赖标准库与 `pycryptodome`（在函数体内延迟导入）。

## 注意事项

- 这些文件是**上游代码的原样副本**，请勿在此处直接改动；如需修复，
  应优先向上游反馈或明确记录本地改动。
- 加载优先级见 `cache_source.find_decryptor_src()`：
  `TG_SCOOP_SRC` 环境变量 → 本目录 → 外部 tg-scoop 检出（兜底）。
