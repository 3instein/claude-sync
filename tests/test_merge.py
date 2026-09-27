"""Acceptance tests for claude_sync/merge.py. Written from the PRD before the code."""
import json
import unittest

from claude_sync import merge
from claude_sync.model import Action, FileInfo

NOW = 1_790_500_000
CUTOFF = {"mac": 1_790_170_950, "ubu": 1_790_170_951}  # the migration copy, 2026-09-23 22:22:30 WIB
T = "cli/projects/{~/x}/s1.jsonl"
RAW = "cli/projects/{~/x}/memory/a.md"
SESSION = "desktop/claude-code-sessions/o/a/local_1.json"
SKILL = "cli/skills/x/SKILL.md"


def fi(h, mtime=NOW - 3600, size=10):
    return FileInfo(h, mtime, size)


def st(h):
    return {"hash": h, "stat": {}}


def opts(**kw):
    return merge.Opts(here_host="mac", there_host="ubu", now=NOW, first_run_cutoff=CUTOFF, **kw)


def acts(plan):
    return {(a.op, a.key, a.to) for a in plan.actions}


class TwoWay(unittest.TestCase):
    def test_same_hash_does_nothing(self):
        p = merge.plan({RAW: st("a")}, {RAW: fi("a")}, {RAW: fi("a")}, opts())
        self.assertEqual(p.actions, [])
        self.assertEqual(p.stops, [])

    def test_one_side_changed_copies(self):
        p = merge.plan({RAW: st("a")}, {RAW: fi("b")}, {RAW: fi("a")}, opts())
        self.assertEqual(acts(p), {("copy", RAW, "there")})
        p = merge.plan({RAW: st("a")}, {RAW: fi("a")}, {RAW: fi("c")}, opts())
        self.assertEqual(acts(p), {("copy", RAW, "here")})

    def test_both_changed_by_kind(self):
        state = {T: st("a"), RAW: st("a"), SESSION: st("a")}
        here = {T: fi("b"), RAW: fi("b"), SESSION: fi("b")}
        there = {T: fi("c"), RAW: fi("c"), SESSION: fi("c")}
        p = merge.plan(state, here, there, opts())
        self.assertEqual(acts(p), {("transcript", T, "there"), ("conflict", RAW, "there"),
                                   ("json_merge", SESSION, "there")})

    def test_both_changed_to_same_content_is_in_sync(self):
        p = merge.plan({RAW: st("a")}, {RAW: fi("b")}, {RAW: fi("b")}, opts())
        self.assertEqual(p.actions, [])

    def test_new_file_copies(self):
        p = merge.plan({RAW: st("a")}, {RAW: fi("a"), "cli/plans/p.md": fi("n")}, {RAW: fi("a")}, opts())
        self.assertEqual(acts(p), {("copy", "cli/plans/p.md", "there")})


class Deletions(unittest.TestCase):
    def test_deleted_on_one_side_is_deleted_on_the_other(self):
        p = merge.plan({RAW: st("a")}, {RAW: fi("a")}, {}, opts())
        self.assertEqual(acts(p), {("delete", RAW, "here")})

    def test_change_wins_over_deletion(self):
        p = merge.plan({RAW: st("a")}, {RAW: fi("b")}, {}, opts())
        self.assertEqual(acts(p), {("copy", RAW, "there")})

    def test_limit_stops_and_token_confirms(self):
        keys = [f"cli/plans/p{i}.md" for i in range(51)]
        state = {k: st("a") for k in keys}
        here = {k: fi("a") for k in keys}
        p = merge.plan(state, here, {}, opts())
        self.assertEqual(p.stops, ["deletions"])
        self.assertEqual(len(p.review["deletions"]), 51)
        self.assertEqual(p.review["deletions"][0], ["here", "cli/plans/p0.md"])
        ok = merge.plan(state, here, {}, opts(confirm=p.token))
        self.assertEqual(ok.stops, [])
        self.assertEqual(len([a for a in ok.actions if a.op == "delete"]), 51)

    def test_fifty_deletions_do_not_stop(self):
        keys = [f"cli/plans/p{i}.md" for i in range(50)]
        p = merge.plan({k: st("a") for k in keys}, {k: fi("a") for k in keys}, {}, opts())
        self.assertEqual(p.stops, [])

    def test_old_transcripts_count(self):
        old = NOW - 31 * 86400
        keys = [f"cli/projects/{{~/x}}/s{i}.jsonl" for i in range(60)]
        p = merge.plan({k: st("a") for k in keys}, {k: fi("a", mtime=old) for k in keys}, {}, opts())
        self.assertEqual(p.stops, ["deletions"])
        self.assertEqual(len(p.review["deletions"]), 60)

    def test_keep_copies_back_a_deletion(self):
        k = "cli/plans/keepme.md"
        p = merge.plan({k: st("a")}, {}, {k: fi("a")}, opts(keep=(k,)))
        self.assertEqual(acts(p), {("copy", k, "here")})

    def test_old_memory_files_still_count(self):
        old = NOW - 31 * 86400
        keys = [f"cli/projects/{{~/x}}/memory/m{i}.md" for i in range(60)]
        p = merge.plan({k: st("a") for k in keys}, {k: fi("a", mtime=old) for k in keys}, {}, opts())
        self.assertEqual(p.stops, ["deletions"])


