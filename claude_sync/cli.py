"""Command line: status, sync, dry run, undo, unlock. Joins helper, merge and paths.
See docs/contract.md and the PRD."""
import argparse
import datetime
import hashlib
import io
import json
import os
import secrets
import subprocess
import sys
import tarfile
import time
import uuid

from claude_sync import groups, merge, paths
from claude_sync.model import Action, FileInfo, Machine
from claude_sync.remote import RemoteError, Runner

UTC = datetime.timezone.utc
# ponytail: the one migration this tool knows, 2026-09-23 22:22:30/31 WIB; a new pair of machines needs its own values.
FIRST_RUN_CUTOFF = {
    "Darwin": int(datetime.datetime(2026, 9, 23, 15, 22, 30, tzinfo=UTC).timestamp()),
    "Linux": int(datetime.datetime(2026, 9, 23, 15, 22, 31, tzinfo=UTC).timestamp()),
}
NEUTRAL_M = Machine("neutral", "~", "{desktop}")  # src for content that is already in neutral form
JSON_KINDS = ("session", "json")
LIST_CAP = 20
BATCH = 500        # keys per helper call: one SSH argument is limited to 128 KB, and stdin keeps memory small
SPLIT_LIMIT = 10   # more transcript splits than this in one run needs a confirm
EXPECT = ".claude-sync-expect.json"
# Tests only: {"here": info overrides, "<host>": info overrides}. Both sides then run locally.
TEST_ROOTS = json.loads(os.environ.get("CLAUDE_SYNC_TEST_ROOTS", "null"))


class Stop(Exception):
    def __init__(self, code, result):
        self.code, self.result = code, result


def main(argv=None) -> int:
    a = parse(argv)
    try:
        try:
            return {"undo": undo, "unlock": unlock}.get(a.cmd, run)(a)
        except RemoteError as e:
            raise Stop(1, {"host": a.host, "error": str(e).strip()[-800:], "in_sync": False})
    except Stop as s:
        emit(a, s.result)
        return s.code


def parse(argv):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv and argv[0] in ("status", "undo", "unlock") else "sync"
    p = argparse.ArgumentParser(prog="claude-sync")
    p.add_argument("host")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--all", action="store_true", help="list every file, not the first 20 per list")
    p.add_argument("--confirm")
    p.add_argument("--keep", action="append", default=[])
    a = p.parse_args(argv)
    a.cmd = cmd
    return a


def runner(host):
    return Runner(None if TEST_ROOTS or host is None else host)


def info(r, name) -> dict:
    out = call_json(r, "info", {})
    return {**out, **TEST_ROOTS[name]} if TEST_ROOTS else out


def machine(name, inf):
    return Machine(name, inf["home"], inf["desktop"], inf["hostname"])


def roots(m: Machine) -> dict:
    return {"home": m.home, "desktop": m.desktop}


def call_json(r: Runner, cmd: str, args: dict, stdin: bytes = b"") -> dict:
    return json.loads(r.call(cmd, args, stdin))


# ---------- one run ----------

