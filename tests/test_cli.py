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

    def test_scratch_session_under_the_desktop_folder(self):
        # A Code tab scratch session: its cwd is inside the desktop folder.
        cwd = self.m["desktop"] + "/scratch-workspaces/o/a/scratch-1"
        line = json.dumps({"type": "user", "cwd": cwd, "sessionId": "s9"}, separators=(",", ":")).encode() + b"\n"
        self.put(self.m, f"projects/{paths.folder_name(cwd)}/s9.jsonl", line)
        self.assertEqual(self.sync()[0], 0)
        ucwd = self.u["desktop"] + "/scratch-workspaces/o/a/scratch-1"
        moved = json.loads(self.get(self.u, f"projects/{paths.folder_name(ucwd)}/s9.jsonl"))
        self.assertEqual(moved["cwd"], ucwd)

    def test_migrated_copy_with_rewritten_text_is_in_sync(self):
        # The migration replaced the home path in all text, not only in path fields.
        mcwd, ucwd = self.m["home"] + "/dev/x", self.u["home"] + "/dev/x"
        def t(home, cwd):
            return (json.dumps({"cwd": cwd, "sessionId": "s1", "message": f"see {home}/notes"},
                               separators=(",", ":")) + "\n").encode()
        self.put(self.m, f"{self.project(self.m)}/s1.jsonl", t(self.m["home"], mcwd))
        self.put(self.u, f"{self.project(self.u)}/s1.jsonl", t(self.u["home"], ucwd))
        code, out = self.sync("status")
        self.assertEqual((code, out["files"]["both_changed"], out["splits"]), (0, 0, 0), out)

    def test_memory_only_folder_joins_the_same_key(self):
        mcwd = self.m["home"] + "/dev/x"
        line = json.dumps({"cwd": mcwd, "sessionId": "s1"}, separators=(",", ":")).encode() + b"\n"
        self.put(self.m, f"{self.project(self.m)}/s1.jsonl", line)
        self.put(self.m, f"{self.project(self.m)}/memory/MEMORY.md", b"mac memory")
        self.put(self.u, f"{self.project(self.u)}/memory/MEMORY.md", b"ubuntu memory")
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        self.assertFalse(any("two keys" in w for w in out["warnings"]), out["warnings"])
        self.assertIn("cli/projects/{~/dev/x}/memory/MEMORY.md", out.get("conflicts", []))

    def test_undo_reverts_only_the_last_run(self):
        self.seed()
        self.assertEqual(self.sync()[0], 0)
        self.put(self.u, "plans/p.md", b"ubuntu v2", mtime=OLD + 10)
        self.assertEqual(self.sync()[0], 0)
        self.assertEqual(self.get(self.m, "plans/p.md"), b"ubuntu v2")
        p = subprocess.run([sys.executable, f"{REPO}/claude-sync", "undo", "ubu", "--json"],
                           env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.get(self.m, "plans/p.md"), b"plan", "the Mac file goes back")
        self.assertEqual(self.get(self.u, "plans/p.md"), b"ubuntu v2", "the Ubuntu edit is not touched")

    def test_sync_after_undo_asks_instead_of_reverting(self):
        self.seed()
        self.assertEqual(self.sync()[0], 0)
        self.put(self.m, "plans/p.md", b"mac v2", mtime=OLD + 10)
        self.assertEqual(self.sync()[0], 0)
        subprocess.run([sys.executable, f"{REPO}/claude-sync", "undo", "ubu", "--json"], env=self.env, check=True,
                       capture_output=True)
        self.assertEqual(self.get(self.u, "plans/p.md"), b"plan")
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.get(self.m, "plans/p.md"), b"mac v2", "the Mac edit is not reverted")
        self.assertIn("cli/plans/p.md", out.get("conflicts", []))

    def test_partial_last_line_does_not_block_updates(self):
        self.seed()
        self.assertEqual(self.sync()[0], 0)
        tu = f"{self.project(self.u)}/s1.jsonl"
        self.put(self.u, tu, self.get(self.u, tu) + b'{"partial', mtime=OLD)
        t = f"{self.project(self.m)}/s1.jsonl"
        self.put(self.m, t, self.get(self.m, t) + b'{"uuid":"2","sessionId":"s1"}\n', mtime=OLD + 10)
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        self.assertIn(b'"uuid":"2"', self.get(self.u, tu))

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
