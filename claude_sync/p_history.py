"""The history part: cli/history.jsonl, unioned by (timestamp, display, sessionId).
See docs/contract.md, Phase 2 parts, history."""
import json
import time

NAME = "history"
KEY = "cli/history.jsonl"


def _parse(data: bytes) -> list:
    out = []
    for line in data.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _identity(entry):
    return entry.get("timestamp"), entry.get("display"), entry.get("sessionId")


def _map_project(entries, fn):
    out = []
    for e in entries:
        e = dict(e)
        if isinstance(e.get("project"), str):
            e["project"] = fn(e["project"])
        out.append(e)
    return out


def _union(*groups):
    by_id = {}
    for group in groups:
        for e in group:
            by_id.setdefault(_identity(e), e)
    return sorted(by_id.values(), key=lambda e: (e.get("timestamp") or 0))


def _dump(entries) -> bytes:
    lines = [json.dumps(e, ensure_ascii=False, separators=(",", ":")).encode() for e in entries]
    return b"\n".join(lines) + (b"\n" if lines else b"")


def plan(ctx):
    """Read history.jsonl on both machines, union the lines with each machine's
    project path, and report per side what needs writing. Reads only."""
    from claude_sync import cli, paths
    packed, m_of = {}, {}
    for s, (r, m) in ctx.side.items():
        packed[s] = cli.pack(r, m, [KEY]).get(KEY)  # None: missing, treated as empty
        m_of[s] = m
    parsed = {s: _parse(packed[s][0]) if packed[s] else [] for s in packed}
    neutral = {s: _map_project(parsed[s], lambda p, m=m_of[s]: paths.neutral(p, m)) for s in parsed}
    merged = _union(neutral["here"], neutral["there"])

    write = {}
    for s, m in m_of.items():
        localized = _map_project(merged, lambda p, m=m: paths.localize(p, m))
        if sorted(localized, key=_identity) != sorted(parsed[s], key=_identity):
            mtime = packed[s][1] if packed[s] else None
            size = len(packed[s][0]) if packed[s] else None
            write[s] = (_dump(localized), mtime, size)
    ctx.result["parts"][NAME] = "in sync" if not write else f"{len(merged)} entries, updates {', '.join(sorted(write))}"
    return write or None


def apply(ctx, plan):
    from claude_sync.model import FileInfo
    from claude_sync import cli
    for s, (data, mtime, size) in plan.items():
        r, m = ctx.side[s]
        inv = {KEY: FileInfo("", mtime, size)} if mtime is not None else {}
        cli.apply(r, m, ctx.run_id, m, {KEY: (data, int(time.time()), 0o600)}, inv, ctx.result)
