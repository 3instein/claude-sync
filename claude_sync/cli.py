"""Command line: status, sync, dry run, undo, unlock. Joins helper, merge and paths.
See docs/contract.md and the PRD."""
import argparse
import datetime
import io
import json
import os
import secrets
import subprocess
import sys
import tarfile
import time
import uuid

from claude_sync import merge, paths
from claude_sync.model import Action, FileInfo, Machine
from claude_sync.remote import RemoteError, Runner

UTC = datetime.timezone.utc
# ponytail: the one migration this tool knows, 2026-09-23 22:22:30/31 WIB; a new pair of machines needs its own values.
FIRST_RUN_CUTOFF = {
    "Darwin": int(datetime.datetime(2026, 9, 23, 15, 22, 30, tzinfo=UTC).timestamp()),
    "Linux": int(datetime.datetime(2026, 9, 23, 15, 22, 31, tzinfo=UTC).timestamp()),
}
NEUTRAL = {"home": "~", "desktop": "{desktop}"}  # src for content that is already in neutral form
NEUTRAL_M = Machine("neutral", "~", "{desktop}")
JSON_KINDS = ("session", "json")
# Tests only: {"here": info overrides, "<host>": info overrides}. Both sides then run locally.
TEST_ROOTS = json.loads(os.environ.get("CLAUDE_SYNC_TEST_ROOTS", "null"))
LIST_CAP = 20


class Stop(Exception):
    def __init__(self, code, result):
        self.code, self.result = code, result


def main(argv=None) -> int:
    a = parse(argv)
    try:
        if a.cmd == "undo":
            return undo(a)
        if a.cmd == "unlock":
            return unlock(a)
        return run(a)
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


# ---------- one run ----------

def runner(host):
    return Runner(None if TEST_ROOTS or host is None else host)


def info(r, name) -> dict:
    out = call_json(r, "info", {})
    return {**out, **TEST_ROOTS[name]} if TEST_ROOTS else out


def run(a) -> int:
    here, there = runner(None), runner(a.host)
    ih = info(here, "here")
    try:
        it = info(there, a.host)
    except RemoteError as e:
        raise Stop(4, {"host": a.host, "error": f"not reachable: {str(e).strip()[-300:]}"})
    mh, mt = machine("here", ih), machine(a.host, it)
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
        inv_h, fold_h, warn_h = inventory(here, mh, state)
        inv_t, fold_t, warn_t = inventory(there, mt, state)
        result["warnings"] += warn_h + [f"{a.host}: {w}" for w in warn_t]
        opts = merge.Opts(here_host=mh.host, there_host=mt.host, now=int(time.time()),
                          first_run_cutoff={mh.host: FIRST_RUN_CUTOFF.get(ih["platform"], 0),
                                            mt.host: FIRST_RUN_CUTOFF.get(it["platform"], 0)},
                          confirm=a.confirm, keep=tuple(a.keep))
        plan = merge.plan(state["files"], inv_h, inv_t, opts)
        plan = drop_suspect_deletes(plan, bool(warn_h), bool(warn_t), a.confirm, result)
        describe(result, plan, a.all)
        result["stopped"] = pre_stops + plan.stops
        if result["stopped"]:
            raise Stop(3, result)
        if not applying:
            result["in_sync"] = not plan.actions
            raise Stop(0 if result["in_sync"] or a.cmd == "sync" else 2, result)
        execute(plan, here, there, mh, mt, state, run_id, ih, result)
        inv_h, fold_h, _ = inventory(here, mh, state)
        inv_t, fold_t, _ = inventory(there, mt, state)
        new = next_state(state, inv_h, inv_t, mh, mt, fold_h, fold_t, run_id, result)
        for r in (there, here):  # the other machine first, see the PRD shared-state rule
            r.call("state_write", roots(r is here and mh or mt), json.dumps(new).encode())
        for r, m in ((here, mh), (there, mt)):
            r.call("prune", {**roots(m), "days": 14})
        result["run_id"] = run_id
        result["in_sync"] = True
        raise Stop(0, result)
    finally:
        for r, m in locked:
            r.call("lock_remove", roots(m))


def drop_suspect_deletes(plan, warn_here, warn_there, confirm, result):
    """A file missing from a side whose file list had warnings may only be unreadable there,
    so it is not deleted on the other side."""
    suspect = {"here": warn_there, "there": warn_here}  # a delete on here means: missing there
    dropped = [a for a in plan.actions if a.op == "delete" and suspect[a.to]]
    if not dropped:
        return plan
    result["warnings"].append(f"{len(dropped)} deletions skipped: the other file list had warnings")
    gone = {(a.to, a.key) for a in dropped}
    plan.actions = [a for a in plan.actions if a not in dropped]
    review = {r: [i for i in items if (i[0], i[1]) not in gone] for r, items in plan.review.items()}
    plan.review = {r: items for r, items in review.items() if items}
    plan.token = merge.review_token(plan.review)
    plan.stops = [r for r in merge.STOP_ORDER if r in plan.review and confirm != plan.token]
    return plan


