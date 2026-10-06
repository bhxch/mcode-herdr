"""herdr 上报协议常量。与本机 0.9.3 CLI/源码对齐。"""

SOURCE = "mcode-herdr"       # 稳定且唯一；换名会导致旧 authority 变成别人的
AGENT = "mcode"              # 自定义 agent 用自己的名字，不要冒用 herdr 内置 kind

STATE_IDLE = "idle"
STATE_WORKING = "working"
STATE_BLOCKED = "blocked"

SOCKET_TIMEOUT = 0.5
CLI_TIMEOUT = 1.0

RESUME_MAX_ARGS = 64
RESUME_MAX_BYTES = 8192
