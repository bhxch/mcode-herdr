import json
import multiprocessing
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from mcode_herdr.env import read_starttime
from mcode_herdr.store import PaneState, Store

# 写进程每次写一份可辨认的大载荷：直写目的地时读者能看到截断或半截内容，
# tmp + rename 时读者只能看到某一份完整值。载荷要够大，直写才会留出可见的窗口。
PAYLOAD_SIZE = 512 * 1024
WRITERS = 4
ROUNDS_PER_WRITER = 30
# 丢更新的窗口靠 fn 里的 sleep 撑开。持锁正确时 4 个进程被完全串行化，
# 这一项约 (WRITERS * BUMP_ROUNDS * BUMP_SLEEP_S) ≈ 0.3s。
BUMP_SLEEP_S = 0.01
BUMP_ROUNDS = 8
# 两个并发测试的总时长上限，卡住时判失败而不是挂住 CI。
DEADLINE_S = 30.0


# 这两个用例关心的是并发与原子性本身，所以状态必须属于一个活着的进程才读得回来：
# 归属失活的状态在 load 时会被丢成空状态，跟这些用例无关。
OWNER_PID = str(os.getpid())
OWNER_START = read_starttime(os.getpid(), Path("/proc"))


def _writer(base, pane_id, tag, rounds):
    """后台子进程：反复 save 同一 pane，每次一个可辨认的大载荷。"""
    store = Store(Path(base))
    filler = "x" * PAYLOAD_SIZE
    for i in range(rounds):
        store.save(pane_id, PaneState(root_session="%s-%d" % (tag, i), last_reported=filler,
                                      owner_pid=OWNER_PID, owner_start=OWNER_START))


def _bumper(base, pane_id, rounds, sink):
    """后台子进程：在 update() 里对同一计数器自增，把每次读到的值交回父进程。"""
    store = Store(Path(base))
    seen = []
    for _ in range(rounds):
        def bump(state):
            time.sleep(BUMP_SLEEP_S)  # 撑开读改写窗口，让丢更新必现而非偶发
            nxt = int(state.blocked_by or 0) + 1
            state.blocked_by = str(nxt)
            # runtime 每次真正改动状态都会盖上归属；少了这一步，下一轮读到的
            # 就是被丢成空的状态，计数永远停在 1
            state.owner_pid, state.owner_start = OWNER_PID, OWNER_START
            return nxt
        seen.append(store.update(pane_id, bump))
    sink.put(seen)


