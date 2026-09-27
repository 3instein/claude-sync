"""End-to-end: the real cli, helper and merge on two fake machines in a temporary folder.
Both sides run locally through the CLAUDE_SYNC_TEST_ROOTS hook; nothing touches real data."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from claude_sync import paths

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD = int(time.time()) - 3600  # older than the live window


class EndToEnd(unittest.TestCase):
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

    def put(self, r, rel, data, mtime=OLD):
        p = f"{r['home']}/.claude/{rel}"
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        os.utime(p, (mtime, mtime))
        return p

    def get(self, r, rel):
        with open(f"{r['home']}/.claude/{rel}", "rb") as f:
            return f.read()

    def project(self, r):
        return "projects/" + paths.folder_name(r["home"] + "/dev/x")

    def seed(self):
        cwd = self.m["home"] + "/dev/x"
        line = json.dumps({"type": "user", "cwd": cwd, "sessionId": "s1", "uuid": "1"}, separators=(",", ":"))
        self.put(self.m, f"{self.project(self.m)}/s1.jsonl", line.encode() + b"\n")
        self.put(self.m, f"{self.project(self.m)}/memory/note.md", b"Mac home is " + cwd.encode() + b"\n")
        self.put(self.m, "plans/p.md", b"plan")

    def test_first_sync_then_in_sync(self):
        self.seed()
        code, out = self.sync("status")
        self.assertEqual((code, out["files"]["to_host"]), (2, 3), out)
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        moved = json.loads(self.get(self.u, f"{self.project(self.u)}/s1.jsonl"))
        self.assertEqual(moved["cwd"], self.u["home"] + "/dev/x")
        memory = self.get(self.u, f"{self.project(self.u)}/memory/note.md")
        self.assertIn(self.m["home"].encode(), memory, "memory text is not rewritten")
        self.assertEqual(self.sync("status")[0], 0)
        for r in (self.m, self.u):
            self.assertTrue(os.path.exists(f"{r['home']}/.cache/claude-sync/state.json"))

    def test_edit_delete_conflict_and_split(self):
        self.seed()
        self.assertEqual(self.sync()[0], 0)
        self.put(self.u, "plans/p.md", b"edited on ubuntu", mtime=OLD + 10)
        os.remove(f"{self.m['home']}/.claude/{self.project(self.m)}/memory/note.md")
        self.put(self.m, "CLAUDE.md", b"mac", mtime=OLD + 10)
        self.put(self.u, "CLAUDE.md", b"ubuntu", mtime=OLD + 20)
        t = f"{self.project(self.m)}/s1.jsonl"
        tu = f"{self.project(self.u)}/s1.jsonl"
        self.put(self.m, t, self.get(self.m, t) + b'{"uuid":"2","sessionId":"s1"}\n', mtime=OLD + 10)
        self.put(self.u, tu, self.get(self.u, tu) + b'{"uuid":"3","sessionId":"s1"}\n', mtime=OLD + 10)
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.get(self.m, "plans/p.md"), b"edited on ubuntu")
        self.assertFalse(os.path.exists(f"{self.u['home']}/.claude/{self.project(self.u)}/memory/note.md"))
        self.assertEqual(self.get(self.u, "CLAUDE.md"), b"mac", "this machine's copy wins a raw conflict")
        self.assertEqual(out.get("conflicts"), ["cli/CLAUDE.md"])
        self.assertEqual(len(out.get("split_sessions", [])), 1)
        new = os.path.basename(out["split_sessions"][0]["new"])
        for r in (self.m, self.u):
            data = self.get(r, f"{self.project(r)}/{new}")
            self.assertIn(b'"uuid":"3"', data)
            self.assertIn(new[:-6].encode(), data)
        self.assertEqual(self.sync("status")[0], 0, "a second run finds everything in sync")

    def test_undo_restores_both_machines(self):
        self.seed()
        self.assertEqual(self.sync()[0], 0)
        self.put(self.m, "plans/p.md", b"v2", mtime=OLD + 10)
        self.assertEqual(self.sync()[0], 0)
        self.assertEqual(self.get(self.u, "plans/p.md"), b"v2")
        p = subprocess.run([sys.executable, f"{REPO}/claude-sync", "undo", "ubu", "--json"],
                           env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.get(self.u, "plans/p.md"), b"plan")

    def test_exec_config_stops_until_confirmed(self):
        self.seed()
        self.assertEqual(self.sync()[0], 0)
        self.put(self.m, "skills/x/SKILL.md", b"new skill")
        code, out = self.sync()
        self.assertEqual((code, out["stopped"]), (3, ["exec_config"]), out)
        self.assertFalse(os.path.exists(f"{self.u['home']}/.claude/skills/x/SKILL.md"))
        code, out = self.sync("--confirm", out["token"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.get(self.u, "skills/x/SKILL.md"), b"new skill")

    def test_lock_blocks_a_second_run(self):
        lock = {"host": "mac", "pid": os.getpid(), "run_id": "x",
                "start": subprocess.run(["ps", "-o", "lstart=", "-p", str(os.getpid())],
                                        capture_output=True, text=True).stdout.strip()}
        os.makedirs(f"{self.u['home']}/.cache/claude-sync", exist_ok=True)
        from claude_sync.remote import Runner
        Runner(None).call("lock_write", {**self.u, "lock": lock})
        code, out = self.sync()
        self.assertEqual(code, 5, out)


if __name__ == "__main__":
    unittest.main()
