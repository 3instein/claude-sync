"""End-to-end tests for the mcp part, with fake machines as in tests/test_cli.py."""
import json
import os
import subprocess
import sys
import tempfile
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

    def put_claude_json(self, r, obj):
        with open(f"{r['home']}/.claude.json", "w") as f:
            json.dump(obj, f)

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
        # a real file at the mapped path, so no PATH resolution is needed for this test
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


if __name__ == "__main__":
    unittest.main()
