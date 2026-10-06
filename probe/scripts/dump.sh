#!/bin/sh
# 一次性探针 v2：除了原始载荷，还要验证 HERDR_* 能否经 /proc 父进程链找回，
# 因为插件钩子只继承白名单环境（safeHookEnvironment），HERDR_* 不会传入。

set -u

event="${1:-unknown}"
out_dir="${MCODE_HERDR_PROBE_DIR:-/tmp/mcode-herdr-probe}"
out_file="$out_dir/events.log"

mkdir -p "$out_dir" 2>/dev/null || exit 0

payload_file="$out_dir/.stdin.$$"
trap 'rm -f "$payload_file"' EXIT HUP INT TERM
head -c 8000 2>/dev/null >"$payload_file"

# 沿 /proc 父链向上找带 HERDR_* 的祖先进程
ancestry() {
  pid=$$
  depth=0
  while [ "$pid" -gt 1 ] && [ "$depth" -lt 12 ]; do
    comm=$(tr -d '\0' <"/proc/$pid/comm" 2>/dev/null || echo '?')
    ppid=$(awk '/^PPid:/ {print $2}' "/proc/$pid/status" 2>/dev/null)
    hit=$(tr '\0' '\n' <"/proc/$pid/environ" 2>/dev/null | grep '^HERDR_' | tr '\n' ' ')
    printf '  depth=%s pid=%s comm=%s HERDR=[%s]\n' "$depth" "$pid" "$comm" "$hit"
    [ -z "$ppid" ] && break
    pid=$ppid
    depth=$((depth + 1))
  done
}

{
  printf '=== EVENT %s pid=%s ts=%s\n' "$event" "$$" "$(date +%s%3N 2>/dev/null || date +%s)"
  printf -- '--- ANCESTRY\n'
  ancestry
  printf -- '--- TTY\n'
  printf 'tty=%s\n' "$(tty 2>/dev/null || echo none)"
  printf -- '--- STDIN\n'
  cat "$payload_file" 2>/dev/null
  printf '\n--- END\n'
} >>"$out_file" 2>/dev/null

exit 0