"""End-to-end tests for the git part: real local git repos in temp folders, a bare repo
as each one's origin. Fake machines as in tests/test_cli.py."""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

from claude_sync import h_git

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
          "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com"}


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=True)


def _commit(path, name, content):
    with open(os.path.join(path, name), "w") as f:
        f.write(content)
    _git("add", name, cwd=path)
    _git("commit", "-m", "msg", cwd=path)


def _ssh_next(cmd, host):
    return f"ssh {host} " + shlex.quote("bash -ic " + shlex.quote(cmd))


class Git(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.m = {"home": f"{t}/mac", "desktop": f"{t}/mac/Library/Application Support/Claude"}
        self.u = {"home": f"{t}/ubu", "desktop": f"{t}/ubu/.config/Claude"}
        common = {"platform": "Test", "app_running": False, "cli_pids": [], "claude_version": "x"}
        self.env = {**os.environ, "CLAUDE_SYNC_TEST_ROOTS": json.dumps({
            "here": {**self.m, **common, "hostname": "mac"}, "ubu": {**self.u, **common, "hostname": "ubu"}})}
        for r in (self.m, self.u):
            os.makedirs(f"{r['home']}/.claude", exist_ok=True)
            os.makedirs(r["desktop"], exist_ok=True)
        self.bare = f"{t}/bare"

    def tearDown(self):
        self.tmp.cleanup()

    def sync(self, *args):
        p = subprocess.run([sys.executable, f"{REPO}/claude-sync", *args, "ubu", "--json"],
                           env=self.env, capture_output=True, text=True, timeout=120)
        self.assertNotIn("Traceback", p.stderr, p.stderr)
        return p.returncode, json.loads(p.stdout or "{}")

    def git_entries(self, out):
        return out.get("parts", {}).get("git", [])

    def by_state(self, out, state):
        return [e for e in self.git_entries(out) if e["state"] == state]

    def _repo_on_both(self, name):
        """The same repo, cloned from one bare origin, under both machines' dev folders."""
        bare_dir = f"{self.bare}/{name}.git"
        os.makedirs(bare_dir, exist_ok=True)
        _git("init", "--bare", "-b", "main", cwd=bare_dir)
        mac_path = os.path.join(self.m["home"], "dev", name)
        os.makedirs(mac_path, exist_ok=True)
        _git("init", "-b", "main", cwd=mac_path)
        _git("remote", "add", "origin", bare_dir, cwd=mac_path)
        _commit(mac_path, "a.txt", "1")
        _git("push", "-u", "origin", "main", cwd=mac_path)
        ubu_dev = os.path.join(self.u["home"], "dev")
        os.makedirs(ubu_dev, exist_ok=True)
        ubu_path = os.path.join(ubu_dev, name)
        _git("clone", bare_dir, ubu_path, cwd=self.tmp.name)
        _git("branch", "--set-upstream-to=origin/main", "main", cwd=ubu_path)
        return mac_path, ubu_path, bare_dir

    def _repo_on_mac_only(self, name):
        bare_dir = f"{self.bare}/{name}.git"
        os.makedirs(bare_dir, exist_ok=True)
        _git("init", "--bare", "-b", "main", cwd=bare_dir)
        path = os.path.join(self.m["home"], "dev", name)
        os.makedirs(path, exist_ok=True)
        _git("init", "-b", "main", cwd=path)
        _git("remote", "add", "origin", bare_dir, cwd=path)
        _commit(path, "a.txt", "1")
        _git("push", "-u", "origin", "main", cwd=path)
        return path, bare_dir

    def _repo_no_origin(self, home, name):
        path = os.path.join(home, "dev", name)
        os.makedirs(path, exist_ok=True)
        _git("init", "-b", "main", cwd=path)
        _commit(path, "a.txt", "1")
        return path

    def test_clean_repo_reports_nothing(self):
        self._repo_on_both("clean")
        code, out = self.sync("status")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.git_entries(out), [])
        self.assertNotIn("git", out["stopped"])

    def test_behind_gets_a_pull_next_and_no_stop(self):
        mac_path, ubu_path, bare_dir = self._repo_on_both("behind")
        other_clone = f"{self.tmp.name}/other-clone"
        _git("clone", bare_dir, other_clone, cwd=self.tmp.name)
        _commit(other_clone, "b.txt", "2")
        _git("push", "origin", "main", cwd=other_clone)
        _git("pull", "--ff-only", cwd=ubu_path)  # keep ubu current: only mac stays behind
        code, out = self.sync("status")
        entries = self.by_state(out, "behind")
        self.assertEqual(len(entries), 1, out)
        self.assertEqual(entries[0]["machine"], "here")
        self.assertNotIn("git", out["stopped"])
        self.assertEqual(entries[0]["next"], f"git -C {shlex.quote(mac_path)} pull --ff-only")

    def test_ahead_stops_with_a_push_next(self):
        mac_path, ubu_path, bare_dir = self._repo_on_both("ahead")
        _commit(mac_path, "c.txt", "unpushed")
        code, out = self.sync("status")
        self.assertIn("git", out["stopped"])
        entries = self.by_state(out, "ahead")
        self.assertEqual(len(entries), 1, out)
        self.assertEqual(entries[0]["machine"], "here")
        self.assertEqual(entries[0]["commits"], 1)
        self.assertEqual(entries[0]["next"], f"git -C {shlex.quote(mac_path)} push")

    def test_uncommitted_stops_and_lists_files(self):
        mac_path, ubu_path, bare_dir = self._repo_on_both("dirty")
        with open(os.path.join(mac_path, "a.txt"), "a") as f:
            f.write("edit")
        with open(os.path.join(mac_path, "new.txt"), "w") as f:
            f.write("new")
        code, out = self.sync("status")
        self.assertIn("git", out["stopped"])
        entries = self.by_state(out, "uncommitted")
        self.assertEqual(len(entries), 1, out)
        self.assertEqual(sorted(entries[0]["files"]), ["a.txt", "new.txt"])
        self.assertNotIn("next", entries[0])

    def test_repo_only_on_one_machine_clones_on_the_other(self):
        path, bare_dir = self._repo_on_mac_only("solo")
        code, out = self.sync("status")
        self.assertIn("git", out["stopped"])
        entries = self.by_state(out, "missing")
        self.assertEqual(len(entries), 1, out)
        self.assertEqual(entries[0]["machine"], "ubu")
        expect_path = f"{self.u['home']}/dev/solo"
        cmd = f"git clone -- {shlex.quote(bare_dir)} {shlex.quote(expect_path)}"
        self.assertEqual(entries[0]["next"], _ssh_next(cmd, "ubu"))

    def test_unsafe_origin_is_skipped_not_cloned(self):
        # fix round 4: a clone URL is content from the other machine and must never be
        # readable as an option by the shell command we hand back.
        path, bare_dir = self._repo_on_mac_only("unsafe")
        _git("config", "remote.origin.url", "--evil-flag", cwd=path)
        code, out = self.sync("status")
        entries = self.by_state(out, "missing")
        self.assertEqual(len(entries), 1, out)
        self.assertNotIn("next", entries[0])
        self.assertNotIn("git", out["stopped"])
        self.assertTrue(any(w.startswith("skipped:") and "unsafe" in w for w in out["warnings"]), out)

    def test_no_remote_is_a_warning_not_a_stop(self):
        self._repo_no_origin(self.m["home"], "norem")
        self._repo_no_origin(self.u["home"], "norem")
        code, out = self.sync("status")
        self.assertNotIn("git", out["stopped"])
        entries = self.by_state(out, "no_remote")
        self.assertEqual(len(entries), 2, out)  # neither side has a remote

    def test_skip_repo_removes_it_from_the_check(self):
        mac_path, ubu_path, bare_dir = self._repo_on_both("skipme")
        with open(os.path.join(mac_path, "new.txt"), "w") as f:
            f.write("new")
        code, out = self.sync("status", "--skip-repo", mac_path)
        self.assertNotIn("git", out["stopped"])
        self.assertEqual(self.git_entries(out), [])

    def test_next_command_runs_through_the_shell_for_a_path_with_a_space(self):
        mac_path, ubu_path, bare_dir = self._repo_on_both("my repo")
        _commit(mac_path, "c.txt", "unpushed")
        code, out = self.sync("status")
        entries = self.by_state(out, "ahead")
        self.assertEqual(len(entries), 1, out)
        next_cmd = entries[0]["next"]
        self.assertIn(shlex.quote(mac_path), next_cmd)
        subprocess.run(next_cmd, shell=True, cwd=self.tmp.name, env=GIT_ENV, check=True, capture_output=True)
        code, out = self.sync("status")
        self.assertEqual(self.by_state(out, "ahead"), [])

    def test_worktree_reported_separately_from_its_repo(self):
        mac_path, ubu_path, bare_dir = self._repo_on_both("wt")
        # .worktrees/ lives inside the repo's own working tree: ignore it there, as a real
        # project would, so the check is of the worktree itself, not an untracked folder.
        with open(os.path.join(mac_path, ".gitignore"), "w") as f:
            f.write(".worktrees/\n")
        _git("add", ".gitignore", cwd=mac_path)
        _git("commit", "-m", "ignore worktrees", cwd=mac_path)
        _git("push", cwd=mac_path)
        _git("pull", "--ff-only", cwd=ubu_path)  # so ubu's copy also ignores .worktrees/
        mac_wt = os.path.join(mac_path, ".worktrees", "feature")
        ubu_wt = os.path.join(ubu_path, ".worktrees", "feature")
        _git("worktree", "add", "-b", "feature", mac_wt, cwd=mac_path)
        _git("worktree", "add", "-b", "feature", ubu_wt, cwd=ubu_path)
        with open(os.path.join(mac_wt, "d.txt"), "w") as f:
            f.write("new")
        code, out = self.sync("status")
        # a worktree's own entries are separate from its main repo's path...
        wt_entries = [e for e in self.git_entries(out) if e["repo"].endswith(".worktrees/feature")]
        self.assertEqual(len(wt_entries), 2, out)
        # ...and per the fix round, a worktree only ever reports an unpushed branch: the
        # uncommitted file in it is not itself reported.
        self.assertEqual({e["state"] for e in wt_entries}, {"unpushed_branch"}, out)
        # the main repo itself, untouched, is not also reported
        self.assertEqual([e for e in self.git_entries(out) if e["repo"].endswith("/wt")], [])

    def test_worktree_only_on_one_machine_is_never_reported_as_missing(self):
        # fix round 3: a real run proposed cloning worktrees that exist on one machine
        # only. A worktree is never a "missing" candidate, only ever an unpushed branch.
        mac_path, ubu_path, bare_dir = self._repo_on_both("solowt")
        with open(os.path.join(mac_path, ".gitignore"), "w") as f:
            f.write(".worktrees/\n")
        _git("add", ".gitignore", cwd=mac_path)
        _git("commit", "-m", "ignore worktrees", cwd=mac_path)
        wt_path = os.path.join(mac_path, ".worktrees", "feature")
        _git("worktree", "add", "-b", "feature", wt_path, cwd=mac_path)  # only on mac
        code, out = self.sync("status")
        wt_entries = [e for e in self.git_entries(out) if "feature" in e["repo"]]
        self.assertEqual({e["state"] for e in wt_entries}, {"unpushed_branch"}, out)
        self.assertFalse(any(e["state"] == "missing" for e in wt_entries), out)
        self.assertFalse(any("next" in e for e in wt_entries), out)

    def test_unpushed_worktree_branch_reports_that_state_only(self):
        mac_path, ubu_path, bare_dir = self._repo_on_both("wtpush")
        with open(os.path.join(mac_path, ".gitignore"), "w") as f:
            f.write(".worktrees/\n")
        _git("add", ".gitignore", cwd=mac_path)
        _git("commit", "-m", "ignore worktrees", cwd=mac_path)
        _git("push", cwd=mac_path)
        _git("pull", "--ff-only", cwd=ubu_path)
        mac_wt = os.path.join(mac_path, ".worktrees", "feature")
        ubu_wt = os.path.join(ubu_path, ".worktrees", "feature")
        _git("worktree", "add", "-b", "feature", mac_wt, cwd=mac_path)
        _git("worktree", "add", "-b", "feature", ubu_wt, cwd=ubu_path)
        code, out = self.sync("status")
        wt_entries = [e for e in self.git_entries(out) if e["repo"].endswith(".worktrees/feature")]
        self.assertEqual({e["state"] for e in wt_entries}, {"unpushed_branch"}, out)
        self.assertIn("git", out["stopped"])