def run(a) -> int:
    here, there = runner(None), runner(a.host)
    ih = info(here, "here")
    try:
        it = info(there, a.host)
    except RemoteError as e:
        raise Stop(4, {"host": a.host, "error": f"not reachable: {str(e).strip()[-300:]}", "in_sync": False})
    mh, mt = machine("here", ih), machine(a.host, it)
    side = {"here": (here, mh), "there": (there, mt)}
    result = {"host": a.host, "in_sync": False, "stopped": [], "warnings": []}
    if ih.get("claude_version") != it.get("claude_version"):
        result["warnings"].append(f"Claude Code versions differ: {ih.get('claude_version')} here, "
                                  f"{it.get('claude_version')} on {a.host}")
    pre_stops = [r for r, bad in (("app_open", it.get("app_running")), ("session_open", it.get("cli_pids"))) if bad]
    applying = a.cmd == "sync" and not a.dry_run
    run_id = datetime.datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(2)
    locked = []
    try:
        if applying:
            locked = take_locks(here, there, mh, mt, run_id)
        state = newest_state(here, there, mh, mt)
        inv = inventories(side, state, result)
        opts = merge.Opts(here_host=mh.host, there_host=mt.host, now=int(time.time()),
                          first_run_cutoff={mh.host: FIRST_RUN_CUTOFF.get(ih["platform"], 0),
                                            mt.host: FIRST_RUN_CUTOFF.get(it["platform"], 0)},
                          confirm=a.confirm, keep=tuple(a.keep))
        plan = merge.plan(state["files"], inv["here"], inv["there"], opts)
        drop_suspect_deletes(plan, inv["warned"], result)
        resolved = resolve_transcripts(plan, side)
        splits = sorted(k for k, (w, _, _) in resolved.items() if w == "split")
        if len(splits) > SPLIT_LIMIT:
            plan.review["splits"] = [["both", k] for k in splits]
        restop(plan, a.confirm)
        describe(result, plan, a.all)
        result["splits"] = len(splits)
        result["stopped"] = pre_stops + plan.stops
        if result["stopped"]:
            raise Stop(3, result)
        group_plan = plan_groups(side, state, result)
        if not applying:
            result["in_sync"] = not plan.actions and not group_plan
            raise Stop(0 if result["in_sync"] or a.cmd == "sync" else 2, result)
        for r, m in side.values():
            r.call("state_write", roots(m), json.dumps({**state, "run_id": run_id}).encode())
        execute(plan, resolved, side, inv, state, run_id, ih, result)
        write_groups(group_plan, side, run_id, result)
        inv2 = inventories(side, state, {"warnings": []})
        new = next_state(state, inv2, mh, mt, run_id, result)
        for s in ("there", "here"):  # the other machine first, see the PRD shared-state rule
            r, m = side[s]
            r.call("state_write", roots(m), json.dumps(new).encode())
        for r, m in side.values():
            r.call("prune", {**roots(m), "days": 14})
        result["run_id"] = run_id
        result["in_sync"] = not any(w.startswith(("skipped", "error")) for w in result["warnings"])
        raise Stop(0, result)
    finally:
        for r, m in locked:
            r.call("lock_remove", roots(m))


def restop(plan, confirm):
    plan.token = merge.review_token(plan.review) if plan.review else ""
    plan.stops = [r for r in (*merge.STOP_ORDER, "splits") if r in plan.review and confirm != plan.token]


def drop_suspect_deletes(plan, warned, result):
    """A file missing from a side whose file list had warnings may only be unreadable there,
    so it is not deleted on the other side."""
    suspect = {"here": warned["there"], "there": warned["here"]}  # a delete on here means: missing there
    dropped = [a for a in plan.actions if a.op == "delete" and suspect[a.to]]
    if not dropped:
        return
    result["warnings"].append(f"{len(dropped)} deletions not done: the other file list had warnings")
    gone = {(a.to, a.key) for a in dropped}
    plan.actions = [a for a in plan.actions if a not in dropped]
    review = {r: [i for i in items if (i[0], i[1]) not in gone] for r, items in plan.review.items()}
    plan.review = {r: items for r, items in review.items() if items}


# ---------- locks and state ----------

def own_start() -> str:
    return subprocess.run(["ps", "-o", "lstart=", "-p", str(os.getpid())],
                          capture_output=True, text=True).stdout.strip()


def take_locks(here, there, mh, mt, run_id):
    lock = {"host": mh.host, "pid": os.getpid(), "start": own_start(), "run_id": run_id}
    by_host = {mh.host: (here, mh), mt.host: (there, mt)}
    locked = []
    try:
        for r, m in ((here, mh), (there, mt)):
            for _ in range(2):
                out = call_json(r, "lock_write", {**roots(m), "lock": lock})
                if out.get("ok", True):
                    locked.append((r, m))
                    break
                held = out["lock"]
                owner = by_host.get(held.get("host"))
                alive = owner is None or call_json(owner[0], "proc_alive", {
                    **roots(owner[1]), "pid": held["pid"], "start": held["start"]})["alive"]
                if alive:
                    raise Stop(5, {"error": f"another sync holds the lock on {m.name}: {held}", "in_sync": False})
                r.call("lock_remove", roots(m))
                print(f"warning: removed a stale lock on {m.name}: {held}", file=sys.stderr)
            else:
                raise Stop(5, {"error": f"could not take the lock on {m.name}", "in_sync": False})
    except BaseException:
        for r, m in locked:
            r.call("lock_remove", roots(m))
        raise
    return locked


def newest_state(here, there, mh, mt) -> dict:
    found = [call_json(r, "state_read", roots(m))["state"] for r, m in ((here, mh), (there, mt))]
    found = [s for s in found if s]
    if not found:
        return {"version": 1, "run_id": "", "files": {}, "folders": {}, "base": {}}
    return max(found, key=lambda s: s.get("run_id", ""))


