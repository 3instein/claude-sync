"""Tests for the sidebar group merge."""
import unittest

from claude_sync import groups


def scope(gs, assign, order):
    return {"groups": [{"id": g, "name": g.upper()} for g in gs], "assignments": assign, "order": order}


class Merge(unittest.TestCase):
    def test_both_sides_add_sessions_and_groups(self):
        base = {"s": scope(["g1"], {"code:x": "g1"}, {"g1": ["code:x"]})}
        here = {"s": scope(["g1"], {"code:x": "g1", "code:y": "g1"}, {"g1": ["code:y", "code:x"]})}
        there = {"s": scope(["g1", "g2"], {"code:x": "g1", "code:z": "g2"}, {"g1": ["code:x"], "g2": ["code:z"]})}
        m = groups.merge(base, here, there, True)["s"]
        self.assertEqual(m["assignments"], {"code:x": "g1", "code:y": "g1", "code:z": "g2"})
        self.assertEqual(m["order"], {"g1": ["code:y", "code:x"], "g2": ["code:z"]})
        self.assertEqual([g["id"] for g in m["groups"]], ["g1", "g2"])

    def test_first_run_takes_the_union(self):
        here = {"s": scope(["g1"], {"code:a": "g1"}, {"g1": ["code:a"]})}
        there = {"s": scope(["g1"], {"code:b": "g1"}, {"g1": ["code:b"]})}
        m = groups.merge({}, here, there, False)["s"]
        self.assertEqual(m["order"], {"g1": ["code:a", "code:b"]})

    def test_deleted_group_ungroups_its_sessions(self):
        base = {"s": scope(["g1", "g2"], {"code:a": "g2"}, {"g2": ["code:a"]})}
        here = {"s": scope(["g1"], {"code:a": "g2"}, {})}
        there = base
        m = groups.merge(base, here, there, True)["s"]
        self.assertEqual((m["assignments"], [g["id"] for g in m["groups"]]), ({}, ["g1"]))

    def test_put_keeps_other_settings(self):
        cfg = {"coworkUserFilesPath": "/Users/r", "preferences": {"sidebarMode": "x", "epitaxyPrefs": {"k": 1}}}
        out = groups.put(cfg, {"s": {}})
        self.assertEqual(out["coworkUserFilesPath"], "/Users/r")
        self.assertEqual(out["preferences"]["epitaxyPrefs"], {"k": 1, "dframe-group-scopes": {"s": {}}})
        self.assertEqual(groups.subtree(out), {"s": {}})
        self.assertNotIn("dframe-group-scopes", cfg["preferences"]["epitaxyPrefs"], "the input is not changed")


if __name__ == "__main__":
    unittest.main()
