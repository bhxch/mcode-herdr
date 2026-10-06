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
PLUGIN_ID="mcode-herdr@local"

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
# 符号链接必须在这一步之前拦下，而不是复制之后再报。mcode 扫描 plugins/* 的直接子项
# 时会跳过符号链接（package-readers.ts 的 rejectSymlink），这样的目录在列表里直接
# 消失、没有报错；而下面紧随的 rm -rf 会把符号链接本身删掉换成真实目录——也就是说
# 装完之后再检查就永远查不到了，用户那份链接（常见于 dotfiles 管理）被无声替换。
if [ -L "$DEST" ]; then
  cat >&2 <<EOF
拒绝执行：$DEST 是一个符号链接，指向 $(readlink "$DEST")。

mcode 扫描 $DATA_DIR/plugins/* 的直接子项时会跳过符号链接，这个插件会一直处于
"已安装但不被识别"的状态且没有任何报错。继续执行则会删掉你的链接、原地换成真实
目录，而链接目标里的原文件不受影响。

处理办法（二选一）：
  1) 想要真实目录：移除链接后重跑本脚本
         unlink "$DEST" && ./install.sh
  2) 想保留链接（dotfiles 管理等）：改用文件级同步，让链接指向仓库
         unlink "$DEST" && ln -s "$(dirname "$SRC")/plugin" "$DEST"
     注意 mcode 会跳过符号链接的插件目录，这种装法 mcode 同样识别不到，
     仅适合你另有办法让 mcode 读取该路径的场合。
EOF
  exit 1
fi
# mkdir -p 在 rm -rf 之前是必需的：cp -r "$SRC/." "$DEST/" 要求目标目录已存在。
mkdir -p "$DEST"
rm -rf "$DEST"
cp -r "$SRC/." "$DEST/"
find "$DEST" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# 自检必须排在 chmod 之前：层级错误时 $DEST/scripts/*.py 根本不展开，
# chmod 会先被 set -e 打断，只留下一句 "No such file or directory"，
# 把真正的原因（清单多套了一层）盖掉。
echo "==> 自检安装产物"
# mcode 对"清单缺失 / 层级多套一层 / 文件没复制全"三种情况的处理都是静默跳过，
# 症状要等到运行期才以"插件什么都不做"的形式出现，那时已经没人记得装过什么。
# 用户在跑安装脚本的这几秒是唯一能低成本发现它们的时机，所以在这里逐条给出结论。
MANIFEST=""
for candidate in plugin.json .minimax-plugin/plugin.json claude-plugin/plugin.json .codex-plugin/plugin.json; do
  if [ -f "$DEST/$candidate" ]; then
    MANIFEST="$DEST/$candidate"
    break
  fi
done
if [ -z "$MANIFEST" ]; then
  {
    echo "拒绝执行：$DEST 下没有插件清单。"
    echo "mcode 只在插件目录的直接下一层查找以下四个路径之一，少一层或多一层都会被静默跳过："
    echo "    plugin.json / .minimax-plugin/plugin.json / claude-plugin/plugin.json / .codex-plugin/plugin.json"
    echo "当前 $DEST 的顶层内容："
    (ls -A "$DEST" 2>/dev/null || true) | sed 's/^/    /'
    echo "若源目录里是存在的，说明多套了一层包装目录。修正后重跑："
    echo "    cp -r <正确的插件目录>/* $DEST/"
  } >&2
  exit 1
fi
# 清单引用到的文件必须真的在树里，否则运行期 hook 会静默失效。
missing=""
for required in hooks/hooks.json scripts/herdr-report.py icon.png; do
  [ -f "$DEST/$required" ] || missing="${missing:+$missing }$required"
done
if [ -n "$missing" ]; then
  {
    echo "拒绝执行：安装产物缺少下列文件：$missing"
    echo "清单：$MANIFEST"
    echo "hooks/hooks.json 由 plugin.json 的 hooks 字段引用，"
    echo "scripts/herdr-report.py 与 icon.png 由 hooks/hooks.json 和 icon 字段引用。"
    echo "缺失说明复制被截断或源目录不对，hook 在运行期会静默不执行："
    echo "    确认 $SRC 下文件完整后重跑本脚本"
  } >&2
  exit 1
fi
# 树内的符号链接不致命（mcode 只在扫描直接子项时拒绝链接），但通常意味着源目录
# 本身有问题，值得说出来而不是让用户日后困惑。
tree_links="$(find "$DEST" -type l 2>/dev/null || true)"
if [ -n "$tree_links" ]; then
  {
    echo "警告：安装产物内含符号链接："
    printf '%s\n' "$tree_links" | sed 's/^/    /'
    echo "若这些链接指向已失效的目标，相关 hook 会静默不执行。"
  } >&2
fi

# 自检通过才轮到 chmod：此时 $DEST/scripts 必然存在且含有 .py，glob 不会落空。
chmod +x "$DEST/scripts"/*.py

echo "==> 验证插件已被本地市场识别"
# 校验必须针对上面实际写入的 $DEST。cron / wrapper / CI 中调用者的 MAVIS_DATA_DIR
# 可能未导出，此时 mcode 列的是默认 profile，校验要么空过、要么报一个与本次写入
# 无关的失败，都不描述装到哪儿了。这里只为这一条命令设置 MINIMAX_DATA_DIR（不动
# 脚本自身环境），因为它是 mcode 解析链的最高优先级（MINIMAX_DATA_DIR →
# MAVIS_DATA_DIR → 默认值，见 data-dir.ts 的 readDataDirOverride），非空即屏蔽
# 其余两层，mcode 必定解析到 $DATA_DIR。
LIST_TEXT="$(MINIMAX_DATA_DIR="$DATA_DIR" mcode plugin list -m local --available 2>/dev/null || true)"
LIST_JSON="$(MINIMAX_DATA_DIR="$DATA_DIR" mcode plugin list -m local --available --json 2>/dev/null || true)"

# enabled 状态优先读 JSON 的 enabled 字段（稳定字段，不依赖人类表格的排版）。
# 任何异常——没有 python3、JSON 不可解析、字段缺失——都收敛成 unknown，绝不因为
# 解析失败把脚本带崩或把状态猜成"已启用"。
plugin_state() {
  printf '%s' "$LIST_JSON" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print("unknown"); raise SystemExit(0)
if not isinstance(data, dict):
    print("unknown"); raise SystemExit(0)
for row in data.get("installed") or []:
    if not isinstance(row, dict) or row.get("pluginId") != sys.argv[1]:
        continue
    flag = row.get("enabled")
    print("enabled" if flag is True else "disabled" if flag is False else "unknown")
    raise SystemExit(0)
print("absent")
' "$PLUGIN_ID" 2>/dev/null || echo unknown
}

# 表格是第二个信息源：JSON 拿不到结论时用 [*]/[-] 标记兜底，两者一致才算认定。
if printf '%s\n' "$LIST_TEXT" | grep -qE '^.\*\]' && printf '%s\n' "$LIST_TEXT" | grep -q "mcode-herdr@local"; then
  STATE=enabled
elif printf '%s\n' "$LIST_TEXT" | grep -qE '^.-\]' && printf '%s\n' "$LIST_TEXT" | grep -q "mcode-herdr@local"; then
  STATE=disabled
else
  STATE="$(plugin_state)"
fi

if [ "$STATE" = absent ] || [ "$STATE" = disabled ]; then
  # 结构自检已全部通过，问题就不在安装产物上，必须换一个方向解释，不能再一股脑
  # 归因到清单字段。缺失 icon 是最常见的真实原因，这里直接查证并给出结论。
  if ! grep -q '"icon"' "$MANIFEST"; then
    cat >&2 <<EOF
未识别：$PLUGIN_ID 没有出现在 mcode 的本地插件列表里。
已确认清单存在且结构正常：$MANIFEST
根因：清单缺少 icon 字段——mcode 会把没有 icon 的本地插件整条跳过，且不打印任何错误。
修复：给清单补上 icon（例如 "icon": "icon.png"）并确保 $DEST/icon.png 存在，然后重跑本脚本。
EOF
    exit 1
  fi
fi

if [ "$STATE" = disabled ]; then
  # 禁用状态来自持久化的 deny 名单：SQLite 表 local_runtime_plugin_local_disabled，
  # 以 canonical_root 为键。它记录的是路径、不是文件内容，所以上面的 rm -rf + 重拷
  # 一次也清不掉，重跑 install.sh 后插件依然不生效——而症状是"插件什么都不做"。
  #
  # 这里选择自愈而不是报错：你主动跑了 install.sh，这个动作本身就是"让它能用"的
  # 最强信号；而 mcode 侧对被禁用插件没有任何提示，留给用户去猜代价更高。自愈只
  # 影响这一条记录、可用 disable 原样撤销，所以下面把这件事和撤销方式都明确打出来，
  # 不做无声处理。
  echo "!! 检测到 $PLUGIN_ID 处于禁用状态，原因是本地禁用名单里有一条残留记录。"
  echo "!! 该记录按路径存在 SQLite 中，与插件文件内容无关，因此重装不会清除它。"
  echo "!! install.sh 的语义是让插件可用，正在自动执行：mcode plugin enable -m local mcode-herdr"
  if ! MINIMAX_DATA_DIR="$DATA_DIR" mcode plugin enable -m local "$PLUGIN_ID"; then
    {
      echo "自动启用失败，请手动执行："
      echo "    MINIMAX_DATA_DIR=$DATA_DIR mcode plugin enable -m local $PLUGIN_ID"
    } >&2
    exit 1
  fi
  # 启用后必须复查：enable 返回 0 不代表状态已落到列表里。
  LIST_TEXT="$(MINIMAX_DATA_DIR="$DATA_DIR" mcode plugin list -m local --available 2>/dev/null || true)"
  if ! printf '%s\n' "$LIST_TEXT" | grep -qE '^.\*\]' || ! printf '%s\n' "$LIST_TEXT" | grep -q "mcode-herdr@local"; then
    {
      echo "已执行 enable，但 $PLUGIN_ID 在列表中仍未标记为启用，实际输出如下："
      printf '%s\n' "$LIST_TEXT" | sed 's/^/    /'
      echo "请手动排查：MINIMAX_DATA_DIR=$DATA_DIR mcode plugin list -m local --available"
    } >&2
    exit 1
  fi
  echo "!! 已重新启用。如果你本意是保持禁用，请执行：mcode plugin disable -m local mcode-herdr"
  STATE=enabled
fi

if [ "$STATE" = unknown ]; then
  {
    echo "无法判定 $PLUGIN_ID 的启用状态：既没读到 JSON 的 enabled 字段，也没读到表格标记。"
    echo "mcode 原始输出如下，请据此排查："
    printf '%s\n' "$LIST_TEXT" | sed 's/^/    /'
    echo "复查命令：MINIMAX_DATA_DIR=$DATA_DIR mcode plugin list -m local --available --json"
  } >&2
  exit 1
fi

if [ "$STATE" != enabled ]; then
  {
    echo "未识别：$PLUGIN_ID 没有出现在 mcode 的本地插件列表里。"
    echo "安装产物已通过结构自检（清单 $MANIFEST 存在、hooks 与 scripts 齐全、非符号链接），"
    echo "因此问题不在本次写入的内容上，按以下顺序排查："
    echo "  1) 确认 mcode 运行时用的是同一个 profile：本次写入 $DEST，"
    echo "     但你平时启动 mcode 时若没有导出 MINIMAX_DATA_DIR / MAVIS_DATA_DIR，"
    echo "     它会退回默认 profile（通常 ~/.minimax），那里并没有这个插件。"
    echo "  2) 确认清单能被解析：icon 字段已存在，名称与 $PLUGIN_ID 是否一致。"
    echo "  3) 确认没有其他进程改过状态：MINIMAX_DATA_DIR=$DATA_DIR mcode plugin list -m local --available"
  } >&2
  exit 1
fi

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
