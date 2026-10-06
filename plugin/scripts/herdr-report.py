#!/usr/bin/env python3
"""mcode 钩子入口。

钩子跑在工具调用的关键路径上，必须立即返回：读掉 stdin 后派生一个
脱离进程组的后台子进程去做真正的上报，父进程直接退出，且不向
stdout/stderr 写任何字节 —— PreToolUse 的 stdout 会被运行时消费。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    raw = sys.stdin.read()
    try:
        proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "worker.py"), event],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:  # noqa: BLE001
        return 0
    try:
        # 故意不等 worker：钩子阻塞一秒就是 mcode 工具调用路径上多一秒。
        # 子进程已 start_new_session=True（脱离进程组）且父进程先退出，
        # 它会被 init 收养，不会留下僵尸。
        proc.stdin.write(raw.encode())
        proc.stdin.close()
        # Popen 对象此刻仍带着一个未回收的子进程：GC 触发 __del__ 时会发
        # ResourceWarning，而它写的是 stderr —— 在 PYTHONWARNINGS=always 或
        # python -X dev 下会污染钩子输出。填上 returncode 等于告诉 Popen
        # 「已经收过了」，析构便不再告警，且完全不产生等待。
        proc.returncode = 0
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
