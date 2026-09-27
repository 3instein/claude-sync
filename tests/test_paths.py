"""Acceptance tests for claude_sync/paths.py. Written from the PRD before the code."""
import hashlib
import json
import unittest

from claude_sync import paths
from claude_sync.model import Machine

MAC = Machine("here", "/Users/r", "/Users/r/Library/Application Support/Claude", "mac")
UBU = Machine("ubuntu-pc", "/home/i", "/home/i/.config/Claude", "ubu")
SCRATCH = "/scratch-workspaces/org/acct/scratch-2026-09-27-978361"


def line(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class FolderName(unittest.TestCase):
    def test_short_path_is_slug(self):
        self.assertEqual(paths.folder_name("/Users/r/dev/my.app x"), "-Users-r-dev-my-app-x")

    def test_long_path_matches_claude_code(self):
        # Measured on 2026-09-27: Claude Code stored this session in the folder below.
        cwd = ("/private/tmp/claude-501/-Users-reynaldikindarto-Library-Application-Support-Claude-"
               "scratch-workspaces-9ad4c346-6bd0-4f29-a48d-ee6dfc0c4b76-a784793a-1b28-4522-a428-"
               "814b2b11f7e3-scratch-2026-09-27-978361/a80c9091-bf27-4d83-be1b-2b5e434e07a6/"
               "scratchpad/probe-a")
        name = paths.folder_name(cwd)
        self.assertTrue(name.endswith("-97836-1dlhuz"), name)
        self.assertEqual(len(name), 207)


class NeutralPaths(unittest.TestCase):
    def test_desktop_prefix_wins_over_home(self):
        self.assertEqual(paths.neutral(MAC.desktop + SCRATCH, MAC), "{desktop}" + SCRATCH)
        self.assertEqual(paths.localize("{desktop}" + SCRATCH, UBU), UBU.desktop + SCRATCH)

    def test_home_round_trip(self):
        n = paths.neutral("/Users/r/dev/x", MAC)
        self.assertEqual(n, "~/dev/x")
        self.assertEqual(paths.localize(n, UBU), "/home/i/dev/x")

    def test_prefix_only_at_boundary(self):
        self.assertEqual(paths.neutral("/Users/r2/dev", MAC), "/Users/r2/dev")
        self.assertEqual(paths.neutral("/Users/r", MAC), "~")
        self.assertEqual(paths.neutral("/opt/x", MAC), "/opt/x")


class Keys(unittest.TestCase):
    def test_project_key_moves_to_other_folder_name(self):
        rel = "projects/-Users-r-dev-x/abc.jsonl"
        key = paths.key_for("cli", rel, MAC, "/Users/r/dev/x")
        self.assertEqual(key, "cli/projects/{~/dev/x}/abc.jsonl")
        self.assertEqual(paths.key_to_path(key, UBU), "/home/i/.claude/projects/-home-i-dev-x/abc.jsonl")
        self.assertEqual(paths.key_to_path(key, MAC), "/Users/r/.claude/projects/-Users-r-dev-x/abc.jsonl")

    def test_memory_only_folder_uses_slug(self):
        key = paths.key_for("cli", "projects/-Users-r-dev-x/memory/a.md", MAC, None)
        self.assertEqual(key, "cli/projects/[~-dev-x]/memory/a.md")
        self.assertEqual(paths.key_to_path(key, UBU), "/home/i/.claude/projects/-home-i-dev-x/memory/a.md")

    def test_scratch_folder_slug_uses_desktop_token(self):
        # "{desktop}" is itself made of "{"/"}", escaped like any other token content.
        folder = paths.folder_name(MAC.desktop + SCRATCH)
        key = paths.key_for("cli", f"projects/{folder}/memory/a.md", MAC, None)
        self.assertTrue(key.startswith("cli/projects/[%7Bdesktop%7D-scratch-workspaces"), key)
        self.assertEqual(paths.key_to_path(key, UBU),
                         "/home/i/.claude/projects/" + paths.folder_name(UBU.desktop + SCRATCH) + "/memory/a.md")

    def test_long_folder_without_cwd_has_no_key(self):
        self.assertIsNone(paths.key_for("cli", "projects/" + "a" * 207 + "/memory/a.md", MAC, None))

    def test_desktop_and_plain_keys(self):
        self.assertEqual(paths.key_for("cli", "skills/x/SKILL.md", MAC, None), "cli/skills/x/SKILL.md")
        key = paths.key_for("desktop", "claude-code-sessions/o/a/local_1.json", MAC, None)
        self.assertEqual(key, "desktop/claude-code-sessions/o/a/local_1.json")
        self.assertEqual(paths.key_to_path(key, UBU), "/home/i/.config/Claude/claude-code-sessions/o/a/local_1.json")


class Kinds(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(paths.classify("cli/projects/{~/x}/a.jsonl"), "transcript")
        self.assertEqual(paths.classify("cli/projects/{~/x}/a/subagents/b.jsonl"), "transcript")
        self.assertEqual(paths.classify("desktop/claude-code-sessions/o/a/local_1.json"), "session")
        self.assertEqual(paths.classify("desktop/claude-code-sessions/o/a/archived-sessions.idx"), "json")
        self.assertEqual(paths.classify("cli/settings.json"), "json")
        self.assertEqual(paths.classify("cli/projects/{~/x}/memory/a.md"), "raw")

    def test_is_exec(self):
        self.assertTrue(paths.is_exec("cli/settings.json"))
        self.assertTrue(paths.is_exec("cli/skills/x/SKILL.md"))
        self.assertTrue(paths.is_exec("cli/commands/x.md"))
        self.assertFalse(paths.is_exec("cli/CLAUDE.md"))
        self.assertFalse(paths.is_exec("cli/projects/{~/x}/memory/a.md"))


def mac_transcript():
    return "\n".join([
        line({"type": "user", "cwd": "/Users/r/dev/x", "sessionId": "s1",
              "message": {"content": "Mac home is /Users/r and Ubuntu home is /home/i"}}),
        line({"type": "file-history-snapshot", "snapshot": {"trackedFileBackups": {
            "/Users/r/dev/x/a.py": {"backupFileName": "h@v1", "realParentDir": "/Users/r/dev/x"},
            "rel.py": {"backupFileName": None, "realParentDir": "/Users/r/dev/x"}}}}),
        line({"type": "system", "serverClassifierContext": {"context": {
            "live_cwd": "/Users/r/dev/x", "git_state": {"cwd": "/Users/r/dev/x"}}}}),
        line({"type": "attachment", "attachment": {"snapshot": {"workingDirectory": "/Users/r/dev/x"}},
              "trackingPath": "/Users/r/dev/x/b.py", "backup": {"realParentDir": "/Users/r/dev/x"}}),
        '{"type":"mode",  "mode":"auto"}',
    ]).encode() + b"\n"


class PathFields(unittest.TestCase):
    KEY = "cli/projects/{~/dev/x}/s1.jsonl"

    def test_transcript_maps_only_path_fields(self):
        out = paths.localize_bytes(self.KEY, mac_transcript(), MAC, UBU).decode().splitlines()
        first = json.loads(out[0])
        self.assertEqual(first["cwd"], "/home/i/dev/x")
        self.assertEqual(first["message"]["content"], "Mac home is /Users/r and Ubuntu home is /home/i")
        backups = json.loads(out[1])["snapshot"]["trackedFileBackups"]
        self.assertEqual(set(backups), {"/home/i/dev/x/a.py", "rel.py"})
        self.assertEqual(backups["rel.py"]["realParentDir"], "/home/i/dev/x")
        ctx = json.loads(out[2])["serverClassifierContext"]["context"]
        self.assertEqual((ctx["live_cwd"], ctx["git_state"]["cwd"]), ("/home/i/dev/x", "/home/i/dev/x"))
        att = json.loads(out[3])
        self.assertEqual(att["attachment"]["snapshot"]["workingDirectory"], "/home/i/dev/x")
        self.assertEqual(att["trackingPath"], "/home/i/dev/x/b.py")
        self.assertEqual(att["backup"]["realParentDir"], "/home/i/dev/x")
        self.assertEqual(out[4], '{"type":"mode",  "mode":"auto"}', "a line without path fields keeps its bytes")

    def test_same_hash_on_both_machines(self):
        mac = mac_transcript()
        ubu = paths.localize_bytes(self.KEY, mac, MAC, UBU)
        self.assertEqual(paths.normalize_bytes(self.KEY, mac, MAC), paths.normalize_bytes(self.KEY, ubu, UBU))

    def test_round_trip_is_stable(self):
        mac = mac_transcript()
        back = paths.localize_bytes(self.KEY, paths.localize_bytes(self.KEY, mac, MAC, UBU), UBU, MAC)
        self.assertEqual(paths.normalize_bytes(self.KEY, back, MAC), paths.normalize_bytes(self.KEY, mac, MAC))

    def test_memory_that_names_both_machines_does_not_change(self):
        text = b"Mac home is `/Users/r`. Ubuntu home is `/home/i`.\n"
        key = "cli/projects/{~/x}/memory/ubuntu.md"
        self.assertEqual(paths.localize_bytes(key, text, MAC, UBU), text)
        self.assertEqual(paths.normalize_bytes(key, text, MAC), text)

    def test_session_fields(self):
        key = "desktop/claude-code-sessions/o/a/local_1.json"
        data = json.dumps({"cwd": MAC.desktop + SCRATCH, "originCwd": "/Users/r/dev/x", "title": "/Users/r",
                           "scratchPromptRecents": ["/Users/r/dev/a", "/opt/b"]}).encode()
        out = json.loads(paths.localize_bytes(key, data, MAC, UBU))
        self.assertEqual(out["cwd"], UBU.desktop + SCRATCH)
        self.assertEqual(out["originCwd"], "/home/i/dev/x")
        self.assertEqual(out["scratchPromptRecents"], ["/home/i/dev/a", "/opt/b"])
        self.assertEqual(out["title"], "/Users/r", "title is free text")

    def test_session_hash_ignores_key_order(self):
        key = "desktop/claude-code-sessions/o/a/local_1.json"
        a = json.dumps({"cwd": "/Users/r/dev/x", "title": "t"}).encode()
        b = json.dumps({"title": "t", "cwd": "/home/i/dev/x"}).encode()
        self.assertEqual(paths.normalize_bytes(key, a, MAC), paths.normalize_bytes(key, b, UBU))

    def test_settings_strings(self):
        data = json.dumps({"hooks": {"Stop": [{"hooks": [{"command": "bash /Users/r/.claude/x.sh"}]}]},
                           "permissions": {"allow": ["Read(/Users/r/dev/**)", "Bash(ls:*)"]}}).encode()
        out = json.loads(paths.localize_bytes("cli/settings.json", data, MAC, UBU))
        self.assertEqual(out["hooks"]["Stop"][0]["hooks"][0]["command"], "bash /home/i/.claude/x.sh")
        self.assertEqual(out["permissions"]["allow"], ["Read(/home/i/dev/**)", "Bash(ls:*)"])

    def test_transcript_cwd(self):
        self.assertEqual(paths.transcript_cwd(mac_transcript()), "/Users/r/dev/x")
        self.assertIsNone(paths.transcript_cwd(b'{"type":"mode"}\n'))

    def test_hash_is_sha256_friendly(self):
        n = paths.normalize_bytes(self.KEY, mac_transcript(), MAC)
        self.assertEqual(len(hashlib.sha256(n).hexdigest()), 64)


class TokenEscaping(unittest.TestCase):
    def test_desktop_folder_cwd_round_trips(self):
        # A Code tab scratch session: cwd is under the desktop folder, whose neutral
        # form is itself "{desktop}", so the token content has its own "{"/"}".
        cwd = MAC.desktop + "/x"
        key = paths.key_for("cli", "projects/ignored/abc.jsonl", MAC, cwd)
        self.assertEqual(key, "cli/projects/{%7Bdesktop%7D/x}/abc.jsonl")
        self.assertEqual(paths.key_to_path(key, UBU),
                          "/home/i/.claude/projects/" + paths.folder_name(UBU.desktop + "/x") + "/abc.jsonl")
        self.assertEqual(paths.key_to_path(key, MAC),
                          "/Users/r/.claude/projects/" + paths.folder_name(cwd) + "/abc.jsonl")

    def test_cwd_containing_closing_brace(self):
        cwd = "/Users/r/dev/weird}x"
        key = paths.key_for("cli", "projects/ignored/abc.jsonl", MAC, cwd)
        self.assertEqual(key, "cli/projects/{~/dev/weird%7Dx}/abc.jsonl")
        self.assertEqual(paths.key_to_path(key, MAC),
                          "/Users/r/.claude/projects/" + paths.folder_name(cwd) + "/abc.jsonl")


class SafeKeys(unittest.TestCase):
    def test_safe(self):
        self.assertTrue(paths.safe_key("cli/settings.json"))
        self.assertTrue(paths.safe_key("cli/projects/{~/dev/x}/abc.jsonl"))

    def test_unsafe(self):
        self.assertFalse(paths.safe_key("cli/../../x"))
        self.assertFalse(paths.safe_key("cli/projects/[..]/../../x"))
        self.assertFalse(paths.safe_key("desktop//x"))
        self.assertFalse(paths.safe_key(""))
        self.assertFalse(paths.safe_key("/cli/x"))
        self.assertFalse(paths.safe_key("opt/x"))
        self.assertFalse(paths.safe_key("cli/x\0y"))

    def test_key_to_path_rejects_unsafe_key(self):
        with self.assertRaises(ValueError):
            paths.key_to_path("cli/../../x", MAC)


class LooseHash(unittest.TestCase):
    PREFIXES = [[MAC.home, MAC.desktop], [UBU.home, UBU.desktop]]

    def test_free_text_home_mentions_hash_the_same(self):
        # The migration rewrote the home path in free text too, not only in path
        # fields, so a Mac copy naming "/Users/r" and an Ubuntu copy of the same
        # line naming "/home/i" must hash the same once both are known prefixes.
        key = "cli/projects/{~/dev/x}/s1.jsonl"
        mac_line = line({"type": "user", "cwd": "/Users/r/dev/x",
                          "message": {"content": "home is /Users/r"}}).encode() + b"\n"
        ubu_line = line({"type": "user", "cwd": "/home/i/dev/x",
                          "message": {"content": "home is /home/i"}}).encode() + b"\n"
        self.assertEqual(paths.normalize_bytes(key, mac_line, MAC, self.PREFIXES),
                          paths.normalize_bytes(key, ubu_line, UBU, self.PREFIXES))

    def test_default_prefixes_leaves_hash_unchanged(self):
        key = "cli/projects/{~/x}/memory/ubuntu.md"
        text = b"Mac home is `/Users/r`. Ubuntu home is `/home/i`.\n"
        self.assertEqual(paths.normalize_bytes(key, text, MAC), text)

    def test_localize_bytes_does_not_apply_loose_matching(self):
        # Fix round 1 item 1: written content never changes because of this rule.
        key = "cli/projects/{~/x}/memory/ubuntu.md"
        text = b"Mac home is `/Users/r`. Ubuntu home is `/home/i`.\n"
        self.assertEqual(paths.localize_bytes(key, text, MAC, UBU), text)



class LooseHashInJsonText(unittest.TestCase):
    def test_paths_after_escaped_newline_and_file_url(self):
        # Measured on real data: the migration rewrote paths inside JSON-escaped tool output.
        key = "cli/projects/{~/x}/s.jsonl"
        pre = [[MAC.home, MAC.desktop], [UBU.home, UBU.desktop]]
        mac = b'{"out":"key\\n/Users/r/ac and file:///Users/r/dev/a"}\n'
        ubu = b'{"out":"key\\n/home/i/ac and file:///home/i/dev/a"}\n'
        self.assertEqual(paths.normalize_bytes(key, mac, MAC, pre), paths.normalize_bytes(key, ubu, UBU, pre))


if __name__ == "__main__":
    unittest.main()
