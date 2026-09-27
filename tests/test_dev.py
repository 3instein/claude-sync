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

    def make_worktree(self, m, rel):
        # A git worktree's ".git" is a pointer file, not a folder.
        p = f"{m['home']}/dev/{rel}"
        os.makedirs(p, exist_ok=True)
        with open(f"{p}/.git", "w") as f:
            f.write("gitdir: /elsewhere/.git/worktrees/x\n")

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
        # guard_dev only copies settings.local.json to a machine that has that repo,
        # so both fake machines need "myrepo" for the copy to go through.
        self.make_repo(self.m, "myrepo")
        self.make_repo(self.u, "myrepo")
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

    def test_worktree_folder_is_not_synced(self):
        # A folder whose .git is a file (a worktree) is a repo for the dev walk too:
        # neither its files nor the .git pointer file itself are copied.
        self.make_worktree(self.m, "wt")
        self.put(self.m, "wt/file.txt", b"tracked by the worktree, not by claude-sync")
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        self.assertFalse(self.exists(self.u, "wt/file.txt"))
        self.assertFalse(self.exists(self.u, "wt/.git"))

    def test_ds_store_is_not_synced_under_dev(self):
        self.put(self.m, ".DS_Store", b"finder junk")
        self.put(self.m, "proj/.DS_Store", b"finder junk")
        self.put(self.m, "proj/keep.md", b"real file")
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        self.assertFalse(self.exists(self.u, ".DS_Store"))
        self.assertFalse(self.exists(self.u, "proj/.DS_Store"))
        self.assertEqual(self.get(self.u, "proj/keep.md"), b"real file")

    def test_dev_file_moved_into_a_repo_is_not_deleted_on_the_other_machine(self):
        # Regression: `git init` in a synced ~/dev folder used to make the next sync
        # delete that folder's files on the other machine, with no stop.
        self.put(self.m, "keep.md", b"always here")
        self.put(self.m, "notes/todo.md", b"keep me")
        self.assertEqual(self.sync()[0], 0)
        self.assertEqual(self.get(self.u, "notes/todo.md"), b"keep me")
        self.make_repo(self.m, "notes")  # git init after the fact
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.get(self.u, "notes/todo.md"), b"keep me",
                         "not deleted just because the folder became a repo on the other machine")
        self.assertTrue(any("not deleted" in w and "dev/notes/todo.md" in w for w in out["warnings"]), out["warnings"])


if __name__ == "__main__":
    unittest.main()
