import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcode_herdr.env import MAX_ANCESTRY, discover_herdr_env, read_environ, read_ppid

HERDR_ENV_VARS = [
    "HERDR_ENV=1",
    "HERDR_PANE_ID=w1:p1",
    "HERDR_BIN_PATH=/opt/herdr",
    "HERDR_SOCKET_PATH=/tmp/s",
]


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

    def _make_chain(self, base, length, top_env):
        """造 base -> base+1 -> ... 的父链，只有链顶带 top_env，返回链顶 pid。"""
        for i in range(length - 1):
            self._make_proc(base + i, base + i + 1, ["PATH=/bin"])
        self._make_proc(base + length - 1, 1, top_env)
        return base + length - 1

    def test_walks_up_until_herdr_env_found(self):
        # 100 -> 101 -> 102，HERDR 只在 102，起点自身不带，必须真回溯两步
        self._make_proc(100, 101, ["PATH=/bin"])
        self._make_proc(101, 102, ["PATH=/bin"])
        self._make_proc(102, 1, HERDR_ENV_VARS)
        got = discover_herdr_env(proc_root=self.proc, start_pid=100)
        self.assertIsNotNone(got)
        self.assertEqual(got["HERDR_PANE_ID"], "w1:p1")
        self.assertEqual(got["HERDR_SOCKET_PATH"], "/tmp/s")

    def test_defaults_to_own_pid(self):
        # 不传 start_pid 时，回溯起点取 os.getpid()
        self._make_proc(100, 1, HERDR_ENV_VARS)
        with mock.patch("os.getpid", return_value=100):
            got = discover_herdr_env(proc_root=self.proc)
        self.assertIsNotNone(got)
        self.assertEqual(got["HERDR_PANE_ID"], "w1:p1")

    def test_returns_only_herdr_prefixed_vars(self):
        # 前缀过滤是安全边界：祖先环境可能带密钥，不能整体外泄给下游
        self._make_proc(100, 1, ["PATH=/bin", "AWS_SECRET_ACCESS_KEY=s3cr3t", *HERDR_ENV_VARS])
        got = discover_herdr_env(proc_root=self.proc, start_pid=100)
        self.assertIsNotNone(got)
        self.assertEqual(sorted(got), ["HERDR_BIN_PATH", "HERDR_ENV", "HERDR_PANE_ID", "HERDR_SOCKET_PATH"])

    def test_ignores_herdr_env_not_equal_one(self):
        # 2/3 长得像但 HERDR_ENV 不为 1，4 才是真祖先；断言命中 4 而不是 None，
        # 才同时钉住「拒绝坏的」和「继续往上走」，用 pid 1 当诱饵会被守卫短路掉
        self._make_proc(2, 3, ["PATH=/bin", "HERDR_ENV=0",
                               "HERDR_PANE_ID=w1:bad", "HERDR_BIN_PATH=/opt/herdr"])
        self._make_proc(3, 4, ["PATH=/bin", "HERDR_ENV=",
                               "HERDR_PANE_ID=w1:bad", "HERDR_BIN_PATH=/opt/herdr"])
        self._make_proc(4, 1, ["PATH=/bin", "HERDR_ENV=1",
                               "HERDR_PANE_ID=w1:good", "HERDR_BIN_PATH=/opt/herdr"])
        got = discover_herdr_env(proc_root=self.proc, start_pid=2)
        self.assertIsNotNone(got)
        self.assertEqual(got["HERDR_PANE_ID"], "w1:good")

    def test_requires_non_empty_herdr_pane_id(self):
        # REQUIRED 里的每个变量都必须非空：PANE_ID 缺了认不出是哪个 pane，
        # BIN_PATH 缺了下游用硬下标取值会 KeyError，报告被静默吞掉
        cases = [
            ["HERDR_ENV=1", "HERDR_PANE_ID=", "HERDR_BIN_PATH=/opt/herdr"],
            ["HERDR_ENV=1", "HERDR_BIN_PATH=/opt/herdr"],
            ["HERDR_ENV=1", "HERDR_PANE_ID=w1:p1", "HERDR_BIN_PATH="],
            ["HERDR_ENV=1", "HERDR_PANE_ID=w1:p1"],
        ]
        for i, env in enumerate(cases):
            with self.subTest(env=env):
                self._make_proc(100 + i, 1, ["PATH=/bin", *env])
                self.assertIsNone(discover_herdr_env(proc_root=self.proc, start_pid=100 + i))

    def test_finds_env_at_max_ancestry_depth(self):
        # 链长正好 MAX_ANCESTRY，HERDR 在链顶，最后一次迭代正好命中
        self._make_chain(200, MAX_ANCESTRY, HERDR_ENV_VARS)
        got = discover_herdr_env(proc_root=self.proc, start_pid=200)
        self.assertIsNotNone(got)
        self.assertEqual(got["HERDR_PANE_ID"], "w1:p1")

    def test_gives_up_beyond_max_ancestry(self):
        # 链长 MAX_ANCESTRY+1，链顶有 HERDR 但预算耗尽，必须放弃而不是无限往上
        self._make_chain(300, MAX_ANCESTRY + 1, HERDR_ENV_VARS)
        self.assertIsNone(discover_herdr_env(proc_root=self.proc, start_pid=300))

    def test_does_not_inspect_pid_1(self):
        # pid 1 是 init，就算它带着 HERDR 也不能认成 mcode 祖先（守卫在读 environ 之前）
        self._make_proc(2, 1, ["PATH=/bin"])
        self._make_proc(1, 0, ["PATH=/bin", *HERDR_ENV_VARS])
        self.assertIsNone(discover_herdr_env(proc_root=self.proc, start_pid=2))

    def test_returns_none_when_no_herdr_ancestor(self):
        self._make_proc(100, 101, ["PATH=/bin"])
        self._make_proc(101, 1, ["PATH=/bin"])
        self.assertIsNone(discover_herdr_env(proc_root=self.proc, start_pid=100))

    def test_reads_ppid_from_status_not_stat(self):
        # comm 可能含空格/括号，stat 按位置解析必错位；给个冲突值确保只读 status
        d = self._make_proc(7, 5, ["PATH=/bin"])
        (d / "stat").write_text("7 (m c code) S 1 999 1 0")
        self.assertEqual(read_ppid(7, self.proc), 5)

    def test_read_ppid_missing_or_malformed(self):
        d = self._make_proc(7, 5, ["PATH=/bin"])
        (d / "status").write_text("Name:\tpid\nState:\tS (sleeping)\n")
        self.assertIsNone(read_ppid(7, self.proc))
        self.assertIsNone(read_ppid(4242, self.proc))

    def test_read_environ_skips_malformed_entries(self):
        d = self._make_proc(7, 5, ["PATH=/bin"])
        d.joinpath("environ").write_bytes(b"NOEQUALS\0PATH=/bin\0HERDR_PANE_ID=w1:p=1\0BAD\xffKEY=v\0\0")
        self.assertEqual(read_environ(7, self.proc), {
            "PATH": "/bin",
            "HERDR_PANE_ID": "w1:p=1",
            "BAD\ufffdKEY": "v",
        })

    def test_read_environ_missing_proc_returns_empty(self):
        self.assertEqual(read_environ(4242, self.proc), {})


if __name__ == "__main__":
    unittest.main()
