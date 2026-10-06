import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mcode_herdr.env import (MAX_ANCESTRY, OWNER_PID, OWNER_START, discover_herdr_env,
                             read_environ, read_ppid, read_starttime)

HERDR_ENV_VARS = [
    "HERDR_ENV=1",
    "HERDR_PANE_ID=w1:p1",
    "HERDR_BIN_PATH=/opt/herdr",
    "HERDR_SOCKET_PATH=/tmp/s",
]

# starttime 是 stat 的第 22 个字段；第 3 个字段（state）起的每个字段在 ')' 之后是第 k 个 token。
STARTTIME = "424242"


def stat_line(pid, comm, ppid, starttime=STARTTIME):
    """造一行 /proc/<pid>/stat。

    comm 默认带空格和右括号：真实进程名就是这样（"weird )name"），
    解析必须从最后一个 ')' 之后开始，按空格或第一个 ')' 切都会错位。
    """
    tail = ["S", str(ppid)] + ["0"] * 17 + [str(starttime)]
    return "%d (%s) %s\n" % (pid, comm, " ".join(tail))


class EnvDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_proc(self, pid, ppid, env, starttime=None):
        d = self.proc / str(pid)
        d.mkdir(parents=True)
        (d / "environ").write_bytes(("\0".join(env) + "\0").encode())
        (d / "status").write_text(f"Name:\tpid\nPPid:\t{ppid}\n")
        if starttime is not None:
            (d / "stat").write_text(stat_line(pid, "weird )name", ppid, starttime))
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

    def test_reads_starttime_from_stat_after_the_last_paren(self):
        # 归属进程的启动指纹只存在于 stat：status 里没有这一项
        d = self._make_proc(7, 5, ["PATH=/bin"], starttime=STARTTIME)
        self.assertEqual(read_starttime(7, self.proc), STARTTIME)
        # 换成"comm 里有右括号"的进程名，值必须不变：按第一个 ')' 或按空格切都会偏掉 19 个字段
        d.joinpath("stat").write_text(stat_line(7, "a) b", 5, "999"))
        self.assertEqual(read_starttime(7, self.proc), "999")

    def test_read_starttime_missing_or_malformed(self):
        self.assertIsNone(read_starttime(4242, self.proc))   # 没有这个进程
        d = self._make_proc(7, 5, ["PATH=/bin"])
        self.assertIsNone(read_starttime(7, self.proc))       # stat 整个缺失
        d.joinpath("stat").write_text("7 (short) S 1\n")     # 字段不够，starttime 取不到
        self.assertIsNone(read_starttime(7, self.proc))
        d.joinpath("stat").write_text("7 (x) S 1 " + "0 " * 30 + "\n")  # 读到了但不是数字
        self.assertIn(read_starttime(7, self.proc), (None, "0"))           # 原样记号或 None，不抛

    def test_discovery_reports_the_matched_ancestor_as_owner(self):
        # 命中的那个祖先就是 mcode 自己；把它的 pid + 启动指纹一并带出去，
        # 状态文件才能判断「写这份状态的 mcode 是不是已经死了」
        self._make_proc(100, 101, ["PATH=/bin"], starttime="1")
        self._make_proc(101, 102, ["PATH=/bin"], starttime="2")
        self._make_proc(102, 1, HERDR_ENV_VARS, starttime=STARTTIME)
        got = discover_herdr_env(proc_root=self.proc, start_pid=100)
        self.assertIsNotNone(got)
        self.assertEqual(got[OWNER_PID], "102")          # 是命中者，不是回溯起点
        self.assertEqual(got[OWNER_START], STARTTIME)

    def test_owner_keys_stay_inside_the_herdr_prefix_filter(self):
        # 前缀过滤是 runtime/worker 搬运归属信息的唯一通道：
        # runtime._resolve_herdr_env 只转发 HERDR_*，键名一旦不满足前缀，归属就静默丢失
        self._make_proc(100, 1, ["PATH=/bin", *HERDR_ENV_VARS], starttime=STARTTIME)
        got = discover_herdr_env(proc_root=self.proc, start_pid=100)
        self.assertIsNotNone(got)
        self.assertTrue({OWNER_PID, OWNER_START} <= set(got))
        self.assertTrue(all(k.startswith("HERDR_") for k in (OWNER_PID, OWNER_START)))
        self.assertEqual({k: v for k, v in got.items() if k.startswith("HERDR_")}, dict(got))

    def test_discovery_omits_owner_when_starttime_unreadable(self):
        # 只有半个身份（pid 有、指纹没有）等于没有身份：落盘时必须整体缺省，
        # 否则「进程还在、指纹读不出来」会被误当成归属仍然有效
        self._make_proc(100, 1, HERDR_ENV_VARS)
        got = discover_herdr_env(proc_root=self.proc, start_pid=100)
        self.assertIsNotNone(got)
        self.assertNotIn(OWNER_PID, got)
        self.assertNotIn(OWNER_START, got)


if __name__ == "__main__":
    unittest.main()
