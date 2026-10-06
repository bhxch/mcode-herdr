#!/usr/bin/env bash
# 移除插件。先释放它对 pane 的占用，否则 herdr 会继续把 pane 显示为有 agent。
set -euo pipefail

DATA_DIR="${MINIMAX_DATA_DIR:-$HOME/.minimax}"
DEST="$DATA_DIR/plugins/mcode-herdr"

if [ -n "${HERDR_BIN_PATH:-}" ] && [ -n "${HERDR_PANE_ID:-}" ]; then
  # seq 必须严格递增。插件用 monotonic_ns（开机以来的纳秒），所以这里也必须
  # 用开机以来的时间；用 date +%s（纪元纳秒）会把高水位抬到 10^18，
  # 重装后 monotonic 的 seq 会被 herdr 判为过期而静默丢弃。
  seq="$(awk '{printf "%d", $1 * 1000000000}' /proc/uptime 2>/dev/null || true)"
  if [ -z "$seq" ]; then
    seq="$(date +%s)000000000"
  fi
  "$HERDR_BIN_PATH" pane release-agent "$HERDR_PANE_ID" \
    --source mcode-herdr --agent mcode --seq "$seq" || true
fi

# rm -rf 不可撤销，而 DEST 由 $MINIMAX_DATA_DIR / $HOME 推导而来，两者都可能为空或
# 被写成奇怪的路径。删之前必须确认 DEST 是绝对路径且末段恰好是 mcode-herdr。
if [ -d "$DEST" ]; then
  if [ -z "$DEST" ] || [ "${DEST#/}" = "$DEST" ]; then
    echo "拒绝执行：DEST 不是绝对路径：$DEST" >&2
    exit 1
  fi
  if [ "${DEST##*/}" != "mcode-herdr" ]; then
    echo "拒绝执行：DEST 末段不是 mcode-herdr：$DEST" >&2
    exit 1
  fi
  echo "==> 删除 $DEST"
  rm -rf "$DEST"
fi
echo "完成。"
