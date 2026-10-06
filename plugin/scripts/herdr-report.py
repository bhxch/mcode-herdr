#!/usr/bin/env python3
"""mcode 钩子入口。

钩子跑在工具调用的关键路径上，必须立即返回：读掉 stdin 后派生一个
脱离进程组的后台子进程去做真正的上报，父进程直接退出，且不向
stdout/stderr 写任何字节 —— PreToolUse 的 stdout 会被运行时消费。

herdr 环境必须在派生之前由本进程解析（mcode 只给钩子白名单环境，HERDR_*
要靠 discover_herdr_env 沿 /proc 父链回溯），再连同 child_env 一起交给 worker，
绝不能指望 worker 自己回溯：钩子一退出，worker 就被 init 收养
（start_new_session 只换会话组，不改父子关系），/proc 父链断成 worker -> init，
env.py 里 pid<=1 的守卫立刻返回 None，上报就静默消失了 —— 钩子照常触发，
状态文件却一个都不写。父链只在钩子进程自己身上是完整的，必须在派生前读出来。

解析不到（不在 herdr 里）时连 worker 都不派生：钩子只多几次 /proc 读就退出，
省掉子进程那次解释器启动，也不会有任何东西去抢 mcode 的进程表。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from mcode_herdr.env import discover_herdr_env  # noqa: E402


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    raw = sys.stdin.read()
    try:
        herdr_env = discover_herdr_env()
    except Exception:  # noqa: BLE001 - 任何异常都不得影响 mcode
        return 0
    if not herdr_env:
        return 0  # 不在 herdr 里：不派生 worker，彻底静默
    # worker 自己已经回溯不到 HERDR_*（父进程退出即被 init 收养），只能由这里交给它；
    # runtime._resolve_herdr_env 会优先采信 child_env 里显式存在的 HERDR_*
    child_env = dict(os.environ)
    child_env.update(herdr_env)
    try:
        proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "worker.py"), event],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, env=child_env,
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
