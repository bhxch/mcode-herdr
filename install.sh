#!/usr/bin/env bash
# 同步插件到本地市场并验证。本地插件被发现即 installed+enabled，无需 plugin add。
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/plugin"
DATA_DIR="${MINIMAX_DATA_DIR:-$HOME/.minimax}"
DEST="$DATA_DIR/plugins/mcode-herdr"

echo "==> 同步 $SRC -> $DEST"
# rm -rf 不可撤销，而 DEST 由 $MINIMAX_DATA_DIR / $HOME 推导而来，两者都可能为空或
# 被写成奇怪的路径。删之前必须确认 DEST 是绝对路径且末段恰好是 mcode-herdr，
# 否则一次误算就会把别的目录连根删掉且无法恢复。
if [ -z "$DEST" ] || [ "${DEST#/}" = "$DEST" ]; then
  echo "拒绝执行：DEST 不是绝对路径：$DEST" >&2
  exit 1
fi
if [ "${DEST##*/}" != "mcode-herdr" ]; then
  echo "拒绝执行：DEST 末段不是 mcode-herdr：$DEST" >&2
  exit 1
fi
# mkdir -p 在 rm -rf 之前是必需的：cp -r "$SRC/." "$DEST/" 要求目标目录已存在。
mkdir -p "$DEST"
rm -rf "$DEST"
cp -r "$SRC/." "$DEST/"
find "$DEST" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
chmod +x "$DEST/scripts"/*.py

echo "==> 验证插件已被本地市场识别"
mcode plugin list -m local --available | grep -E '^.\*\].*mcode-herdr@local' \
  || { echo "未识别！请检查 plugin.json 是否有 icon 字段（必填，缺失会被静默跳过）" >&2; exit 1; }

echo "==> 检查恢复命令的 mcode 是否在登录 shell 的 PATH 上"
if ! env -i /bin/sh -lc 'command -v mcode' >/dev/null 2>&1; then
  cat >&2 <<'EOF'
警告：登录 shell 的 PATH 上找不到 mcode。
herdr 恢复会话时会执行 `--` 后的裸命令名 `mcode`，找不到会导致恢复失败。
建议：ln -s "$(command -v mcode)" ~/.local/bin/mcode
      并确保 ~/.local/bin 在登录 shell 的 PATH 中。
EOF
fi

echo "完成。重启 mcode（或开新会话）后生效。"
