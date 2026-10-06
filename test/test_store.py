import json
import tempfile
import unittest
from pathlib import Path

from mcode_herdr.store import PaneState, Store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_gives_default_state(self):
        state = self.store.load("w1:p1")
        self.assertIsNone(state.root_session)
        self.assertIsNone(state.blocked_by)
        self.assertIsNone(state.last_reported)

    def test_round_trip(self):
        self.store.save("w1:p1", PaneState(root_session="s1", blocked_by="s1", last_reported="blocked"))
        got = self.store.load("w1:p1")
        self.assertEqual(got.root_session, "s1")
        self.assertEqual(got.blocked_by, "s1")
        self.assertEqual(got.last_reported, "blocked")

    def test_panes_are_isolated(self):
        self.store.save("w1:p1", PaneState(root_session="a"))
        self.store.save("w1:p2", PaneState(root_session="b"))
        self.assertEqual(self.store.load("w1:p1").root_session, "a")
        self.assertEqual(self.store.load("w1:p2").root_session, "b")

    def test_corrupt_file_degrades_to_default(self):
        path = self.store.path_for("w1:p1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is not json")
        self.assertIsNone(self.store.load("w1:p1").root_session)

    def test_save_is_atomic_no_temp_left_behind(self):
        self.store.save("w1:p1", PaneState(root_session="a"))
        path = self.store.path_for("w1:p1")
        keep = {path.name, path.with_suffix(".lock").name}
        leftovers = [p.name for p in path.parent.iterdir() if p.name not in keep]
        self.assertEqual(leftovers, [])