def inventories(side, state, result) -> dict:
    """File lists of both sides with the same keys for the same folders."""
    mh, mt = side["here"][1], side["there"][1]
    prefixes = loose_prefixes(mh, mt)
    known = {nc for f in state["folders"].values() for nc in f.values()}
    out = {"warned": {}}
    raw = {}
    for s, (r, m) in side.items():
        cache = {k: [*e["stat"][m.host], e["hash"]] for k, e in state["files"].items() if m.host in e.get("stat", {})}
        given = {paths.folder_name(paths.localize(nc, m)): nc for nc in sorted(known)}
        given.update(state["folders"].get(m.host, {}))  # this machine's own last-sync cwd wins a name clash
        raw[s] = call_json(r, "inventory", {**roots(m), "folders": given, "prefixes": prefixes},
                           json.dumps(cache).encode())
        known |= set(raw[s].get("folders", {}).values())
        warns = raw[s].get("warnings", [])
        # A folder that had files at the last sync and has none now is suspect (moved, symlinked, reinstalled).
        for prefix, what in (("cli/projects/", "project files"), ("desktop/claude-code-sessions/", "desktop sessions")):
            if (any(k.startswith(prefix) for k in state["files"])
                    and not any(k.startswith(prefix) for k in raw[s]["files"])):
                warns = warns + [f"no {what} found"]
        result["warnings"] += [f"{m.name}: {w}" for w in warns]
        out["warned"][s] = bool(warns)
    for s, (r, m) in side.items():
        files = remap_slugs(raw[s]["files"], m, known)
        out[s] = {k: FileInfo(h, mt_, sz) for k, (h, mt_, sz) in files.items()}
        out["folders_" + s] = raw[s].get("folders", {})
    drop_clashes(out, side, result)
    return out


def loose_prefixes(mh, mt):
    """Both machines' folders for the loose hash. The migration replaced only the home part of
    the Mac desktop folder, so both desktop layouts are listed under each home."""
    layouts = ("/Library/Application Support/Claude", "/.config/Claude")
    return [[m.home, m.desktop] for m in (mh, mt)] + [[m.home, m.home + l] for m in (mh, mt) for l in layouts]


def remap_slugs(files: dict, m: Machine, known: set) -> dict:
    """A folder with only memory files gets a [slug] key. If a known cwd names the same
    folder, use the {cwd} key, so both machines agree."""
    by_folder, clash = {}, set()
    for nc in sorted(known):
        f = paths.folder_name(paths.localize(nc, m))
        if f in by_folder:
            clash.add(f)
        by_folder[f] = paths.localize(nc, m)
    for f in clash:  # two cwds give one folder name: do not guess
        del by_folder[f]
    out = {}
    for k, v in files.items():
        if k.startswith("cli/projects/["):
            local = paths.key_to_path(k, m)
            rel = os.path.relpath(local, f"{m.home}/.claude")
            folder = rel.split("/")[1]
            if folder in by_folder:
                k = paths.key_for("cli", rel, m, by_folder[folder]) or k
        out[k] = v
    return out


def drop_clashes(inv, side, result):
    """Two keys for one local file would overwrite each other: leave both out of this run."""
    for s, (_, m) in side.items():
        seen = {}
        for k in inv[s]:
            seen.setdefault(paths.key_to_path(k, m), []).append(k)
        for p, ks in seen.items():
            if len(ks) > 1:
                result["warnings"].append(f"{m.name}: skipped, one file has two keys: {ks}")
                for k in ks:
                    for t in ("here", "there"):
                        inv[t].pop(k, None)


def next_state(old, inv, mh, mt, run_id, result) -> dict:
    files, base = {}, dict(old.get("base", {}))
    keep_old = set(result.pop("_keep_old_state", []))
    ih, it = inv["here"], inv["there"]
    for k in set(ih) | set(it):
        h, t = ih.get(k), it.get(k)
        if k in keep_old or not (h and t and h.hash == t.hash):
            if k in old["files"]:
                files[k] = old["files"][k]
            continue
        files[k] = {"hash": h.hash, "stat": {mh.host: [h.mtime, h.size], mt.host: [t.mtime, t.size]}}
    for k, obj in result.pop("_base", {}).items():
        if k not in keep_old:
            base[k] = obj
    base = {k: v for k, v in base.items() if k in files}
    folders = {**old.get("folders", {}), mh.host: inv["folders_here"], mt.host: inv["folders_there"]}
    groups_base = result.pop("_groups_base", old.get("groups_base", {}))
    return {"version": 1, "run_id": run_id, "files": files, "folders": folders, "base": base,
            "groups_base": groups_base}


