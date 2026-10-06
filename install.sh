#!/usr/bin/env bash
# 同步插件到本地市场并验证。本地插件被发现即 installed+enabled，无需 plugin add。
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/plugin"
# 数据目录必须与 mcode 自身的解析顺序逐层对齐：MINIMAX_DATA_DIR → MAVIS_DATA_DIR
# → 默认值（见 packages/tui/src/runtime/data-dir.ts 的 readDataDirOverride）。
# 漏掉中间一层，插件会装进用户根本没在运行的 profile，而下面的验证步骤同样跑在
# 这个错误目录下，失败会被完全掩盖。本行必须与 uninstall.sh 逐字一致。
# $HOME 保持裸展开：set -u 下 HOME 未设置时脚本直接中止，而空串会让 DEST 退化成
# /plugins/mcode-herdr 并骗过下面的绝对路径检查。
DATA_DIR="${MINIMAX_DATA_DIR:-${MAVIS_DATA_DIR:-$HOME/.minimax}}"
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
# 校验必须针对上面实际写入的 $DEST。cron / wrapper / CI 中调用者的 MAVIS_DATA_DIR
# 可能未导出，此时 mcode 列的是默认 profile，校验要么空过、要么报一个与本次写入
# 无关的失败，都不描述装到哪儿了。这里只为这一条命令设置 MINIMAX_DATA_DIR（不动
# 脚本自身环境），因为它是 mcode 解析链的最高优先级（MINIMAX_DATA_DIR →
# MAVIS_DATA_DIR → 默认值，见 data-dir.ts 的 readDataDirOverride），非空即屏蔽
# 其余两层，mcode 必定解析到 $DATA_DIR。
MINIMAX_DATA_DIR="$DATA_DIR" mcode plugin list -m local --available | grep -E '^.\*\].*mcode-herdr@local' \
  || { echo "未识别！请检查 plugin.json 是否有 icon 字段（必填，缺失会被静默跳过）" >&2; exit 1; }

echo "==> 检查恢复命令的 mcode 是否可被 herdr 执行"
# 恢复命令是 `--` 后的裸命令名 `mcode`，由 herdr server 所在环境去解析，
# 不是由登录 shell 解析。之前这里用 `env -i /bin/sh -lc` 判断，而登录 shell
# 不读 ~/.bashrc（本机 mcode 的 PATH 正是 ~/.bashrc:36 加的），于是无论
# mcode 是否真的可用都会误报。改为分两级：先看当前环境，再看 herdr server
# 自己的 PATH（也就是真正执行恢复命令的那份环境）。
resume_ok=0
if command -v mcode >/dev/null 2>&1; then
  resume_ok=1
elif command -v pgrep >/dev/null 2>&1; then
  for pid in $(pgrep -x herdr 2>/dev/null); do
    if tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep '^PATH=' | tr ':' '\n' \
        | grep -q '/mcode$'; then
      resume_ok=1
      break
    fi
  done
fi
if [ "$resume_ok" -eq 0 ]; then
  cat >&2 <<'EOF'
警告：herdr 恢复会话时要执行的裸命令名 `mcode` 当前解析不到，恢复会失败。
herdr server 继承启动它的终端环境；若那里也没有 mcode，可执行：
      ln -s "$(command -v mcode)" ~/.local/bin/mcode
然后重启 herdr server 让它带上新的 PATH。
EOF
fi

echo "完成。重启 mcode（或开新会话）后生效。"
