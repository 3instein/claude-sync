"""Plugins part: three-way merge of user-scope installed plugins and known marketplaces.
See docs/contract.md, the plugins row. plan() only reads; apply() runs the claude CLI."""
import json

from claude_sync.merge import merge_json

NAME = "plugins"


def _call(ctx, side, cmd, args):
    r, m = ctx.side[side]
    return json.loads(r.call(cmd, {"home": m.home, "desktop": m.desktop, **args}))


def _read(ctx, side):
    return _call(ctx, side, "plugins_read", {})


def _diff(current: dict, merged: dict):
    """Presence only: install/add what merged has and current lacks, drop what current has
    and merged lacks. ponytail: a marketplace whose source changes on both machines keeps
    one machine's old source; `claude plugin marketplace` has no update verb to fix that in
    one step, and a source changing at all is rare."""
    return [k for k in merged if k not in current], [k for k in current if k not in merged]


def plan(ctx):
    here, there = _read(ctx, "here"), _read(ctx, "there")
    errors = [(side, rec["error"]) for side, rec in (("here", here), ("there", there)) if rec.get("error")]
    if errors:
        # An unreadable or unversioned plugin file must not read as "no plugins installed",
        # or a plan would uninstall everything the state remembers. Plan nothing instead.
        for side, err in errors:
            ctx.result["warnings"].append(f"error: plugins on {ctx.side[side][1].name}: {err}")
        ctx.result["parts"]["plugins"] = {}
        return None
    if here == there:
        # Both machines already agree: safe to move the base, the same rule as groups.
        # Without this, a run that finds nothing to do never learns this shared state,
        # and the next real change looks like a first run again (nothing to remove).
        ctx.part_state["plugins"] = here
        ctx.result["parts"]["plugins"] = {}
        return None
    old = ctx.state.get("parts", {}).get("plugins") or {}
    base_mkts = old.get("marketplaces", {})
    base_installed = {i: True for i in old.get("installed", [])}
    # ponytail: here_is_newer=True is a fixed tie-break for the one case merge_json needs
    # it (the same marketplace given a different source on both machines); there is no
    # mtime for a plugin list to compare, and that case should be rare.
    merged_mkts, _ = merge_json(base_mkts, here["marketplaces"], there["marketplaces"], True)
    merged_installed, _ = merge_json(base_installed, {i: True for i in here["installed"]},
                                     {i: True for i in there["installed"]}, True)
    changes = {}
    for side, rec in (("here", here), ("there", there)):
        mkt_add, mkt_remove = _diff(rec["marketplaces"], merged_mkts)
        pl_add, pl_remove = _diff({i: True for i in rec["installed"]}, merged_installed)
        if not (mkt_add or mkt_remove or pl_add or pl_remove):
            continue
        changes[side] = {"marketplace_add": [[n, merged_mkts[n]] for n in sorted(mkt_add)],
                         "marketplace_remove": sorted(mkt_remove),
                         "install": sorted(pl_add), "uninstall": sorted(pl_remove)}
        for name in sorted(mkt_add + mkt_remove):
            ctx.review.setdefault("exec_config", []).append([side, f"marketplace:{name}"])
        for pid in sorted(pl_add + pl_remove):
            ctx.review.setdefault("exec_config", []).append([side, f"plugin:{pid}"])
    ctx.result["parts"]["plugins"] = changes
    return changes or None


def _run(ctx, side, argv):
    r, m = ctx.side[side]
    out = json.loads(r.call("plugins_run", {"home": m.home, "desktop": m.desktop, "argv": argv}))
    if out.get("code", 1) != 0:
        shown = " ".join(a for a in argv if a != "--")  # "--" is real but noisy to read back
        ctx.result["warnings"].append(f"error: claude plugin {shown} failed on {m.name}: {out.get('output', '')}")


def apply(ctx, plan):
    for side, c in plan.items():
        for name, source in c["marketplace_add"]:
            _run(ctx, side, ["marketplace", "add", "--", source])
        for pid in c["install"]:
            _run(ctx, side, ["install", "--", pid])
        for pid in c["uninstall"]:
            _run(ctx, side, ["uninstall", "--", pid])
        for name in c["marketplace_remove"]:
            _run(ctx, side, ["marketplace", "remove", "--", name])
    here2, there2 = _read(ctx, "here"), _read(ctx, "there")
    if not here2.get("error") and not there2.get("error") \
            and sorted(here2["installed"]) == sorted(there2["installed"]) \
            and here2["marketplaces"] == there2["marketplaces"]:
        ctx.part_state["plugins"] = {"installed": sorted(here2["installed"]), "marketplaces": here2["marketplaces"]}
    # else: leave ctx.part_state untouched, so the old state (or none) survives, per contract.
