#!/usr/bin/env bash
# 移除插件。先释放它对 pane 的占用，否则 herdr 会继续把 pane 显示为有 agent。
set -euo pipefail

# 与 install.sh 逐字对齐：数据目录解析顺序必须等同 mcode 自身（MINIMAX_DATA_DIR →
# MAVIS_DATA_DIR → 默认值，见 packages/tui/src/runtime/data-dir.ts 的 readDataDirOverride）。
# 两者一旦漂移，卸载指向的目录就和安装不是同一个：轻则留下插件，重则删掉别人的东西。
DATA_DIR="${MINIMAX_DATA_DIR:-${MAVIS_DATA_DIR:-$HOME/.minimax}}"
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
  # 先写一条本地禁用记录，再删目录。禁用记录是 mcode 里 local 插件唯一的持久状态
  # （SQLite 的 local_runtime_plugin_local_disabled，按 canonical_root 键），写上它
  # 之后，即使下面的 rm -rf 因为权限等原因没删干净，留在原地的插件也是禁用态，
  # 不会继续在用户已经声明要卸载之后还上报状态。
  # 同样的校验作用域要求：MINIMAX_DATA_DIR 是 mcode 解析链的最高优先级（data-dir.ts
  # 的 readDataDirOverride），只给这一条命令设置它，脚本自身环境不变。
  # mcode 不可用、插件未被列出等情况一律放过，卸载不能因此失败。
  echo "==> 标记为禁用"
  MINIMAX_DATA_DIR="$DATA_DIR" mcode plugin disable -m local mcode-herdr >/dev/null 2>&1 || true
  # 注意：目录一旦删掉，mcode 会在下次扫描时自动清理这条孤立记录（源码里的
  # pruneMissingLocalPlugins）。也就是说它只用来兜住「没删干净」这一种情况，
  # 并不构成卸载后的长期标记——重装是一次全新的启用。
  echo "==> 删除 $DEST"
  rm -rf "$DEST"
fi
echo "完成。"