class HGitRobustness(unittest.TestCase):
    def test_run_git_kills_the_whole_process_group_on_timeout(self):
        # fix round 4: subprocess.run(timeout=...) only kills its immediate child, leaving
        # a git subprocess's own children (ssh, credential helpers, ...) running. _run_git
        # must kill the whole group instead.
        with tempfile.TemporaryDirectory() as t:
            pid_file = os.path.join(t, "pid")
            script = f"sleep 5 & echo $! > {shlex.quote(pid_file)}; wait"
            start = time.time()
            code, out, err = h_git._run_git(["sh", "-c", script], timeout=1)
            elapsed = time.time() - start
            self.assertIsNone(code)
            self.assertEqual(err, "timed out")
            self.assertLess(elapsed, 4, "the whole call must return promptly, not wait out the sleep")
            with open(pid_file) as f:
                child_pid = int(f.read().strip())
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError, msg="the grandchild sleep must have been killed too"):
            os.kill(child_pid, 0)

    def test_status_timeout_is_a_warning_not_a_crash(self):
        real_run_git = h_git._run_git
        h_git._run_git = lambda argv, timeout, use_bash_ic=False: (None, "", "timed out")
        try:
            changed, untracked, warning = h_git._status("/nonexistent")
        finally:
            h_git._run_git = real_run_git
        self.assertEqual((changed, untracked), ([], []))
        self.assertEqual(warning, "git status timed out")


if __name__ == "__main__":
    unittest.main()