def machine(name, info):
    return Machine(name, info["home"], info["desktop"], info["hostname"])


def roots(m: Machine) -> dict:
    return {"home": m.home, "desktop": m.desktop}


def call_json(r: Runner, cmd: str, args: dict, stdin: bytes = b"") -> dict:
    return json.loads(r.call(cmd, args, stdin))


# ---------- locks and state ----------

def own_start() -> str:
    return subprocess.run(["ps", "-o", "lstart=", "-p", str(os.getpid())],
                          capture_output=True, text=True).stdout.strip()


def take_locks(here, there, mh, mt, run_id):
    lock = {"host": mh.host, "pid": os.getpid(), "start": own_start(), "run_id": run_id}
    by_host = {mh.host: (here, mh), mt.host: (there, mt)}
    for r, m in ((here, mh), (there, mt)):
        held = call_json(r, "lock_read", roots(m))["lock"]
        if not held:
            continue
        owner = by_host.get(held.get("host"))
        alive = owner is None or call_json(owner[0], "proc_alive",
                                           {**roots(owner[1]), "pid": held["pid"], "start": held["start"]})["alive"]
        if alive:
            raise Stop(5, {"error": f"another sync holds the lock on {m.name}: {held}"})
        r.call("lock_remove", roots(m))
        print(f"warning: removed a stale lock on {m.name}: {held}", file=sys.stderr)
    locked = []
    for r, m in ((here, mh), (there, mt)):
        r.call("lock_write", {**roots(m), "lock": lock})
        locked.append((r, m))
    return locked


def newest_state(here, there, mh, mt) -> dict:
    found = [call_json(r, "state_read", roots(m))["state"] for r, m in ((here, mh), (there, mt))]
    found = [s for s in found if s]
    if not found:
        return {"version": 1, "run_id": "", "files": {}, "folders": {}, "base": {}}
    return max(found, key=lambda s: s.get("run_id", ""))


def inventory(r, m, state):
    cache = {k: [*e["stat"][m.host], e["hash"]] for k, e in state["files"].items() if m.host in e.get("stat", {})}
    out = call_json(r, "inventory", {**roots(m), "folders": state["folders"].get(m.host, {})},
                    json.dumps(cache).encode())
    inv = {k: FileInfo(h, mt, sz) for k, (h, mt, sz) in out["files"].items()}
    return inv, out.get("folders", {}), out.get("warnings", [])


def next_state(old, inv_h, inv_t, mh, mt, fold_h, fold_t, run_id, result) -> dict:
    files, base = {}, dict(old.get("base", {}))
    keep_old = set(result.get("_keep_old_state", []))
    for k in set(inv_h) | set(inv_t):
        h, t = inv_h.get(k), inv_t.get(k)
        if k in keep_old or not (h and t and h.hash == t.hash):
            if k in old["files"]:
                files[k] = old["files"][k]
            continue
        files[k] = {"hash": h.hash, "stat": {mh.host: [h.mtime, h.size], mt.host: [t.mtime, t.size]}}
    for k, obj in result.pop("_base", {}).items():
        if k not in keep_old:
            base[k] = obj
    base = {k: v for k, v in base.items() if k in files}
    result.pop("_keep_old_state", None)
    return {"version": 1, "run_id": run_id, "files": files,
            "folders": {**old.get("folders", {}), mh.host: fold_h, mt.host: fold_t}, "base": base}


# ---------- executing a plan ----------

