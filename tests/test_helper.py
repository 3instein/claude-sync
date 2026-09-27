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
        # rule 4: pack and delete read keys (and delete's expect) from stdin, not the args.
        if command in ("pack", "delete") and "keys" in args:
            payload = {"keys": args.pop("keys")}
            if "expect" in args:
                payload["expect"] = args.pop("expect")
            stdin = json.dumps(payload).encode()
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
        self.put(self.mac, "skills/synced/acct/x/SKILL.md", b"managed by the app")
        os.symlink("/etc/hosts", f"{self.mac.home}/.claude/skills/link")
        files = self.call(self.mac, "inventory", folders={})["files"]
        key = "cli/projects/{~/dev/x}/s1.jsonl"
        self.assertIn(key, files)
        h, mtime, size = files[key]
        self.assertEqual((mtime, size), (MTIME, data.index(b"\n") + 1), "the partial last line is not counted")
        self.assertIn("cli/projects/[-x-memory-only]/memory/a.md", files)
        self.assertFalse(any(k.startswith(("cli/telemetry", "cli/skills/synced")) or "DS_Store" in k or k.endswith("link") for k in files))

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
        self.call(self.ubu, "undo", run_id="r2")  # rule 9: undo now requires an explicit run_id
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


class LoosePrefixHash(Base):
    # rule 1: normalize_bytes(..., prefixes=...) is landing in paths.py in parallel; helper.py
    # only needs to pass it through, so this test goes red again if paths.py loses that param.
    def test_prefixes_neutralize_free_text_for_hashing(self):
        prefixes = [[self.mac.home, self.mac.desktop], [self.ubu.home, self.ubu.desktop]]
        self.put(self.mac, "plans/note.md", f"see {self.mac.home}/dev/x".encode())
        self.put(self.ubu, "plans/note.md", f"see {self.ubu.home}/dev/x".encode())
        a = self.call(self.mac, "inventory", folders={}, prefixes=prefixes)["files"]["cli/plans/note.md"][0]
        b = self.call(self.ubu, "inventory", folders={}, prefixes=prefixes)["files"]["cli/plans/note.md"][0]
        self.assertEqual(a, b)


class SafeKeys(Base):
    def test_pack_refuses_traversal_key(self):
        with self.assertRaises(remote.RemoteError):
            self.call(self.mac, "pack", keys=["cli/../../x"])

    def test_apply_rejects_traversal_members_and_writes_nothing_outside_home(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w|gz") as tar:
            for name in ("cli/../../x", "cli/projects/[..]/../../x", "conflicts/../../../x"):
                info = tarfile.TarInfo(name)
                info.size, info.mtime = 1, MTIME
                tar.addfile(info, io.BytesIO(b"x"))
        out = self.call(self.ubu, "apply", stdin=buf.getvalue(), run_id="rt",
                         src={"home": self.mac.home, "desktop": self.mac.desktop})
        self.assertEqual(out["written"], {})
        self.assertEqual(len(out["errors"]), 3)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "x")))

    def test_delete_refuses_traversal_key(self):
        out = self.call(self.ubu, "delete", run_id="rt2", keys=["cli/../../x"])
        self.assertEqual(out["deleted"], [])
        self.assertEqual(len(out["errors"]), 1)


class KeysOnStdin(Base):
    def test_pack_reads_keys_from_stdin_not_args(self):
        self.put(self.mac, "plans/p.md", b"x")
        stdin = json.dumps({"keys": ["cli/plans/p.md"]}).encode()
        tar = self.run.call("pack", {"home": self.mac.home, "desktop": self.mac.desktop}, stdin)
        with tarfile.open(fileobj=io.BytesIO(tar), mode="r:gz") as t:
            self.assertEqual(t.getnames(), ["cli/plans/p.md"])

    def test_delete_reads_keys_from_stdin(self):
        p = self.put(self.ubu, "plans/p.md", b"x")
        stdin = json.dumps({"keys": ["cli/plans/p.md"]}).encode()
        out = json.loads(self.run.call(
            "delete", {"home": self.ubu.home, "desktop": self.ubu.desktop, "run_id": "rd"}, stdin))
        self.assertEqual(out["deleted"], ["cli/plans/p.md"])
        self.assertFalse(os.path.exists(p))