# ---------- helper transfers, in batches ----------

def chunks(keys):
    keys = list(keys)
    return [keys[i:i + BATCH] for i in range(0, len(keys), BATCH)]


def pack(r, m, keys) -> dict:
    """key -> (content, mtime, mode)"""
    out = {}
    for part in chunks(keys):
        out.update(unpack(r.call("pack", roots(m), json.dumps({"keys": part}).encode())))
    return out


def unpack(tar_bytes: bytes) -> dict:
    out = {}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r|gz") as tar:
        for member in tar:
            if member.isfile():
                out[member.name] = (tar.extractfile(member).read(), int(member.mtime), member.mode)
    return out


def apply(r, m, run_id, src_m, members: dict, inv_dst: dict, result):
    """Write members on m. A target that changed since the file list is skipped by the helper."""
    for part in chunks(members):
        expect = {k: ([inv_dst[k].mtime, inv_dst[k].size] if k in inv_dst else None)
                  for k in part if not k.startswith("conflicts/")}
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w|gz") as tar:
            add(tar, EXPECT, json.dumps(expect).encode(), 0, 0o600)
            for k in part:
                add(tar, k, *members[k])
        out = call_json(r, "apply", {**roots(m), "run_id": run_id, "src": roots(src_m)}, buf.getvalue())
        note(result, m, out)


def add(tar, name, data, mtime, mode=0o600):
    info = tarfile.TarInfo(name)
    info.size, info.mtime, info.mode = len(data), mtime, mode
    tar.addfile(info, io.BytesIO(data))


def delete(r, m, run_id, keys, inv_dst, result):
    for part in chunks(keys):
        expect = {k: [inv_dst[k].mtime, inv_dst[k].size] for k in part if k in inv_dst}
        out = call_json(r, "delete", {**roots(m), "run_id": run_id},
                        json.dumps({"keys": part, "expect": expect}).encode())
        note(result, m, out)


def note(result, m, out):
    result["warnings"] += [f"error on {m.name}: {e}" for e in out.get("errors", [])]
    result["warnings"] += [f"skipped on {m.name}, changed during the run: {k}" for k in out.get("skipped", [])]


# ---------- executing a plan ----------

def resolve_transcripts(plan, side) -> dict:
    """key -> (winner, here copy, there copy) for each transcript changed on both machines.
    Runs before any write, so status can show the splits."""
    keys = [a.key for a in plan.actions if a.op == "transcript"]
    if not keys:
        return {}
    (hr, mh), (tr, mt) = side["here"], side["there"]
    prefixes = loose_prefixes(mh, mt)
    h, t = pack(hr, mh, keys), pack(tr, mt, keys)
    out = {}
    for k in [k for k in keys if k in h and k in t]:
        winner = merge.resolve_transcript(paths.normalize_bytes(k, h[k][0], mh, prefixes),
                                          paths.normalize_bytes(k, t[k][0], mt, prefixes))
        out[k] = (winner, h[k], t[k])
    return out


