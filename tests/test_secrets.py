"""End-to-end tests for the secrets report part (p_secrets.py, h_secrets.py). Same
fake-machine harness as tests/test_cli.py. The report never syncs a file: it only
lists scp commands, and it must never leak a hash or the file content."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SecretsReport(unittest.TestCase):
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

    def test_different_and_missing_secrets_report_scp_commands_with_no_hash_or_content(self):
        self.put(self.m, "proj/.env", b"MAC_SECRET_VALUE=1")
        self.put(self.u, "proj/.env", b"UBU_SECRET_VALUE=2")
        self.put(self.m, "other/id_rsa", b"-----BEGIN PRIVATE KEY-----mac-only-key")
        code, out = self.sync("status")
        self.assertEqual(code, 0, out)
        secrets = out["parts"]["secrets"]
        self.assertEqual(secrets["different"], ["~/dev/proj/.env"])
        self.assertEqual(secrets["missing_there"], ["~/dev/other/id_rsa"])
        self.assertEqual(secrets["missing_here"], [])
        self.assertEqual(set(secrets["next"]), {"~/dev/proj/.env", "~/dev/other/id_rsa"})
        for cmd in secrets["next"].values():
            self.assertTrue(cmd.startswith("scp "), cmd)
        self.assertFalse(os.path.exists(f"{self.u['home']}/dev/other/id_rsa"), "a secret is never written")

        dumped = json.dumps(out)
        self.assertNotIn("MAC_SECRET_VALUE", dumped)
        self.assertNotIn("UBU_SECRET_VALUE", dumped)
        self.assertNotIn("BEGIN PRIVATE KEY", dumped)
        self.assertNotIn("hash", dumped.lower())
        self.assertIsNone(re.search(r"\b[0-9a-f]{64}\b", dumped), "no sha256 hex digest leaks out")

    def test_report_never_stops_or_counts_against_in_sync(self):
        self.put(self.m, "proj/.env", b"only-on-mac")
        code, out = self.sync("status")
        self.assertEqual(code, 0, out)
        self.assertTrue(out["in_sync"])
        self.assertEqual(out["stopped"], [])


if __name__ == "__main__":
    unittest.main()
