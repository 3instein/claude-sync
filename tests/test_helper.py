"""Acceptance tests for helper.py and remote.py. Every call runs the real combined program
locally through remote.Runner(None), on fake machines in a temporary folder."""
import io
import json
import os
import stat
import subprocess
import tarfile
import tempfile
import time
import unittest

from claude_sync import paths, remote
from claude_sync.model import Machine

MTIME = 1_790_000_000


def read(p):
    with open(p, "rb") as f:
        return f.read()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.mac = Machine("here", f"{t}/mac", f"{t}/mac/Library/Application Support/Claude", "mac")
        self.ubu = Machine("ubuntu-pc", f"{t}/ubu", f"{t}/ubu/.config/Claude", "ubu")
        for m in (self.mac, self.ubu):
            os.makedirs(f"{m.home}/.claude", exist_ok=True)
            os.makedirs(m.desktop, exist_ok=True)
        self.run = remote.Runner(None)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, m, command, stdin=b"", **args):
        out = self.run.call(command, {"home": m.home, "desktop": m.desktop, **args}, stdin)
        return out if command == "pack" else json.loads(out)

    def put(self, m, rel, data, mtime=MTIME):
        p = f"{m.home}/.claude/{rel}"
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        os.utime(p, (mtime, mtime))
        return p

    def transcript(self, m):
        cwd = f"{m.home}/dev/x"
        data = json.dumps({"type": "user", "cwd": cwd, "sessionId": "s1"}, separators=(",", ":")).encode()
        return cwd, data + b"\n" + b'{"type":"assistant","partial'


class Inventory(Base):
    def test_keys_sizes_and_skips(self):
        cwd, data = self.transcript(self.mac)
        self.put(self.mac, f"projects/{paths.folder_name(cwd)}/s1.jsonl", data)
        self.put(self.mac, "projects/-x-memory-only/memory/a.md", b"m")
        self.put(self.mac, "skills/.DS_Store", b"junk")
        self.put(self.mac, "telemetry/t.json", b"out of scope")
        os.symlink("/etc/hosts", f"{self.mac.home}/.claude/skills/link")
        files = self.call(self.mac, "inventory", folders={})["files"]
        key = "cli/projects/{~/dev/x}/s1.jsonl"
        self.assertIn(key, files)
        h, mtime, size = files[key]
        self.assertEqual((mtime, size), (MTIME, data.index(b"\n") + 1), "the partial last line is not counted")
        self.assertIn("cli/projects/[-x-memory-only]/memory/a.md", files)
        self.assertFalse(any(k.startswith("cli/telemetry") or "DS_Store" in k or k.endswith("link") for k in files))

    def test_same_hash_on_both_machines(self):
        for m in (self.mac, self.ubu):
            cwd, data = self.transcript(m)
            self.put(m, f"projects/{paths.folder_name(cwd)}/s1.jsonl", data)
        key = "cli/projects/{~/dev/x}/s1.jsonl"
        a = self.call(self.mac, "inventory", folders={})["files"][key][0]
        b = self.call(self.ubu, "inventory", folders={})["files"][key][0]
        self.assertEqual(a, b)