def execute(plan, here, there, mh, mt, state, run_id, ih, result):
    side = {"here": (here, mh), "there": (there, mt)}
    other = {"here": "there", "there": "here"}
    copies = {"here": [], "there": []}
    base, keep_old = {}, []
    for act in plan.actions:
        if act.op == "copy":
            copies[act.to].append(act.key)
    for a in [a for a in plan.actions if a.op in ("transcript", "json_merge", "conflict")]:
        both = {s: unpack(side[s][0].call("pack", {**roots(side[s][1]), "keys": [a.key]})) for s in side}
        h, t = both["here"][a.key], both["there"][a.key]
        if a.op == "transcript":
            winner = merge.resolve_transcript(paths.normalize_bytes(a.key, h[0], mh),
                                              paths.normalize_bytes(a.key, t[0], mt))
            if winner == "split":
                split(a.key, t, here, there, mh, mt, run_id, result)
                winner = "here"
            copies[other[winner]].append(a.key)
        elif a.op == "conflict":
            copies["there"].append(a.key)
            send(here, mh, run_id, mt, {"conflicts/" + a.key: t})
            result.setdefault("conflicts", []).append(a.key)
        else:
            neutral = {s: json.loads(paths.normalize_bytes(a.key, v[a.key][0], side[s][1])) for s, v in both.items()}
            merged, over = merge.merge_json(state["base"].get(a.key), neutral["here"], neutral["there"],
                                            here_is_newer=h[1] >= t[1])
            data = json.dumps(merged, ensure_ascii=False, indent=2).encode()
            member = {a.key: (data, max(h[1], t[1]))}
            for s in ("there", "here"):
                send(side[s][0], side[s][1], run_id, NEUTRAL_M, member)
            base[a.key] = merged
            if ih.get("app_running"):  # the app on this machine may write its old copy back
                keep_old.append(a.key)
            if over:
                result["warnings"].append(f"{a.key}: both machines changed {', '.join(over)}; the newer value won")
    for to in ("there", "here"):
        keys = copies[to]
        if not keys:
            continue
        src_r, src_m = side[other[to]]
        tar = src_r.call("pack", {**roots(src_m), "keys": keys})
        for k, (data, _) in unpack(tar).items():
            if paths.classify(k) in JSON_KINDS:
                base[k] = json.loads(paths.normalize_bytes(k, data, src_m))
                if to == "here" and ih.get("app_running"):
                    keep_old.append(k)
        dst_r, dst_m = side[to]
        errors = call_json(dst_r, "apply", {**roots(dst_m), "run_id": run_id, "src": roots(src_m)}, tar)["errors"]
        result["warnings"] += [f"{dst_m.name}: {e}" for e in errors]
    for to in ("here", "there"):
        keys = [a.key for a in plan.actions if a.op == "delete" and a.to == to]
        if keys:
            r, m = side[to]
            result["warnings"] += call_json(r, "delete", {**roots(m), "run_id": run_id, "keys": keys})["errors"]
    result["_base"], result["_keep_old_state"] = base, keep_old
    result["skipped_live"] = plan.skipped_live


def split(key, there_copy, here, there, mh, mt, run_id, result):
    """Both machines continued one transcript: the other machine's copy becomes a new session."""
    old_id = os.path.basename(key)[:-len(".jsonl")]
    new_id = str(uuid.uuid4())
    new_key = key[: -len(old_id + ".jsonl")] + new_id + ".jsonl"
    data = merge.split_transcript(there_copy[0], old_id, new_id)
    members = {new_key: (data, there_copy[1])}
    history = [k for k in call_json(there, "inventory", {**roots(mt), "folders": {}})["files"]
               if k.startswith(f"cli/file-history/{old_id}/")]
    if history:
        for k, v in unpack(there.call("pack", {**roots(mt), "keys": history})).items():
            members[k.replace(f"/{old_id}/", f"/{new_id}/", 1)] = v
    for r, m in ((there, mt), (here, mh)):
        send(r, m, run_id, mt, members)
    result.setdefault("split_sessions", []).append({"from": key, "new": new_key})


def unpack(tar_bytes: bytes) -> dict:
    """key -> (content, mtime)"""
    out = {}
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r|gz") as tar:
        for member in tar:
            if member.isfile():
                out[member.name] = (tar.extractfile(member).read(), int(member.mtime))
    return out


def send(r, m, run_id, src_m, members: dict):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w|gz") as tar:
        for name, (data, mtime) in members.items():
            info = tarfile.TarInfo(name)
            info.size, info.mtime = len(data), mtime
            tar.addfile(info, io.BytesIO(data))
    r.call("apply", {**roots(m), "run_id": run_id, "src": roots(src_m)}, buf.getvalue())


# ---------- output ----------

def describe(result, plan, show_all):
    to_here = sum(a.to == "here" and a.op == "copy" for a in plan.actions)
    to_host = sum(a.to == "there" and a.op == "copy" for a in plan.actions)
    result["files"] = {"to_here": to_here, "to_host": to_host,
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
    out = {}
    for name, r in (("here", runner(None)), (a.host, runner(a.host))):
        out[name] = call_json(r, "undo", {**roots(machine(name, info(r, name))), "run_id": None})
    # ponytail: the state keeps the undone run; the next run sees equal hashes and records them.
    emit(a, {"host": a.host, "undo": out, "in_sync": False})
    return 0


def unlock(a) -> int:
    for name, r in (("here", runner(None)), (a.host, runner(a.host))):
        r.call("lock_remove", roots(machine(name, info(r, name))))
    emit(a, {"host": a.host, "unlocked": True, "in_sync": False})
    return 0


if __name__ == "__main__":
    sys.exit(main())
