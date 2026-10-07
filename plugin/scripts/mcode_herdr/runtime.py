# plugin/scripts/mcode_herdr/runtime.py
"""把「事件 → decide → transport → 落盘」串起来。

钩子入口 herdr-report.py 会派一个脱离进程组的后台进程调用 main()，
所以这里只管把一次上报做对，任何异常都不得逃出去。

每次真正改动 pane 状态时都把「归属进程」（哪个 mcode）一并落盘，store 在读回时
拿它判活：mcode 被强杀时 Stop 不会到达，不判活就会把停在 working 的状态永远当成
当前事实，pane 也就永远卡死。判活只多一次 /proc 读，不引入任何阻塞调用。
"""
from __future__ import annotations

from pathlib import Path
from typing import IO, Mapping, Optional

from . import herdr, transport
from .decide import Action, Decision, decide
from .env import OWNER_PID, OWNER_START, discover_herdr_env
from .payload import load_payload
from .store import PaneState, Store

_VALID_STATES = frozenset({herdr.STATE_IDLE, herdr.STATE_WORKING, herdr.STATE_BLOCKED})


def _resume_argv(session_id: Optional[str]) -> list:
    # 首词必须是 PATH 上的裸命令名，herdr 会拒绝绝对路径。
    return ["mcode", "--session", session_id] if session_id else []


def _resolve_herdr_env(env: Mapping[str, str], proc_root: Optional[Path]) -> Optional[Mapping[str, str]]:
    # 显式分支是生产主路径：herdr-report.py 在派生 worker 之前就把回溯结果注入子进程
    # 环境，因为 worker 一旦被 reparent 到 init 就再也走不回 /proc 祖先进程链。
    # 它同时覆盖「人在 herdr pane 里手动跑 worker.py」和回放测试注入假环境两种情况。
    # 回退到 /proc 只在既没有显式 HERDR_* 又确实还在 mcode 进程树里时才会命中。
    # 删掉第一条分支会让整个插件静默失效（herdr agent list 永远为空）。
    #
    # 两条路径都靠 HERDR_ 前缀把归属（OWNER_PID/OWNER_START）一并带出来，所以手工注入
    # 假环境时也必须带上这两个键：缺了就等于「归属未知」，store 会把这份状态当陈旧的
    # 丢掉 —— 也就是说手动跑 worker.py 只能看到当次这一次上报，跨钩子的状态不保留。
    if env.get("HERDR_ENV") == "1" and env.get("HERDR_PANE_ID"):
        return {k: v for k, v in env.items() if k.startswith("HERDR_")}
    return discover_herdr_env(proc_root=proc_root or Path("/proc"))


def run_once(action: str, raw_payload: str, env: Mapping[str, str],
             proc_root: Optional[Path] = None) -> int:
    herdr_env = _resolve_herdr_env(env, proc_root)
    if herdr_env is None:
        return 0  # 不在 herdr 里：彻底静默
    try:
        payload = load_payload(raw_payload)
    except ValueError:
        return 0

    pane_id = herdr_env["HERDR_PANE_ID"]
    # 归属 = 拥有这个 pane 状态的那台 mcode。生产里这两个键是 herdr-report.py 在派生
    # 之前回溯出来的（它必须在那时读：worker 一旦被 init 收养就再也走不回父链），
    # 由 child_env 带进来。store 靠它判断状态是不是陈旧：写状态的 mcode 已经没了，
    # 停在 working 的 last_reported 就再也不该当证据。
    owner_pid = herdr_env.get(OWNER_PID) or None
    owner_start = herdr_env.get(OWNER_START) or None
    # 拿不到 PLUGIN_DATA 就什么都不做，不回退到 /tmp：状态文件名由 pane id 派生、
    # 可预测，而 store.py 落临时文件用的是 os.open(O_CREAT)，它会跟随预置的符号链接
    # （0600 只管文件权限，管不了路径解析），在全局可写的 /tmp 里等于把状态 JSON
    # 写进攻击者指定的文件。生产环境 mcode 必定注入 PLUGIN_*，回退分支本来就走不到。
    plugin_data = env.get("PLUGIN_DATA")
    if not plugin_data:
        return 0
    store = Store(Path(plugin_data))

    def step(state: PaneState) -> None:
        decision = decide(Action(action), payload, state)
        if decision is None:
            # 空转的钩子连归属都不许改：子代理的钩子被忽略是常态，改了等于把归属
            # 洗给「当前这个 pid」，真正持有 working 的老 mcode 一死，这份状态
            # 反而因为新归属还活着而永远解不开。
            return
        state.owner_pid = owner_pid
        state.owner_start = owner_start
        if decision.kind is Decision.Kind.RESET:
            # 换会话：只清会话身份。不 release —— mcode 进程还活着，pane 上的 agent 登记
            # 必须留着（这正是本次要修的 bug）；也不上报 —— 接手的新会话会在片刻后用它的
            # SessionStart 报 idle。此处照样盖归属：这也是一次落盘，漏了归属下一次读回
            # 就当成陈旧状态丢掉。
            state.root_session = None
            state.blocked_by = None
            state.last_reported = None
            return
        if decision.kind is Decision.Kind.RELEASE:
            transport.release(herdr_env, transport.next_seq())
            state.root_session = None
            state.blocked_by = None
            state.last_reported = None
            return
        if decision.state not in _VALID_STATES:
            return  # 状态机若将来吐出非法状态，宁可不报也不要让 herdr 拒收
        resume = _resume_argv(payload.session_id) if decision.resume else None
        sent = transport.report(herdr_env, decision.state, transport.next_seq(),
                                message=decision.message,
                                session_id=payload.session_id if decision.attach_session else None,
                                resume_argv=resume)
        state.root_session = decision.new_root_session or state.root_session
        state.blocked_by = decision.blocked_by
        state.last_reported = decision.state if sent else state.last_reported

    store.update(pane_id, step)
    return 0


def main(argv: list, stdin: IO, env: Mapping[str, str],
         proc_root: Optional[Path] = None) -> int:
    try:
        return run_once(argv[0] if argv else "", stdin.read(), env, proc_root=proc_root)
    except Exception:  # noqa: BLE001 - 任何异常都不得影响 mcode
        return 0
