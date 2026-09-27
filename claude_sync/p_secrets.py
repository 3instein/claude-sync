"""Secrets report part: never syncs a secret file, only tells the user what to scp
by hand. See docs/contract.md, Phase 2, `secrets`."""
import json
import shlex

from claude_sync import paths

NAME = "secrets"


def _scan(r, m):
    out = json.loads(r.call("secrets_scan", {"home": m.home, "desktop": m.desktop}))
    return out["secrets"]


def _scp(host, npath, mh, mt, pull):
    """scp command for one neutral path: pull copies there's copy to here, else the reverse."""
    here_path, there_path = paths.localize(npath, mh), paths.localize(npath, mt)
    remote = f"{host}:{shlex.quote(there_path)}"
    local = shlex.quote(here_path)
    return f"scp {remote} {local}" if pull else f"scp {local} {remote}"


def plan(ctx):
    mh, mt = ctx.side["here"][1], ctx.side["there"][1]
    here = _scan(ctx.side["here"][0], mh)
    there = _scan(ctx.side["there"][0], mt)
    host = mt.name

    missing_here = sorted(set(there) - set(here))
    missing_there = sorted(set(here) - set(there))
    different = sorted(p for p in set(here) & set(there) if here[p] != there[p])

    next_cmds = {}
    for p in missing_here:
        next_cmds[p] = _scp(host, p, mh, mt, pull=True)
    for p in missing_there:
        next_cmds[p] = _scp(host, p, mh, mt, pull=False)
    for p in different:
        # No mtime in the scan (only a hash), so there is no signal for which copy is
        # newer; default to pulling the other machine's copy here for a look.
        next_cmds[p] = _scp(host, p, mh, mt, pull=True)

    ctx.result["parts"][NAME] = {
        "missing_here": missing_here,
        "missing_there": missing_there,
        "different": different,
        "next": next_cmds,
    }
    return None  # report only: never a stop, never part of in_sync


def apply(ctx, plan):
    pass  # report only, nothing to write