class FirstRun(unittest.TestCase):
    OLD = CUTOFF["ubu"] - 100
    NEW = CUTOFF["ubu"] + 100

    def test_unchanged_since_migration_on_one_side_only_is_reviewed(self):
        p = merge.plan({}, {}, {RAW: fi("a", mtime=self.OLD)}, opts())
        self.assertEqual(p.stops, ["first_run"])
        self.assertEqual(p.review["first_run"], [["there", RAW]])
        self.assertIn(("delete", RAW, "there"), acts(p))

    def test_keep_copies_it_back(self):
        p = merge.plan({}, {}, {RAW: fi("a", mtime=self.OLD)}, opts(keep=("cli/projects/{~/x}/memory",)))
        self.assertIn(("copy", RAW, "here"), acts(p))
        self.assertNotIn(("delete", RAW, "there"), acts(p))

    def test_confirm_deletes(self):
        p = merge.plan({}, {}, {RAW: fi("a", mtime=self.OLD)}, opts())
        ok = merge.plan({}, {}, {RAW: fi("a", mtime=self.OLD)}, opts(confirm=p.token))
        self.assertEqual(ok.stops, [])
        self.assertIn(("delete", RAW, "there"), acts(ok))

    def test_new_on_one_side_copies(self):
        p = merge.plan({}, {}, {RAW: fi("a", mtime=self.NEW)}, opts())
        self.assertEqual((p.stops, acts(p)), ([], {("copy", RAW, "here")}))

    def test_one_copy_unchanged_since_migration_loses(self):
        p = merge.plan({}, {RAW: fi("a", mtime=CUTOFF["mac"] - 5)}, {RAW: fi("b", mtime=self.NEW)}, opts())
        self.assertEqual(acts(p), {("copy", RAW, "here")})

    def test_both_changed_is_conflict(self):
        p = merge.plan({}, {RAW: fi("a", mtime=self.NEW)}, {RAW: fi("b", mtime=self.NEW)}, opts())
        self.assertEqual(acts(p), {("conflict", RAW, "there")})

    def test_transcript_on_both_sides_uses_content(self):
        p = merge.plan({}, {T: fi("a", mtime=CUTOFF["mac"] - 5)}, {T: fi("b", mtime=self.NEW)}, opts())
        self.assertEqual(acts(p), {("transcript", T, "there")})


class ExecConfigAndLive(unittest.TestCase):
    def test_exec_config_change_stops(self):
        p = merge.plan({SKILL: st("a")}, {SKILL: fi("b")}, {SKILL: fi("a")}, opts())
        self.assertEqual(p.stops, ["exec_config"])
        self.assertEqual(p.review["exec_config"], [["there", SKILL]])
        self.assertEqual(merge.plan({SKILL: st("a")}, {SKILL: fi("b")}, {SKILL: fi("a")},
                                    opts(confirm=p.token)).stops, [])

    def test_token_changes_with_the_list(self):
        a = merge.review_token({"deletions": [["here", "x"]]})
        b = merge.review_token({"deletions": [["here", "y"]]})
        self.assertNotEqual(a, b)
        self.assertEqual(len(a), 12)

    def test_live_transcript_here_is_not_written(self):
        p = merge.plan({T: st("a")}, {T: fi("a", mtime=NOW - 10)}, {T: fi("b")}, opts())
        self.assertEqual(p.actions, [])
        self.assertEqual(p.skipped_live, [T])

    def test_live_rule_is_only_for_here(self):
        p = merge.plan({T: st("a")}, {T: fi("b")}, {T: fi("a", mtime=NOW - 10)}, opts())
        self.assertEqual(acts(p), {("copy", T, "there")})


def lines(*objs):
    return b"".join(json.dumps(o, separators=(",", ":")).encode() + b"\n" for o in objs)


class Content(unittest.TestCase):
    A = {"uuid": "1", "sessionId": "s1"}
    B = {"uuid": "2", "sessionId": "s1"}
    C = {"uuid": "3", "sessionId": "s1"}

    def test_longer_copy_wins(self):
        self.assertEqual(merge.resolve_transcript(lines(self.A), lines(self.A, self.B)), "there")
        self.assertEqual(merge.resolve_transcript(lines(self.A, self.B), lines(self.A)), "here")
        self.assertEqual(merge.resolve_transcript(lines(self.A), lines(self.A)), "here")

    def test_divergent_copies_split(self):
        self.assertEqual(merge.resolve_transcript(lines(self.A, self.B), lines(self.A, self.C)), "split")

    def test_split_rewrites_session_id_only(self):
        data = lines(self.A, {"uuid": "9", "sessionId": "other"}) + b'{"type":"mode"}\n'
        out = merge.split_transcript(data, "s1", "s2").splitlines()
        self.assertEqual(json.loads(out[0])["sessionId"], "s2")
        self.assertEqual(json.loads(out[1])["sessionId"], "other")
        self.assertEqual(out[2], b'{"type":"mode"}')

    def test_merge_json(self):
        base = {"a": 1, "b": 1, "c": 1, "d": 1}
        here = {"a": 2, "b": 1, "c": 5, "d": 1, "e": 1}
        there = {"a": 1, "b": 3, "c": 6}
        merged, over = merge.merge_json(base, here, there, here_is_newer=False)
        self.assertEqual(merged, {"a": 2, "b": 3, "c": 6, "e": 1})
        self.assertEqual(over, ["c"])

    def test_merge_json_first_run(self):
        merged, over = merge.merge_json(None, {"a": 1}, {"b": 2}, here_is_newer=True)
        self.assertEqual((merged, over), ({"a": 1, "b": 2}, []))


if __name__ == "__main__":
    unittest.main()
