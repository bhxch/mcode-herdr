import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mcode_herdr import transport

ENV = {
    "HERDR_ENV": "1",
    "HERDR_PANE_ID": "w1:p1",
    "HERDR_BIN_PATH": "/opt/herdr",
    "HERDR_SOCKET_PATH": "/tmp/fake.sock",
}


OK_REPLY = json.dumps({"id": "r1", "result": {"type": "ok"}}).encode() + b"\n"


class FakeSocketServer:
    """最小 herdr server：接受一行 JSON，回一行 reply（默认 ok 应答）。"""

    def __init__(self, path, reply=OK_REPLY):
        self.path = Path(path)
        self.reply = reply
        self.requests = []
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.path))
        self._sock.listen(4)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                data = conn.makefile("rwb")
                line = data.readline()
                if not line:
                    continue
                self.requests.append(json.loads(line))
                data.write(self.reply)
                data.flush()

    def close(self):
        self._stop = True
        self._sock.close()
        try:
            self.path.unlink()
        except OSError:
            pass


class DripSocketServer(FakeSocketServer):
    """滴字节的对端：定时吐一点字节，且始终不发换行。"""

    def __init__(self, path, step=8, interval=0.1, steps=15):
        super().__init__(path, reply=b"")
        self._step = step
        self._interval = interval
        self._steps = steps

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                data = conn.makefile("rwb")
                if not data.readline():
                    continue
                for _ in range(self._steps):
                    if self._stop:
                        return
                    try:
                        data.write(b"x" * self._step)
                        data.flush()
                    except OSError:
                        return
                    time.sleep(self._interval)


