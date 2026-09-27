"""End-to-end tests for the dev root, same fake-machine harness as tests/test_cli.py:
the real cli, helper and merge on two fake machines in a temporary folder. Files live
under <home>/dev instead of <home>/.claude."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DevRoot(unittest.TestCase):
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

    def put(self, m, rel, data):
        p = f"{m['home']}/dev/{rel}"
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def get(self, m, rel):
        with open(f"{m['home']}/dev/{rel}", "rb") as f:
            return f.read()

    def exists(self, m, rel):
        return os.path.exists(f"{m['home']}/dev/{rel}")

    def make_repo(self, m, rel):
        os.makedirs(f"{m['home']}/dev/{rel}/.git", exist_ok=True)

    def test_doc_copied_with_the_other_machines_path_unchanged(self):
        self.put(self.m, "notes/todo.md", f"see {self.m['home']}/dev/notes for more".encode())
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        copied = self.get(self.u, "notes/todo.md")
        self.assertIn(self.m["home"].encode(), copied, "a raw dev file keeps its exact bytes")

    def test_file_inside_a_repo_is_not_synced(self):
        self.make_repo(self.m, "myrepo")
        self.put(self.m, "myrepo/file.txt", b"tracked by git, not by claude-sync")
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        self.assertFalse(self.exists(self.u, "myrepo/file.txt"))

    def test_node_modules_is_not_synced(self):
        self.put(self.m, "proj/node_modules/pkg/index.js", b"dependency")
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        self.assertFalse(self.exists(self.u, "proj/node_modules/pkg/index.js"))

    def test_settings_local_json_synced_mapped_and_exec_config(self):
        self.make_repo(self.m, "myrepo")
        data = json.dumps({"permissions": {"allow": [f"Read({self.m['home']}/dev/**)"]}}).encode()
        self.put(self.m, "myrepo/.claude/settings.local.json", data)
        code, out = self.sync()
        self.assertEqual((code, out["stopped"]), (3, ["exec_config"]), out)
        self.assertFalse(self.exists(self.u, "myrepo/.claude/settings.local.json"))
        code, out = self.sync("--confirm", out["token"])
        self.assertEqual(code, 0, out)
        copied = json.loads(self.get(self.u, "myrepo/.claude/settings.local.json"))
        self.assertEqual(copied["permissions"]["allow"], [f"Read({self.u['home']}/dev/**)"])

    def test_env_file_is_not_synced(self):
        self.put(self.m, "proj/.env", b"SECRET=abc123")
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        self.assertFalse(self.exists(self.u, "proj/.env"))


if __name__ == "__main__":
    unittest.main()