class PackApplyUndo(Base):
    KEY = "cli/projects/{~/dev/x}/s1.jsonl"

    def send(self, keys, run_id="r1"):
        tar = self.call(self.mac, "pack", keys=keys)
        return self.call(self.ubu, "apply", stdin=tar, run_id=run_id,
                         src={"home": self.mac.home, "desktop": self.mac.desktop})

    def test_apply_maps_paths_and_sets_mode_and_mtime(self):
        cwd, data = self.transcript(self.mac)
        self.put(self.mac, f"projects/{paths.folder_name(cwd)}/s1.jsonl", data)
        out = self.send([self.KEY])
        target = f"{self.ubu.home}/.claude/projects/{paths.folder_name(self.ubu.home + '/dev/x')}/s1.jsonl"
        with open(target, "rb") as f:
            written = f.read()
        self.assertEqual(json.loads(written.splitlines()[0])["cwd"], f"{self.ubu.home}/dev/x")
        self.assertNotIn(b"partial", written, "only full lines are sent")
        st = os.stat(target)
        self.assertEqual((stat.S_IMODE(st.st_mode), int(st.st_mtime)), (0o600, MTIME))
        self.assertEqual(out["written"][self.KEY], [MTIME, len(written)])
        cache = f"{self.ubu.home}/.cache/claude-sync"
        self.assertEqual(stat.S_IMODE(os.stat(cache).st_mode), 0o700)

    def test_backup_and_undo(self):
        self.put(self.mac, "plans/p.md", b"new", mtime=MTIME + 5)
        old = self.put(self.ubu, "plans/p.md", b"old")
        self.put(self.mac, "plans/q.md", b"created")
        self.send(["cli/plans/p.md", "cli/plans/q.md"], run_id="r1")
        self.assertEqual(read(old), b"new")
        out = self.call(self.ubu, "undo", run_id="r1")
        self.assertEqual(read(old), b"old")
        self.assertFalse(os.path.exists(f"{self.ubu.home}/.claude/plans/q.md"))
        self.assertTrue(out["restored"] and out["removed"])

    def test_undo_restores_the_state_before_the_run(self):
        # One run can write a key twice (a copy, then a split) and delete others: undo goes back to the start.
        old = self.put(self.ubu, "plans/p.md", b"old")
        gone = self.put(self.ubu, "plans/q.md", b"q")
        self.put(self.mac, "plans/p.md", b"v1")
        self.send(["cli/plans/p.md"], run_id="r9")
        self.put(self.mac, "plans/p.md", b"v2")
        self.send(["cli/plans/p.md"], run_id="r9")
        self.call(self.ubu, "delete", run_id="r9", keys=["cli/plans/q.md"])
        self.assertEqual(read(old), b"v2")
        self.call(self.ubu, "undo", run_id="r9")
        self.assertEqual((read(old), read(gone)), (b"old", b"q"))

    def test_delete_and_undo(self):
        p = self.put(self.ubu, "plans/p.md", b"x")
        self.assertEqual(self.call(self.ubu, "delete", run_id="r2", keys=["cli/plans/p.md"])["deleted"],
                         ["cli/plans/p.md"])
        self.assertFalse(os.path.exists(p))
        self.call(self.ubu, "undo", run_id=None)
        self.assertEqual(read(p), b"x")

    def test_conflict_member_goes_to_conflicts_folder(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w|gz") as tar:
            info = tarfile.TarInfo("conflicts/cli/plans/p.md")
            info.size, info.mtime = 1, MTIME
            tar.addfile(info, io.BytesIO(b"c"))
        self.call(self.ubu, "apply", stdin=buf.getvalue(), run_id="r3",
                  src={"home": self.mac.home, "desktop": self.mac.desktop})
        found = [os.path.join(d, f) for d, _, fs in os.walk(f"{self.ubu.home}/.cache/claude-sync/conflicts/r3")
                 for f in fs]
        self.assertEqual(len(found), 1)
        self.assertFalse(os.path.exists(f"{self.ubu.home}/.claude/plans/p.md"))


class LockStateProcs(Base):
    def test_lock_round_trip(self):
        self.assertIsNone(self.call(self.mac, "lock_read")["lock"])
        lock = {"host": "mac", "pid": 1, "start": "x", "run_id": "r"}
        self.call(self.mac, "lock_write", lock=lock)
        self.assertEqual(self.call(self.mac, "lock_read")["lock"], lock)
        self.call(self.mac, "lock_remove")
        self.assertIsNone(self.call(self.mac, "lock_read")["lock"])

    def test_proc_alive_checks_start_time(self):
        start = subprocess.run(["ps", "-o", "lstart=", "-p", str(os.getpid())],
                               capture_output=True, text=True).stdout.strip()
        self.assertTrue(self.call(self.mac, "proc_alive", pid=os.getpid(), start=start)["alive"])
        self.assertFalse(self.call(self.mac, "proc_alive", pid=os.getpid(), start="Mon Jan  1 00:00:00 2001")["alive"])
        self.assertFalse(self.call(self.mac, "proc_alive", pid=999_999, start=start)["alive"])

    def test_state_round_trip(self):
        self.assertIsNone(self.call(self.mac, "state_read")["state"])
        state = {"version": 1, "run_id": "r", "files": {}, "folders": {}, "base": {}}
        self.call(self.mac, "state_write", stdin=json.dumps(state).encode())
        self.assertEqual(self.call(self.mac, "state_read")["state"], state)
        p = f"{self.mac.home}/.cache/claude-sync/state.json"
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)

    def test_prune_old_backups(self):
        self.put(self.ubu, "plans/p.md", b"x")
        self.call(self.ubu, "delete", run_id="old", keys=["cli/plans/p.md"])
        d = f"{self.ubu.home}/.cache/claude-sync/backup/old"
        past = time.time() - 15 * 86400
        os.utime(d, (past, past))
        self.assertEqual(self.call(self.ubu, "prune", days=14)["removed"], ["old"])
        self.assertFalse(os.path.exists(d))


class Runner(unittest.TestCase):
    def test_program_fits_one_argument(self):
        self.assertLess(len(remote.program().encode()), 90 * 1024)

    def test_info(self):
        info = json.loads(remote.Runner(None).call("info", {}))
        self.assertTrue({"hostname", "home", "desktop", "platform", "app_running", "cli_pids"} <= set(info))

    def test_program_runs_on_the_macs_ssh_python(self):
        # A non-interactive SSH login on the Mac gets /usr/bin/python3, which is 3.9.
        py = "/usr/bin/python3"
        if not os.path.exists(py):
            self.skipTest("no /usr/bin/python3")
        code = f"import base64;exec(base64.b64decode('{remote.program()}'))"
        with tempfile.TemporaryDirectory() as t:
            args = json.dumps({"home": t, "desktop": t + "/d", "folders": {}})
            p = subprocess.run([py, "-c", code, "inventory", args], input=b"", capture_output=True,
                               cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.assertEqual(p.returncode, 0, p.stderr.decode())

    def test_error_raises(self):
        with self.assertRaises(remote.RemoteError):
            remote.Runner(None).call("no_such_command", {})


if __name__ == "__main__":
    unittest.main()
