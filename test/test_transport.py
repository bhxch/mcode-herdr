import json
import socket
import tempfile
import threading
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