class FloodSocketServer(FakeSocketServer):
    """狂灌数据且不发换行的对端。"""

    def __init__(self, path, chunk=1 << 20, rounds=8):
        super().__init__(path, reply=b"")
        self._chunk = b"x" * chunk
        self._rounds = rounds

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                data = conn.makefile("rwb")
                if not data.readline():
                    continue
                conn.settimeout(0.05)  # 让阻塞中的写能定期醒来检查 _stop
                for _ in range(self._rounds):
                    if self._stop:
                        return
                    try:
                        data.write(self._chunk)
                        data.flush()
                    except OSError:
                        return


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sock_path = Path(self.tmp.name) / "fake.sock"
        self.calls = []
        self._orig_cli = transport._run_cli
        transport._run_cli = self._fake_cli
        self.binlog = Path(self.tmp.name) / "herdr"

    def _fake_cli(self, argv, timeout):
        self.calls.append(argv)
        return 0

    def _fake_cli_code(self, code):
        """让回落 CLI 返回固定码：这样 report() 的聚合结果能反推 socket 通道的判定。"""
        def run(argv, timeout):
            self.calls.append(argv)
            return code
        transport._run_cli = run

    def tearDown(self):
        transport._run_cli = self._orig_cli
        self.tmp.cleanup()

    def _env(self, **over):
        env = dict(ENV)
        env.update(over)
        return env

    def test_socket_channel_used_when_available(self):
        server = FakeSocketServer(self.sock_path)
        try:
            ok = transport.report(self._env(HERDR_SOCKET_PATH=str(self.sock_path)),
                                   state="working", seq=7, message="hi")
        finally:
            server.close()
        self.assertTrue(ok)
        self.assertEqual(self.calls, [])  # 没走 CLI
        req = server.requests[0]
        self.assertEqual(req["method"], "pane.report_agent")
        params = req["params"]
        self.assertEqual(params["pane_id"], "w1:p1")
        self.assertEqual(params["state"], "working")
        self.assertEqual(params["seq"], 7)
        self.assertEqual(params["message"], "hi")

    def test_falls_back_to_cli_when_socket_missing(self):
        ok = transport.report(self._env(), state="idle", seq=8)
        self.assertTrue(ok)
        self.assertEqual(len(self.calls), 1)
        argv = self.calls[0]
        self.assertEqual(argv[:4], ["/opt/herdr", "pane", "report-agent", "w1:p1"])
        self.assertIn("--seq", argv)
        self.assertIn("8", argv)

    def test_release_falls_back_to_cli_with_seq(self):
        ok = transport.release(self._env(), seq=9)
        self.assertTrue(ok)
        self.assertEqual(self.calls[0][:4], ["/opt/herdr", "pane", "release-agent", "w1:p1"])
        # 必须带 seq：不带会被 hook_report_is_newer 静默丢弃
        self.assertIn("--seq", self.calls[0])

    def test_resume_argv_is_appended_after_double_dash(self):
        transport.report(self._env(), state="idle", seq=10,
                         resume_argv=["mcode", "--session", "abc"])
        argv = self.calls[0]
        self.assertIn("--", argv)
        self.assertEqual(argv[argv.index("--") + 1:], ["mcode", "--session", "abc"])

    def test_invalid_resume_argv_still_reports_state(self):
        # session_id 来自 hook JSON，带单引号时校验必然失败；herdr 也会拒收这个恢复命令，
        # 所以要丢掉恢复命令但把状态发出去，而不是抛异常把状态上报一起丢掉
        session_id = "abc'def"
        ok = transport.report(self._env(), state="idle", seq=16,
                              session_id=session_id,
                              resume_argv=["mcode", "--session", session_id])
        self.assertTrue(ok)
        self.assertEqual(len(self.calls), 1)
        argv = self.calls[0]
        self.assertNotIn("--", argv)
        # --session 只可能来自 resume_argv（--agent 的值恰好也是 "mcode"，不能拿它当判据）
        self.assertNotIn("--session", argv)
        self.assertIn(session_id, argv)  # 状态本身确实发出去了

    def test_all_channels_failing_returns_false_without_raising(self):
        def boom(argv, timeout):
            raise OSError("nope")
        transport._run_cli = boom
        self.assertFalse(transport.report(self._env(), state="idle", seq=11))

    def test_resume_argv_rejects_path_like_first_word(self):
        with self.assertRaises(ValueError):
            transport.validate_resume_argv(["/usr/bin/mcode", "--session", "a"])

    def test_resume_argv_rejects_quote_and_too_many_args(self):
        with self.assertRaises(ValueError):
            transport.validate_resume_argv(["mcode", "it's"])
        with self.assertRaises(ValueError):
            transport.validate_resume_argv(["mcode"] + ["x"] * 64)

    def test_null_reply_is_not_treated_as_success(self):
        # null 不是 herdr 应答：既不能当成成功，也不能再让 TypeError 逃出 report()
        self._fake_cli_code(1)
        server = FakeSocketServer(self.sock_path, reply=b"null\n")
        try:
            ok = transport.report(self._env(HERDR_SOCKET_PATH=str(self.sock_path)),
                                  state="working", seq=13)
        finally:
            server.close()
        self.assertFalse(ok)
        self.assertEqual(len(self.calls), 1)  # 确实回落到了 CLI，没被假成功短路

    def test_non_object_reply_is_not_treated_as_success(self):
        # 假阳性方向：数组里没有 "error" 就算成功，调用方会记下 herdr 不知道的状态
        self._fake_cli_code(1)
        server = FakeSocketServer(self.sock_path, reply=b"[1,2]\n")
        try:
            ok = transport.report(self._env(HERDR_SOCKET_PATH=str(self.sock_path)),
                                  state="working", seq=14)
        finally:
            server.close()
        self.assertFalse(ok)
        self.assertEqual(len(self.calls), 1)

    def test_release_with_null_reply_returns_false(self):
        self._fake_cli_code(1)
        server = FakeSocketServer(self.sock_path, reply=b"null\n")
        try:
            ok = transport.release(self._env(HERDR_SOCKET_PATH=str(self.sock_path)), seq=15)
        finally:
            server.close()
        self.assertFalse(ok)

    def test_socket_construction_failure_falls_back_to_cli(self):
        # mcode 进程繁忙时可能 fd 耗尽，socket() 构造本身就抛 OSError；
        # 它必须留在守卫区内，否则异常会逃出 report() 且 close 无从执行
        real_socket = socket.socket

        def no_fd(*args, **kwargs):
            raise OSError(24, "Too many open files")

        transport.socket.socket = no_fd  # 共享 stdlib 模块，用完必须还原
        try:
            ok = transport.report(self._env(HERDR_SOCKET_PATH=str(self.sock_path)),
                                  state="idle", seq=17)
        finally:
            transport.socket.socket = real_socket
        self.assertTrue(ok)  # 状态改由 CLI 通道发出去
        self.assertEqual(len(self.calls), 1)

    # ---- 应答形状：假阳性方向 ----

    def test_bare_object_reply_is_not_treated_as_success(self):
        # {} / {"id":...} 只说明对端会吐 JSON，没说它收下了这次上报；
        # 误判成成功就会记下 last_reported，decide() 之后把同一事件永久去重，pane 无恢复地发散
        self._fake_cli_code(1)
        server = FakeSocketServer(self.sock_path, reply=b"{}\n")
        try:
            ok = transport.report(self._env(HERDR_SOCKET_PATH=str(self.sock_path)),
                                  state="working", seq=18)
        finally:
            server.close()
        self.assertFalse(ok)
        self.assertEqual(len(self.calls), 1)  # 确实回落到了 CLI，没被假成功短路

    def test_error_reply_falls_back_to_cli(self):
        # 假阳性收紧的对照面：herdr 明确拒绝（error 信封）时必须回落 CLI。
        # 只测“拒绝的应答不能算成功”这类收紧方向的测试，会让人把它一路改成“永远 True”而不被发现，
        # 所以这里用 CLI 返回 0 来证明 socket 通道真的放行了拒绝，才由 CLI 完成这次上报
        self._fake_cli_code(0)
        reply = json.dumps({"id": "r1", "error": {"message": "no such pane"}}).encode() + b"\n"
        server = FakeSocketServer(self.sock_path, reply=reply)
        try:
            ok = transport.report(self._env(HERDR_SOCKET_PATH=str(self.sock_path)),
                                  state="working", seq=19)
        finally:
            server.close()
        self.assertTrue(ok)
        self.assertEqual(len(self.calls), 1)

    def test_two_replies_on_one_connection_use_the_first(self):
        # 一条连接上跟了两条应答时，取第一行即可：json.loads 整个 buffer 会报 "Extra data"
        server = FakeSocketServer(self.sock_path, reply=OK_REPLY + OK_REPLY)
        try:
            ok = transport.report(self._env(HERDR_SOCKET_PATH=str(self.sock_path)),
                                  state="working", seq=20)
        finally:
            server.close()
        self.assertTrue(ok)
        self.assertEqual(self.calls, [])

    # ---- 整段读取的总时限与缓冲上限 ----

    def test_slow_drip_without_newline_is_bounded_by_the_deadline(self):
        # SOCKET_TIMEOUT 只约束单次 socket 操作：每 0.1s 吐 8 字节就不会触发任何一次超时，
        # 但整段读取被拖到 1.5s。hook 只有 5s，herdr 不能反过来拖住 mcode
        self._fake_cli_code(1)
        server = DripSocketServer(self.sock_path)
        try:
            start = time.monotonic()
            ok = transport.report(self._env(HERDR_SOCKET_PATH=str(self.sock_path)),
                                  state="working", seq=21)
            elapsed = time.monotonic() - start
        finally:
            server.close()
        self.assertFalse(ok)
        self.assertLess(elapsed, 1.0, f"整段读取没有被总时限截断：{elapsed:.2f}s")

    def test_flood_without_newline_does_not_buffer_without_bound(self):
        # 对端狂灌数据且不发换行：缓冲上限必须让它立刻放弃，而不是在时限内吃下若干兆
        self._fake_cli_code(1)
        server = FloodSocketServer(self.sock_path)
        try:
            start = time.monotonic()
            ok = transport.report(self._env(HERDR_SOCKET_PATH=str(self.sock_path)),
                                  state="working", seq=22)
            elapsed = time.monotonic() - start
        finally:
            server.close()
        self.assertFalse(ok)
        self.assertLess(elapsed, 0.35, f"缓冲没有上限，一路吃到了时限：{elapsed:.2f}s")

    # ---- seq 时钟 ----

    def test_next_seq_is_non_decreasing(self):
        seqs = [transport.next_seq() for _ in range(50)]
        self.assertEqual(seqs, sorted(seqs))
        self.assertLess(seqs[0], seqs[-1])

    def test_next_seq_does_not_follow_the_wall_clock(self):
        # 只测“非递减”杀不掉把 monotonic_ns 换回 time_ns 的变异体（单进程内两者都不倒退），
        # 所以这里把墙上时钟钉死：next_seq 必须仍然给出可用的 seq。
        # seq 一旦倒退，herdr 会静默丢弃这次上报却照样回 ok，调用方无从察觉
        real_time_ns = time.time_ns
        time.time_ns = lambda: 1  # 共享 stdlib 模块，用完必须还原
        try:
            seq = transport.next_seq()
        finally:
            time.time_ns = real_time_ns
        self.assertGreater(seq, 1)

    def test_next_seq_is_monotonic_across_processes(self):
        # 选单调时钟的另一半理由：Linux 上 CLOCK_MONOTONIC 是系统级的，两个进程共有一条时间轴；
        # 墙上时钟则可能在另一个进程两次上报之间被校时拨回去。子进程先打印，父进程的值必须更大
        pkg_root = Path(transport.__file__).resolve().parents[1]
        proc = subprocess.run(
            [sys.executable, "-c",
             "from mcode_herdr import transport; print(transport.next_seq())"],
            env=dict(os.environ, PYTHONPATH=str(pkg_root)),
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(int(proc.stdout.strip()), transport.next_seq())

    # ---- 两条通道的参数对称性 ----

    def test_release_over_socket_sends_method_and_seq(self):
        server = FakeSocketServer(self.sock_path)
        try:
            ok = transport.release(self._env(HERDR_SOCKET_PATH=str(self.sock_path)), seq=23)
        finally:
            server.close()
        self.assertTrue(ok)
        self.assertEqual(self.calls, [])  # 走的是 socket，不该回落
        req = server.requests[0]
        self.assertEqual(req["method"], "pane.release_agent")
        # 必须带 seq：漏了会被 herdr 静默丢弃，pane 上就永远挂着那个旧 agent
        self.assertEqual(req["params"]["seq"], 23)

    def test_report_over_socket_includes_resume_argv(self):
        server = FakeSocketServer(self.sock_path)
        try:
            ok = transport.report(self._env(HERDR_SOCKET_PATH=str(self.sock_path)),
                                  state="blocked", seq=24,
                                  resume_argv=["mcode", "--session", "abc"])
        finally:
            server.close()
        self.assertTrue(ok)
        self.assertEqual(self.calls, [])  # 走的是 socket，不该回落
        # CLI 通道只钉了 "--" 分隔符，socket 通道带没带 resume_argv 之前无人把守
        self.assertEqual(server.requests[0]["params"]["resume_argv"],
                         ["mcode", "--session", "abc"])

    def test_message_is_truncated_on_both_channels(self):
        # 400 是两条通道共同的上限，不能只截其中一边
        server = FakeSocketServer(self.sock_path)
        try:
            ok = transport.report(self._env(HERDR_SOCKET_PATH=str(self.sock_path)),
                                  state="blocked", seq=25, message="x" * 500)
        finally:
            server.close()
        self.assertTrue(ok)
        self.assertEqual(server.requests[0]["params"]["message"], "x" * 400)

        transport.report(self._env(), state="blocked", seq=26, message="y" * 500)
        argv = self.calls[0]
        self.assertEqual(argv[argv.index("--message") + 1], "y" * 400)


if __name__ == "__main__":
    unittest.main()
