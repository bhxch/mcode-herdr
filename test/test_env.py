import tempfile
import unittest
from pathlib import Path

from mcode_herdr.env import discover_herdr_env, read_environ, read_ppid


class EnvDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_proc(self, pid, ppid, env):
        d = self.proc / str(pid)
        d.mkdir(parents=True)
        (d / "environ").write_bytes(("\0".join(env) + "\0").encode())
        (d / "status").write_text(f"Name:\tpid\nPPid:\t{ppid}\n")
        return d

    def test_walks_up_until_herdr_env_found(self):
        # self(1) -> mid(2) -> mcode(3) with HERDR, self ppid chain reversed
        self._make_proc(1, 0, ["PATH=/bin"])
        self._make_proc(2, 1, ["PATH=/bin"])
        self._make_proc(3, 2, ["PATH=/bin", "HERDR_ENV=1", "HERDR_PANE_ID=w1:p1",
                               "HERDR_BIN_PATH=/opt/herdr", "HERDR_SOCKET_PATH=/tmp/s"])
        # 模拟自身 pid 为 1，因此直接命中自身（带 HERDR）
        got = discover_herdr_env(proc_root=self.proc, start_pid=3)
        self.assertEqual(got["HERDR_PANE_ID"], "w1:p1")
        self.assertEqual(got["HERDR_SOCKET_PATH"], "/tmp/s")

    def test_returns_none_when_no_herdr_ancestor(self):
        self._make_proc(1, 0, ["PATH=/bin"])
        self._make_proc(2, 1, ["PATH=/bin"])
        self.assertIsNone(discover_herdr_env(proc_root=self.proc, start_pid=2))

    def test_reads_ppid_from_status_not_stat(self):
        self._make_proc(7, 5, ["PATH=/bin"])
        self.assertEqual(read_ppid(7, self.proc), 5)

    def test_ignores_herdr_env_not_equal_one(self):
        self._make_proc(1, 0, ["PATH=/bin", "HERDR_ENV=", "HERDR_PANE_ID=w1:p1"])
        self.assertIsNone(discover_herdr_env(proc_root=self.proc, start_pid=1))


if __name__ == "__main__":
    unittest.main()