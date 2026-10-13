#!/usr/bin/env bash
# 构建「只解码」的最小 ffmpeg（裁掉用不到的一切），供发布包内置。
#
#   bash packaging/build_ffmpeg.sh <输出目录>
#
# 环境变量：
#   FFMPEG_VER  源码版本（默认 7.1）
#   MAKE        make 命令（默认 make；缺失时回退 mingw32-make）
#
# 配方（关键资产）：只保留 视频解码 + 常见容器解复用 + mjpeg 输出 + scale 滤镜，
# 去掉全部编码器/网络/文档。产物约 5MB（官方完整版约 84MB），License LGPL 2.1+。
# 这是"编译期一次性"的裁剪——产物随包分发，用户侧零编译、零安装、无运行时开销。
set -euo pipefail

OUT="${1:?用法: build_ffmpeg.sh <输出目录>}"
VER="${FFMPEG_VER:-7.1}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) OS=win;  EXE=ffmpeg.exe ;;
  Darwin)               OS=mac;  EXE=ffmpeg ;;
  *)                    OS=linux; EXE=ffmpeg ;;
esac

echo ">> [$OS] 下载 ffmpeg ${VER} 源码"
curl -L --fail -o "$WORK/ffmpeg.tar.gz" \
  "https://ffmpeg.org/releases/ffmpeg-${VER}.tar.gz"
# 用 gzip（各平台的 tar 都自带），不依赖 xz
tar -xzf "$WORK/ffmpeg.tar.gz" -C "$WORK"
SRC="$(find "$WORK" -maxdepth 1 -type d -name 'ffmpeg-*' | head -1)"
[ -n "$SRC" ] || { echo "解压后找不到源码目录"; exit 1; }

COMMON=(
  --disable-everything --disable-doc --disable-network --disable-autodetect
  --disable-ffplay --disable-ffprobe --enable-ffmpeg
  --enable-decoder=h264,hevc,vp8,vp9,mpeg4
  --enable-parser=h264,hevc,vp9,mpeg4video
  --enable-demuxer=mov,matroska,avi
  --enable-muxer=image2,image2pipe
  --enable-encoder=mjpeg
  --enable-protocol=file,pipe
  --enable-filter=scale
  --enable-small
)

EXTRA=()
case "$OS" in
  win) EXTRA=(--arch=x86_64 --target-os=mingw32) ;;
  mac) EXTRA=(--target-os=darwin) ;;
  *)   EXTRA=(--target-os=linux) ;;
esac

MAKE_BIN="${MAKE:-make}"
command -v "$MAKE_BIN" >/dev/null 2>&1 || MAKE_BIN=mingw32-make
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"

echo ">> [$OS] configure"
( cd "$SRC" && ./configure "${COMMON[@]}" "${EXTRA[@]}" )

echo ">> [$OS] make -j${JOBS}"
( cd "$SRC" && "$MAKE_BIN" -j"$JOBS" )

cp "$SRC/$EXE" "$OUT/$EXE"
echo ">> 完成: $OUT/$EXE ($(du -h "$OUT/$EXE" | cut -f1))"