def execute(plan, resolved, side, inv, state, run_id, ih, result):
    other = {"here": "there", "there": "here"}
    copies = {"here": [a.key for a in plan.actions if a.op == "copy" and a.to == "here"],
              "there": [a.key for a in plan.actions if a.op == "copy" and a.to == "there"]}
    base, keep_old = {}, []
    for key, (winner, h, t) in resolved.items():
        if winner == "split":
            split(key, t, side, inv, run_id, result)
            winner = "here"
        copies[other[winner]].append(key)
    both = [a.key for a in plan.actions if a.op in ("json_merge", "conflict")]
    got = {s: pack(*side[s], both) for s in side} if both else {}
    for a in [a for a in plan.actions if a.op in ("json_merge", "conflict")]:
        if a.key not in got["here"] or a.key not in got["there"]:
            result["warnings"].append(f"skipped, deleted during the run: {a.key}")
            continue
        h, t = got["here"][a.key], got["there"][a.key]
        if a.op == "conflict":
            # The newer copy wins, so a sync after undo cannot put an older copy back.
            winner, loser, loser_m = ("here", t, side["there"][1]) if h[1] >= t[1] else ("there", h, side["here"][1])
            copies[other[winner]].append(a.key)
            apply(*side["here"], run_id, loser_m, {"conflicts/" + a.key: loser}, {}, result)
            result.setdefault("conflicts", []).append(a.key)
            continue
        neutral = {s: json.loads(paths.normalize_bytes(a.key, v[a.key][0], side[s][1])) for s, v in got.items()}
        merged, over = merge.merge_json(state["base"].get(a.key), neutral["here"], neutral["there"],
                                        here_is_newer=h[1] >= t[1])
        member = {a.key: (json.dumps(merged, ensure_ascii=False, indent=2).encode(), max(h[1], t[1]), 0o600)}
        for s in ("there", "here"):
            apply(*side[s], run_id, NEUTRAL_M, member, inv[s], result)
        base[a.key] = merged
        if ih.get("app_running"):  # the app on this machine may write its old copy back
            keep_old.append(a.key)
        if over:
            result["warnings"].append(f"{a.key}: both machines changed {', '.join(over)}; the newer value won")
    for to in ("there", "here"):
        if not copies[to]:
            continue
        src_r, src_m = side[other[to]]
        members = pack(src_r, src_m, copies[to])
        for k, (data, _, _) in members.items():
            if paths.classify(k) in JSON_KINDS:
                base[k] = json.loads(paths.normalize_bytes(k, data, src_m))
                if to == "here" and ih.get("app_running"):
                    keep_old.append(k)
        apply(*side[to], run_id, src_m, members, inv[to], result)
    for to in ("here", "there"):
        keys = [a.key for a in plan.actions if a.op == "delete" and a.to == to]
        if keys:
            delete(*side[to], run_id, keys, inv[to], result)
    result["_base"], result["_keep_old_state"] = base, keep_old
    result["skipped_live"] = plan.skipped_live


def split(key, there_copy, side, inv, run_id, result):
    """Both machines continued one transcript: the other machine's copy becomes a new session,
    with its subagent files, rewind checkpoints and Code tab entry."""
    tr, mt = side["there"]
    old_id = os.path.basename(key)[:-len(".jsonl")]
    # Same copies give the same new id: a run that crashed after the split does not split again.
    new_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"claude-sync|{key}|{hashlib.sha256(there_copy[0]).hexdigest()}"))
    folder = key[:-len(old_id + ".jsonl")]
    members = {folder + new_id + ".jsonl": (merge.split_transcript(there_copy[0], old_id, new_id), *there_copy[1:])}
    extra = [k for k in inv["there"] if k.startswith((f"{folder}{old_id}/", f"cli/file-history/{old_id}/"))]
    for k, v in pack(tr, mt, extra).items():
        members[k.replace(f"/{old_id}/", f"/{new_id}/", 1)] = v
    sessions = [k for k in inv["there"] if paths.classify(k) == "session"]
    for k, (data, mtime, mode) in pack(tr, mt, sessions).items():
        obj = json.loads(data)
        if obj.get("cliSessionId") == old_id:
            obj.update(sessionId="local_" + str(uuid.uuid5(uuid.NAMESPACE_URL, new_id)), cliSessionId=new_id,
                       title=f"{obj.get('title', 'Session')} (from {mt.name})")
            members[k.rsplit("/", 1)[0] + "/" + obj["sessionId"] + ".json"] = (json.dumps(obj).encode(), mtime, mode)
    for s in ("there", "here"):  # {}: a split file that exists already may have been continued, so keep it
        apply(*side[s], run_id, mt, members, {}, result)
    result.setdefault("split_sessions", []).append({"from": key, "new": folder + new_id + ".jsonl"})


# ---------- sidebar groups ----------

def plan_groups(side, state, result):
    """None when both machines have the same groups. Otherwise the merged groups and each
    machine's config, for write_groups."""
    cfg = {}
    for s_, (r, m) in side.items():
        got = pack(r, m, [groups.KEY])
        if groups.KEY not in got:
            result["groups"] = f"no desktop config on {m.name}"
            return None
        cfg[s_] = (json.loads(got[groups.KEY][0]), got[groups.KEY][1], len(got[groups.KEY][0]))
    here, there = groups.subtree(cfg["here"][0]), groups.subtree(cfg["there"][0])
    if here == there:
        # The base moves only when both machines show the same groups: an open app can write its
        # old copy back after a sync, and then the next run applies the merge again.
        result["_groups_base"] = here
        result["groups"] = "in sync"
        return None
    merged = groups.merge(state.get("groups_base", {}), here, there, cfg["here"][1] >= cfg["there"][1])
    changed = [s_ for s_ in side if groups.subtree(cfg[s_][0]) != merged]
    result["groups"] = f"differ; the merge changes the groups on {', '.join(side[s_][1].name for s_ in changed)}"
    return {"merged": merged, "cfg": cfg, "changed": changed}