def _spawn(ctx, target, args):
    # 用 fork：模块已硬依赖 fcntl，测试同样只在 POSIX 上跑，省掉子进程重新导入
    # test.test_store 的麻烦（test 目录还没有 __init__.py）。
    proc = ctx.Process(target=target, args=args)
    proc.start()
    return proc


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def _pane(self, **fields):
        """带上一个活着的归属：归属失活的状态读回来是空的，与这些用例无关。"""
        return PaneState(owner_pid=OWNER_PID, owner_start=OWNER_START, **fields)

    def _temp_leftovers(self, base):
        return sorted(p.name for p in base.iterdir() if ".tmp." in p.name)

    def _join_within(self, procs):
        """按总时限收子进程；超时就判失败，避免挂住 CI。"""
        deadline = time.monotonic() + DEADLINE_S
        for proc in procs:
            proc.join(max(0.0, deadline - time.monotonic()))
            if proc.is_alive():
                proc.terminate()
                proc.join()
                self.fail("并发子进程超时未退出")

    def test_missing_file_gives_default_state(self):
        state = self.store.load("w1:p1")
        self.assertIsNone(state.root_session)
        self.assertIsNone(state.blocked_by)
        self.assertIsNone(state.last_reported)
        self.assertIsNone(state.owner_pid)
        self.assertIsNone(state.owner_start)

    def test_round_trip(self):
        self.store.save("w1:p1", self._pane(root_session="s1", blocked_by="s1", last_reported="blocked"))
        got = self.store.load("w1:p1")
        self.assertEqual(got.root_session, "s1")
        self.assertEqual(got.blocked_by, "s1")
        self.assertEqual(got.last_reported, "blocked")

    def test_panes_are_isolated(self):
        self.store.save("w1:p1", self._pane(root_session="a"))
        self.store.save("w1:p2", self._pane(root_session="b"))
        self.assertEqual(self.store.load("w1:p1").root_session, "a")
        self.assertEqual(self.store.load("w1:p2").root_session, "b")

    def test_corrupt_file_degrades_to_default(self):
        path = self.store.path_for("w1:p1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is not json")
        self.assertIsNone(self.store.load("w1:p1").root_session)

    def test_non_dict_json_degrades_to_default(self):
        # 合法 JSON 但不是对象：parse 不报错，若直接 data.get 会 AttributeError 崩掉钩子
        path = self.store.path_for("w1:p1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[]")
        got = self.store.load("w1:p1")
        self.assertIsNone(got.root_session)
        self.assertIsNone(got.blocked_by)
        self.assertIsNone(got.last_reported)

    def test_save_leaves_no_temp_file(self):
        self.store.save("w1:p1", self._pane(root_session="a"))
        path = self.store.path_for("w1:p1")
        keep = {path.name, path.with_suffix(".lock").name}
        leftovers = [p.name for p in path.parent.iterdir() if p.name not in keep]
        self.assertEqual(leftovers, [])

    def test_save_cleans_up_temp_file_when_replace_fails(self):
        # 写完临时文件、replace 之前失败时，pid 后缀的临时文件会永久留在盘上；
        # PLUGIN_DATA 可能是共享 /tmp，攒起来的垃圾还会把状态目录撑大
        with mock.patch("os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                self.store.save("w1:p1", self._pane(root_session="a"))
        self.assertEqual(self._temp_leftovers(self.store.base), [])

    def test_state_file_is_owner_only(self):
        # 状态里是要拼进 `mcode --session <id>` 恢复命令的会话标识，跟锁文件一样
        # 只给属主读写；write_text 会按 umask 落成 0644
        self.store.save("w1:p1", self._pane(root_session="s1"))
        mode = stat.S_IMODE(self.store.path_for("w1:p1").stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_pane_id_with_path_traversal_stays_inside_base(self):
        # pane_id 来自环境变量，非可信输入；不截断成单段目录名就能借它写到 base 之外
        path = self.store.path_for("../../etc/passwd")
        self.assertEqual(path.parent, self.store.base)
        self.assertEqual(path.name, ".._.._etc_passwd.json")
        self.assertTrue(path.resolve().is_relative_to(self.store.base.resolve()))

    def test_save_creates_base_dir_on_demand(self):
        # PLUGIN_DATA 指向的目录不一定已经被谁创建过
        store = Store(Path(self.tmp.name) / "nested" / "state")
        self.assertFalse(store.base.exists())
        store.save("w1:p1", self._pane(root_session="a"))
        self.assertEqual(store.load("w1:p1").root_session, "a")

    def test_update_persists_transformed_state_and_returns_callback_value(self):
        # 决策在锁内基于最新状态算出，返回值是调用方要拿去上报的东西
        def decide(state):
            state.root_session = state.root_session or "s-root"
            state.blocked_by = "s-blocker"
            # 决策方（runtime）会盖上归属，测试里照做：没有归属的状态读回来是空的
            state.owner_pid, state.owner_start = OWNER_PID, OWNER_START
            return "working"

        self.assertEqual(self.store.update("w1:p1", decide), "working")
        got = self.store.load("w1:p1")
        self.assertEqual(got.root_session, "s-root")
        self.assertEqual(got.blocked_by, "s-blocker")

    def test_update_leaves_state_untouched_when_callback_raises(self):
        # 上报参数非法时决策函数抛错，此时不能把改了一半的状态落盘
        self.store.save("w1:p1", self._pane(root_session="s1"))

        def boom(state):
            state.blocked_by = "half-written"
            raise ValueError("bad resume argv")

        with self.assertRaises(ValueError):
            self.store.update("w1:p1", boom)
        got = self.store.load("w1:p1")
        self.assertEqual(got.root_session, "s1")
        self.assertIsNone(got.blocked_by)

    def test_update_serializes_concurrent_read_modify_write(self):
        # 真正丢更新的形状：4 个进程各 8 次自增。update() 必须在一次持锁内完成
        # load→fn→save，否则互相覆盖，计数既不连续也不到 32。断言的是「各进程
        # 读到的值合起来正好是 1..32、无重复无缺口」，比只查终值更紧。
        ctx = multiprocessing.get_context("fork")
        base, pane = str(self.store.base), "w1:p1"
        sink = ctx.SimpleQueue()
        procs = [_spawn(ctx, _bumper, (base, pane, BUMP_ROUNDS, sink)) for _ in range(WRITERS)]
        try:
            self._join_within(procs)
            seen = []
            while not sink.empty():
                seen.extend(sink.get())
        finally:
            for proc in procs:
                if proc.is_alive():
                    proc.terminate()
                proc.join()
        self.assertEqual(sorted(seen), list(range(1, WRITERS * BUMP_ROUNDS + 1)))
        self.assertEqual(self.store.load(pane).blocked_by, str(WRITERS * BUMP_ROUNDS))

    def test_concurrent_writers_never_observed_torn(self):
        # 读者刻意绕开 flock 直接读文件：「读者永远看到完整 JSON」是 tmp + rename
        # 的承诺，herdr CLI 和人肉排查都这么读。若改走 store.load()，锁本身就把并发
        # 读挡住了，把 save 换成直写目的地也照样全绿，测不出原子性。
        base, pane = str(self.store.base), "w1:p1"
        self.store.save(pane, self._pane(root_session="seed"))
        path = self.store.path_for(pane)
        ctx = multiprocessing.get_context("fork")
        complete = {
            json.dumps({"root_session": "w%d-%d" % (tag, i), "blocked_by": None, "last_reported": "x" * PAYLOAD_SIZE,
                        "owner_pid": OWNER_PID, "owner_start": OWNER_START}, sort_keys=True): None
            for tag in range(WRITERS) for i in range(ROUNDS_PER_WRITER)
        }
        # 起手先落一份合法状态，免得读到「文件还不存在」这种非竞态的失败
        complete[json.dumps({"root_session": "seed", "blocked_by": None, "last_reported": None,
                             "owner_pid": OWNER_PID, "owner_start": OWNER_START},
                            sort_keys=True)] = None
        procs = [_spawn(ctx, _writer, (base, pane, "w%d" % tag, ROUNDS_PER_WRITER)) for tag in range(WRITERS)]
        reads = 0
        deadline = time.monotonic() + DEADLINE_S
        try:
            while any(proc.is_alive() for proc in procs):
                self.assertLess(time.monotonic(), deadline, "并发读超时")
                raw = path.read_text()
                # 半截文档既 parse 不过，也对不上任何一份完整值
                self.assertIn(json.dumps(json.loads(raw), sort_keys=True), complete)
                reads += 1
            for proc in procs:
                self.assertEqual(proc.exitcode, 0)
        finally:
            for proc in procs:
                if proc.is_alive():
                    proc.terminate()
                proc.join()
        self.assertGreater(reads, 0, "没读到任何一次，测试没意义")


class OwnerLivenessTest(unittest.TestCase):
    """状态由「哪个 mcode 写的」决定还能不能用：写它的进程没了，这份状态就不作数。

    mcode 被 kill -9 时 Stop 永远不会到达，last_reported 停在 working；新会话的
    SessionStart 又会被「working ⇒ 必然是子代理」吞掉，pane 就此永远卡住。
    这里用一份假 /proc 精确控制「归属进程还在不在」。
    """

    LIVE_PID = 4242
    LIVE_START = "111111"
    STUCK = dict(root_session="sess-dead", last_reported="working")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.proc = root / "proc"
        self.proc.mkdir()
        self.store = Store(root / "state", proc_root=self.proc)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_proc(self, pid, starttime):
        d = self.proc / str(pid)
        d.mkdir(parents=True)
        (d / "stat").write_text("%d (mcode) S 1 " % pid + "0 " * 17 + starttime + " 0\n")

    def _seed(self, **owner):
        """绕过 load 直接落盘：load 会按归属判活，种子状态必须能被写出来。"""
        self.store.save("w1:p1", PaneState(**self.STUCK, **owner))

    def _raw(self, data):
        path = self.store.path_for("w1:p1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def test_state_from_a_dead_owner_is_treated_as_empty(self):
        self._seed(owner_pid=str(self.LIVE_PID), owner_start=self.LIVE_START)  # 归属进程没写进 /proc
        got = self.store.load("w1:p1")
        self.assertIsNone(got.root_session)
        self.assertIsNone(got.last_reported)
        self.assertIsNone(got.owner_pid)

    def test_state_from_a_live_owner_is_honoured(self):
        # 活着的归属必须照常生效：否则每次 load 都在重新认领，子代理抑制规则失效
        self._write_proc(self.LIVE_PID, self.LIVE_START)
        self._seed(owner_pid=str(self.LIVE_PID), owner_start=self.LIVE_START)
        got = self.store.load("w1:p1")
        self.assertEqual(got.root_session, "sess-dead")
        self.assertEqual(got.last_reported, "working")
        self.assertEqual(got.owner_pid, str(self.LIVE_PID))

    def test_recycled_pid_with_a_different_starttime_is_treated_as_dead(self):
        # pid 还在、却是另一个进程（内核把它回收给了别人）：只看 pid 存在与否，
        # 死掉的 mcode 会被永远当成活着，卡住的 pane 就再也解不开
        self._write_proc(self.LIVE_PID, "999999")
        self._seed(owner_pid=str(self.LIVE_PID), owner_start=self.LIVE_START)
        self.assertIsNone(self.store.load("w1:p1").root_session)

    def test_partial_owner_is_treated_as_empty(self):
        # 半个身份（缺 pid 或缺指纹）证明不了归属仍然有效
        self._write_proc(self.LIVE_PID, self.LIVE_START)
        for owner in ({"owner_pid": str(self.LIVE_PID)},
                      {"owner_start": self.LIVE_START},
                      {"owner_pid": "", "owner_start": self.LIVE_START}):
            with self.subTest(owner=owner):
                self._seed(**owner)
                self.assertIsNone(self.store.load("w1:p1").root_session)

    def test_legacy_state_without_owner_is_treated_as_empty(self):
        # 已发布版本落盘的状态没有归属字段。升级后就地改判成过期：证明不了活着
        # 就不能继续当证据 —— 恰恰是这类残留状态最需要被丢掉，否则它们永远等不到
        # 一次写（所有钩子都被子代理启发式吞掉），也就永远不会被新版本接管。
        self._raw({"root_session": "sess-dead", "blocked_by": None, "last_reported": "working"})
        got = self.store.load("w1:p1")
        self.assertIsNone(got.root_session)
        self.assertIsNone(got.last_reported)

    def test_corrupt_owner_does_not_raise(self):
        # 归属字段被手改成对象/数字/布尔都只该落到「过期」，绝不能把钩子崩掉
        for owner in ({"owner_pid": {"a": 1}, "owner_start": [1]},
                      {"owner_pid": 4242, "owner_start": 111111},
                      {"owner_pid": True, "owner_start": "1"}):
            with self.subTest(owner=owner):
                self._raw({**self.STUCK, **owner})
                self.assertIsNone(self.store.load("w1:p1").root_session)

    def test_update_decides_on_empty_state_when_the_owner_is_dead(self):
        # update() 内部也走 _read：决策拿到的必须是空状态，pane 才有救
        self._seed(owner_pid="4242", owner_start="1")
        seen = []
        self.store.update("w1:p1", lambda st: seen.append((st.root_session, st.last_reported)))
        self.assertEqual(seen, [(None, None)])

    def test_owner_survives_the_round_trip_verbatim(self):
        # 归属靠原样比对落盘的值：pid 和 starttime 都不做解析，写什么读回来就是什么
        self._write_proc(self.LIVE_PID, self.LIVE_START)
        self._seed(owner_pid=str(self.LIVE_PID), owner_start=self.LIVE_START)
        on_disk = json.loads(self.store.path_for("w1:p1").read_text())
        self.assertEqual(on_disk["owner_pid"], str(self.LIVE_PID))
        self.assertEqual(on_disk["owner_start"], self.LIVE_START)

    def test_liveness_is_checked_against_a_real_process_when_not_injected(self):
        # 默认的 proc_root 是真 /proc：本测试进程就是归属，比对必须真的读它
        real = Store(Path(self.tmp.name) / "real")
        real.save("w1:p1", PaneState(owner_pid=str(os.getpid()),
                                      owner_start=read_starttime(os.getpid(), Path("/proc"))))
        self.assertEqual(real.load("w1:p1").owner_pid, str(os.getpid()))
        real.save("w1:p1", PaneState(owner_pid=str(os.getpid()), owner_start="0"))
        self.assertIsNone(real.load("w1:p1").owner_pid)


if __name__ == "__main__":
    unittest.main()
