"""The update part: a sync runs `claude update` on both machines; status does not."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A fake claude: `--version` prints the version in its folder, `update` writes the new one.
FAKE = """#!/bin/sh
dir=$(dirname "$0")
case "$1" in
  --version) cat "$dir/version" ;;
  update) echo "2.1.283 (Claude Code)" > "$dir/version"; echo "$0 update" >> "$FAKE_CLAUDE_LOG"; echo updated ;;
esac
"""


class Update(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.log = f"{t}/log"
        roots = {}
        for name, ver in (("here", "2.1.280 (Claude Code)"), ("ubu", "2.1.281 (Claude Code)")):
            home = f"{t}/{name}"
            desktop = f"{home}/desk"
            os.makedirs(f"{home}/.claude", exist_ok=True)
            os.makedirs(desktop, exist_ok=True)
            os.makedirs(f"{home}/.local/bin", exist_ok=True)
            with open(f"{home}/.local/bin/claude", "w") as f:
                f.write(FAKE)
            os.chmod(f"{home}/.local/bin/claude", 0o755)
            with open(f"{home}/.local/bin/version", "w") as f:
                f.write(ver + "\n")
            roots[name] = {"home": home, "desktop": desktop, "hostname": name, "platform": "Test",
                           "app_running": False, "cli_pids": [], "claude_version": ver}
        self.env = {**os.environ, "FAKE_CLAUDE_LOG": self.log, "CLAUDE_SYNC_TEST_ROOTS": json.dumps(roots)}

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        p = subprocess.run([sys.executable, f"{REPO}/claude-sync", *args, "ubu", "--json"],
                           env=self.env, capture_output=True, text=True, timeout=120)
        self.assertNotIn("Traceback", p.stderr, p.stderr)
        return p.returncode, json.loads(p.stdout)

    def test_status_reports_and_does_not_update(self):
        code, out = self.run_cli("status")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["parts"]["update"]["versions"],
                         {"here": "2.1.280 (Claude Code)", "ubu": "2.1.281 (Claude Code)"})
        self.assertFalse(os.path.exists(self.log), "status must not run claude update")

    def test_sync_updates_both_machines(self):
        code, out = self.run_cli()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["parts"]["update"]["versions"]["here"]["after"], "2.1.283 (Claude Code)")
        self.assertEqual(out["parts"]["update"]["versions"]["ubu"]["after"], "2.1.283 (Claude Code)")
        with open(self.log) as f:
            self.assertEqual(len(f.read().splitlines()), 2)
        self.assertFalse(any(w.startswith("Claude Code versions differ") for w in out["warnings"]), out["warnings"])


if __name__ == "__main__":
    unittest.main()