class ExpectPrecondition(Base):
    def _expect_tar(self, expect, key, data):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w|gz") as tar:
            head = json.dumps(expect).encode()
            info = tarfile.TarInfo(".claude-sync-expect.json")
            info.size = len(head)
            tar.addfile(info, io.BytesIO(head))
            k = tarfile.TarInfo(key)
            k.size, k.mtime = len(data), MTIME
            tar.addfile(k, io.BytesIO(data))
        return buf.getvalue()

    def test_apply_skips_when_target_stat_does_not_match(self):
        target = self.put(self.ubu, "plans/p.md", b"old")
        tar = self._expect_tar({"cli/plans/p.md": [MTIME + 999, 999]}, "cli/plans/p.md", b"new")
        out = self.call(self.ubu, "apply", stdin=tar, run_id="re1",
                         src={"home": self.mac.home, "desktop": self.mac.desktop})
        self.assertEqual(out["skipped"], ["cli/plans/p.md"])
        self.assertEqual(read(target), b"old")

    def test_apply_skips_when_expect_null_but_file_exists(self):
        target = self.put(self.ubu, "plans/q.md", b"exists")
        tar = self._expect_tar({"cli/plans/q.md": None}, "cli/plans/q.md", b"incoming")
        out = self.call(self.ubu, "apply", stdin=tar, run_id="re2",
                         src={"home": self.mac.home, "desktop": self.mac.desktop})
        self.assertEqual(out["skipped"], ["cli/plans/q.md"])
        self.assertEqual(read(target), b"exists")

    def test_delete_skips_via_expect(self):
        target = self.put(self.ubu, "plans/r.md", b"current")
        stdin = json.dumps({"keys": ["cli/plans/r.md"], "expect": {"cli/plans/r.md": [1, 1]}}).encode()
        out = json.loads(self.run.call(
            "delete", {"home": self.ubu.home, "desktop": self.ubu.desktop, "run_id": "re3"}, stdin))
        self.assertEqual(out["deleted"], [])
        self.assertEqual(out["skipped"], ["cli/plans/r.md"])
        self.assertTrue(os.path.exists(target))


class Modes(Base):
    def test_exec_bit_round_trips_as_700(self):
        p = self.put(self.mac, "commands/run.sh", b"#!/bin/sh\n")
        os.chmod(p, 0o755)
        tar = self.call(self.mac, "pack", keys=["cli/commands/run.sh"])
        self.call(self.ubu, "apply", stdin=tar, run_id="rm1",
                  src={"home": self.mac.home, "desktop": self.mac.desktop})
        target = f"{self.ubu.home}/.claude/commands/run.sh"
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o700)

    def test_non_exec_file_is_still_600(self):
        self.put(self.mac, "commands/note.md", b"text")
        tar = self.call(self.mac, "pack", keys=["cli/commands/note.md"])
        self.call(self.ubu, "apply", stdin=tar, run_id="rm2",
                  src={"home": self.mac.home, "desktop": self.mac.desktop})
        target = f"{self.ubu.home}/.claude/commands/note.md"
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)


class InventoryWarnings(Base):
    def test_symlinked_item_warns(self):
        os.symlink("/tmp", f"{self.mac.home}/.claude/skills")
        out = self.call(self.mac, "inventory", folders={})
        self.assertTrue(any("skills" in w for w in out["warnings"]))

    def test_unreadable_folder_warns(self):
        p = f"{self.mac.home}/.claude/agents"
        os.makedirs(p)
        os.chmod(p, 0o000)
        try:
            out = self.call(self.mac, "inventory", folders={})
        finally:
            os.chmod(p, 0o700)
        self.assertTrue(any("agents" in w for w in out["warnings"]))

    def test_missing_item_is_not_a_warning(self):
        out = self.call(self.mac, "inventory", folders={})
        self.assertEqual(out["warnings"], [])


class AtomicLock(Base):
    def test_lock_write_refuses_when_a_lock_exists(self):
        lock1 = {"host": "mac", "pid": 1, "start": "x", "run_id": "a"}
        out1 = self.call(self.mac, "lock_write", lock=lock1)
        self.assertEqual(out1, {"ok": True, "lock": lock1})
        out2 = self.call(self.mac, "lock_write", lock={"host": "ubu", "pid": 2, "start": "y", "run_id": "b"})
        self.assertEqual(out2, {"ok": False, "lock": lock1})


class UndoRequiresRunId(Base):
    def test_undo_without_run_id_errors(self):
        with self.assertRaises(remote.RemoteError):
            self.run.call("undo", {"home": self.mac.home, "desktop": self.mac.desktop})

    def test_undo_skips_a_file_changed_since_the_run(self):
        old = self.put(self.ubu, "plans/p.md", b"old")
        self.put(self.mac, "plans/p.md", b"new")
        tar = self.call(self.mac, "pack", keys=["cli/plans/p.md"])
        self.call(self.ubu, "apply", stdin=tar, run_id="ru1",
                  src={"home": self.mac.home, "desktop": self.mac.desktop})
        with open(old, "wb") as f:  # someone edits the file after the run, before undo
            f.write(b"edited-after-run")
        out = self.call(self.ubu, "undo", run_id="ru1")
        self.assertEqual(out["skipped"], ["cli/plans/p.md"])
        self.assertEqual(read(old), b"edited-after-run")

    def test_undo_backs_up_the_current_file_before_removing_it(self):
        self.put(self.mac, "plans/new.md", b"created")
        tar = self.call(self.mac, "pack", keys=["cli/plans/new.md"])
        self.call(self.ubu, "apply", stdin=tar, run_id="ru2",
                  src={"home": self.mac.home, "desktop": self.mac.desktop})
        target = f"{self.ubu.home}/.claude/plans/new.md"
        self.call(self.ubu, "undo", run_id="ru2")
        self.assertFalse(os.path.exists(target))
        safety = f"{self.ubu.home}/.cache/claude-sync/backup/undo-ru2/cli/plans/new.md"
        self.assertEqual(read(safety), b"created")


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
