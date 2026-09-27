"""End-to-end tests for the plugins part: a fake `claude` script under each fake home's
.local/bin records its arguments and edits the fake plugin json files, so no real claude
runs. Fake machines as in tests/test_cli.py."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

from claude_sync import h_plugins

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A stand-in for the real `claude` binary: it only understands the four shapes p_plugins
# sends, logs each call, and updates the fake home's plugin json files the way the real
# CLI would, so the read side of a later scan sees the change.
FAKE_CLAUDE = '''#!/usr/bin/env python3
import json, os, sys

home = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
plugins_dir = os.path.join(home, ".claude", "plugins")
os.makedirs(plugins_dir, exist_ok=True)
installed_path = os.path.join(plugins_dir, "installed_plugins.json")
mkt_path = os.path.join(plugins_dir, "known_marketplaces.json")

def load(p):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

def save(p, obj):
    with open(p, "w") as f:
        json.dump(obj, f)

log = os.environ.get("FAKE_CLAUDE_LOG")
argv = sys.argv[1:]
if log:
    with open(log, "a") as f:
        f.write(json.dumps(argv) + "\\n")
if argv[:1] == ["plugin"]:
    argv = argv[1:]
if argv[0] == "install":
    data = load(installed_path)
    data.setdefault("version", 2)
    data.setdefault("plugins", {})[argv[1]] = [{"scope": "user"}]
    save(installed_path, data)
elif argv[0] == "uninstall":
    data = load(installed_path)
    data.get("plugins", {}).pop(argv[1], None)
    save(installed_path, data)
elif argv[0] == "marketplace" and argv[1] == "add":
    data = load(mkt_path)
    data[argv[2].split("/")[-1]] = {"source": {"source": "github", "repo": argv[2]}}
    save(mkt_path, data)
elif argv[0] == "marketplace" and argv[1] == "remove":
    data = load(mkt_path)
    data.pop(argv[2], None)
    save(mkt_path, data)
sys.exit(0)
'''


class Plugins(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.m = {"home": f"{t}/mac", "desktop": f"{t}/mac/Library/Application Support/Claude"}
        self.u = {"home": f"{t}/ubu", "desktop": f"{t}/ubu/.config/Claude"}
        common = {"platform": "Test", "app_running": False, "cli_pids": [], "claude_version": "x"}
        self.log = f"{t}/claude.log"
        self.env = {**os.environ, "FAKE_CLAUDE_LOG": self.log, "CLAUDE_SYNC_TEST_ROOTS": json.dumps({
            "here": {**self.m, **common, "hostname": "mac"}, "ubu": {**self.u, **common, "hostname": "ubu"}})}
        for r in (self.m, self.u):
            os.makedirs(f"{r['home']}/.claude", exist_ok=True)
            os.makedirs(r["desktop"], exist_ok=True)
            bin_dir = f"{r['home']}/.local/bin"
            os.makedirs(bin_dir, exist_ok=True)
            script = f"{bin_dir}/claude"
            with open(script, "w") as f:
                f.write(FAKE_CLAUDE)
            st = os.stat(script)
            os.chmod(script, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def tearDown(self):
        self.tmp.cleanup()

    def sync(self, *args):
        p = subprocess.run([sys.executable, f"{REPO}/claude-sync", *args, "ubu", "--json"],
                           env=self.env, capture_output=True, text=True, timeout=120)
        self.assertNotIn("Traceback", p.stderr, p.stderr)
        return p.returncode, json.loads(p.stdout or "{}")

    def seed_installed(self, r, ids):
        d = f"{r['home']}/.claude/plugins"
        os.makedirs(d, exist_ok=True)
        with open(f"{d}/installed_plugins.json", "w") as f:
            json.dump({"version": 2, "plugins": {pid: [{"scope": "user"}] for pid in ids}}, f)

    def seed_marketplaces(self, r, mkts):
        d = f"{r['home']}/.claude/plugins"
        os.makedirs(d, exist_ok=True)
        with open(f"{d}/known_marketplaces.json", "w") as f:
            json.dump(mkts, f)

    def installed(self, r):
        with open(f"{r['home']}/.claude/plugins/installed_plugins.json") as f:
            return json.load(f)["plugins"]

    def marketplaces(self, r):
        with open(f"{r['home']}/.claude/plugins/known_marketplaces.json") as f:
            return json.load(f)

    def log_lines(self):
        try:
            with open(self.log) as f:
                return [json.loads(l) for l in f if l.strip()]
        except FileNotFoundError:
            return []

    def test_first_run_installs_missing_and_removes_nothing(self):
        self.seed_installed(self.m, ["foo@bar"])
        self.seed_installed(self.u, ["baz@bar"])
        code, out = self.sync()
        self.assertEqual((code, out["stopped"]), (3, ["exec_config"]), out)
        code, out = self.sync("--confirm", out["token"])
        self.assertEqual(code, 0, out)
        self.assertIn("foo@bar", self.installed(self.u))
        self.assertIn("baz@bar", self.installed(self.m))
        self.assertIn("baz@bar", self.installed(self.u), "first run removes nothing")
        self.assertIn("foo@bar", self.installed(self.m), "first run removes nothing")

    def test_removed_on_one_machine_is_removed_on_the_other(self):
        self.seed_installed(self.m, ["foo@bar"])
        self.seed_installed(self.u, ["foo@bar"])
        self.assertEqual(self.sync()[0], 0, "both machines agree, nothing to confirm")
        self.seed_installed(self.m, [])
        code, out = self.sync()
        self.assertEqual((code, out["stopped"]), (3, ["exec_config"]), out)
        code, out = self.sync("--confirm", out["token"])
        self.assertEqual(code, 0, out)
        self.assertNotIn("foo@bar", self.installed(self.u))

    def test_marketplace_added_before_its_plugin_is_installed(self):
        self.seed_marketplaces(self.m, {"foo-mkt": {"source": {"source": "github", "repo": "someorg/foo-mkt"}}})
        self.seed_installed(self.m, ["plug@foo-mkt"])
        self.seed_marketplaces(self.u, {})
        self.seed_installed(self.u, [])
        code, out = self.sync()
        self.assertEqual((code, out["stopped"]), (3, ["exec_config"]), out)
        code, out = self.sync("--confirm", out["token"])
        self.assertEqual(code, 0, out)
        self.assertIn("foo-mkt", self.marketplaces(self.u))
        self.assertIn("plug@foo-mkt", self.installed(self.u))
        lines = self.log_lines()
        add_i = lines.index(["plugin", "marketplace", "add", "someorg/foo-mkt"])
        install_i = lines.index(["plugin", "install", "plug@foo-mkt"])
        self.assertLess(add_i, install_i, "marketplaces before plugins")

    def test_marketplace_removed_on_one_machine_is_removed_on_the_other(self):
        mkt = {"foo-mkt": {"source": {"source": "github", "repo": "someorg/foo-mkt"}}}
        self.seed_marketplaces(self.m, mkt)
        self.seed_marketplaces(self.u, mkt)
        self.seed_installed(self.m, [])
        self.seed_installed(self.u, [])
        self.assertEqual(self.sync()[0], 0)
        self.seed_marketplaces(self.u, {})
        code, out = self.sync()
        self.assertEqual((code, out["stopped"]), (3, ["exec_config"]), out)
        code, out = self.sync("--confirm", out["token"])
        self.assertEqual(code, 0, out)
        self.assertNotIn("foo-mkt", self.marketplaces(self.m))
        self.assertIn(["plugin", "marketplace", "remove", "foo-mkt"], self.log_lines())

    def test_run_rejects_unknown_argument_shapes(self):
        for bad in (["install"], ["install", "a", "b"], ["marketplace", "list"], ["frobnicate"], ["uninstall", "a", "b"]):
            with self.assertRaises(ValueError):
                h_plugins.cmd_plugins_run({"home": self.m["home"], "desktop": self.m["desktop"], "argv": bad}, b"")

    def test_read_lists_user_scope_plugins_and_marketplace_sources(self):
        d = f"{self.m['home']}/.claude/plugins"
        os.makedirs(d, exist_ok=True)
        with open(f"{d}/installed_plugins.json", "w") as f:
            json.dump({"version": 2, "plugins": {
                "user-one@mkt": [{"scope": "user"}],
                "project-only@mkt": [{"scope": "project", "projectPath": "/x"}]}}, f)
        with open(f"{d}/known_marketplaces.json", "w") as f:
            json.dump({"mkt": {"source": {"source": "github", "repo": "org/mkt"}},
                       "gitmkt": {"source": {"source": "git", "url": "https://example.com/x.git"}}}, f)
        out = h_plugins.cmd_plugins_read({"home": self.m["home"], "desktop": self.m["desktop"]}, b"")
        self.assertEqual(out["installed"], ["user-one@mkt"])
        self.assertEqual(out["marketplaces"], {"mkt": "org/mkt", "gitmkt": "https://example.com/x.git"})

    def test_read_with_no_plugin_files_is_empty(self):
        out = h_plugins.cmd_plugins_read({"home": self.m["home"], "desktop": self.m["desktop"]}, b"")
        self.assertEqual(out, {"installed": [], "marketplaces": {}})


if __name__ == "__main__":
    unittest.main()
