"""End-to-end tests for the mcp part, with fake machines as in tests/test_cli.py."""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class McpPart(unittest.TestCase):
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

    def put_claude_json(self, r, obj, mtime=None):
        p = f"{r['home']}/.claude.json"
        with open(p, "w") as f:
            json.dump(obj, f)
        if mtime is not None:
            os.utime(p, (mtime, mtime))

    def get_claude_json(self, r):
        with open(f"{r['home']}/.claude.json") as f:
            return json.load(f)

    def make_exe(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(path, 0o755)

    def test_new_server_home_path_mapped_and_confirm(self):
        cmd = f"{self.m['home']}/.local/bin/mytool"
        self.put_claude_json(self.m, {"mcpServers": {"tool": {"command": cmd, "args": ["--x"]}}})
        self.put_claude_json(self.u, {"theme": "dark"})
        # a real file at the mapped path on both sides, so resolution finds it cleanly
        self.make_exe(f"{self.m['home']}/.local/bin/mytool")
        self.make_exe(f"{self.u['home']}/.local/bin/mytool")
        code, out = self.sync()
        self.assertEqual((code, out["stopped"]), (3, ["exec_config"]), out)
        self.assertNotIn("mcpServers", self.get_claude_json(self.u))
        code, out = self.sync("--confirm", out["token"])
        self.assertEqual(code, 0, out)
        u_cfg = self.get_claude_json(self.u)
        self.assertEqual(u_cfg["mcpServers"]["tool"]["command"], f"{self.u['home']}/.local/bin/mytool")
        self.assertEqual(u_cfg["mcpServers"]["tool"]["args"], ["--x"])
        self.assertEqual(u_cfg["theme"], "dark", "other keys untouched")
        self.assertEqual(self.get_claude_json(self.m), {"mcpServers": {"tool": {"command": cmd, "args": ["--x"]}}},
                         "the source side is not rewritten")
        self.assertEqual(self.sync("status")[0], 0, "a second run finds everything in sync")

    def test_command_resolved_by_name_on_target(self):
        # the command lives at a fixed path that only exists on the mac; ubuntu must
        # find the same command name through its own PATH / .local/bin instead.
        cmd = "/opt/claude-sync-test-does-not-exist/mytool"
        self.put_claude_json(self.m, {"mcpServers": {"tool": {"command": cmd}}})
        self.put_claude_json(self.u, {})
        self.make_exe(f"{self.u['home']}/.local/bin/mytool")
        code, out = self.sync()
        self.assertEqual(code, 3, out)
        code, out = self.sync("--confirm", out["token"])
        self.assertEqual(code, 0, out)
        u_cfg = self.get_claude_json(self.u)
        self.assertEqual(u_cfg["mcpServers"]["tool"]["command"], f"{self.u['home']}/.local/bin/mytool")

    def test_secret_env_server_skipped_with_warning(self):
        self.put_claude_json(self.m, {"mcpServers": {"secure": {"command": "run-secure",
                                                                 "env": {"API_KEY": "sk-xxxx"}}}})
        self.put_claude_json(self.u, {})
        code, out = self.sync()
        self.assertEqual(code, 0, out)  # nothing to confirm: the secret server is never copied
        self.assertTrue(any("secure" in w and "secret" in w for w in out["warnings"]), out["warnings"])
        self.assertNotIn("secure", self.get_claude_json(self.u).get("mcpServers", {}))

    def test_path_valued_env_name_is_not_a_secret(self):
        # google-sheets style: *_PATH env names hold file paths, not secrets.
        self.put_claude_json(self.m, {"mcpServers": {"google-sheets": {
            "command": "npx", "env": {"CREDENTIALS_PATH": "/Users/x/creds.json", "TOKEN_PATH": "~/token.json"}}}})
        self.put_claude_json(self.u, {})
        code, out = self.sync()
        self.assertEqual((code, out["stopped"]), (3, ["exec_config"]), out)
        code, out = self.sync("--confirm", out["token"])
        self.assertEqual(code, 0, out)
        self.assertIn("google-sheets", self.get_claude_json(self.u)["mcpServers"])

    def test_various_secret_patterns_are_all_skipped(self):
        self.put_claude_json(self.m, {"mcpServers": {
            "flag": {"command": "run", "args": ["--api-key", "sk-live-xyz"]},
            "web": {"command": "npx", "url": "https://example.com/mcp?token=abc123"},
            "db1": {"command": "run", "env": {"DB_PASS": "hunter2"}},
            "db2": {"command": "run", "env": {"DATABASE_URL": "postgres://user:hunter2@host/db"}},
        }})
        self.put_claude_json(self.u, {})
        code, out = self.sync()
        self.assertEqual(code, 0, out)  # every server is a secret, so there is nothing to confirm
        for name in ("flag", "web", "db1", "db2"):
            self.assertTrue(any(name in w and "secret" in w for w in out["warnings"]), (name, out["warnings"]))
        self.assertEqual(self.get_claude_json(self.u).get("mcpServers", {}), {})

    def test_secret_survives_a_newer_copy_that_lost_it(self):
        secret_server = {"command": "run", "env": {"API_TOKEN": "tok-abc"}}
        self.put_claude_json(self.m, {"mcpServers": {"secure": dict(secret_server)}}, mtime=1000)
        self.put_claude_json(self.u, {"mcpServers": {"secure": dict(secret_server)}}, mtime=1000)
        self.assertEqual(self.sync("status")[0], 0, "both sides already match")
        # ubuntu's copy is rewritten without the secret, and is newer
        self.put_claude_json(self.u, {"mcpServers": {"secure": {"command": "run"}}}, mtime=2000)
        code, out = self.sync()
        self.assertEqual(code, 0, out)  # the secret server is excluded from the merge, nothing to confirm
        self.assertTrue(any("has a secret value" in w for w in out["warnings"]), out["warnings"])
        self.assertEqual(self.get_claude_json(self.m)["mcpServers"]["secure"], secret_server,
                         "the mac keeps its token even though ubuntu's newer copy lost it")

    def test_resolved_command_does_not_bounce_back_to_the_source(self):
        cmd = "/opt/claude-sync-test-does-not-exist/mytool"
        self.put_claude_json(self.m, {"mcpServers": {"tool": {"command": cmd}}})
        self.put_claude_json(self.u, {})
        self.make_exe(f"{self.u['home']}/.local/bin/mytool")
        code, out = self.sync()
        self.sync("--confirm", out["token"])
        # a second run must not carry ubuntu's resolved path (missing on the mac) back to it
        code, out = self.sync("status")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.get_claude_json(self.m)["mcpServers"]["tool"]["command"], cmd)

    def test_project_only_written_where_the_folder_exists(self):
        exists_m, exists_u = f"{self.m['home']}/dev/exists", f"{self.u['home']}/dev/exists"
        missing_m = f"{self.m['home']}/dev/missing"
        os.makedirs(exists_m)
        os.makedirs(exists_u)  # the matching folder exists on ubuntu too
        os.makedirs(missing_m)  # nothing matching exists on ubuntu
        self.put_claude_json(self.m, {"projects": {
            exists_m: {"allowedTools": ["Bash"]}, missing_m: {"allowedTools": ["Bash"]}}})
        self.put_claude_json(self.u, {})
        code, out = self.sync()
        self.assertEqual((code, out["stopped"]), (3, ["exec_config"]), out)
        code, out = self.sync("--confirm", out["token"])
        self.assertEqual(code, 0, out)
        u_projects = self.get_claude_json(self.u).get("projects", {})
        self.assertIn(exists_u, u_projects)
        self.assertNotIn(f"{self.u['home']}/dev/missing", u_projects)

    def test_a_working_command_on_each_side_is_kept(self):
        # An nvm npx on one machine and a ~/.local/bin npx on the other are both kept as they are.
        for r, path in ((self.m, "nvm/v22/bin"), (self.u, ".local/bin")):
            os.makedirs(f"{r['home']}/{path}", exist_ok=True)
            tool = f"{r['home']}/{path}/tool"
            with open(tool, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(tool, 0o755)
        self.put_claude_json(self.m, {"mcpServers": {"t": {"command": f"{self.m['home']}/nvm/v22/bin/tool", "args": ["a"]}}}, mtime=1000)
        self.put_claude_json(self.u, {"mcpServers": {"t": {"command": f"{self.u['home']}/.local/bin/tool", "args": ["b"]}}}, mtime=2000)
        code, out = self.sync()
        if code == 3:
            code, out = self.sync("--confirm", out["token"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.get_claude_json(self.m)["mcpServers"]["t"]["command"], f"{self.m['home']}/nvm/v22/bin/tool")
        self.assertEqual(self.get_claude_json(self.m)["mcpServers"]["t"]["args"], ["b"])

    def test_secret_flag_forms(self):
        from claude_sync import p_mcp
        for args in (["--api-key=sk-live"], ["--access-token", "ghp_x"], ["--client-secret", "cs"]):
            self.assertTrue(p_mcp._has_secret({"command": "x", "args": args}), args)
        self.assertFalse(p_mcp._has_secret({"command": "x", "args": ["--token-file"]}))

    def test_review_item_has_a_content_hash_and_result_lists_the_json(self):
        self.put_claude_json(self.m, {"mcpServers": {"tool": {"command": "npx", "args": ["-y", "x"]}}})
        self.put_claude_json(self.u, {})
        code, out = self.sync()
        self.assertEqual(code, 3, out)
        items = out["review"]["exec_config"]
        self.assertTrue(items, out)
        for side, item in items:
            self.assertRegex(item, r"^mcp:[^@]+@[0-9a-f]{8}$", item)
        shown = [v for k, v in out["parts"]["mcp"]["mcpServers"].items() if k.startswith("tool (")]
        self.assertTrue(shown, out["parts"]["mcp"])
        self.assertEqual(shown[0]["args"], ["-y", "x"])


if __name__ == "__main__":
    unittest.main()