def write_groups(group_plan, side, run_id, result):
    if not group_plan:
        return
    for s_ in group_plan["changed"]:
        r, m = side[s_]
        cfg, mtime, size = group_plan["cfg"][s_]
        data = (json.dumps(groups.put(cfg, group_plan["merged"]), ensure_ascii=False, indent=2) + "\n").encode()
        # The machine's own config goes back to it, so src is that machine: nothing is mapped.
        apply(r, m, run_id, m, {groups.KEY: (data, int(time.time()), 0o600)},
              {groups.KEY: FileInfo("", mtime, size)}, result)
    result["groups"] = "merged; restart the desktop app to see them"


# ---------- output ----------

def describe(result, plan, show_all):
    result["files"] = {"to_here": sum(a.to == "here" and a.op == "copy" for a in plan.actions),
                       "to_host": sum(a.to == "there" and a.op == "copy" for a in plan.actions),
                       "deletions": sum(a.op == "delete" for a in plan.actions),
                       "both_changed": sum(a.op in ("transcript", "json_merge", "conflict") for a in plan.actions)}
    result["review"] = {r: cap(items, show_all) for r, items in plan.review.items()}
    if plan.review:
        result["token"] = plan.token
    result["skipped_live"] = plan.skipped_live
    if show_all or len(plan.actions) <= LIST_CAP:
        result["actions"] = [[a.op, a.to, a.key] for a in plan.actions]
    else:
        result["actions_by_folder"] = by_folder(plan.actions)


def cap(items, show_all):
    if show_all or len(items) <= LIST_CAP:
        return items
    return {"count": len(items), "first": items[:LIST_CAP], "by_folder": by_folder(items)}


def by_folder(items) -> dict:
    counts = {}
    for it in items:
        key = it.key if isinstance(it, Action) else it[1]
        folder = key.rsplit("/", 1)[0]
        counts[folder] = counts.get(folder, 0) + 1
    return counts


def emit(a, result):
    if getattr(a, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return
    if "error" in result:
        print("error:", result["error"])
        return
    for w in result.get("warnings", []):
        print("warning:", w)
    f = result.get("files", {})
    if f:
        print(f"files: {f['to_here']} to here, {f['to_host']} to {result['host']}, "
              f"{f['deletions']} deletions, {f['both_changed']} changed on both machines")
    for reason in result.get("stopped", []):
        items = result.get("review", {}).get(reason)
        print(f"stopped: {reason}" + (f" ({items if isinstance(items, dict) else len(items)} items)" if items else ""))
    if result.get("token"):
        print("confirm token:", result["token"])
    for c in result.get("conflicts", []):
        print("conflict:", c)
    print("in sync." if result.get("in_sync") else "not in sync.")


# ---------- undo and unlock ----------

def undo(a) -> int:
    here, there = runner(None), runner(a.host)
    ms = {"here": (here, machine("here", info(here, "here"))), a.host: (there, machine(a.host, info(there, a.host)))}
    state = newest_state(here, there, ms["here"][1], ms[a.host][1])
    if not state["run_id"]:
        raise Stop(1, {"host": a.host, "error": "no run to undo", "in_sync": False})
    out = {name: call_json(r, "undo", {**roots(m), "run_id": state["run_id"]}) for name, (r, m) in ms.items()}
    # The undone keys leave the state: the next run then sees two different copies with no
    # last-sync entry and merges them as a conflict, not as a one-sided change.
    undone = {k for o in out.values() for k in o.get("keys", [])}
    state["files"] = {k: v for k, v in state["files"].items() if k not in undone}
    state["base"] = {k: v for k, v in state.get("base", {}).items() if k not in undone}
    for r, m in ms.values():
        r.call("state_write", roots(m), json.dumps(state).encode())
    emit(a, {"host": a.host, "run_id": state["run_id"], "undo": out, "in_sync": False})
    return 0


def unlock(a) -> int:
    for name, r in (("here", runner(None)), (a.host, runner(a.host))):
        r.call("lock_remove", roots(machine(name, info(r, name))))
    emit(a, {"host": a.host, "unlocked": True, "in_sync": False})
    return 0


if __name__ == "__main__":
    sys.exit(main())
