"""End-to-end tests for the history part, with fake machines as in tests/test_cli.py."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class HistoryPart(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.m = {"home": f"{t}/mac", "desktop": f"{t}/mac/Library/Application Support/Claude"}
        self.u = {"home": f"{t}/ubu", "desktop": f"{t}/ubu/.config/Claude"}
        common = {"platform": "Test", "app_running": False, "cli_pids": [], "claude_version": "x"}
        self.env = {**os.environ, "CLAUDE_SYNC_TEST_ROOTS": json.dumps({
            "here": {**self.m, **common, "hostname": "mac"},
            "ubu": {**self.u, **common, "hostname": "ubu"}})}
        for r in (self.m, self.u):
            os.makedirs(f"{r['home']}/.claude", exist_ok=True)
            os.makedirs(r["desktop"], exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def sync(self, *args):
        p = subprocess.run([sys.executable, f"{REPO}/claude-sync", *args, "ubu", "--json"],
                           env=self.env, capture_output=True, text=True, timeout=120)
        self.assertNotIn("Traceback", p.stderr, p.stderr)
        return p.returncode, json.loads(p.stdout or "{}")

    def put_history(self, r, lines):
        p = f"{r['home']}/.claude/history.jsonl"
        with open(p, "w") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")

    def get_history(self, r):
        p = f"{r['home']}/.claude/history.jsonl"
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_union_and_project_mapping(self):
        mcwd, ucwd = self.m["home"] + "/dev/x", self.u["home"] + "/dev/x"
        self.put_history(self.m, [{"display": "a", "timestamp": 1, "sessionId": "s1", "project": mcwd}])
        self.put_history(self.u, [{"display": "b", "timestamp": 2, "sessionId": "s2", "project": ucwd}])
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        m_lines, u_lines = self.get_history(self.m), self.get_history(self.u)
        self.assertEqual(len(m_lines), 2)
        self.assertEqual(len(u_lines), 2)
        self.assertEqual([e["display"] for e in m_lines], ["a", "b"], "timestamp order")
        for lines, home in ((m_lines, self.m["home"]), (u_lines, self.u["home"])):
            for entry in lines:
                self.assertTrue(entry["project"].startswith(home), entry)

    def test_no_duplicates_on_second_sync(self):
        self.put_history(self.m, [{"display": "a", "timestamp": 1, "sessionId": "s1", "project": self.m["home"]}])
        self.assertEqual(self.sync()[0], 0)
        code, out = self.sync("status")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.get_history(self.u)), 1)

    def test_missing_file_on_one_side_is_empty(self):
        # only mac has a history file: ubuntu should just receive it, not error.
        self.put_history(self.m, [{"display": "a", "timestamp": 1, "sessionId": "s1", "project": self.m["home"]}])
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.get_history(self.u)), 1)


if __name__ == "__main__":
    unittest.main()
