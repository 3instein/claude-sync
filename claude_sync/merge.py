"""Three-way merge plan and content-level merges. Pure functions. See docs/contract.md."""
import hashlib
import json
from dataclasses import dataclass
from fnmatch import fnmatchcase

from claude_sync import paths
from claude_sync.model import Action, Plan

OTHER = {"here": "there", "there": "here"}
MERGE_OP = {"transcript": "transcript", "session": "json_merge", "json": "json_merge", "raw": "conflict"}
STOP_ORDER = ("deletions", "first_run", "exec_config")
MISSING = object()


@dataclass
class Opts:
    here_host: str
    there_host: str
    now: int
    first_run_cutoff: dict
    cleanup_days: int = 30
    deletion_limit: int = 50
    live_window: int = 60
    confirm: str | None = None
    keep: tuple = ()


def plan(state_files: dict, here: dict, there: dict, opts: Opts) -> Plan:
    first = not state_files
    actions = []
    for key in sorted(state_files.keys() | here.keys() | there.keys()):
        rule = _rule(key, state_files.get(key), here.get(key), there.get(key), first, opts)
        if rule:
            actions.append(Action(rule[0], key, rule[1]))

    # In the first run every delete comes from rule 5, so it is reviewed as first_run.
    deletes = [a for a in actions if a.op == "delete"]
    counted = [] if first else deletes
    lists = {
        "deletions": counted if len(counted) > opts.deletion_limit else [],
        "first_run": deletes if first else [],
        "exec_config": [a for a in actions if paths.is_exec(a.key)],
    }
    review = {name: sorted([a.to, a.key] for a in lst) for name, lst in lists.items() if lst}
    token = review_token(review)
    stops = [r for r in STOP_ORDER if r in review and opts.confirm != token]

    live = [a for a in actions if _writes_live(a, here, opts)]
    return Plan(actions=[a for a in actions if a not in live], stops=stops, review=review,
                token=token, skipped_live=[a.key for a in live])


def _rule(key, s, h, t, first, opts):
    """(op, to) for one key, or None."""
    if h is not None and t is not None:
        if h.hash == t.hash:
            return None
        if s is not None:
            return _changed_side(key, h.hash != s["hash"], t.hash != s["hash"])
        if first:
            if paths.classify(key) == "transcript":
                return "transcript", "there"
            return _changed_side(key, not _unchanged(h, opts.here_host, opts),
                                 not _unchanged(t, opts.there_host, opts))
        # Not first run, no state entry, different content: new on both sides. Merge, lose nothing.
        return MERGE_OP[paths.classify(key)], "there"
    if h is None and t is None:
        return None
    side, info = ("here", h) if h is not None else ("there", t)
    host = opts.here_host if side == "here" else opts.there_host
    if s is not None and (info.hash != s["hash"] or _kept(key, opts.keep)):
        return "copy", OTHER[side]
    if s is not None:
        return "delete", side
    if first and _unchanged(info, host, opts) and not _kept(key, opts.keep):
        return "delete", side
    return "copy", OTHER[side]


def _changed_side(key, here_changed, there_changed):
    if here_changed and not there_changed:
        return "copy", "there"
    if there_changed and not here_changed:
        return "copy", "here"
    # Both changed, or (first run) both unchanged since the migration: merge by kind.
    return MERGE_OP[paths.classify(key)], "there"


def _unchanged(info, host, opts):
    """Unchanged since the migration. A host without a cutoff counts as changed: that never deletes."""
    cutoff = opts.first_run_cutoff.get(host)
    return cutoff is not None and info.mtime <= cutoff


def _kept(key, keep):
    for pattern in keep:
        p = pattern.rstrip("/")
        if key == p or key.startswith(p + "/") or fnmatchcase(key, pattern):
            return True
    return False


def _writes_live(a, here, opts):
    # A transcript merge can write here too (the executor picks the direction), so it is skipped as well.
    info = here.get(a.key)
    return (info is not None and paths.classify(a.key) == "transcript"
            and (a.to == "here" or a.op == "transcript")
            and info.mtime > opts.now - opts.live_window)


def review_token(review: dict) -> str:
    canon = json.dumps(review, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:12]


def _complete_lines(data: bytes) -> list:
    return data.split(b"\n")[:-1]


def resolve_transcript(here: bytes, there: bytes) -> str:
    """here | there | split"""
    h, t = _complete_lines(here), _complete_lines(there)
    if h[:len(t)] == t:
        return "here"
    if t[:len(h)] == h:
        return "there"
    return "split"


def split_transcript(data: bytes, old_id: str, new_id: str) -> bytes:
    return b"\n".join(_resplit_line(line, old_id, new_id) for line in data.split(b"\n"))


def _resplit_line(line, old_id, new_id):
    try:
        obj = json.loads(line)
    except (ValueError, RecursionError):
        return line
    if not isinstance(obj, dict) or obj.get("sessionId") != old_id:
        return line
    obj["sessionId"] = new_id
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()


def merge_json(base, here: dict, there: dict, here_is_newer: bool) -> tuple:
    """(merged, overridden key names)"""
    base = base or {}
    merged, overridden = {}, []
    for key in {**base, **here, **there}:
        b, h, t = (d.get(key, MISSING) for d in (base, here, there))
        if not _differs(h, b):
            value = t
        elif not _differs(t, b) or not _differs(h, t):
            value = h
        else:
            overridden.append(key)
            # A change wins over a deletion, as for whole files (rule 3).
            if MISSING in (h, t):
                value = t if h is MISSING else h
            else:
                value = h if here_is_newer else t
        if value is not MISSING:
            merged[key] = value
    return merged, sorted(overridden)


def _differs(a, b):
    """Exact JSON comparison: in Python 1 == True == 1.0, in JSON they differ."""
    if a is MISSING or b is MISSING:
        return a is not b
    return json.dumps(a, sort_keys=True) != json.dumps(b, sort_keys=True)
