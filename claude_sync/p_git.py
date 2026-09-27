"""Git check part: read only. Reports repos and worktrees under ~/dev that need a decision
or a fast-forward pull. See docs/contract.md, the git row and the Git check table.
apply() does nothing: the skill tells Claude to run each reported `next` command."""
import json
import shlex

from claude_sync import paths

NAME = "git"


def _scan(ctx, side):
    r, m = ctx.side[side]
    out = json.loads(r.call("git_scan", {"home": m.home, "desktop": m.desktop}))
    return {paths.neutral(rec["path"], m): rec for rec in out.get("repos", [])}


def _skipped(neutral_path, mh, mt, skip_repos):
    if not skip_repos:
        return False
    candidates = {neutral_path, paths.localize(neutral_path, mh), paths.localize(neutral_path, mt)}
    return any(c in candidates for c in skip_repos)


def _next_cmd(cmd, side, host):
    """rule: every value is shlex.quote-d; a command for the other machine is
    `ssh HOST ` + shlex.quote("bash -ic " + shlex.quote(git command))."""
    if side == "here":
        return cmd
    return f"ssh {host} " + shlex.quote("bash -ic " + shlex.quote(cmd))


def _warn_failures(rec, m, ctx):
    for warning in (rec.get("fetch_warning"), rec.get("status_warning")):
        if warning:
            ctx.result["warnings"].append(f"error: {m.name}: {rec['path']}: {warning}")


def _classify(rec, side, m, ctx):
    _warn_failures(rec, m, ctx)
    if rec["kind"] == "worktree":
        # Fix round: never report a worktree as anything but an unpushed branch. A
        # worktree present on only one machine is never "missing" either (below).
        return None if rec["upstream"] else {"state": "unpushed_branch"}
    if rec["changed"] or rec["untracked"]:
        files = sorted(rec["changed"] + rec["untracked"])[:20]
        return {"state": "uncommitted", "files": files}
    if not rec["origin"]:
        return {"state": "no_remote"}
    if not rec["upstream"]:
        return {"state": "unpushed_branch"}
    ahead, behind = rec["ahead"], rec["behind"]
    if ahead and behind:
        return {"state": "diverged"}
    if ahead:
        cmd = f"git -C {shlex.quote(rec['path'])} push"
        return {"state": "ahead", "commits": ahead, "next": _next_cmd(cmd, side, m.name)}
    if behind:
        cmd = f"git -C {shlex.quote(rec['path'])} pull --ff-only"
        return {"state": "behind", "next": _next_cmd(cmd, side, m.name)}
    return None  # clean


def _missing_repo_entry(neutral_path, here_rec, there_rec, mh, mt, ctx):
    have_side, rec = ("here", here_rec) if here_rec else ("there", there_rec)
    miss_side = "there" if have_side == "here" else "here"
    m_miss = mt if miss_side == "there" else mh
    origin = rec["origin"]
    entry = {"machine": m_miss.name, "repo": neutral_path, "state": "missing"}
    if origin and origin.startswith("-"):
        # rule: a value from the other machine can never be read as an option; refuse to
        # build a command from one that looks like a flag instead of guessing its intent.
        ctx.result["warnings"].append(f"skipped: clone for {neutral_path}: origin looks unsafe ({origin!r})")
    elif origin:
        target = paths.localize(neutral_path, m_miss)
        cmd = f"git clone -- {shlex.quote(origin)} {shlex.quote(target)}"
        entry["next"] = _next_cmd(cmd, miss_side, m_miss.name)
    else:  # nothing can be cloned, so a stop could never clear: warn only, as for no remote
        ctx.result["warnings"].append(f"{neutral_path}: only on one machine, and it has no remote to clone")
    return entry, "next" in entry


def plan(ctx):
    mh, mt = ctx.side["here"][1], ctx.side["there"][1]
    scans = {"here": _scan(ctx, "here"), "there": _scan(ctx, "there")}
    reports, need_stop = [], False
    for neutral_path in sorted(set(scans["here"]) | set(scans["there"])):
        if _skipped(neutral_path, mh, mt, ctx.skip_repos):
            continue
        here_rec, there_rec = scans["here"].get(neutral_path), scans["there"].get(neutral_path)
        kind = (here_rec or there_rec)["kind"]
        if kind == "repo" and (here_rec is None or there_rec is None):
            entry, has_next = _missing_repo_entry(neutral_path, here_rec, there_rec, mh, mt, ctx)
            reports.append(entry)
            need_stop = need_stop or has_next
            continue
        # A worktree missing on one machine is never reported at all (fix round): only
        # classify the sides that actually have it.
        sides = [(s, r) for s, r in (("here", here_rec), ("there", there_rec)) if r is not None]
        for side, rec in sides:
            m = mh if side == "here" else mt
            entry = _classify(rec, side, m, ctx)
            if entry is None:
                continue
            entry.update(machine=m.name, repo=neutral_path)
            reports.append(entry)
            if entry["state"] not in ("behind", "no_remote"):
                need_stop = True
    ctx.result["parts"]["git"] = reports
    if need_stop:
        ctx.stops.append("git")
    # Only a stop or a pending pull keeps the machines out of sync; warnings alone do not.
    return reports if need_stop or any("next" in r for r in reports) else None


def apply(ctx, plan):
    pass  # read only: the skill runs the reported `next` commands, never this tool.
